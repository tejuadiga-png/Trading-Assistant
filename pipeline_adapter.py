"""
pipeline_adapter.py

Replaces market_state_bridge.py. Instead of patching individual mock
fields, this makes regime.run_pipeline() (news filter -> regime filter
-> strategy -> conviction -> signal -> trade levels) the ONE conviction
engine, and reshapes its richer output into the exact dict shape
dashboard.py's refresh_signal_panel() already knows how to render:

    {"score", "verdict", "verdict_color", "direction", "reasons", "raw", "chop"}

Falls back to dashboard.py's original mock_market_state()-based scorer
(compute_conviction_mock, defined in dashboard.py) only if the real
pipeline raises — e.g. not enough candle history yet at market open.
"""
from __future__ import annotations
import time
import pandas as pd

import strategy
import regime
import risk
from volume_profile import calculate_volume_profile
from upstox import get_historical_nifty
from news_calendar import get_upcoming_events

_HTF_CACHE = {"daily_df": None, "h4_df": None, "fetched_at": 0.0}
HTF_CACHE_TTL_SECONDS = 300

VERDICT_COLORS = {"TRADE": "#26A69A", "WATCH": "#F59E0B", "NO TRADE": "#EF5350"}

# Human-readable labels for strategy.evaluate_setup()'s condition keys.
_LABELS = {
    "daily_trend_bullish": "Daily Trend", "daily_trend_bearish": "Daily Trend",
    "h4_trend_bullish": "4H Trend", "h4_trend_bearish": "4H Trend",
    "bullish_sweep": "Liquidity Sweep", "bearish_sweep": "Liquidity Sweep",
    "near_val": "Near Value Area Low", "near_vah": "Near Value Area High",
    "adx_strong": "ADX Regime",
    "rsi_healthy": "RSI", "rsi_weak": "RSI",
}


def _candles_to_df(raw_candles: list) -> pd.DataFrame:
    return pd.DataFrame({
        "time":   [c[0] for c in raw_candles],
        "open":   [c[1] for c in raw_candles],
        "high":   [c[2] for c in raw_candles],
        "low":    [c[3] for c in raw_candles],
        "close":  [c[4] for c in raw_candles],
        "volume": [c[5] for c in raw_candles],
    })


def _fetch_daily_and_h4():
    now = time.time()
    if _HTF_CACHE["daily_df"] is not None and now - _HTF_CACHE["fetched_at"] < HTF_CACHE_TTL_SECONDS:
        return _HTF_CACHE["daily_df"], _HTF_CACHE["h4_df"]

    daily_raw = get_historical_nifty(interval="day", days=250)
    daily_candles = list(reversed((daily_raw or {}).get("data", {}).get("candles") or []))
    daily_df = _candles_to_df(daily_candles)

    h1_raw = get_historical_nifty(interval="60minute", days=60)
    h1_candles = list(reversed((h1_raw or {}).get("data", {}).get("candles") or []))
    h1_df = _candles_to_df(h1_candles)
    h1_df["time"] = pd.to_datetime(h1_df["time"])
    h4_df = (
        h1_df.set_index("time")
        .resample("4h")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )

    if daily_df.empty or h4_df.empty:
        raise ValueError("empty daily/h4 frame from Upstox")

    _HTF_CACHE.update({"daily_df": daily_df, "h4_df": h4_df, "fetched_at": now})
    return daily_df, h4_df


def _adapt(pipeline_result: dict, ltf_df: pd.DataFrame) -> dict:
    """Reshapes regime.run_pipeline()'s output into the UI's conv dict."""
    signal = pipeline_result.get("signal", "WATCH")
    suitable = pipeline_result.get("suitable", False)
    scores = pipeline_result.get("scores") or {"long_score": 0, "short_score": 0}
    setup = pipeline_result.get("setup")

    if signal in ("STRONG BUY", "BUY"):
        direction, side_key, score = "LONG", "long", scores["long_score"]
    elif signal in ("STRONG SELL", "SELL"):
        direction, side_key, score = "SHORT", "short", scores["short_score"]
    else:
        direction, side_key = None, "long" if scores["long_score"] >= scores["short_score"] else "short"
        score = max(scores["long_score"], scores["short_score"])

    if not suitable:
        verdict = "NO TRADE"
        chop = True
    elif signal == "WATCH":
        verdict = "WATCH"
        chop = False
    else:
        verdict = "TRADE"
        chop = False

    reasons = []
    if setup:
        for key, item in setup[side_key].items():
            label = _LABELS.get(key, key)
            sub = f"{item['points']} pts"
            reasons.append((label, sub, bool(item.get("met"))))
    else:
        reasons.append(("Regime Filter", pipeline_result.get("reason") or "Trade not allowed", False))

    last_close = float(ltf_df["close"].iloc[-1])
    strength = pipeline_result.get("strength") or {}
    trade_levels = pipeline_result.get("trade_levels")

    # Pre-entry risk plan (fixed 50/100-point targets, R:R against the
    # strategy's own stop) from risk.py — only the "if you took this
    # trade" numbers. This does NOT call risk.manage_trade(): that
    # function tracks an ALREADY-OPEN position's holding period and
    # target-hit lifecycle, which needs an entry_time that nothing in
    # dashboard.py's live_state currently records. Wiring that up needs
    # a position-tracking layer (see integration notes) — out of scope
    # for this adapter, which only ever sees the current bar's signal.
    risk_plan = None
    if trade_levels:
        risk_plan = {
            **risk.calculate_targets(trade_levels["entry"], trade_levels["direction"]),
            **risk.calculate_risk_reward(trade_levels["entry"], trade_levels["stop_loss"],
                                          trade_levels["direction"]),
        }

    raw = {
        "trend_aligned": direction is not None,
        "sweep_confirmed": any(k.endswith("sweep") and v.get("met") for k, v in (setup[side_key].items() if setup else [])),
        "near_poc_or_val": "mid-range",
        "adx": strength.get("adx", 0),
        # No order-flow / FII-DII / IV-term-structure data source exists
        # anywhere in this codebase — these two are still placeholders,
        # not a regression from the merge.
        "iv_percentile": 0.5,
        "fii_net": 0.0,
    }
    if setup:
        for key in ("near_val", "near_vah"):
            if setup[side_key].get(key, {}).get("met"):
                raw["near_poc_or_val"] = "VAL" if key == "near_val" else "VAH"

    return {
        "score": round(score, 1),
        "verdict": verdict,
        "verdict_color": VERDICT_COLORS[verdict],
        "direction": direction or "-",
        "reasons": reasons,
        "raw": raw,
        "chop": chop,
        # extra fields the old dict never had — the trade calculator
        # can read these directly once Phase 4 wires it up, instead of
        # its current 0.3%-of-price placeholder math
        "trade_levels": trade_levels,
        "risk_plan": risk_plan,
        "last_close": last_close,
    }


def compute_conviction_real(symbol: str, bar_id: int, live_candles: dict,
                             capital: float | None = None, risk_percent: float | None = None) -> dict:
    """
    Single entry point for dashboard.py. Runs regime.run_pipeline() end
    to end and returns the UI-ready dict. Raises on failure (insufficient
    history, a fetch error) — caller should catch and fall back to the
    old mock-based scorer, same convention used throughout this project.
    """
    daily_df, h4_df = _fetch_daily_and_h4()

    ltf_df = pd.DataFrame({
        "high": live_candles["highs"], "low": live_candles["lows"],
        "close": live_candles["closes"], "volume": live_candles["volumes"],
    })

    liquidity = strategy.compute_liquidity_state(ltf_df)
    volume_profile = calculate_volume_profile(ltf_df, bins=30)
    news_events = get_upcoming_events()  # see news_calendar.py — static list, update it periodically

    result = regime.run_pipeline(
        daily_df, h4_df, ltf_df, liquidity, volume_profile,
        capital=capital, risk_percent=risk_percent, news_events=news_events,
    )
    return _adapt(result, ltf_df)
