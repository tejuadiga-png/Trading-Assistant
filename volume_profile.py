"""
volume_profile.py — builds volume profiles from OHLCV data and derives
POC (Point of Control) and Value Area, for use alongside signals.py /
liquidity_sweeps.py in the conviction engine.
"""
import numpy as np
import pandas as pd


def build_volume_profile(df: pd.DataFrame, bins: int = 50, bin_size: float = None) -> pd.DataFrame:
    """
    Distributes volume across price bins for the given OHLCV data.
    Approximates each candle's volume as evenly spread across its
    high-low range, assigning fractional volume to every bin the
    candle touches.

    bins: number of bins to split the price range into (ignored if
        bin_size is provided).
    bin_size: fixed price width per bin (e.g. 0.5, 5, 25 — whatever
        makes sense for the instrument's tick size). When set, the
        number of bins is derived from the price range instead, so
        every bin represents the same price width regardless of how
        wide the range is. Use this when you want consistent bin
        granularity across different symbols/timeframes rather than
        always splitting into a fixed count.

    Returns a DataFrame with columns:
        price_low, price_high, price_mid, volume
    sorted by price_low ascending.
    """
    lo = df["low"].min()
    hi = df["high"].max()

    if bin_size is not None:
        if bin_size <= 0:
            raise ValueError("bin_size must be positive")
        n_bins = max(int(np.ceil((hi - lo) / bin_size)), 1)
        edges = lo + np.arange(n_bins + 1) * bin_size
        edges[-1] = max(edges[-1], hi)  # ensure last bin covers the high
        bins = n_bins
    else:
        edges = np.linspace(lo, hi, bins + 1)

    bin_volumes = np.zeros(bins)

    for _, row in df.iterrows():
        row_lo, row_hi, vol = row["low"], row["high"], row["volume"]
        if row_hi == row_lo:
            idx = np.clip(np.searchsorted(edges, row_lo, side="right") - 1, 0, bins - 1)
            bin_volumes[idx] += vol
            continue

        start_idx = max(np.searchsorted(edges, row_lo, side="right") - 1, 0)
        end_idx = min(np.searchsorted(edges, row_hi, side="left"), bins - 1)

        for i in range(start_idx, end_idx + 1):
            bin_lo, bin_hi = edges[i], edges[i + 1]
            overlap = min(row_hi, bin_hi) - max(row_lo, bin_lo)
            if overlap > 0:
                fraction = overlap / (row_hi - row_lo)
                bin_volumes[i] += vol * fraction

    profile = pd.DataFrame({
        "price_low": edges[:-1],
        "price_high": edges[1:],
        "price_mid": (edges[:-1] + edges[1:]) / 2,
        "volume": bin_volumes,
    })
    return profile


def calculate_poc(profile: pd.DataFrame) -> float:
    """
    Point of Control: the price level (bin midpoint) with the highest
    traded volume. Returns NaN if the profile is empty or all-zero.
    """
    if profile.empty or profile["volume"].sum() == 0:
        return float("nan")
    return float(profile.loc[profile["volume"].idxmax(), "price_mid"])


def calculate_value_area(profile: pd.DataFrame, value_area: float = 0.70) -> dict:
    """
    Value Area: the contiguous price range containing `value_area`
    fraction (default 70%) of total volume, expanding outward from the
    POC bin, always adding whichever adjacent side (above/below) has
    the greater volume at each step.

    Returns {"val": float, "vah": float, "poc": float}.
    Returns all-NaN dict if profile is empty or all-zero volume.
    """
    if profile.empty or profile["volume"].sum() == 0:
        return {"val": float("nan"), "vah": float("nan"), "poc": float("nan")}

    profile = profile.reset_index(drop=True)
    total_volume = profile["volume"].sum()
    target_volume = total_volume * value_area

    poc_idx = profile["volume"].idxmax()
    accumulated = profile.loc[poc_idx, "volume"]
    low_idx, high_idx = poc_idx, poc_idx

    while accumulated < target_volume:
        can_go_lower = low_idx > 0
        can_go_higher = high_idx < len(profile) - 1

        if not can_go_lower and not can_go_higher:
            break

        vol_lower = profile.loc[low_idx - 1, "volume"] if can_go_lower else -1
        vol_higher = profile.loc[high_idx + 1, "volume"] if can_go_higher else -1

        if vol_lower >= vol_higher:
            low_idx -= 1
            accumulated += profile.loc[low_idx, "volume"]
        else:
            high_idx += 1
            accumulated += profile.loc[high_idx, "volume"]

    return {
        "val": float(profile.loc[low_idx, "price_low"]),
        "vah": float(profile.loc[high_idx, "price_high"]),
        "poc": float(profile.loc[poc_idx, "price_mid"]),
    }


def calculate_volume_profile(df: pd.DataFrame, bins: int = 50, bin_size: float = None) -> dict:
    """
    Convenience wrapper: builds the profile and returns POC + Value Area
    together, plus the raw profile itself for charting/inspection.
    """
    profile = build_volume_profile(df, bins=bins, bin_size=bin_size)
    poc = calculate_poc(profile)
    va = calculate_value_area(profile, value_area=0.70)
    return {
        "profile": profile,
        "poc": poc,
        "val": va["val"],
        "vah": va["vah"],
    }


def latest_volume_profile_state(
    candles: dict,
    bins: int = 50,
    bin_size: float = None,
    profile_type: str = "full",
    rolling_window: int = 200,
) -> dict | None:
    """
    Takes dashboard.py's live_state["candles"] dict (opens/highs/lows/
    closes/volumes/times lists) and returns the current volume profile
    reading, in the shape compute_conviction() needs:
        {
            "poc": float,
            "val": float,
            "vah": float,
            "price_vs_poc": str,   # "above" | "below" | "at"
            "in_value_area": bool,
        }
    Returns None if there isn't enough history yet, or if the candle
    lists are mismatched in length — caller should fall back to the
    mock value in that case rather than treat None as "no profile".

    bins / bin_size: same meaning as in build_volume_profile — pass
        bin_size instead of bins for fixed-width bins.

    profile_type:
        "full"    — use all available candle history.
        "rolling" — use only the most recent `rolling_window` candles,
                    e.g. a moving/rolling profile that updates each bar
                    rather than accumulating forever.
        "session" — use only candles from the most recent session
                    (calendar day), based on candles["times"]. Resets
                    at each new day rather than carrying volume over
                    from prior sessions. Requires candles["times"] to
                    be present and parseable by pd.to_datetime (epoch
                    seconds/ms or ISO strings both work).
    """
    highs = candles.get("highs", [])
    lows = candles.get("lows", [])
    closes = candles.get("closes", [])
    volumes = candles.get("volumes", [])
    times = candles.get("times", [])

    lengths = [len(highs), len(lows), len(closes), len(volumes)]
    if profile_type == "session":
        lengths.append(len(times))
    if len(set(lengths)) != 1:
        return None

    n = len(closes)
    if n < bins:
        return None

    if profile_type == "rolling":
        highs = highs[-rolling_window:]
        lows = lows[-rolling_window:]
        closes = closes[-rolling_window:]
        volumes = volumes[-rolling_window:]
        if len(closes) < bins:
            return None
    elif profile_type == "session":
        ts = pd.to_datetime(pd.Series(times))
        session_dates = ts.dt.date
        last_session = session_dates.iloc[-1]
        mask = (session_dates == last_session).to_numpy()

        highs = [h for h, m in zip(highs, mask) if m]
        lows = [l for l, m in zip(lows, mask) if m]
        closes = [c for c, m in zip(closes, mask) if m]
        volumes = [v for v, m in zip(volumes, mask) if m]
        if len(closes) < bins:
            return None
    elif profile_type != "full":
        raise ValueError(
            f"unknown profile_type: {profile_type!r} (expected 'full', 'rolling', or 'session')"
        )

    df = pd.DataFrame({
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })

    result = calculate_volume_profile(df, bins=bins, bin_size=bin_size)
    poc, val, vah = result["poc"], result["val"], result["vah"]

    if pd.isna(poc):
        return None

    last_close = closes[-1]

    if last_close > poc:
        price_vs_poc = "above"
    elif last_close < poc:
        price_vs_poc = "below"
    else:
        price_vs_poc = "at"

    in_value_area = val <= last_close <= vah

    return {
        "poc": poc,
        "val": val,
        "vah": vah,
        "price_vs_poc": price_vs_poc,
        "in_value_area": bool(in_value_area),
    }
