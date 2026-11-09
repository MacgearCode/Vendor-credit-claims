"""Targets tool — the JSON API behind /targets (moved in from the standalone Targets app,
rebuilt 2026-10-22 so everything lives in the database).

  actuals    NetSuite sales from the warehouse, one financial year at a time (actuals.py);
             a scenario's "dataset" id is the FY's end year (2026 = FY26)
  scenarios  the planner's inputs, in the `targets` schema (db.py)
  commit     "Commit" freezes a scenario's computed targets; the newest commit per FY is
             official and is what targets.sales_target (and so the home page) shows

The engine (calc.py) is the standalone app's, unchanged apart from where actuals come from.
2026-10-23: a scenario is created from what is being planned (target year + period) and a
starting point (base.py); payloads carry comparisons (last year, official, this year's
trend) for the period, the scenario's commit status, and commits can preview their effect
and be limited to one country.
"""
import json
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from .. import warehouse
from . import actuals, base, calc, csv_export, duplicates
from .db import NotConfigured, connect

COUNTRIES = actuals.COUNTRIES

router = APIRouter(prefix="/targets")
SYDNEY = ZoneInfo("Australia/Sydney")


# ---------- models ----------

class ScenarioIn(BaseModel):
    name: str
    target_fy: str | None = None        # default: the year after the actuals year
    default_growth_pct: float = 0.0
    actuals_id: int | None = None       # FY end year of the actuals it's built on
    projection_mode: str | None = None  # None/'none' | 'flat' | 'tracking'
    ref_actuals_id: int | None = None   # reference year for projection
    projection_union: bool = False      # also include reference-year-only lines
    blank: bool = False                 # no actuals: manual lines only (e.g. imported targets)
    period_start: int = 0               # fiscal months planned: 0 = Apr .. 11 = Mar
    period_end: int = 11
    start_from: str | None = None       # last_year | this_year | official | blank (the new dialog)
    method: str = "flat"                # this_year: fill remaining months 'flat' or 'tracking'


class GrowthOverride(BaseModel):
    level: str            # 'brand' | 'customer'
    country: str
    brand: str
    customer: str = ""    # '' for brand level
    growth_pct: float


class PacingOverride(BaseModel):
    level: str            # 'global' | 'brand'
    country: str
    brand: str = ""       # '' for global level
    weights: list[float]  # 12 monthly weights (will be normalized)


class BrandExclusion(BaseModel):
    country: str
    brand: str


class AnnualizeFlag(BaseModel):
    country: str
    brand: str


class ManualLine(BaseModel):
    country: str
    brand: str
    customer: str
    target_annual: float = 0.0
    months: list[float] | None = None   # 12 explicit monthly $; None = split target_annual by pacing


class ScenarioUpdate(BaseModel):
    default_growth_pct: float | None = None
    name: str | None = None
    target_fy: str | None = None
    projection_mode: str | None = None   # send 'none' to turn projection off
    ref_actuals_id: int | None = None
    projection_union: bool | None = None
    period_start: int | None = None      # set both together
    period_end: int | None = None
    # full replacement sets (client sends the current complete state)
    growth_overrides: list[GrowthOverride] | None = None
    pacing_overrides: list[PacingOverride] | None = None
    brand_exclusions: list[BrandExclusion] | None = None
    annualize: list[AnnualizeFlag] | None = None
    manual_lines: list[ManualLine] | None = None


class CommitIn(BaseModel):
    note: str = ""
    countries: list[str] | None = None   # default both


START_LABEL = {"last_year": "Last year's sales", "this_year": "This year so far + last year",
               "official": "Official targets", "import": "Imported targets", "blank": "Blank"}


# ---------- helpers ----------

PERIOD_NAMES = {(0, 11): "Full year", (0, 5): "H1", (6, 11): "H2", (0, 2): "Q1",
                (3, 5): "Q2", (6, 8): "Q3", (9, 11): "Q4"}


def period_label(a: int, b: int) -> str:
    """'Q4', 'H1', 'Full year', or 'Jan–Feb' for a custom span (fiscal months)."""
    from .db import FY_MONTHS
    return PERIOD_NAMES.get((a, b)) or (FY_MONTHS[a] if a == b else f"{FY_MONTHS[a]}–{FY_MONTHS[b]}")


def _check_period(a, b):
    if not (0 <= a <= b <= 11):
        raise HTTPException(400, "The period must run forward within the year (April to March).")


def _db():
    try:
        return connect()
    except NotConfigured:
        raise HTTPException(503, "The Targets database isn't configured (TARGETS_DB_URL).")


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.email if user is not None else "local"


def _scenario_row(conn, sid):
    row = conn.execute("SELECT * FROM scenario WHERE id = %s", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "Scenario not found")
    # the front end's names for the two years (it predates the warehouse source)
    d = dict(row)
    d["actuals_id"], d["ref_actuals_id"] = d.pop("actuals_fy"), d.pop("ref_fy")
    d["projection_union"] = 1 if d["projection_union"] else 0
    d["period_label"] = period_label(d["period_start"], d["period_end"])
    for k in ("created_at", "updated_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


def _raw_inputs(conn, sid):
    """The stored overrides, so the client can own the full input state."""
    growth = [dict(r) for r in conn.execute(
        "SELECT level, country, brand, customer, growth_pct "
        "FROM growth_override WHERE scenario_id = %s", (sid,))]
    pacing_map = {}
    for r in conn.execute(
            "SELECT level, country, brand, month_idx, weight "
            "FROM pacing_override WHERE scenario_id = %s ORDER BY month_idx", (sid,)):
        key = (r["level"], r["country"], r["brand"])
        pacing_map.setdefault(key, {"level": r["level"], "country": r["country"],
                                    "brand": r["brand"], "weights": [0.0] * 12})
        pacing_map[key]["weights"][r["month_idx"]] = r["weight"]
    exclusions = [dict(r) for r in conn.execute(
        "SELECT country, brand FROM brand_exclusion WHERE scenario_id = %s", (sid,))]
    annualize = [dict(r) for r in conn.execute(
        "SELECT country, brand FROM annualize WHERE scenario_id = %s", (sid,))]
    manual = []
    for r in conn.execute(
            "SELECT country, brand, customer, target_annual, months FROM manual_line "
            "WHERE scenario_id = %s", (sid,)):
        line = {"country": r["country"], "brand": r["brand"], "customer": r["customer"],
                "target_annual": r["target_annual"]}
        parsed = calc._parse_months(r["months"])
        if parsed is not None:
            line["months"] = parsed
        manual.append(line)
    return {"growth_overrides": growth, "pacing_overrides": list(pacing_map.values()),
            "brand_exclusions": exclusions, "annualize": annualize, "manual_lines": manual}


def _known_year(fy) -> bool:
    return any(y["id"] == fy for y in actuals.years())


def _validate_projection(mode, ref_id, actuals_id):
    """A projection needs a distinct reference year that has actuals."""
    if mode in ("flat", "tracking"):
        if ref_id is None:
            raise HTTPException(400, "Projection needs a reference year (the prior year).")
        if ref_id == actuals_id:
            raise HTTPException(400, "The reference year must differ from the scenario's own actuals.")
        if not _known_year(ref_id):
            raise HTTPException(404, "No actuals for that reference year.")


def _computed_payload(conn, scenario):
    aid = scenario.get("actuals_id")
    mode = scenario.get("projection_mode")
    ref_id = scenario.get("ref_actuals_id")
    union = bool(scenario.get("projection_union"))
    bc = base.cells(conn, scenario)
    countries = [
        calc.compute_country(conn, scenario["id"], scenario["default_growth_pct"],
                             c, aid, mode, ref_id, union,
                             base_cells=bc.get(c) if bc is not None else None)
        for c in COUNTRIES
    ]
    projection = None
    if mode in ("flat", "tracking"):
        projection = {
            "mode": mode,
            "ref_dataset": actuals.meta(ref_id),
            "covered": sorted(calc.covered_months(conn, aid)),
            "union": union,
            "new_lines": sum(b.get("new_lines", 0) for c in countries for b in c["brands"]),
        }
    return {
        "scenario": scenario,
        "period": {"start": scenario["period_start"], "end": scenario["period_end"],
                   "label": scenario["period_label"],
                   "months": list(range(scenario["period_start"], scenario["period_end"] + 1))},
        "dataset": _dataset(scenario),
        "start": {"kind": scenario.get("start_from") or ("blank" if aid is None else "last_year"),
                  "label": scenario.get("base_label") or START_LABEL.get(scenario.get("start_from"), "")},
        "projection": projection,
        "inputs": _raw_inputs(conn, scenario["id"]),
        "countries": countries,
        "compare": _compare(conn, scenario, countries),
        "status": _status(conn, scenario),
    }


def _dataset(scenario):
    """What the grid's 'base' is, in the shape the front end has always used."""
    if (scenario.get("base_kind") or "actuals") in ("snapshot", "blank"):
        label = scenario.get("base_label") or START_LABEL.get(scenario.get("start_from"), "Blank")
        return {"id": None, "name": label, "source_fy": label, "period_start": "", "period_end": "",
                "months_present": [], "is_partial": False}
    return actuals.meta(scenario.get("actuals_id"))


def _sum12(m, idx):
    return sum(m[i] for i in idx)


def _compare(conn, scenario, countries):
    """Per brand / retailer / country, for the scenario's period: last year's sales of the
    same months, the official targets of those months, and this year's trend (finished
    months this year vs the same months last year). Written onto the grid rows as `cmp`;
    returns the flags the page needs."""
    fy = base.fy_end_year(scenario["target_fy"])
    if fy is None:
        return {"has_official": False, "trend": None}
    months = list(range(scenario["period_start"], scenario["period_end"] + 1))
    ly = actuals.cells(fy - 1)
    off = base.official(conn, scenario["target_fy"])
    done = sorted(actuals.covered(fy))
    trend_on = 0 < len(done) < 12
    cur = actuals.cells(fy) if trend_on else {}
    has_official = any(any(v) for c in off.values() for v in c.values())
    for c in countries:
        name = c["country"]
        lyc, offc, curc = ly.get(name, {}), off.get(name, {}), cur.get(name, {}) if trend_on else {}
        zero = [0.0] * 12

        def cell(brand, cust):
            k = (brand, cust)
            return {"ly": round(_sum12(lyc.get(k, zero), months), 2),
                    "official": round(_sum12(offc.get(k, zero), months), 2),
                    "ytd": round(_sum12(curc.get(k, zero), done), 2) if trend_on else None,
                    "ytd_ly": round(_sum12(lyc.get(k, zero), done), 2) if trend_on else None}
        for b in c["brands"]:
            agg = {"ly": 0.0, "official": 0.0, "ytd": 0.0 if trend_on else None, "ytd_ly": 0.0 if trend_on else None}
            # the brand compares ALL its lines, not only the retailers in the grid
            for (brand, cust), m in lyc.items():
                if brand == b["brand"]:
                    agg["ly"] += _sum12(m, months)
                    if trend_on:
                        agg["ytd_ly"] += _sum12(m, done)
            for (brand, cust), m in offc.items():
                if brand == b["brand"]:
                    agg["official"] += _sum12(m, months)
            if trend_on:
                for (brand, cust), m in curc.items():
                    if brand == b["brand"]:
                        agg["ytd"] += _sum12(m, done)
            b["cmp"] = {k: (round(v, 2) if v is not None else None) for k, v in agg.items()}
            for cu in b["customers"]:
                cu["cmp"] = cell(b["brand"], cu["customer"])
        tot = {"ly": sum(_sum12(m, months) for m in lyc.values()),
               "official": sum(_sum12(m, months) for m in offc.values()),
               "ytd": sum(_sum12(m, done) for m in curc.values()) if trend_on else None,
               "ytd_ly": sum(_sum12(m, done) for m in lyc.values()) if trend_on else None}
        c["totals"]["cmp"] = {k: (round(v, 2) if v is not None else None) for k, v in tot.items()}
    from .db import FY_MONTHS
    return {"has_official": has_official, "ly_label": actuals.fy_label(fy - 1),
            "trend": ({"months": done, "label": f"{FY_MONTHS[done[0]]}–{FY_MONTHS[done[-1]]}"}
                      if trend_on else None)}


def _status(conn, scenario):
    """The scenario's last commit, and whether it has changed since."""
    r = conn.execute("SELECT * FROM target_commit WHERE scenario_id = %s ORDER BY id DESC LIMIT 1",
                     (scenario["id"],)).fetchone()
    if not r:
        return {"committed": None}
    meta = _commit_meta(r)
    edited = conn.execute("SELECT updated_at > %s AS e FROM scenario WHERE id = %s",
                          (r["committed_at"], scenario["id"])).fetchone()["e"]
    return {"committed": meta, "edited_since": bool(edited)}


def _replace(conn, sql, rows):
    if rows:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)


# ---------- scenarios ----------

@router.get("/api/scenarios")
def list_scenarios():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, name, target_fy, default_growth_pct, updated_at, period_start, period_end "
            "FROM scenario ORDER BY updated_at DESC").fetchall()
        return [{**r, "updated_at": r["updated_at"].isoformat(),
                 "period_label": period_label(r["period_start"], r["period_end"])} for r in rows]


@router.post("/api/scenarios")
def create_scenario(body: ScenarioIn, request: Request):
    _check_period(body.period_start, body.period_end)
    if body.start_from:
        return _create_from(body, request)
    if body.blank:
        aid, mode = None, "none"
        target_fy = (body.target_fy or actuals.fy_label(actuals.current_fy())).strip().upper()
    else:
        years = actuals.years()
        aid = body.actuals_id if body.actuals_id is not None else (years[0]["id"] if years else None)
        if aid is None or not _known_year(aid):
            raise HTTPException(400, "Pick a financial year of actuals to build on.")
        mode = body.projection_mode or "none"
        _validate_projection(mode, body.ref_actuals_id, aid)
        target_fy = (body.target_fy or actuals.fy_label(aid + 1)).strip().upper()
    with _db() as conn:
        sid = conn.execute(
            "INSERT INTO scenario (name, target_fy, default_growth_pct, actuals_fy, projection_mode, "
            "ref_fy, projection_union, created_by, period_start, period_end) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (body.name, target_fy, body.default_growth_pct, aid,
             None if mode == "none" else mode,
             body.ref_actuals_id if mode != "none" else None,
             mode != "none" and body.projection_union, _who(request),
             body.period_start, body.period_end)).fetchone()["id"]
        conn.commit()
        return _computed_payload(conn, _scenario_row(conn, sid))


def _create_from(body: ScenarioIn, request: Request):
    """The planning-first dialog: what year and period, starting from what."""
    target_fy = (body.target_fy or actuals.fy_label(actuals.current_fy())).strip().upper()
    fy = base.fy_end_year(target_fy)
    if fy is None:
        raise HTTPException(400, "Name the year like FY27.")
    start = body.start_from
    aid = mode = ref = None
    kind, label = "actuals", ""
    if start == "last_year":
        aid = fy - 1
        if not _known_year(aid):
            raise HTTPException(400, f"There are no NetSuite sales for {actuals.fy_label(aid)} to start from.")
        label = f"{actuals.fy_label(aid)} sales" + ("" if len(actuals.covered(aid)) == 12
                                                   else f" (unfinished months from {actuals.fy_label(aid - 1)})")
    elif start == "this_year":
        done = actuals.covered(fy)
        if not 0 < len(done) < 12:
            raise HTTPException(400, f"{target_fy} isn't under way, so there's no 'this year so far' - "
                                     "start from last year instead.")
        if body.method not in ("flat", "tracking"):
            raise HTTPException(400, "method must be 'flat' or 'tracking'")
        aid, mode, ref = fy, body.method, fy - 1
        label = (f"{target_fy} so far + {actuals.fy_label(fy - 1)}"
                 + (" (tracking)" if body.method == "tracking" else ""))
    elif start == "official":
        kind = "snapshot"
    elif start == "blank":
        kind, label = "blank", "Blank"
    else:
        raise HTTPException(400, "start_from must be last_year, this_year, official or blank")
    with _db() as conn:
        if kind == "snapshot":
            off = base.official(conn, target_fy)
            rows = [(c, b, cu, i, amt) for c, cells in off.items() for (b, cu), m in cells.items()
                    for i, amt in enumerate(m) if amt]
            if not rows:
                raise HTTPException(400, f"Nothing is official for {target_fy} yet - start from last year instead.")
            when = conn.execute("SELECT to_char(now() AT TIME ZONE 'Australia/Sydney', 'DD Mon YYYY') AS d"
                                ).fetchone()["d"]
            label = f"Official {target_fy} targets as at {when}"
        sid = conn.execute(
            "INSERT INTO scenario (name, target_fy, default_growth_pct, actuals_fy, projection_mode, "
            "ref_fy, projection_union, created_by, period_start, period_end, start_from, base_kind, base_label) "
            "VALUES (%s,%s,%s,%s,%s,%s,false,%s,%s,%s,%s,%s,%s) RETURNING id",
            (body.name, target_fy, body.default_growth_pct, aid, mode, ref, _who(request),
             body.period_start, body.period_end, start, kind, label)).fetchone()["id"]
        if kind == "snapshot":
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO base_line (scenario_id, country, brand, customer, month_idx, amount) "
                                "VALUES (%s,%s,%s,%s,%s,%s)", [(sid,) + r for r in rows])
        conn.commit()
        return _computed_payload(conn, _scenario_row(conn, sid))


@router.get("/api/scenarios/{sid}")
def get_scenario(sid: int):
    with _db() as conn:
        return _computed_payload(conn, _scenario_row(conn, sid))


@router.put("/api/scenarios/{sid}")
def update_scenario(sid: int, body: ScenarioUpdate):
    with _db() as conn:
        scenario = _scenario_row(conn, sid)
        for col in ("name", "target_fy", "default_growth_pct"):
            val = getattr(body, col)
            if val is not None:
                conn.execute(f"UPDATE scenario SET {col} = %s WHERE id = %s", (val, sid))

        if body.period_start is not None or body.period_end is not None:
            a = body.period_start if body.period_start is not None else scenario["period_start"]
            b = body.period_end if body.period_end is not None else scenario["period_end"]
            _check_period(a, b)
            conn.execute("UPDATE scenario SET period_start = %s, period_end = %s WHERE id = %s", (a, b, sid))

        # Projection mode + reference are set together; 'none' turns it off.
        if body.projection_mode is not None:
            mode = body.projection_mode or "none"
            _validate_projection(mode, body.ref_actuals_id, scenario["actuals_id"])
            conn.execute(
                "UPDATE scenario SET projection_mode = %s, ref_fy = %s, projection_union = %s "
                "WHERE id = %s",
                (None if mode == "none" else mode,
                 body.ref_actuals_id if mode != "none" else None,
                 mode != "none" and bool(body.projection_union), sid))

        if body.growth_overrides is not None:
            conn.execute("DELETE FROM growth_override WHERE scenario_id = %s", (sid,))
            _replace(conn,
                     "INSERT INTO growth_override (scenario_id, level, country, brand, customer, growth_pct) "
                     "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (scenario_id, level, country, brand, customer) "
                     "DO UPDATE SET growth_pct = EXCLUDED.growth_pct",
                     [(sid, g.level, g.country, g.brand,
                       g.customer if g.level == "customer" else "", g.growth_pct)
                      for g in body.growth_overrides])
        if body.pacing_overrides is not None:
            conn.execute("DELETE FROM pacing_override WHERE scenario_id = %s", (sid,))
            rows = []
            for p in body.pacing_overrides:
                if len(p.weights) != 12:
                    raise HTTPException(400, "pacing weights must have 12 values")
                brand = p.brand if p.level == "brand" else ""
                rows += [(sid, p.level, p.country, brand, i, wt) for i, wt in enumerate(p.weights)]
            _replace(conn,
                     "INSERT INTO pacing_override (scenario_id, level, country, brand, month_idx, weight) "
                     "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (scenario_id, level, country, brand, month_idx) "
                     "DO UPDATE SET weight = EXCLUDED.weight", rows)
        if body.brand_exclusions is not None:
            conn.execute("DELETE FROM brand_exclusion WHERE scenario_id = %s", (sid,))
            _replace(conn, "INSERT INTO brand_exclusion (scenario_id, country, brand) VALUES (%s,%s,%s) "
                           "ON CONFLICT DO NOTHING",
                     [(sid, e.country, e.brand) for e in body.brand_exclusions])
        if body.annualize is not None:
            conn.execute("DELETE FROM annualize WHERE scenario_id = %s", (sid,))
            _replace(conn, "INSERT INTO annualize (scenario_id, country, brand) VALUES (%s,%s,%s) "
                           "ON CONFLICT DO NOTHING",
                     [(sid, a.country, a.brand) for a in body.annualize])
        if body.manual_lines is not None:
            conn.execute("DELETE FROM manual_line WHERE scenario_id = %s", (sid,))
            rows = []
            for m in body.manual_lines:
                if m.months is not None:
                    if len(m.months) != 12:
                        raise HTTPException(400, "manual line months must have 12 values")
                    rows.append((sid, m.country, m.brand, m.customer, sum(m.months), json.dumps(m.months)))
                else:
                    rows.append((sid, m.country, m.brand, m.customer, m.target_annual, None))
            _replace(conn,
                     "INSERT INTO manual_line (scenario_id, country, brand, customer, target_annual, months) "
                     "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (scenario_id, country, brand, customer) "
                     "DO UPDATE SET target_annual = EXCLUDED.target_annual, months = EXCLUDED.months", rows)

        conn.execute("UPDATE scenario SET updated_at = now() WHERE id = %s", (sid,))
        conn.commit()
        return _computed_payload(conn, _scenario_row(conn, sid))


@router.post("/api/scenarios/{sid}/duplicate")
def duplicate_scenario(sid: int, body: ScenarioIn, request: Request):
    with _db() as conn:
        src = _scenario_row(conn, sid)
        new_id = conn.execute(
            "INSERT INTO scenario (name, target_fy, default_growth_pct, actuals_fy, projection_mode, "
            "ref_fy, projection_union, created_by, period_start, period_end, start_from, base_kind, base_label) "
            "SELECT %s, %s, default_growth_pct, actuals_fy, projection_mode, ref_fy, projection_union, %s, "
            "period_start, period_end, start_from, base_kind, base_label FROM scenario WHERE id = %s RETURNING id",
            (body.name, body.target_fy or src["target_fy"], _who(request), sid)).fetchone()["id"]
        for table, cols in (("growth_override", "level, country, brand, customer, growth_pct"),
                            ("pacing_override", "level, country, brand, month_idx, weight"),
                            ("brand_exclusion", "country, brand"),
                            ("annualize", "country, brand"),
                            ("manual_line", "country, brand, customer, target_annual, months"),
                            ("base_line", "country, brand, customer, month_idx, amount")):
            conn.execute(f"INSERT INTO {table} (scenario_id, {cols}) "
                         f"SELECT %s, {cols} FROM {table} WHERE scenario_id = %s", (new_id, sid))
        conn.commit()
        return _computed_payload(conn, _scenario_row(conn, new_id))


@router.delete("/api/scenarios/{sid}")
def delete_scenario(sid: int):
    with _db() as conn:
        conn.execute("DELETE FROM scenario WHERE id = %s", (sid,))
        conn.commit()
        return {"deleted": sid}


@router.get("/api/scenarios/{sid}/export.csv")
def export_csv(sid: int, scope: str = "full"):
    with _db() as conn:
        scenario = _scenario_row(conn, sid)
        content = csv_export.build_csv(conn, scenario, scope=scope)
        suffix = "_H1" if scope == "h1" else ""
        fname = f"{scenario['name']}_{scenario['target_fy']}{suffix}_budget.csv".replace(" ", "_")
        return PlainTextResponse(content, media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/api/duplicates")
def get_duplicates():
    return duplicates.find_duplicates(None)


# ---------- actuals (NetSuite, read-only) ----------

@router.get("/api/actuals")
def list_actuals():
    """Every financial year with NetSuite sales, newest first, with how many scenarios use it."""
    with _db() as conn:
        used = {r["actuals_fy"]: r["n"] for r in conn.execute(
            "SELECT actuals_fy, count(*) AS n FROM scenario GROUP BY 1")}
    return [{**y, "scenario_count": used.get(y["id"], 0)} for y in actuals.years()]


# ---------- committed (official) targets ----------

def _commit_meta(r) -> dict:
    return {"id": r["id"], "target_fy": r["target_fy"], "scenario_id": r["scenario_id"],
            "scenario_name": r["scenario_name"], "note": r["note"],
            "period_start": r["period_start"], "period_end": r["period_end"],
            "period_label": period_label(r["period_start"], r["period_end"]),
            "countries": list(r["countries"]),
            "committed_by": r["committed_by"],
            "committed_at": r["committed_at"].astimezone(SYDNEY).strftime("%Y-%m-%d %H:%M"),
            "total_au": float(r["total_au"]), "total_nz": float(r["total_nz"])}


fy_end_year = base.fy_end_year


def _countries(chosen):
    chosen = chosen or COUNTRIES
    bad = [c for c in chosen if c not in COUNTRIES]
    if bad or not chosen:
        raise HTTPException(400, "countries must be Australia and/or New Zealand")
    return [c for c in COUNTRIES if c in chosen]


def commit_scenario(conn, sid: int, note: str, who: str, countries=None) -> int:
    """Freeze the scenario's computed targets (exactly what the grid shows) for the months
    of its period as a new commit; months outside the period are left to earlier commits."""
    payload = _computed_payload(conn, _scenario_row(conn, sid))
    sc = payload["scenario"]
    fy = fy_end_year(sc["target_fy"])
    if fy is None:
        raise HTTPException(400, f"Can't tell which financial year \"{sc['target_fy']}\" is - name it like FY27.")
    months = payload["period"]["months"]
    chosen = _countries(countries)
    rows, totals = [], {"Australia": 0.0, "New Zealand": 0.0}
    for c in payload["countries"]:
        if c["country"] not in chosen:
            continue
        for b in c["brands"]:
            for cell in b["customers"]:
                for i in months:
                    amt = cell["target_months"][i]
                    if amt:
                        rows.append((c["country"], b["brand"], cell["customer"],
                                     actuals.month_start(fy, i), round(amt, 2)))
                        totals[c["country"]] += amt
    if not rows:
        raise HTTPException(400, f"This scenario has no targets in its period ({payload['period']['label']}) to commit.")
    cid = conn.execute(
        "INSERT INTO target_commit (target_fy, scenario_id, scenario_name, note, committed_by, "
        "total_au, total_nz, period_start, period_end, countries) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (sc["target_fy"], sid, sc["name"], note.strip(), who,
         round(totals["Australia"], 2), round(totals["New Zealand"], 2),
         sc["period_start"], sc["period_end"], chosen)).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO committed_target (commit_id, country, brand, customer, month_start, amount) "
            "VALUES (%s,%s,%s,%s,%s,%s)", [(cid,) + r for r in rows])
    conn.commit()
    return cid


@router.post("/api/scenarios/{sid}/commit-preview")
def commit_preview(sid: int, body: CommitIn):
    """What a commit would change: per country and month of the period, the official
    target now and after. Warns where a country would lose official targets."""
    from .db import FY_MONTHS
    with _db() as conn:
        payload = _computed_payload(conn, _scenario_row(conn, sid))
        sc = payload["scenario"]
        if fy_end_year(sc["target_fy"]) is None:
            raise HTTPException(400, f"Can't tell which financial year \"{sc['target_fy']}\" is - name it like FY27.")
        off = base.official(conn, sc["target_fy"])
    chosen = _countries(body.countries)
    months = payload["period"]["months"]
    out, warnings = [], []
    for c in payload["countries"]:
        name = c["country"]
        now = [sum(m[i] for m in off.get(name, {}).values()) for i in range(12)]
        plan = c["totals"]["target_months"]
        rows = [{"month": FY_MONTHS[i], "now": round(now[i], 2),
                 "after": round(plan[i], 2) if name in chosen else round(now[i], 2)} for i in months]
        n_now, n_after = sum(r["now"] for r in rows), sum(r["after"] for r in rows)
        if name in chosen and n_after == 0 and n_now > 0:
            warnings.append(f"{name}: this would replace ${n_now:,.0f} of official {payload['period']['label']} "
                            f"targets with $0. Untick {name} to leave them as they are.")
        elif name in chosen and n_after == 0:
            warnings.append(f"{name} has no targets in this scenario for {payload['period']['label']}.")
        out.append({"country": name, "included": name in chosen, "rows": rows,
                    "now": round(n_now, 2), "after": round(n_after, 2)})
    return {"period": payload["period"], "target_fy": sc["target_fy"], "countries": out, "warnings": warnings}


@router.post("/api/scenarios/{sid}/commit")
def commit(sid: int, body: CommitIn, request: Request):
    with _db() as conn:
        cid = commit_scenario(conn, sid, body.note, _who(request), body.countries)
        row = conn.execute("SELECT * FROM target_commit WHERE id = %s", (cid,)).fetchone()
    warehouse.drop(("sales_targets",))      # the home page shows the new targets at once
    return _commit_meta(row)


@router.get("/api/commits")
def list_commits():
    """Every commit, newest first. `official` = the months (fiscal idx) for which it is
    still the official commit; `current` = it is official for at least one month."""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM target_commit ORDER BY id DESC").fetchall()
    taken: dict = {}
    out = []
    for r in rows:
        d = _commit_meta(r)
        mine = [i for i in range(r["period_start"], r["period_end"] + 1)
                if i not in taken.setdefault(r["target_fy"], set())]
        taken[r["target_fy"]].update(mine)
        d["official_months"] = mine
        d["current"] = bool(mine)
        out.append(d)
    return out


@router.get("/api/commits/{cid}/export.csv")
def export_commit(cid: int):
    with _db() as conn:
        meta = conn.execute("SELECT * FROM target_commit WHERE id = %s", (cid,)).fetchone()
        if not meta:
            raise HTTPException(404, "Commit not found")
        lines = ["Country,Brand,Customer,Month,Amount"]
        for r in conn.execute(
                "SELECT country, brand, customer, month_start, amount FROM committed_target "
                "WHERE commit_id = %s ORDER BY country, brand, customer, month_start", (cid,)):
            cells = [r["country"], r["brand"], r["customer"], r["month_start"].strftime("%b %Y"),
                     f"{r['amount']:.2f}"]
            lines.append(",".join('"' + str(x).replace('"', '""') + '"' for x in cells))
    fname = f"targets_{meta['target_fy']}_commit{cid}.csv"
    return PlainTextResponse("\r\n".join(lines) + "\r\n", media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/api/status")
def status():
    """Whether the tool can run here (the page shows a message if not)."""
    try:
        with connect():
            pass
    except NotConfigured as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return {"ok": True}
