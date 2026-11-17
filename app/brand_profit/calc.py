"""Brand Profitability — the CYA workbook's per-line formulas, pure (no I/O).

Reverse-engineered and checked against every line of `CYA Sales_AU_FY27.xlsb` (2026-11-17;
`Reporting Site/brand profitability/DERIVATION.md`):

  rebate %   = base(head office, month)
             + brand exception(head office, brand, month)              [category '']
             + category exception(head office, brand, item category, month)
             + SKU exception(head office, item code, month)            [the NZ CYA's "SKU rebate"]
             + store extra(store, month)
  rebate $   = rebate % x GROSS (credit memos give a negative rebate)
  net sales  = gross - rebate $
  MDF %      = the customer-specific MDF rule for (head office, brand, month) if one exists,
               else the brand's default MDF rule
  MDF $      = MDF % x GROSS
  GP         = net sales - COGS - freight $ - vendor rebate $
  net profit = GP - MDF $
  NP % on gross is what the GP target is measured against (user, 2026-11-17).

Freight and vendor rebates aren't per sales line: they are GL lines by brand and month, added
at brand level. Positive = cost to Macgear, negative = income (a vendor credit).
Brands are compared upper-case (the workbook's pivot merged "Mova" and "MOVA").
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

NO_BRAND = ""
NO_BRAND_LABEL = "- No Brand -"
NO_PM = "(no PM)"


def month_add(m: date, n: int) -> date:
    y, mo = divmod(m.year * 12 + m.month - 1 + n, 12)
    return date(y, mo + 1, 1)


def fy_of(m: date) -> int:
    """Company financial year by its END year: Apr 2026 .. Mar 2027 = 2027 (FY27)."""
    return m.year + 1 if m.month >= 4 else m.year


def fy_months(fy: int) -> list[date]:
    return [month_add(date(fy - 1, 4, 1), i) for i in range(12)]


def norm_brand(b) -> str:
    return (b or "").strip().upper()


def norm_item(c) -> str:
    return (c or "").strip().upper()


def norm_cat(c) -> str:
    c = (c or "").strip()
    return "" if c in ("0",) else c.upper()


class Rates:
    """Rule lookup for one region. Built from rate_rule / brand_pm rows (dicts)."""

    def __init__(self, rules: list[dict], pms: list[dict] | None = None):
        # key -> list of (valid_from, valid_to, value); keys never overlap in time
        self._idx: dict[tuple, list] = defaultdict(list)
        self._mdf_custom: set[tuple] = set()       # (head office, brand) with a customer MDF rule
        for r in rules:
            kind = r["kind"]
            brand = norm_brand(r.get("brand"))
            cat = norm_cat(r.get("item_category"))
            cid = r.get("customer_id")
            if kind == "brand" and cat:
                key = ("category", cid, brand, cat)
            elif kind in ("base", "store"):
                key = (kind, cid)
            elif kind == "sku":
                key = ("sku", cid, norm_item(r.get("item_code")))
            elif kind == "brand":
                key = ("brand", cid, brand)
            elif kind == "mdf":
                key = ("mdf", cid, brand)
                if cid is not None:
                    self._mdf_custom.add((cid, brand))
            elif kind == "gp_target":
                key = ("gp_target", brand)
            else:
                continue
            self._idx[key].append((r["valid_from"], r.get("valid_to"), float(r["pct"])))
        for p in pms or []:
            self._idx[("pm", norm_brand(p["brand"]))].append((p["valid_from"], p.get("valid_to"), p["pm"]))

    def _at(self, key, m: date, default=0.0):
        """The value in force in month m. Should overlaps ever slip in, they add up — the
        workbook's SUMIFS did the same."""
        found = [v for f, t, v in self._idx.get(key, ()) if f <= m and (t is None or m <= t)]
        if not found:
            return default
        if isinstance(found[0], str):
            return found[-1]
        return sum(found)

    def _has(self, key, m: date) -> bool:
        return any(f <= m and (t is None or m <= t) for f, t, _ in self._idx.get(key, ()))

    def rebate_parts(self, m: date, head_office, store, brand: str, category: str, item: str = "") -> tuple:
        brand, category = norm_brand(brand), norm_cat(category)
        base = self._at(("base", head_office), m)
        extra_brand = self._at(("brand", head_office, brand), m)
        extra_cat = self._at(("category", head_office, brand, category), m) if category else 0.0
        extra_store = self._at(("store", store), m) if store is not None else 0.0
        extra_sku = self._at(("sku", head_office, norm_item(item)), m) if item else 0.0
        return base, extra_store, extra_brand, extra_cat, extra_sku

    def rebate_pct(self, m, head_office, store, brand, category, item: str = "") -> float:
        return sum(self.rebate_parts(m, head_office, store, brand, category, item))

    def mdf_pct(self, m: date, head_office, brand: str) -> float:
        brand = norm_brand(brand)
        if (head_office, brand) in self._mdf_custom and self._has(("mdf", head_office, brand), m):
            return self._at(("mdf", head_office, brand), m)
        return self._at(("mdf", None, brand), m)

    def gp_target(self, m: date, brand: str):
        key = ("gp_target", norm_brand(brand))
        return self._at(key, m) if self._has(key, m) else None

    def pm(self, m: date, brand: str) -> str:
        return self._at(("pm", norm_brand(brand)), m, default=NO_PM) or NO_PM


MEASURES = ("qty", "gross", "rebate", "net", "cogs", "freight", "vr", "gp", "mdf", "np")


@dataclass
class Totals:
    qty: float = 0.0
    gross: float = 0.0
    rebate: float = 0.0
    net: float = 0.0
    cogs: float = 0.0
    freight: float = 0.0
    vr: float = 0.0
    gp: float = 0.0
    mdf: float = 0.0
    np: float = 0.0
    # Targets roll up weighted by sales, not as a max (user, 2026-11-17): the target is a % of
    # gross, so a portfolio's target = sum(brand target x brand gross) / sum(gross) — the NP %
    # it would make if every brand hit its own target. Only brands that HAVE a target count:
    #   target_np    sum of target % x gross        (the NP $ the targets ask for)
    #   target_gross gross of the sales with a target
    #   target_hit   NP $ actually made on those brands (their freight / VR / MDF included)
    target_np: float = 0.0
    target_gross: float = 0.0
    target_hit: float = 0.0
    children: dict = field(default_factory=dict)

    def add(self, other: "Totals"):
        for k in MEASURES + ("target_np", "target_gross", "target_hit"):
            setattr(self, k, getattr(self, k) + getattr(other, k))

    @property
    def gp_target(self) -> float | None:
        return self.target_np / self.target_gross if self.target_gross else None

    def as_dict(self) -> dict:
        d = {k: round(getattr(self, k), 2) for k in MEASURES}
        d["qty"] = round(self.qty, 2)
        d["np_net_pct"] = self.np / self.net if self.net else None
        d["np_gross_pct"] = self.np / self.gross if self.gross else None
        d["gp_target"] = self.gp_target
        d["target_np"] = round(self.target_np, 2) if self.target_gross else None
        # performance of the brands that have a target, against that target
        d["vs_target"] = (self.target_hit / self.target_gross - self.gp_target) if self.target_gross else None
        d["vs_target_amt"] = round(self.target_hit - self.target_np, 2) if self.target_gross else None
        d["target_coverage"] = self.target_gross / self.gross if self.gross and self.target_gross else None
        return d


def with_target(t: Totals, target: float | None) -> Totals:
    """Attach a brand's target (a fraction of gross) to one line's or GL line's totals."""
    if target is not None:
        t.target_np = target * t.gross
        t.target_gross = t.gross
        t.target_hit = t.np
    return t


def line_totals(rates: Rates, m: date, head_office, store, brand, category,
                qty: float, gross: float, cogs: float, item: str = "") -> Totals:
    """One sales line (or a group of lines sharing every rule key) through the CYA formulas."""
    reb = rates.rebate_pct(m, head_office, store, brand, category, item) * gross
    mdf = rates.mdf_pct(m, head_office, brand) * gross
    net = gross - reb
    gp = net - cogs
    return Totals(qty=qty, gross=gross, rebate=reb, net=net, cogs=cogs, gp=gp, mdf=mdf, np=gp - mdf)


def gl_totals(kind: str, amount: float) -> Totals:
    t = Totals()
    if kind == "freight":
        t.freight = amount
    else:
        t.vr = amount
    t.gp = -amount
    t.np = -amount
    return t


def build_tree(rates: Rates, sales_rows, gl_rows, months: set[date], customer_names: dict) -> Totals:
    """PM -> brand -> customer (head office) tree for the months asked.

    sales_rows: dicts with month, head_office, store, brand, category, item, qty, gross, cogs
    gl_rows:    dicts with month, brand, kind, amount (freight / vendor rebate lines)
    """
    root = Totals()

    def node(pm: str, brand: str):
        p = root.children.setdefault(pm, Totals())
        return p, p.children.setdefault(brand, Totals())

    for r in sales_rows:
        m = r["month"]
        if m not in months:
            continue
        brand = norm_brand(r["brand"])
        t = with_target(line_totals(rates, m, r["head_office"], r["store"], brand, r["category"],
                                    r["qty"], r["gross"], r["cogs"], r.get("item", "")), rates.gp_target(m, brand))
        p, b = node(rates.pm(m, brand), brand)
        cust = customer_names.get(r["head_office"], str(r["head_office"]))
        b.children.setdefault(cust, Totals()).add(t)
        b.add(t); p.add(t); root.add(t)
    for g in gl_rows:
        m = g["month"]
        if m not in months:
            continue
        brand = norm_brand(g["brand"])
        t = with_target(gl_totals(g["kind"], g["amount"]), rates.gp_target(m, brand))
        p, b = node(rates.pm(m, brand), brand)
        b.children.setdefault("Freight & vendor rebates (GL)", Totals()).add(t)
        b.add(t); p.add(t); root.add(t)
    return root
