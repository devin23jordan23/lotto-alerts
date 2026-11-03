"""Read-only export of wave_shadow rows for one session."""
import csv
import sqlite3
from pathlib import Path


COLUMNS = (
    "day", "symbol", "at", "family", "side", "bar_end", "trigger", "invalidation",
    "spot", "atr21", "contract", "ask", "bid", "delta", "card_alert_id", "features",
)


def export_wave_shadow(db_path: str, day: str, out_path: str, family: str | None = None) -> int:
    """Write one day's shadow rows to CSV. Opens the database read-only.

    ``family`` limits the file to one kind of row, such as REJECT. Omit it to
    export every family, including posted-card tags and reject reasons.
    """
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wave_shadow'").fetchone()
        if present is None:
            raise SystemExit("wave_shadow table is not in this database")
        query = "SELECT * FROM wave_shadow WHERE day=?"
        args = [day]
        if family:
            query += " AND family=?"
            args.append(family)
        query += " ORDER BY at, symbol, family, side"
        rows = connection.execute(query, args).fetchall()
    finally:
        connection.close()
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in COLUMNS})
    return len(rows)
