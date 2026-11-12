How to add an update (shown at /updates, newest first):

    ## YYYY-MM-DD | Short title | area keys (optional)
    - one line per change, in plain words for the people using the site

Area keys limit who sees an entry: the report/tool keys in perms.py (sales,
st_weekly, field, ageing, items, st_table, sell_through, promos, vendor_bills) or
"admin". No keys = everyone, vendors included - so say nothing internal there.
Add an entry with every commit people would notice. Everything above the first
"##" is ignored.

## 2026-11-12 | Vendor Promos: claims from actual sales | promos
- Once you type actual sales, the vendor claim follows them: the vendor credit request and the totals now show the expected and the actual claim, and claim the actual. Lines without actual sales count as 0 and are flagged.
- Leave the vendor amount blank to claim the calculated figure; a typed amount overrides it, and "Use the calculated amount" switches back.
- New Claim view and Excel export — the claim to send the vendor: support per unit × units sold, per customer, with the claim total.
- New Vendor reference field on a promo (the vendor's own promo id), printed on the claim.
- New Promotion documents section: keep the vendor's promo sheet, approvals and the claim as sent with the promo.

## 2026-11-10 | Try the glass look
- A new button in the top bar, beside the colour and light / dark buttons, switches the site to a frosted "glass" look. It's off unless you turn it on.
- Its menu has sliders for panel opacity, blur, glow and shine, and five backgrounds. Your choice is remembered in this browser only.

## 2026-11-10 | Inventory Ageing: over 120 and over 240 | ageing
- Two new lines under "List products": Over 120 and Over 240, alongside Over 90, 180 and 370.
- The line you pick now drives the whole page: the Over / % over / vs 30d columns, the tiles, the band chart and the aged-share chart all switch to it (All stock measures at 90 days).
- The table has six age bands: 0–90, 91–120, 121–180, 181–240, 241–370 and 370+. They're NetSuite's own bucket edges, so nothing is estimated.
- The 3PL box at the bottom now also shows consignment stock (AU6 Big W and Consignment NZ), in units, split AU and NZ.

## 2026-11-09 | Faster loading
- Sales Explorer, Weekly Sell-Through, Sell-Through, Inventory Ageing and the home page now read from pre-summed tables, so a page that has to be built fresh (the first visit after an update, or a brand-limited view) is several times quicker.
- After the site is updated, its pages are ready about twice as soon, and Inventory Ageing and Field Activity are now prepared in advance too.

## 2026-10-23 | New: Inventory Ageing | ageing
- How old Macgear's stock is, from NetSuite's daily ageing report since January: totals, what's past 90 / 180 / 370 days, and how that has moved.
- One table by brand: open a brand to see its products, and a product to see its locations. Sort by any column, switch between value and units, pick a location, or list only stock past 90, 180 or 370 days.
- "Aged since" shows when a product's stock first crossed the line and has stayed there. Download what the table lists as a CSV.
- Third-party (3PL) and consignment stock are kept out of the figures; 3PL units are shown on their own.

## 2026-10-23 | Sales Explorer shows targets | sales
- Sales Explorer now shows the official targets: a Target card (with % of target), a target line on the by-month chart, and Target / % of target columns in the table and the Excel.
- Account managers can see their own target progress: pick yourself under Account manager, or build the table by Account manager. A retailer's target counts towards the account manager NetSuite has on that retailer.
- Targets are set by retailer, brand and month, so the table doesn't show them by store/account or item (the totals still do).
- For a period still running, the target is counted to date (a part month by its share of weekdays), so it compares like with like.

## 2026-10-22 | New: Targets | targets
- The Targets planner now lives here under Tools: build next year's targets on NetSuite sales (any financial year, or this year to date) with growth %, monthly pacing and new lines. No spreadsheets to upload.
- New "Commit" button: when a scenario is final, commit it as the official targets for that financial year. Earlier commits stay in the history ("Committed"), each with a CSV.
- The home page measures sales against the latest committed targets.
- New scenarios start from what you're planning: the year, the months (e.g. Q4), and where to start — last year's sales, this year so far, or the current official targets (and growth % works on top of official targets).
- A part-year scenario shows just its months, next to last year's same months, the official targets and this year's trend.
- Commit shows exactly what will change, month by month, and can be done for one country only.
- A scenario can plan just part of the year — a half, a quarter or chosen months. Committing it replaces only those months, so you can set Q4 without touching the H1 and Q3 targets already released.

## 2026-10-22 | Every page's header follows your colour palette
- The bar across the top of every page is now the colour of the palette you picked (Blue, Teal or Violet) - reports included, not just the tools.

## 2026-10-21 | Vendor Promos: paste products, side-by-side retailers | promos
- Paste a list of product codes from Excel (a second column can be the % off) and tick which retailers get them — or use "+ products" on one retailer, or "+ add" in an empty cell.
- The promo page is now one grid: products down the side, each retailer as a group of columns you can collapse and expand.
- Type buy ex, % off, 6wk avg and actual sales straight into the grid; it saves as you go and the totals update.
- Products can have their own From / To dates; expected sales use that product's dates.

## 2026-10-21 | Vendor Promos: 6wk avg fills itself | promos
- Typing a SKU on a promo now fills the 6wk avg from the retailer's own sell-through (its last six weeks loaded before the promo starts), as well as the description, RRP and buy price.
- If a retailer's latest week isn't in yet, the six weeks before it are used. Hover a 6wk avg to see where it came from; type over it to use your own figure.
- New "refresh 6wk avg" link on each retailer block re-pulls them all. Copying SKUs to another retailer now brings that retailer's 6wk avg too.
- The retailer list now comes straight from NetSuite price levels, so buy prices always match the retailer's level. Noel Leeming and Warehouse Stationery are listed separately.

## 2026-10-21 | Pick a colour palette
- New palette button next to the light / dark switch: choose Blue, Teal or Violet.
- Your choice is remembered on this browser.

## 2026-10-21 | Light / dark switch moved
- The light / dark switch is now the sun / moon button at the top right of every page.

## 2026-10-21 | Pages use the full screen
- Every page now uses the full width of your screen instead of a centred column. Phones look the same as before.

## 2026-10-21 | New: Weekly Sell-Through report (beta) | st_weekly
- The consolidated weekly sell-through report on one full-screen page: sell-through vs sell-in, retailer stock and weeks of cover, Macgear stock.
- Filter by market (AU / NZ), brand, customer and period (13 / 26 / 52 weeks / all).
- Stores tab for the retailers that send store-level data; each product also lists its stores.
- Select a customer and every number on the page is that customer's only - safe to show in a meeting with them.
- PO arrival dates (ETAs) are coming soon.

## 2026-10-18 | Display compliance: three measures per product | field
- Harvey Norman display now shows Graded (where HN's grading says it should be - the rep measure), Over and above (extra stores reps secured) and Total coverage (all HN stores).
- One HN store tier now applies to Floorcare, Outdoor Living and Personal Care.

## 2026-10-17 | New: Field Activity | field
- Mova field reps from Repsly: dashboard, display compliance, activity feed and map, schedule, visits, reps and photos.

## 2026-10-16 | Sales update within minutes of NetSuite | sales
- Sales figures refresh as soon as new NetSuite data lands, instead of on a timer.
- Sales to NZ intercompany are shown separately (they're not counted as revenue).
- Account manager figures use each customer's current account manager, for all history.

## 2026-10-15 | New: Sales Explorer | sales
- Macgear revenue for any financial-year period vs last year, with a build-your-own table and Excel download.

## 2026-10-15 | Email invites and "Forgot password?"
- New accounts get an invite email to choose their own password, and "Forgot password?" on the sign-in page sends a reset link.

## 2026-10-15 | Macgear Reporting launched
- One site for Macgear's reports and tools at reporting.macgeargroup.com, with a login per person.
