"""Sales Explorer: financial-year periods, how a date range is split between the
monthly and daily summaries, slicing, and who may open it."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import main, sales_explorer as sx
from app.db import SessionLocal
from app.models import User
from app.security import sign

TODAY = date(2026, 10, 15)


# ---- periods ---------------------------------------------------------------------
@pytest.mark.parametrize("code,fy,a,b,ly_a,ly_b", [
    ("ytd", 2027, date(2026, 4, 1), TODAY, date(2025, 4, 1), date(2025, 10, 15)),
    ("mtd", 2027, date(2026, 10, 1), TODAY, date(2025, 10, 1), date(2025, 10, 15)),
    ("qtd", 2027, date(2026, 10, 1), TODAY, date(2025, 10, 1), date(2025, 10, 15)),
    ("week", 2027, date(2026, 10, 12), TODAY, date(2025, 10, 12), date(2025, 10, 15)),
    ("yesterday", 2027, date(2026, 10, 14), date(2026, 10, 14), date(2025, 10, 14), date(2025, 10, 14)),
    ("fy", 2026, date(2025, 4, 1), date(2026, 3, 31), date(2024, 4, 1), date(2025, 3, 31)),
    ("q1", 2027, date(2026, 4, 1), date(2026, 6, 30), date(2025, 4, 1), date(2025, 6, 30)),
    ("q4", 2026, date(2026, 1, 1), date(2026, 3, 31), date(2025, 1, 1), date(2025, 3, 31)),
    ("m1", 2027, date(2026, 4, 1), date(2026, 4, 30), date(2025, 4, 1), date(2025, 4, 30)),
    ("m10", 2026, date(2026, 1, 1), date(2026, 1, 31), date(2025, 1, 1), date(2025, 1, 31)),
    ("m7", 2027, date(2026, 10, 1), TODAY, date(2025, 10, 1), date(2025, 10, 15)),   # running: to date
    ("fy", 2027, date(2026, 4, 1), TODAY, date(2025, 4, 1), date(2025, 10, 15)),     # running: to date
])
def test_periods_follow_the_financial_year(code, fy, a, b, ly_a, ly_b):
    p = sx.period(code, fy, TODAY)
    assert (p["a"], p["b"], p["ly_a"], p["ly_b"]) == (a, b, ly_a, ly_b)


def test_future_period_is_flagged():
    assert sx.period("m9", 2027, TODAY)["future"] is True        # Dec 2026


def test_ranges_use_whole_months_and_days_only_at_the_edges():
    assert sx._parts(date(2026, 4, 1), date(2026, 10, 15)) == [("m", 202604, 202609), ("d", 20261001, 20261015)]
    assert sx._parts(date(2026, 9, 28), date(2026, 10, 4)) == [("d", 20260928, 20260930), ("d", 20261001, 20261004)]
    assert sx._parts(date(2025, 4, 1), date(2026, 3, 31)) == [("m", 202504, 202603)]


# ---- slicing ---------------------------------------------------------------------
def _cube_fixture(monkeypatch):
    # account manager comes from the CUSTOMER record (third field), not the invoice rep
    nm = {"cust": {1: ("JB Hi-Fi Chatswood", "JB HIFI", 7), 2: ("HN Auburn", "Harvey Norman", 8)},
          "brand": {10: "MOVA", 11: "EUFY"}, "rep": {7: "Chris", 8: "Jeremy", 9: "Invoice rep"}}
    monthly = sx.Cube(sorted([
        (202604, 1, 10, 9, 100.0, 60.0, 1), (202605, 2, 11, 9, 50.0, 20.0, 2),
        (202504, 1, 10, 9, 40.0, 30.0, 1), (202510, 1, 10, 9, 999.0, 0.0, 9)]))
    daily = sx.Cube(sorted([
        (20261003, 1, 10, 9, 10.0, 5.0, 1), (20261020, 1, 10, 9, 5000.0, 0.0, 1),   # after "today": excluded
        (20251002, 2, 11, 9, 4.0, 1.0, 1)]))
    monkeypatch.setattr(sx, "names", lambda: nm)
    monkeypatch.setattr(sx, "cubes", lambda region, today: (monthly, daily))


def test_explore_sums_months_and_days_and_compares_same_dates_ly(monkeypatch):
    _cube_fixture(monkeypatch)
    r = sx.explore("AU", "ytd", 2027, TODAY, ["retailer", "brand"], {}, True)
    got = {tuple(x["k"]): (x["v"], x["ly"]) for x in r["rows"]}
    assert got[("JB HIFI", "MOVA")] == (110.0, 40.0)          # Apr month + 3 Oct day; LY Apr only
    assert got[("Harvey Norman", "EUFY")] == (50.0, 4.0)      # LY 2 Oct 2025 day row
    assert r["kpis"]["v"] == 160 and r["kpis"]["gp"] == 160 - 85


def test_filters_and_margin_off(monkeypatch):
    _cube_fixture(monkeypatch)
    r = sx.explore("AU", "ytd", 2027, TODAY, ["brand"], {"retailer": "JB HIFI"}, False)
    assert [x["k"] for x in r["rows"]] == [["MOVA"]]
    assert "gp" not in r["rows"][0] and "gp" not in r["kpis"]


def test_month_rows_line_up_ly_on_this_years_label(monkeypatch):
    _cube_fixture(monkeypatch)
    r = sx.explore("AU", "ytd", 2027, TODAY, ["month"], {}, False)
    got = {x["k"][0]: (x["v"], x["ly"]) for x in r["rows"]}
    assert got["2026-04"] == (100.0, 40.0)


# ---- access ----------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    calls = []
    monkeypatch.setattr(main.sales_explorer, "explore",
                        lambda *a, **k: calls.append(a) or {"period": {"label": "x", "from": "2026-04-01", "to": "2026-10-15", "future": False},
                                                            "dims": ["retailer"], "dim_labels": ["Retailer"], "rows": [], "margin": a[6],
                                                            "currency": "AUD", "region": a[0], "kpis": {}, "quick": [], "trend": {}})
    monkeypatch.setattr(main.sales_explorer, "options", lambda r, t: {"brands": [], "reps": [], "retailers": [], "fys": [2027]})
    monkeypatch.setattr(main.receipts, "status", lambda *a: None)
    db = SessionLocal(); db.query(User).delete(); db.commit(); db.close()
    c = TestClient(main.app)
    c.calls = calls
    return c


def mk(role, areas, email):
    db = SessionLocal()
    u = User(email=email, name=email, role=role, password_hash="x", active=True,
             allowed_tools=json.dumps(areas), allowed_brands=json.dumps(["ANKER"]) if role == "vendor" else None)
    db.add(u); db.commit(); uid = u.id; db.close()
    return uid


def as_(c, uid):
    c.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


def test_sales_needs_the_area_and_the_dollar_tick(client):
    mk("admin", [], "boss@example.com")
    no_cap = mk("user", ["sales"], "a@example.com")
    as_(client, no_cap)
    assert client.get("/reports/sales").status_code == 403
    assert client.get("/reports/sales/data").status_code == 403
    assert "/reports/sales" not in client.get("/").text            # not offered either
    ok = mk("user", ["sales", "sell_in", "sell_in_value"], "b@example.com")
    as_(client, ok)
    assert client.get("/reports/sales/data").status_code == 200
    assert client.calls[-1][6] is False                             # no margin tick -> no GP


def test_vendor_never_reaches_sales_even_if_ticked(client):
    mk("admin", [], "boss@example.com")
    v = mk("vendor", ["sales", "sell_in", "sell_in_value", "margin"], "v@example.com")
    as_(client, v)
    for path in ("/reports/sales", "/reports/sales/data", "/reports/sales/export.xlsx"):
        assert client.get(path).status_code == 403, path
    assert client.calls == []


def test_account_manager_is_the_customers_current_manager_not_the_invoice_rep(monkeypatch):
    _cube_fixture(monkeypatch)
    r = sx.explore("AU", "ytd", 2027, TODAY, ["rep"], {}, False)
    assert {x["k"][0]: x["v"] for x in r["rows"]} == {"Chris": 110.0, "Jeremy": 50.0}
    r = sx.explore("AU", "ytd", 2027, TODAY, ["retailer"], {"rep": 8}, False)
    assert [x["k"] for x in r["rows"]] == [["Harvey Norman"]] and r["kpis"]["v"] == 50
