"""FastAPI app: vendor credit claims manager."""
import hashlib
import hmac
import json
import os
import shutil
import time
import urllib.request
import uuid
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Request, Response, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import (Base, UPLOAD_DIR, engine, ensure_schema, get_db,
                 migrate_statuses, rebuild_reference_tables)
from .export import build_workbook
from .models import (Attachment, CustomerClaim, LineItem, ProcessedInvoice,
                     Product, PromoRetailer, Promotion, Retailer, VendorRequest)
from .service import build_promo_view
from .pricing_sync import sync_pricing
from . import bill_export, bill_service, workflow

HERE = os.path.dirname(os.path.abspath(__file__))
rebuild_reference_tables()   # drop legacy products/retailers so they rebuild country-scoped
Base.metadata.create_all(engine)
ensure_schema()
migrate_statuses()

app = FastAPI(title="Vendor Credit Claims")
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
# slug for pill css classes, and the workflow taxonomy, available in all templates
templates.env.filters["slug"] = lambda s: (s or "").lower().replace(" ", "")
templates.env.globals.update(workflow.context())
# cache-busting version for static assets (changes whenever style.css is updated)
try:
    asset_ver = str(int(os.path.getmtime(os.path.join(HERE, "static", "style.css"))))
except OSError:
    asset_ver = "1"
templates.env.globals["asset_ver"] = asset_ver


# ---- authentication (shared username/password, signed-cookie session) --------
# Auth runs inside the app so we get a styled /login page instead of the browser's
# basic-auth popup. Configure via env on the droplet:
#   APP_USERNAME  shared username testers type (default "macgear")
#   APP_PASSWORD  shared password — if blank, auth is DISABLED (handy for local dev)
#   APP_SECRET    random string used to sign the session cookie (set a long one!)
APP_USERNAME = os.environ.get("APP_USERNAME", "macgear")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_SECRET = os.environ.get("APP_SECRET", "") or "dev-insecure-secret-change-me"
COOKIE_NAME = "claims_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30          # 30 days
# paths reachable without a login (static assets, the login page itself, and the
# token-authenticated pricing sync endpoint that n8n posts to)
_AUTH_EXEMPT_PREFIXES = ("/static", "/login", "/logout", "/admin/sync-pricing")
templates.env.globals["auth_enabled"] = bool(APP_PASSWORD)


def _make_token() -> str:
    sig = hmac.new(APP_SECRET.encode(), b"authenticated", hashlib.sha256).hexdigest()
    return f"authenticated.{sig}"


def _valid_token(tok: str) -> bool:
    if not tok or "." not in tok:
        return False
    msg, _, sig = tok.rpartition(".")
    expected = hmac.new(APP_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not APP_PASSWORD:                     # auth disabled (no password configured)
        return await call_next(request)
    path = request.url.path
    if any(path == p or path.startswith(p + "/") or path.startswith(p)
           for p in _AUTH_EXEMPT_PREFIXES):
        return await call_next(request)
    if not _valid_token(request.cookies.get(COOKIE_NAME, "")):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if not APP_PASSWORD or _valid_token(request.cookies.get(COOKIE_NAME, "")):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    user = form.get("username", "")
    pw = form.get("password", "")
    ok = (hmac.compare_digest(user, APP_USERNAME)
          and hmac.compare_digest(pw, APP_PASSWORD))
    if not ok:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Incorrect username or password."},
            status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE_NAME, _make_token(), max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


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


# Retailer names (from the validation list) don't always match the price-column labels
# in the master (esp. NZ: "Noel Leeming" vs "NZ - Noel Leeming", "JB HIFI" vs "JB HIfi",
# "PB Tech" vs "NZ - PB Technologies"). Match tolerantly so buy-price autofill works.
_CHANNEL_ALIASES = {"pbtech": "pbtechnologies", "costcowholesale": "costco"}


def _norm_channel(s):
    s = (s or "").lower().strip()
    for p in ("nz au - ", "nz - ", "au - "):
        if s.startswith(p):
            s = s[len(p):]
            break
    return "".join(c for c in s if c.isalnum())


def _channel_price(channel_prices, retailer):
    """Look up a retailer's buy price in a product's channel_prices, tolerating
    naming differences between the retailer list and the price-column labels."""
    if not channel_prices or not retailer:
        return None
    if retailer in channel_prices:               # exact match (fast path)
        return channel_prices[retailer]
    target = _norm_channel(retailer)
    target = _CHANNEL_ALIASES.get(target, target)
    for key, val in channel_prices.items():
        if _norm_channel(key) == target:
            return val
    return None


def _parse_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _parse_date(v):
    return datetime.strptime(v, "%Y-%m-%d").date() if v else None


# ---- landing page ------------------------------------------------------------
# The app hosts more than promotions now, so "/" is a tool picker and each tool
# owns a path below it. Add a tile here when a tool is added.
TOOLS = [
    {"href": "/promos", "title": "Vendor Promos",
     "blurb": "Promotional credit claims — customer claims in, vendor credits out."},
    {"href": "/vendor-bills", "title": "PDF Bill Upload",
     "blurb": "Amazon Advertising invoices → NetSuite vendor bill import CSV."},
]


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {"tools": TOOLS})


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
    retailers = db.scalars(
        select(Retailer).order_by(Retailer.country, Retailer.name)).all()
    brands = db.scalars(select(Product.brand).distinct().order_by(Product.brand)).all()
    return templates.TemplateResponse(request, "promo_new.html", {
        "retailers": retailers, "brands": [b for b in brands if b],
        "today": date.today().isoformat(), "live_fx": get_live_fx()})


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
    db.add(promo)
    db.flush()
    for name in form.getlist("retailer"):
        rebate = _parse_float(form.get(f"rebate_{name}")) or 0.0
        db.add(PromoRetailer(promo_id=promo.id, retailer_name=name, rebate=rebate))
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
    retailers = db.scalars(select(Retailer).where(Retailer.country == promo.country)
                           .order_by(Retailer.name)).all()
    if view not in ("internal", "sales", "vendor"):
        view = "internal"
    return templates.TemplateResponse(request, "promo_detail.html", {
        "pv": pv, "promo": promo, "view": view,
        "all_retailers": retailers})


# ---- product lookup (autofill) ----------------------------------------------
@app.get("/api/product/{code}")
def product_lookup(code: str, retailer: str = "", country: str = "AU",
                   db: Session = Depends(get_db)):
    p = db.scalar(select(Product).where(Product.code == code,
                                        Product.country == country.upper()))
    if not p:
        return JSONResponse({"found": False})
    buy = _channel_price(p.channel_prices, retailer) if retailer else None
    return JSONResponse({"found": True, "code": p.code, "description": p.description,
                         "brand": p.brand, "rrp_inc": p.rrp_inc, "retailer_buy_ex": buy})


# ---- line items --------------------------------------------------------------
@app.post("/retailer/{pr_id}/line")
async def add_line(pr_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    pr = db.get(PromoRetailer, pr_id)
    code = form.get("product_code", "").strip()
    prod = db.scalar(select(Product).where(Product.code == code)) if code else None
    # support basis: form value, else the promo's default
    promo = pr.promotion
    basis = form.get("support_basis") or promo.support_basis_default or "pct_off"
    tgt = _parse_float(form.get("target_margin"))
    target_margin = (tgt / 100.0) if tgt is not None else (
        promo.target_margin_default if basis == "margin" else None)
    csp = _parse_float(form.get("cogs_supplier_pct"))
    cmp = _parse_float(form.get("cogs_mg_pct"))
    cogs_supplier_pct = (csp / 100.0) if csp is not None else (
        promo.cogs_supplier_pct_default if basis == "cogs" else None)
    cogs_mg_pct = (cmp / 100.0) if cmp is not None else (
        promo.cogs_mg_pct_default if basis == "cogs" else None)
    line = LineItem(
        promo_retailer_id=pr_id,
        product_code=code,
        description=form.get("description", "").strip() or (prod.description if prod else ""),
        rrp_inc=_parse_float(form.get("rrp_inc")) or (prod.rrp_inc if prod else None),
        retailer_buy_ex=_parse_float(form.get("retailer_buy_ex")) or 0.0,
        pct_off=(_parse_float(form.get("pct_off")) or 0.0) / 100.0,
        avg_6wk=_parse_float(form.get("avg_6wk")),
        actual_sales=_parse_float(form.get("actual_sales")),
        support_basis=basis,
        target_margin=target_margin,
        cogs_supplier_pct=cogs_supplier_pct,
        cogs_mg_pct=cogs_mg_pct,
    )
    db.add(line)
    db.commit()
    return RedirectResponse(f"/promo/{pr.promo_id}", status_code=303)


@app.post("/retailer/{pr_id}/copy_from")
async def copy_lines(pr_id: int, request: Request, db: Session = Depends(get_db)):
    """Copy line items from another retailer block in the same promo into this one.

    Carries code/description/RRP/% off and funding overrides; re-derives the buy price
    from the target retailer's channel price where available; leaves 6wk avg blank
    (customer-specific). Skips SKUs already present in the target block.
    """
    form = await request.form()
    target = db.get(PromoRetailer, pr_id)
    source = db.get(PromoRetailer, int(form.get("source_pr_id")))
    if not target or not source or source.promo_id != target.promo_id:
        return RedirectResponse(f"/promo/{target.promo_id if target else ''}", status_code=303)

    existing_codes = {l.product_code for l in target.lines}
    for src in source.lines:
        if src.product_code and src.product_code in existing_codes:
            continue
        prod = db.scalar(select(Product).where(Product.code == src.product_code)) if src.product_code else None
        buy = None
        if prod and prod.channel_prices:
            buy = _channel_price(prod.channel_prices, target.retailer_name)
        db.add(LineItem(
            promo_retailer_id=target.id,
            product_code=src.product_code,
            description=src.description,
            rrp_inc=src.rrp_inc,
            retailer_buy_ex=buy if buy is not None else src.retailer_buy_ex,
            pct_off=src.pct_off,
            avg_6wk=None,
            support_basis=src.support_basis,
            target_margin=src.target_margin,
            cogs_supplier_pct=src.cogs_supplier_pct,
            cogs_mg_pct=src.cogs_mg_pct,
            ratio_supplier=src.ratio_supplier,
            ratio_mg=src.ratio_mg,
            ratio_retailer=src.ratio_retailer,
            growth=src.growth,
        ))
        existing_codes.add(src.product_code)
    db.commit()
    return RedirectResponse(f"/promo/{target.promo_id}", status_code=303)


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
    line.avg_6wk = _parse_float(form.get("avg_6wk"))
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
    name = form.get("retailer_name", "").strip()
    if name:
        promo = db.get(Promotion, promo_id)
        country = promo.country if promo else "AU"
        existing = db.scalar(select(Retailer).where(
            Retailer.name == name, Retailer.country == country))
        rebate = _parse_float(form.get("rebate"))
        if rebate is None:
            rebate = existing.default_rebate if existing else 0.0
        db.add(PromoRetailer(promo_id=promo_id, retailer_name=name, rebate=rebate))
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
    promo.start_date = sd
    promo.end_date = ed
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
