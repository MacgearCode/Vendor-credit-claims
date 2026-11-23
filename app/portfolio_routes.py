"""Portfolios page posts (/admin/portfolios/...). Admin only: the auth middleware guards the whole
/admin prefix. The page itself is in main.py; logic in portfolio.py."""
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import portfolio
from .brand_profit import db
from .brand_profit.settings import Invalid

router = APIRouter(prefix="/admin/portfolios")


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return user.email if user is not None else "local"


def _run(fn, ok: str, anchor: str) -> RedirectResponse:
    try:
        with db.connect() as conn:
            fn(conn)
        portfolio.changed()
        q = {"ok": ok}
    except Invalid as e:
        q = {"err": str(e)}
    except db.NotConfigured:
        q = {"err": "The settings database isn't configured (BRAND_PROFIT_DB_URL / TARGETS_DB_URL)."}
    return RedirectResponse(f"/admin/portfolios?{urlencode(q)}#{anchor}", status_code=303)


@router.post("/team")
async def team_assign(request: Request, pm: str = Form(""), reports_to: str = Form(""), month: str = Form("")):
    msg = f"{pm} reports to {reports_to} from {month}." if reports_to else f"{pm} reports to nobody from {month}."
    return _run(lambda c: portfolio.assign_team(c, pm, reports_to, month, _who(request)), msg, "teams")


@router.post("/team/{rid}/delete")
async def team_delete(request: Request, rid: int):
    return _run(lambda c: portfolio.delete_row(c, "pm_team", rid, _who(request)), "Row deleted.", "teams")


@router.post("/am")
async def am_assign(request: Request):
    f = dict(await request.form())
    who = f.get("am") or "NetSuite's rep"
    msg = f"{f.get('retailer', '')} × {f.get('brand') or 'every brand'} → {who} from {f.get('valid_from', '')}."
    return _run(lambda c: portfolio.assign_am(c, f, _who(request)), msg, "ams")


@router.post("/am/{rid}/delete")
async def am_delete(request: Request, rid: int):
    return _run(lambda c: portfolio.delete_row(c, "am_assignment", rid, _who(request)), "Row deleted.", "ams")
