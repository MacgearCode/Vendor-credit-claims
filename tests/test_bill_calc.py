"""Vendor Bill Importer arithmetic.

Figures are from the five July 2026 Amazon Advertising invoices, hand-checked
against the PDFs and against the bills that imported into NetSuite on
2026-08-05.
"""
from decimal import Decimal

from app import bill_calc

# The text a real invoice extracts to, trimmed to the blocks the parser reads.
# 432277HAPA26 is the useful case: three brands, and per-line GST that lands a
# cent above the invoice's own figure.
INVOICE_432277 = """\
Invoice Number: 432277HAPA26
Invoice Date: 02-07-2026
Invoice Period: 30-06-2026 to 01-07-2026
Invoice Currency: AUD
Summary of Portfolio Charges
Portfolio name: Amount
MOVA - Sponsored Products 954.50 AUD
Twelve South- HoverBar 44.95 AUD
Adonit 1.86 AUD
Total Amount: 1001.31 AUD
Total Campaign Charges 1001.31
Subtotal (excluding tax) 1001.31
Tax Rate - GST 10%
Tax Amount - GST 100.13
Total Amount (tax included) 1101.44 AUD
"""


def _parsed(text, name="432277HAPA26.pdf"):
    inv = bill_calc.parse_invoice_text(text, name)
    totals = bill_calc.allocate(inv)
    bill_calc.validate(inv, totals)
    return inv, totals


def test_header_fields_parse():
    inv, _ = _parsed(INVOICE_432277)
    assert inv.number == "432277HAPA26"
    assert inv.date_ns == "02/07/2026"           # dd/mm/yyyy for the import
    assert inv.currency == "AUD"
    assert inv.period_label == "30-06-2026 to 01-07-2026"
    assert inv.subtotal_ex_tax == Decimal("1001.31")
    assert inv.total_inc_tax == Decimal("1101.44")


def test_brand_split_matches_the_invoice():
    _, totals = _parsed(INVOICE_432277)
    assert totals["MOVA"] == Decimal("954.50")
    assert totals["TWELVE SOUTH"] == Decimal("44.95")
    assert totals["ADONIT"] == Decimal("1.86")
    # The allocation must always tie back to the ex-tax subtotal exactly.
    assert sum(totals.values()) == Decimal("1001.31")


def test_netsuite_rounds_gst_per_line_and_warns():
    """Amazon rounds GST once on the total (100.13); NetSuite rounds each line
    and sums (95.45 + 4.50 + 0.19 = 100.14). The cent is expected — the point is
    that it is surfaced, not silently imported."""
    inv, totals = _parsed(INVOICE_432277)
    assert bill_calc.netsuite_tax(totals) == Decimal("100.14")
    assert inv.tax_amount == Decimal("100.13")
    assert any("NetSuite will calculate GST" in w for w in inv.warnings)


def test_rows_are_item_lines_with_no_ap_or_gst_line():
    inv, totals = _parsed(INVOICE_432277)
    rows = bill_calc.rows_for(inv, totals)

    assert len(rows) == 3                        # one per brand, nothing else
    assert [r["Class"] for r in rows] == ["MOVA", "TWELVE SOUTH", "ADONIT"]
    assert all(r["Item"] == bill_calc.ITEM for r in rows)
    assert all(r["Quantity"] == "1" for r in rows)
    # Rates are ex-tax and sum to the subtotal; NetSuite adds GST from the code.
    assert sum(Decimal(r["Rate"]) for r in rows) == Decimal("1001.31")
    assert all(r["Tax Code"] == bill_calc.TAX_CODE for r in rows)
    # Reference types that took several failed imports to pin down.
    assert all(r["Subsidiary"] == "2" and r["Location"] == "4" for r in rows)
    # Only MOVA carries a department.
    assert [r["Department"] for r in rows] == ["10", "", ""]


def test_only_brands_with_spend_get_a_line():
    text = INVOICE_432277.replace("Adonit 1.86 AUD\n", "").replace(
        "Total Amount: 1001.31", "Total Amount: 999.45").replace(
        "Total Campaign Charges 1001.31", "Total Campaign Charges 999.45").replace(
        "Subtotal (excluding tax) 1001.31", "Subtotal (excluding tax) 999.45").replace(
        "Tax Amount - GST 100.13", "Tax Amount - GST 99.95").replace(
        "Total Amount (tax included) 1101.44", "Total Amount (tax included) 1099.40")
    inv, totals = _parsed(text)
    rows = bill_calc.rows_for(inv, totals)
    assert [r["Class"] for r in rows] == ["MOVA", "TWELVE SOUTH"]


def test_unrecognised_portfolio_falls_to_mova_and_warns():
    """A new brand's portfolio must never be booked silently — it lands on MOVA
    via the residual, and the warning is what prompts a BRAND_RULES update."""
    text = INVOICE_432277.replace("Adonit 1.86 AUD", "Belkin Widgets 1.86 AUD")
    inv, totals = _parsed(text)
    assert totals["ADONIT"] == Decimal("0.00")
    assert totals["MOVA"] == Decimal("956.36")   # 954.50 + the 1.86 residual
    assert sum(totals.values()) == Decimal("1001.31")
    assert any("does not match a brand rule" in w for w in inv.warnings)


def test_non_aud_invoice_warns():
    text = INVOICE_432277.replace("Invoice Currency: AUD", "Invoice Currency: NZD")
    inv, _ = _parsed(text)
    assert any("is not configured" in w for w in inv.warnings)
