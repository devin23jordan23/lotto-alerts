# Lotto scanner: full-history coverage and alert-quality audit (Oct 17, 2026)

## Measurement

I read the production SQLite archive from scanner inception, not only this week's alerts: 18 sessions of snapshots, 433 delivered idea alerts (93 legacy and 340 ACTIVE), 152,581 decisions, and 265,425 chain-coverage records. The comparison below uses the best *sampled bid* within 30 minutes relative to the ask quoted at alert time. It measures potential, not an executable or realized return. A "clean" winner reaches +40% before a sampled bid first falls 28% below the alert ask. Legacy and ACTIVE code versions are reported separately.

| ACTIVE session | Alerts | +40% sampled bid within 30m | After proposed quality check | +40% retained |
| --- | ---: | ---: | ---: | ---: |
| Oct 7 | 28 | 13 | 25 | 12 |
| Oct 8 | 43 | 11 | 22 | 9 |
| Oct 9 | 35 | 13 | 33 | 13 |
| Oct 12 | 18 | 7 | 18 | 7 |
| Oct 13 | 29 | 2 | 14 | 1 |
| Oct 14 | 61 | 26 | 56 | 26 |
| Oct 15 | 54 | 15 | 29 | 11 |
| Oct 16 | 72 | 44 | 60 | 42 |
| **Total** | **340** | **131 (38.5%)** | **257** | **121 (47.1%)** |

Clean +40% before −28%: 101/340 (29.7%) baseline; 92/257 (35.8%) retained. +100% potential: 47/340 baseline; 46/257 retained. The removed set includes **10** +40% winners and **one** +100% winner (AMD Oct 15); this is a trade-off, not a free improvement. The Oct 13 regime remains poor even after filtering. The legacy 93 delivered alerts had 32 +40% potentials; the same retrospective rule would retain 29 of those among 75 alerts, but legacy features and rules differ, so that is not evidence of prospective performance.

The two live checks are observable at alert time: (1) option response proxy under 0.5 *and* directional five-minute stock return under 0.5%, excluding the opening-expansion path, or (2) after 2 p.m. ET, the stock already moved at least one ATR from its open in the proposed direction. Excluded ideas remain in the candidate and decision database as `RESEARCH`; their chosen contracts are pinned for 30-minute quote tracking and included in the nightly report. The active check can be disabled with `LOTTO_ACTIVE_QUALITY_GATE=0`. These thresholds were explored on the available history, including later sessions; the 47.1% figure is **in-sample** and must not be described as a validated future win rate. No test here establishes a 50% live hit rate.

## Why coverage lagged

Every cycle already requests bulk stock quotes for all 106 universe symbols, including AMD and AMZN. The deep path was limited to 30 symbols and fetched price history plus a full option chain **sequentially**, with a self-imposed 0.55-second gap before every API request. Sixty deep symbols alone therefore required at least 66 seconds before network time, more than the one-minute scan cycle; the old 54-second budget silently left later symbols unprocessed. The Oct 16 archive averaged about 30 deep chains per minute.

The new deep pool can hold 60 names, fetches independent histories/chains with six workers, and spaces request starts by 0.25 seconds behind a lock. It retains the entire-universe stock-quote discovery and a rotating option-chain sweep. Names whose 0/1-day chain is known empty for the session do not consume a deep slot, but stay in stock discovery and option-chain sweep; availability resets the next session. Historical chain records show roughly 20 names with eligible 0/1-day contracts Monday through Wednesday and essentially the full universe Thursday and Friday. Thus "60 every minute" is only meaningful when 60 names have eligible expirations. A weekend read-only probe returned 40/40 Schwab chain requests using parallel starts in 12 seconds; this is **not** proof of market-open throughput or a published provider rate limit. HTTP 429 responses back off and retry.

## Forward checks

On the next market session, record requested vs. completed deep chains, full-cycle duration, 429 count, and active alerts by strategy version. If 60-name polling exceeds a minute or Schwab throttles, raise the request interval or lower capacity; do not silently claim 60-name coverage. Compare the `RESEARCH` cohort and sent cohort using the same ask-to-bid 30-minute outcome and the clean-before-28% outcome over multiple new sessions. Preserve the old strategy tag to make this comparison possible.
