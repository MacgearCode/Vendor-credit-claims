# Macgear internal tools (Vendor Credit Claims)

Two tools behind one login. `/` is a landing page; each tool has its own path.

| Path | Tool |
|---|---|
| `/promos` | **Vendor Promos** — promotional credit claims, the original app |
| `/vendor-bills` | **PDF Bill Upload** — Amazon Advertising invoices → NetSuite vendor bill CSV |
| `/admin/users` | **Users** — accounts and per-tool access (admins only) |

Everyone signs in with their own email and password. Admins see every tool and
manage accounts; everyone else sees only the tools ticked on their account —
enforced on the URL, not just hidden from the landing page. New accounts get a
single-use link and choose their own password; nobody else ever knows it.

Live at **https://apps.macgeargroup.com** (was `promos.macgeargroup.com`, which now
redirects). See [CLAUDE.md](CLAUDE.md) for how the two tools share the shell, and
[deploy/DEPLOY-BASIC.md](deploy/DEPLOY-BASIC.md) to deploy or change the hostname.

---

## Vendor Promos

Manage promotional credit claims end-to-end — create promos, track inbound
**customer claims**, and raise outbound **vendor credit requests** — replacing the
Monday.com + per-promo Excel workflow. See [SPEC.md](SPEC.md) for the full domain model.

### What it does (Phase 1)

- **Create a promotion** — brand, dates, AUD→USD rate, a default 3-way funding split
  (Retailer / MacGear / Brand), and the retailers running it (rebate pre-filled per retailer).
- **Add line items** per retailer by SKU code — description, RRP and channel buy-price
  auto-fill from the imported product master. The calc engine (ported 1:1 from the Excel
  template and unit-tested against it) computes supplier/MG support, margins, expected
  sales, and both claim amounts.
- **Three views** of every promo — *Internal* (everything), *Sales/Customer* (hides the
  funding split), *Vendor* (supplier support + claim only) — each exportable to Excel.
- **Lifecycle tracking** — per-customer claim status (Not Received → Received → Verified →
  Credited) with variance vs the expected claim, and a vendor request (Not Sent → Sent →
  Approved → Credited) with AUD/USD amounts. Dashboard shows all promos at a glance.

---

## PDF Bill Upload

Turns Amazon Advertising tax invoice PDFs into a NetSuite **Vendor Bill** import
CSV, with the advertising spend split across brands (classes) so the P&L lands on
the right brand without anyone re-keying it. Built for the accounts team, who
can't run a PowerShell script.

1. Drop in a day's PDFs, several, or the whole zip.
2. Check the review table against the invoices — ex-tax, GST as the invoice states
   it, GST as NetSuite will calculate it, and the brand split.
3. Download the CSV and import it in NetSuite (**Setup → Import/Export → Import
   CSV Records**, saved mapping *Amazon Advertising Vendor Bill*).

Things it deliberately surfaces rather than hides:

- **A 1c GST difference on some invoices.** Amazon rounds GST once on the invoice
  total; NetSuite rounds per line and sums. About two in five land a cent high.
  Expected, not a parse error.
- **Invoices already exported**, flagged before you download. External ID on the
  import is the invoice number, so NetSuite would reject a second import anyway.
- **A portfolio that matches no brand rule**, which otherwise falls silently to
  MOVA. That's the warning that says a new brand has started advertising.

The command-line version this was ported from still lives in `vendorbill/`, and
its README documents the NetSuite import mapping and reference values in full —
read that before touching the config block in `app/bill_calc.py`.

---

## Run it

```powershell
# from the project folder:
./run.ps1
```

That creates the virtualenv + seeds the database on first run, then serves the app at
**http://127.0.0.1:8030**. Stop with Ctrl+C.

Manual equivalent:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\extract_seed.py   # template -> data/seed_*.json
.\.venv\Scripts\python.exe -m app.seed               # create + seed data/app.db
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## Refreshing reference data

Product master and retailer rebates are seeded from `AU_Promo Form_TEMPLATE.xlsx`
(Master Price Level + Data Validation sheets). When that file changes, re-run:

```powershell
.\.venv\Scripts\python.exe scripts\extract_seed.py
.\.venv\Scripts\python.exe -m app.seed   # idempotent: updates existing, adds new
```

## Layout

```
app/
  calc.py        calculation engine (pure, unit-tested vs the template)
  service.py     resolves overrides + rolls totals up to retailer/promo level
  models.py      SQLAlchemy models
  db.py          SQLite engine/session
  seed.py        create tables + load reference data
  export.py      Excel export (internal / sales / vendor)
  main.py        FastAPI routes
  templates/     Jinja pages
  static/        CSS
scripts/extract_seed.py   one-off template -> JSON extractor
tests/test_calc.py        verifies calc matches the template numbers
data/                     seed JSON + app.db (db is gitignored)
```

## Conventions

- Rebates, funding ratios and growth are stored as **fractions** (0.20 = 20%).
- Per-line "% off" is entered as a **percentage** in the UI and stored as a fraction.
- All money is **AUD ex-GST** unless labelled USD. GST factor is 1.1.

## Not yet built (future phases)

- Auth + multi-user hosting (currently single-user, local).
- Email vendor/sales copies via the Microsoft Graph app registration.
- Live product-master sync from NetSuite (currently imported from the template).
- Editing existing line items in place (currently add/delete).
```
