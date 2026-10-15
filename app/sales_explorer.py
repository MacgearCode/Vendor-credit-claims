"""Sales Explorer — Macgear's internal NetSuite revenue, sliced however the user
likes: period, filters, and a matrix whose rows they build from the dimensions.
Staff with the `sell_in_value` capability only; margin columns need `margin`.

Definitions are the home page's (app/sales.py): revenue = net_sales (account 54,
intercompany excluded, ex-GST, subsidiary currency); periods on Macgear's FINANCIAL
year (April–March, FY27 = Apr 2026 – Mar 2027); "LY" = the same dates a year earlier;
a period that hasn't finished compares to-date with to-date. AU and NZ never summed.

ACCOUNT MANAGER = the CURRENT account rep on the customer record (NetSuite customer
salesrep, dim_ns_customer.account_rep_id), applied to ALL history — so a manager's
numbers are their whole portfolio, including sales made before they took an account
over (user, 2026-10-15). It is NOT the rep on each invoice.

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

from . import sales, warehouse

MONTHLY_TTL = 30 * 60          # closed months barely move
DAILY_TTL = 5 * 60             # today's invoices

DIMS = {                       # key -> label (order = the builder's chips)
    "retailer": "Retailer", "account": "Account", "brand": "Brand",
    "rep": "Account manager", "item": "Item", "month": "Month",
}
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
        return {"cust": cust, "brand": brand, "rep": rep}
    return warehouse.cached(("sx_names",), build, ttl=60 * 60)


# ---- the in-memory summary --------------------------------------------------------
class Cube:
    """Columnar rows: period key (yyyymm or yyyymmdd), customer_id, brand_id, rep_id,
    revenue, est. cost, units. Ids of None are stored as -1. Rows arrive SORTED by the
    period key, so a date range is a bisect slice, not a full scan."""
    __slots__ = ("k", "c", "b", "r", "v", "x", "q")

    def __init__(self, rows):
        self.k, self.c, self.b, self.r = array("i"), array("i"), array("i"), array("i")
        self.v, self.x, self.q = array("d"), array("d"), array("d")
        for k, c, b, r, v, x, q in rows:
            self.k.append(k); self.c.append(-1 if c is None else c)
            self.b.append(-1 if b is None else b); self.r.append(-1 if r is None else r)
            self.v.append(float(v or 0)); self.x.append(float(x or 0)); self.q.append(float(q or 0))

    def __len__(self):
        return len(self.k)


def _monthly(region: str) -> Cube:
    rk = warehouse.region_key(region)
    # No rep column: the account manager is a property of the customer (see top), so
    # it's looked up per customer at slice time. Dropping it also shrinks the summary.
    rows = warehouse.query("""
        select s.date_key / 100, s.customer_id, s.brand_id, null::int,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where s.region_key = %(rk)s and s.net_sales is not null
        group by 1, 2, 3 order by 1""", {"rk": rk})
    return Cube(rows)


def daily_window(today: date) -> tuple[date, date, date, date]:
    """(this_lo, this_hi, ly_lo, ly_hi) held at day grain: 1st of last month -> today,
    and the same span a year earlier extended to that month's end."""
    lo = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    ly_lo, ly_today = sales.last_year(lo), sales.last_year(today)
    return lo, today, ly_lo, _month_end(ly_today.year, ly_today.month)


def _daily(region: str, today: date) -> Cube:
    rk = warehouse.region_key(region)
    lo, hi, ly_lo, ly_hi = daily_window(today)
    rows = warehouse.query("""
        select s.date_key, s.customer_id, s.brand_id, null::int,
               sum(s.net_sales), sum(s.net_sales_est_cost), sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where s.region_key = %(rk)s and s.net_sales is not null
          and (s.date_key between %(lo)s and %(hi)s or s.date_key between %(llo)s and %(lhi)s)
        group by 1, 2, 3 order by 1""",
        {"rk": rk, "lo": _key(lo), "hi": _key(hi), "llo": _key(ly_lo), "lhi": _key(ly_hi)})
    return Cube(rows)


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
        return {"label": label, "a": a, "b": a - timedelta(days=1), "future": True}
    partial = b > today
    b = min(b, today)
    return {"label": label + (" (to date)" if partial and code not in
                              ("mtd", "qtd", "ytd", "week", "today") else ""),
            "a": a, "b": b, "ly_a": sales.last_year(a), "ly_b": sales.last_year(b),
            "future": False}


# ---- slicing ------------------------------------------------------------------------
def _am(c: int, nm: dict):
    """The customer's current account manager id (None if the record has none)."""
    t = nm["cust"].get(c)
    return t[2] if t else None


def _filter_ok(f: dict, c: int, b: int, r: int, nm: dict) -> bool:
    if f.get("brand") is not None and b != f["brand"]:
        return False
    if f.get("rep") is not None and _am(c, nm) != f["rep"]:
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
    if dim == "rep":
        return nm["rep"].get(_am(c, nm)) or "(no account manager)"
    if dim == "month":
        ym = k if k < 1000000 else k // 100
        return f"{ym // 100}-{ym % 100:02d}"
    return ""


def _sum_range(m: Cube, d: Cube, a: date, b: date, dims, f, nm, slot: int, acc: dict):
    """Add [a, b] into acc[key][slot] as [rev, cost, qty]."""
    for kind, lo, hi in _parts(a, b):
        cube = m if kind == "m" else d
        k, c, bb, r, v, x, q = cube.k, cube.c, cube.b, cube.r, cube.v, cube.x, cube.q
        for i in range(bisect_left(k, lo), bisect_right(k, hi)):
            ki = k[i]
            ci, bi, ri = c[i], bb[i], r[i]
            if f and not _filter_ok(f, ci, bi, ri, nm):
                continue
            key = tuple(_label(dm, ci, bi, ri, ki, nm) for dm in dims)
            cell = acc.get(key)
            if cell is None:
                cell = acc[key] = [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
            s = cell[slot]
            s[0] += v[i]; s[1] += x[i]; s[2] += q[i]


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
        ids = [cid for cid, t in nm["cust"].items()
               if (f.get("rep") is None or t[2] == f["rep"])
               and (not f.get("retailer") or t[1] == f["retailer"])]
        where.append("s.customer_id = any(%(cids)s)"); prm["cids"] = ids
    # Group by only what the rows (or the brand filter) need — every id took ~6 s.
    need_c = any(x in dims for x in ("retailer", "account", "rep"))
    need_b = "brand" in dims or f.get("brand") is not None
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
    keys = {r[0] for r in rows if r[0] is not None}
    items = dict(((k, (c, d)) for k, c, d in warehouse.query(
        "select item_key, macgear_code, description from reporting.dim_item where item_key = any(%(k)s)",
        {"k": list(keys)}))) if keys else {}
    acc: dict = {}
    for ik, ym, c, b, r, is_cur, v, x, q in rows:
        c = -1 if c is None else c
        b = -1 if b is None else b
        r = -1 if r is None else r
        if f.get("brand") is not None and b != f["brand"]:
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
        cell = acc.setdefault(tuple(lab), [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
        s = cell[0 if is_cur else 1]
        s[0] += float(v or 0); s[1] += float(x or 0); s[2] += float(q or 0)
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
            cell = out.setdefault(tuple(k), [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
            cell[slot] = [a + b for a, b in zip(cell[slot], vals)]
    return out


def explore(region: str, code: str, fy: int, today: date, dims: list, f: dict,
            margin: bool) -> dict:
    dims = [d for d in dims if d in DIMS][:MAX_LEVELS] or ["retailer"]
    p = period(code, fy, today)
    nm = names()
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
                row["gp"] = round(cur[0] - cur[1], 2)
            out["rows"].append(row)
    out["kpis"] = _kpis(out["rows"], margin)
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
        k["gp"] = round(gp)
        k["gp_pct"] = (gp / v * 100) if v else None
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
    cur, prev = [], []
    for i in range(12):
        mo = (i + 3) % 12 + 1
        y = fy - 1 + (1 if mo < 4 else 0)
        cur.append(None if date(y, mo, 1) > today else round(by.get(y * 100 + mo, 0.0)))
        prev.append(round(by.get((y - 1) * 100 + mo, 0.0)))
    return {"labels": list(FY_MONTHS), "cur": cur, "prev": prev,
            "fy": f"FY{fy % 100:02d}", "prev_fy": f"FY{(fy - 1) % 100:02d}"}


def options(region: str, today: date) -> dict:
    """Filter lists: brands, reps, retailers with sales in the last two FYs."""
    def build():
        nm = names()
        m, _ = cubes(region, today)
        since = (sales.fy_start(today).year - 1) * 100 + 4
        brands, reps, rets = {}, {}, {}
        for i in range(len(m)):
            if m.k[i] < since:
                continue
            v = m.v[i]
            b, c = m.b[i], m.c[i]
            r = _am(c, nm)
            if b in nm["brand"]:
                brands[b] = brands.get(b, 0) + v
            if r in nm["rep"]:
                reps[r] = reps.get(r, 0) + v
            g = nm["cust"].get(c, (None, None))[1]
            if g:
                rets[g] = rets.get(g, 0) + v
        return {"brands": [{"id": b, "name": nm["brand"][b]} for b in sorted(brands, key=lambda x: nm["brand"][x])],
                "reps": [{"id": r, "name": nm["rep"][r]} for r in sorted(reps, key=lambda x: nm["rep"][x])],
                "retailers": sorted(rets, key=lambda g: -rets[g]),
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
        hdr += ["Est. GP", "GP %"]
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
            vals += [r["gp"], (r["gp"] / r["v"]) if r["v"] else None]
        for j, val in enumerate(vals, 1):
            c = ws.cell(row=i, column=j, value=val)
            if j in (nd + 1, nd + 2, nd + 3) or (margin and j == nd + 7):
                c.number_format = money
            elif j == nd + 4 or (margin and j == nd + 8):
                c.number_format = "0.0%"
            elif j > nd:
                c.number_format = "#,##0"
    ws.freeze_panes = ws.cell(row=3, column=nd + 1)
    ws.auto_filter.ref = f"A2:{get_column_letter(len(hdr))}{len(rows) + 2}"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
