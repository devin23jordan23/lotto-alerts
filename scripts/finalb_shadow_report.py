#!/usr/bin/env python3
"""Export one ET session of the finalb_shadow table to CSV.

Usage:
    python scripts/finalb_shadow_report.py --db data/live.sqlite3 --day 2026-11-17
"""
import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lotto.models import ET

COLUMNS = (
    "id", "ts", "ticker", "side", "live_decision", "live_block_reasons",
    "finalb_decision", "finalb_block_reasons", "live_score", "finalb_score",
    "rule_flags", "cooldown_state", "contract",
    # NULL unless LOTTO_REARM_ATR_MODE=1 and this row is an in-cooldown re-arm.
    "rearm_atr_010", "rearm_atr_015",
)


def _cell(row, column):
    return row[column] if column in row.keys() else None


def session_rows(db_path, day):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "finalb_shadow" not in tables:
            return []
        rows = connection.execute("SELECT * FROM finalb_shadow ORDER BY id").fetchall()
    finally:
        connection.close()
    kept = []
    for row in rows:
        stamp = datetime.fromisoformat(row["ts"])
        if stamp.astimezone(ET).date().isoformat() == day:
            kept.append(row)
    return kept


def main():
    parser = argparse.ArgumentParser(description="Export one day's FINAL-B shadow rows to CSV")
    parser.add_argument("--db", required=True, help="SQLite database that holds finalb_shadow")
    parser.add_argument("--day", help="ET session YYYY-MM-DD; default is today ET")
    parser.add_argument("--out", help="CSV path; default finalb-shadow-<day>.csv")
    args = parser.parse_args()
    day = args.day or datetime.now(ET).date().isoformat()
    out = args.out or f"finalb-shadow-{day}.csv"
    rows = session_rows(args.db, day)
    with open(out, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _cell(row, column) for column in COLUMNS})
    print(f"wrote {len(rows)} rows to {out}")


if __name__ == "__main__":
    main()
