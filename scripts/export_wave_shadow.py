"""Export one session of wave_shadow rows to CSV. Read-only; nothing is posted.

Examples:
  python scripts/export_wave_shadow.py --db data/live.sqlite3 --day 2026-11-03 --out wave.csv
  python scripts/export_wave_shadow.py --db data/live.sqlite3 --day 2026-11-03 --family REJECT --out rejects.csv
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lotto.shadow_export import export_wave_shadow


def main():
    parser = argparse.ArgumentParser(description="Export wave_shadow rows for one date")
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--day", required=True, help="Session date YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="CSV destination")
    parser.add_argument("--family", help="Optional family filter, such as REJECT, CARD, F4, F7A")
    args = parser.parse_args()
    count = export_wave_shadow(args.db, args.day, args.out, family=args.family)
    print(f"{count} rows -> {args.out}")


if __name__ == "__main__":
    main()
