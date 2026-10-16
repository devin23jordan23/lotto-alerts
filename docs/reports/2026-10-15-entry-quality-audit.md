# Lotto alert entry audit, inception through October 15, 2026

This audit uses every **delivered** idea in the saved production alert database: 93 legacy alerts over September 25–October 6 and 268 phased ACTIVE alerts over October 7–15. The [trade-by-trade CSV](2026-10-15-all-alerts-since-inception.csv) lists all 361. Outcomes use fresh sampled option **bid** quotes relative to the quoted alert **ask**. A hit means a bid at least 40% above that ask within 30 minutes. A sampled bid high is potential, not a fill or realized return. Legacy and phased rules are different and are shown separately.

| Cohort | Delivered ideas | +40% within 30m | +40% within 10m |
| --- | ---: | ---: | ---: |
| Legacy | 93 | 32 (34.4%) | 22 (23.7%) |
| Phased ACTIVE | 268 | 87 (32.5%) | 33 (12.3%) |

The exact contract selected by a winning alert had a lower fresh ask in the previous ten minutes in 81 of 87 phased winners. The lowest sampled ask was a median eight minutes before the alert and 46% below the alert ask. This is an **oracle low**, measured after knowing the winner and its later high; it is not a tradeable entry rule. It shows why simply finding the right direction is insufficient. At those earlier quotes, the saved scanner decision was commonly blocked by missing price setup (47/87), low stock-volume pace/burst (51/87), or score below threshold (42/87). Several blockers can coexist.

The tempting fix—send a score-80 candidate as soon as it appears—fails the whole-universe replay. Applied to every recorded candidate with a 30-minute same-symbol/side interval, it emitted 1,180 events; only 338 (28.6%) reached +40%. Requiring additional options acceleration did not improve it (269/994, 27.1%). This replay is a screening test, not a perfect reconstruction of the live engine, but it is enough to reject a blanket early-alert bypass. On the *selected contracts the old scanner eventually alerted*, the earlier score-80 ask improved results; that comparison is conditioned on a future alert and must not be used as a live hit-rate claim.

The more defensible immediate cut is an **output tier**, leaving the existing scanner decision path intact. Among the 268 phased alerts, a score at least 90 plus either (a) option response proxy at least 1.1, or (b) directional stock price acceleration at least 0.2 percentage points, retained 71 alerts and 37 +40% hits (52.1%). It retained 16 of the 33 ten-minute hits and 37 of the 87 thirty-minute hits. This rule was selected on the same seven sessions; 52.1% is an in-sample historical figure, **not a validated forward win rate**. It missed worthwhile winners, and October 12–13 remained poor. Its value is reducing public volume while the 197 other original ideas stay tracked internally with their entry asks and future quotes.

| Session | Original ACTIVE | Original +40% | Public-tier subset | Subset +40% |
| --- | ---: | ---: | ---: | ---: |
| Oct 7 | 28 | 13 | 13 | 7 |
| Oct 8 | 43 | 11 | 12 | 8 |
| Oct 9 | 35 | 13 | 10 | 6 |
| Oct 12 | 18 | 7 | 5 | 1 |
| Oct 13 | 29 | 2 | 2 | 0 |
| Oct 14 | 61 | 26 | 18 | 10 |
| Oct 15 | 54 | 15 | 11 | 5 |

Two misses need coverage and setup work, separately from the output tier. MRNA recovered from its opening low while its ranking against the open fell, so it did not receive continuous deep option history until 10:12, after the user's 9:52 idea. The change reserves two dynamic deep slots for strong opening recoveries, uses shorter *observed* option-history windows during the open, and gives the requested liquid names persistent coverage. SPY's October 15 break above 768 illustrates a separate missing short intraday-range trigger; its 10:34–10:39 evaluations had no developing setup and weak stock-volume confirmation. No blanket volume bypass is shipped from this one example; the failed whole-universe early replay shows why.

The new nightly report splits public ACTIVE ideas from internal RESEARCH ideas and measures both. Forward evaluation should compare the actual number of public calls and +40% sampled-bid outcomes against this audit, monitor missed winners among RESEARCH, and reconsider the tier if it loses too much useful coverage. The filter makes no claim that every session will exceed 50%.
