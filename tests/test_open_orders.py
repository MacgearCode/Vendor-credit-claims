"""Open Orders: payload shape, the home summary's class rules, the item view, access."""
from datetime import date
from decimal import Decimal as D

from app import main, open_orders as oo, perms

TODAY = date(2026, 11, 19)


def _row(doc, d, cls, retailer, code, qty, amount, committed=0, rg="AU", brand="MOVA"):
    """committed + back-ordered = open qty (always true in the warehouse); ready $ = committed x rate."""
    rate = D(amount) / D(qty)
    return (rg, doc, d, "Pending Fulfillment", cls, retailer, retailer + " store", brand, code,
            code + " desc", D(qty), D(committed), D(qty) - D(committed), D(amount),
            round(D(committed) * rate, 2), None, "PO" + doc)


ROWS = [
    _row("SO1", date(2026, 11, 18), "customer", "JB HIFI", "A1", 2, "200.00", committed=2),
    _row("SO1", date(2026, 11, 18), "customer", "JB HIFI", "B2", 1, "50.50"),
    _row("SO2", date(2026, 10, 1), "customer", "Harvey Norman", "A1", 5, "500.00"),
    _row("SO3", date(2026, 9, 1), "holding", "Holding Orders", "A1", 100, "9999.00", committed=90),
    _row("SO4", date(2026, 11, 10), "intercompany", "MacGear NZ", "A1", 10, "800.00"),
]


def test_orders_and_lines_roll_up():
    d = oo.shape(ROWS, TODAY, True)
    R = d["regions"]["AU"]
    so1 = next(o for o in R["orders"] if o["doc"] == "SO1")
    assert so1["lines"] == 2 and so1["units"] == 3 and so1["value"] == 250.5 and so1["age"] == 1
    assert so1["r_lines"] == 1 and so1["r_units"] == 2 and so1["r_value"] == 200.0   # B2 back-ordered
    assert len(R["lines"]) == 5 and len(R["items"]) == 2          # items listed once
    assert [o["doc"] for o in R["orders"]] == ["SO3", "SO2", "SO4", "SO1"]   # oldest first


def test_summary_counts_customer_orders_only_holding_apart():
    s = oo.summary(oo.shape(ROWS, TODAY, True))["AU"]
    assert s["orders"] == 2 and s["lines"] == 3 and s["units"] == 8
    assert s["value"] == 750.5                      # holding and intercompany left out
    assert s["ready_value"] == 200.0 and s["ready_lines"] == 1 and s["ready_units"] == 2
    assert s["holding_ready_value"] == 8999.1 and s["holding_ready_units"] == 90   # stock held for them
    assert s["holding_value"] == 9999.0 and s["holding_units"] == 100
    assert s["over_7"] == 1 and s["over_30"] == 1   # SO2: 49 days
    assert s["top"][0]["name"] == "Harvey Norman"   # by value


def test_no_money_capability_means_no_amounts():
    rows = [r[:13] + (None, None) + r[15:] for r in ROWS]  # the SQL selects NULL without sell_in_value
    d = oo.shape(rows, TODAY, False)
    assert all(o["value"] is None for o in d["regions"]["AU"]["orders"])
    assert all(line[5] is None and line[6] is None for line in d["regions"]["AU"]["lines"])
    assert all(o["r_value"] is None for o in d["regions"]["AU"]["orders"])
    s = oo.summary(d)["AU"]
    assert s["value"] is None and s["holding_value"] is None and s["ready_value"] is None
    assert s["top"][0]["name"] == "JB HIFI"         # by lines when there's no $


def test_item_view_is_customer_figures_with_holding_listed():
    v = oo.item_open(oo.shape(ROWS, TODAY, True), "a1", "AU")   # case-insensitive code
    assert v["qty"] == 7 and v["orders"] == 2 and v["value"] == 700.0
    assert v["committed"] == 2 and v["backordered"] == 5 and v["ready_value"] == 200.0
    assert v["holding"] == 100 and len(v["rows"]) == 4
    assert oo.item_open(oo.shape(ROWS, TODAY, True), "A1", "NZ")["rows"] == []


def test_staff_only_area_on_its_own_path():
    assert "open_orders" in perms.REPORT_KEYS and "open_orders" not in perms.VENDOR_AREAS
    assert main._tool_for_path("/reports/open-orders") == "open_orders"
