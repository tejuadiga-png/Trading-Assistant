"""
news_calendar.py — a hand-maintained list of known high-impact events,
in news.py's exact `news_events` shape.

Why this exists: Upstox's API has a News API (GET /v2/news), but it
only returns PAST articles for specific instruments (last 7 days,
headline/summary/link/published-timestamp) — there's no forward-looking
economic calendar and no impact classification, so it can't feed
news.py's blackout-window logic, which needs to know an event is
SCHEDULED before it happens.

This is a stopgap, not a permanent solution: RBI MPC dates and major US
macro releases (CPI, FOMC, jobs report) are published publicly months in
advance on fixed schedules, so a manually-updated list works and costs
nothing. If you later get access to a real economic-calendar API (e.g.
FXStreet's, Trading Economics, investing.com), replace get_upcoming_events()
below with a real fetch — nothing else needs to change, since
pipeline_adapter.py only ever consumes the list this returns, in exactly
news.py's {"name","time","impact"} shape.

Usage:
    from news_calendar import get_upcoming_events
    events = get_upcoming_events()
    regime.run_pipeline(..., news_events=events)
"""
from __future__ import annotations
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────
# Hand-maintained calendar. Update this list periodically — e.g. RBI's MPC
# calendar is published on rbi.org.in months ahead; US Fed meeting dates
# on federalreserve.gov; CPI release dates on bls.gov's schedule page.
#
# `time` must be timezone-aware (UTC here) so comparisons against
# news.py's `now` (also UTC-aware by default) work correctly.
#
# THESE ARE PLACEHOLDER DATES — replace with the real, current schedule
# before relying on this. I have no way to verify future RBI/Fed/CPI
# dates are still accurate by the time you read this; always check the
# official calendar.
# ─────────────────────────────────────────────────────────────────────────
_STATIC_EVENTS = [
    # {"name": "RBI Interest Rate Decision", "time": datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc), "impact": "HIGH"},
    # {"name": "US CPI", "time": datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc), "impact": "HIGH"},
    # {"name": "FOMC Rate Decision", "time": datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc), "impact": "HIGH"},
]


def get_upcoming_events(now: datetime | None = None, lookahead_hours: float = 48) -> list[dict]:
    """
    Returns events from _STATIC_EVENTS within the next `lookahead_hours`
    (default 48h) of `now`, plus anything from the past hour (so a
    just-passed event can still register in news.py's "after" blackout
    window). Trimming the list like this keeps what gets passed into
    regime.run_pipeline() small and relevant, rather than handing over
    the entire static list (including irrelevant events months away)
    on every call.
    """
    now = now or datetime.now(timezone.utc)
    window_start = now.timestamp() - 3600  # 1 hour in the past
    window_end = now.timestamp() + lookahead_hours * 3600

    return [
        e for e in _STATIC_EVENTS
        if window_start <= e["time"].timestamp() <= window_end
    ]
