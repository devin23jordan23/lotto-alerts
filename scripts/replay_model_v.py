"""Replay Model V and print cards/day, true win, and +200% miss reasons.

The year-replay win rate is not recomputed here: this box does not have the
Jul 20–Oct 19 chain archive. Pass a JSONL snapshot file when one is available.
Without --input the synthetic demo tape is scored so the harness can be checked.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lotto.config import model_v_settings
from lotto.demo import demo_frames
from lotto.main import replay_frames
from lotto.replay_v import replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="JSONL of snapshot frames")
    parser.add_argument("--runners", type=Path, help="JSON list of +200% runners")
    parser.add_argument("--preset", choices=("combined", "s12", "S0", "S11", "S12"), default=None,
                        help="Score one preset. Default prints combined, s12, S0, and S11.")
    parser.add_argument("--stock-0dte", type=float, default=None, help="Override the s12 stock-led 0DTE bar")
    parser.add_argument("--stock-1dte", type=float, default=None, help="Override the s12 stock-led 1DTE bar")
    args = parser.parse_args()
    overrides = {}
    if args.stock_0dte is not None:
        overrides["post_v2_stock_0dte"] = args.stock_0dte
    if args.stock_1dte is not None:
        overrides["post_v2_stock_1dte"] = args.stock_1dte
    if args.input:
        frames = list(replay_frames(args.input))
    else:
        frames = list(demo_frames())
    runners = json.loads(args.runners.read_text()) if args.runners else None
    if runners:
        from datetime import datetime
        for row in runners:
            if isinstance(row.get("at"), str):
                row["at"] = datetime.fromisoformat(row["at"])
    names = [args.preset] if args.preset else ["combined", "s12", "S0", "S11"]
    out = {}
    for name in names:
        if name == "combined":
            settings = model_v_settings(score_mode="combined", **overrides)
        else:
            settings = model_v_settings(score_mode="s12", preset=name.upper(), **overrides)
        result = replay(frames, settings, runners)
        result["source"] = str(args.input) if args.input else "synthetic demo_frames"
        out[name] = result
    out["vince_cited_not_rerun"] = {
        "S0": "Probability path off. Stock-led cards use the 58/80 v2 bars. Not a Vince final-check row.",
        "S11": "Vince replay, not re-run here: test clean 41.0%, all days 41.8%, 17.8/30.1 cards a day, 29 of 142 runners.",
        "S12": "Vince replay, not re-run here: test clean 40.8%, all days 41.5%, 18.0/30.6 cards a day, 30 of 142 runners.",
        "combined": "Rollback score. Not in Vince's final table.",
    }
    out["note"] = "Local tape only. The year chain archive is not on this box."
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
