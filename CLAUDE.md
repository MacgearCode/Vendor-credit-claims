# Vendor Credit Claims — Macgear internal tools site

Despite the folder name, this app hosts **more than one tool**. `/` is a landing
page of tiles; each tool owns a path below it.

Live at **https://apps.macgeargroup.com**. It was `promos.macgeargroup.com` until
2026-08-05, when the second tool made a promos-specific name wrong; that host now
301s to the new one, path preserved, and the redirect should stay. The folder name
and the GitHub repo (`Aaron-Steel/Vendor-credit-claims`) still say claims/promos —
renaming those would break the droplet's clone path for no real gain.

| Path | Tool | Audience |
|---|---|---|
| `/` | Landing page — tile per tool | everyone |
| `/promos` | Vendor Promos — promotional credit claims | sales, finance |
| `/vendor-bills` | PDF Bill Upload — Amazon Advertising → NetSuite CSV | accounts |

Add a tool by adding a tile to `TOOLS` in `app/main.py` and a route below it.

## Run it

```powershell
.\run.ps1          # http://127.0.0.1:8030
```

Port 8030 locally; the container still serves 8000 internally and the docker
service is still named `claims` — **do not change the Docker/Caddy/n8n
references.** n8n posts the nightly pricing sync to `http://claims:8000` on the
shared docker network, so it is unaffected by public hostname changes.

Deployment is `Dockerfile` + `deploy/` on the n8n droplet, behind the Caddy that
already serves n8n. Runbook: `deploy/DEPLOY-BASIC.md`.

Auth is in-app (styled `/login`, signed cookie), configured by `APP_USERNAME` /
`APP_PASSWORD` / `APP_SECRET`. **Blank `APP_PASSWORD` disables auth**, which is
what makes local dev painless — and what would expose the app if it were ever
deployed without those set.

## File roles

Shared shell:

| File | |
|---|---|
| `app/main.py` | All routes, auth middleware, template filters, the `TOOLS` tile list |
| `app/db.py` | Engine, session, additive column migrations |
| `app/models.py` | SQLAlchemy models for both tools |
| `app/templates/base.html` | Shell for every page — `{% block nav %}` and `{% block brandword %}` |
| `app/static/style.css` | All CSS, light + dark. No framework, no build step |

Vendor Promos: `calc.py`, `service.py`, `export.py`, `workflow.py`,
`pricing_sync.py`, `seed.py`, templates `promo_base.html` / `dashboard.html` /
`promo_detail.html` / `promo_new.html` / `help.html`.

Vendor Bill Importer: `bill_calc.py`, `bill_pdf.py`, `bill_export.py`,
`bill_service.py`, template `bills.html`.

Promo pages extend `promo_base.html` (which supplies the promo nav), not
`base.html` directly.

## Vendor Bill Importer — the parts worth knowing

Upload PDFs (or a zip) → review table → download CSV → import into NetSuite by
hand. It does **not** post to NetSuite; the CSV import keeps a human checkpoint,
and the house rule is that apps don't call NetSuite at runtime.

`bill_calc.py` is pure — text in, numbers out — so the allocation and the GST
arithmetic are unit-tested in `tests/test_bill_calc.py` against the five July
2026 invoices that imported successfully on 2026-08-05. `bill_pdf.py` holds the
only I/O.

**The NetSuite reference values in `bill_calc.py` are hard-won.** Subsidiary,
location, department and tax code go as **internal IDs**; vendor, item and class
go as **names**; currency is left out entirely. The full reasoning — including
why the location name fails a Name lookup, and why there is no A/P line and no
GST line — is in `vendorbill/README.md`, which also documents the import mapping
in NetSuite. Read it before changing anything in the config block.

**Expect a 1c GST variance on some invoices.** Amazon rounds GST once on the
invoice total; NetSuite rounds per line and sums. Roughly two in five invoices
land a cent high. That is surfaced as a warning on the review page, not a bug.

**The duplicate guard** is the `processed_invoices` table. External ID on the
NetSuite import is the invoice number, so a second import of the same invoice is
rejected by NetSuite; the review page flags it first. "Mark imported" is manual —
the app has no way to know whether the CSV was actually run.

## Conventions

- Business logic in `calc.py` / `bill_calc.py`, never in a route handler. These
  are analysis tools; the numbers being right is the whole point.
- Server-rendered Jinja2. No SPA, no npm, no build step.
- Timestamps are stored UTC (SQLite `now()`, and the droplet runs UTC). Render
  them through the `sydney` filter or they read a day early all working morning.
- `data/app.db` is live local data. Don't delete, recreate or re-seed it without
  asking.
