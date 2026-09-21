-- =====================================================================
-- AI Router Platform — Supabase (PostgreSQL) Schema
-- =====================================================================
-- Design notes:
--   * `users` is a *public* metadata mirror of Supabase's internal
--     `auth.users`. We never write auth data directly — a trigger on
--     auth.users keeps this table in sync on signup/update.
--   * `conversations` and `messages` are the hot path. `messages` is
--     append-only from the app's perspective; JSONB `provider_meta`
--     carries racing/failover telemetry without needing a schema
--     migration every time we add a provider.
--   * All foreign keys cascade on delete so removing a user or a
--     conversation cannot leave orphaned rows.
--   * RLS is enabled everywhere. Policies scope every row to
--     `auth.uid()`, so the anon/authenticated key is safe to ship to
--     the browser. The FastAPI service uses the service_role key and
--     bypasses RLS for the async background insert.
-- =====================================================================

create extension if not exists "pgcrypto";
create extension if not exists "vector";

-- ---------------------------------------------------------------------
-- users: public metadata mirror of auth.users
-- ---------------------------------------------------------------------
create table if not exists public.users (
    id                uuid primary key references auth.users (id) on delete cascade,
    email             text not null,
    display_name      text,
    avatar_url        text,
    default_provider  text not null default 'openrouter',
    -- per-user plan/quota bookkeeping — cheap free-tier rate limiting
    daily_message_cap integer not null default 200,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

comment on table public.users is
    'Mirror of auth.users, kept in sync by handle_new_user()/handle_user_update() triggers.';

-- ---------------------------------------------------------------------
-- conversations
-- ---------------------------------------------------------------------
create table if not exists public.conversations (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references public.users (id) on delete cascade,
    title        text not null default 'New conversation',
    -- persisted per-conversation settings: system prompt, temperature,
    -- preferred provider order, etc. Kept as JSONB so the frontend can
    -- add settings without a migration.
    settings     jsonb not null default '{}'::jsonb,
    is_archived  boolean not null default false,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists idx_conversations_user_id
    on public.conversations (user_id, updated_at desc);

-- ---------------------------------------------------------------------
-- messages
-- ---------------------------------------------------------------------
create type public.message_role as enum ('system', 'user', 'assistant');

create table if not exists public.messages (
    id                uuid primary key default gen_random_uuid(),
    conversation_id   uuid not null references public.conversations (id) on delete cascade,
    user_id           uuid not null references public.users (id) on delete cascade,
    role              public.message_role not null,
    content           text not null,

    -- --- provider / racing telemetry (populated for role='assistant') ---
    winning_provider  text,                       -- e.g. 'groq', 'openrouter'
    winning_model     text,                       -- e.g. 'llama-3.3-70b-versatile'
    latency_ms        integer,                    -- time-to-first-token of the winner
    total_duration_ms integer,                    -- full stream duration
    token_count       integer,
    -- structured record of every provider that entered the race:
    -- [{ "provider": "groq", "status": "won" | "cancelled" | "error",
    --    "ttft_ms": 210, "error": null }, ...]
    provider_meta     jsonb not null default '[]'::jsonb,

    created_at        timestamptz not null default now()
);

create index if not exists idx_messages_conversation_id
    on public.messages (conversation_id, created_at asc);

create index if not exists idx_messages_user_id
    on public.messages (user_id, created_at desc);

-- GIN index lets us query/aggregate provider win-rates cheaply, e.g.
-- for a "which provider is fastest today" dashboard.
create index if not exists idx_messages_provider_meta
    on public.messages using gin (provider_meta);

-- ---------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_users_updated_at on public.users;
create trigger trg_users_updated_at
    before update on public.users
    for each row execute function public.set_updated_at();

drop trigger if exists trg_conversations_updated_at on public.conversations;
create trigger trg_conversations_updated_at
    before update on public.conversations
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------
-- auth.users -> public.users sync trigger
-- ---------------------------------------------------------------------
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.users (id, email, display_name, avatar_url)
    values (
        new.id,
        new.email,
        coalesce(new.raw_user_meta_data ->> 'full_name', split_part(new.email, '@', 1)),
        new.raw_user_meta_data ->> 'avatar_url'
    )
    on conflict (id) do nothing;
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
    after insert on auth.users
    for each row execute function public.handle_new_user();

-- Keep email/display name in sync if the user updates them via
-- Supabase Auth (e.g. email change confirmation).
create or replace function public.handle_user_update()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.users
       set email = new.email,
           display_name = coalesce(new.raw_user_meta_data ->> 'full_name', display_name)
     where id = new.id;
    return new;
end;
$$;

drop trigger if exists on_auth_user_updated on auth.users;
create trigger on_auth_user_updated
    after update on auth.users
    for each row execute function public.handle_user_update();

-- ---------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------
alter table public.users         enable row level security;
alter table public.conversations enable row level security;
alter table public.messages      enable row level security;

drop policy if exists "users_select_own" on public.users;
create policy "users_select_own"
    on public.users for select
    using (auth.uid() = id);

drop policy if exists "users_update_own" on public.users;
create policy "users_update_own"
    on public.users for update
    using (auth.uid() = id);

drop policy if exists "conversations_owner_all" on public.conversations;
create policy "conversations_owner_all"
    on public.conversations for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

drop policy if exists "messages_owner_all" on public.messages;
create policy "messages_owner_all"
    on public.messages for all
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Service role (used exclusively by the FastAPI backend for the
-- async background log write) bypasses RLS automatically — no
-- policy needed for it.

-- =====================================================================
-- Semantic response cache (pgvector)
-- =====================================================================
-- Only ever written/read by the backend's service_role key, keyed on a
-- 384-dim embedding (fastembed's BAAI/bge-small-en-v1.5 — a small ONNX
-- model, no PyTorch/GPU needed, safe to run in a free-tier container).
-- Deliberately NOT scoped to a user — a cache hit on a generic factual
-- question benefits every user, and nothing sensitive/personal is cached
-- because main.py only consults the cache for single-turn (no prior
-- conversation context) requests.
create table if not exists public.response_cache (
    id               uuid primary key default gen_random_uuid(),
    query_text       text not null,
    query_embedding  vector(384) not null,
    response_text    text not null,
    provider         text not null,
    hit_count        integer not null default 0,
    created_at       timestamptz not null default now(),
    last_hit_at      timestamptz not null default now()
);

-- Approximate nearest-neighbor index for cosine distance (`<=>`). Lists=100
-- is a reasonable default for a cache that stays in the thousands-of-rows
-- range; rebuild with a higher `lists` value if this table grows large.
create index if not exists idx_response_cache_embedding
    on public.response_cache
    using ivfflat (query_embedding vector_cosine_ops)
    with (lists = 100);

alter table public.response_cache enable row level security;
-- No policies defined -> RLS default-denies all access via the anon/
-- authenticated key. Only the service_role key (used exclusively by the
-- FastAPI backend) can read or write this table, which is intentional:
-- cached answers are served back through the backend, never queried
-- directly from the browser.

-- Parameter is prefixed `p_` specifically so it can't collide with the
-- `response_cache.query_embedding` column name inside the query below —
-- Postgres does not let you disambiguate same-named columns/params by
-- qualifying a parameter with the function's name.
create or replace function public.match_cached_response(
    p_query_embedding vector(384),
    match_threshold float,
    match_count int default 1
)
returns table (
    id uuid,
    response_text text,
    provider text,
    similarity float
)
language sql
stable
as $$
    select
        response_cache.id,
        response_cache.response_text,
        response_cache.provider,
        1 - (response_cache.query_embedding <=> p_query_embedding) as similarity
    from public.response_cache
    where 1 - (response_cache.query_embedding <=> p_query_embedding) > match_threshold
    order by response_cache.query_embedding <=> p_query_embedding
    limit match_count;
$$;
