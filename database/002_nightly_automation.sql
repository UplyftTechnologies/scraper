-- Apply this in Supabase SQL Editor when upgrading an existing project.
-- It is idempotent and is also included in schema.sql for new installations.

create table if not exists public.retailer_scrape_runs (
    id uuid primary key default gen_random_uuid(),
    site text not null check (site in ('nykaa', 'tira')),
    status text not null default 'running'
        check (status in ('running', 'success', 'partial', 'failed')),
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    products_seen integer not null default 0,
    products_new integer not null default 0,
    products_changed integer not null default 0,
    products_unchanged integer not null default 0,
    details_refreshed integer not null default 0,
    failures integer not null default 0,
    blocks integer not null default 0,
    requests integer not null default 0,
    message text not null default '',
    metadata jsonb not null default '{}'::jsonb
);

alter table public.retailer_products
    add column if not exists source_fingerprint text not null default '',
    add column if not exists detail_fingerprint text not null default '',
    add column if not exists first_seen_at timestamptz not null default now(),
    add column if not exists last_seen_at timestamptz not null default now(),
    add column if not exists last_checked_at timestamptz not null default now(),
    add column if not exists last_changed_at timestamptz not null default now(),
    add column if not exists last_detail_scraped_at timestamptz,
    add column if not exists detail_refresh_pending boolean not null default false,
    add column if not exists detail_unavailable boolean not null default false,
    add column if not exists last_seen_run_id uuid,
    add column if not exists missing_run_count integer not null default 0,
    add column if not exists is_active boolean not null default true;

create index if not exists retailer_products_active_seen_idx
    on public.retailer_products (site, is_active, last_seen_at desc);

create index if not exists retailer_scrape_runs_site_started_idx
    on public.retailer_scrape_runs (site, started_at desc);

create or replace function public.finalize_retailer_scrape_run(
    p_site text,
    p_run_id uuid,
    p_inactive_threshold integer default 3
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    affected integer;
begin
    update public.retailer_products
    set
        missing_run_count = missing_run_count + 1,
        is_active = (missing_run_count + 1) < greatest(1, p_inactive_threshold)
    where site = p_site
      and last_seen_run_id is distinct from p_run_id;
    get diagnostics affected = row_count;
    return affected;
end;
$$;

revoke all on function public.finalize_retailer_scrape_run(text, uuid, integer)
    from public, anon, authenticated;
grant execute on function public.finalize_retailer_scrape_run(text, uuid, integer)
    to service_role;

alter table public.retailer_scrape_runs enable row level security;
