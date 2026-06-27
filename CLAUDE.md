# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A BMC (Baseband Management Controller) log analysis tool with a ReAct agent chat on top. The end-to-end story:

1. **Authenticate** (`backend/auth/`) — register/login returns a bearer token; `get_current_user` gates every business endpoint.
2. **Upload & parse** — user uploads `dump_info.tar.gz`; the backend extracts + parses `app.log`/`framework.log` in memory and renders structured entries on a WattVision dark-theme dashboard.
3. **Chat with a ReAct agent** (`backend/agent/`) — the agent analyzes the uploaded logs by calling tools (search/filter/stats/timeline/context). When the user has built an **AST source index** for a component, the agent also grounds root-cause diagnosis in the actual source code (function bodies, sibling symbols, cross-file call sites) via the source tools.
4. **Observe** — optional **Langfuse v4** tracing auto-nests every chat into `chat → react-iteration → llm-generation / tool`.

Supporting subsystems, all **per-user isolated** (sessions / datasets / components / source indexes are invisible across users):
- **Component registry** (`backend/db.py`, SQLite) — manages the component repos available for source analysis.
- **AST source analysis** (`backend/ast_analysis/`) — clones a component, runs **tree-sitter** (C/C++/Lua) to build a symbol index stored in SQLite + a source snapshot on disk; the agent queries it via the source tools.
- **Observability** (`backend/observability/`) — Langfuse client with OpenTelemetry-style auto-nesting.

**Note:** `README.md` documents the full system (startup incl. Langfuse, config, API surface); `docs/agent-execution-flow.md` details the ReAct agent end-to-end. Trust the code over any doc when they disagree.

## Commands

Backend (run from `backend/`, Python 3.10+):
```bash
source venv/bin/activate            # README uses `venv/`; `.venv/` also exists — pick one
pip install -r requirements.txt
uvicorn main:app --reload --port 8000   # dev server

# Tests (developer test suite — backend only)
pip install -r requirements-dev.txt     # pytest + pytest-asyncio
python -m pytest -q                     # run all backend tests (asyncio_mode=auto)
```

Frontend (run from `frontend/`, Node 18+):
```bash
npm install
npm run dev        # dev server on :5173, proxies /api → http://localhost:8000 (see vite.config.ts)
npm run build      # tsc -b && vite build (typecheck + bundle to dist/)
```

The **backend has a pytest test suite** under `backend/tests/` (see `backend/pytest.ini`, `asyncio_mode = auto`) covering the parser, extractor, tools, ReAct agent loop, OpenAI adapter, and FastAPI endpoints. LLM calls / Langfuse / network are all stubbed — `tests/conftest.py` force-overrides env (`LANGFUSE_ENABLED=false`, empty `LLM_API_KEY`) before any module import, and redirects `main.DATASETS_DIR`/`session_manager` to `tmp_path`, so tests run fully offline and never touch real `backend/data`. The **frontend has no test runner** (`package.json` has no `test` script); end-to-end UI validation is still manual: upload `dump_info.tar.gz` and exercise the dashboard + chat.

Configuration lives in `backend/.env` (gitignored). It is read by a hand-rolled loader in `config.py` (no `python-dotenv`). Env vars always override `.env`. Key vars: `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_API_BASE`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `AGENT_MAX_ITERATIONS`, `AGENT_MAX_HISTORY`, and `LANGFUSE_*`. **If `LLM_API_KEY` is unset, the app degrades gracefully** — `agent`/`llm` are `None` and `/api/chat*` fall back to a rule-based reply (`_generate_reply` in `main.py`).

## Architecture

### Request lifecycle (the central data flow)
1. **Upload** `POST /api/parse/stream` (SSE) or `/api/parse` (plain) → `extractor.extract_logs` pulls `app.log` + `framework.log` out of `LogDump/` **in memory** (no temp files on disk).
2. **Parse** `parser.parse_logs` runs a dual-regex strategy (standard format first, `LAUNCH` format fallback, else `UNKNOWN`). Each entry gets an incrementing `id` starting at 1; produces `{entries, summary}`.
3. **Persist** to `backend/data/datasets/<dataset_id>.json` via `atomic_write_json` (write `.tmp` → `fsync` → `os.replace`).
4. **Create session** `POST /api/sessions` binds a session to a dataset. Session ↔ dataset is 1:1 per session, fully isolated.
5. **Chat** `/api/chat` or `/api/chat/stream` loads the dataset JSON into a `LogDataset`, runs the ReAct agent, tools query the dataset.

**Critical invariant (don't break it):** the SSE `summary` event is emitted **only after** the dataset JSON is fully + atomically persisted. The frontend creates a session on receiving `summary`; `/api/chat` re-reads the dataset from disk each request. This means a half-written JSON must never be observable — always use `atomic_write_json` for dataset/session persistence.

### Backend agent module (`backend/agent/`)
- **`core.py` — ReAct loop.** `Agent.run` / `run_stream`: system prompt → LLM → if `tool_calls`, execute tools, append tool results to the session, loop; else return text. Capped at `max_iterations` (default 10). Both `run` and `run_stream` persist every assistant/tool message back to the session so context survives across turns and server restarts. `run_stream` forwards `content_delta` events live (real streaming).
- **`tools/registry.py` — decorator-based tool registry.** Tools are async functions whose **first positional arg is always `dataset: LogDataset`**; the rest are LLM-supplied kwargs. Registered at import time via `@ToolRegistry.register(name, description, parameters, required)`. `agent/tools/__init__.py` imports `log_tools` to trigger registration — `main.py` does `import agent.tools` for this side effect.
- **`llm/base.py` — adapter contract.** All providers normalize to an internal OpenAI-shaped message format: `{"role","content","tool_calls"}`. `chat_stream` must yield `{"type":"content_delta","text"}`, then `{"type":"done","content","tool_calls","finish_reason"}`. Tool-call deltas arrive in fragments and must be **accumulated by `index`** before surfacing in `done.tool_calls` (see `openai_adapter.py`). `factory.py` maps `LLM_PROVIDER` (`openai`/`ollama`/`custom`) → `OpenAIAdapter` (any OpenAI-compatible endpoint via `LLM_API_BASE`).
- **`prompts/` — modular prompts.** Each prompt is a `PromptTemplate` (`${var}` substitution via `string.Template`, zero deps). `system.build_system_prompt()` injects live dataset stats. To add provider-specific logic, add a module here, not in the agent.
- **`context.py`** trims history to `max_history` (default 40) before sending to the LLM.
- **`session.py`** — one JSON file per session under `backend/data/sessions/`. Stores full message history (incl. tool calls/results) for session restore.
- **`dataset.py`** — thin typed wrapper over parsed entries + summary; passed to every tool. Carries `user_id` (injected by `/api/chat`) so the source tools can scope AST queries to the logged-in user.

### Other backend subsystems (per-user isolated)
- **`auth/`** — user auth (register/login/me/logout). Tokens + users in SQLite (`backend/data/users.db`). `get_current_user` is the `Depends` guard on every business route; it yields `{"id", "username"}`, and `user["id"]` is the isolation key threaded into sessions, datasets, AST queries, and `LogDataset.user_id`.
- **`ast_analysis/`** — tree-sitter source analysis (router prefix `/api/ast`). `POST /api/ast/analyze` clones a component repo, parses C/C++/Lua into a symbol table in `ast.db`, and keeps a source snapshot under `backend/data/source/<user_id>/<component>/`. `db.find_function_at_line` / `find_symbols_by_name` power the agent's source tools. Must run under `backend/venv` — tree-sitter wheels only resolve there.
- **`db.py`** — component registry (SQLite, `backend/data/components.db`). CRUD for component name / git_url / branch; the `enabled` flag is set by the AST pipeline, not by the user.
- **`observability/`** is documented in its own section below.

**Isolation invariant:** every `list`/`get`/`delete` on sessions, datasets, components, and AST data accepts a `user_id` and treats "not yours" identically to "doesn't exist" (404 / empty / `False`), so other users' data existence is never leaked. New per-user stores must follow the same pattern.

### Frontend (`frontend/src/`)
- **`App.tsx` holds all global state** with hooks (no state library): current `summary`, `activeDatasetId`, `filters`, session list, `activeSessionId`. Log entries are **not** held in `App` — `LogTable` paginates them itself via `fetchEntries` (infinite scroll). Three dataset-activation paths (upload / session-switch / restore) all converge on one activation routine.
- **`api/client.ts`** is the single API surface. A shared `readSSE` async generator parses `data:` lines for both the chat and upload streams. Chat requests can be aborted via `AbortSignal` (stop button).
- **`types/log.ts`** is the shared contract between client and components; keep it in sync with `parser.py`'s output shape.

### Observability (`backend/observability/`)
Optional Langfuse v4 tracing, auto-disabled when `LANGFUSE_ENABLED != true`. Uses OTEL context so `tracer.observation()` calls auto-nest (chat → react-iteration → tool/llm-generation) without passing trace objects around. `docker-compose.yml` is the Langfuse infra stack (postgres/clickhouse/redis/minio) — **not** the app itself. Backend endpoints call `tracer.flush()` after each request.

## Conventions for changes

- **Adding a tool:** define an `async def` in `tools/log_tools.py` (log analysis) or `tools/source_tools.py` (AST source retrieval) with `(dataset: LogDataset, ...params)`, decorate with `@ToolRegistry.register`. It auto-appears in the LLM's tool list and agent loop — no other wiring. `tools/__init__.py` must import the module (it already imports both). Source tools read `dataset.user_id` to scope AST queries.
- **Adding an LLM provider:** add an adapter under `llm/` implementing `BaseLLMAdapter`, then add a branch in `factory.create_llm`.
- **Persistence:** always use `atomic_write_json` (defined in `main.py`) for dataset/session writes. Blocking CPU/IO work (parsing, file reads in paginated endpoints) goes through `asyncio.to_thread` to avoid blocking the event loop.
- **SSE streaming:** SSE event payloads are defined as discriminated unions in `types/log.ts` (`StreamEvent` for chat, `ParseStreamEvent` for upload) — keep these two **separate**; merging them creates unreachable `switch` branches on both sides.
- **The parser must stay in sync** with `LogEntry`/`ParseSummary` in `frontend/src/types/log.ts` — entry fields (`id, timestamp, component, level, file, line, message, source`) and summary fields are the cross-stack contract.
