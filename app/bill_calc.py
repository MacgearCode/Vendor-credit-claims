r"""Amazon Advertising tax invoices -> NetSuite Vendor Bill import rows.

Pure functions only -- text in, numbers out. No PDF reading, no DB, no HTTP, so
the allocation and the GST arithmetic can be unit-tested directly. `bill_pdf.py`
turns a PDF into the text this module parses; `bill_export.py` turns the rows
into a CSV.

Ported from `vendorbill/parse_amazon_invoices.py`, whose README documents how
the NetSuite reference values below were verified.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

# --------------------------------------------------------------------------
# Configuration -- everything that maps Amazon's wording onto NetSuite values.
# --------------------------------------------------------------------------

# Reference-type mismatches are the main source of import failures, so the
# comments record which way NetSuite matches each field. Proven by a clean
# import of all five July 2026 invoices on 2026-08-05.
VENDOR = "V00339 Amazon Comm AU"  # internal id 8679; matched on Name
SUBSIDIARY = "2"                  # Parent Company : MacGear AU; internal id
# Location and tax code go as internal IDs because their names do not survive a
# Name lookup: "AU3 - Sydney Office" contains an en dash and the importer
# defaults to Windows-1252, and "NCT-AU" is a display name NetSuite builds by
# appending the nexus country.
LOCATION = "4"                    # AU3 - Sydney Office; internal id
TAX_CODE = "15"                   # NCT-AU, AU GST on purchases; internal id

# Bills are written against the Items sublist, matching every bill already
# posted for this vendor. The item posts to 64060 Marketing Promotion With
# Customers & Suppliers, so the GL result is the same as an expense line would
# give, but reporting stays consistent with history.
#
# No GST line: the tax code drives it. An explicit 16010 line gets taxed itself,
# which inflated bill 432277HAPA26 to 1,211.59 against a PDF of 1,101.44.
ITEM = "Marketing Promotion With Customers & Suppliers AU"

# NCT-AU is a 10% code. If Amazon ever prints a different rate the code is no
# longer the right one, so check rather than assume.
EXPECTED_TAX_RATE = "10%"
TAX_FRACTION = Decimal("0.10")  # EXPECTED_TAX_RATE, for the rounding check

# Portfolio name prefix -> NetSuite class. Match is on the lower-cased portfolio
# name; first matching prefix wins.
BRAND_RULES: list[tuple[str, str]] = [
    ("mova", "MOVA"),
    ("twelve south", "TWELVE SOUTH"),
    ("adonit", "ADONIT"),
]

# Brands emit in this order, and only when their total is non-zero.
BRAND_ORDER = ["MOVA", "TWELVE SOUTH", "ADONIT"]
BRAND_MEMO_LABEL = {"MOVA": "Mova", "TWELVE SOUTH": "Twelvesouth", "ADONIT": "Adonit"}
BRAND_DEPARTMENT = {"MOVA": "10"}  # brands not listed get a blank department

# Portfolios that are not brand-specific, plus any residual (regulatory fees,
# rounding adjustments) land on this brand.
DEFAULT_BRAND = "MOVA"
NON_BRAND_PORTFOLIOS = {"other fees and adjustments"}

# Currencies this module knows how to book. Guard against an NZ invoice being
# silently booked to the AU subsidiary.
SUPPORTED_CURRENCIES = {"AUD"}

CSV_COLUMNS = [
    "External ID", "Vendor", "Bill #", "Date", "Due Date", "Terms",
    "Subsidiary", "Currency", "Location", "Header Memo", "Description",
    "Item", "Quantity", "Rate", "Class", "Department", "Tax Code",
]

ZERO = Decimal("0.00")

AMOUNT = r"(-?[\d,]+\.\d{2})"
# "<portfolio name>  1,234.56 AUD" / "<portfolio name>  -2.90 AUD"
RE_SUMMARY_ROW = re.compile(rf"^(.*?)\s+{AMOUNT}\s+[A-Z]{{3}}$")
# "01-07-2026  Over Delivery Adjustment ...  -0.41 AUD"
RE_ADJUSTMENT_ROW = re.compile(rf"^\d{{2}}-\d{{2}}-\d{{4}}\s+(.*?)\s+{AMOUNT}\s+[A-Z]{{3}}$")
RE_APPLIED_AGAINST = re.compile(r"Applied Against Portfolio:\s*(.+?)\s*$")


class InvoiceParseError(ValueError):
    """The PDF is not an Amazon Advertising tax invoice we recognise."""


@dataclass
class Invoice:
    """One Amazon Advertising tax invoice, as parsed."""
    source_name: str
    number: str
    date: str            # dd-mm-yyyy, as printed
    period_from: str
    period_to: str
    currency: str
    portfolios: list[tuple[str, Decimal]] = field(default_factory=list)
    adjustments: list[tuple[str, str, Decimal]] = field(default_factory=list)
    campaign_total: Decimal = ZERO
    subtotal_ex_tax: Decimal = ZERO
    tax_rate: str = ""
    tax_amount: Decimal = ZERO
    total_inc_tax: Decimal = ZERO
    warnings: list[str] = field(default_factory=list)

    @property
    def period_label(self) -> str:
        return f"{self.period_from} to {self.period_to}"

    @property
    def date_ns(self) -> str:
        """Invoice date as dd/mm/yyyy for the NetSuite import."""
        return self.date.replace("-", "/")


def _money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _find(pattern: str, text: str, label: str, source_name: str) -> str:
    """Single required capture group, or a clear error naming the file."""
    m = re.search(pattern, text)
    if not m:
        raise InvoiceParseError(f"{source_name}: could not find {label}")
    return m.group(1)


def fmt(amount: Decimal) -> str:
    """Two decimal places with trailing zeros stripped, e.g. 954.50 -> '954.5'."""
    s = f"{amount:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_invoice_text(text: str, source_name: str) -> Invoice:
    """Parse the extracted text of one invoice PDF."""
    lines = [ln.strip() for ln in text.split("\n")]

    # Header. These sit in a right-hand box that extract_text often merges with
    # the address column on the left, so search rather than match whole lines.
    period = re.search(
        r"Invoice Period:\s*(\d{2}-\d{2}-\d{4})\s*to\s*(\d{2}-\d{2}-\d{4})", text
    )
    if not period:
        raise InvoiceParseError(f"{source_name}: could not find Invoice Period")

    inv = Invoice(
        source_name=source_name,
        number=_find(r"Invoice Number:\s*(\S+)", text, "Invoice Number", source_name),
        date=_find(r"Invoice Date:\s*(\d{2}-\d{2}-\d{4})", text, "Invoice Date", source_name),
        period_from=period.group(1),
        period_to=period.group(2),
        currency=_find(r"Invoice Currency:\s*([A-Z]{3})", text, "Invoice Currency", source_name),
    )

    _parse_portfolio_summary(inv, lines)
    _parse_adjustments(inv, lines)

    inv.campaign_total = _money(
        _find(rf"Total Campaign Charges\s+{AMOUNT}", text, "Total Campaign Charges", source_name)
    )
    inv.subtotal_ex_tax = _money(
        _find(rf"Subtotal \(excluding tax\)\s+{AMOUNT}", text,
              "Subtotal (excluding tax)", source_name)
    )
    inv.tax_rate = _find(r"Tax Rate\s*-\s*GST\s+(\S+)", text, "Tax Rate", source_name)
    inv.tax_amount = _money(
        _find(rf"Tax Amount\s*-\s*GST\s+{AMOUNT}", text, "Tax Amount - GST", source_name)
    )
    inv.total_inc_tax = _money(
        _find(rf"Total Amount \(tax included\)\s+{AMOUNT}\s+[A-Z]{{3}}", text,
              "Total Amount (tax included)", source_name)
    )
    return inv


def _parse_portfolio_summary(inv: Invoice, lines: list[str]) -> None:
    """Read the 'Summary of Portfolio Charges' table on page 1."""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("Portfolio name: Amount"))
        end = next(i for i, ln in enumerate(lines[start:], start)
                   if ln.startswith("Total Amount:"))
    except StopIteration:
        raise InvoiceParseError(
            f"{inv.source_name}: could not find the portfolio summary table"
        )

    for raw in lines[start + 1:end]:
        if not raw:
            continue
        m = RE_SUMMARY_ROW.match(raw)
        if not m:
            # A wrapped portfolio name would land here. Never guess -- the
            # amount would be dropped from the allocation silently.
            inv.warnings.append(f"unparsed portfolio summary line: {raw!r}")
            continue
        inv.portfolios.append((m.group(1).strip(), _money(m.group(2))))

    printed_total = _money(lines[end].split("Total Amount:")[1].strip().split()[0])
    summed = sum((amt for _, amt in inv.portfolios), ZERO)
    if summed != printed_total:
        inv.warnings.append(
            f"portfolio subtotals sum to {summed} but the invoice says {printed_total}"
        )


def _parse_adjustments(inv: Invoice, lines: list[str]) -> None:
    """Read the optional 'Adjustments' table (absent on most invoices)."""
    start = None
    for i, ln in enumerate(lines[:-1]):
        if ln == "Adjustments" and lines[i + 1].startswith("Date Comments"):
            start = i + 2
            break
    if start is None:
        return

    for raw in lines[start:]:
        if raw.startswith("Adjustment total:"):
            break
        m = RE_ADJUSTMENT_ROW.match(raw)
        if not m:
            if raw:
                inv.warnings.append(f"unparsed adjustment line: {raw!r}")
            continue
        comment, amount = m.group(1).strip(), _money(m.group(2))
        against = RE_APPLIED_AGAINST.search(comment)
        inv.adjustments.append((against.group(1) if against else "", comment, amount))


# --------------------------------------------------------------------------
# Allocation
# --------------------------------------------------------------------------

def brand_for(portfolio: str) -> str | None:
    """NetSuite class for a portfolio name, or None if it isn't brand-specific."""
    name = portfolio.strip().lower()
    if name in NON_BRAND_PORTFOLIOS:
        return None
    for prefix, brand in BRAND_RULES:
        if name.startswith(prefix):
            return brand
    return None


def allocate(inv: Invoice) -> dict[str, Decimal]:
    """Split the ex-tax subtotal across brands.

    Portfolio charges go to the brand named in the portfolio. Adjustments follow
    the portfolio they were applied against. Anything left over -- the "Other
    Fees and Adjustments" portfolio, regulatory fees, rounding -- falls to
    DEFAULT_BRAND, which is how the manually prepared bills were coded.
    """
    totals: dict[str, Decimal] = {b: ZERO for b in BRAND_ORDER}

    for name, amount in inv.portfolios:
        brand = brand_for(name)
        if brand is None:
            if name.strip().lower() not in NON_BRAND_PORTFOLIOS:
                inv.warnings.append(
                    f"portfolio {name!r} ({amount}) does not match a brand rule "
                    f"-- booked to {DEFAULT_BRAND}"
                )
            continue  # picked up by the residual below
        totals[brand] += amount

    for against, comment, amount in inv.adjustments:
        brand = brand_for(against) if against else None
        if brand is None:
            inv.warnings.append(
                f"adjustment {comment!r} ({amount}) not tied to a brand "
                f"-- booked to {DEFAULT_BRAND}"
            )
            continue  # residual
        totals[brand] += amount

    # Residual: non-brand portfolios, regulatory fees, rounding adjustments.
    totals[DEFAULT_BRAND] += inv.subtotal_ex_tax - sum(totals.values(), ZERO)
    return totals


def netsuite_tax(totals: dict[str, Decimal]) -> Decimal:
    """The GST NetSuite will calculate -- per line, then summed."""
    return sum(
        (amount * TAX_FRACTION).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        for amount in totals.values() if amount != ZERO
    ) or ZERO


def validate(inv: Invoice, totals: dict[str, Decimal]) -> None:
    """Append any warnings to `inv.warnings`. Never raises -- warnings are the
    output, and the user decides whether they matter."""
    allocated = sum(totals.values(), ZERO)
    if allocated != inv.subtotal_ex_tax:
        inv.warnings.append(
            f"allocated {allocated} != subtotal ex tax {inv.subtotal_ex_tax}"
        )
    if allocated + inv.tax_amount != inv.total_inc_tax:
        inv.warnings.append(
            f"subtotal {allocated} + tax {inv.tax_amount} != total {inv.total_inc_tax}"
        )
    if inv.currency not in SUPPORTED_CURRENCIES:
        inv.warnings.append(
            f"currency {inv.currency} is not configured -- the subsidiary, "
            f"location and tax code below are the AU ones and are probably wrong"
        )
    if inv.tax_rate != EXPECTED_TAX_RATE:
        inv.warnings.append(
            f"tax rate is {inv.tax_rate}, expected {EXPECTED_TAX_RATE} -- "
            f"tax code NCT-AU (internal id {TAX_CODE}) may no longer be correct"
        )

    # Amazon rounds GST once, on the invoice total; NetSuite rounds it per line
    # and sums. On some invoices that lands a cent apart, so say so up front
    # rather than leaving it to be found at reconciliation.
    ns_tax = netsuite_tax(totals)
    if ns_tax != inv.tax_amount:
        inv.warnings.append(
            f"NetSuite will calculate GST of {ns_tax} per line against the "
            f"invoice's {inv.tax_amount} -- bill total will be "
            f"{allocated + ns_tax}, not {inv.total_inc_tax}"
        )


# --------------------------------------------------------------------------
# Import rows
# --------------------------------------------------------------------------

def rows_for(inv: Invoice, totals: dict[str, Decimal]) -> list[dict[str, str]]:
    """One Items-sublist row per brand with a non-zero total, priced ex-tax."""
    header_memo = f"Amazon Advertising {inv.period_label}"
    base = {
        "External ID": inv.number,
        "Vendor": VENDOR,
        "Bill #": inv.number,
        "Date": inv.date_ns,
        "Due Date": inv.date_ns,
        "Terms": "",
        "Subsidiary": SUBSIDIARY,
        "Currency": inv.currency,
        "Location": LOCATION,
        "Header Memo": header_memo,
        "Item": ITEM,
        "Quantity": "1",
        "Class": "",
        "Department": "",
        "Tax Code": "",
    }

    rows = []
    for brand in BRAND_ORDER:
        amount = totals.get(brand, ZERO)
        if amount == ZERO:
            continue
        rows.append({**base,
                     "Description": f"Amazon Advertising {BRAND_MEMO_LABEL[brand]} "
                                    f"{inv.period_label}",
                     "Class": brand,
                     "Department": BRAND_DEPARTMENT.get(brand, ""),
                     "Tax Code": TAX_CODE,
                     "Rate": fmt(amount)})
    return rows
