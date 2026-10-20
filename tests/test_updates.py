"""What's new: the update log parser, and that each user only sees entries for areas they can open."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import main, updates
from app.db import SessionLocal
from app.models import User
from app.security import sign

LOG = """Anything before the first heading is notes.
    ## YYYY-MM-DD | example in the notes | ignored

## 2026-10-18 | Field thing | field
- reps

## 2026-10-21 | For everyone
Some words.
- one
- two

## not-a-date | broken
- dropped

## 2026-10-21 | Sales thing | sales, admin
- money
"""


def test_parse_reads_headings_points_and_areas_newest_first():
    e = updates.parse(LOG)
    assert [(x["date"], x["title"], x["areas"]) for x in e] == [
        (date(2026, 10, 21), "For everyone", []),
        (date(2026, 10, 21), "Sales thing", ["sales", "admin"]),
        (date(2026, 10, 18), "Field thing", ["field"])]
    assert e[0]["points"] == ["one", "two"] and e[0]["text"] == ["Some words."]


def test_visible_by_area():
    e = updates.parse(LOG)
    assert [x["title"] for x in updates.visible(e, set())] == ["For everyone"]
    assert [x["title"] for x in updates.visible(e, {"field"})] == ["For everyone", "Field thing"]
    assert len(updates.visible(e, {"admin"})) == 2


def test_the_shipped_log_parses():
    e = updates.load()
    assert e and all(x["title"] and x["points"] for x in e)


# ---- through the site -----------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(updates, "load", lambda: updates.parse(LOG))
    monkeypatch.setattr(main.receipts, "status", lambda *a: None)
    db = SessionLocal(); db.query(User).delete(); db.commit(); db.close()
    return TestClient(main.app)


def mk(role, areas, email, brands=None):
    db = SessionLocal()
    u = User(email=email, name=email, role=role, password_hash="x", active=True,
             allowed_tools=json.dumps(areas), allowed_brands=json.dumps(brands) if brands is not None else None)
    db.add(u); db.commit(); uid = u.id; db.close()
    return uid


def as_(c, uid):
    c.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


def test_vendor_opens_updates_but_sees_only_shared_entries(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("vendor", ["items"], "anker@example.com", ["ANKER"]))
    r = client.get("/updates")
    assert r.status_code == 200
    assert "For everyone" in r.text and "Field thing" not in r.text and "Sales thing" not in r.text


def test_sales_entries_need_the_sales_dollars(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["sales", "field"], "rep@example.com"))       # sales area but no sell_in_value
    r = client.get("/updates")
    assert "Field thing" in r.text and "Sales thing" not in r.text


def test_admin_sees_everything(client):
    as_(client, mk("admin", [], "boss@example.com"))
    r = client.get("/updates")
    assert all(t in r.text for t in ("For everyone", "Field thing", "Sales thing"))
