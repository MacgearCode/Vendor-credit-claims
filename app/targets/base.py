"""Where a scenario's starting numbers come from, and the official targets to compare with.

A scenario plans `target_fy` (T) from a starting point (scenario.start_from / base_kind):
  last_year  NetSuite sales of T-1; while T-1 is still running, its unfinished months
             come from T-2 (actuals.cells_filled) so the base is a whole year
  this_year  T's finished months as actuals + the rest projected from T-1 (the engine's
             projection: scenario.projection_mode / ref_fy)
  official   a frozen copy of T's official targets (base_line rows)
  import     a targets workbook (base_line rows, importer.py)
  blank      nothing; manual lines only
"""
from collections import defaultdict

from . import actuals

COUNTRY_NAME = {"AU": "Australia", "NZ": "New Zealand"}


def fy_end_year(target_fy: str) -> int | None:
    """'FY27' -> 2027 (Macgear's FY is April-March, named by the year it ends)."""
    t = (target_fy or "").strip().upper().removeprefix("FY")
    if not t.isdigit():
        return None
    return 2000 + int(t) if len(t) == 2 else int(t)


def cells(conn, scenario: dict) -> dict | None:
    """{country: {(brand, customer): [12]}} for the engine, or None = let the engine load
    the scenario's year of actuals itself (this_year, and old scenarios)."""
    kind = scenario.get("base_kind") or "actuals"
    if kind == "snapshot":
        out = {c: defaultdict(lambda: [0.0] * 12) for c in actuals.COUNTRIES}
        for r in conn.execute("SELECT country, brand, customer, month_idx, amount FROM base_line "
                              "WHERE scenario_id = %s", (scenario["id"],)):
            out.setdefault(r["country"], defaultdict(lambda: [0.0] * 12))
            out[r["country"]][(r["brand"], r["customer"])][r["month_idx"]] = r["amount"]
        return out
    if kind == "blank":
        return {c: {} for c in actuals.COUNTRIES}
    fy = scenario.get("actuals_id")
    if fy and not scenario.get("projection_mode") and len(actuals.covered(fy)) < 12:
        return actuals.cells_filled(fy)
    return None


def official(conn, target_fy: str) -> dict:
    """The official targets for a year: {country: {(brand, customer): [12]}}."""
    out = {c: defaultdict(lambda: [0.0] * 12) for c in actuals.COUNTRIES}
    for r in conn.execute("SELECT country_code, brand, customer, month_start, amount FROM sales_target "
                          "WHERE target_fy = %s", (target_fy,)):
        country = COUNTRY_NAME.get(r["country_code"])
        if country:
            out[country][(r["brand"], r["customer"])][(r["month_start"].month - 4) % 12] += float(r["amount"])
    return out
