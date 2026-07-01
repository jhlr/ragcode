#!/usr/bin/env python3
"""PreToolUse gate steering code work toward the ragcode semantic index.

Policy — a recent code_search (per project, within RAGCODE_GATE_STALE seconds)
is the key that unlocks raw file access; without it the cheap path is to search
first:
  - Read (any non-media file) -> BLOCK unless a code_search ran recently. Even
    when fresh, a large whole-file read (no range) is blocked toward
    ollama_summarize/ollama_grep_explain or an offset/limit range. Gating small
    reads too closes the "read files one by one" backfire. Media/binary pass.
  - Grep tool: conceptual patterns -> BLOCK (use ollama_code_search). Exact /
    identifier / regex patterns -> BLOCK when no recent code_search, else pass.
  - Bash grep/rg -> BLOCK when no recent code_search, else pass. Bash find and
    the Glob tool (find-by-name) -> BLOCK. Bypass any Bash command with a
    trailing `# allow-grep: <reason>` (a bare `# allow-grep` is rejected).

Recency is stamped per project by ragcode-mark-search.py (a PostToolUse hook on
ollama_code_search). Fails open on any error so a bug never wedges the session;
set RAGCODE_GATE_OFF=1 to disable the gate entirely (e.g. if ragcode is down).

Tunables (env): RAGCODE_GATE_STALE (s, default 120), RAGCODE_GATE_READ_BYTES
(default 80000), RAGCODE_GATE_OFF (any value disables).
"""
import hashlib
import json
import os
import pathlib
import re
import sys
import time

TOOL = "mcp__ragcode__ollama_code_search"
STALE = int(os.environ.get("RAGCODE_GATE_STALE", "120"))
READ_MAX = int(os.environ.get("RAGCODE_GATE_READ_BYTES", "80000"))
STATE_DIR = pathlib.Path.home() / ".claude" / "state"

# Stopwords that mark a query as natural language (pt/en/es).
STOP = {
    "onde", "como", "quando", "qual", "quais", "porque", "por", "que", "o", "a",
    "os", "as", "de", "do", "da", "dos", "das", "um", "uma", "no", "na", "em",
    "pra", "para", "com", "sem", "se", "quem", "isso", "esse", "essa", "seu",
    "the", "where", "how", "what", "when", "which", "why", "who", "is", "are",
    "does", "do", "to", "of", "in", "an", "we", "i", "this", "that", "it",
    "donde", "dónde", "cómo", "cuando", "cuándo", "cual", "cuál", "quien",
    "quién", "qué", "el", "la", "los", "las", "del", "con", "sin", "y", "es",
    "son", "esto", "ese", "esa", "su", "sus",
}
# Metacharacters => precise regex/exact search, let it through.
META = set(r"""[](){}\^$|+*?/<>="'.:""")
# Read handles these specially / summarize can't help => never gate them.
MEDIA = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico", ".pdf",
    ".mp4", ".mov", ".mp3", ".wav", ".zip", ".gz", ".tar", ".tgz", ".ipynb",
    ".woff", ".woff2", ".ttf", ".eot",
}


def _natural_word(t: str) -> bool:
    """A plain lowercase word (>=4 letters, accents ok) — prose, not a code id."""
    return bool(re.fullmatch(r"[a-zà-ÿ]{4,}", t))


def is_conceptual(pattern: str) -> bool:
    """Aggressive: a lone natural word, or a multi-word phrase that is all plain
    words / contains a stopword, reads as a concept. Anything with a code-shaped
    token (camelCase, snake_case, digits) or regex metachars is treated exact."""
    p = (pattern or "").strip()
    if not p:
        return False
    if any(c in META for c in p):
        return False
    toks = p.split()
    if len(toks) == 1:
        return _natural_word(toks[0])
    if any(t.lower() in STOP for t in toks):
        return True
    return all(_natural_word(t) for t in toks)


def _marker(cwd: str) -> pathlib.Path:
    h = hashlib.md5((cwd or "").encode()).hexdigest()[:16]
    return STATE_DIR / f"ragcode-lastsearch-{h}"


def fresh(cwd: str) -> bool:
    """True if a code_search ran in this project within STALE seconds."""
    try:
        return (time.time() - float(_marker(cwd).read_text().strip())) <= STALE
    except Exception:
        return False


def _emit(obj: dict) -> None:
    print(json.dumps(obj))


def deny(reason: str) -> None:
    _emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})


SEARCH_FIRST = (
    f"Use {TOOL} (busca semantica) primeiro. Se for mesmo string/identificador "
    "exato, rode via Bash com `# allow-grep: <motivo>`."
)


def stale_msg(tail: str) -> str:
    return (
        f"Nenhum {TOOL} recente neste projeto (janela {STALE}s). Faca a busca "
        f"semantica primeiro — {tail}. Um code_search libera grep/read por "
        f"{STALE}s. (Se o servidor ragcode estiver fora, defina RAGCODE_GATE_OFF=1.)"
    )


def large_msg(size: int) -> str:
    return (
        f"Arquivo grande (~{size // 1024} KB). Nao puxe tudo pro contexto: use "
        f"mcp__ragcode__ollama_summarize(file_path=...) ou ollama_grep_explain, "
        f"ou faca Read com offset/limit num trecho especifico."
    )


def main() -> int:
    if os.environ.get("RAGCODE_GATE_OFF"):
        return 0  # master kill switch — never wedge the session
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    cwd = data.get("cwd") or ""

    if tool == "Read":
        fp = ti.get("file_path") or ""
        ext = os.path.splitext(fp)[1].lower()
        if ext in MEDIA:
            return 0  # images/pdf/binaries: summarize can't help, never gate
        # Recency: opening files (even small) without a recent semantic search
        # is the "read one by one" backfire — block it toward code_search first.
        if not fresh(cwd):
            deny(stale_msg("evita abrir arquivos um a um em vez de buscar"))
            return 0
        # Even when fresh, don't dump a huge whole file into context.
        if fp and not ti.get("limit"):
            path = fp if os.path.isabs(fp) else os.path.join(cwd, fp)
            try:
                size = os.path.getsize(os.path.expanduser(path))
            except OSError:
                size = 0
            if size > READ_MAX:
                deny(large_msg(size))
        return 0

    if tool == "Glob":
        # Glob is the native "find files by name" — treated like Bash `find` for
        # consistency, so blocking find isn't defeated by routing through Glob.
        deny(
            f"Glob (achar arquivo por NOME) bloqueado. Se procura ONDE algo "
            f"acontece no codigo (conceito), use {TOOL}. Se precisa MESMO achar "
            f"arquivo por nome, use Bash `find ... # allow-grep: <motivo>`."
        )
        return 0

    if tool == "Grep":
        pat = ti.get("pattern", "")
        if is_conceptual(pat):
            deny(f"Busca conceitual: \"{pat}\". {SEARCH_FIRST}")
        elif not fresh(cwd):
            deny(stale_msg("depois refine com grep exato, ou Bash `# allow-grep: <motivo>`"))
        return 0

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if re.search(r"#\s*allow-grep:\s*\S", cmd):
            return 0  # deliberate, reasoned bypass
        if "# allow-grep" in cmd or "#allow-grep" in cmd:
            deny(
                "Bypass precisa de motivo: use `# allow-grep: <razao>` "
                "(ex.: `# allow-grep: string exata de config`)."
            )
            return 0
        if re.search(r"(^|[|&;]\s*|\s)find\b", cmd):
            deny(
                f"`find` bloqueado. Pra achar codigo por conceito use {TOOL}; pra "
                f"achar arquivo por nome deliberadamente, Bash `# allow-grep: <motivo>`."
            )
            return 0
        if re.search(r"(^|[|&;]\s*|\s)(grep|rg)\b", cmd):
            if not fresh(cwd):
                deny(stale_msg("depois refine com grep, ou use `# allow-grep: <motivo>`"))
            return 0
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
