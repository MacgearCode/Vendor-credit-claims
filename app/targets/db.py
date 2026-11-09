"""Targets tool storage — the `targets` schema in the Postgres reporting database
(rebuilt 2026-10-22: everything lives in the database, nothing in SQLite files).

Connection: TARGETS_DB_URL, a role that owns / may write the `targets` schema (the
site's REPORTING_DB_URL role is read-only). Every connection sets search_path to the
schema (TARGETS_SCHEMA, default "targets"; tests use a throwaway one), so the SQL
below and in calc / routes uses unqualified table names. The schema and tables are
created on the first connection of the process — the role needs CREATE on the
database (or the schema made for it once by an admin; see Reporting Site/CLAUDE.md).

Actuals are NOT stored here: they come from NetSuite sales in the warehouse
(actuals.py). A scenario points at a financial year of them by its end year
(2026 = FY26 = Apr 2025-Mar 2026).
"""
import os
import re

import psycopg
from psycopg.rows import dict_row

# Fiscal-year month order: index 0..11 == Apr..Mar
FY_MONTHS = ["Apr", "May", "Jun", "Jul", "Aug", "Sep",
             "Oct", "Nov", "Dec", "Jan", "Feb", "Mar"]


class NotConfigured(RuntimeError):
    """TARGETS_DB_URL isn't set."""


def schema() -> str:
    s = os.environ.get("TARGETS_SCHEMA", "targets")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", s):
        raise ValueError(f"bad TARGETS_SCHEMA {s!r}")
    return s


SCHEMA_SQL = """
-- One budgeting scenario. actuals_fy / ref_fy are fiscal END years (2026 = FY26).
CREATE TABLE IF NOT EXISTS scenario (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name               TEXT        NOT NULL,
    target_fy          TEXT        NOT NULL DEFAULT 'FY27',
    default_growth_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    actuals_fy         INTEGER,                 -- the year of NetSuite actuals it's built on
    projection_mode    TEXT,                    -- NULL | 'flat' | 'tracking'
    ref_fy             INTEGER,                 -- reference year for a projection
    projection_union   BOOLEAN     NOT NULL DEFAULT false,
    created_by         TEXT        NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Growth overrides. level 'brand' (customer = '') or 'customer'.
CREATE TABLE IF NOT EXISTS growth_override (
    scenario_id BIGINT NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    level       TEXT   NOT NULL,
    country     TEXT   NOT NULL,
    brand       TEXT   NOT NULL,
    customer    TEXT   NOT NULL DEFAULT '',
    growth_pct  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (scenario_id, level, country, brand, customer)
);

-- Pacing overrides: 12 weights per (level, country, brand); global -> brand = ''.
CREATE TABLE IF NOT EXISTS pacing_override (
    scenario_id BIGINT  NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    level       TEXT    NOT NULL,
    country     TEXT    NOT NULL,
    brand       TEXT    NOT NULL DEFAULT '',
    month_idx   INTEGER NOT NULL,
    weight      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (scenario_id, level, country, brand, month_idx)
);

-- Brands hidden from a scenario (reversible; actuals untouched).
CREATE TABLE IF NOT EXISTS brand_exclusion (
    scenario_id BIGINT NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    country     TEXT   NOT NULL,
    brand       TEXT   NOT NULL,
    PRIMARY KEY (scenario_id, country, brand)
);

-- Brands launched part-way through the base year, scaled to a full-year run-rate.
CREATE TABLE IF NOT EXISTS annualize (
    scenario_id BIGINT NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    country     TEXT   NOT NULL,
    brand       TEXT   NOT NULL,
    PRIMARY KEY (scenario_id, country, brand)
);

-- Lines with no history (new brands / customers): an annual target, split by pacing
-- unless `months` (JSON array of 12 $) is set.
CREATE TABLE IF NOT EXISTS manual_line (
    scenario_id   BIGINT NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    country       TEXT   NOT NULL,
    brand         TEXT   NOT NULL,
    customer      TEXT   NOT NULL,
    target_annual DOUBLE PRECISION NOT NULL DEFAULT 0,
    months        TEXT,
    PRIMARY KEY (scenario_id, country, brand, customer)
);

-- Committed (official) targets: "Commit" freezes a scenario's computed grid. Never
-- edited; the newest commit per target_fy is the official one (sales_target below).
CREATE TABLE IF NOT EXISTS target_commit (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    target_fy     TEXT        NOT NULL,
    scenario_id   BIGINT,                      -- the scenario may be deleted later
    scenario_name TEXT        NOT NULL,
    note          TEXT        NOT NULL DEFAULT '',
    committed_by  TEXT        NOT NULL DEFAULT '',
    committed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_au      NUMERIC(16,2) NOT NULL DEFAULT 0,
    total_nz      NUMERIC(16,2) NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS committed_target (
    commit_id   BIGINT  NOT NULL REFERENCES target_commit(id) ON DELETE CASCADE,
    country     TEXT    NOT NULL,              -- 'Australia' | 'New Zealand'
    brand       TEXT    NOT NULL,
    customer    TEXT    NOT NULL,
    month_start DATE    NOT NULL,              -- first day of the calendar month
    amount      NUMERIC(16,2) NOT NULL,        -- the country's own currency, ex-GST
    PRIMARY KEY (commit_id, country, brand, customer, month_start)
);

-- Periods (2026-10-22): a scenario plans a span of fiscal months (0 = April .. 11 = March),
-- e.g. Q4 = 9..11, and a commit covers only its span - other months keep their targets.
ALTER TABLE scenario ADD COLUMN IF NOT EXISTS period_start INTEGER NOT NULL DEFAULT 0;
ALTER TABLE scenario ADD COLUMN IF NOT EXISTS period_end   INTEGER NOT NULL DEFAULT 11;
ALTER TABLE target_commit ADD COLUMN IF NOT EXISTS period_start INTEGER NOT NULL DEFAULT 0;
ALTER TABLE target_commit ADD COLUMN IF NOT EXISTS period_end   INTEGER NOT NULL DEFAULT 11;

-- Starting points (2026-10-23). A scenario plans `target_fy` from one of:
--   last_year  NetSuite sales of the year before (base_kind 'actuals')
--   this_year  this year's finished months + the rest projected from last year
--              (base_kind 'actuals', projection on)
--   official   a frozen copy of the official targets, taken when it was created
--              (base_kind 'snapshot', rows in base_line) - growth applies on top
--   import     a targets workbook (base_kind 'snapshot' too)
--   blank      nothing (manual lines only)
ALTER TABLE scenario ADD COLUMN IF NOT EXISTS start_from TEXT;
ALTER TABLE scenario ADD COLUMN IF NOT EXISTS base_kind  TEXT NOT NULL DEFAULT 'actuals';
ALTER TABLE scenario ADD COLUMN IF NOT EXISTS base_label TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS base_line (
    scenario_id BIGINT  NOT NULL REFERENCES scenario(id) ON DELETE CASCADE,
    country     TEXT    NOT NULL,
    brand       TEXT    NOT NULL,
    customer    TEXT    NOT NULL,
    month_idx   INTEGER NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (scenario_id, country, brand, customer, month_idx)
);

-- A commit can cover one country or both (default both).
ALTER TABLE target_commit ADD COLUMN IF NOT EXISTS countries TEXT[] NOT NULL
    DEFAULT ARRAY['Australia', 'New Zealand'];

-- What the rest of the site reads: the official targets, one row per country x brand x
-- customer x month. For each FY, month AND country, the NEWEST commit covering it wins
-- (a Q4 commit replaces Jan-Mar only, and only for the countries it was committed for).
DROP VIEW IF EXISTS sales_target;
CREATE VIEW sales_target AS
WITH covers AS (
    SELECT t.id, t.target_fy, m.idx, ctry
    FROM target_commit t
    CROSS JOIN LATERAL generate_series(t.period_start, t.period_end) AS m(idx)
    CROSS JOIN LATERAL unnest(t.countries) AS ctry
), official AS (
    SELECT target_fy, idx, ctry, max(id) AS commit_id FROM covers GROUP BY 1, 2, 3
)
SELECT CASE c.country WHEN 'Australia' THEN 'AU' WHEN 'New Zealand' THEN 'NZ' END AS country_code,
       c.month_start, c.brand, c.customer, c.amount, t.target_fy, t.id AS commit_id
FROM committed_target c
JOIN target_commit t ON t.id = c.commit_id
JOIN official o ON o.commit_id = c.commit_id AND o.target_fy = t.target_fy AND o.ctry = c.country
               AND o.idx = (extract(month FROM c.month_start)::int + 8) % 12;
"""

_ready: set = set()


def connect() -> psycopg.Connection:
    url = os.environ.get("TARGETS_DB_URL", "")
    if not url:
        raise NotConfigured("TARGETS_DB_URL is not set")
    s = schema()
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10,
                           options=f"-c search_path={s} -c statement_timeout=60000")
    key = (url, s)
    if key not in _ready:
        with conn.transaction():
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            conn.execute(SCHEMA_SQL)
            # the read-only site role may read the official targets (home page)
            conn.execute(f"""DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporting_read') THEN
                    GRANT USAGE ON SCHEMA {s} TO reporting_read;
                    GRANT SELECT ON {s}.sales_target TO reporting_read;
                END IF; END $$""")
        _ready.add(key)
    return conn
