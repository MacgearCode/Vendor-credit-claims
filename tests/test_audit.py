"""Sign-in and usage logging (app/audit.py) and the admin-only activity pages."""
import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import audit, main
from app.db import SessionLocal
from app.models import AuthEvent, UsageDay, User
from app.security import hash_password, sign

PW = secrets.token_urlsafe(12)            # throwaway, generated per run


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(main.notify, "send_account_link", lambda **kw: True)
    db = SessionLocal()
    for t in (User, AuthEvent, UsageDay):
        db.query(t).delete()
    db.commit()
    db.close()
    audit._seen.clear()
    return TestClient(main.app)


def add(email, role="user", tools=None):
    db = SessionLocal()
    u = User(email=email, name=email.split("@")[0], role=role, active=True,
             password_hash=hash_password(PW), allowed_tools=tools)
    db.add(u)
    db.commit()
    uid = u.id
    db.close()
    return uid


def events(kind=None):
    db = SessionLocal()
    q = db.query(AuthEvent)
    if kind:
        q = q.filter(AuthEvent.event == kind)
    out = [(e.event, e.email, e.detail, e.actor_email, e.ip) for e in q.order_by(AuthEvent.id)]
    db.close()
    return out


def user(uid):
    db = SessionLocal()
    u = db.get(User, uid)
    db.expunge(u)
    db.close()
    return u


def login_as(client, uid):
    client.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


def test_sign_in_records_time_ip_and_count(client):
    uid = add("sam@example.com")
    for _ in range(2):
        r = client.post("/login", data={"username": "Sam@Example.com", "password": PW},
                        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}, follow_redirects=False)
        assert r.status_code == 303
    u = user(uid)
    assert u.login_count == 2 and u.last_login is not None and u.last_seen_at is not None
    assert u.last_login_ip == "203.0.113.9"          # Caddy's entry = the last one
    assert [e[0] for e in events()] == ["sign_in", "sign_in"]


def test_failed_sign_ins_are_logged_with_the_reason_and_counted_on_the_list(client):
    admin = add("boss@example.com", "admin")
    add("sam@example.com")
    client.post("/login", data={"username": "sam@example.com", "password": "wrong"})
    client.post("/login", data={"username": "nobody@example.com", "password": "x"})
    fails = events("sign_in_failed")
    assert [(e[1], e[2]) for e in fails] == [("sam@example.com", "wrong password"),
                                             ("nobody@example.com", "no such account")]
    # the visitor still gets the one generic message
    r = client.post("/login", data={"username": "sam@example.com", "password": "wrong"})
    assert r.status_code == 401 and "Incorrect email or password." in r.text
    login_as(client, admin)
    page = client.get("/admin/users").text
    assert "2 failed" in page                         # since sam's last successful sign-in


def test_page_opens_are_counted_per_area_but_not_json_or_denied_pages(client):
    uid = add("sam@example.com", tools='["ageing"]')
    login_as(client, uid)
    client.get("/")                                   # home
    client.get("/updates")                            # other
    client.get("/reports/sales")                      # not ticked -> 403, not counted
    db = SessionLocal()
    rows = {(r.area, r.views) for r in db.query(UsageDay).filter(UsageDay.user_id == uid)}
    db.close()
    assert ("home", 1) in rows and ("other", 1) in rows
    assert not any(a == "sales" for a, _ in rows)
    assert user(uid).last_seen_at is not None


def test_admin_changes_are_logged_with_what_changed(client):
    admin = add("boss@example.com", "admin")
    uid = add("sam@example.com", tools='["ageing"]')
    login_as(client, admin)
    client.post(f"/admin/users/{uid}", data={"email": "sam@example.com", "name": "Sam", "role": "user",
                                             "tools": ["ageing", "open_orders"], "regions": ["AU", "NZ"],
                                             "active": "on"})
    ev = events("account_changed")
    assert ev and ev[-1][3] == "boss@example.com"
    assert "Open Orders" in ev[-1][2] and "Areas:" in ev[-1][2]
    client.post(f"/admin/users/{uid}", data={"email": "sam@example.com", "name": "Sam", "role": "user",
                                             "tools": ["ageing", "open_orders"], "regions": ["AU", "NZ"]})
    assert events("account_disabled")
    client.post(f"/admin/users/{uid}/delete")
    gone = events("account_deleted")
    assert gone and gone[-1][1] == "sam@example.com"   # history outlives the account


def test_activity_pages_are_admin_only(client):
    staff = add("sam@example.com", tools='["ageing"]')
    vendor = add("v@vendor.com", "vendor", tools='["items"]')
    admin = add("boss@example.com", "admin")
    for uid in (staff, vendor):
        login_as(client, uid)
        assert client.get("/admin/activity").status_code == 403
    login_as(client, admin)
    r = client.get("/admin/activity?days=7&event=sign_in_failed")
    assert r.status_code == 200 and "Activity log" in r.text
    assert client.get(f"/admin/users/{staff}").status_code == 200


def test_snapshot_diff_and_ago():
    assert audit.changes({"Role": "user", "Name": "A"}, {"Role": "admin", "Name": "A"}) == "Role: user → admin"
    now = datetime(2026, 11, 19, 12, 0)
    assert audit.ago(now - timedelta(seconds=30), now) == "just now"
    assert audit.ago(now - timedelta(hours=3), now) == "3 h ago"
    assert audit.ago(now - timedelta(days=1, hours=2), now) == "yesterday"
    assert audit.ago(now - timedelta(days=40), now) == "5 weeks ago"
