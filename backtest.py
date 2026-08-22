"""
backtest.py — walk-forward backtest of the full pipeline exactly as
wired in pipeline_adapter.py: regime.py -> strategy.py -> trade_gate.py.

======================================================================
READ THIS BEFORE TRUSTING ANY NUMBER THIS PRODUCES
======================================================================
WHAT THIS VALIDATES:
  - How often each gate fires (regime/confidence, trend, score-margin,
    persistence, daily cap, cooldown) -- i.e. answers "is this too
    strict" with actual counts, not guesses.
  - Hypothetical win rate / R-multiple IF the underlying index moves
    the way strategy.compute_trade_levels()'s stop/target assume.

WHAT THIS DOES NOT VALIDATE:
  - Real option premium P&L. Trades are simulated in UNDERLYING INDEX
    POINTS, not option premium. Delta, gamma, theta decay, IV changes,
    strike liquidity, and slippage are NOT modeled. A signal that is
    structurally correct on the index (the stop/target logic calls it
    a "win") can still lose money on the actual option bought against
    it, because of theta/IV alone.
  - Why: Upstox's historical candle API (the only data source wired
    into this project) provides index/underlying OHLC, not historical
    option chain snapshots. A real premium-based backtest needs a paid
    options-history data vendor, which isn't part of this codebase.

Treat every number below as "how the SIGNAL ENGINE behaves", not "how
much money this would have made." Anyone auditing this should start
from that distinction.
======================================================================

NO-LOOKAHEAD DISCIPLINE:
  - At simulated time t, only daily bars from FULLY CLOSED prior days
    are used (a bar dated today is never used intraday today).
  - Only 4H bars that have fully closed (open_time + 4h <= t) are used.
  - The LTF (low-timeframe) window is truncated to bars up to and
    including t only.
  - Trade outcomes are resolved walking STRICTLY FORWARD from the entry
    bar; the exit-bar decision (stop vs target) never peeks past the
    bar being evaluated.

USAGE:
    from backtest import run_backtest, summarize
    results = run_backtest(daily_df, h4_df, ltf_df)
    print(summarize(results))
    results["ticks"].to_csv("backtest_ticks.csv", index=False)
    results["trades"].to_csv("backtest_trades.csv", index=False)

Or run this file directly for a self-contained demo (see __main__).
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from datetime import datetime

import strategy
import regime
import risk
from volume_profile import calculate_volume_profile
from trade_gate import TradeGate

MIN_LTF_HISTORY = 60          # bars of LTF history required before any signal is even attempted
LTF_ROLLING_WINDOW = 300      # bars of LTF history fed to liquidity/volume-profile detectors per tick
                               # (bounded window -- matches how the live dashboard's rolling
                               # candle buffer works, and keeps per-tick cost roughly constant
                               # instead of growing unboundedly over a multi-month backtest)


def _cutoff_indices(daily_times: pd.Series, h4_times: pd.Series, ts: pd.Timestamp) -> tuple[int, int]:
    """Point-in-time cutoffs, no-lookahead:
    - daily: only days strictly BEFORE ts's date (today's daily candle
      isn't closed yet during today's session)
    - h4: only 4H bars whose bar + 4h has fully elapsed by ts
    Returns (daily_count, h4_count) -- i.e. df.iloc[:count] is valid."""
    daily_dates = daily_times.dt.date.to_numpy()
    daily_count = int(np.searchsorted(daily_dates, ts.date(), side="left"))

    h4_close_times = (h4_times + pd.Timedelta(hours=4)).to_numpy()
    h4_count = int(np.searchsorted(h4_close_times, np.datetime64(ts), side="right"))

    return daily_count, h4_count


def run_backtest(daily_df: pd.DataFrame, h4_df: pd.DataFrame, ltf_df: pd.DataFrame,
                  min_ltf_history: int = MIN_LTF_HISTORY,
                  verbose: bool = False) -> dict:
    """
    Walks ltf_df bar by bar. Each df needs columns: time, open, high,
    low, close, volume (time as tz-aware or naive pandas Timestamps,
    consistently).

    Returns:
        {
            "ticks": pd.DataFrame,   # one row per LTF bar processed -- the full gate funnel
            "trades": pd.DataFrame,  # one row per simulated trade, with outcome
            "summary": dict,         # aggregate counts/rates
        }
    """
    assert {"time", "open", "high", "low", "close", "volume"}.issubset(ltf_df.columns)
    ltf_df = ltf_df.reset_index(drop=True)
    daily_df = daily_df.reset_index(drop=True)
    h4_df = h4_df.reset_index(drop=True)

    gate = TradeGate()
    last_confirmed_direction: str | None = None

    open_trade = None  # dict, or None if flat
    ticks = []
    trades = []

    n = len(ltf_df)
    for i in range(n):
        if i < min_ltf_history:
            continue

        row = ltf_df.iloc[i]
        ts = row["time"]
        window_start = max(0, i - LTF_ROLLING_WINDOW + 1)
        ltf_slice = ltf_df.iloc[window_start:i + 1]

        d_count, h_count = _cutoff_indices(daily_df["time"], h4_df["time"], ts)
        if d_count < 50 or h_count < 50:
            # not enough closed daily/4H history yet for EMA50 etc. --
            # skip rather than compute on insufficient data (matches
            # the real system's insufficient-history fallback behavior)
            continue

        daily_slice = daily_df.iloc[:d_count]
        h4_slice = h4_df.iloc[:h_count]

        try:
            liquidity = strategy.compute_liquidity_state(ltf_slice)
            vp = calculate_volume_profile(ltf_slice, bins=20)
            dash = strategy.build_dashboard(daily_slice, h4_slice, ltf_slice, liquidity, vp)
            regime_result = regime.classify_market_state(daily_slice, h4_slice)
        except Exception as e:
            if verbose:
                print(f"[{ts}] skipped (compute error): {e}")
            continue

        suitable = regime.is_market_suitable(regime_result)
        raw_signal = dash["signal"]                       # ignores regime suitability
        final_signal = raw_signal if suitable else "WATCH"  # matches run_pipeline's short-circuit

        direction = None
        if final_signal in ("STRONG BUY", "BUY"):
            direction = "LONG"
        elif final_signal in ("STRONG SELL", "SELL"):
            direction = "SHORT"

        gate.record_signal(direction)
        gate_result = gate.gate(direction, now=ts)

        entered_trade = False
        if open_trade is None and gate_result["allowed"] and direction is not None:
            if direction != last_confirmed_direction:
                gate.record_trade_entry(ts)
                last_confirmed_direction = direction
                tl = dash["trade_levels"]
                if tl is not None:
                    open_trade = {
                        "entry_time": ts, "entry_idx": i,
                        "direction": direction, "signal": final_signal,
                        "entry_price": tl["entry"], "stop_loss": tl["stop_loss"],
                        "target": tl["target"],
                    }
                    entered_trade = True
        elif direction is None:
            last_confirmed_direction = None

        ticks.append({
            "time": ts,
            "regime": regime_result.get("regime"),
            "confidence": regime_result.get("confidence"),
            "regime_suitable": suitable,
            "long_score": dash["scores"]["long_score"],
            "short_score": dash["scores"]["short_score"],
            "raw_signal": raw_signal,
            "final_signal": final_signal,
            "direction": direction,
            "gate_allowed": gate_result["allowed"],
            "gate_reason": gate_result["reason"],
            "trade_entered": entered_trade,
            "in_open_trade": open_trade is not None,
        })

        # -- resolve an open trade, walking forward one bar at a time --
        if open_trade is not None and not entered_trade:
            direction_o = open_trade["direction"]
            stop, target = open_trade["stop_loss"], open_trade["target"]
            bar_high, bar_low, bar_close = row["high"], row["low"], row["close"]

            hit_stop = (bar_low <= stop) if direction_o == "LONG" else (bar_high >= stop)
            hit_target = (bar_high >= target) if direction_o == "LONG" else (bar_low <= target)

            exit_reason = None
            exit_price = None
            if hit_stop and hit_target:
                # both touched in the same bar -- assume the WORSE outcome
                # (stop first), standard conservative backtest convention
                exit_reason, exit_price = "stop", stop
            elif hit_stop:
                exit_reason, exit_price = "stop", stop
            elif hit_target:
                exit_reason, exit_price = "target", target
            else:
                holding = risk.check_holding_period(open_trade["entry_time"], now=ts)
                if holding["exit_required"]:
                    exit_reason, exit_price = "timeout", bar_close

            if exit_reason is not None:
                entry_price = open_trade["entry_price"]
                risk_dist = abs(entry_price - stop)
                pnl_points = (exit_price - entry_price) if direction_o == "LONG" else (entry_price - exit_price)
                r_multiple = round(pnl_points / risk_dist, 3) if risk_dist > 0 else None

                trades.append({
                    **{k: v for k, v in open_trade.items() if k not in ("entry_idx",)},
                    "exit_time": ts, "exit_price": exit_price, "exit_reason": exit_reason,
                    "pnl_points": round(pnl_points, 2), "r_multiple": r_multiple,
                    "bars_held": i - open_trade["entry_idx"],
                })
                if exit_reason == "stop":
                    gate.record_stop_loss_hit(ts)
                open_trade = None
                last_confirmed_direction = None

    ticks_df = pd.DataFrame(ticks)
    trades_df = pd.DataFrame(trades)
    return {"ticks": ticks_df, "trades": trades_df, "summary": _summarize_dict(ticks_df, trades_df)}


def _summarize_dict(ticks_df: pd.DataFrame, trades_df: pd.DataFrame) -> dict:
    n_ticks = len(ticks_df)
    if n_ticks == 0:
        return {"n_ticks": 0}

    n_regime_suitable = int(ticks_df["regime_suitable"].sum())
    n_raw_signal = int((ticks_df["raw_signal"] != "WATCH").sum())
    n_final_signal = int((ticks_df["final_signal"] != "WATCH").sum())
    n_gate_allowed = int(ticks_df["gate_allowed"].sum())
    n_entries = int(ticks_df["trade_entered"].sum())

    gate_block_reasons = (
        ticks_df.loc[~ticks_df["gate_allowed"] & ticks_df["direction"].notna(), "gate_reason"]
        .value_counts().to_dict()
    )

    summary = {
        "n_ticks_processed": n_ticks,
        "n_regime_suitable_ticks": n_regime_suitable,
        "n_raw_signal_ticks (pre-regime, post trend+margin gate)": n_raw_signal,
        "n_final_signal_ticks (post-regime)": n_final_signal,
        "n_gate_allowed_ticks": n_gate_allowed,
        "n_trades_entered": n_entries,
        "gate_block_reason_counts": gate_block_reasons,
    }

    if not trades_df.empty:
        wins = trades_df[trades_df["exit_reason"] == "target"]
        losses = trades_df[trades_df["exit_reason"] == "stop"]
        timeouts = trades_df[trades_df["exit_reason"] == "timeout"]
        closed = trades_df[trades_df["r_multiple"].notna()]

        summary.update({
            "n_trades_closed": len(trades_df),
            "n_wins (target hit)": len(wins),
            "n_losses (stop hit)": len(losses),
            "n_timeouts (max holding period)": len(timeouts),
            "win_rate_pct (target vs target+stop, excl timeouts)":
                round(len(wins) / max(1, len(wins) + len(losses)) * 100, 1),
            "avg_r_multiple": round(closed["r_multiple"].mean(), 3) if not closed.empty else None,
            "total_r_multiple": round(closed["r_multiple"].sum(), 2) if not closed.empty else None,
            "max_losing_streak_r": _max_losing_streak(closed),
        })
    else:
        summary.update({"n_trades_closed": 0})

    return summary


def _max_losing_streak(closed_trades: pd.DataFrame) -> int:
    if closed_trades.empty:
        return 0
    streak = max_streak = 0
    for r in closed_trades["r_multiple"]:
        if r < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def summarize(results: dict) -> str:
    """Human-readable report string from run_backtest()'s output."""
    s = results["summary"]
    lines = ["=" * 60, "BACKTEST SUMMARY", "=" * 60]
    for k, v in s.items():
        if isinstance(v, dict):
            lines.append(f"{k}:")
            for kk, vv in v.items():
                lines.append(f"    {kk}: {vv}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("=" * 60)
    lines.append("REMINDER: outcomes above are simulated on the underlying")
    lines.append("index in POINTS, not real option premium. See module")
    lines.append("docstring for what this does and doesn't validate.")
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Standalone demo / entry point
# ---------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    daily_df = h4_df = ltf_df = None

    try:
        from upstox import get_historical_nifty

        def _candles_to_df(raw_candles):
            return pd.DataFrame({
                "time": pd.to_datetime([c[0] for c in raw_candles]),
                "open": [c[1] for c in raw_candles], "high": [c[2] for c in raw_candles],
                "low": [c[3] for c in raw_candles], "close": [c[4] for c in raw_candles],
                "volume": [c[5] for c in raw_candles],
            })

        print("Attempting to fetch real Upstox historical data...")
        daily_raw = get_historical_nifty(interval="day", days=400)
        daily_candles = list(reversed((daily_raw or {}).get("data", {}).get("candles") or []))
        h1_raw = get_historical_nifty(interval="60minute", days=90)
        h1_candles = list(reversed((h1_raw or {}).get("data", {}).get("candles") or []))
        ltf_raw = get_historical_nifty(interval="15minute", days=60)
        ltf_candles = list(reversed((ltf_raw or {}).get("data", {}).get("candles") or []))

        if not (daily_candles and h1_candles and ltf_candles):
            raise ValueError("empty response from Upstox -- credentials/network issue")

        daily_df = _candles_to_df(daily_candles)
        h1_df = _candles_to_df(h1_candles)
        h4_df = (h1_df.set_index("time").resample("4h")
                 .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                 .dropna().reset_index())
        ltf_df = _candles_to_df(ltf_candles)
        print(f"Loaded real data: {len(daily_df)} daily, {len(h4_df)} 4H, {len(ltf_df)} LTF (15m) bars.")

    except Exception as e:
        print(f"Could not load real Upstox data ({e}).")
        print("Falling back to SYNTHETIC data -- this validates the ENGINE'S")
        print("MECHANICS ONLY (no-lookahead correctness, gate wiring), NOT")
        print("real market behavior. Re-run with a working Upstox connection")
        print("for results that mean anything about actual trading.\n")

        rng = np.random.default_rng(42)

        def _synthetic(n, start, freq_minutes, drift=0.02, noise=0.3, start_time=None):
            start_time = start_time or pd.Timestamp("2025-01-01 09:15", tz="Asia/Kolkata")
            times = pd.date_range(start_time, periods=n, freq=f"{freq_minutes}min")
            close = start + np.cumsum(rng.normal(drift, noise, n))
            high = close + rng.uniform(0.1, 1.0, n)
            low = close - rng.uniform(0.1, 1.0, n)
            open_ = close + rng.normal(0, 0.3, n)
            vol = rng.uniform(1000, 5000, n)
            return pd.DataFrame({"time": times, "open": open_, "high": high, "low": low,
                                  "close": close, "volume": vol})

        daily_df = _synthetic(400, 24000, 60 * 24, drift=2.0, noise=15)
        h4_df = _synthetic(600, 24000, 240, drift=0.5, noise=8)
        ltf_df = _synthetic(1500, 24000, 15, drift=0.15, noise=3)

    print(f"\nRunning backtest over {len(ltf_df)} LTF bars...")
    t0 = datetime.now()
    results = run_backtest(daily_df, h4_df, ltf_df, verbose=False)
    elapsed = (datetime.now() - t0).total_seconds()
    print(f"Done in {elapsed:.1f}s.\n")

    print(summarize(results))

    results["ticks"].to_csv("backtest_ticks.csv", index=False)
    results["trades"].to_csv("backtest_trades.csv", index=False)
    print("\nWrote backtest_ticks.csv and backtest_trades.csv for full audit trail.")
