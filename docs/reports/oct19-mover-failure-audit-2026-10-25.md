# Oct 19 MOVER failure audit — saved opening snapshots

This is a **counterfactual MOVER replay**, not the production alert history. It uses 3,633 saved snapshots from 09:31–11:14 ET on Oct 19 and grades a 0DTE entry ask against later *new* sampled bids. A chart of every stock path is [here](images/oct19-mover-14-stock-paths.svg). The chart's vertical axis is the signed stock move in units of prior-day ATR, so up is favorable for calls and puts. The red line is the hypothetical alert minute.

The Hybrid A/B + MOVER replay yielded 14 MOVER and 9 option-led A/B ideas. Two MOVER contracts appeared to reach +40% within 30 minutes. The AMZN 10:10 quote is inconsistent with intrinsic value and cannot count as a validated winner: the 257.5C was quoted at $0.16 while AMZN's saved stock quote was $258.38. The 255C was $0.42 against $3.38 intrinsic at the same snapshot. Stock and option quotes were 15 seconds apart, so this is a *suspect asynchronous price*, not proof of what a real fill was. TSLA 10:14 is the one unambiguous +40% MOVER contract in this partial replay.

| Alert ET | Stock's best move with side next 15m | Selected contract's best bid vs alert ask, 30m | Diagnosis |
|---|---:|---:|---|
| 09:32 NVDA put | +0.48% | −5% | Stock moved, but premium did not expand enough from the $2.84 ask. |
| 09:33 AMZN call | +0.03% | −12% | Opening impulse stalled. Same contract first reached +40% **45 minutes later**. Early entry, not a dead daily thesis. |
| 09:33 AAPL call | +0.35% | −7% | Stock moved, but 345C at $1.78 did not pay; no other eligible same-day strike reached +40%. |
| 09:34 TSLA call | +0.35% | −3% | First impulse pulled back. Contract first reached +40% **44 minutes later**. Early entry, not a dead daily thesis. |
| 09:36 SMH put | +0.05% | −17% | Stock stopped moving almost immediately. |
| 09:36 AVGO put | −0.07% | −10% | Stock reversed against the alert. |
| 09:37 MSFT call | +0.05% | −18% | Stock consolidated. |
| 09:39 META put | +0.37% | −14% | Direction was right, but selected premium failed; best bid reached only +25% by 60m. |
| 09:47 GLD put | −0.09% | −5% | Stock reversed against the alert. |
| 10:02 AMD put | +0.83% | −7% | Stock moved strongly, but $17.50 ask on 542.5P made +40% hard; no eligible alternative reached it within 30m. |
| 10:03 SMH put | +0.09% | −25% | Old 15m move qualified although 2m and 5m speed were just .05 and .06 ATR. |
| 11:05 TSLA call | ≈0% | −6% | Base-break path qualified despite just .02 ATR over 2m and .08 ATR over 5m. |

For comparison, TSLA 10:14 reached +157% on sampled bid within 30m. Its stock moved +0.69% with the side over the next 15m, with SPY also rising. AMZN 10:10 had +0.58% stock follow-through in 15m, but its option return is contaminated by the quote discrepancy. The 12 failed entries all remained below +40% on the **best hindsight eligible same-day strike** in the saved quote paths as well; switching to another strike at the same alert minute does not rescue this sample. This is diagnostic hindsight, not a tradable selector.

### What the trigger actually saw on each chart

All values below are side-signed moves in units of the prior-day ATR, using completed stock bars at the alert minute. A dash means that many minutes of history did not yet exist. “From open” is **not** a measure of current speed.

| Failed entry | From open | Last 2m | Last 5m | Last 15m | Trigger flaw or remaining problem |
|---|---:|---:|---:|---:|---|
| NVDA 09:32 put | .29 | — | — | — | First down bar passed the substituted-open test; the stock later moved, but the $2.84 ask never paid +40% in 30m. |
| AMZN 09:33 call | .29 | .25 | — | — | Initial thrust stalled for 30m. The *same* contract paid after about 45m, so suppressing the ticker for the day would lose it. |
| AAPL 09:33 call | .34 | .26 | — | — | New opening high and a modest further stock move, yet the $1.78 ask never paid +40% in 30m. |
| TSLA 09:34 call | .36 | .28 | — | — | First thrust faded while SPY's available 2m move was against calls; the contract paid only about 44m later. |
| SMH 09:36 put | .27 | .11 | .16 | — | Distance from open passed although the latest 2m was weaker; next 15m favorable stock excursion was only .05%. |
| AVGO 09:36 put | .29 | .07 | .18 | — | Distance from open passed after speed slowed; stock reversed against the put. SPY's available 2m move was against puts. |
| MSFT 09:37 call | .32 | .10 | .19 | — | The opening rise had slowed; next 15m best favorable stock excursion was only .05%. |
| META 09:39 put | .31 | .24 | .38 | — | Real short-side stock movement, but the selected $2.73 put, with a 14% entry spread, failed to reach +40%; no eligible alternate did either. |
| GLD 09:47 put | .29 | .14 | .18 | .32 | Fifteen-minute history existed, but its benefit was already past; stock reversed after alert. |
| AMD 10:02 put | .31 | .12 | .17 | .31 | Genuine stock follow-through (+.83% best in 15m), but the selected put cost $17.50 and no eligible same-day strike reached +40% in 30m. |
| SMH 10:03 put | .49 | .05 | .06 | .27 | A fifteen-minute gain kept PUSH true when the latest five minutes were nearly flat. The selected contract began with a 25% spread. |
| TSLA 11:05 call | .92 | .02 | .08 | .16 | BASE_BREAK accepted a fresh high wick after a pause without recent price speed or a close-through; next 15m did not extend. |

Seven of these twelve had less than .10% favorable stock excursion over the next 15m or reversed (AMZN, SMH twice, AVGO, MSFT, GLD, late TSLA). The other five are **not all flat-stock failures**: NVDA, AAPL, early TSLA, META, and AMD moved .35–.83% favorably at their best within 15m, while their option bids still did not reach +40% within 30m. The audit therefore supports two different repairs: qualify *current* stock motion before posting, and treat a valid stock thesis whose option does not pay as a separate entry/contract problem. The two apparent winners are not equal evidence: AMZN 10:10 has a stock/option intrinsic-value discrepancy, while TSLA 10:14 is a clean sampled-bid winner.

## The code causes

1. `stock_moves()` calls the field `15-minute move`, but when no bar exists 15 minutes back it substitutes the opening price. Before 09:45, PUSH's two tests are effectively the **same .25 ATR from-open test twice**. Eight of the 14 alerts occurred with no full 15-minute reference. A breakout-looking first bar alone can become a priced idea.
2. `spy_move15()` is unavailable in that opening interval; MOVER treats `UNKNOWN` as non-veto. That preserves coverage, but it does not supply confirmation. Several early alerts therefore had **one effective stock test and no market test**.
3. After 09:45, a 15-minute gain can coexist with a nearly flat latest 2–5 minutes. SMH 10:03 is the concrete example. The BASE_BREAK alternative looks for a new bar **high/low wick** after a 15-minute gap, with no minimum near-term speed or close-through requirement. TSLA 11:05 is the concrete example.
4. The contract chooser prefers about .30 delta, falling back to a 0.5%-OTM proxy when Greeks are missing. It checks quote recency and spread but not consistency with contemporaneous intrinsic value. The AMZN 10:10 false-looking +1,643% return is the concrete failure. Nevertheless, no alternative eligible strike made the other 12 into +40% winners in this window.

## Rule work to test across full sessions

The opening needs a **2-minute signal path and a 5-minute continuation/level path**, without pretending that an unfinished 15-minute window exists. The 09:33–09:35 card can be an explicitly risky opening *watch*; a priced scalp idea should require evidence that the move has continued through a completed level, not only a distance from the open. A later upgrade must preserve early AMZN/TSLA rather than suppress them for 30 minutes. After 09:45, PUSH must measure recent 2/5-minute speed and level acceptance; BASE_BREAK needs a close-through and recent speed check. Quote inconsistency must be flagged before counting any option outcome or posting a premium. These are test hypotheses, **not chosen numeric thresholds**.

The single-morning threshold smoke test shows why just changing .25 is insufficient: requiring a full 2-minute reference at .10 ATR yielded 13 stock-led ideas (3 sampled +40% in 30m, including one price-inconsistent record); at .20 ATR it yielded 3 ideas (1 +40%). A full 5-minute reference at .20 ATR yielded 8 ideas (1 +40%). This is too little evidence to pick a live rule. The read-only four-session sweep of Oct 16/19/20/23 is the selection input; it separately counts +40% at 10/15/30 minutes, stock follow-through, suspect option prices, and whether PANW/CRWD/NET are found by 10:00 on Oct 23. A complete Hybrid A/B overlap replay and send-time re-quote check remain necessary before a live win-rate claim.
