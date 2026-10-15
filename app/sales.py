"""Internal sales summary for the home page — Macgear's own NetSuite sales, not
sell-through. Staff with the `sell_in_value` capability only (Macgear sales $
are never shown to vendors).

Definitions (shared with the warehouse — see Sell Through/CLAUDE.md):
  revenue = fact_sales_line.net_sales: posting account 54, intercompany
            (AU -> NZ) excluded, ex-GST, in the subsidiary's currency.
            AU and NZ are never added together.
  est. GP = net_sales - net_sales_est_cost (NetSuite's estimate, not COGS).
  periods = Macgear's financial year, April–March, named by its end year
            (FY27 = Apr 2026 – Mar 2027); quarters Q1 = Apr–Jun … Q4 = Jan–Mar.
            Same as dim_date.fy_* in the warehouse.
  last year = the same calendar dates one year earlier ("same period LY").
  pace    = month-to-date ÷ weekdays elapsed × weekdays in the month — a
            simple run-rate, not a forecast.

Targets: read from reporting.sales_target if that table exists (see
`targets()`); until then the page says "no target loaded".
"""
import calendar
from datetime import date, timedelta

from . import warehouse

REGIONS = ("AU", "NZ")
# NetSuite lands in the warehouse every 15 min (Zone export -> S3 -> Windmill);
# keep the site's copy at most 5 min behind the warehouse.
SALES_TTL = 5 * 60
CURRENCY = {"AU": "AUD", "NZ": "NZD"}


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def fy_label(d: date) -> str:
    return f"FY{(fy_start(d).year + 1) % 100:02d}"


def fq_start(d: date) -> date:
    """First day of the financial quarter: 1 Apr, 1 Jul, 1 Oct or 1 Jan — always
    in the same calendar year as d (a quarter never straddles 31 Dec)."""
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)


def fq_label(d: date) -> str:
    return f"Q{((d.month - 4) % 12) // 3 + 1}"


def last_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                               # 29 Feb
        return d.replace(year=d.year - 1, day=28)


def weekdays(a: date, b: date) -> int:
    """Mon–Fri days from a to b inclusive."""
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def _key(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _from_key(k: int) -> date:
    return date(k // 10000, k // 100 % 100, k % 100)


# ---- data ----------------------------------------------------------------------
def daily(start: date) -> dict:
    """{region: {date: [revenue, est_cost, units]}} from `start` to today."""
    rk = {v: k for k, v in ((c, warehouse.region_key(c)) for c in REGIONS)}
    rows = warehouse.query("""
        select s.region_key, s.date_key, sum(s.net_sales), sum(s.net_sales_est_cost),
               sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where s.date_key >= %(start)s and s.net_sales is not null
        group by 1, 2""", {"start": _key(start)})
    out = {r: {} for r in REGIONS}
    for region_key, dk, v, c, q in rows:
        if region_key in rk:
            out[rk[region_key]][_from_key(dk)] = [float(v or 0), float(c or 0), float(q or 0)]
    return out


def tops(start: date, end: date) -> dict:
    """Top customers (retail group), brands, reps and items by revenue, per region."""
    p = {"lo": _key(start), "hi": _key(end)}
    rk = {warehouse.region_key(c): c for c in REGIONS}
    q = {
        "customers": """select s.region_key,
                   coalesce(nc.customer_group, nc.parent_name, nc.customer_name, 'Other'),
                   sum(s.net_sales), sum(s.net_sales_est_cost)
            from reporting.fact_sales_line s
            left join reporting.dim_ns_customer nc on nc.customer_id = s.customer_id
            where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
            group by 1, 2""",
        "brands": """select s.region_key, coalesce(b.brand, 'Other'),
                   sum(s.net_sales), sum(s.net_sales_est_cost)
            from reporting.fact_sales_line s
            left join reporting.dim_brand b on b.brand_id = s.brand_id
            where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
            group by 1, 2""",
        # the customer's CURRENT account manager, not the rep on each invoice (user,
        # 2026-10-15) — same rule as Sales Explorer
        "reps": """select s.region_key, coalesce(nc.account_rep, 'No account manager'),
                   sum(s.net_sales), sum(s.net_sales_est_cost)
            from reporting.fact_sales_line s
            left join reporting.dim_ns_customer nc on nc.customer_id = s.customer_id
            where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
            group by 1, 2""",
        "items": """select s.region_key, i.macgear_code || ' — ' || coalesce(i.description, ''),
                   sum(s.net_sales), sum(s.net_sales_est_cost), i.macgear_code
            from reporting.fact_sales_line s
            join reporting.dim_item i on i.item_key = s.item_key
            where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
            group by 1, 2, 5""",
    }
    out = {r: {} for r in REGIONS}
    with warehouse.connect() as conn, conn.cursor() as cur:
        for name, sql in q.items():
            cur.execute(sql, p)
            per = {r: [] for r in REGIONS}
            for row in cur.fetchall():
                region = rk.get(row[0])
                if region:
                    v, c = float(row[2] or 0), float(row[3] or 0)
                    item = {"name": row[1], "value": round(v), "gp": round(v - c)}
                    if name == "items":
                        item["code"] = row[4]
                    per[region].append(item)
            for r in REGIONS:
                out[r][name] = sorted(per[r], key=lambda x: -x["value"])[:8]
    return out


def targets() -> dict:
    """{region: {month_start: amount}} from reporting.sales_target, or {} while it
    doesn't exist. Expected shape (one row per country x month; finer rows —
    brand, customer — are summed):
        country_code text ('AU'|'NZ'), month_start date, amount numeric (ex-GST)
    """
    try:
        rows = warehouse.query("""
            select country_code, month_start, sum(amount)
            from reporting.sales_target group by 1, 2""")
    except Exception:
        return {}
    out: dict = {}
    for cc, m, amt in rows:
        out.setdefault(cc, {})[m] = float(amt or 0)
    return out


# ---- the summary -----------------------------------------------------------------
def _sum(days: dict, a: date, b: date) -> list:
    t = [0.0, 0.0, 0.0]
    for d, v in days.items():
        if a <= d <= b:
            t[0] += v[0]; t[1] += v[1]; t[2] += v[2]
    return t


def _block(days: dict, a: date, b: date, tgt: float | None, margin: bool) -> dict:
    cur = _sum(days, a, b)
    ly = _sum(days, last_year(a), last_year(b))
    out = {"value": round(cur[0]), "ly": round(ly[0]), "units": round(cur[2]),
           "growth": ((cur[0] - ly[0]) / abs(ly[0]) * 100) if ly[0] else None,
           "target": round(tgt) if tgt else None,
           "to_target": (cur[0] / tgt * 100) if tgt else None}
    if margin:
        out["gp"] = round(cur[0] - cur[1])
        out["gp_pct"] = ((cur[0] - cur[1]) / cur[0] * 100) if cur[0] else None
    return out


def summary(today: date, margin: bool) -> dict:
    """Everything the home page's sales panel draws."""
    fy0 = fy_start(today)
    days = warehouse.cached(("sales_daily", fy0), lambda: daily(last_year(fy0)), ttl=SALES_TTL)
    m0, q0 = today.replace(day=1), fq_start(today)
    month_days = calendar.monthrange(today.year, today.month)[1]
    m_end = today.replace(day=month_days)
    tg = warehouse.cached(("sales_targets",), targets)
    top = warehouse.cached(("sales_tops", m0, today), lambda: tops(m0, today), ttl=SALES_TTL)

    out = {"today": today.isoformat(), "fy": fy_label(today), "fq": fq_label(today),
           "month": today.strftime("%B"), "fy_start": fy0.isoformat(),
           "fq_start": q0.isoformat(), "has_targets": bool(tg), "regions": {}}
    wd_done, wd_all = weekdays(m0, today), weekdays(m0, m_end)
    for r in REGIONS:
        d = days.get(r, {})
        t = tg.get(r, {})
        month_t = t.get(m0)
        q_t = sum(v for k, v in t.items() if q0 <= k <= today) or None
        y_t = sum(v for k, v in t.items() if fy0 <= k <= today) or None
        mtd = _block(d, m0, today, month_t, margin)
        mtd["pace"] = round(mtd["value"] / wd_done * wd_all) if wd_done else None
        if month_t:
            mtd["pace_to_target"] = mtd["pace"] / month_t * 100
        # cumulative by day of month: this month, last month, same month last year
        lm0 = (m0 - timedelta(days=1)).replace(day=1)
        ly0 = last_year(m0)

        def cum(start, n):
            acc, outl = 0.0, []
            for i in range(n):
                acc += d.get(start + timedelta(days=i), [0])[0]
                outl.append(round(acc))
            return outl
        lm_days = calendar.monthrange(lm0.year, lm0.month)[1]
        ly_days = calendar.monthrange(ly0.year, ly0.month)[1]
        # last 14 days by day, for the bars
        recent = [{"d": (today - timedelta(days=i)).isoformat(),
                   "v": round(d.get(today - timedelta(days=i), [0])[0])} for i in range(13, -1, -1)]
        out["regions"][r] = {
            "currency": CURRENCY[r],
            "mtd": mtd,
            "qtd": _block(d, q0, today, q_t, margin),
            "ytd": _block(d, fy0, today, y_t, margin),
            "today": round(d.get(today, [0])[0]),
            "cum": {"this": cum(m0, today.day), "last_month": cum(lm0, lm_days),
                    "last_year": cum(ly0, ly_days), "month_days": month_days,
                    "month_target": round(month_t) if month_t else None},
            "recent": recent,
            # GP rides in the cached lists; drop it here for anyone without margin
            "top": {k: [x if margin else {kk: vv for kk, vv in x.items() if kk != "gp"} for x in v]
                    for k, v in top.get(r, {}).items()},
        }
    out["weekdays"] = {"done": wd_done, "all": wd_all}
    return out
