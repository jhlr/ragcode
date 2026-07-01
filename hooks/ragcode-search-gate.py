#!/usr/bin/env python3
"""PreToolUse gate steering code work toward the ragcode semantic index.

Tuned as a funnel (block only where a strictly-cheaper path exists, so it never
pushes the agent into reading files one by one):
  - Read of a large text/log/data file (whole-file, no range) -> BLOCK: use
    ollama_summarize/ollama_grep_explain, or Read a specific offset/limit range.
    Small reads and media/binary files pass untouched.
  - Grep tool: conceptual patterns -> BLOCK (use ollama_code_search). Exact /
    identifier / regex patterns pass; if no code_search ran recently in this
    project they pass with a REMINDER to search semantically first.
  - Bash grep/rg -> same: pass, with a reminder when no recent code_search.
    Bash find -> BLOCK. Bypass any Bash command with `# allow-grep: <reason>`
    (a bare `# allow-grep` with no reason is rejected).
  - Glob -> non-blocking reminder.

Recency is stamped per project by ragcode-mark-search.py (a PostToolUse hook on
ollama_code_search). Fails open on any error so a bug never wedges the session.

Tunables (env): RAGCODE_GATE_STALE (s, default 600), RAGCODE_GATE_READ_BYTES
(default 80000).
"""
import hashlib
import json
import os
import pathlib
import re
import sys
import time

TOOL = "mcp__ragcode__ollama_code_search"
STALE = int(os.environ.get("RAGCODE_GATE_STALE", "600"))
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


def remind(ctx: str) -> None:
    _emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": ctx,
    }})


SEARCH_FIRST = (
    f"Use {TOOL} (busca semantica) primeiro. Se for mesmo string/identificador "
    "exato, rode via Bash com `# allow-grep: <motivo>`."
)
STALE_HINT = (
    f"Nenhum {TOOL} recente neste projeto (janela {STALE}s). Considere buscar "
    "semantico antes de garimpar com grep — costuma custar ~10x menos contexto."
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    cwd = data.get("cwd") or ""

    if tool == "Read":
        fp = ti.get("file_path") or ""
        if fp and not ti.get("limit"):
            ext = os.path.splitext(fp)[1].lower()
            if ext not in MEDIA:
                path = fp if os.path.isabs(fp) else os.path.join(cwd, fp)
                try:
                    size = os.path.getsize(os.path.expanduser(path))
                except OSError:
                    size = 0
                if size > READ_MAX:
                    deny(
                        f"Arquivo grande (~{size // 1024} KB). Nao puxe tudo pro "
                        f"contexto: use mcp__ragcode__ollama_summarize(file_path=...) "
                        f"ou ollama_grep_explain, ou faca Read com offset/limit num "
                        f"trecho especifico."
                    )
        return 0

    if tool == "Glob":
        remind(
            f"Glob acha arquivo por NOME. Se procura ONDE algo acontece no "
            f"codigo (conceito), prefira {TOOL}."
        )
        return 0

    if tool == "Grep":
        pat = ti.get("pattern", "")
        if is_conceptual(pat):
            deny(f"Busca conceitual: \"{pat}\". {SEARCH_FIRST}")
        elif not fresh(cwd):
            remind(STALE_HINT)  # exact search: nudge, don't block (avoid backfire)
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
                remind(STALE_HINT)  # nudge, don't block
            return 0
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
