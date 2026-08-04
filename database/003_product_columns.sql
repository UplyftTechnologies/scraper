-- Apply this in Supabase SQL Editor when upgrading an existing project.
-- It is idempotent and is also included in schema.sql for new installations.
-- Without it, a sync carrying the new columns is rejected by PostgREST.

alter table public.retailer_products
    add column if not exists gtin text not null default '',
    add column if not exists key_ingredients text[] not null default '{}';

create index if not exists retailer_products_gtin_idx
    on public.retailer_products (gtin)
    where gtin <> '';
