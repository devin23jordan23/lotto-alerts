# Model S release — October 19, 2026

Authorized by Andre: "finish and ship Model S." Baseline/rollback code is
`e5f295129d695c354267c9851502aac9f032c3c4`.

## Routing

`LOTTO_QUALITY_MODE=enforce`, `LOTTO_QUALITY_POLICY=S`.

- MAIN: existing Discord destination.
- SECONDARY: optional `DISCORD_LOTTO_SECONDARY_WEBHOOK`; unset means internal,
  tracked alerts. These use the existing cooldown/rearm state, as in the handoff.
- RESEARCH: tracked research idea, no Discord alert and no new alert cooldown.
- Potential watches remain internal. No new count caps or repeat suppression.

The secondary webhook was absent in the deployment preflight. Consequently the
OPEN rule makes 09:30–09:59 eligible active ideas internal unless a secondary
webhook is subsequently configured. This is explicit Model S routing, not a
coverage repair. SPX is also disproportionately classified SECONDARY by PREM.

## Exact policy

WF: no opening_expansion flag, response_proxy < .50 and directional ret5 < .004.
LATE: ET hour >= 14 and directional move from open >= 1 ATR.
Either is RESEARCH. Remaining rules make SECONDARY:

- OPEN: 09:30 <= ET < 10:00.
- EXHQ: local_rvol_5m > 2.16036 AND price_acceleration < .00039389.
- W1DTE: one calendar day to expiry, setup other than LEVEL_RECLAIM.
- PREM: ask > 1.50 without all five trend-escape conditions.

Escape requires ret5 >= .001, directional extension >= .25 ATR, efficiency >=
.45, relative-market >= 0 and local RVOL >= .8. It overrides only PREM.

Model S replaces the previous .005 weak-response gate in enforce mode. Off or
shadow restores that prior gate. Stack 8 is comparison-only; it cannot enforce.
The implementation does not mutate candidate numerical metrics with policy data.

## Corrected historical benchmark

The actual policy module replays all 433 delivered alerts:

| Policy | MAIN | SECONDARY | RESEARCH | MAIN +40 | MAIN doubles |
|---|---:|---:|---:|---:|---:|
| S | 245 | 103 | 85 | 130 | 58 |
| Stack 8 | 254 | 179 | 0 | 129 | 54 |

S rule fires: WF65, LATE25, OPEN25, EXHQ35, W1DTE77, PREM69.
The handoff's 246/102/85 requires a different RVOL threshold precision. Literal
2.16036 flags QQQ 2026-10-14 11:42, whose RVOL was 2.1603607104117084.
We retain the written rule and correct the benchmark, rather than silently fit
the boundary to preserve that winner. MAIN rate = 130/245 = 53.1%, historical
selected-alert opportunity only. Excluding Oct16: 91/192 = 47.4%.

Reproduce with `scripts/quality_replay.py <full-alert-paths.jsonl> --assert-benchmark`.
No raw private trading archive is committed to GitHub. The immutable export,
scripts and full audit are in the local research output directory.

## Observability and compatibility

Additive tables `quality_shadow`, `quality_quotes`; additive alert columns
`quality_tier`, `quality_policy`, `delivered_at`. Historical entries are not
recast. Original alert/decision timestamps and entry asks remain fixed.

Shadow observations occur before persistence/cooldown suppression and their
eventual live decision is recorded. They do not enter `decisions` or pin extra
symbols for collection. Already collected quotes supply their paths. Actual
MAIN/SECONDARY alerts and RESEARCH ideas use existing outcome tracking.

Nightly output separates policy/tier/rule, watches, alerts, research, and suppressed
candidate observations. Outcomes use sampled bids in 30 minutes and explicit
-28%/+40% ordering; incomplete/gapped nonwinners are not counted as losses.
High-water marks are opportunities, not executable fills or calibrated probabilities.

## Release boundary

No changes to universe, promotion, chain capacity, collection deadlines,
confirmation, cooldown, contract integrity, existing score weights, or entry logic.
No live dip-buy or regression model. Those remain separate review proposals.

Rollback routing: `LOTTO_QUALITY_MODE=off` (prior gate) or `shadow` (prior gate
plus comparisons). Secondary queue destinations remain recorded; internal
secondary alerts are never bulk resent after a configuration change.
The additive schema is compatible with the prior code if code rollback is needed.
