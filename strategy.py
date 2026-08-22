#strategy
"""
strategy.py — the conviction-scoring pipeline:

    trend        = get_higher_timeframe_trend(daily_df, h4_df)
    strength     = get_market_strength(df)
    liquidity    = compute_liquidity_state(ltf_df)          # or signals.latest_sweep_state(...)
    setup        = evaluate_setup(trend, strength, liquidity, volume_profile, last_close)
    scores       = compute_conviction(setup)
    signal       = generate_signal(scores)
    trade_levels = compute_trade_levels(signal, last_close, atr, liquidity)
    size         = position_size(capital, risk_percent, stop_distance)

Design notes (assumptions made where your spec was ambiguous — flagged
so you can change them):

  * Trend is scored per-timeframe, never gated as one binary AND. Daily
    and 4H each carry their own weight (25 / 20 pts) in evaluate_setup,
    so a single out-of-alignment timeframe only costs its own points,
    not the whole trend category. get_higher_timeframe_trend() still
    returns a combined `trend_bullish` / `trend_bearish` field, but
    that's dashboard-display-only — nothing in scoring reads it.
  * EMA slope confirmation: "bullish" now requires EMA20 > EMA50 AND
    close > EMA20 AND EMA20 rising AND EMA50 rising (mirrored for
    bearish) — not just the EMA20 > EMA50 snapshot. "Rising/falling" is
    measured as `value now > value EMA_SLOPE_LOOKBACK bars ago`.
    EMA_SLOPE_LOOKBACK = 3 is an assumption; tune it below.
  * Bidirectional scoring: evaluate_setup scores a LONG setup and a
    SHORT setup independently (mirrored condition tables), since the
    5-tier signal (STRONG BUY/BUY/WATCH/SELL/STRONG SELL) needs both
    sides to pick a direction.
  * Near-VAL / Near-VAH is now a graduated distance score instead of a
    boolean: tighter proximity earns more points, via NEAR_LEVEL_SCORE_TIERS
    (currently <=0.05% -> 15, <=0.15% -> 10, <=0.30% -> 5, else 0). The
    exact tier cutoffs/points are my read of your example — adjust freely.
  * Sweep recency decay: a bullish/bearish sweep confirmed further back
    scores less than a fresh one, via SWEEP_RECENCY_TIERS (currently
    <=3 bars -> 20, <=6 bars -> 12, <=10 bars -> 6, else 0). This needs
    the sweep's *age in bars*, which signals.latest_sweep_state() doesn't
    report (it only tells you if the very last bar swept). So I added
    compute_liquidity_state() below, which scans back through the full
    sweep history and reports `bars_since_sweep`. If you still pass in
    signals.latest_sweep_state()'s shape (no age field), evaluate_setup
    falls back to treating it as freshest (full points) — flagged as a
    compatibility shim, not a real recency read.
  * generate_signal thresholds: score >=80 on a side -> STRONG BUY/STRONG
    SELL, 60-79 -> BUY/SELL, otherwise (both sides <60) -> WATCH.
  * compute_trade_levels: stop loss uses the swept liquidity level
    (structural stop) when available, ATR-multiple fallback otherwise.
    Target = risk distance * RISK_REWARD_RATIO (not volume-profile-based).
  * position_size(capital, risk_percent, stop_distance): risk_percent is
    a plain percentage, e.g. 1.0 means "risk 1% of capital", not 0.01.
  * trend_strength label thresholds (STRONG/MODERATE/WEAK) are ADX-based
    guesses: >25 STRONG, 20-25 MODERATE, <20 WEAK.
  * volume_profile.py's dicts don't carry a close price, so evaluate_setup
    takes `last_close` as its own argument for the Near-VAL/VAH checks.
  * _print_dashboard() has been removed per your request — build_dashboard()
    now only returns data, it doesn't print anything.
"""
from __future__ import annotations
import pandas as pd

from indicators import ema, rsi, atr, adx
from liquidity_sweeps import detect_liquidity_sweeps

# ---------------------------------------------------------------------
# Config — tune all thresholds/weights here, nowhere else in the file
# ---------------------------------------------------------------------

# Flat-weight conditions only. Near-VAL/VAH and sweep points are graduated
# (see NEAR_LEVEL_SCORE_TIERS / SWEEP_RECENCY_TIERS) — their max points
# (15 and 20) still add up to the same 100-point ceiling.
WEIGHTS_LONG = {
    "daily_trend_bullish": 25,
    "h4_trend_bullish": 20,
    "adx_strong": 10,
    "rsi_healthy": 10,
}  # + up to 20 (sweep) + up to 15 (near_val) = 100 max

WEIGHTS_SHORT = {
    "daily_trend_bearish": 25,
    "h4_trend_bearish": 20,
    "adx_strong": 10,
    "rsi_weak": 10,
}  # + up to 20 (sweep) + up to 15 (near_vah) = 100 max — mirror of WEIGHTS_LONG

ADX_STRONG_THRESHOLD = 25
RSI_HEALTHY_RANGE = (50, 70)   # long-side "healthy" RSI band
RSI_SHORT_RANGE = (30, 50)     # short-side "healthy" RSI band — assumption

EMA_SLOPE_LOOKBACK = 3  # bars back to compare against for "rising"/"falling" — assumption

TREND_STRENGTH_BANDS = {
    "STRONG": 25,
    "MODERATE": 20,
}

SIGNAL_THRESHOLDS = {
    "STRONG": 80,
    "ACTIVE": 60,
}  # below ACTIVE on both sides -> WATCH

# Distance tiers for Near-VAL / Near-VAH, tightest first. Each tuple is
# (max_distance_pct_as_fraction, points). First tier whose threshold the
# actual distance falls within wins; beyond the last tier -> 0 points.
NEAR_LEVEL_SCORE_TIERS = [
    (0.0005, 15),  # within 0.05%
    (0.0015, 10),  # within 0.15%
    (0.0030, 5),   # within 0.30%
]

# Recency tiers for a confirmed sweep, freshest first. Each tuple is
# (max_age_in_bars, points). Beyond the last tier -> 0 points (too stale
# to count as a live liquidity grab).
SWEEP_RECENCY_TIERS = [
    (3, 20),   # swept within the last 3 bars
    (6, 12),   # within 6 bars
    (10, 6),   # within 10 bars
]

RISK_REWARD_RATIO = 2.0      # target distance = risk distance * this multiple
STOP_ATR_MULT = 1.5          # fallback stop distance (in ATRs) when no swept_level is available
STOP_BUFFER_ATR_MULT = 0.25  # extra buffer beyond a structural swept_level, in ATRs


# ---------------------------------------------------------------------
# 1. Higher-timeframe trend (EMA position + slope)
# ---------------------------------------------------------------------

def _is_rising(series: pd.Series, lookback: int = EMA_SLOPE_LOOKBACK) -> bool:
    """True if the series' latest value is higher than it was `lookback`
    bars ago. Used to confirm an EMA is actually sloping up, not just
    sitting above another EMA."""
    if len(series) <= lookback:
        return False
    return bool(series.iloc[-1] > series.iloc[-1 - lookback])


def _is_falling(series: pd.Series, lookback: int = EMA_SLOPE_LOOKBACK) -> bool:
    if len(series) <= lookback:
        return False
    return bool(series.iloc[-1] < series.iloc[-1 - lookback])


def get_higher_timeframe_trend(daily_df: pd.DataFrame, h4_df: pd.DataFrame) -> dict:
    """
    Bullish conditions (per timeframe):
        EMA20 > EMA50 AND Close > EMA20 AND EMA20 rising AND EMA50 rising
    Bearish conditions mirror this with < and falling.

    "Rising"/"falling" replaces a plain EMA20 > EMA50 snapshot check —
    both EMAs must actually be sloping in the trend's direction, not
    just positioned correctly at this instant.

    Returns a dict meant to be displayed on the dashboard as-is, with
    both the raw values and the booleans derived from them.

    NOTE: `trend_bullish` / `trend_bearish` (the combined AND of both
    timeframes) are dashboard-display-only. evaluate_setup scores
    daily_trend_bullish and h4_trend_bullish independently with their
    own weights — it does NOT read these combined fields — so a single
    out-of-alignment timeframe only costs its own points.
    """
    d_close = daily_df["close"]
    h_close = h4_df["close"]

    d_ema20, d_ema50 = ema(d_close, 20), ema(d_close, 50)
    h_ema20, h_ema50 = ema(h_close, 20), ema(h_close, 50)

    daily_bullish = bool(
        d_ema20.iloc[-1] > d_ema50.iloc[-1]
        and d_close.iloc[-1] > d_ema20.iloc[-1]
        and _is_rising(d_ema20) and _is_rising(d_ema50)
    )
    daily_bearish = bool(
        d_ema20.iloc[-1] < d_ema50.iloc[-1]
        and d_close.iloc[-1] < d_ema20.iloc[-1]
        and _is_falling(d_ema20) and _is_falling(d_ema50)
    )
    h4_bullish = bool(
        h_ema20.iloc[-1] > h_ema50.iloc[-1]
        and h_close.iloc[-1] > h_ema20.iloc[-1]
        and _is_rising(h_ema20) and _is_rising(h_ema50)
    )
    h4_bearish = bool(
        h_ema20.iloc[-1] < h_ema50.iloc[-1]
        and h_close.iloc[-1] < h_ema20.iloc[-1]
        and _is_falling(h_ema20) and _is_falling(h_ema50)
    )

    return {
        "daily_ema20": float(d_ema20.iloc[-1]),
        "daily_ema50": float(d_ema50.iloc[-1]),
        "daily_close": float(d_close.iloc[-1]),
        "daily_ema20_rising": _is_rising(d_ema20),
        "daily_ema50_rising": _is_rising(d_ema50),
        "daily_trend_bullish": daily_bullish,
        "daily_trend_bearish": daily_bearish,
        "h4_ema20": float(h_ema20.iloc[-1]),
        "h4_ema50": float(h_ema50.iloc[-1]),
        "h4_close": float(h_close.iloc[-1]),
        "h4_ema20_rising": _is_rising(h_ema20),
        "h4_ema50_rising": _is_rising(h_ema50),
        "h4_trend_bullish": h4_bullish,
        "h4_trend_bearish": h4_bearish,
        # Dashboard-display-only combined view — NOT used in scoring:
        "trend_bullish": daily_bullish and h4_bullish,
        "trend_bearish": daily_bearish and h4_bearish,
    }


# ---------------------------------------------------------------------
# 2. Market strength
# ---------------------------------------------------------------------

def get_market_strength(df: pd.DataFrame) -> dict:
    """
    Calculates ADX, RSI, ATR off the given dataframe (needs high/low/close)
    and returns e.g. {"adx": 27, "rsi": 58, "atr": 12.4, "trend_strength": "STRONG"}
    """
    adx_val = float(adx(df["high"], df["low"], df["close"]).iloc[-1])
    rsi_val = float(rsi(df["close"]).iloc[-1])
    atr_val = float(atr(df["high"], df["low"], df["close"]).iloc[-1])

    if adx_val > TREND_STRENGTH_BANDS["STRONG"]:
        label = "STRONG"
    elif adx_val > TREND_STRENGTH_BANDS["MODERATE"]:
        label = "MODERATE"
    else:
        label = "WEAK"

    return {
        "adx": round(adx_val, 2),
        "rsi": round(rsi_val, 2),
        "atr": round(atr_val, 4),
        "trend_strength": label,
    }


# ---------------------------------------------------------------------
# Graduated Near-VAL / Near-VAH scoring
# ---------------------------------------------------------------------

def _distance_pct(price: float, level: float) -> float | None:
    if level in (None, 0) or price is None or pd.isna(level):
        return None
    return abs(price - level) / level


def _score_proximity(distance_pct: float | None, tiers=NEAR_LEVEL_SCORE_TIERS) -> int:
    """Graduated score: the first tier whose threshold the distance falls
    within wins (tiers must be sorted tightest-first). Beyond every tier
    -> 0."""
    if distance_pct is None:
        return 0
    for max_distance, points in tiers:
        if distance_pct <= max_distance:
            return points
    return 0


def _score_near_val(volume_profile: dict, last_close: float) -> dict:
    distance_pct = _distance_pct(last_close, volume_profile.get("val"))
    return {"points": _score_proximity(distance_pct), "distance_pct": distance_pct}


def _score_near_vah(volume_profile: dict, last_close: float) -> dict:
    distance_pct = _distance_pct(last_close, volume_profile.get("vah"))
    return {"points": _score_proximity(distance_pct), "distance_pct": distance_pct}


# ---------------------------------------------------------------------
# Sweep recency scoring + liquidity state (with age) helper
# ---------------------------------------------------------------------

def compute_liquidity_state(df: pd.DataFrame, left_bars: int = 3, right_bars: int = 3) -> dict:
    """
    Recency-aware alternative to signals.latest_sweep_state(): scans the
    full sweep history for the most recent bullish/bearish sweep bar and
    reports how many bars old it is, so sweep points can decay with age
    instead of being all-or-nothing.

    Returns:
        {
            "sweep_confirmed": bool,
            "direction": "bullish" | "bearish" | None,
            "swept_level": float | None,
            "bars_since_sweep": int | None,   # 0 = the most recent bar
        }
    """
    out = detect_liquidity_sweeps(df, left_bars, right_bars)
    if out.empty:
        return {"sweep_confirmed": False, "direction": None, "swept_level": None, "bars_since_sweep": None}

    last_idx = out.index[-1]
    bull_idx = out.index[out["bullish_sweep"]]
    bear_idx = out.index[out["bearish_sweep"]]

    latest_bull = bull_idx[-1] if len(bull_idx) else None
    latest_bear = bear_idx[-1] if len(bear_idx) else None

    if latest_bull is None and latest_bear is None:
        return {"sweep_confirmed": False, "direction": None, "swept_level": None, "bars_since_sweep": None}

    # Pick whichever sweep happened most recently.
    if latest_bull is not None and (latest_bear is None or latest_bull >= latest_bear):
        idx, direction = latest_bull, "bullish"
    else:
        idx, direction = latest_bear, "bearish"

    return {
        "sweep_confirmed": True,
        "direction": direction,
        "swept_level": float(out.loc[idx, "swept_level"]),
        "bars_since_sweep": int(last_idx - idx),
    }


def _score_sweep_recency(liquidity: dict, expected_direction: str, tiers=SWEEP_RECENCY_TIERS) -> dict:
    """
    Scores a confirmed sweep in `expected_direction` ("bullish" or
    "bearish") by how many bars old it is. If `liquidity` doesn't carry
    a `bars_since_sweep` field (e.g. it came from signals.latest_sweep_state(),
    which only reports the current bar), this falls back to treating a
    confirmed sweep as maximally fresh — a compatibility shim, not a
    real recency read.
    """
    confirmed = bool(liquidity.get("sweep_confirmed")) and liquidity.get("direction") == expected_direction
    if not confirmed:
        return {"points": 0, "bars_since_sweep": None}

    age = liquidity.get("bars_since_sweep")
    if age is None:
        return {"points": tiers[0][1], "bars_since_sweep": None}  # fallback: assume freshest

    for max_age, points in tiers:
        if age <= max_age:
            return {"points": points, "bars_since_sweep": age}
    return {"points": 0, "bars_since_sweep": age}


# ---------------------------------------------------------------------
# 3. Entry filter / itemized scoring (long AND short, independently)
# ---------------------------------------------------------------------

def evaluate_setup(trend: dict, strength: dict, liquidity: dict, volume_profile: dict,
                    last_close: float) -> dict:
    """
    Scores a LONG setup and a SHORT setup independently and returns:
        {
            "long":  {condition_name: {"met": bool, "points": int, ...}, ...},
            "short": {condition_name: {"met": bool, "points": int, ...}, ...},
        }

    Long scoring:
        Daily Trend Bullish (EMA20>EMA50, Close>EMA20, both rising)  +25
        4H Trend Bullish (same, 4H)                                  +20
        Bullish Sweep, decayed by recency                            up to +20
        Near VAL, decayed by distance                                up to +15
        ADX > 25                                                     +10
        RSI between 50-70                                            +10

    Short scoring mirrors this: Daily/4H Trend Bearish, Bearish Sweep,
    Near VAH, ADX > 25, RSI between 30-50.
    """
    rsi_val = strength.get("rsi", 0)
    adx_strong = strength.get("adx", 0) > ADX_STRONG_THRESHOLD

    long_lo, long_hi = RSI_HEALTHY_RANGE
    short_lo, short_hi = RSI_SHORT_RANGE

    bullish_sweep = _score_sweep_recency(liquidity, "bullish")
    bearish_sweep = _score_sweep_recency(liquidity, "bearish")
    near_val = _score_near_val(volume_profile, last_close)
    near_vah = _score_near_vah(volume_profile, last_close)

    long_setup = {
        "daily_trend_bullish": {"met": bool(trend.get("daily_trend_bullish")),
                                 "points": WEIGHTS_LONG["daily_trend_bullish"] if trend.get("daily_trend_bullish") else 0},
        "h4_trend_bullish": {"met": bool(trend.get("h4_trend_bullish")),
                              "points": WEIGHTS_LONG["h4_trend_bullish"] if trend.get("h4_trend_bullish") else 0},
        "bullish_sweep": {"met": bullish_sweep["points"] > 0, **bullish_sweep},
        "near_val": {"met": near_val["points"] > 0, **near_val},
        "adx_strong": {"met": adx_strong, "points": WEIGHTS_LONG["adx_strong"] if adx_strong else 0},
        "rsi_healthy": {"met": long_lo <= rsi_val <= long_hi,
                         "points": WEIGHTS_LONG["rsi_healthy"] if long_lo <= rsi_val <= long_hi else 0},
    }

    short_setup = {
        "daily_trend_bearish": {"met": bool(trend.get("daily_trend_bearish")),
                                 "points": WEIGHTS_SHORT["daily_trend_bearish"] if trend.get("daily_trend_bearish") else 0},
        "h4_trend_bearish": {"met": bool(trend.get("h4_trend_bearish")),
                              "points": WEIGHTS_SHORT["h4_trend_bearish"] if trend.get("h4_trend_bearish") else 0},
        "bearish_sweep": {"met": bearish_sweep["points"] > 0, **bearish_sweep},
        "near_vah": {"met": near_vah["points"] > 0, **near_vah},
        "adx_strong": {"met": adx_strong, "points": WEIGHTS_SHORT["adx_strong"] if adx_strong else 0},
        "rsi_weak": {"met": short_lo <= rsi_val <= short_hi,
                      "points": WEIGHTS_SHORT["rsi_weak"] if short_lo <= rsi_val <= short_hi else 0},
    }

    return {"long": long_setup, "short": short_setup}


# ---------------------------------------------------------------------
# 4. Conviction scoring
# ---------------------------------------------------------------------

def compute_conviction(setup: dict) -> dict:
    """Sums the itemized points from evaluate_setup() into two scores:
        {"long_score": int, "short_score": int}   (each 0-100)
    """
    return {
        "long_score": sum(item["points"] for item in setup["long"].values()),
        "short_score": sum(item["points"] for item in setup["short"].values()),
    }


# ---------------------------------------------------------------------
# 5. Final signal
# ---------------------------------------------------------------------

def generate_signal(scores: dict) -> str:
    """
    Picks the dominant side (higher of long_score / short_score) and
    maps it to a 5-tier signal:
        score >= 80  -> STRONG BUY  / STRONG SELL
        score 60-79  -> BUY         / SELL
        below 60 on both sides -> WATCH
    """
    long_score = scores.get("long_score", 0)
    short_score = scores.get("short_score", 0)
    strong, active = SIGNAL_THRESHOLDS["STRONG"], SIGNAL_THRESHOLDS["ACTIVE"]

    if long_score >= short_score:
        if long_score >= strong:
            return "STRONG BUY"
        if long_score >= active:
            return "BUY"
    else:
        if short_score >= strong:
            return "STRONG SELL"
        if short_score >= active:
            return "SELL"
    return "WATCH"


# ---------------------------------------------------------------------
# 6. Trade levels — entry / stop_loss / target / risk_reward
# ---------------------------------------------------------------------

def compute_trade_levels(signal: str, last_close: float, atr_value: float, liquidity: dict,
                          risk_reward_ratio: float = RISK_REWARD_RATIO) -> dict | None:
    """
    Returns {"direction": "long"|"short", "entry": float, "stop_loss": float,
    "target": float, "risk_reward": float}, or None when the signal is
    WATCH (no trade to size).
    """
    if signal not in ("STRONG BUY", "BUY", "STRONG SELL", "SELL"):
        return None

    direction = "long" if signal in ("STRONG BUY", "BUY") else "short"
    swept_level = liquidity.get("swept_level")
    entry = last_close

    if direction == "long":
        if swept_level is not None and swept_level < entry:
            stop_loss = swept_level - STOP_BUFFER_ATR_MULT * atr_value
        else:
            stop_loss = entry - STOP_ATR_MULT * atr_value
        risk = entry - stop_loss
        if risk <= 0:
            return None
        target = entry + risk * risk_reward_ratio
    else:
        if swept_level is not None and swept_level > entry:
            stop_loss = swept_level + STOP_BUFFER_ATR_MULT * atr_value
        else:
            stop_loss = entry + STOP_ATR_MULT * atr_value
        risk = stop_loss - entry
        if risk <= 0:
            return None
        target = entry - risk * risk_reward_ratio

    reward = abs(target - entry)
    return {
        "direction": direction,
        "entry": round(entry, 4),
        "stop_loss": round(stop_loss, 4),
        "target": round(target, 4),
        "risk_reward": round(reward / risk, 2),
    }


# ---------------------------------------------------------------------
# 7. Position sizing
# ---------------------------------------------------------------------

def position_size(capital: float, risk_percent: float, stop_distance: float) -> dict:
    """
    capital       : account size, e.g. 100000
    risk_percent  : plain percentage, e.g. 1.0 means "risk 1% of capital"
                    (not 0.01 — this function does the /100 conversion)
    stop_distance : entry-to-stop distance in price terms (e.g. entry - stop_loss)

    Returns {"risk_amount": float, "position_size": float, "stop_distance": float}.
    position_size is in units of the underlying (shares/contracts/lots) —
    divide further by lot size if your instrument trades in fixed lots.
    """
    if stop_distance <= 0:
        raise ValueError("stop_distance must be positive")
    if capital <= 0:
        raise ValueError("capital must be positive")

    risk_amount = capital * (risk_percent / 100)
    size = risk_amount / stop_distance

    return {
        "risk_amount": round(risk_amount, 2),
        "position_size": round(size, 4),
        "stop_distance": round(stop_distance, 4),
    }


# ---------------------------------------------------------------------
# Dashboard — runs the full pipeline and returns a summary dict
# ---------------------------------------------------------------------

def build_dashboard(daily_df: pd.DataFrame, h4_df: pd.DataFrame, ltf_df: pd.DataFrame,
                     liquidity: dict, volume_profile: dict,
                     capital: float | None = None, risk_percent: float | None = None) -> dict:
    """
    Runs the entire pipeline once and collects every intermediate output
    (trend, strength, setup, scores, signal, trade_levels, and optionally
    position sizing) into one dict — the "output/dashboard" surface
    referenced in your spec for `trend`. Returns data only; it doesn't
    print anything (see module docstring re: _print_dashboard removal).

    Parameters
    ----------
    daily_df, h4_df : OHLC dataframes for trend detection
    ltf_df          : lower-timeframe df (used for market strength; needs
                      high/low/close columns)
    liquidity       : dict shaped like compute_liquidity_state()'s or
                      signals.latest_sweep_state()'s return value
    volume_profile  : dict shaped like volume_profile.calculate_volume_profile()'s
                      or volume_profile.latest_volume_profile_state()'s
                      return value (poc/val/vah/...)
    capital, risk_percent : optional — if both given and a trade signal
                      fires, position_size() is computed and attached
                      under dashboard["position"]
    """
    trend = get_higher_timeframe_trend(daily_df, h4_df)
    strength = get_market_strength(ltf_df)
    last_close = float(ltf_df["close"].iloc[-1])
    setup = evaluate_setup(trend, strength, liquidity, volume_profile, last_close)
    scores = compute_conviction(setup)
    signal = generate_signal(scores)
    trade_levels = compute_trade_levels(signal, last_close, strength["atr"], liquidity)

    position = None
    if trade_levels and capital is not None and risk_percent is not None:
        stop_distance = abs(trade_levels["entry"] - trade_levels["stop_loss"])
        position = position_size(capital, risk_percent, stop_distance)

    return {
        "trend": trend,
        "strength": strength,
        "setup": setup,
        "scores": scores,
        "signal": signal,
        "trade_levels": trade_levels,
        "position": position,
    }


if __name__ == "__main__":
    # Minimal smoke test with synthetic data so the file runs standalone.
    import numpy as np
    from pprint import pprint

    rng = np.random.default_rng(42)

    def fake_ohlc(n, start=100.0, drift=0.15):
        close = start + np.cumsum(rng.normal(drift, 1.0, n))
        high = close + rng.uniform(0.1, 1.0, n)
        low = close - rng.uniform(0.1, 1.0, n)
        vol = rng.uniform(1000, 5000, n)
        return pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol})

    daily_df = fake_ohlc(80)
    h4_df = fake_ohlc(120)
    ltf_df = fake_ohlc(60)

    fake_liquidity = compute_liquidity_state(ltf_df)

    from volume_profile import calculate_volume_profile
    fake_vp = calculate_volume_profile(ltf_df, bins=20)

    dashboard = build_dashboard(daily_df, h4_df, ltf_df, fake_liquidity, fake_vp,
                                 capital=100000, risk_percent=1.0)
    pprint(dashboard)
