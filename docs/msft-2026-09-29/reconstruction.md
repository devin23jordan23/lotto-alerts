# MSFT Sep 30 $510 calls: September 29, 2026

The lotto scanner did **not** alert on this contract. All times below are New York time and come from the scanner's saved Schwab option-chain snapshots and decision records. The quotes are sampled bid/ask, not executable fills or proof of trade direction.

| Time | MSFT spot | Sep 30 $510C bid/ask | Scanner state |
| --- | ---: | ---: | --- |
| 10:05:41 | $504.07 | $1.50 / $1.54 | Deep scan present; weak setup score 33.53 |
| 10:06–10:11 | — | — | Deep scan dropped despite fresh universe quotes; one sweep at 10:10 was not a full scoring snapshot |
| 10:12:28 | $504.48 | $1.61 / $1.65 | Deep scan resumed; option-history baseline reset by gap |
| 10:17:32 | $505.76 | $1.97 / $2.10 | Three-strike flow observed, but no continuous 10-minute baseline |
| 10:18:27 | $505.23 | $1.80 / $1.90 | Three-strike flow observed, still no baseline |
| 10:20:34 | $505.93 | $2.46 / $2.75 | Still no baseline; $510C also above $2.50 ask cap and $0.15 spread cap |
| 10:22:37 | $506.33 | $2.78 / $2.91 | First evaluable call group: score 48.71, chose $512.50C; stock volume, opposing-volume and option-acceleration checks failed |
| 10:28:33 | $509.55 | $3.50 / $3.70 | Best call score 67.86, chose $515C; below 72 score threshold and option acceleration below 1.5× |
| 10:40:28 | $513.20 | $6.10 / $6.25 | The $510C was already too expensive for the current entry filter |

At the 10:18 snapshot, the $510C was still inside the entry price and spread limits. Comparing that sampled $1.90 ask with the 10:40 sampled $6.10 bid gives a *hypothetical* 221% gross increase before fees and slippage. At 10:20, the sampled ask-to-later-bid change was about 122%, but the 10:20 quote failed both entry limits. Neither comparison establishes an actual available entry or an advance prediction.

The immediate software failure was a promotion churn bug. MSFT was promoted at 10:05 on option-flow priority, but the next sweep erased that temporary rank boost and a stronger newcomer displaced its deep-chain slot. The scoring engine correctly refuses to invent missing 5- and 10-minute option-volume history, so it had no candidate during the decisive 10:17–10:20 window. A change now gives flow-promoted names a 12-minute deep-scan lease even after the temporary flow priority expires. This prevents that exact gap in future sessions; it does not prove the existing score and confirmation rules would have emitted an alert before the move. The later 10:28 evaluation also shows that simply keeping history may still leave a reversal under the current 72-point threshold and option-acceleration rule.

The Sep 29 shared Schwab token broker outage was later, from about 11:38 to 13:25, and did not cause this morning MSFT miss. Operational outage/recovery notices now make sustained scan failures visible in the lotto channel.
