"""Inventory Ageing: the page's data shape, "aged since" runs, and who may open it."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import ageing, main
from app.db import SessionLocal
from app.models import User
from app.security import sign

D1, D2, D3, D4 = date(2026, 1, 22), date(2026, 9, 22), date(2026, 10, 21), date(2026, 10, 22)
DATES = [D1, D2, D3, D4]


def test_reference_dates():
    assert ageing.nearest_on_or_before(DATES, date(2026, 9, 22)) == D2
    assert ageing.nearest_on_or_before(DATES, date(2026, 10, 1)) == D2      # a missing day falls back
    assert ageing.nearest_on_or_before(DATES, date(2025, 1, 1)) == D1


def test_run_start_is_the_current_unbroken_run():
    assert ageing.run_start(DATES, {D2, D3, D4}) == "2026-09-22"
    assert ageing.run_start(DATES, {D1, D3, D4}) == "2026-10-21"            # D2 without it breaks the run
    assert ageing.run_start(DATES, {D1, D2, D3}) is None                    # not past the line today


def test_queries_draw_the_lines_at_whole_bands():
    assert ageing.BANDS == ("0_90", "91_120", "121_180", "181_240", "241_370", "370_plus")
    assert ageing._past(0) == "u_91_120 + u_121_180 + u_181_240 + u_241_370 + u_370_plus"   # over 90
    assert ageing._past(1) == "u_121_180 + u_181_240 + u_241_370 + u_370_plus"              # over 120
    assert ageing._past(3) == "u_241_370 + u_370_plus"                                      # over 240
    assert ageing._past(4) == "u_370_plus"
    assert ageing._RUNS_SQL.count("bool_or(") == 5


def test_shape():
    # six value bands, then six unit bands
    series = [(D4, "AU", 100, 12, 8, 3, 2, 1, 10, 1, 1, 1, 0, 1)]
    tpl = [(D4, "AU", 500, 3)]
    # item x location at latest, 30 days ago (D2) and first; a line gone since D2 still counts as a fall
    W = "AU2 – Melbourne Warehouse"
    lines = [(D4, "AU", "A1", "Item A", "MOVA", W, "Warehouse", 80, 6, 4, 3, 2, 1, 8, 1, 0, 1, 0, 1),
             (D2, "AU", "A1", "Item A old name", "MOVA", W, "Warehouse", 0, 30, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0),
             (D2, "AU", "B1", "Item B", "BONELK", "QA Melb", "QA", 0, 0, 7, 0, 0, 0, 0, 0, 2, 0, 0, 0),
             (D1, "AU", "A1", "Item A", "MOVA", W, "Warehouse", 0, 4, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0)]
    # past 90, 120, 180, 240, 370
    runs = [(D3, "AU", "A1", True, True, True, False, False),
            (D4, "AU", "A1", True, True, True, True, True)]
    out = ageing.shape(["AU"], series, tpl, lines, runs, DATES)

    assert out["ref"] == {"latest": "2026-10-22", "d30": "2026-09-22", "first": "2026-01-22"}
    au = out["regions"]["AU"]
    assert au["ccy"] == "AUD"
    assert au["series"]["v1"] == [0, 0, 0, 12.0] and au["series"]["u5"] == [0, 0, 0, 1.0]
    assert au["tpl"] == {"units": [0, 0, 0, 500.0], "skus": 3}
    by = {(l[0], l[1]): l for l in au["lines"]}
    # [code, loc, 6 value + 6 unit bands now, 6 value + 6 unit bands 30 days ago]
    assert by[("A1", W)] == ["A1", W, 80, 6, 4, 3, 2, 1, 8, 1, 0, 1, 0, 1,
                             0, 30, 0, 0, 0, 0, 0, 3, 0, 0, 0, 0]
    assert by[("B1", "QA Melb")][2:] == [0] * 12 + [0, 0, 7, 0, 0, 0, 0, 0, 2, 0, 0, 0]
    assert au["skus"]["A1"] == ["Item A", "MOVA", "2026-10-21", "2026-10-21", "2026-10-21",
                                "2026-10-22", "2026-10-22"]                    # latest name wins
    assert au["skus"]["B1"][2:] == [None] * 5
    assert out["locs"]["QA Melb"] == "QA"


# ---- access ------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(ageing, "for_user", lambda b, r: ageing.shape(r, [], [], [], [], DATES))
    db = SessionLocal(); db.query(User).delete(); db.commit(); db.close()
    return TestClient(main.app)


def mk(role, areas, email):
    db = SessionLocal()
    u = User(email=email, name=email, role=role, password_hash="x", active=True,
             allowed_tools=json.dumps(areas), allowed_brands=json.dumps(["MOVA"]) if role == "vendor" else None)
    db.add(u); db.commit(); uid = u.id; db.close()
    return uid


def as_(c, uid):
    c.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


def test_staff_with_the_area_see_it(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["ageing"], "ops@example.com"))
    r = client.get("/reports/ageing")
    assert r.status_code == 200 and "Stock by age" in r.text


def test_staff_without_the_area_are_refused(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["items"], "other@example.com"))
    assert client.get("/reports/ageing").status_code == 403


def test_vendor_never_reaches_it_even_if_ticked(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("vendor", ["ageing", "items"], "mova@example.com"))
    assert client.get("/reports/ageing").status_code == 403


def test_not_loaded_says_so(client, monkeypatch):
    def boom(b, r):
        raise ageing.NotLoaded("missing")
    monkeypatch.setattr(ageing, "for_user", boom)
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["ageing"], "ops@example.com"))
    r = client.get("/reports/ageing")
    assert r.status_code == 200 and "in the reporting warehouse yet" in r.text
