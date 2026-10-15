"""End-to-end access through the real middleware: who reaches which URL, and
which data scope a report is built with.

The report builder is replaced with a recorder, so these tests never touch the
warehouse; what they check is the (brands, regions) the app asks it for.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import main, perms
from app.db import SessionLocal
from app.models import User
from app.security import sign


@pytest.fixture()
def client(monkeypatch):
    calls = []

    def fake_for_user(brands, regions):
        calls.append((brands, regions))
        return {"weeks": [], "built": "", "perBrand": 10, "regions": {}, "notes": []}

    monkeypatch.setattr(main.sell_through, "for_user", fake_for_user)
    db = SessionLocal()
    db.query(User).delete()
    db.commit()
    db.close()
    c = TestClient(main.app)
    c.calls = calls
    return c


def make_user(role, areas=None, brands=None, regions=None, email=None):
    db = SessionLocal()
    u = User(email=email or f"{role}-{len(db.query(User).all())}@example.com", name=role,
             password_hash="x", role=role,
             allowed_tools=json.dumps(areas) if areas is not None else None,
             allowed_brands=json.dumps(brands) if brands is not None else None,
             allowed_regions=json.dumps(regions) if regions is not None else None,
             active=True)
    db.add(u)
    db.commit()
    uid = u.id
    db.close()
    return uid


def login_as(client, uid):
    client.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


INTERNAL_PATHS = ["/promos", "/promo/new", "/help", "/vendor-bills", "/tools",
                  "/admin/users", "/api/product/X1", "/attachment/1"]


def test_vendor_is_refused_everything_but_home_and_their_reports(client):
    make_user("admin", email="boss@example.com")      # keeps auth switched on
    v = make_user("vendor", areas=["sell_through", "promos"], brands=["ANKER", "EUFY"])
    login_as(client, v)
    for path in INTERNAL_PATHS:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 403, path
    assert client.get("/").status_code == 200
    assert client.get("/reports/sell-through").status_code == 200


def test_vendor_report_is_built_with_only_their_brands(client):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["sell_through"], brands=["ANKER", "EUFY"], regions=["AU"])
    login_as(client, v)
    client.get("/reports/sell-through")
    assert client.calls == [(["ANKER", "EUFY"], ["AU"])]


def test_vendor_without_brands_gets_an_empty_scope_not_everything(client):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["sell_through"])
    login_as(client, v)
    client.get("/reports/sell-through")
    assert client.calls == [([], ["AU", "NZ"])]


def test_vendor_without_the_report_ticked_is_refused(client):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", brands=["ANKER"])
    login_as(client, v)
    assert client.get("/reports/sell-through").status_code == 403
    assert client.calls == []


def test_vendor_home_shows_no_tools(client):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["sell_through", "promos"], brands=["ANKER"])
    login_as(client, v)
    html = client.get("/").text
    assert "/reports/sell-through" in html
    assert "/promos" not in html and "/vendor-bills" not in html and "/admin/users" not in html


def test_staff_and_admin_scopes(client):
    a = make_user("admin", email="boss@example.com")
    s = make_user("user", areas=["sell_through", "promos"], brands=["MOVA"])
    login_as(client, s)
    assert client.get("/promos").status_code == 200
    assert client.get("/vendor-bills").status_code == 403
    assert client.get("/admin/users").status_code == 403
    client.get("/reports/sell-through")
    login_as(client, a)
    client.get("/reports/sell-through")
    assert client.calls == [(["MOVA"], ["AU", "NZ"]), (None, ["AU", "NZ"])]


def test_signed_out_is_sent_to_login(client):
    make_user("admin", email="boss@example.com")
    r = client.get("/reports/sell-through", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_admin_form_saves_vendor_scope_and_drops_tools(client):
    a = make_user("admin", email="boss@example.com")
    login_as(client, a)
    r = client.post("/admin/users/new", data={
        "email": "rep@anker.example", "name": "Anker rep", "role": "vendor",
        "company": "Anker", "tools": ["sell_through", "promos"],
        "brands": ["ANKER", "EUFY"], "regions": ["AU"], "active": "on"})
    assert r.status_code == 200
    db = SessionLocal()
    u = db.query(User).filter_by(email="rep@anker.example").one()
    assert perms.effective_access(u) == ["sell_through"]
    assert json.loads(u.allowed_tools) == ["sell_through"]
    assert perms.effective_brands(u) == ["ANKER", "EUFY"]
    assert perms.effective_regions(u) == ["AU"]
    assert u.company == "Anker"
    db.close()


# ---- Item Explorer + Sell-Through Table ----------------------------------------
@pytest.fixture()
def spy(monkeypatch):
    """Record what the report builders are asked for, return tiny fixtures."""
    calls = {"item": [], "board": [], "search": [], "table": []}
    hdr = {"item_key": 1, "code": "X1", "name": "Thing", "brand": "ANKER", "category": None,
           "status": {"AU": "Active", "NZ": None}, "rrp": {"AU": 10.0, "NZ": None}, "upc": None,
           "pm": None, "inactive": False, "type": None}
    det = {"weeks": ["2026-10-11"], "series": {"st_units": [1], "st_hist": [0], "st_value": [10],
           "soh": [5], "on_order": [None]}, "retailers": [], "stores": [],
           "kpis": {"st_units": 1, "st_value": 10, "last_week": 1, "prev_week": 0, "soh": 5,
                    "on_order": None, "cover": 5.0, "accounts": 1, "stores": 0, "hist_units": 0}}

    def item_for(code, brands, region, weeks, caps):
        calls["item"].append((code, brands, region, set(caps)))
        return (dict(hdr), det) if (brands is None or "ANKER" in brands) else (None, None)

    monkeypatch.setattr(main.items, "item_for", item_for)
    monkeypatch.setattr(main.items, "leaderboard_for",
                        lambda b, r: calls["board"].append((b, r)) or {"weeks": [], "items": []})
    monkeypatch.setattr(main.items, "search", lambda q, b, limit=12: calls["search"].append((q, b)) or calls.setdefault("limit", []).append(limit) or [])
    monkeypatch.setattr(main.st_table, "options", lambda b, r: {
        "weeks": ["2026-10-11"], "retailers": ["JB HIFI"], "store_retailers": [], "brands": b or ["ANKER", "MOVA"]})

    def build_for(brands, region, grain, anchor, prior, brand, retailer):
        calls["table"].append((brands, region, brand))
        return {"grain": grain, "keys": ["code", "description", "brand"], "anchor": "2026-10-11",
                "weeks": ["2026-10-11"], "rows": [], "region": region, "currency": "AUD"}
    monkeypatch.setattr(main.st_table, "build_for", build_for)
    return calls


def test_vendor_item_page_never_gets_sell_in_or_margin(client, spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items", "margin"], brands=["ANKER"])
    login_as(client, v)
    assert client.get("/reports/items/i/X1").status_code == 200
    assert spy["item"][-1] == ("X1", ["ANKER"], "AU", set())


def test_vendor_with_sell_in_ticked_gets_sell_in_only(client, spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items", "sell_in", "sell_in_value", "margin"], brands=["ANKER"])
    login_as(client, v)
    client.get("/reports/items/i/X1")
    assert spy["item"][-1][3] == {"sell_in"}


def test_admin_item_page_gets_everything(client, spy):
    a = make_user("admin", email="boss@example.com")
    login_as(client, a)
    client.get("/reports/items/i/X1?region=NZ")
    assert spy["item"][-1] == ("X1", None, "NZ", {"sell_in", "sell_in_value", "margin"})


def test_item_outside_scope_is_404_not_403(client, spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items"], brands=["EUFY"])
    login_as(client, v)
    assert client.get("/reports/items/i/X1").status_code == 404


def test_region_outside_scope_falls_back(client, spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items"], brands=["ANKER"], regions=["NZ"])
    login_as(client, v)
    client.get("/reports/items/i/X1?region=AU")
    assert spy["item"][-1][2] == "NZ"
    client.get("/reports/items?region=AU")
    assert spy["board"][-1] == (["ANKER"], "NZ")


def test_vendor_search_and_table_are_scoped(client, spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items", "st_table"], brands=["ANKER"])
    login_as(client, v)
    client.get("/reports/items/search?q=mova")
    assert spy["search"][-1] == ("mova", ["ANKER"])
    client.get("/reports/table?brand=MOVA")            # a brand outside scope is ignored
    assert spy["table"][-1] == (["ANKER"], "AU", "ANKER")
    r = client.get("/reports/table/export.xlsx?brand=MOVA")
    assert r.status_code == 200 and spy["table"][-1] == (["ANKER"], "AU", "ANKER")
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_new_reports_need_their_tick(client, spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["sell_through"], brands=["ANKER"])
    login_as(client, v)
    for path in ["/reports/items", "/reports/items/i/X1", "/reports/items/search?q=a",
                 "/reports/table", "/reports/table/export.xlsx"]:
        assert client.get(path).status_code == 403, path


def test_only_admins_upload_images(client, spy):
    make_user("admin", email="boss@example.com")
    s = make_user("user", areas=["items"])
    login_as(client, s)
    r = client.post("/reports/items/img/X1", files={"image": ("x.png", b"\x89PNG", "image/png")})
    assert r.status_code == 403


def test_sell_in_quantity_only_never_carries_dollars(monkeypatch):
    """With sell_in but not sell_in_value the SQL is told not to select $, and
    nothing $-shaped reaches the page data — for any role."""
    from app import items
    seen = {}

    class Cur:
        description = None
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def execute(self, sql, p=None): seen.setdefault("money", p.get("money") if p else None)
        def fetchall(self): return []

    class Conn(Cur):
        def cursor(self): return Cur()

    monkeypatch.setattr(items.warehouse, "connect", lambda: Conn())
    monkeypatch.setattr(items.warehouse, "recent_weeks", lambda n, upto=None: [__import__("datetime").date(2026, 10, 11)])
    d = items.item_detail(1, "AU", 4, {"sell_in"})
    assert seen["money"] is False
    flat = json.dumps(d)
    assert "si_value" not in flat and "gp" not in d["kpis"] and '"v"' not in json.dumps(d.get("sell_in_wtd") or {})
    assert "si_units" in d["series"] and "inventory" in d


# ---- home page: Macgear sales $ only with sell_in_value ---------------------------
@pytest.fixture()
def home_spy(monkeypatch):
    calls = []
    fake = {"today": "2026-10-15", "fy": "FY27", "fq": "Q3", "month": "October", "fy_start": "2026-04-01",
            "fq_start": "2026-10-01", "has_targets": False, "weekdays": {"done": 11, "all": 22}, "regions": {}}
    monkeypatch.setattr(main.sales, "summary", lambda today, margin: calls.append(margin) or dict(fake))
    monkeypatch.setattr(main.items, "leaderboard_for", lambda b, r: {"weeks": [], "items": []})
    monkeypatch.setattr(main.warehouse, "query", lambda *a, **k: [[None]])
    monkeypatch.setattr(main.receipts, "status", lambda b, r, t: None)
    return calls


def test_vendor_home_never_builds_sales_even_if_ticked(client, home_spy):
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items", "sell_in", "sell_in_value", "margin"], brands=["ANKER"])
    login_as(client, v)
    html = client.get("/").text
    assert home_spy == [] and "October to date" not in html


def test_staff_home_sales_needs_the_dollar_tick(client, home_spy):
    make_user("admin", email="boss@example.com")
    s1 = make_user("user", areas=["items", "sell_in"])
    login_as(client, s1)
    client.get("/")
    assert home_spy == []
    s2 = make_user("user", areas=["items", "sell_in", "sell_in_value"])
    login_as(client, s2)
    client.get("/")
    assert home_spy == [False]                     # sales, but no margin


def test_admin_home_gets_sales_with_margin(client, home_spy):
    a = make_user("admin", email="boss@example.com")
    login_as(client, a)
    assert client.get("/").status_code == 200
    assert home_spy == [True]


# ---- receipt strip: scoped like everything else -----------------------------------
def test_receipt_strip_is_scoped_and_hidden_without_a_sell_through_report(client, monkeypatch):
    seen = []
    monkeypatch.setattr(main.receipts, "status", lambda b, r, t: seen.append((b, r)) or {
        "week": "2026-10-11", "feeds": [{"retailer": "JB HIFI", "region": "AU", "status": "in",
                                         "missing_parts": [], "loaded": "Wed", "last_week": "2026-10-11"}],
        "total": 1, "received": 1, "partial": 0, "waiting": 0})
    monkeypatch.setattr(main.items, "leaderboard_for", lambda b, r: {"weeks": [], "items": []})
    make_user("admin", email="boss@example.com")
    v = make_user("vendor", areas=["items"], brands=["ANKER"], regions=["NZ"])
    login_as(client, v)
    assert "1 / 1 in" in client.get("/").text
    assert seen[-1] == (["ANKER"], ["NZ"])
    s = make_user("user", areas=["promos"])           # no sell-through report
    login_as(client, s)
    seen.clear()
    assert "rcvStrip" not in client.get("/").text and seen == []


def test_search_limit_is_capped(client, spy):
    a = make_user("admin", email="boss@example.com")
    login_as(client, a)
    client.get("/reports/items/search?q=eufy&limit=30")
    client.get("/reports/items/search?q=eufy&limit=5000")
    assert spy["limit"] == [30, 50]


# ---- deployment guard -------------------------------------------------------------
def test_deployment_refuses_to_start_without_a_secret_or_any_way_to_sign_in():
    p = main.deployment_problems
    assert p("", "", "", False) == []                                  # local dev: fine
    assert p("https://reporting.example", "s3cret", "", True) == []    # proper deployment
    assert p("https://reporting.example", "s3cret", "shared", False) == []
    assert len(p("https://reporting.example", "", "shared", True)) == 1      # no APP_SECRET
    assert len(p("https://reporting.example", "s3cret", "", False)) == 1     # auth would be off
    assert len(p("https://reporting.example", "", "", False)) == 2
