-- Widen the allowed `site` values.
--
-- Every retailer table pins `site` to a check constraint listing the sites
-- that existed when it was written. Three things had outgrown it:
--
--   * purplle, kindlife and broadway were added as storefronts, and every
--     row they produced was rejected with 23514
--     ("violates check constraint retailer_products_site_check"),
--   * `manual`, which the spreadsheet import writes, was never in the list at
--     all, so that import could not have stored a row,
--   * retailer_scrape_runs did not even allow `amazon`, so an Amazon run
--     could not open a run record.
--
-- A rejected check constraint fails the whole request, not the offending row,
-- so one disallowed site loses the entire batch it travelled in.
--
-- Safe to run more than once.

begin;

alter table public.retailer_products
    drop constraint if exists retailer_products_site_check;
alter table public.retailer_products
    add constraint retailer_products_site_check
    check (
        site in (
            'nykaa',
            'tira',
            'amazon',
            'purplle',
            'kindlife',
            'broadway',
            'manual'
        )
    );

alter table public.retailer_price_history
    drop constraint if exists retailer_price_history_site_check;
alter table public.retailer_price_history
    add constraint retailer_price_history_site_check
    check (
        site in (
            'nykaa',
            'tira',
            'amazon',
            'purplle',
            'kindlife',
            'broadway',
            'manual'
        )
    );

alter table public.retailer_scrape_runs
    drop constraint if exists retailer_scrape_runs_site_check;
alter table public.retailer_scrape_runs
    add constraint retailer_scrape_runs_site_check
    check (
        site in (
            'nykaa',
            'tira',
            'amazon',
            'purplle',
            'kindlife',
            'broadway'
        )
    );

commit;
