"""Weekly Sell-Through — the consolidated weekly report as one interactive page
(/reports/weekly). Layout follows a staff-built dashboard over the "Consolidated
Sell Through ANZ" workbook; the data comes from the warehouse instead of the file.

The server sends one compact dataset per scope; the page filters and totals it
in the browser (market, brand, customer, period are instant):

  rows  = region x item x retailer: weekly sell-through units, weekly sell-in
          units, this week's retailer SOH / on order / retail value
  mg    = Macgear stock per region x item (latest snapshot, saleable locations):
          on hand, available, on PO, open sales orders (committed + back ordered)
  eta   = open PO lines per region x item from reporting.fact_open_po — a
          placeholder until that table is loaded (see Sell Through/db/11_open_po.sql)

Definitions (the dashboard guide's, on warehouse data):
  sell-through   = retailer files (fact_sell_through), week ending Sunday
  sell-in        = fact_sales_line.net_sales_qty (account 54, intercompany
                   excluded) by the transaction's week-ending Sunday
  retail value   = units x item-list RRP (the region's currency) — the workbook's
                   "Retailer Sales Inc GST" is the same thing
  reporting retailer = a customer that sends sell-through in that region. Sell-in
                   to anyone else (Air NZ, loyalty, online …) only shows under
                   "All channels". NetSuite customers reach a retailer through
                   reporting.map_retailer, or failing that by the normalised name
                   (case, punctuation, "(NZ)", Limited/Ltd, Computers ignored).
AU and NZ stay separate rows; the page only adds them for units ("ANZ").

Sell-in, Macgear stock and ETAs are the `sell_in` capability: without it they
are never queried, so they never reach the browser.
"""
import re
from collections import defaultdict
from datetime import date, timedelta

import psycopg

from . import warehouse

WEEKS = 104                  # 52-week period + same weeks last year
UNMATCHED = "Not on item list"
CURRENCY = {"AU": "AUD", "NZ": "NZD"}

_ST_SQL = """
    with f as (
        select f.region_key, f.item_key, f.customer_key, f.date_key,
               sum(f.qty_sold) as u,
               sum(f.soh)      filter (where f.date_key = %(hi)s) as s,
               sum(f.on_order) filter (where f.date_key = %(hi)s) as o,
               sum(f.qty_sold * f.rrp) filter (where f.date_key = %(hi)s) as v
        from reporting.fact_sell_through f
        where f.region_key = any(%(rks)s) and f.date_key between %(lo)s and %(hi)s
        group by 1, 2, 3, 4)
    select f.region_key, i.macgear_code, i.description, i.brand, c.customer_name,
           f.date_key, f.u, f.s, f.o, f.v
    from f
    join reporting.dim_item     i on i.item_key     = f.item_key
    join reporting.dim_customer c on c.customer_key = f.customer_key
    where (%(brands)s::text[] is null or i.brand = any(%(brands)s::text[]))"""

# Brand is filtered in Python for sell-in: filtering fact_sales_line on brand in
# SQL has sent the planner down an 80-second path (Reporting Site/CLAUDE.md).
_SI_SQL = """
    with s as (
        select s.region_key, s.item_key, s.customer_key, s.customer_id,
               d.week_ending_date as wk, sum(s.net_sales_qty) as q
        from reporting.fact_sales_line s
        join reporting.dim_date d on d.date_key = s.date_key
        where s.region_key = any(%(rks)s) and s.date_key between %(lo)s and %(hi)s
          and s.net_sales_qty <> 0 and s.item_key is not null
        group by 1, 2, 3, 4, 5)
    select s.region_key, i.macgear_code, i.description, i.brand, c.customer_name,
           coalesce(nc.customer_group, nc.parent_name, nc.customer_name, 'Other'),
           s.wk, sum(s.q)
    from s
    join reporting.dim_item i on i.item_key = s.item_key
    left join reporting.dim_customer c on c.customer_key = s.customer_key
    left join reporting.dim_ns_customer nc on nc.customer_id = s.customer_id
    group by 1, 2, 3, 4, 5, 6, 7"""

_MG_SQL = """
    select f.region_key, i.macgear_code, sum(f.on_hand), sum(f.available), sum(f.on_order),
           sum(coalesce(f.committed, 0) + coalesce(f.back_ordered, 0)), max(f.date_key)
    from reporting.fact_inventory_daily f
    join reporting.dim_item i on i.item_key = f.item_key
    where f.is_latest and f.stock_class = 'Saleable' and f.region_key = any(%(rks)s)
    group by 1, 2"""

# Placeholder: the table behind this view is provisioned but not loaded yet.
_ETA_SQL = """
    select p.region_key, p.item_code, p.po_number, p.eta, p.inbound_status, p.open_qty, p.loaded_at
    from reporting.fact_open_po p
    where p.region_key = any(%(rks)s) and p.open_qty > 0"""

_NAME_DROP = re.compile(r"\(nz\)|\blimited\b|\bltd\b|\bcomputers\b|[^a-z0-9]")


def norm_name(name: str) -> str:
    """Retailer names as the two sides spell them: MWAVE = Mwave, Scorptec
    Computers = Scorptec, Bunnings (NZ) Limited = Bunnings."""
    return _NAME_DROP.sub("", (name or "").lower())


def _dk(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _from_dk(k: int) -> date:
    return date(k // 10000, k // 100 % 100, k % 100)


def _packed(series: list) -> list | int:
    """Dense run from the first to the last non-zero week: [start, [values]]; 0 if none."""
    nz = [k for k, x in enumerate(series) if x]
    if not nz:
        return 0
    return [nz[0], [round(x) if x == int(x) else round(x, 2) for x in series[nz[0]:nz[-1] + 1]]]


def shape(weeks: list[date], regions: list[str], region_of: dict, st_rows, si_rows=None,
          mg_rows=None, eta_rows=None, eta_loaded=False) -> dict:
    """Raw query rows -> the page's dataset. Pure, so the rules are testable.

    st_rows:  (region_key, code, name, brand, customer, date_key, units, soh, on_order, value)
    si_rows:  (region_key, code, name, brand, retailer or None, ns_group, week date, units)
    mg_rows:  (region_key, code, on_hand, available, on_po, open_orders, date_key)
    eta_rows: (region_key, code, po, eta, status, open_qty, loaded_at)
    """
    wi = {w: k for k, w in enumerate(weeks)}
    n = len(weeks)
    items: dict = {}                     # code -> index
    item_list: list = []
    cust: dict = {}                      # name -> index
    cust_list: list = []
    reporting = defaultdict(set)         # region -> retailers that send sell-through
    rows: dict = {}                      # (region, item, cust) -> row

    def item(code, name, brand):
        if code not in items:
            items[code] = len(item_list)
            item_list.append([code, name or code, brand or UNMATCHED])
        return items[code]

    def customer(name):
        if name not in cust:
            cust[name] = len(cust_list)
            cust_list.append({"name": name, "reports": []})
        return cust[name]

    def row(rg, i, c):
        key = (rg, i, c)
        if key not in rows:
            rows[key] = {"t": [0] * n, "n": [0] * n, "s": None, "o": None, "v": None}
        return rows[key]

    hi = _dk(weeks[-1]) if weeks else None
    for rk, code, name, brand, cname, dk, u, s, o, v in st_rows:
        rg = region_of.get(rk)
        k = wi.get(_from_dk(dk))
        if rg is None or k is None:
            continue
        reporting[rg].add(cname)
        r = row(rg, item(code, name, brand), customer(cname))
        r["t"][k] += int(u or 0)
        if dk == hi:
            r["s"] = None if s is None else int(s)
            r["o"] = None if o is None else int(o)
            r["v"] = None if v is None else round(float(v), 2)

    if si_rows is not None:
        by_norm = {rg: {norm_name(c): c for c in names} for rg, names in reporting.items()}
        for rk, code, name, brand, cname, group, wk, q in si_rows:
            rg = region_of.get(rk)
            k = wi.get(wk)
            if rg is None or k is None:
                continue
            target = cname if cname in reporting[rg] else by_norm.get(rg, {}).get(norm_name(group))
            label = target or cname or group
            r = row(rg, item(code, name, brand), customer(label))
            r["n"][k] += float(q or 0)

    for c in cust_list:
        c["reports"] = [rg for rg in regions if c["name"] in reporting[rg]]

    out_rows = []
    for (rg, i, c), r in rows.items():
        t, si = _packed(r["t"]), _packed(r["n"])
        if not t and not si and not r["s"]:
            continue
        out_rows.append([regions.index(rg), i, c, t, si, r["s"], r["o"], r["v"]])

    mg = None
    if mg_rows is not None:
        mg = {rg: {} for rg in regions}
        asat = None
        for rk, code, oh, av, po, oo, dk in mg_rows:
            rg = region_of.get(rk)
            if rg is None or code not in items:
                continue
            mg[rg][items[code]] = [int(oh or 0), int(av or 0), int(po or 0), int(oo or 0)]
            asat = max(asat or dk, dk)
        mg = {"by_region": mg, "as_at": _from_dk(asat).isoformat() if asat else None}

    eta = None
    if eta_rows is not None or eta_loaded:
        lines = {rg: defaultdict(list) for rg in regions}
        loaded = None
        for rk, code, po, when, status, qty, loaded_at in eta_rows or []:
            rg = region_of.get(rk)
            if rg is None or code not in items:
                continue
            lines[rg][items[code]].append([po, when.isoformat() if when else None, status,
                                           round(float(qty))])
            if loaded_at is not None:
                loaded = max(loaded or loaded_at, loaded_at)
        for rg in lines:
            for v in lines[rg].values():
                v.sort(key=lambda x: (x[1] is None, x[1] or ""))
        eta = {"loaded": eta_loaded, "by_region": {rg: dict(v) for rg, v in lines.items()},
               "as_at": loaded.isoformat() if loaded else None}

    return {
        "weeks": [w.isoformat() for w in weeks],
        "regions": regions,
        "currency": {rg: CURRENCY.get(rg, rg) for rg in regions},
        "items": item_list,
        "customers": cust_list,
        "rows": out_rows,
        "mg": mg,
        "eta": eta,
        "sell_in": si_rows is not None,
        "built": date.today().isoformat(),
    }


def build(brands: list[str] | None, regions: list[str], sell_in: bool) -> dict:
    weeks = warehouse.recent_weeks(WEEKS)
    region_of = {warehouse.region_key(rg): rg for rg in regions}
    if not weeks or not regions or brands == []:
        return shape(weeks, regions, region_of, [])
    p = {"rks": list(region_of), "brands": brands,
         "lo": _dk(weeks[0]), "hi": _dk(weeks[-1])}
    si_rows = mg_rows = eta_rows = None
    eta_loaded = False
    with warehouse.connect() as conn, conn.cursor() as cur:
        cur.execute(_ST_SQL, p)
        st_rows = cur.fetchall()
        if sell_in:
            cur.execute(_SI_SQL, {**p, "lo": _dk(weeks[0] - timedelta(days=6))})
            si_rows = [r for r in cur.fetchall() if brands is None or r[3] in brands]
            cur.execute(_MG_SQL, p)
            mg_rows = cur.fetchall()
            try:
                cur.execute(_ETA_SQL, p)
                eta_rows, eta_loaded = cur.fetchall(), True
            except psycopg.errors.UndefinedTable:
                conn.rollback()          # not provisioned yet: the page says so
    out = shape(weeks, regions, region_of, st_rows, si_rows, mg_rows, eta_rows, eta_loaded)
    out["store_retailers"] = {rg: store_retailers(brands, rg) for rg in regions}
    return out


# ---- store level ----------------------------------------------------------------
# Only some retailers send stores (HN, Bunnings, Bing Lee, Retravision AU; JB HI-FI,
# PB Tech, HN, Bunnings NZ). HN AU alone is ~200 stores x ~25k store-product lines,
# so stores load on demand: one retailer's stores, or one product's stores.
_STORE_SQL = """
    with f as (
        select f.customer_key, f.store_name, f.item_key, f.date_key,
               sum(f.qty_sold) as u,
               sum(f.soh) filter (where f.date_key = %(hi)s) as s
        from reporting.fact_sell_through f
        where f.region_key = %(rk)s and f.date_key between %(lo)s and %(hi)s
          and f.store_name is not null
          {where}
        group by 1, 2, 3, 4)
    select c.customer_name, f.store_name, i.macgear_code, f.date_key, f.u, f.s
    from f
    join reporting.dim_item     i on i.item_key     = f.item_key
    join reporting.dim_customer c on c.customer_key = f.customer_key
    where (%(brands)s::text[] is null or i.brand = any(%(brands)s::text[]))"""


def store_retailers(brands, region: str) -> list[str]:
    """Retailers that send store-level rows in this region (last 104 weeks)."""
    def build():
        weeks = warehouse.recent_weeks(WEEKS)
        if not weeks:
            return []
        return [r[0] for r in warehouse.query("""
            select c.customer_name from reporting.dim_customer c
            where c.customer_key in (select distinct f.customer_key from reporting.fact_sell_through f
                                     where f.region_key = %(rk)s and f.date_key >= %(lo)s
                                       and f.store_name is not null)
            order by 1""", {"rk": warehouse.region_key(region), "lo": _dk(weeks[0])})]
    return warehouse.cached(("st_weekly_sr", region), build, ttl=warehouse.SELL_THROUGH_TTL)


def shape_stores(weeks: list[date], rows) -> dict:
    """rows: (retailer, store, code, date_key, units, soh). One row per retailer x
    store x product: [store index, code, packed weekly units, this week's SOH]."""
    wi = {w: k for k, w in enumerate(weeks)}
    hi = _dk(weeks[-1]) if weeks else None
    stores: dict = {}
    lines: dict = {}
    for retailer, store, code, dk, u, s in rows:
        k = wi.get(_from_dk(dk))
        if k is None:
            continue
        si = stores.setdefault((retailer, store), len(stores))
        ln = lines.setdefault((si, code), {"t": [0] * len(weeks), "s": None})
        ln["t"][k] += int(u or 0)
        if dk == hi and s is not None:
            ln["s"] = int(s)
    out = []
    for (si, code), ln in lines.items():
        t = _packed(ln["t"])
        if t or ln["s"]:
            out.append([si, code, t, ln["s"]])
    return {"weeks": [w.isoformat() for w in weeks],
            "stores": [[r, s] for (r, s), _ in sorted(stores.items(), key=lambda kv: kv[1])],
            "rows": out}


def build_stores(brands, region: str, retailer: str = "", code: str = "") -> dict:
    """One retailer's stores (every product), or one product's stores (every retailer)."""
    weeks = warehouse.recent_weeks(WEEKS)
    if not weeks or brands == [] or not (retailer or code):
        return shape_stores(weeks, [])
    where, p = [], {"rk": warehouse.region_key(region), "brands": brands,
                    "lo": _dk(weeks[0]), "hi": _dk(weeks[-1])}
    if retailer:
        where.append("and f.customer_key in (select customer_key from reporting.dim_customer"
                     " where customer_name = %(retailer)s)")
        p["retailer"] = retailer
    if code:
        where.append("and f.item_key in (select item_key from reporting.dim_item"
                     " where macgear_code = %(code)s)")
        p["code"] = code
    rows = warehouse.query(_STORE_SQL.format(where="\n          ".join(where)), p)
    return shape_stores(weeks, rows)


def stores_for(brands, region: str, retailer: str = "", code: str = "") -> dict:
    key = ("st_weekly_st", None if brands is None else tuple(sorted(brands)), region, retailer, code)
    return warehouse.cached(key, lambda: build_stores(brands, region, retailer, code),
                            ttl=warehouse.SELL_THROUGH_TTL)


def for_user(brands: list[str] | None, regions: list[str], sell_in: bool) -> dict:
    key = ("st_weekly", None if brands is None else tuple(sorted(brands)), tuple(regions), sell_in)
    return warehouse.cached(key, lambda: build(brands, regions, sell_in), ttl=warehouse.CACHE_TTL_SEC)
