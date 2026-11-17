"""Brand Profitability settings — form posts under /brand-profit (area brand_profit_settings,
enforced by the auth middleware's URL prefix). Pages are rendered in main.py; every post here
redirects back to the settings page with ?ok= or ?err=.
"""
import re
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from .. import warehouse
from . import db, np as bp_np, settings, upload

# a settings change alters net profit across the site: rebuild the deductions first, then
# everything built from them (home page, Sales Explorer, Item Explorer)
NP_DEPENDENT = ("sales_daily", "sales_tops", "sx_month", "sx_day", "sx_items", "item")

router = APIRouter(prefix="/brand-profit")


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.email if user is not None else "local"


def _back(region: str, tab: str, ok: str = "", err: str = "") -> RedirectResponse:
    q = {"region": region, "tab": tab}
    if ok:
        q["ok"] = ok
    if err:
        q["err"] = err
    return RedirectResponse("/brand-profit/settings?" + urlencode(q), status_code=303)


def _customer(v: str) -> tuple[int | None, str]:
    """'JB HI-FI - AU · 2374' (the picker's text) -> (2374, 'JB HI-FI - AU'); a bare id works too."""
    v = (v or "").strip()
    m = re.match(r"^(.*?)\s*·\s*(\d+)$", v) or re.match(r"^()(\d+)$", v)
    return (int(m.group(2)), m.group(1).strip()) if m else (None, v)


TAB_OF_KIND = {"base": "base", "brand": "brand", "store": "store", "sku": "sku", "mdf": "mdf", "gp_target": "gp"}


def _run(region, tab, fn, ok):
    try:
        with db.connect() as conn:
            fn(conn)
        warehouse.drop(bp_np.CACHE_PREFIXES)
        warehouse.expire(NP_DEPENDENT)
        return _back(region, tab, ok=ok)
    except (settings.Invalid, upload.Invalid) as e:
        return _back(region, tab, err=str(e))
    except db.NotConfigured:
        return _back(region, tab, err="The settings database isn't configured (BRAND_PROFIT_DB_URL / TARGETS_DB_URL).")


@router.post("/rule")
async def rule_add(request: Request):
    f = dict(await request.form())
    f["customer_id"], f["customer_name"] = _customer(f.get("customer", ""))
    tab = TAB_OF_KIND.get(f.get("kind", ""), "base")
    return _run(f.get("region", "AU"), tab, lambda c: settings.add_rule(c, f, _who(request)), "Added.")


@router.post("/rule/{rid}")
async def rule_edit(request: Request, rid: int):
    f = dict(await request.form())
    f["customer_id"], f["customer_name"] = _customer(f.get("customer", ""))
    tab = TAB_OF_KIND.get(f.get("kind", ""), "base")
    return _run(f.get("region", "AU"), tab, lambda c: settings.edit_rule(c, rid, f, _who(request)), "Saved.")


@router.post("/rule/{rid}/change")
async def rule_change(request: Request, rid: int, region: str = Form("AU"), kind: str = Form("base"),
                      month: str = Form(""), pct: str = Form(""), note: str = Form("")):
    return _run(region, TAB_OF_KIND.get(kind, "base"),
                lambda c: settings.change_from(c, rid, month, pct, _who(request), note), "New rate saved.")


@router.post("/rule/{rid}/delete")
async def rule_delete(request: Request, rid: int, region: str = Form("AU"), kind: str = Form("base")):
    return _run(region, TAB_OF_KIND.get(kind, "base"),
                lambda c: settings.delete_rule(c, rid, _who(request)), "Deleted.")


@router.post("/pm")
async def pm_add(request: Request):
    f = dict(await request.form())
    return _run(f.get("region", "AU"), "pm", lambda c: settings.save_pm(c, f, _who(request)), "Added.")


@router.post("/pm/{pid}")
async def pm_edit(request: Request, pid: int):
    f = dict(await request.form())
    return _run(f.get("region", "AU"), "pm", lambda c: settings.save_pm(c, f, _who(request), pid), "Saved.")


@router.post("/pm/{pid}/change")
async def pm_change(request: Request, pid: int, region: str = Form("AU"), month: str = Form(""),
                    pm: str = Form("")):
    return _run(region, "pm", lambda c: settings.pm_change_from(c, pid, month, pm, _who(request)), "New PM saved.")


@router.post("/pm/{pid}/delete")
async def pm_delete(request: Request, pid: int, region: str = Form("AU")):
    return _run(region, "pm", lambda c: settings.delete_pm(c, pid, _who(request)), "Deleted.")


@router.post("/account")
async def account_save(request: Request, region: str = Form("AU"), account: str = Form(""), kind: str = Form("")):
    return _run(region, "gl", lambda c: settings.save_account(c, region, account, kind, _who(request)), "Saved.")


@router.post("/account/delete")
async def account_delete(request: Request, region: str = Form("AU"), account: str = Form("")):
    return _run(region, "gl", lambda c: settings.delete_account(c, region, account, _who(request)), "Removed.")


@router.post("/upload")
async def gl_upload(request: Request, region: str = Form("AU"), note: str = Form(""),
                    file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    result = {}

    def go(conn):
        accts = {r["account"].upper(): r["kind"] for r in
                 conn.execute("SELECT account, kind FROM gl_account WHERE region = %s", (region,)).fetchall()}
        lines = upload.parse(text, accts)
        result.update(upload.import_lines(conn, region, lines, file.filename or "upload.csv", note, _who(request)))

    resp = _run(region, "gl", go, "")
    if result:
        return _back(region, "gl", ok=(f"Loaded {result['lines']} lines for {', '.join(result['months'])} "
                                       f"(freight ${result['freight']:,.2f}, vendor rebates ${result['vr']:,.2f}); "
                                       f"{result['replaced']} earlier lines for those months replaced."))
    return resp


@router.post("/batch/{bid}/delete")
async def batch_delete(request: Request, bid: int, region: str = Form("AU")):
    return _run(region, "gl", lambda c: settings.delete_batch(c, bid, _who(request)),
                "Upload removed; the lines it had replaced are back in force.")
