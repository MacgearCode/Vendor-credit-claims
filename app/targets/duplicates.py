"""Flag likely-duplicate and junk customer names for later cleanup (v1: report only)."""
import re
from collections import defaultdict

JUNK = {"- none -", "none", "other", "others", "cash sales", "samples",
        "staff purchases", "corporate"}

# Legal-suffix / noise tokens stripped when normalizing for duplicate detection
NOISE = {"pty", "ltd", "limited", "inc", "group", "australia", "nz",
         "new", "zealand", "the", "co", "sales", "online", "commercial"}


def _normalize(name: str) -> str:
    tokens = re.sub(r"[^a-z0-9\s]", " ", name.lower()).split()
    kept = [t for t in tokens if t not in NOISE]
    return " ".join(kept) or " ".join(tokens)


def find_duplicates(conn, fy=None):
    """Over the customers in one financial year of actuals (default: the newest)."""
    from . import actuals
    if fy is None:
        ys = actuals.years()
        fy = ys[0]["id"] if ys else None
    names = sorted({cust for c in actuals.cells(fy).values() for (_, cust) in c})

    groups = defaultdict(list)
    junk = []
    for n in names:
        if n.strip().lower() in JUNK:
            junk.append(n)
            continue
        groups[_normalize(n)].append(n)

    dupes = [sorted(v) for v in groups.values() if len(v) > 1]
    dupes.sort(key=lambda g: g[0].lower())
    return {
        "duplicate_groups": dupes,
        "junk_names": sorted(junk),
        "total_customers": len(names),
    }
