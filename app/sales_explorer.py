"""Sales Explorer — Macgear's internal NetSuite revenue, sliced however the user
likes: period, filters, and a matrix whose rows they build from the dimensions.
Staff with the `sell_in_value` capability only; margin columns need `margin`.

MARGIN = NET PROFIT (2026-11-17, user): the Brand Profitability method per sales line
(brand_profit/np.py) — customer rebates, MDF and each brand's freight / vendor rebates shared by
gross — is added to the est. cost column as the cubes are built, so "revenue - cost" is net profit;
a 4th column carries the rebates so net sales (revenue - rebates) gives NP % on net.

Definitions are the home page's (app/sales.py): revenue = net_sales (account 54,
intercompany excluded, ex-GST, subsidiary currency); periods on Macgear's FINANCIAL
year (April–March, FY27 = Apr 2026 – Mar 2027); "LY" = the same dates a year earlier;
a period that hasn't finished compares to-date with to-date. AU and NZ never summed.

ACCOUNT MANAGER = the CURRENT account rep on the customer record (NetSuite customer
salesrep, dim_ns_customer.account_rep_id), applied to ALL history — so a manager's
numbers are their whole portfolio, including sales made before they took an account
over (user, 2026-10-15). It is NOT the rep on each invoice. Since 2026-11-24 a manager
set on the Portfolios page for the retailer (and brand) wins over NetSuite's
(portfolio.py), so the manager is per customer x brand: _am(c, b, nm). A Portfolios row
can name an item CATEGORY within a brand (AU The Good Guys EUFY: Security / Appliances /
Baby): those customer x brand pairs' summary rows are split by category, the category id
riding in the rep column (portfolio.split_rows), and _am(c, b, nm, r) resolves it. Their
targets, set by retailer x brand only, are split by the last 12 months' category mix.

PRODUCT MANAGER (user, 2026-11-25) = the brand's PM in force NOW (Brand Profitability
settings → PMs, per region), applied to all history like the account manager — a PM's
whole portfolio with a fair LY. A brand with no PM this month falls back to its latest
PM row. The filter also takes "team:<senior PM>": the senior's brands plus those of the
PMs reporting to them this month (Portfolios). Brands with no PM row at all = "(no PM)".

Speed. Querying the warehouse per click took ~6 s, so each region's sales are held in
memory as a compact summary and sliced in Python:
  - monthly: month x customer x brand x rep, all history   (AU ~190k rows)
  - daily:   day x customer x brand x rep, for the current and previous month and the
             same span a year earlier                      (AU ~40k rows)
A date range is answered from whole months where it covers them and from days for the
partial months at its edges. Both are cached and refreshed in the background
(warehouse.cached). ITEM is the one dimension not in the summary (it would make it
~1M rows): when the matrix uses Item, that query goes to the warehouse (0.2–2 s,
cached).
"""
import calendar
from array import array
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from io import BytesIO

from . import portfolio, sales, warehouse
from .brand_profit import np as bp_np

MONTHLY_TTL = 30 * 60          # closed months barely move
DAILY_TTL = 5 * 60             # today's invoices

DIMS = {                       # key -> label (order = the builder's chips)
    "retailer": "Retailer", "account": "Account", "brand": "Brand",
    "pm": "Product manager", "rep": "Account manager", "item": "Item", "month": "Month",
}
NO_PM = "(no PM)"
MAX_LEVELS = 3
FY_MONTHS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]


def _key(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


# ---- dimension names (small, cached) ---------------------------------------------
def names() -> dict:
    def build():
        cust, brand, rep = {}, {}, {}
        with warehouse.connect() as conn, conn.cursor() as cur:
            cur.execute("""select customer_id, customer_name,
                                  coalesce(customer_group, parent_name, customer_name),
                                  account_rep_id
                           from reporting.dim_ns_customer""")
            for cid, nm, grp, am in cur.fetchall():
                cust[cid] = (nm or str(cid), grp or nm or str(cid), am)
            cur.execute("select brand_id, brand from reporting.dim_brand")
            brand = {b: n for b, n in cur.fetchall()}
            cur.execute("select rep_id, rep_name from reporting.dim_salesrep")
            rep = {r: n for r, n in cur.fetchall()}
        # account managers set on the Portfolios page override NetSuite's (portfolio.py):
        # an every-brand row replaces the customer's manager, a brand row goes in am_cb
        amap = portfolio.am_map()
        rep_id = {n: i for i, n in sorted(rep.items(), reverse=True)}

        def rid(name):
            if name is None:
                return None
            if name not in rep_id:                    # not a NetSuite rep: give it its own id
                rep_id[name] = -len(rep_id) - 1
                rep[rep_id[name]] = name
            return rep_id[name]
        for cid, (nm, grp, am) in list(cust.items()):
            if cid in amap["cust"] and amap["cust"][cid] != rep.get(am):
                cust[cid] = (nm, grp, rid(amap["cust"][cid]))
        am_cb = {k: rid(n) for k, n in amap["cb"].items()}
        am_cbk = {k: rid(n) for k, n in amap.get("cbk", {}).items()}
        am_rb = {k: rid(n) for k, n in amap["rb"].items()}
        am_rbk = {k: rid(n) for k, n in amap.get("rbk", {}).items()}
        am_ra = {k: rid(n) for k, n in amap["ra"].items()}
        return {"cust": cust, "brand": brand, "rep": rep, "am_cb": am_cb, "am_cbk": am_cbk,
                "am_rb": am_rb, "am_rbk": am_rbk, "am_ra": am_ra}
    return warehouse.cached(("sx_names",), build, ttl=60 * 60)


def pm_by_brand_name(region: str, today: date | None = None) -> dict:
    """{BRAND NAME: PM name} for the region: the brand_pm row in force this month, else the
    brand's latest row. By name so brands with a target but no sales yet keep their PM.
    Settings changes drop it (bp_np prefix, brand_profit/routes.py)."""
    t = today or date.today()
    m0 = date(t.year, t.month, 1)

    def build():
        from .brand_profit import db as bp_db
        try:
            with bp_db.connect() as conn:
                rows = bp_db.load_pms(conn, region)
        except Exception:                       # settings DB not there: no PMs, not an error
            return {}
        best: dict = {}
        for r in rows:
            cur = r["valid_from"] <= m0 and (r["valid_to"] is None or r["valid_to"] >= m0)
            key = (cur, r["valid_from"])
            b = portfolio.calc.norm_brand(r["brand"])
            if b not in best or key > best[b][0]:
                best[b] = (key, r["pm"])
        return {b: v[1] for b, v in best.items()}
    return warehouse.cached(("bp_np_sxpm", region, m0), build, ttl=10 * 60)


def pm_brands(region: str, today: date | None = None) -> dict:
    """{brand_id: PM name} for the region (pm_by_brand_name over the warehouse's brands)."""
    by_name = pm_by_brand_name(region, today)
    out = {}
    for bid, bn in (names().get("brand") or {}).items():
        pm = by_name.get(portfolio.calc.norm_brand(bn))
        if pm:
            out[bid] = pm
    return out


def pm_teams(today: date | None = None) -> dict:
    """{senior PM: {members}} in force this month (Portfolios)."""
    t = today or date.today()
    m0 = date(t.year, t.month, 1)
    rows = portfolio.rows()["team"]
    out: dict = {}
    for s_ in portfolio.seniors(rows, [m0]):
        out[s_] = {r["pm"] for r in rows if r["reports_to"] == s_ and portfolio._in_force(r, m0)}
    return out


def pm_match(sel: str, pm: str | None, teams: dict) -> bool:
    """Does a brand's PM pass the PM filter? sel = a PM's name, "team:<senior>" or NO_PM."""
    if sel == NO_PM:
        return not pm
    if sel.startswith("team:"):
        senior = sel[5:]
        return bool(pm) and (pm == senior or pm in teams.get(senior, ()))
    return pm == sel


def with_pms(nm: dict, region: str, f: dict | None = None, today: date | None = None) -> dict:
    """names() plus this region's brand -> PM (pm_b) and, when filtering by PM, the brand
    ids that pass (pm_bids). A shallow copy: names() itself stays region-free."""
    out = dict(nm)
    out["pm_b"] = pm_brands(region, today)
    out["pm_n"] = pm_by_brand_name(region, today)
    if f and f.get("pm"):
        teams = pm_teams(today)
        out["pm_bids"] = {b for b in nm["brand"] if pm_match(f["pm"], out["pm_b"].get(b), teams)}
        out["pm_teams"] = teams
    return out


# ---- the in-memory summary --------------------------------------------------------
class Cube:
    """Columnar rows: period key (yyyymm or yyyymmdd), customer_id, brand_id, rep_id,
    revenue, cost (est. cost + net-profit deductions), units, customer rebates. Ids of None are
    stored as -1. Rows arrive SORTED by the period key, so a date range is a bisect slice."""
    __slots__ = ("k", "c", "b", "r", "v", "x", "q", "n")

    def __init__(self, rows):
        self.k, self.c, self.b, self.r = array("i"), array("i"), array("i"), array("i")
        self.v, self.x, self.q, self.n = array("d"), array("d"), array("d"), array("d")
        for row in rows:
            k, c, b, r, v, x, q = row[:7]
            self.k.append(k); self.c.append(-1 if c is None else c)
            self.b.append(-1 if b is None else b); self.r.append(-1 if r is None else r)
            self.v.append(float(v or 0)); self.x.append(float(x or 0)); self.q.append(float(q or 0))
            self.n.append(float(row[7]) if len(row) > 7 else 0.0)

    def __len__(self):
        return len(self.k)


def _monthly(region: str) -> Cube:
    rk = warehouse.region_key(region)
    # No rep column: the account manager is a property of the customer (see top), so
    # it's looked up per customer at slice time. Dropping it also shrinks the summary.
    rows = warehouse.query_summary("agg_sales_month", """
        select s.month_key, s.customer_id, s.brand_id, null::int,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.agg_sales_month s
        where s.region_key = %(rk)s and s.n_net > 0
        group by 1, 2, 3 order by 1""", """
        select s.date_key / 100, s.customer_id, s.brand_id, null::int,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where s.region_key = %(rk)s and s.net_sales is not null
        group by 1, 2, 3 order by 1""", {"rk": rk})
    return Cube(_split(region, bp_np.adjust_cube_rows(region, rows, monthly=True), "s.date_key / 100", "", {}))


def daily_window(today: date) -> tuple[date, date, date, date]:
    """(this_lo, this_hi, ly_lo, ly_hi) held at day grain: 1st of last month -> today,
    and the same span a year earlier extended to that month's end."""
    lo = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    ly_lo, ly_today = sales.last_year(lo), sales.last_year(today)
    return lo, today, ly_lo, _month_end(ly_today.year, ly_today.month)


def _daily(region: str, today: date) -> Cube:
    rk = warehouse.region_key(region)
    lo, hi, ly_lo, ly_hi = daily_window(today)
    rows = warehouse.query_summary("agg_sales_day", """
        select s.date_key, s.customer_id, s.brand_id, null::int,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.agg_sales_day s
        where s.region_key = %(rk)s and s.n_net > 0
          and (s.date_key between %(lo)s and %(hi)s or s.date_key between %(llo)s and %(lhi)s)
        group by 1, 2, 3 order by 1""", """
        select s.date_key, s.customer_id, s.brand_id, null::int,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where s.region_key = %(rk)s and s.net_sales is not null
          and (s.date_key between %(lo)s and %(hi)s or s.date_key between %(llo)s and %(lhi)s)
        group by 1, 2, 3 order by 1""",
        {"rk": rk, "lo": _key(lo), "hi": _key(hi), "llo": _key(ly_lo), "lhi": _key(ly_hi)})
    return Cube(_split(region, bp_np.adjust_cube_rows(region, rows, monthly=False), "s.date_key",
                       "and (s.date_key between %(lo)s and %(hi)s or s.date_key between %(llo)s and %(lhi)s)",
                       {"lo": _key(lo), "hi": _key(hi), "llo": _key(ly_lo), "lhi": _key(ly_hi)}))


def _split(region: str, rows: list, kcol: str, extra_where: str, prm: dict) -> list:
    """Split the rows of customer x brand pairs a Portfolios category row covers by item
    category (portfolio.split_rows). Filtered on customer only (see _items_sql on brand)."""
    pairs = portfolio.category_pairs(region)
    if not pairs:
        return rows
    fine = warehouse.query(f"""
        select {kcol}, s.customer_id, s.brand_id, i.category,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        left join reporting.dim_item i on i.item_key = s.item_key
        where s.region_key = %(rk)s and s.net_sales is not null and s.customer_id = any(%(c)s) {extra_where}
        group by 1, 2, 3, 4""", {"rk": warehouse.region_key(region), "c": sorted({c for c, _ in pairs}), **prm})
    fine = [(k, c, b, portfolio.category_id(cat), v, x, q) for k, c, b, cat, v, x, q in fine if (c, b) in pairs]
    return portfolio.split_rows(rows, pairs, fine)


def cubes(region: str, today: date) -> tuple[Cube, Cube]:
    m = warehouse.cached(("sx_month", region), lambda: _monthly(region), ttl=MONTHLY_TTL)
    d = warehouse.cached(("sx_day", region, today), lambda: _daily(region, today), ttl=DAILY_TTL)
    return m, d


def _parts(a: date, b: date) -> list[tuple[str, int, int]]:
    """Split [a, b] into ("m", yyyymm, yyyymm) for whole months and ("d", lo, hi) day
    spans for partial months at the edges."""
    out, cur = [], a.replace(day=1)
    while cur <= b:
        me = _month_end(cur.year, cur.month)
        lo, hi = max(a, cur), min(b, me)
        ym = cur.year * 100 + cur.month
        if lo == cur and hi == me:
            if out and out[-1][0] == "m" and out[-1][2] == _prev_ym(ym):
                out[-1] = ("m", out[-1][1], ym)
            else:
                out.append(("m", ym, ym))
        else:
            out.append(("d", _key(lo), _key(hi)))
        cur = me + timedelta(days=1)
    return out


def _prev_ym(ym: int) -> int:
    y, m = divmod(ym, 100)
    return (y - 1) * 100 + 12 if m == 1 else ym - 1


# ---- periods ------------------------------------------------------------------------
def period(code: str, fy: int, today: date) -> dict:
    """code: today | yesterday | week | mtd | qtd | ytd | fy | q1..q4 | m1..m12 (fiscal
    month, Apr = 1). fy = the FY's end year (2027 = FY27). A period reaching past today
    is cut at today, and LY is cut at the same date."""
    fy0 = date(fy - 1, 4, 1)
    if code == "today":
        a = b = today
        label = "Today"
    elif code == "yesterday":
        a = b = today - timedelta(days=1)
        label = "Yesterday"
    elif code == "week":
        a, b = today - timedelta(days=today.weekday()), today
        label = "This week"
    elif code == "mtd":
        a, b = today.replace(day=1), today
        label = today.strftime("%B") + " to date"
    elif code == "qtd":
        a, b = sales.fq_start(today), today
        label = f"{sales.fq_label(today)} to date"
    elif code == "ytd":
        a, b = sales.fy_start(today), today
        label = f"{sales.fy_label(today)} to date"
    elif code.startswith("q") and code[1:].isdigit() and 1 <= int(code[1:]) <= 4:
        qi = int(code[1:])
        a = date(fy0.year + (1 if qi == 4 else 0), [4, 7, 10, 1][qi - 1], 1)
        e = a.replace(day=28) + timedelta(days=70)
        b = (e.replace(day=1) - timedelta(days=1))
        label = f"FY{fy % 100:02d} Q{qi}"
    elif code.startswith("m") and code[1:].isdigit() and 1 <= int(code[1:]) <= 12:
        mi = int(code[1:])
        mo = (mi + 2) % 12 + 1
        y = fy0.year + (1 if mo < 4 else 0)
        a, b = date(y, mo, 1), _month_end(y, mo)
        label = f"{FY_MONTHS[mi - 1]} {y}"
    else:                                          # "fy"
        a, b = fy0, date(fy, 3, 31)
        label = f"FY{fy % 100:02d}"
    if a > today:
        return {"label": label, "a": a, "b": a - timedelta(days=1), "end": b, "future": True}
    partial = b > today
    end, b = b, min(b, today)
    return {"label": label + (" (to date)" if partial and code not in
                              ("mtd", "qtd", "ytd", "week", "today") else ""),
            "a": a, "b": b, "end": end, "ly_a": sales.last_year(a), "ly_b": sales.last_year(b),
            "future": False}


# ---- targets ------------------------------------------------------------------------
# The official targets committed in the Targets tool (sales.target_lines), set per
# country x brand x retailer x month. A retailer's target belongs to its ACCOUNT MANAGER
# in NetSuite (retailer_ams: the manager on the customer records, user 2026-10-23 —
# NetSuite is the source of truth, not a manager typed on the target; since 2026-11-24 a
# Portfolios row for the retailer x brand or the retailer wins over it), so targets sit on
# Retailer, Brand, Month and Account manager rows and follow the manager filter; only
# Account and Item rows have no target. A month the period covers only part of counts
# in proportion to its weekdays — the home page's pace rule — so "to date" compares
# like with like.
TARGET_DIMS = ("retailer", "brand", "month", "rep", "pm")


def _target_frac(ms: date, a: date, b: date) -> float:
    me = _month_end(ms.year, ms.month)
    lo, hi = max(a, ms), min(b, me)
    if lo > hi:
        return 0.0
    if lo == ms and hi == me:
        return 1.0
    return sales.weekdays(lo, hi) / (sales.weekdays(ms, me) or 1)


def target_lines() -> dict:
    return warehouse.cached(("sales_targets_lines",), sales.target_lines)


def retailer_ams(region: str, today: date) -> dict:
    """{retailer: account manager id} from NetSuite's customer records. Where a retailer's
    accounts have different managers (NZ JB HIFI), the one with the most sales over this
    and last FY wins; a retailer with no sales then, the manager on most of its accounts."""
    def build():
        nm = names()
        m, _ = cubes(region, today)
        since = (sales.fy_start(today).year - 1) * 100 + 4
        by: dict = {}
        for i in range(bisect_left(m.k, since), len(m)):
            t = nm["cust"].get(m.c[i])
            if t and t[2] is not None:
                g = by.setdefault(t[1], {})
                g[t[2]] = g.get(t[2], 0.0) + m.v[i]
        out = {g: max(v, key=v.get) for g, v in by.items()}
        count: dict = {}
        for _, g, am in nm["cust"].values():
            if am is not None and g not in out:
                c = count.setdefault(g, {})
                c[am] = c.get(am, 0) + 1
        out.update({g: max(c, key=c.get) for g, c in count.items()})
        return out
    return warehouse.cached(("sx_ram", region, today), build, ttl=MONTHLY_TTL)


def category_mix(region: str, today: date) -> dict:
    """{(RETAILER, BRAND): {category_id: share}} over the last 12 months (this month
    included) for the retailer x brands a Portfolios category row covers."""
    def build():
        nm = names()
        if not nm.get("am_rbk"):
            return {}
        m, _ = cubes(region, today)
        since = (today.year - 1) * 100 + today.month + 1
        if today.month == 12:
            since = today.year * 100 + 1
        cr = {c: t[1] for c, t in nm["cust"].items()}
        rows = [(m.c[i], m.b[i], m.r[i], m.v[i]) for i in range(bisect_left(m.k, since), len(m)) if m.r[i] >= 0]
        return portfolio.category_mix(rows, cr, nm["brand"])
    return warehouse.cached(("sx_mix", region, today), build, ttl=MONTHLY_TTL)


def targets(region: str, a: date, b: date, dims: list, f: dict, nm: dict, ams: dict, mix: dict | None = None):
    """(total, {row key: target}) for [a, b], or None when no targets apply here. The
    row dict is None when the rows use a dimension targets aren't set by. ams =
    retailer_ams(); mix = category_mix() — a retailer x brand with Portfolios category rows
    has its target split between the categories' managers by it."""
    lines = target_lines().get(region)
    if not lines:
        return None
    brand = nm["brand"].get(f["brand"]) if f.get("brand") is not None else None
    by_row = {} if all(d in TARGET_DIMS for d in dims) else None
    total = 0.0
    rb, ra, rbk = nm.get("am_rb") or {}, nm.get("am_ra") or {}, nm.get("am_rbk") or {}
    mix = mix or {}
    pm_by_name = nm.get("pm_n") or {}

    def am_of(cust, bn):           # Portfolios retailer x brand, retailer, else NetSuite's
        rk = portfolio.norm_retailer(cust)
        a = rb.get((region, rk, portfolio.calc.norm_brand(bn))) if rb else None
        if a is None and ra:
            a = ra.get((region, rk))
        return a if a is not None else ams.get(cust)
    for ms, bn, cust, amt in lines:
        if (brand is not None and bn != brand) or (f.get("retailer") and cust != f["retailer"]):
            continue
        pm = pm_by_name.get(portfolio.calc.norm_brand(bn))
        if f.get("pm") and not pm_match(f["pm"], pm, nm.get("pm_teams") or {}):
            continue
        fr = _target_frac(ms, a, b)
        if not fr:
            continue
        base = am_of(cust, bn)
        parts = [(base, 1.0)]
        if rbk:
            rk, bk = portfolio.norm_retailer(cust), portfolio.calc.norm_brand(bn)
            sh = mix.get((rk, bk))
            if sh and any((region, rk, bk, k) in rbk for k in sh):
                parts = [(rbk.get((region, rk, bk, k), base), v) for k, v in sh.items()]
        for am, share in parts:
            if f.get("rep") is not None and am != f["rep"]:
                continue
            t = amt * fr * share
            total += t
            if by_row is not None:
                key = tuple({"retailer": cust, "brand": bn, "month": f"{ms.year}-{ms.month:02d}",
                             "rep": nm["rep"].get(am) or "(no account manager)", "pm": pm or NO_PM}[d]
                            for d in dims)
                by_row[key] = by_row.get(key, 0.0) + t
    return total, by_row


# ---- slicing ------------------------------------------------------------------------
def _am(c: int, b: int, nm: dict, r: int = -1):
    """The current account manager id for a customer's sales of a brand (r = item category id,
    -1 none): a Portfolios category row, brand row, else the customer's (Portfolios retailer
    row or NetSuite). None if none has one."""
    if r is not None and r >= 0:
        k = nm.get("am_cbk")
        a = k.get((c, b, r)) if k else None
        if a is not None:
            return a
    cb = nm.get("am_cb")
    a = cb.get((c, b)) if cb else None
    if a is not None:
        return a
    t = nm["cust"].get(c)
    return t[2] if t else None


def _pm_ok(sel: str, b: int, nm: dict) -> bool:
    if sel == NO_PM:
        return b not in (nm.get("pm_b") or {})
    return b in nm.get("pm_bids", ())


def _filter_ok(f: dict, c: int, b: int, r: int, nm: dict) -> bool:
    if f.get("brand") is not None and b != f["brand"]:
        return False
    if f.get("pm") and not _pm_ok(f["pm"], b, nm):
        return False
    if f.get("rep") is not None and _am(c, b, nm, r) != f["rep"]:
        return False
    if f.get("retailer"):
        if nm["cust"].get(c, ("", ""))[1] != f["retailer"]:
            return False
    return True


def _label(dim: str, c: int, b: int, r: int, k: int, nm: dict) -> str:
    if dim == "retailer":
        return nm["cust"].get(c, ("(no customer)", "(no customer)"))[1]
    if dim == "account":
        return nm["cust"].get(c, ("(no customer)", ""))[0]
    if dim == "brand":
        return nm["brand"].get(b) or "(no brand)"
    if dim == "pm":
        return (nm.get("pm_b") or {}).get(b) or NO_PM
    if dim == "rep":
        return nm["rep"].get(_am(c, b, nm, r)) or "(no account manager)"
    if dim == "month":
        ym = k if k < 1000000 else k // 100
        return f"{ym // 100}-{ym % 100:02d}"
    return ""


def _sum_range(m: Cube, d: Cube, a: date, b: date, dims, f, nm, slot: int, acc: dict):
    """Add [a, b] into acc[key][slot] as [rev, cost, qty, rebates]."""
    for kind, lo, hi in _parts(a, b):
        cube = m if kind == "m" else d
        k, c, bb, r, v, x, q, n = cube.k, cube.c, cube.b, cube.r, cube.v, cube.x, cube.q, cube.n
        for i in range(bisect_left(k, lo), bisect_right(k, hi)):
            ki = k[i]
            ci, bi, ri = c[i], bb[i], r[i]
            if f and not _filter_ok(f, ci, bi, ri, nm):
                continue
            key = tuple(_label(dm, ci, bi, ri, ki, nm) for dm in dims)
            cell = acc.get(key)
            if cell is None:
                cell = acc[key] = [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]]
            s = cell[slot]
            s[0] += v[i]; s[1] += x[i]; s[2] += q[i]; s[3] += n[i]


def _items_sql(region: str, p: dict, dims: list, f: dict, nm: dict) -> dict:
    """The matrix with Item in it: straight from the warehouse, grouped by raw ids,
    names attached here. Months for LY land on the CURRENT period's month labels
    (shifted a year) so a Month x Item matrix lines up."""
    rk = warehouse.region_key(region)
    where = ["s.region_key = %(rk)s", "s.net_sales is not null",
             "(s.date_key between %(a)s and %(b)s or s.date_key between %(la)s and %(lb)s)"]
    prm = {"rk": rk, "a": _key(p["a"]), "b": _key(p["b"]), "la": _key(p["ly_a"]), "lb": _key(p["ly_b"])}
    # Rep and retailer filter in SQL (1-2 s). BRAND must not: filtering fact_sales_line on
    # brand_id sends the planner down a path that ran past 80 s (0.13 s without it), so
    # brand is grouped on and filtered here instead.
    if f.get("rep") is not None or f.get("retailer"):
        # a customer can have the manager through one brand only (Portfolios brand rows),
        # so the SQL keeps every customer that might, and the rows are checked below
        via_brand = ({c for (c, _), a in (nm.get("am_cb") or {}).items() if a == f.get("rep")}
                     | {c for (c, _, _), a in (nm.get("am_cbk") or {}).items() if a == f.get("rep")})
        ids = [cid for cid, t in nm["cust"].items()
               if (f.get("rep") is None or t[2] == f["rep"] or cid in via_brand)
               and (not f.get("retailer") or t[1] == f["retailer"])]
        where.append("s.customer_id = any(%(cids)s)"); prm["cids"] = ids
    # Group by only what the rows (or the brand filter) need — every id took ~6 s.
    # The manager depends on customer AND brand (Portfolios), so it needs both.
    by_am = "rep" in dims or f.get("rep") is not None
    need_c = any(x in dims for x in ("retailer", "account", "rep")) or by_am
    need_b = "brand" in dims or "pm" in dims or f.get("brand") is not None or f.get("pm") or by_am
    cols = ["s.item_key",
            "s.date_key / 100" if "month" in dims else "0",
            "s.customer_id" if need_c else "null::int",
            "s.brand_id" if need_b else "null::int",
            "null::int"]
    rows = warehouse.query(f"""
        select {', '.join(cols)}, (s.date_key >= %(a)s),
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where {' and '.join(where)}
        group by 1, 2, 3, 4, 5, 6""", prm)
    # net-profit deductions, grouped exactly like the rows (brand_profit/np.py)
    def dkey(month_dims):
        return lambda k, c, b, i: (i, (k // 100) if month_dims else 0, c if need_c else -1, b if need_b else -1)
    ded = {True: bp_np.deductions(region, prm["a"], prm["b"], dkey("month" in dims)),
           False: bp_np.deductions(region, prm["la"], prm["lb"], dkey("month" in dims))}
    keys = {r[0] for r in rows if r[0] is not None}
    items = dict(((k, (c, d)) for k, c, d in warehouse.query(
        "select item_key, macgear_code, description from reporting.dim_item where item_key = any(%(k)s)",
        {"k": list(keys)}))) if keys else {}
    icat = portfolio.item_categories() if nm.get("am_cbk") else {}
    acc: dict = {}
    for ik, ym, c, b, r, is_cur, v, x, q in rows:
        c = -1 if c is None else c
        b = -1 if b is None else b
        r = icat.get(ik, -1)                      # item category, for Portfolios category rows
        dx, dn = ded[bool(is_cur)].get((-1 if ik is None else ik, ym, c, b), (0.0, 0.0))
        if f.get("brand") is not None and b != f["brand"]:
            continue
        if f.get("pm") and not _pm_ok(f["pm"], b, nm):
            continue
        if f.get("rep") is not None and _am(c, b, nm, r) != f["rep"]:
            continue
        lab = []
        for dm in dims:
            if dm == "item":
                code, desc = items.get(ik, ("(no item)", ""))
                lab.append(f"{code} — {desc}" if desc else code)
            elif dm == "month":
                y, mo = divmod(ym, 100)
                if not is_cur:
                    y += 1
                lab.append(f"{y}-{mo:02d}")
            else:
                lab.append(_label(dm, c, b, r, ym, nm))
        cell = acc.setdefault(tuple(lab), [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]])
        s = cell[0 if is_cur else 1]
        s[0] += float(v or 0); s[1] += float(x or 0) + dx; s[2] += float(q or 0); s[3] += dn
    return acc


def _shift_month_labels(acc: dict, dims: list) -> dict:
    """In the cube path LY months carry LY dates; relabel them a year on so
    'Oct 2026' and its LY sit on one row."""
    if "month" not in dims:
        return acc
    mi = dims.index("month")
    out: dict = {}
    for key, (cur, ly) in acc.items():
        for slot, vals in ((0, cur), (1, ly)):
            if not any(vals):
                continue
            k = list(key)
            if slot == 1:
                y, m = k[mi].split("-")
                k[mi] = f"{int(y) + 1}-{m}"
            cell = out.setdefault(tuple(k), [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]])
            cell[slot] = [a + b for a, b in zip(cell[slot], vals)]
    return out


def explore(region: str, code: str, fy: int, today: date, dims: list, f: dict,
            margin: bool) -> dict:
    dims = [d for d in dims if d in DIMS][:MAX_LEVELS] or ["retailer"]
    p = period(code, fy, today)
    nm = with_pms(names(), region, f, today)
    m, d = cubes(region, today)
    out = {"period": {"label": p["label"], "from": p["a"].isoformat(), "to": p["b"].isoformat(),
                      "future": p["future"]},
           "dims": dims, "dim_labels": [DIMS[x] for x in dims], "rows": [], "margin": margin,
           "currency": sales.CURRENCY[region], "region": region}
    if not p["future"]:
        out["period"].update({"ly_from": p["ly_a"].isoformat(), "ly_to": p["ly_b"].isoformat()})
        if "item" in dims:
            key = ("sx_items", region, p["a"], p["b"], tuple(dims), tuple(sorted(
                (k, v) for k, v in f.items() if v not in (None, ""))))
            acc = warehouse.cached(key, lambda: _items_sql(region, p, dims, f, nm), ttl=DAILY_TTL)
        else:
            acc = {}
            _sum_range(m, d, p["a"], p["b"], dims, f, nm, 0, acc)
            _sum_range(m, d, p["ly_a"], p["ly_b"], dims, f, nm, 1, acc)
            acc = _shift_month_labels(acc, dims)
        for key, (cur, ly) in acc.items():
            row = {"k": list(key), "v": round(cur[0], 2), "ly": round(ly[0], 2),
                   "q": round(cur[2]), "lq": round(ly[2])}
            if margin:
                row["gp"] = round(cur[0] - cur[1], 2)          # net profit (see top)
                row["ns"] = round(cur[0] - cur[3], 2)          # net sales, after customer rebates
            out["rows"].append(row)
    # targets: to date for the dates shown, and for the whole period
    ams = retailer_ams(region, today)
    mix = category_mix(region, today)
    tg = targets(region, p["a"], p["b"], dims, f, nm, ams, mix)
    full = targets(region, p["a"], p["end"], [], f, nm, ams, mix)
    out["targets"] = {"has": tg is not None or full is not None,
                      "rows": bool(tg and tg[1] is not None),
                      "full": round(full[0]) if full and abs(full[0]) >= 1 else None}
    if tg and tg[1] is not None:
        idx = {tuple(r["k"]): r for r in out["rows"]}
        for key, t in tg[1].items():
            r = idx.get(key)
            if r is None:                     # a target with no sales (yet)
                r = idx[key] = {"k": list(key), "v": 0.0, "ly": 0.0, "q": 0, "lq": 0}
                if margin:
                    r["gp"] = 0.0
                    r["ns"] = 0.0
                out["rows"].append(r)
            r["t"] = round(t, 2)
    out["kpis"] = _kpis(out["rows"], margin)
    if margin and not p["future"]:
        miss, cur_m = [], p["a"].replace(day=1)
        while cur_m <= p["b"]:
            if bp_np.gl_missing(region, cur_m):
                miss.append(cur_m.strftime("%b %Y"))
            cur_m = (cur_m.replace(day=28) + timedelta(days=4)).replace(day=1)
        out["kpis"]["np_gl_missing"] = miss
    out["kpis"]["t"] = round(tg[0]) if tg else None
    out["kpis"]["to_t"] = (out["kpis"]["v"] / tg[0] * 100) if tg and tg[0] > 0 else None
    out["quick"] = quick(region, today, f, nm, m, d)
    out["trend"] = trend(region, fy, today, f, nm, m, d)
    return out


def _kpis(rows, margin):
    v = sum(r["v"] for r in rows)
    ly = sum(r["ly"] for r in rows)
    k = {"v": round(v), "ly": round(ly), "q": sum(r["q"] for r in rows),
         # % growth only off a positive base: a net-negative LY (credits) makes it nonsense
         "growth": ((v - ly) / ly * 100) if ly > 0 else None}
    if margin:
        gp = sum(r["gp"] for r in rows)
        ns = sum(r.get("ns", 0.0) for r in rows)
        k["gp"] = round(gp)
        k["gp_pct"] = (gp / v * 100) if v else None           # NP % on gross (revenue)
        k["np_net_pct"] = (gp / ns * 100) if ns else None     # NP % on net sales
    return k


def _total(m, d, a, b, f, nm) -> float:
    acc: dict = {}
    _sum_range(m, d, a, b, [], f, nm, 0, acc)
    return round(acc.get((), [[0.0]])[0][0])


def quick(region, today, f, nm, m, d) -> list:
    """Today / Yesterday / This week / This month, each vs the same dates LY."""
    out = []
    for code in ("today", "yesterday", "week", "mtd"):
        p = period(code, 2000, today)
        v = _total(m, d, p["a"], p["b"], f, nm)
        ly = _total(m, d, p["ly_a"], p["ly_b"], f, nm)
        out.append({"code": code, "label": {"today": "Today", "yesterday": "Yesterday",
                                            "week": "This week", "mtd": "This month"}[code],
                    "v": v, "ly": ly, "growth": ((v - ly) / ly * 100) if ly > 0 else None})
    return out


def trend(region, fy, today, f, nm, m, d) -> dict:
    """Revenue by fiscal month for the chosen FY and the FY before, one pass over the
    monthly summary (the current month there already holds everything to today)."""
    lo, hi = (fy - 2) * 100 + 4, fy * 100 + 3
    by: dict = {}
    k = m.k
    for i in range(bisect_left(k, lo), bisect_right(k, hi)):
        if f and not _filter_ok(f, m.c[i], m.b[i], m.r[i], nm):
            continue
        by[k[i]] = by.get(k[i], 0.0) + m.v[i]
    ams, mix = retailer_ams(region, today), category_mix(region, today)
    cur, prev, tgt = [], [], []
    for i in range(12):
        mo = (i + 3) % 12 + 1
        y = fy - 1 + (1 if mo < 4 else 0)
        cur.append(None if date(y, mo, 1) > today else round(by.get(y * 100 + mo, 0.0)))
        prev.append(round(by.get((y - 1) * 100 + mo, 0.0)))
        t = targets(region, date(y, mo, 1), _month_end(y, mo), [], f, nm, ams, mix)
        tgt.append(round(t[0]) if t and t[0] else None)
    return {"labels": list(FY_MONTHS), "cur": cur, "prev": prev,
            "target": tgt if any(tgt) else None,
            "fy": f"FY{fy % 100:02d}", "prev_fy": f"FY{(fy - 1) % 100:02d}"}


def options(region: str, today: date) -> dict:
    """Filter lists: brands, reps, PMs (and senior PMs' teams), retailers with sales in the
    last two FYs."""
    def build():
        nm = with_pms(names(), region, None, today)
        m, _ = cubes(region, today)
        since = (sales.fy_start(today).year - 1) * 100 + 4
        brands, reps, rets, pms = {}, {}, {}, set()
        for i in range(len(m)):
            if m.k[i] < since:
                continue
            v = m.v[i]
            b, c = m.b[i], m.c[i]
            r = _am(c, b, nm, m.r[i])
            if b in nm["brand"]:
                brands[b] = brands.get(b, 0) + v
                pms.add(nm["pm_b"].get(b) or NO_PM)
            if r in nm["rep"]:
                reps[r] = reps.get(r, 0) + v
            g = nm["cust"].get(c, (None, None))[1]
            if g:
                rets[g] = rets.get(g, 0) + v
        return {"brands": [{"id": b, "name": nm["brand"][b]} for b in sorted(brands, key=lambda x: nm["brand"][x])],
                "reps": [{"id": r, "name": nm["rep"][r]} for r in sorted(reps, key=lambda x: nm["rep"][x])],
                "retailers": sorted(rets, key=lambda g: -rets[g]),
                "pms": sorted(p_ for p_ in pms if p_ != NO_PM) + ([NO_PM] if NO_PM in pms else []),
                "teams": sorted(pm_teams(today)),
                "fys": list(range(2023, sales.fy_start(today).year + 2))}
    return warehouse.cached(("sx_opts", region, today), build, ttl=MONTHLY_TTL)


# ---- Excel ----------------------------------------------------------------------------
def to_xlsx(data: dict, title: str, filters: str) -> bytes:
    """The matrix flattened: one row per leaf with its level columns, then the measures."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    margin = data.get("margin")
    hdr = list(data["dim_labels"]) + ["Revenue", "LY", "YoY $", "YoY %", "Units", "LY units"]
    if margin:
        hdr += ["Net profit", "NP % on gross", "NP % on net"]
    tgt = (data.get("targets") or {}).get("rows")
    if tgt:
        hdr += ["Target", "% of target"]
    ws.cell(row=1, column=1, value=f"{title} — {filters}").font = Font(bold=True)
    fill = PatternFill("solid", fgColor="1F4E79")
    for j, h in enumerate(hdr, 1):
        c = ws.cell(row=2, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill
        c.alignment = Alignment(horizontal="center")
        ws.column_dimensions[get_column_letter(j)].width = 34 if j <= len(data["dim_labels"]) else 14
    money = '"NZ$"#,##0.00' if data["currency"] == "NZD" else "$#,##0.00"
    nd = len(data["dim_labels"])
    rows = sorted(data["rows"], key=lambda r: [str(x) for x in r["k"]])
    for i, r in enumerate(rows, 3):
        yoy = r["v"] - r["ly"]
        vals = list(r["k"]) + [r["v"], r["ly"], yoy, (yoy / r["ly"]) if r["ly"] > 0 else None, r["q"], r["lq"]]
        if margin:
            vals += [r["gp"], (r["gp"] / r["v"]) if r["v"] else None,
                     (r["gp"] / r["ns"]) if r.get("ns") else None]
        if tgt:
            t = r.get("t")
            vals += [t, (r["v"] / t) if t else None]
        tj = len(hdr) - 1                     # the Target column, when there is one
        for j, val in enumerate(vals, 1):
            c = ws.cell(row=i, column=j, value=val)
            if j in (nd + 1, nd + 2, nd + 3) or (margin and j == nd + 7) or (tgt and j == tj):
                c.number_format = money
            elif j == nd + 4 or (margin and j in (nd + 8, nd + 9)) or (tgt and j == tj + 1):
                c.number_format = "0.0%"
            elif j > nd:
                c.number_format = "#,##0"
    ws.freeze_panes = ws.cell(row=3, column=nd + 1)
    ws.auto_filter.ref = f"A2:{get_column_letter(len(hdr))}{len(rows) + 2}"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
