#sweep
"""
signals.py — bridges real detectors (liquidity_sweeps.py) into the
conviction engine, replacing pieces of mock_market_state one at a time.
"""
import pandas as pd
from liquidity_sweeps import detect_liquidity_sweeps
from flags import combine_flags


def compute_sweep_signal(df: pd.DataFrame, left_bars: int = 3, right_bars: int = 3) -> pd.DataFrame:
    """Your original line, generalized:
        out["sweep_confirmed"] = (out["bullish_sweep"] | out["bearish_sweep"])
    is now just this combine_flags call — same result, but reusable for
    every future composite flag (trend_aligned, banknifty_confirms, etc.)
    without writing a new '|' line each time."""
    out = detect_liquidity_sweeps(df, left_bars, right_bars)
    out = combine_flags(out, "sweep_confirmed", ["bullish_sweep", "bearish_sweep"], op="or")
    return out


def latest_sweep_state(candles: dict, left_bars: int = 3, right_bars: int = 3) -> dict | None:
    """
    Takes dashboard.py's live_state["candles"] dict (opens/highs/lows/
    closes/volumes/times lists) and returns the most recent bar's real
    sweep reading, in the shape compute_conviction() needs:
        {"sweep_confirmed": bool, "swept_level": float | None, "direction": str | None}

    Returns None if there isn't enough history yet (need >= left_bars +
    right_bars + 1 candles) — caller should fall back to the mock value
    in that case rather than treat None as "no sweep".
    """
    n = len(candles.get("closes", []))
    if n < left_bars + right_bars + 1:
        return None

    df = pd.DataFrame({
        "high":  candles["highs"],
        "low":   candles["lows"],
        "close": candles["closes"],
    })

    out = compute_sweep_signal(df, left_bars, right_bars)
    # NOTE: right_bars delay means the most recent right_bars rows can
    # never be confirmed sweeps yet — that's correct (no lookahead), not
    # a bug. The latest available reading is simply the last row.
    last = out.iloc[-1]
    return {
        "sweep_confirmed": bool(last["sweep_confirmed"]),
        "swept_level": None if pd.isna(last["swept_level"]) else float(last["swept_level"]),
        "direction": last["direction"],
    }
