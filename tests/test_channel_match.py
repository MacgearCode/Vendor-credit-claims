"""Retailer-name matching between the tool's rebate list, NetSuite price levels and
the sell-through retailers (app/promo_data.py).

The three spell retailers differently — "NZ - Noel Leeming" vs "Noel Leeming",
"JBHiFi" vs "JB HIFI", "NZ - PB Technologies" vs "PB Tech" — and old promo blocks
hold only a name, so these must still match.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import promo_data
from app.promo_data import RetailerEntry, default_rebate, norm_name

NZ = [RetailerEntry("11:18", "Harvey Norman", "NZ", 11, "Harvey Norman", 18),
      RetailerEntry("13:53", "JB HIFI", "NZ", 13, "JBHiFi", 53),
      RetailerEntry("28:192", "Noel Leeming", "NZ", 28, "NZ - Noel Leeming", 192),
      RetailerEntry("28:226", "Warehouse Stationery", "NZ", 28, "NZ - Noel Leeming", 226),
      RetailerEntry("23:208", "PB Tech", "NZ", 23, "NZ - PB Technologies", 208),
      RetailerEntry("12", "Costco", "NZ", 12, "Costco", None),
      RetailerEntry("2", "Dealer 1", "NZ", 2, "Dealer 1", None),
      RetailerEntry("34", "NZ AU - Smart Loyalty / Oppo", "NZ", 34, "NZ AU - Smart Loyalty / Oppo", None)]


def _block(name, level=None, ckey=None):
    return SimpleNamespace(retailer_name=name, price_level_id=level, st_customer_key=ckey,
                           promotion=SimpleNamespace(country="NZ"))


def test_norm_name():
    assert norm_name("JB HIFI") == norm_name("JBHiFi")
    assert norm_name("Noel Leeming") == norm_name("NZ - Noel Leeming")
    assert norm_name("PB Tech") == norm_name("NZ - PB Technologies")
    assert norm_name("Costco Wholesale") == norm_name("Costco")
    assert norm_name("NZ AU - Smart Loyalty") == norm_name("NZ AU - Smart Loyalty / Oppo")
    assert norm_name("Cyclone Computer Co Ltd") != norm_name("Dealer 1")


def test_entry_for_block(monkeypatch):
    monkeypatch.setattr(promo_data, "retailer_entries", lambda country: NZ)
    by = promo_data.entry_for_block
    # stored ids win — two retailers share price level 28
    assert by(_block("anything", 28, 226)).name == "Warehouse Stationery"
    # old blocks by name; a shared level's name picks the sell-through retailer it's named for
    assert by(_block("Noel Leeming")).st_customer_key == 192
    assert by(_block("JB HIFI")).key == "13:53"
    assert by(_block("PB Tech")).key == "23:208"
    assert by(_block("Costco Wholesale")).key == "12"
    assert by(_block("Cyclone Computer Co Ltd")) is None


def test_default_rebate_from_tool_list():
    rebates = [SimpleNamespace(name="Noel Leeming", country="NZ", default_rebate=0.16),
               SimpleNamespace(name="JB HIFI", country="NZ", default_rebate=0.203),
               SimpleNamespace(name="JB HIFI", country="AU", default_rebate=0.19),
               SimpleNamespace(name="Costco Wholesale", country="NZ", default_rebate=0.055)]
    assert default_rebate(NZ[2], rebates) == 0.16
    assert default_rebate(NZ[1], rebates) == 0.203          # NZ row, not AU
    assert default_rebate(NZ[5], rebates) == 0.055          # alias
    assert default_rebate(NZ[3], rebates) == 0.0            # Warehouse Stationery: none listed
