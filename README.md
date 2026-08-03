# Beauty Price Comparison Scraper

A config-driven Python tool that collects public beauty/skincare catalogue and
product-detail data from Nykaa, Tira, and Amazon India. It normalizes all three
retailers into one comparison workbook and CSV.

## What is collected

Each SKU/size is a separate row. The normalized output includes:

- retailer, category memberships, parent ID, product ID, and SKU/ASIN
- brand, product name, variant/size, MRP, selling price, discount, and stock
- overall rating, rating count, review count, rating breakdown, and selected
  visible reviews
- product URL, primary image, all gallery images, description HTML/text,
  ingredients, directions/how to use, key features, special features, and
  structured product attributes

Not every retailer publishes every field for every product. Missing source
values remain blank rather than being guessed.

The configured comparison taxonomy is identical for all three sites:

`Kits & Combos`, `Moisturizers`, `Cleansers`, `Serums`, `Masks`, `Sun Care`,
`Korean Beauty`, `Body Care`, `Shop Toners & Mists`, `Lip Care`, `Eye Care`,
`Dermocosmetic Brands`, `Hands & Feet`, `Skin Tools`,
`Specialised Skincare`, `Neck Creams`, and `Skin Supplements`.

## Project layout

```text
pricing_scraper/
  clients/
    base.py          # requests session, cURL parsing, retry/rate limiting
    nykaa.py         # Nykaa listing/detail JSON APIs
    tira.py          # Tira listing/variant JSON APIs
    amazon.py        # Playwright search and public product pages
  checkpoint.py      # resumable listing and product-detail checkpoints
  cli.py
  config.py
  dashboard_service.py
  exporter.py
  models.py
app_pages/
  scraper.py         # "Scraper" tab
  product_view.py    # "Product view" tab
streamlit_app.py     # entry point: both tabs
product_viewer_app.py # entry point: product view only (hosted deployment)
dashboard.py
config.yaml
config.local.yaml    # ignored private Nykaa override
private/             # ignored captured cURL/session files
data/                # generated output/checkpoints
logs/                # request logs, failures, CAPTCHA screenshots
tests/
main.py
```

## Setup

```powershell
cd D:\scraper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
playwright install chromium
python -m unittest discover -s tests -v
```

The Amazon client first tries the installed Chrome channel configured in
`config.yaml`, then falls back to Playwright Chromium.

## Database storage (Supabase)

Every successful export can also upsert the normalized catalogue to Supabase
and append the same observations to a price-history table.

1. Create a Supabase project.
2. Open **SQL Editor** and run `database/schema.sql`.
3. Open `.env` and add the project URL and server-side service-role key:

```dotenv
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_SECRET_KEY=YOUR_SB_SECRET_KEY
```

The database integration enables itself when both values exist. Leave both
blank to keep database writes disabled. With
`DATABASE_SYNC_REQUIRED=true`, a failed database write prevents the dashboard
from reporting **Scraping complete**, while the Excel and CSV files remain
saved locally.

The current Supabase secret key is preferred; the integration also accepts the
legacy `SUPABASE_SERVICE_ROLE_KEY`. `.env` is ignored by Git. Both key types
must stay server-side and must never be embedded in a frontend or committed.

If the original tables already exist, run
`database/002_nightly_automation.sql` once. It adds durable run history,
comparison fingerprints, last-seen timestamps, and safe missing-product
tracking without deleting current catalogue rows.

## Nightly incremental automation

Amazon remains manual. Nykaa and Tira have independent database-backed jobs:

```powershell
python -m pricing_scraper.scheduler --site nykaa
python -m pricing_scraper.scheduler --site tira
```

Each job sweeps its configured category listings, then requests full details
only for a new, changed, incomplete, or periodically stale product. Unchanged
products only receive new checked/seen timestamps. Price history is inserted
only when price or stock changes. Products absent from three complete sweeps
become inactive; partial or blocked sweeps never age missing products.

Every run is saved in `retailer_scrape_runs` with independent Nykaa/Tira
status, product counts, failures, blocks, requests, and start/end times. The
hosted product viewer displays the latest result for both retailers.

### Render deployment

`render.yaml` creates three services:

- `beauty-catalogue`: the `streamlit_app.py` Scraper and Product view tabs
- `beauty-nykaa-nightly`: daily at 19:00 UTC (00:30 IST)
- `beauty-tira-nightly`: daily at 20:00 UTC (01:30 IST)

Deploy the repository as a Render **Blueprint**, not as a single Web Service.
Creating the service by hand ignores `render.yaml`, so the environment values
below are never applied and the two cron jobs are never created. Enter these
server-side values when requested:

```text
APP_PASSWORD            # Web service only: password gate for the public URL
SUPABASE_URL
SUPABASE_SECRET_KEY
NYKAA_CURL_COMMAND      # Nykaa job and the hosted Scraper tab
TIRA_APPLICATION_ID     # Tira job and the hosted Scraper tab
TIRA_APPLICATION_TOKEN  # Tira job and the hosted Scraper tab
```

The hosted Scraper tab can start runs that write to Supabase, so `app_auth.py`
gates both entry points behind `APP_PASSWORD` before any page renders. A
deployment with `HOSTED_DASHBOARD=true` and no `APP_PASSWORD` refuses to serve
rather than publishing the dashboard. Local runs leave the variable unset and
are never prompted. The session unlocks per browser tab, so a reload asks
again.

The hosted Scraper tab offers Nykaa and Tira only. `requirements-render.txt`
omits Playwright, so `amazon_dependencies_available()` reports `False` and the
retailer list drops Amazon rather than offering a run that cannot start.
Amazon stays local, and is intentionally absent from `render.yaml` too.

Render web and cron filesystems are temporary, so anything a hosted run writes
to `data/` or `logs/` disappears on the next deploy or restart. Only the
Supabase rows survive, which is why `HOSTED_DASHBOARD=true` makes the Product
view read the database instead of local checkpoints.

Test the production commands locally before deploying:

```powershell
python -m pricing_scraper.scheduler --site nykaa --config config.local.yaml
python -m pricing_scraper.scheduler --site tira --config config.yaml
```

## Dashboard

Run without CLI arguments:

```powershell
python main.py
```

Open <http://localhost:8501> if it does not open automatically. Choose the
retailer, leave all 17 categories selected or narrow the selection, set a page
safety cap, and select **Collect latest prices**.

The dashboard shows **Scraping complete** only after the selected listing and
detail work is complete and the Excel/CSV files are written. Interrupted runs
resume from `data/checkpoints/`. Refreshing one retailer preserves rows already
saved for the other retailers.

## Read-only product viewer

The dashboard has two top tabs — **Scraper** and **Product view** — so the
viewer is available in the same app on port `8501` without a second process.

Run the independent viewer in a second terminal when you want it on its own
port while scraping continues:

```powershell
cd D:\scraper
.\.venv\Scripts\Activate.ps1
python product_viewer.py
```

Open <http://127.0.0.1:8502>. That entry point shows only the product view and
never writes to checkpoints, exports, or the database.

The default **Live checkpoints** source shows products before the current run
reaches its final Excel/database synchronization. It refreshes every 15
seconds and includes a retail-style product grid plus dedicated product pages
with galleries, variants, prices, stock, descriptions, ingredients, usage,
attributes, ratings, and reviews. A raw data-table view, filters, pagination,
and filtered CSV download remain available. You can also switch to
**Supabase database** or **Latest exported CSV**.

Use another port when needed:

```powershell
python product_viewer.py --port 8503
```

## CLI

PowerShell arguments must be entered on the same command line:

```powershell
python main.py --site nykaa --all-categories
python main.py --site tira --category Moisturizers
python main.py --site amazon --category "Sun Care"
python main.py --site amazon --all-categories
python main.py --site all --category Moisturizers
python main.py --site all --output data\pricing_all.xlsx
```

To split a PowerShell command across lines, end each continued line with a
backtick:

```powershell
python main.py `
  --site amazon `
  --category Moisturizers
```

## Retailer behavior

### Nykaa

The client paginates the captured category listing JSON request and calls
Nykaa's product-details JSON endpoint once per parent. The detail response is
expanded into separate SKU/size rows.

`private/nykaa.curl.txt` contains private session headers and is ignored. If
Nykaa starts returning `403`, CAPTCHA, or expired-session content:

1. Open a configured Nykaa category in Chrome.
2. Open **Developer Tools → Network → Fetch/XHR**.
3. Select the request returning product-list JSON, not analytics `collect`,
   `beauty`, `events`, or `trending` traffic.
4. Choose **Copy → Copy as cURL (bash)**.
5. Replace `private/nykaa.curl.txt` and retry.

Never share or commit this file.

### Tira

The client uses the public collection JSON used by Tira's storefront. It
expands listing variants and calls the public size endpoint when a variant
needs an independent price/SKU. Tira has no dedicated public collection for
Specialised Skincare, Neck Creams, or Skin Supplements, so those labels use
broader live collections plus conservative keyword filters configured in
`config.yaml`.

If the size endpoint returns `401`, refresh `tira.application_id` and
`tira.application_token` from the storefront's own network/bootstrap data.

### Amazon India

The client uses Playwright, not Amazon category JSON. For each selected
category it performs a capped beauty search, opens the discovered public
product pages, and follows selectable size ASINs up to
`max_variants_per_product`.

The browser first opens Amazon's storefront and retains that session's cookies
for search and product requests. A failed or blocked attempt is retried from a
fresh context. This prevents Amazon's normal `202` storefront warm-up and
Akamai verification response from being mistaken for an empty search page.

CAPTCHA pages are detected, screenshotted under `logs/`, retried from a fresh
browser context, and left pending if retries are exhausted. Amazon layouts and
offers vary by location/session, so unavailable fields remain blank. Tune
`search_page_limit`, `max_products_per_category`, delays, and the global
requests-per-minute cap in `config.yaml`.

## Output

Default files:

```text
data/pricing.xlsx
data/pricing_combined.csv
```

The workbook contains `combined` plus one sheet per available retailer,
structured `images` sheets, and a `reviews` sheet. Rows are deduplicated by
`(site, product_id)`. Repeated products retain all matched category labels.
Headers are frozen and bold, filters are enabled, prices are numeric rupee
values, and widths are adjusted automatically.

## Reliability

- Configurable random delays and global requests-per-minute limit
- Exponential retry/backoff for JSON API `403`, `429`, and `5xx` responses
- Soft-block detection for CAPTCHA, access-denied, and unexpected HTML
- Raw failed responses in `logs/failures/`
- Page and product-level failure isolation
- Resumable page/detail checkpoints
- Completion status that distinguishes a finished run from a saved partial run
