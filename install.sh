#!/usr/bin/env bash
# Bootstrap the ollama-mcp skill on a new machine.
# Idempotent: safe to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found" >&2; exit 1
fi

if ! command -v ollama >/dev/null 2>&1; then
  echo "warning: ollama not on PATH. install from https://ollama.com" >&2
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q mcp httpx

if command -v claude >/dev/null 2>&1; then
  claude mcp remove ollama-local --scope user >/dev/null 2>&1 || true
  claude mcp add ollama-local --scope user -- \
    "$HERE/.venv/bin/python" "$HERE/server.py"
  echo "registered ollama-local MCP at user scope"
else
  echo "claude CLI not found; register manually:"
  echo "  claude mcp add ollama-local --scope user -- $HERE/.venv/bin/python $HERE/server.py"
fi

# Global CLIs: `ollama-mcp-index` (build/refresh index) + `ollama-mcp-find` (search).
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
for name in index find; do
  cat > "$BIN_DIR/ollama-mcp-$name" <<EOF
#!/usr/bin/env bash
# Global launcher for ollama-mcp ($name).
exec "$HERE/.venv/bin/python" "$HERE/ollama_mcp_$name.py" "\$@"
EOF
  chmod +x "$BIN_DIR/ollama-mcp-$name"
done
if echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
  echo "installed CLIs: ollama-mcp-index, ollama-mcp-find"
else
  echo "installed CLIs in $BIN_DIR (add $BIN_DIR to PATH)"
fi

echo "done. restart claude code to load the tools."
echo "optional: ./install-hook.sh  # PreToolUse gate steering search to the index"
