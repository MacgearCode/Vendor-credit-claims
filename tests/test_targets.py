"""Targets tool on Postgres: the API under /targets, actuals from the warehouse (faked
here), commits freeze the grid, targets.sales_target = the newest commit per FY (what
the home page reads), and only staff with the area get in.

Needs the LOCAL Postgres (SELLTHROUGH_LOCAL_DB_URL in Sell Through/.env); each run uses
a throwaway schema that is dropped afterwards. Skipped when that database isn't there."""
import json
import os
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import main, sales, warehouse
from app.db import SessionLocal
from app.models import User
from app.security import sign
from app.targets import actuals, db as tdb, routes


def _local_url():
    env = Path(__file__).resolve().parents[2] / "Sell Through" / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("SELLTHROUGH_LOCAL_DB_URL="):
            url = line.split("=", 1)[1].strip().strip('"')
            try:
                psycopg.connect(url, connect_timeout=3).close()
                return url
            except psycopg.OperationalError:
                return None
    return None


URL = _local_url()
pytestmark = pytest.mark.skipif(URL is None, reason="local Postgres not available")

# FY26 (end year 2026): AU MOVA @ Harvey Norman $1,000 Apr + $3,000 May; NZ $500 Apr
FAKE = {"Australia": {("MOVA", "Harvey Norman"): [1000, 3000] + [0] * 10},
        "New Zealand": {("MOVA", "Noel Leeming"): [500] + [0] * 11}}


@pytest.fixture()
def client(monkeypatch):
    s = "targets_test_" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("TARGETS_DB_URL", URL)
    monkeypatch.setenv("TARGETS_SCHEMA", s)
    monkeypatch.setattr(actuals, "cells", lambda fy: FAKE if fy == 2026 else {c: {} for c in actuals.COUNTRIES})
    monkeypatch.setattr(actuals, "years", lambda: [actuals.meta(2026, {"AU": 4000, "NZ": 500})])
    monkeypatch.setattr(main.receipts, "status", lambda *a: None)
    db = SessionLocal(); db.query(User).delete(); db.commit(); db.close()
    yield TestClient(main.app)           # no users = auth off (local dev)
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")


def scenario(c, growth=0.10):
    r = c.post("/targets/api/scenarios", json={"name": "Draft", "actuals_id": 2026,
                                               "default_growth_pct": growth})
    assert r.status_code == 200, r.text
    return r.json()["scenario"]


def test_new_scenario_targets_the_year_after_its_actuals(client):
    assert client.get("/targets").status_code == 200
    sc = scenario(client)
    assert sc["target_fy"] == "FY27" and sc["actuals_id"] == 2026
    p = client.get(f"/targets/api/scenarios/{sc['id']}").json()
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["target_annual"] == pytest.approx(4400)     # 4,000 x 1.10
    assert p["dataset"]["source_fy"] == "FY26"


def test_inputs_round_trip(client):
    sid = scenario(client)["id"]
    body = {"growth_overrides": [{"level": "brand", "country": "Australia", "brand": "MOVA", "growth_pct": 0.5}],
            "manual_lines": [{"country": "Australia", "brand": "NEW", "customer": "JB HIFI",
                              "months": [0, 0, 0, 100] + [100] * 8}]}
    p = client.put(f"/targets/api/scenarios/{sid}", json=body).json()
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["target_annual"] == pytest.approx(6000 + 900)
    assert p["inputs"]["manual_lines"][0]["months"][3] == 100
    dup = client.post(f"/targets/api/scenarios/{sid}/duplicate", json={"name": "Copy"}).json()
    assert dup["inputs"]["growth_overrides"] == p["inputs"]["growth_overrides"]


def test_commit_freezes_the_grid_and_the_view_shows_the_newest(client, monkeypatch):
    sid = scenario(client)["id"]
    c1 = client.post(f"/targets/api/scenarios/{sid}/commit", json={"note": "first"}).json()
    assert (c1["total_au"], c1["total_nz"], c1["committed_by"]) == (pytest.approx(4400), pytest.approx(550), "local")
    client.put(f"/targets/api/scenarios/{sid}", json={"default_growth_pct": 0.5})
    c2 = client.post(f"/targets/api/scenarios/{sid}/commit", json={"note": "second"}).json()
    assert c2["total_au"] == pytest.approx(6000)
    assert [(x["id"], x["current"]) for x in client.get("/targets/api/commits").json()] == \
        [(c2["id"], True), (c1["id"], False)]
    # the home page reads the view (through the Targets connection): newest commit only,
    # FY27 month 0 = 1 Apr 2026
    t = sales.targets()
    assert t["AU"] == {date(2026, 4, 1): pytest.approx(1500), date(2026, 5, 1): pytest.approx(4500)}
    assert t["NZ"] == {date(2026, 4, 1): pytest.approx(750)}
    csv = client.get(f"/targets/api/commits/{c1['id']}/export.csv").text
    assert '"Australia","MOVA","Harvey Norman","Apr 2026","1100.00"' in csv


def test_nothing_committed_means_no_targets(client, monkeypatch):
    scenario(client)                      # creates the schema; nothing committed
    assert sales.targets() == {}


def test_without_a_database_the_api_says_so(client, monkeypatch):
    monkeypatch.delenv("TARGETS_DB_URL")
    r = client.get("/targets/api/scenarios")
    assert r.status_code == 503 and "TARGETS_DB_URL" in r.text


@pytest.mark.parametrize("fy,idx,expected", [(2027, 0, date(2026, 4, 1)), (2027, 8, date(2026, 12, 1)),
                                             (2027, 9, date(2027, 1, 1)), (2027, 11, date(2027, 3, 1))])
def test_fiscal_months_are_april_to_march(fy, idx, expected):
    assert actuals.month_start(fy, idx) == expected


@pytest.mark.parametrize("today,fy,months", [(date(2026, 10, 22), 2027, set(range(6))),   # Apr-Sep complete
                                              (date(2026, 10, 22), 2026, set(range(12))),
                                              (date(2026, 10, 22), 2028, set())])
def test_only_complete_months_count_as_actuals(today, fy, months):
    assert actuals.covered(fy, today) == months


def mk(role, areas, email):
    db = SessionLocal()
    u = User(email=email, name=email, role=role, password_hash="x", active=True,
             allowed_tools=json.dumps(areas), allowed_brands=json.dumps(["MOVA"]) if role == "vendor" else None)
    db.add(u); db.commit(); uid = u.id; db.close()
    return uid


def test_only_staff_with_the_area(client):
    mk("admin", [], "boss@example.com")
    for role, areas, code in (("user", ["targets"], 200), ("user", ["promos"], 403), ("vendor", ["targets"], 403)):
        client.cookies.set(main.COOKIE_NAME, sign(str(mk(role, areas, f"{role}{len(areas[0])}@x.com")), main.APP_SECRET))
        assert client.get("/targets").status_code == code, (role, areas)
        assert client.get("/targets/api/scenarios").status_code == code, (role, areas)


def test_commit_records_who(client):
    mk("admin", [], "boss@example.com")
    client.cookies.set(main.COOKIE_NAME, sign(str(mk("user", ["targets"], "planner@example.com")), main.APP_SECRET))
    sid = scenario(client)["id"]
    assert client.post(f"/targets/api/scenarios/{sid}/commit", json={}).json()["committed_by"] == "planner@example.com"


# ---- periods ------------------------------------------------------------------------
def test_a_q4_commit_replaces_only_jan_to_mar(client):
    full = scenario(client)["id"]                                  # FY27 full year, +10%
    client.post(f"/targets/api/scenarios/{full}/commit", json={})
    # a blank Q4 scenario with one line: $100 a month all year, but only Jan-Mar commit
    q4 = client.post("/targets/api/scenarios", json={"name": "Q4", "blank": True, "target_fy": "FY27",
                                                     "period_start": 9, "period_end": 11}).json()
    assert q4["period"] == {"start": 9, "end": 11, "label": "Q4", "months": [9, 10, 11]}
    client.put(f"/targets/api/scenarios/{q4['scenario']['id']}", json={"manual_lines": [
        {"country": "Australia", "brand": "MOVA", "customer": "Harvey Norman", "months": [100] * 12}]})
    c = client.post(f"/targets/api/scenarios/{q4['scenario']['id']}/commit", json={}).json()
    assert (c["period_label"], c["total_au"], c["total_nz"]) == ("Q4", 300, 0)
    t = sales.targets()
    assert t["AU"][date(2026, 4, 1)] == pytest.approx(1100)         # H1 still from the full-year commit
    assert t["AU"][date(2027, 1, 1)] == pytest.approx(100)          # Q4 from the Q4 commit
    assert t["NZ"] == {date(2026, 4, 1): pytest.approx(550)}        # NZ Q4 replaced by nothing
    lst = {x["period_label"]: x for x in client.get("/targets/api/commits").json()}
    assert lst["Q4"]["official_months"] == [9, 10, 11]
    assert lst["Full year"]["official_months"] == list(range(9)) and lst["Full year"]["current"]


def test_period_must_run_forward(client):
    r = client.post("/targets/api/scenarios", json={"name": "x", "blank": True, "period_start": 9, "period_end": 2})
    assert r.status_code == 400
    sid = scenario(client)["id"]
    assert client.put(f"/targets/api/scenarios/{sid}", json={"period_start": 6, "period_end": 8}).json()["period"]["label"] == "Q3"


def test_blank_scenario_has_no_actuals(client):
    p = client.post("/targets/api/scenarios", json={"name": "b", "blank": True, "target_fy": "FY27"}).json()
    assert p["dataset"] is None and all(not c["brands"] for c in p["countries"])


# ---- importing a targets workbook ---------------------------------------------------
def test_import_cleaning():
    from app.targets import importer
    rows = [("Australia", "Anker", "Mwave", 0, 10.0), ("Australia", "ANKER", "MWAVE", 0, 5.0),
            ("New Zealand", "MOVA", "MacGear NZ - Purchases AUD", 0, 99.0),
            ("Australia", "MOVA", "School Direct 30EOM � AU", 1, 7.0)]
    lines, rep = importer.clean(rows, {"ANKER", "MOVA"}, {"MWAVE", "School Direct 30EOM – AU"})
    assert lines[("Australia", "ANKER", "MWAVE")][0] == 15            # spellings merged, duplicates added
    assert rep["dropped_intercompany"] == 99
    assert lines[("Australia", "MOVA", "School Direct 30EOM – AU")][1] == 7   # garbled dash matched


def test_seeded_scenario_shows_the_imported_months_and_takes_growth(client):
    from app.targets import importer
    with tdb.connect() as conn:
        sid = importer.seed(conn, "Imported", "FY27",
                            {("Australia", "MOVA", "JB HIFI"): [1.0] * 9 + [0, 0, 0]}, (0, 8))
    p = client.get(f"/targets/api/scenarios/{sid}").json()
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["target_months"] == [1.0] * 9 + [0, 0, 0] and p["period"]["label"] == "Apr–Dec"
    assert p["start"]["label"] == "Imported targets"
    p = client.put(f"/targets/api/scenarios/{sid}", json={"default_growth_pct": 0.5}).json()   # growth works on it
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["target_months"][0] == pytest.approx(1.5)


# ---- planning-first creation, comparisons, per-country commits (2026-10-23) ---------
def new(c, **kw):
    body = {"name": "s", "target_fy": "FY27", "start_from": "last_year", "default_growth_pct": 0.1, **kw}
    r = c.post("/targets/api/scenarios", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_last_year_start_plans_the_named_year(client):
    p = new(client, period_start=0, period_end=0)
    assert (p["scenario"]["target_fy"], p["scenario"]["actuals_id"], p["start"]["kind"]) == ("FY27", 2026, "last_year")
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["cmp"]["ly"] == pytest.approx(1000)            # April only: last year's April
    assert p["compare"]["ly_label"] == "FY26" and p["status"] == {"committed": None}


def test_this_year_start_needs_a_year_under_way(client):
    r = client.post("/targets/api/scenarios", json={"name": "x", "target_fy": "FY28", "start_from": "this_year"})
    assert r.status_code == 400 and "isn't under way" in r.text


def test_official_start_takes_growth_on_top(client):
    sid = new(client)["scenario"]["id"]
    client.post(f"/targets/api/scenarios/{sid}/commit", json={})        # official FY27 = FY26 x 1.1
    p = new(client, start_from="official", default_growth_pct=0.0)
    assert p["start"]["label"].startswith("Official FY27 targets as at")
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["target_annual"] == pytest.approx(4400)
    assert p["compare"]["has_official"] and au["totals"]["cmp"]["official"] == pytest.approx(4400)
    p = client.put(f"/targets/api/scenarios/{p['scenario']['id']}", json={"default_growth_pct": 0.5}).json()
    au = next(x for x in p["countries"] if x["country"] == "Australia")
    assert au["totals"]["target_annual"] == pytest.approx(6600)        # 4,400 x 1.5


def test_official_start_needs_something_official(client):
    r = client.post("/targets/api/scenarios", json={"name": "x", "target_fy": "FY27", "start_from": "official"})
    assert r.status_code == 400 and "Nothing is official" in r.text


def test_commit_one_country_leaves_the_other(client):
    sid = new(client)["scenario"]["id"]
    client.post(f"/targets/api/scenarios/{sid}/commit", json={})          # AU 4,400 / NZ 550
    q4 = new(client, start_from="blank", period_start=0, period_end=0)["scenario"]["id"]
    client.put(f"/targets/api/scenarios/{q4}", json={"manual_lines": [
        {"country": "Australia", "brand": "MOVA", "customer": "Harvey Norman", "months": [7] + [0] * 11}]})
    pv = client.post(f"/targets/api/scenarios/{q4}/commit-preview", json={}).json()
    assert any("New Zealand: this would replace $550" in w for w in pv["warnings"])
    pv = client.post(f"/targets/api/scenarios/{q4}/commit-preview", json={"countries": ["Australia"]}).json()
    nz = next(c for c in pv["countries"] if c["country"] == "New Zealand")
    assert (nz["included"], nz["now"], nz["after"], pv["warnings"]) == (False, 550, 550, [])
    c = client.post(f"/targets/api/scenarios/{q4}/commit", json={"countries": ["Australia"]}).json()
    assert c["countries"] == ["Australia"]
    t = sales.targets()
    assert t["AU"][date(2026, 4, 1)] == pytest.approx(7)               # replaced
    assert t["NZ"][date(2026, 4, 1)] == pytest.approx(550)             # untouched
    assert t["AU"][date(2026, 5, 1)] == pytest.approx(3300)            # outside the period: untouched


def test_status_shows_commit_and_later_edits(client):
    sid = new(client)["scenario"]["id"]
    client.post(f"/targets/api/scenarios/{sid}/commit", json={})
    st = client.get(f"/targets/api/scenarios/{sid}").json()["status"]
    assert st["committed"]["period_label"] == "Full year" and st["edited_since"] is False
    st = client.put(f"/targets/api/scenarios/{sid}", json={"default_growth_pct": 0.2}).json()["status"]
    assert st["edited_since"] is True


def test_rename_and_list_show_the_period(client):
    sid = new(client, period_start=9, period_end=11)["scenario"]["id"]
    client.put(f"/targets/api/scenarios/{sid}", json={"name": "FY27 Q4 v2"})
    row = next(x for x in client.get("/targets/api/scenarios").json() if x["id"] == sid)
    assert (row["name"], row["period_label"]) == ("FY27 Q4 v2", "Q4")


def test_import_merges_spellings_netsuite_has_no_sales_for():
    from app.targets import importer
    rows = [("New Zealand", "Rivacase", "PB Tech", 0, 25.0), ("New Zealand", "RIVACASE", "PB Tech", 0, 150.0)]
    lines, rep = importer.clean(rows, set(), set())
    assert dict(lines) == {("New Zealand", "RIVACASE", "PB Tech"): [175.0] + [0.0] * 11}
