"""Promo grid: pasted product lists, per-product dates and the product rows.

parse_paste takes what people copy out of Excel; build_promo_view turns the
per-retailer lines into one row per product, each line counting its own weeks.
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.promo_data import parse_paste
from app.service import build_promo_view


def test_parse_paste_excel_columns():
    rows, bad = parse_paste("ELK-80150-R\t20\nELK-80151-R\t20%\r\nELK-80152-R\t0.25\n\nELK-80194-R\n")
    assert rows == [("ELK-80150-R", 20.0), ("ELK-80151-R", 20.0),
                    ("ELK-80152-R", 25.0), ("ELK-80194-R", None)]
    assert bad == []


def test_parse_paste_separators_dupes_and_bad_values():
    rows, bad = parse_paste("A1, 15\nB2 30\nA1\t50\nC3\tlots\nD4\t140")
    assert rows == [("A1", 15.0), ("B2", 30.0)]          # A1 again: first row wins
    assert bad == ["C3\tlots", "D4\t140"]


def _line(id, code, pct=0.2, avg=10.0, start=None, end=None):
    return SimpleNamespace(
        id=id, product_code=code, description=f"desc {code}", rrp_inc=110.0,
        retailer_buy_ex=50.0, pct_off=pct, avg_6wk=avg, actual_sales=None,
        support_basis="pct_off", target_margin=None, cogs_supplier_pct=None, cogs_mg_pct=None,
        ratio_supplier=None, ratio_mg=None, ratio_retailer=None, growth=None,
        start_date=start, end_date=end)


def _promo(retailers):
    return SimpleNamespace(
        start_date=date(2026, 11, 12), end_date=date(2026, 12, 2), country="AU",
        aud_usd_rate=0.65, ratio_supplier=0.5, ratio_mg=0.5, ratio_retailer=0.0,
        growth_default=0.0, retailers=retailers)


def _block(id, name, lines):
    return SimpleNamespace(id=id, retailer_name=name, rebate=0.0, lines=lines,
                           customer_claim=None)


def test_product_rows_and_own_weeks():
    # B2 runs 19/11-30/11 (12 days) on both retailers; A1 follows the promo (21 days).
    ow = _block(1, "Officeworks", [_line(1, "A1"), _line(3, "B2", start=date(2026, 11, 19),
                                                           end=date(2026, 11, 30))])
    jb = _block(2, "JB HIFI", [_line(2, "b2", start=date(2026, 11, 19), end=date(2026, 11, 30))])
    pv = build_promo_view(_promo([ow, jb]))

    assert [r.key for r in pv.products] == ["A1", "B2"]  # first-added order, case-blind
    a1, b2 = pv.products
    assert set(a1.cells) == {1} and set(b2.cells) == {1, 2}
    assert not a1.own_dates and b2.own_dates
    assert (b2.start, b2.end) == (date(2026, 11, 19), date(2026, 11, 30))

    assert a1.cells[1].weeks == 3.0                      # 21 days
    assert b2.cells[2].weeks == 12 / 7
    # expected sales = ceil(avg x (1+growth)) x the product's own weeks
    assert a1.cells[1].result.expected_sales == 10 * 3.0
    assert b2.cells[2].result.expected_sales == 10 * 12 / 7
