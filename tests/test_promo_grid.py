"""Promo grid: pasted product lists, per-product dates and the product rows.

parse_paste takes what people copy out of Excel; build_promo_view turns the
per-retailer lines into one row per product, each line counting its own weeks.
"""
import os
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.export import grouped_lines
from app.promo_data import parse_paste
from app.service import build_promo_view


def test_parse_paste_excel_columns():
    rows, bad, breaks, dates = parse_paste("ELK-80150-R\t20\nELK-80151-R\t20%\r\nELK-80152-R\t0.25\n\nELK-80194-R\n")
    assert rows == [("ELK-80150-R", 20.0), ("ELK-80151-R", 20.0),
                    ("ELK-80152-R", 25.0), ("ELK-80194-R", None)]
    assert bad == []
    assert breaks == {"ELK-80152-R"}                    # the blank row after it


def test_parse_paste_blank_rows_from_excel():
    # Excel copies an empty row as tabs; leading / trailing / repeated blanks don't count
    text = "\t\t\nA1\t20\nB2\t20\n\t\t\n\t\t\nC3\t15\n\nD4\n\t\n"
    rows, bad, breaks, dates = parse_paste(text)
    assert [c for c, _ in rows] == ["A1", "B2", "C3", "D4"]
    assert breaks == {"B2", "C3"}


def test_parse_paste_separators_dupes_and_bad_values():
    rows, bad, breaks, dates = parse_paste("A1, 15\nB2 30\nA1\t50\nC3\tLaptop stand\nD4\t140")
    assert rows == [("A1", 15.0), ("B2", 30.0), ("C3", None)]   # A1 again: first row wins;
    assert bad == ["D4\t140"]                                   # C3's 2nd cell is a description


def test_parse_paste_rows_from_the_excel_promo_form():
    text = ("From\tTo\tCode\tProduct Description\n"
            "12/11/2026\t2/12/2026\tELK-80150-R\tBONELK Vision 6-in-1, Hub\tELK80150R\t843209\t\t42.77\n"
            "\t\t\t\n"
            "19/11/2026\t30/11/2026\tELK-70411-R\tBONELK Stand\n"
            "1/12/2026\t20/11/2026\tELK-70419-R\n")
    rows, bad, breaks, dates = parse_paste(text)
    assert rows == [("ELK-80150-R", None), ("ELK-70411-R", None)]   # header skipped; no % off guessed
    assert dates == {"ELK-80150-R": (date(2026, 11, 12), date(2026, 12, 2)),
                     "ELK-70411-R": (date(2026, 11, 19), date(2026, 11, 30))}
    assert breaks == {"ELK-80150-R"}
    assert bad == ["1/12/2026\t20/11/2026\tELK-70419-R"]          # To before From


def test_parse_date_text_day_first():
    from app.promo_data import parse_date_text as d
    assert d("2/12/2026") == date(2026, 12, 2) and d("12-Nov-26") == date(2026, 11, 12)
    assert d("2026-11-12 00:00:00") == date(2026, 11, 12)
    assert d("31/02/2026") is None and d("ELK-80150-R") is None and d("20") is None


def _line(id, code, pct=0.2, avg=10.0, start=None, end=None):
    return SimpleNamespace(
        id=id, product_code=code, description=f"desc {code}", rrp_inc=110.0,
        retailer_buy_ex=50.0, pct_off=pct, avg_6wk=avg, actual_sales=None,
        support_basis="pct_off", target_margin=None, cogs_supplier_pct=None, cogs_mg_pct=None,
        ratio_supplier=None, ratio_mg=None, ratio_retailer=None, growth=None,
        start_date=start, end_date=end, position=None)


def _promo(retailers):
    return SimpleNamespace(
        start_date=date(2026, 11, 12), end_date=date(2026, 12, 2), country="AU",
        aud_usd_rate=0.65, ratio_supplier=0.5, ratio_mg=0.5, ratio_retailer=0.0,
        growth_default=0.0, retailers=retailers, vendor_request=None)


def _block(id, name, lines, growth=None):
    return SimpleNamespace(id=id, retailer_name=name, rebate=0.0, lines=lines,
                           customer_claim=None, growth=growth,
                           ratio_supplier=None, ratio_mg=None, ratio_retailer=None)


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


def _claim_promo(actuals, vendor_amount=None):
    lines = [_line(i + 1, f"C{i}", avg=10.0) for i in range(len(actuals))]
    for l, a in zip(lines, actuals):
        l.actual_sales = a
    promo = _promo([_block(1, "Officeworks", lines)])
    if vendor_amount is not None:
        promo.vendor_request = SimpleNamespace(amount_aud=vendor_amount, amount_usd=None)
    return build_promo_view(promo)


def test_claim_follows_actual_sales():
    # no actuals yet: the vendor claim is the expected one
    pv = _claim_promo([None, None])
    assert pv.claim_basis == "expected" and pv.claim_aud == pv.supplier_total_aud
    # actuals on one line: the claim is actual sales only; the other line counts 0
    pv = _claim_promo([7, None])
    support = pv.retailers[0].lines[0].result.supplier_support
    assert pv.claim_basis == "actual"
    assert pv.claim_aud == 7 * support == pv.actual_supplier_aud
    assert pv.lines_without_actuals == 1
    assert pv.claim_usd == pv.claim_aud * 0.65


def test_typed_vendor_amount_overrides():
    pv = _claim_promo([7, 3], vendor_amount=123.45)
    assert pv.claim_basis == "override"
    assert pv.claim_aud == 123.45 and pv.claim_usd == 123.45 * 0.65


def test_growth_line_then_customer_then_promo():
    # promo default 0 (fixture), customer 50%, one line with its own 100%
    lines = [_line(1, "A1", avg=10.0), _line(2, "B2", avg=10.0)]
    lines[1].growth = 1.0
    pv = build_promo_view(_promo([_block(1, "JB HIFI", lines, growth=0.5)]))
    a1, b2 = pv.retailers[0].lines
    assert a1.result.expected_sales == 15 * 3.0      # ceil(10 x 1.5) x 3 wks
    assert b2.result.expected_sales == 20 * 3.0      # the line's own growth wins
    pv = build_promo_view(_promo([_block(1, "JB HIFI", [_line(1, "A1", avg=10.0)])]))
    assert pv.retailers[0].lines[0].result.expected_sales == 10 * 3.0   # promo default


def test_export_keeps_grid_order_and_separators():
    ow = _block(1, "Officeworks", [_line(1, "A1"), _line(2, "B2"), _line(4, "D4")])
    jb = _block(2, "JB HIFI", [_line(3, "C3"), _line(5, "D4")])
    promo = _promo([ow, jb])
    # grid: A1 | -- Stands -- | B2, C3 | -- (gap) -- | D4
    promo.separators = [SimpleNamespace(after_key="A1", label="Stands"),
                        SimpleNamespace(after_key="C3", label="")]
    pv = build_promo_view(promo)
    show = lambda pr_id, **kw: [x[1] if isinstance(x, tuple) else x.line.product_code
                                for x in grouped_lines(pv, pr_id, **kw)]
    assert show(1) == ["A1", "Stands", "B2", "", "D4"]   # the gap after C3 still separates B2 / D4
    assert show(2) == ["C3", "", "D4"]                   # no leading separator before its first line
    assert show(1, keep=lambda lv: lv.line.product_code != "B2") == ["A1", "Stands", "D4"]  # last heading wins


def test_find_items_brand_words_and_limit(monkeypatch):
    from app import promo_data
    catalog = [{"code": "T8173C21", "description": "eufyCam E42 Solo", "brand": "EUFY", "rrp_inc": 349.0, "status": "Active"},
               {"code": "E8173C22", "description": "EUFYCam E42 4-Cam Kit", "brand": "EUFY", "rrp_inc": 1599.0, "status": "EOL"},
               {"code": "ELK-70411-R", "description": "BONELK Elevate Junior Stand", "brand": "BONELK", "rrp_inc": 59.0, "status": "Active"}]
    monkeypatch.setattr(promo_data.warehouse, "cached", lambda key, build, ttl=0: catalog)
    codes = lambda *a, **k: [i["code"] for i in promo_data.find_items(*a, **k)[0]]
    assert codes("AU", "", "eufy") == ["T8173C21", "E8173C22"]           # browse a brand
    assert codes("AU", "e42 kit", "EUFY") == ["E8173C22"]                # every word, any order/case
    assert codes("AU", "stand", "") == ["ELK-70411-R"]                   # all brands
    assert codes("AU", "70411") == ["ELK-70411-R"]                       # part of a code
    monkeypatch.setattr(promo_data, "FIND_LIMIT", 1)
    items, total = promo_data.find_items("AU", "", "EUFY")
    assert len(items) == 1 and total == 2


def test_grid_order_follows_position_then_when_added():
    lines = [_line(1, "A1"), _line(2, "B2"), _line(3, "C3")]
    pv = build_promo_view(_promo([_block(1, "OW", lines)]))
    assert [r.key for r in pv.products] == ["A1", "B2", "C3"]          # no positions: as added
    for l, pos in zip(lines, (2.0, 0.0, 1.0)):
        l.position = pos
    pv = build_promo_view(_promo([_block(1, "OW", lines)]))
    assert [r.key for r in pv.products] == ["B2", "C3", "A1"]          # moved


def test_split_line_then_customer_then_promo():
    # promo default 50/50 (fixture); customer 80/20; one line with its own 100/0
    lines = [_line(1, "A1"), _line(2, "B2")]
    lines[1].ratio_supplier, lines[1].ratio_mg, lines[1].ratio_retailer = 1.0, 0.0, 0.0
    blk = _block(1, "OW", lines)
    blk.ratio_supplier, blk.ratio_mg, blk.ratio_retailer = 0.8, 0.2, 0.0
    a1, b2 = build_promo_view(_promo([blk])).retailers[0].lines
    t = a1.result.total_support_unit
    assert round(a1.result.supplier_support, 6) == round(t * 0.8, 6)
    assert round(a1.result.mg_support, 6) == round(t * 0.2, 6)
    assert b2.result.mg_support == 0 and round(b2.result.supplier_support, 6) == round(t, 6)
    plain = build_promo_view(_promo([_block(1, "OW", [_line(1, "A1")])])).retailers[0].lines[0]
    assert round(plain.result.supplier_support, 6) == round(t * 0.5, 6)   # promo default
