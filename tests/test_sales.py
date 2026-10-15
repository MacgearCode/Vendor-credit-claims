"""Financial-year periods and the period maths behind the home page tiles.
Macgear FY = April–March, named by its end year (FY27 = Apr 2026 – Mar 2027)."""
from datetime import date

from app import sales


def test_fy_start_and_label():
    assert sales.fy_start(date(2026, 10, 15)) == date(2026, 4, 1)
    assert sales.fy_start(date(2027, 3, 31)) == date(2026, 4, 1)
    assert sales.fy_start(date(2026, 4, 1)) == date(2026, 4, 1)
    assert sales.fy_label(date(2026, 10, 15)) == "FY27"
    assert sales.fy_label(date(2026, 3, 31)) == "FY26"


def test_fiscal_quarters():
    cases = {date(2026, 4, 1): (date(2026, 4, 1), "Q1"), date(2026, 6, 30): (date(2026, 4, 1), "Q1"),
             date(2026, 7, 1): (date(2026, 7, 1), "Q2"), date(2026, 10, 15): (date(2026, 10, 1), "Q3"),
             date(2027, 1, 5): (date(2027, 1, 1), "Q4"), date(2027, 3, 31): (date(2027, 1, 1), "Q4")}
    for d, (start, label) in cases.items():
        assert sales.fq_start(d) == start, d
        assert sales.fq_label(d) == label, d


def test_last_year_handles_leap_day():
    assert sales.last_year(date(2028, 2, 29)) == date(2027, 2, 28)
    assert sales.last_year(date(2026, 10, 15)) == date(2025, 10, 15)


def test_weekdays():
    assert sales.weekdays(date(2026, 10, 1), date(2026, 10, 15)) == 11
    assert sales.weekdays(date(2026, 10, 1), date(2026, 10, 31)) == 22


def test_block_growth_target_and_margin():
    days = {date(2026, 10, 1): [100.0, 60.0, 2.0], date(2026, 10, 2): [50.0, 30.0, 1.0],
            date(2025, 10, 1): [75.0, 40.0, 1.0]}
    b = sales._block(days, date(2026, 10, 1), date(2026, 10, 2), 300.0, True)
    assert b["value"] == 150 and b["ly"] == 75 and b["units"] == 3
    assert round(b["growth"]) == 100 and round(b["to_target"]) == 50
    assert b["gp"] == 60 and round(b["gp_pct"]) == 40
    assert "gp" not in sales._block(days, date(2026, 10, 1), date(2026, 10, 2), None, False)


def test_expected_sell_through_week_is_the_last_completed_sunday():
    from app import receipts
    assert receipts.expected_week(date(2026, 10, 15)) == date(2026, 10, 11)   # Thu
    assert receipts.expected_week(date(2026, 10, 19)) == date(2026, 10, 18)   # Mon
    assert receipts.expected_week(date(2026, 10, 18)) == date(2026, 10, 11)   # Sun: today isn't over


def test_netsuite_last_modified_is_read_as_brisbane_time():
    """Scheduled Zone exports stamp Brisbane time; in daylight saving that is an
    hour behind Sydney, so 3:32pm in the data is 4:32pm on the office clock."""
    from datetime import datetime
    from app import main
    assert main._short_time(main._brisbane_to_sydney(datetime(2026, 10, 15, 15, 32))) == "15 Oct 4:32pm"
    assert main._short_time(main._brisbane_to_sydney(datetime(2026, 6, 15, 15, 32))) == "15 Jun 3:32pm"
