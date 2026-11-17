"""Freight / vendor-rebate GL lines from a CSV — the NetSuite saved search, exported by hand.

Nothing goes in that isn't in NetSuite (user, 2026-11-17): the upload takes the same CSV the
Zone export will send to S3, so the two are interchangeable. Columns are matched by header,
case-insensitively; only these matter:

  Date | Period        the posting month comes from Period ("Oct 2026", "FY27 : Q3 : Oct 2026")
                       if present, else from Date (d/m/yyyy or yyyy-mm-dd)
  Account              must be mapped to freight or vendor rebate on the settings page
  Amount               debit - credit; positive = cost. "(1,234.50)" and "$" are fine
  Class | Brand        the brand (a "Parent : Child" class keeps the last part)
  Line Unique Key, Document Number, Type, Name | Vendor, Memo   kept for the drill-down

An upload REPLACES the region's earlier lines (seeded or uploaded) for every month it contains;
the old lines are kept, marked superseded. Months it doesn't contain are left alone.
"""
import csv
import io
import re
from datetime import date

from . import calc, db

MONTHS = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
ALIASES = {
    "line_key": ("line unique key", "line key"),
    "document_number": ("document number", "document no", "doc number", "number"),
    "tran_type": ("type", "transaction type"),
    "tran_date": ("date", "transaction date"),
    "period": ("period", "posting period", "accounting period"),
    "account": ("account", "account (line)", "account name", "account number"),
    "brand": ("class", "brand", "class (line)"),
    "vendor": ("name", "vendor", "entity"),
    "memo": ("memo", "memo (line)", "description"),
    "memo_main": ("memo (main)", "main memo"),
    "amount": ("amount", "amount (debit - credit)", "net amount"),
    "kind": ("kind",),
}


class Invalid(ValueError):
    pass


def _amount(s: str) -> float:
    t = (s or "").strip().replace("$", "").replace(",", "").replace("AUD", "").replace("NZD", "").strip()
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    if t in ("", "-"):
        return 0.0
    v = float(t)
    return -v if neg else v


def _month(period: str, tran_date: str) -> date:
    m = re.findall(r"\b([A-Za-z]{3})[a-z]*\s+(\d{4})\b", period or "")
    if m:
        mon, yr = m[-1]
        if mon.upper() in MONTHS:
            return date(int(yr), MONTHS[mon.upper()], 1)
    d = (tran_date or "").strip()
    for pat, order in ((r"^(\d{4})-(\d{1,2})-(\d{1,2})", "ymd"), (r"^(\d{1,2})/(\d{1,2})/(\d{4})", "dmy")):
        mm = re.match(pat, d)
        if mm:
            a, b, c = (int(x) for x in mm.groups())
            return date(a, b, 1) if order == "ymd" else date(c, b, 1)
    raise Invalid(f"no month in Period '{period}' or Date '{tran_date}'")


def _date(s: str):
    s = (s or "").strip()
    mm = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if mm:
        return date(int(mm.group(3)), int(mm.group(2)), int(mm.group(1)))
    mm = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3))) if mm else None


def parse(text: str, accounts: dict[str, str]) -> list[dict]:
    """CSV text -> GL line dicts. accounts maps an upper-case account to 'freight' | 'vr'.
    Raises Invalid listing every problem (unmapped accounts, bad rows) — all or nothing."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        raise Invalid("The file is empty.")
    header = [h.strip().lower() for h in rows[0]]
    col = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in header:
                col[key] = header.index(n)
                break
    missing = [k for k in ("account", "amount") if k not in col]
    if "period" not in col and "tran_date" not in col:
        missing.append("date or period")
    if missing:
        raise Invalid(f"Missing column(s): {', '.join(missing)}. Header found: {', '.join(rows[0])}")
    get = lambda r, k: (r[col[k]].strip() if k in col and col[k] < len(r) else "")
    out, problems, unmapped = [], [], set()
    for n, r in enumerate(rows[1:], start=2):
        try:
            acct = get(r, "account")
            kind = get(r, "kind").lower() or accounts.get(acct.upper())
            if kind == "vendor rebate":
                kind = "vr"
            if kind not in db.GL_KINDS:
                unmapped.add(acct)
                continue
            brand = get(r, "brand").split(":")[-1]
            memo = " · ".join(x for x in (get(r, "memo"), get(r, "memo_main")) if x)
            out.append({"month": _month(get(r, "period"), get(r, "tran_date")), "tran_date": _date(get(r, "tran_date")),
                        "line_key": get(r, "line_key"), "document_number": get(r, "document_number"),
                        "tran_type": get(r, "tran_type"), "account": acct, "brand": calc.norm_brand(brand),
                        "vendor": get(r, "vendor"), "memo": memo, "kind": kind, "amount": round(_amount(get(r, "amount")), 2)})
        except (Invalid, ValueError) as e:
            problems.append(f"row {n}: {e}")
    if unmapped:
        problems.insert(0, "Accounts not mapped to freight or vendor rebate (map them under GL accounts first): "
                        + ", ".join(sorted(unmapped)))
    if problems:
        raise Invalid("; ".join(problems[:12]) + (f" … and {len(problems) - 12} more" if len(problems) > 12 else ""))
    if not out:
        raise Invalid("No lines found.")
    return out


def import_lines(conn, region: str, lines: list[dict], filename: str, note: str, by: str) -> dict:
    months = sorted({g["month"] for g in lines})
    with conn.transaction():
        bid = conn.execute("INSERT INTO gl_batch (region, source, filename, note, created_by) "
                           "VALUES (%s, 'upload', %s, %s, %s) RETURNING id", (region, filename, note, by)).fetchone()["id"]
        replaced = conn.execute("UPDATE gl_line SET superseded_by = %s WHERE region = %s AND month = any(%s) "
                                "AND superseded_by IS NULL", (bid, region, months)).rowcount
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO gl_line (batch_id, region, month, tran_date, line_key, document_number, tran_type, "
                "account, brand, vendor, memo, kind, amount) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(bid, region, g["month"], g["tran_date"], g["line_key"], g["document_number"], g["tran_type"],
                  g["account"], g["brand"], g["vendor"], g["memo"], g["kind"], g["amount"]) for g in lines])
        summary = {"lines": len(lines), "months": [m.isoformat()[:7] for m in months], "replaced": replaced,
                   "freight": round(sum(g["amount"] for g in lines if g["kind"] == "freight"), 2),
                   "vr": round(sum(g["amount"] for g in lines if g["kind"] == "vr"), 2)}
        db.log(conn, by, "gl_batch", bid, "upload", after=summary, note=filename)
    return {"batch": bid, **summary}
