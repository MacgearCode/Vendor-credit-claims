"""Portfolios: who looks after which brands and retailers (built 2026-11-24, user's request).

Two things NetSuite can't hold are kept in the brand_profit settings schema (brand_profit/db.py),
admin-edited on /admin/portfolios:

PM TEAMS (pm_team). A PM may report to a senior PM, by month. The senior sees their own brands
  AND their team's (Brand Profitability's PM filter: "<senior> (team)"). Brands keep ONE owner
  (brand_pm), so a team total is never double-counted. User, 2026-11-24: David Vanderkley ←
  Prasanna Wad; Rachel Harris ← Jeffrey Zhu, Mia Chen; Dale Farnham ← Charles Cruz, Carlo De Guzman
  (from 1 Sep 2026). Reassigning someone ends their current row the month before and starts a new one.

ACCOUNT MANAGERS (am_assignment). By region × retailer (Sales Explorer's Retailer = customer group)
  × brand ('' = every brand). NetSuite holds one rep per customer record, but key accounts are split
  by brand (AU JB HIFI: Mova / Outin → Mike Bihary, the rest Chris O'Donohue). A sale's manager is:
    1. the row for its retailer and brand, else
    2. the row for its retailer and every brand, else
    3. NetSuite's account rep on the customer record (unchanged rule from 2026-10-15).
  A row can also name an item CATEGORY within the brand (dim_item.category — user 2026-11-24, AU
  The Good Guys EUFY: Security → Chris O'Donohue, Appliances (floorcare) and Baby → Mike Bihary).
  It wins over the brand row for that category's items; other categories fall through as above.
  Targets are set by retailer x brand only, so such a target is split between the categories by
  the retailer x brand's actual sales mix over the last 12 months (category_mix) — an estimate
  that moves with sales (user's choice, 2026-11-24).
  As before, the assignment in force NOW applies to all history (a manager's whole portfolio);
  the dates keep the record of who had what and when.

Everything that says "account manager" (home page, Sales Explorer, Sales Explorer targets) goes
through am_map(). Edits are admin only (user, 2026-11-24).
"""
import logging
import zlib
from datetime import date

from . import warehouse
from .brand_profit import calc, db
from .brand_profit.settings import Invalid, parse_month

log = logging.getLogger(__name__)

ALL = ""                                  # am_assignment.brand for "every brand"
# entries that resolve an account manager or a team; dropped after every change here
# (sx_month / sx_day: the explorer's summaries carry the category split, see split_rows)
CACHE_PREFIXES = ("pf_", "sx_names", "sx_ram", "sx_opts", "sx_items", "sx_month", "sx_day", "sx_mix",
                  "sales_tops")


def norm_category(s) -> str:
    return (s or "").strip().upper()


def category_id(name) -> int:
    """A stable id for an item category (the explorer's summary rows carry it); -1 = none."""
    c = norm_category(name)
    return zlib.crc32(c.encode()) & 0x3FFFFFFF if c else -1


def _this_month(today: date | None = None) -> date:
    t = today or date.today()
    return date(t.year, t.month, 1)


def _in_force(r: dict, m: date) -> bool:
    return r["valid_from"] <= m and (r["valid_to"] is None or r["valid_to"] >= m)


def norm_retailer(s) -> str:
    return (s or "").strip().upper()


# ---- reading ---------------------------------------------------------------------------
def _load() -> dict:
    try:
        with db.connect() as conn:
            return {"am": conn.execute("SELECT * FROM am_assignment ORDER BY region, retailer, brand, category, "
                                       "valid_from").fetchall(),
                    "team": conn.execute("SELECT * FROM pm_team ORDER BY reports_to, pm, valid_from").fetchall()}
    except db.NotConfigured:
        return {"am": [], "team": []}
    except Exception as e:                       # settings DB down: fall back to NetSuite / no teams
        log.warning("portfolio: settings database unavailable (%s)", e)
        return {"am": [], "team": []}


def rows() -> dict:
    return warehouse.cached(("pf_rows",), _load, ttl=10 * 60)


def am_overrides(region: str, today: date | None = None, am_rows=None) -> dict:
    """{(RETAILER, BRAND or '', CATEGORY or ''): account manager name} in force this month."""
    m = _this_month(today)
    return {(norm_retailer(r["retailer"]), r["brand"], norm_category(r.get("category"))): r["am"]
            for r in (rows()["am"] if am_rows is None else am_rows)
            if r["region"] == region and _in_force(r, m)}


def resolve(ov: dict, retailer: str, brand: str, netsuite_am, category: str = ""):
    """The manager for one sale: retailer x brand x category row, retailer x brand row,
    retailer row, else NetSuite's."""
    rk, b, c = norm_retailer(retailer), calc.norm_brand(brand), norm_category(category)
    return ((c and ov.get((rk, b, c))) or ov.get((rk, b, ALL)) or ov.get((rk, ALL, ALL))
            or netsuite_am)


def am_map(today: date | None = None) -> dict:
    """Account manager NAMES for every sale, from the warehouse's customers + the overrides:
         cust: {customer_id: name}          NetSuite rep, or the retailer's every-brand row
         cb:   {(customer_id, brand_id): name}   where a brand row applies
         cbk:  {(customer_id, brand_id, category_id): name}  where a category row applies
         rb:   {(region, RETAILER, BRAND): name} the brand rows (for targets, set by retailer)
         rbk:  {(region, RETAILER, BRAND, category_id): name}  the category rows
         ra:   {(region, RETAILER): name}        the every-brand rows
       None = no manager."""
    def build():
        regions = {warehouse.region_key(r): r for r in ("AU", "NZ")}
        brand_ids: dict = {}
        for bid, bn in warehouse.query("select brand_id, brand from reporting.dim_brand"):
            brand_ids.setdefault(calc.norm_brand(bn), []).append(bid)
        ov = {r: am_overrides(r, today) for r in ("AU", "NZ")}
        by_ret: dict = {}                       # (region, RETAILER) -> [(BRAND, CATEGORY, name)]
        for r, o in ov.items():
            for (rk, b, k), name in o.items():
                if b:
                    by_ret.setdefault((r, rk), []).append((b, k, name))
        cust, cb, cbk, pairs = {}, {}, {}, {}
        for cid, grp, rep, rkey in warehouse.query(
                "select customer_id, coalesce(customer_group, parent_name, customer_name), account_rep, region_key "
                "from reporting.dim_ns_customer"):
            region = regions.get(rkey)
            o = ov.get(region) or {}
            rk = norm_retailer(grp)
            cust[cid] = o.get((rk, ALL, ALL)) or rep
            for b, k, name in by_ret.get((region, rk), ()):
                for bid in brand_ids.get(b, ()):
                    if k:
                        cbk[(cid, bid, category_id(k))] = name
                        pairs.setdefault(region, set()).add((cid, bid))
                    else:
                        cb[(cid, bid)] = name
        return {"cust": cust, "cb": cb, "cbk": cbk, "pairs": pairs,
                "rb": {(r, rk, b): n for r, o in ov.items() for (rk, b, k), n in o.items() if b and not k},
                "rbk": {(r, rk, b, category_id(k)): n for r, o in ov.items()
                        for (rk, b, k), n in o.items() if b and k},
                "ra": {(r, rk): n for r, o in ov.items() for (rk, b, k), n in o.items() if not b}}
    return warehouse.cached(("pf_am", _this_month(today)), build, ttl=30 * 60)


def am_of(amap: dict, customer_id, brand_id, cat_id: int = -1):
    return ((cat_id is not None and cat_id >= 0 and (amap.get("cbk") or {}).get((customer_id, brand_id, cat_id)))
            or amap["cb"].get((customer_id, brand_id)) or amap["cust"].get(customer_id))


def category_pairs(region: str, today: date | None = None) -> set:
    """{(customer_id, brand_id)} in the region whose sales split by item category — the pairs a
    category row covers. Usually tiny (AU The Good Guys x EUFY)."""
    try:
        return set(am_map(today).get("pairs", {}).get(region, ()))
    except warehouse.WarehouseUnavailable:
        return set()


def item_categories() -> dict:
    """{item_key: category_id} for items with a category."""
    return warehouse.cached(("pf_item_cat",), lambda: {
        ik: category_id(c) for ik, c in warehouse.query(
            "select item_key, category from reporting.dim_item where category is not null")}, ttl=60 * 60)


def category_names() -> list[str]:
    return [r[0] for r in warehouse.query(
        "select distinct category from reporting.dim_item where category is not null order by 1")]


def split_rows(rows, pairs: set, fine) -> list:
    """Replace summary rows (k, customer, brand, rep, revenue, cost, qty, rebates) of the
    category-split pairs with one row per category, rep = category_id. fine = rows
    (k, customer, brand, category_id, revenue, est_cost, qty) for those pairs from the line facts.
    The row's net-profit deductions (cost above est. cost), rebates, and any revenue the summary
    has that the facts don't, go to the categories in proportion to revenue."""
    if not pairs:
        return list(rows)
    by: dict = {}
    for k, c, b, cat, v, x, q in fine:
        by.setdefault((k, c, b), []).append((-1 if cat is None else cat, float(v or 0), float(x or 0), float(q or 0)))
    out = []
    for r in rows:
        parts = by.get((r[0], r[1], r[2])) if (r[1], r[2]) in pairs else None
        if not parts:
            out.append(r)
            continue
        rev, est = sum(p[1] for p in parts), sum(p[2] for p in parts)
        extra, reb = float(r[5] or 0) - est, (float(r[7] or 0) if len(r) > 7 else 0.0)
        dv = float(r[4] or 0) - rev
        for i, (cat, v, x, q) in enumerate(parts):
            sh = (v / rev) if rev else (1.0 if i == 0 else 0.0)
            out.append((r[0], r[1], r[2], cat, v + dv * sh, x + extra * sh, q, reb * sh))
    out.sort(key=lambda r: r[0])
    return out


def category_mix(rows, cust_retailer: dict, brand_name: dict) -> dict:
    """{(RETAILER, BRAND): {category_id: share}} from split summary rows (customer, brand,
    category_id, revenue) of the category-split pairs. Shares sum to 1."""
    tot: dict = {}
    for c, b, cat, v in rows:
        key = (norm_retailer(cust_retailer.get(c)), calc.norm_brand(brand_name.get(b)))
        d = tot.setdefault(key, {})
        d[cat] = d.get(cat, 0.0) + v
    out = {}
    for key, d in tot.items():
        s = sum(d.values())
        if s > 0:
            out[key] = {k: v / s for k, v in d.items() if v}
    return out


# ---- PM teams ----------------------------------------------------------------------------
def reports_to(team_rows, pm: str, m: date) -> str | None:
    for r in team_rows:
        if r["pm"] == pm and _in_force(r, m):
            return r["reports_to"]
    return None


def seniors(team_rows, months) -> list[str]:
    """Senior PMs with a team in any of the months."""
    return sorted({r["reports_to"] for r in team_rows if any(_in_force(r, m) for m in months)})


def in_team(team_rows, senior: str):
    """(month, pm) -> True when pm is the senior or reports to them that month."""
    return lambda m, pm: pm == senior or reports_to(team_rows, pm, m) == senior


# ---- writing (admin page) ----------------------------------------------------------------
def _reassign(conn, table: str, key: dict, value_col: str, value: str, vf: date, by: str, note: str = "") -> int | None:
    """End the key's row covering vf the month before and start a new one from vf (open-ended).
    A row starting exactly at vf is replaced. Rows starting after vf must be removed first.
    value '' = end only (back to NetSuite / reports to nobody). Returns the new row's id."""
    with conn.transaction():
        for old in db.overlaps(conn, table, key, vf, None):
            if old["valid_from"] > vf:
                raise Invalid(f"There's already a later row from {old['valid_from']:%b %Y} "
                              f"({old[value_col]}). Delete it first.")
            if old["valid_from"] == vf:
                conn.execute(f"DELETE FROM {table} WHERE id = %s", (old["id"],))
                db.log(conn, by, table, old["id"], "delete", before=old, note=f"replaced from {vf:%b %Y}")
            else:
                if old[value_col] == value:
                    raise Invalid(f"That's already the case (since {old['valid_from']:%b %Y}).")
                ended = conn.execute(f"UPDATE {table} SET valid_to = %s, updated_by = %s, updated_at = now() "
                                     f"WHERE id = %s RETURNING *", (calc.month_add(vf, -1), by, old["id"])).fetchone()
                db.log(conn, by, table, old["id"], "edit", before=old, after=ended, note=f"ended from {vf:%b %Y}")
        if not value:
            return None
        cols = {**key, value_col: value, "valid_from": vf, "updated_by": by}
        if table == "am_assignment":
            cols["note"] = note
        names = ", ".join(cols)
        row = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING *",
                           list(cols.values())).fetchone()
        db.log(conn, by, table, row["id"], "add", after=row)
        return row["id"]


def assign_am(conn, f: dict, by: str) -> int | None:
    region = f.get("region", "")
    if region not in db.REGIONS:
        raise Invalid("Pick AU or NZ.")
    retailer = (f.get("retailer") or "").strip()
    if not retailer:
        raise Invalid("Pick a retailer.")
    brand = calc.norm_brand(f.get("brand"))
    if brand in ("ALL", "ALL BRANDS", "*"):
        brand = ALL
    category = (f.get("category") or "").strip()
    if category and not brand:
        raise Invalid("A category needs a brand.")
    am = (f.get("am") or "").strip()
    vf = parse_month(f.get("valid_from"))
    return _reassign(conn, "am_assignment", {"region": region, "retailer": retailer, "brand": brand,
                                             "category": category},
                     "am", am, vf, by, (f.get("note") or "").strip())


def assign_team(conn, pm: str, senior: str, month: str, by: str) -> int | None:
    pm, senior = (pm or "").strip(), (senior or "").strip()
    if not pm:
        raise Invalid("Pick a PM.")
    if pm == senior:
        raise Invalid("A PM can't report to themselves.")
    vf = parse_month(month)
    if senior:
        rows_ = conn.execute("SELECT * FROM pm_team").fetchall()
        if reports_to(rows_, senior, vf) == pm:
            raise Invalid(f"{senior} reports to {pm} that month — that would be a loop.")
    return _reassign(conn, "pm_team", {"pm": pm}, "reports_to", senior, vf, by)


def delete_row(conn, table: str, rid: int, by: str):
    if table not in ("am_assignment", "pm_team"):
        raise ValueError(table)
    with conn.transaction():
        before = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (rid,)).fetchone()
        if not before:
            raise Invalid("That row no longer exists.")
        conn.execute(f"DELETE FROM {table} WHERE id = %s", (rid,))
        db.log(conn, by, table, rid, "delete", before=before)


def changed():
    """Forget everything resolved from the old assignments."""
    warehouse.drop(CACHE_PREFIXES)


# ---- the admin page ----------------------------------------------------------------------
def page(today: date | None = None) -> dict:
    """Everything /admin/portfolios shows. Warehouse lookups degrade to empty lists."""
    m = _this_month(today)
    with db.connect() as conn:
        am_rows = conn.execute("SELECT * FROM am_assignment ORDER BY region, retailer, brand, category, "
                               "valid_from").fetchall()
        team_rows = conn.execute("SELECT * FROM pm_team ORDER BY reports_to, pm, valid_from").fetchall()
        pm_rows = conn.execute("SELECT region, brand, pm FROM brand_pm WHERE valid_from <= %s "
                               "AND (valid_to IS NULL OR valid_to >= %s) ORDER BY pm, region, brand", (m, m)).fetchall()
        pm_names = [r["pm"] for r in conn.execute("SELECT DISTINCT pm FROM brand_pm ORDER BY pm").fetchall()]
    brands_now: dict = {}
    for r in pm_rows:
        brands_now.setdefault(r["pm"], {}).setdefault(r["region"], []).append(r["brand"])
    current_team = [r for r in team_rows if _in_force(r, m)]
    teams = []
    for s in sorted({r["reports_to"] for r in current_team}):
        members = sorted(r["pm"] for r in current_team if r["reports_to"] == s)
        teams.append({"senior": s, "members": members})
    in_a_team = {r["pm"] for r in current_team} | {t["senior"] for t in teams}
    out = {"month": m, "am_rows": am_rows, "team_rows": team_rows, "teams": teams, "brands_now": brands_now,
           "solo": [p for p in pm_names if p not in in_a_team and p in brands_now],
           "pm_names": pm_names, "retailers": {"AU": [], "NZ": []}, "reps": [], "brands": [], "categories": [],
           "netsuite": {}, "warehouse_error": ""}
    try:
        regions = {warehouse.region_key(r): r for r in ("AU", "NZ")}
        ns: dict = {}
        for rkey, grp, rep, n in warehouse.query(
                "select region_key, coalesce(customer_group, parent_name, customer_name), "
                "coalesce(account_rep, '(none)'), count(*) from reporting.dim_ns_customer "
                "group by 1, 2, 3 order by 4 desc"):
            r = regions.get(rkey)
            if r:
                ns.setdefault((r, norm_retailer(grp)), []).append(rep)
                if grp not in out["retailers"][r]:
                    out["retailers"][r].append(grp)
        for r in out["retailers"]:
            out["retailers"][r].sort(key=str.upper)
        out["netsuite"] = {f"{r}|{k}": " / ".join(v) for (r, k), v in ns.items()}
        out["reps"] = [r[0] for r in warehouse.query(
            "select distinct account_rep from reporting.dim_ns_customer where account_rep is not null "
            "union select rep_name from reporting.dim_salesrep where rep_name is not null order by 1")]
        out["brands"] = warehouse.brand_names()
        out["categories"] = category_names()
    except warehouse.WarehouseUnavailable as e:
        out["warehouse_error"] = str(e)
    for r in am_rows:
        r["netsuite"] = out["netsuite"].get(f"{r['region']}|{norm_retailer(r['retailer'])}", "")
        r["current"] = _in_force(r, m)
        r["differs"] = r["current"] and r["am"] not in r["netsuite"].split(" / ")
    return out


# ---- seed from the user's workbook (AM PM Assignment.xlsx, 2026-11-24) ------------------------
SEED_TEAMS = [("Prasanna Wad", "David Vanderkley", "2026-10"),
              ("Jeffrey Zhu", "Rachel Harris", "2026-04"),
              ("Mia Chen", "Rachel Harris", "2026-04"),
              ("Charles Cruz", "Dale Farnham", "2026-09"),
              ("Carlo De Guzman", "Dale Farnham", "2026-09")]
# workbook retailer -> NetSuite customer group(s); EUFY sub-ranges are item categories (dim_item)
SEED_RETAILERS = {"BIG W/DUKE LIVING": ["Big W", "Duke Living Pty Ltd"], "COSTCO": ["Costco Wholesale"],
                  "JB HI-FI": ["JB HIFI"]}
SEED_CATEGORIES = {"EUFY SECURITY": "Security", "EUFY FLOORCARE": "Appliances", "EUFY BABY": "Baby"}


def seed_rows_from_workbook(path) -> tuple[list[dict], list[str]]:
    """(am_assignment rows, skipped notes) from the sheet's 'Key Accounts – Australia' block."""
    import openpyxl
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    out, skipped, seen = [], [], set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        retailer, brand, am = (row[5:8] + (None, None, None))[:3]
        if not retailer or not am or str(am).strip().lower() == "account manager":   # header row
            continue
        am = str(am).replace("’", "'").replace("�", "'").strip()
        brand = calc.norm_brand(brand)
        brand = ALL if brand == "ALL" else brand
        category = ""
        if brand in SEED_CATEGORIES:
            brand, category = "EUFY", SEED_CATEGORIES[brand]
        elif brand.startswith("EUFY "):
            skipped.append(f"{retailer} × {brand.title()} → {am}: no item category matches")
            continue
        for grp in SEED_RETAILERS.get(str(retailer).strip().upper(), [str(retailer).strip()]):
            key = ("AU", grp.upper(), brand, category)
            if key in seen:
                continue
            seen.add(key)
            out.append({"region": "AU", "retailer": grp, "brand": brand, "category": category, "am": am,
                        "valid_from": "2026-04", "note": "AM PM Assignment.xlsx (2026-11-24)"})
    return out, skipped


def seed(path, by: str) -> dict:
    added, skipped = 0, []
    am_rows, skipped = seed_rows_from_workbook(path)
    with db.connect() as conn:
        for f in am_rows:
            try:
                assign_am(conn, f, by); added += 1
            except Invalid as e:
                skipped.append(f"{f['retailer']} × {f['brand'] or 'all'}: {e}")
        teams = 0
        for pm, senior, month in SEED_TEAMS:
            try:
                assign_team(conn, pm, senior, month, by); teams += 1
            except Invalid as e:
                skipped.append(f"{pm} → {senior}: {e}")
    changed()
    return {"am_rows": added, "team_rows": teams, "skipped": skipped}


if __name__ == "__main__":                      # python -m app.portfolio "AM PM Assignment.xlsx"
    import sys
    from dotenv import load_dotenv
    load_dotenv(".env")
    print(seed(sys.argv[1], "Aaron (seed from AM PM Assignment.xlsx)"))
