# Vendor Credit Claims — Project Spec

A platform to manage promotional credit claims end-to-end: create promos, send them to
customers, track inbound customer claims, and raise outbound credit requests to vendors.
Replaces the current Monday.com + per-promo Excel (`AU_Promo Form_TEMPLATE.xlsx`) workflow.

## Business context

MacGear (the user) sits between **retailers/customers** (Officeworks, JB Hi-Fi, Harvey
Norman, The Good Guys, Bing Lee, Amazon, Bunnings…) and **brands/vendors** (the suppliers).

Flow:
1. MacGear runs a promo with a customer on stock the customer **already bought**.
2. Customer runs the promo, then sends MacGear a **credit claim** for the discount given.
3. MacGear raises a **credit request to the vendor** to recover the funded portion.
4. The discount is split 3 ways — **Retailer / MacGear / Brand** — so the vendor claim,
   MacGear's absorbed cost, and the customer claim are usually *not* equal.

Tracking is at **line-item / SKU level**. Split is rule-based (per retailer/vendor defaults)
**plus** per-line and ad-hoc overrides.

## Data model (derived from the template)

- **Promotion** — one brand, a name, AU claim number, date range (drives # weeks), a
  3-way funding split (Retailer/MacGear/Brand ratios), an AUD→USD rate.
- **Promotion → Retailer block** — each promo covers 1..N retailers. Each retailer has a
  **customer rebate %** (looked up from the retailer master; e.g. JB 19%, Officeworks 13.5%).
- **Line item** (per retailer) — Code, Description (lookup), Retailer Buy ex, Recommended
  % Off, 6wk avg, growth expectation, SOH. Computed fields below.
- **Customer claim** (inbound) — date, amount claimed by customer, status
  (Not Received → Received → Verified → Credited), check value vs expected.
- **Vendor request** (outbound) — supplier claim total, AUD & USD, status
  (Not Sent → Sent → Approved → Credited), claim date.

### Reference data
- **Retailer master** — name + default rebate % (the "Data Validation" sheet).
- **Product master / Master Price Level (Query table)** — Code → Description, RRP Inc,
  Base price, etc. Source of truth for product lookups. (Origin TBD — NetSuite export?)

## Calculation engine (per line item)

Inputs: `G` = Retailer Buy ex; `rebate` = retailer rebate %; `pctOff` = Recommended % Off;
`ratioRetailer/ratioMG/ratioSupplier` (sum ≈ 1); `weeks` = (end-start+1)/7;
`avg6wk`; `growth`; `RRPInc`, `RecSaleInc` from product master.

```
netBuy            = G * (1 - rebate)
totalSupportUnit  = netBuy * pctOff
supplierSupport   = totalSupportUnit * ratioSupplier      # H
mgSupport         = totalSupportUnit * ratioMG            # I
totalSupport      = supplierSupport + mgSupport           # J  (note: retailer share excluded)
stdMargin         = ((RRPInc/1.1) - (G - G*rebate)) / (RRPInc/1.1)            # K
promoMargin       = ((RecSaleInc/1.1) + totalSupport - (G - G*rebate)) / (RecSaleInc/1.1)  # L
expectedSales     = CEILING(avg6wk + avg6wk*growth, 1) * weeks               # O
supplierClaimAmt  = expectedSales * supplierSupport       # P
mgClaimAmt        = expectedSales * mgSupport             # Q
```

Totals: `Brand/Supplier claim AUD = Σ supplierClaimAmt`; `MacGear claim AUD = Σ mgClaimAmt`;
`Vendor claim USD = Brand claim AUD * audUsdRate`.

> Verify the exact margin formulas against the live workbook before relying on them — the
> `/1.1` is the AU GST factor; recommended-sale-inc and RRP-inc come from the product master.

## Three output views (same data, different projections)

| View       | Columns shown                                                                 | Hidden |
|------------|-------------------------------------------------------------------------------|--------|
| Internal   | everything                                                                     | —      |
| Sales/Cust | Code, Desc, Cust Code, Retailer Buy ex, **Total Support**, margins, 6wk avg, expected sales | funding split |
| Vendor     | Code, Desc, **Supplier Support**, expected sales, **Supplier Claim Amount**    | margins, MG support, retailer buy, rebate |

## Lifecycle / statuses to track (the Monday.com layer)

- Promo: Draft → Sent to customer → Live → Closed
- Customer claim: Not Received → Received → Verified → Credited (+ amount, date, check value)
- Vendor request: Not Sent → Sent → Approved → Credited (+ amount AUD/USD, date)

## Open questions — resolved

This was the pre-build spec; these were all settled during implementation and are
recorded here so the file isn't read as still-open.

- **Product master source** — NetSuite. Saved searches `customsearch1084` (AU) /
  `customsearch1413` (NZ) → RESTlet → n8n → `POST /admin/sync-pricing`, nightly.
  The Excel template is bootstrap-only now. See `netsuite/PRICING_SYNC.md`.
- **Multi-user / auth** — multi-user. Per-user accounts with per-tool access,
  managed at `/admin/users`; roles admin and user. See `CLAUDE.md`.
- **Hosting** — alongside n8n, on the DigitalOcean droplet behind its Caddy, at
  `https://apps.macgeargroup.com`. See `deploy/DEPLOY-BASIC.md`.
- **Monday.com migration** — started fresh; no migration was done.

Still open: whether to move from in-app accounts to M365 Entra SSO
(`deploy/DEPLOY.md` has the runbook ready).
