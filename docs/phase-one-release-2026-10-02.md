# Phase-one lotto scanner release — October 2, 2026

Phase one is the first live research release built from the trader's July–October examples. It is designed to surface a small number of potential 0DTE/1DTE option ideas earlier while preserving fresh quotes, spread, delta/gamma, contract, and trigger-quality checks. It places no orders.

The live rules now recognize opening drive, opening break, pullback resumption, intraday reversal, level reclaim, recovery continuation, coiled continuation, and continuation. The opening lane can evaluate after five completed bars. A candidate must still have a fresh chain, eligible expiry, valid spread, positive bid, positive gamma, reachable strike, and synchronized activity. A one-minute confirmation uses two distinct completed bars; polling jitter does not add extra confirmation time.

The score floor is 70, with a local-improvement path for names whose stock volume is improving against their own recent tape. This initial release had caps of eight ideas per day, two per cycle, and two per ticker; the same-day uncapped follow-up removed those count ceilings after they blocked later qualifying ideas. The 30-minute ticker cooldown remains. Entries must be close to the setup trigger; extended entries are rejected. Pullback trades need room to retest the prior extreme relative to invalidation. Final-30-minute ideas require a triggered setup, tighter spread, minimum delta, limited extension, and a higher score.

SPY, QQQ, and IWM retain continuous deep chain slots. The full 102-name universe still receives quote discovery and rotating chain coverage; the chain sweep target is every name within roughly three minutes under normal request speed. The configured Railway service remains one replica with its persistent `/app/data` volume and live Discord delivery enabled.

The contract price ceiling remains $2.50 for normal names. It scales up to `min($7.50, 0.25 × prior ATR)` for high-priced, high-volatility underlyings such as MU, while retaining both the 15% and $0.15 spread limits. This addresses the MU miss without allowing arbitrary expensive contracts. The chosen out-of-the-money strike must be within one prior ATR.

The worker now stores every evaluated expiry/side candidate, its blockers, full settings, and strategy version. Nightly reports include all candidate evaluations, blocker counts, first sampled doubling, sampled invalidation, and the best bid before invalidation. Trader feedback can be recorded separately with `python -m lotto.main feedback --alert-id ID --outcome worked|failed|mixed`.

## Replay evidence

The phase-one replay covered 12 saved case exports and 786 snapshots. It reproduced the existing IWM call opportunity, recognized an earlier SPY/QQQ afternoon recovery, recognized the MU high-priced call lane, and rejected the extended IWM put through the trigger-distance rule. It continues to expose losing META and SPY ideas in the report; those are retained as controls. MRNA and AAOI remain unqualified in their sampled exports because their available contracts failed activity or liquidity requirements. The replay is a selected-case regression check, not an independent performance estimate.

The machine-readable replay is [phase1-replay.json](../data/research/phase1-replay.json), and the broader trade-log evidence is [the additional trader callout review](trader-trade-log-extra-2026-10-01.md). Historical option snapshots remain sampled quotes, not executable fills.

## Validation

`python3 -m unittest discover -s tests -q` passes 64 tests. `git diff --check` passes. The Railway configuration was read without exposing secrets: the production service is `lotto-alerts`, branch `main`, current service status was running before this release, the webhook and Schwab broker variables are present, and `/app/data` is mounted. The release should be deployed from the commit below, then checked with `python -m lotto.main check` through the worker logs after Railway completes.

This release does not claim a calibrated win rate, 500% prediction probability, dealer positioning, signed flow, or automatic self-modification. Feedback is collected as labeled research data; thresholds remain versioned and require review before future changes.
