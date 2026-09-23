# Session examples and limitations

Source: read-only Schwab one-minute regular-session price history, retrieved
September 22–23, 2026. Raw responses remain under ignored `data/research/2026-09-22/`.
Run `scripts/analyze_sessions.py data/research/2026-09-22` to reproduce the
prefix-only price analysis. These are price hypotheses, **not backtested option
alerts**. No historical intraday options tape was supplied.

These examples informed universal rules; they do not replace the 102-name live
universe. SKHY is a research example and is absent from the shared configured
universe. Its inclusion in live scanning would require a universe change.

| Session | Name | Open | Minute-bar close | High | Low |
|---|---|---:|---:|---:|---:|
| Sep 22 | TSM | 441.22 | 452.03 | 452.88 | 441.20 |
| Sep 22 | GOOGL | 357.65 | 351.185 | 364.17 | 350.22 |
| Sep 22 | SKHY | 187.05 | 195.36 | 196.4799 | 186.37 |
| Sep 21 | META | 680.01 | 741.13 | 753.00 | 679.60 |
| Sep 22 | MU | 1032.85 | 1094.41 | 1097.25 | 1030.015 |
| Sep 22 | QQQ | 740.98 | 747.465 | 748.349 | 740.93 |
| Sep 22 | SPY | 774.03 | 773.36 | 775.14 | 772.57 |

The minute-bar close may differ from an official daily closing print. The pasted
TSM example mixed sessions: 437.06 was the Sep 21 low, not the Sep 22 low.

## What was observable earlier

- TSM: opening-break price structure at 9:44 ET near 446.30; a 30-minute
  consolidation hypothesis at 10:19 near 447.2234. The first break's next
  30-minute stock return was only about +0.05%; the coil's was about -0.12%.
- SKHY: opening-break price structure at 9:41 near 191.34, followed by about
  +1.97% over the next 30 minutes. This does not establish option liquidity.
- META Sep 21: continuation price structure at 9:41 near 699.7963, with about
  3.6× cumulative volume pace and +2.04% over the next 30 minutes.
- MU: opening-break price structure at 9:41 near 1052.91; next 30 minutes about
  +2.01%. Cumulative stock volume pace was only about 0.73× at that moment.
- GOOGL: an opening call hypothesis at 9:41 preceded a negative next 30-minute
  return. An early bearish reversal at 10:00 also failed over the next 30
  minutes. Bearish continuation appeared at 11:04 near 359.145; the next
  30-minute directional stock return was about +0.68% for the bearish thesis.
- QQQ: opening-break structure at 9:43 near 744.73. SPY's later continuation
  hypotheses had very small next 30-minute displacement. Relative movement
  and options confirmation must distinguish the stronger candidate.

The first TSM, SKHY, MU and GOOGL price examples did not satisfy a blanket 2×
cumulative stock-volume requirement. A separate local-volume-burst path now
exists, but it still requires historical volume, ATR and options evidence.
These examples are not used to claim that every highlighted timestamp would
have emitted a live alert. Confirmation, score, contract quality, context,
promotion history and budgets still apply.

## Evaluation requirements

Future stock returns are labels stored separately from features known at the
candidate timestamp. Keep failed patterns and matched quiet/flat names. Assess
new thresholds on later sessions before treating changes as improvements.
Actual option returns need captured entry asks and subsequent fresh bids;
closing chain volume does not show when activity arrived or who initiated it.
