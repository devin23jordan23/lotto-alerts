# Trader examples and revised research plan

Research only. No scanner logic, settings, deployment, or alerts were changed for this review.

## What we are trying to find

Find developing directional opportunities across the whole configured universe, then select a liquid 0DTE or 1DTE contract positioned to benefit from the plausible stock move and its expected timing. An opportunity can be a rapid option expansion, a recovery toward an earlier extreme, or a sustained move into the close. A new high/low or an extreme close is not required for a successful short-duration trade.

The user's examples are research cases, not a restricted watchlist. Their reported option returns are leads to investigate, not measured returns unless historical contract quotes support them. Opening impulse size, consolidation duration, and round-number levels are features to study, not hardcoded copies of a winning example.

## Sources and limits

- Read-only Schwab regular-session one-minute histories. Existing captures were reused for META and MSFT; missing histories were retrieved for MRVL, GOOGL, BE, QCOM, RKLB, ROK, SNDK, AMD, NET, CRWD, PANW, and $SPX through October 1, 2026. Raw files are in `data/research/2026-10-01/`, with older META/MSFT captures under `data/research/2026-09-29/`.
- The cases below use the dates in the user's examples, with “today” interpreted as October 1, 2026. Price-history timestamps identify the **start** of each minute. A complete 10:00 candle is available at 10:01, not at 10:00.
- `data/research/2026-10-01/trader-case-study-metrics.json` records 21 case windows, their source files, price features at selected completed-bar timestamps, and separate subsequent stock outcomes. Review windows were chosen retrospectively from the user's examples and chart inspection. This is **not** an independently validated strategy backtest.
- Stock features include contemporaneous VWAP, high/low so far, return from the open, five-minute volume against historical same-time volume, and a prior-session 21-period ATR where enough history exists. Future stock returns and favorable/adverse excursions are labels, never input features. No synthetic option returns were inferred from stock returns.
- Minute-bar closes may differ from official closing prints. Historical options quote paths are available only for captured portions of selected cases. A full historical chain and execution-quality backtest remains incomplete for the other sessions.

## Reviewed examples

All prices below were checked against the saved stock/index histories. Option gains mentioned by the user remain unverified unless stated otherwise.

| Case | Verified price sequence | What it teaches us to investigate |
| --- | --- | --- |
| SPY, Oct 1 morning | 9:49 low $762.5925; bounce to $763.97 at 9:52 and $764.04 at 9:55, around contemporaneous VWAP near $764.03. The 9:55 bar closed $763.2816, followed by $762.88 at 9:56 and $762.58 at 9:59. The 10:00 bar closed $761.26; the 10:01 bar closed $760.44. | Established weakness, a failed recovery into VWAP/resistance, then renewed downside. Preparation belongs around the failed bounce, before the large breakdown candle. The reported multimillion-dollar put purchase is not verified by these stock bars. |
| SPX, Oct 1 morning | 9:55 index high 7668.95; 9:59 close 7656.33; 10:00 close 7642.62; 10:01 close 7634.05; 10:05 low 7627.43. | Same broad-market rejection, but SPX is a separate instrument. Its supplied index candles have zero volume: do not manufacture SPX stock-volume or VWAP features. Use an explicitly named traded-market proxy for those inputs. |
| AAOI, Oct 1 | Opening-minute high $100.20 failed; the stock reached $97.77 at 9:46. The 9:53 candle reclaimed $100 and closed $100.76; 9:54 closed $100.95. A 9:56 pullback held $100.05. High $108.28; close $107.28, about 90.5% up the session range. | The useful distinction is failed first crossing versus reclaim, acceptance, and renewed participation. A $100 touch alone would also have generated the opening false start. |
| MRVL, Oct 1 | Low $257.58 at 9:35; 10:30 close $261.705 above VWAP near $261.114; high $270.2799 at 13:30; close $268.01. | Recovery from an early selloff, rebuilding structure, and potential exposure through the user's suggested 262/265 calls. Exact contracts and quote paths still require validation. |
| GOOGL, Oct 1 | Open $350.785; 10:45 close $341.49 below VWAP near $345.034; 11:00 close $340.49; 12:30 close $339.31; session low $335.5108 at 12:54; close $338.26. | Bearish continuation under VWAP with failed recoveries. The low near $336 arrived later than the approximate 12:30 time in the narration. Study deterioration before the next downside leg. |
| META, Sep 21 | Open $680.01; 10:00 close $710.29. The 11:01 pullback low was $706.10 with VWAP near $704.77. Price recovered to $708.495 at 11:05 and $711.47 at 11:15; 14:15 close $744.175. Session high $753; close $741.13. | Initial impulse remains relevant while the stock relaxes. This example held above VWAP rather than requiring an exact VWAP touch. Pullback completion and local resistance recovery matter more than waiting for a new session high. |
| META, Sep 24 | Open $744.355; high $779.8192; close $777.44, about 93.5% up the daily range. MSFT that day traded $491.22–$498.90. | The voice example mentioning 740/750/755 calls and a roughly $775 finish matches META, not MSFT. Study repeated continuation opportunities, not only the final high. |
| MSFT, Sep 25 | Open $498.74; 9:55 high $517.48. Pullback traded below $510 and VWAP: 10:07 low $509.06. The 10:09 close $511.835 reclaimed VWAP near $511.303. Price retested the area, then closed $512.25 at 10:19 and $512.912 at 10:20. 10:55 close $516.105; high $519.40 at 12:56. | Strong opening impulse, premium reset hypothesis, support formation, then reclaim. “Must always stay above VWAP” would reject a useful part of this sequence. First reclaim and later confirmation have different entry cost and false-start risk. |
| QCOM, Sep 25 | Open $195.03; session low $194.55. After the opening run, the pullback reached $198.70 at 10:21, not $192. At 10:23 price reclaimed $200 and closed $200.20; the next bar held $200.01 and closed $200.215. 11:25 high $205.30; session high $205.8532. | Opening strength, pullback near VWAP, reclaim of a significant whole number, and follow-through. Compare the later hold with earlier crossings and failures around $200. |
| RKLB, Sep 24 | Open $69.72; consolidation around $71.4–$72 after the opening advance. Several crossings of $72 around noon failed to hold immediately. By 12:30 the close was $74; high $75.46 at 13:18; close $73.62. ROK was trading above $426 that day. | The “Rockwell” narration matches Rocket Lab/RKLB. Compression beneath a known intraday ceiling can create a candidate before the expansion, but the first tick through $72 was not sufficient confirmation. |
| BE, Sep 29 | Open $273; low $271.20 in the opening minute; 9:45 close $287.54; high $302.354 at 10:29; close $291.245. | An opening momentum opportunity that did not finish at its high. Do not use the closing-location label to erase a potentially useful morning trade. |
| SNDK, Sep 18 | Low $1,616 in the opening minute; 10:00 close $1,697.4999; session high $1,797; close $1,792.83, about 97.7% up the range. | Persistent trend-day behavior with repeated shallow rests. The user's 1,000% options figure is not verified by stock history. The recorded close is about $1,793, not $1,737. |
| AMD, Sep 21 | Open $583.89; 11:00 close $615.545; afternoon pullback low $605.9301 at 13:27; 13:45 close $607.475; 15:00 close $612.29; close $615.36 versus high $616.69. | Strong initial drive, a long afternoon reset, and renewed strength. The user’s approximate 13:15 low occurred closer to 13:27 in the minute data. A new high was not needed for the recovery leg. |
| AMD, Sep 8 | First attempt above $500 failed around 10:30; price was still $497.72 at 10:45. It touched $500.10 at 10:56 and closed $501.16 at 10:58. 11:30 close $508.55; high $512.3569. | Second attempt/reclaim at a major whole number. A reconstruction must use the actual 10:56–10:58 recovery, not award a breakout alert at the approximate narrated 10:45 time. |
| NET, Sep 21 | Open $327; low $321.85; 10:00 close $338.355; high $352.45; close $351.68, about 97.5% up the range. | Opening strength and sustained relative leadership, with group participation as context. |
| CRWD, Sep 21 | Open $231.62; 10:00 close $246.63; high $250.31; close $249.27, about 94.7% up the range. | Strong early move followed by prolonged consolidation and modest later extension. A strong close alone does not tell us a late option entry would have paid. |
| CRWD, Sep 23 | 10:30 close $251.5599 below VWAP near $252.285; 11:00 close $258.60; high $263.19; close $262.50. | Another recovery/expansion case. The last spoken description was incomplete, so the exact intended window remains a lead rather than an assumed contract entry. |
| PANW, Sep 21 | 10:00 close $370.915; high $374.56 at 10:51; 13:00 close $369.08; close $371.62. | The group participated, but PANW did not share NET's all-day advance. This is useful comparative evidence: sector alignment cannot substitute for each name's own structure and remaining opportunity. |
| MU, Oct 1 | Low $1,022.90 at 10:23. A first $1,050 crossing at 12:28 faded. Price reclaimed $1,050 at 12:33, closed $1,051.615 at 12:34, and held a $1,050.79 low at 12:35. High $1,098.90; close $1,097.675. | Recovery, VWAP acceptance, and a whole-number reclaim before a larger move. The first crossing and the stronger second attempt are valuable positive/negative comparisons. |
| SNDK, Oct 1 | Low $1,708 at 10:23; 12:35 close $1,748.20 above VWAP near $1,737.19; high $1,802 at 13:30; close $1,786.91. | A related semiconductor recovery, supporting the need to study group timing and individual entry structure together. |
| MRNA, Oct 1 | Previously reviewed opening minute $186.01–$193.70; high $201 at 10:25; close $188.92. A saved Oct 2 $200 call was $2.40 ask at 9:40 and subsequently reached a sampled $4.15 bid by 10:00. | Opening-drive research needs an early observation path. This particular verified option interval was about +73%, not evidence for an unspecified larger percentage. The stock's later fade distinguishes a fast opportunity from an EOD runner. |

## SPX chain evidence

The October 1 same-day put chain was retrieved successfully after the session and saved as `data/research/2026-10-01/SPX-chain-after-session.json` with its retrieval timestamp. It includes:

| Contract | Provider-reported session high | Per standard 100-multiplier contract |
| --- | --- | --- |
| SPXW Oct 1 7630 put | $22.70 | $2,270 |
| SPXW Oct 1 7635 put | $25.94 | $2,594 |
| SPXW Oct 1 7640 put | $29.50 | $2,950 |

This narrows the user's “76.40” reference to a plausible 7640 put and verifies that nearby contracts reached the quoted order of magnitude. It **does not establish** which contract was bought for $5.80 near 10:00, the time of each session high, or an executable bid-to-ask return. Daily low/high fields must not be combined into an alleged intraday trade. The minute-by-minute option path was not present in the retrieved closing snapshot, and the scanner did not capture SPX historically.

SPX also exposes a scope mismatch in the existing scanner: it is absent from the default stock universe, index options are excluded by the chain parser, and the global $2.50 premium cap would exclude a $5.80 contract. The revised research scope includes SPX with its own instrument assumptions; this review does not silently add it to live scanning.

Historical option quote intervals are available as a separate data product, for example [Cboe Option Quotes](https://datashop.cboe.com/option-quote-intervals), which describes timestamped NBBO and interval trading information. No data purchase or subscription was made.

## Revised setup model

Two broad families remain useful, with distinct subtypes and time horizons:

1. **Momentum expansion:** opening drive, breakout from consolidation, coordinated market/sector expansion, and afternoon continuation. Identify the setup while meaningful room remains, not only after price has extended far beyond the trigger.
2. **Directional reset and resumption:** opening strength followed by a pullback; an established downtrend followed by a failed bounce; recovery from an intraday low; or a prior breakout level being reclaimed. Maintain memory of the original impulse through quiet or temporarily opposing bars.

Whole-number levels are contextual features within both families. Track approach, first test, failure, reclaim, acceptance, retest, and distance to the next relevant level. $100, $200, $500, and $1,050 in these examples are leads, not proof that every integer is meaningful. Scale the distance by price and prior volatility. Compare each whole-number event with non-round nearby levels and failures.

Preserve an internal developing candidate while the structure forms; a trader-facing idea should explain why attention is warranted now. Candidate formation is not an instruction to send a notification every time price touches VWAP or a whole number.

### Evidence available before the next move

- **Session memory:** opening impulse in dollars, percent, and prior ATR units; impulse duration; gap; location relative to open/prior close/premarket and prior-day levels where data exists.
- **Pullback quality:** depth relative to the impulse and ATR; speed; countertrend volume; repeated failures to extend against the original direction; local higher lows/lower highs; interaction with VWAP and known levels. A brief undercut is different from sustained loss of support.
- **Renewed participation:** local volume acceleration, break/reclaim of the pullback range, persistence and breadth across relevant options strikes, and fresh market/sector/peer movement. Option volume is supporting evidence, not automatically buyer-initiated opening activity.
- **Entry geometry:** distance to trigger, plausible first target, and invalidation; how much of the prospective move remains; whether the setup is already extended; time available for the contract to respond.
- **Contract state:** current executable bid/ask, spread, quote freshness, premium reset from earlier same-contract quotes, delta/gamma/IV/theta/vega when available, strike location, and expiry. A prior premium high is context, not a guaranteed recoverable price.
- **Time of day:** early opportunity, midday reset, and late-session continuation require separate evaluation. A universal two-minute wait or final-30-minute exclusion should be tested against each setup family rather than assumed correct.
- **Market cooperation:** joint timing of SPY/QQQ/IWM and sector peers. Group support strengthens a name's thesis, but PANW versus NET shows why its own structure still matters. For SPX, use explicit proxy context rather than pretending the index has stock trading volume.

### Contract choice

Identify a plausible stock path and horizon first, then compare 0DTE and 1DTE contracts that can benefit from it. Study expected response to reachable targets alongside downside response, spread, decay, and remaining time. Avoid a global preference for the cheapest contract or an assumption that 0DTE always wins. Avoid a universal dollar premium cap that eliminates appropriate near-the-money exposure in high-priced underlyings and SPX. Historical repricing estimates require time-appropriate volatility assumptions and must be distinguished from actual recorded quotes.

### Learning and validation

The question for research is: which conditions observable at the candidate time separate profitable option opportunities from similar failed setups?

1. Preserve the complete case list and timestamped source data, including corrected dates/tickers and missing option histories. Add narrated screen recordings as visual evidence with contract/date/time labels.
2. Assemble timestamped contract quotes for all eligible candidates in each study window, including failures and quiet comparison periods. Start with existing saved chains; fill historical gaps from an actual historical source or clearly marked chart evidence. Never invent earlier options quotes from a closing chain.
3. Generate candidates from rules defined before the evaluation session. These retrospectively selected case windows are useful for forming hypotheses, not estimating precision or a win rate.
4. Label executable ask-to-later-bid returns, time to 100/200/300/500% where observed, maximum adverse excursion, time to peak, 5/15/30/60-minute outcomes, and end-of-day persistence separately. Include slippage, latency, stale quotes, transaction costs, and adverse movement before an eventual peak. A sampled peak is not an assumed exit fill.
5. Compare matched failures and nearby false starts: AAOI's opening $100 rejection, QCOM's earlier $200 churn, MU's first $1,050 failure, RKLB's repeated noon breaks, and the failed alerts already documented. Keep symbols, sessions, time of day, volatility, and liquidity comparable.
6. Fit interpretable statistical models for outcome probabilities and timing. Regression can help, but one line fitted only to winning contracts would be misleading. Different setup families and nonlinear interactions may require separate models. Group overlapping observations from the same day/move to avoid pretending they are independent examples.
7. Evaluate on later untouched sessions; report alert precision, missed opportunities, adverse excursion, latency, and coverage. User-selected examples are hypothesis-development data and should not be reused as independent proof.
8. Run proposed changes without changing live alert selection first. Promote a change only after prospective evidence supports it. The nightly collector currently records feedback; it does not autonomously retrain or alter production rules.

## Screen recordings

A recording can show the information the trader noticed before entry: candles, level tests, tape/flow, the exact contract, and premium changes. Useful on-screen details are ticker, trading date, ET clock, option strike/expiration, chart timeframe, and whether a price is last trade, bid, ask, or mark. Natural narration is welcome; the user need not produce a formatted spreadsheet. A directly attached video is preferable if a Loom page cannot be accessed. Recordings clarify selected examples but are not a substitute for a complete, timestamped market dataset when estimating performance.

## Status

- Completed: stock/index reconstruction of the listed cases, clarification of two likely ticker transcription errors, review of the available SPX closing chain, and this revised research plan.
- Additional trader callouts from August 31–September 29 were aligned with their stock charts and the saved September 29 SPY option snapshots in `trader-trade-log-review-2026-10-01.md`. That review distinguishes conditional watches, entries, exits, later peaks, and the different opening, pullback, and afternoon setup families.
- August 18–28 additions in the same trade-log review test an **unbroken level watched before entry** (SPY Aug 28), weak sideways trade before a downside burst (SPY Aug 25), two strikes on the same name (AAPL Aug 19; NFLX Aug 18), and simultaneous competing ideas where one watch or trade failed (MRVL vs AAPL Aug 19; QQQ puts vs AAPL calls Aug 18). These should supply control examples when evaluating candidate selection and avoid labeling every watch as an alert or a win.
- [Additional July–September callouts](trader-trade-log-extra-2026-10-01.md) add an early SPY 772C flow watch that ended near breakeven while AAPL and MSFT calls worked on September 25, separate morning and late SPY 766C reversal legs on August 25, and a WMT weekly overnight swing that must not be mixed into 0/1DTE intraday results. The August 17 “Google 340C at $0.13” conflicts with the intrinsic value implied by both GOOG and GOOGL charts at the heading and is quarantined pending contract verification. Many July/early-August examples fall outside the currently available minute-history window; their chart precursors remain unverified.
- Partial: historical options reconstruction. Existing saved option examples are documented in `feedback-2026-10-01.md`; the new SPX chain confirms daily ranges but not its 10:00 quote path. Most newly listed historical contracts still lack a verified intraday quote sequence.
- Not performed: regression fitting, an independent historical strategy backtest, new live scanner rules, configuration changes, deployment, or trade execution. Those should not be represented as completed by this case-study review.
