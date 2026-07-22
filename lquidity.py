import numpy as np
import pandas as pd

def find_swing_high(highs, left_bars=3, right_bars=3):
    """Identify swing highs in a series of high prices.
    A swing high at position i means:
        highs[i] > highs[i - left_bars : i]   (all of them)
        highs[i] > highs[i + 1 : i + right_bars + 1]   (all of them)
Parameters
highs : pd.Series or array-like
    left_bars : int
        Number of candles to the left that must be lower.
    right_bars : int
        Number of candles to the right that must be lower.
    Returns
    pd.Series of bool, same index as `highs`.
        True where a swing high is confirmed."""
    highs = pd.Series(highs).reset_index(drop=True)
    n = len(highs)
    swing = pd.Series(False, index=highs.index)

    for i in range(left_bars, n - right_bars):
        left_window = highs[i - left_bars:i]
        right_window = highs[i + 1:i + right_bars + 1]
        current = highs[i]

        if current > left_window.max() and current > right_window.max():
            swing.iloc[i] = True

    return swing

def find_swing_low(lows, left_bars=3, right_bars=3):
    """Identify swing lows in a series of low prices.
    A swing low at position i means:
        lows[i] < lows[i - left_bars : i]   (all of them)
        lows[i] < lows[i + 1 : i + right_bars + 1]   (all of them)
    Parameters
    lows : pd.Series or array-like
    left_bars : int
        Number of candles to the left that must be higher.
    right_bars : int
        Number of candles to the right that must be higher.
    Returns
    pd.Series of bool, same index as `lows`.
        True where a swing low is confirmed."""
    lows = pd.Series(lows).reset_index(drop=True)
    n = len(lows)
    swing = pd.Series(False, index=lows.index)

    for i in range(left_bars, n - right_bars):
        left_window = lows[i - left_bars:i]
        right_window = lows[i + 1:i + right_bars + 1]
        current = lows[i]

        if current < left_window.min() and current < right_window.min():
            swing.iloc[i] = True

    return swing

def bullish_liquidity_sweep(df, left_bars=3, right_bars=3):
    """ Detect bullish liquidity sweeps (a.k.a. bullish stop hunts).
    Logic: price wicks below a previously confirmed swing low
    (triggering resting sell-stops / breakout-short orders) but the
    candle closes back above that swing low, suggesting the downside
    break was a liquidity grab rather than a genuine breakdown.
    Parameters
    df : pd.DataFrame
        Must contain 'high', 'low', 'close' columns.
    left_bars, right_bars : int
        Passed through to find_swing_low for swing detection.
    Returns
    pd.Series of bool, same index as `df`.
        True on candles where a bullish liquidity sweep occurred."""
    df = df.reset_index(drop=True)
    lows = df["low"]
    closes = df["close"]

    swing_low_flags = find_swing_low(lows, left_bars, right_bars)
    swing_low_indices = swing_low_flags[swing_low_flags].index.tolist()

    n = len(df)
    sweep = pd.Series(False, index=df.index)

    # Track the most recent confirmed swing low level as we move forward
    last_swing_low_level = None
    swing_pointer = 0

    for i in range(n):
        # Update last confirmed swing low if one was confirmed at/before i
        while swing_pointer < len(swing_low_indices) and swing_low_indices[swing_pointer] <= i:
            last_swing_low_level = lows.iloc[swing_low_indices[swing_pointer]]
            swing_pointer += 1

        if last_swing_low_level is None:
            continue

        # Bullish sweep: current low pierces below the swing low level,
        # but current close ends back above that level.
        if lows.iloc[i] < last_swing_low_level and closes.iloc[i] > last_swing_low_level:
            sweep.iloc[i] = True

    return sweep

def bearish_liquidity_sweep(df, left_bars=3, right_bars=3):
    """Detect bearish liquidity sweeps (a.k.a. bearish stop hunts).
    Logic: price wicks above a previously confirmed swing high
    (triggering resting buy-stops / breakout-long orders) but the
    candle closes back below that swing high, suggesting the upside
    break was a liquidity grab rather than a genuine breakout.
Parameters
df : pd.DataFrame
        Must contain 'high', 'low', 'close' columns.
    left_bars, right_bars : int
        Passed through to find_swing_high for swing detection.
    Returns
    pd.Series of bool, same index as `df`.
        True on candles where a bearish liquidity sweep occurred."""
    df = df.reset_index(drop=True)
    highs = df["high"]
    closes = df["close"]

    swing_high_flags = find_swing_high(highs, left_bars, right_bars)
    swing_high_indices = swing_high_flags[swing_high_flags].index.tolist()

    n = len(df)
    sweep = pd.Series(False, index=df.index)

    # Track the most recent confirmed swing high level as we move forward
    last_swing_high_level = None
    swing_pointer = 0

    for i in range(n):
        # Update last confirmed swing high if one was confirmed at/before i
        while swing_pointer < len(swing_high_indices) and swing_high_indices[swing_pointer] <= i:
            last_swing_high_level = highs.iloc[swing_high_indices[swing_pointer]]
            swing_pointer += 1

        if last_swing_high_level is None:
            continue

        # Bearish sweep: current high pierces above the swing high level,
        # but current close ends back below that level.
        if highs.iloc[i] > last_swing_high_level and closes.iloc[i] < last_swing_high_level:
            sweep.iloc[i] = True

    return sweep
