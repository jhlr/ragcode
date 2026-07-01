#!/usr/bin/env python3
"""PreToolUse gate that steers code search toward the ragcode semantic index.

Reads the hook payload on stdin and:
  - Grep tool: BLOCKS natural-language / conceptual patterns (deny), telling
    Claude to use mcp__ragcode__ollama_code_search instead. Exact-string /
    identifier / regex searches pass through untouched.
  - Bash running grep/rg/find, and the Glob tool: non-blocking REMINDER only.

Fails open: any parsing error → allow (exit 0, no output), so a broken hook
never wedges the session.
"""
import json
import re
import sys

TOOL = "mcp__ragcode__ollama_code_search"

# Stopwords that signal a natural-language query rather than a code token.
STOP = {
    # pt
    "onde", "como", "quando", "qual", "quais", "porque", "por", "que", "o", "a",
    "os", "as", "de", "do", "da", "dos", "das", "um", "uma", "no", "na", "em",
    "pra", "para", "com", "sem", "se", "quem", "isso", "esse", "essa", "seu",
    # en
    "the", "where", "how", "what", "when", "which", "why", "who", "is", "are",
    "does", "do", "to", "of", "in", "a", "an", "we", "i", "this", "that", "it",
    # es
    "donde", "dónde", "como", "cómo", "cuando", "cuándo", "cual", "cuál",
    "cuales", "cuáles", "quien", "quién", "quienes", "que", "qué", "por",
    "para", "el", "la", "los", "las", "un", "una", "unos", "unas", "del", "de",
    "en", "con", "sin", "se", "y", "o", "es", "son", "esto", "ese", "esa",
    "hace", "hacemos", "su", "sus",
}

# Metacharacters that mark a pattern as a precise regex/exact search → let pass.
META = set(r"""[](){}\^$|+*?/<>="'.:""")


def is_conceptual(pattern: str) -> bool:
    p = (pattern or "").strip()
    if not p:
        return False
    if any(c in META for c in p):
        return False  # regex / dotted-path / quoted → precise, allow
    toks = p.split()
    if len(toks) <= 1:
        return False  # single identifier → allow
    low = [t.lower() for t in toks]
    if len(toks) >= 4:
        return True  # long phrase → almost certainly conceptual
    return any(t in STOP for t in low)  # short phrase w/ NL stopword


def emit(obj: dict) -> None:
    print(json.dumps(obj))


def deny(reason: str) -> None:
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }})


def remind(ctx: str) -> None:
    emit({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": ctx,
    }})


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0  # fail open
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    if tool == "Grep":
        pat = ti.get("pattern", "")
        if is_conceptual(pat):
            deny(
                f"Busca conceitual detectada: \"{pat}\". Use {TOOL} "
                "(busca semantica no indice ragcode) em vez de grep para "
                "encontrar codigo por CONCEITO. Se o servidor ragcode nao "
                "estiver carregado nesta sessao, OU se esta e mesmo uma busca "
                "por string/identificador exato, refaca via Bash: "
                "`grep -rn '<pattern>' .  # allow-grep`."
            )
        return 0

    if tool == "Glob":
        remind(
            "Glob acha arquivos por NOME. Se voce esta procurando ONDE algo "
            f"acontece no codigo (conceito), prefira {TOOL}."
        )
        return 0

    if tool == "Bash":
        cmd = ti.get("command", "") or ""
        if "allow-grep" in cmd:
            return 0  # explicit bypass
        if re.search(r"(^|[|&;]\s*|\s)(grep|rg|find)\b", cmd):
            remind(
                "Este comando roda grep/rg/find. Para achar codigo por CONCEITO "
                f"(nao string/nome exato), {TOOL} costuma ser melhor. Para "
                "silenciar este lembrete num uso legitimo, adicione `# allow-grep` "
                "ao comando."
            )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
