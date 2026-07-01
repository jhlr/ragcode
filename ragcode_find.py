#!/usr/bin/env python3
"""CLI front-end for ragcode semantic code search.

Takes a natural-language query and prints the top matching code locations
(`path:start-end` + snippet) from the project's index
(`<root>/.git/ragcode-index.sqlite`). Finds a CONCEPT, not a literal
string — use it when grep won't cut it.

Usage:
    ragcode-find "where do we recalculate the PDI?"
    ragcode-find onde validamos o token de auth -k 15
    ragcode-find "embedding recompute" --root /path/to/repo --glob 'backend/**/*.ts'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402  (path set above)


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="ragcode-find",
        description="Semantic code search over the ragcode index. Returns the "
                    "top matching locations for a natural-language query.",
    )
    ap.add_argument("query", nargs="+", help="what you're looking for (free text)")
    ap.add_argument("--root", default=".", help="project root (default: cwd)")
    ap.add_argument("-k", "--count", type=int, default=10,
                    help="how many occurrences to return (default: 10)")
    ap.add_argument("--glob", dest="path_glob", action="append",
                    help="restrict the search to paths matching this glob/path/dir; "
                         "repeatable to search a set (e.g. --glob 'backend/auth/**' "
                         "--glob src/users/users.service.ts)")
    ap.add_argument("--snippet-lines", type=int, default=8, help="snippet length per hit")
    ap.add_argument("--model", help="override embedding model")
    ap.add_argument("--no-index", action="store_true",
                    help="skip the pre-search reindex (search the index as-is)")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    # Always refresh the index before searching so results reflect the current
    # tree. It's git-incremental (~instant if nothing changed); only the very
    # first build on a fresh repo is slow. Best-effort: if the refresh fails
    # (ollama down, etc.) we still search whatever index exists.
    if not args.no_index:
        try:
            summary = server.index_project(root=str(root), model=args.model)
            tail = summary.strip().splitlines()[-3:]
            print("[reindex] " + " | ".join(s.strip() for s in tail), file=sys.stderr)
        except KeyboardInterrupt:
            return 130
        except Exception as e:
            print(f"[reindex skipped: {e}]", file=sys.stderr)

    try:
        out = server.code_search(
            query=" ".join(args.query),
            root=str(root),
            k=args.count,
            path_glob=args.path_glob,
            snippet_lines=args.snippet_lines,
            model=args.model,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
