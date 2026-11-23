"""Brand Profitability storage — the `brand_profit` schema in Postgres (built 2026-11-17).

Holds what the CYA workbook hard-coded and the finance team pasted by hand:

  rate_rule   effective-dated percentages, by kind:
                base       head-office customer's rebate             (customer_id = head office)
                brand      customer x brand exception, added to base (customer_id = head office;
                           item_category '' = every category, else only that dim_item.category)
                store      store extra, added to base                (customer_id = the store)
                sku        customer x item code exception, added     (customer_id = head office;
                           item_code = NetSuite item code; the NZ CYA's "SKU rebate")
                mdf        brand MDF %, optionally for one customer  (customer_id NULL = default)
                gp_target  brand's target for net profit % on gross  (no customer)
  brand_pm    brand -> product manager, effective-dated
  pm_team     PM -> the senior PM they report to, effective-dated   (app/portfolio.py)
  am_assignment  region x retailer x brand ('' = all) -> account manager, overriding
              NetSuite's customer rep                               (app/portfolio.py)
  gl_account  which NetSuite GL accounts are freight and which are vendor rebates
  gl_batch /  freight and vendor-rebate GL lines that came in by seed or upload. They are
  gl_line     NetSuite lines (the upload takes the saved search's own CSV). An upload replaces
              the region's earlier lines for every month it contains (they are kept, marked
              superseded_by); once the NetSuite extract covers a month, it replaces all of
              these for that month (see data.py).
  manual_adjustment  freight / vendor-rebate amounts finance adds by hand that are NOT in the
              NetSuite GL (the CYA's "RPP only (Non-GL posting)" rows: customer-rebate accruals,
              internal MDF accruals). Entered row by row on the settings page; always added on top
              of whichever GL source a month uses, never replaced by it.
  document    supporting PDFs (a customer contract behind a rate change, …): the file lives in
              DigitalOcean Spaces (app/doc_store.py), this row says which rule it backs, with a
              snapshot of the rule as it read at upload, its size and SHA-256. Never deleted —
              it outlives the rule (audit).
  change_log  every add / edit / delete: who, when, before -> after (append-only)

Percentages are stored as fractions (0.19 = 19%). Months are DATE, the 1st. A rule applies to
months valid_from .. valid_to inclusive (valid_to NULL = open-ended). Rules with the same key
may not overlap (enforced by `overlaps()` before every write), so a month never has two rates.

Connection: BRAND_PROFIT_DB_URL, else TARGETS_DB_URL (a role that may write its schema; the
site's REPORTING_DB_URL role is read-only). Schema BRAND_PROFIT_SCHEMA (default brand_profit;
tests use a throwaway one). Tables are created on the process's first connection.
"""
import json
import os
import re
from datetime import date

import psycopg
from psycopg.rows import dict_row

KINDS = ("base", "brand", "store", "sku", "mdf", "gp_target")
GL_KINDS = ("freight", "vr")
REGIONS = ("AU", "NZ")


class NotConfigured(RuntimeError):
    """Neither BRAND_PROFIT_DB_URL nor TARGETS_DB_URL is set."""


def schema() -> str:
    s = os.environ.get("BRAND_PROFIT_SCHEMA", "brand_profit")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", s):
        raise ValueError(f"bad BRAND_PROFIT_SCHEMA {s!r}")
    return s


def db_url() -> str:
    return os.environ.get("BRAND_PROFIT_DB_URL", "") or os.environ.get("TARGETS_DB_URL", "")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rate_rule (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region        TEXT    NOT NULL CHECK (region IN ('AU', 'NZ')),
    kind          TEXT    NOT NULL CHECK (kind IN ('base', 'brand', 'store', 'mdf', 'gp_target')),
    customer_id   INTEGER,                         -- NetSuite internal id (head office, or store)
    customer_name TEXT    NOT NULL DEFAULT '',     -- as named when entered (display only)
    brand         TEXT    NOT NULL DEFAULT '',     -- upper-case NetSuite class name
    item_category TEXT    NOT NULL DEFAULT '',     -- brand kind only; '' = every category
    pct           NUMERIC(9, 6) NOT NULL,
    valid_from    DATE    NOT NULL,
    valid_to      DATE,
    note          TEXT    NOT NULL DEFAULT '',
    updated_by    TEXT    NOT NULL DEFAULT '',
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS rate_rule_key ON rate_rule (region, kind, customer_id, brand);
-- added 2026-11-17 for the NZ CYA's SKU rebates
ALTER TABLE rate_rule ADD COLUMN IF NOT EXISTS item_code TEXT NOT NULL DEFAULT '';
ALTER TABLE rate_rule DROP CONSTRAINT IF EXISTS rate_rule_kind_check;
ALTER TABLE rate_rule ADD CONSTRAINT rate_rule_kind_check
    CHECK (kind IN ('base', 'brand', 'store', 'sku', 'mdf', 'gp_target'));

CREATE TABLE IF NOT EXISTS brand_pm (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region      TEXT NOT NULL CHECK (region IN ('AU', 'NZ')),
    brand       TEXT NOT NULL,
    pm          TEXT NOT NULL,
    valid_from  DATE NOT NULL,
    valid_to    DATE,
    note        TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

-- Portfolios (2026-11-24, app/portfolio.py): who a PM reports to, and account managers set
-- here instead of NetSuite's one rep per customer record
CREATE TABLE IF NOT EXISTS pm_team (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pm          TEXT NOT NULL,                     -- as named in brand_pm
    reports_to  TEXT NOT NULL,                     -- a senior PM
    valid_from  DATE NOT NULL,
    valid_to    DATE,
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from),
    CHECK (pm <> reports_to)
);

CREATE TABLE IF NOT EXISTS am_assignment (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region      TEXT NOT NULL CHECK (region IN ('AU', 'NZ')),
    retailer    TEXT NOT NULL,                     -- Sales Explorer's Retailer (customer group)
    brand       TEXT NOT NULL DEFAULT '',          -- upper-case class name; '' = every brand
    am          TEXT NOT NULL,                     -- NetSuite sales rep name
    valid_from  DATE NOT NULL,
    valid_to    DATE,
    note        TEXT NOT NULL DEFAULT '',
    updated_by  TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to >= valid_from)
);

CREATE TABLE IF NOT EXISTS gl_account (
    region      TEXT    NOT NULL CHECK (region IN ('AU', 'NZ')),
    account     TEXT    NOT NULL,                  -- as the saved search prints it (number / name)
    kind        TEXT    NOT NULL CHECK (kind IN ('freight', 'vr')),
    updated_by  TEXT    NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (region, account)
);

CREATE TABLE IF NOT EXISTS gl_batch (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region      TEXT NOT NULL CHECK (region IN ('AU', 'NZ')),
    source      TEXT NOT NULL CHECK (source IN ('seed', 'upload')),
    filename    TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT '',
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gl_line (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id        BIGINT NOT NULL REFERENCES gl_batch(id) ON DELETE CASCADE,
    region          TEXT   NOT NULL,
    month           DATE   NOT NULL,               -- posting month, the 1st
    tran_date       DATE,
    line_key        TEXT   NOT NULL DEFAULT '',    -- NetSuite Line Unique Key when known
    document_number TEXT   NOT NULL DEFAULT '',
    tran_type       TEXT   NOT NULL DEFAULT '',
    account         TEXT   NOT NULL DEFAULT '',
    brand           TEXT   NOT NULL DEFAULT '',    -- upper-case class name
    vendor          TEXT   NOT NULL DEFAULT '',
    memo            TEXT   NOT NULL DEFAULT '',
    kind            TEXT   NOT NULL CHECK (kind IN ('freight', 'vr')),
    amount          NUMERIC(14, 2) NOT NULL,       -- positive = cost to Macgear
    superseded_by   BIGINT REFERENCES gl_batch(id) ON DELETE SET NULL
                                                   -- a later upload covering the same month
);
CREATE INDEX IF NOT EXISTS gl_line_month ON gl_line (region, month);

CREATE TABLE IF NOT EXISTS manual_adjustment (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region      TEXT   NOT NULL CHECK (region IN ('AU', 'NZ')),
    month       DATE   NOT NULL,                   -- the 1st
    brand       TEXT   NOT NULL,                   -- upper-case class name
    kind        TEXT   NOT NULL CHECK (kind IN ('freight', 'vr')),
    amount      NUMERIC(14, 2) NOT NULL,           -- positive = cost to Macgear (the CYA's sign)
    memo        TEXT   NOT NULL DEFAULT '',
    created_by  TEXT   NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT   NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS manual_adjustment_month ON manual_adjustment (region, month);

CREATE TABLE IF NOT EXISTS document (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    region       TEXT   NOT NULL CHECK (region IN ('AU', 'NZ')),
    rule_table   TEXT   NOT NULL DEFAULT 'rate_rule',
    rule_id      BIGINT NOT NULL,                  -- not a FK: the document outlives the rule
    rule_label   TEXT   NOT NULL DEFAULT '',       -- the rule as it read when the file was attached
    filename     TEXT   NOT NULL,
    size_bytes   INTEGER NOT NULL,
    sha256       TEXT   NOT NULL,
    object_key   TEXT   NOT NULL,                  -- in SPACES_BUCKET
    note         TEXT   NOT NULL DEFAULT '',
    uploaded_by  TEXT   NOT NULL DEFAULT '',
    uploaded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS document_rule ON document (rule_table, rule_id);

CREATE TABLE IF NOT EXISTS change_log (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    by_user    TEXT NOT NULL DEFAULT '',
    entity     TEXT NOT NULL,                      -- rate_rule | brand_pm | gl_account | gl_batch
    entity_id  TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,                      -- add | edit | delete | seed | upload
    before     JSONB,
    after      JSONB,
    note       TEXT NOT NULL DEFAULT ''
);
"""

_ready: set = set()


def connect() -> psycopg.Connection:
    url = db_url()
    if not url:
        raise NotConfigured("BRAND_PROFIT_DB_URL / TARGETS_DB_URL is not set")
    s = schema()
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10,
                           options=f"-c search_path={s} -c statement_timeout=60000")
    key = (url, s)
    if key not in _ready:
        with conn.transaction():
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            conn.execute(SCHEMA_SQL)
        _ready.add(key)
    return conn


# ---- helpers shared by the routes and the seed ------------------------------------
def _jsonable(row: dict | None) -> str | None:
    if row is None:
        return None
    return json.dumps({k: (v.isoformat() if hasattr(v, "isoformat") else
                           float(v) if k == "pct" and v is not None else v)
                       for k, v in row.items() if k not in ("updated_at",)}, default=str)


def log(conn, by: str, entity: str, entity_id, action: str, before=None, after=None, note: str = ""):
    conn.execute("INSERT INTO change_log (by_user, entity, entity_id, action, before, after, note) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                 (by, entity, str(entity_id or ""), action, _jsonable(before), _jsonable(after), note))


def overlaps(conn, table: str, key: dict, valid_from: date, valid_to: date | None,
             exclude_id: int | None = None) -> list[dict]:
    """Rows of `table` with the same key whose months overlap [valid_from, valid_to]."""
    if table not in ("rate_rule", "brand_pm", "pm_team", "am_assignment"):
        raise ValueError(table)
    where, params = [], []
    for col, val in key.items():
        if val is None:
            where.append(f"{col} IS NULL")
        else:
            where.append(f"{col} = %s")
            params.append(val)
    where.append("valid_from <= coalesce(%s::date, 'infinity'::date)")
    params.append(valid_to)
    where.append("coalesce(valid_to, 'infinity'::date) >= %s")
    params.append(valid_from)
    if exclude_id is not None:
        where.append("id <> %s")
        params.append(exclude_id)
    return conn.execute(f"SELECT * FROM {table} WHERE " + " AND ".join(where), params).fetchall()


def rule_key(row: dict) -> dict:
    return {"region": row["region"], "kind": row["kind"], "customer_id": row.get("customer_id"),
            "brand": row.get("brand", ""), "item_category": row.get("item_category", ""),
            "item_code": row.get("item_code", "")}


def load_rules(conn, region: str) -> list[dict]:
    return conn.execute("SELECT * FROM rate_rule WHERE region = %s", (region,)).fetchall()


def load_pms(conn, region: str) -> list[dict]:
    return conn.execute("SELECT * FROM brand_pm WHERE region = %s", (region,)).fetchall()


def data_version(conn) -> tuple:
    """Changes whenever a setting or GL line changes — part of the report's cache key."""
    return tuple(conn.execute(
        "SELECT (SELECT max(id) FROM change_log), (SELECT count(*) FROM change_log)").fetchone().values())
