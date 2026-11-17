"""Reconcile the site's brand profitability (warehouse sales + brand_profit settings) with the
CYA workbooks, month x brand, FY27.

    cd "Reporting Site"
    .\\.venv\\Scripts\\python.exe "brand profitability\\reconcile.py" [AU|NZ]    (env as run.ps1 loads it)

The expected figures (cya_fy27_<region>_expected.json) are each CYA re-aggregated with the agreed
differences already applied: AU -> intercompany removed, the Airport West store extra removed;
NZ -> none needed.
Prints every month x brand cell that differs by more than a cent, then the month totals.
"""
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app.brand_profit import calc, data, db  # noqa: E402

FIELDS = ("qty", "gross", "rebate", "cogs", "freight", "vr", "mdf", "np")


def site_cells(region="AU", fy=2027):
    lines = data.sales_lines(region, fy)
    with db.connect() as conn:
        rates = calc.Rates(db.load_rules(conn, region), db.load_pms(conn, region))
        gl, _ = data.gl_lines(conn, region, fy)
    cells = {}
    for r in lines["sales"]:
        t = calc.line_totals(rates, r["month"], r["head_office"], r["store"], r["brand"], r["category"],
                             r["qty"], r["gross"], r["cogs"], r.get("item", ""))
        cells.setdefault((r["month"].isoformat(), calc.norm_brand(r["brand"])), calc.Totals()).add(t)
    for g in gl:
        cells.setdefault((g["month"].isoformat(), calc.norm_brand(g["brand"])), calc.Totals()).add(
            calc.gl_totals(g["kind"], g["amount"]))
    return cells, lines["intercompany"]


def main(region="AU"):
    exp = json.loads((HERE / f"cya_fy27_{region.lower()}_expected.json").read_text())
    want = {(r["month"][:10], r["brand"]): r for r in exp["rows"]}
    got, ic = site_cells(region)
    months = sorted({k[0] for k in want})
    bad = 0
    for key in sorted(set(want) | {k for k in got if k[0] in months}):
        w, g = want.get(key, {}), got.get(key, calc.Totals())
        diffs = {f: (round(w.get(f, 0), 2), round(getattr(g, f), 2)) for f in FIELDS
                 if abs(w.get(f, 0) - getattr(g, f)) > 0.01}
        if diffs:
            bad += 1
            print("DIFF", key, diffs)
    print(f"{bad} month x brand cells differ")
    for m in months:
        tw = {f: sum(r.get(f, 0) for k, r in want.items() if k[0] == m) for f in FIELDS}
        tg = {f: sum(getattr(t, f) for k, t in got.items() if k[0] == m) for f in FIELDS}
        print(m, " ".join(f"{f} {tw[f]:,.2f}/{tg[f]:,.2f}" for f in ("gross", "rebate", "np")),
              f"| intercompany CYA {exp['intercompany'].get(m, 0):,.2f} site {ic.get(date.fromisoformat(m), 0):,.2f}")


if __name__ == "__main__":
    main(sys.argv[1].upper() if len(sys.argv) > 1 else "AU")
