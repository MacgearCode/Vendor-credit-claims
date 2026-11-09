"""Target calculation engine — the single source of truth for computed budgets.

Given a scenario's inputs (global growth, per-brand / per-customer growth overrides,
and optional pacing overrides), computes the full budget grid and roll-ups for a country.

Growth cascade (most specific wins):  customer override -> brand override -> global default.
Pacing weights (sum to 1):            brand override -> global override -> FY26 seasonality.
Monthly target = target_annual * weight[month].
"""
import json
from collections import defaultdict

from . import actuals
from .db import FY_MONTHS

N = 12


def _uniform():
    return [1.0 / N] * N


def _normalize(weights):
    total = sum(weights)
    if total <= 0:
        return _uniform()
    return [w / total for w in weights]


def load_baseline(conn, dataset_id, country):
    """{(brand, customer): [12 monthly amounts]} for a country + financial year of
    NetSuite actuals (dataset_id = the FY's end year; see actuals.py). A copy, so the
    engine can't change the cached actuals."""
    cells = defaultdict(lambda: [0.0] * N)
    for key, months in actuals.cells(dataset_id).get(country, {}).items():
        cells[key] = list(months)
    return cells


def load_scenario_inputs(conn, scenario_id, country):
    """Load growth + pacing overrides for one country of a scenario."""
    growth = {"brand": {}, "customer": {}}
    for row in conn.execute(
        "SELECT level, brand, customer, growth_pct FROM growth_override "
        "WHERE scenario_id = %s AND country = %s",
        (scenario_id, country),
    ):
        if row["level"] == "brand":
            growth["brand"][row["brand"]] = row["growth_pct"]
        else:  # customer
            growth["customer"][(row["brand"], row["customer"])] = row["growth_pct"]

    pacing = {"global": None, "brand": {}}
    tmp_global = {}
    tmp_brand = defaultdict(dict)
    for row in conn.execute(
        "SELECT level, brand, month_idx, weight FROM pacing_override "
        "WHERE scenario_id = %s AND country = %s",
        (scenario_id, country),
    ):
        if row["level"] == "global":
            tmp_global[row["month_idx"]] = row["weight"]
        else:
            tmp_brand[row["brand"]][row["month_idx"]] = row["weight"]
    if len(tmp_global) == N:
        pacing["global"] = _normalize([tmp_global[i] for i in range(N)])
    for brand, months in tmp_brand.items():
        if len(months) == N:
            pacing["brand"][brand] = _normalize([months[i] for i in range(N)])
    return growth, pacing


def load_exclusions(conn, scenario_id, country):
    return {r["brand"] for r in conn.execute(
        "SELECT brand FROM brand_exclusion WHERE scenario_id = %s AND country = %s",
        (scenario_id, country))}


def _parse_months(raw):
    """Return a list of 12 floats if `raw` holds a valid JSON array, else None."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list) and len(parsed) == N:
        try:
            return [float(x) for x in parsed]
        except (ValueError, TypeError):
            return None
    return None


def load_manual(conn, scenario_id, country):
    return [(r["brand"], r["customer"], r["target_annual"], _parse_months(r["months"]))
            for r in conn.execute(
        "SELECT brand, customer, target_annual, months FROM manual_line "
        "WHERE scenario_id = %s AND country = %s ORDER BY brand, customer",
        (scenario_id, country))]


def load_annualize(conn, scenario_id, country):
    return {r["brand"] for r in conn.execute(
        "SELECT brand FROM annualize WHERE scenario_id = %s AND country = %s",
        (scenario_id, country))}


def _first_sale_idx(months):
    """Index of the first month with positive sales, or None if never positive."""
    for i, m in enumerate(months):
        if m > 0:
            return i
    return None


def _annualize_factor(months):
    """Scale factor to turn a partial FY26 (launched mid-year) into a full 12-month
    run-rate: 12 / (months from first sale to Mar). 1.0 for a full year or no sales."""
    first = _first_sale_idx(months)
    if first is None or first == 0:
        return 1.0
    span = N - first        # months from launch to year-end
    return N / span


def _country_seasonality(baseline):
    """Blended full-year shape across the whole country (negatives clamped to 0)."""
    totals = [0.0] * N
    for months in baseline.values():
        for i, m in enumerate(months):
            totals[i] += m
    return _normalize([max(m, 0.0) for m in totals])


def covered_months(conn, dataset_id):
    """Fiscal indices of the year that are complete actuals (all 12 for a closed year)."""
    if dataset_id is None:
        return set()
    return actuals.covered(dataset_id)


def _tracking_factors(actual_cells, ref_cells, covered):
    """How the uploaded (covered) window is tracking vs the reference year in the same
    window, per brand (with a country-wide fallback). Used by the 'tracking' remainder
    method to scale the rest of the year. Returns (brand_factor{}, country_factor).
    brand_factor value is None when that brand had no reference sales in the window."""
    cset = set(covered)
    b_act, b_ref = defaultdict(float), defaultdict(float)
    c_act = c_ref = 0.0
    for (b, _c), m in actual_cells.items():
        s = sum(m[i] for i in cset)
        b_act[b] += s
        c_act += s
    for (b, _c), m in ref_cells.items():
        s = sum(m[i] for i in cset)
        b_ref[b] += s
        c_ref += s
    country_factor = c_act / c_ref if c_ref > 0 else 1.0
    brand_factor = {b: (b_act[b] / b_ref[b] if b_ref.get(b, 0) > 0 else None)
                    for b in set(b_act) | set(b_ref)}
    return brand_factor, country_factor


def _project_cell(brand, customer, months, ref_m, covered, remaining, mode, g, gsource,
                  brand_fac, country_fac):
    """Projection cell: uploaded months stay as ACTUALS; the remaining months are filled
    from the reference year (mode 'flat' = ref × (1+growth); 'tracking' = ref × how the
    covered window is tracking × (1+growth)). Baseline shown is the reference full year,
    so the 'vs' comparison is FY27-projected vs the reference year."""
    target = [months[i] if i in covered else 0.0 for i in range(N)]
    has_basis = ref_m is not None and any(ref_m[i] > 0 for i in remaining)
    if has_basis:
        fac = 1.0
        if mode == "tracking":
            f = brand_fac.get(brand)
            fac = f if f is not None else country_fac
        for i in remaining:
            target[i] = ref_m[i] * fac * (1 + g)
        psource = "cell"
    else:
        # no reference basis for the un-shipped months -> only the actuals; needs manual
        psource = "new" if ref_m is None else "cell"
    base_months = ref_m if ref_m is not None else [0.0] * N
    base_annual = sum(base_months)
    target_annual = sum(target)
    return {
        "brand": brand, "customer": customer,
        "baseline_months": [round(x, 2) for x in base_months],
        "baseline_annual": round(base_annual, 2),
        "growth_pct": round(_pct(base_annual, target_annual), 6),   # vs reference year
        "input_growth": round(g, 6),                                # remainder uplift lever
        "growth_source": gsource,
        "target_months": [round(x, 2) for x in target],
        "target_annual": round(target_annual, 2),
        "delta": round(target_annual - base_annual, 2),
        "annualized_factor": None,
        "projected": ref_m is not None,
        "coverage_frac": None,
        "projection_source": psource,
        "is_manual": False,
    }


def _own_seasonality(months, annual):
    return [m / annual for m in months] if annual > 0 else _uniform()


def _effective_growth(brand, customer, default_growth, growth):
    if (brand, customer) in growth["customer"]:
        return growth["customer"][(brand, customer)], "customer"
    if brand in growth["brand"]:
        return growth["brand"][brand], "brand"
    return default_growth, "global"


def _pacing_for(brand, pacing, default_shape):
    """Resolve the 12 pacing weights for a cell: brand override -> global override -> default."""
    if brand in pacing["brand"]:
        return pacing["brand"][brand]
    if pacing["global"] is not None:
        return pacing["global"]
    return default_shape


def compute_country(conn, scenario_id, default_growth, country, dataset_id,
                    projection_mode=None, ref_dataset_id=None, projection_union=False,
                    base_cells=None):
    """Compute the full grid + roll-ups for one country.

    projection_mode 'flat'/'tracking' (with ref_dataset_id) keeps the uploaded months as
    actuals and fills the remaining months from the reference year — 'flat' = reference
    × (1+growth); 'tracking' = reference × how the covered window is tracking vs the
    reference × (1+growth). No per-line extrapolation, so totals stay well-behaved.
    projection_union=True also pulls in reference-year lines missing from the upload, so
    every historical line gets a rest-of-year forecast (its covered months = 0).

    base_cells: {(brand, customer): [12]} to use instead of a year of actuals (a
    snapshot of official / imported targets, or last year with its gaps filled).

    Returns {country, months, brands[] (each nesting its customers), totals}.
    """
    if base_cells is not None:
        baseline = defaultdict(lambda: [0.0] * N)
        for key, months in base_cells.items():
            baseline[key] = list(months)
    else:
        baseline = load_baseline(conn, dataset_id, country)
    growth, pacing = load_scenario_inputs(conn, scenario_id, country)
    excluded = load_exclusions(conn, scenario_id, country)
    annualize = load_annualize(conn, scenario_id, country)
    manual = load_manual(conn, scenario_id, country)
    country_season = _country_seasonality(baseline)

    proj_active = projection_mode in ("flat", "tracking") and ref_dataset_id is not None
    ref_cells = {}
    covered = set()
    remaining = list(range(N))
    brand_fac, country_fac = {}, 1.0
    keys = sorted(baseline.keys())
    if proj_active:
        ref_cells = load_baseline(conn, ref_dataset_id, country)
        covered = covered_months(conn, dataset_id)
        remaining = [i for i in range(N) if i not in covered]
        if projection_mode == "tracking":
            brand_fac, country_fac = _tracking_factors(baseline, ref_cells, covered)
        if projection_union:
            keys = sorted(set(baseline) | set(ref_cells))

    cells = []
    for (brand, customer) in keys:
        if brand in excluded:
            continue
        months = baseline.get((brand, customer)) or [0.0] * N
        g, source = _effective_growth(brand, customer, default_growth, growth)
        if proj_active:
            cells.append(_project_cell(brand, customer, months, ref_cells.get((brand, customer)),
                                       covered, remaining, projection_mode, g, source,
                                       brand_fac, country_fac))
            continue
        base_annual = sum(months)
        if brand in annualize:
            # scale this cell's partial year to a full run-rate; shape it with the
            # country's full-year seasonality (its own shape has the launch-gap zeros)
            factor = _annualize_factor(months)
            effective_base = base_annual * factor
            default_shape = country_season
        else:
            factor = 1.0
            effective_base = base_annual
            default_shape = _own_seasonality(months, base_annual)
        target_annual = effective_base * (1 + g)
        weights = _pacing_for(brand, pacing, default_shape)
        target_months = [target_annual * w for w in weights]
        cells.append({
            "brand": brand,
            "customer": customer,
            "baseline_months": [round(x, 2) for x in months],
            "baseline_annual": round(base_annual, 2),
            "growth_pct": round(g, 6),
            "input_growth": round(g, 6),
            "growth_source": source,
            "target_months": [round(x, 2) for x in target_months],
            "target_annual": round(target_annual, 2),
            "delta": round(target_annual - base_annual, 2),
            "annualized_factor": round(factor, 4) if factor != 1.0 else None,
            "projected": False,
            "coverage_frac": None,
            "projection_source": None,
            "is_manual": False,
        })

    # Real/projected brands (incl. union-added reference-year lines) — the rest are manual.
    baseline_brands = {c["brand"] for c in cells}
    produced = {(c["brand"], c["customer"]) for c in cells}
    # aggregate each brand's FY26 seasonality so new retailers inherit its monthly shape
    brand_months = defaultdict(lambda: [0.0] * N)
    for (brand, _), months in baseline.items():
        for i, m in enumerate(months):
            brand_months[brand][i] += m
    for brand, customer, target, months in manual:
        if brand in excluded or (brand, customer) in produced:
            continue
        if months is not None:
            # explicit per-month amounts (e.g. mid-year launch); annual = their sum
            target_months = months
            target_annual = sum(months)
        else:
            # pacing precedence: brand override -> global override -> brand seasonality -> uniform
            bmonths = brand_months.get(brand, [0.0] * N)
            weights = _pacing_for(brand, pacing, _own_seasonality(bmonths, sum(bmonths)))
            target_annual = target
            target_months = [target * w for w in weights]
        cells.append({
            "brand": brand,
            "customer": customer,
            "baseline_months": [0.0] * N,
            "baseline_annual": 0.0,
            "growth_pct": None,
            "growth_source": "manual",
            "target_months": [round(x, 2) for x in target_months],
            "target_annual": round(target_annual, 2),
            "delta": round(target_annual, 2),
            "has_months_override": months is not None,
            "is_manual": True,
        })

    return _rollup(country, cells, growth, pacing, baseline_brands, annualize)


def _rollup(country, cells, growth, pacing, baseline_brands, annualize):
    def zero():
        return {"baseline_months": [0.0] * N, "target_months": [0.0] * N,
                "baseline_annual": 0.0, "target_annual": 0.0}

    brands = defaultdict(zero)
    country_tot = zero()
    for c in cells:
        for scope in (brands[c["brand"]], country_tot):
            for i in range(N):
                scope["baseline_months"][i] += c["baseline_months"][i]
                scope["target_months"][i] += c["target_months"][i]
            scope["baseline_annual"] += c["baseline_annual"]
            scope["target_annual"] += c["target_annual"]

    # nest customers under brands
    by_brand = defaultdict(list)
    for c in cells:
        by_brand[c["brand"]].append(c)

    brand_rows = []
    for brand in sorted(brands):
        b = brands[brand]
        is_manual = brand not in baseline_brands
        first_sale = _first_sale_idx(b["baseline_months"])
        # partial-year = launched mid-FY26 (has leading empty months) with real sales
        partial_year = (not is_manual) and b["baseline_annual"] > 0 and bool(first_sale)
        is_annualized = brand in annualize
        brand_cells = by_brand[brand]
        brand_rows.append({
            "brand": brand,
            "baseline_months": [round(x, 2) for x in b["baseline_months"]],
            "baseline_annual": round(b["baseline_annual"], 2),
            "target_months": [round(x, 2) for x in b["target_months"]],
            "target_annual": round(b["target_annual"], 2),
            "delta": round(b["target_annual"] - b["baseline_annual"], 2),
            "growth_pct": round(_pct(b["baseline_annual"], b["target_annual"]), 6),
            "brand_growth_override": growth["brand"].get(brand),
            "has_pacing_override": brand in pacing["brand"],
            "is_manual": is_manual,
            "partial_year": partial_year,
            "is_annualized": is_annualized,
            "annualize_factor": round(_annualize_factor(b["baseline_months"]), 2) if partial_year else None,
            "projected": any(c.get("projected") for c in brand_cells),
            "new_lines": sum(1 for c in brand_cells if c.get("projection_source") == "new"),
            "customers": brand_cells,
        })

    totals = {
        "baseline_months": [round(x, 2) for x in country_tot["baseline_months"]],
        "baseline_annual": round(country_tot["baseline_annual"], 2),
        "target_months": [round(x, 2) for x in country_tot["target_months"]],
        "target_annual": round(country_tot["target_annual"], 2),
        "delta": round(country_tot["target_annual"] - country_tot["baseline_annual"], 2),
        "growth_pct": round(_pct(country_tot["baseline_annual"], country_tot["target_annual"]), 6),
    }

    return {
        "country": country,
        "months": FY_MONTHS,
        "brands": brand_rows,
        "totals": totals,
        "global_pacing_override": pacing["global"],
    }


def _pct(base, target):
    return (target - base) / base if base else 0.0
