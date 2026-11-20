"""GL Accounts: opening / movement / closing, parent roll-up, regions apart, access per group."""
import secrets
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import gl_accounts as gl, main, perms
from app.db import SessionLocal
from app.models import User
from app.security import hash_password, sign


def _ln(acc, month, amount, rg="AU", day=15, tran=1):
    m = date(*month, 1)
    return {"region": rg, "account": acc, "month": m, "date": date(m.year, m.month, day), "type": "Journal",
            "doc": f"JE{tran}", "tran_id": tran, "entity": "", "memo": "", "brand": "", "amount": amount}


DATA = {"lines": [
    _ln(962, (2026, 3), 100.0),                  # before the range -> opening
    _ln(962, (2026, 5), 50.0, tran=2),
    _ln(962, (2026, 6), -30.0, tran=3),
    _ln(962, (2026, 12), 999.0, tran=4),         # after the range -> not shown, not in closing
    _ln(962, (2026, 5), 7000.0, rg="NZ", tran=5),  # NZ never in the AU figures
    _ln(960, (2026, 4), 12.5, tran=6),
]}


def _acc(v, num):
    for a in v["accounts"]:
        for r in [a] + a["children"]:
            if r["number"] == num:
                return r


def test_opening_movement_closing_and_running_balance():
    v = gl.view("gl_mova", DATA, "AU", date(2026, 4, 1), date(2026, 11, 1))
    a = _acc(v, "22195")
    assert (a["open"], a["move"], a["close"]) == (100.0, 20.0, 120.0)
    assert [ln["balance"] for ln in a["lines"]] == [150.0, 120.0]
    assert _acc(v, "11546")["close"] == 12.5
    assert _acc(v, "22196")["lines"] == [] and _acc(v, "22196")["close"] == 0   # empty accounts still listed
    assert v["count"] == 3
    nz = gl.view("gl_mova", DATA, "NZ", date(2026, 4, 1), date(2026, 11, 1))
    assert _acc(nz, "22195")["close"] == 7000.0


def test_parent_is_the_total_of_its_sub_accounts():
    data = {"lines": [_ln(927, (2026, 5), 10.0), _ln(928, (2026, 6), 5.0, tran=2),
                      _ln(936, (2026, 1), 2.0, tran=3)]}
    v = gl.view("gl_anker", data, "AU", date(2026, 4, 1), date(2026, 11, 1))
    p = _acc(v, "22192")
    assert len(p["children"]) == 10
    assert (p["open"], p["move"], p["close"]) == (2.0, 15.0, 17.0)
    assert not any(a["number"].startswith("22192-") for a in v["accounts"])   # subs only under the parent


def test_months_and_links():
    assert gl.fy_start(date(2026, 3, 31)) == date(2025, 4, 1)
    assert gl.fy_start(date(2026, 4, 1)) == date(2026, 4, 1)
    assert gl.parse_month("2026-07", date(2000, 1, 1)) == date(2026, 7, 1)
    assert gl.parse_month("junk", date(2000, 1, 1)) == date(2000, 1, 1)
    opts = gl.month_options(date(2026, 11, 20), date(2026, 10, 1))
    assert opts[0] == date(2026, 11, 1) and opts[-1] == date(2026, 4, 1)   # never short of the FY start
    assert gl.netsuite_link("", 5) is None
    assert gl.netsuite_link("1234567_SB1", 5).startswith("https://1234567-sb1.app.netsuite.com/")


def test_xlsx_has_summary_and_transactions():
    from io import BytesIO
    from openpyxl import load_workbook
    v = gl.view("gl_mova", DATA, "AU", date(2026, 4, 1), date(2026, 11, 1))
    wb = load_workbook(BytesIO(gl.to_xlsx("gl_mova", "AU", date(2026, 4, 1), date(2026, 11, 1), v)))
    assert wb.sheetnames == ["Summary", "Transactions"]
    assert wb["Transactions"].max_row == 1 + 3


# ---- access: each group is its own area, staff only -------------------------------
PW = secrets.token_urlsafe(12)


@pytest.fixture()
def client(monkeypatch):
    db = SessionLocal()
    db.query(User).delete()
    db.commit()
    db.close()
    # never reach a warehouse: an empty group renders the page
    monkeypatch.setattr(gl, "for_user", lambda group, regions: {"lines": [], "loaded": None, "modified": None})
    return TestClient(main.app)


def _login(client, role, tools):
    db = SessionLocal()
    u = User(email=f"{secrets.token_hex(4)}@example.com", name="x", role=role, active=True,
             password_hash=hash_password(PW), allowed_tools=tools, allowed_brands='["EUFY"]')
    db.add(u)
    db.commit()
    client.cookies.set(main.COOKIE_NAME, sign(str(u.id), main.APP_SECRET))
    db.close()


def test_each_group_needs_its_own_tick(client):
    _login(client, "user", '["gl_mova"]')
    assert client.get("/reports/gl/mova").status_code == 200
    assert client.get("/reports/gl/anker").status_code == 403
    assert client.get("/reports/gl/anker/export.xlsx").status_code == 403


def test_vendors_never(client):
    assert "gl_mova" not in perms.VENDOR_AREAS and "gl_anker" not in perms.VENDOR_AREAS
    _login(client, "vendor", '["items", "gl_anker"]')
    assert client.get("/reports/gl/anker").status_code == 403


def test_paths_belong_to_their_areas():
    assert main._tool_for_path("/reports/gl/mova") == "gl_mova"
    assert main._tool_for_path("/reports/gl/anker/export.xlsx") == "gl_anker"
