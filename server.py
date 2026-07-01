"""MCP server that exposes local Ollama as tools for Claude Code.

Goal: let Claude offload bulky/cheap text work (summarize, extract, classify,
grep-and-explain large files) to a local model so the bulk text never enters
Claude's context window. Only the compact result comes back.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# httpx logs every request at INFO; that noise leaks into the CLI stdout/stderr.
logging.getLogger("httpx").setLevel(logging.WARNING)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.environ.get("OLLAMA_DEFAULT_MODEL", "llama3.2:latest")
CODE_MODEL = os.environ.get("OLLAMA_CODE_MODEL", "qwen2.5-coder:7b")
PTBR_MODEL = os.environ.get("OLLAMA_PTBR_MODEL", "hf.co/cnmoro/Gemma-3-Gaia-PT-BR-4b-it-Q8_0-GGUF:latest")
EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text:latest")
# Code/semantic index model. nomic-embed-text is ~3x faster than bge-m3 on this
# hardware (the model is compute-bound and Ollama serializes inference, so
# threading doesn't help — model choice is the lever). bge-m3 is stronger on
# pure multilingual prose; set OLLAMA_CODE_EMBED_MODEL=bge-m3:latest to switch
# back (requires a rebuild — the index pins its model).
CODE_EMBED_MODEL = os.environ.get("OLLAMA_CODE_EMBED_MODEL", "nomic-embed-text:latest")
EMBED_BATCH = int(os.environ.get("OLLAMA_EMBED_BATCH", "64"))
INDEX_FILENAME = "ragcode-index.sqlite"
# Older index filenames we still read from and migrate into the canonical name
# (so an existing index survives the rename without a rebuild).
LEGACY_INDEX_NAMES = [".ollama-mcp-index.sqlite"]
# Canonical location: every project has a .git/ — keep the index there so it
# stays out of source trees and is trivially gitignored once.
INDEX_SUBDIR = ".git"

MAX_INPUT_CHARS = 200_000

mcp = FastMCP("ragcode")


def _generate(model: str, prompt: str, system: str | None = None, num_predict: int = 512) -> str:
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": 0.2},
    }
    if system:
        payload["system"] = system
    with httpx.Client(timeout=300.0) as client:
        r = client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return r.json().get("response", "").strip()


def _embed(texts: list[str], model: str | None = None) -> list[list[float]]:
    """Embed a list of texts. Uses Ollama's batch `/api/embed` endpoint (one
    request per EMBED_BATCH texts) instead of one request per text — the index
    builds several times faster. Falls back to the single-input `/api/embeddings`
    endpoint per item if a batch response comes back malformed."""
    if not texts:
        return []
    chosen = model or EMBED_MODEL
    out: list[list[float]] = []
    with httpx.Client(timeout=300.0) as client:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i:i + EMBED_BATCH]
            try:
                r = client.post(
                    f"{OLLAMA_URL}/api/embed",
                    json={"model": chosen, "input": batch},
                )
                r.raise_for_status()
                embs = r.json().get("embeddings")
            except httpx.HTTPError:
                embs = None
            if isinstance(embs, list) and len(embs) == len(batch):
                out.extend(embs)
                continue
            # fallback: legacy single-input endpoint, one call per text
            for t in batch:
                rr = client.post(
                    f"{OLLAMA_URL}/api/embeddings",
                    json={"model": chosen, "prompt": t},
                )
                rr.raise_for_status()
                out.append(rr.json()["embedding"])
    return out


def _read_input(text: str | None, file_path: str | None) -> str:
    if file_path:
        p = Path(file_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"not found: {file_path}")
        data = p.read_text(encoding="utf-8", errors="replace")
    elif text is not None:
        data = text
    else:
        raise ValueError("provide either text or file_path")
    if len(data) > MAX_INPUT_CHARS:
        data = data[:MAX_INPUT_CHARS] + f"\n\n[truncated at {MAX_INPUT_CHARS} chars]"
    return data


@mcp.tool()
def ollama_summarize(
    text: str | None = None,
    file_path: str | None = None,
    max_lines: int = 10,
    focus: str | None = None,
    model: str | None = None,
) -> str:
    """Summarize text or a file using a local model. Returns only the summary.

    Use this when you want to digest a large file/log/document without pulling
    all of it into context. Pass either `text` or `file_path`. `focus` narrows
    what to extract (e.g. "errors and stack traces only").
    """
    content = _read_input(text, file_path)
    system = (
        f"You are a precise summarizer. Output at most {max_lines} lines. "
        "No preamble, no closing remarks. Plain text only."
    )
    instruction = f"Summarize the content below"
    if focus:
        instruction += f", focusing on: {focus}"
    instruction += f".\n\n---\n{content}"
    return _generate(model or DEFAULT_MODEL, instruction, system=system, num_predict=max_lines * 80)


@mcp.tool()
def ollama_extract(
    schema: str,
    text: str | None = None,
    file_path: str | None = None,
    model: str | None = None,
) -> str:
    """Extract structured fields from text/file. `schema` describes what to pull
    (e.g. "JSON with keys: error_type, file, line, message"). Returns the raw
    model output."""
    content = _read_input(text, file_path)
    system = "You extract structured data. Output only the requested format. No prose."
    prompt = f"Schema:\n{schema}\n\nContent:\n{content}"
    return _generate(model or DEFAULT_MODEL, prompt, system=system, num_predict=1024)


@mcp.tool()
def ollama_classify(
    text: str,
    labels: list[str],
    model: str | None = None,
) -> str:
    """Classify a short text into one of `labels`. Returns just the label."""
    system = "You are a classifier. Reply with exactly one of the given labels and nothing else."
    prompt = f"Labels: {', '.join(labels)}\n\nText:\n{text}\n\nLabel:"
    return _generate(model or DEFAULT_MODEL, prompt, system=system, num_predict=32)


@mcp.tool()
def ollama_grep_explain(
    pattern: str,
    file_path: str,
    context_lines: int = 2,
    model: str | None = None,
) -> str:
    """Find lines matching `pattern` in a file and ask the local model to
    explain what they collectively mean. Useful for scanning a log for a
    behavior pattern without sending the full log to Claude."""
    import re

    p = Path(file_path).expanduser()
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    rx = re.compile(pattern)
    hits: list[str] = []
    for i, line in enumerate(lines):
        if rx.search(line):
            lo, hi = max(0, i - context_lines), min(len(lines), i + context_lines + 1)
            chunk = "\n".join(f"{j+1}: {lines[j]}" for j in range(lo, hi))
            hits.append(chunk)
            if len(hits) >= 50:
                break
    if not hits:
        return f"no matches for /{pattern}/ in {file_path}"
    body = "\n---\n".join(hits)
    if len(body) > MAX_INPUT_CHARS:
        body = body[:MAX_INPUT_CHARS]
    system = "You analyze grep hits. Be concise. State the pattern of behavior, not each line."
    prompt = f"Pattern: /{pattern}/\nHits ({len(hits)}):\n{body}\n\nExplain what these hits collectively indicate."
    return _generate(model or CODE_MODEL, prompt, system=system, num_predict=400)


@mcp.tool()
def ollama_translate(
    text: str | None = None,
    file_path: str | None = None,
    target: str = "pt-BR",
    source: str | None = None,
    register: str = "neutro",
    model: str | None = None,
) -> str:
    """Translate text or a file. Defaults to the PT-BR-tuned Gemma-Gaia model
    when target is pt-BR/pt — it produces noticeably more natural Portuguese
    than llama3.2. Pass `register` to steer tone: "neutro" (default),
    "informal", "tecnico", "marketing", "juridico"."""
    content = _read_input(text, file_path)
    is_ptbr = target.lower().replace("_", "-") in {"pt", "pt-br", "ptbr", "portugues", "português"}
    chosen = model or (PTBR_MODEL if is_ptbr else DEFAULT_MODEL)
    src = f"from {source} " if source else ""
    system = (
        f"You are a professional translator. Translate {src}to {target} with "
        f"a {register} register. Preserve meaning, terminology, formatting, "
        "code blocks, variable names, and markdown. Output ONLY the translation, "
        "no preamble, no notes, no quotes around it."
    )
    return _generate(chosen, content, system=system, num_predict=max(512, len(content) // 2))


@mcp.tool()
def ollama_ask(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    max_tokens: int = 512,
) -> str:
    """Generic escape hatch: send a freeform prompt to a local model."""
    return _generate(model or DEFAULT_MODEL, prompt, system=system, num_predict=max_tokens)


@mcp.tool()
def ollama_redact(
    text: str | None = None,
    file_path: str | None = None,
    extra_patterns: list[str] | None = None,
    model: str | None = None,
) -> str:
    """Redact PII and secrets from text before it enters Claude's context.
    Replaces e-mail, phone, CPF/CNPJ, AWS/GCP keys, JWT, bearer tokens,
    private IPs with stable placeholders ([EMAIL_1], [CPF_1], etc.). Pass
    `extra_patterns` (regexes) to redact additional shapes.

    Uses regex first (deterministic), then asks the local model to catch
    anything regex missed (names in obvious contexts, addresses)."""
    import re

    content = _read_input(text, file_path)
    counters: dict[str, int] = {}
    cache: dict[str, str] = {}

    def repl(tag: str, value: str) -> str:
        key = f"{tag}::{value}"
        if key in cache:
            return cache[key]
        counters[tag] = counters.get(tag, 0) + 1
        token = f"[{tag}_{counters[tag]}]"
        cache[key] = token
        return token

    patterns = [
        ("EMAIL", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        ("JWT", r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        ("AWS_KEY", r"AKIA[0-9A-Z]{16}"),
        ("AWS_SECRET", r"(?i)aws(.{0,20})?(secret|access).{0,3}[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"),
        ("BEARER", r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
        ("CNPJ", r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"),
        ("CPF", r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
        ("PHONE_BR", r"\b(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}-?\d{4}\b"),
        ("IPV4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
        ("CC", r"\b(?:\d[ -]?){13,16}\b"),
    ]
    for name, rx in patterns:
        content = re.sub(rx, lambda m, n=name: repl(n, m.group(0)), content)
    if extra_patterns:
        for i, rx in enumerate(extra_patterns):
            content = re.sub(rx, lambda m, i=i: repl(f"CUSTOM{i}", m.group(0)), content)

    # Regex-only is deterministic and safe. The LLM second pass tends to
    # corrupt placeholders in small models, so we keep it disabled. If you
    # need name/address redaction, run ollama_extract with an explicit schema.
    return content


@mcp.tool()
def ollama_diff_summary(
    diff: str | None = None,
    file_path: str | None = None,
    model: str | None = None,
) -> str:
    """Summarize a git diff as bullets per file: what changed and why it
    likely changed. Use before reading a large diff so you know where to
    focus. Pass `diff` directly or `file_path` to a file holding the diff."""
    content = _read_input(diff, file_path)
    system = (
        "You summarize git diffs. Output format:\n"
        "- <path>: <one-line summary of the change>\n"
        "Group related files when obvious. No preamble. No code snippets. "
        "Flag anything that looks risky (schema change, auth, public API) "
        "with [RISK] prefix."
    )
    return _generate(model or CODE_MODEL, content, system=system, num_predict=800)


@mcp.tool()
def ollama_commit_message(
    diff: str | None = None,
    file_path: str | None = None,
    style: str = "conventional",
    language: str = "pt-BR",
    model: str | None = None,
) -> str:
    """Generate a commit message from a diff. `style` is "conventional"
    (feat/fix/chore/...) or "plain". `language` controls the message body."""
    content = _read_input(diff, file_path)
    is_ptbr = language.lower().startswith("pt")
    chosen = model or (PTBR_MODEL if is_ptbr else CODE_MODEL)
    fmt = (
        "Conventional Commits: <type>(<scope>): <subject>. type in "
        "feat|fix|chore|refactor|docs|test|style|perf. Subject <= 72 chars, "
        "imperative mood. Optional body explaining the WHY in 1-3 lines."
        if style == "conventional"
        else "One-line subject (<= 72 chars) + optional body explaining WHY."
    )
    system = (
        f"You write git commit messages in {language}. {fmt} "
        "Output ONLY the message. No quotes, no preamble, no trailing notes."
    )
    return _generate(chosen, content, system=system, num_predict=300)


@mcp.tool()
def ollama_sql_explain(
    query: str,
    dialect: str = "postgres",
    language: str = "pt-BR",
    model: str | None = None,
) -> str:
    """Explain what a SQL query does in plain language and suggest indexes
    or rewrites. `dialect` defaults to postgres."""
    is_ptbr = language.lower().startswith("pt")
    chosen = model or (PTBR_MODEL if is_ptbr else CODE_MODEL)
    system = (
        f"You explain {dialect} SQL in {language}. Output sections:\n"
        "1. O que faz (1-3 linhas)\n"
        "2. Tabelas e joins\n"
        "3. Filtros e ordenacao\n"
        "4. Indices sugeridos (se algum)\n"
        "5. Riscos / N+1 / full scan (se algum)\n"
        "Conciso. Sem reescrever a query inteira a nao ser que peca."
    )
    return _generate(chosen, query, system=system, num_predict=600)


@mcp.tool()
def ollama_dedupe(
    items: list[str],
    threshold: float = 0.65,
    model: str | None = None,
) -> str:
    """Cluster semantically similar items (ticket titles, error messages,
    user feedback). Returns groups as text: each group lists its members,
    one cluster per blank-line-separated block. `threshold` is cosine
    similarity tuned for nomic-embed-text (0.65 default merges paraphrases;
    raise to 0.75 for stricter, lower to 0.55 for looser)."""
    import math

    if not items:
        return "no items"
    embs = _embed(items, model=model)

    def cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0

    clusters: list[list[int]] = []
    centroids: list[list[float]] = []
    for i, e in enumerate(embs):
        placed = False
        for ci, cent in enumerate(centroids):
            if cos(e, cent) >= threshold:
                clusters[ci].append(i)
                k = len(clusters[ci])
                centroids[ci] = [(cent[d] * (k - 1) + e[d]) / k for d in range(len(e))]
                placed = True
                break
        if not placed:
            clusters.append([i])
            centroids.append(e)

    clusters.sort(key=len, reverse=True)
    out: list[str] = []
    for ci, members in enumerate(clusters, 1):
        out.append(f"# group {ci} ({len(members)} items)")
        for idx in members:
            out.append(f"- {items[idx]}")
        out.append("")
    return "\n".join(out).strip()


@mcp.tool()
def ollama_review_copy(
    text: str,
    audience: str = "candidato",
    register: str = "neutro",
    max_chars: int | None = None,
    model: str | None = None,
) -> str:
    """Review UI/marketing/email copy in PT-BR. Returns the revised copy
    plus a short list of changes. `audience` examples: "candidato",
    "advisor", "admin", "lead frio". `register`: "neutro", "informal",
    "tecnico", "marketing". Pass `max_chars` to enforce a length budget
    (push notification, button label)."""
    chosen = model or PTBR_MODEL
    constraints = [f"publico: {audience}", f"registro: {register}"]
    if max_chars:
        constraints.append(f"limite: {max_chars} caracteres")
    system = (
        "Voce revisa copy em PT-BR. Saida em duas partes separadas por "
        "uma linha '---':\n"
        "1. A copy revisada (so o texto final, sem aspas)\n"
        "2. Bullets curtos do que mudou e por que\n"
        "Restricoes: " + "; ".join(constraints) + ". "
        "Priorize clareza, voz ativa, verbo forte, sem hype, sem emoji, "
        "sem 'voce vai amar / incrivel / poderoso'. Mantenha intencao."
    )
    return _generate(chosen, text, system=system, num_predict=600)


# NOTE: *.json is intentionally excluded — it's data (i18n bundles, config,
# lockfiles, fixtures), not code; it bloats the index and adds noise to concept
# search. Pass globs=["*.json", ...] explicitly if you ever need it.
_DEFAULT_GLOBS = (
    "*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.go", "*.rs", "*.java",
    "*.rb", "*.php", "*.cs", "*.c", "*.h", "*.cpp", "*.hpp", "*.swift",
    "*.kt", "*.scala", "*.sql", "*.sh", "*.bash", "*.zsh", "*.lua",
    "*.vue", "*.svelte", "*.md", "*.mdx", "*.yaml", "*.yml", "*.toml",
    "*.html", "*.css", "*.scss",
)

_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".nuxt", ".turbo", ".cache", "coverage", ".pytest_cache",
    "target", ".gradle", ".idea", ".git", "out", ".parcel-cache",
    ".terraform", "vendor", "bower_components", ".mypy_cache", ".ruff_cache",
}

# Lockfiles / minified / generated blobs: tracked but useless for concept search
# and they explode into hundreds of chunks. Skip by exact name or suffix.
_SKIP_FILENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb",
    "composer.lock", "Gemfile.lock", "Cargo.lock", "poetry.lock", "uv.lock",
    "Podfile.lock", "go.sum",
}
# Data / generated / minified suffixes — never useful for concept search. These
# are skipped even if a caller passes a broad `globs` (belt-and-suspenders; the
# default globs are already an allowlist that excludes them).
_SKIP_SUFFIXES = (
    ".min.js", ".min.css", ".map", ".lock", ".lockb",
    ".csv", ".tsv", ".psv", ".parquet", ".jsonl", ".ndjson",
    ".json",  # data, not code (i18n/config/fixtures) — excluded by default too
)
# Hard cap on chunks per file — bounds worst-case cost for any accidentally huge
# data file (e.g. a multi-thousand-line JSON fixture) that slips past the size cap.
MAX_CHUNKS_PER_FILE = int(os.environ.get("OLLAMA_MAX_CHUNKS_PER_FILE", "120"))


def _iter_files(root: Path, globs: tuple[str, ...]):
    import fnmatch
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if any(fnmatch.fnmatch(fn, g) for g in globs):
                p = Path(dirpath) / fn
                try:
                    if p.stat().st_size > 512_000:  # skip files > 500KB
                        continue
                except OSError:
                    continue
                yield p


def _chunk_lines(text: str, window: int, overlap: int) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    if not lines:
        return []
    step = max(1, window - overlap)
    chunks: list[tuple[int, int, str]] = []
    for start in range(0, len(lines), step):
        end = min(len(lines), start + window)
        body = "\n".join(lines[start:end]).strip()
        if body:
            chunks.append((start + 1, end, body))
        if end >= len(lines):
            break
    return chunks


def _open_index(db_path: Path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _pack_vec(v: list[float]) -> bytes:
    import struct
    return struct.pack(f"{len(v)}f", *v)


def _unpack_vec(b: bytes) -> list[float]:
    import struct
    n = len(b) // 4
    return list(struct.unpack(f"{n}f", b))


def _index_anchor(root_p: Path, max_up: int = 3) -> Path:
    """Directory whose .git/ holds the index. Usually root_p, but if root_p has
    no .git/ (you're in a subfolder), walk up to `max_up` levels looking for one
    so the whole repo shares a single index. Falls back to root_p if none found."""
    p = root_p
    for _ in range(max_up + 1):  # root_p itself + up to max_up ancestors
        if (p / INDEX_SUBDIR).is_dir():
            return p
        if p.parent == p:  # filesystem root
            break
        p = p.parent
    return root_p


def _index_path(root_p: Path) -> Path:
    """Canonical index location: <anchor>/.git/ragcode-index.sqlite, where anchor
    is root_p or the nearest ancestor (<= 3 levels up) that has a .git/."""
    return _index_anchor(root_p) / INDEX_SUBDIR / INDEX_FILENAME


def _resolve_index(root_p: Path) -> Path:
    """Prefer the canonical .git/ index; otherwise fall back to a legacy index
    (older filename, or an old .vscode/ or root-level location) if that is the
    only one present, so pre-rename indexes keep working until re-indexed."""
    anchor = _index_anchor(root_p)
    new = anchor / INDEX_SUBDIR / INDEX_FILENAME
    if new.exists():
        return new
    for name in [INDEX_FILENAME, *LEGACY_INDEX_NAMES]:
        for base in (anchor / INDEX_SUBDIR, root_p / ".vscode", root_p):
            legacy = base / name
            if legacy != new and legacy.exists():
                return legacy
    return new


def _git(root_p: Path, *args: str) -> str | None:
    """Run a git command in root_p. Returns stdout, or None if git is absent /
    the command fails (e.g. not a repo)."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(root_p), *args],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _git_head(root_p: Path) -> str | None:
    """Current HEAD sha if root_p is the top level of a git work tree."""
    top = _git(root_p, "rev-parse", "--show-toplevel")
    if top is None or Path(top.strip()).resolve() != root_p:
        return None  # only drive git-incremental when root == repo toplevel
    sha = _git(root_p, "rev-parse", "HEAD")
    return sha.strip() if sha else None


def _git_changed_since(root_p: Path, since_sha: str) -> tuple[set[str], set[str]] | None:
    """Paths changed since `since_sha`, as (changed_or_added, deleted), relative
    to the repo root. Covers committed changes (since_sha..HEAD) AND the current
    working tree (staged + unstaged + untracked). None if git fails."""
    changed: set[str] = set()
    deleted: set[str] = set()

    diff = _git(root_p, "diff", "--name-status", since_sha, "HEAD")
    if diff is None:
        return None
    for line in diff.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            deleted.add(parts[1]); changed.add(parts[2])
        elif status.startswith("D"):
            deleted.add(parts[1])
        else:
            changed.add(parts[-1])

    st = _git(root_p, "status", "--porcelain", "--untracked-files=all")
    if st is None:
        return None
    for line in st.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:  # rename in the working tree
            old, new = path.split(" -> ", 1)
            deleted.add(old); changed.add(new)
        elif "D" in code:
            deleted.add(path)
        else:
            changed.add(path)

    changed -= deleted
    return changed, deleted


def _git_listed_files(root_p: Path) -> list[str] | None:
    """All tracked + untracked-but-NOT-ignored files, relative to root_p. This
    is git's own view, so it honors .gitignore exactly. None if not a git repo."""
    out = _git(root_p, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if out is None:
        return None
    return [x for x in out.split("\0") if x]


def _iter_rel(root_p: Path, globs: tuple[str, ...]):
    """Yield candidate file paths (relative to root_p) for a full index. In a
    git repo, enumerate via `git ls-files` so .gitignore is respected; otherwise
    fall back to an os.walk that only knows the built-in skip dirs."""
    listed = _git_listed_files(root_p)
    if listed is not None:
        for rel in listed:
            yield rel
    else:
        for p in _iter_files(root_p, globs):
            yield str(p.relative_to(root_p))


def _index_file(conn, root_p: Path, rel: str, file_globs: tuple[str, ...],
                window: int, overlap: int, chosen_model: str,
                known: dict[str, tuple[float, int]]) -> tuple[str, int]:
    """Embed one file into the index. Returns (status, n_chunks) where status is
    'indexed' | 'skipped' (unchanged) | 'ignored' (glob/size/dir) | 'error'."""
    import fnmatch
    rel_parts = Path(rel).parts
    if any(d in _SKIP_DIRS or d.startswith(".") for d in rel_parts[:-1]):
        return ("ignored", 0)
    name = rel_parts[-1]
    if name in _SKIP_FILENAMES or name.endswith(_SKIP_SUFFIXES):
        return ("ignored", 0)
    if not any(fnmatch.fnmatch(name, g) for g in file_globs):
        return ("ignored", 0)
    p = root_p / rel
    try:
        st = p.stat()
    except OSError:
        return ("ignored", 0)
    if st.st_size > 512_000:
        return ("ignored", 0)
    prev = known.get(rel)
    if prev and abs(prev[0] - st.st_mtime) < 1 and prev[1] == st.st_size:
        return ("skipped", 0)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("error", 0)

    conn.execute("DELETE FROM chunks WHERE path=?", (rel,))
    chunks = _chunk_lines(text, window, overlap)[:MAX_CHUNKS_PER_FILE]
    if not chunks:
        conn.execute("INSERT OR REPLACE INTO files(path, mtime, size) VALUES (?,?,?)",
                     (rel, st.st_mtime, st.st_size))
        return ("indexed", 0)
    embs = _embed([c[2] for c in chunks], model=chosen_model)
    for (start, end, body), emb in zip(chunks, embs):
        conn.execute(
            "INSERT INTO chunks(path, start_line, end_line, text, embedding) VALUES (?,?,?,?,?)",
            (rel, start, end, body, _pack_vec(emb)),
        )
    conn.execute("INSERT OR REPLACE INTO files(path, mtime, size) VALUES (?,?,?)",
                 (rel, st.st_mtime, st.st_size))
    return ("indexed", len(chunks))


def index_project(
    root: str = ".",
    globs: list[str] | None = None,
    window: int = 60,
    overlap: int = 12,
    model: str | None = None,
    rebuild: bool = False,
) -> str:
    """Index a project for semantic code search. Walks `root` (default cwd),
    chunks each file into sliding windows of `window` lines (overlap=12),
    embeds each chunk with bge-m3 (multilingual, good for code+PT-BR comments),
    and stores everything in `.git/ragcode-index.sqlite` at the project
    root (kept out of the source tree; gitignore it once).

    Incremental by commit: when `root` is a git repo's top level and an index
    already exists, only files git reports as changed since the last indexed
    commit (committed diff + working-tree edits + untracked) are re-embedded,
    and deletions are pruned. Falls back to an mtime full-walk for non-git roots
    or the first build. Pass `rebuild=True` to wipe and start over; `globs` to
    override the default file types."""
    root_p = Path(root).expanduser().resolve()
    if not root_p.is_dir():
        return f"not a directory: {root_p}"

    db_path = _index_path(root_p)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not rebuild:  # migrate any legacy index (old name/location) into .git/
        legacy = _resolve_index(root_p)
        if legacy != db_path and legacy.exists():
            if not db_path.exists():
                legacy.replace(db_path)
            else:
                legacy.unlink()
    if rebuild and db_path.exists():
        db_path.unlink()

    conn = _open_index(db_path)
    chosen_model = model or CODE_EMBED_MODEL
    row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    if row and row[0] != chosen_model:
        conn.close()
        return (f"index was built with '{row[0]}' but you requested '{chosen_model}'. "
                f"pass rebuild=True to re-embed with the new model.")
    if not row:
        conn.execute("INSERT INTO meta(key,value) VALUES('model',?)", (chosen_model,))
        conn.commit()

    file_globs = tuple(globs) if globs else _DEFAULT_GLOBS
    known: dict[str, tuple[float, int]] = {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT path, mtime, size FROM files")
    }

    indexed = skipped = chunks_total = 0
    errors: list[str] = []
    mode = "full"

    head = _git_head(root_p)
    sha_row = conn.execute("SELECT value FROM meta WHERE key='head_sha'").fetchone()
    stored_sha = sha_row[0] if sha_row else None

    def _apply(rel: str) -> bool:
        """Index one path; record counters. Returns False to abort (too many errors)."""
        nonlocal indexed, skipped, chunks_total
        try:
            status, n = _index_file(conn, root_p, rel, file_globs,
                                    window, overlap, chosen_model, known)
        except httpx.HTTPError as e:
            errors.append(f"{rel}: embed error {e}")
            return len(errors) <= 5
        if status == "indexed":
            indexed += 1
            chunks_total += n
        elif status == "skipped":
            skipped += 1
        elif status == "error":
            errors.append(f"{rel}: read error")
        conn.commit()  # commit after every file: exact progress + zero-loss resume
        return True

    if head and stored_sha and known and not rebuild:
        delta = _git_changed_since(root_p, stored_sha)
        if delta is not None:
            mode = "git-incremental"
            changed, deleted = delta
            for rel in sorted(deleted):
                conn.execute("DELETE FROM chunks WHERE path=?", (rel,))
                conn.execute("DELETE FROM files WHERE path=?", (rel,))
            for rel in sorted(changed):
                if not _apply(rel):
                    break

    if mode == "full":
        present: set[str] = set()
        for rel in _iter_rel(root_p, file_globs):
            present.add(rel)
            if not _apply(rel):
                break
        # Prune files that vanished or became gitignored since the last index.
        for rel in set(known) - present:
            conn.execute("DELETE FROM chunks WHERE path=?", (rel,))
            conn.execute("DELETE FROM files WHERE path=?", (rel,))

    if head:
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('head_sha',?)", (head,))

    conn.commit()
    total_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    total_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()

    mode_line = f"mode: {mode}"
    if mode == "git-incremental" and stored_sha:
        mode_line += f" (since {stored_sha[:8]})"
    msg = [
        f"index: {db_path}",
        f"model: {chosen_model}",
        mode_line,
        f"indexed this run: {indexed} files, {chunks_total} new chunks",
        f"skipped (unchanged): {skipped}",
        f"total in index: {total_files} files, {total_chunks} chunks",
    ]
    if errors:
        msg.append(f"errors ({len(errors)}): " + "; ".join(errors[:5]))
    return "\n".join(msg)


@mcp.tool()
def ollama_index_project(
    root: str = ".",
    globs: list[str] | None = None,
    window: int = 60,
    overlap: int = 12,
    model: str | None = None,
    rebuild: bool = False,
) -> str:
    """Index a project for semantic code search. Stores the index in
    `.git/ragcode-index.sqlite` and refreshes incrementally by commit
    (only git-changed files since the last indexed sha). Respects .gitignore.
    Usually you don't call this directly — the `ragcode-index` CLI keeps it
    fresh from a terminal. See `index_project` for the full contract."""
    return index_project(root=root, globs=globs, window=window, overlap=overlap,
                         model=model, rebuild=rebuild)


def code_search(
    query: str,
    root: str = ".",
    k: int = 8,
    path_glob: str | None = None,
    snippet_lines: int = 8,
    model: str | None = None,
) -> str:
    """Semantic search over a project previously indexed with
    `ollama_index_project`. Returns top-`k` chunks as `path:start-end`
    plus a short snippet each. Use BEFORE Read/Grep when looking for a
    concept ("where do we handle PDI recalculation?") instead of a literal
    string. `path_glob` filters results (e.g. "frontend/src/**/*.tsx")."""
    import fnmatch
    import math
    import sqlite3

    root_p = Path(root).expanduser().resolve()
    db_path = _resolve_index(root_p)
    if not db_path.exists():
        return (f"no index at {db_path}. run `ragcode-index {root_p}` in a "
                "terminal (or ollama_index_project) first.")

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM meta WHERE key='model'").fetchone()
    chosen_model = model or (row[0] if row else CODE_EMBED_MODEL)
    try:
        q_emb = _embed([query], model=chosen_model)[0]
    except httpx.HTTPError as e:
        conn.close()
        return f"embed failed for query: {e}"

    qn = math.sqrt(sum(x * x for x in q_emb)) or 1.0
    hits: list[tuple[float, str, int, int, str]] = []
    for path, start, end, text, blob in conn.execute(
        "SELECT path, start_line, end_line, text, embedding FROM chunks"
    ):
        if path_glob and not fnmatch.fnmatch(path, path_glob):
            continue
        v = _unpack_vec(blob)
        dot = sum(a * b for a, b in zip(q_emb, v))
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        score = dot / (qn * vn)
        hits.append((score, path, start, end, text))
    conn.close()

    hits.sort(key=lambda h: h[0], reverse=True)
    hits = hits[:k]
    if not hits:
        return "no hits"

    out: list[str] = []
    for score, path, start, end, text in hits:
        snippet = "\n".join(text.splitlines()[:snippet_lines])
        out.append(f"{path}:{start}-{end}  (score {score:.3f})\n{snippet}")
    # Separador explicito entre hits: os snippets sao codigo e podem conter
    # linhas em branco, entao so uma linha vazia seria fronteira ambigua.
    return ("\n" + "=" * 40 + "\n").join(out).strip()


@mcp.tool()
def ollama_code_search(
    query: str,
    root: str = ".",
    k: int = 8,
    path_glob: str | None = None,
    snippet_lines: int = 8,
    model: str | None = None,
    auto_index: bool = True,
) -> str:
    """Semantic search over the project's index (`.git/ragcode-index.sqlite`).
    Returns top-`k` chunks as `path:start-end` + snippet. Use BEFORE Read/Grep
    when looking for a concept ("where do we handle PDI recalculation?") instead
    of a literal string. `path_glob` filters (e.g. "frontend/src/**/*.tsx").

    Auto-reindexes (git-incremental, ~instant if nothing changed) before
    searching — same behavior as the `ragcode-find` CLI — so results reflect
    the current tree. Pass `auto_index=False` to search the index as-is; the
    first build on a fresh repo is slow (that one time only). See `code_search`."""
    if auto_index:
        try:
            index_project(root=root, model=model)
        except Exception:
            pass  # best-effort refresh: still search whatever index exists
    return code_search(query=query, root=root, k=k, path_glob=path_glob,
                       snippet_lines=snippet_lines, model=model)


@mcp.tool()
def ollama_list_models() -> str:
    """List models available in the local Ollama instance."""
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{OLLAMA_URL}/api/tags")
        r.raise_for_status()
        models = r.json().get("models", [])
    if not models:
        return "no models installed"
    return "\n".join(f"{m['name']}  ({m.get('size', 0) // (1024*1024)} MB)" for m in models)


if __name__ == "__main__":
    mcp.run()
