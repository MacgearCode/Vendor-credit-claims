"""Field Activity — the Mova field reps' visits, forms, photos and display compliance,
from Repsly via the warehouse (reporting.field_* views, loaded by
Sell Through/repsly_intake.py). Staff only for now.

DISPLAY COMPLIANCE — the measure reps are judged on (user, 2026-10-17). Rules:

  * Unit = store × product. Within the period, each store × product counts ONCE, using
    its LATEST answer — a store visited three times isn't weighted three times.
  * A blank answer counts as No: skipping a question must never help.
  * The rep credited is the rep who gave that latest answer.
  * Harvey Norman is THE measure ("HN core range"): only products HN grades A–D
    (column E of HN's weekly Mova file), and only at stores whose tier for that
    category ranges the grade — an "AB" store is measured on A and B products only.
    Other HN grades (E/F/O/S/Z — new, clearing, special order, deleted) never count.
    Categories without a tier list yet (only Floorcare has one) aren't in the HN
    measure; they show as "no tier list".
  * JB Hi-Fi and The Good Guys are measured too but separately ("merchandising"): reps
    can't influence ranging there. No grading exists, so a No whose follow-up says
    "not ranged" is left out; everything else counts.
  * "Is X STILL on display" (products being cleared) is never compliance.

The rules live in classify() / compliance() and are unit-tested
(tests/test_field.py). The warehouse views only lay out the facts per answer.
"""
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import warehouse

TTL = 10 * 60
CORE_GRADES = set("ABCD")
BANNERS = {"HN": "Harvey Norman", "JB": "JB Hi-Fi", "TGG": "The Good Guys", "OTHER": "Other"}
CACHE_PREFIXES = ("field_",)


# ---- status / freshness ---------------------------------------------------------
def data_version():
    """Changes whenever a Repsly load lands (newest form + last good run)."""
    return tuple(warehouse.query("select newest_form, last_ok from reporting.field_sync")[0])


def sync_status() -> dict:
    def build():
        newest, last_ok, status = warehouse.query(
            "select newest_form, last_ok, last_status from reporting.field_sync")[0]
        return {"newest_form": newest, "last_ok": last_ok, "status": status}
    return warehouse.cached(("field_sync",), build, ttl=120)


# ---- loads (the whole set is small: ~5k visits, ~22k display answers a year) ------
def _rows(sql, params=None):
    with warehouse.connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params or {})
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def display_rows() -> list[dict]:
    return warehouse.cached(("field_display",), lambda: _rows("""
        select form_id, visit_id, answer_date, submitted_at, seq, rep_code, rep_name,
               client_code, store_name, banner, state, product_key, product_name, category,
               sort_order, answer, hn_grade, store_grades, store_has_tier, reason
        from reporting.field_display"""), ttl=TTL)


def visits() -> list[dict]:
    return warehouse.cached(("field_visits",), lambda: _rows("""
        select visit_id, visit_date, rep_code, rep_name, client_code, store_name, banner, state,
               started_at, ended_at, minutes, checkin_m, forms, photos, notes, status_by_schedule
        from reporting.field_visit order by started_at desc nulls last"""), ttl=TTL)


def schedule() -> list[dict]:
    return warehouse.cached(("field_schedule",), lambda: _rows("""
        select schedule_date, scheduled_at, rep_code, rep_name, client_code, store_name, banner,
               state, due_date, note, visited
        from reporting.field_schedule"""), ttl=TTL)


def photos() -> list[dict]:
    return warehouse.cached(("field_photos",), lambda: _rows("""
        select photo_id, visit_id, taken_at, photo_date, rep_code, rep_name, client_code,
               store_name, banner, url, note
        from reporting.field_photo order by taken_at desc"""), ttl=TTL)


def notes() -> list[dict]:
    return warehouse.cached(("field_notes",), lambda: _rows("""
        select note_id, visit_id, noted_at, note_date, rep_code, rep_name, client_code,
               store_name, banner, note
        from reporting.field_note order by noted_at desc"""), ttl=TTL)


def stores() -> list[dict]:
    return warehouse.cached(("field_stores",), lambda: _rows("""
        select client_code, store_name, banner, state, city, lat, lng, active,
               floorcare_grades, hn_site_no
        from reporting.field_store"""), ttl=TTL)


def reps() -> list[dict]:
    return warehouse.cached(("field_reps",), lambda: _rows(
        "select rep_code, rep_name, active from reporting.field_rep order by rep_name"), ttl=TTL)


def unmapped_questions() -> list[dict]:
    return warehouse.cached(("field_unmapped",), lambda: _rows("""
        select question, answers, last_seen from reporting.field_unmapped_question
        order by last_seen desc"""), ttl=TTL)


def visit_answers(visit_id: int) -> list[dict]:
    return warehouse.cached(("field_visit_answers", visit_id), lambda: _rows("""
        select form_id, form_name, submitted_at, seq, question, value, measure, product_key
        from reporting.field_answer where visit_id = %(v)s order by submitted_at, form_id, seq""",
        {"v": visit_id}), ttl=TTL)


# ---- the rules -------------------------------------------------------------------
def _not_ranged_reason(reason) -> bool:
    return bool(reason) and "rang" in reason.lower()


def classify(r: dict) -> str:
    """Which measure an answer belongs to:
       hn            counts in the HN core-range measure
       hn_not_ranged HN store whose tier doesn't range this grade (left out)
       hn_not_core   HN grade outside A–D, or no HN grade (left out)
       hn_no_tier    HN store with no tier list for this category yet (left out, shown)
       merch         JB / TGG / other — counts in the merchandising measure
       merch_not_ranged  ... a No the rep says is "not ranged" (left out)"""
    if r["banner"] == "HN":
        g = (r.get("hn_grade") or "").strip().upper()
        if g not in CORE_GRADES:
            return "hn_not_core"
        if not r.get("store_has_tier"):
            return "hn_no_tier"
        return "hn" if g in (r.get("store_grades") or "") else "hn_not_ranged"
    if (r.get("answer") or "") != "Yes" and _not_ranged_reason(r.get("reason")):
        return "merch_not_ranged"
    return "merch"


def latest(rows: list[dict]) -> dict:
    """Latest answer per (store, product). Ties on time go to the later form position."""
    out = {}
    for r in sorted(rows, key=lambda r: (r["submitted_at"], r["form_id"], r["seq"])):
        out[(r["client_code"], r["product_key"])] = r
    return out


def _rate(rows) -> dict:
    n = len(rows)
    y = sum(1 for r in rows if r["answer"] == "Yes")
    return {"checks": n, "yes": y, "no": n - y, "rate": y / n if n else None}


def compliance(rows: list[dict], start: date, end: date, *, rep: str = "", banner: str = "",
               category: str = "") -> dict:
    """Display compliance over [start, end]. rows = display_rows() (any range)."""
    sel = [r for r in rows if start <= r["answer_date"] <= end
           and (not category or r["category"] == category)]
    every = [r for r in sel if r["answer"] in ("Yes", "No")]       # the old Power BI way, for comparison
    last = latest(sel)
    cls = defaultdict(list)
    for r in last.values():
        cls[classify(r)].append(r)
    if rep:
        cls = {k: [r for r in v if r["rep_code"] == rep] for k, v in cls.items()}
    if banner:
        cls = {k: [r for r in v if r["banner"] == banner] for k, v in cls.items()}
    counted = {"hn": cls.get("hn", []), "merch": cls.get("merch", [])}

    def by(key, rows_):
        g = defaultdict(list)
        for r in rows_:
            g[key(r)].append(r)
        return g

    def table(rows_, key, label):
        out = []
        for k, rs in by(key, rows_).items():
            t = _rate(rs)
            t.update(label(k, rs))
            t["stores"] = len({r["client_code"] for r in rs})
            out.append(t)
        return sorted(out, key=lambda t: (t["rate"] if t["rate"] is not None else 2, -t["checks"]))

    res = {"start": start, "end": end, "every_answer": _rate(every), "excluded": {}}
    for m, rs in counted.items():
        res[m] = {
            "total": _rate(rs),
            "products": table(rs, lambda r: r["product_key"],
                              lambda k, rs: {"key": k, "name": rs[0]["product_name"], "grade": rs[0].get("hn_grade"),
                                             "category": rs[0]["category"], "sort": rs[0]["sort_order"]}),
            "reps": table(rs, lambda r: r["rep_code"],
                          lambda k, rs: {"key": k, "name": rs[0]["rep_name"] or k}),
            "stores": table(rs, lambda r: r["client_code"],
                            lambda k, rs: {"key": k, "name": rs[0]["store_name"] or k, "state": rs[0]["state"],
                                           "grades": rs[0].get("store_grades"), "banner": rs[0]["banner"],
                                           "missing": sorted(r["product_name"] for r in rs if r["answer"] != "Yes")}),
            "reasons": Counter((r.get("reason") or "No reason given").strip() for r in rs if r["answer"] != "Yes")
                       .most_common(8),
        }
    for k in ("hn_not_ranged", "hn_not_core", "hn_no_tier", "merch_not_ranged"):
        res["excluded"][k] = len(cls.get(k, []))
    return res


def persistent_gaps(rows: list[dict], end: date, *, measure: str = "hn", min_run: int = 2,
                    days: int = 120) -> list[dict]:
    """Store × product whose last `min_run`+ answers (within `days`) were all No — the
    chase list. Only rows that count in `measure` (hn or merch)."""
    hist = defaultdict(list)
    for r in sorted(rows, key=lambda r: (r["submitted_at"], r["form_id"], r["seq"])):
        if end - timedelta(days=days) <= r["answer_date"] <= end and classify(r) == measure:
            hist[(r["client_code"], r["product_key"])].append(r)
    out = []
    for seq in hist.values():
        run = 0
        for r in reversed(seq):
            if r["answer"] == "Yes":
                break
            run += 1
        if run >= min_run:
            last = seq[-1]
            out.append({"client_code": last["client_code"], "store_name": last["store_name"],
                        "product": last["product_name"], "run": run, "since": seq[-run]["answer_date"],
                        "last": last["answer_date"], "rep_code": last["rep_code"], "rep_name": last["rep_name"],
                        "grade": last.get("hn_grade"), "reason": last.get("reason") or ""})
    return sorted(out, key=lambda g: (-g["run"], g["since"]))


# ---- activity ------------------------------------------------------------------
def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def rep_activity(vis: list[dict], sched: list[dict], start: date, end: date, today: date) -> list[dict]:
    """Per rep: visits done, scheduled (to today), scheduled visits made, avg minutes,
    stores, check-ins far (>300 m) from the store."""
    acc = defaultdict(lambda: {"visits": 0, "minutes": [], "stores": set(), "far": 0,
                               "scheduled": 0, "sched_done": 0, "name": ""})
    for v in vis:
        if v["visit_date"] and start <= v["visit_date"] <= end:
            a = acc[v["rep_code"]]
            a["name"] = v["rep_name"] or v["rep_code"]
            a["visits"] += 1
            a["stores"].add(v["client_code"])
            if v["minutes"] is not None and 0 < float(v["minutes"]) < 600:
                a["minutes"].append(float(v["minutes"]))
            if v["checkin_m"] is not None and float(v["checkin_m"]) > 300:
                a["far"] += 1
    for s in sched:
        if start <= s["schedule_date"] <= min(end, today):
            a = acc[s["rep_code"]]
            a["name"] = a["name"] or s["rep_name"] or s["rep_code"]
            a["scheduled"] += 1
            a["sched_done"] += bool(s["visited"])
    out = []
    for code, a in acc.items():
        out.append({"rep_code": code, "name": a["name"], "visits": a["visits"], "stores": len(a["stores"]),
                    "avg_minutes": sum(a["minutes"]) / len(a["minutes"]) if a["minutes"] else None,
                    "far": a["far"], "scheduled": a["scheduled"], "sched_done": a["sched_done"],
                    "sched_rate": a["sched_done"] / a["scheduled"] if a["scheduled"] else None})
    return sorted(out, key=lambda r: -r["visits"])


def month_bounds(d: date) -> tuple[date, date]:
    s = d.replace(day=1)
    e = (s.replace(year=s.year + 1, month=1) if s.month == 12 else s.replace(month=s.month + 1)) - timedelta(days=1)
    return s, e
