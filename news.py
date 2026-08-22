"""
news.py — news/event regime filter.

Sits ahead of (and overrides) the price-action regime classifier in
regime.py: a high-impact news event approaching, in progress, or just
concluded takes priority over TRENDING/CONSOLIDATION. regime.py calls
check_news_regime() and does:

    if news_state["regime"] == "NEWS":
        return news_state

Design notes / assumptions (flag and change as needed):

  * Event shape — a plain dict, so you can source these from wherever
    your news calendar lives (DB, API, CSV):
        {"name": str, "time": datetime, "impact": "HIGH"|"MEDIUM"|"LOW"}
    `time` must be timezone-consistent with whatever `now` you pass in
    (both naive or both aware — mixing the two raises a TypeError from
    datetime subtraction, which is Python's, not this module's).

        news_events = [
            {"name": "RBI Interest Rate Decision", "time": ..., "impact": "HIGH"},
            {"name": "US CPI", "time": ..., "impact": "HIGH"},
        ]

  * NEWS_BLOCKING_IMPACTS = {"HIGH"} — only high-impact events block
    trading by default. Add "MEDIUM" here later if you decide
    medium-impact events should pause trading too:
        NEWS_BLOCKING_IMPACTS = {"HIGH", "MEDIUM"}
  * A single global blackout window (not per-impact) around any
    blocking event:
        NEWS_BLACKOUT_BEFORE_MIN = 30
        NEWS_BLACKOUT_AFTER_MIN = 30
    i.e. 30 min before -> NO TRADE -> event -> NO TRADE -> 30 min after.
    These two numbers are the thing that actually needs tuning against
    your own event types — no specific windows were given, so treat
    these as placeholders.
  * If multiple blocking events are inside their blackout window at the
    same time, the nearest one (smallest |minutes_to_event|) is reported.
"""
from __future__ import annotations
from datetime import datetime, timezone

NEWS_IMPACT_LEVELS = ("HIGH", "MEDIUM", "LOW")

# Only impacts in this set can trigger a NEWS regime / block trading.
NEWS_BLOCKING_IMPACTS = {"HIGH"}

# Global blackout window applied around any blocking-impact event.
NEWS_BLACKOUT_BEFORE_MIN = 30
NEWS_BLACKOUT_AFTER_MIN = 30


def _minutes_to_event(event_time: datetime, now: datetime) -> float:
    """Positive = event is upcoming; negative = event has already happened."""
    return (event_time - now).total_seconds() / 60.0


def _in_blackout_window(minutes_to_event: float,
                         before_min: float = NEWS_BLACKOUT_BEFORE_MIN,
                         after_min: float = NEWS_BLACKOUT_AFTER_MIN) -> bool:
    """
    30 min before -> NO TRADE -> event -> NO TRADE -> 30 min after.
    minutes_to_event > 0 means the event hasn't happened yet (compare to
    the "before" window); <= 0 means it already has (compare to "after").
    """
    if minutes_to_event >= 0:
        return minutes_to_event <= before_min
    return abs(minutes_to_event) <= after_min


def check_news_regime(news_events: list[dict] | None, now: datetime | None = None,
                       blocking_impacts=NEWS_BLOCKING_IMPACTS,
                       before_min: float = NEWS_BLACKOUT_BEFORE_MIN,
                       after_min: float = NEWS_BLACKOUT_AFTER_MIN) -> dict:
    """
    The main function regime.py calls. Checks `news_events` against
    `now` and returns:

    If a blocking event is inside its blackout window:
        {
            "regime": "NEWS",
            "trade_allowed": False,
            "news_impact": "HIGH",
            "event_name": "US CPI",
            "event_time": "...",       # ISO format
            "minutes_to_event": 12.5,
            "reason": "High-impact news within blackout window.",
        }

    Otherwise:
        {
            "regime": "NO_NEWS",
            "trade_allowed": True,
            "news_impact": None,
            "event_name": None,
            "event_time": None,
            "minutes_to_event": None,
            "reason": "No blocking high-impact news.",
        }

    `news_events` may be None/empty.
    """
    now = now or datetime.now(timezone.utc)

    no_news_result = {
        "regime": "NO_NEWS",
        "trade_allowed": True,
        "news_impact": None,
        "event_name": None,
        "event_time": None,
        "minutes_to_event": None,
        "reason": "No blocking high-impact news.",
    }

    if not news_events:
        return no_news_result

    blocking_candidates = []
    for event in news_events:
        if event.get("impact") not in blocking_impacts:
            continue
        minutes = _minutes_to_event(event["time"], now)
        if _in_blackout_window(minutes, before_min, after_min):
            blocking_candidates.append((event, minutes))

    if not blocking_candidates:
        return no_news_result

    # Nearest blocking event wins (smallest absolute distance to now).
    event, minutes = min(blocking_candidates, key=lambda pair: abs(pair[1]))

    return {
        "regime": "NEWS",
        "trade_allowed": False,
        "news_impact": event.get("impact"),
        "event_name": event.get("name"),
        "event_time": event["time"].isoformat(),
        "minutes_to_event": round(minutes, 1),
        "reason": "High-impact news within blackout window.",
    }
