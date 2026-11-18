"""Sign-in and usage logging for the admin pages (admin only — /admin/...).

Three things are kept, all in the app's own SQLite (not the warehouse):
  - users.last_login / last_login_ip / login_count / last_seen_at on each account;
  - auth_events: sign-ins, failed sign-ins, sign-outs, password set, reset requests,
    links sent, and every admin change to an account (with what changed);
  - usage_days: page opens per user x Sydney day x area (a report / tool key, 'home',
    'admin' or 'other') — what people use, not a click trail.

Logging must never break a request: every write is wrapped and swallows errors.
Times are stored UTC (the house rule) and shown in Sydney time.
Client IP: the site sits behind Caddy, which appends the real client to
X-Forwarded-For (and drops whatever a client sent itself), so the LAST entry is
the one to trust; locally there is no proxy and request.client is used.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import AuthEvent, UsageDay, User

SYD = ZoneInfo("Australia/Sydney")
KEEP_EVENTS_DAYS = 400                 # a year and a bit of sign-in history
SEEN_EVERY_SEC = 5 * 60                # last_seen_at written at most this often per user

EVENTS = {
    "sign_in": "Signed in",
    "sign_in_failed": "Failed sign-in",
    "sign_out": "Signed out",
    "password_set": "Set password",
    "reset_requested": "Asked for a reset (Forgot password)",
    "link_sent": "Sign-in link sent",
    "account_created": "Account created",
    "account_changed": "Account changed",
    "account_disabled": "Account disabled",
    "account_enabled": "Account enabled",
    "account_deleted": "Account deleted",
}
ACCOUNT_EVENTS = {"account_created", "account_changed", "account_disabled", "account_enabled",
                  "account_deleted", "link_sent"}

_seen: dict[int, float] = {}           # user id -> last time last_seen_at was written


def now_utc() -> datetime:
    return datetime.utcnow()


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _agent(request) -> str:
    return (request.headers.get("user-agent", "") or "")[:300]


def log(db: Session, event: str, request=None, user: User | None = None, email: str = "",
        actor=None, detail: str = "", commit: bool = True) -> None:
    """Record one event. `actor` = the signed-in admin making an account change."""
    try:
        db.add(AuthEvent(
            at=now_utc(), event=event,
            user_id=getattr(user, "id", None),
            email=(email or getattr(user, "email", "") or "")[:254],
            actor_email=(getattr(actor, "email", "") or "")[:254] if actor is not None else "",
            ip=client_ip(request) if request is not None else "",
            user_agent=_agent(request) if request is not None else "",
            detail=detail[:2000]))
        if commit:
            db.commit()
    except Exception:
        db.rollback()


def signed_in(db: Session, user: User, request) -> None:
    user.last_login = now_utc()
    user.last_login_ip = client_ip(request)
    user.login_count = (user.login_count or 0) + 1
    user.last_seen_at = user.last_login
    log(db, "sign_in", request, user=user, commit=False)
    db.commit()


def page_view(user_id: int, area: str) -> None:
    """Count one page open and keep last_seen_at fresh. Own session; never raises."""
    db = SessionLocal()
    try:
        t = now_utc()
        day = t.replace(tzinfo=timezone.utc).astimezone(SYD).date()
        row = db.get(UsageDay, (user_id, day, area))
        if row is None:
            db.add(UsageDay(user_id=user_id, day=day, area=area, views=1, last_at=t))
        else:
            row.views += 1
            row.last_at = t
        mono = time.monotonic()
        if mono - _seen.get(user_id, -1e9) >= SEEN_EVERY_SEC:
            db.execute(update(User).where(User.id == user_id).values(last_seen_at=t))
            _seen[user_id] = mono
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def prune(db: Session) -> int:
    """Drop events older than KEEP_EVENTS_DAYS (run at startup)."""
    try:
        n = db.execute(delete(AuthEvent).where(
            AuthEvent.at < now_utc() - timedelta(days=KEEP_EVENTS_DAYS))).rowcount
        db.commit()
        return n or 0
    except Exception:
        db.rollback()
        return 0


# ---- describing an admin's change to an account -------------------------------

def snapshot(u: User, perms) -> dict:
    """The parts of an account an admin can change, in words, for a before/after diff."""
    return {
        "Email": u.email or "",
        "Name": u.name or "",
        "Role": u.role or "",
        "Company": u.company or "",
        "Active": "yes" if u.active else "no",
        "Areas": ", ".join(perms.AREA_LABELS.get(a, a).split(" (")[0] for a in perms.effective_access(u)) or "none",
        "Extra data": ", ".join(sorted(perms.effective_caps(u))) or "none",
        "Brands": "all" if perms.effective_brands(u) is None else (", ".join(perms.effective_brands(u)) or "none"),
        "Regions": ", ".join(perms.effective_regions(u)),
    }


def changes(before: dict, after: dict) -> str:
    out = []
    for k in after:
        if before.get(k) != after[k]:
            out.append(f"{k}: {before.get(k) or '—'} → {after[k] or '—'}")
    return "; ".join(out)


# ---- what the admin pages read ------------------------------------------------

def sydney(dt: datetime | None) -> datetime | None:
    return None if dt is None else dt.replace(tzinfo=timezone.utc).astimezone(SYD)


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    """'just now' / '12 min ago' / '3 h ago' / 'yesterday' / '5 days ago' / '7 weeks ago'."""
    if dt is None:
        return ""
    s = ((now or now_utc()) - dt).total_seconds()
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    d = int(s // 86400)
    if d == 1:
        return "yesterday"
    if d < 21:
        return f"{d} days ago"
    if d < 120:
        return f"{d // 7} weeks ago"
    return f"{d // 30} months ago"


def failed_since_success(db: Session) -> dict[int, int]:
    """Per user id: failed sign-ins (by their email) since their last successful one,
    within the last 30 days — a hint someone is guessing, or forgot their password."""
    since = now_utc() - timedelta(days=30)
    users = db.execute(select(User.id, User.email, User.last_login)).all()
    fails = db.execute(select(AuthEvent.email, AuthEvent.at).where(
        AuthEvent.event == "sign_in_failed", AuthEvent.at >= since)).all()
    by_email: dict[str, list] = {}
    for email, at in fails:
        by_email.setdefault((email or "").lower(), []).append(at)
    out = {}
    for uid, email, last in users:
        n = sum(1 for at in by_email.get(email.lower(), []) if last is None or at > last)
        if n:
            out[uid] = n
    return out


def user_history(db: Session, user: User, limit: int = 25) -> dict:
    """Events and usage for one account (the user form's Activity card)."""
    events = db.scalars(select(AuthEvent).where(
        (AuthEvent.user_id == user.id) | ((AuthEvent.user_id.is_(None)) & (func.lower(AuthEvent.email) == user.email.lower())))
        .order_by(AuthEvent.at.desc()).limit(limit)).all()
    since = (now_utc() - timedelta(days=30)).replace(tzinfo=timezone.utc).astimezone(SYD).date()
    usage = db.execute(select(UsageDay.area, func.sum(UsageDay.views), func.count(UsageDay.day),
                              func.max(UsageDay.last_at))
                       .where(UsageDay.user_id == user.id, UsageDay.day >= since)
                       .group_by(UsageDay.area).order_by(func.sum(UsageDay.views).desc())).all()
    days = db.scalar(select(func.count(func.distinct(UsageDay.day))).where(
        UsageDay.user_id == user.id, UsageDay.day >= since)) or 0
    return {"events": events, "usage": usage, "days_30": days,
            "failed": failed_since_success(db).get(user.id, 0)}


def overview(db: Session, days: int, event: str = "", who: str = "", limit: int = 300) -> dict:
    """The /admin/activity page: headline counts, usage by area, and the event list."""
    now = now_utc()
    since = now - timedelta(days=days)
    since_day = sydney(since).date()
    q = select(AuthEvent).where(AuthEvent.at >= since)
    if event == "account":
        q = q.where(AuthEvent.event.in_(ACCOUNT_EVENTS))
    elif event:
        q = q.where(AuthEvent.event == event)
    if who:
        q = q.where(func.lower(AuthEvent.email).contains(who.lower())
                    | func.lower(AuthEvent.actor_email).contains(who.lower()))
    events = db.scalars(q.order_by(AuthEvent.at.desc()).limit(limit)).all()
    total = db.scalar(select(func.count()).select_from(q.subquery()))

    def count(ev):
        return db.scalar(select(func.count()).select_from(AuthEvent).where(
            AuthEvent.event == ev, AuthEvent.at >= since)) or 0

    users = db.scalars(select(User)).all()
    active = [u for u in users if u.active]
    seen = [u for u in active if u.last_seen_at and u.last_seen_at >= since]
    never = [u for u in active if u.last_login is None]
    idle = [u for u in active if u.last_login is not None
            and (u.last_seen_at or u.last_login) < now - timedelta(days=30)]
    failed_ips = db.execute(select(AuthEvent.ip, func.count()).where(
        AuthEvent.event == "sign_in_failed", AuthEvent.at >= since)
        .group_by(AuthEvent.ip).order_by(func.count().desc()).limit(5)).all()
    usage = db.execute(select(UsageDay.area, func.sum(UsageDay.views),
                              func.count(func.distinct(UsageDay.user_id)), func.max(UsageDay.last_at))
                       .where(UsageDay.day >= since_day)
                       .group_by(UsageDay.area).order_by(func.sum(UsageDay.views).desc())).all()
    return {"events": events, "total": total, "sign_ins": count("sign_in"),
            "failed": count("sign_in_failed"), "active_users": len(seen), "accounts": len(active),
            "never": never, "idle": idle, "failed_ips": failed_ips, "usage": usage}
