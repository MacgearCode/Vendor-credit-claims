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
  As before, the assignment in force NOW applies to all history (a manager's whole portfolio);
  the dates keep the record of who had what and when.

Everything that says "account manager" (home page, Sales Explorer, Sales Explorer targets) goes
through am_map(). Edits are admin only (user, 2026-11-24).
"""
import logging
from datetime import date

from . import warehouse
from .brand_profit import calc, db
from .brand_profit.settings import Invalid, parse_month

log = logging.getLogger(__name__)

ALL = ""                                  # am_assignment.brand for "every brand"
# entries that resolve an account manager or a team; dropped after every change here
CACHE_PREFIXES = ("pf_", "sx_names", "sx_ram", "sx_opts", "sx_items", "sales_tops")


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
            return {"am": conn.execute("SELECT * FROM am_assignment ORDER BY region, retailer, brand, valid_from").fetchall(),
                    "team": conn.execute("SELECT * FROM pm_team ORDER BY reports_to, pm, valid_from").fetchall()}
    except db.NotConfigured:
        return {"am": [], "team": []}
    except Exception as e:                       # settings DB down: fall back to NetSuite / no teams
        log.warning("portfolio: settings database unavailable (%s)", e)
        return {"am": [], "team": []}


def rows() -> dict:
    return warehouse.cached(("pf_rows",), _load, ttl=10 * 60)


def am_overrides(region: str, today: date | None = None, am_rows=None) -> dict:
    """{(RETAILER, BRAND or ''): account manager name} in force this month for the region."""
    m = _this_month(today)
    return {(norm_retailer(r["retailer"]), r["brand"]): r["am"]
            for r in (rows()["am"] if am_rows is None else am_rows)
            if r["region"] == region and _in_force(r, m)}


def resolve(ov: dict, retailer: str, brand: str, netsuite_am):
    """The manager for one sale: retailer x brand row, retailer row, else NetSuite's."""
    rk = norm_retailer(retailer)
    return ov.get((rk, calc.norm_brand(brand))) or ov.get((rk, ALL)) or netsuite_am


def am_map(today: date | None = None) -> dict:
    """Account manager NAMES for every sale, from the warehouse's customers + the overrides:
         cust: {customer_id: name}          NetSuite rep, or the retailer's every-brand row
         cb:   {(customer_id, brand_id): name}   where a brand row applies
         rb:   {(region, RETAILER, BRAND): name} the brand rows (for targets, set by retailer)
         ra:   {(region, RETAILER): name}        the every-brand rows
       None = no manager."""
    def build():
        regions = {warehouse.region_key(r): r for r in ("AU", "NZ")}
        brand_ids: dict = {}
        for bid, bn in warehouse.query("select brand_id, brand from reporting.dim_brand"):
            brand_ids.setdefault(calc.norm_brand(bn), []).append(bid)
        ov = {r: am_overrides(r, today) for r in ("AU", "NZ")}
        by_ret: dict = {}                       # (region, RETAILER) -> [(BRAND, name)]
        for r, o in ov.items():
            for (rk, b), name in o.items():
                if b:
                    by_ret.setdefault((r, rk), []).append((b, name))
        cust, cb = {}, {}
        for cid, grp, rep, rkey in warehouse.query(
                "select customer_id, coalesce(customer_group, parent_name, customer_name), account_rep, region_key "
                "from reporting.dim_ns_customer"):
            region = regions.get(rkey)
            o = ov.get(region) or {}
            rk = norm_retailer(grp)
            cust[cid] = o.get((rk, ALL)) or rep
            for b, name in by_ret.get((region, rk), ()):
                for bid in brand_ids.get(b, ()):
                    cb[(cid, bid)] = name
        return {"cust": cust, "cb": cb,
                "rb": {(r, rk, b): n for r, o in ov.items() for (rk, b), n in o.items() if b},
                "ra": {(r, rk): n for r, o in ov.items() for (rk, b), n in o.items() if not b}}
    return warehouse.cached(("pf_am", _this_month(today)), build, ttl=30 * 60)


def am_of(amap: dict, customer_id, brand_id):
    return amap["cb"].get((customer_id, brand_id)) or amap["cust"].get(customer_id)


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
    am = (f.get("am") or "").strip()
    vf = parse_month(f.get("valid_from"))
    return _reassign(conn, "am_assignment", {"region": region, "retailer": retailer, "brand": brand},
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
        am_rows = conn.execute("SELECT * FROM am_assignment ORDER BY region, retailer, brand, valid_from").fetchall()
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
           "pm_names": pm_names, "retailers": {"AU": [], "NZ": []}, "reps": [], "brands": [],
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
# workbook retailer -> NetSuite customer group(s); EUFY sub-ranges aren't NetSuite brands
SEED_RETAILERS = {"BIG W/DUKE LIVING": ["Big W", "Duke Living Pty Ltd"], "COSTCO": ["Costco Wholesale"],
                  "JB HI-FI": ["JB HIFI"]}


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
        if brand.startswith("EUFY "):
            skipped.append(f"{retailer} × {brand.title()} → {am}: NetSuite has only EUFY, not its sub-ranges")
            continue
        for grp in SEED_RETAILERS.get(str(retailer).strip().upper(), [str(retailer).strip()]):
            key = ("AU", grp.upper(), brand)
            if key in seen:
                continue
            seen.add(key)
            out.append({"region": "AU", "retailer": grp, "brand": brand, "am": am, "valid_from": "2026-04",
                        "note": "AM PM Assignment.xlsx (2026-11-24)"})
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
