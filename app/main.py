"""FastAPI app: Macgear Reporting — reports over the warehouse, plus internal tools."""
import hashlib
import hmac
import json
import os
import shutil
import time
import urllib.request
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Request, Response, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import (Base, SessionLocal, UPLOAD_DIR, engine, ensure_schema, get_db,
                 migrate_statuses, rebuild_reference_tables)
from .export import build_workbook
from .models import (Attachment, CustomerClaim, LineItem, ProcessedInvoice,
                     PromoRetailer, Promotion, Retailer, User,
                     VendorRequest)
from .service import build_promo_view, line_dates
from .pricing_sync import sync_pricing
from .security import (hash_password, hash_token, make_reset_token, sign,
                       unsign, verify_password)
from . import (bill_export, bill_service, field, items, notify, perms, promo_data, receipts, sales,
               sales_explorer, sell_through, st_table, st_weekly, updates, warehouse, workflow)

HERE = os.path.dirname(os.path.abspath(__file__))
rebuild_reference_tables()   # drop legacy products/retailers so they rebuild country-scoped
Base.metadata.create_all(engine)
ensure_schema()
migrate_statuses()

app = FastAPI(title="Macgear Reporting", lifespan=lambda a: _lifespan(a))
# Report pages embed their data as JSON (the table can be several hundred KB);
# gzip cuts that ~8x over the wire once deployed.
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))


# ---- template helpers --------------------------------------------------------
def money(v):
    return "" if v is None else f"{v:,.2f}"


def pct(v):
    return "" if v is None else f"{v * 100:.1f}%"


# SQLite stores server_default=now() timestamps in UTC, and the droplet runs UTC
# too, so a bare timestamp reads a day early for most of the Sydney working day.
def sydney(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Australia/Sydney")).strftime("%d/%m/%Y")


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.filters["sydney"] = sydney
# Repsly times are the rep's local wall-clock time (no zone), shown as they are.
templates.env.filters["hm"] = lambda dt: "" if dt is None else dt.strftime("%H:%M")
templates.env.filters["dmy"] = lambda d: "" if d is None else f"{d:%a} {d.day} {d:%b}"
# slug for pill css classes, and the workflow taxonomy, available in all templates
templates.env.filters["slug"] = lambda s: (s or "").lower().replace(" ", "")
templates.env.globals.update(workflow.context())
# cache-busting version for static assets (changes whenever any static file is updated)
try:
    _static = os.path.join(HERE, "static")
    asset_ver = str(int(max(os.path.getmtime(os.path.join(_static, f)) for f in os.listdir(_static))))
except (OSError, ValueError):
    asset_ver = "1"
templates.env.globals["asset_ver"] = asset_ver


# ---- authentication (per-user accounts, signed-cookie session) ---------------
# Users live in the `users` table and are managed at /admin/users. Env on the droplet:
#   APP_SECRET    random string signing the session cookie (set a long one!)
#   APP_USERNAME  legacy shared username (default "macgear")
#   APP_PASSWORD  legacy shared password — see the fallback note below
#
# LEGACY SHARED LOGIN: while APP_PASSWORD is set, those credentials still sign you
# in as a synthetic admin. It is the migration path off the old single shared
# login and the way back in if the last real admin account is lost. Blank
# APP_PASSWORD once real admin accounts exist and are known to work.
#
# If APP_PASSWORD is blank AND no users exist, auth is disabled entirely — that
# is local dev, and it is why a deployment must always set one or the other.
APP_USERNAME = os.environ.get("APP_USERNAME", "macgear")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_SECRET = os.environ.get("APP_SECRET", "") or "dev-insecure-secret-change-me"
COOKIE_NAME = "claims_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30          # 30 days
# A reset is asked for by the person who will use it, seconds later; an invite may sit in
# an inbox until tomorrow. Same split as 3PL Portal.
RESET_TOKEN_TTL_MIN = int(os.environ.get("RESET_TOKEN_TTL_MIN", "45"))
INVITE_TOKEN_TTL_MIN = int(os.environ.get("INVITE_TOKEN_TTL_MIN", str(7 * 24 * 60)))
# paths reachable without a login (static assets, the login and set-password
# pages, and the token-authenticated pricing sync endpoint that n8n posts to)
_AUTH_EXEMPT_PREFIXES = ("/static", "/login", "/logout", "/set-password", "/forgot",
                         "/admin/sync-pricing")


class LegacyAdmin:
    """Stand-in user for the shared APP_PASSWORD login. Quacks like a User for
    perms.py and the templates, but has no database row and cannot be edited."""
    id = None
    email = APP_USERNAME
    name = "Shared login"
    role = "admin"
    allowed_tools = None
    allowed_brands = None
    allowed_regions = None
    company = ""
    active = True
    is_legacy = True


def _any_users(db: Session) -> bool:
    return db.scalar(select(func.count()).select_from(User)) > 0


def _user_from_cookie(request: Request):
    """Resolve the signed cookie to a user, or None. The cookie holds either a
    user id or the literal "legacy" for the shared login."""
    value = unsign(request.cookies.get(COOKIE_NAME, ""), APP_SECRET)
    if not value:
        return None
    if value == "legacy":
        return LegacyAdmin() if APP_PASSWORD else None
    if not value.isdigit():
        return None
    db = SessionLocal()
    try:
        user = db.get(User, int(value))
        if user and user.active:
            db.expunge(user)       # detached copy for the request; never written back
            return user
        return None
    finally:
        db.close()


# Which tool owns which URL prefix. Enforced in the middleware rather than on
# each handler: a tool has a dozen routes and forgetting one is a silent hole.
# Longest prefix wins, so a new tool nested under an existing one still works.
_TOOL_PATHS = [
    ("/reports/sales", "sales"),
    ("/reports/sell-through", "sell_through"),
    ("/reports/items", "items"),
    ("/reports/table", "st_table"),
    ("/reports/weekly", "st_weekly"),
    ("/reports/field", "field"),
    ("/vendor-bills", "vendor_bills"),
    ("/promos", "promos"),
    ("/promo", "promos"),          # /promo/new, /promo/{id}, /promo/{id}/...
    ("/line", "promos"),
    ("/retailer", "promos"),
    ("/claim", "promos"),
    ("/vendor-request", "promos"),
    ("/attachment", "promos"),
    ("/help", "promos"),
]


# Paths every signed-in user, vendors included, may open without an area.
_VENDOR_OPEN_PATHS = ("/", "/updates")


def _tool_for_path(path: str) -> str | None:
    best = None
    for prefix, key in _TOOL_PATHS:
        if (path == prefix or path.startswith(prefix + "/")) and (
                best is None or len(prefix) > len(best[0])):
            best = (prefix, key)
    return best[1] if best else None


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if any(path == p or path.startswith(p + "/") for p in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)

    # Auth off only when there is nothing at all to authenticate against.
    if not APP_PASSWORD:
        db = SessionLocal()
        try:
            if not _any_users(db):
                request.state.user = None
                return await call_next(request)
        finally:
            db.close()

    user = _user_from_cookie(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    request.state.user = user

    if path.startswith("/admin/users") and not perms.is_admin(user):
        return templates.TemplateResponse(request, "denied.html",
                                          {"tool": "User management"}, status_code=403)

    tool = _tool_for_path(path)
    # Vendors are allow-listed, not deny-listed: the landing page plus the
    # reports ticked on their account, and nothing else. A path no area claims
    # (a new tool someone forgot to add to _TOOL_PATHS, /api/..., /tools) is
    # refused rather than let through.
    if perms.is_vendor(user) and path not in _VENDOR_OPEN_PATHS and tool is None:
        return templates.TemplateResponse(request, "denied.html",
                                          {"tool": "this page"}, status_code=403)
    if tool and not perms.can_access(user, tool):
        return templates.TemplateResponse(
            request, "denied.html",
            {"tool": perms.AREA_LABELS.get(tool, tool)}, status_code=403)

    return await call_next(request)


def current_user(request: Request):
    return getattr(request.state, "user", None)


templates.env.globals["current_user"] = current_user
templates.env.globals["perms"] = perms


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _user_from_cookie(request):
        return RedirectResponse("/", status_code=303)
    notice = ("Your password has been set — please sign in."
              if request.query_params.get("msg") == "set" else "")
    return templates.TemplateResponse(request, "login.html",
                                      {"error": "", "notice": notice})


@app.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    ident = (form.get("username", "") or "").strip()
    pw = form.get("password", "")

    user = db.scalar(select(User).where(User.email == ident.lower()))
    if user and user.active and user.password_hash and verify_password(pw, user.password_hash):
        user.last_login = datetime.utcnow()
        db.commit()
        token = sign(str(user.id), APP_SECRET)
    elif (APP_PASSWORD and hmac.compare_digest(ident, APP_USERNAME)
            and hmac.compare_digest(pw, APP_PASSWORD)):
        token = sign("legacy", APP_SECRET)
    else:
        # One message for every failure — a distinct "no such user" would let
        # anyone enumerate who has an account here.
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Incorrect email or password.", "notice": ""},
            status_code=401)

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, token, max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---- invites, resets and set-password (public, single-use token) -------------
# Same design as 3PL Portal: only the token's SHA-256 digest, its expiry and its purpose are
# stored; the email goes out through the shared n8n workflow (app/notify.py); the link is
# also shown to the admin, because "the webhook accepted it" is not "it arrived".
def _issue_link(db: Session, user: User, request: Request, *, purpose: str,
                note: str = "", invited_by: str = "", send: bool = True) -> tuple[str, bool]:
    """Mint a single-use link and (optionally) email it. Returns (link, emailed).
    A disabled account gets no token. Minting kills any earlier link for the user."""
    if not user.active:
        return "", False
    ttl = INVITE_TOKEN_TTL_MIN if purpose == notify.INVITE else RESET_TOKEN_TTL_MIN
    raw = make_reset_token()
    user.reset_token_hash = hash_token(raw)
    user.reset_expires_at = datetime.utcnow() + timedelta(minutes=ttl)
    user.reset_purpose = purpose
    db.commit()
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    link = f"{base}/set-password?token={raw}"
    emailed = send and notify.send_account_link(
        to=user.email, url=link, purpose=purpose, note=note, invited_by=invited_by,
        expires_min=ttl, vendor=user.role == "vendor")
    return link, emailed


def _user_for_token(db: Session, raw: str):
    if not raw:
        return None
    user = db.scalar(select(User).where(User.reset_token_hash == hash_token(raw)))
    if not user or not user.reset_expires_at or user.reset_expires_at < datetime.utcnow():
        return None
    if not user.active:            # a link minted before the account was disabled dies
        return None
    return user


def _purpose_for_token(db: Session, raw: str) -> str:
    """Invite or reset, even for an expired link, so the page words it right. Safe without
    a validity check: the token is 256 random bits, not a guessable address."""
    if not raw:
        return notify.RESET
    user = db.scalar(select(User).where(User.reset_token_hash == hash_token(raw)))
    return (user.reset_purpose if user and user.reset_purpose else notify.RESET)


def user_status(u: User) -> tuple[str, str]:
    """(pill class, label) — shared by the users list and the user form."""
    if not u.active:
        return "pg-red", "disabled"
    if u.password_hash:
        return "pg-green", "active"
    if u.reset_expires_at and u.reset_expires_at > datetime.utcnow():
        return "pg-amber", "invited · link expires " + _short_time(
            u.reset_expires_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Australia/Sydney")))
    return "pg-grey", "not invited yet"


templates.env.globals["user_status"] = user_status


# Mail-bomb guard for /forgot, by address and by client IP: in-process and forgotten on
# restart — it stops someone holding down F5 on a person's inbox, it is not a security
# control. Admin buttons aren't limited: an admin pressing send twice means it.
_FORGOT_WINDOW_S = 15 * 60
_FORGOT_MAX_EMAIL = 3
_FORGOT_MAX_IP = 10
_forgot_hits: dict = {}


def _forgot_allowed(email: str, ip: str) -> bool:
    now = time.monotonic()
    if len(_forgot_hits) > 500:
        _forgot_hits.clear()
    ok = True
    for key, cap in ((f"e:{email}", _FORGOT_MAX_EMAIL), (f"i:{ip}", _FORGOT_MAX_IP)):
        hits = [t for t in _forgot_hits.get(key, []) if now - t < _FORGOT_WINDOW_S]
        if len(hits) >= cap:
            ok = False
        hits.append(now)
        _forgot_hits[key] = hits
    return ok


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(request, "forgot_password.html",
                                      {"sent": False, "ttl": notify.in_words(RESET_TOKEN_TTL_MIN)})


@app.post("/forgot", response_class=HTMLResponse)
async def forgot_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email", "") or "").strip().lower()
    user = db.scalar(select(User).where(User.email == email)) if email else None
    ip = request.client.host if request.client else "?"
    if user and user.active and _forgot_allowed(email, ip):
        _issue_link(db, user, request, purpose=notify.RESET)
    # The same page whether or not the address exists, and whether or not we throttled —
    # anything else would tell a stranger who has an account here.
    return templates.TemplateResponse(request, "forgot_password.html",
                                      {"sent": True, "ttl": notify.in_words(RESET_TOKEN_TTL_MIN)})


@app.get("/set-password", response_class=HTMLResponse)
def set_password_form(request: Request, token: str = "", db: Session = Depends(get_db)):
    user = _user_for_token(db, token)
    return templates.TemplateResponse(request, "set_password.html", {
        "token": token, "email": user.email if user else "",
        "invalid": user is None, "error": "", "purpose": _purpose_for_token(db, token)})


@app.post("/set-password", response_class=HTMLResponse)
async def set_password_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = form.get("token", "")
    pw = form.get("password", "")
    confirm = form.get("confirm", "")
    user = _user_for_token(db, token)

    ctx = {"token": token, "email": user.email if user else "", "invalid": user is None,
           "purpose": _purpose_for_token(db, token)}
    if user is None:
        return templates.TemplateResponse(request, "set_password.html",
                                          {**ctx, "error": ""}, status_code=400)
    if len(pw) < 10:
        return templates.TemplateResponse(
            request, "set_password.html",
            {**ctx, "error": "Use at least 10 characters."}, status_code=400)
    if pw != confirm:
        return templates.TemplateResponse(
            request, "set_password.html",
            {**ctx, "error": "The two passwords do not match."}, status_code=400)

    user.password_hash = hash_password(pw)
    user.reset_token_hash = None       # single use
    user.reset_expires_at = None
    user.reset_purpose = None
    db.commit()
    return RedirectResponse("/login?msg=set", status_code=303)


# live FX (local->USD) for guidance on the new-promo form, cached ~6h, fails soft
_FX_CACHE = {"ts": 0.0, "rates": None}


def get_live_fx():
    now = time.time()
    if _FX_CACHE["rates"] is not None and now - _FX_CACHE["ts"] < 6 * 3600:
        return _FX_CACHE["rates"]
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=4) as resp:
            data = json.load(resp)
        aud = data.get("rates", {}).get("AUD")
        nzd = data.get("rates", {}).get("NZD")
        if aud and nzd:
            _FX_CACHE.update(ts=now, rates={"AUD": round(1 / aud, 4),
                                            "NZD": round(1 / nzd, 4)})
    except Exception:
        pass  # leave any prior (stale) value in place; template hides it if None
    return _FX_CACHE["rates"]


def _parse_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_date(v):
    return datetime.strptime(v, "%Y-%m-%d").date() if v else None


# ---- landing pages -----------------------------------------------------------
# "/" lists the reports a user may open; "/tools" lists the internal tools.
# Add a tile here when a report or tool is added, its key to perms.REPORT_KEYS /
# TOOL_KEYS and AREA_LABELS, and its URL prefix to _TOOL_PATHS.
REPORTS = [
    {"key": "sales", "href": "/reports/sales", "title": "Sales Explorer", "icon": "$",
     "blurb": "Macgear revenue by any period, vs last year — build the table however you like."},
    {"key": "items", "href": "/reports/items", "title": "Item Explorer", "icon": "◎",
     "blurb": "Search any product: sell-through, stock and cover by retailer, with trends and insights."},
    {"key": "st_table", "href": "/reports/table", "title": "Sell-Through Table", "icon": "▦",
     "blurb": "The weekly workbook as a live table — filter, sort, and download to Excel."},
    {"key": "st_weekly", "href": "/reports/weekly", "title": "Weekly Sell-Through", "icon": "▤",
     "blurb": "The consolidated weekly report: sell-through vs sell-in, stock, cover and PO ETAs."},
    {"key": "sell_through", "href": "/reports/sell-through", "title": "Sell-Through Dashboard", "icon": "◔",
     "blurb": "Weekly units, retail value, stock and cover by retailer and product."},
    {"key": "field", "href": "/reports/field", "title": "Field Activity", "icon": "⚑",
     "blurb": "Mova reps: visits, schedule, photos and display compliance (Harvey Norman core range)."},
]
TOOLS = [
    {"key": "promos", "href": "/promos", "title": "Vendor Promos", "icon": "%",
     "blurb": "Promotional credit claims — customer claims in, vendor credits out."},
    {"key": "vendor_bills", "href": "/vendor-bills", "title": "PDF Bill Upload", "icon": "⇪",
     "blurb": "Amazon Advertising invoices → NetSuite vendor bill import CSV."},
]


def _allowed(request: Request) -> list[str]:
    user = current_user(request)
    return perms.effective_access(user) if user else perms.AREA_KEYS   # auth off = local dev


def nav_tools(request: Request) -> list[dict]:
    return [t for t in TOOLS if t["key"] in _allowed(request)]


def nav_reports(request: Request) -> list[dict]:
    # Sales Explorer is all Macgear $: list it only for people who may see them.
    caps = _caps(request)
    return [r for r in REPORTS if r["key"] in _allowed(request)
            and (r["key"] != "sales" or "sell_in_value" in caps)]


def my_updates(request: Request) -> list[dict]:
    """The update log entries this user may see (areas as the side menu lists them)."""
    user = current_user(request)
    areas = {r["key"] for r in nav_reports(request)} | {t["key"] for t in nav_tools(request)}
    if user is None or perms.is_admin(user):
        areas.add("admin")
    return updates.visible(updates.load(), areas)


def updates_latest(request: Request) -> str:
    u = my_updates(request)
    return u[0]["date"].isoformat() if u else ""


@app.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request):
    return templates.TemplateResponse(request, "updates.html", {"entries": my_updates(request)})


templates.env.globals["updates_latest"] = updates_latest
templates.env.globals["nav_tools"] = nav_tools
templates.env.globals["nav_reports"] = nav_reports


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Home: internal sales (sell_in_value capability only — Macgear's sales $
    never reach a vendor), a sell-through snapshot for anyone with a sell-through
    report, then the report and tool cards."""
    user = current_user(request)
    caps = _caps(request)
    brands, regions = _scope(request)
    ctx = {"reports": nav_reports(request), "tools": nav_tools(request),
           "first_name": ((user.name or "").split() or [""])[0] if user is not None else "",
           "sales": None, "sales_error": "", "sales_fresh": "", "margin": "margin" in caps,
           "st": None, "st_week": "", "scoped": brands is not None}
    try:
        if "sell_in_value" in caps:
            ctx["sales"] = sales.summary(sales_today(), "margin" in caps)
            fresh = warehouse.cached(("sales_fresh",), ttl=sales.SALES_TTL, build=lambda: warehouse.query(
                "select max(last_modified) from reporting.fact_sales_line where date_key >= %s",
                (int((date.today() - timedelta(days=7)).strftime("%Y%m%d")),))[0][0])
            ctx["sales_fresh"] = _short_time(_brisbane_to_sydney(fresh))
        allowed = _allowed(request)
        if "items" in allowed or "sell_through" in allowed or "st_table" in allowed:
            st = {}
            for r in regions:
                board = items.leaderboard_for(brands, r)
                its = board["items"]
                if not its:
                    continue
                n = len(board["weeks"])
                spark = [sum(i["spark"][k] for i in its) for k in range(n)]
                top = sorted([i for i in its if i["units"] > 0], key=lambda i: (-i["units"], i["code"]))[:5]
                st[r] = {"units": spark[-1], "prev": spark[-2] if n > 1 else 0, "spark": spark,
                         "top": [{"code": i["code"], "name": i["name"], "units": i["units"]} for i in top]}
                ctx["st_week"] = _weekfmt(board["weeks"][-1])
            ctx["st"] = st or None
    except warehouse.WarehouseUnavailable as e:
        ctx["sales_error"] = _warehouse_error(e)
    return templates.TemplateResponse(request, "home.html", ctx)


def _brisbane_to_sydney(dt):
    """NetSuite's Last Modified carries no zone. The 15-minute Zone exports stamp
    it in Brisbane time (no daylight saving), an hour behind Sydney in summer —
    read as Sydney it looked an hour stale. Manual exports stamp Sydney time, but
    the newest change is always from a scheduled run."""
    if not dt:
        return dt
    return dt.replace(tzinfo=ZoneInfo("Australia/Brisbane")).astimezone(ZoneInfo("Australia/Sydney"))


def _short_time(dt) -> str:
    """'15 Oct 3:01pm'. NetSuite's Last Modified is the exporting user's local
    time (Sydney or Brisbane), shown as-is."""
    if not dt:
        return "—"
    return f"{dt.day} {dt:%b} {dt.hour % 12 or 12}:{dt:%M}{'am' if dt.hour < 12 else 'pm'}"


def sales_today() -> date:
    """'Today' in Sydney — the server runs UTC, which is yesterday until 10–11am.
    REPORT_TODAY=YYYY-MM-DD pretends it's another day (local testing only, e.g.
    to see Monday's receipt strip before the files arrive)."""
    fake = os.environ.get("REPORT_TODAY", "")
    if fake:
        return date.fromisoformat(fake)
    return datetime.now(ZoneInfo("Australia/Sydney")).date()


@app.get("/tools", response_class=HTMLResponse)
def tools_home(request: Request):
    return templates.TemplateResponse(request, "tools.html", {"tools": nav_tools(request)})


# ---- reports -----------------------------------------------------------------
def _scope(request: Request):
    """The rows this user may see: (brands, or None for all; regions)."""
    user = current_user(request)
    if user is None:                     # auth off = local dev
        return None, list(perms.REGIONS)
    return perms.effective_brands(user), perms.effective_regions(user)


@app.get("/reports/sell-through", response_class=HTMLResponse)
def report_sell_through(request: Request):
    brands, regions = _scope(request)
    error = ""
    data = None
    try:
        data = sell_through.for_user(brands, regions)
    except warehouse.WarehouseUnavailable as e:
        error = f"The reporting warehouse could not be reached ({e})."
    return templates.TemplateResponse(request, "report_sell_through.html", {
        "data": data, "error": error, "scoped": brands is not None})


@app.get("/reports/weekly", response_class=HTMLResponse)
def report_weekly(request: Request):
    _, regions = _scope(request)
    return templates.TemplateResponse(request, "st_weekly.html", {
        "regions": regions, "sell_in": "sell_in" in _caps(request)})


@app.get("/reports/weekly/data.json")
def report_weekly_data(request: Request):
    """The page's whole dataset for this user's scope; the browser filters it."""
    brands, regions = _scope(request)
    try:
        return JSONResponse(st_weekly.for_user(brands, regions, "sell_in" in _caps(request)),
                            headers={"Cache-Control": "private, no-cache"})
    except warehouse.WarehouseUnavailable as e:
        return JSONResponse({"error": _warehouse_error(e)}, status_code=503)


@app.get("/reports/weekly/stores.json")
def report_weekly_stores(request: Request, region: str = "", retailer: str = "", code: str = ""):
    """Store-level rows on demand: one retailer's stores, or one product's stores."""
    brands, regions = _scope(request)
    if region not in regions:
        return JSONResponse({"error": "no access"}, status_code=403)
    try:
        return JSONResponse(st_weekly.stores_for(brands, region, retailer, code),
                            headers={"Cache-Control": "private, no-cache"})
    except warehouse.WarehouseUnavailable as e:
        return JSONResponse({"error": _warehouse_error(e)}, status_code=503)


def _caps(request: Request) -> set:
    user = current_user(request)
    return set(perms.CAP_KEYS) if user is None else perms.effective_caps(user)


def _region(request: Request, wanted: str) -> tuple[str, list[str]]:
    """The requested region if the user may see it, else their first one."""
    _, regions = _scope(request)
    return (wanted if wanted in regions else regions[0]), regions


def _warehouse_error(e) -> str:
    return f"The reporting warehouse could not be reached ({e})."


def _weekfmt(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%b %Y} (wk {d.isocalendar().week})"


templates.env.filters["weekfmt"] = _weekfmt


_ST_AREAS = ("sell_through", "items", "st_table", "st_weekly")


def st_status(request: Request):
    """Receipt strip data (templates/_st_status.html) for this user's scope, or
    None when they have no sell-through report or the warehouse is down."""
    if not any(a in _allowed(request) for a in _ST_AREAS):
        return None
    brands, regions = _scope(request)
    try:
        return receipts.status(brands, regions, sales_today())
    except warehouse.WarehouseUnavailable:
        return None


templates.env.globals["st_status"] = st_status
templates.env.filters["datefmt"] = lambda iso: f"{date.fromisoformat(iso).day} {date.fromisoformat(iso):%b %Y}"


# ---- cache warm-up --------------------------------------------------------------
def _warm_reports():
    """Build the views most people open first, for the all-brands scope, so the
    first visit of the day is instant. Vendor and brand-limited scopes stay warm
    once used (warehouse.refresh_due)."""
    items._units_by_item()
    sales.summary(sales_today(), True)
    for region in perms.REGIONS:
        sales_explorer.cubes(region, sales_today())
        sales_explorer.options(region, sales_today())
    receipts.status(None, list(perms.REGIONS), sales_today())
    sell_through.for_user(None, list(perms.REGIONS))
    st_weekly.for_user(None, list(perms.REGIONS), True)
    for region in perms.REGIONS:
        items.leaderboard_for(None, region)
        opts = st_table.options(None, region)
        if opts["weeks"]:
            st_table.build_for(None, region, "retailer", date.fromisoformat(opts["weeks"][0]),
                               12, "", "")
    # Weekly Sell-Through's store data last: the slowest (~10-20 s per retailer)
    for region in perms.REGIONS:
        for retailer in st_weekly.store_retailers(None, region):
            st_weekly.stores_for(None, region, retailer)


def deployment_problems(public_base_url: str, secret: str, shared_password: str,
                        has_users: bool) -> list[str]:
    """Reasons a DEPLOYED instance must not start. PUBLIC_BASE_URL is the marker
    of a deployment (it's required there for set-password links); local dev never
    sets it, so local runs are unaffected."""
    if not public_base_url:
        return []
    problems = []
    if not secret:
        problems.append("APP_SECRET is not set — sign-in cookies would be signed with the "
                        "built-in development secret and could be forged.")
    if not shared_password and not has_users:
        problems.append("APP_PASSWORD is blank and there are no user accounts — sign-in "
                        "would be switched off entirely.")
    return problems


def _check_deployment():
    db = SessionLocal()
    try:
        has_users = _any_users(db)
    finally:
        db.close()
    problems = deployment_problems(os.environ.get("PUBLIC_BASE_URL", ""),
                                   os.environ.get("APP_SECRET", ""), APP_PASSWORD, has_users)
    if problems:
        raise RuntimeError("Refusing to start: " + " ".join(problems))


@asynccontextmanager
async def _lifespan(app):
    _check_deployment()
    # Tests and machines without a warehouse URL skip the warm-up entirely.
    if os.environ.get("REPORTING_DB_URL"):
        warehouse.watch(sales.data_version, sales.SALES_CACHE_PREFIXES)
        warehouse.watch(field.data_version, field.CACHE_PREFIXES)
        warehouse.start_refresher(lambda: _warm_reports())
    yield


# ---- Item Explorer -------------------------------------------------------------
# Product images live on disk, one file per item code. Images aren't confidential
# (they're on the retailers' sites) but the route still checks the item is in
# the user's scope, like everything else here.
ITEM_IMG_DIR = os.path.join(os.path.dirname(HERE), "data", "item_images")
_IMG_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _img_name(code: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in code)


def _img_path(code: str) -> str | None:
    base = os.path.join(ITEM_IMG_DIR, _img_name(code))
    for ext in _IMG_TYPES:
        if os.path.exists(base + ext):
            return base + ext
    return None


@app.get("/reports/items", response_class=HTMLResponse)
def report_items(request: Request, region: str = ""):
    brands, _ = _scope(request)
    region, regions = _region(request, region)
    board, error = {"weeks": [], "items": []}, ""
    try:
        board = items.leaderboard_for(brands, region)
    except warehouse.WarehouseUnavailable as e:
        error = _warehouse_error(e)
    have = set(os.listdir(ITEM_IMG_DIR)) if os.path.isdir(ITEM_IMG_DIR) else set()
    stems = {os.path.splitext(n)[0] for n in have}
    images = [i["code"] for i in board["items"] if _img_name(i["code"]) in stems]
    return templates.TemplateResponse(request, "items.html", {
        "board": board, "region": region, "regions": regions, "error": error, "images": images})


@app.get("/reports/items/search")
def report_items_search(request: Request, q: str = "", limit: int = 12):
    brands, _ = _scope(request)
    try:
        return JSONResponse(items.search(q, brands, limit=max(1, min(limit, 50))))
    except warehouse.WarehouseUnavailable:
        return JSONResponse([], status_code=503)


@app.get("/reports/items/i/{code:path}", response_class=HTMLResponse)
def report_item(request: Request, code: str, region: str = "", weeks: int = 26):
    brands, _ = _scope(request)
    region, regions = _region(request, region)
    weeks = weeks if weeks in (4, 13, 26, 52) else 26
    caps = _caps(request)
    try:
        h, d = items.item_for(code, brands, region, weeks, caps)
        board = items.leaderboard_for(brands, region) if h else None
    except warehouse.WarehouseUnavailable as e:
        return templates.TemplateResponse(request, "denied.html",
                                          {"message": _warehouse_error(e)}, status_code=503)
    if h is None:                      # not found and out-of-scope look the same
        return templates.TemplateResponse(request, "denied.html",
                                          {"message": f"No item {code} in your reports."}, status_code=404)
    img = _img_path(h["code"])
    return templates.TemplateResponse(request, "item.html", {
        "h": {k: v for k, v in h.items() if k not in ("item_key", "ns_item_id")}, "d": d,
        "region": region, "regions": regions, "weeks": weeks, "caps": sorted(caps),
        "cur": "NZD" if region == "NZ" else "AUD", "rank": items.ranks(h["code"], h["brand"], board),
        "initials": "".join(w[0] for w in (h["name"] or "?").split()[:2]).upper(),
        "has_image": img is not None, "image_ver": int(os.path.getmtime(img)) if img else 0,
        "can_upload": perms.is_admin(current_user(request)) or current_user(request) is None})


@app.get("/reports/items/img/{code:path}")
def report_item_image(request: Request, code: str):
    brands, _ = _scope(request)
    path = _img_path(code)
    if path is None:
        return Response(status_code=404)
    try:
        if items.item_for(code, brands, perms.REGIONS[0], 4, set())[0] is None:
            return Response(status_code=404)
    except warehouse.WarehouseUnavailable:
        return Response(status_code=503)
    return FileResponse(path, media_type=_IMG_TYPES[os.path.splitext(path)[1]],
                        headers={"Cache-Control": "private, max-age=3600"})


@app.post("/reports/items/img/{code:path}")
async def report_item_image_upload(request: Request, code: str, image: UploadFile = File(...)):
    user = current_user(request)
    if user is not None and not perms.is_admin(user):
        return templates.TemplateResponse(request, "denied.html",
                                          {"tool": "image upload"}, status_code=403)
    ext = os.path.splitext(image.filename or "")[1].lower().replace(".jpeg", ".jpg")
    data = await image.read()
    if ext not in _IMG_TYPES or not data or len(data) > 5 * 1024 * 1024:
        return Response("Upload a JPG, PNG or WebP under 5 MB.", status_code=400)
    os.makedirs(ITEM_IMG_DIR, exist_ok=True)
    old = _img_path(code)
    if old:
        os.remove(old)
    with open(os.path.join(ITEM_IMG_DIR, _img_name(code) + ext), "wb") as fh:
        fh.write(data)
    return RedirectResponse(str(request.url_for("report_item", code=code)), status_code=303)


# ---- Sell-Through Table --------------------------------------------------------
def _table_args(request: Request, region, grain, week, prior, brand, retailer):
    brands, _ = _scope(request)
    region, regions = _region(request, region)
    opts = st_table.options(brands, region)
    week = week if week in opts["weeks"] else (opts["weeks"][0] if opts["weeks"] else "")
    prior = prior if prior in (4, 8, 12, 26, 52) else 12
    brand = brand if brand in opts["brands"] else ""
    if len(opts["brands"]) == 1:
        brand = opts["brands"][0]
    retailer = retailer if retailer in opts["retailers"] else ""
    grain = grain if grain in st_table.GRAINS else "retailer"
    f = {"region": region, "grain": grain, "week": week, "prior": prior,
         "brand": brand, "retailer": retailer}
    return brands, regions, opts, f


@app.get("/reports/table", response_class=HTMLResponse)
def report_table(request: Request, region: str = "", grain: str = "retailer", week: str = "",
                 prior: int = 12, brand: str = "", retailer: str = ""):
    try:
        brands, regions, opts, f = _table_args(request, region, grain, week, prior, brand, retailer)
        t = st_table.build_for(brands, f["region"], f["grain"],
                               date.fromisoformat(f["week"]) if f["week"] else None,
                               f["prior"], f["brand"], f["retailer"])
    except warehouse.WarehouseUnavailable as e:
        return templates.TemplateResponse(request, "denied.html",
                                          {"message": _warehouse_error(e)}, status_code=503)
    return templates.TemplateResponse(request, "st_table.html", {
        "t": t, "f": f, "opts": opts, "regions": regions,
        "wos_low": st_table.WOS_LOW, "wos_high": st_table.WOS_HIGH,
        "can_items": "items" in _allowed(request)})


@app.get("/reports/table/export.xlsx")
def report_table_xlsx(request: Request, region: str = "", grain: str = "retailer", week: str = "",
                      prior: int = 12, brand: str = "", retailer: str = ""):
    try:
        brands, _, _, f = _table_args(request, region, grain, week, prior, brand, retailer)
        t = st_table.build_for(brands, f["region"], f["grain"],
                               date.fromisoformat(f["week"]) if f["week"] else None,
                               f["prior"], f["brand"], f["retailer"])
    except warehouse.WarehouseUnavailable as e:
        return Response(_warehouse_error(e), status_code=503)
    desc = " · ".join(x for x in [f["region"], {"retailer": "Product x retailer", "product": "Product total",
                                               "store": "Product x store"}[f["grain"]],
                                  f["brand"] or ("All brands" if brands is None else ", ".join(brands)),
                                  f["retailer"] or "All retailers",
                                  f"week ending {f['week']}"] if x)
    body = st_table.to_xlsx(t, "Macgear Sell-Through", desc)
    name = f"Macgear_Sell_Through_{f['region']}_{f['grain']}_WE{f['week'].replace('-', '')}.xlsx"
    return Response(body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- Sales Explorer ----------------------------------------------------------------
# Macgear's own revenue: Sales Explorer access AND the sell_in_value capability (the
# middleware checks the first; these routes check the second). Never vendors.
def _sx_args(request: Request, region: str, p: str, fy: int, dims: str,
             brand: str, rep: str, retailer: str):
    caps = _caps(request)
    if "sell_in_value" not in caps:
        return None
    region, regions = _region(request, region)
    today = sales_today()
    fy = fy or (sales.fy_start(today).year + 1)
    f = {"brand": int(brand) if brand.lstrip("-").isdigit() else None,
         "rep": int(rep) if rep.lstrip("-").isdigit() else None,
         "retailer": retailer or None}
    dl = [d for d in (dims or "retailer").split(",") if d in sales_explorer.DIMS]
    return {"region": region, "regions": regions, "today": today, "code": p or "ytd",
            "fy": fy, "dims": dl, "f": f, "margin": "margin" in caps}


def _sx_denied(request):
    return templates.TemplateResponse(request, "denied.html", {
        "message": "Sales Explorer shows Macgear's sales $ — ask an admin to tick "
                   "\"Sell-in $\" on your account."}, status_code=403)


@app.get("/reports/sales", response_class=HTMLResponse)
def report_sales(request: Request, region: str = "", p: str = "ytd", fy: int = 0, dims: str = "",
                 brand: str = "", rep: str = "", retailer: str = ""):
    a = _sx_args(request, region, p, fy, dims, brand, rep, retailer)
    if a is None:
        return _sx_denied(request)
    try:
        data = sales_explorer.explore(a["region"], a["code"], a["fy"], a["today"], a["dims"],
                                      a["f"], a["margin"])
        opts = sales_explorer.options(a["region"], a["today"])
    except warehouse.WarehouseUnavailable as e:
        return templates.TemplateResponse(request, "denied.html",
                                          {"message": _warehouse_error(e)}, status_code=503)
    return templates.TemplateResponse(request, "sales_explorer.html", {
        "data": data, "opts": opts, "a": {k: v for k, v in a.items() if k != "today"},
        "regions": a["regions"], "dims_all": sales_explorer.DIMS,
        "fy_months": sales_explorer.FY_MONTHS, "has_targets": False})


@app.get("/reports/sales/data")
def report_sales_data(request: Request, region: str = "", p: str = "ytd", fy: int = 0,
                      dims: str = "", brand: str = "", rep: str = "", retailer: str = ""):
    a = _sx_args(request, region, p, fy, dims, brand, rep, retailer)
    if a is None:
        return JSONResponse({"error": "no access"}, status_code=403)
    try:
        return JSONResponse(sales_explorer.explore(a["region"], a["code"], a["fy"], a["today"],
                                                   a["dims"], a["f"], a["margin"]))
    except warehouse.WarehouseUnavailable as e:
        return JSONResponse({"error": _warehouse_error(e)}, status_code=503)


@app.get("/reports/sales/export.xlsx")
def report_sales_xlsx(request: Request, region: str = "", p: str = "ytd", fy: int = 0,
                      dims: str = "", brand: str = "", rep: str = "", retailer: str = ""):
    a = _sx_args(request, region, p, fy, dims, brand, rep, retailer)
    if a is None:
        return _sx_denied(request)
    try:
        data = sales_explorer.explore(a["region"], a["code"], a["fy"], a["today"], a["dims"],
                                      a["f"], a["margin"])
        nm = sales_explorer.names()
    except warehouse.WarehouseUnavailable as e:
        return Response(_warehouse_error(e), status_code=503)
    f = a["f"]
    desc = " · ".join(x for x in [
        a["region"], data["period"]["label"], f"{data['period']['from']} to {data['period']['to']}",
        "vs same dates LY",
        f"brand {nm['brand'].get(f['brand'], f['brand'])}" if f["brand"] is not None else "",
        f"account manager {nm['rep'].get(f['rep'], f['rep'])}" if f["rep"] is not None else "",
        f"retailer {f['retailer']}" if f["retailer"] else ""] if x)
    body = sales_explorer.to_xlsx(data, "Macgear Sales", desc)
    name = f"Macgear_Sales_{a['region']}_{data['period']['from']}_{data['period']['to']}.xlsx"
    return Response(body, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- Field Activity (Repsly) -----------------------------------------------------
# Mova reps from Repsly via the warehouse (app/field.py). Staff only: the area key
# "field" is not in VENDOR_AREAS. Rep GPS, notes and photos are internal.
def _date(v: str, default: date) -> date:
    try:
        return date.fromisoformat(v) if v else default
    except ValueError:
        return default


def _field_ctx(request: Request, page: str):
    try:
        status = field.sync_status()
        rep_list = field.reps()
    except warehouse.WarehouseUnavailable as e:
        return None, templates.TemplateResponse(request, "denied.html",
                                                {"message": _warehouse_error(e)}, status_code=503)
    return {"page": page, "sync": status, "rep_list": rep_list, "today": sales_today(),
            "banners": field.BANNERS}, None


@app.get("/reports/field", response_class=HTMLResponse)
def field_dashboard(request: Request):
    t = sales_today()
    ctx, err = _field_ctx(request, "dash")
    if err:
        return err
    wk = field.week_start(t)
    m0, _ = field.month_bounds(t)
    rows = field.display_rows()
    vis = field.visits()
    week_vis = [v for v in vis if v["visit_date"] and wk <= v["visit_date"] <= t]
    sched = [s for s in field.schedule() if wk <= s["schedule_date"] <= t]
    gaps = field.persistent_gaps(rows, t)
    ctx.update({"t": t, "wk": wk, "m0": m0, "comp": field.compliance(rows, m0, t, network=field.hn_network(field.stores())),
                "week_visits": len(week_vis), "week_stores": len({v["client_code"] for v in week_vis}),
                "week_sched": len(sched), "week_sched_done": sum(1 for s in sched if s["visited"]),
                "week_photos": sum(1 for p in field.photos() if p["photo_date"] and wk <= p["photo_date"] <= t),
                "gaps": gaps[:12], "n_gaps": len(gaps), "recent": vis[:10],
                "unmapped": len(field.unmapped_questions())})
    return templates.TemplateResponse(request, "field_dashboard.html", ctx)


@app.get("/reports/field/compliance", response_class=HTMLResponse)
def field_compliance(request: Request, month: str = "", start: str = "", end: str = "",
                     rep: str = "", banner: str = "", category: str = "", measure: str = "hn"):
    t = sales_today()
    ctx, err = _field_ctx(request, "comp")
    if err:
        return err
    m = _date(month + "-01" if len(month) == 7 else "", t)
    s, e = field.month_bounds(m)
    s, e = _date(start, s), min(_date(end, e), t)
    rows = field.display_rows()
    measure = measure if measure in ("hn", "merch") else "hn"
    comp = field.compliance(rows, s, e, rep=rep, banner=banner, category=category,
                                network=field.hn_network(field.stores()))
    gaps = field.persistent_gaps(rows, e, measure=measure)
    if rep:
        gaps = [g for g in gaps if g["rep_code"] == rep]
    ctx.update({"comp": comp, "measure": measure, "m": comp[measure], "s": s, "e": e,
                "month": m.strftime("%Y-%m"),
                "months": sorted({r["answer_date"].strftime("%Y-%m") for r in rows}, reverse=True),
                "rep": rep, "banner": banner, "category": category,
                "categories": sorted({r["category"] for r in rows if r["category"]}),
                "gaps": gaps[:40], "n_gaps": len(gaps), "unmapped": field.unmapped_questions()[:20]})
    return templates.TemplateResponse(request, "field_compliance.html", ctx)


@app.get("/reports/field/feed", response_class=HTMLResponse)
def field_feed(request: Request, day: str = "", rep: str = ""):
    t = sales_today()
    ctx, err = _field_ctx(request, "feed")
    if err:
        return err
    d = _date(day, t)
    allv = field.visits()
    vis = [v for v in allv if v["visit_date"] == d and (not rep or v["rep_code"] == rep)]
    ids = {v["visit_id"] for v in vis}
    disp = defaultdict(lambda: {"yes": 0, "n": 0, "missing": []})
    for r in field.display_rows():
        if r["visit_id"] in ids:
            x = disp[r["visit_id"]]
            x["n"] += 1
            if r["answer"] == "Yes":
                x["yes"] += 1
            else:
                x["missing"].append(r["product_name"])
    ph, nt = defaultdict(list), defaultdict(list)
    for p in field.photos():
        if p["visit_id"] in ids:
            ph[p["visit_id"]].append(p)
    for n in field.notes():
        if n["visit_id"] in ids:
            nt[n["visit_id"]].append(n)
    stores = {s["client_code"]: s for s in field.stores()}
    points = [{"lat": stores[v["client_code"]]["lat"], "lng": stores[v["client_code"]]["lng"],
               "name": v["store_name"], "rep": v["rep_name"]}
              for v in vis if v["client_code"] in stores and stores[v["client_code"]]["lat"]]
    last_active = None
    if not vis:
        prior = [v["visit_date"] for v in allv if v["visit_date"] and v["visit_date"] < d]
        last_active = max(prior) if prior else None
    ctx.update({"d": d, "rep": rep, "visits": vis, "disp": disp, "photos": ph, "notes": nt,
                "points": points, "last_active": last_active,
                "prev": d - timedelta(days=1), "next": d + timedelta(days=1)})
    return templates.TemplateResponse(request, "field_feed.html", ctx)


@app.get("/reports/field/schedule", response_class=HTMLResponse)
def field_schedule(request: Request, week: str = "", rep: str = ""):
    t = sales_today()
    ctx, err = _field_ctx(request, "sched")
    if err:
        return err
    wk = field.week_start(_date(week, t))
    days = [wk + timedelta(days=i) for i in range(7)]
    grid, done, names = defaultdict(list), defaultdict(list), {}
    for s in field.schedule():
        if wk <= s["schedule_date"] <= days[-1] and (not rep or s["rep_code"] == rep):
            grid[(s["rep_code"], s["schedule_date"])].append(s)
            names[s["rep_code"]] = s["rep_name"] or s["rep_code"]
    for v in field.visits():            # visits made, scheduled or not
        if v["visit_date"] and wk <= v["visit_date"] <= days[-1] and (not rep or v["rep_code"] == rep):
            done[(v["rep_code"], v["visit_date"])].append(v)
            names.setdefault(v["rep_code"], v["rep_name"] or v["rep_code"])
    ctx.update({"wk": wk, "days": days, "grid": grid, "done": done, "rep": rep, "t": t,
                "shown": sorted(names.items(), key=lambda x: x[1]),
                "prev": wk - timedelta(days=7), "next": wk + timedelta(days=7)})
    return templates.TemplateResponse(request, "field_schedule.html", ctx)


@app.get("/reports/field/visits", response_class=HTMLResponse)
def field_visits(request: Request, start: str = "", end: str = "", rep: str = "", banner: str = "",
                 q: str = ""):
    t = sales_today()
    ctx, err = _field_ctx(request, "visits")
    if err:
        return err
    s, e = _date(start, t - timedelta(days=13)), _date(end, t)
    ql = q.lower().strip()
    vis = [v for v in field.visits() if v["visit_date"] and s <= v["visit_date"] <= e
           and (not rep or v["rep_code"] == rep) and (not banner or v["banner"] == banner)
           and (not ql or ql in (v["store_name"] or "").lower())]
    ctx.update({"visits": vis[:1000], "n": len(vis), "s": s, "e": e, "rep": rep, "banner": banner, "q": q})
    return templates.TemplateResponse(request, "field_visits.html", ctx)


@app.get("/reports/field/visits/{visit_id}", response_class=HTMLResponse)
def field_visit(request: Request, visit_id: int):
    ctx, err = _field_ctx(request, "visits")
    if err:
        return err
    v = next((x for x in field.visits() if x["visit_id"] == visit_id), None)
    if v is None:
        return templates.TemplateResponse(request, "denied.html", {"message": "No such visit."}, status_code=404)
    forms = defaultdict(list)
    for a in field.visit_answers(visit_id):
        forms[(a["form_id"], a["form_name"])].append(a)
    ctx.update({"v": v, "forms": dict(forms),
                "photos": [p for p in field.photos() if p["visit_id"] == visit_id],
                "notes": [n for n in field.notes() if n["visit_id"] == visit_id],
                "display": [r for r in field.display_rows() if r["visit_id"] == visit_id]})
    return templates.TemplateResponse(request, "field_visit.html", ctx)


@app.get("/reports/field/activity", response_class=HTMLResponse)
def field_activity(request: Request, start: str = "", end: str = ""):
    t = sales_today()
    ctx, err = _field_ctx(request, "act")
    if err:
        return err
    m0, _ = field.month_bounds(t)
    s, e = _date(start, m0), _date(end, t)
    ctx.update({"s": s, "e": e, "rows": field.rep_activity(field.visits(), field.schedule(), s, e, t)})
    return templates.TemplateResponse(request, "field_activity.html", ctx)


@app.get("/reports/field/photos", response_class=HTMLResponse)
def field_photos(request: Request, start: str = "", end: str = "", rep: str = "", q: str = ""):
    t = sales_today()
    ctx, err = _field_ctx(request, "photos")
    if err:
        return err
    s, e = _date(start, t - timedelta(days=6)), _date(end, t)
    ql = q.lower().strip()
    ph = [p for p in field.photos() if p["photo_date"] and s <= p["photo_date"] <= e
          and (not rep or p["rep_code"] == rep) and (not ql or ql in (p["store_name"] or "").lower())]
    ctx.update({"photos": ph[:400], "n": len(ph), "s": s, "e": e, "rep": rep, "q": q})
    return templates.TemplateResponse(request, "field_photos.html", ctx)


# ---- dashboard ---------------------------------------------------------------
@app.get("/promos", response_class=HTMLResponse)
def dashboard(request: Request, brand: str = "", status: str = "", country: str = "",
              db: Session = Depends(get_db)):
    q = select(Promotion).order_by(Promotion.created_at.desc())
    if country:
        q = q.where(Promotion.country == country)
    if brand:
        q = q.where(Promotion.brand == brand)
    if status:
        q = q.where(Promotion.status == status)
    rows = [build_promo_view(p) for p in db.scalars(q).all()]

    # compact summary tiles over the currently-shown (filtered) set
    customer_claimed = sum(
        rv.pr.customer_claim.amount_claimed or 0.0
        for r in rows for rv in r.retailers
        if rv.pr.customer_claim and rv.pr.customer_claim.amount_claimed is not None)
    vr_done = "Credit Received"
    stats = {
        "count": len(rows),
        "active": sum(1 for r in rows if r.promo.status not in ("Draft", "Closed")),
        "customer_claimed": customer_claimed,
        "vendor_outstanding": sum(
            r.supplier_total_aud for r in rows
            if not (r.promo.vendor_request and r.promo.vendor_request.status == vr_done)),
        "vendor_recovered": sum(
            r.supplier_total_aud for r in rows
            if r.promo.vendor_request and r.promo.vendor_request.status == vr_done),
        "mg_absorbed": sum(r.mg_total_aud for r in rows),
    }
    brands = [b for b in db.scalars(
        select(Promotion.brand).distinct().order_by(Promotion.brand)).all() if b]
    countries = [c for c in db.scalars(
        select(Promotion.country).distinct().order_by(Promotion.country)).all() if c]
    return templates.TemplateResponse(request, "dashboard.html", {
        "rows": rows, "stats": stats, "brands": brands, "countries": countries,
        "sel_brand": brand, "sel_status": status, "sel_country": country})


# ---- help / instructions -----------------------------------------------------
@app.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    return templates.TemplateResponse(request, "help.html", {})


# ---- pricing sync (NetSuite -> products), called by n8n -----------------------
PRICING_SYNC_TOKEN = os.environ.get("PRICING_SYNC_TOKEN", "")


@app.post("/admin/sync-pricing")
async def sync_pricing_endpoint(request: Request, db: Session = Depends(get_db)):
    """Upsert product/customer pricing from a NetSuite saved-search export.

    Auth: shared secret in the X-Sync-Token header (set PRICING_SYNC_TOKEN).
    Body: {"country": "AU"|"NZ", "rows": [ {<saved-search row>}, ... ]}.
    """
    if not PRICING_SYNC_TOKEN or request.headers.get("X-Sync-Token") != PRICING_SYNC_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    country = (payload.get("country") or "").upper()
    rows = payload.get("rows")
    if country not in ("AU", "NZ"):
        return JSONResponse({"error": "country must be AU or NZ"}, status_code=400)
    if not isinstance(rows, list):
        return JSONResponse({"error": "rows must be a list"}, status_code=400)
    prune = payload.get("prune", True)
    return JSONResponse(sync_pricing(db, country, rows, prune=bool(prune)))


# ---- create promotion --------------------------------------------------------
@app.get("/promo/new", response_class=HTMLResponse)
def new_promo_form(request: Request, db: Session = Depends(get_db)):
    retailers = _retailer_picker(db, ("AU", "NZ"))
    try:
        brands = warehouse.brand_names()
    except warehouse.WarehouseUnavailable:
        brands = db.scalars(select(Promotion.brand).distinct().order_by(Promotion.brand)).all()
    return templates.TemplateResponse(request, "promo_new.html", {
        "retailers": retailers, "brands": [b for b in brands if b],
        "wh_down": retailers is None,
        "today": date.today().isoformat(), "live_fx": get_live_fx()})


# ---- promo reference data: the warehouse (app/promo_data.py) ------------------
def _retailer_picker(db, countries):
    """Retailer picker rows, each with its default rebate from the tool's own
    retailer list. None when the warehouse can't be reached."""
    rebates = db.scalars(select(Retailer)).all()
    try:
        return [{"key": e.key, "name": e.name, "country": e.country,
                 "price_level": e.price_level, "sell_through": e.st_customer_key is not None,
                 "default_rebate": promo_data.default_rebate(e, rebates)}
                for c in countries for e in promo_data.retailer_entries(c)]
    except warehouse.WarehouseUnavailable:
        return None


def _block_entry(pr):
    """The picker entry behind a promo retailer block. A block made before the
    warehouse switch is matched by name once, then remembers its ids (caller
    commits). Raises WarehouseUnavailable."""
    entry = promo_data.entry_for_block(pr)
    if entry and pr.price_level_id is None:
        pr.price_level_id, pr.st_customer_key = entry.price_level_id, entry.st_customer_key
    return entry


HAND = "Entered by hand"          # avg_6wk_source for a figure someone typed


def _fill_avg_6wk(pr, lines, keep_hand=False):
    """Set avg_6wk + its source on lines from the block's sell-through, each over
    the six weeks before its own product's start. keep_hand leaves figures typed by
    hand alone. False when the retailer sends no sell-through (lines untouched).
    Raises WarehouseUnavailable."""
    entry = _block_entry(pr)
    if entry is None or entry.st_customer_key is None:
        return False
    promo = pr.promotion
    by_start = defaultdict(list)
    for l in lines:
        if l.product_code and not (keep_hand and l.avg_6wk_source == HAND):
            by_start[line_dates(l, promo)[0]].append(l)
    for start, group in by_start.items():
        avg = promo_data.six_week_avg(entry, [l.product_code for l in group], start)
        if avg is not None:
            for l in group:
                l.avg_6wk, l.avg_6wk_source = avg.get(l.product_code), avg.source
    return True


def _own_dates(promo, start, end):
    """What a line stores for a product window: None where it equals the promo's,
    so the product keeps following the promo dates."""
    start, end = start or promo.start_date, end or promo.end_date
    return (None if start == promo.start_date else start,
            None if end == promo.end_date else end)


def _product_key(line):
    return (line.product_code or "").strip().upper() or f"#{line.id}"


def _set_product_dates(promo, keys, start, end):
    """Give every line of these products the window start..end (None = promo's),
    and re-pull the sell-through 6wk avg of lines whose start moved (hand-typed
    figures kept). Returns False if the warehouse couldn't be reached for that."""
    s, e = _own_dates(promo, start, end)
    moved = defaultdict(list)
    for pr in promo.retailers:
        for l in pr.lines:
            if _product_key(l) in keys:
                before = line_dates(l, promo)[0]
                l.start_date, l.end_date = s, e
                if line_dates(l, promo)[0] != before:
                    moved[pr].append(l)
    try:
        for pr, ls in moved.items():
            _fill_avg_6wk(pr, ls, keep_hand=True)
    except warehouse.WarehouseUnavailable:
        return False
    return True


def _new_line(pr, info, pct, start, end):
    """A line for one product on one retailer block, with the promo's defaults and
    the block's buy price. The caller fills the 6wk avg."""
    promo = pr.promotion
    basis = promo.support_basis_default or "pct_off"
    return LineItem(
        promo_retailer_id=pr.id,
        product_code=info["code"],
        description=info["description"],
        rrp_inc=info["rrp_inc"],
        retailer_buy_ex=info["prices"].get(pr.price_level_id) or 0.0,
        pct_off=(pct or 0.0) / 100.0,
        support_basis=basis,
        target_margin=promo.target_margin_default if basis == "margin" else None,
        cogs_supplier_pct=promo.cogs_supplier_pct_default if basis == "cogs" else None,
        cogs_mg_pct=promo.cogs_mg_pct_default if basis == "cogs" else None,
        start_date=start,
        end_date=end,
    )


def _validated_split(form):
    """Return (supplier, mg, retailer) with retailer as the remainder, or None if invalid."""
    s = _parse_float(form.get("ratio_supplier")) or 0.0
    m = _parse_float(form.get("ratio_mg")) or 0.0
    r = round(1.0 - s - m, 6)
    if s < 0 or m < 0 or r < -1e-9:
        return None
    return s, m, r


@app.post("/promo/new")
async def create_promo(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    start = _parse_date(form.get("start_date")) or date.today()
    end = _parse_date(form.get("end_date")) or date.today()
    if end < start:
        return RedirectResponse("/promo/new?err=dates", status_code=303)
    split = _validated_split(form)
    if split is None:
        return RedirectResponse("/promo/new?err=split", status_code=303)
    supplier, mg, retailer = split
    tgt_default = _parse_float(form.get("target_margin_default"))
    csp_default = _parse_float(form.get("cogs_supplier_pct_default"))
    cmp_default = _parse_float(form.get("cogs_mg_pct_default"))
    country = (form.get("country") or "AU").upper()
    promo = Promotion(
        claim_number=form.get("claim_number", "").strip(),
        name=form.get("name", "").strip(),
        brand=form.get("brand", "").strip(),
        country=country,
        start_date=start,
        end_date=end,
        aud_usd_rate=_parse_float(form.get("aud_usd_rate")) or 0.65,
        ratio_retailer=retailer,
        ratio_mg=mg,
        ratio_supplier=supplier,
        growth_default=_parse_float(form.get("growth_default")) or 0.0,
        support_basis_default=form.get("support_basis_default") or "pct_off",
        target_margin_default=(tgt_default / 100.0) if tgt_default is not None else None,
        cogs_supplier_pct_default=(csp_default / 100.0) if csp_default is not None else None,
        cogs_mg_pct_default=(cmp_default / 100.0) if cmp_default is not None else None,
        notes=form.get("notes", "").strip(),
    )
    keys = form.getlist("retailer")
    try:
        entries = [(k, promo_data.entry_by_key(country, k)) for k in keys]
    except warehouse.WarehouseUnavailable:
        return RedirectResponse("/promo/new?err=warehouse", status_code=303)
    db.add(promo)
    db.flush()
    for key, entry in entries:
        if entry is None:
            continue
        rebate = _parse_float(form.get(f"rebate_{key}")) or 0.0
        db.add(PromoRetailer(promo_id=promo.id, retailer_name=entry.name, rebate=rebate,
                             price_level_id=entry.price_level_id,
                             st_customer_key=entry.st_customer_key))
    db.add(VendorRequest(promo_id=promo.id))
    db.commit()
    return RedirectResponse(f"/promo/{promo.id}", status_code=303)


# ---- promotion detail --------------------------------------------------------
@app.get("/promo/{promo_id}", response_class=HTMLResponse)
def promo_detail(promo_id: int, request: Request, view: str = "internal",
                 db: Session = Depends(get_db)):
    promo = db.get(Promotion, promo_id)
    if not promo:
        return RedirectResponse("/promos", status_code=303)
    pv = build_promo_view(promo)
    retailers = _retailer_picker(db, (promo.country,))
    if view not in ("internal", "sales", "vendor"):
        view = "internal"
    return templates.TemplateResponse(request, "promo_detail.html", {
        "pv": pv, "promo": promo, "view": view,
        "all_retailers": retailers})


# ---- products (the promo grid) -----------------------------------------------
@app.post("/promo/{promo_id}/products")
async def add_products(promo_id: int, request: Request, db: Session = Depends(get_db)):
    """Add pasted products to the ticked retailer blocks. Each line gets that
    retailer's buy price and 6wk avg; a product already on a block keeps its line
    (a pasted % off updates it). Codes not in NetSuite are skipped and listed."""
    form = await request.form()
    promo = db.get(Promotion, promo_id)
    if not promo:
        return RedirectResponse("/promos", status_code=303)
    rows, bad = promo_data.parse_paste(form.get("codes", ""))
    ticked = set(form.getlist("pr"))
    blocks = [pr for pr in promo.retailers if str(pr.id) in ticked]
    start, end = _parse_date(form.get("start_date")), _parse_date(form.get("end_date"))
    if (end or promo.end_date) < (start or promo.start_date):
        return RedirectResponse(f"/promo/{promo_id}?err=dates", status_code=303)
    if not rows or not blocks:
        return RedirectResponse(f"/promo/{promo_id}?err=paste", status_code=303)
    try:
        for pr in blocks:
            _block_entry(pr)
        info = promo_data.items([c for c, _ in rows], promo.country,
                                [pr.price_level_id for pr in blocks])
    except warehouse.WarehouseUnavailable:
        return RedirectResponse(f"/promo/{promo_id}?err=warehouse", status_code=303)

    # A product already in the promo keeps its window on the new lines.
    known = {}
    for pr in promo.retailers:
        for l in pr.lines:
            known.setdefault(_product_key(l), (l.start_date, l.end_date))
    found = [(info[c.upper()], pct) for c, pct in rows if c.upper() in info]
    missing = [c for c, _ in rows if c.upper() not in info]
    added = updated = 0
    wh_err = False
    for pr in blocks:
        have = {_product_key(l): l for l in pr.lines}
        new = []
        for it, pct in found:
            key = it["code"].upper()
            if key in have:
                if pct is not None:
                    have[key].pct_off = pct / 100.0
                    updated += 1
                continue
            s, e = known.get(key, (None, None))
            line = _new_line(pr, it, pct, s, e)
            new.append(line)
            have[key] = line
        db.add_all(new)
        added += len(new)
        if new and not wh_err:
            try:
                _fill_avg_6wk(pr, new)
            except warehouse.WarehouseUnavailable:
                wh_err = True
    db.flush()
    if (start or end) and found:
        wh_err = not _set_product_dates(promo, {it["code"].upper() for it, _ in found},
                                        start, end) or wh_err
    db.commit()
    q = {"added": added, "updated": updated}
    if missing:
        q["missing"] = ",".join(missing[:20]) + ("…" if len(missing) > 20 else "")
    if bad:
        q["bad"] = len(bad)
    if wh_err:
        q["err"] = "warehouse"
    return RedirectResponse(f"/promo/{promo_id}?{urlencode(q)}", status_code=303)


# Grid cells typed in place: field -> how the typed value is stored.
_GRID_FIELDS = ("retailer_buy_ex", "pct_off", "avg_6wk", "actual_sales")


@app.post("/line/{line_id}/set")
async def set_line_field(line_id: int, request: Request, db: Session = Depends(get_db)):
    """Save one grid cell: {"field": ..., "value": ...}. % off is a percentage."""
    data = await request.json()
    line = db.get(LineItem, line_id)
    field = data.get("field")
    if not line or field not in _GRID_FIELDS:
        return JSONResponse({"error": "unknown line or field"}, status_code=400)
    raw = data.get("value")
    v = _parse_float(raw)
    if (raw not in (None, "") and v is None) or (v is not None and v < 0):
        return JSONResponse({"error": "enter a number, 0 or more"}, status_code=400)
    if field == "pct_off":
        if v is not None and v > 100:
            return JSONResponse({"error": "% off can't be over 100"}, status_code=400)
        line.pct_off = (v or 0.0) / 100.0
    elif field == "retailer_buy_ex":
        line.retailer_buy_ex = v or 0.0
    elif field == "avg_6wk":
        if v != line.avg_6wk:
            line.avg_6wk_source = HAND if v is not None else None
        line.avg_6wk = v
    else:
        line.actual_sales = v
    db.commit()
    return JSONResponse({"ok": True})


@app.post("/promo/{promo_id}/product_dates")
async def set_product_dates(promo_id: int, request: Request, db: Session = Depends(get_db)):
    """Set one product's promo window across every retailer: {"key", "start", "end"};
    blank = the promotion's date."""
    data = await request.json()
    promo = db.get(Promotion, promo_id)
    if not promo:
        return JSONResponse({"error": "no such promotion"}, status_code=404)
    start, end = _parse_date(data.get("start")), _parse_date(data.get("end"))
    if (end or promo.end_date) < (start or promo.start_date):
        return JSONResponse({"error": "the end date can't be before the start date"},
                            status_code=400)
    ok = _set_product_dates(promo, {(data.get("key") or "").upper()}, start, end)
    db.commit()
    return JSONResponse({"ok": True, **({} if ok else {
        "warning": "dates saved, but the 6wk avg couldn't be re-pulled (warehouse unreachable)"})})


@app.post("/promo/{promo_id}/product_delete")
async def delete_product(promo_id: int, request: Request, db: Session = Depends(get_db)):
    """Take a product off the promo — its line on every retailer."""
    form = await request.form()
    promo = db.get(Promotion, promo_id)
    if not promo:
        return RedirectResponse("/promos", status_code=303)
    key = (form.get("key") or "").upper()
    for pr in promo.retailers:
        for l in list(pr.lines):
            if _product_key(l) == key:
                db.delete(l)
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


@app.post("/retailer/{pr_id}/refresh_avg")
def refresh_avg(pr_id: int, db: Session = Depends(get_db)):
    """Re-pull every line's 6wk avg in this block from sell-through (overwrites)."""
    pr = db.get(PromoRetailer, pr_id)
    if not pr:
        return RedirectResponse("/promos", status_code=303)
    try:
        ok = _fill_avg_6wk(pr, pr.lines)
    except warehouse.WarehouseUnavailable:
        return RedirectResponse(f"/promo/{pr.promo_id}?err=warehouse", status_code=303)
    db.commit()
    return RedirectResponse(f"/promo/{pr.promo_id}" + ("" if ok else "?err=no_st")
                            + f"#pr{pr.id}", status_code=303)


@app.post("/line/{line_id}/edit")
async def edit_line(line_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    line = db.get(LineItem, line_id)
    if not line:
        return RedirectResponse("/promos", status_code=303)
    line.product_code = form.get("product_code", "").strip()
    line.description = form.get("description", "").strip()
    line.rrp_inc = _parse_float(form.get("rrp_inc"))
    line.retailer_buy_ex = _parse_float(form.get("retailer_buy_ex")) or 0.0
    promo_id = line.promo_retailer.promo_id
    # funding split: validate first so a bad split rejects the whole save
    if form.get("override_split"):
        s = _parse_float(form.get("ratio_supplier")) or 0.0
        m = _parse_float(form.get("ratio_mg")) or 0.0
        r = round(1.0 - s - m, 6)
        if s < 0 or m < 0 or r < -1e-9:
            return RedirectResponse(f"/promo/{promo_id}?err=split", status_code=303)
        line.ratio_supplier, line.ratio_mg, line.ratio_retailer = s, m, r
    else:
        line.ratio_supplier = line.ratio_mg = line.ratio_retailer = None
    line.pct_off = (_parse_float(form.get("pct_off")) or 0.0) / 100.0
    avg_6wk = _parse_float(form.get("avg_6wk"))
    if avg_6wk != line.avg_6wk:
        line.avg_6wk_source = "Entered by hand" if avg_6wk is not None else None
    line.avg_6wk = avg_6wk
    line.actual_sales = _parse_float(form.get("actual_sales"))
    line.growth = _parse_float(form.get("growth"))
    line.support_basis = form.get("support_basis") or "pct_off"
    tgt = _parse_float(form.get("target_margin"))
    line.target_margin = (tgt / 100.0) if tgt is not None else None
    csp = _parse_float(form.get("cogs_supplier_pct"))
    cmp = _parse_float(form.get("cogs_mg_pct"))
    line.cogs_supplier_pct = (csp / 100.0) if csp is not None else None
    line.cogs_mg_pct = (cmp / 100.0) if cmp is not None else None
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


@app.post("/line/{line_id}/delete")
def delete_line(line_id: int, db: Session = Depends(get_db)):
    line = db.get(LineItem, line_id)
    promo_id = line.promo_retailer.promo_id
    db.delete(line)
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


# ---- add retailer block to existing promo -----------------------------------
@app.post("/promo/{promo_id}/retailer")
async def add_retailer(promo_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    key = form.get("retailer_key", "").strip()
    promo = db.get(Promotion, promo_id)
    if key and promo:
        try:
            entry = promo_data.entry_by_key(promo.country, key)
        except warehouse.WarehouseUnavailable:
            return RedirectResponse(f"/promo/{promo_id}?err=warehouse", status_code=303)
        if entry:
            rebate = _parse_float(form.get("rebate"))
            if rebate is None:
                rebate = promo_data.default_rebate(entry, db.scalars(select(Retailer)).all())
            db.add(PromoRetailer(promo_id=promo_id, retailer_name=entry.name, rebate=rebate,
                                 price_level_id=entry.price_level_id,
                                 st_customer_key=entry.st_customer_key))
            db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


def _remove_attachment_files(promo_retailers):
    """Best-effort delete of attachment files on disk for the given retailer blocks."""
    for pr in promo_retailers:
        for att in pr.attachments:
            path = os.path.join(UPLOAD_DIR, att.stored_name)
            if os.path.exists(path):
                os.remove(path)


@app.post("/retailer/{pr_id}/delete")
def delete_retailer(pr_id: int, db: Session = Depends(get_db)):
    pr = db.get(PromoRetailer, pr_id)
    promo_id = pr.promo_id
    _remove_attachment_files([pr])
    db.delete(pr)
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


# ---- customer claim (inbound) -----------------------------------------------
@app.post("/retailer/{pr_id}/claim")
async def update_claim(pr_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    pr = db.get(PromoRetailer, pr_id)
    cc = pr.customer_claim or CustomerClaim(promo_retailer_id=pr_id)
    cc.amount_claimed = _parse_float(form.get("amount_claimed"))
    cc.claim_date = _parse_date(form.get("claim_date"))
    cc.status = form.get("status", "Awaiting Claim")
    cc.notes = form.get("notes", "").strip()
    if cc.id is None:
        db.add(cc)
    db.commit()
    return RedirectResponse(f"/promo/{pr.promo_id}", status_code=303)


# ---- attachments (files against a customer block) ---------------------------
@app.post("/retailer/{pr_id}/attach")
async def upload_attachment(pr_id: int, file: UploadFile = File(...),
                            db: Session = Depends(get_db)):
    pr = db.get(PromoRetailer, pr_id)
    if pr and file and file.filename:
        ext = os.path.splitext(file.filename)[1]
        stored = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(UPLOAD_DIR, stored)
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out)
        db.add(Attachment(
            promo_retailer_id=pr_id,
            filename=os.path.basename(file.filename),
            stored_name=stored,
            content_type=file.content_type or "",
            size=os.path.getsize(dest),
        ))
        db.commit()
    return RedirectResponse(f"/promo/{pr.promo_id}", status_code=303)


@app.get("/attachment/{att_id}")
def download_attachment(att_id: int, db: Session = Depends(get_db)):
    att = db.get(Attachment, att_id)
    if not att:
        return RedirectResponse("/promos", status_code=303)
    path = os.path.join(UPLOAD_DIR, att.stored_name)
    if not os.path.exists(path):
        return RedirectResponse(f"/promo/{att.promo_retailer.promo_id}", status_code=303)
    return FileResponse(path, filename=att.filename,
                        media_type=att.content_type or "application/octet-stream")


@app.post("/attachment/{att_id}/delete")
def delete_attachment(att_id: int, db: Session = Depends(get_db)):
    att = db.get(Attachment, att_id)
    if not att:
        return RedirectResponse("/promos", status_code=303)
    promo_id = att.promo_retailer.promo_id
    path = os.path.join(UPLOAD_DIR, att.stored_name)
    if os.path.exists(path):
        os.remove(path)
    db.delete(att)
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


# ---- vendor request (outbound) ----------------------------------------------
@app.post("/promo/{promo_id}/vendor")
async def update_vendor(promo_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    promo = db.get(Promotion, promo_id)
    vr = promo.vendor_request or VendorRequest(promo_id=promo_id)
    pv = build_promo_view(promo)
    vr.amount_aud = _parse_float(form.get("amount_aud"))
    if vr.amount_aud is None:
        vr.amount_aud = pv.supplier_total_aud
    vr.amount_usd = _parse_float(form.get("amount_usd")) or (vr.amount_aud * promo.aud_usd_rate)
    vr.status = form.get("status", "Not Sent")
    vr.claim_date = _parse_date(form.get("claim_date"))
    vr.notes = form.get("notes", "").strip()
    if vr.id is None:
        db.add(vr)
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


# ---- promo workflow status (setup / approval track) -------------------------
@app.post("/promo/{promo_id}/status")
async def update_promo_status(promo_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    promo = db.get(Promotion, promo_id)
    if promo:
        promo.status = form.get("status", promo.status)
        db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


@app.post("/promo/{promo_id}/details")
async def update_details(promo_id: int, request: Request, db: Session = Depends(get_db)):
    """Edit promo header fields after creation (name, claim #, brand, date range)."""
    form = await request.form()
    promo = db.get(Promotion, promo_id)
    if not promo:
        return RedirectResponse("/promos", status_code=303)
    promo.name = form.get("name", "").strip()
    promo.claim_number = form.get("claim_number", "").strip()
    promo.brand = form.get("brand", "").strip()
    sd = _parse_date(form.get("start_date")) or promo.start_date
    ed = _parse_date(form.get("end_date")) or promo.end_date
    if ed < sd:
        return RedirectResponse(f"/promo/{promo_id}?err=dates", status_code=303)
    start_moved = sd != promo.start_date
    promo.start_date = sd
    promo.end_date = ed
    if start_moved:                  # products on the promo's dates: new 6wk window
        try:
            for pr in promo.retailers:
                _fill_avg_6wk(pr, [l for l in pr.lines if l.start_date is None], keep_hand=True)
        except warehouse.WarehouseUnavailable:
            pass                     # keep the old figures; "refresh 6wk avg" re-pulls later
    rate = _parse_float(form.get("aud_usd_rate"))
    if rate is not None:
        promo.aud_usd_rate = rate
    if form.get("support_basis_default"):
        promo.support_basis_default = form.get("support_basis_default")
    tgt_default = _parse_float(form.get("target_margin_default"))
    promo.target_margin_default = (tgt_default / 100.0) if tgt_default is not None else None
    csp_default = _parse_float(form.get("cogs_supplier_pct_default"))
    promo.cogs_supplier_pct_default = (csp_default / 100.0) if csp_default is not None else None
    cmp_default = _parse_float(form.get("cogs_mg_pct_default"))
    promo.cogs_mg_pct_default = (cmp_default / 100.0) if cmp_default is not None else None
    db.commit()
    return RedirectResponse(f"/promo/{promo_id}", status_code=303)


@app.post("/promo/{promo_id}/delete")
def delete_promo(promo_id: int, db: Session = Depends(get_db)):
    promo = db.get(Promotion, promo_id)
    if promo:
        _remove_attachment_files(promo.retailers)
        db.delete(promo)
        db.commit()
    return RedirectResponse("/promos", status_code=303)


# ---- excel export ------------------------------------------------------------
@app.get("/promo/{promo_id}/export/{view}")
def export_promo(promo_id: int, view: str, db: Session = Depends(get_db)):
    promo = db.get(Promotion, promo_id)
    if not promo:
        return RedirectResponse("/promos", status_code=303)
    pv = build_promo_view(promo)
    if view not in ("internal", "sales", "vendor"):
        view = "internal"
    buf = build_workbook(pv, view)
    fname = f"{(promo.name or 'promo').replace(' ', '_')}_{view}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ==============================================================================
# Vendor Bill Importer  —  Amazon Advertising PDFs -> NetSuite import CSV
# ==============================================================================
# Nothing here touches the promo tables. The flow is upload -> review -> download,
# with the download logging each invoice number so the same bill can't quietly be
# imported twice (NetSuite keys on External ID and would reject it anyway, but a
# warning here is cheaper than a failed import job).

@app.get("/vendor-bills", response_class=HTMLResponse)
def vendor_bills(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "bills.html", {
        "run": None, "payload": "", "history": bill_service.history(db)})


@app.post("/vendor-bills", response_class=HTMLResponse)
async def vendor_bills_upload(request: Request, files: list[UploadFile] = File(default=[]),
                              db: Session = Depends(get_db)):
    uploads = [(f.filename, await f.read()) for f in files if f and f.filename]

    if not uploads:
        return templates.TemplateResponse(request, "bills.html", {
            "run": None, "payload": "", "history": bill_service.history(db),
            "error": "Choose at least one PDF or a .zip of PDFs."})

    run = bill_service.analyse(uploads, db)
    payload = ""
    if run.rows:
        payload = bill_service.payload_for(run, bill_export.csv_text(run.rows))
    return templates.TemplateResponse(request, "bills.html", {
        "run": run, "payload": payload, "history": bill_service.history(db)})


@app.post("/vendor-bills/download")
async def vendor_bills_download(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        payload = bill_service.read_payload(form.get("payload", ""))
    except Exception:
        return RedirectResponse("/vendor-bills", status_code=303)

    bill_service.record_export(payload, db)
    numbers = [i["number"] for i in payload.get("invoices", [])]
    fname = bill_export.filename_for(numbers)
    return Response(
        content=payload["csv"],
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/vendor-bills/{invoice_number}/imported")
def vendor_bill_mark_imported(invoice_number: str, db: Session = Depends(get_db)):
    """Tick an invoice off once its CSV has actually gone into NetSuite."""
    row = db.scalar(select(ProcessedInvoice)
                    .where(ProcessedInvoice.invoice_number == invoice_number))
    if row:
        row.imported = not row.imported
        db.commit()
    return RedirectResponse("/vendor-bills", status_code=303)


# ==============================================================================
# User management  —  /admin/users  (admin only; enforced in the middleware)
# ==============================================================================

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, db: Session = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.role, User.email)).all()
    rows = [{"u": u,
             "areas": [perms.AREA_LABELS[t] for t in perms.effective_access(u)],
             "caps": sorted(perms.effective_caps(u)),
             "brands": perms.effective_brands(u),
             "regions": perms.effective_regions(u),
             "status": user_status(u)} for u in users]
    return templates.TemplateResponse(request, "admin_users.html", {"rows": rows})


def _brand_choices(u: User | None) -> tuple[list[str], str]:
    """Every NetSuite brand from the warehouse, plus any already ticked on the
    account (so a brand renamed in NetSuite can still be unticked)."""
    stored = (perms._json_list(u.allowed_brands) if u else None) or []
    try:
        names, warn = warehouse.brand_names(), ""
    except warehouse.WarehouseUnavailable as e:
        names, warn = [], f"Brand list unavailable — the warehouse could not be reached ({e})."
    return sorted(set(names) | set(stored)), warn


def _user_form_ctx(u: User | None, notice: str = "", link: str = "",
                   error: str = "", emailed: bool | None = None) -> dict:
    brand_choices, brand_warn = _brand_choices(u)
    return {"u": u,
            "roles": perms.ROLES, "role_labels": perms.ROLE_LABELS,
            "report_keys": perms.REPORT_KEYS, "tool_keys": perms.TOOL_KEYS,
            "area_labels": perms.AREA_LABELS, "vendor_areas": perms.VENDOR_AREAS,
            "cap_keys": perms.CAP_KEYS, "cap_labels": perms.CAP_LABELS, "vendor_caps": perms.VENDOR_CAPS,
            "sel_caps": sorted(perms.effective_caps(u)) if u else [],
            "selected": perms.effective_access(u) if u else [],
            "brand_choices": brand_choices, "brand_warn": brand_warn,
            "sel_brands": (perms._json_list(u.allowed_brands) if u else None) or [],
            "regions": perms.REGIONS,
            "sel_regions": perms.effective_regions(u) if u else list(perms.REGIONS),
            "notice": notice, "set_link": link, "error": error, "emailed": emailed,
            "email_on": bool(notify.WEBHOOK_URL),
            "invite_ttl": notify.in_words(INVITE_TOKEN_TTL_MIN),
            "reset_ttl": notify.in_words(RESET_TOKEN_TTL_MIN)}


@app.get("/admin/users/new", response_class=HTMLResponse)
def admin_user_new(request: Request):
    return templates.TemplateResponse(request, "admin_user_form.html", _user_form_ctx(None))


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
def admin_user_edit(user_id: int, request: Request, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u:
        return RedirectResponse("/admin/users", status_code=303)
    return templates.TemplateResponse(request, "admin_user_form.html", _user_form_ctx(u))


# Order matters: decorators apply bottom-up, so the literal /new must be the
# LOWER one or "{user_id}" is registered first and tries to parse "new" as an int.
@app.post("/admin/users/{user_id}", response_class=HTMLResponse)
@app.post("/admin/users/new", response_class=HTMLResponse)
async def admin_user_save(request: Request, user_id: int | None = None,
                          db: Session = Depends(get_db)):
    form = await request.form()
    email = (form.get("email", "") or "").strip().lower()
    name = (form.get("name", "") or "").strip()
    role = form.get("role", "user")
    if role not in perms.ROLES:
        role = "user"
    allowed = perms.normalize_allowed(role, form.getlist("tools"))
    brands = perms.normalize_brands(form.getlist("brands"))
    regions = perms.normalize_regions(form.getlist("regions"))
    company = (form.get("company", "") or "").strip()
    active = form.get("active") == "on"

    u = db.get(User, user_id) if user_id else None

    if u is None:
        if not email:
            return templates.TemplateResponse(
                request, "admin_user_form.html",
                _user_form_ctx(None, error="An email address is required."),
                status_code=400)
        if db.scalar(select(User).where(User.email == email)):
            return templates.TemplateResponse(
                request, "admin_user_form.html",
                _user_form_ctx(None, error=f"{email} already has an account."),
                status_code=400)
        # No password is set here. The user chooses their own via the link below,
        # so nobody — including whoever creates the account — ever knows it.
        u = User(email=email, password_hash="")
        db.add(u)
        new_account = True
    else:
        if email and email != u.email:
            clash = db.scalar(select(User).where(User.email == email, User.id != u.id))
            if clash:
                return templates.TemplateResponse(
                    request, "admin_user_form.html",
                    _user_form_ctx(u, error=f"{email} already has an account."),
                    status_code=400)
            u.email = email
        new_account = False

    # Guard against removing the last way in: the final active admin must stay
    # an active admin, or the next deploy with APP_PASSWORD blank locks everyone out.
    if not new_account and u.role == "admin" and (role != "admin" or not active):
        others = db.scalar(select(func.count()).select_from(User).where(
            User.role == "admin", User.active.is_(True), User.id != u.id))
        if not others:
            return templates.TemplateResponse(
                request, "admin_user_form.html",
                _user_form_ctx(u, error="This is the only active admin — promote "
                                        "someone else before changing this account."),
                status_code=400)

    u.name = name
    u.role = role
    u.allowed_tools = allowed
    u.allowed_brands = brands
    u.allowed_regions = regions
    u.company = company if role == "vendor" else ""
    u.active = active
    db.commit()

    if new_account:
        # Creating emails nothing: check the access you've just set, then send the
        # invite from the "Sign-in" box (with a note, if you like). Same as 3PL Portal.
        return templates.TemplateResponse(request, "admin_user_form.html", _user_form_ctx(
            u, notice="Account created. Check their access, then send the invite below."))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/send-link", response_class=HTMLResponse)
@app.post("/admin/users/{user_id}/set-password-link", response_class=HTMLResponse)
async def admin_user_send_link(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Email an invite (no password yet) or a password reset, and show the link too."""
    u = db.get(User, user_id)
    if not u:
        return RedirectResponse("/admin/users", status_code=303)
    if not u.active:
        return templates.TemplateResponse(request, "admin_user_form.html", _user_form_ctx(
            u, error="This account is disabled — enable it before sending a link."), status_code=400)
    form = await request.form()
    note = (form.get("note", "") or "").strip()[:1000]
    send = form.get("send", "email") == "email"
    me = current_user(request)
    purpose = notify.RESET if u.password_hash else notify.INVITE
    link, emailed = _issue_link(db, u, request, purpose=purpose, note=note, send=send,
                                invited_by=(getattr(me, "email", "") if me is not None
                                            and getattr(me, "id", None) else ""))
    what = "invite" if purpose == notify.INVITE else "password reset"
    if not send:
        msg = f"New {what} link below — copy it to them yourself. Any earlier link is now dead."
    elif emailed:
        msg = (f"{what.capitalize()} emailed to {u.email}. The link is below too, in case it's slow "
               "to arrive (first emails can take a few minutes, or land in Junk).")
    elif notify.WEBHOOK_URL:
        msg = f"The email couldn't be handed to the mail service — copy the {what} link below to them."
    else:
        msg = f"Email isn't set up on this server — copy the {what} link below to them."
    return templates.TemplateResponse(request, "admin_user_form.html",
                                      _user_form_ctx(u, notice=msg, link=link, emailed=emailed))


@app.post("/admin/users/{user_id}/delete")
def admin_user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u:
        others = db.scalar(select(func.count()).select_from(User).where(
            User.role == "admin", User.active.is_(True), User.id != u.id))
        if u.role == "admin" and not others:
            return templates.TemplateResponse(
                request, "admin_user_form.html",
                _user_form_ctx(u, error="This is the only active admin — it cannot be deleted."),
                status_code=400)
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)
