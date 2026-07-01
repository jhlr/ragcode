#!/usr/bin/env bash
# Install (or remove) the ragcode search-gate hook.
#
# A Claude Code PreToolUse hook that steers code search toward the semantic
# index: blocks conceptual `Grep` patterns (telling Claude to use
# mcp__ragcode__ollama_code_search) and reminds on Bash grep/rg/find + Glob.
# Exact identifiers / regex / quoted strings pass through. Bypass any Bash
# command with a trailing `# allow-grep`.
#
# Writes into ~/.claude — separate from ./install.sh on purpose, since it
# modifies the user's global Claude Code settings.
#
# Usage:
#   ./install-hook.sh            # install / update (idempotent)
#   ./install-hook.sh --uninstall  # remove the hook + script
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2; exit 1
fi

HOOK_SRC="$HERE/hooks/ragcode-search-gate.py"
HOOK_DST="$HOME/.claude/hooks/ragcode-search-gate.py"
SETTINGS="$HOME/.claude/settings.json"
MODE="install"
[ "${1:-}" = "--uninstall" ] && MODE="uninstall"

if [ "$MODE" = "install" ]; then
  if [ ! -f "$HOOK_SRC" ]; then
    echo "missing $HOOK_SRC" >&2; exit 1
  fi
  mkdir -p "$(dirname "$HOOK_DST")"
  cp "$HOOK_SRC" "$HOOK_DST"
fi

# Merge/unmerge the hook block in settings.json. Idempotent, never clobbers
# other settings or hooks — keyed on the ragcode-search-gate command string.
python3 - "$SETTINGS" "$MODE" "$HOOK_DST" <<'PY'
import json, os, sys
path, mode, hook_dst = sys.argv[1], sys.argv[2], sys.argv[3]
cmd = "python3 ~/.claude/hooks/ragcode-search-gate.py"
try:
    with open(path) as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}

hooks = cfg.setdefault("hooks", {}).setdefault("PreToolUse", [])

def runs_gate(block):
    return any("ragcode-search-gate" in h.get("command", "")
               for h in block.get("hooks", []))

# Drop any existing gate block first (dedup / clean uninstall).
hooks[:] = [b for b in hooks if not runs_gate(b)]

if mode == "install":
    hooks.append({"matcher": "Grep|Glob|Bash", "hooks": [
        {"type": "command", "command": cmd, "timeout": 10,
         "statusMessage": "ragcode search gate"}]})

# Tidy empty containers so uninstall leaves no cruft.
if not hooks:
    cfg["hooks"].pop("PreToolUse", None)
if not cfg.get("hooks"):
    cfg.pop("hooks", None)

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

if mode == "install":
    print("installed PreToolUse hook (ragcode search gate)")
else:
    try:
        os.remove(hook_dst)
    except FileNotFoundError:
        pass
    print("removed PreToolUse hook (ragcode search gate)")
PY

echo "restart Claude Code (or open /hooks) to reload settings."
