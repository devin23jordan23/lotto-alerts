# Fashionably Late: source audit for the Model T exception

**Status:** research only. This is a separate, structured exception to Andre's directional A/B policy; no Fashionably Late logic is live in the lotto scanner. The legacy `REVERSAL` and `INTRADAY_REVERSAL` labels do not establish this setup.

## Sources checked

- Andre's two-page illustrated cheat sheet: `/Users/owner/Downloads/Trading_With_Dre_Fashionably_Late_Scalp_Cheat_Sheet.pdf` (both pages visually inspected).
- Andre's reference manual: `/Users/owner/Downloads/Andre_Trading_Playbook_Reference_Manual.docx`, table 18.
- The archived Grok/Claude conversation: `/Users/owner/.codex/attachments/72617110-5b2e-44b5-88f5-7d8f6b660607/pasted-text.txt`, particularly lines 858–975 and 988–1007. This includes Grok's written phase model and commentary on the chart images, but the chart image pixels were not available in that text export.
- Grok's actual implementations: `/Users/owner/Downloads/main_v3_15_final.py`, lines 2182–2605, and `/Users/owner/Downloads/main_v3_15c.py`, lines 2286–2709. Neither is part of the lotto repository or a validated Model T backtest.
- Also searched the other two locally named HedgeFund playbook PDFs; neither has extractable mentions of Fashionably Late, Hitchhiker, or Rubber Band.

## What the setup is

The illustrated **long** begins with a sustained move **down** away from VWAP. The 9 EMA diverges **below** VWAP; price makes a distinct low. Buyers turn price upward, convergence toward VWAP is relatively quick, volume grows during the convergence compared with the divergence, the 9 EMA slopes up, and VWAP is flat/down. The cheat sheet calls for entry **as the 9 EMA crosses above VWAP**, followed by immediate momentum and a hold above the EMA. The short mirrors this from a distinct **high** and a downward EMA/VWAP cross. This is a reversal of the *earlier leg*, followed by a directional move after the turn. It is not a generic reversal and not a late-afternoon setup.

The cheat sheet rejects a flat/choppy turn, repeated reversals, an extended pause before the cross, and a 9 EMA that stays flat for more than 15 minutes after the turn. It lists ideal windows of 10:00–10:45 and 10:46–13:30 ET. These are preferred windows in the source, not proof that other times should be vetoed. Its printed **60% win rate and 3:1 reward/risk** have no sample size, entry/exit series, or audit method and must not be used as this scanner's measured performance.

The manual says the EMA **prepares** to cross; Grok's archived chart discussion explicitly favors an **approaching cross** entry. That conflicts with the cheat sheet's explicit “enter as soon as the cross occurs.” Preserve both interpretations as **separate variants** in replay: a pre-cross watch/early-entry hypothesis and a confirmed-cross entry. Do not silently promote an approaching EMA into a confirmed-cross ACTIVE alert. A post-cross first retest is a third, later entry to grade separately.

The cheat sheet diagram puts a long stop approximately one-third of the **LOD-to-cross distance below the cross**, and targets one full measured move above the cross. The archived Grok prose contains a different long formula (`LOD + distance/3`), so the risk anchor is inconsistent across sources; use the illustrated rule in the baseline replay and report the formula explicitly. These are stock-level framework prices, not a measured option stop or promised option return. The short diagram is described by inversion; verify its exact stop against a short lesson before live use.

## Why Grok's function cannot simply be copied

| Source requirement | Grok v3.15 code | Consequence |
|---|---|---|
| Long: down leg to a distinct **low**, then rising EMA over VWAP | `_measure_ema_vwap_divergence(..., direction="long")` chooses the **maximum high** in the first two-thirds of a 20-bar window; docstring calls it an upward impulse | The phase detector can label the opposite pattern Fashionably Late Long. Short makes the symmetric mistake, choosing a low rather than the required high. |
| Meaningful EMA/VWAP *divergence*, then quick convergence | Helper takes separation only at its chosen extreme; it does not verify that absolute separation actually grew through the first phase | A choppy path can pass the shape label with a sufficiently large single separation. |
| Valid early 10:00–10:45 window | `len(r) < 12` rejects the function, and `r` contains five-minute regular-hours bars | With a complete 9:30 start, the first possible evaluation is about 10:25–10:30, after much of the preferred morning window. Missing opening bars delay it further. |
| Distinct approaching and confirmed triggers | One function can return `Actionable` for `ema_approaching` or `ema_just_crossed`, with 0.3% approach and 0.8% separation constants | The alert label can imply a trade trigger before the cheat sheet's confirmed cross. Neither numerical threshold has been calibrated on lotto outcomes. |
| Quick turn and no pause | The code caps convergence at three five-minute bars but tests pause using average range below 25% of ATR | This does not directly measure repeated EMA/VWAP crosses, overlapping bars, or time *near* the cross. The 25% cutoff is unvalidated here. |
| Market context | Code only vetoes a categorical opposing SPY direction | Need replay to determine whether sector/QQQ/IWM context adds discrimination; do not use a single opposing SPY tick as an automatic veto. |

The high/low inversion is present in **both** local v3.15 files. Passing syntax or unit checks cannot validate the setup's meaning. Grok's later `HITCHHIKER_SCALP_LONG` is a separate opening consolidation breakout function, whereas the reference manual groups “Fashionably Late / Hitchhiker” in one table row. Preserve those as distinct candidate definitions until their original lessons are reconciled.

## Testable Model T exception

1. Build an as-of-stock-bar detector with explicit states: down/up impulse, expanding EMA/VWAP separation, distinct LOD/HOD, fast convergence with rising relative volume, cross approaching, confirmed cross, immediate retest, failed/stalled. Require a coherent phase sequence, not just an endpoint threshold. Track 9 EMA/VWAP signs, slopes, duration, repeated crossings, volume by phase, market/sector context, and stock-level stop/target. Invert every phase for shorts.
2. Replay **pre-cross**, **cross**, and **retest** timestamps separately. A watch generated before cross must remain identifiable even if no cross follows. Compare missed valid crosses and bad watches, not just paths selected after a winning cross.
3. Fit separation and duration cutoffs on historical bars and rotate held-out sessions; do not inherit 0.8%, 0.3%, three bars, 25% ATR, or a 60% claimed win rate as truth. Grade each variant from a fresh event-time option ask to a sellable bid at 5/10/15/30 minutes, stop-first and +40/+100, preserving big movers lost. Include no-alert stock controls and the directional A/B lane to measure incremental value.
4. The current local stock-path export does **not** have complete 9:30–9:34 bars on any tracked name-day, so it cannot reconstruct session VWAP and this phase sequence without leakage. Obtain complete production five-minute bars/snapshots for all available sessions or a contemporaneous historical bar feed before claiming a Fashionably Late backtest. No Fashionably Late win-rate estimate is established by the existing 340-alert sent cohort.

**Model decision:** directional A/B remains the principal Model T path. Fashionably Late gets a dedicated research lane and its own label; no ordinary reversal is grandfathered in. A live exception requires the corrected phase detector and held-out entry/outcome evidence.
