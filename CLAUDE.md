# Reporting Site — Macgear Reporting (reports + internal tools, one login)

**Vision (user, 2026-10-15):** this site becomes Macgear's central reporting hub
and **replaces Power BI** — revenue / sales reporting, sell-through for staff and
vendors, and **KPI tracking** to come. Build new reporting here rather than in a
.pbix.

**Moved here from `Vendor Credit Claims/` on 2026-10-15** (user's decision): the
apps.macgeargroup.com site is becoming **reporting.macgeargroup.com** — one site
for staff AND vendors, reports first, the two original tools under a Tools menu.
This supersedes the 2026-09-30 design of a *separate* reporting app with identity
handed off from apps (`Sell Through/CLAUDE.md`, "Reporting site"); the rest of that
design (vendor isolation, 2FA, server-side filtering, audit) still applies.

The folder was copied with its git history; `Vendor Credit Claims/` is left in
place until the user confirms it can go. The GitHub repo
(`MacgearCode/Vendor-credit-claims`), docker service `claims` and droplet clone
path are unchanged. **LIVE at https://reporting.macgeargroup.com since 2026-10-15**
(commit d5acda1); apps.macgeargroup.com and promos.macgeargroup.com 301 to it with the
path kept. Runbook: `deploy/DEPLOY-BASIC.md` "Going live as reporting…". Go-live notes:
the `reporting` DNS A record was added late, so Caddy's first certificate attempts hit
NXDOMAIN and some resolvers cached "not found" for the SOA's 30 min; and a mistyped
warehouse host in the droplet `.env` broke the warm-up — compare the host, not the whole URL.

| Path | What | Audience |
|---|---|---|
| `/` | Home — internal sales (sell-in $ tick only), sell-through snapshot, report + tool cards | everyone |
| `/reports/sales` | Sales Explorer — Macgear revenue, any FY period vs LY, build-your-own matrix, Excel | staff with Sell-in $ |
| `/reports/items` | Item Explorer — search + leaderboards; `/reports/items/i/<code>` = one item | staff, vendors |
| `/reports/table` | Sell-Through Table — the weekly workbook live; `/reports/table/export.xlsx` | staff, vendors |
| `/reports/sell-through` | Sell-Through Dashboard (from `Sell Through/prototype/`) | staff, vendors |
| `/reports/weekly` | Weekly Sell-Through — the consolidated weekly report, full screen | staff only |
| `/reports/field` | Field Activity — Mova reps from Repsly: dashboard, display compliance, feed, schedule, visits, reps, photos | staff only |
| `/updates` | What's new — the site's update log (`app/updates.md`) | everyone, filtered by area |
| `/tools` | Tools landing (tools are also listed in the side menu) | staff |
| `/promos` | Vendor Promos — promotional credit claims | sales, finance |
| `/vendor-bills` | PDF Bill Upload — Amazon Advertising → NetSuite CSV | accounts |
| `/admin/users` | User management | admins |

**Adding a report or tool** takes three edits: a tile in `REPORTS` or `TOOLS`
(`app/main.py`), its key in `perms.REPORT_KEYS` / `TOOL_KEYS` + `AREA_LABELS`
(and `VENDOR_AREAS` if vendors may have it), and its URL prefix in `_TOOL_PATHS`.
Miss the last one and staff can reach it by typing the URL (vendors can't — see below).

**What's new / update log (2026-10-21).** `/updates` lists `app/updates.md`, newest first
(`app/updates.py`). **With every commit people would notice, add an entry there in the same
commit**: `## YYYY-MM-DD | Title | area keys` + `- ` lines in plain words for site users. Area
keys hide an entry from anyone who can't open that area (same rule as the side menu, so `sales`
needs Sell-in $; `admin` = admins); **no keys = everyone, vendors included** — keep internal
detail out of those. Vendors may open `/updates` (`_VENDOR_OPEN_PATHS`). The side menu's
"What's new" (and the ☰ button) carry a dot until this browser has opened the page since the
newest entry (`localStorage.updates_seen`). Tests: `tests/test_updates.py`.

## Access control

Roles: `admin` (everything + user management), `user` (staff — only the
reports/tools ticked), `vendor` (only the reports ticked, only the brands ticked).
Resolution lives in `app/perms.py` and is **default deny**: a report or tool added
later appears for nobody until ticked.

Two independent controls per user:
- **Areas** (`allowed_tools` — the column name predates reports; it holds report
  AND tool keys). NULL = role default. Vendors can only ever hold `VENDOR_AREAS`;
  a tool key stored on a vendor grants nothing.
- **Data scope** — `allowed_brands` (NetSuite class names, = `dim_item.brand`) and
  `allowed_regions` (AU/NZ). Staff: NULL brands = all brands. **Vendor: NULL or
  empty = NO brands** (fail closed). Regions: NULL = both, for everyone.
  `company` is display-only.

Enforcement is in the `require_login` middleware, matched on URL prefix, not on
handlers. **Vendors are allow-listed**: `/` plus the areas ticked; any path no
area claims (`/tools`, `/api/...`, a tool missing from `_TOOL_PATHS`) is 403 for
them. Reports apply the data scope **in SQL** (`sell_through._FACT_SQL` takes the
brand/region lists), so other brands' rows never leave the warehouse; the report
cache key includes the scope. Hiding a tile or menu item is presentation only.
`tests/test_access.py` drives the real middleware for these rules.

**Capabilities** widen what a report shows, not which pages open — ticked per
user under "Extra data in reports", stored in the same `allowed_tools` list:
`sell_in` (sell-in UNITS + Macgear warehouse stock quantities), `sell_in_value`
(Macgear's sales $; needs `sell_in`) and `margin` (est. GP; needs
`sell_in_value`). Default-deny for everyone but admins. **User decision
2026-10-15: vendors may see sell-in quantity; Macgear's sales $ amounts stay
hidden from them** — a vendor can hold `sell_in` only (`perms.VENDOR_CAPS`).
Enforced in SQL: without `sell_in_value` the item query selects NULL for the $
columns (`%(money)s`), so the amounts never leave the database; a test checks
nothing $-shaped reaches the page. "Retail value" (units × public RRP) is not
Macgear's sales $ and shows for everyone.

`allowed_tools` stores NULL when the selection equals the role default, so
defaults keep flowing as areas are added. Don't "helpfully" expand it.

Passwords: PBKDF2-SHA256 via `app/security.py` (copied from 3PL Portal so both
apps behave the same). An admin never sets another person's password. Only the
token's SHA-256 digest is stored, with its expiry and purpose.

**Invites and resets (2026-10-15, same design as 3PL Portal).** Creating a user emails
nothing; the user form's **Sign-in** box then sends an **invite** (no password yet, 7-day
link) or a **password reset** (45 min), with an optional personal note, Reply-To = the
admin — or "Just give me the link". The link is always shown too ("the webhook accepted
it" ≠ "it arrived"). **Forgot password?** on `/login` → `/forgot`: identical response
whether or not the address exists, and throttled (3 per address / 10 per IP per 15 min,
in-process). Disabled accounts get no link and their old links die. Status per user:
active / invited · link expires … / not invited yet / disabled. Email goes through the
**3PL Portal's n8n workflow** (`app/notify.py`, payload `app: "reporting"`) →
Graph app-only → noreply@ — env `N8N_RESET_WEBHOOK_URL`, `N8N_WEBHOOK_TOKEN` (same token
as 3PL). Unset = nothing sent, the email is logged, admins copy the link. Everything
about that mailbox (Junk on first sends, the client-secret expiry, IT-only Exchange
steps) is in `3PL Portal/docs/email_delivery.md`. Tests: `tests/test_account_email.py`.

The app refuses to demote, disable or delete the last active admin.

### Before the first real vendor account (from the 2026-09-30 design — NOT done)

1. Retire the legacy `APP_PASSWORD` admin login, and make "no users + no
   APP_PASSWORD" refuse to start instead of disabling auth.
2. 2FA (TOTP) at login, recovery codes, admin can reset a user's 2FA (`segno` is
   already in requirements for the QR).
3. Audit log of vendor access (page views / exports).
4. Postgres-side isolation: a `reporting_vendor` role with SELECT only on a
   `vendor` schema of views + RLS on brand; today the app uses `reporting_read`
   and filters in its SQL.
5. Decide whether pre-Macgear Eufy history (`customer_department = 'EUFY
   Historical'`) is shown to vendors — the report currently includes it, as the
   prototype did.

## Reports

Data comes from the warehouse (`macgear_reporting` on the DO cluster) through
`REPORTING_DB_URL` = the read-only `reporting_read` role (`app/warehouse.py`;
connection per query, results cached 10 min per scope). Locally `.env` holds it
(gitignored; copied from `Sell Through/.env` `REPORTING_READ_DB_URL`) and
`run.ps1` loads it. Without it, reports show "warehouse could not be reached"
and the tools still work. The droplet will need the variable (already wired in
`deploy/docker-compose.basic.yml`) and to be a trusted source on the cluster.

**Sell-Through** (`app/sell_through.py` + `templates/report_sell_through.html`):
the prototype's builder and page, with the JSON built per request and scoped.
Checked 2026-10-15 against `build_prototype.build(26, 10)` on live data: units,
SOH, on order, SKU counts, retailer lists and product picks identical; retail
value differs only where a sum lands on exactly $x.50 (exact Decimal + round
half-up here vs pandas float rounding). Vendor scope ANKER+EUFY WE 2026-10-11 =
AU 6,312 / NZ 878 units = direct SQL. The page is self-styled (not `base.html`)
because its class names collide with `style.css`. Light + dark (2026-10-15): same
rule as the other report pages — dark unless the viewer chose light in the side
menu; colours are CSS variables (`[data-theme="dark"]` block), the SVG charts
read them at draw time and redraw when the theme flips (MutationObserver).

**Home page** (`app/sales.py`, `templates/home.html`, built 2026-10-15). For users
with `sell_in_value` (never vendors — the route doesn't even build it): AU and NZ
side by side, never summed — month-to-date revenue vs the same dates last year,
"on pace for" (MTD ÷ weekdays so far × weekdays in month), today, FY quarter- and
year-to-date vs LY, est. GP (margin tick), a cumulative-by-day chart (this month,
last month, same month LY, target line when loaded), and top customers / brands /
reps / items this month. Everyone with a sell-through report also gets a
sell-through snapshot (last week's units, WoW, top 5) in their scope.
- Revenue = `fact_sales_line.net_sales` by `date_key`. **Periods use Macgear's FY,
  April–March (FY27 = Apr 2026–Mar 2027), Q1 = Apr–Jun.** "Today" is Sydney's
  date (`sales_today()`).
- **Two calendars, by subject (user, 2026-10-15; root CLAUDE.md "Which calendar
  where"):** internal revenue → financial year (Apr–Mar); sell-through, and sell-in
  shown next to sell-through (always for vendors) → calendar year. Today the
  sell-through pages use rolling weeks (4/13/26/52), so no conflict yet — any
  YTD / quarter added to Item Explorer, the table or the dashboard must be
  CALENDAR; anything added to the revenue side must be FY. Label every period.
- Checked 2026-10-15 vs independent SQL: AU MTD $8,485,109 (LY $1,944,926), FY27
  YTD $63,432,141 (LY $27,713,755), GP MTD $2,583,861; NZ MTD $1,749,081, YTD
  $14,643,586 — equal.
- **Targets:** `sales.targets()` reads `reporting.sales_target` (country_code,
  month_start, amount — finer rows summed) if the table exists; until then tiles
  say "No target loaded". The Targets app (`Targets/targets.db`) holds FY27
  targets by country × brand × customer × month — the natural source to export.
- Daily series ~6 s to build; cached + warmed like every report.
- **Revenue vs a NetSuite figure (2026-10-15).** `net_sales` already = invoices +
  credit memos on the Sales account (all of them inventory items — "inventory
  credits"); freight / rebate / misc credits post to other accounts and aren't
  revenue. AU Oct-to-date at the 3:32pm (Brisbane) data: invoices $8,905,740 −
  credits $304,207 = **$8,601,533**. The user's NetSuite figure $9,103,960 ≈ that
  + **AU→NZ intercompany $506,110** ($9,107,643) — so their search includes
  intercompany, which the warehouse rule (2026-10-14) excludes. Asked the user
  which the site should show; don't change `net_sales` without that answer.
- **How fresh (measured 2026-10-16):** NetSuite → Zone export → S3 lands **every
  30 min, at :17 and :47** (the runbook's "15 min" isn't what Zone is set to) →
  Windmill load every 15 min (:05/:20/:35/:50, all OK, <8 s) picks each file up ~3 min
  later → site. The site no longer waits on a TTL: `sales.data_version()` (newest Last
  Modified + recent line count, ~0.1 s) is a **warehouse watch** checked every minute;
  when it changes every sales/stock cache entry (`SALES_CACHE_PREFIXES`) is expired
  and the used ones rebuilt at once ⇒ invoice → home page ≈ Zone wait (≤30 min) + ~3
  + ~1–2 min. Setting the Zone sales export to 15 min is the remaining big lever
  (user's change in Zone).
- **The recurring "NetSuite says more" question = intercompany.** AU Oct-to-date
  2026-10-16: site $8,774,786 + AU→NZ intercompany $506,110 = $9,280,896 = the user's
  NetSuite figure. The home tiles now show "Excl. $… intercompany to NZ"
  (`sales.intercompany()`); revenue still excludes it (warehouse rule) until the user
  decides otherwise. The "latest
  change" chip is the newest NetSuite Last Modified in the data, read as
  **Brisbane** time (scheduled exports) and shown in Sydney time.

**Sales Explorer** (`app/sales_explorer.py`, `templates/sales_explorer.html`, built
2026-10-15 to carry the core of the Power BI sales report). Revenue = home-page
definition; **financial-year periods** (Apr–Mar): quick (today / yesterday / this week /
MTD / QTD / YTD), full year, Q1–Q4, months; FY selector FY23→; filters account manager,
brand, retailer (customer group); every figure vs the **same dates LY**
(to-date vs to-date while a period runs). Matrix: up to 3 row levels from Retailer,
Account (store), Brand, Account manager, Item, Month — expandable tree, sortable,
share bars, GP only with `margin`; Excel = the matrix flattened. Needs the `sales`
area AND `sell_in_value` (route-checked; not listed in menus without it); never vendors.
- **Account manager = the CURRENT account rep on the customer record**
  (`dim_ns_customer.account_rep_id`), applied to all history — the manager's whole
  portfolio (user, 2026-10-15). NOT the rep on each invoice (`fact_sales_line.rep_id`,
  which matched ~94%). The summary therefore has no rep column; the manager is looked
  up per customer. The home page "Account managers" list uses the same rule.
  Checked: FY27 YTD per manager = SQL grouped by `dim_ns_customer.account_rep` exactly.
- **Speed:** per-click warehouse queries took ~6 s, so each region's sales live in memory
  as a sorted columnar summary — monthly (month × customer × brand, all history,
  AU ~183k rows) + daily (this & last month and the same span LY). A range is answered
  from whole months plus days at the partial edges (`_parts`, bisect). Slices 0.02–0.06 s;
  build ~15–19 s, warmed at startup, monthly TTL 30 min, daily 5 min. **Item** isn't in
  the summary (~1M rows): Item matrices query the warehouse grouped by only the needed
  ids (0.2–3.3 s, cached).
- ⚠️ **Never filter `reporting.fact_sales_line` on `brand_id` in SQL** — the planner
  takes a path that ran past 80 s (0.13 s without it). Brand is grouped on and filtered
  in Python; rep_id / customer_id filters are fine in SQL. All warehouse connections now
  carry `statement_timeout` 60 s (`warehouse.STATEMENT_TIMEOUT_MS`).
- Growth % only off a positive LY (a net-negative LY day from credits made "+166%");
  very large growth shows as a multiple ("404×").
- Checked 2026-10-15: NZ FY27 YTD, FY26 and FY27 Q2 — every brand = independent SQL
  to $1; item-path totals = summary totals (rep / retailer / brand filters).
- Targets: columns/cards say "not loaded" until `reporting.sales_target` exists (see
  home page note).

**Field Activity** (`app/field.py`, `templates/field_*.html` + `_field_head.html` /
`_field_macros.html`, `static/field.css`; built 2026-10-17). Mova field reps from
**Repsly**, replacing the Repsly Power BI report and replicating the Field Reps
prototype's manager pages over real data. Data = `reporting.field_*` views, loaded by
`Sell Through/repsly_intake.py` (Windmill every 30 min once published — see
`Sell Through/CLAUDE.md`, "Repsly layer"). Area key `field`, **staff only** (not in
`VENDOR_AREAS`; rep GPS, notes and photos are internal — Mova vendor access is a later
decision about what they may see). Pages under `/reports/field`: dashboard, `/compliance`,
`/feed?day=` (Leaflet map from cdnjs, OSM tiles), `/schedule?week=`, `/visits` +
`/visits/{id}`, `/activity` (reps), `/photos`. Everything is cached whole (≈5k visits,
22k display answers) and expired by a warehouse watch on `field.data_version`.
- **Display compliance is what reps are measured on — rules in `field.py`'s docstring,
  tested in `tests/test_field.py`** (user, 2026-10-17): one count per store × product per
  period using the LATEST answer; blank = No; rep credited = rep on that answer;
  **Harvey Norman is the measure**: only HN grades **A–D** (column E "Core Range" of HN's
  weekly Mova file → `field.hn_product_grade`) at stores whose tier ranges the grade
  (`field.hn_store_tier`, from HN's "Store Site Group Tiering" PDF — the PDF says
  Floorcare but **the same tier applies to Outdoor Living and Personal Care too**, user
  confirmed 2026-10-18; HN stores not on the list don't count). Other grades E/F/O/S/Z
  never count. **JB / TGG** = a separate "merchandising" measure (reps can't influence
  ranging there): everything asked except a No whose follow-up says "not ranged". "Still
  on display" questions (clearance) are never compliance. Target line 80% (placeholder).
- Checked 2026-10-17 on the full history: HN floorcare last 90 days 75.5% every-answer →
  91.2% ranged-only; rep answers agree with HN's own Display SOH 83% (92% for V70 once
  the "V70 Ultra Complete" questions were mapped to the V70 Ultra SKU — reps answer them
  about the V70 Ultra).
- **Repsly times are the rep's LOCAL wall-clock time** (the API labels them +0000), so
  they're shown as they are (`hm` filter), never converted.
- A new or reworded Repsly question shows on the dashboard / compliance page as "not in
  the question map" until a row is added to `Sell Through/reference/field/question_map.csv`
  and `repsly_intake.py --reference` is run.
- Local check against the LOCAL warehouse copy: launch config `reporting-site-localdb`
  (port 8031, throwaway SQLite in %TEMP% so auth is off; the real `data/app.db` untouched).

**Weekly Sell-Through** (`app/st_weekly.py`, `templates/st_weekly.html`, `static/stw.js` +
`stw.css`; built 2026-10-20). The LAYOUT of a staff member's dashboard over the "Consolidated
Sell Through ANZ" workbook (their build guide's file-drop / IndexedDB / live-NetSuite parts were
deliberately dropped — data comes from the warehouse). Full width, sticky filter bar (market AU / NZ —
AU first, no ANZ, user 2026-10-20; brand, customer, period 13/26/52/All, sell-in reporting retailers vs all channels), headings
pinned under it, tabs Overview / Brands / Customers / Stores / Products (+ side panel) / Stock watch / How it
works, a Full screen button (F).
- One dataset per scope from `/reports/weekly/data.json` (104 weeks, ~440 KB gzipped, ~18 s to
  build, cached + warmed, expired by the sales watch); the browser does every filter and total.
  Area key `st_weekly`, **staff only** for now (not in `VENDOR_AREAS`). Sell-in, Macgear stock and
  ETAs are the `sell_in` capability — not queried without it.
- **Stores** (2026-10-20): only some retailers send stores (AU: HN, Bunnings, Bing Lee, Retravision;
  NZ: JB HI-FI, PB Tech, HN, Bunnings — `st_weekly.store_retailers`). Loaded on demand from
  `/reports/weekly/stores.json?region=&retailer=` (one retailer's stores × products; HN AU ≈ 194 stores,
  ~24k lines, ~180 KB gz, ~20 s cold — warmed last at startup) or `&code=` (one product's stores, for
  the product panel's "By store"). Picking a retailer on the Stores tab selects it as the customer,
  so the rest of the page stays retailer-safe. Per store: units, vs 6-wk / LY, SOH, cover, products
  selling, "out of stock" = sold in the last 6 weeks with SOH ≤ 0 now; a store opens its product list.
  Checked: HN AU store rows w/e 18 Oct = 2,247 = the retailer total.
- **Selecting a customer limits every number on every tab and the product panel to that customer**
  (the guide's "retailer-safe view"); Macgear open orders (all customers) are hidden then.
- Rules: 6-wk avg incl. this week; cover = retail SOH ÷ 6-wk avg (SOH ≤ 0 = 0 cover); vs LY = 364
  days earlier; **YTD is CALENDAR** (the guide said FY from 1 April — sell-through is calendar here);
  sell-in → retailer by `map_retailer`, else by normalised name (case, punctuation, "(NZ)",
  Limited/Ltd, Computers ignored). Macgear totals cover the SKUs on the page.
- **PO ETAs are a placeholder**: `reporting.fact_open_po` over `netsuite_raw.dw_open_po_line`
  (`Sell Through/db/11_open_po.sql`, written 2026-10-20, NOT applied to the cluster). Until it
  exists the page says "PO ETAs not loaded yet"; once loaded, Next ETA / the stock-watch pill /
  the panel's "Incoming Macgear stock" fill in with no code change.
- Checked 2026-10-20 vs independent SQL (w/e 18 Oct): NZ 3,214 units, 13 wks 43,011, SOH 74,466,
  sell-in (reporting retailers) 7,855; AU 20,323 / 252,466 / 292,961 / 23,803 — equal.
- Local preview without sign-in: launch config `reporting-site-preview` (port 8032, live
  warehouse via `.env`, throwaway SQLite in %TEMP%).

**Receipt strip** (`app/receipts.py`, `templates/_st_status.html`, 2026-10-15) —
"who have we received" for sell-through, a one-line collapsible bar at the top
of home, Item Explorer (landing + item), the table and the dashboard: "w/e 11 Oct
· 15 / 15 in · waiting on …", expanding to every feed with ✓ in (Sydney load
time) / ◐ part in / ○ waiting (latest week received).
- Expected week = the most recent Sunday BEFORE today (Sydney): on Monday it
  shows last night's week with nothing in yet — that's the point.
- Feed = retailer × country; expected feeds = those that sent in the previous 4
  weeks. "Part in" = some of a feed's usual departments missing (Harvey Norman's
  IT and Small Appliances are separate files). 'EUFY Historical' isn't a feed.
- Vendors see only retailers that carried their brands in the last 8 weeks.
- Cached 5 min (files land through the day). Testing: `REPORT_TODAY=2026-10-19`
  pretends it's another day (e.g. a Monday with nothing in).
- To add it to a new sell-through page: `{% include "_st_status.html" %}`; it
  renders nothing for users without a sell-through report.

**Item Explorer** (`app/items.py`, `templates/items.html` + `item.html`). Landing
= a search box that **filters the tiles on the page** (user, 2026-10-15 — no jump
to the item page): every word must match code / name / brand, a "Matches" tab
appears (13-week units order), the other tabs and the summary cards work on the
narrowed set, `?q=` stays in the address for Back, Enter opens the item when one
tile is left. The server search (`/reports/items/search`, word-by-word + exact
UPC, ranked by lifetime units so dummy packs sort last) adds barcode matches and
items with no sell-through in the 13 weeks as plain tiles. The item page's top
search still drops down and jumps straight to an item. Plus tabbed leaderboards (top
sellers, value, risers, fallers, stock-out risk, new, a per-browser watchlist)
from one cached 13-week leaderboard per scope × region. Item page = header (image
slot, codes, RRP, status, rank in scope), KPI cards, a switchable chart (units,
value, retailer stock, sell-in vs sell-through, sell-in $, GP, cumulative),
retailer table, top stores (4 weeks), Macgear stock by location, and rule-based
plain-English insights. Rules worth knowing:
- **Pre-Macgear Eufy history** (`customer_department = 'EUFY Historical'`) counts
  as sell-through, but is its own series (`st_hist`): grey in the chart, and left
  out of the in-vs-out ratio and the trend insight. The ratio covers only weeks
  since the first sell-in week in the window.
- Sell-in = `fact_sales_line.net_sales*` (account 54, intercompany excluded),
  grouped by `dim_date.week_ending_date`; the current part-week shows as a faded
  bar. Retailer = `fact_sales_line.customer_key` (NOT `v_sell_in_vs_sell_through`,
  7 s per item). Macgear stock = `fact_inventory_daily.is_latest`, Saleable only;
  its cover = available ÷ 4-week average SELL-IN.
- Leaderboard totals exclude lines not on the item list (brand NULL), so they sit
  slightly under the dashboard: WE 2026-10-11 AU 20,953 + 254 unmatched = 21,207.
- Out-of-scope items are 404, never 403. Vendor ranks are within their own scope.
- Product images: `data/item_images/<code>.(jpg|png|webp)` (non-alphanumerics → `_`),
  uploaded by admins on the item page. No image → initials.
Checked 2026-10-15 vs independent SQL for E8173C22 AU 26 weeks: units 3,703
(2,860 history), SOH 2,324, on order 46, sell-in 624 / $627,064 / GP $126,486,
available 350 — all equal.

**Sell-Through Table** (`app/st_table.py`, `templates/st_table.html`). Same measures
and SQL shape as `Sell Through/db/export_matrix.py` (change both together).
Grains: product × retailer (the "All AU" tab), product total, product × store (one
retailer). Week columns come from the global loaded-week list via
`warehouse.recent_weeks` (date_key index; the same question through the dimension
joins took ~10 s). Checked 2026-10-15 against `export_matrix.fetch` for WE
2026-10-11, 12 prior weeks: AU 2,025 rows and NZ every cell identical (a NZ row
splits where a retailer sent two customer codes in one week — the workbook does
the same). ~3 s uncached. The Excel download is built server-side
(`st_table.to_xlsx`, workbook styling) from the same filters; the Find box filters
the screen only.

**Speed (2026-10-15).** Connecting costs ~70 ms and a round trip ~6 ms, so time
is in the queries. Two layers:
- *Queries are fact-first:* aggregate `fact_sell_through` on its own indexed keys
  (`region_key`, `date_key` ints — `warehouse.region_key()`, `recent_weeks()`),
  then join names. Table 2.5 → 1.4 s, filter options 3.3 → 0.4 s, leaderboard
  1.3 → 0.3 s. Sell-in filters `fact_sales_line` on `ns_item_id` as well as
  `item_key` (the view maps item_key, so only the NetSuite id hits the raw index;
  2x). Each rewrite was re-checked: same rows / totals as before.
- *Cache* (`warehouse.cached`): fresh → served; stale → served at once and rebuilt
  in a background thread; cold → built once even under concurrent requests. TTL
  30 min for sell-through results, 10 min for sell-in/stock. A refresher thread
  (`start_refresher`, started by the app lifespan when `REPORTING_DB_URL` is set)
  warms the all-brands views at startup (`_warm_reports`) and rebuilds anything
  used in the last 2 h just before it goes stale — vendor scopes included.
  Measured over HTTP after warm-up: landing pages 36–141 ms; a never-seen item
  ~0.7 s then 5 ms; an unusual table filter ~2.8 s the first time, then ~40 ms.
- Front end: GZip on responses; item links prefetch on hover/touch and the top
  search hit is prefetched as results appear; a thin progress bar shows while a
  page loads.
Tests: `tests/test_cache.py`.
- **Auto-reload on Windows can hang** ("Reloading..." then nothing; the old
  process keeps serving old code). Reproduced 2026-10-15 even with a 5-line
  FastAPI app, so it's uvicorn's Ctrl+C-based restart, not this app. If a change
  doesn't show, stop `run.ps1` (Ctrl+C) and start it again. The cache lives in the process — two uvicorn
workers would each warm their own (fine at this size).

**Report look** (`_rpt_base.html`, `static/report.css`, `static/rpt.js`): dark
by default (light if the viewer chose it in the side menu), `r-` prefixed
classes, a no-build SVG chart (`RPT.chart`, bars can `stack`), sparklines, the
search box. `asset_ver` is the newest mtime of any static file.
**Every page is full width (user, 2026-10-21)** — no centred max-width container (`.r-wrap`,
`.r-top-in`, `style.css` `main`, the dashboard's `.wrap`); most use is on a computer. Phones keep
their own padding rules. Narrow measures stay only where they help reading (forms, help text).

## Run it

```powershell
.\run.ps1          # http://127.0.0.1:8030 — loads .env (REPORTING_DB_URL)
```

Local `data/app.db` has no user accounts, so auth is off locally (everything
visible). `APP_DB_PATH` points the app at another SQLite file — tests use it
(`tests/conftest.py`), and it's how to try a vendor login without adding test
accounts to the real `app.db`.

Port 8030 locally; the container still serves 8000 internally and the docker
service is still named `claims` — **do not change the Docker/Caddy/n8n
references.** n8n posts the nightly pricing sync to `http://claims:8000` on the
shared docker network, so it is unaffected by public hostname changes.

Deployment is `Dockerfile` + `deploy/` on the n8n droplet, behind the Caddy that
already serves n8n. Runbook: `deploy/DEPLOY-BASIC.md`.

Auth is in-app (styled `/login`, signed cookie). Accounts live in the `users`
table; `APP_USERNAME` / `APP_PASSWORD` remain as a **legacy shared login that
still signs in as admin** — the migration path and the way back in if the last
admin account is lost. `APP_SECRET` signs the cookie; changing it signs everyone
out.

**Auth is disabled entirely only when `APP_PASSWORD` is blank AND the users table
is empty.** That combination is local dev; it must never be the state of a
deployment. `PUBLIC_BASE_URL` must be set when deployed, or set-password links
are built from the container's internal address.

## File roles

Shared shell:

| File | |
|---|---|
| `app/main.py` | All routes, auth middleware, template filters, the `REPORTS` / `TOOLS` tile lists |
| `app/perms.py` | Roles, area keys, access + brand/region scope resolution — the unit-tested part |
| `app/warehouse.py` | Read-only warehouse connection + the scoped report cache |
| `app/sell_through.py` | Sell-Through report data (scoped SQL → page JSON) |
| `app/items.py` | Item Explorer data: search, leaderboard, one item (scoped, capability-gated) |
| `app/st_table.py` | Sell-Through Table data + Excel export |
| `app/notify.py` | Invite / password-reset email via the shared n8n webhook |
| `app/sales_explorer.py` | Sales Explorer: in-memory revenue summary, FY periods, matrix, Excel |
| `app/receipts.py` | Which retailers' sell-through is in for the latest week (the strip) |
| `app/sales.py` | Home page internal sales (FY periods, pace, targets hook) |
| `app/field.py` | Field Activity (Repsly): loads + the display-compliance rules |
| `app/security.py` | PBKDF2 hashing, cookie signing, set-password tokens |
| `app/db.py` | Engine, session, additive column migrations |
| `app/models.py` | SQLAlchemy models for both tools, plus `User` |
| `app/templates/base.html` | Shell for every page — `{% block nav %}` and `{% block brandword %}` |
| `app/static/style.css` | All CSS, light + dark. No framework, no build step |

Vendor Promos: `calc.py`, `service.py`, `export.py`, `workflow.py`,
`promo_data.py`, `pricing_sync.py`, `seed.py`, templates `promo_base.html` / `dashboard.html` /
`promo_detail.html` / `promo_new.html` / `help.html`.

**Promo reference data comes from the warehouse (2026-10-21, user's request)** —
`app/promo_data.py`, not the `products` table. Item = `dim_item` (case-insensitive
code), buy price = `dim_item_price` for the block's price level, RRP = level 1.
- **Retailer picker** = NetSuite price levels with prices in the promo's currency,
  except a level used by sell-through retailers is listed as those retailers
  (`dim_ns_customer.customer_key`; a retailer's level = the one most of its accounts
  are on). NZ level 28 → "Noel Leeming" and "Warehouse Stationery", separately
  (user). `promo_retailers.price_level_id` / `st_customer_key` store the pick; blocks
  made earlier hold only a name and are matched by `norm_name` once, then remember.
- **Rebates stay the tool's own list** (`retailers` table, user — NOT NetSuite's
  `base_rebate_pct`, which differs: Bunnings 8% vs 15%), matched by the entry's name
  only (a shared price level must not pass NL's rebate to Warehouse Stationery).
  Retailers with no row default to 0 (Retravision, NZ Bunnings, Warehouse Stationery).
- **6wk avg = sell-through only (user):** sum of `fact_sell_through.qty_sold` over the
  retailer's last six LOADED weeks with week-ending before the promo start, ÷ weeks
  found (zero weeks count) — a late file never blanks it. Filled on add / copy, stored
  with `line_items.avg_6wk_source` (tooltip; "Entered by hand" when typed over);
  "refresh 6wk avg" per block overwrites. No sell-through feed → blank, typed by hand.
  Checked 2026-10-21 vs independent SQL (HN AU / JB AU / NL NZ E8173C22, OW A1902T11):
  averages and buy prices equal.
- `/api/product` is now under the promos area in `_TOOL_PATHS` (it returns buy prices
  and sell-through; before, any staff login could call it).
- **The promo page is a grid (user, 2026-10-21, from their Excel promo form):** products
  = rows (`service.ProductRow`, keyed by upper(code)), each retailer block = a column group
  (`templates/_promo_grid.html`), collapsible per browser (`localStorage promo-open-<id>`).
  Buy ex / % off / 6wk avg / actual sales are typed in the grid → `POST /line/{id}/set`
  (JSON), then the page re-fetches itself and swaps `#promo-live` (totals, grid, customer
  cards, vendor request) keeping focus — numbers are only ever computed server-side.
  ✎ = the old line form in a `<dialog>`. Products are added by paste
  (`POST /promo/{id}/products`, `promo_data.parse_paste` + `items()` — two queries for
  the whole paste) to the ticked blocks; codes not in NetSuite are skipped and listed.
  The old one-line add form, `/api/product` and copy-from are gone.
- **Per-product dates:** `line_items.start_date/end_date` (NULL = the promo's), kept
  equal on every line of a code (`_set_product_dates`); a line's weeks = its own window
  (`service.line_dates`) and its 6wk-avg window ends before its own start. Moving a
  product's or the promo's start re-pulls sell-through avgs, never hand-typed ones.
  The export has From / To columns.
- **Not yet removed:** the n8n nightly pricing push → `/admin/sync-pricing` →
  `products` still runs but nothing reads `products` any more. Retire the n8n
  workflow, then the endpoint, `pricing_sync.py`, the product seed and the
  `products` table.

Vendor Bill Importer: `bill_calc.py`, `bill_pdf.py`, `bill_export.py`,
`bill_service.py`, template `bills.html`.

Landing pages: `home.html` (reports), `tools.html`. Reports: `report_sell_through.html`.

**Navigation is a left fly-out menu** (`_sidenav.html` + `static/sidenav.css`,
2026-10-15, built for phones first): a ☰ button in each page's top bar opens a
drawer listing the current tool's own links, Reports, Tools, Admin, then the
user and sign-out. The light / dark switch is a sun/moon button at the top right of every top bar (`_theme_btn.html`, included by `base.html`, `_rpt_base.html` and the dashboard; moved out of the menu 2026-10-21). Beside it, a **colour palette** picker
(Blue = default, Teal, Violet; user 2026-10-21): `html[data-palette]` overrides only the accent
family in each stylesheet (`--r-accent*`, `style.css --brand`, the dashboard's `--accent*`,
`--sn-*`) — defined once at the end of `sidenav.css`, never the up/down / warning / sell-in
colours. Saved per browser (`localStorage.palette`), applied pre-paint in each shell's head;
`RPT.chart` redraws on a theme or palette change. A new colour should use the accent tokens,
not a hard-coded blue, or it won't follow the palette. `base.html` includes the drawer and also shows the
current tool's `{% block nav %}` links in the top bar on screens wider than
760px; the self-styled report pages include the same partial. The drawer lists
only what `nav_reports` / `nav_tools` allow — presentation only.

Auth and admin templates: `login.html`, `set_password.html`, `denied.html`,
`admin_users.html`, `admin_user_form.html`. The first two are standalone pages
(no `base.html`) because they render before there is a session to build a nav from.

Promo pages extend `promo_base.html` (which supplies the promo nav), not
`base.html` directly. Everything else extends `base.html` and fills
`{% block nav %}` — don't add a "Users" link there, the side menu already has it
for admins.

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
