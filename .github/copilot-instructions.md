# Copilot instructions

## Code search — prefer the ragcode semantic index

A `ragcode` MCP server is configured for this workspace (`.vscode/mcp.json`).
When it is available, use it as the default way to search code:

- To find **where/what** something happens in the codebase (a concept, a
  behavior, "where do we handle X") → call `ollama_code_search` first, before
  reaching for `grep`/search/`find`.
- Use text search only for **exact** strings or identifiers you already know
  exist, and file search only to locate a file by **name**.
- When reading, go straight to a specific range if you know the spot; only pull
  a whole large file into context when a targeted read/search won't do — prefer
  `ollama_summarize` / `ollama_grep_explain` for bulk understanding.

The index lives at `<repo>/.git/ragcode-index.sqlite`; run `ragcode-index` once
per repo to build it. Requires a local [Ollama](https://ollama.com) running and
the ragcode skill installed at `~/.claude/skills/ragcode`.
