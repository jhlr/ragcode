---
name: ollama-mcp
description: Servidor MCP stdio que expoe Ollama local como tools (summarize, extract, classify, grep_explain, ask) para o Claude delegar trabalho "bruto" e nao queimar contexto. CARREGUE PROATIVAMENTE quando detectar trabalho de alto volume e baixo raciocinio - antes de ler arquivo > 2k linhas, antes de grep em log/dump grande, em batches (classificar/extrair N itens), quando o usuario disser "economizar token / esta ficando grande / resume isso / extraia X de Y / rodar local / usar ollama".
---

# ollama-mcp

Servidor MCP que da ao Claude Code acesso a um Ollama local. A ideia: tarefas
volumosas e baratas (resumir log de 5k linhas, classificar 200 tickets, extrair
campos de um JSON cru) rodam no modelo local; so o resultado compacto volta
para o contexto do Claude. O texto bruto nunca entra.

## Quando ACIONAR (auto-detecte sem o usuario pedir)

Antes de Read/Bash que va trazer texto cru pro contexto, **pare e pergunte**:
"isso e raciocinio que so o Claude faz, ou e trabalho bruto que o local resolve?"
Se for bruto, use ollama-mcp.

Gatilhos concretos:

1. **Arquivo grande prestes a entrar no contexto.** Antes de `Read` num arquivo
   com > ~2k linhas / > 80KB (log, dump JSON, CSV, transcript, output de SQL),
   prefira `ollama_summarize(file_path=...)` ou `ollama_grep_explain(...)`.
   Regra de bolso: se o tamanho do arquivo > 10x o tamanho do resultado que
   voce precisa, delega.
2. **Grep que vai voltar muitos hits.** Se `grep -c` der > 50, nao puxe o
   `grep` completo - use `ollama_grep_explain` para o modelo local sintetizar
   o padrao.
3. **Batch repetitivo.** Classificar/rotular/extrair sobre N itens (tickets,
   PRs, linhas de CSV, mensagens). Loop com `ollama_classify` ou
   `ollama_extract` - cada item custa zero token Anthropic.
4. **Extracao estruturada de texto cru.** "Pegue todos os erros do tipo X",
   "extraia email/telefone/CNPJ", "liste os endpoints citados". `ollama_extract`
   com schema explicito.
5. **Output volumoso de comando.** `kubectl logs`, `aws ... describe-*`,
   `gh api`, `find` recursivo, dumps de DB. Pipe pra arquivo temp e delega
   ao local em vez de deixar o stdout entrar no contexto.
6. **Sinais explicitos do usuario.** "ta ficando grande", "economiza token",
   "resume isso", "varre esse log pra mim", "extrai X disso".
7. **Busca conceitual em codigo (COMPORTAMENTO PADRAO — use a tool MCP).**
   Para encontrar ONDE uma funcionalidade vive ("onde lidamos com auth/PDI/
   recalculo de embedding"), a **PRIMEIRA ferramenta e sempre** a tool MCP
   `mcp__ollama-local__ollama_code_search` — **antes de Grep E antes do Bash
   `ollama-mcp-find`**. Ela **auto-reindexa git-incremental antes de cada
   busca** (sempre fresca, sem chamar index a parte) e devolve os hits como
   `path:start-end` + snippet. Grep so acha string literal; o code_search acha
   o conceito mesmo quando o nome no codigo e diferente.

   ```
   ollama_code_search(query="onde recalculamos o PDI", k=8)
   ollama_code_search(query="config do PWA manifest", path_glob="frontend/**/*.tsx")
   ```

   - Params: `query`, `root` (default cwd), `k` (default 8), `path_glob`
     (filtra path), `snippet_lines`, `auto_index` (default True — deixe ligado).
   - Ordem de preferencia:
     1. **tool MCP `ollama_code_search`** — SEMPRE o default.
     2. **Bash `ollama-mcp-find "<query>" [-k N] [--glob ...]`** — mesmo motor,
        mesmo auto-reindex; use **so se a tool MCP estiver indisponivel** (ex.:
        servidor MCP nao carregado nesta sessao).
     3. **Grep** — so pra identificador/string exata, ou quando o code_search
        nao trouxe o que precisa.
   - Indice em **`<root>/.vscode/.ollama-mcp-index.sqlite`** (toda pasta de
     projeto tem `.vscode/`; gitignore o arquivo uma vez). Incremental por
     commit, respeita `.gitignore`, ignora `.json`/`.csv`/lockfiles.
   - Indexar avulso do terminal (opcional): `ollama-mcp-index [root]` (ou
     `--watch 300`). Mesmo arquivo `.vscode/`.
8. **Traducao para PT-BR.** Sempre que for traduzir qualquer coisa para
   portugues (copy de UI, mensagem de erro, README, comentario, e-mail),
   passe por `ollama_translate(target="pt-BR")` - o Gemma-Gaia foi tunado
   nativo em PT-BR e produz texto mais fluente que a traducao "default" do
   Claude, sem aquele cheiro de traducao literal. Vale tambem como
   passo de **revisao**: traduza voce mesmo, depois rode o resultado pelo
   Gaia com `register="neutro"` e compare. Para textos de marketing/UX
   passe `register="marketing"`; para erro tecnico `register="tecnico"`.

Heuristica de "quanto vale a pena": calcule mentalmente
`tokens_economizados ~= tamanho_do_input - tamanho_do_resultado`. Se for
> ~2k tokens de economia, vale. Abaixo disso o overhead de dar a volta
no Ollama nao compensa.

## Quando NAO usar (mantenha no Claude)

- Decisao arquitetural, design de API, escolha entre abordagens.
- Revisao de codigo onde a qualidade importa (PR review, security review).
- Raciocinio multi-passo com dependencia entre etapas.
- Output que vai voltar pro contexto e ser base de uma decisao subsequente -
  modelo 3B-7B erra mais e o erro contamina o resto.
- Conteudo pequeno (< 500 tokens). O overhead nao paga.
- Tarefa que exige conhecimento atualizado do mundo - locais nao tem.

## Fluxo padrao

1. Detectou gatilho → enuncie em uma linha: "esse arquivo tem 6k linhas,
   vou pedir pro local resumir antes de olhar".
2. Chame a tool MCP apropriada (`mcp__ollama-local__ollama_*`).
3. Trabalhe so com o resultado compacto. Se o resultado parecer suspeito,
   ai sim leia o trecho especifico do original com `Read offset/limit`.

## Arquivos

- `server.py` — servidor stdio em Python, usa `mcp.server.fastmcp` + `httpx`
  para falar com `http://localhost:11434`.

## Tools expostas

- `ollama_summarize(text | file_path, max_lines, focus, model)` — resume conteudo
- `ollama_extract(schema, text | file_path, model)` — extrai campos estruturados
- `ollama_classify(text, labels, model)` — devolve uma label
- `ollama_grep_explain(pattern, file_path, context_lines, model)` — varre arquivo
  com regex e pede ao modelo para interpretar o padrao dos hits
- `ollama_translate(text | file_path, target, source, register, model)` —
  traduz; quando `target` e PT-BR usa o **Gemma-Gaia (PT-BR tuned)** por
  default, que da PT mais natural que llama3.2. Use sempre que precisar
  traduzir copy/erro/doc para portugues - inclusive para revisar uma traducao
  sua antes de entregar.
- `ollama_redact(text | file_path, extra_patterns)` — redacao deterministica
  de PII/segredos via regex (email, CPF/CNPJ, telefone BR, IPv4, JWT, AWS
  keys, bearer, cartao). Substitui por `[TAG_N]` estaveis. Use ANTES de
  colar log/dump de prod no contexto. Apenas regex (LLM-pass desativado
  porque modelos pequenos corrompem placeholders).
- `ollama_diff_summary(diff | file_path)` — resume `git diff` por arquivo,
  marca `[RISK]` em mudancas sensiveis (schema, auth, API publica). Use
  antes de revisar diff > 500 linhas.
- `ollama_commit_message(diff | file_path, style, language)` — gera mensagem
  de commit (`conventional` ou `plain`). PT-BR default usa Gaia.
- `ollama_sql_explain(query, dialect, language)` — explica SQL em PT-BR,
  sugere indices, flag de full scan / N+1. Use em migracoes antigas e
  queries de relatorio.
- `ollama_dedupe(items[], threshold)` — agrupa semanticamente itens
  parecidos via embeddings (`nomic-embed-text`). Use para triagem de
  tickets/erros/feedback. Threshold default 0.65 (nomic em PT-BR e
  fraco em paragrafo curto; para precisao maior, baixe bge-m3 e passe
  `model="bge-m3"`).
- `ollama_review_copy(text, audience, register, max_chars)` — revisa copy
  de UI/email/push em PT-BR via Gaia, devolve copy revisada + bullets do
  que mudou. Respeita limite de caracteres.
- `ollama_index_project(root, globs, window, overlap, model, rebuild)` —
  indexa um projeto para busca semantica. Chunka em janelas de 40 linhas
  com overlap 10, embeda com **bge-m3** (multilingue, bom em codigo +
  PT-BR) e salva em **`<root>/.vscode/.ollama-mcp-index.sqlite`** (migra
  automaticamente um indice legado na raiz). **Incremental por commit:**
  num repo git, re-roda so re-embeda os arquivos que o git reporta como
  mudados desde o ultimo sha indexado (commit + working tree + untracked)
  e poda os deletados; fora de git, cai pra full-walk por mtime. **Respeita
  `.gitignore`** (enumera via `git ls-files`). Tambem exposto como CLI
  global `ollama-mcp-index [root] [--rebuild] [--watch SECONDS]`. Primeira
  vez: `ollama pull bge-m3`.
- `ollama_code_search(query, root, k, path_glob, snippet_lines, model)` —
  busca semantica top-k sobre o indice. Devolve `path:start-end` + snippet.
  **Prefira o CLI Bash `ollama-mcp-find` (gatilho #7), que reindexa sozinho** —
  esta tool MCP NAO reindexa, entao so use se o Bash nao estiver disponivel
  (rode `ollama_index_project` antes se o indice puder estar velho). Use ANTES
  de Read/Grep quando procura conceito ("onde fazemos o recalculo de PDI?") e
  nao string literal. Filtre com `path_glob="frontend/src/**/*.tsx"`.
- `ollama_ask(prompt, model, system, max_tokens)` — escape hatch generico
- `ollama_list_models()` — lista modelos instalados no Ollama

## Defaults de modelo

Configuraveis via env var:

- `OLLAMA_DEFAULT_MODEL` — geral (`llama3.2:latest`, ~2GB)
- `OLLAMA_CODE_MODEL` — codigo/log/diff (`qwen2.5-coder:7b`, ~4.7GB)
- `OLLAMA_PTBR_MODEL` — texto em PT-BR (Gemma-3-Gaia-PT-BR-4b)
- `OLLAMA_URL` — endpoint do Ollama (default `http://localhost:11434`)

Cada tool aceita `model=` para override por chamada.

## Instalacao em maquina nova

```bash
# 1. Ollama rodando + modelos baixados
brew install ollama
ollama serve &
ollama pull llama3.2
ollama pull qwen2.5-coder:7b

# 2. Venv da skill
cd ~/.claude/skills/ollama-mcp
python3 -m venv .venv
.venv/bin/pip install mcp httpx

# 3. Registrar no Claude Code (user scope)
claude mcp add ollama-local --scope user -- \
  ~/.claude/skills/ollama-mcp/.venv/bin/python \
  ~/.claude/skills/ollama-mcp/server.py

# 4. Reiniciar o Claude Code (sair e abrir de novo)
```

Apos reiniciar, as tools aparecem como `mcp__ollama-local__ollama_summarize`
etc. e podem ser chamadas diretamente.

## Quando usar (heuristica)

Boa pedida:

- "Resuma esse log de 8k linhas em /tmp/app.log" — use `ollama_grep_explain`
  ou `ollama_summarize(file_path=...)`. Evita puxar 8k linhas para o contexto.
- "Classifique esses 200 titulos de ticket em bug/feature/duvida" — loop com
  `ollama_classify`. Cada chamada custa zero token na Anthropic.
- "Extraia error_type/file/line dessa stack trace" — `ollama_extract` com schema.
- Geracao de boilerplate repetitivo onde qualidade media basta.

Pedida ruim:

- Raciocinio multi-passo, decisao arquitetural, revisao de codigo critico.
  Modelos locais 3B-7B alucinam. Mantenha no Claude.
- Tarefas onde o output precisa entrar de volta no contexto e ser confiavel
  para decisao subsequente — local erra mais.

## Limites

- `MAX_INPUT_CHARS = 200_000` no `server.py` (~50k tokens). Acima disso o
  conteudo e truncado.
- Timeout HTTP de 300s por chamada.
- Stdio puro, sem auth — destinado a uso local apenas.

## Trocar de modelo default

Edite as constantes no topo de `server.py` ou exporte env vars antes de subir
o Claude Code. Para listar o que esta instalado, chame `ollama_list_models`.
