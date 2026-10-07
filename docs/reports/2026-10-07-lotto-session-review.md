# Lotto scanner session review — October 7, 2026

Source: production Railway nightly report `/app/data/nightly/2026-10-07/report.json`, generated 4:08 p.m. ET, plus saved intraday option quotes. All times below are ET. The scanner covered 104/104 names with no nightly data or option-bar errors. Every listed record is marked `sent` in the scanner database, meaning its webhook request returned without an error. The system does not save a Discord message ID, so this does not prove that each message is visible in the channel.

## Result at a glance

| Measure | Result |
|---|---:|
| Potential watches | 30 |
| Watches directly promoted to active | 6 (20%) |
| Watches without a direct `parent_id` promotion | 24 (80%) |
| Of those 24, watches with an active idea on the same symbol and side sometime that day | 13 |
| Of those 24, watches with no active idea on the same symbol and side that day | 11 records, 7 distinct contracts |
| Active ideas | 28: 6 promoted, 22 direct |
| Active labels | 4 Strong, 11 Worked, 8 Marginal, 1 Late pop, 4 Flat/failed |
| Active option trade highs ≥50% within 30 minutes | 14/28 |
| Active option trade highs ≥100% within 30 minutes | 4/28 |
| Watches lacking a direct promotion with trade high ≥50% within 30 minutes | 12/24 |
| Those reaching +50% before sampled stock invalidation | 10/24 records, 8 distinct symbol/contracts |
| Watches lacking a direct promotion with sampled bid positive at 30 minutes | 6/24; median −33% |

“Strong” means a ≥100% one-minute option trade high within 30 minutes. “Worked” means a ≥30% high within 30 minutes **and** at least $0.10 premium gain. “Late pop” means a later ≥50% high without meeting that early rule. The watch labels describe hindsight potential, not trades or scanner activation. One-minute trade highs may be isolated prints and are not executable exits. The sampled bid at 30 minutes is a separate quote-based check from the alert ask; it is not a fill. Repeated alerts on the same contract are correlated, so these counts are alerts, not independent trades.

**Terminology correction:** The earlier phrase “unlinked watches” meant only that the watch record was not the `parent_id` of a later active record. It did **not** mean that the ticker never produced an active alert. For example, the AMZN 257.5C watch at 9:57 a.m. and the active AMZN 257.5C at 11:09 a.m. share a contract, but the latter has no `parent_id` and is counted as a fresh direct active idea. Among the 24 records without a direct promotion, 13 had some same-symbol, same-direction active idea during the day. Only 11 watch records, representing seven contracts, had no same-direction active alert at any point.

**AMZN watch and active idea:** The trader confirmed the 9:57:06 a.m. ET AMZN 257.5C **Potential Trade Watch** is visible in Discord. It recorded a $0.32 ask, stock $255.40, watch trigger $255.47, and invalidation $254.39. The same contract received a separate **Active Trade Idea** at 11:09:43 a.m. ET at $0.27 ask, visible around 11:10 a.m. The 9:57 watch and 11:09 active idea were separate setups; they were not a single standing price-trigger order.

The 9:57 watch's +712% eventual high and the 11:09 active idea's +863% eventual high used the **same contract and later price run**. They are overlapping measurements, not two independent trade opportunities. The watch had already been invalidated before the later active idea; its eventual peak must not be read as a good 9:57 entry.

## Watch lifecycle study

Potential and active are classifications of the **current** qualifying setup, not obligatory stages of a funnel. The scanner records a potential watch when the stock is still below its directional setup trigger. It may record an active idea directly when the trigger is already met. Both must still pass stock-volume, options, context, quality, and two-distinct-bar confirmation checks. A prior watch is not sufficient on its own. The confirmation key is symbol, option side, and expiry rather than a fixed named setup, so setup names and trigger levels may change between qualifying minutes.

On AMZN, the 9:56 a.m. qualifying candidate was an already-triggered level reclaim, still awaiting confirmation. At 9:57 the selected candidate became a developing reversal and generated the potential watch. At 9:58 the stock was above a level-reclaim trigger, but stock-volume pace failed, so it did not activate. The watch's sampled invalidation occurred at 10:27. At 11:08–11:09 fresh qualification and confirmation produced the active level-reclaim idea at $0.27.

Of today's 28 active ideas, six were direct watch promotions, 11 had an earlier same-direction watch without a direct parent link, and 11 had **no earlier same-direction watch**. Six of 30 watches preceded a same-direction active idea within 30 minutes; 16 of 30 had a +50% one-minute option trade high in 30 minutes, but only 10 had a positive sampled bid at 30 minutes. These are correlated research observations, not fills or independent trades. Removing watches from the main Discord stream reduces 30 of 58 posts while retaining their internal records for later comparison. The next study should distinguish a watch that crossed its trigger but failed other checks, a watch whose setup changed, and a watch invalidated before a new leg.

## Watch-to-active transitions

| Watch → active | Watch ask → active ask | Delay | Active 30m high | Active bid at 30m | Read |
|---|---:|---:|---:|---:|---|
| 11:09 AM MU 1080C → 11:11 AM MU 1080C | $2.57 → $2.71 | 2.1m | +55% | -15% | Worked |
| 12:48 PM MU 1090C → 12:49 PM MU 1095C | $2.67 → $1.62 | 1.0m | +98% | +36% | Worked |
| 1:43 PM AAPL 337.5C → 1:47 PM AAPL 337.5C | $0.25 → $0.29 | 4.0m | +55% | +14% | Worked |
| 2:12 PM TSLA 375P → 2:13 PM TSLA 375P | $0.23 → $0.20 | 1.1m | +50% | -50% | Marginal |
| 2:48 PM AMZN 260C → 2:52 PM AMZN 260C | $0.23 → $0.30 | 4.0m | +3% | -60% | Flat/failed |
| 3:26 PM GOOGL 350C → 3:27 PM GOOGL 350C | $0.29 → $0.38 | 1.0m | +134% | +76% | Strong |

Four of six promoted ideas were labeled Worked/Strong, one Marginal, and one Flat/failed. The AMZN 260C watch at 2:48 p.m. was $0.23; activation four minutes later at $0.30 left only a +3% later trade high and a −60% sampled bid at 30 minutes. That is the clearest example of promotion arriving after the useful window. The GOOGL 350C activation had a +134% later high but a −58% sampled bid at five minutes, so even a strong peak involved substantial near-term risk. The MU 1090C watch at 12:48 p.m. promoted to a different 1095C contract at 12:49 p.m.; comparisons use each alert’s own ask and contract.

## Watches without a direct promotion: were they missed trades?

Ten watches without a direct promotion reached a +50% one-minute trade high within 30 minutes **before** the scanner’s sampled stock invalidation. That count includes names with other active ideas. Of the 11 watch records with **no same-direction active idea all day**, seven reached that early +50% threshold, representing five distinct contracts: AAPL 332.5P, AMD 640P, INTC 115C (two watches) and 112P (two watches), and IWM 278C. Those five contracts are the cleaner set for reviewing possible missed activation logic. Some were fast scalps: AAPL 332.5P at 10:44 a.m. had a +79% high but its sampled bid went from +46% at five minutes to −21% at 30; INTC 115C at 10:17 a.m. went from +63% at five minutes to −37% at 30.

Other watches lacking a direct promotion still merit timing review, but not the label “never alerted”: AMD 645C at 12:52 p.m. had a +173% 30-minute high and +155% sampled bid at 30 minutes, yet a later AMD call active alert used a different strike; MU 1080C at 11:54 a.m. had a +94% high and +66% bid at 30 minutes, but MU calls had already received an active idea earlier; AMZN 260C at 2:17 p.m. had a +93% high and +47% bid at 30 minutes, with another AMZN 260C active idea at 2:52 p.m.

Two more watches without direct promotion, SPY 776C at 11:17 a.m. and IWM 278P at 3:08 p.m., reached a +50% trade high within 30 minutes **after** sampled stock invalidation. They should not be counted as clean missed activations. The AMZN 257.5C watch at 9:57 a.m. is the strongest hindsight trap: its eventual high was +712%, but the sampled bid was −69% at 30 minutes, stock invalidation occurred at 10:27 a.m., and premium fell about 72% before the day peak. META 722.5P at 2:05 p.m. similarly printed +102% later only after a roughly 90% prepeak premium drop and a 2:10 p.m. invalidation.

The four flat/failed watches without direct promotion were META 715P (10:31 a.m.), TSLA 372.5P (11:04 a.m.), META 730C (11:13 a.m.), and QQQ 758C (2:07 p.m.). None later printed a gain above about 4% from its watch ask. These are useful negative examples; simply turning every watch into an active alert would have added them.

### All 24 watches without a direct promotion

| Watch time | Contract | Watch ask | Setup | 30m trade high | Bid at 30m | Day trade high | Label |
|---|---|---:|---|---:|---:|---:|---|
| 9:57 AM | AMZN 257.5C | $0.32 | Reversal | +22% | -69% | +712% | Late pop |
| 10:17 AM | INTC 115C | $0.62 | Coiled Continuation | +73% | -37% | +73% | Worked |
| 10:31 AM | META 715P | $1.01 | Coiled Continuation | -25% | -59% | -25% | Flat/failed |
| 10:44 AM | AAPL 332.5P | $0.24 | Pullback Resumption | +79% | -21% | +79% | Worked |
| 11:04 AM | TSLA 372.5P | $0.50 | Coiled Continuation | +4% | -22% | +4% | Flat/failed |
| 11:08 AM | INTC 115C | $0.27 | Pullback Resumption | +52% | -33% | +52% | Worked |
| 11:13 AM | META 730C | $1.29 | Reversal | -8% | -57% | -8% | Flat/failed |
| 11:17 AM | SPY 776C | $0.63 | Reversal | +65% | +63% | +211% | Worked |
| 11:54 AM | MU 1080C | $3.40 | Coiled Continuation | +94% | +66% | +219% | Worked |
| 12:03 PM | INTC 113P | $0.39 | Coiled Continuation | +26% | -10% | +105% | Late pop |
| 12:38 PM | INTC 112P | $0.16 | Coiled Continuation | +12% | -44% | +50% | Late pop |
| 12:52 PM | AMD 645C | $1.39 | Coiled Continuation | +173% | +155% | +177% | Strong |
| 1:04 PM | IWM 278C | $0.44 | Reversal | +68% | -9% | +68% | Worked |
| 1:09 PM | AMZN 260C | $0.30 | Coiled Continuation | +23% | -47% | +23% | Marginal |
| 1:14 PM | INTC 112P | $0.14 | Coiled Continuation | +7% | -43% | +71% | Late pop |
| 1:48 PM | GOOGL 350C | $0.23 | Continuation | +26% | -57% | +287% | Late pop |
| 1:48 PM | INTC 112P | $0.10 | Coiled Continuation | +140% | -20% | +140% | Strong |
| 2:05 PM | META 722.5P | $1.30 | Coiled Continuation | -3% | -67% | +102% | Late pop |
| 2:07 PM | QQQ 758C | $0.47 | Continuation | +2% | -36% | +2% | Flat/failed |
| 2:17 PM | AMZN 260C | $0.15 | Coiled Continuation | +93% | +47% | +107% | Worked |
| 2:38 PM | INTC 112P | $0.14 | Coiled Continuation | +57% | +7% | +57% | Late pop |
| 3:04 PM | AAPL 337.5C | $0.21 | Coiled Continuation | +29% | -90% | +29% | Marginal |
| 3:08 PM | IWM 278P | $0.26 | Pullback Resumption | +69% | +54% | +131% | Worked |
| 3:08 PM | AMD 640P | $0.76 | Reversal | +84% | -91% | +84% | Worked |

## All 28 active ideas

| Alert time | Contract | Alert ask | Setup | 30m trade high | Bid at 30m | Day trade high | Label |
|---|---|---:|---|---:|---:|---:|---|
| 10:07 AM | TSLA 370P | $0.48 | Level Reclaim | +0% | -62% | +0% | Flat/failed |
| 10:13 AM | GOOGL 342.5P | $0.54 | Pullback Resumption | +24% | -19% | +24% | Marginal |
| 10:22 AM | QQQ 756C | $0.88 | Reversal | +22% | -45% | +147% | Late pop |
| 10:22 AM | MU 1070C | $2.66 | Continuation | +135% | +37% | +631% | Strong |
| 10:46 AM | SPY 772P | $0.49 | Level Reclaim | +10% | -61% | +10% | Marginal |
| 11:09 AM | AMZN 257.5C | $0.27 | Level Reclaim | +274% | +256% | +863% | Strong |
| 11:11 AM | MU 1080C | $2.71 | Coiled Continuation | +55% | -15% | +300% | Worked |
| 11:15 AM | QQQ 757C | $0.63 | Level Reclaim | +62% | +51% | +106% | Worked |
| 11:37 AM | AMD 645C | $2.71 | Pullback Resumption | +22% | -22% | +42% | Marginal |
| 11:46 AM | AMZN 260C | $0.33 | Continuation | +15% | +9% | +48% | Marginal |
| 11:48 AM | AVGO 375C | $1.35 | Level Reclaim | +19% | -39% | +44% | Marginal |
| 12:17 PM | AMZN 260C | $0.39 | Coiled Continuation | +26% | -49% | +26% | Marginal |
| 12:49 PM | MU 1095C | $1.62 | Coiled Continuation | +98% | +36% | +98% | Worked |
| 1:04 PM | SPY 777C | $0.67 | Level Reclaim | +75% | +40% | +75% | Worked |
| 1:34 PM | META 727.5C | $1.06 | Reversal | +55% | -46% | +55% | Worked |
| 1:47 PM | AAPL 337.5C | $0.29 | Coiled Continuation | +55% | +14% | +55% | Worked |
| 2:06 PM | MU 1095C | $1.20 | Pullback Resumption | +4% | -52% | +4% | Flat/failed |
| 2:10 PM | AMD 647.5C | $0.96 | Pullback Resumption | +46% | -18% | +46% | Worked |
| 2:13 PM | TSLA 375P | $0.20 | Level Reclaim | +50% | -50% | +50% | Marginal |
| 2:38 PM | GOOGL 350C | $0.20 | Coiled Continuation | +90% | -10% | +345% | Worked |
| 2:52 PM | AMZN 260C | $0.30 | Coiled Continuation | +3% | -60% | +3% | Flat/failed |
| 2:52 PM | TSLA 377.5C | $0.55 | Reversal | +75% | +55% | +75% | Worked |
| 2:57 PM | META 727.5C | $1.32 | Reversal | -9% | -92% | -9% | Flat/failed |
| 3:04 PM | MU 1070P | $0.50 | Intraday Reversal | +10% | -92% | +10% | Marginal |
| 3:27 PM | GOOGL 350C | $0.38 | Level Reclaim | +134% | +76% | +134% | Strong |
| 3:34 PM | AVGO 375C | $0.55 | Level Reclaim | +253% | — | +253% | Strong |
| 3:35 PM | MU 1085C | $1.50 | Pullback Resumption | +93% | — | +93% | Worked |
| 3:35 PM | META 720P | $0.55 | Coiled Continuation | +80% | — | +80% | Worked |

The active failures were TSLA 370P at 10:07 a.m., MU 1095C at 2:06 p.m., AMZN 260C at 2:52 p.m. (promoted from watch), and META 727.5C at 2:57 p.m. QQQ 756C at 10:22 a.m. is labeled Late pop: +22% 30m high, then +147% by the close after roughly 51% premium drawdown. These are research outcomes, not realized trade returns.

## What to evaluate next

1. Review the ten watches without a direct promotion that had timely +50% highs against their stock triggers. Five distinct contracts had no same-direction active idea at any point that day; start there when evaluating missed activation logic. AMD 645C and MU 1080C also merit review, but each name had another same-direction active idea that day. Do not promote all watches based on peak hindsight.
2. Review activation delay and contract substitution. AMZN shows an active alert can arrive after the useful premium window; the MU strike switch changes the trade being measured. Preserve both timestamps and both contract asks.
3. Keep the 30-minute sampled bid and invalidation next to trade highs. Among the 24 watches without a direct promotion, 13 sampled bids were down at least 30% at 30 minutes despite 12 showing a +50% trade high sometime in that window. This is why peak-only hit rates are insufficient.
4. Revisit the label wording for cheap premiums during later research: the current “Worked” rule requires a $0.10 absolute move as well as a percentage move. For example, INTC 112P at 2:38 p.m. showed a +57% high within 30 minutes but is labeled Late pop because the dollar gain was under $0.10. No scanner logic was changed in this review.

## MU opening-drive audit (subsequent research)

The worker was running at the open: its logs show 104/104 fresh universe quotes on repeated cycles from 9:31 a.m. ET. MU had 390 quote observations, 380 deeper promotions, and 379 deeper chain observations for the session. Full-chain quote capture began around 9:40 a.m.; its first saved option-side candidate was at 9:44 a.m., once enough comparable chain history was available. The earlier universe sweep saw MU **put-side** flow at 9:35 and 9:39 while the stock fell toward its $1,011.42 session low; the first saved **call-side** sweep flow was around 9:42. Thus a bullish call alert at 9:35 would have been premature. No daily alert-count cap or before-10:00 throttle held MU back.

| ET | MU stock | Candidate | Why it did not alert |
|---|---:|---|---|
| 9:44 | $1,027.54 | 78/100 level reclaim, 15 active neighboring call strikes, 6,961 five-minute call contracts | Cumulative stock-volume pace was 1.01× versus the 2× gate; option activity was steady at 1.04× rather than accelerating enough. |
| 9:46 | $1,028.59 | 82/100 triggered opening drive, 15 call strikes, 7,221 five-minute call contracts | Stock pace was 1.02× and option acceleration 0.62×, so both relative gates rejected it. |
| 9:47 | $1,024.81 | 77/100 level reclaim, 12 call strikes | Stock-volume burst passed, but relative option replenishment still failed. |
| 10:06 | $1,036.79 | 90/100 continuation | Cumulative pace remained 0.96× and local stock-volume burst was below its separate threshold. |
| 10:14 and 10:17 | $1,042.34 and $1,043.32 | Both qualified independently | Each lacked a qualifying next completed bar, so the confirmation timer reset. |
| 10:21–10:23 | $1,049.11 to $1,053.80 | Consecutive qualifying continuation; $1,070 call active idea | First active alert, about $36 above the $1,017.37 session open. |

The failure mode was **relative-acceleration vetoes during an already broad, steady opening flow**, followed by intermittent confirmation. A narrow alternative now treats strong absolute early options volume across multiple strikes, rising local stock volume, and a triggered directional price setup as an additional way to satisfy the stock-volume and option-replenishment gates. It keeps the other quality checks and does not require a potential watch first. Applied counterfactually to all saved October 7 expiry-side evaluations before 10:15 a.m., it newly qualifies only three MU-call candidate minutes (9:44, 9:46, 9:47); 9:46 and 9:47 are consecutive and would have permitted an earlier active idea around 9:47. That is a single-session replay of saved features, not evidence of a repeatable edge or of executable option returns. Track alert timing, failed alternatives, and post-alert drawdown on later days before tightening the rule.
