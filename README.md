# Beauty Price Comparison Scraper

A config-driven Python tool that collects public beauty/skincare catalogue and
product-detail data from Nykaa, Tira, and Amazon India. It normalizes all three
retailers into one comparison workbook and CSV.

## What is collected

Each SKU/size is a separate row. The normalized output includes:

- retailer, category memberships, parent ID, product ID, and SKU/ASIN
- GTIN/EAN/UPC barcode where the retailer publishes one (see below)
- brand, product name, variant/size, MRP, selling price, discount, and stock
- overall rating, rating count, review count, rating breakdown, and selected
  visible reviews
- product URL, primary image, all gallery images, description HTML/text,
  ingredients, key ingredients, directions/how to use, key features, special
  features, and structured product attributes

Not every retailer publishes every field for every product. Missing source
values remain blank rather than being guessed.

### Key ingredients

`key_ingredients` is a separate list column holding only the highlighted
ingredient names. The full INCI list stays in `ingredients`.

- **Nykaa** embeds the section inside the ingredients HTML, either as a
  bulleted list or as one inline paragraph; both forms are parsed, and a
  product that publishes only an INCI list yields an empty list.
- **Tira** publishes `super-ingredients`. These used to be merged into
  `special_features` alongside claims such as `Cruelty Free`; they now appear
  in `key_ingredients` only, so `special_features` holds claims alone.
- **Amazon India** uses the `Special Ingredients` row, falling back to
  `Active Ingredients`. That row is no longer copied into `special_features`,
  where it previously dumped the entire ingredient list into a feature cell.

### GTIN/EAN/UPC

The `gtin` column holds the retailer's own barcode for that exact SKU:

- **Nykaa** publishes one per SKU, so this column is populated for Nykaa rows.
- **Tira** publishes only its internal numeric item code, which is already
  exported as `sku`. `gtin` stays blank unless Tira starts returning a real
  barcode in its identifier lists.
- **Amazon India** does not list UPC/EAN on beauty product pages — the detail
  table carries ASIN, model number, and part number only — so `gtin` stays
  blank. The parser still reads a `UPC`/`EAN`/`GTIN` row if one appears.

The `gtin` value is only accepted when it is a digit string of GTIN-8/12/13/14 length
whose GS1 check digit is valid, so a seller code that merely looks numeric is
never exported as a barcode. Parent-level barcodes are never copied onto other
size variants: each row carries its own barcode or nothing.

The configured comparison taxonomy is identical for all three sites:

`Kits & Combos`, `Moisturizers`, `Cleansers`, `Serums`, `Masks`, `Sun Care`,
`Korean Beauty`, `Body Care`, `Shop Toners & Mists`, `Lip Care`, `Eye Care`,
`Dermocosmetic Brands`, `Hands & Feet`, `Skin Tools`,
`Specialised Skincare`, `Neck Creams`, and `Skin Supplements`.

## Project layout

Two entry points, one library, one config file:

```text
main.py                  # python main.py -> dashboard; with arguments -> CLI
streamlit_app.py         # the dashboard itself (Streamlit runs this file)
app_auth.py              # password gate for the hosted deployment

pricing_scraper/         # all scraping and export logic
  clients/
    base.py              # requests session, cURL parsing, retry/rate limiting
    nykaa.py             # Nykaa listing/detail JSON APIs
    tira.py              # Tira listing/variant JSON APIs
    amazon.py            # Playwright search and public product pages
  automation.py          # nightly incremental sweep
  checkpoint.py          # resumable listing and product-detail checkpoints
  cli.py                 # command-line interface
  config.py              # YAML loading and .env overrides
  dashboard_service.py   # collection runs the dashboard calls
  database.py            # Supabase sync
  exporter.py            # Excel/CSV output
  models.py              # the Product record
  scheduler.py           # cron entry point

config.yaml              # retailers, categories, request limits
database/                # Supabase schema and migrations
tests/
requirements.txt         # local install (includes Playwright for Amazon)
requirements-render.txt  # hosted install (no Playwright)
Dockerfile, render.yaml  # deployment

config.local.yaml        # ignored private Nykaa override
.env                     # ignored secrets and SCRAPE_BRANDS
private/                 # ignored captured cURL/session files
data/                    # generated output and checkpoints
logs/                    # request logs, failures, CAPTCHA screenshots
```

## Limiting the run to specific brands

`SCRAPE_BRANDS` in `.env` restricts every retailer to a comma-separated list:

```dotenv
SCRAPE_BRANDS=COSRX, Laneige, d'Alba Piedmont
```

Leave it blank to keep every brand the retailer returns. The variable replaces
the `brands:` list in `config.yaml` whenever it is set, and it applies to the
dashboard, the CLI, and the nightly scheduler alike.

Matching ignores case, spacing, and punctuation, so `dalba piedmont` still
matches `d'Alba Piedmont`. It is otherwise an exact brand-name match: a
partial name such as `d'Alba` does not match `d'Alba Piedmont`, so copy the
brand exactly as the retailer displays it. The sidebar shows the active list,
and an unmatched name simply contributes no rows.

Brands are filtered while listings are parsed, so a filtered Nykaa or Tira run
skips the product-detail requests for excluded products. Amazon searches by
category first and drops non-matching brands from the results.

A brand-filtered nightly run is treated as a partial sweep: it never sees the
rest of the catalogue, so it never ages other brands into `is_active = false`.
Filtered runs still add and update rows normally.

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

On an existing project, apply the migrations once, in order:

- `database/002_nightly_automation.sql` adds durable run history, comparison
  fingerprints, last-seen timestamps, and safe missing-product tracking
  without deleting current catalogue rows.
- `database/003_product_columns.sql` adds the `gtin` and `key_ingredients`
  columns.

Supabase rejects the whole product sync until a new column exists, so apply
the migrations before the next run.

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
status, product counts, failures, blocks, requests, and start/end times.

### Render deployment

Everything the platform needs is committed: one `Dockerfile` builds the image,
and `render.yaml` declares all three services. Deploy in three steps:

1. Run `database/schema.sql` (new project) or the migrations above (existing
   project) in the Supabase SQL Editor.
2. In Render, choose **New → Blueprint** and point it at this repository.
3. Fill in the secret values Render prompts for (listed below), then deploy.

Deploy as a **Blueprint**, not as a single Web Service. Creating the service by
hand ignores `render.yaml`, so the environment values below are never applied
and the two cron jobs are never created.

The blueprint creates:

- `beauty-catalogue`: the `streamlit_app.py` dashboard, bound to Render's
  `${PORT}` by the Dockerfile
- `beauty-nykaa-nightly`: daily at 19:00 UTC (00:30 IST)
- `beauty-tira-nightly`: daily at 20:00 UTC (01:30 IST)

All three build from the same image and install `requirements-render.txt`.
Enter these server-side values when requested:

```text
APP_PASSWORD            # Web service only: password gate for the public URL
SUPABASE_URL
SUPABASE_SECRET_KEY
NYKAA_CURL_COMMAND      # Nykaa job and the hosted dashboard
TIRA_APPLICATION_ID     # Tira job and the hosted dashboard
TIRA_APPLICATION_TOKEN  # Tira job and the hosted dashboard
```

The hosted dashboard can start runs that write to Supabase, so `app_auth.py`
gates the entry point behind `APP_PASSWORD` before any page renders. A
deployment with `HOSTED_DASHBOARD=true` and no `APP_PASSWORD` refuses to serve
rather than publishing the dashboard. Local runs leave the variable unset and
are never prompted. The session unlocks per browser tab, so a reload asks
again.

The hosted dashboard offers Nykaa and Tira only. `requirements-render.txt`
omits Playwright, so `amazon_dependencies_available()` reports `False` and the
retailer list drops Amazon rather than offering a run that cannot start.
Amazon stays local, and is intentionally absent from `render.yaml` too.

Render web and cron filesystems are temporary, so anything a hosted run writes
to `data/` or `logs/` disappears on the next deploy or restart. Only the
Supabase rows survive.

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
