from dotenv import load_dotenv
import os
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

load_dotenv("private.env")
ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN")
HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

INSTRUMENT_KEY = "NSE_INDEX|Nifty 50"


# ─────────────────────────────────────────────────────────────────────────
# Live LTP
# ─────────────────────────────────────────────────────────────────────────
def get_nifty_data():
    """Returns live NIFTY 50 LTP payload from Upstox."""
    if not ACCESS_TOKEN:
        raise Exception(
            "UPSTOX_ACCESS_TOKEN is not set — check private.env is present "
            "and loaded before this module is imported."
        )
    url = "https://api.upstox.com/v2/market-quote/ltp"
    params = {"instrument_key": INSTRUMENT_KEY}
    response = requests.get(url, headers=HEADERS, params=params, timeout=10)
    if response.status_code in (401, 403):
        # Upstox access tokens expire daily — this is almost always a stale
        # token rather than a genuine outage, so flag it distinctly instead
        # of looking like every other failure.
        raise Exception(
            f"Upstox auth failed ({response.status_code}) — access token is "
            f"likely expired, regenerate it: {response.text}"
        )
    if response.status_code != 200:
        raise Exception(response.text)
    return response.json()


def get_live_nifty():
    try:
        data = get_nifty_data()
        for key in data["data"]:
            return data["data"][key]["last_price"]
    except Exception as e:
        print(f"get_live_nifty failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
# Historical candles — v3 API (supports 1/5/15/30/60-minute and day bars,
# which the old v2 intraday endpoint could not: it only offered 1minute
# and 30minute). Combines completed-day history with today's intraday
# candles so the chart is continuous right up to the current bar.
# ─────────────────────────────────────────────────────────────────────────

# dashboard.py's TF_MAP passes these interval strings straight through
_INTERVAL_TO_UNIT = {
    "1minute":  ("minutes", 1),
    "5minute":  ("minutes", 5),
    "15minute": ("minutes", 15),
    "30minute": ("minutes", 30),
    "60minute": ("hours",   1),
    "day":      ("days",    1),
}


def _v3_historical(unit: str, interval: int, from_date: str, to_date: str) -> list:
    """Completed-candle history, v3. Dates are 'YYYY-MM-DD'. Returns raw
    candle list: [timestamp, open, high, low, close, volume, oi]."""
    url = (f"https://api.upstox.com/v3/historical-candle/"
           f"{INSTRUMENT_KEY}/{unit}/{interval}/{to_date}/{from_date}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"v3 historical failed ({unit}/{interval}): {r.text}")
        return []
    return (r.json() or {}).get("data", {}).get("candles", [])


def _v3_intraday(unit: str, interval: int) -> list:
    """Today's candles so far, v3."""
    url = f"https://api.upstox.com/v3/historical-candle/intraday/{INSTRUMENT_KEY}/{unit}/{interval}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"v3 intraday failed ({unit}/{interval}): {r.text}")
        return []
    return (r.json() or {}).get("data", {}).get("candles", [])


def get_historical_nifty(interval: str = "5minute", days: int = 5):
    """
    Returns NIFTY 50 OHLC candles for the given interval, covering the last
    `days` days plus today's candles so far.

    `interval` matches dashboard.py's TF_MAP values: 1minute, 5minute,
    15minute, 30minute, 60minute, day.

    Response shape matches what dashboard.py expects:
        {"data": {"candles": [[timestamp, open, high, low, close, volume, oi], ...]}}
    Candles are returned newest-first, which is what dashboard.py's
    fetch_candles_for_tf() already assumes (it does
    list(reversed(candles))[-n_bars:]), so we pass Upstox's native order
    straight through.
    """
    unit, step = _INTERVAL_TO_UNIT.get(interval, ("minutes", 5))

    to_date = date.today()
    from_date = to_date - timedelta(days=days)

    try:
        candles = []

        # Completed days (v3 historical). Skip if the window is same-day only.
        if from_date < to_date:
            candles += _v3_historical(
                unit, step,
                from_date.isoformat(),
                (to_date - timedelta(days=1)).isoformat(),
            )

        # Today's candles so far (v3 intraday)
        candles += _v3_intraday(unit, step)

        if not candles:
            return None

        # Defensive guard: some Upstox intraday responses include a
        # trailing placeholder/forming candle stamped slightly ahead of
        # the actual last tick. Drop anything timestamped after "now"
        # (compared as real IST-aware datetimes, not raw strings, so this
        # stays correct regardless of the server's system timezone).
        now_ist = datetime.now(IST)

        def _not_future(c):
            try:
                ts = datetime.fromisoformat(str(c[0]))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                return ts <= now_ist
            except Exception:
                return True  # if we can't parse it, don't drop it silently

        candles = [c for c in candles if _not_future(c)]

        # De-dupe by timestamp, keep newest-first ordering (Upstox default)
        seen = set()
        deduped = []
        for c in candles:
            ts = c[0]
            if ts not in seen:
                seen.add(ts)
                deduped.append(c)
        deduped.sort(key=lambda c: c[0], reverse=True)

        return {"data": {"candles": deduped}}

    except Exception as e:
        print(f"get_historical_nifty failed ({interval}): {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
# Option chain — v2 API. Needs an expiry_date, which Upstox does not infer
# automatically, so we look up the nearest upcoming weekly expiry from the
# Option Contracts endpoint and cache it for a few hours (it only changes
# once a week).
# ─────────────────────────────────────────────────────────────────────────
_expiry_cache = {"date": None, "fetched_at": None}


def _get_nearest_expiry() -> str | None:
    """Fetch available expiries and return the nearest one on/after today
    as 'YYYY-MM-DD'. Cached for 6 hours since expiries rarely change."""
    now = datetime.now()
    if (_expiry_cache["date"] and _expiry_cache["fetched_at"]
            and now - _expiry_cache["fetched_at"] < timedelta(hours=6)):
        return _expiry_cache["date"]

    url = "https://api.upstox.com/v2/option/contract"
    params = {"instrument_key": INSTRUMENT_KEY}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        if r.status_code != 200:
            print(f"option/contract failed: {r.text}")
            return _expiry_cache["date"]  # fall back to stale cache if any
        contracts = (r.json() or {}).get("data", [])
        expiries = sorted({c["expiry"] for c in contracts if c.get("expiry")})
        today_str = date.today().isoformat()
        upcoming = [e for e in expiries if e >= today_str]
        nearest = upcoming[0] if upcoming else (expiries[-1] if expiries else None)
        if nearest:
            _expiry_cache["date"] = nearest
            _expiry_cache["fetched_at"] = now
        return nearest
    except Exception as e:
        print(f"_get_nearest_expiry failed: {e}")
        return _expiry_cache["date"]


def get_option_chain(expiry_date: str | None = None):
    """
    Returns the live NIFTY 50 put/call option chain from Upstox.

    Output shape matches what dashboard.py's _fetch_upstox_chain() expects:
    a list of dicts, each with:
        strike_price, call_options.market_data.{ltp, oi, volume, net_change},
        call_options.option_greeks.{iv, delta}, and the same under put_options.

    If expiry_date isn't given, uses the nearest upcoming weekly expiry.
    Returns None on any failure so the dashboard falls back to mock data.
    """
    exp = expiry_date or _get_nearest_expiry()
    if not exp:
        print("get_option_chain: could not resolve an expiry date")
        return None

    url = "https://api.upstox.com/v2/option/chain"
    params = {"instrument_key": INSTRUMENT_KEY, "expiry_date": exp}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code != 200:
            print(f"get_option_chain failed: {r.text}")
            return None
        data = (r.json() or {}).get("data")
        return data
    except Exception as e:
        print(f"get_option_chain failed: {e}")
        return None
