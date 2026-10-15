"""Invites, password resets and the account email — the same flow as 3PL Portal,
sending through the same n8n webhook (app/notify.py)."""
import json
import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main, notify
from app.db import SessionLocal
from app.models import User
from app.security import hash_password, sign

# throwaway test credentials, generated per run
PW = secrets.token_urlsafe(12)
PW2 = secrets.token_urlsafe(12)


@pytest.fixture()
def client(monkeypatch):
    sent = []
    monkeypatch.setattr(main.notify, "send_account_link",
                        lambda **kw: sent.append(kw) or True)
    main._forgot_hits.clear()
    db = SessionLocal()
    db.query(User).delete()
    db.commit()
    db.close()
    c = TestClient(main.app)
    c.sent = sent
    return c


def add(email, role="user", with_pw=False, active=True):
    db = SessionLocal()
    u = User(email=email, name=email.split("@")[0], role=role, active=active,
             password_hash=hash_password(PW) if with_pw else "")
    db.add(u)
    db.commit()
    uid = u.id
    db.close()
    return uid


def get(uid):
    db = SessionLocal()
    u = db.get(User, uid)
    db.expunge(u)
    db.close()
    return u


def login_as(client, uid):
    client.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


# ---- forgot password ---------------------------------------------------------------
def test_forgot_answers_the_same_whether_or_not_the_address_exists(client):
    add("boss@example.com", "admin", with_pw=True)
    known = client.post("/forgot", data={"email": "boss@example.com"})
    unknown = client.post("/forgot", data={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text
    assert [s["to"] for s in client.sent] == ["boss@example.com"]
    assert client.sent[0]["purpose"] == notify.RESET
    assert client.sent[0]["expires_min"] == main.RESET_TOKEN_TTL_MIN


def test_forgot_is_rate_limited_but_still_looks_the_same(client):
    add("boss@example.com", "admin", with_pw=True)
    pages = [client.post("/forgot", data={"email": "boss@example.com"}).text for _ in range(5)]
    assert len(set(pages)) == 1
    assert len(client.sent) == main._FORGOT_MAX_EMAIL


def test_forgot_sends_nothing_to_a_disabled_account(client):
    add("gone@example.com", with_pw=True, active=False)
    client.post("/forgot", data={"email": "gone@example.com"})
    assert client.sent == []


def test_forgot_and_login_pages_are_public(client):
    add("boss@example.com", "admin", with_pw=True)
    assert client.get("/forgot", follow_redirects=False).status_code == 200
    assert "Forgot password?" in client.get("/login").text


# ---- admin sends ------------------------------------------------------------------
def test_new_account_gets_an_invite_with_a_long_life_and_the_admins_reply_to(client):
    a = add("boss@example.com", "admin", with_pw=True)
    login_as(client, a)
    r = client.post("/admin/users/new", data={"email": "sam@anker.example", "role": "vendor",
                                              "brands": ["ANKER"], "tools": ["items"], "active": "on"})
    assert r.status_code == 200 and client.sent == []        # creating sends nothing
    uid = SessionLocal().query(User).filter_by(email="sam@anker.example").one().id
    r = client.post(f"/admin/users/{uid}/send-link", data={"note": "Hi Sam", "send": "email"})
    assert r.status_code == 200
    s = client.sent[-1]
    assert s["purpose"] == notify.INVITE and s["note"] == "Hi Sam" and s["vendor"] is True
    assert s["invited_by"] == "boss@example.com"
    assert s["expires_min"] == main.INVITE_TOKEN_TTL_MIN
    u = get(uid)
    assert u.reset_purpose == notify.INVITE
    assert u.reset_expires_at > datetime.utcnow() + timedelta(days=6)
    assert s["url"] in r.text                                # link shown as well


def test_existing_password_gets_a_reset_and_link_only_sends_nothing(client):
    a = add("boss@example.com", "admin", with_pw=True)
    s_id = add("sales@example.com", with_pw=True)
    login_as(client, a)
    client.post(f"/admin/users/{s_id}/send-link", data={"send": "email"})
    assert client.sent[-1]["purpose"] == notify.RESET
    n = len(client.sent)
    r = client.post(f"/admin/users/{s_id}/send-link", data={"send": "link"})
    assert len(client.sent) == n and "/set-password?token=" in r.text


def test_disabled_account_gets_no_link(client):
    a = add("boss@example.com", "admin", with_pw=True)
    d = add("old@example.com", active=False)
    login_as(client, a)
    r = client.post(f"/admin/users/{d}/send-link", data={"send": "email"})
    assert r.status_code == 400 and client.sent == [] and get(d).reset_token_hash is None


def test_only_admins_send_links(client):
    add("boss@example.com", "admin", with_pw=True)
    s = add("sales@example.com", with_pw=True)
    other = add("other@example.com")
    login_as(client, s)
    assert client.post(f"/admin/users/{other}/send-link", data={"send": "email"}).status_code == 403
    assert client.sent == []


# ---- using the link ------------------------------------------------------------------
def _mint(client, uid):
    a = add("boss@example.com", "admin", with_pw=True)
    login_as(client, a)
    client.post(f"/admin/users/{uid}/send-link", data={"send": "email"})
    client.cookies.clear()
    return client.sent[-1]["url"].split("token=")[1]


def test_invite_link_sets_the_password_once(client):
    uid = add("new@example.com")
    token = _mint(client, uid)
    page = client.get(f"/set-password?token={token}").text
    assert "choose your password" in page
    r = client.post("/set-password", data={"token": token, "password": PW, "confirm": PW},
                    follow_redirects=False)
    assert r.status_code == 303
    u = get(uid)
    assert u.password_hash and u.reset_token_hash is None and u.reset_purpose is None
    again = client.post("/set-password", data={"token": token, "password": PW2, "confirm": PW2})
    assert again.status_code == 400


def test_link_dies_when_the_account_is_disabled(client):
    uid = add("new@example.com")
    token = _mint(client, uid)
    db = SessionLocal()
    db.get(User, uid).active = False
    db.commit()
    db.close()
    assert "Link expired" in client.get(f"/set-password?token={token}").text


# ---- the email itself -----------------------------------------------------------------
def test_compose_and_payload(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "WEBHOOK_URL", "http://n8n:5678/webhook/3pl-account-email")
    monkeypatch.setattr(notify, "WEBHOOK_TOKEN", "tok")
    monkeypatch.setattr(notify, "_post", lambda url, body, headers: posted.append(
        (url, json.loads(body), headers)) or 200)
    ok = notify.send_account_link(to="sam@anker.example", url="https://r/set-password?token=x",
                                  purpose=notify.INVITE, note="Hi Sam", invited_by="boss@example.com",
                                  expires_min=10080, vendor=True)
    assert ok
    url, p, h = posted[0]
    assert p["app"] == "reporting" and p["to"] == "sam@anker.example" and p["reply_to"] == "boss@example.com"
    assert p["expires_in"] == "7 days" and "Macgear Reporting" in p["subject"]
    assert "your brands" in p["body"] and "Hi Sam" in p["body"] and "https://r/set-password?token=x" in p["body"]
    assert h["X-Sync-Token"] == "tok"


def test_send_never_raises_and_reports_false_when_off_or_failing(monkeypatch):
    monkeypatch.setattr(notify, "WEBHOOK_URL", "")
    assert notify.send_account_link(to="a@b.c", url="u") is False
    monkeypatch.setattr(notify, "WEBHOOK_URL", "http://n8n/x")
    monkeypatch.setattr(notify, "_post", lambda *a: (_ for _ in ()).throw(OSError("down")))
    assert notify.send_account_link(to="a@b.c", url="u") is False
