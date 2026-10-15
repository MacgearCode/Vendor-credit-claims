"""Item Explorer — one product's sell-through, sell-in and stock, plus the
leaderboards on the Items landing page.

Every query takes the user's scope (brands, regions) in SQL; an item outside it
is "not found", never "forbidden", so its existence doesn't leak. Sell-in,
Macgear stock, sell-in $ and margin are separate capabilities (perms.CAP_*) — the caller
decides which blocks to build, and blocks a user may not see are never queried.

Definitions (shared with the warehouse — see Sell Through/CLAUDE.md):
  sell-through units / SOH / on order = retailer files, week ending Sunday
  retail value = units x item-list RRP (the region's own currency)
  sell-in      = net_sales / net_sales_qty: posting account 54, intercompany
                 excluded, ex-GST, subsidiary currency. Weeks end Sunday too.
  est. GP      = net_sales - net_sales_est_cost (NetSuite's estimate, not COGS)
  Macgear stock = latest inventory snapshot, saleable locations only
  weeks of cover = SOH / average weekly units over the last 4 weeks
AU and NZ are never summed: every block is per region.

Pre-Macgear history (customer_department = 'EUFY Historical', the previous
distributor's sell-out) counts as sell-through like everywhere else, but is
also kept as its own series so the page can grey it out and leave it out of
anything that compares against Macgear's own sell-in.
"""
HISTORY_DEPT = "EUFY Historical"
from datetime import date, timedelta

from . import warehouse

LEADER_WEEKS = 13


def _f(x):
    return None if x is None else float(x)


def _scope_sql(alias: str = "i") -> str:
    return f"(%(brands)s::text[] is null or {alias}.brand = any(%(brands)s::text[]))"


# ---- search ------------------------------------------------------------------
def _units_by_item() -> dict:
    """Lifetime sell-through units per item_key — ranks search results so items
    that sell come before dummy packs and displays (which retailers still report
    as stock lines). One grouped query, cached; a per-row subquery cost ~2 s."""
    return warehouse.cached(("units_by_item",), lambda: dict(warehouse.query(
        "select item_key, sum(qty_sold) from reporting.fact_sell_through group by 1")),
        ttl=60 * 60)


def search(q: str, brands, limit: int = 12) -> list[dict]:
    q = (q or "").strip()
    if len(q) < 2 or brands == []:
        return []
    rows = warehouse.query(f"""
        select i.item_key, i.macgear_code, i.description, i.brand, i.category,
               coalesce(i.is_inactive, false) as inactive
        from reporting.dim_item i
        where {_scope_sql()} and i.brand is not null
          and (i.upc = %(q)s or not exists (
                select 1 from unnest(%(words)s::text[]) w
                where not (i.macgear_code ilike '%%' || w || '%%'
                           or coalesce(i.description, '') ilike '%%' || w || '%%')))
        limit 400""", {"brands": brands, "q": q, "words": q.split()})
    units = _units_by_item()
    ql = q.lower()
    rows.sort(key=lambda r: (r[1].lower() != ql, r[5], -(units.get(r[0]) or 0),
                             not r[1].lower().startswith(ql), r[2] or ""))
    return [{"code": r[1], "name": r[2] or r[1], "brand": r[3], "category": r[4],
             "inactive": r[5]} for r in rows[:limit]]


# ---- leaderboards ------------------------------------------------------------
def latest_weeks(n: int) -> list[date]:
    return warehouse.recent_weeks(n)


def leaderboard(brands, region: str, weeks_n: int = LEADER_WEEKS) -> dict:
    """Per item over the last `weeks_n` weeks in one region: weekly units, this
    week's value, latest SOH and cover. Feeds every list on the landing page."""
    weeks = latest_weeks(weeks_n)
    if not weeks or brands == []:
        return {"weeks": [], "items": []}
    # Fact first on its indexed keys, names after: ~0.3 s against ~1.3 s through
    # the dimension joins. Item codes are unique in dim_item, so grouping by
    # item_key is grouping by code.
    rows = warehouse.query(f"""
        with f as (
            select f.item_key, f.date_key, sum(f.qty_sold) as u,
                   sum(f.qty_sold * f.rrp) as v, sum(f.soh) as s
            from reporting.fact_sell_through f
            where f.region_key = %(rk)s and f.date_key = any(%(keys)s)
            group by 1, 2)
        select i.macgear_code, i.description, i.brand, i.category,
               to_date(f.date_key::text, 'YYYYMMDD'), f.u, f.v, f.s
        from f join reporting.dim_item i on i.item_key = f.item_key
        where i.brand is not null and {_scope_sql()}""",
        {"rk": warehouse.region_key(region), "brands": brands,
         "keys": [int(w.strftime("%Y%m%d")) for w in weeks]})
    wi = {w: k for k, w in enumerate(weeks)}
    items: dict = {}
    for code, name, brand, cat, wk, u, v, s in rows:
        it = items.setdefault(code, {"code": code, "name": name or code, "brand": brand,
                                     "category": cat, "units": [0] * len(weeks),
                                     "value": [0.0] * len(weeks), "soh": [None] * len(weeks)})
        k = wi[wk]
        it["units"][k] = int(u or 0)
        it["value"][k] = float(v or 0)
        it["soh"][k] = None if s is None else int(s)
    out = []
    for it in items.values():
        u = it["units"]
        last, prev = u[-1], (u[-2] if len(u) > 1 else 0)
        avg4 = sum(u[-4:]) / min(4, len(u))
        soh = it["soh"][-1]
        out.append({
            "code": it["code"], "name": it["name"], "brand": it["brand"],
            "category": it["category"], "spark": u, "units": last, "prev": prev,
            "change": last - prev,
            "pct": None if not prev else (last - prev) / prev * 100,
            "value": round(it["value"][-1]), "period_units": sum(u),
            "soh": soh, "cover": (soh / avg4) if soh is not None and avg4 > 0 else None,
            "new": sum(u[:-4]) == 0 and sum(u[-4:]) > 0,
        })
    return {"weeks": [w.isoformat() for w in weeks], "items": out}


def leaderboard_for(brands, region):
    key = ("leader", None if brands is None else tuple(sorted(brands)), region)
    return warehouse.cached(key, lambda: leaderboard(brands, region), ttl=warehouse.SELL_THROUGH_TTL)


def lists(board: dict, n: int = 12) -> dict:
    """The landing page's tabs, cut from one leaderboard."""
    items = board["items"]
    selling = [i for i in items if i["units"] > 0]
    risk = [i for i in items if i["cover"] is not None and i["cover"] < 3 and i["units"] >= 5]
    return {
        "top": sorted(selling, key=lambda i: (-i["units"], i["code"]))[:n],
        "value": sorted(selling, key=lambda i: (-i["value"], i["code"]))[:n],
        "risers": sorted([i for i in items if i["change"] > 0 and max(i["units"], i["prev"]) >= 10],
                         key=lambda i: (-i["change"], i["code"]))[:n],
        "fallers": sorted([i for i in items if i["change"] < 0 and max(i["units"], i["prev"]) >= 10],
                          key=lambda i: (i["change"], i["code"]))[:n],
        "risk": sorted(risk, key=lambda i: (i["cover"], -i["units"]))[:n],
        "new": sorted([i for i in items if i["new"]], key=lambda i: (-sum(i["spark"][-4:]), i["code"]))[:n],
    }


# ---- one item ----------------------------------------------------------------
def item_header(code: str, brands) -> dict | None:
    rows = warehouse.query(f"""
        select i.item_key, i.macgear_code, i.description, i.brand, i.category, i.au_status,
               i.nz_status, i.aud_rrp, i.nzd_rrp, i.upc, i.product_manager,
               coalesce(i.is_inactive, false), i.item_type, i.ns_item_id
        from reporting.dim_item i
        where i.macgear_code = %(code)s and i.brand is not null and {_scope_sql()}""",
        {"code": code, "brands": brands})
    if not rows:
        return None
    r = rows[0]
    return {"item_key": r[0], "code": r[1], "name": r[2] or r[1], "brand": r[3],
            "category": r[4], "status": {"AU": r[5], "NZ": r[6]},
            "rrp": {"AU": _f(r[7]), "NZ": _f(r[8])}, "upc": r[9], "pm": r[10],
            "inactive": r[11], "type": r[12], "ns_item_id": r[13]}


def _weeks_back(end: date, n: int) -> list[date]:
    return [end - timedelta(days=7 * k) for k in range(n - 1, -1, -1)]


def item_detail(item_key: int, region: str, weeks_n: int, caps: set,
                ns_item_id: int | None = None) -> dict:
    """Everything the item page draws for one region. `caps` = the user's
    capabilities; blocks outside them are left out entirely."""
    st_weeks = latest_weeks(1)
    end = st_weeks[-1] if st_weeks else date.today()
    weeks = _weeks_back(end, weeks_n)
    p = {"item": item_key, "region": region, "weeks": weeks, "hist": HISTORY_DEPT,
         "money": "sell_in_value" in caps, "ns": ns_item_id,
         "start": weeks[0] - timedelta(days=6), "end": end,
         "start_key": int((weeks[0] - timedelta(days=6)).strftime("%Y%m%d"))}

    with warehouse.connect() as conn, conn.cursor() as cur:
        # sell-through by retailer x week (retailer is the finest grain we chart)
        cur.execute("""
            select c.customer_name, d.full_date, sum(f.qty_sold), sum(f.qty_sold * f.rrp),
                   sum(f.soh), sum(f.on_order), count(distinct f.store_name),
                   coalesce(sum(f.qty_sold) filter (where f.customer_department = %(hist)s), 0)
            from reporting.fact_sell_through f
            join reporting.dim_customer c on c.customer_key = f.customer_key
            join reporting.dim_region   r on r.region_key   = f.region_key
            join reporting.dim_date     d on d.date_key     = f.date_key
            where f.item_key = %(item)s and r.country_code = %(region)s
              and d.full_date = any(%(weeks)s)
            group by 1, 2""", p)
        st_rows = cur.fetchall()

        # top stores, last 4 weeks (store-level data may be shared with vendors)
        cur.execute("""
            select c.customer_name, f.store_name, sum(f.qty_sold),
                   sum(f.soh) filter (where d.full_date = %(end)s)
            from reporting.fact_sell_through f
            join reporting.dim_customer c on c.customer_key = f.customer_key
            join reporting.dim_region   r on r.region_key   = f.region_key
            join reporting.dim_date     d on d.date_key     = f.date_key
            where f.item_key = %(item)s and r.country_code = %(region)s
              and f.store_name is not null and d.full_date > %(end)s - 28 and d.full_date <= %(end)s
            group by 1, 2 having sum(f.qty_sold) > 0
            order by 3 desc limit 10""", p)
        stores = [{"retailer": a, "store": b, "units": int(u or 0), "soh": None if s is None else int(s)}
                  for a, b, u, s in cur.fetchall()]

        si_rows, inv, inv_total = [], [], None
        if "sell_in" in caps:
            # sell-in by retailer x week; the current (part) week included
            cur.execute("""
                select coalesce(c.customer_name, nc.customer_group, 'Other'), d.week_ending_date,
                       sum(s.net_sales_qty),
                       -- $ only when the user may see it: never leaves the database otherwise
                       case when %(money)s then sum(s.net_sales) end,
                       case when %(money)s then sum(s.net_sales_est_cost) end
                from reporting.fact_sales_line s
                join reporting.dim_date d on d.date_key = s.date_key
                join reporting.dim_region r on r.region_key = s.region_key
                left join reporting.dim_customer c on c.customer_key = s.customer_key
                left join reporting.dim_ns_customer nc on nc.customer_id = s.customer_id
                where s.item_key = %(item)s and r.country_code = %(region)s
                  and s.net_sales is not null and s.date_key >= %(start_key)s
                  -- the view derives item_key through a mapping, so this filter
                  -- alone can't use the raw table's index; the NetSuite id can (2x)
                  and (%(ns)s::int is null or s.ns_item_id = %(ns)s::int)
                group by 1, 2""", p)
            si_rows = cur.fetchall()

            cur.execute("""
                select l.location, sum(f.on_hand), sum(f.available), sum(f.committed),
                       sum(f.on_order), sum(f.in_transit), max(d.full_date)
                from reporting.fact_inventory_daily f
                join reporting.dim_location l on l.location_id = f.location_id
                join reporting.dim_region   r on r.region_key   = f.region_key
                join reporting.dim_date     d on d.date_key     = f.date_key
                where f.is_latest and f.item_key = %(item)s and r.country_code = %(region)s
                  and f.stock_class = 'Saleable'
                group by 1 order by 2 desc""", p)
            for loc, oh, av, cm, oo, it, asat in cur.fetchall():
                inv.append({"location": loc.replace("–", "-"), "on_hand": int(oh or 0),
                            "available": int(av or 0), "committed": int(cm or 0),
                            "on_order": int(oo or 0), "in_transit": int(it or 0)})
                inv_total = asat
    return _shape(weeks, st_rows, si_rows, stores, inv, inv_total, caps)


def _shape(weeks, st_rows, si_rows, stores, inv, inv_asat, caps) -> dict:
    n = len(weeks)
    wi = {w: k for k, w in enumerate(weeks)}

    def blank():
        return {"u": [0] * n, "v": [0.0] * n, "s": [None] * n, "o": [None] * n, "h": [0] * n,
                "stores": 0, "si_u": [0] * n, "si_v": [0.0] * n, "si_c": [0.0] * n}

    by_ret: dict = {}
    for name, wk, u, v, s, o, ns, hist in st_rows:
        if wk not in wi:
            continue
        b = by_ret.setdefault(name, blank())
        k = wi[wk]
        b["u"][k] += int(u or 0)
        b["h"][k] += int(hist or 0)
        b["v"][k] += float(v or 0)
        if s is not None:
            b["s"][k] = (b["s"][k] or 0) + int(s)
        if o is not None:
            b["o"][k] = (b["o"][k] or 0) + int(o)
        if k == n - 1:
            b["stores"] = int(ns or 0)

    # The current part-week of sell-in sits one past the last sell-through week.
    si_extra = {"u": 0, "v": 0.0, "c": 0.0, "week": None}
    for name, wk, u, v, c in si_rows:
        if wk in wi:
            b = by_ret.setdefault(name, blank())
            k = wi[wk]
            b["si_u"][k] += float(u or 0)
            b["si_v"][k] += float(v or 0)
            b["si_c"][k] += float(c or 0)
        elif wk > weeks[-1]:
            si_extra["u"] += float(u or 0)
            si_extra["v"] += float(v or 0)
            si_extra["c"] += float(c or 0)
            si_extra["week"] = wk.isoformat()

    def total(key):
        out = [0] * n
        for b in by_ret.values():
            for k in range(n):
                out[k] += b[key][k]
        return out

    def total_nullable(key):
        out = [None] * n
        for b in by_ret.values():
            for k in range(n):
                if b[key][k] is not None:
                    out[k] = (out[k] or 0) + b[key][k]
        return out

    series = {"st_units": total("u"), "st_hist": total("h"),
              "st_value": [round(x) for x in total("v")],
              "soh": total_nullable("s"), "on_order": total_nullable("o")}
    if "sell_in" in caps:
        series["si_units"] = [round(x) for x in total("si_u")]
        if "sell_in_value" in caps:
            series["si_value"] = [round(x) for x in total("si_v")]
        if "margin" in caps:
            series["si_gp"] = [round(v - c) for v, c in zip(total("si_v"), total("si_c"))]

    def cover(units, soh):
        if soh is None:
            return None
        avg = sum(units[-4:]) / min(4, n)
        return soh / avg if avg > 0 else None

    retailers = []
    for name, b in by_ret.items():
        row = {"name": name, "spark": b["u"][-13:], "units": sum(b["u"]),
               "last": b["u"][-1], "prev": b["u"][-2] if n > 1 else 0,
               "value": round(sum(b["v"])), "soh": b["s"][-1], "on_order": b["o"][-1],
               "cover": cover(b["u"], b["s"][-1]), "stores": b["stores"]}
        if "sell_in" in caps:
            row["si_units"] = round(sum(b["si_u"]))
            if "sell_in_value" in caps:
                row["si_value"] = round(sum(b["si_v"]))
        if row["units"] or row.get("si_units") or row["soh"]:
            retailers.append(row)
    retailers.sort(key=lambda r: (-r["units"], -(r.get("si_units") or 0), r["name"]))

    st_u = series["st_units"]
    kpis = {
        "st_units": sum(st_u), "st_value": round(sum(total("v"))),
        "last_week": st_u[-1], "prev_week": st_u[-2] if n > 1 else 0,
        "soh": series["soh"][-1], "on_order": series["on_order"][-1],
        "cover": cover(st_u, series["soh"][-1]),
        "accounts": sum(1 for r in retailers if r["last"] > 0),
        "hist_units": sum(series["st_hist"]),
        "stores": sum(b["stores"] for b in by_ret.values()),
    }
    out = {"weeks": [w.isoformat() for w in weeks], "series": series,
           "retailers": retailers, "stores": stores, "kpis": kpis}
    if "sell_in" in caps:
        # totals from the unrounded weekly sums — adding rounded weeks drifts by dollars
        si_units, si_value = round(sum(total("si_u"))), round(sum(total("si_v")))
        # In vs out only over the weeks since sell-in started in this window, and
        # Macgear-era sell-through only — the previous distributor's history and
        # the weeks before Macgear sold the item would make the ratio meaningless.
        first = next((k for k, x in enumerate(series["si_units"]) if x), None)
        st_mg = [u - h for u, h in zip(st_u, series["st_hist"])]
        st_since = sum(st_mg[first:]) if first is not None else 0
        if "sell_in_value" in caps:
            kpis["si_value"] = si_value
        kpis.update({"si_units": si_units,
                     "ratio_from": weeks[first].isoformat() if first is not None else None,
                     "st_since": st_since,
                     "sell_ratio": (st_since / si_units) if si_units > 0 else None})
        out["inventory"] = {"rows": inv, "as_at": inv_asat.isoformat() if inv_asat else None,
                            "on_hand": sum(r["on_hand"] for r in inv),
                            "available": sum(r["available"] for r in inv),
                            "on_order": sum(r["on_order"] for r in inv),
                            "in_transit": sum(r["in_transit"] for r in inv)}
        # Macgear's own stock drains at the SELL-IN rate (what retailers order), not sell-through
        si_u = series["si_units"]
        avg_si4 = sum(si_u[-4:]) / min(4, n) if n else 0
        out["inventory"]["weeks_cover"] = (out["inventory"]["available"] / avg_si4) if avg_si4 > 0 else None
        out["sell_in_wtd"] = si_extra if si_extra["week"] else None
        if "margin" in caps:
            gp = round(sum(total("si_v")) - sum(total("si_c")))
            kpis["gp"] = gp
            kpis["gp_pct"] = (gp / sum(total("si_v")) * 100) if si_value else None
            if si_extra["week"]:
                si_extra["gp"] = si_extra["v"] - si_extra["c"]
        si_extra.pop("c", None)
        if "sell_in_value" not in caps:
            si_extra.pop("v", None)
    return out


def item_for(code: str, brands, region: str, weeks_n: int, caps: set):
    """(header, detail) or (None, None) when the item is outside the scope."""
    hkey = ("item_hdr", code, None if brands is None else tuple(sorted(brands)))
    header = warehouse.cached(hkey, lambda: item_header(code, brands))
    if header is None:
        return None, None
    dkey = ("item", header["item_key"], region, weeks_n, tuple(sorted(caps)))
    detail = warehouse.cached(dkey, lambda: item_detail(header["item_key"], region, weeks_n, caps,
                                                        header.get("ns_item_id")))
    return header, detail


def ranks(code: str, brand: str, board: dict) -> dict:
    """This week's unit rank, overall in scope and within the brand."""
    selling = sorted([i for i in board["items"] if i["units"] > 0],
                     key=lambda i: (-i["units"], i["code"]))
    overall = next((k + 1 for k, i in enumerate(selling) if i["code"] == code), None)
    in_brand = [i for i in selling if i["brand"] == brand]
    brand_rank = next((k + 1 for k, i in enumerate(in_brand) if i["code"] == code), None)
    return {"overall": overall, "of": len(selling), "brand": brand_rank, "brand_of": len(in_brand)}

