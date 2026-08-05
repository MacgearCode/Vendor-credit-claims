"""Roles and per-tool access.

Roles:
  admin — can manage users, and reach every tool.
  user  — reaches only the tools ticked on their account.

Each user's tool access defaults by role but can be overridden per user
(User.allowed_tools, a JSON list). NULL/empty => use the role default.

Deliberately the same shape as 3PL Portal's perms.py, so the two apps behave
alike. Storing NULL rather than the expanded default matters: when a third tool
is added, every admin picks it up automatically instead of silently lacking it.

A group layer would slot in between the user override and the role default
without changing this signature or the stored data.
"""
import json

# Tool keys. Must match the `key` of each entry in main.TOOLS.
TOOL_KEYS = ["promos", "vendor_bills"]
TOOL_LABELS = {"promos": "Vendor Promos", "vendor_bills": "PDF Bill Upload"}

ROLES = ["admin", "user"]
ROLE_LABELS = {"admin": "Admin — full access and user management",
               "user": "User — only the tools ticked below"}

# Admins get everything. A new plain user gets nothing until tools are ticked;
# silence is safer than a default that quietly grants access to the next tool.
ROLE_DEFAULT_TOOLS = {"admin": TOOL_KEYS, "user": []}


def role_default(role: str) -> list[str]:
    return list(ROLE_DEFAULT_TOOLS.get(role, []))


def effective_tools(user) -> list[str]:
    """Resolved tool access: per-user override if set, else the role default."""
    if user is None:
        return []
    if user.role == "admin":
        return list(TOOL_KEYS)          # admins are never locked out of a tool
    if user.allowed_tools:
        try:
            chosen = [t for t in json.loads(user.allowed_tools) if t in TOOL_KEYS]
            return chosen
        except (ValueError, TypeError):
            pass
    return role_default(user.role)


def normalize_allowed(role: str, selected: list[str]) -> str | None:
    """Store NULL when the selection equals the role default (so defaults keep
    flowing as tools are added), otherwise a JSON list in canonical order."""
    chosen = [t for t in TOOL_KEYS if t in set(selected)]
    if chosen == role_default(role):
        return None
    return json.dumps(chosen)


def can_access(user, tool_key: str) -> bool:
    return tool_key in effective_tools(user)


def is_admin(user) -> bool:
    return user is not None and user.role == "admin"
