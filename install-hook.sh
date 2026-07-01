#!/usr/bin/env bash
# Install (or remove) the ragcode search-gate hooks.
#
# Two Claude Code hooks that funnel code work toward the semantic index:
#   - PreToolUse (Grep|Glob|Bash|Read): ragcode-search-gate.py — blocks
#     conceptual Grep patterns and large whole-file Reads, reminds on Bash
#     grep/rg when no code_search ran recently, blocks Bash find. Bypass any
#     Bash command with `# allow-grep: <reason>`.
#   - PostToolUse (ollama_code_search): ragcode-mark-search.py — stamps the
#     per-project time of the last semantic search, so the gate only nudges
#     when a search is stale.
#
# Writes into ~/.claude — separate from ./install.sh on purpose, since it
# modifies the user's global Claude Code settings.
#
# Usage:
#   ./install-hook.sh              # install / update (idempotent)
#   ./install-hook.sh --uninstall  # remove the hooks + scripts
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2; exit 1
fi

GATE_SRC="$HERE/hooks/ragcode-search-gate.py"
MARK_SRC="$HERE/hooks/ragcode-mark-search.py"
HOOK_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
MODE="install"
[ "${1:-}" = "--uninstall" ] && MODE="uninstall"

if [ "$MODE" = "install" ]; then
  for src in "$GATE_SRC" "$MARK_SRC"; do
    [ -f "$src" ] || { echo "missing $src" >&2; exit 1; }
  done
  mkdir -p "$HOOK_DIR"
  cp "$GATE_SRC" "$MARK_SRC" "$HOOK_DIR/"
fi

# Merge/unmerge both hook blocks in settings.json. Idempotent, never clobbers
# other settings or hooks — keyed on the ragcode-* command strings.
python3 - "$SETTINGS" "$MODE" "$HOOK_DIR" <<'PY'
import json, os, sys
path, mode, hook_dir = sys.argv[1], sys.argv[2], sys.argv[3]
gate_cmd = "python3 ~/.claude/hooks/ragcode-search-gate.py"
mark_cmd = "python3 ~/.claude/hooks/ragcode-mark-search.py"
try:
    with open(path) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}

hooks = cfg.setdefault("hooks", {})

def strip(event, key):
    blocks = hooks.get(event, [])
    blocks[:] = [b for b in blocks
                 if not any(key in h.get("command", "") for h in b.get("hooks", []))]
    if not blocks:
        hooks.pop(event, None)

# Always drop our existing blocks first (dedup / clean uninstall).
strip("PreToolUse", "ragcode-search-gate")
strip("PostToolUse", "ragcode-mark-search")

if mode == "install":
    hooks.setdefault("PreToolUse", []).append({
        "matcher": "Grep|Glob|Bash|Read",
        "hooks": [{"type": "command", "command": gate_cmd, "timeout": 10,
                   "statusMessage": "ragcode search gate"}]})
    hooks.setdefault("PostToolUse", []).append({
        "matcher": "mcp__ragcode__ollama_code_search",
        "hooks": [{"type": "command", "command": mark_cmd, "timeout": 10}]})

if not cfg.get("hooks"):
    cfg.pop("hooks", None)

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

if mode == "install":
    print("installed hooks: PreToolUse gate + PostToolUse search marker")
else:
    for name in ("ragcode-search-gate.py", "ragcode-mark-search.py"):
        try:
            os.remove(os.path.join(hook_dir, name))
        except FileNotFoundError:
            pass
    print("removed ragcode hooks (gate + marker)")
PY

echo "restart Claude Code (or open /hooks) to reload settings."
