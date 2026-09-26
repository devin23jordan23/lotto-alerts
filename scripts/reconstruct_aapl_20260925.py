"""Offline case study; does not alter live settings or send alerts.

Inputs are authenticated Schwab history and checksum-verified saved scanner
observations. The alternative rules are an in-sample hypothesis, not validation.
"""
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lotto.config import Settings
from lotto.features import candidates, quote_ok
from lotto.models import ET, Snapshot
from lotto.schwab import epoch

ROOT = Path("data/research/2026-09-25")
OUT = Path("docs/aapl-2026-09-25")
OUT.mkdir(parents=True,exist_ok=True)
case = json.loads((ROOT/"AAPL-scanner-export.json").read_text())
records = case["records"]
snapshots = [Snapshot.from_dict(r["payload"]) for r in records["snapshots"]]
settings = Settings()
overrides = json.loads((ROOT/"deployed-settings.json").read_text())
settings = replace(settings, **{k.removeprefix("LOTTO_").lower():type(getattr(settings,k.removeprefix("LOTTO_").lower()))(v)
                              for k,v in overrides.items()})
daily = json.loads((ROOT/"AAPL-daily.json").read_text())["candles"]
daily = [r for r in daily if epoch(r["datetime"]).astimezone(ET).date().isoformat()<case["day"]]
tr = [max(b["high"]-b["low"],abs(b["high"]-a["close"]),abs(b["low"]-a["close"])) for a,b in zip(daily,daily[1:])]
atr = sum(tr[:21])/21
for value in tr[21:]:
    atr = (20*atr+value)/21
extended = json.loads((ROOT/"AAPL-extended.json").read_text())["candles"]
premarket = [r for r in extended if epoch(r["datetime"]).astimezone(ET).date().isoformat()==case["day"]
             and "04:00"<=epoch(r["datetime"]).astimezone(ET).strftime("%H:%M")<"09:30"]
pmh = max(r["high"] for r in premarket)
stock = json.loads((ROOT/"AAPL.json").read_text())["candles"]
stock = [r for r in stock if epoch(r["datetime"]).astimezone(ET).date().isoformat()==case["day"]]
option_id = "AAPL  260925C00340000"
history, observations, hypotheses, replay_matches = [], [], [], 0
decisions = {r["at"]:r for r in records["decisions"]}
for snap in snapshots:
    option = next((o for o in snap.options if o.symbol==option_id),None)
    if not option:
        continue
    current = {"at":snap.at.isoformat(),"time_et":snap.at.astimezone(ET).strftime("%H:%M:%S"),
               "spot":snap.spot,"bid":option.bid,"ask":option.ask,"volume":option.volume,
               "delta":option.delta,"gamma":option.gamma,"quote_at":option.quote_time.isoformat(),
               "stock_quote_at":snap.spot_time.isoformat(),
               "spread_fraction":(option.ask-option.bid)/option.ask}
    if not snap.problems(min_bars=1):
        evaluated = candidates(snap,history,settings) if not snap.problems() else []
        best = max(evaluated,key=lambda c:c.score,default=None)
        saved = decisions.get(snap.at.isoformat())
        if saved and saved["score"] is not None:
            assert best and abs(saved["score"]-best.score)<.011, (snap.at,saved["score"],best.score if best else None)
            replay_matches += 1
        calls = [c for c in evaluated if c.option.side=="CALL" and c.option.expiry.isoformat()==case["day"]]
        call = max(calls,key=lambda c:c.score,default=None)
        if call:
            m = call.metrics
            current.update({"call_score":call.score,"selected_contract":call.option.symbol,
                            "blockers":list(call.blockers),"features":m,"parts":call.parts,
                            "setup":call.setup.name if call.setup else None})
            # Hypothesis scoped to liquid, low-premium OTM calls reclaiming a known
            # level. All inputs here are available by this snapshot timestamp.
            # It does NOT use the subsequent high, close, or contract return.
            gates = {
                "stock_above_open_prior_close_vwap":snap.spot>max(snap.bars[0].open,snap.prior_close,m["vwap"]),
                "vwap_acceptance":m["above_vwap_share"]>=.8,
                "efficient_recovery":m["efficiency"]>=.55 and m["return_5m_directional"]>0,
                "volume_improving_from_own_base":m["volume_acceleration"]>=1.2,
                "options_breadth_and_acceleration":m["cluster_size"]>=3 and m["option_acceleration"]>=1.5,
                "relative_strength":m["relative_sector_5m"]>0,
                "near_known_premarket_level":abs(snap.spot-pmh)<=.1*atr,
                "strike_within_atr_distance":0<340-snap.spot<=.6*atr,
                "option_quote_liquid":quote_ok(option,snap,settings) and option.bid>0
                    and .05<=option.ask<=settings.max_ask
                    and current["spread_fraction"]<=settings.max_spread_fraction
                    and option.ask-option.bid<=settings.max_spread_dollars,
                "early_lotto_delta":option.delta is not None and .05<=option.delta<=.55,
            }
            current["hypothesis_gates"] = gates
            if all(gates.values()):
                state = "TRIGGER" if snap.spot>pmh else "WATCH"
                hypotheses.append({"at":snap.at.isoformat(),"time_et":current["time_et"],"state":state,
                                   "spot":snap.spot,"bid":option.bid,"ask":option.ask})
        endpoints=[]
        for minutes in (5,10):
            target=snap.at-timedelta(minutes=minutes)
            matches=[s for s in history if s.at<=target and (target-s.at).total_seconds()<=90]
            endpoints.append(matches[-1] if matches else None)
        if all(endpoints):
            five,ten=endpoints
            old=next((o for o in five.options if o.symbol==option_id),None)
            older=next((o for o in ten.options if o.symbol==option_id),None)
            if old and older:
                new_volume=option.volume-old.volume;prior_volume=old.volume-older.volume
                current["contract_volume_5m"]=new_volume
                current["contract_volume_prior_5m"]=prior_volume
                current["contract_acceleration"]=(new_volume/(snap.at-five.at).total_seconds())/(prior_volume/(five.at-ten.at).total_seconds()) if prior_volume>0 else None
        history.append(snap)
        history=[s for s in history if snap.at-s.at<=timedelta(minutes=25)]
    observations.append(current)

def first_touch(level,after="10:00"):
    row=next((r for r in stock if r["high"]>level and epoch(r["datetime"]).astimezone(ET).strftime("%H:%M")>=after),None)
    return epoch(row["datetime"]).astimezone(ET).isoformat() if row else None

summary={"case":case["day"]+" AAPL 340C", "record_counts":case["record_counts"],
         "replayed_score_matches":replay_matches,"atr21_wilder":atr,"atr21_sma":sum(tr[-21:])/21,
         "premarket_high":pmh,"prior_day_high":daily[-1]["high"],"prior_close":daily[-1]["close"],
         "regular_open":stock[0]["open"],"pmh_rebreak_bar_start":first_touch(pmh),
         "prior_high_break_bar_start":first_touch(daily[-1]["high"]),"strike_touch_bar_start":first_touch(340),
         "deployed_settings":asdict(settings),"hypothesis_is_in_sample":True,
         "hypothesis_first_watch":next((x for x in hypotheses if x["state"]=="WATCH"),None),
         "hypothesis_first_trigger":next((x for x in hypotheses if x["state"]=="TRIGGER"),None),
         "hypotheses":hypotheses,"observations":observations}
(OUT/"analysis.json").write_text(json.dumps(summary,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k not in {"observations","hypotheses","deployed_settings"}},indent=2))

# Standard plotting library; preserve empty portions rather than fabricating
# missing options quotes. Scatter plots do not imply a continuous quote tape.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
times=[datetime.fromisoformat(r["at"]).astimezone(ET) for r in observations]
entry=datetime(2026,9,25,10,32,tzinfo=ET)
start=datetime(2026,9,25,10,10,tzinfo=ET);end=datetime(2026,9,25,12,10,tzinfo=ET)
fig,axes=plt.subplots(4,1,figsize=(14,12),sharex=True,gridspec_kw={"height_ratios":[2.2,1.4,1,1.1]})
stock_times=[epoch(r["datetime"]).astimezone(ET)+timedelta(minutes=1) for r in stock]
cv=0;pv=0;vw=[]
for r in stock:
    cv+=r["volume"];pv+=(r["high"]+r["low"]+r["close"])/3*r["volume"];vw.append(pv/cv)
axes[0].plot(stock_times,[r["close"] for r in stock],color="#2466b5",label="AAPL completed 1-min close",lw=1.4)
axes[0].plot(stock_times,vw,color="#b47a15",label="Reconstructed regular-session VWAP",lw=1.1)
for level,label,color in [(pmh,f"Premarket high {pmh:.2f}","#8b5fbf"),(336.8,"Opening-range high 336.80","#708090"),(daily[-1]["high"],"Previous-day high 338.91","#b86046"),(340,"340 strike","#28854f")]:
    axes[0].axhline(level,lw=.9,ls="--",color=color,label=label)
axes[0].set_ylim(334.7,340.9);axes[0].set_ylabel("AAPL ($)")
axes[0].legend(loc="upper left",ncol=2,fontsize=8)
axes[1].scatter(times,[r["bid"] for r in observations],s=11,color="#217448",label="Saved bid")
axes[1].scatter(times,[r["ask"] for r in observations],s=10,color="#ad6e29",label="Saved ask")
axes[1].scatter([entry],[.15],marker="*",s=130,color="#111111",label="User-reported entry $0.15",zorder=5)
axes[1].set_ylabel("340C quote ($)");axes[1].legend(loc="upper left",fontsize=8)
flow=[r for r in observations if r.get("contract_volume_5m") is not None]
axes[2].scatter([datetime.fromisoformat(r["at"]).astimezone(ET) for r in flow],[r["contract_volume_5m"] for r in flow],color="#397e95",s=12)
axes[2].set_ylabel("340C volume increase\n~5-min window")
score=[r for r in observations if r.get("call_score") is not None]
axes[3].scatter([datetime.fromisoformat(r["at"]).astimezone(ET) for r in score],[r["call_score"] for r in score],s=12,label="Replayed best same-day call score",color="#66549b")
axes[3].axhline(settings.min_score,color="#b44444",ls="--",label=f"Live score threshold {settings.min_score}")
axes[3].set_ylabel("Research score");axes[3].set_ylim(0,90);axes[3].legend(loc="upper left",fontsize=8)
for ax in axes:
    ax.axvline(entry,color="#111111",lw=1,ls=":")
    ax.axvspan(entry,end,color="#cccccc",alpha=.12)
    ax.grid(alpha=.16);ax.set_xlim(start,end)
axes[3].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M",tz=ET))
axes[3].set_xlabel("September 25, 2026 • Eastern time • Vertical line: reported 10:32 entry; shaded region: later outcomes")
fig.suptitle("AAPL 340 calls: the setup was observed, but the rules rejected it",fontsize=16,fontweight="bold")
fig.text(.5,.018,"Options are sampled quotes, not executions. Volume is unsigned. Later prices are outcome labels, never inputs to the pre-entry hypothesis.",ha="center",fontsize=9)
fig.tight_layout(rect=[0,.035,1,.96])
fig.savefig(OUT/"reconstruction.png",dpi=170)
fig.savefig(OUT/"reconstruction.pdf")
