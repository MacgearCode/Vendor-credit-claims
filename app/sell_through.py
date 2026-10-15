"""Sell-Through report — the data behind /reports/sell-through.

Started from `Sell Through/prototype/build_prototype.py`; the page's JSON shape
is the same, so the prototype's front end works unchanged. Differences:

  - built per request (cached) instead of written into a static file;
  - scoped server-side: the SQL takes the user's brands and regions, so rows a
    user may not see never leave the database, let alone reach the browser;
  - plain Python instead of pandas (the app doesn't carry pandas).

What goes in:
  - totals per region x brand x retailer x week (units, retail value, SOH, on
    order) - every product, so the headline, trend and retailer split are exact;
  - product detail only for each brand's top `per_brand` products per region,
    by retail value and by units over the window (union).
AU and NZ are never summed - the page switches between them (AUD vs NZD).
Value = units x item-list RRP (the workbook's Retail Value), not retailer $.
"""
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import warehouse

UNMATCHED = "Not on item list"
CURRENCY = {"AU": "AUD", "NZ": "NZD"}
WEEKS = 26
PER_BRAND = 10

# brands = NULL means every brand INCLUDING lines not on the item list (staff
# only). A list matches brand names exactly, so unmatched lines (brand NULL)
# can never reach a brand-scoped user.
_FACT_SQL = """
    select r.country_code as region, c.customer_name as customer,
           i.macgear_code as code, i.description as name, i.brand,
           d.full_date as wk,
           sum(f.qty_sold) as u, sum(f.qty_sold * f.rrp) as v,
           sum(f.soh) as s, sum(f.on_order) as o, max(f.rrp) as rrp
    from reporting.fact_sell_through f
    join reporting.dim_item     i on i.item_key     = f.item_key
    join reporting.dim_customer c on c.customer_key = f.customer_key
    join reporting.dim_region   r on r.region_key   = f.region_key
    join reporting.dim_date     d on d.date_key     = f.date_key
    where d.full_date = any(%(weeks)s)
      and r.country_code = any(%(regions)s)
      and (%(brands)s::text[] is null or i.brand = any(%(brands)s::text[]))
    group by 1, 2, 3, 4, 5, 6"""


def _num(x):
    """Sums stay exact Decimals until the final rounding; float addition order
    would otherwise move a .50 value by a dollar between two builds."""
    return None if x is None else Decimal(x)


def _int(x) -> int:
    return int(Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _series(rows, weeks, col, fill_zero):
    """One measure as a list aligned to `weeks`; 0 when it carries nothing at all.
    A week with no non-null value is None (pandas sum(min_count=1))."""
    per = {}
    for r in rows:
        x = r[col]
        if x is not None:
            per[r["wk"]] = per.get(r["wk"], 0) + x
    s = [per.get(w) for w in weeks]
    if all(x is None for x in s) or (fill_zero and all(not x for x in s)):
        return 0
    if fill_zero:
        s = [x or 0 for x in s]
    return [None if x is None else _int(x) for x in s]


def _cell(rows, weeks):
    return [_series(rows, weeks, "u", True), _series(rows, weeks, "v", True),
            _series(rows, weeks, "s", False), _series(rows, weeks, "o", False)]


def _group(rows, key):
    out = defaultdict(list)
    for r in rows:
        out[r[key]].append(r)
    return out


def _top(totals: dict, measure: str, n: int) -> set:
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1][measure], kv[0]))
    return {code for code, _ in ranked[:n]}


def build_region(rows: list[dict], weeks: list, per_brand: int, currency: str) -> dict:
    cust_units = defaultdict(int)
    for r in rows:
        cust_units[r["customer"]] += r["u"] or 0
    customers = sorted(cust_units, key=lambda c: (-cust_units[c], c))
    ci = {c: i for i, c in enumerate(customers)}

    totals, skus = {}, {}
    by_brand = _group(rows, "brand")
    for brand, bg in by_brand.items():
        totals[brand] = {str(ci[c]): _cell(cg, weeks) for c, cg in _group(bg, "customer").items()}
        sold = defaultdict(set)
        for r in bg:
            if (r["u"] or 0) > 0:
                sold[r["wk"]].add(r["code"])
        skus[brand] = [len(sold.get(w, ())) for w in weeks]

    brand_units = {b: sum(r["u"] or 0 for r in bg) for b, bg in by_brand.items() if b != UNMATCHED}
    brands = [{"name": b, "units": _int(u)}
              for b, u in sorted(brand_units.items(), key=lambda kv: (-kv[1], kv[0])) if u > 0]

    products = []
    for brand, bg in by_brand.items():
        if brand == UNMATCHED:
            continue
        by_code = _group(bg, "code")
        tot = {code: {"u": sum(r["u"] or 0 for r in pg), "v": sum(r["v"] or 0 for r in pg)}
               for code, pg in by_code.items()}
        keep = _top(tot, "v", per_brand) | _top(tot, "u", per_brand)
        for code in sorted(keep):
            if tot[code]["u"] <= 0:
                continue
            pg = by_code[code]
            dated = sorted((r for r in pg if r["rrp"] is not None), key=lambda r: r["wk"])
            products.append({
                "code": code, "name": pg[0]["name"], "brand": brand,
                "rrp": float(round(dated[-1]["rrp"], 2)) if dated else None,
                "cells": {str(ci[c]): _cell(cg, weeks) for c, cg in _group(pg, "customer").items()},
            })

    last = weeks[-1]
    return {
        "currency": currency, "customers": customers, "brands": brands,
        "totals": totals, "skus": skus, "products": products,
        "check": {"units_last_week": _int(sum(r["u"] or 0 for r in rows if r["wk"] == last))},
    }


def build(brands: list[str] | None, regions: list[str],
          weeks_n: int = WEEKS, per_brand: int = PER_BRAND) -> dict:
    """The page's DATA object for one data scope. brands None = every brand."""
    with warehouse.connect() as conn, conn.cursor() as cur:
        weeks = warehouse.recent_weeks(weeks_n)
        rows = []
        if weeks and regions and (brands is None or brands):
            cur.execute(_FACT_SQL, {"weeks": weeks, "regions": list(regions), "brands": brands})
            cols = [d.name for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    for r in rows:
        for c in ("u", "v", "s", "o", "rrp"):
            r[c] = _num(r[c])
        r["brand"] = r["brand"] or UNMATCHED
        r["name"] = r["name"] or r["code"]

    by_region = _group(rows, "region")
    out_regions = {rg: build_region(by_region[rg], weeks, per_brand, CURRENCY.get(rg, rg))
                   for rg in regions if rg in by_region}

    notes = [
        "Value = units x item-list RRP, in the region's own currency (AUD / NZD are never added).",
        f"Totals include every product; product detail shows each brand's top {per_brand} per region.",
        "Retailers that send no stock are left out of weeks of cover.",
    ]
    if any(r["brand"] == UNMATCHED for r in rows):
        notes.append(f'"{UNMATCHED}" = retailer lines not on the Macgear item list - counted in totals, no RRP.')

    return {
        "weeks": [w.isoformat() for w in weeks],
        "built": date.today().isoformat(),
        "perBrand": per_brand,
        "regions": out_regions,
        "notes": notes,
    }


def for_user(brands: list[str] | None, regions: list[str]) -> dict:
    key = ("sell_through", None if brands is None else tuple(sorted(brands)), tuple(regions))
    return warehouse.cached(key, lambda: build(brands, regions))
