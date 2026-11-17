"""Load a brand profitability seed file (rates, PMs, GL lines) into the brand_profit schema.

    .\\.venv\\Scripts\\python.exe -m app.brand_profit.seed "brand profitability\\seed_fy27_au.json"
    ... --replace     clear that region's rules, PMs and seeded GL lines first

The FY27 AU file was built from the CYA workbook on 2026-11-17 (rate sheets with duplicate rows
summed as Excel's SUMIFS did, customers mapped to NetSuite internal ids, Airport West dropped
from the store extras, the 848 pasted freight / vendor-rebate rows). The last rate of each rule
is left open-ended so it carries on until someone changes it.
Refuses to run over existing settings without --replace: the settings are live data.
"""
import json
import sys
from datetime import date

from . import db


def _d(v):
    return date.fromisoformat(v[:10]) if v else None


def load(path: str, replace: bool = False, by: str = "seed") -> dict:
    data = json.loads(open(path, encoding="utf-8").read())
    region = data["region"]
    with db.connect() as conn, conn.transaction():
        have = conn.execute("SELECT count(*) AS n FROM rate_rule WHERE region = %s", (region,)).fetchone()["n"]
        if have and not replace:
            raise SystemExit(f"{region} already has {have} rate rules — rerun with --replace to overwrite them.")
        if replace:
            conn.execute("DELETE FROM rate_rule WHERE region = %s", (region,))
            conn.execute("DELETE FROM brand_pm WHERE region = %s", (region,))
            conn.execute("DELETE FROM gl_batch WHERE region = %s AND source = 'seed'", (region,))
        for r in data["rules"]:
            conn.execute(
                "INSERT INTO rate_rule (region, kind, customer_id, customer_name, brand, item_category, item_code, pct, "
                "valid_from, valid_to, note, updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (region, r["kind"], r["customer_id"], r["customer_name"], r["brand"], r["item_category"],
                 r.get("item_code", ""),
                 r["pct"], _d(r["valid_from"]), _d(r["valid_to"]), r["note"], by))
        for p in data["pms"]:
            conn.execute("INSERT INTO brand_pm (region, brand, pm, valid_from, valid_to, note, updated_by) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (region, p["brand"], p["pm"], _d(p["valid_from"]), _d(p["valid_to"]), p["note"], by))
        batch = None
        if data.get("gl_lines"):
            batch = conn.execute("INSERT INTO gl_batch (region, source, filename, note, created_by) "
                                 "VALUES (%s, 'seed', %s, %s, %s) RETURNING id",
                                 (region, path.replace("\\", "/").split("/")[-1], data.get("source", ""), by)
                                 ).fetchone()["id"]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO gl_line (batch_id, region, month, document_number, tran_type, brand, memo, kind, amount) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    [(batch, region, _d(g["month"]), g.get("document_number", ""), g.get("tran_type", ""),
                      g["brand"], g["memo"], g["kind"], g["amount"]) for g in data["gl_lines"]])
        counts = {"rules": len(data["rules"]), "pms": len(data["pms"]), "gl_lines": len(data.get("gl_lines", []))}
        db.log(conn, by, "seed", batch or "", "seed", after=counts,
               note=f"{region} from {data.get('source', path)}" + (" (replaced)" if replace else ""))
    return counts


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    print(load(args[0], replace="--replace" in sys.argv))
