"""Area access and data scope.

The rule that matters: a tile being hidden is presentation, but `can_access` and
`effective_brands` are the controls the middleware and the reports enforce.
These tests are about who gets in, and what they see once they are.
"""
import json
from types import SimpleNamespace

from app import perms


def user(role="user", allowed=None, active=True, brands=None, regions=None):
    return SimpleNamespace(role=role, allowed_tools=allowed, active=active,
                           allowed_brands=brands, allowed_regions=regions)


# ---- areas -------------------------------------------------------------------
def test_admin_reaches_every_area_even_with_an_empty_override():
    """An admin row could carry a stale allowed_tools from a demotion and back.
    It must never lock an admin out — that is how you lose the last way in."""
    assert perms.effective_access(user("admin")) == perms.AREA_KEYS
    assert perms.effective_access(user("admin", json.dumps([]))) == perms.AREA_KEYS
    assert perms.can_access(user("admin", json.dumps([])), "vendor_bills")


def test_new_plain_user_has_nothing_until_ticked():
    """Default-deny. A report added next year must not appear for everyone."""
    u = user("user")
    assert perms.effective_access(u) == []
    for k in perms.AREA_KEYS:
        assert not perms.can_access(u, k)


def test_ticked_areas_are_the_ones_granted():
    u = user("user", json.dumps(["vendor_bills", "sell_through"]))
    assert perms.can_access(u, "vendor_bills")
    assert perms.can_access(u, "sell_through")
    assert not perms.can_access(u, "promos")


def test_unknown_area_keys_are_dropped():
    """A renamed or removed key left in the DB must not grant anything."""
    u = user("user", json.dumps(["vendor_bills", "nuclear_launch"]))
    assert perms.effective_access(u) == ["vendor_bills"]
    assert not perms.can_access(u, "nuclear_launch")


def test_corrupt_override_falls_back_to_the_role_default():
    assert perms.effective_access(user("user", "not json at all")) == []


def test_no_user_gets_nothing():
    assert perms.effective_access(None) == []
    assert not perms.is_admin(None)
    assert not perms.is_vendor(None)


def test_vendor_never_gets_a_tool_even_if_one_is_stored():
    """Tools are Macgear-internal (costs, claims). A tool key that reached a
    vendor row by any route — an old form, a hand edit — must grant nothing."""
    u = user("vendor", json.dumps(["promos", "vendor_bills", "sell_through"]))
    assert perms.effective_access(u) == ["sell_through"]
    assert not perms.can_access(u, "promos")
    assert not perms.can_access(u, "vendor_bills")


def test_normalize_strips_tools_from_a_vendor():
    stored = perms.normalize_allowed("vendor", ["promos", "sell_through"])
    assert json.loads(stored) == ["sell_through"]
    assert perms.normalize_allowed("vendor", ["promos"]) is None   # = role default: nothing


def test_normalize_stores_null_when_selection_equals_role_default():
    """NULL, not an expanded list — so defaults keep flowing as areas are added."""
    assert perms.normalize_allowed("user", []) is None
    assert perms.normalize_allowed("admin", perms.AREA_KEYS) is None


def test_normalize_keeps_canonical_order_and_ignores_junk():
    stored = perms.normalize_allowed("user", ["vendor_bills", "bogus", "promos"])
    assert json.loads(stored) == ["promos", "vendor_bills"]


def test_every_area_key_has_a_label():
    """A missing label would render a raw key like 'vendor_bills' in the UI."""
    assert set(perms.AREA_LABELS) == set(perms.AREA_KEYS)
    assert set(perms.ROLE_LABELS) == set(perms.ROLES)
    assert set(perms.VENDOR_AREAS) <= set(perms.REPORT_KEYS)


# ---- data scope --------------------------------------------------------------
def test_vendor_with_no_brands_sees_no_brand():
    """Fail closed. NULL means 'all' for staff, but for a vendor it must mean
    nothing — a half-set-up vendor account must not show every brand."""
    assert perms.effective_brands(user("vendor")) == []
    assert perms.effective_brands(user("vendor", brands="[]")) == []
    assert perms.effective_brands(user("vendor", brands="corrupt")) == []


def test_vendor_sees_exactly_the_brands_ticked():
    u = user("vendor", brands=json.dumps(["ANKER", "EUFY"]))
    assert perms.effective_brands(u) == ["ANKER", "EUFY"]


def test_staff_brand_scope_defaults_to_all_and_can_be_limited():
    assert perms.effective_brands(user("user")) is None
    assert perms.effective_brands(user("user", brands=json.dumps(["MOVA"]))) == ["MOVA"]
    assert perms.effective_brands(user("admin", brands=json.dumps(["MOVA"]))) is None


def test_regions_default_to_both_and_ignore_junk():
    assert perms.effective_regions(user("vendor")) == ["AU", "NZ"]
    assert perms.effective_regions(user("vendor", regions=json.dumps(["NZ"]))) == ["NZ"]
    assert perms.effective_regions(user("user", regions=json.dumps(["XX"]))) == ["AU", "NZ"]


def test_normalize_brands_and_regions():
    assert perms.normalize_brands([]) is None
    assert json.loads(perms.normalize_brands(["EUFY", " ANKER ", "EUFY", ""])) == ["ANKER", "EUFY"]
    assert perms.normalize_regions(["AU", "NZ"]) is None
    assert perms.normalize_regions([]) is None
    assert json.loads(perms.normalize_regions(["NZ"])) == ["NZ"]


# ---- capabilities (sell-in, margin) --------------------------------------------
def test_capabilities_are_default_deny_except_admin():
    assert perms.effective_caps(user("admin")) == {"sell_in", "sell_in_value", "margin"}
    assert perms.effective_caps(user("user")) == set()
    assert perms.effective_caps(user("vendor")) == set()


def test_vendor_gets_sell_in_quantity_but_never_dollars_or_margin():
    u = user("vendor", json.dumps(["items", "sell_in", "sell_in_value", "margin"]))
    assert perms.effective_caps(u) == {"sell_in"}
    assert json.loads(perms.normalize_allowed(
        "vendor", ["items", "sell_in", "sell_in_value", "margin", "promos"])) == ["items", "sell_in"]


def test_dollars_and_margin_need_what_they_are_built_on():
    caps = lambda *k: perms.effective_caps(user("user", json.dumps(["items", *k])))
    assert caps("margin") == set()
    assert caps("sell_in_value") == set()
    assert caps("sell_in", "margin") == {"sell_in"}
    assert caps("sell_in", "sell_in_value", "margin") == {"sell_in", "sell_in_value", "margin"}


def test_caps_never_open_a_page():
    u = user("user", json.dumps(["sell_in", "margin"]))
    assert perms.effective_access(u) == []
