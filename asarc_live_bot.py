#!/usr/bin/env python3
"""ASARC-B absolute-best production bot — Asia hours live/shadow.

Policy (asarc_meta.json):
  sessions: asia only, hours UTC 0–6
  thr: 0.55
  SL=3.0×ATR  TP=2.0×ATR  (price)
  rich features + last-12m train
  entry: next M1 open after M5 close ± cost (floor 0.30 or measured spread)
  drift gate 0.30, spread reject 0.35, one trade, max hold 180m
  even M5 slots (::2 → minute%10==0)

  python3 asarc_live_bot.py --mode shadow
  python3 asarc_live_bot.py --mode live --lot 0.01 --confirm-live
"""
from __future__ import annotations

import argparse
import json
import pickle
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from features import ASARC_FEATURES, latest_asarc_row  # noqa: E402
from metaapi_client import MetaApiClient  # noqa: E402

MODELS = HERE / "models"
LOGS = HERE / "logs"
DECISIONS = HERE / "decisions"
OUTCOMES = HERE / "outcomes"
META_PATH = MODELS / "asarc_meta.json"
BUY_PATH = MODELS / "asarc_buy.pkl"
SELL_PATH = MODELS / "asarc_sell.pkl"

SL_ATR = 3.0
TP_ATR = 2.0
THR = 0.55
COST_FLOOR = 0.30
MAX_SPREAD_USD = 0.35
DRIFT_GATE_USD = 1.0
MAX_TRADE_MIN = 180
ASIA_HOURS = {0, 1, 2, 3, 4, 5, 6}
RUNNING = True


def _load_meta() -> dict:
    global SL_ATR, TP_ATR, THR, COST_FLOOR, MAX_SPREAD_USD, DRIFT_GATE_USD, ASIA_HOURS
    meta = json.loads(META_PATH.read_text())
    SL_ATR = float(meta.get("sl_atr", 3.0))
    TP_ATR = float(meta.get("tp_atr", 2.0))
    THR = float(meta.get("thr_asia", 0.55))
    COST_FLOOR = float(meta.get("cost", 0.30))
    MAX_SPREAD_USD = float(meta.get("max_spread_usd", 0.35))
    DRIFT_GATE_USD = float(meta.get("drift_gate_usd", 1.0))
    hrs = meta.get("asia_hours_utc", list(ASIA_HOURS))
    ASIA_HOURS = set(int(h) for h in hrs)
    return meta


def log(msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOGS / "latest.log", "a") as f:
        f.write(line + "\n")
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    with open(LOGS / f"asarc_{day}.log", "a") as f:
        f.write(line + "\n")


def load_models():
    if not BUY_PATH.exists() or not SELL_PATH.exists():
        raise FileNotFoundError(f"Missing models in {MODELS} — run train_asarc_model.py")
    meta = _load_meta()
    buy = pickle.loads(BUY_PATH.read_bytes())
    sell = pickle.loads(SELL_PATH.read_bytes())
    return buy, sell, meta


def even_m5_slot(bar_time) -> bool:
    t = pd.Timestamp(bar_time)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return int(t.minute) % 10 == 0


def atr_levels(direction: str, entry: float, atr: float) -> dict:
    sl_d, tp_d = SL_ATR * atr, TP_ATR * atr
    if direction == "BUY":
        return {"entry": round(entry, 2), "sl": round(entry - sl_d, 2), "tp": round(entry + tp_d, 2)}
    return {"entry": round(entry, 2), "sl": round(entry + sl_d, 2), "tp": round(entry - tp_d, 2)}


def score_bar(row: pd.Series, buy_clf, sell_clf) -> dict:
    session = str(row.get("session", ""))
    hour = int(row.get("hour", -1))
    x = [[float(row[c]) for c in ASARC_FEATURES]]
    p_buy = float(buy_clf.predict_proba(x)[0][1])
    p_sell = float(sell_clf.predict_proba(x)[0][1])
    base = {
        "p_buy": round(p_buy, 4),
        "p_sell": round(p_sell, 4),
        "session": session,
        "hour": hour,
        "atr": round(float(row.get("atr_14", 0) or 0), 4),
        "bar_time": str(row["time"]),
        "signal_close": round(float(row["close"]), 2),
    }
    if session != "asia" or hour not in ASIA_HOURS:
        return {**base, "action": "SKIP", "reason": "session_hour"}
    if not even_m5_slot(row["time"]):
        return {**base, "action": "SKIP", "reason": "odd_m5_slot"}
    if p_buy >= THR and p_buy >= p_sell:
        return {**base, "action": "SIGNAL", "direction": "BUY", "prob": round(p_buy, 4)}
    if p_sell >= THR and p_sell > p_buy:
        return {**base, "action": "SIGNAL", "direction": "SELL", "prob": round(p_sell, 4)}
    return {**base, "action": "SKIP", "reason": "below_threshold"}


def measured_cost(client: MetaApiClient) -> float:
    try:
        px = client.current_price()
        spread = abs(float(px["ask"]) - float(px["bid"]))
        return max(COST_FLOOR, spread)
    except Exception:
        return COST_FLOOR


def check_paper(trade: dict, m1: pd.DataFrame) -> dict | None:
    entry_t = pd.Timestamp(trade["entry_time"])
    if entry_t.tzinfo is None:
        entry_t = entry_t.tz_localize("UTC")
    bars = m1[m1["time"] >= entry_t]
    if bars.empty:
        return None
    direction = trade["direction"]
    entry, sl, tp = float(trade["entry"]), float(trade["sl"]), float(trade["tp"])
    opened = pd.Timestamp(trade["opened_at"])
    if opened.tzinfo is None:
        opened = opened.tz_localize("UTC")
    age = (datetime.now(timezone.utc) - opened).total_seconds() / 60
    for _, b in bars.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        if direction == "BUY":
            sh, th = lo <= sl, hi >= tp
        else:
            sh, th = hi >= sl, lo <= tp
        if sh and th:
            return {"result": "LOSS", "exit_reason": "sl_first", "exit_price": sl,
                    "pnl": -abs(entry - sl), "exit_time": str(b["time"])}
        if sh:
            return {"result": "LOSS", "exit_reason": "sl", "exit_price": sl,
                    "pnl": -abs(entry - sl), "exit_time": str(b["time"])}
        if th:
            return {"result": "WIN", "exit_reason": "tp", "exit_price": tp,
                    "pnl": abs(tp - entry), "exit_time": str(b["time"])}
    if age >= MAX_TRADE_MIN:
        close = float(bars.iloc[-1]["close"])
        pnl = (close - entry) if direction == "BUY" else (entry - close)
        return {"result": "TIMEOUT", "exit_reason": "timeout", "exit_price": close,
                "pnl": round(pnl, 2), "exit_time": str(bars.iloc[-1]["time"])}
    return None


def run(mode: str, lot: float, poll: int) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    DECISIONS.mkdir(parents=True, exist_ok=True)
    OUTCOMES.mkdir(parents=True, exist_ok=True)
    (LOGS / "latest.log").write_text("")

    buy_clf, sell_clf, meta = load_models()
    client = MetaApiClient()
    log("=" * 60)
    log(f"ASARC-B {'LIVE' if mode == 'live' else 'SHADOW'}  symbol={client.symbol}")
    log(f"SL={SL_ATR}×ATR TP={TP_ATR}×ATR thr={THR} asia_hours={sorted(ASIA_HOURS)}")
    log(f"cost_floor={COST_FLOOR} spread_max={MAX_SPREAD_USD} drift={DRIFT_GATE_USD} hold={MAX_TRADE_MIN}m")
    log(f"features={ASARC_FEATURES}")
    log(f"parity: next-M1 entry, even M5, one-trade, SL-first, train={meta.get('train')}")
    log("=" * 60)

    paper = None
    last_bar = None
    stats = {"signals": 0, "wins": 0, "losses": 0, "timeouts": 0, "pnl": 0.0}

    def _stop(*_):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while RUNNING:
        try:
            m1 = client.fetch_m1(bars=2500)
            if m1 is None or len(m1) < 400:
                log("wait: not enough M1")
                time.sleep(poll)
                continue
            row = latest_asarc_row(m1)
            if row is None:
                log("wait: no completed ASARC feature row")
                time.sleep(poll)
                continue
            bar_t = str(row["time"])
            if bar_t == last_bar:
                # manage open paper on same bar polls
                if paper:
                    done = check_paper(paper, m1)
                    if done:
                        stats["pnl"] += float(done["pnl"])
                        if done["result"] == "WIN":
                            stats["wins"] += 1
                        elif done["result"] == "LOSS":
                            stats["losses"] += 1
                        else:
                            stats["timeouts"] += 1
                        log(f"PAPER {done['result']} pnl={done['pnl']} total={stats['pnl']:.2f}")
                        (OUTCOMES / "summary.json").write_text(json.dumps(stats, indent=2))
                        paper = None
                time.sleep(poll)
                continue
            last_bar = bar_t

            if paper:
                done = check_paper(paper, m1)
                if done:
                    stats["pnl"] += float(done["pnl"])
                    key = {"WIN": "wins", "LOSS": "losses"}.get(done["result"], "timeouts")
                    stats[key] = stats.get(key, 0) + 1
                    log(f"PAPER {done['result']} pnl={done['pnl']} total={stats['pnl']:.2f}")
                    paper = None
                else:
                    log(f"skip new signal — paper trade open since {paper.get('opened_at')}")
                    time.sleep(poll)
                    continue

            # Broker one-trade: skip if any ASARC XAU position already open
            try:
                open_pos = [
                    p for p in client.positions()
                    if str(p.get("comment", "")).startswith("ASARC")
                ]
            except Exception:
                open_pos = []
            if open_pos:
                log(f"skip — broker ASARC position open id={open_pos[0].get('id')}")
                time.sleep(poll)
                continue

            decision = score_bar(row, buy_clf, sell_clf)
            (DECISIONS / "latest.json").write_text(json.dumps(decision, indent=2))
            if decision["action"] != "SIGNAL":
                log(f"SKIP {decision.get('reason')} sess={decision['session']} h={decision['hour']} "
                    f"pb={decision['p_buy']} ps={decision['p_sell']} bar={bar_t}")
                time.sleep(poll)
                continue

            atr = float(decision["atr"])
            if atr <= 0:
                log("SKIP bad atr")
                time.sleep(poll)
                continue

            # Production fill: next M1 open after bar close ≈ latest M1 at/after bar_end
            bar_end = pd.Timestamp(row["time"])
            if bar_end.tzinfo is None:
                bar_end = bar_end.tz_localize("UTC")
            bar_end = bar_end + pd.Timedelta(minutes=5)
            fill_bars = m1[m1["time"] >= bar_end]
            if fill_bars.empty:
                log("SKIP no M1 after bar close")
                time.sleep(poll)
                continue
            fill_open = float(fill_bars.iloc[0]["open"])
            cost = measured_cost(client)
            px = client.current_price()
            spread = abs(float(px["ask"]) - float(px["bid"]))
            if spread > MAX_SPREAD_USD:
                log(f"SKIP spread {spread:.3f} > {MAX_SPREAD_USD}")
                time.sleep(poll)
                continue
            drift = abs(fill_open - float(decision["signal_close"]))
            if drift > DRIFT_GATE_USD:
                log(f"SKIP drift {drift:.3f} > {DRIFT_GATE_USD}")
                time.sleep(poll)
                continue

            direction = decision["direction"]
            entry = fill_open + cost if direction == "BUY" else fill_open - cost
            lv = atr_levels(direction, entry, atr)
            stats["signals"] += 1
            log(
                f"SIGNAL {direction} prob={decision['prob']} atr={atr:.3f} "
                f"entry={lv['entry']} SL={lv['sl']} TP={lv['tp']} cost={cost:.3f} drift={drift:.3f}"
            )

            paper = {
                **lv,
                "direction": direction,
                "entry_time": str(fill_bars.iloc[0]["time"]),
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "atr": atr,
                "prob": decision["prob"],
            }

            if mode == "live":
                live_entry = float(px["ask"] if direction == "BUY" else px["bid"])
                if abs(live_entry - lv["entry"]) > DRIFT_GATE_USD:
                    log(f"LIVE SKIP drift live={live_entry} vs model={lv['entry']} gate={DRIFT_GATE_USD}")
                    paper = None  # don't block next signal after skipped live fill
                else:
                    live_lv = atr_levels(direction, live_entry, atr)
                    result = client.open_market(
                        direction=direction,
                        volume=lot,
                        sl=live_lv["sl"],
                        tp=live_lv["tp"],
                        comment="ASARC_BEST",
                    )
                    log(f"LIVE ORDER {result}")
                    if not result.get("ok"):
                        log(f"LIVE ORDER FAILED — clearing paper lock")
                        paper = None

        except Exception as e:
            log(f"ERROR {e}")
            log(traceback.format_exc())
        time.sleep(poll)

    log("Stopped.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    ap.add_argument("--lot", type=float, default=0.01)
    ap.add_argument("--poll", type=int, default=20)
    ap.add_argument("--confirm-live", action="store_true")
    ap.add_argument("--daemon", action="store_true", help="double-fork detach from parent")
    args = ap.parse_args()
    if args.mode == "live" and not args.confirm_live:
        print("Refuse live without --confirm-live", file=sys.stderr)
        sys.exit(2)
    if args.daemon:
        # Double-fork so Cursor/shell process-group kill cannot take us down
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        if os.fork() > 0:
            sys.exit(0)
        sys.stdin.close()
    run(args.mode, args.lot, args.poll)


if __name__ == "__main__":
    import os
    main()
