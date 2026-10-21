How to add an update (shown at /updates, newest first):

    ## YYYY-MM-DD | Short title | area keys (optional)
    - one line per change, in plain words for the people using the site

Area keys limit who sees an entry: the report/tool keys in perms.py (sales,
st_weekly, field, items, st_table, sell_through, promos, vendor_bills) or
"admin". No keys = everyone, vendors included - so say nothing internal there.
Add an entry with every commit people would notice. Everything above the first
"##" is ignored.

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
