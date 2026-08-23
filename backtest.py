"""
backtest.py — walk-forward backtest of the full signal pipeline
(regime -> strategy -> trade_gate), so "is this too strict?" gets
answered with counted numbers instead of guesses.

============================================================================
SCOPE -- READ THIS BEFORE TRUSTING ANY NUMBER THIS PRODUCES
============================================================================

1. DAILY BARS ONLY, not the live 5-minute dashboard configuration.
   Upstox's historical API only gives ~10-60 days of intraday history
   (see dashboard.py's own TF_MAP), which isn't enough for a
   statistically meaningful multi-year backtest. Daily bars have a
   long, reliable history instead. Consequence: the "4H timeframe" is
   NOT independently backtested here -- this script feeds the SAME
   daily df to strategy.get_higher_timeframe_trend()'s daily_df and
   h4_df parameters, which makes h4_trend_bullish/bearish identical to
   the daily ones. Any condition that specifically needs 4H-vs-daily
   DISAGREEMENT (e.g. htf_disagreement in regime.py) will never fire
   as intended in this backtest. This validates the DAILY-level gating
   logic and signal frequency; it is not a faithful simulation of the
   live multi-timeframe dashboard.

2. NO REAL OPTION PREMIUM DATA. There is no historical option-chain
   API being used here (Upstox's historical endpoints don't give past
   option premiums for arbitrary expired strikes/dates, and nothing in
   this project fetches that). P&L is a PROXY using the underlying
   index-point stop/target from strategy.compute_trade_levels() and
   risk.py's MAX_HOLDING_DAYS, expressed in R-multiples (multiples of
   the initial risk distance). This tells you whether the SIGNAL
   ENGINE has directional edge on the underlying. It does NOT tell you
   what actual option P&L would have been -- that's a nonlinear
   function of delta/gamma/theta/vega that this script doesn't model.
   Do not report "R multiples" to anyone as if they were rupee returns
   on an actual options position.

3. I (the assistant) HAVE NOT RUN THIS AGAINST REAL DATA. This sandbox
   has no Upstox credentials. Every number in this docstring's example
   output, if any, would be from synthetic test data, clearly labeled
   as such. Only a run against your real Upstox history (or a CSV you
   supply) produces numbers worth acting on.

4. No lookahead: at simulated bar i, every gate/score/signal is
   computed using ONLY data[0:i+1]. If a trade triggers, entry is
   priced at data.open[i+1] (the next bar's open -- you can't act on a
   signal before it's known), and exit is determined by scanning
   FORWARD from i+1 for a stop/target/time-based exit. Scanning forward
   after entry is normal backtesting, not lookahead -- lookahead would
   be using bar i+1's data to help decide whether to enter at bar i,
   which this script never does.

============================================================================
USAGE
============================================================================
    python backtest.py --days 750                  # pulls live from Upstox
    python backtest.py --csv historical_nifty.csv   # or from a local CSV
        (CSV must have columns: date,open,high,low,close,volume)

Outputs:
    - a funnel table (how many bars survive each gate, in order)
    - a trade log (every simulated trade, one row per trade)
    - summary stats (win rate, avg R, total R, max drawdown in R, count)
    - writes trade_log.csv and backtest_summary.json to --output-dir
"""
from __future__ import annotations
import argparse
import json
import math
from datetime import datetime, timedelta
import pytz
import pandas as pd

import strategy
import regime
from volume_profile import calculate_volume_profile
from trade_gate import TradeGate
import risk

IST = pytz.timezone("Asia/Kolkata")

WARMUP_BARS = 80          # min bars before daily EMA50/ADX/ATR/volume-profile are meaningful
PERSISTENCE_BARS = 2      # same signal required on this many consecutive DAYS
COOLDOWN_DAYS_AFTER_STOP = 2
MAX_HOLDING_DAYS = risk.MAX_HOLDING_DAYS   # reuse the project's existing constant (3)
VOLUME_PROFILE_BINS = 30


# ---------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------

def load_from_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {missing}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def load_from_upstox(days: int) -> pd.DataFrame:
    from upstox import get_historical_nifty
    raw = get_historical_nifty(interval="day", days=days)
    candles = list(reversed((raw or {}).get("data", {}).get("candles") or []))
    if not candles:
        raise RuntimeError("Upstox returned no daily candles -- check credentials/network.")
    df = pd.DataFrame({
        "date":   [pd.to_datetime(c[0]) for c in candles],
        "open":   [c[1] for c in candles],
        "high":   [c[2] for c in candles],
        "low":    [c[3] for c in candles],
        "close":  [c[4] for c in candles],
        "volume": [c[5] for c in candles],
    })
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------
# Per-bar gate evaluation (mirrors pipeline_adapter.py's logic, but
# walk-forward over history instead of one live snapshot)
# ---------------------------------------------------------------------

def evaluate_bar(daily_df: pd.DataFrame) -> dict:
    """Runs the full gate stack on data[0:i+1] and returns every
    intermediate result needed for both the funnel stats and (if a
    trade fires) the trade simulation."""
    trend = strategy.get_higher_timeframe_trend(daily_df, daily_df)  # h4 == daily, see docstring
    strength = strategy.get_market_strength(daily_df)
    liquidity = strategy.compute_liquidity_state(daily_df)
    vp = calculate_volume_profile(daily_df, bins=VOLUME_PROFILE_BINS)
    last_close = float(daily_df["close"].iloc[-1])

    setup = strategy.evaluate_setup(trend, strength, liquidity, vp, last_close)
    scores = strategy.compute_conviction(setup, strength)
    raw_signal = strategy.generate_signal(scores)

    regime_result = regime.classify_regime(daily_df, daily_df)
    suitable = regime.is_market_suitable(regime_result)
    final_signal = raw_signal if suitable else "WATCH"

    long_score, short_score = scores["long_score"], scores["short_score"]
    margin = abs(long_score - short_score)

    return {
        "trend": trend, "strength": strength, "liquidity": liquidity, "vp": vp,
        "last_close": last_close, "setup": setup, "scores": scores,
        "raw_signal": raw_signal, "regime_result": regime_result,
        "suitable": suitable, "final_signal": final_signal,
        "long_score": long_score, "short_score": short_score, "margin": margin,
        # funnel flags, independent of each other, for attrition counting:
        "f_trend_gate_long": setup["long"].get("daily_trend_bullish", {}).get("met", False),
        "f_trend_gate_short": setup["short"].get("daily_trend_bearish", {}).get("met", False),
        "f_score_threshold": max(long_score, short_score) >= strategy.SIGNAL_THRESHOLDS["ACTIVE"],
        "f_margin_ok": margin >= strategy.MIN_SCORE_MARGIN,
        "f_regime_suitable": suitable,
        "f_has_direction": final_signal in ("STRONG BUY", "BUY", "STRONG SELL", "SELL"),
    }


def _direction_of(signal: str) -> str | None:
    if signal in ("STRONG BUY", "BUY"):
        return "LONG"
    if signal in ("STRONG SELL", "SELL"):
        return "SHORT"
    return None


# ---------------------------------------------------------------------
# Trade simulation (no lookahead: entry at next bar's open, exit
# scanned forward from there)
# ---------------------------------------------------------------------

def simulate_trade(data: pd.DataFrame, entry_bar: int, direction: str,
                    entry_price: float, stop_loss: float, target: float,
                    max_holding_days: int = MAX_HOLDING_DAYS) -> dict | None:
    """
    Scans forward from entry_bar (inclusive) for a stop/target/time exit.
    If both stop and target are within the same bar's high/low range,
    the STOP is assumed to hit first (conservative -- standard
    backtesting convention when intra-bar sequencing is unknown).

    direction must be lowercase "long"/"short" -- matches
    strategy.compute_trade_levels()'s actual return value. (An earlier
    version of this function checked "LONG"/"SHORT" uppercase, which
    silently never matched, routing every long trade through the short
    branch and flipping its R-multiple sign -- caught by testing
    against synthetic data before this shipped, not by inspection.)

    Returns None if stop_loss/target don't correctly bracket entry_price
    for the given direction -- this happens when the level was computed
    off the signal bar's close, but entry executes at the NEXT bar's
    open, and an overnight gap moved price past where the stop was
    calculated before the position was even live. That's not a
    simulation bug, it's a real gap-risk scenario; the correct backtest
    behavior is to skip the trade as invalid, not fabricate a result
    from a broken setup.
    """
    if direction == "long":
        if not (stop_loss < entry_price < target):
            return None
    else:
        if not (target < entry_price < stop_loss):
            return None

    risk_per_unit = abs(entry_price - stop_loss)
    end_bar = min(entry_bar + max_holding_days, len(data) - 1)

    for bar in range(entry_bar, end_bar + 1):
        hi, lo = float(data["high"].iloc[bar]), float(data["low"].iloc[bar])
        if direction == "long":
            if lo <= stop_loss:
                exit_price, reason = stop_loss, "stop"
                r = (exit_price - entry_price) / risk_per_unit
                return {"exit_bar": bar, "exit_price": exit_price, "reason": reason, "r_multiple": round(r, 3)}
            if hi >= target:
                exit_price, reason = target, "target"
                r = (exit_price - entry_price) / risk_per_unit
                return {"exit_bar": bar, "exit_price": exit_price, "reason": reason, "r_multiple": round(r, 3)}
        else:  # short
            if hi >= stop_loss:
                exit_price, reason = stop_loss, "stop"
                r = (entry_price - exit_price) / risk_per_unit
                return {"exit_bar": bar, "exit_price": exit_price, "reason": reason, "r_multiple": round(r, 3)}
            if lo <= target:
                exit_price, reason = target, "target"
                r = (entry_price - exit_price) / risk_per_unit
                return {"exit_bar": bar, "exit_price": exit_price, "reason": reason, "r_multiple": round(r, 3)}

    # neither hit within max_holding_days -- time-based exit at that bar's close
    exit_price = float(data["close"].iloc[end_bar])
    r = ((exit_price - entry_price) if direction == "long" else (entry_price - exit_price)) / risk_per_unit
    return {"exit_bar": end_bar, "exit_price": exit_price, "reason": "time", "r_multiple": round(r, 3)}


# ---------------------------------------------------------------------
# Main walk-forward loop
# ---------------------------------------------------------------------

def run_backtest(data: pd.DataFrame) -> dict:
    n = len(data)
    if n <= WARMUP_BARS + 1:
        raise ValueError(f"Need more than {WARMUP_BARS + 1} bars, got {n}")

    gate = TradeGate(persistence_bars=PERSISTENCE_BARS,
                      max_trades_per_day=10**9,   # daily-bar backtest: 1 decision/day already, cap doesn't apply
                      cooldown_minutes=COOLDOWN_DAYS_AFTER_STOP * 24 * 60)

    funnel = {
        "total_bars": 0, "has_direction": 0, "score_threshold": 0, "margin_ok": 0,
        "regime_suitable": 0, "trend_gate": 0, "persistent": 0, "not_in_cooldown": 0,
        "trades_taken": 0, "gap_invalidated_setups": 0,
    }
    trade_log = []
    blocked_until_bar = -1  # while a simulated position is open, skip new entries

    for i in range(WARMUP_BARS, n - 1):  # -1: need i+1 available for next-bar entry price
        daily_df = data.iloc[:i + 1]
        bar_date = data["date"].iloc[i].to_pydatetime()
        if bar_date.tzinfo is None:
            bar_date = IST.localize(bar_date)

        result = evaluate_bar(daily_df)
        funnel["total_bars"] += 1
        if result["f_has_direction"]:
            funnel["has_direction"] += 1
        if result["f_score_threshold"]:
            funnel["score_threshold"] += 1
        if result["f_margin_ok"]:
            funnel["margin_ok"] += 1
        if result["f_regime_suitable"]:
            funnel["regime_suitable"] += 1

        direction = _direction_of(result["final_signal"])
        trend_gate_ok = (result["f_trend_gate_long"] if direction == "LONG"
                          else result["f_trend_gate_short"] if direction == "SHORT" else False)
        if direction and trend_gate_ok:
            funnel["trend_gate"] += 1

        gate.record_signal(direction)
        gate_result = gate.gate(direction, now=bar_date)

        if direction and gate.is_persistent(direction):
            funnel["persistent"] += 1
        if direction and not gate.in_cooldown(bar_date):
            funnel["not_in_cooldown"] += 1

        if i <= blocked_until_bar:
            continue  # a simulated position is still open, skip new entries

        if direction and gate_result["allowed"]:
            trade_levels = strategy.compute_trade_levels(
                result["final_signal"], result["last_close"],
                result["strength"]["atr"], result["liquidity"]
            )
            if trade_levels is None:
                continue

            entry_bar = i + 1
            entry_price = float(data["open"].iloc[entry_bar])
            outcome = simulate_trade(data, entry_bar, trade_levels["direction"],
                                      entry_price, trade_levels["stop_loss"], trade_levels["target"])

            if outcome is None:
                # Overnight gap moved price past the computed stop/target
                # before the position was even live -- a real gap-risk
                # scenario (see simulate_trade()'s docstring), not a bug.
                # Not counted as a trade taken: nothing was actually
                # executable here, so it shouldn't occupy a gate slot.
                funnel["gap_invalidated_setups"] = funnel.get("gap_invalidated_setups", 0) + 1
                continue

            gate.record_trade_entry(bar_date)
            funnel["trades_taken"] += 1
            blocked_until_bar = outcome["exit_bar"]

            if outcome["reason"] == "stop":
                gate.record_stop_loss_hit(data["date"].iloc[outcome["exit_bar"]].to_pydatetime()
                                           .replace(tzinfo=IST) if data["date"].iloc[outcome["exit_bar"]].tzinfo is None
                                           else data["date"].iloc[outcome["exit_bar"]].to_pydatetime())

            trade_log.append({
                "signal_bar_date": str(data["date"].iloc[i].date()),
                "entry_bar_date": str(data["date"].iloc[entry_bar].date()),
                "exit_bar_date": str(data["date"].iloc[outcome["exit_bar"]].date()),
                "signal": result["final_signal"],
                "direction": trade_levels["direction"],
                "entry_price": round(entry_price, 2),
                "stop_loss": trade_levels["stop_loss"],
                "target": trade_levels["target"],
                "exit_price": outcome["exit_price"],
                "exit_reason": outcome["reason"],
                "holding_days": outcome["exit_bar"] - entry_bar,
                "r_multiple": outcome["r_multiple"],
                "long_score": result["long_score"], "short_score": result["short_score"],
            })

    return {"funnel": funnel, "trade_log": trade_log}


def summarize(trade_log: list) -> dict:
    if not trade_log:
        return {"count": 0, "win_rate": None, "avg_r": None, "total_r": None, "max_drawdown_r": None}

    rs = [t["r_multiple"] for t in trade_log]
    wins = [r for r in rs if r > 0]
    count = len(rs)
    win_rate = round(len(wins) / count * 100, 1)
    avg_r = round(sum(rs) / count, 3)
    total_r = round(sum(rs), 2)

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return {"count": count, "win_rate": win_rate, "avg_r": avg_r,
            "total_r": total_r, "max_drawdown_r": round(max_dd, 2)}


def print_funnel(funnel: dict) -> None:
    print("\n--- Signal funnel (bars surviving each gate, NOT cumulative-required order) ---")
    print(f"  Total bars evaluated:        {funnel['total_bars']}")
    print(f"  Had a direction (raw signal):{funnel['has_direction']}")
    print(f"  Score >= ACTIVE threshold:   {funnel['score_threshold']}")
    print(f"  Margin >= MIN_SCORE_MARGIN:  {funnel['margin_ok']}")
    print(f"  Regime suitable (TRENDING+high conf): {funnel['regime_suitable']}")
    print(f"  Trend gate passed (for the signaled direction): {funnel['trend_gate']}")
    print(f"  Persistent (2 consecutive days):      {funnel['persistent']}")
    print(f"  Not in post-stop cooldown:            {funnel['not_in_cooldown']}")
    print(f"  ACTUAL TRADES TAKEN:                  {funnel['trades_taken']}")
    print(f"  Gap-invalidated setups (skipped, not counted): {funnel['gap_invalidated_setups']}")


def print_summary(summary: dict) -> None:
    print("\n--- Trade outcome summary (R-multiples -- underlying-move PROXY, NOT option P&L) ---")
    if summary["count"] == 0:
        print("  No trades were taken over this period.")
        return
    print(f"  Trades taken:      {summary['count']}")
    print(f"  Win rate:          {summary['win_rate']}%")
    print(f"  Avg R per trade:   {summary['avg_r']}")
    print(f"  Total R:           {summary['total_r']}")
    print(f"  Max drawdown (R):  {summary['max_drawdown_r']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=750, help="days of daily history to pull from Upstox")
    parser.add_argument("--csv", type=str, default=None, help="path to a local CSV instead of calling Upstox")
    parser.add_argument("--output-dir", type=str, default=".", help="where to write trade_log.csv and backtest_summary.json")
    args = parser.parse_args()

    if args.csv:
        print(f"Loading historical data from {args.csv} ...")
        data = load_from_csv(args.csv)
    else:
        print(f"Pulling {args.days} days of daily Nifty history from Upstox ...")
        data = load_from_upstox(args.days)

    print(f"Loaded {len(data)} daily bars: {data['date'].iloc[0].date()} -> {data['date'].iloc[-1].date()}")

    result = run_backtest(data)
    summary = summarize(result["trade_log"])

    print_funnel(result["funnel"])
    print_summary(summary)

    import os
    os.makedirs(args.output_dir, exist_ok=True)
    trade_log_path = os.path.join(args.output_dir, "trade_log.csv")
    summary_path = os.path.join(args.output_dir, "backtest_summary.json")

    pd.DataFrame(result["trade_log"]).to_csv(trade_log_path, index=False)
    with open(summary_path, "w") as f:
        json.dump({"funnel": result["funnel"], "summary": summary}, f, indent=2)

    print(f"\nWrote {trade_log_path} and {summary_path}")


if __name__ == "__main__":
    main()
