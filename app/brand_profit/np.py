"""Net profit for the rest of the site — the Brand Profitability method applied to every sales
line, so the home page, Sales Explorer and Item Explorer show NET PROFIT instead of NetSuite's
estimated GP (user, 2026-11-17: "the GP reporting on the rest of the site should come from here").

For each NetSuite sales line (the site's revenue definition: account 54, intercompany excluded):

  deductions = customer rebate $ + MDF $                    (calc.Rates, per line, exactly as the
                                                             Brand Profitability report)
             + the line's share of its brand's freight + vendor rebates for the month,
               shared by gross sales                        (user, 2026-11-17: allocate by gross
                                                             share so every view adds up)
  net profit = revenue - estimated cost - deductions

Callers add the deductions to their cost column, so their existing "revenue - cost" code yields
net profit. Grain held: date_key x customer x brand_id x item_key (≈ line level; AU FY27 ~0.5M rows),
one cached build per region x financial year, expired by the sales watch (prefix "bp_np") and
whenever a setting changes (routes.py).

Freight / vendor rebates a brand-month can't take (no positive sales that month, or GL lines with no
brand) are "unallocated": they count in region totals (see `unallocated`) but sit on no customer,
brand-of-sales or item row.

Differences from the Brand Profitability report, by design: that report also counts the other
income lines the CYA carried (freight charged to customers, short supply & damage, 3PL income,
courier claims) and their rebates; the site's revenue is account 54 only, so its NP is lower or
higher by those lines' net amount.

Months before the settings start (Apr 2026) have no deductions, so NP there = estimated GP.
"""
from array import array
from bisect import bisect_left, bisect_right
from datetime import date

from .. import warehouse
from . import calc, data, db

NP_TTL = 30 * 60
# A finished financial year barely changes, so it isn't rebuilt on every NetSuite load (each AU year
# re-reads ~4 s of sales lines on the cluster; user's CPU alerts, 2026-11-27): the current year is
# "bp_np" and goes with every sales load (SALES_DROP); earlier years are "bp_closed", kept a day.
# Settings changes drop both (CACHE_PREFIXES).
CLOSED_TTL = 24 * 60 * 60
SALES_DROP = ("bp_np",)
CACHE_PREFIXES = ("bp_np", "bp_closed")


def _current_fy() -> int:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    t = datetime.now(ZoneInfo("Australia/Sydney")).date()
    return t.year + 1 if t.month >= 4 else t.year


class Fine:
    """Columnar, sorted by date_key: k date_key, c customer_id, b brand_id, i item_key (-1 = None),
    g revenue, d all deductions, r the customer-rebate part of d (net sales = revenue - r).
    unalloc: {yyyymm: amount}."""
    __slots__ = ("k", "c", "b", "i", "g", "d", "r", "unalloc", "configured", "gl_months")

    def __init__(self):
        self.k, self.c, self.b, self.i = array("i"), array("i"), array("i"), array("i")
        self.g, self.d, self.r = array("d"), array("d"), array("d")
        self.unalloc: dict = {}
        self.configured = True
        self.gl_months: set = set()        # yyyymm with freight / vendor-rebate lines loaded

    def span(self, lo: int, hi: int) -> range:
        return range(bisect_left(self.k, lo), bisect_right(self.k, hi))


def _build(region: str, fy: int) -> Fine:
    out = Fine()
    try:
        with db.connect() as conn:
            rates = calc.Rates(db.load_rules(conn, region), db.load_pms(conn, region))
            gl, _ = data.gl_lines(conn, region, fy)
    except db.NotConfigured:
        out.configured = False
        return out
    first = calc.fy_months(fy)[0]
    lo, hi = int(first.strftime("%Y%m%d")), int(date(fy, 3, 31).strftime("%Y%m%d"))
    rows = warehouse.query("""
        select f.date_key, f.customer_id, f.brand_id, f.item_key, c.parent_id,
               upper(coalesce(nullif(b.brand, ''), i.brand, '')), coalesce(i.category, ''),
               upper(coalesce(i.macgear_code, '')), sum(f.net_sales)
        from reporting.fact_sales_line f
        left join reporting.dim_item i on i.item_key = f.item_key
        left join reporting.dim_brand b on b.brand_id = f.brand_id
        left join reporting.dim_ns_customer c on c.customer_id = f.customer_id
        where f.subsidiary_id = %s and f.date_key between %s and %s
          and f.account_id = 54 and f.customer_id <> all(%s)
        group by 1, 2, 3, 4, 5, 6, 7, 8
        order by 1""", (data.SUBSIDIARY[region], lo, hi, list(data.INTERCOMPANY)))
    # brand-month revenue, to share freight / vendor rebates by
    gross_bm: dict = {}
    for k, _c, _b, _i, _p, br, _cat, _it, g in rows:
        key = (k // 100, br)
        gross_bm[key] = gross_bm.get(key, 0.0) + float(g or 0)
    gl_bm: dict = {}
    out.gl_months = {g["month"].year * 100 + g["month"].month for g in gl}
    for g in gl:
        key = (g["month"].year * 100 + g["month"].month, calc.norm_brand(g["brand"]))
        gl_bm[key] = gl_bm.get(key, 0.0) + g["amount"]
    factor = {}
    for key, amt in gl_bm.items():
        base = gross_bm.get(key, 0.0)
        if base > 0 and key[1]:
            factor[key] = amt / base
        else:
            out.unalloc[key[0]] = out.unalloc.get(key[0], 0.0) + amt
    month_of = {}
    for k, c, b, it, parent, br, cat, item, g in rows:
        g = float(g or 0)
        ym = k // 100
        m = month_of.get(ym)
        if m is None:
            m = month_of[ym] = date(ym // 100, ym % 100, 1)
        reb = rates.rebate_pct(m, parent, c, br, cat, item) * g
        ded = reb + rates.mdf_pct(m, parent, br) * g + g * factor.get((ym, br), 0.0)
        out.k.append(k); out.c.append(-1 if c is None else c); out.b.append(-1 if b is None else b)
        out.i.append(-1 if it is None else it); out.g.append(g); out.d.append(ded); out.r.append(reb)
    return out


def fine(region: str, fy: int) -> Fine:
    if fy < _current_fy():
        return warehouse.cached(("bp_closed", region, fy), lambda: _build(region, fy), ttl=CLOSED_TTL)
    return warehouse.cached(("bp_np", region, fy), lambda: _build(region, fy), ttl=NP_TTL)


def _fys(lo_key: int, hi_key: int) -> list[int]:
    a = date(lo_key // 10000, lo_key // 100 % 100, 1)
    b = date(hi_key // 10000, hi_key // 100 % 100, 1)
    return list(range(calc.fy_of(a), calc.fy_of(b) + 1))


FIRST_FY = 2027          # settings start Apr 2026 (FY27); earlier years have no deductions


def deductions(region: str, lo_key: int, hi_key: int, keyfn) -> dict:
    """{keyfn(k, c, b, i): [deductions, of which rebates]} over date_keys lo..hi (inclusive)."""
    out: dict = {}
    lo_key = max(lo_key, FIRST_FY * 10000 - 10000 + 401)
    if lo_key > hi_key:
        return out
    for fy in _fys(lo_key, hi_key):
        f = fine(region, fy)
        k, c, b, i, d, r = f.k, f.c, f.b, f.i, f.d, f.r
        for n in f.span(lo_key, hi_key):
            key = keyfn(k[n], c[n], b[n], i[n])
            cell = out.get(key)
            if cell is None:
                cell = out[key] = [0.0, 0.0]
            cell[0] += d[n]
            cell[1] += r[n]
    return out


def unallocated(region: str, lo_key: int, hi_key: int) -> dict:
    """{yyyymm: freight + vendor rebates no sales line could take} for whole months in range."""
    out: dict = {}
    lo_key = max(lo_key, FIRST_FY * 10000 - 10000 + 401)
    if lo_key > hi_key:
        return out
    for fy in _fys(lo_key, hi_key):
        for ym, amt in fine(region, fy).unalloc.items():
            if lo_key // 100 <= ym <= hi_key // 100 and amt:
                out[ym] = out.get(ym, 0.0) + amt
    return out


def gl_missing(region: str, d: date) -> bool:
    """True when d's month has no freight / vendor-rebate lines loaded (its NP leaves them out)."""
    if calc.fy_of(date(d.year, d.month, 1)) < FIRST_FY:
        return False
    f = fine(region, calc.fy_of(date(d.year, d.month, 1)))
    return f.configured and (d.year * 100 + d.month) not in f.gl_months


def configured(region: str = "AU") -> bool:
    return fine(region, FIRST_FY).configured


def adjust_cube_rows(region: str, rows, monthly: bool) -> list:
    """Sales Explorer cube rows (k, customer, brand_id, rep, revenue, est_cost, qty), k = yyyymm
    (monthly) or yyyymmdd -> the same rows plus an 8th value, rebates: the deductions are added to
    est_cost, so revenue - cost = net profit, and revenue - rebates = net sales.
    Unallocated freight / vendor rebates become a row with no customer or brand (on the 1st of the
    month in the daily cube) so period totals match the brand totals."""
    rows = list(rows)
    if not rows:
        return []
    ks = [r[0] for r in rows]
    lo, hi = min(ks), max(ks)
    if monthly:
        lo, hi = lo * 100 + 1, hi * 100 + 31
        ded = deductions(region, lo, hi, lambda k, c, b, i: (k // 100, c, b))
    else:
        ded = deductions(region, lo, hi, lambda k, c, b, i: (k, c, b))
    out = []
    for r in rows:
        k, c, b = r[0], (-1 if r[1] is None else r[1]), (-1 if r[2] is None else r[2])
        extra, reb = ded.pop((k, c, b), (0.0, 0.0))
        out.append((r[0], r[1], r[2], r[3], r[4], float(r[5] or 0) + extra, r[6], reb))
    for (k, c, b), (extra, reb) in ded.items():   # a line the cube didn't carry (shouldn't happen)
        out.append((k, None if c == -1 else c, None if b == -1 else b, None, 0.0, extra, 0.0, reb))
    for ym, amt in unallocated(region, lo, hi).items():
        k = ym if monthly else ym * 100 + 1
        if monthly or lo <= k <= hi:
            out.append((k, None, None, None, 0.0, amt, 0.0, 0.0))
    out.sort(key=lambda r: r[0])
    return out
