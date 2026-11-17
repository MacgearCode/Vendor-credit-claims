# Brand Profitability: how the Excel derives each figure

Reverse-engineered 2026-11-17 from `CYA Sales_AU_FY27.xlsb` and
`MGAU ALL Brands Profitability FY27.xlsx` (both read from copies; the originals were not touched).
Everything below was checked against the workbooks' own cached values. Nothing has been built yet.

## 1. The chain

```
NetSuite sales lines (AU)  ──►  CYA "SalesReport Data" (318,474 rows, Apr–Oct 2026)
  + hand-pasted GL lines        │  per-line formulas: rebate %, MDF %, PM, GP target
    (freight, vendor rebates)   ▼
                            CYA pivot  ──►  Brand Profitability "YTD" sheet (pivot over the CYA on SharePoint)
                                            ──►  "NEW_Brand Report" pivot: PM → Brand, one month or quarter
```

The other sheets in the Brand Profitability file (`Brand Report`, `MTH`, `Cust Report`) are hidden
and point at the FY26 CYA. Only `NEW_Brand Report` + `YTD` are live.

## 2. Inputs

### 2a. Sales lines (columns A–V of `SalesReport Data`)

One row per NetSuite invoice / credit-memo line. **Reconciled to the warehouse to the cent, every month
Apr–Oct 2026** (gross amount, quantity and COGS, document by document):

| CYA column | Warehouse equivalent |
|---|---|
| OrderId | `fact_sales_line.document_number` |
| Invoice Date (1st of month) | posting month (same as transaction month for FY27) |
| NS Account ID (`00267`) / Billing Company Name | the line customer's **top-level parent** (`dim_ns_customer.parent_id`). Code ↔ parent is 1:1. Name = parent name on 100% of documents |
| OrderNotes | the line's own customer, i.e. the store (`customer_id`) |
| Product Category / Prod Cat 2 | item brand (`dim_item.brand`; the line's own class is blank on ~150 lines) |
| Item Category | `dim_item.category` |
| Quantity, Order Line Amount (= gross), COGS | `quantity`, `amount`, `est_cost` |
| Account Group Name | customer category (Mass Retail, Amazon, …) |

**Which lines:** posting accounts **54 Sales, 108 Freight From Customers, 957 Sales short supply &
damage, and the 3PL income accounts 866 / 867 / 869 / 902** (Spacewalker 3PL invoices).
**AU→NZ intercompany is included** ("MacGear NZ - Purchases AUD", $659,790 in October).

| Month | CYA gross | Warehouse, same rule |
|---|---|---|
| Apr | 8,044,018.18 | 8,044,018.18 |
| May | 7,466,978.13 | 7,466,978.13 |
| Jun | 6,242,750.96 | 6,242,750.96 |
| Jul | 12,040,671.72 | 12,040,671.72 |
| Aug | 8,315,726.38 | 8,315,726.38 |
| Sep | 13,854,672.22 | 13,854,672.22 |
| Oct | 27,796,741.12 | 27,796,741.12 |

### 2b. Hand-pasted rows at the top (rows 2–849)

Account Group = "Freight & Rebate". Each row is one GL line: date, document (JE / vendor bill / vendor
credit), vendor, memo, brand, and an amount in **Freight $** (519 rows, $387,064) or **VR $** (329 rows,
$2,795,409). Sign: **positive = cost to Macgear, negative = income** (a vendor credit).
Examples: Think Global / Mainfreight destination and importation charges (freight); "Mova Std Accrual",
"Withings April 2026 Rebate", "Eufy Rebate 1% cash discount", "HN AU Sales Reps by Brand Commission 5%",
"EUFY Directed Returns" (VR).

The sheets `Withings`, `Adonit`, `Mova HN` and `Anker` are calculators for vendor **claims**
(Withings: per-unit USD rebate by tier from `Withings Rebate Info`; Adonit: per-unit; Mova HN: 2% of
HN sales; Anker: a work-in-progress copy of Withings). They feed nothing in the CYA directly; the
claim, once posted in NetSuite, comes back in as a VR row.

### 2c. Rate tables (`Rebates`, `MDF`, `PM` sheets)

All rates are by **month** (one row per month of FY27).

| Table | Key | Example |
|---|---|---|
| Base customer rebate (Rebates A–D) | head-office customer × month | JB 19%, HN 17%, OW 13.5%, Bunnings 8% → **3% from Sep**, Baby Bunting 0% → **8.84% from Oct** |
| Brand exception (Rebates H–M, Category blank) | customer × brand × month | JB × Mova −10%, HN × Mova +5%, JB × EUFY −8% from Sep, OW × Bonelk 2.5% then 5.5% from Jul |
| Brand + item-category exception (Rebates K filled) | customer × brand × category × month | HN × EUFY × Appliances +5% from Sep (the only one) |
| Store extra (Rebates Q–T) | head office × store × month | 10 JB airport stores +5% |
| MDF (MDF sheet) | brand × month, optional customer override | Nanoleaf 6%, Ulike 10%, Adonit 5%; Amazon × Mova 0%, JB × EUFY 0% |
| GP target (Rebates W–AD) | brand (no month) | Mova 13%, Bonelk 17%, Withings 14% |
| PM (PM sheet) | brand | Mova → Jeffrey Zhu, Withings → Rachel Harris |

Exceptions are **added** to the base (they're signed adjustments, so −10% takes JB's 19% to 9% on Mova).
Duplicate rows add up too (Excel's SUMIFS): Officeworks has two blocks for Bonelk, Twelve South, Satechi,
Paperlike, Adonit and Anker, so the effective rate is the sum. Matching is case-insensitive.

## 3. Per-line formulas (columns W–AS)

Checked on all 317,626 sales lines: every rate and dollar figure below equals the workbook's cached value.

| Figure | Formula |
|---|---|
| Base Rebate % | base table for (head office, month); 0 if none |
| Extra Cust Rebate % | store-extra table for (head office, store, month) |
| Extra Product Rebate % | brand exception for (head office, brand, month), category blank |
| Extra Category Rebate % | brand+category exception for (head office, brand, month, item category); 0 if item has no category |
| Total Rebate % | sum of the four |
| **Total Rebate $** | Total Rebate % × **gross** (credit memos give a negative rebate) |
| **Net Sales** | gross − Total Rebate $ |
| **MDF %** | the customer-specific MDF row for (head office, brand, month) if one exists, else the brand's default row |
| **MDF $** | MDF % × **gross** |
| **Gross Profit $** | Net Sales − COGS − Freight $ − VR $ |
| **Net Profit $** | Gross Profit $ − MDF $ |
| Net Profit % on Net / on Gross | Net Profit ÷ Net Sales / ÷ gross |
| PM | brand → PM sheet |
| GP Target | brand → Rebates!AD ("GP % on top line, landed after accruals") |
| FYQ | Apr–Jun = Q1 … Jan–Mar = Q4 (company financial year) |

Lines with no brand (3PL fees, customer freight, TIC fee) still get the customer's base rebate.

## 4. The report (`NEW_Brand Report`)

Pivot of `YTD` (which is a pivot of the CYA): rows **PM → Brand** (brand names merged case-insensitively,
so the "Mova" VR rows join "MOVA" sales), filtered to one month or quarter by slicers. Columns:
Quantity, Gross Sales, Rebate $, Net Sales, COGS, Freight $, Vendor Rebate $, Gross Profit, MDF $,
Net Profit, NP % on net, NP % on gross, GP Target (max), and a variance column (NP % on gross − target).

The saved file shows **October 2026**, and the CYA re-aggregated reproduces it exactly:

| | October 2026 |
|---|---|
| Quantity | 169,871.14 |
| Gross sales | 27,796,741.12 |
| Rebate $ | 3,701,597.35 |
| Net sales | 24,095,143.77 |
| COGS | 19,653,209.67 |
| Freight $ | 101,761.74 |
| Vendor rebate $ | 453,241.55 |
| Gross profit | 3,886,930.81 |
| MDF $ | 62,834.88 |
| Net profit | 3,824,095.92 (13.76% of gross) |

e.g. EUFY: gross 12,671,795.92, rebate 1,245,194.15, VR −174,342.13, NP 1,726,728.99 (13.6% of gross
against a 14% target); Mova: gross 11,194,461.04, VR +781,824.24, NP 1,539,891.34.

## Status (2026-11-17): built locally

Report `/reports/brand-profit` and settings `/brand-profit/settings` are built; nothing is committed
or deployed. Code: `app/brand_profit/`. Seed: `seed_fy27_au.json`. Check: `reconcile.py` against
`cya_fy27_au_expected.json` (the CYA re-aggregated, with intercompany and Airport West taken out).

**Reconciliation, Apr–Oct 2026 AU, live warehouse:** gross, quantity and COGS equal the CYA every
month. Rebate, MDF and NP equal it on every month × brand cell, except two things:
- **Line-class gap:** 158 credit-memo lines (−$134,500.96) on generic items ("Sales", "Sales short
  supply & damage", "Lost in Transit", freight credits) take their brand from the NetSuite line's
  Class. `DW_fact_sales` exports that as blank, so they fall under "- No Brand -" (and miss the brand
  exception and MDF). With those lines treated as unbranded on the CYA side too, every cell equals the
  CYA to the cent. **Fix: the sales export's brandkey should be the line Class.**
- **One Sep 2026 credit memo** ($445.40, 2 units) that the CYA had unbranded and NetSuite now classes
  EUFY.

October on the site: gross 27,136,950.98, rebates 3,700,066.99, NP 3,703,372.02. The CYA, less
intercompany and Airport West, has gross 27,136,950.98, rebates 3,700,297.40, NP 3,703,142.14.

Decisions 2026-11-17 (second round): PM by brand follows the CYA PM table (it is the seed for managing
portfolio assignments here rather than in NetSuite). Base rebates follow the CYA, not NetSuite's customer
field. NZ exists (an NZ CYA, not yet supplied). The GP target is measured against NP % on gross. The
user is building the GL saved search.

**Departure from the Excel (user, 2026-11-17): portfolio targets are sales-weighted.** The pivot's
"Max of GP Target" gave a PM their highest brand target (David Vanderkley 32%, from Paperlike). The
site uses Σ(brand target × brand gross) ÷ Σ gross, over brands with a target. That's the NP % on gross
the portfolio makes if every brand hits its own target, plus "vs target $" = NP made − Σ(target × gross).
The CYA's 0% targets for end-of-life brands were removed (user, 2026-11-17), so those brands now have
no target and are left out of the comparison. The −20% clearance targets (AQUATONE, AZTRON) stay.
October 2026: DV 14.7% target vs 14.7% actual (−$7,282); Jeffrey Zhu 13.0% vs 13.2% (+$21,895); Rachel
Harris 17.1% vs −1.8% (−$176,018, Withings); company 14.2% vs 13.6% (−$161,137).
PMs (like every setting) are per region: the same brand can have one PM in AU and another in NZ.

## NZ (added 2026-11-17, from `CYA Sales NZ_FY27.xlsb` + `MGNZ Brands Profitability FY27.xlsx`)

Same method as AU. Differences in the NZ CYA:
- **SKU rebate** (Rebates W–AA): customer × NetSuite item code × month, added to the rebate. It's the
  site's rule type `sku` (24 rows: Noel Leeming −10% on Withings watches and chargers).
- MDF is brand × month only (no customer overrides). No store extras. Vendors / GP targets at Rebates
  AC–AJ (looked up on Prod Cat 2). The hidden "Vendor" sheet is unused ("Will just use P&L").
- Includes account **836 Courier Claims (NZ)**, now in the site's account list (AU has none in FY27).
- Claim calculators (Withings, SpaceTalk, Adonit, Honor HN / pass) feed nothing directly, like AU's.

Checked: every rate on all 68,797 NZ lines reproduces exactly. Seed `seed_fy27_nz.json` (177 rules
incl. 24 SKU, 74 PMs, 651 GL lines; 0% targets left out; EB Games → "EB Games (NZ)" 9876 and Fly Buys
(LNZ) → "Fly Buys" 1894 mapped by hand, since neither has FY27 sales). Loaded into the local Postgres.

**NZ reconciliation (`reconcile.py NZ`):** gross, qty and COGS equal the CYA every month, apart from
CNNZ007460 (−$84.78, May), which was modified in NetSuite on 2 Jul, after the CYA was pulled. April and
June tie on every brand to the cent. The other differences are the same line-Class gap as AU: 43
documents, each netting to zero, on generic items "Sales NZ" (including a +$39,552.56 Garmin line in
September) and "Courier Claims (NZ)".

## 5. Decisions so far (user, 2026-11-17)

- **Intercompany AU→NZ is excluded** (warehouse rule); the page states the amount left out so it can be
  tied back to the Excel.
- **Freight / VR:** seed FY27 Apr–Oct with the workbook's 848 pasted rows, AND an upload page.
  Nothing goes in that isn't in NetSuite, so the upload takes the **same CSV as the saved search**
  (run by hand). Per month, NetSuite-extract rows win: once the S3 extract covers a month, uploaded /
  seeded rows for that month are superseded (kept, shown as replaced, not counted).
- **Airport West dropped** from the store extras (Apr–Oct then differs from the Excel by $3,452.94 of
  rebate; every other figure still ties).
- **Two area keys:** view report, edit settings.
- **The rest of the site's GP should come from here**, showing both net % on gross and net % on net
  (home page, Sales Explorer, Item Explorer currently use NetSuite estimated GP). Follow-up after the
  report reconciles.

## 6. Proposed site design (for confirmation)

- **Calculation** in Python over `reporting.fact_sales_line` (the line rule in 2a), reproducing §3
  per line, then summed by PM → brand. Freight and VR from a new NetSuite GL extract (§6).
- **Settings** in Postgres (own schema), effective-dated rows `valid_from` month / `valid_to` month,
  and an append-only change log (who, when, old → new). Tables: base rebate (customer), brand exception
  (customer × brand [× item category]), store extra (store), MDF (brand [× customer]), GP target
  (brand), brand → PM. Customers keyed by NetSuite internal id, so they line up with the future
  portfolio page (rep ↔ customer ↔ brand, PM ↔ brand); the brand → PM table is the seed for that.
- Seeded from this workbook's FY27 tables (effective rates, duplicates summed).
- **Report page**: month / FY-quarter / FY-to-date selector, PM → brand tree, the same columns, Excel export.
- Access: a view area and a separate edit area for the settings, ticked per user.

## 7. NetSuite extract needed for freight + vendor rebates

A saved search of **GL transaction lines** (Transaction search, "Main Line = any", one row per GL line)
on the freight / landed-cost and vendor-rebate accounts, AU (and NZ) subsidiary:

| Column | Why |
|---|---|
| Line Unique Key | primary key (same as DW_fact_sales) |
| Internal ID, Line ID | transaction + line |
| Type | Journal, Vendor Bill, Bill Credit, … |
| Document Number | shown on the report drill-down |
| Date | |
| Posting Period (internal id) | which month the cost belongs to (the CYA uses the month) |
| Subsidiary (internal id) | AU / NZ |
| Account (internal id + number) | tells freight from vendor rebate |
| Class (brand internal id) | which brand bears it |
| Name / Entity (internal id + name) | vendor |
| Memo (line) and Memo (main) | the description column |
| Amount (debit − credit, subsidiary currency) | positive = cost |
| Department, Location | optional |
| Last Modified | same load rule as sales |

Volume is small (~850 lines in 7 months), so export the **whole current FY every run** and let the
loader replace it: edits and deletions then take care of themselves.
