How to add an update (shown at /updates, newest first):

    ## YYYY-MM-DD | Short title | area keys (optional) | feature (optional)
    - one line per change, in plain words for the people using the site

"feature" puts an entry under What's new: a new report or tool page, or a major new
capability. Everything else (cosmetic changes, tweaks, fixes) goes under Release
notes. Leave the area keys empty when a feature is for everyone:
    ## YYYY-MM-DD | Short title | | feature

Area keys limit who sees an entry: the report/tool keys in perms.py (sales,
st_weekly, field, ageing, items, st_table, sell_through, promos, vendor_bills) or
"admin". No keys = everyone, vendors included - so say nothing internal there.
Add an entry with every commit people would notice. Everything above the first
"##" is ignored.

## 2026-11-24 | Team view for senior PMs | brand_profit
- Brand Profitability's PM filter now lists each senior PM's team, e.g. "Rachel Harris (team)": their own brands plus those of the PMs who report to them, with the team total. Picking one PM still shows just that PM's brands.
- Teams follow the month: someone who joined a team in September counts in it from September on.

## 2026-11-25 | Sales Explorer: product manager filter and rows | sales
- Filter Sales Explorer by product manager, or by a senior PM's team (their own brands plus those of the PMs who report to them). Targets follow the filter.
- "Product manager" is also a level in Build your table, e.g. Product manager › Brand. As with account managers, a brand's PM today applies to all its past sales, so last year compares like with like.

## 2026-11-25 | Month target dial on the home page | sales
- This month's target is now a dial. The solid fill is sales so far, the faded fill is where the current pace lands, and the tick is the target. It shows amber when behind pace and green when on pace to beat it.
- The dial runs to the next whole million above the target (or above sales, if they're higher), so going past target shows.

## 2026-11-24 | Account managers by product category | sales, admin
- An account manager can now be set for one product category of a brand at a retailer. At The Good Guys AU, Eufy Security is Chris O'Donohue's and Eufy floorcare (Appliances) and Baby are Mike Bihary's.
- Their sales split exactly by each product's category. Targets are set by retailer and brand only, so each manager's share of the Eufy target is estimated from the last 12 months' sales mix (about 65% Security, 35% floorcare and Baby today), and moves as the mix does.

## 2026-11-24 | Account managers by retailer and brand | sales, admin | feature
- Account managers can now be set by retailer, and by brand where a retailer is split, e.g. at JB Hi-Fi AU, Mova and Outin are Mike Bihary's and the other brands Chris O'Donohue's. Retailers not set keep the account manager on the NetSuite customer record.
- The home page's account managers, Sales Explorer (rows, filter, targets) all follow it. As before, the current manager applies to the whole history, so each manager sees their full portfolio.
- Admins set PM teams and account managers on the new **Portfolios** page (side menu, Admin). Rows that differ from NetSuite are marked so the customer record can be fixed too.

## 2026-11-20 | Manual adjustments already in NetSuite are flagged | brand_profit, brand_profit_settings
- When accounts post in NetSuite something that's also a manual adjustment (e.g. a vendor's rebate reimbursement on 51085 Brand Rebate recovery), it would count twice. The settings page now marks that row ⚠ with the NetSuite document it matches (same month, brand and type, amount within $10 or 0.1%), and the report's drill-down warns too. Delete the manual row when it's the same entry.
- Three such rows were removed today: AU October EUFY and Anker customer-rebate accruals, and NZ September EUFY's — NetSuite now has them.

## 2026-11-20 | Freight and vendor rebates straight from NetSuite | brand_profit, brand_profit_settings
- Brand Profitability now takes freight and vendor rebates from NetSuite automatically, about every 30 minutes — no more uploading the saved search at month end.
- New on the settings page (Freight & vendor rebates tab): **Manual adjustments**, for amounts finance adds that never post in NetSuite, like the CYA's "RPP only (Non-GL posting)" customer-rebate and MDF accruals. Enter them row by row (month, brand, type, amount, memo); they're added on top of the NetSuite lines, show as "Manual adjustment" in the report's drill-down, and every change is in the change log. September and October's rows from the CYA are already in.
- Freight and vendor rebates sit on the brand NetSuite has on each line. Where the CYA moved a line to another brand by hand, fix the line's brand (class) in NetSuite.
- **Download all settings (Excel)** on the settings page, for audit: every rate with the months it covers and whether it's in force now, PMs, GL accounts, manual adjustments, attached documents and the full change log. The change log tab also downloads on its own.
- Attach a supporting PDF (e.g. the customer contract, up to 10 MB) when you enter a **New rate from**, add a rate, or to any existing row with **Attach PDF**. It's listed on the row, opens from there, and is never deleted.

## 2026-11-20 | GL Accounts for PMs | gl_mova, gl_anker | feature
- New reports: every NetSuite transaction on the balance-sheet accounts PMs look after, so you can check them during the month instead of waiting for finance at month end. One for Mova (recoverables, MDF accruals) and one for Anker / Eufy / Soundcore (recoverables, rebate and returns claims, CMF accruals by retailer, the EUFY MDF buffer).
- Each account shows its opening balance, the movement in the months you pick and its closing balance (which matches NetSuite), and lists every transaction with its date, type, document, name, memo, brand and a running balance.
- Pick any months (This month, Financial year to date or All history are one click), switch Australia / New Zealand, download to Excel. Refreshed from NetSuite about every 30 minutes.

## 2026-11-19 | User activity log | admin | feature
- Users list: last sign-in (how long ago, with the date, time and number of sign-ins), last active, and a red flag when someone has failed to sign in since their last success.
- Each user's page has an Activity card: last sign-in and IP, days used in the last 30, which reports they open, and their sign-in and account history.
- New Activity log (Admin menu): sign-ins, failed sign-ins (and from which address), password resets, invites, and every change an admin makes to an account with what changed; which reports are used and by how many people; accounts never signed in or idle 30+ days.

## 2026-11-19 | Open Orders | open_orders | feature
- New report: everything customers have ordered that the warehouse hasn't shipped yet, refreshed from NetSuite about every 30 minutes — open value, picking lines, units, and how long orders have been waiting.
- Top 10 charts for open value and for picking lines, by retailer, account or brand. Every order and product is listed below (oldest first, click an order to see its lines); download it all as CSV.
- Every open order is split into **ready to ship now** (stock committed to it — it can be picked and invoiced today) and **back-ordered** (waiting on stock). The Top 10 bars show both (solid = ready, faded = back-ordered), and "Rank Top 10 by: Ready to ship now" puts first the customers the warehouse can actually ship.
- Holding Orders (forward orders held for retailers), intercompany orders to NZ and samples are kept out of the figures unless you tick them in.
- The home page has an Open orders card per country, and each item in the Item Explorer shows its open orders.

## 2026-11-18 | Targets is live | targets | feature
- Tools → Targets now works on the live site: build next year's sales targets from NetSuite actuals with growth %, monthly pacing and new lines, then Commit the final set as the official targets for that financial year.
- Once a set is committed, the home page tiles and Sales Explorer show progress against it.
- Scenarios made while it was being tested on a laptop weren't carried over, so the list starts empty.

## 2026-11-17 | Brand Profitability report | brand_profit, brand_profit_settings | feature
- New report: net profit by PM and brand, after customer rebates, freight, vendor rebates and MDF — the CYA workbook's method, for AU and NZ, any month, quarter or year to date of the financial year. Open a brand to see its customers and the freight / vendor-rebate lines behind it; download to Excel.
- Filter by PM; collapse to PMs, brands or customers. A PM's GP target is now their brands' targets weighted by sales, with the gap in dollars.
- Freight and vendor rebates are coloured by their effect on profit: red reduces it, green (a vendor credit) adds to it.
- Settings (for those who manage them): customer rebates by month, brand, item-category and SKU exceptions, store extras, MDF, GP targets, the PM for each brand (per region), and freight / vendor-rebate uploads. Every change is logged.

## 2026-11-17 | Net profit instead of estimated GP | sales | feature
- Where the site showed estimated GP (home page, Sales Explorer, Item Explorer) it now shows net profit: revenue less cost, customer rebates, MDF, and each brand's freight and vendor rebates shared by sales — the same figures as the Brand Profitability report.
- Net profit % is shown on gross sales and on net sales (after customer rebates). A month whose freight and vendor rebates aren't loaded yet is marked *.

## 2026-11-12 | Vendor Promos: funding split per customer | promos
- The green / red highlight on actual sales is now readable in dark mode and the glass look.
- Actual sales typed on a line with no 6wk avg now show their claim: once a promo has actual sales, the grid adds "Act Supp Claim" and "Act MG Claim" beside the expected ones (and the Internal Excel export has them too).
- Each customer card now has the funding split beside Rebate and Growth: Supplier and MacGear %, with the retailer funding the rest (shown as you type). Leave them blank to use the promo's default split; a split set on a single line still wins.
- The card is tidier: the name and its links on one line, then one strip of settings.

## 2026-11-12 | Vendor Promos: move products around | promos
- Editing a box in a long grid no longer jumps the table back to the top; it stays where you were.
- Drag a product, or a separator, by the ⋮⋮ handle at the start of its row to move it; it saves when you let go. The Excel exports follow the new order.
- Add products has a "Place new products" choice: at the end, at the top, or after a chosen product. Added after a product that ends a group, they stay inside that group.

## 2026-11-12 | Vendor Promos: easier dates, clearer removing | promos
- The From / To boxes in the grid are now plain dd/mm/yyyy boxes: select, copy and paste them like any cell (they didn't take Ctrl+C / Ctrl+V before). 📅 beside each opens a calendar; Enter moves down a row.
- The ✕ in a retailer's columns takes the product off that retailer only — it now says which retailer. The ✕ at the end of each row (every retailer) is gone: tick products and press "Remove selected" instead.

## 2026-11-12 | Vendor Promos: find products, paste dates | promos
- Add products now has two boxes: paste codes on the left, or find products on the right — it lists the promo brand's products and filters as you type (tick "all brands" to search everything). Tick what you want and add it, optionally with a % off.
- Rows copied straight from the Excel promo form (From, To, Code, Description…) bring each product's dates with them; a header row is skipped.
- Dates copy and paste: Ctrl+C on a From / To box copies it, pasting a date (12/11/2026, 12-Nov-26…) fills it, and pasting a column of dates from Excel fills down the products. "Dates for ticked" gives every ticked product the same dates.

## 2026-11-12 | Vendor Promos: growth per customer, separators in Excel | promos
- Each customer card now has a Growth box beside Rebate: that customer's expected growth, which drives its expected sales and claim. Leave it blank to use the promo's default; a growth set on a single line still wins.
- The Excel exports (including the Claim) now list products in the grid's order, with the separators and their headings.

## 2026-11-12 | Vendor Promos: remove several products at once, edit rebates, separators | promos
- Each product row on a promo now has a tick box. Tick the ones to drop (the box in the Code heading ticks them all) and press "Remove selected" to take them off every retailer in one go.
- A customer's rebate can now be changed after the promo is set up: type it in the Rebate box on the customer's card. Support, margins and claims update straight away. "rebate x%" in the grid heading jumps to the card.
- Separate groups of products in the grid, like the blank rows in the Excel form: blank rows in what you paste become separators, or click "⎯ gap" at the end of a product row. A separator can have a heading (e.g. "Laptop stands").

## 2026-11-12 | Vendor Promos: claims from actual sales | promos | feature
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

## 2026-10-23 | New: Inventory Ageing | ageing | feature
- How old Macgear's stock is, from NetSuite's daily ageing report since January: totals, what's past 90 / 180 / 370 days, and how that has moved.
- One table by brand: open a brand to see its products, and a product to see its locations. Sort by any column, switch between value and units, pick a location, or list only stock past 90, 180 or 370 days.
- "Aged since" shows when a product's stock first crossed the line and has stayed there. Download what the table lists as a CSV.
- Third-party (3PL) and consignment stock are kept out of the figures; 3PL units are shown on their own.

## 2026-10-23 | Sales Explorer shows targets | sales | feature
- Sales Explorer now shows the official targets: a Target card (with % of target), a target line on the by-month chart, and Target / % of target columns in the table and the Excel.
- Account managers can see their own target progress: pick yourself under Account manager, or build the table by Account manager. A retailer's target counts towards the account manager NetSuite has on that retailer.
- Targets are set by retailer, brand and month, so the table doesn't show them by store/account or item (the totals still do).
- For a period still running, the target is counted to date (a part month by its share of weekdays), so it compares like with like.

## 2026-10-22 | New: Targets | targets | feature
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

## 2026-10-21 | New: Weekly Sell-Through report (beta) | st_weekly | feature
- The consolidated weekly sell-through report on one full-screen page: sell-through vs sell-in, retailer stock and weeks of cover, Macgear stock.
- Filter by market (AU / NZ), brand, customer and period (13 / 26 / 52 weeks / all).
- Stores tab for the retailers that send store-level data; each product also lists its stores.
- Select a customer and every number on the page is that customer's only - safe to show in a meeting with them.
- PO arrival dates (ETAs) are coming soon.

## 2026-10-18 | Display compliance: three measures per product | field
- Harvey Norman display now shows Graded (where HN's grading says it should be - the rep measure), Over and above (extra stores reps secured) and Total coverage (all HN stores).
- One HN store tier now applies to Floorcare, Outdoor Living and Personal Care.

## 2026-10-17 | New: Field Activity | field | feature
- Mova field reps from Repsly: dashboard, display compliance, activity feed and map, schedule, visits, reps and photos.

## 2026-10-16 | Sales update within minutes of NetSuite | sales
- Sales figures refresh as soon as new NetSuite data lands, instead of on a timer.
- Sales to NZ intercompany are shown separately (they're not counted as revenue).
- Account manager figures use each customer's current account manager, for all history.

## 2026-10-15 | New: Sales Explorer | sales | feature
- Macgear revenue for any financial-year period vs last year, with a build-your-own table and Excel download.

## 2026-10-15 | Email invites and "Forgot password?" |  | feature
- New accounts get an invite email to choose their own password, and "Forgot password?" on the sign-in page sends a reset link.

## 2026-10-15 | Macgear Reporting launched |  | feature
- One site for Macgear's reports and tools at reporting.macgeargroup.com, with a login per person.
