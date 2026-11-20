"""GL Accounts — the transactions on the balance-sheet accounts PMs look after, so they can
check them during the month instead of waiting on finance (CFO request "GL access",
2026-11-11). Two reports, one per account group, each its own area key, so a PM sees only
their group: Mova (Rachel, Jeffrey) and Anker / Eufy / Soundcore (David, Kenneth, Dale).

Data = reporting.fact_gl_line (Sell Through/db/16_gl_lines.sql): every GL posting line on
the watched accounts, ALL dates, from the Zone export DW_gl_lines (a whole snapshot each run).
  - Amounts are in the ACCOUNT'S NATURAL SIGN (NetSuite's transaction-search Amount), in the
    subsidiary's currency: a receivable's balance is positive when the vendor owes Macgear,
    an accrual's is positive when Macgear has accrued / owes it. AU and NZ never summed.
  - Opening balance = every line before the first month shown; closing = to the end of the
    last. Because the export carries all history, these equal NetSuite's balances.
  - Month = the posting period's month.
The account numbers aren't in the warehouse (DW_dim_account has internal ids and names
only), so each group lists its accounts here: (internal id, number, name, parent id).
"""
from datetime import date

import psycopg

from . import warehouse

TTL = 10 * 60

GROUPS = {
    "gl_mova": {
        "title": "Mova", "slug": "mova",
        "blurb": "Spacewalker and Automax marketing recoverables and MDF accruals.",
        "accounts": [
            (960, "11546", "Mova Spacewalker Marketing Recoverable", None),
            (961, "11547", "Mova Automax Marketing Recoverable", None),
            (962, "22195", "MDF Accrual - Mova Spacewalker", None),
            (963, "22196", "MDF Accrual - Mova Automax", None),
        ],
    },
    "gl_anker": {
        "title": "Anker / Eufy / Soundcore", "slug": "anker",
        "blurb": "Marketing recoverables, rebate and returns claims, CMF accruals by retailer, the EUFY MDF buffer.",
        "accounts": [
            (940, "11540", "Anker marketing recoverable", None),
            (941, "11541", "EUFY marketing recoverable", None),
            (942, "11542", "Soundcore marketing recoverable", None),
            (954, "11543", "EUFY Rebate Claim Receivable", None),
            (955, "11544", "Anker Rebate Claim Receivable", None),
            (956, "11545", "EUFY Directed Returns Claim Receivable", None),
            (919, "22191", "CMF accrual - Anker", None),
            (923, "22191-01", "CMF Anker - JB Hi-Fi", 919),
            (924, "22191-02", "CMF Anker - Harvey Norman", 919),
            (925, "22191-08", "CMF Anker - Noel Leeming", 919),
            (926, "22191-99", "CMF Anker - Other Retailers", 919),
            (920, "22192", "CMF accrual - EUFY", None),
            (927, "22192-01", "CMF EUFY - JB Hi-Fi", 920),
            (928, "22192-02", "CMF EUFY - Harvey Norman", 920),
            (929, "22192-03", "CMF EUFY - The Good Guys", 920),
            (930, "22192-04", "CMF EUFY - Bunnings", 920),
            (931, "22192-05", "CMF EUFY - Bing Lee", 920),
            (932, "22192-06", "CMF EUFY - Retravision", 920),
            (933, "22192-07", "CMF EUFY - Baby Bunting", 920),
            (934, "22192-08", "CMF EUFY - Noel Leeming", 920),
            (935, "22192-09", "CMF EUFY - PB Tech", 920),
            (936, "22192-99", "CMF EUFY - Other Retailers", 920),
            (922, "22193", "CMF accrual - Soundcore", None),
            (937, "22193-01", "CMF Soundcore - JB Hi-Fi", 922),
            (938, "22193-08", "CMF Soundcore - Noel Leeming", 922),
            (939, "22193-99", "CMF Soundcore - Other Retailers", 922),
            (949, "22194", "EUFY MDF Buffer - MGAU Internal Only", None),
        ],
    },
}
BY_SLUG = {g["slug"]: k for k, g in GROUPS.items()}

_LINES_SQL = """
    select g.country_code, g.account_id, g.month, g.tran_date, g.tran_type, g.document_number,
           g.transaction_id, g.entity, g.memo, g.memo_main, g.brand, g.amount, g.line_unique_key
    from reporting.fact_gl_line g
    where g.account_id = any(%(accounts)s) and g.country_code = any(%(regions)s)
    order by g.tran_date, g.transaction_id, g.line_unique_key"""
_FRESH_SQL = "select max(loaded_from_export_at), max(last_modified) from reporting.fact_gl_line"


class NotLoaded(RuntimeError):
    pass


def build(group: str, regions) -> dict:
    ids = [a[0] for a in GROUPS[group]["accounts"]]
    try:
        with warehouse.connect() as conn, conn.cursor() as cur:
            cur.execute(_LINES_SQL, {"accounts": ids, "regions": list(regions)})
            rows = cur.fetchall()
            cur.execute(_FRESH_SQL)
            loaded, modified = cur.fetchone()
    except psycopg.errors.UndefinedTable as e:
        raise NotLoaded("reporting.fact_gl_line isn't in this warehouse yet") from e
    lines = [{"region": rg, "account": acc, "month": m, "date": d, "type": t, "doc": doc or "",
              "tran_id": tid, "entity": ent or "", "memo": memo or memo_main or "", "brand": br or "",
              "amount": float(amt)}
             for rg, acc, m, d, t, doc, tid, ent, memo, memo_main, br, amt, _ in rows]
    return {"lines": lines, "loaded": loaded, "modified": modified}


def for_user(group: str, regions) -> dict:
    return warehouse.cached(("gl_lines", group, tuple(sorted(regions))),
                            lambda: build(group, regions), ttl=TTL)


def fy_start(today: date) -> date:
    """First month of the company financial year (April) containing today."""
    return date(today.year if today.month >= 4 else today.year - 1, 4, 1)


def parse_month(s: str | None, default: date) -> date:
    try:
        y, m = (s or "").split("-")[:2]
        return date(int(y), int(m), 1)
    except ValueError:
        return default


def view(group: str, data: dict, region: str, first: date, last: date) -> dict:
    """The page for one region and month range: per account opening / movement / closing and
    the lines in the range with a running balance; parent accounts carry their sub-accounts'
    totals. Accounts with nothing ever posted are listed with zeros (so a PM sees the account
    exists and is empty)."""
    accts = GROUPS[group]["accounts"]
    rows = {a: {"id": a, "number": n, "name": nm, "parent": p, "open": 0.0, "move": 0.0,
                "close": 0.0, "lines": [], "children": []}
            for a, n, nm, p in accts}
    for ln in data["lines"]:
        if ln["region"] != region or ln["account"] not in rows:
            continue
        r = rows[ln["account"]]
        if ln["month"] < first:
            r["open"] += ln["amount"]
        elif ln["month"] <= last:
            r["move"] += ln["amount"]
            r["lines"].append(ln)
    out = []
    for a, n, nm, p in accts:
        r = rows[a]
        bal = r["open"]
        shown = []
        for ln in r["lines"]:
            bal += ln["amount"]
            shown.append({**ln, "balance": round(bal, 2)})
        r["lines"] = shown
        r["close"] = r["open"] + r["move"]
        if p is not None and p in rows:
            rows[p]["children"].append(r)
        else:
            out.append(r)
    for r in out:                       # a parent = its own postings + its sub-accounts
        for c in r["children"]:
            for k in ("open", "move", "close"):
                r[k] += c[k]
    for r in rows.values():
        for k in ("open", "move", "close"):
            r[k] = round(r[k], 2)
    months = sorted({ln["month"] for ln in data["lines"] if ln["region"] == region})
    return {"accounts": out, "count": sum(len(r["lines"]) for r in rows.values()),
            "first_month": months[0] if months else None}


def month_options(today: date, earliest: date | None) -> list[date]:
    start = min(earliest or fy_start(today), fy_start(today))
    out, d = [], date(today.year, today.month, 1)
    while d >= start:
        out.append(d)
        d = date(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, 1)
    return out


def netsuite_link(account_id: str | None, tran_id) -> str | None:
    """The transaction in NetSuite (opens in the viewer's own NetSuite session), or None
    when NETSUITE_ACCOUNT_ID isn't set."""
    if not account_id or not tran_id:
        return None
    host = account_id.strip().lower().replace("_", "-")
    return f"https://{host}.app.netsuite.com/app/accounting/transactions/transaction.nl?id={tran_id}"


def to_xlsx(group: str, region: str, first: date, last: date, v: dict) -> bytes:
    """Summary sheet (every account: opening, movement, closing) + Transactions sheet."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    g = GROUPS[group]
    cur = "AUD" if region == "AU" else "NZD"
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append([f"GL accounts - {g['title']} - {'Australia' if region == 'AU' else 'New Zealand'} ({cur})"])
    ws.append([f"{first:%b %Y} to {last:%b %Y}. Balances in each account's normal sign "
               "(receivable: owed to Macgear; accrual: accrued / owed by Macgear)."])
    ws.append([])
    ws.append(["Account", "Name", "Opening", "Movement", "Closing"])
    for c in ws[4]:
        c.font = Font(bold=True)
    tx = wb.create_sheet("Transactions")
    tx.append(["Account", "Name", "Date", "Period", "Type", "Document", "Entity", "Memo", "Brand",
               "Amount", "Running balance"])
    for c in tx[1]:
        c.font = Font(bold=True)

    def emit(r, indent=""):
        ws.append([r["number"], indent + r["name"], r["open"], r["move"], r["close"]])
        if not r["children"]:
            for ln in r["lines"]:
                tx.append([r["number"], r["name"], ln["date"], ln["month"].strftime("%b %Y"), ln["type"],
                           ln["doc"], ln["entity"], ln["memo"], ln["brand"], ln["amount"], ln["balance"]])
        for c in r["children"]:
            emit(c, "    ")
    for r in v["accounts"]:
        emit(r)
    for sheet, cols in ((ws, "CDE"), (tx, "JK")):
        for col in cols:
            for cell in sheet[col]:
                cell.number_format = '#,##0.00;[Red]-#,##0.00'
    for c in tx["C"]:
        c.number_format = "dd/mm/yyyy"
    for sheet, widths in ((ws, {"A": 12, "B": 44, "C": 15, "D": 15, "E": 15}),
                          (tx, {"A": 11, "B": 34, "C": 11, "D": 10, "E": 13, "F": 22, "G": 30, "H": 60,
                                "I": 12, "J": 14, "K": 16})):
        for k, w in widths.items():
            sheet.column_dimensions[k].width = w
    tx.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
