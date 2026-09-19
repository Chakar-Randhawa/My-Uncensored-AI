# AI Router Platform

A multi-provider AI chat router: every message races concurrently across
every configured LLM provider, the first provider to emit a token wins,
every other in-flight request is cancelled, and the winning stream is
relayed to the browser over Server-Sent Events. Runs entirely on free
tiers (Vercel + Render + Supabase).

## Architecture

```
┌─────────────────┐        SSE (fetch + ReadableStream)      ┌──────────────────────┐
│  Next.js (App    │ ───────────────────────────────────────▶ │  FastAPI (asyncio)    │
│  Router, React19)│ ◀─────────────────────────────────────── │  Racing Engine        │
└────────┬──────────┘        Authorization: Bearer <jwt>      └──────────┬────────────┘
         │                                                                │
         │ Supabase Auth (magic link)                    asyncio.TaskGroup, race N adapters
         ▼                                                                ▼
┌──────────────────┐                                         ┌───────────────────────────┐
│  Supabase Auth /  │ ◀──── service_role, async, post-stream ─│  OpenRouter / Groq / ...   │
│  Postgres (RLS)   │        background log write             │  (one adapter per provider)│
└───────────────────┘                                         └───────────────────────────┘
```

**Why this split:** Next.js owns auth state and rendering; FastAPI owns the
part that actually benefits from Python's async I/O model — N concurrent
long-lived HTTP streams, cancelled the instant one of them wins.

## How the race works

1. `POST /v1/chat/stream` builds one adapter per eligible provider (any
   provider whose circuit breaker isn't `OPEN`).
2. `RacingEngine.race()` starts every adapter's `stream()` inside an
   `asyncio.TaskGroup`.
3. The first task to receive a real token wins an atomic compare-and-set on
   a shared `winner` slot. Every losing task returns immediately; a
   supervisor task then explicitly `.cancel()`s them, which propagates
   `CancelledError` up through each adapter's `httpx.AsyncClient.stream()`
   context manager and closes the TCP connection — no dangling requests
   burning free-tier rate limit.
4. The winner's chunks are relayed to the SSE layer as they arrive. Only
   after the full stream reaches the client does an `asyncio.create_task`
   fire the Supabase write — the user never waits on a database round trip.
5. If a provider returns `429`/`5xx`/times out, its circuit breaker records
   a failure; after `CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive
   failures it opens for `CIRCUIT_BREAKER_RESET_SECONDS` and is skipped
   from future races entirely (see `app/core/circuit_breaker.py`).

## Adding a provider

Every provider implements `BaseModelAdapter` (`backend/app/adapters/base.py`):
implement `_build_headers`, `_build_payload`, and an async generator
`stream()` that yields normalized `StreamChunk`s and raises `ProviderError`
on any failure. Register the instance in `_build_adapters()` in
`backend/app/main.py`. Nothing else in the engine, SSE layer, or frontend
needs to change. `openrouter.py` and `groq.py` are the two reference
implementations.

## System prompt

There is no hidden, platform-injected system prompt. `system_prompt` in the
request body is entirely user/conversation-controlled and defaults to
empty — set whatever persona or instructions you want per conversation from
the frontend or by persisting it in `conversations.settings`.

## Setup

### 1. Supabase

1. Create a project, then run `database/schema.sql` in the SQL editor.
2. Enable the **Email** auth provider with magic links (Authentication →
   Providers).
3. Copy: Project URL, `anon` key, `service_role` key, and the JWT secret
   (Project Settings → API).

### 2. Backend (FastAPI)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Supabase + provider keys
uvicorn app.main:app --reload --port 8000
```

**Deploy (Render free tier):** new Web Service → build command
`pip install -r requirements.txt` → start command
`uvicorn app.main:app --host 0.0.0.0 --port $PORT` → add the same env vars
from `.env.example`. Render's free tier idles after inactivity, so the
first request after a cold start will pay a ~30-60s spin-up — the racing
engine itself adds no extra cold-start cost since providers are only
constructed per-request.

### 3. Frontend (Next.js)

```bash
cd frontend
npm install
cp .env.example .env.local   # fill in Supabase public keys + backend URL
npm run dev
```

**Deploy (Vercel free tier):** import the repo, set the root directory to
`frontend/`, add the three env vars from `.env.example`, deploy. Set
`NEXT_PUBLIC_BACKEND_URL` to the Render service URL.

## Free-tier provider notes

- **Groq**: fastest entrant almost every race on LPU hardware, but the
  free tier's requests-per-minute limit is the tightest of the pool — the
  circuit breaker is what keeps a burst of `429`s from taking Groq out of
  rotation for longer than necessary.
- **OpenRouter**: use a `:free`-suffixed model (e.g.
  `meta-llama/llama-3.1-8b-instruct:free`) to stay on OpenRouter's no-cost
  tier; paid models will work with the same adapter but will incur cost.

## Repository layout

```
database/schema.sql              Postgres schema, RLS policies, auth trigger
backend/app/
  config.py                      Typed Settings (pydantic-settings)
  auth.py                        Supabase JWT verification dependency
  main.py                        FastAPI app, /v1/chat/stream SSE endpoint
  adapters/
    base.py                      BaseModelAdapter ABC + shared types
    openrouter.py                OpenRouter adapter
    groq.py                      Groq adapter
  core/
    racing_engine.py             TaskGroup-based racing controller
    circuit_breaker.py           Per-provider circuit breaker
  services/
    supabase_logger.py           Async, non-blocking PostgREST writer
frontend/
  middleware.ts                  Root auth middleware
  lib/supabase/{client,server,middleware}.ts
  app/chat/page.tsx              Protected chat route
  components/chat/
    ChatInterface.tsx            Composer, message list, theme toggle
    MessageBubble.tsx            Markdown + syntax-highlighted rendering
    useSSEChat.ts                fetch-based SSE consumer, AbortController
```
