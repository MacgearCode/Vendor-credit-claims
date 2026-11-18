"""Open Orders — what customers have ordered from Macgear and the warehouse has
not shipped yet, from reporting.fact_open_so (Sell Through/db/15_open_so.sql,
the Zone export DW_open_sales_orders, a whole snapshot every 30 min).

Rules (user, 2026-11-18; the view carries them):
  - One row per open sales-order line. open_qty = ordered - fulfilled; open value =
    open_qty x the line's rate, ex GST, in the order's currency (AUD / NZD). AU and NZ
    are never summed - the page shows one region at a time.
  - order_class: 'customer' = real customer demand (the headline figures);
    'holding' = customer 6870 "Holding Orders", forward / placeholder orders for
    retailers, shown APART; 'intercompany' = AU -> NZ, 'internal' = samples. The page
    can add the last three in, never silently.
  - A "picking line" = one open order line (what the warehouse has to pick).
  - Ship Date is mostly NetSuite's default rolling forward, so it is shown, never
    used to call an order late; age = days since the order date.
  - Retailer = Sales Explorer's: customer group, else the head office, else the account.
  - $ only with the sell_in_value capability: without it the SQL selects NULL, so the
    amounts never leave the warehouse.
Brand scope = the line's brand (NetSuite class), else the item's brand.
"""
from datetime import date

import psycopg

from . import warehouse

TTL = 10 * 60                  # the export lands every 30 min
CLASSES = ("customer", "holding", "intercompany", "internal")

_BASE = """
    from reporting.fact_open_so o
    left join reporting.dim_brand b on b.brand_id = o.brand_id
    left join reporting.dim_item i on i.item_key = o.item_key
    left join reporting.dim_ns_customer c on c.customer_id = o.customer_id
    where o.country_code = any(%(regions)s)
      and (%(brands)s::text[] is null or coalesce(b.brand, i.brand) = any(%(brands)s))"""

# One row per order line, at the grain the page aggregates from.
_LINES_SQL = """
    select o.country_code, o.document_number, o.tran_date, o.status, o.order_class,
           coalesce(c.customer_group, c.parent_name, c.customer_name, o.customer_name,
                    'Customer ' || o.customer_id) as retailer,
           coalesce(c.customer_name, o.customer_name, 'Customer ' || o.customer_id) as account,
           coalesce(b.brand, i.brand, '(no brand)') as brand,
           coalesce(o.item_code, i.macgear_code, o.ns_item_id::text) as item_code,
           i.description, o.open_qty, o.quantity_committed, o.quantity_backordered,
           case when %(money)s then o.open_amount end,
           case when %(money)s then round(o.quantity_committed * coalesce(o.rate, 0), 2) end,
           o.ship_date, o.po_number""" + _BASE

_FRESH_SQL = "select max(loaded_from_export_at), max(last_modified) from reporting.fact_open_so"


class NotLoaded(RuntimeError):
    pass


def _f(x) -> float | None:
    return None if x is None else round(float(x), 2)


def shape(rows, today: date, money: bool, fresh=None) -> dict:
    """Rows -> the page payload: per region, orders (one entry per order) and the
    item lines inside them, both carrying their class so the browser can include or
    exclude holding / intercompany / internal."""
    out = {"money": money, "today": today.isoformat(), "as_at": fresh, "regions": {}}
    for (rg, doc, d, status, cls, retailer, account, brand, code, desc, qty, committed,
         backordered, amount, ready_amt, ship, po) in rows:
        R = out["regions"].setdefault(rg, {"orders": {}, "lines": [], "items": [], "_ix": {}})
        o = R["orders"].get(doc)
        if o is None:
            o = R["orders"][doc] = {"doc": doc, "date": d.isoformat(), "age": (today - d).days,
                                    "status": status, "cls": cls, "retailer": retailer,
                                    "account": account, "po": po or "", "lines": 0, "units": 0.0,
                                    "value": 0.0 if money else None, "ship": None,
                                    "r_lines": 0, "r_units": 0.0, "r_value": 0.0 if money else None}
        o["lines"] += 1
        o["units"] += float(qty)
        if committed:                       # committed = allocated from stock: pickable / invoiceable now
            o["r_lines"] += 1
            o["r_units"] += float(committed)
        if money:
            o["value"] += float(amount or 0)
            o["r_value"] += float(ready_amt or 0)
        if ship and (o["ship"] is None or ship.isoformat() < o["ship"]):
            o["ship"] = ship.isoformat()
        ix = R["_ix"].get(code)
        if ix is None:                      # items listed once; lines point at them
            ix = R["_ix"][code] = len(R["items"])
            R["items"].append([code, desc or "", brand])
        R["lines"].append([doc, ix, float(qty), float(committed or 0), float(backordered or 0),
                           _f(amount) if money else None, _f(ready_amt) if money else None])
    for R in out["regions"].values():
        del R["_ix"]
        orders = sorted(R["orders"].values(), key=lambda o: (o["date"], o["doc"]))
        for o in orders:
            o["units"] = round(o["units"], 4)
            o["r_units"] = round(o["r_units"], 4)
            if money:
                o["value"] = round(o["value"], 2)
                o["r_value"] = round(o["r_value"], 2)
        R["orders"] = orders
    return out


def build(brands, regions, money: bool) -> dict:
    params = {"regions": list(regions), "brands": None if brands is None else list(brands),
              "money": money}
    try:
        with warehouse.connect() as conn, conn.cursor() as cur:
            cur.execute(_LINES_SQL, params)
            rows = cur.fetchall()
            cur.execute(_FRESH_SQL)
            loaded, modified = cur.fetchone()
    except psycopg.errors.UndefinedTable as e:
        raise NotLoaded("reporting.fact_open_so isn't in this warehouse yet") from e
    fresh = {"loaded": loaded.isoformat() if loaded else None,
             "modified": modified.isoformat() if modified else None}
    return shape(rows, date.today(), money, fresh)


def for_user(brands, regions, money: bool) -> dict:
    key = ("open_orders", None if brands is None else tuple(sorted(brands)), tuple(regions), money)
    return warehouse.cached(key, lambda: build(brands, regions, money), ttl=TTL)


def summary(data: dict) -> dict:
    """Home-page figures per region: customer demand only, holding orders apart."""
    out = {}
    for rg, R in data["regions"].items():
        cust = [o for o in R["orders"] if o["cls"] == "customer"]
        hold = [o for o in R["orders"] if o["cls"] == "holding"]
        lines = sum(o["lines"] for o in cust)
        m = data["money"]
        out[rg] = {
            "orders": len(cust), "lines": lines, "units": round(sum(o["units"] for o in cust)),
            "value": round(sum(o["value"] for o in cust), 2) if m else None,
            "ready_value": round(sum(o["r_value"] for o in cust), 2) if m else None,
            "ready_lines": sum(o["r_lines"] for o in cust),
            "ready_units": round(sum(o["r_units"] for o in cust)),
            "over_7": sum(1 for o in cust if o["age"] > 7),
            "over_30": sum(1 for o in cust if o["age"] > 30),
            "holding_value": round(sum(o["value"] for o in hold), 2) if data["money"] else None,
            "holding_units": round(sum(o["units"] for o in hold)),
            "holding_ready_value": round(sum(o["r_value"] for o in hold), 2) if m else None,
            "holding_ready_units": round(sum(o["r_units"] for o in hold)),
            "top": [{"name": n, "value": v, "lines": l, "ready_value": rv, "ready_lines": rl}
                    for n, v, l, rv, rl in _top(cust, m)[:5]],
        }
    return out


def _top(orders, money: bool):
    agg = {}
    for o in orders:
        a = agg.setdefault(o["retailer"], [0.0, 0, 0.0, 0])
        if money:
            a[0] += o["value"] or 0
            a[2] += o["r_value"] or 0
        a[1] += o["lines"]
        a[3] += o["r_lines"]
    key = (lambda kv: -kv[1][0]) if money else (lambda kv: -kv[1][1])
    return [(n, round(v[0], 2) if money else None, v[1], round(v[2], 2) if money else None, v[3])
            for n, v in sorted(agg.items(), key=key)]


def item_open(data: dict, code: str, region: str) -> dict:
    """One item's open order lines in one region, by account, for the Item Explorer."""
    R = data["regions"].get(region) or {"orders": [], "lines": [], "items": []}
    orders = {o["doc"]: o for o in R["orders"]}
    rows = []
    want = {i for i, it in enumerate(R["items"]) if it[0].upper() == code.upper()}
    for doc, ix, qty, committed, backordered, amount, ready_amt in R["lines"]:
        if ix not in want:
            continue
        o = orders[doc]
        rows.append({"doc": doc, "date": o["date"], "age": o["age"], "status": o["status"],
                     "cls": o["cls"], "retailer": o["retailer"], "account": o["account"],
                     "qty": qty, "committed": committed, "backordered": backordered,
                     "value": amount, "ready_value": ready_amt, "ship": o["ship"]})
    rows.sort(key=lambda r: (r["date"], r["doc"]))
    cust = [r for r in rows if r["cls"] == "customer"]
    return {"rows": rows, "money": data["money"],
            "qty": sum(r["qty"] for r in cust), "committed": sum(r["committed"] for r in cust),
            "backordered": sum(r["backordered"] for r in cust),
            "value": round(sum(r["value"] or 0 for r in cust), 2) if data["money"] else None,
            "ready_value": round(sum(r["ready_value"] or 0 for r in cust), 2) if data["money"] else None,
            "orders": len({r["doc"] for r in cust}),
            "holding": sum(r["qty"] for r in rows if r["cls"] == "holding")}
