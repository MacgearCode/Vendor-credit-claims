"""Orchestration for the Vendor Bill Importer: uploads in, review rows out.

Routes call this; it calls `bill_pdf` for extraction, `bill_calc` for the
numbers and the DB for the duplicate guard. No arithmetic lives here.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import bill_calc, bill_pdf
from .bill_calc import ZERO, Invoice, InvoiceParseError
from .models import ProcessedInvoice


def _sydney_date(dt) -> str:
    """SQLite timestamps are UTC and so is the droplet, which reads a day early
    for most of the Sydney working day."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Australia/Sydney")).strftime("%d/%m/%Y")


@dataclass
class InvoiceResult:
    """One invoice as shown in the review table."""
    invoice: Invoice
    totals: dict[str, Decimal]
    already_exported: str = ""   # the earlier export date, or "" if new

    @property
    def netsuite_tax(self) -> Decimal:
        return bill_calc.netsuite_tax(self.totals)

    @property
    def netsuite_total(self) -> Decimal:
        return sum(self.totals.values(), ZERO) + self.netsuite_tax

    @property
    def split(self) -> list[tuple[str, Decimal]]:
        return [(b, v) for b, v in self.totals.items() if v != ZERO]


@dataclass
class BillRun:
    """The outcome of one upload. `failures` are files that could not be read;
    they do not stop the rest from being processed."""
    results: list[InvoiceResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)

    @property
    def warning_count(self) -> int:
        return sum(len(r.invoice.warnings) for r in self.results)

    @property
    def duplicate_count(self) -> int:
        return sum(1 for r in self.results if r.already_exported)

    @property
    def grand_total(self) -> Decimal:
        return sum((r.invoice.total_inc_tax for r in self.results), ZERO)


def analyse(uploads: list[tuple[str, bytes]], db: Session) -> BillRun:
    """Parse every uploaded PDF and build the NetSuite rows."""
    run = BillRun()
    pdfs = bill_pdf.collect_pdfs(uploads)
    if len(pdfs) > bill_pdf.MAX_FILES:
        run.failures.append(
            f"{len(pdfs)} PDFs uploaded; the limit is {bill_pdf.MAX_FILES}. "
            f"Split the batch."
        )
        return run

    for name, data in pdfs:
        try:
            text = bill_pdf.extract_text(data)
            inv = bill_calc.parse_invoice_text(text, name)
        except InvoiceParseError as exc:
            run.failures.append(str(exc))
            continue
        except Exception as exc:                      # a corrupt or non-PDF file
            run.failures.append(f"{name}: could not be read ({exc})")
            continue

        totals = bill_calc.allocate(inv)
        bill_calc.validate(inv, totals)

        prior = db.scalar(
            select(ProcessedInvoice).where(ProcessedInvoice.invoice_number == inv.number)
        )
        run.results.append(InvoiceResult(
            invoice=inv,
            totals=totals,
            already_exported=(_sydney_date(prior.exported_at) if prior else ""),
        ))
        run.rows.extend(bill_calc.rows_for(inv, totals))

    return run


def payload_for(run: BillRun, csv_text: str) -> str:
    """Everything the download route needs, as one base64 form field.

    Keeps the CSV and the log entries together without a temp file to clean up
    or a server-side session to expire. A run is a few KB.
    """
    data = {
        "csv": csv_text,
        "invoices": [
            {
                "number": r.invoice.number,
                "date": r.invoice.date_ns,
                "period": r.invoice.period_label,
                "currency": r.invoice.currency,
                "ex_tax": str(r.invoice.subtotal_ex_tax),
                "tax": str(r.invoice.tax_amount),
                "total": str(r.invoice.total_inc_tax),
                "split": {b: str(v) for b, v in r.split},
                "warnings": r.invoice.warnings,
            }
            for r in run.results
        ],
    }
    return base64.b64encode(json.dumps(data).encode()).decode()


def read_payload(encoded: str) -> dict:
    return json.loads(base64.b64decode(encoded.encode()).decode())


def record_export(payload: dict, db: Session) -> None:
    """Log each invoice in the run. Idempotent -- re-downloading the same run
    updates the existing entry rather than failing on the unique constraint."""
    for item in payload.get("invoices", []):
        row = db.scalar(
            select(ProcessedInvoice).where(ProcessedInvoice.invoice_number == item["number"])
        )
        if row is None:
            row = ProcessedInvoice(invoice_number=item["number"])
            db.add(row)
        row.invoice_date = item["date"]
        row.period_label = item["period"]
        row.currency = item["currency"]
        row.subtotal_ex_tax = float(item["ex_tax"])
        row.tax_amount = float(item["tax"])
        row.total_inc_tax = float(item["total"])
        row.brand_split = item["split"]
        row.warnings = item["warnings"]
    db.commit()


def history(db: Session, limit: int = 200) -> list[ProcessedInvoice]:
    return list(db.scalars(
        select(ProcessedInvoice)
        .order_by(ProcessedInvoice.exported_at.desc(), ProcessedInvoice.id.desc())
        .limit(limit)
    ))
