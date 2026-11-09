"""Inventory Ageing report data — the daily NetSuite "Inventory Aging Report"
history, from reporting.fact_inventory_ageing (Sell Through/db/12_inventory_ageing.sql,
loaded by Sell Through/ageing_intake.py).

Rules (from the Inventory Ageing project; every number on the page follows them):
  - Owned stock only (`is_owned`): warehouse 3PL is third-party stock at $0 and
    is reported apart in units; consignment (AU6 Big W, Consignment NZ) is in no
    figure. The split is by LOCATION — MOVA in Melbourne / Auckland is ours.
  - AU (AUD) and NZ (NZD) are never summed; the page shows one region at a time,
    and only the aged SHARE (a %) puts both on one chart.
  - Bands 0-90 / 91-180 / 181-370 / 370+ are mutually exclusive roll-ups of
    NetSuite's 13 buckets. "Over 90" = the last three; "over 180" = the last two.
  - Age restarts when stock moves location (NetSuite ages from arrival at the
    current location).
  - "Aged since" = the first snapshot of the item's current unbroken run with
    stock past the line (units > 0 — some lines carry units at $0 cost).

The page gets every line (item x location) at the latest snapshot, plus the
lines held 30 days earlier and on the first snapshot (so stock that has since
gone still shows as a fall), and builds its brand -> product -> location tree in
the browser, where the filters and sorting live.
"""
from datetime import date, timedelta

import psycopg

from . import warehouse

TTL = 30 * 60                     # one report a day lands; 30 min is plenty fresh
CCY = {"AU": "AUD", "NZ": "NZD"}

_SCOPE = """
    is_owned and country_code = any(%(regions)s)
    and (%(brands)s::text[] is null or brand = any(%(brands)s))"""

_SERIES_SQL = f"""
    select snapshot_date, country_code,
           sum(v_0_90), sum(v_91_180), sum(v_181_370), sum(v_370_plus),
           sum(u_0_90), sum(u_91_180), sum(u_181_370), sum(u_370_plus)
    from reporting.fact_inventory_ageing
    where {_SCOPE}
    group by 1, 2 order by 1"""

_TPL_SQL = """
    select snapshot_date, country_code, sum(on_hand), count(distinct item_code) filter (where on_hand <> 0)
    from reporting.fact_inventory_ageing
    where is_third_party and country_code = any(%(regions)s)
      and (%(brands)s::text[] is null or brand = any(%(brands)s))
    group by 1, 2"""

_LINES_SQL = f"""
    select snapshot_date, country_code, item_code, max(item_name), max(brand), location_name,
           max(location_type),
           sum(v_0_90), sum(v_91_180), sum(v_181_370), sum(v_370_plus),
           sum(u_0_90), sum(u_91_180), sum(u_181_370), sum(u_370_plus)
    from reporting.fact_inventory_ageing
    where snapshot_date = any(%(dates)s) and {_SCOPE}
    group by snapshot_date, country_code, item_code, location_name"""

# Which items carried stock past each line on each day, for "aged since".
_RUNS_SQL = f"""
    select snapshot_date, country_code, item_code,
           bool_or(u_91_180 + u_181_370 + u_370_plus > 0),
           bool_or(u_181_370 + u_370_plus > 0),
           bool_or(u_370_plus > 0)
    from reporting.fact_inventory_ageing
    where {_SCOPE} and u_91_180 + u_181_370 + u_370_plus > 0
    group by 1, 2, 3"""


class NotLoaded(RuntimeError):
    """reporting.fact_inventory_ageing doesn't exist in this warehouse yet."""


def nearest_on_or_before(dates: list[date], target: date) -> date:
    prior = [d for d in dates if d <= target]
    return prior[-1] if prior else dates[0]


def run_start(dates: list[date], days_with: set) -> str | None:
    """First date of the unbroken run ending at the latest snapshot, or None
    when the latest snapshot doesn't have it. Missing snapshots break a run only
    where there is a snapshot without it — the dates list is what was loaded."""
    if not dates or dates[-1] not in days_with:
        return None
    start = dates[-1]
    for d in reversed(dates):
        if d not in days_with:
            break
        start = d
    return start.isoformat()


def _r2(x) -> float:
    return round(float(x or 0), 2)


def shape(regions, series_rows, tpl_rows, line_rows, run_rows, dates: list[date]) -> dict:
    """Rows from the four queries -> the page's JSON. Pure, so it's testable."""
    latest = dates[-1]
    d30 = nearest_on_or_before(dates, latest - timedelta(days=30))
    first = dates[0]
    idx = {d: i for i, d in enumerate(dates)}

    out = {"dates": [d.isoformat() for d in dates],
           "ref": {"latest": latest.isoformat(), "d30": d30.isoformat(), "first": first.isoformat()},
           "regions": {}, "locs": {}}
    for rg in regions:
        z = [0.0] * len(dates)
        out["regions"][rg] = {
            "ccy": CCY[rg],
            "series": {k: list(z) for k in ("v0", "v1", "v2", "v3", "u0", "u1", "u2", "u3")},
            "tpl": {"units": list(z), "skus": 0},
            "lines": [], "skus": {},
        }

    for d, rg, *vals in series_rows:
        s = out["regions"][rg]["series"]
        for k, v in zip(("v0", "v1", "v2", "v3", "u0", "u1", "u2", "u3"), vals):
            s[k][idx[d]] = _r2(v)
    for d, rg, units, skus in tpl_rows:
        t = out["regions"][rg]["tpl"]
        t["units"][idx[d]] = _r2(units)
        if d == latest:
            t["skus"] = skus

    # item x location: [v0..v3, u0..u3] at latest; over-90 value / units at d30 and first
    lines: dict = {}
    for d, rg, code, name, brand, loc, ltype, *vals in line_rows:
        out["locs"][loc] = ltype
        sk = out["regions"][rg]["skus"]
        if code not in sk or d == latest:
            sk[code] = [name, brand, None, None, None]
        ln = lines.setdefault((rg, code, loc), {"now": [0.0] * 8, "v90_30": 0.0, "u90_30": 0.0, "v90_1": 0.0})
        if d == latest:
            ln["now"] = [_r2(v) for v in vals]
        if d == d30:
            ln["v90_30"], ln["u90_30"] = _r2(sum(vals[1:4])), _r2(sum(vals[5:8]))
        if d == first:
            ln["v90_1"] = _r2(sum(vals[1:4]))
    for (rg, code, loc), ln in sorted(lines.items()):
        out["regions"][rg]["lines"].append([code, loc, *ln["now"], ln["v90_30"], ln["u90_30"], ln["v90_1"]])

    runs: dict = {}
    for d, rg, code, a90, a180, a370 in run_rows:
        r = runs.setdefault((rg, code), (set(), set(), set()))
        if a90:
            r[0].add(d)
        if a180:
            r[1].add(d)
        if a370:
            r[2].add(d)
    for (rg, code), sets in runs.items():
        meta = out["regions"][rg]["skus"].get(code)
        if meta is not None:
            meta[2:5] = [run_start(dates, s) for s in sets]
    return out


def build(brands: list[str] | None, regions: list[str]) -> dict:
    p = {"brands": brands, "regions": list(regions)}
    try:
        with warehouse.connect() as conn, conn.cursor() as cur:
            # The materialized copy of the view (Sell Through/db/13_summaries.sql) when it's
            # fresh - same rows, bands computed once - else the view itself.
            src = ("reporting.agg_inventory_ageing" if "agg_inventory_ageing" in warehouse.fresh_summaries(cur)
                   else "reporting.fact_inventory_ageing")
            q = lambda sql: sql.replace("reporting.fact_inventory_ageing", src)   # noqa: E731
            cur.execute(q("select distinct snapshot_date from reporting.fact_inventory_ageing order by 1"))
            dates = [r[0] for r in cur.fetchall()]
            if not dates:
                raise NotLoaded("no snapshots loaded")
            latest = dates[-1]
            ref = sorted({latest, nearest_on_or_before(dates, latest - timedelta(days=30)), dates[0]})
            cur.execute(q(_SERIES_SQL), p)
            series_rows = cur.fetchall()
            cur.execute(q(_TPL_SQL), p)
            tpl_rows = cur.fetchall()
            cur.execute(q(_LINES_SQL), {**p, "dates": ref})
            line_rows = cur.fetchall()
            cur.execute(q(_RUNS_SQL), p)
            run_rows = cur.fetchall()
    except psycopg.errors.UndefinedTable as e:
        raise NotLoaded("reporting.fact_inventory_ageing isn't in this warehouse yet") from e
    return shape(regions, series_rows, tpl_rows, line_rows, run_rows, dates)


def for_user(brands: list[str] | None, regions: list[str]) -> dict:
    key = ("ageing", tuple(sorted(brands)) if brands is not None else None, tuple(regions))
    return warehouse.cached(key, lambda: build(brands, regions), ttl=TTL)
