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
    <=3 bars -> 20, <=5 bars -> 10, else 0). This needs the sweep's
    *age in bars*, which signals.latest_sweep_state() doesn't report
    (it only tells you if the very last bar swept). So compute_
    liquidity_state() below scans back through the full sweep history
    and reports `bars_since_sweep`. If you still pass in
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

  * VOLUME GATE — DISABLED, not deleted (see VOLUME_GATE_ENABLED below).
    A hard volume gate (zero both scores when current-bar volume falls
    below MIN_VOLUME_RATIO of its rolling average) was added during the
    mock-vs-real pipeline merge as an "institutional-grade tightening."
    It was wrong on two counts, both now documented here instead of
    silently fixed:
      1. Volume-price relationship is direction-dependent. A healthy
         uptrend often pulls back on LOW volume (contraction on the
         corrective leg is normal, not weak) while expanding on the
         impulse leg — a single "volume must be >= X% of average"
         threshold applied identically to both LONG and SHORT setups
         systematically penalizes legitimate low-volume continuation
         setups on one side or the other, depending on which phase of
         the trend you're in. Fixing this properly needs a
         direction-aware rule (e.g. volume expanding *in the direction
         of the setup*), not a single symmetric floor.
      2. The instrument this trades (Nifty 50) is a computed index, not
         a directly tradable security — whatever Upstox returns in an
         index candle's volume field may be a proxy, may be zero, or
         may not be comparable to a 20-bar rolling average the way real
         traded volume would be. This gate was only ever tested against
         synthetic random volume data; it was never verified against
         what Upstox's actual index candle response contains.
    Given both problems, VOLUME_GATE_ENABLED defaults to False:
    volume_ratio/volume_ok are still computed and returned in
    get_market_strength()'s output (so you can inspect real values
    against live Upstox data before trusting them), but compute_conviction()
    no longer zeroes scores based on them. Flip VOLUME_GATE_ENABLED to
    True only after (a) confirming Upstox's Nifty index candles carry a
    meaningful, non-zero volume field, and (b) replacing the single
    MIN_VOLUME_RATIO floor with a direction-aware check.
"""
from __future__ import annotations
import pandas as pd

from indicators import ema, rsi, atr, adx
from liquidity_sweeps import detect_liquidity_sweeps

# ---------------------------------------------------------------------
# Config — tune all thresholds/weights here, nowhere else in the file
# ---------------------------------------------------------------------

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
    "ACTIVE": 55,
}  # below ACTIVE on both sides -> WATCH

# Institutional-grade tightening (reduce trade frequency, raise setup
# quality) — added after the mock-vs-real pipeline merge:
MIN_SCORE_MARGIN = 10
# Below this gap between long_score and short_score, the two sides are
# too close to call — treat as genuinely ambiguous and force WATCH,
# rather than picking whichever is marginally higher (a near-tie is not
# a directional edge). Lowered from 15 alongside SIGNAL_THRESHOLDS["ACTIVE"]
# dropping to 55, so a 55 doesn't get vetoed just because the opposite
# side scored in the high 40s — still forces WATCH on genuine near-ties
# (e.g. 60 vs 58).

TREND_GATE_REQUIRED = True
# Hard requirement: without daily OR 4H trend alignment in a side's
# direction, that side's score is zeroed regardless of how many other
# points it accumulated. Previously a LONG could reach 55+ points from
# sweep+near_val+adx+rsi alone with ZERO trend confirmation — that is a
# coincidence of secondary signals, not a trend-following setup, and
# was the single biggest quality gap in the original scoring.

VOLUME_GATE_ENABLED = False
# DISABLED — see the "VOLUME GATE" section of the module docstring for
# why. volume_ratio/volume_ok are still computed below for visibility,
# they just aren't used to zero scores until verified against real
# Upstox index-candle data and made direction-aware.
MIN_VOLUME_RATIO = 0.8
# Current-bar volume must be at least this fraction of its
# VOLUME_LOOKBACK-bar rolling average — unused while VOLUME_GATE_ENABLED
# is False; kept as the starting point for whoever re-enables this.
VOLUME_LOOKBACK = 20

NEAR_LEVEL_SCORE_TIERS = [
    (0.0005, 15),  # within 0.05%
    (0.0015, 10),  # within 0.15%
    (0.0030, 5),   # within 0.30%
]

SWEEP_RECENCY_TIERS = [
    (3, 20),   # swept within the last 3 bars
    (5, 10),   # within 5 bars
]  # tightened from a 3-tier/10-bar decay -- a 6-10-bar-old sweep is
   # stale liquidity for a strength-following entry, not an active setup

RISK_REWARD_RATIO = 2.0      # target distance = risk distance * this multiple
STOP_ATR_MULT = 1.5          # fallback stop distance (in ATRs) when no swept_level is available
STOP_BUFFER_ATR_MULT = 0.25  # extra buffer beyond a structural swept_level, in ATRs


# ---------------------------------------------------------------------
# 1. Higher-timeframe trend (EMA position + slope)
# ---------------------------------------------------------------------

def _is_rising(series: pd.Series, lookback: int = EMA_SLOPE_LOOKBACK) -> bool:
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

    NOTE: `trend_bullish` / `trend_bearish` (the combined AND of both
    timeframes) are dashboard-display-only. evaluate_setup scores
    daily_trend_bullish and h4_trend_bullish independently with their
    own weights — it does NOT read these combined fields.
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

    volume_ratio/volume_ok are computed for visibility only — see the
    "VOLUME GATE" note in the module docstring for why they're not used
    to gate scoring right now (VOLUME_GATE_ENABLED = False).
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

    volume_ok = True
    volume_ratio = None
    if "volume" in df.columns and len(df) > VOLUME_LOOKBACK:
        avg_vol = float(df["volume"].iloc[-(VOLUME_LOOKBACK + 1):-1].mean())
        cur_vol = float(df["volume"].iloc[-1])
        if avg_vol > 0:
            volume_ratio = round(cur_vol / avg_vol, 3)
            volume_ok = volume_ratio >= MIN_VOLUME_RATIO

    return {
        "adx": round(adx_val, 2),
        "rsi": round(rsi_val, 2),
        "atr": round(atr_val, 4),
        "trend_strength": label,
        "volume_ratio": volume_ratio,   # None if no volume column / not enough history
        "volume_ok": volume_ok,         # informational only — see VOLUME_GATE_ENABLED
    }


# ---------------------------------------------------------------------
# Graduated Near-VAL / Near-VAH scoring
# ---------------------------------------------------------------------

def _distance_pct(price: float, level: float) -> float | None:
    if level in (None, 0) or price is None or pd.isna(level):
        return None
    return abs(price - level) / level


def _score_proximity(distance_pct: float | None, tiers=NEAR_LEVEL_SCORE_TIERS) -> int:
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

def compute_conviction(setup: dict, strength: dict | None = None) -> dict:
    """Sums the itemized points from evaluate_setup() into two scores:
        {"long_score": int, "short_score": int}   (each 0-100)

    Applies one hard gate on top of the raw sum (see TREND_GATE_REQUIRED
    in the config section):

      Trend gate: a side scores 0 unless its daily OR 4H trend condition
      is met — no longer possible to reach a tradeable score from
      secondary signals (sweep/near-level/adx/rsi) alone with zero trend
      confirmation.

    A second gate — zeroing both scores when strength["volume_ok"] is
    False — previously lived here too and has been DISABLED (see the
    "VOLUME GATE" section of the module docstring): it applied a single
    symmetric volume floor to both directions despite volume-price
    relationships being direction-dependent, and was never verified
    against what Upstox's Nifty 50 index candles actually report for
    volume. `strength` is still accepted here (and volume_ratio/volume_ok
    are still computed in get_market_strength()) so re-enabling it later
    — behind VOLUME_GATE_ENABLED, once verified — doesn't need a new
    function signature.
    """
    long_score = sum(item["points"] for item in setup["long"].values())
    short_score = sum(item["points"] for item in setup["short"].values())

    if TREND_GATE_REQUIRED:
        long_trend_ok = setup["long"].get("daily_trend_bullish", {}).get("met") or \
                         setup["long"].get("h4_trend_bullish", {}).get("met")
        short_trend_ok = setup["short"].get("daily_trend_bearish", {}).get("met") or \
                          setup["short"].get("h4_trend_bearish", {}).get("met")
        if not long_trend_ok:
            long_score = 0
        if not short_trend_ok:
            short_score = 0

    if VOLUME_GATE_ENABLED and strength is not None and not strength.get("volume_ok", True):
        long_score = 0
        short_score = 0

    return {"long_score": long_score, "short_score": short_score}


# ---------------------------------------------------------------------
# 5. Final signal
# ---------------------------------------------------------------------

def generate_signal(scores: dict) -> str:
    long_score = scores.get("long_score", 0)
    short_score = scores.get("short_score", 0)
    strong, active = SIGNAL_THRESHOLDS["STRONG"], SIGNAL_THRESHOLDS["ACTIVE"]

    # Ambiguity gate: if the two sides are within MIN_SCORE_MARGIN of
    # each other, this isn't a directional edge, it's noise -- force
    # WATCH rather than picking whichever is marginally higher.
    if abs(long_score - short_score) < MIN_SCORE_MARGIN:
        return "WATCH"

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
    position sizing) into one dict. Returns data only; doesn't print.

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
    scores = compute_conviction(setup, strength)
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
