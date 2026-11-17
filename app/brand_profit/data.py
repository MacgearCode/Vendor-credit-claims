"""Brand Profitability — data: NetSuite sales from the warehouse, freight / vendor-rebate GL
lines, the settings, and the report built from them.

Sales lines (checked to the cent against the CYA workbook, Apr–Oct 2026, 2026-11-17):
  reporting.fact_sales_line, the region's subsidiary, posting ACCOUNTS below, by transaction
  month. The workbook also carried AU->NZ intercompany; the site EXCLUDES it (warehouse rule,
  user 2026-11-17) and reports the amount left out so the page still ties to the Excel.
  Head office = dim_ns_customer.parent_id, store = the line's own customer, item category =
  dim_item.category. Brand = the line's NetSuite class (what the CYA used), else the item's.
  KNOWN GAP (2026-11-17): generic non-stock items ("Sales", "Sales short supply & damage",
  "Lost in Transit", freight credits) carry their brand only in the line's class, which the
  DW_fact_sales export leaves blank — 158 credit memos, -$134k Apr-Oct, land under "- No Brand -"
  where the CYA has the brand. Every document still nets to the same total; fixed once the
  export's brandkey is the line class.

The warehouse rows are grouped by every key a rule can look at (month, head office, store,
brand, item category, item code), so one FY is some tens of thousands of rows, cached per region x FY and
expired by the sales watch (prefix "bp_lines" in sales.SALES_CACHE_PREFIXES).

Freight / vendor rebates, per region x month, first source that has the month wins:
  1. reporting.fact_gl_cost — the NetSuite GL extract once it exists (Zone -> S3 -> Windmill;
     columns month, subsidiary_id, brand, kind, amount, document_number, memo, vendor, account);
  2. brand_profit.gl_line rows not superseded (seeded from the workbook, or uploaded).
"""
from datetime import date

from .. import warehouse
from . import calc, db

SUBSIDIARY = {"AU": 2, "NZ": 3}
# Posting accounts the CYA's sales lines come from: 54 Sales, 108 Freight From Customers,
# 957 Sales short supply & damage, 866 / 867 / 869 / 902 3PL income (Spacewalker), 836 Courier
# Claims (NZ) (the NZ CYA carries it; AU has none in FY27).
ACCOUNTS = [54, 108, 957, 866, 867, 869, 902, 836]
INTERCOMPANY = (6871, 9459)          # MacGear AU -> MacGear NZ (same rule as the warehouse views)
LINES_TTL = 30 * 60


def _key(fy: int) -> tuple[int, int]:
    first = calc.fy_months(fy)[0]
    return int(first.strftime("%Y%m%d")), int(date(fy, 3, 31).strftime("%Y%m%d"))


def _build_lines(region: str, fy: int) -> dict:
    lo, hi = _key(fy)
    rows = warehouse.query("""
        select date_trunc('month', f.tran_date)::date as m, c.parent_id, f.customer_id,
               upper(coalesce(nullif(b.brand, ''), i.brand, '')) as brand,
               coalesce(i.category, '') as category,
               upper(coalesce(i.macgear_code, '')) as item,
               (f.customer_id = any(%s)) as ic,
               sum(f.quantity), sum(f.amount), sum(coalesce(f.est_cost, 0)), count(*)
        from reporting.fact_sales_line f
        left join reporting.dim_item i on i.item_key = f.item_key
        left join reporting.dim_brand b on b.brand_id = f.brand_id
        left join reporting.dim_ns_customer c on c.customer_id = f.customer_id
        where f.subsidiary_id = %s and f.date_key between %s and %s and f.account_id = any(%s)
        group by 1, 2, 3, 4, 5, 6, 7""", (list(INTERCOMPANY), SUBSIDIARY[region], lo, hi, ACCOUNTS))
    sales, ic = [], {}
    for m, parent, cust, brand, cat, item, is_ic, q, amt, cost, n in rows:
        if is_ic:
            ic[m] = ic.get(m, 0.0) + float(amt)
            continue
        sales.append({"month": m, "head_office": parent, "store": cust, "brand": brand or "",
                      "category": cat, "item": item, "qty": float(q), "gross": float(amt), "cogs": float(cost)})
    parents = {r["head_office"] for r in sales}
    names = {}
    if parents:
        for cid, name in warehouse.query(
                "select customer_id, customer_name from reporting.dim_ns_customer where customer_id = any(%s)",
                (sorted(p for p in parents if p is not None),)):
            names[cid] = name
    return {"sales": sales, "intercompany": ic, "names": names,
            "months_with_sales": sorted({r["month"] for r in sales})}


def sales_lines(region: str, fy: int) -> dict:
    return warehouse.cached(("bp_lines", region, fy), lambda: _build_lines(region, fy), ttl=LINES_TTL)


def _netsuite_gl(region: str, fy: int) -> list[dict]:
    """The NetSuite GL extract, if the warehouse has it yet (else [])."""
    if not warehouse.query("select to_regclass('reporting.fact_gl_cost') is not null")[0][0]:
        return []
    first, last = calc.fy_months(fy)[0], calc.fy_months(fy)[-1]
    return [{"month": m, "brand": b or "", "kind": k, "amount": float(a), "document_number": d,
             "memo": memo, "vendor": v, "account": acc, "source": "netsuite"}
            for m, b, k, a, d, memo, v, acc in warehouse.query(
                "select month, upper(coalesce(brand, '')), kind, amount, document_number, memo, vendor, account "
                "from reporting.fact_gl_cost where subsidiary_id = %s and month between %s and %s",
                (SUBSIDIARY[region], first, last))]


def gl_lines(conn, region: str, fy: int) -> tuple[list[dict], dict]:
    """(lines in force, {month: source}) — per month the NetSuite extract wins over local lines."""
    first, last = calc.fy_months(fy)[0], calc.fy_months(fy)[-1]
    ns = _netsuite_gl(region, fy)
    ns_months = {g["month"] for g in ns}
    local = conn.execute(
        "SELECT l.*, b.source FROM gl_line l JOIN gl_batch b ON b.id = l.batch_id "
        "WHERE l.region = %s AND l.month BETWEEN %s AND %s AND l.superseded_by IS NULL",
        (region, first, last)).fetchall()
    lines = list(ns)
    source = {m: "netsuite" for m in ns_months}
    for r in local:
        if r["month"] in ns_months:
            continue
        lines.append({"month": r["month"], "brand": r["brand"], "kind": r["kind"],
                      "amount": float(r["amount"]), "document_number": r["document_number"],
                      "memo": r["memo"], "vendor": r["vendor"], "account": r["account"],
                      "source": r["source"]})
        source.setdefault(r["month"], r["source"])
    return lines, source


# ---- periods -----------------------------------------------------------------------
def period_months(fy: int, period: str) -> tuple[list[date], str]:
    """'m4'..'m3' style is awkward in URLs; periods are 'YYYY-MM' (one month), 'Q1'..'Q4'
    (FY quarter), 'YTD:YYYY-MM' (Apr to that month) or 'FY'."""
    months = calc.fy_months(fy)
    if period == "FY":
        return months, f"FY{fy % 100:02d}"
    if period in ("Q1", "Q2", "Q3", "Q4"):
        q = int(period[1])
        return months[(q - 1) * 3:q * 3], f"FY{fy % 100:02d} {period}"
    if period.startswith("YTD:"):
        upto = date.fromisoformat(period[4:] + "-01")
        sel = [m for m in months if m <= upto]
        return sel, f"FY{fy % 100:02d} YTD to {upto:%b %Y}"
    m = date.fromisoformat(period + "-01")
    if m not in months:
        raise ValueError("month outside the financial year")
    return [m], f"{m:%B %Y}"


def default_period(fy_lines: dict, today: date) -> str:
    """The latest complete month with sales, else the latest month with sales."""
    have = fy_lines["months_with_sales"]
    if not have:
        return "FY"
    this_month = date(today.year, today.month, 1)
    done = [m for m in have if m < this_month]
    return (done or have)[-1].strftime("%Y-%m")


# ---- the report --------------------------------------------------------------------
def _flatten(root: calc.Totals) -> list[dict]:
    out = []
    for pm in sorted(root.children, key=lambda k: -root.children[k].gross):
        p = root.children[pm]
        out.append({"level": 0, "label": pm, **p.as_dict()})
        for brand in sorted(p.children, key=lambda k: -p.children[k].gross):
            b = p.children[brand]
            out.append({"level": 1, "label": brand or calc.NO_BRAND_LABEL, **b.as_dict()})
            for cust in sorted(b.children, key=lambda k: (k.startswith("Freight & vendor"), -b.children[k].gross)):
                out.append({"level": 2, "label": cust, **b.children[cust].as_dict()})
    return out


def report(region: str, fy: int, period: str, brands: list[str] | None = None, pm: str = "") -> dict:
    """brands: the viewer's brand scope (None = all). Out-of-scope brands are left out of every
    figure, totals included. pm: show only that PM's portfolio (tiles and total follow it)."""
    lines = sales_lines(region, fy)
    months, label = period_months(fy, period)
    with db.connect() as conn:
        rates = calc.Rates(db.load_rules(conn, region), db.load_pms(conn, region))
        gl, gl_source = gl_lines(conn, region, fy)
        has_rules = bool(conn.execute("SELECT 1 FROM rate_rule WHERE region = %s LIMIT 1", (region,)).fetchone())
        rules_from = conn.execute("SELECT least((SELECT min(valid_from) FROM rate_rule WHERE region = %s), "
                                  "(SELECT min(valid_from) FROM brand_pm WHERE region = %s)) AS f",
                                  (region, region)).fetchone()["f"]
    sales = lines["sales"]
    if brands is not None:
        keep = {calc.norm_brand(b) for b in brands}
        sales = [r for r in sales if calc.norm_brand(r["brand"]) in keep]
        gl = [g for g in gl if calc.norm_brand(g["brand"]) in keep]
    root = calc.build_tree(rates, sales, gl, set(months), lines["names"])
    pms = sorted(root.children, key=lambda k: (k == calc.NO_PM, k))
    if pm and pm in root.children:
        only = calc.Totals()
        only.add(root.children[pm])
        only.children = {pm: root.children[pm]}
        root = only
    else:
        pm = ""
    return {
        "pm": pm, "pms": pms,
        "region": region, "fy": fy, "period": period, "label": label,
        "months": [m.isoformat() for m in months],
        "total": root.as_dict(), "rows": _flatten(root),
        "intercompany": round(sum(lines["intercompany"].get(m, 0.0) for m in months), 2),
        "gl_source": {m.isoformat(): gl_source.get(m, "") for m in months},
        "gl_missing": [f"{m:%b %Y}" for m in months if m in set(lines["months_with_sales"]) and m not in gl_source],
        "fy_months": [m.isoformat() for m in calc.fy_months(fy)],
        "months_with_sales": [m.isoformat() for m in lines["months_with_sales"]],
        "has_rules": has_rules,
        # months picked that start before any setting: rebates, MDF, targets and PMs are blank there
        "before_settings": f"{rules_from:%B %Y}" if rules_from and months and months[0] < rules_from else "",
    }


def pickers(region: str) -> dict:
    """Head offices, stores, brands and item categories for the settings forms (cached)."""
    def build():
        sub = SUBSIDIARY[region]
        heads = warehouse.query("select customer_id, customer_name from reporting.dim_ns_customer "
                                "where subsidiary_id = %s and customer_id = parent_id order by customer_name", (sub,))
        stores = warehouse.query("select c.customer_id, c.customer_name, p.customer_name from reporting.dim_ns_customer c "
                                 "join reporting.dim_ns_customer p on p.customer_id = c.parent_id "
                                 "where c.subsidiary_id = %s and c.customer_id <> c.parent_id "
                                 "order by p.customer_name, c.customer_name", (sub,))
        brands = [r[0] for r in warehouse.query(
            "select distinct upper(brand) from reporting.dim_brand where brand is not null order by 1")]
        cats = [r[0] for r in warehouse.query(
            "select distinct category from reporting.dim_item where coalesce(category, '') <> '' order by 1")]
        return {"heads": [f"{n} · {i}" for i, n in heads],
                "stores": [f"{n} · {i}" for i, n, _ in stores],
                "brands": brands, "categories": cats}
    return warehouse.cached(("bp_pick", region), build, ttl=LINES_TTL)


def settings_view(region: str) -> dict:
    with db.connect() as conn:
        rules = conn.execute("SELECT * FROM rate_rule WHERE region = %s "
                             "ORDER BY kind, customer_name, brand, item_category, valid_from", (region,)).fetchall()
        pms = conn.execute("SELECT * FROM brand_pm WHERE region = %s ORDER BY brand, valid_from", (region,)).fetchall()
        accounts = conn.execute("SELECT * FROM gl_account WHERE region = %s ORDER BY kind, account", (region,)).fetchall()
        batches = conn.execute("""
            SELECT b.*, count(l.id) AS lines,
                   count(l.id) FILTER (WHERE l.superseded_by IS NULL) AS in_force,
                   min(l.month) AS first_month, max(l.month) AS last_month,
                   coalesce(sum(l.amount) FILTER (WHERE l.kind = 'freight'), 0) AS freight,
                   coalesce(sum(l.amount) FILTER (WHERE l.kind = 'vr'), 0) AS vr
            FROM gl_batch b LEFT JOIN gl_line l ON l.batch_id = b.id
            WHERE b.region = %s GROUP BY b.id ORDER BY b.id DESC""", (region,)).fetchall()
        log = conn.execute("SELECT * FROM change_log ORDER BY id DESC LIMIT 300").fetchall()
    by_kind = {k: [r for r in rules if r["kind"] == k] for k in db.KINDS}
    return {"rules": by_kind, "pms": pms, "accounts": accounts, "batches": batches, "log": log}


def gl_detail(region: str, fy: int, months: list[date], brand: str) -> list[dict]:
    with db.connect() as conn:
        gl, _ = gl_lines(conn, region, fy)
    b = calc.norm_brand(brand)
    return sorted([g for g in gl if g["month"] in set(months) and calc.norm_brand(g["brand"]) == b],
                  key=lambda g: (g["month"], g["kind"], -abs(g["amount"])))
