"""Build CSV export from the computed grid (single source of truth = calc.py).

scope="full"  -> whole-year detail grid, with H1 (Apr–Sep) and H2 (Oct–Mar) subtotals.
scope="h1"    -> Apr–Sep target only, the sheet released to the sales team for the first half.
"""
import csv
import io

from .db import FY_MONTHS
from . import actuals, base, calc

COUNTRIES = ["Australia", "New Zealand"]
H1_IDX = [0, 1, 2, 3, 4, 5]      # Apr–Sep
H2_IDX = [6, 7, 8, 9, 10, 11]    # Oct–Mar


def _sum(arr, idxs):
    return round(sum(arr[i] for i in idxs), 2)


def _baseline_label(conn, scenario) -> str:
    """Short label for the actuals dataset a scenario is built on (e.g. 'FY26')."""
    fy = scenario.get("actuals_id")
    return actuals.fy_label(fy) if fy else "baseline"


def _ref_label(conn, scenario):
    """Label for the reference year used by a projection (e.g. 'FY26'), or None."""
    if scenario.get("projection_mode") not in ("flat", "tracking"):
        return None
    ref = scenario.get("ref_actuals_id")
    return actuals.fy_label(ref) if ref else None


def _projection_note(conn, scenario):
    """A human note describing the projection in effect, or None."""
    mode = scenario.get("projection_mode")
    ref_label = _ref_label(conn, scenario)
    if not ref_label:
        return None
    label = ("Completed months kept as actuals; rest of year from " + ref_label
             + (" scaled by covered-window tracking" if mode == "tracking" else ""))
    return f"Projection: {label}"


def build_csv(conn, scenario, scope="full") -> str:
    if scope == "h1":
        return _build_h1(conn, scenario)
    return _build_full(conn, scenario)


def _build_full(conn, scenario) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    fy = scenario["target_fy"]
    aid = scenario.get("actuals_id")
    bc = base.cells(conn, scenario)
    mode = scenario.get("projection_mode")
    ref_id = scenario.get("ref_actuals_id")
    proj_note = _projection_note(conn, scenario)
    show_proj = bool(proj_note)
    # In projection mode the baseline columns hold the reference year (full year); otherwise
    # they hold the scenario's own actuals dataset.
    base = _ref_label(conn, scenario) if show_proj else _baseline_label(conn, scenario)

    w.writerow([f"{scenario['name']} — {fy} Budget"])
    w.writerow([f"Baseline: {base} actuals | Global growth: {scenario['default_growth_pct'] * 100:.1f}%"])
    if proj_note:
        w.writerow([proj_note])
    w.writerow([])

    header = ["Country", "Brand", "Customer", "Growth %"]
    header += [f"{m} ({base})" for m in FY_MONTHS] + [f"Annual ({base})"]
    header += [f"{m} ({fy})" for m in FY_MONTHS]
    header += [f"Annual ({fy})", f"H1 Apr-Sep ({fy})", f"H2 Oct-Mar ({fy})", "Delta"]
    if show_proj:
        header += ["Line type"]
    w.writerow(header)

    union = bool(scenario.get("projection_union"))
    grand = {"base": 0.0, "target": 0.0, "h1": 0.0, "h2": 0.0}
    for country in COUNTRIES:
        grid = calc.compute_country(conn, scenario["id"], scenario["default_growth_pct"],
                                    country, aid, mode, ref_id, union,
                                    base_cells=(bc or {}).get(country) if bc is not None else None)
        for brand in grid["brands"]:
            for c in brand["customers"]:
                growth = "new" if c["growth_pct"] is None else f"{c['growth_pct'] * 100:.2f}"
                row = (
                    [country, c["brand"], c["customer"], growth]
                    + c["baseline_months"] + [c["baseline_annual"]]
                    + c["target_months"]
                    + [c["target_annual"], _sum(c["target_months"], H1_IDX),
                       _sum(c["target_months"], H2_IDX), c["delta"]]
                )
                if show_proj:
                    src = c.get("projection_source")
                    row += ["actuals + projected" if src == "cell"
                            else "new (actuals only)" if src == "new" else ""]
                w.writerow(row)
        t = grid["totals"]
        grand["base"] += t["baseline_annual"]
        grand["target"] += t["target_annual"]
        grand["h1"] += _sum(t["target_months"], H1_IDX)
        grand["h2"] += _sum(t["target_months"], H2_IDX)

    w.writerow([])
    w.writerow(["GRAND TOTAL (AU+NZ)", "", "", "",
                *([""] * 12), grand["base"], *([""] * 12),
                round(grand["target"], 2), round(grand["h1"], 2), round(grand["h2"], 2),
                round(grand["target"] - grand["base"], 2)])
    return out.getvalue()


def _build_h1(conn, scenario) -> str:
    """Apr–Sep target release sheet for the sales team."""
    out = io.StringIO()
    w = csv.writer(out)
    fy = scenario["target_fy"]
    h1_months = [FY_MONTHS[i] for i in H1_IDX]

    w.writerow([f"{scenario['name']} — {fy} H1 Targets (Apr–Sep)"])
    w.writerow(["Released to sales team. Oct–Mar (H2) finalised after H1 review."])
    w.writerow([])
    w.writerow(["Country", "Brand", "Customer"] + h1_months + ["H1 Total"])

    grand_h1 = 0.0
    aid = scenario.get("actuals_id")
    bc = base.cells(conn, scenario)
    mode = scenario.get("projection_mode")
    ref_id = scenario.get("ref_actuals_id")
    union = bool(scenario.get("projection_union"))
    for country in COUNTRIES:
        grid = calc.compute_country(conn, scenario["id"], scenario["default_growth_pct"],
                                    country, aid, mode, ref_id, union,
                                    base_cells=(bc or {}).get(country) if bc is not None else None)
        for brand in grid["brands"]:
            for c in brand["customers"]:
                h1_vals = [c["target_months"][i] for i in H1_IDX]
                w.writerow([country, c["brand"], c["customer"]] + h1_vals + [_sum(c["target_months"], H1_IDX)])
        grand_h1 += _sum(grid["totals"]["target_months"], H1_IDX)

    w.writerow([])
    w.writerow(["GRAND TOTAL (AU+NZ)", "", "", *([""] * 6), round(grand_h1, 2)])
    return out.getvalue()
