"""Tool-access resolution.

The rule that matters: a tile being hidden is presentation, but `can_access` is
the control the middleware enforces. These tests are about who gets in.
"""
import json
from types import SimpleNamespace

from app import perms


def user(role="user", allowed=None, active=True):
    return SimpleNamespace(role=role, allowed_tools=allowed, active=active)


def test_admin_reaches_every_tool_even_with_an_empty_override():
    """An admin row could carry a stale allowed_tools from a demotion and back.
    It must never lock an admin out — that is how you lose the last way in."""
    assert perms.effective_tools(user("admin")) == perms.TOOL_KEYS
    assert perms.effective_tools(user("admin", json.dumps([]))) == perms.TOOL_KEYS
    assert perms.can_access(user("admin", json.dumps([])), "vendor_bills")


def test_new_plain_user_has_nothing_until_ticked():
    """Default-deny. A tool added next year must not appear for everyone."""
    u = user("user")
    assert perms.effective_tools(u) == []
    assert not perms.can_access(u, "promos")
    assert not perms.can_access(u, "vendor_bills")


def test_ticked_tools_are_the_ones_granted():
    u = user("user", json.dumps(["vendor_bills"]))
    assert perms.can_access(u, "vendor_bills")
    assert not perms.can_access(u, "promos")


def test_unknown_tool_keys_are_dropped():
    """A renamed or removed tool key left in the DB must not grant anything."""
    u = user("user", json.dumps(["vendor_bills", "nuclear_launch"]))
    assert perms.effective_tools(u) == ["vendor_bills"]
    assert not perms.can_access(u, "nuclear_launch")


def test_corrupt_override_falls_back_to_the_role_default():
    assert perms.effective_tools(user("user", "not json at all")) == []


def test_no_user_gets_nothing():
    assert perms.effective_tools(None) == []
    assert not perms.is_admin(None)


def test_normalize_stores_null_when_selection_equals_role_default():
    """NULL, not an expanded list — so defaults keep flowing as tools are added."""
    assert perms.normalize_allowed("user", []) is None
    assert perms.normalize_allowed("admin", perms.TOOL_KEYS) is None


def test_normalize_keeps_canonical_order_and_ignores_junk():
    stored = perms.normalize_allowed("user", ["vendor_bills", "bogus", "promos"])
    assert json.loads(stored) == ["promos", "vendor_bills"]


def test_every_tool_key_has_a_label():
    """A missing label would render a raw key like 'vendor_bills' in the UI."""
    assert set(perms.TOOL_LABELS) == set(perms.TOOL_KEYS)
    assert set(perms.ROLE_LABELS) == set(perms.ROLES)
