# ollama-mcp

MCP server + Claude Code skill that exposes a **local Ollama** as tools, so Claude
can offload bulky/cheap text work (summarize, extract, classify, translate,
semantic code search) to a local model — the bulk text never enters Claude's
context, only the compact result comes back.

Includes a **semantic code index** that lives in `.vscode/.ollama-mcp-index.sqlite`,
is **incremental by git commit**, honors `.gitignore`, and ships two terminal CLIs.

## What's inside

| File | Purpose |
|------|---------|
| `server.py` | The stdio MCP server (FastMCP + httpx). All `ollama_*` tools. |
| `ollama_mcp_index.py` | CLI: build/refresh the semantic index. |
| `ollama_mcp_find.py` | CLI: semantic code search (auto-reindexes first). |
| `install.sh` | Bootstrap: venv, register the MCP, install the CLIs. |
| `SKILL.md` | Full skill manual (when to use each tool). |

## Tools

`summarize`, `extract`, `classify`, `grep_explain`, `translate` (PT-BR via
Gemma-Gaia), `redact`, `diff_summary`, `commit_message`, `sql_explain`, `dedupe`,
`review_copy`, `index_project`, `code_search`, `ask`, `list_models`.

## Quickstart

```bash
# 1. Ollama + models
brew install ollama && ollama serve &
ollama pull nomic-embed-text      # code index (fast)
ollama pull bge-m3                # optional: stronger multilingual embeddings
ollama pull llama3.2

# 2. Install (venv + register MCP at user scope + install CLIs to ~/.local/bin)
./install.sh

# 3. Restart Claude Code — tools appear as mcp__ollama-local__ollama_*
```

## Semantic code search from the terminal

```bash
ollama-mcp-index .                          # build/refresh the index (commit-incremental)
ollama-mcp-find "where do we recalc the PDI?" -k 8   # search by concept, not literal string
```

- Index path: `<root>/.vscode/.ollama-mcp-index.sqlite` (gitignore it once).
- `ollama-mcp-find` auto-reindexes (git-incremental, ~instant) before every search.
- Excludes `.json`/`.csv`/lockfiles/minified blobs (data, not code).

## Config (env vars)

`OLLAMA_URL`, `OLLAMA_DEFAULT_MODEL`, `OLLAMA_CODE_MODEL`, `OLLAMA_PTBR_MODEL`,
`OLLAMA_CODE_EMBED_MODEL` (index model, default `nomic-embed-text`),
`OLLAMA_EMBED_BATCH`, `OLLAMA_MAX_CHUNKS_PER_FILE`.

## License

MIT
