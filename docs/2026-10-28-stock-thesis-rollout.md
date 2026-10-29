# Stock thesis and nearest listed expiry — Oct 28

## Production observations that drove this change

- ORCL had 353 stored stock snapshots on Oct 28, yet every snapshot held zero
  options. The previous request stopped at 1 DTE. ORCL was +$14.18 / +2.23
  prior ATR from the open at 3:39 ET. The first qualifying breakout minute and
  a tradable Friday option path were not exported, so the historical ORCL
  counterfactual remains unverified.
- Oct 28 AMZN 255C at 10:21 had only about +0.10 ATR from the open. The SPX
  7750C at 1:06 had about +0.10 ATR from the open and weak local volume.
  TSLA 402.5C at 3:32 was in the opposite direction from its open and had a
  15-minute range of about 0.08 ATR. These cards motivated the flat-stock
  check. The exact three-bar values were not available in this workspace.
- The existing fallback could promote an EMA pattern match without requiring
  an actual directional stock thesis. The new strict check applies to that
  path and to the stock-event path.

## Live behavior

- The same Schwab chain request now reaches the upcoming Friday weekly.
  This adds expirations without doubling request count. The routine fitted
  option model still uses 0/1 DTE; a stock event may use the nearest listed
  expiry within seven days.
- The stock-event path selects the nearest-expiry fresh ATM contract with a
  real bid and ask. Spread, delta, and option volume do not veto the event;
  spread and stock volume appear on the card. Send-time direct requoting still
  withholds a card if a fresh quote cannot be obtained.
- A stock event must have a completed-bar directional move from open and in
  the last three bars, a fresh same-side level or local range break (or a
  confirmed trend pullback), and a break-bar volume burst. A strong move
  through a frozen level can proceed when historical volume is absent.
- Routine option cards need a named setup and are held when both the
  from-open and recent three-bar move are under 0.10 ATR in their direction.
  REVERSAL and INTRADAY_REVERSAL are held under the user's directional-trade
  instruction. A raw additive score cannot override those checks.
- No stock-only alert is sent. LOTTO_STOCK_THESIS_ENABLED=0 restores the prior
  routing, including the old 0/1DTE handoff selection.

## Historical check and limits

`python3 scripts/replay_stock_motion.py` reads the saved alert, stock, and bid
paths through Oct 16. It matched 463 sent alerts with stock paths and measured
the best later bid within 15 minutes against the alert ask. Under the proposed
motion-plus-setup gate:

| Group | Alerts | Bid +40% in 15m | Bid +100% in 15m |
| --- | ---: | ---: | ---: |
| Kept | 358 | 95 | 30 |
| Held | 105 | 18 | 5 |

The 15-minute +40% rate changes from 113/463 (24.4%) to 95/358 (26.5%)
within this older cohort. The gate sacrifices 18 fast +40% paths and five
fast doubles while holding 87 alerts that did not reach +40%. Historical ATR
was generally the stored prior ATR, while live may use ATR21. These sessions
span earlier strategy versions; this is a preservation check, not a validated
forecast for the Oct 28 production model. The new stock-event lane has only
synthetic integration coverage until today's full quote paths can be read.

## Checks

- `python3 -m unittest discover -s tests`
- `python3 scripts/replay_stock_motion.py`
- `git diff --check`

The Oct 28 full delivered-card inventory and true ORCL option returns still
need a production DB export. This change must not be reported as a proven ORCL
winner or a 50% model.
