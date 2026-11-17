"""Brand Profitability: the CYA formulas (calc.py), the GL upload parser, and the settings
writes (overlap refused, "new rate from", change log, uploads superseding months).

The calc / parser tests are pure. The settings tests need the LOCAL Postgres
(SELLTHROUGH_LOCAL_DB_URL in Sell Through/.env) and use a throwaway schema, dropped afterwards;
they're skipped when that database isn't there."""
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.brand_profit import calc, db, settings, upload

A, M, J, O = date(2026, 4, 1), date(2026, 5, 1), date(2026, 7, 1), date(2026, 10, 1)
JB, HN, AIRPORT = 2374, 2372, 3634


def rule(kind, pct, vf=A, vt=None, cid=None, brand="", cat=""):
    return {"kind": kind, "customer_id": cid, "brand": brand, "item_category": cat,
            "pct": pct, "valid_from": vf, "valid_to": vt}


RATES = calc.Rates([
    rule("base", 0.19, cid=JB),
    rule("base", 0.17, cid=HN),
    rule("brand", -0.10, cid=JB, brand="MOVA"),
    rule("brand", 0.05, vf=date(2026, 9, 1), cid=HN, brand="EUFY", cat="Appliances"),
    rule("store", 0.05, cid=AIRPORT),
    rule("mdf", 0.05, brand="ADONIT"),
    rule("mdf", 0.0, cid=JB, brand="EUFY"),
    rule("mdf", 0.02, brand="EUFY"),
    rule("base", 0.08, vt=date(2026, 8, 1), cid=2469),
    rule("base", 0.03, vf=date(2026, 9, 1), cid=2469),
    rule("gp_target", 0.13, brand="MOVA"),
], [{"brand": "MOVA", "pm": "Jeffrey Zhu", "valid_from": A, "valid_to": None}])


# ---- the CYA formulas ------------------------------------------------------------------
def test_rebate_parts_add_up_like_the_workbook():
    assert RATES.rebate_pct(O, JB, 1, "Mova", "") == pytest.approx(0.09)         # 19% - 10%, case-insensitive
    assert RATES.rebate_pct(O, JB, AIRPORT, "SATECHI", "") == pytest.approx(0.24)  # + airport store extra
    assert RATES.rebate_pct(O, HN, 1, "EUFY", "Appliances") == pytest.approx(0.22)
    assert RATES.rebate_pct(O, HN, 1, "EUFY", "Accessories") == pytest.approx(0.17)  # category rule only for its category
    assert RATES.rebate_pct(J, HN, 1, "EUFY", "Appliances") == pytest.approx(0.17)   # not in force before Sep
    assert RATES.rebate_pct(O, 99999, 1, "MOVA", "") == 0                             # unknown customer: nothing


def test_rates_change_by_month():
    assert RATES.rebate_pct(date(2026, 8, 1), 2469, 1, "X", "") == pytest.approx(0.08)
    assert RATES.rebate_pct(date(2026, 9, 1), 2469, 1, "X", "") == pytest.approx(0.03)


def test_mdf_customer_row_overrides_the_brand_default():
    assert RATES.mdf_pct(O, JB, "EUFY") == 0
    assert RATES.mdf_pct(O, HN, "EUFY") == pytest.approx(0.02)
    assert RATES.mdf_pct(O, HN, "adonit") == pytest.approx(0.05)
    assert RATES.mdf_pct(O, HN, "MOVA") == 0


def test_line_formulas():
    # JB, Adonit, gross 100, COGS 50: rebate 19, net 81, GP 31, MDF 5 (on gross), NP 26
    t = calc.line_totals(RATES, O, JB, 1, "ADONIT", "", qty=2, gross=100.0, cogs=50.0)
    assert (t.rebate, t.net, t.gp, t.mdf, t.np) == pytest.approx((19, 81, 31, 5, 26))
    d = t.as_dict()
    assert d["np_gross_pct"] == pytest.approx(0.26) and d["np_net_pct"] == pytest.approx(26 / 81)
    # a credit memo gives the rebate back
    c = calc.line_totals(RATES, O, JB, 1, "ADONIT", "", qty=-1, gross=-50.0, cogs=-25.0)
    assert c.rebate == pytest.approx(-9.5)


def test_tree_puts_gl_on_the_brand_and_targets_on_np_gross():
    sales = [{"month": O, "head_office": JB, "store": 1, "brand": "MOVA", "category": "", "qty": 1,
              "gross": 1000.0, "cogs": 700.0},
             {"month": J, "head_office": JB, "store": 1, "brand": "MOVA", "category": "", "qty": 1,
              "gross": 999.0, "cogs": 0.0}]                                      # other month: left out
    gl = [{"month": O, "brand": "Mova", "kind": "vr", "amount": -50.0},
          {"month": O, "brand": "MOVA", "kind": "freight", "amount": 10.0}]
    root = calc.build_tree(RATES, sales, gl, {O}, {JB: "JB HI-FI - AU"})
    pm = root.children["Jeffrey Zhu"]
    mova = pm.children["MOVA"]
    assert set(mova.children) == {"JB HI-FI - AU", "Freight & vendor rebates (GL)"}
    d = mova.as_dict()
    # rebate 9% = 90, net 910, GP = 910 - 700 - 10 - (-50) = 250, NP 250
    assert (d["gross"], d["rebate"], d["gp"], d["np"], d["freight"], d["vr"]) == pytest.approx((1000, 90, 250, 250, 10, -50))
    assert d["gp_target"] == pytest.approx(0.13) and d["vs_target"] == pytest.approx(0.25 - 0.13)
    assert root.as_dict()["np"] == pytest.approx(250)


def test_unassigned_brand_goes_to_no_pm():
    root = calc.build_tree(RATES, [{"month": O, "head_office": JB, "store": 1, "brand": "", "category": "",
                                    "qty": 1, "gross": 15.0, "cogs": 0.0}], [], {O}, {})
    assert list(root.children) == [calc.NO_PM]


# ---- the GL upload parser --------------------------------------------------------------
ACCTS = {"51080 VENDOR REBATES": "vr", "68020 FREIGHT INCOMING": "freight"}


def test_parse_saved_search_csv():
    text = ("﻿Internal ID,Type,Date,Period,Document Number,Account,Class,Name,Memo,Amount\n"
            "1,Journal,31/10/2026,FY27 : Q3 : Oct 2026,JE006531,51080 Vendor Rebates,Brands : Mova,,Std accrual,\"1,234.50\"\n"
            "2,Bill Credit,29/10/2026,,CN1,51080 Vendor Rebates,EUFY,Anker,Rebate,(500.00)\n"
            "3,Bill,03/09/2026,Sep 2026,142230,68020 Freight Incoming,Mova,Think Global,Battery,$873.02\n")
    rows = upload.parse(text, ACCTS)
    assert [(r["month"], r["kind"], r["brand"], r["amount"]) for r in rows] == [
        (O, "vr", "MOVA", 1234.50), (O, "vr", "EUFY", -500.0), (date(2026, 9, 1), "freight", "MOVA", 873.02)]


def test_parse_refuses_unmapped_accounts_and_missing_columns():
    with pytest.raises(upload.Invalid, match="not mapped.*99999 Mystery"):
        upload.parse("Date,Account,Amount\n01/10/2026,99999 Mystery,5\n", ACCTS)
    with pytest.raises(upload.Invalid, match="Missing column"):
        upload.parse("Date,Memo\n01/10/2026,x\n", ACCTS)


# ---- settings writes (local Postgres) ------------------------------------------------------
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
needs_db = pytest.mark.skipif(URL is None, reason="local Postgres not available")


@pytest.fixture()
def conn(monkeypatch):
    s = "bp_test_" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("BRAND_PROFIT_DB_URL", URL)
    monkeypatch.setenv("BRAND_PROFIT_SCHEMA", s)
    c = db.connect()
    yield c
    c.close()
    with psycopg.connect(URL, autocommit=True) as x:
        x.execute(f"DROP SCHEMA IF EXISTS {s} CASCADE")


def _form(**kw):
    base = {"region": "AU", "kind": "base", "customer_id": JB, "customer_name": "JB HI-FI - AU",
            "pct": "19", "valid_from": "2026-04", "valid_to": "", "note": ""}
    return {**base, **kw}


@needs_db
def test_add_overlap_change_from_and_log(conn):
    rid = settings.add_rule(conn, _form(), "a@x")
    with pytest.raises(settings.Invalid, match="overlaps"):
        settings.add_rule(conn, _form(pct="20", valid_from="2026-09"), "a@x")
    new_id = settings.change_from(conn, rid, "2026-09", "17.5", "b@x", "new deal")
    rows = conn.execute("SELECT id, pct, valid_from, valid_to FROM rate_rule ORDER BY id").fetchall()
    assert [(float(r["pct"]), r["valid_from"], r["valid_to"]) for r in rows] == [
        (0.19, A, date(2026, 8, 1)), (0.175, date(2026, 9, 1), None)]
    rates = calc.Rates(db.load_rules(conn, "AU"))
    assert rates.rebate_pct(date(2026, 8, 1), JB, 1, "X", "") == pytest.approx(0.19)
    assert rates.rebate_pct(O, JB, 1, "X", "") == pytest.approx(0.175)
    settings.delete_rule(conn, new_id, "c@x")
    log = conn.execute("SELECT by_user, action FROM change_log ORDER BY id").fetchall()
    assert [(l["by_user"], l["action"]) for l in log] == [("a@x", "add"), ("b@x", "edit"), ("b@x", "add"), ("c@x", "delete")]


@needs_db
def test_rule_validation(conn):
    with pytest.raises(settings.Invalid, match="customer"):
        settings.add_rule(conn, _form(customer_id=None), "a@x")
    with pytest.raises(settings.Invalid, match="brand"):
        settings.add_rule(conn, _form(kind="mdf", customer_id=None), "a@x")
    with pytest.raises(settings.Invalid, match="before"):
        settings.add_rule(conn, _form(valid_from="2026-09", valid_to="2026-04"), "a@x")
    # an MDF default and a customer MDF row for the same brand are different keys
    settings.add_rule(conn, _form(kind="mdf", customer_id=None, brand="EUFY", pct="2"), "a@x")
    settings.add_rule(conn, _form(kind="mdf", brand="EUFY", pct="0"), "a@x")


@needs_db
def test_upload_replaces_its_months_and_removing_it_restores(conn):
    settings.save_account(conn, "AU", "51080 Vendor Rebates", "vr", "a@x")
    first = [{"month": M, "tran_date": None, "line_key": "", "document_number": "A", "tran_type": "", "account": "51080 Vendor Rebates",
              "brand": "MOVA", "vendor": "", "memo": "", "kind": "vr", "amount": 100.0},
             {"month": O, "tran_date": None, "line_key": "", "document_number": "B", "tran_type": "", "account": "51080 Vendor Rebates",
              "brand": "MOVA", "vendor": "", "memo": "", "kind": "vr", "amount": 200.0}]
    upload.import_lines(conn, "AU", first, "may-oct.csv", "", "a@x")
    second = [{**first[1], "document_number": "C", "amount": 50.0}]
    res = upload.import_lines(conn, "AU", second, "oct.csv", "", "a@x")
    assert res["replaced"] == 1
    active = conn.execute("SELECT document_number FROM gl_line WHERE superseded_by IS NULL ORDER BY month").fetchall()
    assert [r["document_number"] for r in active] == ["A", "C"]
    settings.delete_batch(conn, res["batch"], "a@x")
    active = conn.execute("SELECT document_number FROM gl_line WHERE superseded_by IS NULL ORDER BY month").fetchall()
    assert [r["document_number"] for r in active] == ["A", "B"]


@needs_db
def test_settings_pages_and_posts(conn, monkeypatch):
    from app import main
    from app.brand_profit import data
    from app.db import SessionLocal
    from app.models import User
    s = SessionLocal(); s.query(User).delete(); s.commit(); s.close()   # no users = auth off
    monkeypatch.setattr(data, "pickers", lambda region: {"heads": ["JB HI-FI - AU · 2374"], "stores": [],
                                                          "brands": ["MOVA"], "categories": []})
    client = TestClient(main.app)
    r = client.post("/brand-profit/rule", data={"region": "AU", "kind": "base", "customer": "JB HI-FI - AU · 2374",
                                                 "pct": "19", "valid_from": "2026-04", "valid_to": "", "note": ""},
                    follow_redirects=False)
    assert r.status_code == 303 and "ok=" in r.headers["location"]
    r = client.post("/brand-profit/rule", data={"region": "AU", "kind": "base", "customer": "JB HI-FI - AU · 2374",
                                                 "pct": "20", "valid_from": "2026-05", "valid_to": ""},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]
    page = client.get("/brand-profit/settings?region=AU&tab=base")
    assert page.status_code == 200 and "JB HI-FI - AU" in page.text and "19%" in page.text


# ---- access ------------------------------------------------------------------------------
def test_access_needs_the_area_and_for_the_report_margin_too(monkeypatch):
    from app import main
    from app.brand_profit import data
    from tests.test_access import login_as, make_user
    from app.db import SessionLocal
    from app.models import User
    s = SessionLocal(); s.query(User).delete(); s.commit(); s.close()
    seen = []
    monkeypatch.setattr(data, "sales_lines", lambda region, fy: {"months_with_sales": [], "sales": [], "intercompany": {}, "names": {}})
    monkeypatch.setattr(data, "report", lambda region, fy, period, brands=None, pm="": seen.append(brands) or {"pm": "", "pms": [],
        "region": region, "fy": fy, "period": period, "label": "x", "months": [], "total": calc.Totals().as_dict(),
        "rows": [], "intercompany": 0, "gl_source": {}, "gl_missing": [], "fy_months": [], "months_with_sales": [],
        "has_rules": True})
    monkeypatch.setattr(data, "settings_view", lambda region: {"rules": {k: [] for k in db.KINDS}, "pms": [],
                                                                "accounts": [], "batches": [], "log": []})
    monkeypatch.setattr(data, "pickers", lambda region: {"heads": [], "stores": [], "brands": [], "categories": []})
    make_user("admin", email="boss@example.com")
    c = TestClient(main.app)
    nothing = make_user("user", areas=[])
    login_as(c, nothing)
    assert c.get("/reports/brand-profit").status_code == 403
    assert c.get("/brand-profit/settings").status_code == 403
    assert c.post("/brand-profit/rule", data={}, follow_redirects=False).status_code == 403
    no_margin = make_user("user", areas=["brand_profit", "sell_in", "sell_in_value"])
    login_as(c, no_margin)
    assert c.get("/reports/brand-profit").status_code == 403            # area but no margin
    assert c.get("/brand-profit/settings").status_code == 403           # viewing isn't editing
    viewer = make_user("user", areas=["brand_profit", "sell_in", "sell_in_value", "margin"], brands=["MOVA"])
    login_as(c, viewer)
    assert c.get("/reports/brand-profit").status_code == 200
    assert seen[-1] == ["MOVA"]                                         # brand scope reaches the build
    editor = make_user("user", areas=["brand_profit_settings"])
    login_as(c, editor)
    assert c.get("/brand-profit/settings").status_code == 200
    vendor = make_user("vendor", areas=["brand_profit", "brand_profit_settings", "sell_in", "margin"], brands=["MOVA"])
    login_as(c, vendor)
    assert c.get("/reports/brand-profit").status_code == 403
    assert c.get("/brand-profit/settings").status_code == 403


def test_pm_target_is_sales_weighted_not_the_max():
    rates = calc.Rates([rule("gp_target", 0.32, brand="PAPERLIKE"), rule("gp_target", 0.14, brand="EUFY")],
                       [{"brand": "PAPERLIKE", "pm": "DV", "valid_from": A, "valid_to": None},
                        {"brand": "EUFY", "pm": "DV", "valid_from": A, "valid_to": None},
                        {"brand": "NOTARGET", "pm": "DV", "valid_from": A, "valid_to": None}])
    row = lambda b, g, c: {"month": O, "head_office": 1, "store": 1, "brand": b, "category": "", "qty": 1, "gross": g, "cogs": c}
    sales = [row("PAPERLIKE", 100.0, 60.0), row("EUFY", 900.0, 800.0), row("NOTARGET", 500.0, 0.0)]
    gl = [{"month": O, "brand": "EUFY", "kind": "vr", "amount": 10.0}]
    d = calc.build_tree(rates, sales, gl, {O}, {}).children["DV"].as_dict()
    # (0.32 x 100 + 0.14 x 900) / 1000 = 15.8%, not 32%; NOTARGET left out
    assert d["gp_target"] == pytest.approx(0.158)
    assert d["target_np"] == pytest.approx(158)
    # NP made on the targeted brands: 40 + (100 - 10) = 130 -> 13.0% vs 15.8%, $ gap -28
    assert d["vs_target"] == pytest.approx(0.13 - 0.158)
    assert d["vs_target_amt"] == pytest.approx(-28)
    assert d["target_coverage"] == pytest.approx(1000 / 1500)


def test_sku_exception_adds_for_that_item_only():
    rates = calc.Rates([rule("base", 0.16, cid=24), {"kind": "sku", "customer_id": 24, "item_code": "HWA10-model-9",
                                                      "pct": -0.10, "valid_from": A, "valid_to": None}])
    assert rates.rebate_pct(O, 24, 1, "WITHINGS", "", "hwa10-MODEL-9") == pytest.approx(0.06)
    assert rates.rebate_pct(O, 24, 1, "WITHINGS", "", "HWA10-model-1") == pytest.approx(0.16)
    t = calc.line_totals(rates, O, 24, 1, "WITHINGS", "", 1, 100.0, 50.0, item="HWA10-model-9")
    assert t.rebate == pytest.approx(6)


@needs_db
def test_sku_rule_needs_an_item_and_keys_on_it(conn):
    with pytest.raises(settings.Invalid, match="item code"):
        settings.add_rule(conn, _form(kind="sku"), "a@x")
    settings.add_rule(conn, _form(kind="sku", item_code="hwa10-model-9", pct="-10"), "a@x")
    settings.add_rule(conn, _form(kind="sku", item_code="HWA10-model-1", pct="-10"), "a@x")   # other item: no overlap
    with pytest.raises(settings.Invalid, match="overlaps"):
        settings.add_rule(conn, _form(kind="sku", item_code="HWA10-MODEL-9", pct="-5", valid_from="2026-09"), "a@x")


# ---- net profit for the rest of the site (np.py) -------------------------------------------
def _fake_np(monkeypatch, rows, gl, rules):
    from contextlib import contextmanager
    from app import warehouse
    from app.brand_profit import data, np as bp_np

    @contextmanager
    def fake_connect():
        yield None
    monkeypatch.setattr(bp_np.db, "connect", fake_connect)
    monkeypatch.setattr(bp_np.db, "load_rules", lambda conn, region: rules)
    monkeypatch.setattr(bp_np.db, "load_pms", lambda conn, region: [])
    monkeypatch.setattr(data, "gl_lines", lambda conn, region, fy: (gl, {}))
    monkeypatch.setattr(warehouse, "query", lambda sql, params=None: rows)
    warehouse.drop(("bp_np",))
    return bp_np


def test_np_deductions_and_gross_share_of_freight(monkeypatch):
    # (date_key, customer, brand_id, item_key, head office, brand, category, item code, revenue)
    rows = [(20261003, 11, 7, 100, JB, "MOVA", "", "V70", 1000.0),
            (20261020, 12, 7, 101, HN, "MOVA", "", "S70", 3000.0),
            (20261020, 12, 9, 102, HN, "NOSUCH", "", "X", 500.0)]
    gl = [{"month": O, "brand": "MOVA", "kind": "vr", "amount": -400.0},     # shared 1:3 by revenue
          {"month": O, "brand": "", "kind": "freight", "amount": 25.0}]      # no brand: unallocated
    bp_np = _fake_np(monkeypatch, rows, gl, [rule("base", 0.19, cid=JB), rule("brand", -0.10, cid=JB, brand="MOVA"),
                                             rule("base", 0.17, cid=HN), rule("mdf", 0.02, brand="MOVA")])
    d = bp_np.deductions("AU", 20261001, 20261031, lambda k, c, b, i: i)
    # V70 @ JB: rebate 9% = 90, MDF 20, VR share -100 -> 10 ; rebates part 90
    assert d[100] == pytest.approx([10.0, 90.0])
    # S70 @ HN: rebate 17% = 510, MDF 60, VR share -300 -> 270
    assert d[101] == pytest.approx([270.0, 510.0])
    assert d[102] == pytest.approx([85.0, 85.0])                             # no brand rules, no GL
    assert bp_np.unallocated("AU", 20261001, 20261031) == {202610: 25.0}
    assert bp_np.gl_missing("AU", date(2026, 10, 15)) is False
    assert bp_np.gl_missing("AU", date(2026, 11, 15)) is True
    assert bp_np.deductions("AU", 20250401, 20260331, lambda *a: 1) == {}    # before the settings start


def test_cube_rows_get_deductions_in_cost_and_a_rebates_column(monkeypatch):
    rows = [(20261003, 11, 7, 100, JB, "MOVA", "", "V70", 1000.0)]
    gl = [{"month": O, "brand": "", "kind": "freight", "amount": 25.0}]
    bp_np = _fake_np(monkeypatch, rows, gl, [rule("base", 0.19, cid=JB)])
    out = bp_np.adjust_cube_rows("AU", [(202610, 11, 7, None, 1000.0, 600.0, 2.0)], monthly=True)
    assert out[0] == (202610, 11, 7, None, 1000.0, pytest.approx(790.0), 2.0, pytest.approx(190.0))
    assert out[1] == (202610, None, None, None, 0.0, 25.0, 0.0, 0.0)            # unallocated freight row
    # revenue - cost over the month = 1000 - 790 - 25 = 185 net profit
    assert sum(r[4] - r[5] for r in out) == pytest.approx(185.0)
