# VINCE: additive weights v4c (v4b + Nia's EMA terms). Default cutoff MIN 60
Vince, Mon 2026-10-26, ~5:30 PM ET. Research only: no branch, push, or deploy, and no live DB. Weights file: `VINCE-ADDITIVE-WEIGHTS-v4c.json`, a new file. v3, v4, v4b and v5 are untouched.

**Source.** The terms come from Nia's files only: NIA-EMA-ADDITIONS.md/.csv, -stats and -instack, using the 4-bar reclaim window per her 5:25 addendum. ace-ema-weights.json was not used.

**Join.** Year cards join Nia's card_id → pool_cid. Bypass cards join on cid. Coverage is 99.1% of year cards, 98% of the bypass pool and 85% of the bypass rows inside A. On matched rows, date, ticker, side and minute agree 100%. Missing rows get 0.

**Grading.** Same as v4b: Nia3 blockers and the liquid-contract roll, true win = +40% before −35%, entry at the ask at m+2. Fit is before 2026-07-20, test is on or after (51 days). Runners are counted of 144.

## Terms
| Term | Stack | Primary | Bypass | Why |
|---|---|---|---|---|
| 2-min 21 EMA dip-reclaim (DRC_21_2), calls | ≥2 of level/RVOL1.5/SPY-with/ovol200 | **+6 calls**, puts 0 | 0 | Fit +7.8 (n58), test +12.7 (n50) on our pool. Test doesn't drop at 45 or 60. |
| 5/10-min 9/21 EMA dip-reclaim (DRP_510), puts | ≥4 other positive | **0** (shadow) | **+6 puts** (+9 is the upside) | Primary lift holds (fit +9.0 n163, test +18.0 n79), but +6 adds only losing cards over the cutoff: MIN 60 goes 681/1605 → 681/1606 and MIN 45 goes 762/1830 → 763/1833. Stacked cards already clear the cutoff. On the bypass, +6 is a no-op. +9 gives bypass ≥40 49.1% (n55) vs 46.9% (n49). |
| 2-min 9 EMA pull-in before 10:00, calls | ≥2 conf | **0** (shadow) | 0 | Our pool: fit −0.6 (n30), test −9.6 (n10). +6 lowers MIN 60 test. Nia's +11.4/+7.1 is on her unrolled pool. |
| 21 EMA pull-ins 2/5/10m, 2-min 9 EMA dip-reclaim, call dip-reclaims 5/10m | — | 0 | 0 | Fail on Nia's year test. Not refit. |

E1×RVOL1.5 (+6) and all other v4b weights are unchanged.

## Frontier (test; win is deduped true win; runners of 144)
| | MIN 45 win / cards/day / runners | + bypass ≥40 | MIN 60 win / cards/day / runners | + bypass ≥40 |
|---|---|---|---|---|
| v4b | 41.3% / 36.84 / 25 | 41.4% / 37.45 / 27 | 42.4% / 31.02 / 20 | 42.4% / 31.76 / 22 |
| **v4c** | 41.3% / 36.88 / 25 | 41.4% / 37.49 / 27 | 42.4% / 31.02 / 20 | 42.4% / 31.76 / 22 |
| alt: v4c + bypass DRP +9 | same | 41.4% / 37.53 / 27 | same | 42.5% / 31.80 / 22 |
| alt: all three at +6 (Nia) | 41.3% / 36.96 / 25 | 41.5% / 37.55 / 27 | 42.3% / 31.06 / 20 | 42.4% / 31.80 / 22 (42.36 vs 42.44 undeduped) |

Bypass ≥40 alone: v4b and v4c are both 46.9% (n49, 0.96/day). The +9 alt is 49.1% (n55, 1.08/day).

**Read.** At MIN 45 and 60 these terms are close to a no-op. They only fire with 2–4+ other positives, and those cards mostly score above 60 already. They move 0–6 cards and no runners. Nia tested ~400 combinations, so treat them as shadow-check sized.

## Scripts and files (run dir `vince/live-check-runs/run-20261026-1610-atr21-refit/`)
- **Fit and frontier.** `code/v4c_nia.py` writes `v4c_nia_frontier.csv`.
- **Abandoned loose self-built EMA check.** This is `code/v4c_feats.py`, `code/v4c.py`, `v4c_grid.csv` and `v4c_nia_xcheck.txt`. My loose 0–1 shapes fired on 30–70% of cards vs Nia's 2–9%, so they're too loose to use, and it was stopped per the steer. Directionally it agreed: put 5/10-min dip-reclaims and 2-min 21 call dip-reclaims were positive with stacks.
- **Bar data.** No bars missing for any pool ticker.
