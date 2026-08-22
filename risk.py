"""
risk.py — trade risk management.

Handles:
    - Fixed 2-lot position
    - Target 1 = 50 points
    - Target 2 = 100 points
    - Maximum holding period = 3 trading days
    - Theta-decay exit warning
    - Basic risk/reward calculations

The strategy decides WHETHER to trade.
This module decides HOW the trade should be managed.
"""

from __future__ import annotations

from datetime import datetime, timedelta


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

LOTS = 2

TARGET_1_POINTS = 50
TARGET_2_POINTS = 100

# Maximum intended holding period.
MAX_HOLDING_DAYS = 3

# Because options lose value with time, the system should prefer
# closing the position rather than carrying it beyond this limit.
THETA_EXIT_ENABLED = True


# ---------------------------------------------------------------------
# Target calculation
# ---------------------------------------------------------------------

def calculate_targets(entry: float, direction: str) -> dict:
    """
    Calculate the two predefined targets.

    LONG:
        T1 = entry + 50
        T2 = entry + 100

    SHORT:
        T1 = entry - 50
        T2 = entry - 100
    """

    direction = direction.lower()

    if direction == "long":
        target_1 = entry + TARGET_1_POINTS
        target_2 = entry + TARGET_2_POINTS

    elif direction == "short":
        target_1 = entry - TARGET_1_POINTS
        target_2 = entry - TARGET_2_POINTS

    else:
        raise ValueError("direction must be 'long' or 'short'")

    return {
        "entry": round(entry, 2),
        "target_1": round(target_1, 2),
        "target_2": round(target_2, 2),
        "lots": LOTS,
        "target_1_points": TARGET_1_POINTS,
        "target_2_points": TARGET_2_POINTS,
    }


# ---------------------------------------------------------------------
# Holding-period / theta risk
# ---------------------------------------------------------------------

def check_holding_period(entry_time: datetime,
                         now: datetime | None = None) -> dict:
    """
    Checks how long the trade has been open.

    Returns:
        holding_days
        max_holding_days
        theta_risk
        exit_required
    """

    now = now or datetime.now(entry_time.tzinfo)

    elapsed = now - entry_time
    holding_days = elapsed.total_seconds() / 86400

    exit_required = holding_days >= MAX_HOLDING_DAYS

    if holding_days >= MAX_HOLDING_DAYS:
        theta_risk = "HIGH"
    elif holding_days >= 2:
        theta_risk = "ELEVATED"
    else:
        theta_risk = "NORMAL"

    return {
        "holding_days": round(holding_days, 2),
        "max_holding_days": MAX_HOLDING_DAYS,
        "theta_risk": theta_risk,
        "exit_required": exit_required,
    }


# ---------------------------------------------------------------------
# Trade management
# ---------------------------------------------------------------------

def manage_trade(entry: float,
                 direction: str,
                 entry_time: datetime,
                 now: datetime | None = None,
                 target_1_hit: bool = False) -> dict:
    """
    Returns the complete risk-management state for an open trade.
    """

    targets = calculate_targets(entry, direction)

    holding = check_holding_period(
        entry_time,
        now
    )

    # Once T1 is hit, the first lot can be considered for exit.
    remaining_lots = LOTS - 1 if target_1_hit else LOTS

    if holding["exit_required"]:
        action = "EXIT"
        reason = "Maximum holding period reached — avoid further theta decay."

    elif target_1_hit:
        action = "MANAGE"
        reason = "Target 1 reached — first lot can be exited."

    else:
        action = "HOLD"
        reason = "Trade remains within the planned holding period."

    return {
        **targets,
        **holding,
        "remaining_lots": remaining_lots,
        "action": action,
        "reason": reason,
    }


# ---------------------------------------------------------------------
# Simple points-risk calculation
# ---------------------------------------------------------------------

def calculate_point_risk(entry: float,
                         stop_loss: float,
                         direction: str) -> float:
    """
    Returns the entry-to-stop distance in points.
    """

    direction = direction.lower()

    if direction == "long":
        risk = entry - stop_loss

    elif direction == "short":
        risk = stop_loss - entry

    else:
        raise ValueError("direction must be 'long' or 'short'")

    if risk <= 0:
        raise ValueError("Stop loss must be on the correct side of entry.")

    return round(risk, 2)


# ---------------------------------------------------------------------
# Risk/reward using the fixed targets
# ---------------------------------------------------------------------

def calculate_risk_reward(entry: float,
                          stop_loss: float,
                          direction: str) -> dict:
    """
    Calculates R:R to Target 1 and Target 2.
    """

    risk = calculate_point_risk(entry, stop_loss, direction)

    direction = direction.lower()

    if direction == "long":
        reward_1 = TARGET_1_POINTS
        reward_2 = TARGET_2_POINTS
    else:
        reward_1 = TARGET_1_POINTS
        reward_2 = TARGET_2_POINTS

    return {
        "risk_points": risk,
        "target_1_reward": reward_1,
        "target_2_reward": reward_2,
        "target_1_rr": round(reward_1 / risk, 2),
        "target_2_rr": round(reward_2 / risk, 2),
    }


# ---------------------------------------------------------------------
# Example
# ---------------------------------------------------------------------

if __name__ == "__main__":

    entry = 25000
    stop_loss = 24950

    print("Targets:")
    print(calculate_targets(entry, "long"))

    print("\nRisk / Reward:")
    print(calculate_risk_reward(entry, stop_loss, "long"))
