"""What's new — the site's update log (/updates), read from app/updates.md.

Each entry is a "## date | title | area keys | kind" heading followed by "- " lines.
Kind "feature" = What's new (a new report / tool page or a major capability); anything
else = Release notes (cosmetic changes, tweaks, fixes) — user, 2026-11-25.
Area keys limit who sees an entry, by the same rule as the side menu: a user
sees it if they can open one of those areas (or are an admin for "admin").
No keys = everyone, vendors included.
"""
import os
import re
from datetime import date

from markupsafe import Markup, escape

PATH = os.path.join(os.path.dirname(__file__), "updates.md")


def _inline(text: str) -> Markup:
    """HTML-escaped, with **bold** shown as bold."""
    return Markup(re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(text))))


def parse(text: str) -> list[dict]:
    entries, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            parts = [p.strip() for p in line[3:].split("|")]
            try:
                when = date.fromisoformat(parts[0])
            except ValueError:
                cur = None                       # a malformed heading drops its entry
                continue
            cur = {"date": when, "title": parts[1] if len(parts) > 1 else "",
                   "areas": [a.strip() for a in (parts[2] if len(parts) > 2 else "").split(",") if a.strip()],
                   "feature": len(parts) > 3 and parts[3].lower() == "feature",
                   "points": [], "text": []}
            entries.append(cur)
        elif cur is not None and line.startswith("- "):
            cur["points"].append(_inline(line[2:].strip()))
        elif cur is not None and line:
            cur["text"].append(_inline(line))
    entries.sort(key=lambda e: e["date"], reverse=True)     # stable: file order within a day
    return entries


_cache = {"mtime": None, "entries": []}


def load() -> list[dict]:
    """Re-read only when the file changes."""
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return []
    if mtime != _cache["mtime"]:
        with open(PATH, encoding="utf-8") as f:
            _cache["entries"] = parse(f.read())
        _cache["mtime"] = mtime
    return _cache["entries"]


def visible(entries: list[dict], areas: set[str]) -> list[dict]:
    """Entries this user may see. `areas` = the areas they can open (as the side
    menu lists them) plus "admin" for admins."""
    return [e for e in entries if not e["areas"] or areas.intersection(e["areas"])]
