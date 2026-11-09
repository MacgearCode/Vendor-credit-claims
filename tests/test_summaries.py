"""Summary tables (Sell Through/db/13_summaries.sql): a build reads a summary only when
the warehouse says it's fresh, and otherwise its original query - so a stale or missing
summary can slow a page, never change its numbers."""
from datetime import date

import psycopg
import pytest

from app import sell_through, st_weekly, warehouse


class FakeCursor:
    def __init__(self, fresh=(), fail=None, rows=()):
        self.fresh, self.fail, self.rows = list(fresh), fail, list(rows)
        self.sql, self.connection = [], self
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "fresh_summaries" in sql:
            if self.fail:
                raise self.fail
            self._out = [(n,) for n in self.fresh]
        else:
            self._out = self.rows

    def fetchall(self):
        return self._out

    def rollback(self):
        self.rolled_back = True

    # `with connect() as conn, conn.cursor() as cur`
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fresh_summaries_lists_what_the_warehouse_says():
    assert warehouse.fresh_summaries(FakeCursor(fresh=["agg_sales_day"])) == {"agg_sales_day"}


def test_no_summaries_in_this_warehouse_means_none_and_the_transaction_is_reset():
    cur = FakeCursor(fail=psycopg.errors.UndefinedFunction("no such function"))
    assert warehouse.fresh_summaries(cur) == set()
    assert cur.rolled_back                       # later queries on the connection still work


def test_switch_off_never_asks(monkeypatch):
    monkeypatch.setenv("REPORTING_SUMMARIES", "0")
    cur = FakeCursor(fresh=["agg_sales_day"])
    assert warehouse.fresh_summaries(cur) == set() and cur.sql == []


@pytest.mark.parametrize("fresh,expect", [(["agg_sales_day"], "FAST"), ([], "SLOW"),
                                          (["agg_sales_month"], "SLOW")])
def test_query_summary_reads_the_summary_only_when_fresh(monkeypatch, fresh, expect):
    cur = FakeCursor(fresh=fresh, rows=[(1,)])
    monkeypatch.setattr(warehouse, "connect", lambda: cur)
    assert warehouse.query_summary("agg_sales_day", "FAST", "SLOW") == [(1,)]
    assert cur.sql[-1] == expect


def test_fast_queries_really_read_the_summaries():
    # they're built by replacing text in the originals: a missed match would silently
    # leave the source view in place (slow) or a mismatched measure (wrong)
    for sql in (st_weekly._ST_SQL_FAST, sell_through._FACT_SQL_FAST):
        assert "agg_sell_through_week" in sql and "fact_sell_through" not in sql
        assert "sales_value_rrp" in sql and "qty_sold * f.rrp" not in sql
    assert "agg_sell_in_week" in st_weekly._SI_SQL_FAST
    assert sell_through._FACT_SQL.rstrip().endswith("order by 1, 2, 3, 6")
    assert sell_through._FACT_SQL_FAST.rstrip().endswith("order by 1, 2, 3, 6")
    # same row order whichever answers: the page lists items in arrival order
    assert st_weekly._ST_SQL.rstrip().endswith("order by f.region_key, f.item_key, f.customer_key, f.date_key")
    assert st_weekly._ST_SQL_FAST.rstrip().endswith("order by f.region_key, f.item_key, f.customer_key, f.date_key")
    assert st_weekly._SI_SQL.rstrip().endswith("order by 1, 2, 5, 6, 7")
    assert st_weekly._SI_SQL_FAST.rstrip().endswith("order by 1, 2, 5, 6, 7")


def test_weekly_sell_in_falls_back_unless_the_weeks_end_on_sunday(monkeypatch):
    fresh = ["agg_sell_through_week", "agg_sell_in_week"]
    for last, uses_summary in ((date(2026, 11, 8), True), (date(2026, 11, 7), False)):   # Sun, Sat
        cur = FakeCursor(fresh=fresh)
        monkeypatch.setattr(warehouse, "connect", lambda: cur)
        monkeypatch.setattr(warehouse, "recent_weeks", lambda n, upto=None: [date(2026, 10, 18), last])
        monkeypatch.setattr(warehouse, "region_key", lambda code: {"AU": 1, "NZ": 2}[code])
        monkeypatch.setattr(st_weekly, "store_retailers", lambda brands, rg: [])
        st_weekly.build(None, ["AU"], True)
        assert (st_weekly._SI_SQL_FAST in cur.sql) is uses_summary
        assert (st_weekly._SI_SQL in cur.sql) is not uses_summary
