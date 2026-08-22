"""
options_risk.py — Phase 4: makes risk management actually reflect
OPTION BUYING on limited capital, instead of trading the underlying.

Three gaps this closes:
  1. strategy.py/risk.py size stops/targets in NIFTY POINTS on the
     underlying. Option premium doesn't move 1:1 with the underlying
     (delta/gamma/theta/vega), so a "50-point target" is meaningless
     for an actual option P&L. -> premium_risk_levels()
  2. risk.py hardcodes LOTS = 2 regardless of account size. For a
     capital-constrained buyer, max loss = premium x lot_size x lots,
     which is KNOWN at entry -> size FROM that, not a fixed lot count.
     -> capital_aware_lots()
  3. Nothing picks WHICH strike to buy. -> pick_strike()

Also included: a rough transaction-cost estimate (STT + exchange +
GST + SEBI fees), since none of the R:R math anywhere in this project
nets these out, and they matter more the smaller your capital is.
"""
from __future__ import annotations
import math

# ---------------------------------------------------------------------
# Config — starting points, tune freely
# ---------------------------------------------------------------------

# Premium-based exit, not index-points-based. Chosen so a losing trade
# is cut well before theta/gamma can turn a 35% premium loss into a
# total wipeout into expiry.
STOP_LOSS_PREMIUM_PCT = 0.35   # exit if premium falls 35% from entry
TARGET_PREMIUM_PCT = 0.70      # take-profit at +70% premium (~1:2 R:R)

# Conviction-scaled targets: a STRONG BUY/SELL signal (score >= 80 in
# strategy.py's SIGNAL_THRESHOLDS) can afford to let a winner run
# further than a marginal BUY/SELL (score 60-79).
TARGET_PREMIUM_PCT_BY_SIGNAL = {
    "STRONG BUY": 1.00, "STRONG SELL": 1.00,   # +100%
    "BUY": 0.70, "SELL": 0.70,                  # +70%
}

# Strike selection: delta ~0.4-0.6 balances cost vs. probability of
# profit for a buyer. Lower delta (further OTM) is cheaper but lower
# probability; used automatically for STRONG signals where a bigger
# payoff is being targeted for less premium outlay.
TARGET_DELTA_DEFAULT = 0.50
TARGET_DELTA_STRONG = 0.40   # slightly OTM for STRONG BUY/SELL signals

# Rough Indian options transaction cost model (buyer side, per lot,
# INDICATIVE ONLY — verify against your actual broker's charge slip;
# STT/exchange fees are revised periodically by SEBI/NSE).
STT_SELL_SIDE_PCT = 0.001       # ~0.1% of premium, charged on sell/exit leg only for buyers
EXCHANGE_TXN_PCT = 0.0003503    # NSE F&O options exchange transaction charge (approx, both legs)
SEBI_FEE_PCT = 0.0000010        # negligible but included for completeness
GST_PCT = 0.18                  # on (brokerage + exchange txn charges)
FLAT_BROKERAGE_PER_ORDER = 20.0  # typical discount-broker flat fee per executed order


# ---------------------------------------------------------------------
# 1. Premium-based stop/target (replaces index-point-based levels)
# ---------------------------------------------------------------------

def premium_risk_levels(entry_premium: float, signal: str = "BUY",
                         stop_pct: float = STOP_LOSS_PREMIUM_PCT) -> dict:
    """
    Returns stop-loss and target premium PRICES (not index points),
    scaled by signal strength. This is what should actually gate an
    option-buying exit — not a fixed 50/100 Nifty-point move.

    Returns:
        {
            "entry_premium": float,
            "stop_loss_premium": float,   # exit here, floors at 0
            "target_premium": float,
            "stop_pct": float, "target_pct": float,
            "risk_reward": float,
        }
    """
    if entry_premium <= 0:
        raise ValueError("entry_premium must be positive")

    target_pct = TARGET_PREMIUM_PCT_BY_SIGNAL.get(signal, TARGET_PREMIUM_PCT)

    stop_loss_premium = max(0.0, entry_premium * (1 - stop_pct))
    target_premium = entry_premium * (1 + target_pct)

    risk = entry_premium - stop_loss_premium
    reward = target_premium - entry_premium

    return {
        "entry_premium": round(entry_premium, 2),
        "stop_loss_premium": round(stop_loss_premium, 2),
        "target_premium": round(target_premium, 2),
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "risk_reward": round(reward / risk, 2) if risk > 0 else None,
    }


# ---------------------------------------------------------------------
# 2. Capital-aware lot sizing (replaces risk.py's fixed LOTS = 2)
# ---------------------------------------------------------------------

def capital_aware_lots(capital: float, risk_percent: float, entry_premium: float,
                        stop_pct: float = STOP_LOSS_PREMIUM_PCT,
                        lot_size: int = 65, max_lots: int | None = None) -> dict:
    """
    For BUYING options, sizes lots against the ACTUAL planned loss if
    the stop-loss is hit (entry_premium x stop_pct), not a full
    wipeout-to-zero worst case — the strategy always exits at the
    premium stop (see premium_risk_levels()), so sizing against 100%
    loss is needlessly conservative and would strand most retail
    capital at 0 lots even for perfectly tradeable setups. This is
    still the correct defined-risk logic (unlike risk.py's flat
    LOTS = 2 regardless of account size) — it just uses the real risk,
    not the theoretical max.

    Note: if the stop-loss order itself gets skipped or slips badly
    (e.g. a gap through the stop, or a manual override), actual loss
    CAN still reach the full premium — this sizing assumes the stop is
    honored, it doesn't eliminate tail risk.

    capital       : account size, e.g. 50000
    risk_percent  : e.g. 2.0 means "risk up to 2% of capital on this trade"
    entry_premium : option premium per unit (not per lot)
    stop_pct      : planned stop-loss as a fraction of premium (0.35 = -35%)
    lot_size      : 65 for Nifty as of the Jan 2026 revision — pass
                    explicitly rather than trusting a hardcoded default,
                    since NSE revises this periodically
    max_lots      : optional hard cap regardless of what capital allows

    Returns:
        {
            "lots": int,                # 0 if even 1 lot exceeds the risk budget
            "risk_amount": float,       # capital x risk_percent/100
            "risk_per_lot": float,      # actual $ lost per lot if stopped out
            "premium_per_lot": float,   # full outlay per lot (capital required, not risk)
            "max_loss_at_lots": float,  # risk_per_lot x lots, if stop is honored
            "capital_required": float,  # premium_per_lot x lots — must actually have this much free
        }
    """
    if capital <= 0 or risk_percent <= 0 or entry_premium <= 0 or lot_size <= 0:
        raise ValueError("capital, risk_percent, entry_premium, lot_size must all be positive")
    if not (0 < stop_pct <= 1):
        raise ValueError("stop_pct must be between 0 and 1")

    risk_amount = capital * (risk_percent / 100)
    premium_per_lot = entry_premium * lot_size
    risk_per_lot = premium_per_lot * stop_pct

    lots = math.floor(risk_amount / risk_per_lot)
    if max_lots is not None:
        lots = min(lots, max_lots)
    lots = max(lots, 0)

    return {
        "lots": lots,
        "risk_amount": round(risk_amount, 2),
        "risk_per_lot": round(risk_per_lot, 2),
        "premium_per_lot": round(premium_per_lot, 2),
        "max_loss_at_lots": round(risk_per_lot * lots, 2),
        "capital_required": round(premium_per_lot * lots, 2),
    }


# ---------------------------------------------------------------------
# 3. Strike selection by target delta
# ---------------------------------------------------------------------

def pick_strike(chain_rows: list, direction: str, signal: str = "BUY",
                 target_delta: float | None = None) -> dict | None:
    """
    Picks the strike closest to a target delta from dashboard.py's
    chain_rows (state["chain_rows"], the shape make_option_chain()
    returns: each row has strike, call_ltp/call_delta, put_ltp/put_delta).

    direction: "LONG" -> buy a call; "SHORT" -> buy a put.
    signal: passed through to pick a slightly-OTM target delta for
            STRONG BUY/SELL signals (cheaper premium, bigger % payoff),
            vs. a closer-to-ATM delta for a plain BUY/SELL.

    Returns the matching chain row (with an added "side": "CE"|"PE"
    and "premium": the relevant ltp) or None if chain_rows is empty.
    """
    if not chain_rows:
        return None

    if target_delta is None:
        target_delta = TARGET_DELTA_STRONG if signal in ("STRONG BUY", "STRONG SELL") else TARGET_DELTA_DEFAULT

    side = "CE" if direction == "LONG" else "PE"
    delta_key = "call_delta" if side == "CE" else "put_delta"
    ltp_key = "call_ltp" if side == "CE" else "put_ltp"

    # Calls have positive delta, puts negative in most feeds — compare
    # on magnitude so target_delta stays a plain positive number for
    # both sides.
    best = min(chain_rows, key=lambda r: abs(abs(r.get(delta_key, 0)) - target_delta))
    return {**best, "side": side, "premium": best.get(ltp_key, 0.0)}


# ---------------------------------------------------------------------
# 4. Rough transaction cost estimate (buyer side, per lot)
# ---------------------------------------------------------------------

def estimate_round_trip_cost(entry_premium: float, exit_premium: float,
                              lot_size: int, lots: int) -> dict:
    """
    INDICATIVE ONLY — a rough estimate of STT + exchange txn charges +
    GST + flat brokerage for a full buy-then-sell round trip, so the
    signal engine's expected-value math isn't implicitly assuming
    frictionless trades. Verify against your actual broker's charges
    before relying on this for sizing decisions; SEBI/NSE revise these
    fee schedules periodically.

    STT is charged on the SELL leg only for option buyers (on premium
    turnover); exchange txn charges + GST apply to both legs.
    """
    if lot_size <= 0 or lots <= 0:
        raise ValueError("lot_size and lots must be positive")

    units = lot_size * lots
    buy_turnover = entry_premium * units
    sell_turnover = exit_premium * units

    stt = sell_turnover * STT_SELL_SIDE_PCT
    exchange_txn = (buy_turnover + sell_turnover) * EXCHANGE_TXN_PCT
    sebi_fee = (buy_turnover + sell_turnover) * SEBI_FEE_PCT
    brokerage = FLAT_BROKERAGE_PER_ORDER * 2  # one order each side
    gst = (brokerage + exchange_txn) * GST_PCT

    total = stt + exchange_txn + sebi_fee + brokerage + gst

    return {
        "stt": round(stt, 2),
        "exchange_txn": round(exchange_txn, 2),
        "sebi_fee": round(sebi_fee, 2),
        "brokerage": round(brokerage, 2),
        "gst": round(gst, 2),
        "total_cost": round(total, 2),
    }


# ---------------------------------------------------------------------
# 5. Combined trade plan — what dashboard.py's trade calculator should call
# ---------------------------------------------------------------------

def build_option_trade_plan(direction: str, signal: str, chain_rows: list,
                             capital: float, risk_percent: float,
                             lot_size: int = 65) -> dict | None:
    """
    One call that ties strike selection + premium-based risk levels +
    capital-aware sizing + cost estimate together — this is what
    dashboard.py's do_calc() should call instead of its current
    0.3%-of-index-price placeholder math.

    Returns None if no strike could be selected (empty chain) or if
    capital can't cover even 1 lot within the risk budget.
    """
    strike_row = pick_strike(chain_rows, direction, signal)
    if not strike_row or not strike_row.get("premium"):
        return None

    entry_premium = strike_row["premium"]
    levels = premium_risk_levels(entry_premium, signal)
    sizing = capital_aware_lots(capital, risk_percent, entry_premium,
                                 stop_pct=levels["stop_pct"], lot_size=lot_size)

    if sizing["lots"] < 1:
        return {
            "strike": strike_row["strike"], "side": strike_row["side"],
            "entry_premium": entry_premium, "levels": levels, "sizing": sizing,
            "cost_estimate": None,
            "warning": "Capital/risk budget too small for even 1 lot at this premium.",
        }

    cost_estimate = estimate_round_trip_cost(
        entry_premium, levels["target_premium"], lot_size, sizing["lots"]
    )

    return {
        "strike": strike_row["strike"],
        "side": strike_row["side"],
        "entry_premium": entry_premium,
        "levels": levels,
        "sizing": sizing,
        "cost_estimate": cost_estimate,
        "warning": None,
    }
