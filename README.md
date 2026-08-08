# NxtChat

Memory-augmented chat console: a rolling-summary + long-term-fact memory layer on top of a standard chat UI.

## Structure

```
app/        Frontend — static HTML/CSS/JS, no build step
backend/    Backend — FastAPI + Postgres + OpenAI
docx/       Planning & progress docs (plan.md, setup.md, frontend_progress.md, backend_progress.md, understanding.md)
```

## Running everything locally

1. **Start Docker Desktop** (must be running before step 2):
   ```
   open -a Docker
   ```
2. **Start Postgres:**
   ```
   cd backend && docker compose up -d
   ```
3. **Configure the backend:**
   ```
   cp .env.example .env    # then fill in OPENAI_API_KEY
   ```
4. **Start the backend** (from the repo root — `backend/` is the importable package):
   ```
   cd ..
   source _env/bin/activate
   uvicorn backend.main:app --reload --port 8000
   ```
   Check `http://localhost:8000/health` and API docs at `http://localhost:8000/docs`.
5. **Start the frontend** (separate terminal — use a different port than the backend):
   ```
   cd app && python3 serve.py
   ```
   Open `http://localhost:5500`. Use `serve.py`, not `python3 -m http.server` — the app has
   client-side routes (`/chat/<id>`, `/memory/`) that aren't real files, and only `serve.py`
   knows to fall back to `index.html` for those.

## Frontend

Static, no build step. Wired to the real backend: sessions, streaming chat, and the memory debug panel (rolling summary + token budget) are live. Facts dashboard is wired but not yet exercised with real extracted data. See `docx/frontend_progress.md`.

## Backend

FastAPI + Postgres, chat completions via OpenAI. See steps 1–4 above.

Session-scoped requests take an `X-User-Id` header (no auth layer yet — that's a later phase).

### Backend file structure

```
backend/                     (the importable package — run uvicorn from the repo root as backend.main:app)
├── docker-compose.yml      Postgres service for local dev (pgvector/pgvector:pg16)
├── .env.example            Config template (copy to .env)
├── main.py                 FastAPI app: creates the app, runs init_db() on startup,
│                           mounts routers, CORS, /health
├── config.py                Settings (pydantic-settings) — all budget/threshold knobs,
│                           DATABASE_URL, OPENAI_API_KEY, model names — loaded from .env
├── db.py                    Async SQLAlchemy engine + session factory; init_db() runs
│                           create_all + CREATE EXTENSION vector on startup
├── models.py                 SQLAlchemy tables: ChatSession, Message, Summary, Fact
├── schemas.py                 Pydantic request/response models
├── llm.py                     OpenAI client — stream_chat(), complete_utility()
├── routers/
│   ├── sessions.py           Session CRUD, get_owned_session() (404 on cross-user access)
│   ├── chat.py                POST /sessions/{id}/chat — SSE streaming; persists user +
│   │                         assistant messages via a fresh DB session in `finally`,
│   │                         fires background memory maintenance after responding
│   ├── memory.py               GET /sessions/{id}/memory — debug endpoint: summary,
│   │                         per-slot token budget, injected facts, dropped-message count
│   └── facts.py                GET /users/{id}/facts, DELETE /facts/{id}
└── memory/                     Context assembly + summarization + fact-memory subsystem
    ├── context.py               build_context() / fit_to_budget() — assembles the prompt
    │                           under budget, newest-first, oldest dropped first
    ├── tokens.py                 chars/4 token estimate heuristic
    ├── summarizer.py             Rolling summary: trigger logic, incremental merge,
    │                           from-scratch regen, transactional watermark
    ├── locks.py                   Per-session row lock (FOR UPDATE SKIP LOCKED) on summaries
    ├── facts.py                   Fact extraction, dedup/supersede via cosine thresholds
    └── tasks.py                   fire_and_forget() background task runner
```

See `docx/backend_progress.md` for what's still ahead (Phase 5 polish, evals) as it lands.

## Docs

No single doc has everything — they split between "what to build" and "why/how it works, and what's actually done":

- [`docx/plan.md`](docx/plan.md) — the spec. Exact schema, the context-assembly algorithm, the actual summarization/extraction prompts, full API surface, phase-by-phase build order, edge cases to handle. **Start here if implementing from scratch.**
- [`docx/understanding.md`](docx/understanding.md) — the necessary companion to `plan.md`, not optional reading. Explains *why* each design decision was made, with real code-line references into this repo. §6 ("If you were rebuilding this from zero, in order") is a naturally-ordered build sequence, not just a spec dump — read this alongside `plan.md`, not after it.
- [`docx/backend_progress.md`](docx/backend_progress.md) — progress log: what's done, bugs found/fixed, live verification results, what's still open. Useful for picking up this specific implementation where it left off — not a build guide.
- [`docx/frontend_progress.md`](docx/frontend_progress.md) — same idea, for the frontend.
- [`docx/setup.md`](docx/setup.md) — local setup commands, smoke test, gotchas. How to *run* the existing code, not what to build.
