# Stock-only stop and stock-to-option integration audit — Oct 26, 2026

## Verified live change

At 12:09 ET, Railway deployment `935eb44d-b521-4b5c-9c34-86e24d8458cd` became Active with `LOTTO_STOCK_FIRST_MODE=shadow`. The worker logged `Stock-first: enabled=True mode=shadow`, `delivery=True`, `hybrid=0`, `Quality policy: mode=enforce policy=S`, and 106 universe symbols with 60 deep slots. Standalone stock WATCH cards are no longer routed to Discord. Stock observations continue in the database. The existing option-led ACTIVE path remains enabled. This was an environment change on the same code revision, not a switch to Hybrid or MOVER.

The worker recorded a fresh snapshot at 12:15 ET and a discovery observation at 12:14 ET after restart. No stock-only card was queued after the restart at the time of inspection; no option card had yet fired after restart. Before restart the database contained 133 sent stock-only cards and 32 sent ACTIVE option cards for the session. Only 9 of the 133 stock cards had a same-symbol, same-side ACTIVE option alert within five minutes. The stock lane and option lane were producing separate streams.

## Why blindly attaching an option is not the fix

Of the 133 stock cards, 76 had no deep stock/option snapshot within three minutes. Another 28 had a deep snapshot but no fresh two-sided 0/1-DTE option quote at that timestamp. Only 29 could be priced within three minutes using saved snapshots. Every one of those 29 had at least one quote passing the proposed 35% spread preference; the primary failure today was coverage and quote availability, not that spread preference.

Using a causal contract choice at the first available snapshot, followed by *sampled* later bids, the 29 priced stock events produced:

| Contract choice | +40% within 15 min | +40% within 30 min |
| --- | ---: | ---: |
| Nearest ATM strike | 9/29 | 11/29 |
| First OTM strike | 10/29 | 11/29 |
| Closest to 0.30 delta | 10/29 | 11/29 |

These are peak-bid opportunity rates, not stop-first win rates or achievable fills. Only 5 of the 25 stock-tier-A cards were priced; 1/5 reached +40% in 15 and 30 minutes. Tier A is not yet a validated quality filter. The 29 contracts are a selected subset: names without a deep snapshot or fresh quote are excluded from the outcome denominator.

An earlier read-only sweep of all 21 saved sessions (217,231 snapshots) tested stock speed thresholds with preselected, quoted options. A strict full two-minute 0.25-ATR push plus 0.25 ATR from open generated 66 priced ideas, with 16 reaching +40% in 15 minutes, 18 in 30 minutes, and 16 reaching +40% before a -28% sampled-bid drawdown. A looser five-minute 0.15-ATR rule generated 710 ideas, 146 reaching +40% in 15 minutes and 203 in 30 minutes. Neither is a justified live send rule. The current unmerged MOVER bypass also had 2/14 +40% results in its Oct 19 smoke replay. These results do not justify turning that bypass on now.

## Code staged on `codex/hybrid-ab-mover`, not live

1. Stock ideas can be prioritized into a bounded deep-chain slot in the same poll cycle (`LOTTO_STOCK_OPTION_PROMOTION=1`). This directly addresses the 76/133 missing-deep cases without increasing the 60-slot request budget. It only changes collection priority; it does not send a card or override option quality rules. It defaults off pending full replay of displaced names and saved contract paths.
2. The stock-led MOVER contract selector keeps the liquid 0.30-delta choice when available. If none meets the spread preference, it falls back to a fresh two-sided ATM or first-OTM quote. The option card explicitly labels a wide spread, including a send-time re-quote. The stock-led MOVER send path remains disabled by default; the stock trigger itself needs a better out-of-sample result before promotion.

## Next release test

Replay the unified path by whole session, measuring distinct stock moves captured, first option quote minute, priced cards per day, +40% at 10/15/30 minutes, -28% stop-first rate, and every lost +100%/+300% winner. Count displaced deep names when stock-priority swaps a slot. Select a stock trigger using held-out sessions, then enable one priced option lane and keep stock-only delivery in shadow. No hard alert cap or fixed 30-minute cooldown is introduced.
