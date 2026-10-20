"""Roles, per-area access and per-user data scope.

Roles:
  admin  — Macgear; every report and tool, every brand, and user management.
  user   — Macgear staff; only the reports/tools ticked on their account.
  vendor — a brand's own staff; only the reports ticked, only the brands ticked.
           Never a tool, never user management, whatever is stored.

An "area" is anything a user can be granted: a report or a tool. Each user's
areas default by role but can be overridden per user (User.allowed_tools, a
JSON list of area keys — the column predates reports). NULL/empty => role default.

A "capability" widens what a report SHOWS rather than which pages open:
  sell_in       — Macgear sell-in QUANTITY (NetSuite sales units) and Macgear
                  warehouse stock quantity; may be given to a vendor
  sell_in_value — Macgear's sales $ amount; staff only (user, 2026-10-15)
  margin        — estimated GP $ / %; needs sell_in_value; staff only
Capabilities live in the same allowed_tools list (keys don't collide) and are
default-deny for everyone but admins.

Data scope is separate from areas: User.allowed_brands / allowed_regions limit
which rows a report returns. For staff NULL means "everything"; for a vendor
NULL or empty brands means NOTHING — a vendor account with no brands ticked sees
an empty report, never every brand. Brands are the confidentiality boundary.

Deliberately the same shape as 3PL Portal's perms.py, so the two apps behave
alike. Storing NULL rather than the expanded default matters: when an area is
added, every admin picks it up automatically instead of silently lacking it.
"""
import json

# Area keys. Reports must match the `key` of each entry in main.REPORTS, tools
# the `key` of each entry in main.TOOLS.
REPORT_KEYS = ["sales", "sell_through", "items", "st_table", "st_weekly", "field"]
TOOL_KEYS = ["promos", "vendor_bills"]
AREA_KEYS = REPORT_KEYS + TOOL_KEYS
AREA_LABELS = {"sales": "Sales Explorer (needs Sell-in $)",
               "sell_through": "Sell-Through Dashboard", "items": "Item Explorer",
               "st_table": "Sell-Through Table",
               "st_weekly": "Weekly Sell-Through (consolidated report)",
               "field": "Field Activity (reps, display compliance)",
               "promos": "Vendor Promos", "vendor_bills": "PDF Bill Upload"}

CAP_KEYS = ["sell_in", "sell_in_value", "margin"]
CAP_LABELS = {"sell_in": "Sell-in units & Macgear stock (quantities only)",
              "sell_in_value": "Sell-in $ — Macgear's sales amounts (staff only)",
              "margin": "Margin — estimated GP $ and % (staff only)"}

# What a vendor may ever be granted. Tools are internal workflows over Macgear's
# own costs and claims; Macgear's sales $ and margin are commercial — a vendor
# gets quantities only, never either of those, even if ticked
# even if it is ticked.
VENDOR_AREAS = ["sell_through", "items", "st_table"]
VENDOR_CAPS = ["sell_in"]

ROLES = ["admin", "user", "vendor"]
ROLE_LABELS = {"admin": "Admin — full access and user management",
               "user": "Staff — only what is ticked below",
               "vendor": "Vendor — only the reports and brands ticked below"}

# Admins get everything. Anyone else gets nothing until areas are ticked;
# silence is safer than a default that quietly grants access to the next report.
ROLE_DEFAULT_AREAS = {"admin": AREA_KEYS, "user": [], "vendor": []}

REGIONS = ["AU", "NZ"]


def role_default(role: str) -> list[str]:
    return list(ROLE_DEFAULT_AREAS.get(role, []))


def _grantable(role: str) -> list[str]:
    return VENDOR_AREAS if role == "vendor" else AREA_KEYS


def _json_list(raw) -> list | None:
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return v if isinstance(v, list) else None


def effective_access(user) -> list[str]:
    """Resolved area access: per-user override if set, else the role default."""
    if user is None:
        return []
    if user.role == "admin":
        return list(AREA_KEYS)          # admins are never locked out of anything
    chosen = _json_list(user.allowed_tools)
    keys = chosen if chosen is not None else role_default(user.role)
    grantable = set(_grantable(user.role))
    return [k for k in AREA_KEYS if k in set(keys) and k in grantable]


def effective_caps(user) -> set[str]:
    """Capabilities this user holds. Admins: all. Vendors: never margin."""
    if user is None:
        return set()
    if user.role == "admin":
        return set(CAP_KEYS)
    chosen = set(_json_list(user.allowed_tools) or [])
    grantable = VENDOR_CAPS if user.role == "vendor" else CAP_KEYS
    caps = {c for c in grantable if c in chosen}
    if "sell_in" not in caps:
        caps.discard("sell_in_value")   # $ is a column of sell-in
    if "sell_in_value" not in caps:
        caps.discard("margin")          # margin is a column of sell-in $
    return caps


def normalize_allowed(role: str, selected: list[str]) -> str | None:
    """Store NULL when the selection equals the role default (so defaults keep
    flowing as areas are added), otherwise a JSON list in canonical order —
    areas first, then capabilities."""
    sel = set(selected)
    areas = [k for k in AREA_KEYS if k in sel and k in set(_grantable(role))]
    caps = [c for c in CAP_KEYS if c in sel and c in (VENDOR_CAPS if role == "vendor" else CAP_KEYS)]
    if areas == role_default(role) and not caps:
        return None
    return json.dumps(areas + caps)


def can_access(user, key: str) -> bool:
    return key in effective_access(user)


def is_admin(user) -> bool:
    return user is not None and user.role == "admin"


def is_vendor(user) -> bool:
    return user is not None and user.role == "vendor"


# ---- data scope ---------------------------------------------------------------
def effective_brands(user) -> list[str] | None:
    """Brands this user may see. None = every brand (staff only); a list = just
    those. A vendor always gets a list, empty when nothing is ticked."""
    if user is None or user.role == "admin":
        return None
    chosen = _json_list(user.allowed_brands)
    if user.role == "vendor":
        return [b for b in (chosen or []) if isinstance(b, str) and b]
    return [b for b in chosen if isinstance(b, str) and b] if chosen else None


def effective_regions(user) -> list[str]:
    """Regions this user may see; NULL/empty = both."""
    if user is None or user.role == "admin":
        return list(REGIONS)
    chosen = _json_list(user.allowed_regions)
    regions = [r for r in REGIONS if r in set(chosen or [])]
    return regions or list(REGIONS)


def normalize_brands(selected: list[str]) -> str | None:
    chosen = sorted({b.strip() for b in selected if b and b.strip()})
    return json.dumps(chosen) if chosen else None


def normalize_regions(selected: list[str]) -> str | None:
    chosen = [r for r in REGIONS if r in set(selected)]
    return None if not chosen or chosen == REGIONS else json.dumps(chosen)
