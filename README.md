# ragcode

A stdio **MCP server** (and Claude Code skill) that exposes a **local Ollama**
instance as a set of tools. The point: offload bulky, low-reasoning work —
summarizing a 5k-line log, classifying 200 tickets, extracting fields from a raw
JSON dump, searching a codebase by concept — to a local model. The bulk text
runs locally and never enters the agent's context window; only the compact
result comes back.

It also ships a **semantic code index** that lives in
`.git/ragcode-index.sqlite`, refreshes **incrementally by git commit**,
honors `.gitignore`, and is usable both as MCP tools and as two terminal CLIs.

---

## Why

A coding agent burns its context (and your tokens) on text it shouldn't have to
read in full. Reading a giant log to find one error, grepping a dump, classifying
a batch — that's bulk work a 1-7B local model does well enough. Keep the agent's
context for reasoning; push the grunt work to `localhost:11434`.

The rule of thumb the skill encodes: before a `Read`/`Bash` that will pull a lot
of raw text into context, ask "is this reasoning, or is this grunt work?" If
grunt, delegate it here.

---

## How it works

```
Claude / CLI ──▶ ragcode (server.py) ──▶ http://localhost:11434 (Ollama)
                      │
                      └─▶ .git/ragcode-index.sqlite   (semantic code index)
```

Pure stdio, no auth, local-only by design.

---

## Tools

| Tool | What it does |
|------|--------------|
| `ollama_summarize` | Summarize text/file (digest a large file without reading it all). |
| `ollama_extract` | Pull structured fields from raw text given a schema. |
| `ollama_classify` | Label a short text into one of N labels. |
| `ollama_grep_explain` | Regex-scan a file and explain the pattern of hits. |
| `ollama_translate` | Translate; PT-BR uses a PT-BR-tuned Gemma-Gaia model. |
| `ollama_redact` | Deterministic PII/secret redaction (regex) before text enters context. |
| `ollama_diff_summary` | Summarize a git diff per file; flags risky changes. |
| `ollama_commit_message` | Generate a commit message from a diff. |
| `ollama_sql_explain` | Explain a SQL query, suggest indexes, flag full scans. |
| `ollama_dedupe` | Cluster semantically similar items via embeddings. |
| `ollama_review_copy` | Review UI/marketing/email copy (PT-BR). |
| `ollama_index_project` | Build/refresh the semantic code index. |
| `ollama_code_search` | Search the index by concept, not literal string. |
| `ollama_ask` | Generic escape hatch — freeform prompt to a local model. |
| `ollama_list_models` | List installed Ollama models. |

---

## Semantic code search

The standout feature. `grep` finds a literal string; this finds the *concept*
even when the code names it differently ("where do we recalc the PDI?" →
the right file, regardless of the function name).

```bash
# Build/refresh the index (commit-incremental, ~instant after the first build)
ragcode-index .

# Search by concept; returns path:start-end + a snippet per hit
ragcode-find "where do we recalculate the PDI?" -k 8
ragcode-find "fallback de provedor de LLM" --glob 'backend/**/*.ts'
```

Design notes:

- **Location:** `<repo>/.git/ragcode-index.sqlite`. Living inside `.git/`
  keeps it out of the working tree, so git never tracks it — no `.gitignore`
  entry needed (and no leading dot, since `.git/` is already hidden). If you run
  from a subfolder without its own `.git/`, it walks up to 3 levels to find the
  repo's `.git/` so the whole repo shares one index. (Old indexes — the previous
  `.ollama-mcp-index.sqlite` name, or `.vscode/`/root locations — are still read
  and migrated on the next index.)
- **Incremental by commit:** in a git repo, re-running only re-embeds the files
  git reports as changed since the last indexed SHA (committed diff + working
  tree + untracked) and prunes deletions. Outside git, it falls back to an
  mtime full-walk.
- **Honors `.gitignore`** (enumerates via `git ls-files`).
- **Skips data:** `.json`, `.csv`, `.tsv`, `.parquet`, lockfiles, and minified
  blobs are excluded — they're data, not code, and they bloat the index.
- **`ragcode-find` auto-reindexes** before each search, so results always
  reflect the current tree (pass `--no-index` to skip). Results go to stdout,
  the reindex note to stderr.

Embedding model defaults to `nomic-embed-text` (roughly 3x faster than `bge-m3`
on CPU; the model is compute-bound, so threading doesn't help — model choice is
the lever). Set `OLLAMA_CODE_EMBED_MODEL=bge-m3:latest` for stronger multilingual
prose (requires a rebuild — the index pins its model).

---

## Install

```bash
# 1. Ollama + models
brew install ollama
ollama serve &
ollama pull nomic-embed-text     # code index (fast)
ollama pull llama3.2             # general text
# optional, stronger but heavier:
ollama pull bge-m3               # multilingual embeddings
ollama pull qwen2.5-coder:7b     # code/log/diff

# 2. Install (run from the clone; you can delete it afterward)
git clone https://github.com/jhlr/ragcode && cd ragcode
./install.sh

# 3. Restart Claude Code. Tools appear as mcp__ragcode__ollama_*
```

`install.sh` is idempotent. It **copies the project into
`~/.claude/skills/ragcode/`** (so the `/ragcode` skill loads), then, anchored
there, creates `.venv`, installs `mcp` + `httpx`, registers the server with
`claude mcp add` (if the `claude` CLI is present), and drops `ragcode-index` /
`ragcode-find` into `~/.local/bin`. Everything is self-contained in the skill
dir, so the clone is disposable. It does **not** touch your global Claude Code
settings — the optional search-gate hook is a separate opt-in step (see below).

Manual MCP registration, if needed:

```bash
claude mcp add ragcode --scope user -- \
  /path/to/ragcode/.venv/bin/python /path/to/ragcode/server.py
```

---

## Search-gate hook (optional)

An opt-in Claude Code `PreToolUse` hook that steers code search toward the
semantic index instead of raw text search. It's separate from `install.sh`
because it writes to your **global** `~/.claude/settings.json`:

```bash
./install-hook.sh              # install / update (idempotent)
./install-hook.sh --uninstall  # remove the hook + script
```

It installs `hooks/ragcode-search-gate.py` into `~/.claude/hooks/` and merges a
`Grep|Glob|Bash` block into `~/.claude/settings.json`. Behavior:

- **`Grep` tool + conceptual pattern** (natural-language phrase, e.g. `onde
  recalcula o PDI`) → **blocked**, with a message to use
  `mcp__ragcode__ollama_code_search`. Exact identifiers, quoted strings and
  regex (`validateToken`, `def .*embed`, `class Foo`) pass through.
- **Bash `grep`/`rg`/`find` and the `Glob` tool** → non-blocking reminder only.
- **Escape hatch:** append `# allow-grep` to any Bash command to silence the
  reminder / bypass the gate for a genuine exact search.

The merge is idempotent and never clobbers existing settings or other hooks. To
review or disable it later, use `/hooks` in Claude Code, or remove the
`Grep|Glob|Bash` block from `~/.claude/settings.json`.

---

## Configuration

All via environment variables (sane defaults shown):

| Var | Default | Meaning |
|-----|---------|---------|
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `OLLAMA_DEFAULT_MODEL` | `llama3.2:latest` | General text tasks |
| `OLLAMA_CODE_MODEL` | `qwen2.5-coder:7b` | Code/log/diff tasks |
| `OLLAMA_PTBR_MODEL` | Gemma-3-Gaia-PT-BR-4b | PT-BR translation/copy |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Dedupe embeddings |
| `OLLAMA_CODE_EMBED_MODEL` | `nomic-embed-text` | Code index embeddings |
| `OLLAMA_EMBED_BATCH` | `64` | Texts per `/api/embed` call |
| `OLLAMA_MAX_CHUNKS_PER_FILE` | `120` | Cap per file (huge-file safety net) |

---

## When NOT to use it

Honest limits — this delegates to small local models, so:

- **Architectural decisions, code review, multi-step reasoning** — keep on the
  main agent. 3-7B models hallucinate and the error contaminates downstream.
- **Exact symbols / exhaustive lists** ("all callers of `makeChain`") — use
  `grep`. Semantic search is approximate and won't guarantee recall.
- **Anything needing up-to-date world knowledge** — local models don't have it.
- **Small inputs (< ~500 tokens)** — the round-trip overhead isn't worth it.

Semantic `code_search` is a *first locator* for fuzzy "where is X" questions and
a complement to grep, not a replacement.

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com) running locally
- `mcp` and `httpx` (installed into the skill venv by `install.sh`)

---

## License

MIT
