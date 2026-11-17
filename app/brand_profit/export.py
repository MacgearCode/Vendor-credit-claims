"""Brand Profitability report -> Excel (the on-screen tree, flattened, PM / brand / customer)."""
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

COLS = [("Quantity", "qty", "#,##0"), ("Gross sales $", "gross", "#,##0.00"), ("Rebate $", "rebate", "#,##0.00"),
        ("Net sales $", "net", "#,##0.00"), ("Cost of goods", "cogs", "#,##0.00"),
        # NetSuite's sign kept (ties to the GL); colour = effect on profit: red reduces it, green adds
        ("Freight $ (cost)", "freight", "[Red]#,##0.00;[Color10]-#,##0.00;0.00"),
        ("Vendor rebate $ (− = income)", "vr", "[Red]#,##0.00;[Color10]-#,##0.00;0.00"),
        ("Gross profit $", "gp", "#,##0.00"), ("MDF $", "mdf", "#,##0.00"), ("Net profit $", "np", "#,##0.00"),
        ("NP % on net sales", "np_net_pct", "0.0%"), ("NP % on gross sales", "np_gross_pct", "0.0%"),
        ("GP target (NP % on gross, sales-weighted)", "gp_target", "0.0%"),
        ("vs target (pts)", "vs_target", "+0.0%;-0.0%;0.0%"), ("Target NP $", "target_np", "#,##0.00"),
        ("vs target $", "vs_target_amt", "+#,##0.00;-#,##0.00;0.00"),
        ("Share of gross with a target", "target_coverage", "0%")]


def to_xlsx(rep: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Brand profitability"
    region = "Australia (AUD)" if rep["region"] == "AU" else "New Zealand (NZD)"
    ws.append([f"Brand profitability — {region} — {rep['label']} (company financial year, Apr–Mar)"])
    ws["A1"].font = Font(bold=True, size=13)
    note = f"Excludes AU→NZ intercompany sales of ${rep['intercompany']:,.2f}." if rep["intercompany"] else ""
    ws.append([note])
    ws.append([])
    ws.append(["PM", "Brand", "Customer"] + [c[0] for c in COLS])
    head = ws.max_row
    for c in ws[head]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2F5496")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    pm = brand = ""
    for r in rep["rows"]:
        if r["level"] == 0:
            pm, brand = r["label"], ""
            lab = [pm, "", ""]
        elif r["level"] == 1:
            brand = r["label"]
            lab = [pm, brand, ""]
        else:
            lab = [pm, brand, r["label"]]
        ws.append(lab + [r.get(c[1]) for c in COLS])
        if r["level"] < 2:
            for c in ws[ws.max_row]:
                c.font = Font(bold=r["level"] == 0)
    t = rep["total"]
    ws.append(["Total", "", ""] + [t.get(c[1]) for c in COLS])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    for row in ws.iter_rows(min_row=head + 1):
        for i, (_, _, fmt) in enumerate(COLS):
            row[3 + i].number_format = fmt
    for col, w in zip("ABC", (20, 18, 40)):
        ws.column_dimensions[col].width = w
    for i in range(len(COLS)):
        ws.column_dimensions[ws.cell(row=head, column=4 + i).column_letter].width = 14
    ws.freeze_panes = ws.cell(row=head + 1, column=4)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
