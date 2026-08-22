"""
regime.py — market regime filter, sitting in front of strategy.py:

    Market Data
        v
    News Filter             <- news.check_news_regime()  (takes priority)
        v
    Regime Filter            <- classify_regime()  (TRENDING / CONSOLIDATION)
        v
    Is market suitable?      <- trade_allowed
        v
    Strategy                 <- strategy.build_dashboard()
        (Trend + Volume Profile + Liquidity Sweep)
        v
    Conviction Score
        v
    Signal

classify_market_state() is the combined entry point implementing:

    if high_impact_news:
        regime = "NEWS"
    elif trending_market:
        regime = "TRENDING"
    else:
        regime = "CONSOLIDATION"

run_pipeline() wires the whole diagram together: news check first, then
(if no blocking news) the price-action regime, and only then strategy.py.

Design notes / assumptions (flag and change as needed):

  * NEWS always takes priority. classify_market_state() checks
    news.check_news_regime() first; if it reports regime="NEWS", that's
    returned immediately and classify_regime() (price action) never
    runs at all — matches your if/elif/else priority ordering exactly.
  * BREAKING CHANGE from the previous version: classify_regime()'s
    returned dict key is now "regime" (e.g. "TRENDING"), not "label" —
    renamed so it lines up with news.py's "regime" key and a UI can
    read the same field name regardless of which filter produced it.
  * classify_market_state() always returns the same flat shape so a UI
    can destructure it identically no matter which branch fired:
        {
            "regime": "NEWS" | "TRENDING" | "CONSOLIDATION",
            "trade_allowed": bool,
            "news_impact": str | None,
            "event_name": str | None,
            "event_time": str | None,
            "minutes_to_event": float | None,
            "reason": str,
            ...plus, when regime != "NEWS", the full classify_regime()
            breakdown (trending_score, trending_conditions,
            consolidation_conditions, adx, etc.) merged in — absent
            when regime == "NEWS" since price action was never computed.
        }
  * Regime is scored on FOUR factors per side (TRENDING vs
    CONSOLIDATION), mirroring strategy.py's evaluate_setup() style —
    itemized, points-based, not a single binary check:

      TRENDING (25 pts each, max 100):
        - adx_high            : ADX >= REGIME_ADX_THRESHOLD
        - ema_structure_aligned: daily EMA20/EMA50 clearly separated AND
                                 aligned with price AND sloping (reuses
                                 strategy.get_higher_timeframe_trend()'s
                                 daily_trend_bullish/bearish flags)
        - price_directional    : net price move over REGIME_LOOKBACK bars,
                                 normalized by ATR, exceeds
                                 DIRECTIONAL_MOVE_ATR_MULT
        - htf_agreement        : Daily AND 4H trend agree (both bullish
                                 or both bearish)

      CONSOLIDATION (25 pts each, max 100):
        - adx_low              : ADX < REGIME_ADX_THRESHOLD
        - narrow_range         : (rolling high-low range) / price over
                                 REGIME_LOOKBACK bars <= NARROW_RANGE_PCT
        - ema_flat_intertwined : EMA20/EMA50 spread is tight AND neither
                                 EMA is clearly rising or falling
        - htf_disagreement     : Daily and 4H trend do NOT agree

    Higher score wins the label; confidence is "high" when the winning
    score's margin over the other is >=50, else "low".
  * classify_regime() takes both daily_df and h4_df (Daily/4H agreement
    needs both frames).
  * All thresholds (REGIME_ADX_THRESHOLD, DIRECTIONAL_MOVE_ATR_MULT,
    NARROW_RANGE_PCT, EMA_SPREAD_FLAT_PCT, and news.py's blackout
    windows) are my picks — treat as starting points to tune.
  * is_market_suitable() defaults to allowing only TRENDING, since
    strategy.py's scoring is a trend-following setup, not mean-reversion.
  * When trade isn't allowed (NEWS, or a disallowed price regime),
    run_pipeline() returns "WATCH" as the signal (reusing strategy.py's
    5-tier vocabulary) rather than a separate "NO TRADE" label.
"""
from __future__ import annotations
import pandas as pd
from datetime import datetime

from indicators import ema, atr, adx
from news import check_news_regime, NEWS_BLOCKING_IMPACTS

import strategy


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

REGIME_ADX_THRESHOLD = 20        # ADX >= this scores adx_high; below scores adx_low
REGIME_LOOKBACK = 20             # bars used for the range / directional-move checks
DIRECTIONAL_MOVE_ATR_MULT = 3.0  # net move over the lookback, in ATRs, to count as "directional"
NARROW_RANGE_PCT = 0.03          # (rolling high-low range) / price <= this counts as "narrow"
EMA_SPREAD_FLAT_PCT = 0.005      # |EMA20-EMA50| / price <= this counts as "intertwined"
EMA_FLAT_LOOKBACK = 3            # bars back used to judge EMA "flatness" (reuses strategy's slope lookback style)

REGIME_SCORE_MARGIN_FOR_HIGH_CONFIDENCE = 50  # winning score must lead by this much for "high" confidence

TRENDING_WEIGHTS = {
    "adx_high": 25,
    "ema_structure_aligned": 25,
    "price_directional": 25,
    "htf_agreement": 25,
}  # max 100

CONSOLIDATION_WEIGHTS = {
    "adx_low": 25,
    "narrow_range": 25,
    "ema_flat_intertwined": 25,
    "htf_disagreement": 25,
}  # max 100

DEFAULT_ALLOWED_REGIMES = ("TRENDING",)


# ---------------------------------------------------------------------
# Per-factor helpers
# ---------------------------------------------------------------------

def _is_flat(series: pd.Series, lookback: int = EMA_FLAT_LOOKBACK, tol_pct: float = EMA_SPREAD_FLAT_PCT) -> bool:
    """True if the series has barely moved over `lookback` bars — used
    to judge whether an EMA is sloping or sitting flat."""
    if len(series) <= lookback:
        return False
    then = series.iloc[-1 - lookback]
    if then == 0 or pd.isna(then):
        return False
    return bool(abs(series.iloc[-1] - then) / abs(then) <= tol_pct)


def _price_range_pct(df: pd.DataFrame, lookback: int = REGIME_LOOKBACK) -> float | None:
    """(rolling high - rolling low) / latest close, over the trailing
    `lookback` bars — a small value means price has stayed inside a
    narrow range."""
    if len(df) < lookback:
        return None
    window = df.iloc[-lookback:]
    close = df["close"].iloc[-1]
    if close == 0:
        return None
    return float((window["high"].max() - window["low"].min()) / close)


def _directional_move_in_atr(df: pd.DataFrame, atr_value: float, lookback: int = REGIME_LOOKBACK) -> float | None:
    """Net price change over `lookback` bars, expressed in multiples of
    ATR — large values mean price has been moving persistently in one
    direction rather than churning sideways."""
    if len(df) <= lookback or atr_value in (None, 0) or pd.isna(atr_value):
        return None
    net_move = abs(df["close"].iloc[-1] - df["close"].iloc[-1 - lookback])
    return float(net_move / atr_value)


# ---------------------------------------------------------------------
# Regime classification
# ---------------------------------------------------------------------

def compute_regime_metrics(daily_df: pd.DataFrame, h4_df: pd.DataFrame) -> dict:
    """Raw metrics behind the regime call."""
    trend = strategy.get_higher_timeframe_trend(daily_df, h4_df)

    adx_val = float(adx(daily_df["high"], daily_df["low"], daily_df["close"]).iloc[-1])
    atr_val = float(atr(daily_df["high"], daily_df["low"], daily_df["close"]).iloc[-1])

    d_ema20 = ema(daily_df["close"], 20)
    d_ema50 = ema(daily_df["close"], 50)
    ema_spread_pct = float(abs(d_ema20.iloc[-1] - d_ema50.iloc[-1]) / daily_df["close"].iloc[-1])

    range_pct = _price_range_pct(daily_df)
    directional_atr = _directional_move_in_atr(daily_df, atr_val)

    return {
        "trend": trend,
        "adx": round(adx_val, 2),
        "atr": round(atr_val, 4),
        "ema_spread_pct": round(ema_spread_pct, 5),
        "ema20_flat": _is_flat(d_ema20),
        "ema50_flat": _is_flat(d_ema50),
        "range_pct": round(range_pct, 5) if range_pct is not None else None,
        "directional_move_atr": round(directional_atr, 2) if directional_atr is not None else None,
    }


def classify_regime(daily_df: pd.DataFrame, h4_df: pd.DataFrame) -> dict:
    """
    Scores TRENDING and CONSOLIDATION independently on four factors each
    (see module docstring), and returns the higher-scoring regime.

    Returns:
        {
            "regime": "TRENDING" | "CONSOLIDATION",
            "confidence": "high" | "low",
            "reason": str,
            "trending_score": int,
            "consolidation_score": int,
            "trending_conditions": {name: {"met": bool, "points": int}, ...},
            "consolidation_conditions": {name: {"met": bool, "points": int}, ...},
            "adx": float, "atr": float, ...  # supporting metrics
        }
    """
    metrics = compute_regime_metrics(daily_df, h4_df)
    trend = metrics["trend"]
    adx_val = metrics["adx"]

    adx_high = adx_val >= REGIME_ADX_THRESHOLD
    ema_structure_aligned = bool(trend["daily_trend_bullish"] or trend["daily_trend_bearish"])
    price_directional = (metrics["directional_move_atr"] is not None
                          and metrics["directional_move_atr"] >= DIRECTIONAL_MOVE_ATR_MULT)
    htf_agreement = bool(trend["trend_bullish"] or trend["trend_bearish"])

    narrow_range = metrics["range_pct"] is not None and metrics["range_pct"] <= NARROW_RANGE_PCT
    ema_flat_intertwined = (metrics["ema_spread_pct"] <= EMA_SPREAD_FLAT_PCT
                             and metrics["ema20_flat"] and metrics["ema50_flat"])

    trending_conditions = {
        "adx_high": {"met": adx_high, "points": TRENDING_WEIGHTS["adx_high"] if adx_high else 0},
        "ema_structure_aligned": {"met": ema_structure_aligned,
                                   "points": TRENDING_WEIGHTS["ema_structure_aligned"] if ema_structure_aligned else 0},
        "price_directional": {"met": price_directional,
                               "points": TRENDING_WEIGHTS["price_directional"] if price_directional else 0},
        "htf_agreement": {"met": htf_agreement, "points": TRENDING_WEIGHTS["htf_agreement"] if htf_agreement else 0},
    }

    consolidation_conditions = {
        "adx_low": {"met": not adx_high, "points": CONSOLIDATION_WEIGHTS["adx_low"] if not adx_high else 0},
        "narrow_range": {"met": narrow_range, "points": CONSOLIDATION_WEIGHTS["narrow_range"] if narrow_range else 0},
        "ema_flat_intertwined": {"met": ema_flat_intertwined,
                                  "points": CONSOLIDATION_WEIGHTS["ema_flat_intertwined"] if ema_flat_intertwined else 0},
        "htf_disagreement": {"met": not htf_agreement,
                              "points": CONSOLIDATION_WEIGHTS["htf_disagreement"] if not htf_agreement else 0},
    }

    trending_score = sum(c["points"] for c in trending_conditions.values())
    consolidation_score = sum(c["points"] for c in consolidation_conditions.values())

    if trending_score >= consolidation_score:
        regime_label = "TRENDING"
    else:
        regime_label = "CONSOLIDATION"
    confidence = "high" if abs(trending_score - consolidation_score) >= REGIME_SCORE_MARGIN_FOR_HIGH_CONFIDENCE else "low"

    return {
        "regime": regime_label,
        "confidence": confidence,
        "reason": f"{regime_label} regime — score {trending_score} (trending) vs "
                  f"{consolidation_score} (consolidation), confidence {confidence}.",
        "trending_score": trending_score,
        "consolidation_score": consolidation_score,
        "trending_conditions": trending_conditions,
        "consolidation_conditions": consolidation_conditions,
        "adx": metrics["adx"],
        "atr": metrics["atr"],
        "ema_spread_pct": metrics["ema_spread_pct"],
        "range_pct": metrics["range_pct"],
        "directional_move_atr": metrics["directional_move_atr"],
    }


def is_market_suitable(regime: dict, allowed_regimes=DEFAULT_ALLOWED_REGIMES) -> bool:
    """True if the classified regime is one the strategy should trade in."""
    return regime.get("regime") in allowed_regimes


def classify_market_state(daily_df: pd.DataFrame, h4_df: pd.DataFrame,
                           news_events: list[dict] | None = None,
                           now: datetime | None = None,
                           allowed_regimes=DEFAULT_ALLOWED_REGIMES) -> dict:
    """
    Combined entry point implementing:
        if high_impact_news:
            regime = "NEWS"
        elif trending_market:
            regime = "TRENDING"
        else:
            regime = "CONSOLIDATION"

    Checks news first — if a blocking event is active, returns
    news.check_news_regime()'s dict as-is (regime="NEWS",
    trade_allowed=False, plus event details) and never runs the
    price-action classifier at all. Otherwise runs classify_regime()
    and merges in trade_allowed / news-context fields so the return
    shape is identical regardless of which branch fired.
    """
    news_state = check_news_regime(news_events, now)

    if news_state["regime"] == "NEWS":
        return news_state

    price_regime = classify_regime(daily_df, h4_df)
    trade_allowed = is_market_suitable(price_regime, allowed_regimes)
    reason = price_regime["reason"] + (
        "" if trade_allowed else f" Regime not in allowed_regimes {allowed_regimes} — trade not allowed."
    )

    return {
        "regime": price_regime["regime"],
        "trade_allowed": trade_allowed,
        "news_impact": news_state.get("news_impact"),
        "event_name": news_state.get("event_name"),
        "event_time": news_state.get("event_time"),
        "minutes_to_event": news_state.get("minutes_to_event"),
        "reason": reason,
        **{k: v for k, v in price_regime.items() if k not in ("regime", "reason")},
    }


# ---------------------------------------------------------------------
# Full pipeline: Market Data -> Regime Filter -> Strategy -> Signal
# ---------------------------------------------------------------------

def run_pipeline(daily_df: pd.DataFrame, h4_df: pd.DataFrame, ltf_df: pd.DataFrame,
                  liquidity: dict, volume_profile: dict,
                  capital: float | None = None, risk_percent: float | None = None,
                  news_events: list[dict] | None = None, now: datetime | None = None,
                  allowed_regimes=DEFAULT_ALLOWED_REGIMES) -> dict:
    """
    Runs the full diagram:
        Market Data -> News Filter -> Regime Filter -> Is market suitable?
        -> Strategy (Trend + Volume Profile + Liquidity Sweep -> Conviction -> Signal)

    News is checked first via classify_market_state(); a blocking event
    short-circuits everything below it (regime="NEWS", strategy.py never
    runs). Otherwise the price-action regime decides whether strategy.py's
    pipeline runs at all.
    """
    regime = classify_market_state(daily_df, h4_df, news_events=news_events, now=now,
                                    allowed_regimes=allowed_regimes)

    if not regime["trade_allowed"]:
        return {
            "regime": regime,
            "suitable": False,
            "signal": "WATCH",
            "reason": regime["reason"],
            "trend": None,
            "strength": None,
            "setup": None,
            "scores": None,
            "trade_levels": None,
            "position": None,
        }

    dashboard = strategy.build_dashboard(daily_df, h4_df, ltf_df, liquidity, volume_profile,
                                          capital=capital, risk_percent=risk_percent)

    return {
        "regime": regime,
        "suitable": True,
        "reason": None,
        **dashboard,
    }


if __name__ == "__main__":
    # Minimal smoke test with synthetic data so the file runs standalone.
    import numpy as np
    from pprint import pprint
    from datetime import timedelta

    rng = np.random.default_rng(7)

    def fake_trending_ohlc(n, start=100.0, drift=0.8, noise=0.15):
        close = start + np.cumsum(np.full(n, drift) + rng.normal(0, noise, n))
        high = close + rng.uniform(0.05, 0.2, n)
        low = close - rng.uniform(0.05, 0.2, n)
        vol = rng.uniform(1000, 5000, n)
        return pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol})

    def fake_choppy_ohlc(n, start=100.0, amplitude=1.0, noise=0.1):
        # Oscillates around a fixed level with no net drift — genuinely
        # sideways, unlike a noisy random walk (which can still trend).
        close = start + amplitude * np.sin(np.linspace(0, 15, n)) + rng.normal(0, noise, n)
        high = close + rng.uniform(0.05, 0.2, n)
        low = close - rng.uniform(0.05, 0.2, n)
        vol = rng.uniform(1000, 5000, n)
        return pd.DataFrame({"high": high, "low": low, "close": close, "volume": vol})

    from volume_profile import calculate_volume_profile

    print("--- TRENDING market, no news ---")
    daily_df = fake_trending_ohlc(150)
    h4_df = fake_trending_ohlc(150)
    ltf_df = fake_trending_ohlc(60)
    liquidity = strategy.compute_liquidity_state(ltf_df)
    vp = calculate_volume_profile(ltf_df, bins=20)
    pprint(run_pipeline(daily_df, h4_df, ltf_df, liquidity, vp, capital=100000, risk_percent=1.0))

    print("\n--- CONSOLIDATION market, no news ---")
    daily_df_chop = fake_choppy_ohlc(150)
    h4_df_chop = fake_choppy_ohlc(150)
    ltf_df_chop = fake_choppy_ohlc(60)
    liquidity_chop = strategy.compute_liquidity_state(ltf_df_chop)
    vp_chop = calculate_volume_profile(ltf_df_chop, bins=20)
    pprint(run_pipeline(daily_df_chop, h4_df_chop, ltf_df_chop, liquidity_chop, vp_chop,
                         capital=100000, risk_percent=1.0))

    print("\n--- TRENDING market, but a HIGH-impact event is 10 min away ---")
    from datetime import timezone
    now = datetime.now(timezone.utc)
    news_events = [
        {"name": "FOMC Rate Decision", "time": now + timedelta(minutes=10), "impact": "HIGH"},
        {"name": "Retail Sales", "time": now + timedelta(hours=3), "impact": "LOW"},
    ]
    pprint(run_pipeline(daily_df, h4_df, ltf_df, liquidity, vp,
                         capital=100000, risk_percent=1.0, news_events=news_events, now=now))
