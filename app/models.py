"""SQLAlchemy ORM models for the vendor credit claims tool.

Hierarchy:
    Promotion  (one brand/vendor, a date range, a default funding split)
      └─ PromoRetailer   (one retailer/customer block within the promo)
           ├─ LineItem        (a SKU on promo for that retailer)
           └─ CustomerClaim   (the inbound claim from that customer)
      └─ VendorRequest        (the outbound credit request to the brand, one per promo)

Reference data: Product (master price list) and Retailer (default rebates).
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (Boolean, Date, DateTime, Float, ForeignKey, Integer,
                        String, Text, JSON, UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("code", "country", name="uq_product_code_country"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String, index=True)
    country: Mapped[str] = mapped_column(String, default="AU", index=True)  # AU / NZ
    description: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="", index=True)
    status: Mapped[str] = mapped_column(String, default="")
    rrp_inc: Mapped[float | None] = mapped_column(Float, nullable=True)
    channel_prices: Mapped[dict] = mapped_column(JSON, default=dict)


class Retailer(Base):
    __tablename__ = "retailers"
    __table_args__ = (UniqueConstraint("name", "country", name="uq_retailer_name_country"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, index=True)
    country: Mapped[str] = mapped_column(String, default="AU", index=True)  # AU / NZ
    default_rebate: Mapped[float] = mapped_column(Float, default=0.0)


class Promotion(Base):
    __tablename__ = "promotions"
    id: Mapped[int] = mapped_column(primary_key=True)
    claim_number: Mapped[str] = mapped_column(String, default="", index=True)
    name: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="", index=True)
    country: Mapped[str] = mapped_column(String, default="AU", index=True)  # AU / NZ
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    aud_usd_rate: Mapped[float] = mapped_column(Float, default=0.65)
    # default 3-way funding split (sum should be ~1.0)
    ratio_retailer: Mapped[float] = mapped_column(Float, default=0.333)
    ratio_mg: Mapped[float] = mapped_column(Float, default=0.333)
    ratio_supplier: Mapped[float] = mapped_column(Float, default=0.333)
    growth_default: Mapped[float] = mapped_column(Float, default=0.2)
    # default support basis for new lines: "pct_off" | "margin" | "cogs" (+ defaults)
    support_basis_default: Mapped[str] = mapped_column(String, default="pct_off")
    target_margin_default: Mapped[float | None] = mapped_column(Float, nullable=True)
    cogs_supplier_pct_default: Mapped[float | None] = mapped_column(Float, nullable=True)
    cogs_mg_pct_default: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String, default="Draft")  # Draft/Sent/Live/Closed
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    retailers: Mapped[list[PromoRetailer]] = relationship(
        back_populates="promotion", cascade="all, delete-orphan")
    vendor_request: Mapped[VendorRequest | None] = relationship(
        back_populates="promotion", cascade="all, delete-orphan", uselist=False)


class PromoRetailer(Base):
    """One retailer/customer block within a promotion."""
    __tablename__ = "promo_retailers"
    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promotions.id"))
    retailer_name: Mapped[str] = mapped_column(String)
    rebate: Mapped[float] = mapped_column(Float, default=0.0)  # rebate for this block

    promotion: Mapped[Promotion] = relationship(back_populates="retailers")
    lines: Mapped[list[LineItem]] = relationship(
        back_populates="promo_retailer", cascade="all, delete-orphan")
    customer_claim: Mapped[CustomerClaim | None] = relationship(
        back_populates="promo_retailer", cascade="all, delete-orphan", uselist=False)
    attachments: Mapped[list[Attachment]] = relationship(
        back_populates="promo_retailer", cascade="all, delete-orphan")


class LineItem(Base):
    __tablename__ = "line_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    promo_retailer_id: Mapped[int] = mapped_column(ForeignKey("promo_retailers.id"))
    product_code: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(String, default="")
    rrp_inc: Mapped[float | None] = mapped_column(Float, nullable=True)
    retailer_buy_ex: Mapped[float] = mapped_column(Float, default=0.0)
    pct_off: Mapped[float] = mapped_column(Float, default=0.0)
    avg_6wk: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_sales: Mapped[float | None] = mapped_column(Float, nullable=True)  # qty actually claimed
    # support basis: "pct_off" (discount, default) | "margin" (target margin) | "cogs" (% off cost)
    support_basis: Mapped[str] = mapped_column(String, default="pct_off")
    target_margin: Mapped[float | None] = mapped_column(Float, nullable=True)  # fraction, margin mode
    cogs_supplier_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # fraction, cogs mode
    cogs_mg_pct: Mapped[float | None] = mapped_column(Float, nullable=True)        # fraction, cogs mode
    # per-line funding overrides (null -> inherit promo defaults)
    ratio_supplier: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratio_mg: Mapped[float | None] = mapped_column(Float, nullable=True)
    ratio_retailer: Mapped[float | None] = mapped_column(Float, nullable=True)
    growth: Mapped[float | None] = mapped_column(Float, nullable=True)

    promo_retailer: Mapped[PromoRetailer] = relationship(back_populates="lines")


class CustomerClaim(Base):
    """Inbound claim from a customer for their block of a promo."""
    __tablename__ = "customer_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    promo_retailer_id: Mapped[int] = mapped_column(ForeignKey("promo_retailers.id"))
    amount_claimed: Mapped[float | None] = mapped_column(Float, nullable=True)
    claim_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # see app/workflow.py CLAIM_STATUSES
    status: Mapped[str] = mapped_column(String, default="Awaiting Claim")
    notes: Mapped[str] = mapped_column(Text, default="")

    promo_retailer: Mapped[PromoRetailer] = relationship(back_populates="customer_claim")


class VendorRequest(Base):
    """Outbound credit request to the brand/vendor for the whole promo."""
    __tablename__ = "vendor_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promotions.id"))
    amount_aud: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Not Sent / Sent / Approved / Credited
    status: Mapped[str] = mapped_column(String, default="Not Sent")
    claim_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    promotion: Mapped[Promotion] = relationship(back_populates="vendor_request")


class User(Base):
    """A person who can sign in. Tool access is resolved through app/perms.py.

    Passwords are never set by an admin — a new user gets a single-use link and
    chooses their own, so nobody but the account holder ever knows it. Only the
    token's SHA-256 digest is stored, never the raw token.
    """
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, default="")
    password_hash: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="user")          # admin|user|vendor
    # JSON list of area keys (reports + tools); NULL means "use the role default" — see perms.py
    allowed_tools: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Data scope — JSON lists; see perms.effective_brands / effective_regions.
    # NULL brands = every brand for staff, but NO brand for a vendor.
    allowed_brands: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_regions: Mapped[str | None] = mapped_column(Text, nullable=True)
    company: Mapped[str] = mapped_column(String, default="")           # vendor's company, display only
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reset_token_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProcessedInvoice(Base):
    """A vendor invoice that has been converted to a NetSuite import CSV.

    Belongs to the Vendor Bill Importer tool, not to the promo hierarchy above.
    Its only job is the duplicate guard: External ID on the NetSuite import is
    the invoice number, so re-importing one that has already been billed is
    rejected by NetSuite. Better to say so before the file is downloaded than to
    let someone find out on the Import Job Status page.
    """
    __tablename__ = "processed_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str] = mapped_column(String, unique=True, index=True)
    invoice_date: Mapped[str] = mapped_column(String, default="")   # dd/mm/yyyy, as imported
    period_label: Mapped[str] = mapped_column(String, default="")
    currency: Mapped[str] = mapped_column(String, default="")
    subtotal_ex_tax: Mapped[float] = mapped_column(Float, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)
    total_inc_tax: Mapped[float] = mapped_column(Float, default=0.0)
    brand_split: Mapped[dict] = mapped_column(JSON, default=dict)   # {class: amount}
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    exported_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # Set by hand once the CSV has actually gone into NetSuite, so the log
    # distinguishes "downloaded" from "posted".
    imported: Mapped[bool] = mapped_column(Boolean, default=False)


class Attachment(Base):
    """A file uploaded against a customer/retailer block (e.g. the claim document)."""
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    promo_retailer_id: Mapped[int] = mapped_column(ForeignKey("promo_retailers.id"))
    filename: Mapped[str] = mapped_column(String)        # original name (for display/download)
    stored_name: Mapped[str] = mapped_column(String)     # unique name on disk
    content_type: Mapped[str] = mapped_column(String, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    promo_retailer: Mapped[PromoRetailer] = relationship(back_populates="attachments")
