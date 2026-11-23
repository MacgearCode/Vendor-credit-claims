"""Portfolios (app/portfolio.py): PM teams and account-manager overrides. The pure rules run
anywhere; the database tests use the local Postgres in a throwaway schema (as test_brand_profit)."""
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import portfolio, sales_explorer, warehouse
from app.brand_profit import db
from app.brand_profit.settings import Invalid
from tests.test_brand_profit import URL, needs_db

D = date


def _team(pm, senior, f, t=None):
    return {"pm": pm, "reports_to": senior, "valid_from": f, "valid_to": t}


TEAM = [_team("Prasanna Wad", "David Vanderkley", D(2026, 10, 1)),
        _team("Jeffrey Zhu", "Rachel Harris", D(2026, 4, 1)),
        _team("Charles Cruz", "Dale Farnham", D(2026, 9, 1)),
        _team("Carlo De Guzman", "Dale Farnham", D(2026, 9, 1))]


def test_team_membership_follows_the_month():
    ok = portfolio.in_team(TEAM, "Dale Farnham")
    assert ok(D(2026, 9, 1), "Dale Farnham")               # the senior's own brands
    assert ok(D(2026, 9, 1), "Charles Cruz") and ok(D(2026, 11, 1), "Carlo De Guzman")
    assert not ok(D(2026, 8, 1), "Charles Cruz")            # before 1 Sep: not Dale's team
    assert not ok(D(2026, 9, 1), "Jeffrey Zhu")
    assert portfolio.seniors(TEAM, [D(2026, 8, 1)]) == ["Rachel Harris"]
    assert portfolio.seniors(TEAM, [D(2026, 10, 1)]) == ["Dale Farnham", "David Vanderkley", "Rachel Harris"]


def test_account_manager_brand_row_then_retailer_row_then_netsuite():
    rows = [{"region": "AU", "retailer": "JB HIFI", "brand": "MOVA", "am": "Mike Bihary",
             "valid_from": D(2026, 4, 1), "valid_to": None},
            {"region": "AU", "retailer": "Harvey Norman", "brand": "", "am": "Samantha Musson",
             "valid_from": D(2026, 4, 1), "valid_to": None},
            {"region": "AU", "retailer": "Costco Wholesale", "brand": "", "am": "Old Rep",
             "valid_from": D(2026, 4, 1), "valid_to": D(2026, 6, 1)},          # ended: NetSuite again
            {"region": "NZ", "retailer": "JB HIFI", "brand": "", "am": "NZ Person",
             "valid_from": D(2026, 4, 1), "valid_to": None}]
    ov = portfolio.am_overrides("AU", D(2026, 11, 24), rows)
    assert portfolio.resolve(ov, "JB HIFI", "Mova", "Chris O'Donohue") == "Mike Bihary"
    assert portfolio.resolve(ov, "jb hifi ", "ANKER", "Chris O'Donohue") == "Chris O'Donohue"
    assert portfolio.resolve(ov, "Harvey Norman", "ANKER", "Jeremy Sedley") == "Samantha Musson"
    assert portfolio.resolve(ov, "Costco Wholesale", "ANKER", "Jeremy Sedley") == "Jeremy Sedley"
    assert ("JB HIFI", "") not in ov                        # NZ rows stay in NZ


def test_sales_explorer_manager_is_per_customer_and_brand():
    nm = {"cust": {1: ("JB Hi-Fi Store", "JB HIFI", 10), 2: ("Other", "Other", None)},
          "am_cb": {(1, 237): 20}, "rep": {10: "Chris", 20: "Mike"}}
    assert sales_explorer._am(1, 237, nm) == 20            # Mova at JB -> Mike
    assert sales_explorer._am(1, 250, nm) == 10            # Anker at JB -> NetSuite's
    assert sales_explorer._am(2, 237, nm) is None
    assert sales_explorer._label("rep", 1, 237, None, 202610, nm) == "Mike"
    assert sales_explorer._filter_ok({"rep": 20}, 1, 237, None, nm)
    assert not sales_explorer._filter_ok({"rep": 20}, 1, 250, None, nm)


def test_targets_follow_the_portfolio_manager(monkeypatch):
    monkeypatch.setattr(sales_explorer, "target_lines", lambda: {"AU": [
        (D(2026, 10, 1), "MOVA", "JB HIFI", 100.0), (D(2026, 10, 1), "ANKER", "JB HIFI", 50.0),
        (D(2026, 10, 1), "ANKER", "Harvey Norman", 30.0)]})
    nm = {"brand": {}, "rep": {10: "Chris", 20: "Mike", 30: "Sam", 40: "Jeremy"},
          "am_rb": {("AU", "JB HIFI", "MOVA"): 20}, "am_ra": {("AU", "HARVEY NORMAN"): 30}}
    ams = {"JB HIFI": 10, "Harvey Norman": 40}
    tot, rows = sales_explorer.targets("AU", D(2026, 10, 1), D(2026, 10, 31), ["rep"], {}, nm, ams)
    assert tot == 180 and rows == {("Mike",): 100.0, ("Chris",): 50.0, ("Sam",): 30.0}
    tot, _ = sales_explorer.targets("AU", D(2026, 10, 1), D(2026, 10, 31), [], {"rep": 20}, nm, ams)
    assert tot == 100


def test_workbook_seed_rows():
    path = Path(__file__).resolve().parents[1] / "AM PM Assignment.xlsx"
    if not path.exists():
        pytest.skip("workbook not here")
    rows, skipped = portfolio.seed_rows_from_workbook(path)
    key = {(r["retailer"], r["brand"]): r["am"] for r in rows}
    assert key[("JB HIFI", "MOVA")] == "Mike Bihary" and key[("JB HIFI", "ANKER")] == "Chris O'Donohue"
    assert key[("Harvey Norman", "")] == "Samantha Musson"
    assert key[("Big W", "")] == key[("Duke Living Pty Ltd", "")] == "Deborah Clucas"
    assert ("The Good Guys", "WITHINGS") in key
    assert len([s for s in skipped if "EUFY" in s.upper()]) == 3          # Security / Floorcare / Baby
    assert all(r["region"] == "AU" for r in rows)


# ---- database --------------------------------------------------------------------------------
@pytest.fixture()
def conn(monkeypatch):
    s = "pf_test_" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("BRAND_PROFIT_DB_URL", URL)
    monkeypatch.setenv("BRAND_PROFIT_SCHEMA", s)
    c = db.connect()
    yield c
    c.close()
    warehouse.drop(portfolio.CACHE_PREFIXES)
    with psycopg.connect(URL, autocommit=True) as x:
        x.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")


@needs_db
def test_reassigning_a_pm_ends_the_old_row_the_month_before(conn):
    portfolio.assign_team(conn, "Charles Cruz", "Rachel Harris", "2026-04", "a@x")
    portfolio.assign_team(conn, "Charles Cruz", "Dale Farnham", "2026-09", "a@x")
    rows = conn.execute("SELECT * FROM pm_team ORDER BY valid_from").fetchall()
    assert [(r["reports_to"], r["valid_from"], r["valid_to"]) for r in rows] == [
        ("Rachel Harris", D(2026, 4, 1), D(2026, 8, 1)), ("Dale Farnham", D(2026, 9, 1), None)]
    assert portfolio.reports_to(rows, "Charles Cruz", D(2026, 8, 1)) == "Rachel Harris"
    with pytest.raises(Invalid, match="already"):
        portfolio.assign_team(conn, "Charles Cruz", "Dale Farnham", "2026-11", "a@x")
    with pytest.raises(Invalid, match="later row"):
        portfolio.assign_team(conn, "Charles Cruz", "David Vanderkley", "2026-06", "a@x")
    with pytest.raises(Invalid, match="loop"):
        portfolio.assign_team(conn, "Dale Farnham", "Charles Cruz", "2026-10", "a@x")
    portfolio.assign_team(conn, "Charles Cruz", "", "2026-12", "a@x")          # stands alone
    rows = conn.execute("SELECT * FROM pm_team ORDER BY valid_from").fetchall()
    assert rows[-1]["valid_to"] == D(2026, 11, 1) and len(rows) == 2
    n = conn.execute("SELECT count(*) AS n FROM change_log WHERE entity = 'pm_team'").fetchone()["n"]
    assert n >= 4


@needs_db
def test_account_manager_rows_reassign_and_same_month_replaces(conn):
    f = {"region": "AU", "retailer": "JB HIFI", "brand": "mova", "am": "Mike Bihary", "valid_from": "2026-04"}
    portfolio.assign_am(conn, f, "a@x")
    portfolio.assign_am(conn, {**f, "am": "Chris O'Donohue", "valid_from": "2026-04"}, "a@x")   # same month: replaced
    rows = conn.execute("SELECT * FROM am_assignment").fetchall()
    assert len(rows) == 1 and rows[0]["am"] == "Chris O'Donohue" and rows[0]["brand"] == "MOVA"
    portfolio.assign_am(conn, {**f, "am": "", "valid_from": "2026-11"}, "a@x")                  # back to NetSuite
    rows = conn.execute("SELECT * FROM am_assignment").fetchall()
    assert len(rows) == 1 and rows[0]["valid_to"] == D(2026, 10, 1)
    with pytest.raises(Invalid):
        portfolio.assign_am(conn, {**f, "region": "UK"}, "a@x")
    portfolio.delete_row(conn, "am_assignment", rows[0]["id"], "a@x")
    assert not conn.execute("SELECT * FROM am_assignment").fetchall()


@needs_db
def test_portfolios_page_posts_and_lists(conn, monkeypatch):
    from app import main
    from app.db import SessionLocal
    from app.models import User
    s = SessionLocal(); s.query(User).delete(); s.commit(); s.close()          # no users = auth off
    monkeypatch.setattr(warehouse, "query", lambda *a, **k: (_ for _ in ()).throw(warehouse.WarehouseUnavailable("x")))
    client = TestClient(main.app)
    r = client.post("/admin/portfolios/team", data={"pm": "Mia Chen", "reports_to": "Rachel Harris", "month": "2026-04"},
                    follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    r = client.post("/admin/portfolios/am", data={"region": "AU", "retailer": "Harvey Norman", "brand": "",
                                                  "am": "Samantha Musson", "valid_from": "2026-04"}, follow_redirects=False)
    assert "ok=" in r.headers["location"]
    page = client.get("/admin/portfolios")
    assert page.status_code == 200 and "Samantha Musson" in page.text and "Mia Chen" in page.text
    assert "every brand" in page.text
