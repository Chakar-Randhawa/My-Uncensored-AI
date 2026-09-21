# AI Router Platform

A multi-provider AI chat router: every message races concurrently across
every configured LLM provider, the first provider to emit a token wins,
every other in-flight request is cancelled, and the winning stream is
relayed to the browser over Server-Sent Events. If the winner dies
mid-stream, the backend automatically retries with a different provider.
Runs entirely on free tiers (Vercel + Render + Supabase + optional Upstash).

**Status:** backend has a pytest suite (9/9 passing) covering the racing
engine and circuit breaker; frontend has a clean `tsc --noEmit`, `next
lint`, and `next build`. See "What's verified vs. what isn't" near the
bottom before deploying.

## Architecture

```
┌───────────────────┐        SSE (fetch + ReadableStream)      ┌──────────────────────────┐
│  Next.js (App      │ ───────────────────────────────────────▶ │  FastAPI (asyncio)        │
│  Router, React 19)  │ ◀─────────────────────────────────────── │  Racing Engine + Failover │
└─────────┬───────────┘        Authorization: Bearer <jwt>      └──────────┬────────────────┘
          │                                                                │
          │ Supabase Auth (magic link)                    asyncio.TaskGroup, race N adapters
          ▼                                                                ▼
┌───────────────────┐                                          ┌────────────────────────────┐
│  Supabase Auth /    │ ◀── service_role, async, post-stream ───│  Groq / OpenRouter / Cerebras│
│  Postgres + pgvector │      background log + cache write       │  / Mistral / Google / CF AI  │
└───────────────────┘                                          └────────────────────────────┘
                                                                              ▲
                                                                              │ optional
                                                                    ┌──────────────────┐
                                                                    │ Upstash Redis      │
                                                                    │ (circuit breaker)  │
                                                                    └──────────────────┘
```

## How the race works

1. `POST /v1/chat/stream` builds one adapter per eligible provider (any
   provider with an API key configured, whose circuit breaker isn't
   `OPEN`, and — for image requests — that has a vision-capable model).
2. A semantic cache check runs first for single-turn requests: if a
   sufficiently similar question was answered before, the cached answer
   streams back immediately with zero provider calls (`database/schema.sql`'s
   `response_cache` table + pgvector cosine similarity).
3. `RacingEngine.race()` starts every adapter's `stream()` inside an
   `asyncio.TaskGroup`. The first task to receive a real token wins an
   atomic compare-and-set; every losing task is explicitly `.cancel()`'d,
   closing its HTTP connection immediately.
4. If the winner dies mid-stream (connection drop, upstream 5xx),
   `race_with_failover()` excludes that provider and starts a fresh race
   among whoever's left — the frontend clears the partial answer (a
   `restart` SSE event) rather than concatenating two different models'
   half-answers.
5. `request.is_disconnected()` is polled during streaming, so hitting
   "Stop" in the UI cancels the upstream provider connection too, not
   just the browser's read of the response.
6. Only after the full stream reaches the client does an
   `asyncio.create_task` fire the Supabase write (message log + semantic
   cache store) — the user never waits on a database round trip.
7. A provider that returns `429`/`5xx`/times out records a circuit-breaker
   failure; after `CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive failures
   it opens for `CIRCUIT_BREAKER_RESET_SECONDS` and is skipped from future
   races entirely. Breaker state is in-memory by default, or shared via
   Upstash Redis's REST API when `UPSTASH_REDIS_REST_URL`/`_TOKEN` are set.

## Providers

| Provider | Free tier | Notes |
|---|---|---|
| Groq | Yes | Fastest on LPU hardware; tightest rate limit — expect its breaker to trip first under load |
| OpenRouter | Yes (`:free` models) | Wide model selection |
| Cerebras | Yes | WSE hardware, competitive with Groq |
| Mistral | Yes (La Plateforme) | Own model family |
| Google AI Studio (Gemini) | Yes | Native vision support |
| Cloudflare Workers AI | Yes | Different wire format, own adapter |

Only `OPENROUTER_API_KEY` and `GROQ_API_KEY` are required — the other four
are optional; omit a key and that provider is simply left out of the race,
no error. See `backend/app/adapters/` — every provider implements the same
`BaseModelAdapter` ABC, so adding a 7th is one new file plus a registration
line in `_build_adapters()` in `main.py`.

## Features

- **Conversation memory** — the full message history is sent on every
  request (`frontend/components/chat/useSSEChat.ts`), not just the latest
  turn.
- **Sidebar** — conversation list, switch, rename (double-click), delete.
- **Regenerate / Edit & resend** — regenerate drops the last reply and
  re-asks (bypassing the cache); editing a past message discards
  everything after it and resends.
- **Provider selector** — race mode (default) or pin a single provider.
- **Image upload (vision)** — attach up to 4 images; only vision-capable
  providers (Groq, OpenRouter, Google AI Studio) are raced for that turn.
- **Semantic cache** — pgvector + local ONNX embeddings (`fastembed`, no
  PyTorch/API key needed), scoped to single-turn requests only so a cached
  answer can never be missing context a follow-up needed.
- **Admin dashboard** (`/admin`) — per-provider win counts, average
  latency, and live circuit-breaker state.
- **Small stuff**: message timestamps, copy buttons, theme persisted to
  `localStorage`, auto-resizing composer, character counter, toast
  notifications, connection-status pill, `⌘K` new chat / `Esc` stop.

There is no hidden, platform-injected system prompt. `system_prompt` in the
request body is entirely user/conversation-controlled and defaults to
empty.

**Not implemented on purpose:** per-user rate limiting. The `users` table
has a `daily_message_cap` column, but nothing enforces it — every signed-in
user can use as much of the configured free-tier quota as they want.

## Setup

### 1. Supabase

1. Create a project, then run `database/schema.sql` in the SQL editor
   (this also enables the `vector` extension for the semantic cache).
2. Enable the **Email** auth provider with magic links (Authentication →
   Providers).
3. Copy: Project URL, `anon` key, `service_role` key, and the JWT secret
   (Project Settings → API).

### 2. Backend (FastAPI)

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in Supabase + at least Groq/OpenRouter keys
uvicorn app.main:app --reload --port 8000
pytest                 # run the test suite
```

**Deploy (Render free tier):**
- Using Docker: point Render at `backend/Dockerfile` directly (it's a
  standard multi-stage build — Render's free tier builds it natively).
- Without Docker: new Web Service → build command
  `pip install -r requirements.txt` → start command
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Either way, add the env vars from `.env.example`. Render's free tier
  idles after inactivity — see the keep-alive workflow below.

### 3. Frontend (Next.js)

```bash
cd frontend
npm install
cp .env.example .env.local   # fill in Supabase public keys + backend URL
npm run dev
npx tsc --noEmit && npm run lint && npm run build   # verify before deploying
```

**Deploy (Vercel free tier):** import the repo, set the root directory to
`frontend/`, add the three env vars from `.env.example`, deploy. Set
`NEXT_PUBLIC_BACKEND_URL` to the Render service URL.

### 4. Optional: Upstash Redis (persistent circuit breaker)

Free tier at [upstash.com](https://upstash.com) → create a Redis database
→ copy the REST URL and token into `UPSTASH_REDIS_REST_URL` /
`UPSTASH_REDIS_REST_TOKEN`. Without this, the breaker still works, just
resets on every backend redeploy/restart.

### 5. Optional: Sentry (error tracking)

Free tier at [sentry.io](https://sentry.io) → new FastAPI project → copy
the DSN into `SENTRY_DSN`. Omit it and Sentry is simply never initialized.

### 6. Keep the Render free-tier backend awake

`.github/workflows/keep-alive.yml` pings `/health` every 10 minutes via
GitHub Actions (free on public repos). Add a repository secret
`BACKEND_HEALTH_URL` set to `https://your-service.onrender.com/health`
(Settings → Secrets and variables → Actions). GitHub can auto-disable
scheduled workflows on a repo with no other activity for 60 days — if that
happens, re-enable it from the Actions tab.

### 7. CI

`.github/workflows/ci.yml` runs on every push/PR: backend `pytest` +
app-import smoke test, frontend `tsc --noEmit` + `next lint` + `next
build`. All free on GitHub Actions for public repos.

## Repository layout

```
database/schema.sql                Postgres schema, RLS, auth trigger, pgvector cache + match function
backend/
  Dockerfile, .dockerignore        Multi-stage build for Render/any container host
  pytest.ini
  app/
    config.py                      Typed Settings (pydantic-settings)
    auth.py                        Supabase JWT verification dependency
    logging_config.py              Structured JSON logging
    main.py                        FastAPI app — /v1/chat/stream, /v1/admin/stats, /health
    adapters/
      base.py                      BaseModelAdapter ABC + shared types
      openai_compatible.py         Shared SSE parser/payload builder for the 4 OpenAI-shaped providers
      groq.py, openrouter.py, cerebras.py, mistral.py   Thin subclasses of the above
      google_ai_studio.py          Custom Gemini wire format (contents/parts, streamGenerateContent)
      cloudflare_workers_ai.py     Custom Cloudflare wire format (account-scoped path)
    core/
      racing_engine.py             TaskGroup-based racing controller + mid-stream failover
      circuit_breaker.py           In-memory or Upstash-Redis-backed breaker
    services/
      supabase_logger.py           Async, non-blocking PostgREST writer
      semantic_cache.py            fastembed + pgvector cache lookup/store
  tests/
    conftest.py                    FakeAdapter test double
    test_racing_engine.py          Race, timeout, mid-stream failure, failover coverage
    test_circuit_breaker.py        Open/half-open/closed transition coverage
frontend/
  middleware.ts                    Root auth middleware (protects /chat and /admin)
  lib/
    supabase/{client,server,middleware}.ts
    conversations.ts               Sidebar's Supabase CRUD (list/create/rename/delete)
    types.ts, useLocalStorage.ts, useToast.tsx, useConnectionStatus.ts, fileToDataUrl.ts
  app/
    layout.tsx, page.tsx           Root layout (ToastProvider) + redirect to /chat
    chat/page.tsx                  New-chat route
    chat/[conversationId]/page.tsx Existing-conversation route (server-loaded history)
    admin/page.tsx                 Provider stats + circuit breaker dashboard
    auth/sign-in, auth/callback    Magic-link flow
  components/chat/
    ChatInterface.tsx              Layout: sidebar + composer + shortcuts + image upload
    Sidebar.tsx, MessageBubble.tsx, ProviderSelector.tsx, ConnectionStatus.tsx
    useSSEChat.ts                  Full-history streaming, regenerate, edit-resend, restart handling
.github/workflows/
  ci.yml                           Backend pytest + frontend typecheck/lint/build
  keep-alive.yml                   Pings Render every 10 minutes
```

## What's verified vs. what isn't

Verified by actually running it in this build, not just read over:
- Backend: `pytest` (9/9 passing, including two real concurrency bugs
  found and fixed during development — see git history / the test file
  comments for what they were), a live `uvicorn` boot + `/health` hit,
  and a real JWT-rejection check on the chat endpoint.
- Frontend: `npx tsc --noEmit` (0 errors), `next lint` (0 warnings), and a
  full `next build` (all 8 routes compile and prerender/route correctly).
- `npm audit`: down to 2 residual vulnerabilities, both inside a nested
  `next/node_modules/postcss` copy (build-time source-map handling, not a
  runtime attack surface), fixable only by a Next.js 16 major-version
  bump — left as-is deliberately rather than jumping a major version
  untested this late in the build.

NOT verified (no way to in this environment — be extra careful here):
- No live Supabase project, so the SQL schema/RPC function, RLS policies,
  and the actual magic-link email flow have not been exercised end to end.
- No real provider API keys, so no adapter has made a real network call —
  each one's SSE parsing was validated against its provider's documented
  wire format, not a live response.
- No Docker available in this build environment, so `backend/Dockerfile`
  has not actually been built or run.
- `fastembed`'s model download (semantic cache) happens on first use at
  runtime — confirm it works and fits Render's free-tier container on
  first deploy; if it doesn't, set `SEMANTIC_CACHE_ENABLED=false`.

Test all of the above against your real Supabase project and provider
keys before relying on this in production.
