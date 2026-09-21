"""Synthetic plumbing demonstration. This is not a backtest or real performance."""
from datetime import datetime, timedelta

from .models import Bar, ET, Option, Snapshot


def demo_frames():
    start = datetime(2026, 9, 21, 9, 30, tzinfo=ET)
    bars = []
    volumes = [0] * 5
    for minute in range(1, 56):
        at = start + timedelta(minutes=minute)
        opening = 100 + (minute - 1) * .20
        close = opening + .20
        volume = int(30_000 * 1.05 ** minute)
        total = sum(b.volume for b in bars) + volume
        bars.append(Bar(at, opening, close + .02, opening - .02, close, volume, total / 3))
        options = []
        for index in range(5):
            # Growing demand broadens toward higher strikes over time.
            volumes[index] += int(80 * 1.25 ** minute * (1 + max(0, minute - 15) * index * .10))
            ask = .80 if minute < 40 else 5.0
            bid = ask - .05
            options.append(Option(f"DEMO-{102 + index * 2}C", at.date(), "CALL", 102 + index * 2,
                                  bid, ask, volumes[index], 1000, .30, .06, at))
        yield [Snapshot("DEMO", at, close, at, 94, tuple(bars), tuple(options),
                        .004, at, "SYNTHETIC SECTOR", "synthetic",
                        at.replace(hour=16, minute=0))]
