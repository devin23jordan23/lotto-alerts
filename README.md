# Lotto Alerts

Selective intraday **potential** options runner alerts using Schwab market data,
a Railway worker, and a dedicated Discord webhook. The goal is a few strong
opportunities that can be followed for outsized options moves, including 500%+.
The scanner does not predict or promise a 500% return, and places no orders.

## Current version

- Discovery → confirmed ignition → runner tracking → cooling/rearm.
- Symmetric bullish calls and bearish puts, evaluated separately by expiration.
- Same-time stock RVOL using the median of up to 20 prior sessions, requiring
  at least 10 usable sessions. Today's observations never enter the baseline.
- Completed one-minute bars, rising/falling VWAP, directional efficiency,
  proximity to the session extreme, controlled opposing volume, and momentum.
- Option volume changes over rolling five-minute windows, acceleration,
  adjacent active strikes, and spot-adjusted strike migration.
- Sector confirmation: SMH for semiconductors, IBIT as a **Bitcoin ETF proxy**
  for crypto equities, XBI for MRNA, and QQQ as the default benchmark.
- Separate prior-close return, opening gap, and return from the regular-session
  open. A large existing percentage gain does not automatically disqualify a name.
- Contract selection by quoted price, spread, delta, gamma, moneyness and DTE.
- SQLite persistence for observations, decisions, queued alerts and outcomes.
- Nightly end-of-day option report for the configured universe: first valid
  regular-session ask versus the latest bid captured by 3:59 PM, plus sampled
  favorable/adverse excursions.

This first implementation uses configurable research rules. It has not been
validated against historical OPRA data or connected to live credentials in its
initial development tests. The configured universe is intentional; nightly
analysis evaluates those names only and does not claim to scan every U.S. stock.

## Alert restraint

Defaults are **at most five new ideas per trading day**, one per scan cycle,
two per ticker, with a 45-minute ticker cooldown. A setup must persist for three
minutes across distinct completed bars. Re-alerting also needs a measured reset,
a fresh qualifying setup and a different contract. Zero alerts is valid.

Candidates are ranked across the scan before the daily budget is spent. No new
entries are issued in the last 30 minutes of the regular session. Schwab market
hours determine holidays and early closes. The live worker must obtain that
calendar successfully before scanning.

Example format (illustrative, not a real signal):

```text
🚨 POTENTIAL LOTTO
XYZ 105C · YYYY-MM-DD · 1DTE
Ask $0.80 · Bid $0.75
Runner score: 78/100
Stock pace 3.1× · Options velocity 2.8× · 4 neighboring strikes · SMH confirms
```

## Score and data limits

The original 100-point research framework is preserved: price 20, stock volume
20, options flow 20, breadth 12, migration 13, context 10, liquidity 5.
Schwab chain snapshots do **not** identify trade aggressors or new opening
positions. This version awards at most 12/20 flow points from option velocity;
the other 8 points remain unavailable. Sector confirmation supplies 5/10 context
points; the remaining 5 catalyst points stay unavailable without a news feed.
The obtainable score is therefore **87/100**, with a default threshold of 72;
missing evidence is never normalized into full confidence. Scores are not odds.

Option volume is cumulative snapshot data. Its differences are activity proxies,
not signed premium, sweep detection, trade counts or verified buyer initiation.
Contract-level historical same-time option RVOL is not available in this version.
Migration is volume-weighted and spot-adjusted, not aggressor-weighted.
0DTE, 1DTE and later eligible expirations are evaluated separately.

Stale/future quotes, missing stock baselines, incomplete session bars, delayed
entitlements, option volume-counter resets and large observation gaps block
affected setups. An initial regular-session startup needs 16 completed bars,
then roughly 10 minutes of options observations and 3 minutes of confirmation.
A restart can reload recorded observations but must reconfirm persistence.

## Outcome tracking

Every emitted potential idea stores its original **entry ask**. Subsequent fresh
bid quotes update observed favorable/adverse returns and first observed +50%,
+100%, +200%, +300% and +500% milestones. A +500% gain means the later bid is
six times the entry ask. Tracking is intraday; it closes at session end.
Missing quotes are gaps, not fills or losses. These are sampled quote returns,
not actual trade executions, and polling can miss intervening highs/lows.
Last available quote timestamps remain visible in the report.

Milestones are saved to the database; this version posts only new potential
ideas to Discord, keeping the channel concise. Tracking continues after the
entry cutoff and after the new-alert budget is exhausted.

## Nightly end-of-day review

After the session, run:

```bash
python3 -m lotto.main nightly --db /app/data/live.sqlite3 --day YYYY-MM-DD
```

If `--day` is omitted, the previous calendar day is used. The command reads the
captured snapshots for the configured `LOTTO_UNIVERSE` and writes
`/app/data/nightly/options-YYYY-MM-DD.json`. For each contract observed from
9:30 AM through 3:59 PM it records the first valid ask, latest valid bid,
open-to-close return, and sampled maximum/minimum bid return. A contract must
have valid captured quotes; the job does not infer missing prices.

This is a sampled quote report. It can miss a brief intraday high between polls,
and it cannot reconstruct contracts that were never captured. It is intended to
describe what happened in the configured universe and improve the scanner's
research dataset, not to claim a complete historical options tape.

## Railway setup

1. Create a Railway service from `devin23jordan23/lotto-alerts`, branch `main`.
2. The included Dockerfile and `railway.json` run `python -m lotto.main live`.
   This is a long-running worker; no public domain or HTTP healthcheck is needed.
3. Attach a persistent volume at `/app/data` and set `DATA_DIR=/app/data`.
   Use **one replica** so daily budgets, duplicate suppression and tokens share
   one state database. The worker also takes an exclusive file lock.
4. Set `SCHWAB_CLIENT_ID`, `SCHWAB_CLIENT_SECRET`, `SCHWAB_REFRESH_TOKEN`, and
   `DISCORD_LOTTO_WEBHOOK`. Use the existing authorized Schwab connection and a
   dedicated lotto webhook. Never put real credentials in GitHub.
5. Start with `LOTTO_SEND_ALERTS=false` for shadow observation, then set it to
   `true` to enable Discord. Shadow ideas are tracked and consume that day's
   budget; they are never retroactively sent when the flag changes.

All tuning variables are listed in [.env.example](.env.example). Railway injects
them; the Python worker does not automatically load a local `.env` file.
Tokens are refreshed and saved with restricted file permissions on the volume.
Expired Schwab refresh authorization must be renewed through your existing
authorization workflow; an invalid persisted token file must also be replaced.
Saved raw observations accumulate for replay; provision storage and archive
older observations as needed.

Discord alerts are persisted before delivery. An uncertain response is **not
automatically resent**, avoiding duplicate posts after timeouts or crashes.
Rate-limited pending alerts can retry next cycle, but expire after two minutes.
Uncertain/failed/expired delivery remains visible in the report and still counts
toward the budget. Check the channel before any manual resend.

Railway configuration follows the [official config-as-code reference](https://docs.railway.com/config-as-code/reference).
Schwab endpoint behavior was cross-checked against the
[schwab-py maintainer documentation](https://schwab-py.readthedocs.io/en/latest/client.html)
and the existing scanner's read-only integration. Live entitlement and response
compatibility still require a shadow run with the account.

## Local verification and replay

Python 3.11+ on macOS/Linux; only the standard library is required.

```bash
python3 -m unittest discover -s tests -v
python3 -m lotto.main demo --db /tmp/lotto-demo.sqlite3
python3 -m lotto.main report --db /tmp/lotto-demo.sqlite3
python3 -m lotto.main replay --input observations.jsonl --db /tmp/lotto-replay.sqlite3
```

The demo is explicitly synthetic and never sends to Discord. Replays also never
send. Use a new database for each independent experiment; reusing one intentionally
preserves its duplicate suppression. Replay JSONL accepts one array of snapshots
per scan cycle, using `Snapshot.to_dict()` from `lotto/models.py`. Preserve the
arrays so candidates are ranked together. Timestamps must include UTC offsets.
Tests cover persistence, failure cases, call/put symmetry, liquidity gates,
session boundaries, budgets, restart suppression and ask-to-bid milestone math.

Future calibration must compare runners against similar failed setups with
timestamp-frozen features and actual executable quotes. The synthetic demo and
unit tests establish software behavior, not an edge or a real-world hit rate.
