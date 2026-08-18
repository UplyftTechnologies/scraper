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

- **Nykaa** publishes one per SKU in its product-detail response, so coverage
  is close to total — but only for rows collected with detail enrichment. A
  listing-only sweep leaves the column blank.
- **Tira** publishes a real barcode for about two thirds of its catalogue. It
  used to expose only its internal numeric item code, which is exported as
  `sku`; rows without a published barcode still leave `gtin` blank.
- **Amazon India** has no UPC/EAN row on beauty product pages — the detail
  table carries ASIN, model number, and part number only. Many sellers put the
  product's real EAN in the model or part number, so those rows are read as a
  fallback under the extra restrictions below.

The `gtin` value is only accepted when it is a digit string of GTIN-8/12/13/14
length whose GS1 check digit is valid, so a seller code that merely looks
numeric is never exported as a barcode. Parent-level barcodes are never copied
onto other size variants: each row carries its own barcode or nothing.

Amazon's model/part-number fallback is held to a stricter standard than a
published barcode row, because the field usually holds something else:

- only EAN-13 and GTIN-14 lengths are accepted. An eight-digit model number
  passes the GTIN-8 check digit one time in ten, and every such value measured
  against Nykaa's published barcode disagreed with it.
- GS1 prefixes reserved for coupons and in-store use are refused, which is what
  separates a padded internal code such as `992880990000` from a genuine EAN
  such as `8904417306224` (890 = India).

Measured against Nykaa's published barcodes for the same products, the values
Amazon yields this way agreed in 10 of 13 cases; the exceptions were valid
barcodes for a different pack, which the cross-platform matcher then rejects
as a mismatched pair.

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

The barcode-only mode applies the same filter separately. It reaches products
by ID out of the saved catalogue rather than through a listing, so the clients'
own filter never sees them — and a catalogue collected under an older
`SCRAPE_BRANDS` still holds brands that are no longer wanted. Those products
are skipped and reported, and cross-filled barcodes respect the filter too.

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
saved locally: the export is written first and the database is contacted
afterwards, so an outage costs the sync, never the collection.

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

### Streaming to the database during a run

A run writes each batch of products to Supabase as it scrapes, at the same
points it commits to its checkpoint — every listing page and every enriched
parent. Batches are 100 rows, so a ten-thousand product catalogue costs about
100 requests on top of the ~5,000 the scrape already makes.

This replaced an end-of-run sync that pushed the whole catalogue at once:

- **Memory stays flat.** Nothing accumulates for a single large write.
- **A run that dies keeps its work.** Previously a collection that failed at
  hour two of three wrote nothing at all.
- **The database fills in real time** rather than in one lump at the end.

The end-of-run sync now only runs when a streamed batch failed, acting as the
reconciliation pass. A failed batch never ends a run: the checkpoint is still
the source of truth for resuming, and the export reconciles the rest.

After a **complete** sweep the run calls `finalize_retailer_scrape_run`, so
products the sweep never saw age towards inactive — the same bookkeeping the
nightly jobs use. A partial or stopped run skips it, because a run that ended
early has no opinion about what is missing. So does a run with a failed batch,
since the gap would make live products look absent.

### Hosted runs skip the workbook

With `HOSTED_DASHBOARD=true` the export writes the CSV and **skips the Excel
workbook**. Building it holds every cell in memory — around 500 MB for a
ten-thousand product catalogue — which exceeds a Render `starter` instance, and
the file would not survive the next restart on a temporary disk anyway. Local
runs are unchanged and still write both files.

The CSV is streamed row by row and is written before the workbook, so a
catalogue survives even when the workbook cannot be built.

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

### Runs continue after you close the browser

Selecting **Collect latest prices** starts the collection in a separate
process, so it is not tied to the browser session. Close the tab, put the
machine's browser away, or reopen the dashboard from another device: the run
keeps going and the dashboard reattaches to its progress.

This matters because Streamlit drops a session about two minutes after its tab
closes, which would otherwise kill a multi-hour collection.

- Progress, counters, and the outcome live in `data/runs/<run_id>.status.json`;
  the worker's output is in the matching `.log` file.
- One run at a time. A second start is refused while a run is working, because
  concurrent runs would write the same checkpoints and export files.
- **Stop this run** asks the worker to stop at its next progress checkpoint,
  leaving the checkpoint intact so the next run resumes from there. The worker
  also checks the request while it waits out a retry backoff, so a stop does
  not have to wait for a minute-long delay to elapse first.
- A worker that dies without finishing is reported as failed the next time the
  dashboard looks, rather than blocking new runs. Liveness is not judged from
  the recorded process id alone: the operating system reuses process ids, so a
  status left behind by a killed worker could otherwise point at an unrelated
  live process and keep a finished run looking active forever. A run is retired
  when its process is gone or is not the worker, when the machine booted after
  the run last reported, or when it has not reported for 30 minutes.

The run lives in the server process, not the browser, so it still ends if the
server itself stops. On Render that means a restart or redeploy cancels an
in-flight run; the checkpoint survives only until the container is replaced.

## Skipping products that are already up to date

Every run compares what it discovers against the catalogue already stored, and
requests a product only when there is a reason to. A product is requested when:

- it is **new** — no stored record exists
- the listing shows it **changed** — price, stock, or name differs from what is
  stored, caught the same day regardless of age
- its stored record is **incomplete** — missing any of `refresh.required_fields`
- its stored record is **stale** — older than `refresh.refresh_days` (30)

Anything else is left alone: its stored detail is reused and written to the
export exactly as a fresh fetch would be, so skipping never costs content.

The comparison reads Supabase when `database.enabled` is true and credentials
exist, and the combined CSV otherwise. If neither can be read the run simply
requests everything, which is what it did before this existed — a freshness
check may cost extra requests, never the run.

```powershell
python main.py --site nykaa            # skips products already up to date
python main.py --site nykaa --full     # re-requests everything
```

The dashboard exposes the same thing as **Skip products already up to date**.
Turn it off in `config.yaml`:

```yaml
refresh:
  enabled: true
  refresh_days: 30
  required_fields: [description, image_urls]
```

Keep `required_fields` narrow. `ingredients`, `how_to_use`, and `gtin` are
legitimately absent for many real products, so requiring them would mark those
products incomplete on every run and re-request them forever. Use the
barcode-only mode below to fill barcodes instead.

This is separate from the checkpoint. A checkpoint stops one interrupted run
from repeating work it already did; this stops a *new* run from repeating work
an *earlier* run did.

## Collecting barcodes only

Filling a missing `gtin` through a normal run means re-requesting each
product's whole detail payload. `--gtin-only` asks each retailer for the
cheapest thing that yields a barcode instead, and writes no other field:

```powershell
python main.py --site nykaa --gtin-only
python main.py --site amazon --gtin-only --gtin-limit 200
python main.py --site all --gtin-only --refresh-all-gtins
```

| Site | Barcode source | Cost |
| --- | --- | --- |
| Tira | listing JSON | a listing sweep; no product opened individually |
| Nykaa | product-detail endpoint | one request per parent still missing one |
| Amazon | product-information table | free from stored attributes, then one page each |

Amazon is read offline first: because its barcode lives in the model/part
number, a catalogue scraped before that was supported already contains the
answer, and only products with no stored attributes need a page opened.
Re-opening a page whose attributes are already held returns the same
product-information table, so it cannot yield a barcode the offline pass
missed — a full sweep spent 522 page opens confirming that and found nothing.
Pass `recheck_pages=True` to force them anyway, for products whose seller may
have added an EAN since the last scrape.

### Why Amazon coverage stays low

Amazon India publishes no barcode field, and only about 15% of sellers put a
real EAN in the model number. Of 596 Amazon products carrying a model or part
number, 91 were valid barcodes; the rest are seller SKUs, company names, or
values like `1` and `50ml+100g`. That is the native ceiling, not a parsing gap.

The remaining source is a platform that does publish barcodes. A cross-platform
match is the same physical product, so its barcode applies, and the sweep fills
what it can from matched products on other platforms:

| match threshold | agreed | disagreed | accuracy |
| --- | --- | --- | --- |
| 0.70 | 26 | 3 | 90% |
| 0.80 | 23 | 1 | 96% |
| 0.90 | 25 | 0 | 100% |

Measured against pairs where two platforms each published their own barcode.
The default `cross_fill_threshold` is therefore `0.90`: looser matches pair
different pack sizes of the same product, and copying a barcode across them
would state something false about the SKU. Borrowed barcodes are reported
separately as `matched <platform>`.

Because Nykaa publishes a barcode per SKU, running `--site nykaa --gtin-only`
first materially raises what Amazon can borrow afterwards.

Only products with an empty `gtin` are requested unless `--refresh-all-gtins`
is passed, and `--gtin-limit` caps how many are requested in one go. The
dashboard offers the same thing as **Collect GTINs only**.

A barcode sweep writes the Excel and CSV **and syncs to Supabase**, exactly as
a normal collection does, so the barcodes land in `retailer_products` rather
than only in the local files. Pass `--gtin-no-sync-db` for a local-only sweep.
If nothing new is found, neither the files nor the database are touched.

## Inserting products from a sheet

Some products never turn up in a sweep. The dashboard sidebar has an
**Insert products from a sheet** panel: upload a CSV or Excel file, then

- **Check only** — report what would happen and write nothing
- **Insert** — write the new products into Supabase

Only rows Supabase does not already hold are inserted. Nothing is updated and
nothing is deleted, so running the same sheet twice is harmless.

`manual_products.template.csv` shows the columns; only `brand` and
`product_name` are required. Headers are matched by name, so `Vendor`/`Title`
from a Shopify or marketplace export work unedited.

A row counts as already present when its `site` + `product_id` matches a stored
product, or when its brand and product name match one — so a hand-written row
does not need an ID the retailer assigned. Matching folds case and punctuation
the same way the brand filter does, so `AKIND` still matches `Akind`.
Duplicates within the sheet itself are caught too, and a row with no
`product_id` is given a stable generated one so re-uploading the same file
cannot create a second copy.

Only Supabase is consulted. The local CSV export is deliberately ignored: a
product sitting in the file but missing from the database is exactly the row
this is meant to insert.

### Inserted rows get their own site

A row with no `site` is filed under `manual` rather than a retailer, because
the retailer pipeline would otherwise destroy it:

- `merge_with_existing_sites` replaces one site's rows wholesale on every
  export, so a manual row filed under `nykaa` disappears at the next Nykaa run.
- `finalize_retailer_scrape_run` ages rows of the swept site that the sweep did
  not see, so the same row would be counted missing and go inactive.

Set the `site` column explicitly if you want a row to belong to a retailer
anyway; the panel warns and names the sites affected. The default for rows that
name no site is editable next to the uploader.

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

#### Searching by brand as well as by category

Amazon has no catalogue to paginate, so a category is approximated by a generic
search — `face serum skincare` — and Amazon answers with whatever it ranks
highest. Your brands are never named in the query, so a brand that does not
happen to rank is never seen. Measured against a 207-brand filter, category
searches alone returned products for **50 brands**, and eight of fifteen
categories stopped at `max_products_per_category` rather than because Amazon
had run out.

Each configured brand is therefore also searched by name, which asks Amazon for
that brand's own catalogue:

```yaml
amazon:
  brand_search:
    enabled: true
    page_limit: 2
    max_products_per_brand: 40
```

Both sets of results are merged by ASIN, so a product found twice is fetched
once. The discovery ceiling rises from `17 × 40 = 680` products to roughly
9,000 before deduplication and the brand filter, at the cost of 448 search
pages instead of 34.

A product found by brand search has no category, because no category asked for
it. Its labels are inferred from the same category queries by matching their
distinctive words against the product's title and generic name; words that
appear in nearly every query (`skincare`, `face`, `beauty`) are ignored, and a
product matching nothing keeps an empty category list rather than being forced
into one it is not in.

Set `brand_search.enabled: false` to return to category searches only.

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

## Cross-platform comparison

`build_comparison.py` matches the same product across platforms and writes one
row per product with a price column per platform:

```powershell
python build_comparison.py --own data/roopsee_catalogue.csv
python build_comparison.py --source csv --threshold 0.78
python build_comparison.py --own catalogue.xlsx --brand "Minimalist, COSRX"
```

Retailer rows come from Supabase when it is configured, otherwise from
`data/pricing_combined.csv` (`--source csv` forces the file). The own catalogue
is any CSV or Excel export; its headers are matched by name, so a Shopify
product export works unedited. `roopsee_catalogue.template.csv` shows the
columns, of which only `brand`, `product_name`, and a price are required.

### How products are matched

No identifier is shared across platforms: only Nykaa publishes a usable GTIN,
Tira exposes its internal item code, and Amazon India omits barcodes on beauty
pages. Two rows with barcodes are matched on the barcode; everything else is
matched on brand, pack size, and title wording, and a pair is rejected outright
when it disagrees on any of:

- **brand** — compared with the same key the scrape filter uses
- **pack size** — `2 x 50 ml` never matches a single 100 ml bottle
- **product form** — a sunscreen never matches a moisturiser
- **concentration or SPF** — `Retinol 0.3%` never matches `Retinol 0.6%`
- **kit vs single product** — a combo listing is its own item

What survives is scored 0-1 from how much of the two titles overlap. The
default `--threshold` is `0.70`; matches below `--review-below` (`0.80`) or
carrying a caveat are copied to a `review` sheet, because a wrong pairing
silently misreports a competitor's price. Each platform contributes at most one
row per match, and every row is used at most once.

### Output

`data/comparison.xlsx` holds three sheets, and `data/comparison.csv` repeats
the first one:

- `comparison` — brand, product, form, size, then per platform the selling
  price, MRP, discount, stock, name and URL, then `min_price`, `max_price`,
  `price_gap`, `cheapest_platform`, and how the own catalogue compares
  (`roopsee_vs_cheapest`)
- `review` — the matches worth a human glance
- `single_platform` — products found on only one platform, for gap analysis

## Request pacing

The retailer APIs answer in well under a second — measured medians are ~0.35s
for a Nykaa detail call and ~0.9s for a Tira listing page — so throughput is
decided entirely by `request.delay_*_seconds` and
`request.max_requests_per_minute` in `config.yaml`, not by the network.

The defaults are 0.5–1.5s delays under a 30/minute cap, which a bounded probe
sustained at 32 requests/minute against Nykaa and Tira with no failures and no
blocks. The previous 2–5s delays under a 12/minute cap left the scraper idle
about 93% of the time and made a full Nykaa refresh an eight-hour job.

Raise the cap in steps rather than in one jump, and check `Failures` and
`Blocks` in the run summary afterwards. A cap that is too high is recoverable
rather than free: soft blocks trigger backoff, checkpoints make the run
resumable, and the only manual cost is re-copying the Nykaa cURL if its session
is rejected.

## Reliability

- Configurable random delays and global requests-per-minute limit
- Exponential retry/backoff for JSON API `403`, `429`, and `5xx` responses
- Soft-block detection for CAPTCHA, access-denied, and unexpected HTML
- Raw failed responses in `logs/failures/`
- Page and product-level failure isolation
- Resumable page/detail checkpoints
- Checkpoint state is flushed to disk before it is renamed into place, and a
  file left damaged by a crash is moved aside and rebuilt from the append-only
  products and processed files rather than ending the run
- Excel and CSV are written before the database is contacted, so a Supabase
  outage cannot discard a collection that already ran
- Idempotent Supabase writes are retried through transient timeouts and `5xx`
  responses (`DATABASE_MAX_ATTEMPTS`, default 4). The end-of-sweep
  `finalize_retailer_scrape_run` call is sent exactly once because it counts
  how many sweeps have missed each product
- Database snapshots are read with keyset pagination, so a growing table cannot
  silently return a short catalogue to the nightly comparison
- Completion status that distinguishes a finished run from a saved partial run
