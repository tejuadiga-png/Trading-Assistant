from __future__ import annotations
import numpy as np
import pandas as pd

def _validate_bars(left_bars: int, right_bars: int) -> None:
    if left_bars < 1 or right_bars < 1:
        raise ValueError("left_bars and right_bars must both be >= 1")

def find_swing_high(highs, left_bars: int = 3, right_bars: int = 3) -> pd.Series:
    """Identify confirmed swing highs (fractal tops) in a series of highs."""
    _validate_bars(left_bars, right_bars)
    s = pd.Series(highs, dtype="float64").reset_index(drop=True)
    n = len(s)
    if n < left_bars + right_bars + 1:
        return pd.Series(False, index=s.index)

    left_max = s.shift(1).rolling(left_bars).max()
    right_max = s[::-1].shift(1).rolling(right_bars).max()[::-1]

    return ((s > left_max) & (s > right_max)).fillna(False)

def find_swing_low(lows, left_bars: int = 3, right_bars: int = 3) -> pd.Series:
    """Identify confirmed swing lows (fractal bottoms)."""
    _validate_bars(left_bars, right_bars)
    s = pd.Series(lows, dtype="float64").reset_index(drop=True)
    n = len(s)
    if n < left_bars + right_bars + 1:
        return pd.Series(False, index=s.index)

    left_min = s.shift(1).rolling(left_bars).min()
    right_min = s[::-1].shift(1).rolling(right_bars).min()[::-1]

    return ((s < left_min) & (s < right_min)).fillna(False)


def _last_confirmed_level(swing_flags: pd.Series, level_values: pd.Series,
                           right_bars: int) -> tuple[pd.Series, pd.Series]:
    idx = pd.Series(swing_flags.index.to_numpy(), index=swing_flags.index, dtype="float64")

    level_at_swing = level_values.where(swing_flags)
    idx_at_swing = idx.where(swing_flags)

    level = level_at_swing.shift(right_bars).ffill()
    swing_index = idx_at_swing.shift(right_bars).ffill()
    return level, swing_index


def bullish_liquidity_sweep(df: pd.DataFrame, left_bars: int = 3, right_bars: int = 3,
                             detailed: bool = False):
    """Bullish liquidity sweep: wick below the last confirmed swing low,
    close back above it."""
    _validate_bars(left_bars, right_bars)
    if not {"low", "close"}.issubset(df.columns):
        raise ValueError("df must contain 'low' and 'close' columns")

    df = df.reset_index(drop=True)
    lows = df["low"].astype("float64")
    closes = df["close"].astype("float64")

    swing_low_flags = find_swing_low(lows, left_bars, right_bars)
    last_level, swing_index = _last_confirmed_level(swing_low_flags, lows, right_bars)

    sweep = ((lows < last_level) & (closes > last_level)).fillna(False)

    if not detailed:
        return sweep

    bars_since = pd.Series(sweep.index, index=sweep.index, dtype="float64") - swing_index
    return pd.DataFrame({
        "sweep": sweep,
        "swept_level": last_level.where(sweep),
        "bars_since_swing": bars_since.where(sweep),
    })


def bearish_liquidity_sweep(df: pd.DataFrame, left_bars: int = 3, right_bars: int = 3,
                             detailed: bool = False):
    """Bearish liquidity sweep: wick above the last confirmed swing high,
    close back below it."""
    _validate_bars(left_bars, right_bars)
    if not {"high", "close"}.issubset(df.columns):
        raise ValueError("df must contain 'high' and 'close' columns")

    df = df.reset_index(drop=True)
    highs = df["high"].astype("float64")
    closes = df["close"].astype("float64")

    swing_high_flags = find_swing_high(highs, left_bars, right_bars)
    last_level, swing_index = _last_confirmed_level(swing_high_flags, highs, right_bars)

    sweep = ((highs > last_level) & (closes < last_level)).fillna(False)

    if not detailed:
        return sweep

    bars_since = pd.Series(sweep.index, index=sweep.index, dtype="float64") - swing_index
    return pd.DataFrame({
        "sweep": sweep,
        "swept_level": last_level.where(sweep),
        "bars_since_swing": bars_since.where(sweep),
    })


def detect_liquidity_sweeps(df: pd.DataFrame, left_bars: int = 3, right_bars: int = 3) -> pd.DataFrame:
    """Run both directions, return a combined signal frame:
    bullish_sweep, bearish_sweep, swept_level, direction."""
    bull = bullish_liquidity_sweep(df, left_bars, right_bars, detailed=True)
    bear = bearish_liquidity_sweep(df, left_bars, right_bars, detailed=True)

    out = pd.DataFrame(index=df.reset_index(drop=True).index)
    out["bullish_sweep"] = bull["sweep"]
    out["bearish_sweep"] = bear["sweep"]
    out["swept_level"] = bull["swept_level"].fillna(bear["swept_level"])
    out["direction"] = np.select(
        [out["bullish_sweep"], out["bearish_sweep"]],
        ["bullish", "bearish"],
        default=None,
    )
    return out
