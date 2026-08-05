"""NetSuite Vendor Bill import CSV.

Written as pure ASCII with UTF-8 encoding. NetSuite's CSV importer defaults to
Windows-1252, so any non-ASCII value would arrive mangled and fail its lookup --
which is exactly why the location and tax code go as internal IDs.
"""
from __future__ import annotations

import csv
import io

from .bill_calc import CSV_COLUMNS


def csv_text(rows: list[dict[str, str]]) -> str:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def filename_for(invoice_numbers: list[str]) -> str:
    """`NetSuite_VendorBill_AmazonAdvertising_<first>-<last>.csv`, so a download
    sitting in someone's Downloads folder still says what it holds."""
    if not invoice_numbers:
        return "NetSuite_VendorBill_AmazonAdvertising.csv"
    first, last = invoice_numbers[0], invoice_numbers[-1]
    span = first if first == last else f"{first}-{last}"
    return f"NetSuite_VendorBill_AmazonAdvertising_{span}.csv"
