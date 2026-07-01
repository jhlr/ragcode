#!/usr/bin/env python3
"""PostToolUse hook: stamp the time of the last ragcode code_search, per project.

The search-gate reads this to decide whether a raw grep/rg deserves a "search
semantically first" nudge — the nudge only fires when no code_search ran in this
project recently. Keyed by cwd so freshness is per-project. Fails open silently.
"""
import hashlib
import json
import pathlib
import sys
import time

STATE_DIR = pathlib.Path.home() / ".claude" / "state"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    cwd = data.get("cwd") or ""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        h = hashlib.md5(cwd.encode()).hexdigest()[:16]
        (STATE_DIR / f"ragcode-lastsearch-{h}").write_text(str(time.time()))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
