"""Seed a Targets scenario from an existing targets workbook (the Power BI-era
PBI_TARGETS.xlsx: Date, Reporting Name, Brand, Sales Rep Cust, Target, Product Manager,
Subsidiary — one row per month x retailer x brand x subsidiary).

The imported targets become the scenario's STARTING POINT (base_kind 'snapshot', rows in
base_line), so the grid shows them exactly as imported at 0% growth and growth % works on
top of them like any other start. It is not committed — that's a person's decision.

Cleaning (the issues found in the 2026-10-13 file):
  - AU->NZ intercompany rows are dropped (they aren't revenue on the site either)
  - brand spellings are matched to NetSuite's brand names (Anker -> ANKER)
  - retailer names are matched to NetSuite's spelling where they differ only in case /
    punctuation (Mwave -> MWAVE), using the warehouse's actuals
  - duplicate rows are added together
Usage (from Reporting Site/, with .env loaded):
    .venv\\Scripts\\python.exe -m app.targets.importer "C:\\...\\PBI_TARGETS.xlsx" --fy FY27
"""
import argparse
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import actuals
from .db import connect

SUBSIDIARY = {"Parent Company : MacGear AU": "Australia", "Parent Company : MacGear NZ": "New Zealand"}
INTERCOMPANY = {"macgear nz - purchases aud", "macgear australia (nzd)"}


def _key(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def read_rows(path: str, fy: int):
    """[(country, brand, customer, fiscal month idx, amount)] for one financial year."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    hdr = [str(h or "").strip().lower() for h in next(it)]
    col = {name: hdr.index(name) for name in ("date", "reporting name", "brand", "target", "subsidiary")}
    a, b = actuals.fy_bounds(fy)
    out = []
    for r in it:
        d = r[col["date"]]
        if not isinstance(d, datetime) or not (a <= d.date() < b):
            continue
        out.append((SUBSIDIARY.get(r[col["subsidiary"]]), r[col["brand"]], r[col["reporting name"]],
                    (d.month - 4) % 12, float(r[col["target"]] or 0)))
    return out


def clean(rows, known_brands, known_customers):
    """-> ({(country, brand, customer): [12]}, report dict)."""
    def canon(known):
        """key -> NetSuite's spelling. Where NetSuite itself has two spellings (MWAVE and Mwave
        are separate customer groups), the one with the most sales wins, so runs agree."""
        weight = known if isinstance(known, dict) else dict.fromkeys(known, 0)
        out = {}
        for name in sorted(weight, key=lambda n: (-weight[n], n)):
            out.setdefault(_key(name), name)
        return out
    brands, custs = canon(known_brands), canon(known_customers)
    # spellings NetSuite has no sales for still merge within the file: Rivacase + RIVACASE
    # -> RIVACASE (the capitalised one, else the most used)
    for known, pos in ((brands, 1), (custs, 2)):
        seen = defaultdict(lambda: defaultdict(int))
        for r in rows:
            name = str(r[pos] or "").strip()
            if name:
                seen[_key(name)][name] += 1
        for k, variants in seen.items():
            if k not in known and len(variants) > 1:
                known[k] = max(variants, key=lambda v: (v == v.upper(), variants[v], v))
    lines = defaultdict(lambda: [0.0] * 12)
    rep = {"dropped_intercompany": 0.0, "dropped_no_country": 0.0, "brand_renamed": {}, "customer_renamed": {}}
    for country, brand, cust, idx, amt in rows:
        cust = str(cust or "").strip() or "Other"
        if cust.lower() in INTERCOMPANY:
            rep["dropped_intercompany"] += amt
            continue
        if country is None:
            rep["dropped_no_country"] += amt
            continue
        b = brands.get(_key(brand), str(brand or actuals.NO_BRAND).strip())
        if b != brand:
            rep["brand_renamed"][str(brand)] = b
        c = custs.get(_key(cust), cust)
        if c != cust:
            rep["customer_renamed"][cust] = c
        lines[(country, b, c)][idx] += amt
    return lines, rep


def seed(conn, name: str, target_fy: str, lines: dict, period=(0, 11), who="import",
         label="Imported targets") -> int:
    sid = conn.execute(
        "INSERT INTO scenario (name, target_fy, default_growth_pct, actuals_fy, created_by, "
        "period_start, period_end, start_from, base_kind, base_label) "
        "VALUES (%s,%s,0,NULL,%s,%s,%s,'import','snapshot',%s) RETURNING id",
        (name, target_fy, who, period[0], period[1], label)).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO base_line (scenario_id, country, brand, customer, month_idx, amount) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(sid, c, b, cu, i, round(x, 2)) for (c, b, cu), m in sorted(lines.items())
             for i, x in enumerate(m) if x])
    conn.commit()
    return sid


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("path")
    ap.add_argument("--fy", default="FY27")
    ap.add_argument("--name", default=None)
    ap.add_argument("--period", default="0-11", help="fiscal months, e.g. 0-8 = Apr-Dec")
    a = ap.parse_args()
    fy = 2000 + int(a.fy.upper().removeprefix("FY"))
    rows = read_rows(a.path, fy)
    known_b, known_c = defaultdict(float), defaultdict(float)     # name -> sales, last + this year
    for y in (fy - 1, fy):
        for country in actuals.cells(y).values():
            for (b, c), m in country.items():
                known_b[b] += sum(m); known_c[c] += sum(m)
    lines, rep = clean(rows, known_b, known_c)
    tot = defaultdict(lambda: [0.0] * 12)
    for (c, _, _), m in lines.items():
        tot[c] = [x + y for x, y in zip(tot[c], m)]
    p0, p1 = (int(x) for x in a.period.split("-"))
    with connect() as conn:
        sid = seed(conn, a.name or f"{a.fy.upper()} current targets (imported)", a.fy.upper(), lines, (p0, p1),
                   label=f"Imported from {Path(a.path).name}")
    print(f"scenario {sid}: {len(lines)} lines")
    for c, m in tot.items():
        print(f"  {c}: {round(sum(m)):,}  by month {[round(x) for x in m]}")
    print("  dropped intercompany", round(rep["dropped_intercompany"]), "| no country", round(rep["dropped_no_country"]))
    print("  brands renamed", rep["brand_renamed"])
    print("  customers renamed", rep["customer_renamed"])


if __name__ == "__main__":
    main()
