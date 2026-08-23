-- Schema Usage
grant usage on schema public to anon, authenticated, service_role;

-- Table Grants for Desktop Client (anon) & Maintainers (service_role)
grant select, insert, update on public.runs to anon, authenticated;
grant select, insert, update on public.run_results to anon, authenticated;
grant select, insert, update on public.custom_models to anon, authenticated;
grant usage, select on all sequences in schema public to anon, authenticated;

grant select, insert, update, delete on public.runs to service_role;
grant select, insert, update, delete on public.run_results to service_role;
grant select, insert, update, delete on public.custom_models to service_role;
grant usage, select on all sequences in schema public to service_role;

-- Enable RLS and Configure Ingestion Policies
alter table public.runs enable row level security;
alter table public.run_results enable row level security;
alter table public.custom_models enable row level security;

-- Allow anon to submit new runs (enforcing approved = false)
drop policy if exists "Allow anon insert unapproved runs" on public.runs;
create policy "Allow anon insert unapproved runs"
    on public.runs for insert to anon
    with check (approved = false);

drop policy if exists "Allow anon insert run results" on public.run_results;
create policy "Allow anon insert run results"
    on public.run_results for insert to anon
    with check (true);

drop policy if exists "Allow anon update pending runs" on public.runs;
create policy "Allow anon update pending runs"
    on public.runs for update to anon
    using (approved = false)
    with check (approved = false);