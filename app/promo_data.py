"""Vendor Promos reference data, read from the reporting warehouse.

Replaces the products table that n8n filled daily from the NetSuite saved
searches "Master Price Levels_AU/NZ" (/admin/sync-pricing). The warehouse has
the same NetSuite data, loaded by the Sell Through ETL:

  item master  reporting.dim_item        code, description, brand, AUD/NZD RRP, status
  buy prices   reporting.dim_item_price  item x price level x currency (daily ~5am)
  retailers    reporting.dim_ns_customer each NetSuite customer's price level, and
                                         customer_key -> the sell-through retailer
  6-wk avg     reporting.fact_sell_through  retailer-reported weekly units sold

Retailer picker (user, 2026-10-21): one entry per NetSuite price level that has
prices in the promo's currency — except that a level used by sell-through
retailers is listed as those retailers instead (NZ "NZ - Noel Leeming" ->
"Noel Leeming" and "Warehouse Stationery", each with its own sell-through).
A sell-through retailer's price level is the one most of its NetSuite accounts
are on. Rebates stay the tool's own list (`retailers` table), matched by name.

6-wk avg (user, 2026-10-21): sell-through only — units over the retailer's last
six LOADED weeks ending before the promo starts, so a retailer whose latest file
hasn't arrived still gets an average (from the weeks it has), zero weeks count.
Filled when a line is added and stored on it; "Refresh" re-pulls it.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import warehouse

ENTRIES_TTL = 60 * 60            # price levels / customers load daily

_CURRENCY = {"AU": "AUD", "NZ": "NZD"}
# Levels that are not a customer's buy price.
_NOT_BUY_LEVELS = {"Base (RRP Inc)", "Online (Unused)"}
# The other country's levels carry a stray price or two in this currency.
_OTHER_PREFIX = {"AU": "nz - ", "NZ": "au - "}

# Names differ between the tool's rebate list, NetSuite price levels and the
# sell-through retailers ("PB Tech" / "NZ - PB Technologies", "Costco Wholesale" /
# "Costco", "NZ AU - Smart Loyalty" / "NZ AU - Smart Loyalty / Oppo").
_ALIASES = {"pbtech": "pbtechnologies", "costcowholesale": "costco",
            "smartloyaltyoppo": "smartloyalty"}


def norm_name(s: str) -> str:
    """Compare retailer names ignoring case, punctuation and an AU/NZ prefix."""
    s = (s or "").lower().strip()
    for p in ("nz au - ", "nz - ", "au - "):
        if s.startswith(p):
            s = s[len(p):]
            break
    s = "".join(c for c in s if c.isalnum())
    return _ALIASES.get(s, s)


@dataclass(frozen=True)
class RetailerEntry:
    key: str                  # form value: "<level>" or "<level>:<customer_key>"
    name: str                 # shown in the picker and stored on the promo block
    country: str
    price_level_id: int
    price_level: str
    st_customer_key: int | None   # sell-through retailer, None = no feed


def _build_entries(country: str) -> list[RetailerEntry]:
    currency = _CURRENCY[country]
    region = warehouse.region_key(country)
    levels = {lid: name for lid, name in warehouse.query(
        """select price_level_id, min(price_level) from reporting.dim_item_price
           where currency_code = %s and unit_price is not null group by 1""", (currency,))}
    # Each sell-through retailer's accounts in this region, counted per price level.
    counts = warehouse.query(
        """select c.customer_key, dc.customer_name, c.price_level_id, count(*)
           from reporting.dim_ns_customer c
           join reporting.dim_customer dc on dc.customer_key = c.customer_key
           where c.region_key = %s and not c.is_intercompany and c.price_level_id is not null
           group by 1, 2, 3""", (region,))
    weeks = warehouse.recent_weeks(26)
    active = {k for (k,) in warehouse.query(
        """select distinct customer_key from reporting.fact_sell_through
           where region_key = %s and date_key >= %s""",
        (region, int(weeks[0].strftime("%Y%m%d")) if weeks else 0))}

    best = {}                             # customer_key -> (count, -level, name, level)
    for ckey, cname, lid, n in counts:
        if ckey in active and lid in levels:
            cand = (n, -lid, cname, lid)
            if ckey not in best or cand > best[ckey]:
                best[ckey] = cand

    entries, claimed = [], set()
    for ckey, (_, _, cname, lid) in best.items():
        claimed.add(lid)
        entries.append(RetailerEntry(f"{lid}:{ckey}", cname, country, lid, levels[lid], ckey))
    for lid, lname in levels.items():
        if (lid in claimed or lname in _NOT_BUY_LEVELS
                or lname.lower().startswith(_OTHER_PREFIX[country])):
            continue
        entries.append(RetailerEntry(str(lid), lname, country, lid, lname, None))
    return sorted(entries, key=lambda e: e.name.lower())


def retailer_entries(country: str) -> list[RetailerEntry]:
    """The retailer picker for a country. Raises WarehouseUnavailable."""
    country = (country or "AU").upper()
    return warehouse.cached(("promo_retailers", country),
                            lambda: _build_entries(country), ttl=ENTRIES_TTL)


def entry_by_key(country: str, key: str) -> RetailerEntry | None:
    return next((e for e in retailer_entries(country) if e.key == key), None)


def entry_for_block(pr) -> RetailerEntry | None:
    """The picker entry behind a promo retailer block: by its stored price level
    and sell-through retailer, or — for blocks made before 2026-10-21, which hold
    only a name — by name (sell-through retailers win a tie)."""
    entries = retailer_entries(pr.promotion.country)
    if pr.price_level_id is not None:
        return next((e for e in entries if e.price_level_id == pr.price_level_id
                     and e.st_customer_key == pr.st_customer_key), None)
    target = norm_name(pr.retailer_name)
    hits = ([e for e in entries if norm_name(e.name) == target]
            or [e for e in entries if norm_name(e.price_level) == target])
    hits.sort(key=lambda e: e.st_customer_key is None)
    return hits[0] if hits else None


def default_rebate(entry: RetailerEntry, rebate_list) -> float:
    """Rebate from the tool's own retailer list (Retailer rows for the country), by
    the entry's own name — not its price level, which retailers can share."""
    name = norm_name(entry.name)
    for r in rebate_list:
        if r.country == entry.country and norm_name(r.name) == name:
            return r.default_rebate
    return 0.0


# ---- items -------------------------------------------------------------------
def items(codes, country: str, level_ids) -> dict:
    """Item master for many codes at once, with each code's buy price (ex GST) at
    each of the given price levels. {upper(code): {"code", "description", "brand",
    "rrp_inc", "status", "prices": {level_id: price}}}; codes not in NetSuite are
    absent. One round trip each for items and prices. Raises WarehouseUnavailable."""
    country = (country or "AU").upper()
    wanted = sorted({(c or "").strip().upper() for c in codes if (c or "").strip()})
    if not wanted:
        return {}
    out, ns_ids = {}, {}
    for (c, desc, brand, aud_rrp, nzd_rrp, au_status, nz_status, ns_id) in warehouse.query(
            """select i.macgear_code, i.description, i.brand, i.aud_rrp, i.nzd_rrp,
                      i.au_status, i.nz_status, i.ns_item_id
               from reporting.dim_item i
               where upper(i.macgear_code) = any(%s)
               order by (i.ns_item_id is null), i.is_matched desc, i.item_key""", (wanted,)):
        key = c.upper()
        if key in out:                    # first row per code wins (NetSuite, matched)
            continue
        rrp = nzd_rrp if country == "NZ" else aud_rrp
        out[key] = {"code": c, "description": desc or "", "brand": brand or "",
                    "rrp_inc": float(rrp) if rrp is not None else None,
                    "status": (nz_status if country == "NZ" else au_status) or "",
                    "prices": {}}
        if ns_id is not None:
            ns_ids[ns_id] = key
    levels = sorted({l for l in level_ids if l is not None})
    if ns_ids and levels:
        for ns_id, lid, price in warehouse.query(
                """select ns_item_id, price_level_id, unit_price from reporting.dim_item_price
                   where ns_item_id = any(%s) and price_level_id = any(%s)
                     and currency_code = %s and unit_price is not null""",
                (list(ns_ids), levels, _CURRENCY[country])):
            out[ns_ids[ns_id]]["prices"][lid] = float(price)
    return out


def parse_paste(text: str):
    """Rows pasted from Excel (or typed): one product per line, code first, an
    optional second column = % off ("20", "20%", or 0.2 as Excel copies a
    percent-formatted cell's raw value). Returns ([(code, pct_or_None)], [bad lines]);
    a code pasted twice keeps its first row."""
    rows, bad, seen = [], [], set()
    for raw in (text or "").splitlines():
        parts = [p.strip() for p in re.split(r"[\t,;]", raw) if p.strip()]
        if len(parts) == 1 and " " in parts[0]:
            parts = parts[0].split()          # "CODE 20" typed with a space
        if not parts:
            continue
        code, pct = parts[0], None
        if len(parts) > 1:
            v = parts[1].replace("%", "").strip()
            try:
                pct = float(v)
            except ValueError:
                bad.append(raw.strip())
                continue
            if "%" not in parts[1] and 0 < pct < 1:
                pct *= 100                    # 0.2 -> 20%
            if not 0 <= pct <= 100:
                bad.append(raw.strip())
                continue
        if code.upper() not in seen:
            seen.add(code.upper())
            rows.append((code, pct))
    return rows, bad


# ---- 6-week average ------------------------------------------------------------
@dataclass
class SixWeekAvg:
    by_code: dict             # upper(code) -> avg weekly units (codes not sold = 0)
    weeks: list               # week-ending dates used, oldest first
    source: str               # shown beside the figure

    def get(self, code):
        return self.by_code.get((code or "").strip().upper(), 0.0)


def _loaded_weeks(customer_key: int, region: int, upto: date) -> list:
    """The retailer's newest six loaded week-endings on or before upto."""
    candidates = warehouse.recent_weeks(26, upto=upto)
    if not candidates:
        return []
    have = {k for (k,) in warehouse.query(
        """select distinct date_key from reporting.fact_sell_through
           where customer_key = %s and region_key = %s and date_key = any(%s)""",
        (customer_key, region, [int(d.strftime("%Y%m%d")) for d in candidates]))}
    return [d for d in candidates if int(d.strftime("%Y%m%d")) in have][-6:]


def six_week_avg(entry: RetailerEntry | None, codes, promo_start: date) -> SixWeekAvg | None:
    """Average weekly sell-through units per code at this retailer over its last
    six loaded weeks ending before promo_start. None when the retailer sends no
    sell-through or none is loaded yet. Raises WarehouseUnavailable."""
    if entry is None or entry.st_customer_key is None:
        return None
    region = warehouse.region_key(entry.country)
    upto = promo_start - timedelta(days=1)
    weeks = warehouse.cached(("promo_st_weeks", entry.st_customer_key, region, upto),
                             lambda: _loaded_weeks(entry.st_customer_key, region, upto),
                             ttl=warehouse.SELL_THROUGH_TTL)
    if not weeks:
        return None
    codes = sorted({(c or "").strip().upper() for c in codes if (c or "").strip()})
    units = dict(warehouse.query(
        """select upper(i.macgear_code), sum(st.qty_sold)
           from reporting.fact_sell_through st
           join reporting.dim_item i on i.item_key = st.item_key
           where st.customer_key = %s and st.region_key = %s and st.date_key = any(%s)
             and upper(i.macgear_code) = any(%s)
           group by 1""",
        (entry.st_customer_key, region, [int(d.strftime("%Y%m%d")) for d in weeks], codes))) if codes else {}
    n = len(weeks)
    source = f"{entry.name} sell-through, {n} wk{'s' if n != 1 else ''} to {weeks[-1]:%d %b %Y}"
    if n < 6:
        source += f" (only {n} loaded)"
    return SixWeekAvg({c: round(float(units.get(c) or 0) / n, 2) for c in codes}, weeks, source)
