"""Brand Profitability settings — writes, each one checked and logged.

Every change goes through here so the rules hold everywhere:
  - months are the 1st, valid_to >= valid_from (NULL = open-ended)
  - rules with the same key (region, kind, customer, brand, item category) never overlap,
    so a month has exactly one rate per key; "New rate from <month>" ends the old row the
    month before and starts a new one
  - every add / edit / delete lands in change_log with before -> after and who did it
"""
from datetime import date

from . import calc, db

KIND_LABELS = {"base": "Customer rebate", "brand": "Brand exception", "store": "Store extra",
               "sku": "SKU exception",
               "mdf": "MDF", "gp_target": "GP target (NP % on gross)"}


class Invalid(ValueError):
    pass


def parse_month(v: str | None, required=True) -> date | None:
    v = (v or "").strip()
    if not v:
        if required:
            raise Invalid("A month is required.")
        return None
    try:
        return date.fromisoformat(v[:7] + "-01")
    except ValueError:
        raise Invalid(f"'{v}' isn't a month (YYYY-MM).")


def parse_pct(v) -> float:
    s = str(v or "").strip().rstrip("%").strip()
    try:
        x = float(s)
    except ValueError:
        raise Invalid(f"'{v}' isn't a percentage.")
    if not -100 <= x <= 100:
        raise Invalid("A percentage must be between -100 and 100.")
    return round(x / 100, 6)


def _clean_rule(f: dict) -> dict:
    kind = f.get("kind", "")
    if kind not in db.KINDS:
        raise Invalid("Unknown rule type.")
    region = f.get("region", "")
    if region not in db.REGIONS:
        raise Invalid("Unknown region.")
    r = {"region": region, "kind": kind, "customer_id": f.get("customer_id"),
         "customer_name": (f.get("customer_name") or "").strip(),
         "brand": calc.norm_brand(f.get("brand")),
         "item_category": (f.get("item_category") or "").strip() if kind == "brand" else "",
         "item_code": calc.norm_item(f.get("item_code")) if kind == "sku" else "",
         "pct": parse_pct(f.get("pct")),
         "valid_from": parse_month(f.get("valid_from")),
         "valid_to": parse_month(f.get("valid_to"), required=False),
         "note": (f.get("note") or "").strip()}
    if r["valid_to"] and r["valid_to"] < r["valid_from"]:
        raise Invalid("'To' is before 'From'.")
    if kind == "sku" and not r["item_code"]:
        raise Invalid("Enter the item code.")
    if kind in ("base", "brand", "store", "sku") and r["customer_id"] is None:
        raise Invalid("Pick a customer." if kind != "store" else "Pick a store.")
    if kind in ("brand", "mdf", "gp_target") and not r["brand"]:
        raise Invalid("Pick a brand.")
    if kind in ("base", "store", "sku"):
        r["brand"] = ""
    if kind == "gp_target":
        r["customer_id"], r["customer_name"] = None, ""
    return r


def _check_overlap(conn, r: dict, exclude_id=None):
    clash = db.overlaps(conn, "rate_rule", db.rule_key(r), r["valid_from"], r["valid_to"], exclude_id)
    if clash:
        c = clash[0]
        to = c["valid_to"].strftime("%b %Y") if c["valid_to"] else "open-ended"
        raise Invalid(f"That overlaps an existing {KIND_LABELS[r['kind']].lower()} for the same key "
                      f"({c['valid_from']:%b %Y} – {to}, {float(c['pct']) * 100:g}%). "
                      "End that one first, or use “New rate from”.")


def add_rule(conn, f: dict, by: str) -> int:
    r = _clean_rule(f)
    with conn.transaction():
        _check_overlap(conn, r)
        row = conn.execute(
            "INSERT INTO rate_rule (region, kind, customer_id, customer_name, brand, item_category, item_code, pct, "
            "valid_from, valid_to, note, updated_by) VALUES (%(region)s, %(kind)s, %(customer_id)s, "
            "%(customer_name)s, %(brand)s, %(item_category)s, %(item_code)s, %(pct)s, %(valid_from)s, %(valid_to)s, "
            "%(note)s, %(by)s) RETURNING *", {**r, "by": by}).fetchone()
        db.log(conn, by, "rate_rule", row["id"], "add", after=row)
    return row["id"]


def _get(conn, table, rid):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = %s", (rid,)).fetchone()
    if not row:
        raise Invalid("That row no longer exists.")
    return row


def edit_rule(conn, rid: int, f: dict, by: str):
    with conn.transaction():
        before = _get(conn, "rate_rule", rid)
        # the edit form posts every field; region and type never change
        r = _clean_rule({**f, "region": before["region"], "kind": before["kind"]})
        _check_overlap(conn, r, exclude_id=rid)
        after = conn.execute(
            "UPDATE rate_rule SET customer_id=%(customer_id)s, customer_name=%(customer_name)s, brand=%(brand)s, "
            "item_category=%(item_category)s, item_code=%(item_code)s, pct=%(pct)s, valid_from=%(valid_from)s, valid_to=%(valid_to)s, "
            "note=%(note)s, updated_by=%(by)s, updated_at=now() WHERE id=%(id)s RETURNING *",
            {**r, "by": by, "id": rid}).fetchone()
        db.log(conn, by, "rate_rule", rid, "edit", before=before, after=after)


def change_from(conn, rid: int, month: str, pct, by: str, note: str = "") -> int:
    """End rule rid the month before `month` and start a new rate from `month`, same key."""
    m = parse_month(month)
    with conn.transaction():
        old = _get(conn, "rate_rule", rid)
        if m <= old["valid_from"]:
            raise Invalid(f"The new rate must start after {old['valid_from']:%b %Y} — or edit the row instead.")
        if old["valid_to"] and m > old["valid_to"]:
            raise Invalid(f"That row already ends {old['valid_to']:%b %Y}; add a new row instead.")
        ended = conn.execute("UPDATE rate_rule SET valid_to=%s, updated_by=%s, updated_at=now() "
                             "WHERE id=%s RETURNING *", (calc.month_add(m, -1), by, rid)).fetchone()
        db.log(conn, by, "rate_rule", rid, "edit", before=old, after=ended, note=f"ended for new rate from {m:%b %Y}")
        new = {**old, "pct": pct, "valid_from": m.isoformat(),
               "valid_to": old["valid_to"].isoformat() if old["valid_to"] else "", "note": note or old["note"]}
        r = _clean_rule(new)
        _check_overlap(conn, r)
        row = conn.execute(
            "INSERT INTO rate_rule (region, kind, customer_id, customer_name, brand, item_category, item_code, pct, "
            "valid_from, valid_to, note, updated_by) VALUES (%(region)s, %(kind)s, %(customer_id)s, "
            "%(customer_name)s, %(brand)s, %(item_category)s, %(item_code)s, %(pct)s, %(valid_from)s, %(valid_to)s, "
            "%(note)s, %(by)s) RETURNING *", {**r, "by": by}).fetchone()
        db.log(conn, by, "rate_rule", row["id"], "add", after=row, note=f"new rate from {m:%b %Y}")
    return row["id"]


def delete_rule(conn, rid: int, by: str):
    with conn.transaction():
        before = _get(conn, "rate_rule", rid)
        conn.execute("DELETE FROM rate_rule WHERE id = %s", (rid,))
        db.log(conn, by, "rate_rule", rid, "delete", before=before)


# ---- brand -> PM -------------------------------------------------------------------
def save_pm(conn, f: dict, by: str, pid: int | None = None) -> int:
    region = f.get("region", "")
    if region not in db.REGIONS:
        raise Invalid("Unknown region.")
    brand, pm = calc.norm_brand(f.get("brand")), (f.get("pm") or "").strip()
    if not brand or not pm:
        raise Invalid("Brand and PM are both needed.")
    vf, vt = parse_month(f.get("valid_from")), parse_month(f.get("valid_to"), required=False)
    if vt and vt < vf:
        raise Invalid("'To' is before 'From'.")
    with conn.transaction():
        clash = db.overlaps(conn, "brand_pm", {"region": region, "brand": brand}, vf, vt, pid)
        if clash:
            raise Invalid(f"{brand} already has a PM ({clash[0]['pm']}) from {clash[0]['valid_from']:%b %Y}. "
                          "End that row first, or use “New PM from”.")
        args = {"region": region, "brand": brand, "pm": pm, "vf": vf, "vt": vt,
                "note": (f.get("note") or "").strip(), "by": by}
        if pid is None:
            row = conn.execute("INSERT INTO brand_pm (region, brand, pm, valid_from, valid_to, note, updated_by) "
                               "VALUES (%(region)s, %(brand)s, %(pm)s, %(vf)s, %(vt)s, %(note)s, %(by)s) RETURNING *",
                               args).fetchone()
            db.log(conn, by, "brand_pm", row["id"], "add", after=row)
        else:
            before = _get(conn, "brand_pm", pid)
            row = conn.execute("UPDATE brand_pm SET brand=%(brand)s, pm=%(pm)s, valid_from=%(vf)s, valid_to=%(vt)s, "
                               "note=%(note)s, updated_by=%(by)s, updated_at=now() WHERE id=%(id)s RETURNING *",
                               {**args, "id": pid}).fetchone()
            db.log(conn, by, "brand_pm", pid, "edit", before=before, after=row)
    return row["id"]


def pm_change_from(conn, pid: int, month: str, pm: str, by: str) -> int:
    m = parse_month(month)
    with conn.transaction():
        old = _get(conn, "brand_pm", pid)
        if m <= old["valid_from"] or (old["valid_to"] and m > old["valid_to"]):
            raise Invalid("The change month must fall inside that row's months, after its first month.")
        ended = conn.execute("UPDATE brand_pm SET valid_to=%s, updated_by=%s, updated_at=now() WHERE id=%s RETURNING *",
                             (calc.month_add(m, -1), by, pid)).fetchone()
        db.log(conn, by, "brand_pm", pid, "edit", before=old, after=ended, note=f"ended for new PM from {m:%b %Y}")
    return save_pm(conn, {"region": old["region"], "brand": old["brand"], "pm": pm, "valid_from": m.isoformat(),
                          "valid_to": old["valid_to"].isoformat() if old["valid_to"] else "", "note": ""}, by)


def delete_pm(conn, pid: int, by: str):
    with conn.transaction():
        before = _get(conn, "brand_pm", pid)
        conn.execute("DELETE FROM brand_pm WHERE id = %s", (pid,))
        db.log(conn, by, "brand_pm", pid, "delete", before=before)


# ---- GL accounts + batches ------------------------------------------------------------
def save_account(conn, region: str, account: str, kind: str, by: str):
    account = (account or "").strip()
    if region not in db.REGIONS or kind not in db.GL_KINDS or not account:
        raise Invalid("Region, account and type (freight / vendor rebate) are needed.")
    with conn.transaction():
        before = conn.execute("SELECT * FROM gl_account WHERE region=%s AND upper(account)=upper(%s)",
                              (region, account)).fetchone()
        if before:
            conn.execute("DELETE FROM gl_account WHERE region=%s AND account=%s", (region, before["account"]))
        after = conn.execute("INSERT INTO gl_account (region, account, kind, updated_by) VALUES (%s,%s,%s,%s) "
                             "RETURNING *", (region, account, kind, by)).fetchone()
        db.log(conn, by, "gl_account", f"{region}:{account}", "edit" if before else "add", before=before, after=after)


def delete_account(conn, region: str, account: str, by: str):
    with conn.transaction():
        before = conn.execute("SELECT * FROM gl_account WHERE region=%s AND account=%s", (region, account)).fetchone()
        if before:
            conn.execute("DELETE FROM gl_account WHERE region=%s AND account=%s", (region, account))
            db.log(conn, by, "gl_account", f"{region}:{account}", "delete", before=before)


def delete_batch(conn, bid: int, by: str):
    """Remove an uploaded batch; lines it had superseded come back into force."""
    with conn.transaction():
        b = _get(conn, "gl_batch", bid)
        n = conn.execute("SELECT count(*) AS n FROM gl_line WHERE batch_id=%s", (bid,)).fetchone()["n"]
        conn.execute("UPDATE gl_line SET superseded_by = NULL WHERE superseded_by = %s", (bid,))
        conn.execute("DELETE FROM gl_batch WHERE id = %s", (bid,))
        # a month another batch still covers keeps only that batch's lines (the newest one)
        conn.execute("""
            WITH cover AS (
                SELECT region, month, max(batch_id) AS keep FROM gl_line
                WHERE superseded_by IS NULL GROUP BY region, month)
            UPDATE gl_line l SET superseded_by = c.keep
            FROM cover c
            WHERE l.region = c.region AND l.month = c.month AND l.superseded_by IS NULL
              AND l.batch_id <> c.keep""")
        db.log(conn, by, "gl_batch", bid, "delete", before={**b, "lines": n})
