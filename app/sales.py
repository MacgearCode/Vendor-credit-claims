"""Internal sales summary for the home page — Macgear's own NetSuite sales, not
sell-through. Staff with the `sell_in_value` capability only (Macgear sales $
are never shown to vendors).

Definitions (shared with the warehouse — see Sell Through/CLAUDE.md):
  revenue = fact_sales_line.net_sales: posting account 54, intercompany
            (AU -> NZ) excluded, ex-GST, in the subsidiary's currency.
            AU and NZ are never added together.
  net profit (the "margin" figures, 2026-11-17) = net_sales - est. cost - the Brand Profitability
            deductions per line (customer rebates, MDF, the brand's freight / vendor rebates
            shared by gross; brand_profit/np.py). Was est. GP (net_sales - est. cost). NP % is
            shown on gross (revenue) and on net sales (revenue - customer rebates).
  periods = Macgear's financial year, April–March, named by its end year
            (FY27 = Apr 2026 – Mar 2027); quarters Q1 = Apr–Jun … Q4 = Jan–Mar.
            Same as dim_date.fy_* in the warehouse.
  last year = the same calendar dates one year earlier ("same period LY").
  pace    = month-to-date ÷ weekdays elapsed × weekdays in the month — a
            simple run-rate, not a forecast.

Targets: the official targets committed in the Targets tool (targets.sales_target, see
`targets()`); until something is committed the page says "no target loaded".
"""
import calendar
from datetime import date, timedelta

from . import portfolio, warehouse
from .brand_profit import np as bp_np

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
    """{region: {date: [revenue, cost, units, rebates]}} from `start` to today; cost = est. cost +
    net-profit deductions (unallocated freight / vendor rebates on the month's 1st)."""
    rk = {v: k for k, v in ((c, warehouse.region_key(c)) for c in REGIONS)}
    rows = warehouse.query_summary("agg_sales_day", """
        select s.region_key, s.date_key, sum(s.net_sales), sum(s.net_sales_est_cost),
               sum(s.net_sales_qty)
        from reporting.agg_sales_day s
        where s.date_key >= %(start)s and s.n_net > 0
        group by 1, 2""", """
        select s.region_key, s.date_key, sum(s.net_sales), sum(s.net_sales_est_cost),
               sum(s.net_sales_qty)
        from reporting.fact_sales_line s
        where s.date_key >= %(start)s and s.net_sales is not null
        group by 1, 2""", {"start": _key(start)})
    out = {r: {} for r in REGIONS}
    for region_key, dk, v, c, q in rows:
        if region_key in rk:
            out[rk[region_key]][_from_key(dk)] = [float(v or 0), float(c or 0), float(q or 0), 0.0]
    hi = _key(date.today() + timedelta(days=1))
    for r in REGIONS:
        days = out[r]
        for dk, (dx, dn) in bp_np.deductions(r, _key(start), hi, lambda k, c, b, i: k).items():
            cell = days.setdefault(_from_key(dk), [0.0, 0.0, 0.0, 0.0])
            cell[1] += dx
            cell[3] += dn
        for ym, amt in bp_np.unallocated(r, _key(start), hi).items():
            cell = days.setdefault(date(ym // 100, ym % 100, 1), [0.0, 0.0, 0.0, 0.0])
            cell[1] += amt
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
        # the CURRENT account manager, not the rep on each invoice (user, 2026-10-15) —
        # same rule as Sales Explorer: Portfolios page row for retailer x brand / retailer,
        # else NetSuite's customer rep (portfolio.am_map) — so grouped by customer x brand
        "reps": """select s.region_key, s.customer_id, s.brand_id,
                   sum(s.net_sales), sum(s.net_sales_est_cost)
            from reporting.fact_sales_line s
            where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
            group by 1, 2, 3""",
        "items": """select s.region_key, i.macgear_code || ' — ' || coalesce(i.description, ''),
                   sum(s.net_sales), sum(s.net_sales_est_cost), i.macgear_code
            from reporting.fact_sales_line s
            join reporting.dim_item i on i.item_key = s.item_key
            where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
            group by 1, 2, 5""",
    }
    # net-profit deductions by the same labels (brand_profit/np.py)
    lab = {"customers": {}, "brands": {}, "items": {}}
    for cid, grp in warehouse.query(
            "select customer_id, coalesce(customer_group, parent_name, customer_name, 'Other') "
            "from reporting.dim_ns_customer"):
        lab["customers"][cid] = grp
    lab["brands"] = dict(warehouse.query("select brand_id, brand from reporting.dim_brand"))
    amap = portfolio.am_map()
    # Portfolios category rows (AU The Good Guys EUFY by item category): those customer x
    # brand pairs are re-read from the line facts by category below
    pairs = {(c, b) for r in REGIONS for (c, b) in portfolio.category_pairs(r)}
    icat = portfolio.item_categories() if pairs else {}

    def am_name(c, b, cat=-1):
        return portfolio.am_of(amap, c, b, cat) or "No account manager"
    ded = {}
    for r in REGIONS:
        ded[r] = {
            "customers": bp_np.deductions(r, p["lo"], p["hi"], lambda k, c, b, i: lab["customers"].get(c, "Other")),
            "reps": bp_np.deductions(r, p["lo"], p["hi"], lambda k, c, b, i: am_name(c, b, icat.get(i, -1))),
            "brands": bp_np.deductions(r, p["lo"], p["hi"], lambda k, c, b, i: lab["brands"].get(b) or "Other"),
            "items": bp_np.deductions(r, p["lo"], p["hi"], lambda k, c, b, i: i)}
    out = {r: {} for r in REGIONS}
    with warehouse.connect() as conn, conn.cursor() as cur:
        # customers / brands / reps from the day summary when it's fresh (same sums;
        # n_net > 0 = the lines with a net_sales value); items need the line facts
        fast = "agg_sales_day" in warehouse.fresh_summaries(cur)
        cur.execute("select macgear_code, item_key from reporting.dim_item")
        item_key_of = {}
        for code, ik in cur.fetchall():
            item_key_of.setdefault(code, []).append(ik)
        for name, sql in q.items():
            if fast and name != "items":
                sql = (sql.replace("from reporting.fact_sales_line s", "from reporting.agg_sales_day s")
                          .replace("s.net_sales is not null", "s.n_net > 0"))
            cur.execute(sql, p)
            fetched = cur.fetchall()
            if name == "reps":                 # customer x brand -> manager, summed
                if pairs:
                    cur.execute("""select s.region_key, s.customer_id, s.brand_id, i.category,
                               sum(s.net_sales), sum(s.net_sales_est_cost)
                        from reporting.fact_sales_line s
                        left join reporting.dim_item i on i.item_key = s.item_key
                        where s.date_key between %(lo)s and %(hi)s and s.net_sales is not null
                          and s.customer_id = any(%(c)s)
                        group by 1, 2, 3, 4""", {**p, "c": sorted({c for c, _ in pairs})})
                    fine = [(rkey, c, b, portfolio.category_id(k), v, x) for rkey, c, b, k, v, x in cur.fetchall()
                            if (c, b) in pairs]
                    fetched = [(rkey, c, b, -1, v, x) for rkey, c, b, v, x in fetched if (c, b) not in pairs] + fine
                else:
                    fetched = [(rkey, c, b, -1, v, x) for rkey, c, b, v, x in fetched]
                agg: dict = {}
                for rkey, c, b, k, v, x in fetched:
                    cell = agg.setdefault((rkey, am_name(c, b, k)), [0.0, 0.0])
                    cell[0] += float(v or 0); cell[1] += float(x or 0)
                fetched = [(rkey, n, v, x) for (rkey, n), (v, x) in agg.items()]
            per = {r: [] for r in REGIONS}
            for row in fetched:
                region = rk.get(row[0])
                if region:
                    v, c = float(row[2] or 0), float(row[3] or 0)
                    if name == "items":
                        c += sum(ded[region]["items"].get(ik, (0.0, 0.0))[0] for ik in item_key_of.get(row[4], ()))
                    else:
                        c += ded[region][name].get(row[1], (0.0, 0.0))[0]
                    item = {"name": row[1], "value": round(v), "gp": round(v - c)}   # gp = net profit
                    if name == "items":
                        item["code"] = row[4]
                    per[region].append(item)
            for r in REGIONS:
                out[r][name] = sorted(per[r], key=lambda x: -x["value"])[:8]
    return out


def data_version():
    """Changes whenever NetSuite sales land: newest Last Modified plus the line count of
    the last few days (the count catches a load that only removes lines). ~30 ms."""
    since = int((date.today() - timedelta(days=4)).strftime("%Y%m%d"))
    return tuple(warehouse.query(
        "select max(last_modified), count(*) from reporting.fact_sales_line where date_key >= %s",
        (since,))[0])


# Everything built from NetSuite sales or stock: rebuilt as soon as a new load lands.
SALES_CACHE_PREFIXES = ("sales_daily", "sales_tops", "sales_fresh", "sales_ic",
                        "sx_day", "sx_month", "sx_mix", "sx_items", "sx_opts", "sx_ram", "item", "st_weekly",
                        "targets_actuals", "targets_years", "bp_lines")


def intercompany(start: date, end: date) -> dict:
    """AU -> NZ intercompany sales on the Sales account, per region — EXCLUDED from
    revenue (warehouse rule), shown beside it so the tile reconciles to a NetSuite
    search that includes it."""
    rk = {warehouse.region_key(c): c for c in REGIONS}
    rows = warehouse.query_summary("agg_sales_day", """
        select s.region_key, sum(s.intercompany_amount) from reporting.agg_sales_day s
        where s.intercompany_amount is not null
          and s.date_key between %(lo)s and %(hi)s
        group by 1""", """
        select s.region_key, sum(s.amount) from reporting.fact_sales_line s
        where s.account_id = 54 and s.is_intercompany
          and s.date_key between %(lo)s and %(hi)s
        group by 1""", {"lo": _key(start), "hi": _key(end)})
    return {rk[r]: round(float(v or 0)) for r, v in rows if r in rk}


# The official targets the site may show: each FY up to its "show up to" month, if one is set.
SHOWN_TARGETS = ("from sales_target s left join site_visibility v on v.target_fy = s.target_fy "
                 "where v.show_through is null or s.month_start <= v.show_through")


def targets() -> dict:
    """{region: {month_start: amount}} — the official (committed) targets from the
    Targets tool: targets.sales_target, the newest commit per financial year
    (app/targets), read through the Targets connection (TARGETS_DB_URL) — where they
    were committed. {} until something is committed or when Targets isn't configured.
    Finer rows (brand, customer) are summed. Months after an FY's "show up to" month
    (targets.site_visibility, set by admins) are left out."""
    from .targets import db as tdb
    try:
        with tdb.connect() as conn:
            rows = [(r["country_code"], r["month_start"], r["amount"]) for r in conn.execute(
                "select country_code, month_start, sum(amount) as amount "
                f"{SHOWN_TARGETS} group by 1, 2")]
    except Exception:
        return {}
    out: dict = {}
    for cc, m, amt in rows:
        out.setdefault(cc, {})[m] = float(amt or 0)
    return out


def target_lines() -> dict:
    """{region: [(month_start, brand, retailer, amount)]} — the same official targets at
    the grain they were set, for Sales Explorer. Brand and retailer are NetSuite's names
    (dim_brand, and the retailer label coalesce(customer_group, parent_name,
    customer_name)), so they line up with the explorer's rows. {} as for targets()."""
    from .targets import db as tdb
    try:
        with tdb.connect() as conn:
            rows = [(r["country_code"], r["month_start"], r["brand"], r["customer"], r["amount"])
                    for r in conn.execute(
                        "select country_code, month_start, brand, customer, sum(amount) as amount "
                        f"{SHOWN_TARGETS} group by 1, 2, 3, 4")]
    except Exception:
        return {}
    out: dict = {}
    for cc, m, b, c, amt in rows:
        out.setdefault(cc, []).append((m, b, c, float(amt or 0)))
    return out


# ---- the summary -----------------------------------------------------------------
def _sum(days: dict, a: date, b: date) -> list:
    t = [0.0, 0.0, 0.0, 0.0]
    for d, v in days.items():
        if a <= d <= b:
            t[0] += v[0]; t[1] += v[1]; t[2] += v[2]; t[3] += v[3] if len(v) > 3 else 0.0
    return t


def _block(days: dict, a: date, b: date, tgt: float | None, margin: bool) -> dict:
    cur = _sum(days, a, b)
    ly = _sum(days, last_year(a), last_year(b))
    out = {"value": round(cur[0]), "ly": round(ly[0]), "units": round(cur[2]),
           "growth": ((cur[0] - ly[0]) / abs(ly[0]) * 100) if ly[0] else None,
           "target": round(tgt) if tgt else None,
           "to_target": (cur[0] / tgt * 100) if tgt else None}
    if margin:                                       # net profit (see top)
        out["gp"] = round(cur[0] - cur[1])
        out["gp_pct"] = ((cur[0] - cur[1]) / cur[0] * 100) if cur[0] else None
        net = cur[0] - cur[3]
        out["np_net_pct"] = ((cur[0] - cur[1]) / net * 100) if net else None
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
    ic = warehouse.cached(("sales_ic", m0, today), lambda: intercompany(m0, today), ttl=SALES_TTL)
    ic_q = warehouse.cached(("sales_ic", q0, today), lambda: intercompany(q0, today), ttl=SALES_TTL)
    ic_y = warehouse.cached(("sales_ic", fy0, today), lambda: intercompany(fy0, today), ttl=SALES_TTL)

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
        if margin:
            mtd["np_gl_missing"] = bp_np.gl_missing(r, today)
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

        def tline(start, n):
            """Cumulative target by day: each month's target spread evenly over its days. The
            line stops after the last month that has a target (AU FY27 Q4 isn't set yet, user
            2026-11-27) instead of running flat through months with none."""
            acc, outl, last = 0.0, [], -1
            for i in range(n):
                dd = start + timedelta(days=i)
                mt = t.get(dd.replace(day=1))
                if mt and mt >= 1:              # AU Q4 FY27 holds a few −$1 lines, not a target
                    last = i
                acc += (mt or 0) / calendar.monthrange(dd.year, dd.month)[1]
                outl.append(round(acc))
            if last < 0:
                return None
            end = start + timedelta(days=last)
            stop = (end.replace(day=calendar.monthrange(end.year, end.month)[1]) - start).days + 1
            return outl[:stop]

        def span(a, b):
            return (b - a).days + 1
        # the chart's Quarter and Year views (user, 2026-11-27): cumulative by day over the
        # financial quarter / year, against the previous quarter and the same period LY
        q_end = (q0.replace(day=28) + timedelta(days=70)).replace(day=1) - timedelta(days=1)
        pq0 = fq_start(q0 - timedelta(days=1))
        y_end = date(fy0.year + 1, 3, 31)
        cum_q = {"start": q0.isoformat(), "days": span(q0, q_end), "this": cum(q0, span(q0, today)),
                 "prev": cum(pq0, span(pq0, q0 - timedelta(days=1))),
                 "last_year": cum(last_year(q0), span(last_year(q0), last_year(q_end))),
                 "target": tline(q0, span(q0, q_end))}
        cum_y = {"start": fy0.isoformat(), "days": span(fy0, y_end), "this": cum(fy0, span(fy0, today)),
                 "last_year": cum(last_year(fy0), span(last_year(fy0), last_year(y_end))),
                 "target": tline(fy0, span(fy0, y_end))}
        def period(a, end, blk):
            """The Quarter / Year view of the main number (user, 2026-11-27): the dial measures
            against the whole period's target and a weekday pace to the period end, like the
            month's. Where only some months have a shown target (FY27 to Dec while Q4 is
            unreleased) the dial — sales, pace and target — runs to the last such month
            ("dial_end"), never comparing a full year's sales with nine months of targets."""
            tm = [k for k, v in t.items() if a <= k <= end and v >= 1]
            stop = end
            if tm:
                last = max(tm)
                stop = last.replace(day=calendar.monthrange(last.year, last.month)[1])
                tgt = sum(v for k, v in t.items() if a <= k <= stop)
            else:
                tgt = None
            upto = min(today, stop)
            val = _sum(d, a, upto)[0]
            done, all_ = weekdays(a, upto), weekdays(a, stop)
            blk["dial_value"] = round(val)
            blk["dial_target"] = round(tgt) if tgt else None
            blk["dial_pct"] = (val / tgt * 100) if tgt else None
            blk["dial_end"] = stop.isoformat() if stop != end else None
            blk["pace"] = round(val / done * all_) if done else None
            blk["pace_to_target"] = (blk["pace"] / tgt * 100) if tgt and blk["pace"] is not None else None
            return blk
        qtd = period(q0, q_end, _block(d, q0, today, q_t, margin))
        ytd = period(fy0, y_end, _block(d, fy0, today, y_t, margin))
        if margin:
            qtd["np_gl_missing"] = ytd["np_gl_missing"] = mtd["np_gl_missing"]
        qtd["intercompany"] = ic_q.get(r, 0)
        ytd["intercompany"] = ic_y.get(r, 0)
        # last 14 days by day, for the bars
        recent = [{"d": (today - timedelta(days=i)).isoformat(),
                   "v": round(d.get(today - timedelta(days=i), [0])[0])} for i in range(13, -1, -1)]
        out["regions"][r] = {
            "currency": CURRENCY[r],
            "mtd": mtd,
            "qtd": qtd,
            "ytd": ytd,
            "today": round(d.get(today, [0])[0]),
            "intercompany_mtd": ic.get(r, 0),
            "cum": {"this": cum(m0, today.day), "last_month": cum(lm0, lm_days),
                    "last_year": cum(ly0, ly_days), "month_days": month_days,
                    "month_target": round(month_t) if month_t else None},
            "cum_q": cum_q, "cum_y": cum_y,
            "recent": recent,
            # GP rides in the cached lists; drop it here for anyone without margin
            "top": {k: [x if margin else {kk: vv for kk, vv in x.items() if kk != "gp"} for x in v]
                    for k, v in top.get(r, {}).items()},
        }
    out["weekdays"] = {"done": wd_done, "all": wd_all}
    return out
