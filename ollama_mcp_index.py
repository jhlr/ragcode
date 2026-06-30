#!/usr/bin/env python3
"""CLI front-end for the ollama-mcp semantic index.

Lets you (re)build the project index from a terminal, independent of any
Claude session. The index lives in `<root>/.vscode/.ollama-mcp-index.sqlite`,
refreshes incrementally by commit, and respects .gitignore.

Usage:
    ollama-mcp-index                 # index the current directory
    ollama-mcp-index /path/to/repo   # index a specific repo
    ollama-mcp-index --rebuild       # wipe and re-embed from scratch
    ollama-mcp-index --watch 300     # re-index every 300s (commit-incremental)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402  (path set above)


def _run(args: argparse.Namespace) -> str:
    return server.index_project(
        root=str(Path(args.root).expanduser().resolve()),
        globs=args.glob or None,
        window=args.window,
        overlap=args.overlap,
        model=args.model,
        rebuild=args.rebuild,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ollama-mcp-index",
        description="Build/refresh the ollama-mcp semantic code index "
                    "(.vscode/.ollama-mcp-index.sqlite, commit-incremental, "
                    "honors .gitignore).",
    )
    ap.add_argument("root", nargs="?", default=".", help="project root (default: cwd)")
    ap.add_argument("--rebuild", action="store_true", help="wipe and re-embed everything")
    ap.add_argument("--model", help="override embedding model (default: bge-m3)")
    ap.add_argument("--glob", action="append", help="file glob to include (repeatable)")
    ap.add_argument("--window", type=int, default=60, help="chunk window in lines")
    ap.add_argument("--overlap", type=int, default=12, help="chunk overlap in lines")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="re-index on a loop every SECONDS (Ctrl-C to stop)")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    try:
        print(_run(args))
        args.rebuild = False  # only the first pass of --watch should rebuild
        while args.watch:
            time.sleep(args.watch)
            print("---")
            print(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # surface ollama-down / git errors plainly
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
