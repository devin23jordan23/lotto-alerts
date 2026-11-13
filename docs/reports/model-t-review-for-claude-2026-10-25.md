# Model T review for Andre and Claude — corrected Oct 25, 2026

**Status:** research branch only. No Model T production logic or Railway setting changed in this review. The current Railway `lotto-alerts` deployment is ACTIVE and contains the earlier stock-first lane. No `LOTTO_STOCK_FIRST_*` override is set, so the code defaults to `enabled=true, mode=send`. Sunday logs show zero observed symbols, which is expected outside a trading session; they do not establish that Monday's alerts will work. Model S still controls the legacy priced lane. Model T is not live.

## Decisions from Claude's response

1. Withdraw the pace-persistence hard veto. On 340 sent ACTIVE alerts from Oct 7–16, the 117 it would remove had 30.8% clean +40-before-28 versus 29.1% kept and included 15 contracts that later doubled within 30 minutes. Its removed group was also stronger on the +2-minute ask entry check (30.2% versus 23.8%). This is not a useful blanket filter.
2. Keep the four-rule stack *without* pace as a **demote-to-research candidate**, not a live delete. It kept 227/340 sends at 36.1% clean and removed 113 at 16.8%, while losing 5/47 doubling paths. Under two-consecutive-bid confirmation, kept was 33.0%, removed 13.3%; at a +2-minute ask, kept was 30.4%, removed 17.0%. These are overlapping, in-sample sent-alert comparisons, not an expected live hit rate.
3. Do not declare the stack stable. Its day-level direction reverses on Oct 8 (removed 21%, kept 10%) and Oct 13 (removed 4%, kept 0%); Oct 14 has only one removed alert. H1 is **Oct 7–8 only**, not a broad first half. Rotate held-out days on the full candidate universe before setting tier cut points.
4. `REVERSAL` and `INTRADAY_REVERSAL` both occur in the archive. The against-open exception now covers both. The corrected standalone against-open rule removes only 7 sent alerts, none clean at -28 and none with a +100% path; n=7 cannot justify a universal veto.
5. The sent-alert sample is selected by the current option gates, so it cannot test whether sustained options flow rescues otherwise blocked ideas. Flow remains a candidate feature for the full replay.
6. Andre's latest decision is **directional A/B only**, with a specific Fashionably Late scalp exception. Both legacy `REVERSAL` labels leave the proposed A/B lane. In this sent cohort that removes 36 alerts (22.2% clean versus 30.6% on 304 other alerts) but also removes three contracts that doubled: IWM Oct 8, GOOGL Oct 9 and MU Oct 12. Those losses are visible, not hidden. The Fashionably Late source audit is in `docs/reports/model-t-fashionably-late-source-audit-2026-10-25.md`: the long is a quick turn from a **low** into an upward 9 EMA/VWAP cross, the short mirrors from a **high**, and Grok's v3.15 code reverses those phase anchors. The original PDF's confirmed cross and Grok's pre-cross variant require separate replay. The ordinary reversal label cannot qualify an A/B idea by itself. This changes the proposed model, not today's live Model S behavior.

## What the corrected stock-event check can and cannot say

The local stock-path archive has 179 tracked symbol-days across Sep 25–Oct 16, not the full 60–106-name universe. **96** first appear after 09:32 ET, many at 09:35–09:40. For these, the replay suppresses all session-open, range-extreme and ORB features; it still evaluates prior-day and premarket level crossings. This is stricter than Claude's estimate of 26 late starts because even a 09:35 first row cannot establish the actual opening price or first three-minute range. META Oct 16 starts at 09:55 and must never be treated as an opening observation.

The script now reports the original +0.25/-0.15 daily-ATR stop-first grade, symmetric +0.25/-0.25 and +0.50/-0.50 daily-ATR grades, and a symmetric threshold using only prior observed one-minute volatility. It compares each event to a deterministic random minute on the same symbol/day/side and 30-minute clock bucket, excluding the event's five-minute neighborhood. These controls are descriptive: a random minute in a trend may fall *before* the breakout and is not a causal counterfactual. The event and control denominators differ where no matched minute or intraday-volatility history exists.

| Event | N | +0.25/-0.25 ATR clean | Matched random minute |
|---|---:|---:|---:|
| Prior-day cross | 124 | 21.8% | 22.0% (n=123) |
| Premarket cross | 183 | 20.8% | 25.4% (n=181) |
| ORB3 cross | 115 | 16.5% | 25.2% |
| ORB5 cross | 105 | 14.3% | 22.9% |
| PUSH | 284 | 18.3% | 43.3% |
| BASE_BREAK | 100 | 10.0% | 15.0% |

These numbers **do not disqualify** base breaks, ORBs, or stock-first coverage. They show that scoring the instant of a raw cross as a priced entry is a poor proxy for a trade that waits for acceptance or the first pullback. Intraday-volatility grading changes the base-break rate to 36% (n=100), still below the matched control's 47%; the direction varies with how entry and stop are defined. We must grade the two timestamps separately: an immediate **STOCK THESIS**, then a later **ACTIVE TRADE IDEA** if acceptance or pullback produces a quoted entry. Never assign a hindsight option return to the thesis timestamp.

## Build and replay order

1. Make a read-only four-session snapshot replay for Oct 16/19/20/23. First reproduce real Model S alert IDs and timestamps per day. Count raw stock triggers, unique symbol/side/level setups, entries with a fresh option ask, and actual sends separately. Verify universe coverage by hour on Oct 16, including late-start names.
2. Score full as-of levels: prior-day high/low, premarket high/low, ORB3/5, current day high/low, daily-range position and 15/30/60-minute bases. Compare immediate break, accepted break and first-pullback entries. Test opening variants O1–O4 separately. A missing bar may lower confidence, but should not erase an observable stock move.
3. Test each contract-selection rule independently, including event-time and +2-minute ask, fresh single-bid versus two-observation bid, +40% before -28%/-35%, +100%/+300%, and 5/10/15/30/60-minute horizons. Preserve a list of every doubling path demoted by each rule and inspect Friday's 15 high-score losers row by row.
4. Sweep event re-arm by price distance and new level rather than a fixed cooldown. Measure fetch-on-trigger against a warm strike ladder for actual entry delay and Schwab request load. No fixed sends/day cap.
5. Fit directional A/B cut points on three days and grade the fourth, rotating all four. Separately reconstruct the actual Fashionably Late phase sequence from complete five-minute bars, then test its pre-cross, confirmed-cross and retest entries independently. Its illustrated 10:00–13:30 ET preferred window is **not** a “late-day” definition. Options flow can promote a qualified stock setup; a bad option quote cannot erase a stock thesis. Only the held-out stop-first and winner-retention results determine whether a priced A/B lane replaces Model S. Until then, the four-rule stack is shadow/demotion research.

The local export ends before the full Oct 19/20/23 snapshots. They remain in the Railway database. A read-only export or remote replay is the next data dependency; do not imply this local diagnostic is the requested four-session proof.
