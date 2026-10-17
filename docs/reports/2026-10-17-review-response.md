# Review of `af33ddc` and full-history filter combination

The reviewer correctly found that a burst of HTTP 429 responses could leave the long-running worker at 2.0-second request spacing indefinitely. Request spacing now decays by 20% after each 50 consecutive successful requests until it returns to the configured baseline; each cycle logs the number of Schwab requests started in the trailing 60 seconds, current spacing, and 429 count. The six-429 / 500-success recovery case is covered by a test.

The capacity warning is credible but the review's *documented 120 market-data requests/minute* premise could not be verified in publicly accessible Schwab primary documentation. The Schwab portal documents a configurable **order** limit, which should not be presented as an established market-data limit. A full 60-name minute can start roughly 60 chain requests, 60 price-history requests, up to 26 rotating sweep chains and a bulk quote request, before cold-start baseline requests or other applications. The unusual-volume scanner normally does approximately six batched quote requests per minute, with additional conditional history requests. An app-wide quota shared by these services is possible, but is unmeasured here. A fixed 105/min local bucket would necessarily prevent 60 fresh histories plus 60 fresh chains every minute; halving candle frequency would weaken one-minute entry timing. Reducing `strikeCount` saves response bytes but not request count and could remove farther-OTM contracts. Those changes need provider evidence and an alternate minute-bar feed before deployment. The new per-cycle telemetry and 429 recovery make this measurable on the next open.

## Matched ACTIVE alerts, Oct 7–16

Outcome: a sampled bid at least 40% above the alert ask within 30 minutes. This is price potential, not a fill or realized trade. "Structural stack" means research-only for 1DTE, any 9:30–9:59 alert, a LEVEL_RECLAIM put, or SPX. The filters were constructed with historical knowledge; no row below is a prospective validation.

| Rule | Kept | +40% potentials | Rate | +100% potentials |
| --- | ---: | ---: | ---: | ---: |
| No cut | 340 | 131 | 38.5% | 47 |
| Current live quality gate | 257 | 121 | 47.1% | 46 |
| Structural stack alone | 178 | 90 | 50.6% | 35 |
| Both | 161 | 87 | 54.0% | 35 |

On Oct 7–15 alone, the combined rule keeps 58/117 (49.6%) versus 79/197 (40.1%) for the live gate. On Oct 16, it keeps 29/44 (65.9%) versus 42/60 (70.0%) for the live gate. Applying both to all sessions would discard 44 of the 131 +40% opportunities and 12 of the 47 +100% opportunities. The extra cuts improve the selected sample's hit rate but sacrifice many big winners; they do not uniformly improve the rate on the most recent session.

1DTE specifically was 16/89 (18.0%) for +40%, including three +100% opportunities. Routing *all* 1DTE ideas to research-only conflicts with the stated 0/1DTE scanner mandate, so this review does not activate it. Opening-window alerts were 4/18 for +40% and 0/18 for +100%; most of that archive predates the latest opening fast-lane, so it is not a valid before/after evaluation of that code. The conditional late-extension rule removed 23 alerts across the history; only 3 reached +40% and none reached +100%, even though the broader late-day window sometimes performed well. Keep that condition under forward review rather than cutting the whole window.

The separate machine-readable per-alert export through Oct 16 is saved in the workspace as `lotto-alert-audit-through-2026-10-16.jsonl`. It has all 433 delivered legacy/ACTIVE rows, with ET time, expiry, setup, alert-time metrics, sampled-bid outcomes, and both filter decisions. Only the 340 ACTIVE rows are used in the matched table above; legacy strategy versions differ and may lack the newer features.
