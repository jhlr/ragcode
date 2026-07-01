#!/usr/bin/env bash
# Bootstrap the ragcode skill on a new machine.
# Idempotent: safe to re-run. Run it from a fresh github clone — it installs
# itself into ~/.claude/skills/ragcode and anchors everything there.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2; exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "warning: ollama not on PATH. install from https://ollama.com" >&2
fi

# Install as a Claude Code skill: copy the project into ~/.claude/skills/ragcode
# so `/ragcode` loads and the whole setup (venv, MCP server, CLIs) is anchored
# there and self-contained. You can delete the clone afterward. Skipped when
# already running from the skill dir (in-place re-runs / updates).
SKILL_DIR="$HOME/.claude/skills/ragcode"
if [ "$HERE" != "$SKILL_DIR" ]; then
  mkdir -p "$SKILL_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
      "$HERE"/ "$SKILL_DIR"/
  else  # portable fallback if rsync is absent
    ( cd "$HERE" && find . \( -name .git -o -name .venv -o -name __pycache__ \) -prune -o \
        -type f ! -name '*.pyc' -print | while IFS= read -r f; do
        mkdir -p "$SKILL_DIR/$(dirname "$f")"; cp "$f" "$SKILL_DIR/$f"
      done )
  fi
  echo "installed skill files into $SKILL_DIR"
fi
HERE="$SKILL_DIR"
cd "$HERE"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q mcp httpx

if command -v claude >/dev/null 2>&1; then
  claude mcp remove ragcode --scope user >/dev/null 2>&1 || true
  claude mcp add ragcode --scope user -- \
    "$HERE/.venv/bin/python" "$HERE/server.py"
  echo "registered ragcode MCP at user scope"
else
  echo "claude CLI not found; register manually:"
  echo "  claude mcp add ragcode --scope user -- $HERE/.venv/bin/python $HERE/server.py"
fi

# Global CLIs: `ragcode-index` (build/refresh index) + `ragcode-find` (search).
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for name in index find; do
  cat > "$BIN_DIR/ragcode-$name" <<EOF
#!/usr/bin/env bash
# Global launcher for ragcode ($name).
exec "$HERE/.venv/bin/python" "$HERE/ragcode_$name.py" "\$@"
EOF
  chmod +x "$BIN_DIR/ragcode-$name"
done
if echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
  echo "installed CLIs: ragcode-index, ragcode-find"
else
  echo "installed CLIs in $BIN_DIR (add $BIN_DIR to PATH)"
fi

echo "done. restart claude code to load the tools."
echo "optional: ./install-hook.sh  # PreToolUse gate steering search to the index"
