"""Sell-through receipt status — which retailers' files for the latest week are
in, for the small "who have we received" strip at the top of the sell-through
pages and the home page.

  expected week = the most recent Sunday before today (Sydney). Files for a week
                  arrive Monday–Wednesday, so on Monday morning nothing is in yet
                  and the strip says so.
  a feed        = retailer x country (dim_customer x region), as loaded.
  expected feeds = feeds that sent any of the 4 weeks before the expected week,
                  so a retailer that stopped long ago (or sends only weeks with a
                  sale, like Woolworths) doesn't sit "missing" forever.
  partial       = some of a feed's usual departments are in, some not — Harvey
                  Norman sends IT and Small Appliances as separate files.
Pre-Macgear Eufy history ('EUFY Historical') is not a feed and is ignored.

Vendors see only the retailers that carried their brands in the last 8 weeks —
whether a retailer's file is in isn't sensitive, but the list of retailers is.
"""
from collections import defaultdict
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from . import warehouse

SYD = ZoneInfo("Australia/Sydney")
LOOKBACK_WEEKS = 4
TTL = 5 * 60                    # files land through the day; keep this fresher
HISTORY_DEPT = "EUFY Historical"


def expected_week(today: date) -> date:
    """Most recent Sunday strictly before `today`."""
    return today - timedelta(days=(today.weekday() + 1) % 7 or 7)


def _key(d: date) -> int:
    return int(d.strftime("%Y%m%d"))


def _feeds(week: date) -> list[dict]:
    lo = week - timedelta(weeks=LOOKBACK_WEEKS)
    region = {v: k for k, v in ((c, warehouse.region_key(c)) for c in ("AU", "NZ"))}
    rows = warehouse.query("""
        select c.customer_name, f.region_key, coalesce(f.customer_department, ''),
               f.date_key, max(f.loaded_at)
        from reporting.fact_sell_through f
        join reporting.dim_customer c on c.customer_key = f.customer_key
        where f.date_key between %(lo)s and %(hi)s
          and f.customer_department is distinct from %(hist)s
        group by 1, 2, 3, 4""", {"lo": _key(lo), "hi": _key(week), "hist": HISTORY_DEPT})
    wk = _key(week)
    feeds: dict = defaultdict(lambda: {"depts": set(), "got": set(), "loaded": None, "last": None})
    for name, rk, dept, dk, loaded in rows:
        f = feeds[(name, region.get(rk, "?"))]
        if dk < wk:
            f["depts"].add(dept)
        else:
            f["got"].add(dept)
            if loaded and (f["loaded"] is None or loaded > f["loaded"]):
                f["loaded"] = loaded
        f["last"] = max(f["last"] or 0, dk)
    out = []
    for (name, region_code), f in feeds.items():
        if not f["depts"] and not f["got"]:
            continue
        if f["got"] and f["depts"] <= f["got"]:
            status = "in"
        elif f["got"]:
            status = "partial"
        else:
            status = "waiting"
        missing = sorted(d for d in f["depts"] - f["got"] if d)
        out.append({
            "retailer": name, "region": region_code, "status": status,
            "missing_parts": missing,
            "loaded": f["loaded"].astimezone(SYD).strftime("%a %d %b %I:%M%p").replace(" 0", " ")
                      .replace("AM", "am").replace("PM", "pm") if f["loaded"] else None,
            "last_week": date(f["last"] // 10000, f["last"] // 100 % 100, f["last"] % 100).isoformat()
                         if f["last"] else None,
        })
    order = {"waiting": 0, "partial": 1, "in": 2}
    out.sort(key=lambda x: (order[x["status"]], x["region"], x["retailer"]))
    return out


def _vendor_retailers(brands: list[str], week: date) -> set:
    """(retailer, region) pairs that carried any of these brands in 8 weeks."""
    region = {v: k for k, v in ((c, warehouse.region_key(c)) for c in ("AU", "NZ"))}
    rows = warehouse.query("""
        select distinct c.customer_name, f.region_key
        from reporting.fact_sell_through f
        join reporting.dim_customer c on c.customer_key = f.customer_key
        where f.date_key between %(lo)s and %(hi)s
          and f.item_key in (select item_key from reporting.dim_item where brand = any(%(brands)s::text[]))""",
        {"lo": _key(week - timedelta(weeks=8)), "hi": _key(week), "brands": brands})
    return {(n, region.get(rk, "?")) for n, rk in rows}


def status(brands, regions: list[str], today: date) -> dict:
    """The strip's data for one user's scope."""
    week = expected_week(today)
    feeds = warehouse.cached(("receipts", week), lambda: _feeds(week), ttl=TTL)
    if brands is not None:
        allowed = warehouse.cached(("receipts_scope", tuple(sorted(brands)), week),
                                   lambda: _vendor_retailers(brands, week), ttl=60 * 60)
        feeds = [f for f in feeds if (f["retailer"], f["region"]) in allowed]
    feeds = [f for f in feeds if f["region"] in regions]
    n_in = sum(f["status"] == "in" for f in feeds)
    return {"week": week.isoformat(), "feeds": feeds, "total": len(feeds), "received": n_in,
            "partial": sum(f["status"] == "partial" for f in feeds),
            "waiting": sum(f["status"] == "waiting" for f in feeds)}
