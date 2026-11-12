"""Desk sector-ETF map and peer baskets.

Source files, committed so they can be edited without a code change:
- lotto/desk/VINCE-SECTOR-MAP-V3.csv  (ticker → sector ETF)
- lotto/desk/VINCE-SECTOR-BASKETS.json (peer baskets)

The flag-0 benchmark map in schwab.SECTORS is a different, smaller table
and is left alone. Mode 1 reads only these files.
"""
import csv
import json
from functools import lru_cache
from pathlib import Path

DESK = Path(__file__).resolve().parent / "desk"
MAP_PATH = DESK / "VINCE-SECTOR-MAP-V3.csv"
BASKET_PATH = DESK / "VINCE-SECTOR-BASKETS.json"


@lru_cache(maxsize=1)
def _map():
    rows = {}
    with MAP_PATH.open(newline="") as handle:
        for row in csv.DictReader(handle):
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            mapped = tuple(part.strip().upper() for part in (row.get("mapped") or "").split(";") if part.strip())
            specific = (row.get("specific_etf") or "").strip().upper()
            rows[ticker] = {"mapped": mapped, "specific": specific, "sector": (row.get("sector") or "").strip()}
    return rows


@lru_cache(maxsize=1)
def _baskets():
    payload = json.loads(BASKET_PATH.read_text())
    baskets = {name: tuple(symbol.upper() for symbol in members)
               for name, members in payload.get("baskets", {}).items()}
    slow = tuple(payload.get("slow_sector_baskets", {}))
    return baskets, slow


def etfs_for(symbol):
    row = _map().get((symbol or "").upper())
    if not row:
        return ()
    ordered = []
    if row["specific"]:
        ordered.append(row["specific"])
    for etf in row["mapped"]:
        if etf not in ordered:
            ordered.append(etf)
    return tuple(ordered)


def primary_etf(symbol):
    etfs = etfs_for(symbol)
    return etfs[0] if etfs else None


def baskets_for(symbol):
    symbol = (symbol or "").upper()
    baskets, _slow = _baskets()
    return tuple(name for name, members in baskets.items() if symbol in members)


def peers_for(symbol):
    symbol = (symbol or "").upper()
    baskets, _slow = _baskets()
    found = []
    for members in baskets.values():
        if symbol not in members:
            continue
        for peer in members:
            if peer != symbol and peer not in found:
                found.append(peer)
    return tuple(found)


def basket_etf(name):
    """Most common specific ETF among the basket's members."""
    baskets, _slow = _baskets()
    counts = {}
    for symbol in baskets.get(name, ()):
        etf = primary_etf(symbol)
        if etf:
            counts[etf] = counts.get(etf, 0) + 1
    if not counts:
        return None
    return max(sorted(counts), key=counts.get)


def symbols_to_quote(universe):
    """ETFs and peers that mode 1 adds to the existing quotes batch."""
    needed = set()
    for symbol in universe:
        needed.update(etfs_for(symbol))
        needed.update(peers_for(symbol))
        for name in baskets_for(symbol):
            etf = basket_etf(name)
            if etf:
                needed.add(etf)
    needed.update({"SPY", "QQQ"})
    return needed
