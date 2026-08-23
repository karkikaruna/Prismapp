-- PRISM cloud mirror schema.
-- Run this once in the Supabase SQL editor (or via `supabase db push`).
-- Mirrors prism_core/store.py's local SQLite schema; app/services/supabase_sync.py
-- upserts into these tables using the anon key over PostgREST.

create table if not exists public.runs (
    benchmark_run_id  text primary key,
    created_utc       text not null,
    finished_utc      text,
    status            text not null,
    model             text not null,
    model_digest      text,
    datasets          jsonb not null default '[]'::jsonb,
    question_count    integer,
    app_version       text,
    protocol_version  text,
    run_dir           text not null,
    device_id         text,
    synced_at         timestamptz not null default now(),

    -- Maintainer approval gate. Every row lands here as pending (false);
    -- nothing is published to the public repo until a maintainer flips
    -- this to true (see scripts/approve_runs.py). The public-sync job
    -- only ever reads rows where approved = true.
    approved          boolean not null default false,
    approved_at       timestamptz,
    approved_by       text
);

create table if not exists public.run_results (
    id                               bigint generated always as identity primary key,
    benchmark_run_id                 text not null references public.runs(benchmark_run_id) on delete cascade,
    dataset                          text not null,
    model                            text,
    config_fingerprint               text not null,
    n_questions                      integer,
    prompt_response_accuracy         double precision,
    conditional_accuracy             double precision,
    answer_recovery_rate             double precision,
    instruction_compliance_rate      double precision,
    question_majority_accuracy       double precision,
    mean_agreement                   double precision,
    mean_prompt_sensitivity          double precision,
    answer_unanimous_rate            double precision,
    prompt_invariant_incorrect_rate  double precision,
    device_id                        text,
    synced_at                        timestamptz not null default now(),
    unique (benchmark_run_id, dataset)
);

create index if not exists idx_run_results_run on public.run_results(benchmark_run_id);

-- Migration for projects created before the `model` column existed on
-- run_results. `create table if not exists` above is a no-op against an
-- already-existing table, and Postgres (like SQLite) always appends new
-- columns at the *end* - there's no "ADD COLUMN ... AFTER dataset". This
-- gets the column onto the table; it will show up after
-- prompt_invariant_incorrect_rate/device_id/synced_at rather than next to
-- `dataset`. Safe to re-run (idempotent).
alter table public.run_results add column if not exists model text;

-- Optional: reorder `model` to sit right after `dataset`, matching the
-- local SQLite schema exactly. NOT run automatically, because doing this
-- via raw SQL means dropping and recreating the table, which also drops
-- its RLS policies/grants - if this runs and something below fails or is
-- skipped, run_results is briefly left with default deny (RLS enabled, no
-- policies) or worse, fully open, depending on where it stops. The
-- straightforward, low-risk way to reorder is Supabase Studio's Table
-- Editor -> run_results -> drag the `model` column - it does the
-- equivalent rebuild through Supabase's own tested tooling and reapplies
-- RLS/policies/grants for you. If you'd rather do it here anyway, wrap the
-- block below in a single transaction (BEGIN; ... COMMIT;) so it's all-or-
-- nothing, and double check the policies/grants section below still runs
-- after it in the same transaction.
--
-- alter table public.run_results rename to run_results_old;
-- create table public.run_results (
--     id                               bigint generated always as identity primary key,
--     benchmark_run_id                 text not null references public.runs(benchmark_run_id) on delete cascade,
--     dataset                          text not null,
--     model                            text,
--     config_fingerprint               text not null,
--     n_questions                      integer,
--     prompt_response_accuracy         double precision,
--     conditional_accuracy             double precision,
--     answer_recovery_rate             double precision,
--     instruction_compliance_rate      double precision,
--     question_majority_accuracy       double precision,
--     mean_agreement                   double precision,
--     mean_prompt_sensitivity          double precision,
--     answer_unanimous_rate            double precision,
--     prompt_invariant_incorrect_rate  double precision,
--     device_id                        text,
--     synced_at                        timestamptz not null default now(),
--     unique (benchmark_run_id, dataset)
-- );
-- insert into public.run_results (
--     id, benchmark_run_id, dataset, model, config_fingerprint, n_questions,
--     prompt_response_accuracy, conditional_accuracy, answer_recovery_rate,
--     instruction_compliance_rate, question_majority_accuracy, mean_agreement,
--     mean_prompt_sensitivity, answer_unanimous_rate,
--     prompt_invariant_incorrect_rate, device_id, synced_at
-- )
-- select
--     id, benchmark_run_id, dataset, model, config_fingerprint, n_questions,
--     prompt_response_accuracy, conditional_accuracy, answer_recovery_rate,
--     instruction_compliance_rate, question_majority_accuracy, mean_agreement,
--     mean_prompt_sensitivity, answer_unanimous_rate,
--     prompt_invariant_incorrect_rate, device_id, synced_at
-- from public.run_results_old;
-- drop table public.run_results_old;
-- create index if not exists idx_run_results_run on public.run_results(benchmark_run_id);
-- alter table public.run_results enable row level security;
-- create policy "anon can insert run_results" on public.run_results for insert to anon with check (true);
-- create policy "anon can update own run_results (upsert)" on public.run_results for update to anon using (true) with check (true);
-- grant select, insert, update, delete on public.run_results to service_role;

-- "Other models" added from the startup screen's Manage Models panel. Any
-- model tag a user pulls that isn't one of prism_core.config.MODELS' four
-- validated models is upserted here, so it shows up in the picker on every
-- device/run of the app instead of being forgotten after the session ends.
create table if not exists public.custom_models (
    model_tag   text primary key,
    label       text,
    device_id   text,
    added_at    timestamptz not null default now()
);

alter table public.runs enable row level security;
alter table public.run_results enable row level security;
alter table public.custom_models enable row level security;

-- The desktop app only ever uses the anon key. Lock it to insert/upsert only
-- (no delete, no arbitrary reads of other people's rows if you're worried
-- about that) — reads for the GitHub Actions snapshot job should go through
-- a service_role key held only in CI secrets, never in the client.

drop policy if exists "anon can insert runs" on public.runs;
create policy "anon can insert runs"
    on public.runs for insert
    to anon
    with check (true);

-- IMPORTANT: with check (approved = false) means an anon upsert can never
-- set approved = true, and can never re-touch a row a maintainer already
-- approved (that update would be rejected outright). Only the service_role
-- key (used solely by scripts/approve_runs.py, held only as a maintainer-
-- side CI/local secret) can flip approved to true.
drop policy if exists "anon can update own runs (upsert)" on public.runs;
create policy "anon can update own runs (upsert)"
    on public.runs for update
    to anon
    using (approved = false)
    with check (approved = false);

drop policy if exists "anon can insert run_results" on public.run_results;
create policy "anon can insert run_results"
    on public.run_results for insert
    to anon
    with check (true);

drop policy if exists "anon can update own run_results (upsert)" on public.run_results;
create policy "anon can update own run_results (upsert)"
    on public.run_results for update
    to anon
    using (true)
    with check (true);

-- Optional: allow anon SELECT too if you want the desktop app itself (not
-- just CI) to read back cloud data, e.g. to show "N other devices have
-- benchmarked this model". Omit this if you'd rather reads only happen from
-- CI with the service_role key.
-- create policy "anon can read runs" on public.runs for select to anon using (true);
-- create policy "anon can read run_results" on public.run_results for select to anon using (true);

-- custom_models needs anon SELECT (unlike runs/run_results above) because
-- the desktop app itself reads this list back on the startup screen to
-- populate the "Other models on this project" section.
drop policy if exists "anon can read custom models" on public.custom_models;
create policy "anon can read custom models"
    on public.custom_models for select
    to anon
    using (true);

drop policy if exists "anon can insert custom models" on public.custom_models;
create policy "anon can insert custom models"
    on public.custom_models for insert
    to anon
    with check (true);

drop policy if exists "anon can update custom models (upsert)" on public.custom_models;
create policy "anon can update custom models (upsert)"
    on public.custom_models for update
    to anon
    using (true)
    with check (true);
-- Supabase's newer secret-key role needs explicit table grants - unlike
-- the legacy service_role JWT, it doesn't get full table access for free.
-- Run this once (safe to re-run) if push_bundled_results_to_supabase.py or
-- approve_runs.py fail with a 42501 permission error.
grant select, insert, update, delete on public.runs to service_role;
grant select, insert, update, delete on public.run_results to service_role;
grant select, insert, update, delete on public.custom_models to service_role;
grant usage, select on all sequences in schema public to service_role;