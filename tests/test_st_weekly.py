"""Weekly Sell-Through: the dataset rules in st_weekly.shape(), and who may load it."""
import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, st_weekly
from app.db import SessionLocal
from app.models import User
from app.security import sign

W = [date(2026, 9, 27), date(2026, 10, 4), date(2026, 10, 11)]
REG = {1: "AU", 2: "NZ"}


def st(rk=2, code="A1", brand="MOVA", cust="Noel Leeming", wk=W[-1], u=5, s=10, o=None, v=500.0):
    return (rk, code, code + " name", brand, cust, int(wk.strftime("%Y%m%d")), u, s, o, v)


def si(rk=2, code="A1", brand="MOVA", cust=None, group="Noel Leeming Group", wk=W[-1], q=3):
    return (rk, code, code + " name", brand, cust, group, wk, q)


def build(st_rows, si_rows=None, mg=None, eta=None, eta_loaded=False, regions=("AU", "NZ")):
    return st_weekly.shape(W, list(regions), REG, st_rows, si_rows, mg, eta, eta_loaded)


def unpack(p, n=len(W)):
    out = [0] * n
    if p:
        for k, x in enumerate(p[1]):
            out[p[0] + k] = x
    return out


@pytest.mark.parametrize("a,b", [("MWAVE", "Mwave"), ("Scorptec Computers", "Scorptec"),
                                 ("Bunnings (NZ) Limited", "Bunnings"), ("Noel Leeming Ltd", "noel-leeming")])
def test_names_normalise_like_the_guide(a, b):
    assert st_weekly.norm_name(a) == st_weekly.norm_name(b)


def test_sell_through_series_and_this_weeks_stock():
    d = build([st(wk=W[0], u=2, s=99), st(wk=W[-1], u=5, s=10, o=4, v=500)])
    r, = d["rows"]
    assert unpack(r[3]) == [2, 0, 5]
    assert r[5:8] == [10, 4, 500.0]                    # SOH / on order / value: latest week only
    assert d["customers"][r[2]] == {"name": "Noel Leeming", "reports": ["NZ"]}


def test_sell_in_reaches_the_retailer_by_name_when_unmapped():
    # NetSuite group "Noel Leeming Group" doesn't normalise to "Noel Leeming"; "Mwave" does reach MWAVE
    d = build([st(), st(cust="MWAVE", code="B2")],
              [si(group="Mwave", code="B2", q=7), si(group="Air New Zealand", q=40)])
    names = {c["name"]: c for c in d["customers"]}
    assert "Mwave" not in names                         # merged into the reporting retailer
    mw = next(r for r in d["rows"] if d["customers"][r[2]]["name"] == "MWAVE")
    assert unpack(mw[4]) == [0, 0, 7]
    assert names["Air New Zealand"]["reports"] == []    # sell-in only: "All channels"


def test_mapped_retailer_is_used_as_is_and_regions_stay_apart():
    d = build([st(rk=1, cust="JB HIFI"), st(rk=2, cust="JB HIFI")],
              [si(rk=1, cust="JB HIFI", group="JB HIFI", q=11)])
    assert sorted((d["regions"][r[0]], unpack(r[4])[-1]) for r in d["rows"]) == [("AU", 11), ("NZ", 0)]
    assert next(c for c in d["customers"] if c["name"] == "JB HIFI")["reports"] == ["AU", "NZ"]


def test_weeks_outside_the_window_are_ignored():
    d = build([st(wk=date(2026, 9, 20), u=50), st(u=1)], [si(wk=date(2026, 10, 18), q=9)])
    r, = d["rows"]
    assert unpack(r[3]) == [0, 0, 1] and r[4] == 0      # the part-week of sell-in isn't a week here


def test_macgear_stock_and_eta_attach_to_items_in_the_data():
    d = build([st()], [], mg=[(2, "A1", 100, 60, 200, 15, 20261020), (2, "ZZ", 1, 1, 1, 1, 20261020)],
              eta=[(2, "A1", "PO9", date(2026, 12, 1), "In transit (sea)", 150, datetime(2026, 10, 20, tzinfo=timezone.utc)),
                   (2, "A1", "PO8", None, "Production", 50, None),
                   (2, "A1", "PO7", date(2026, 11, 2), "Production", 20, None)],
              eta_loaded=True)
    i = [it[0] for it in d["items"]].index("A1")
    assert d["mg"]["by_region"]["NZ"] == {i: [100, 60, 200, 15]}      # ZZ isn't on the page: dropped
    assert [l[0] for l in d["eta"]["by_region"]["NZ"][i]] == ["PO7", "PO9", "PO8"]   # dated first, undated last
    assert d["mg"]["as_at"] == "2026-10-20" and d["eta"]["loaded"]


def test_without_sell_in_nothing_of_it_is_in_the_dataset():
    d = build([st()])
    assert d["sell_in"] is False and d["mg"] is None and d["eta"] is None
    assert d["rows"][0][4] == 0


def test_unmatched_lines_get_a_brand_label():
    d = build([st(brand=None)])
    assert d["items"][0][2] == st_weekly.UNMATCHED


# ---- access ---------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    calls = []

    def fake(brands, regions, sell_in):
        calls.append((brands, tuple(regions), sell_in))
        return build([st()], [si()] if sell_in else None, regions=regions)
    monkeypatch.setattr(st_weekly, "for_user", fake)
    monkeypatch.setattr(main.receipts, "status", lambda *a: None)
    db = SessionLocal(); db.query(User).delete(); db.commit(); db.close()
    c = TestClient(main.app)
    c.calls = calls
    return c


def mk(role, areas, email, brands=None):
    db = SessionLocal()
    u = User(email=email, name=email, role=role, password_hash="x", active=True,
             allowed_tools=json.dumps(areas), allowed_brands=json.dumps(brands) if brands is not None else None)
    db.add(u); db.commit(); uid = u.id; db.close()
    return uid


def as_(c, uid):
    c.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


def test_staff_with_the_area_get_the_page_and_scoped_data(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["st_weekly"], "sales@example.com"))
    assert client.get("/reports/weekly").status_code == 200
    d = client.get("/reports/weekly/data.json").json()
    assert d["sell_in"] is False                        # no sell_in capability ticked
    assert client.calls[-1][2] is False


def test_sell_in_capability_adds_sell_in(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["st_weekly", "sell_in"], "ops@example.com"))
    assert client.get("/reports/weekly/data.json").json()["sell_in"] is True


def test_staff_without_the_area_are_refused(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["items"], "other@example.com"))
    assert client.get("/reports/weekly").status_code == 403
    assert client.get("/reports/weekly/data.json").status_code == 403


def test_vendors_never_reach_it(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("vendor", ["st_weekly", "items"], "anker@example.com", ["ANKER"]))
    assert client.get("/reports/weekly").status_code == 403
    assert client.get("/reports/weekly/data.json").status_code == 403


# ---- store level ----------------------------------------------------------------
def test_store_rows_pack_weekly_units_and_this_weeks_soh():
    k = lambda d: int(d.strftime("%Y%m%d"))
    d = st_weekly.shape_stores(W, [("HN", "Moore Park", "A1", k(W[0]), 2, 9),
                                   ("HN", "Moore Park", "A1", k(W[-1]), 3, 4),
                                   ("HN", "Auburn", "A1", k(W[-1]), 0, 0),          # nothing at all: dropped
                                   ("HN", "Auburn", "B2", k(date(2026, 9, 20)), 7, 1)])  # outside the window
    assert d["stores"] == [["HN", "Moore Park"], ["HN", "Auburn"]]
    r, = d["rows"]
    assert r[:2] == [0, "A1"] and unpack(r[2]) == [2, 0, 3] and r[3] == 4


def test_stores_only_for_a_region_the_user_may_see(client, monkeypatch):
    monkeypatch.setattr(st_weekly, "stores_for", lambda *a: {"weeks": [], "stores": [], "rows": []})
    mk("admin", [], "boss@example.com")
    uid = mk("user", ["st_weekly"], "nz@example.com")
    db = SessionLocal(); u = db.get(User, uid); u.allowed_regions = json.dumps(["NZ"]); db.commit(); db.close()
    as_(client, uid)
    assert client.get("/reports/weekly/stores.json?region=NZ&retailer=PB+Tech").status_code == 200
    assert client.get("/reports/weekly/stores.json?region=AU&retailer=Harvey+Norman").status_code == 403
