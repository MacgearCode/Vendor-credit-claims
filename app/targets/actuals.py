"""Actuals for the Targets tool — Macgear's NetSuite sales from the warehouse (no
uploads since the 2026-10-22 rebuild).

Same revenue as the home page and Sales Explorer: reporting.fact_sales_line.net_sales
(account 54, intercompany excluded, ex-GST, the subsidiary's currency), by
    country  'Australia' / 'New Zealand' (the subsidiary's region)
    brand    the NetSuite brand (class); none -> "(no brand)"
    customer the retailer: customer group, else parent, else the customer
    month    fiscal month 0..11 = April..March of the financial year
A "dataset" in the tool is one financial year, identified by its end year (2026 = FY26).
The current year is partial: only COMPLETE months are loaded at all (the open month's
sales wait until it closes), so a projection fills the rest from a reference year.
"""
import os
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .. import warehouse

COUNTRY = {"AU": "Australia", "NZ": "New Zealand"}
COUNTRIES = list(COUNTRY.values())
NO_BRAND = "(no brand)"
TTL = 30 * 60
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def sales_today() -> date:
    """Sydney's date (REPORT_TODAY=YYYY-MM-DD pretends) - same rule as main.sales_today."""
    fake = os.environ.get("REPORT_TODAY", "")
    return date.fromisoformat(fake) if fake else datetime.now(ZoneInfo("Australia/Sydney")).date()


def fy_label(fy: int) -> str:
    return f"FY{fy % 100:02d}"


def fy_bounds(fy: int) -> tuple[date, date]:
    """First day of the year, first day after it: FY26 = 1 Apr 2025 .. 1 Apr 2026."""
    return date(fy - 1, 4, 1), date(fy, 4, 1)


def month_start(fy: int, idx: int) -> date:
    m = 4 + idx
    return date(fy - 1, m, 1) if m <= 12 else date(fy, m - 12, 1)


def current_fy(today: date | None = None) -> int:
    t = today or sales_today()
    return t.year + 1 if t.month >= 4 else t.year


def _first_open(today: date | None = None) -> date:
    """First day of the current (incomplete) month - actuals stop before it."""
    t = today or sales_today()
    return date(t.year, t.month, 1)


def covered(fy: int, today: date | None = None) -> set:
    """Fiscal months of `fy` that are complete (all 12 for a past year, none for a future one)."""
    first_open = _first_open(today)
    return {i for i in range(12) if month_start(fy, i) < first_open}


def _load(fy: int) -> dict:
    """{country: {(brand, customer): [12 monthly $]}} for one financial year."""
    a, b = fy_bounds(fy)
    b = min(b, _first_open())                  # the open month isn't an actual yet
    # Whole months either way (b is a 1st), so the month summary answers it exactly:
    # n_nonzero > 0 keeps the groups the `net_sales <> 0` filter kept.
    fast = """
        select r.country_code, b.brand,
               coalesce(nc.customer_group, nc.parent_name, nc.customer_name, 'Other'),
               make_date(s.month_key / 100, s.month_key %% 100, 1), sum(s.net_sales)
        from reporting.agg_sales_month s
        join reporting.dim_region r on r.region_key = s.region_key
        left join reporting.dim_brand b on b.brand_id = s.brand_id
        left join reporting.dim_ns_customer nc on nc.customer_id = s.customer_id
        where s.month_key >= %(am)s and s.month_key < %(bm)s and s.n_nonzero > 0
        group by 1, 2, 3, 4"""
    rows = warehouse.query_summary("agg_sales_month", fast, """
        select r.country_code, b.brand,
               coalesce(nc.customer_group, nc.parent_name, nc.customer_name, 'Other'),
               date_trunc('month', s.tran_date)::date, sum(s.net_sales)
        from reporting.fact_sales_line s
        join reporting.dim_region r on r.region_key = s.region_key
        left join reporting.dim_brand b on b.brand_id = s.brand_id
        left join reporting.dim_ns_customer nc on nc.customer_id = s.customer_id
        where s.tran_date >= %(a)s and s.tran_date < %(b)s and s.net_sales <> 0
        group by 1, 2, 3, 4""", {"a": a, "b": b, "am": a.year * 100 + a.month,
                                 "bm": b.year * 100 + b.month})
    out: dict = {c: defaultdict(lambda: [0.0] * 12) for c in COUNTRIES}
    for cc, brand, cust, m, amt in rows:
        country = COUNTRY.get(cc)
        if country is None:
            continue
        idx = (m.month - 4) % 12
        out[country][(brand or NO_BRAND, cust)][idx] += float(amt or 0)
    return out


def cells(fy: int | None) -> dict:
    """Cached per year: a closed year never changes, the open one refreshes with sales."""
    if fy is None:
        return {c: {} for c in COUNTRIES}
    return warehouse.cached(("targets_actuals", fy), lambda: _load(fy), ttl=TTL)


def cells_filled(fy: int) -> dict:
    """A year of actuals with its unfinished months taken from the year before - the base
    for planning next year while this one is still running. Equal to cells(fy) once fy is
    complete."""
    done = covered(fy)
    if len(done) == 12:
        return cells(fy)
    cur, prev = cells(fy), cells(fy - 1)
    out: dict = {}
    for country in COUNTRIES:
        keys = set(cur.get(country, {})) | set(prev.get(country, {}))
        c, p = cur.get(country, {}), prev.get(country, {})
        out[country] = {k: [(c.get(k) or [0.0] * 12)[i] if i in done else (p.get(k) or [0.0] * 12)[i]
                            for i in range(12)] for k in keys}
    return out


def years() -> list[dict]:
    """Every financial year with sales, newest first — the "datasets" a scenario can use."""
    def build():
        rows = warehouse.query_summary("agg_sales_month", """
            select case when s.month_key %% 100 >= 4 then s.month_key / 100 + 1
                        else s.month_key / 100 end as fy,
                   r.country_code, sum(s.net_sales)
            from reporting.agg_sales_month s
            join reporting.dim_region r on r.region_key = s.region_key
            where s.n_nonzero > 0 and s.month_key < %(om)s
            group by 1, 2""", """
            select case when extract(month from s.tran_date) >= 4
                        then extract(year from s.tran_date) + 1
                        else extract(year from s.tran_date) end::int as fy,
                   r.country_code, sum(s.net_sales)
            from reporting.fact_sales_line s
            join reporting.dim_region r on r.region_key = s.region_key
            where s.net_sales <> 0 and s.tran_date < %(open)s
            group by 1, 2""", {"open": _first_open(), "om": _first_open().year * 100 + _first_open().month})
        by: dict = {}
        for fy, cc, amt in rows:
            by.setdefault(fy, {})[cc] = float(amt or 0)
        return by
    by = warehouse.cached(("targets_years",), build, ttl=TTL)
    return [meta(fy, by[fy]) for fy in sorted(by, reverse=True)]


def meta(fy: int | None, totals: dict | None = None) -> dict | None:
    """The tool's dataset descriptor for one year (id = the end year)."""
    if fy is None:
        return None
    if totals is None:
        c = cells(fy)
        totals = {cc: sum(sum(v) for v in c[name].values()) for cc, name in COUNTRY.items()}
    months = sorted(covered(fy))
    first, last = month_start(fy, 0), month_start(fy, 11)
    if months and len(months) < 12:
        last = month_start(fy, months[-1])
    return {"id": fy, "name": f"{fy_label(fy)} NetSuite sales", "source_fy": fy_label(fy),
            "period_start": f"{_MON[first.month - 1]} {first.year}",
            "period_end": f"{_MON[last.month - 1]} {last.year}",
            "months_present": months, "is_partial": 0 < len(months) < 12,
            "total_amount": round(sum(totals.values()), 2),
            "totals": {cc: round(v, 2) for cc, v in totals.items()}}
