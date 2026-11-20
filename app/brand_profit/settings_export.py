"""Brand Profitability settings as an Excel workbook — for audit (user, 2026-11-20).

One workbook with every setting for the regions the viewer may see: each rate with the months
it covers, whether it's in force today, its note, who last changed it and the supporting
documents behind it; PMs; GL accounts; manual adjustments; the documents register; and the
full change log (every add / edit / delete with what changed, who and when). `log_only` gives
the change log on its own.
"""
import io
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import data, db
from .settings import KIND_LABELS, month_range

SYD = ZoneInfo("Australia/Sydney")
SHEETS = [("base", "Customer rebates"), ("brand", "Brand exceptions"), ("sku", "SKU exceptions"),
          ("store", "Store extras"), ("mdf", "MDF"), ("gp_target", "GP targets")]
HEAD = Font(bold=True, color="FFFFFF")
FILL = PatternFill("solid", fgColor="2F5496")
WRAP = Alignment(wrap_text=True, vertical="top")


def _syd(ts) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(SYD).replace(tzinfo=None)


def _status(vf: date, vt: date | None, today: date) -> str:
    m = date(today.year, today.month, 1)
    if vf > m:
        return "Starts later"
    if vt is not None and vt < m:
        return "Ended"
    return "In force"


def _sheet(wb, title, headers, rows, widths=None, pct_cols=(), money_cols=(), date_cols=(), wrap_cols=()):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font, c.fill = HEAD, FILL
    for r in rows:
        ws.append(r)
    for i, h in enumerate(headers, 1):
        col = get_column_letter(i)
        ws.column_dimensions[col].width = (widths or {}).get(h, max(10, min(40, len(h) + 4)))
        for cell in ws[col][1:]:
            if h in pct_cols:
                cell.number_format = "0.00##%"
            elif h in money_cols:
                cell.number_format = "#,##0.00;[Red]-#,##0.00"
            elif h in date_cols:
                cell.number_format = "dd/mm/yyyy hh:mm"
            if h in wrap_cols:
                cell.alignment = WRAP
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def _changes(action: str, before, after) -> str:
    """'pct: 19% → 17.5%; valid_to: open → 2026-08-01' for an edit; the key fields for add / delete."""
    def fmt(k, v):
        if v is None or v == "":
            return "open" if k == "valid_to" else "–"
        if k == "pct":
            return f"{float(v) * 100:g}%"
        return str(v)[:10] if k in ("valid_from", "valid_to", "month") else str(v)
    skip = {"id", "updated_at", "updated_by", "created_at", "created_by", "superseded_by", "batch_id"}
    b, a = before or {}, after or {}
    if action == "edit" and b and a:
        return "; ".join(f"{k}: {fmt(k, b.get(k))} → {fmt(k, a.get(k))}" for k in a
                         if k not in skip and b.get(k) != a.get(k))
    row = a or b
    return "; ".join(f"{k}: {fmt(k, v)}" for k, v in row.items() if k not in skip and v not in (None, ""))


def _log_sheet(wb, log):
    rows = []
    for l in log:
        before = l["before"] if not isinstance(l["before"], str) else json.loads(l["before"])
        after = l["after"] if not isinstance(l["after"], str) else json.loads(l["after"])
        rows.append([_syd(l["at"]), l["by_user"], l["entity"], l["entity_id"], l["action"],
                     _changes(l["action"], before, after), l["note"],
                     json.dumps(before) if before else "", json.dumps(after) if after else ""])
    _sheet(wb, "Change log", ["When (Sydney)", "Who", "What", "ID", "Action", "Changes", "Note", "Before", "After"],
           rows, widths={"When (Sydney)": 17, "Who": 26, "What": 16, "ID": 8, "Action": 9, "Changes": 70,
                         "Note": 34, "Before": 40, "After": 40},
           date_cols=("When (Sydney)",), wrap_cols=("Changes",))


def build(regions: list[str], by: str, log_only: bool = False, today: date | None = None) -> tuple[bytes, str]:
    today = today or datetime.now(SYD).date()
    now = datetime.now(SYD).replace(tzinfo=None, microsecond=0)
    with db.connect() as conn:
        log = conn.execute("SELECT * FROM change_log ORDER BY id").fetchall()
        if not log_only:
            rules = conn.execute("SELECT * FROM rate_rule WHERE region = ANY(%s) ORDER BY region, kind, customer_name, "
                                 "brand, item_category, item_code, valid_from", (regions,)).fetchall()
            pms = conn.execute("SELECT * FROM brand_pm WHERE region = ANY(%s) ORDER BY region, brand, valid_from",
                               (regions,)).fetchall()
            accounts = conn.execute("SELECT * FROM gl_account WHERE region = ANY(%s) ORDER BY region, kind, account",
                                    (regions,)).fetchall()
            adjustments = conn.execute("SELECT * FROM manual_adjustment WHERE region = ANY(%s) "
                                       "ORDER BY region, month, brand, id", (regions,)).fetchall()
            docs = conn.execute("SELECT * FROM document WHERE region = ANY(%s) ORDER BY uploaded_at",
                                (regions,)).fetchall()
    wb = Workbook()
    about = wb.active
    about.title = "About"
    stamp = now.strftime("%Y%m%d-%H%M")
    if log_only:
        about.append(["Brand Profitability — change log"])
        about.append([f"Generated {now:%d/%m/%Y %H:%M} Sydney time by {by}. Every add, edit and delete to the settings, "
                      "oldest first. Rows are never edited or removed."])
        _log_sheet(wb, log)
        about["A1"].font = Font(bold=True, size=14)
        about.column_dimensions["A"].width = 120
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue(), f"Brand_Profitability_change_log_{stamp}.xlsx"

    by_rule = {}
    for d in docs:
        by_rule.setdefault(d["rule_id"], []).append(d)
    lines = [
        ["Brand Profitability settings — audit export"],
        [f"Generated {now:%d/%m/%Y %H:%M} Sydney time by {by}. Regions: {', '.join(regions)}."],
        [],
        ["How to read it"],
        ["Rates are a % of GROSS sales (credit memos give them back). A rate applies to every month from 'From' to 'To' "
         "inclusive; 'To' blank = open, until changed."],
        ["A customer's rebate = its customer rebate + any brand exception (brand, optionally one item category) + SKU "
         "exception (one item) + store extra (one store). Negative exceptions reduce it. Customers with no row get 0%."],
        ["MDF = brand MDF % × gross; a row with a customer overrides the brand's default for that customer. GP target = the "
         "brand's target for net profit % on gross."],
        [f"Status is as at {today:%B %Y}: In force / Ended / Starts later. 'Supporting documents' lists the PDFs attached "
         "to that row (the Documents sheet has each file's size, SHA-256 and who uploaded it)."],
        ["Freight and vendor rebates come from the NetSuite GL (accounts on the GL accounts sheet); Manual adjustments are "
         "the amounts finance adds that never post in NetSuite (positive = a cost to Macgear)."],
        ["The Change log sheet holds every change ever made, with what changed, who and when."],
        [],
        ["Sheet", "Rows"],
    ]
    for l in lines:
        about.append(l)

    counts = []
    for kind, title in SHEETS:
        rs = [r for r in rules if r["kind"] == kind]
        head = ["Region"]
        if kind in ("base", "brand", "sku", "mdf"):
            head += ["Customer (head office)", "Customer ID"]
        if kind == "store":
            head += ["Store", "Store ID"]
        if kind in ("brand", "mdf", "gp_target"):
            head += ["Brand"]
        if kind == "brand":
            head += ["Item category"]
        if kind == "sku":
            head += ["Item code"]
        head += ["Target (NP % on gross)" if kind == "gp_target" else "Rate", "From", "To", "Months covered",
                 f"Status ({today:%b %Y})", "Note", "Last changed by", "Last changed (Sydney)", "Supporting documents"]
        rows = []
        for r in rs:
            row = [r["region"]]
            if kind in ("base", "brand", "sku", "mdf"):
                row += [r["customer_name"] or ("every customer" if kind == "mdf" else ""), r["customer_id"]]
            if kind == "store":
                row += [r["customer_name"], r["customer_id"]]
            if kind in ("brand", "mdf", "gp_target"):
                row += [r["brand"]]
            if kind == "brand":
                row += [r["item_category"] or "all"]
            if kind == "sku":
                row += [r["item_code"]]
            ds = by_rule.get(r["id"], [])
            row += [float(r["pct"]), r["valid_from"], r["valid_to"], month_range(r["valid_from"], r["valid_to"]),
                    _status(r["valid_from"], r["valid_to"], today), r["note"], r["updated_by"],
                    _syd(r["updated_at"]), "; ".join(d["filename"] for d in ds)]
            rows.append(row)
        ws = _sheet(wb, title, head, rows,
                    widths={"Customer (head office)": 34, "Store": 34, "Months covered": 30, "Note": 34,
                            "Last changed by": 26, "Last changed (Sydney)": 17, "Supporting documents": 40,
                            "Target (NP % on gross)": 12},
                    pct_cols=("Rate", "Target (NP % on gross)"), date_cols=("Last changed (Sydney)",))
        for col in ("From", "To"):
            i = head.index(col) + 1
            for cell in ws[get_column_letter(i)][1:]:
                cell.number_format = "mmm yyyy"
        counts.append((title, len(rows)))

    ws = _sheet(wb, "PMs", ["Region", "Brand", "PM", "From", "To", "Months covered", f"Status ({today:%b %Y})", "Note",
                            "Last changed by", "Last changed (Sydney)"],
                [[p["region"], p["brand"], p["pm"], p["valid_from"], p["valid_to"],
                  month_range(p["valid_from"], p["valid_to"]), _status(p["valid_from"], p["valid_to"], today),
                  p["note"], p["updated_by"], _syd(p["updated_at"])] for p in pms],
                widths={"Months covered": 30, "PM": 22, "Last changed by": 26, "Last changed (Sydney)": 17},
                date_cols=("Last changed (Sydney)",))
    for col in "DE":
        for cell in ws[col][1:]:
            cell.number_format = "mmm yyyy"
    counts.append(("PMs", len(pms)))

    acct_rows = [["AU, NZ", f"NetSuite internal id {a}", "Freight" if k == "freight" else "Vendor rebate", "built in", "", None]
                 for a, k in sorted(data.GL_KIND.items())]
    acct_rows += [[a["region"], a["account"], "Freight" if a["kind"] == "freight" else "Vendor rebate", "settings page",
                   a["updated_by"], _syd(a["updated_at"])] for a in accounts]
    _sheet(wb, "GL accounts", ["Region", "Account", "Counts as", "Set on", "Last changed by", "Last changed (Sydney)"],
           acct_rows, widths={"Account": 40, "Last changed by": 26, "Last changed (Sydney)": 17},
           date_cols=("Last changed (Sydney)",))
    counts.append(("GL accounts", len(acct_rows)))

    ws = _sheet(wb, "Manual adjustments", ["Region", "Month", "Brand", "Type", "Amount (+ = cost)", "Memo", "Entered by",
                                           "Entered (Sydney)", "Last changed by", "Last changed (Sydney)"],
                [[a["region"], a["month"], a["brand"], "Freight" if a["kind"] == "freight" else "Vendor rebate",
                  float(a["amount"]), a["memo"], a["created_by"], _syd(a["created_at"]), a["updated_by"],
                  _syd(a["updated_at"])] for a in adjustments],
                widths={"Memo": 70, "Entered by": 22, "Last changed by": 22, "Entered (Sydney)": 17,
                        "Last changed (Sydney)": 17, "Amount (+ = cost)": 16},
                money_cols=("Amount (+ = cost)",), date_cols=("Entered (Sydney)", "Last changed (Sydney)"),
                wrap_cols=("Memo",))
    for cell in ws["B"][1:]:
        cell.number_format = "mmm yyyy"
    counts.append(("Manual adjustments", len(adjustments)))

    live = {r["id"] for r in rules}
    _sheet(wb, "Documents", ["Region", "Rule (as it read when attached)", "Rule still exists", "File", "Size (KB)",
                             "SHA-256", "Note", "Uploaded by", "Uploaded (Sydney)"],
           [[d["region"], d["rule_label"], "yes" if d["rule_id"] in live else "no (deleted)", d["filename"],
             round(d["size_bytes"] / 1024, 1), d["sha256"], d["note"], d["uploaded_by"], _syd(d["uploaded_at"])]
            for d in docs],
           widths={"Rule (as it read when attached)": 70, "File": 36, "SHA-256": 66, "Note": 30, "Uploaded by": 24,
                   "Uploaded (Sydney)": 17}, date_cols=("Uploaded (Sydney)",), wrap_cols=("Rule (as it read when attached)",))
    counts.append(("Documents", len(docs)))
    _log_sheet(wb, log)
    counts.append(("Change log", len(log)))

    for title, n in counts:
        about.append([title, n])
    about["A1"].font = Font(bold=True, size=14)
    about["A4"].font = Font(bold=True)
    about.cell(row=len(lines), column=1).font = Font(bold=True)
    about.column_dimensions["A"].width = 120
    about.column_dimensions["B"].width = 10
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), f"Brand_Profitability_settings_{'_'.join(regions)}_{stamp}.xlsx"
