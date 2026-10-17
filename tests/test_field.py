"""Field Activity: the display-compliance rules reps are measured on (field.py
docstring), and that the pages are staff-only."""
import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import field, main
from app.db import SessionLocal
from app.models import User
from app.security import sign

D = date(2026, 10, 10)
_seq = iter(range(1, 10_000))


def row(store="HN 1", product="Z70 ULTRA", answer="Yes", day=D, hour=9, banner="HN", grade="A",
        tier="AB", has_tier=True, rep="R1", reason=None, category="FLOORCARE"):
    n = next(_seq)
    return {"form_id": n, "visit_id": n, "seq": 1, "answer_date": day,
            "submitted_at": datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc),
            "rep_code": rep, "rep_name": rep, "client_code": store, "store_name": store, "banner": banner,
            "state": "NSW", "product_key": product, "product_name": product, "category": category,
            "sort_order": 1, "answer": answer, "hn_grade": grade, "store_grades": tier,
            "store_has_tier": has_tier, "reason": reason}


# ---- classify ----------------------------------------------------------------------
@pytest.mark.parametrize("kw,expected", [
    ({}, "hn"),                                                  # A product at an AB store
    ({"grade": "C", "tier": "AB"}, "hn_not_ranged"),             # C product, store only ranges A+B
    ({"grade": "D", "tier": "ABCD"}, "hn"),
    ({"grade": "F"}, "hn_not_core"),                             # clearing: never counts
    ({"grade": "S"}, "hn_not_core"),
    ({"grade": None}, "hn_not_core"),                            # no HN grade for the product
    ({"has_tier": False, "tier": None}, "hn_no_tier"),           # HN store not on HN's tier list
    ({"category": "OUTDOOR LIVING", "grade": "B", "tier": "AB"}, "hn"),      # one tier, all categories
    ({"category": "PERSONAL CARE", "grade": "C", "tier": "AB"}, "hn_not_ranged"),
    ({"banner": "JB", "grade": None}, "merch"),
    ({"banner": "TGG", "answer": "No", "reason": "N/A not ranged in store"}, "merch_not_ranged"),
    ({"banner": "TGG", "answer": "", "reason": "N/A  - Not ranged"}, "merch_not_ranged"),
    ({"banner": "JB", "answer": "No", "reason": "Waiting on demo stock"}, "merch"),
    ({"banner": "JB", "answer": "Yes", "reason": "not ranged"}, "merch"),    # a Yes always counts
])
def test_classify(kw, expected):
    assert field.classify(row(**kw)) == expected


# ---- compliance --------------------------------------------------------------------
def test_each_store_product_counts_once_using_the_latest_answer():
    rows = [row(answer="No", hour=8), row(answer="Yes", hour=9),               # same store twice: latest = Yes
            row(store="HN 2", answer="No", day=date(2026, 10, 3)),
            row(store="HN 2", answer="Yes", day=date(2026, 10, 2))]            # older Yes doesn't help
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert c["hn"]["total"] == {"checks": 2, "yes": 1, "no": 1, "rate": 0.5}
    assert c["every_answer"]["checks"] == 4                                   # the old way counted all four


def test_blank_counts_as_no():
    c = field.compliance([row(answer="")], date(2026, 10, 1), date(2026, 10, 31))
    assert c["hn"]["total"]["no"] == 1 and c["hn"]["total"]["rate"] == 0.0


def test_only_ranged_core_grades_count_for_hn():
    rows = [row(product="Z70 ULTRA", grade="A", answer="Yes"),
            row(product="MOBIUS 60", grade="C", tier="AB", answer="No"),     # not ranged here: left out
            row(product="Z60 ULTRA", grade="F", answer="No"),                # clearing: left out
            row(product="DIVER A10", grade="A", has_tier=False, tier=None, answer="No", category="OUTDOOR LIVING")]
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert c["hn"]["total"]["rate"] == 1.0
    assert c["excluded"] == {"hn_not_ranged": 1, "hn_not_core": 1, "hn_no_tier": 1, "merch_not_ranged": 0}


def test_graded_over_and_above_and_coverage_stay_separate():
    rows = [row(store="HN 1", product="V70 ULTRA", grade="B", tier="AB", answer="Yes"),     # graded, on display
            row(store="HN 2", product="V70 ULTRA", grade="B", tier="A", answer="Yes"),      # outside grade, on display
            row(store="HN 3", product="V70 ULTRA", grade="B", tier="A", answer="No"),       # outside grade, not
            row(store="HN 4", product="V70 ULTRA", grade="B", has_tier=False, tier=None, answer="Yes"),  # off the tier list
            row(store="HN 5", product="V70 ULTRA", grade="B", tier="AB", answer="Yes",
                day=date(2026, 8, 20)),                                                     # earlier: coverage only
            row(store="HN 6", product="Z60 ULTRA", grade="F", answer="Yes"),                # clearing: in none of them
            row(store="JB 1", product="V70 ULTRA", banner="JB", answer="Yes")]              # not HN
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31), network=10)
    p = next(x for x in c["hn"]["products"] if x["key"] == "V70 ULTRA")
    assert (p["checks"], p["rate"]) == (1, 1.0)                     # graded: 1 of 1, never above 100%
    assert p["over_and_above"] == 2                                 # HN 2 + HN 4
    # coverage: tier-list stores only, latest answer in the 90 days to 31 Oct: HN 1, 2, 5 on; HN 3 off
    assert p["coverage"] == {"on": 3, "off": 1, "unknown": 6, "rate": 0.3}
    assert c["hn"]["over_and_above"] == 2 and c["hn"]["coverage"]["network"] == 10
    assert [x["key"] for x in c["hn"]["products"]] == ["V70 ULTRA"]


def test_coverage_ignores_answers_older_than_the_window_and_the_rep_filter():
    rows = [row(store="HN 1", answer="Yes", day=date(2026, 6, 1), rep="R1"),        # > 90 days before 31 Oct
            row(store="HN 2", answer="Yes", day=date(2026, 10, 2), rep="R2")]
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31), rep="R1", network=4)
    assert c["hn"]["coverage"]["on"] == 1 and c["hn"]["total"]["checks"] == 0


def test_a_core_product_seen_only_outside_its_grade_is_still_listed():
    rows = [row(product="MOBIUS 60", grade="C", tier="AB", answer="Yes")]
    p = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31), network=5)["hn"]["products"][0]
    assert p["checks"] == 0 and p["rate"] is None and p["over_and_above"] == 1 and p["coverage"]["on"] == 1


def test_hn_network_counts_tiered_hn_stores():
    st = [{"banner": "HN", "floorcare_grades": "AB"}, {"banner": "HN", "floorcare_grades": ""},
          {"banner": "HN", "floorcare_grades": None}, {"banner": "JB", "floorcare_grades": None}]
    assert field.hn_network(st) == 2


def test_jb_tgg_are_their_own_measure():
    rows = [row(banner="JB", store="JB 1", answer="Yes"), row(banner="TGG", store="TGG 1", answer="No"),
            row(banner="TGG", store="TGG 2", answer="No", reason="N/A not ranged")]
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert c["hn"]["total"]["checks"] == 0
    assert c["merch"]["total"] == {"checks": 2, "yes": 1, "no": 1, "rate": 0.5}
    assert c["excluded"]["merch_not_ranged"] == 1


def test_rep_credited_is_the_rep_on_the_latest_answer():
    rows = [row(rep="R1", answer="No", hour=8), row(rep="R2", answer="Yes", hour=10)]
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31))
    assert [(r["key"], r["checks"]) for r in c["hn"]["reps"]] == [("R2", 1)]
    assert field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31), rep="R1")["hn"]["total"]["checks"] == 0


def test_period_bounds_and_category_filter():
    rows = [row(day=date(2026, 9, 30), answer="No"), row(day=date(2026, 10, 1), store="HN 2"),
            row(store="HN 3", category="OUTDOOR LIVING", answer="No")]
    c = field.compliance(rows, date(2026, 10, 1), date(2026, 10, 31), category="FLOORCARE")
    assert c["hn"]["total"]["checks"] == 1 and c["hn"]["total"]["rate"] == 1.0


def test_persistent_gaps_need_two_nos_in_a_row():
    rows = [row(answer="Yes", day=date(2026, 9, 1)), row(answer="No", day=date(2026, 9, 15)),
            row(answer="", day=date(2026, 10, 1), reason="No space"),
            row(store="HN 2", answer="No", day=date(2026, 9, 1)), row(store="HN 2", answer="Yes", day=date(2026, 10, 1)),
            row(store="HN 3", answer="No", grade="C", tier="AB", day=date(2026, 9, 1)),     # not ranged: never a gap
            row(store="HN 3", answer="No", grade="C", tier="AB", day=date(2026, 10, 1))]
    g = field.persistent_gaps(rows, D)
    assert [(x["client_code"], x["run"], x["since"]) for x in g] == [("HN 1", 2, date(2026, 9, 15))]
    assert g[0]["reason"] == "No space"


def test_month_bounds():
    assert field.month_bounds(date(2026, 12, 15)) == (date(2026, 12, 1), date(2026, 12, 31))
    assert field.month_bounds(date(2027, 2, 3)) == (date(2027, 2, 1), date(2027, 2, 28))


# ---- access ------------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(field, "sync_status", lambda: {"newest_form": None, "last_ok": None, "status": "ok"})
    monkeypatch.setattr(field, "reps", lambda: [])
    for fn in ("display_rows", "visits", "schedule", "photos", "notes", "stores", "unmapped_questions"):
        monkeypatch.setattr(field, fn, lambda: [])
    monkeypatch.setattr(main.receipts, "status", lambda *a: None)
    db = SessionLocal(); db.query(User).delete(); db.commit(); db.close()
    return TestClient(main.app)


def mk(role, areas, email):
    db = SessionLocal()
    u = User(email=email, name=email, role=role, password_hash="x", active=True,
             allowed_tools=json.dumps(areas), allowed_brands=json.dumps(["MOVA"]) if role == "vendor" else None)
    db.add(u); db.commit(); uid = u.id; db.close()
    return uid


def as_(c, uid):
    c.cookies.set(main.COOKIE_NAME, sign(str(uid), main.APP_SECRET))


PAGES = ["/reports/field", "/reports/field/compliance", "/reports/field/feed", "/reports/field/schedule",
         "/reports/field/visits", "/reports/field/activity", "/reports/field/photos"]


def test_staff_with_the_area_see_every_page(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["field"], "fieldmgr@example.com"))
    for p in PAGES:
        assert client.get(p).status_code == 200, p


def test_staff_without_the_area_are_refused(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("user", ["items"], "other@example.com"))
    for p in PAGES:
        assert client.get(p).status_code == 403, p


def test_vendor_never_reaches_field_even_if_ticked(client):
    mk("admin", [], "boss@example.com")
    as_(client, mk("vendor", ["field", "items"], "mova@example.com"))
    for p in PAGES:
        assert client.get(p).status_code == 403, p
