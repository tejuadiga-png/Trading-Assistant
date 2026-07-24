"""
All market-data fetching lives in this one file. dashboard.py only ever
calls the four functions at the bottom (get_live_nifty, get_historical_nifty,
get_option_chain, get_status) — it never touches Upstox-specific code.

To swap to a different broker/API later:
    1. Write a new class below that implements MarketDataProvider
       (same 3 methods + `status` property as UpstoxProvider).
    2. Add it to the _PROVIDERS dict near the bottom.
    3. Set MARKET_DATA_PROVIDER=<name> in private.env.
dashboard.py does not change at all.
"""
import os
import requests
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo
from typing import Optional
from dotenv import load_dotenv

load_dotenv("private.env")

IST = ZoneInfo("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────────
# Interface — any provider (Upstox, Zerodha, whatever comes next) must
# implement this. dashboard.py never sees these classes directly.
# ─────────────────────────────────────────────────────────────────────────
class MarketDataProvider(ABC):

    @abstractmethod
    def get_live_price(self, instrument_key: str) -> Optional[float]:
        """Latest traded price, or None if unavailable."""

    @abstractmethod
    def get_historical_candles(self, instrument_key: str, interval: str, days: int) -> list:
        """
        Candles covering the last `days` days plus today so far, newest-first:
        [timestamp, open, high, low, close, volume, oi]. Return [] (not an
        exception) if none are available.
        """

    @abstractmethod
    def get_option_chain(self, instrument_key: str, expiry_date: Optional[str] = None) -> Optional[list]:
        """
        Raw option chain: list of per-strike dicts with call_options /
        put_options sub-dicts (market_data + option_greeks). Return None
        on failure — callers treat None as "no live data available".
        """

    @abstractmethod
    def get_expiries(self, instrument_key: str) -> list:
        """
        All available expiry dates for this instrument, sorted ascending,
        as 'YYYY-MM-DD' strings. Return [] on failure — callers should
        fall back to whatever they last had (or an empty selector).
        """

    @property
    @abstractmethod
    def status(self) -> dict:
        """
        Diagnostics for the last call, e.g.:
            {"ok": True,  "reason": None}
            {"ok": False, "reason": "auth_expired"}
            {"ok": False, "reason": "network_error"}
        Lets the UI show *why* something fell back instead of a single
        generic "API DOWN" badge.
        """


# ─────────────────────────────────────────────────────────────────────────
# Upstox implementation
# ─────────────────────────────────────────────────────────────────────────

# dashboard.py's TF_MAP interval strings -> Upstox v3 (unit, step)
_INTERVAL_TO_UNIT = {
    "1minute":  ("minutes", 1),
    "5minute":  ("minutes", 5),
    "15minute": ("minutes", 15),
    "30minute": ("minutes", 30),
    "60minute": ("hours",   1),
    "day":      ("days",    1),
}


class UpstoxProvider(MarketDataProvider):

    def __init__(self):
        self._access_token = os.getenv("UPSTOX_ACCESS_TOKEN")
        self._status = {"ok": True, "reason": None}
        self._expiry_cache = {"list": None, "fetched_at": None}

    # ── internal helpers ─────────────────────────────────────────────
    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
        }

    def _set_status(self, ok: bool, reason: Optional[str] = None):
        self._status = {"ok": ok, "reason": reason}

    def _get(self, url: str, params: dict | None = None, timeout: int = 10):
        """
        Shared GET with consistent auth-failure detection. Returns the
        parsed JSON dict on success, or None on any failure (self._status
        is set either way so callers/UI can inspect *why*).
        """
        if not self._access_token:
            self._set_status(False, "no_token")
            print("Upstox: UPSTOX_ACCESS_TOKEN is not set — check private.env is loaded.")
            return None
        try:
            r = requests.get(url, headers=self._headers, params=params, timeout=timeout)
        except requests.RequestException as e:
            self._set_status(False, "network_error")
            print(f"Upstox request failed: {url} -> {e}")
            return None

        if r.status_code in (401, 403):
            # Upstox access tokens expire daily — flag this distinctly so
            # the UI can say "token expired" instead of "API down".
            self._set_status(False, "auth_expired")
            print(f"Upstox auth failed ({r.status_code}) on {url} — token likely expired: {r.text}")
            return None
        if r.status_code != 200:
            self._set_status(False, "http_error")
            print(f"Upstox request failed ({r.status_code}) on {url}: {r.text}")
            return None

        self._set_status(True, None)
        return r.json()

    @staticmethod
    def _quoted(instrument_key: str) -> str:
        """
        Instrument keys look like 'NSE_INDEX|Nifty 50' — the '|' and space
        MUST be percent-encoded when they go straight into a URL *path*
        segment (e.g. the v3 historical-candle endpoints below). Endpoints
        that pass instrument_key via `params=` don't need this — `requests`
        already encodes query-string values correctly. This encoding was
        previously missing on the path-based calls, which silently 400'd
        and made the chart fall back to mock candles with no visible error.
        """
        return quote(instrument_key, safe="")

    # ── live price ───────────────────────────────────────────────────
    def get_live_price(self, instrument_key: str) -> Optional[float]:
        data = self._get(
            "https://api.upstox.com/v2/market-quote/ltp",
            params={"instrument_key": instrument_key},
        )
        if not data:
            return None
        try:
            for key in data["data"]:
                return data["data"][key]["last_price"]
        except (KeyError, TypeError) as e:
            self._set_status(False, "bad_response")
            print(f"Upstox LTP response missing expected fields: {e}")
        return None

    # ── historical candles ───────────────────────────────────────────
    def _v3_historical(self, instrument_key: str, unit: str, interval: int,
                        from_date: str, to_date: str) -> list:
        url = (f"https://api.upstox.com/v3/historical-candle/"
               f"{self._quoted(instrument_key)}/{unit}/{interval}/{to_date}/{from_date}")
        data = self._get(url, timeout=15)
        return (data or {}).get("data", {}).get("candles", [])

    def _v3_intraday(self, instrument_key: str, unit: str, interval: int) -> list:
        url = (f"https://api.upstox.com/v3/historical-candle/intraday/"
               f"{self._quoted(instrument_key)}/{unit}/{interval}")
        data = self._get(url, timeout=15)
        return (data or {}).get("data", {}).get("candles", [])

    def get_historical_candles(self, instrument_key: str, interval: str, days: int) -> list:
        unit, step = _INTERVAL_TO_UNIT.get(interval, ("minutes", 5))
        to_date = date.today()
        from_date = to_date - timedelta(days=days)

        candles = []
        if from_date < to_date:
            candles += self._v3_historical(
                instrument_key, unit, step,
                from_date.isoformat(),
                (to_date - timedelta(days=1)).isoformat(),
            )
        candles += self._v3_intraday(instrument_key, unit, step)

        if not candles:
            return []

        # Drop any candle timestamped after "now" (IST-aware compare) —
        # some intraday responses include a forming/placeholder bar ahead
        # of the real last tick.
        now_ist = datetime.now(IST)

        def _not_future(c):
            try:
                ts = datetime.fromisoformat(str(c[0]))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=IST)
                return ts <= now_ist
            except Exception:
                return True

        candles = [c for c in candles if _not_future(c)]

        seen = set()
        deduped = []
        for c in candles:
            if c[0] not in seen:
                seen.add(c[0])
                deduped.append(c)
        deduped.sort(key=lambda c: c[0], reverse=True)
        return deduped

    # ── option chain ─────────────────────────────────────────────────
    def _fetch_all_expiries(self, instrument_key: str) -> list:
        """
        Hits /v2/option/contract once and caches the full sorted expiry
        list for 6 hours (expiries only change weekly). Both
        get_expiries() and _get_nearest_expiry() read from this same
        cache so we don't double-fetch.
        """
        now = datetime.now()
        cache = self._expiry_cache
        if (cache.get("list") is not None and cache.get("fetched_at")
                and now - cache["fetched_at"] < timedelta(hours=6)):
            return cache["list"]

        data = self._get(
            "https://api.upstox.com/v2/option/contract",
            params={"instrument_key": instrument_key},
        )
        if data is None:
            return cache.get("list") or []  # fall back to stale cache, if any

        contracts = data.get("data", [])
        expiries = sorted({c["expiry"] for c in contracts if c.get("expiry")})
        today_str = date.today().isoformat()
        expiries = [e for e in expiries if e >= today_str]  # drop past expiries
        cache["list"] = expiries
        cache["fetched_at"] = now
        return expiries

    def get_expiries(self, instrument_key: str) -> list:
        return list(self._fetch_all_expiries(instrument_key))

    def _get_nearest_expiry(self, instrument_key: str) -> Optional[str]:
        expiries = self._fetch_all_expiries(instrument_key)
        return expiries[0] if expiries else None

    def get_option_chain(self, instrument_key: str, expiry_date: Optional[str] = None) -> Optional[list]:
        exp = expiry_date or self._get_nearest_expiry(instrument_key)
        if not exp:
            print("Upstox: could not resolve an expiry date for option chain")
            return None

        data = self._get(
            "https://api.upstox.com/v2/option/chain",
            params={"instrument_key": instrument_key, "expiry_date": exp},
            timeout=15,
        )
        if data is None:
            return None
        return data.get("data")

    @property
    def status(self) -> dict:
        return dict(self._status)


# ─────────────────────────────────────────────────────────────────────────
# Factory + module-level functions — this is the only part dashboard.py
# imports from. Adding a provider = new class above (or new file) + a line
# here + an env var. dashboard.py's imports never need to change.
# ─────────────────────────────────────────────────────────────────────────
_PROVIDERS = {
    "upstox": UpstoxProvider,
    # "zerodha": ZerodhaProvider,   # future: add class above, register here
}

_provider_name = os.getenv("MARKET_DATA_PROVIDER", "upstox")
if _provider_name not in _PROVIDERS:
    raise ValueError(f"Unknown MARKET_DATA_PROVIDER '{_provider_name}'. Available: {list(_PROVIDERS)}")
_provider = _PROVIDERS[_provider_name]()

INSTRUMENT_KEY = os.getenv("MARKET_INSTRUMENT_KEY", "NSE_INDEX|Nifty 50")


def get_live_nifty() -> Optional[float]:
    try:
        return _provider.get_live_price(INSTRUMENT_KEY)
    except Exception as e:
        print(f"get_live_nifty failed: {e}")
        return None


def get_historical_nifty(interval: str = "5minute", days: int = 5) -> Optional[dict]:
    """
    Same response shape dashboard.py already expects:
        {"data": {"candles": [[ts, o, h, l, c, v, oi], ...]}}
    Returns None if no candles could be fetched (dashboard.py falls back
    to mock candles in that case).
    """
    try:
        candles = _provider.get_historical_candles(INSTRUMENT_KEY, interval, days)
    except Exception as e:
        print(f"get_historical_nifty failed ({interval}): {e}")
        return None
    if not candles:
        return None
    return {"data": {"candles": candles}}


def get_option_chain(expiry_date: Optional[str] = None) -> Optional[list]:
    try:
        return _provider.get_option_chain(INSTRUMENT_KEY, expiry_date)
    except Exception as e:
        print(f"get_option_chain failed: {e}")
        return None


def get_expiries() -> list:
    """All available expiry dates, ascending, as 'YYYY-MM-DD' strings."""
    try:
        return _provider.get_expiries(INSTRUMENT_KEY)
    except Exception as e:
        print(f"get_expiries failed: {e}")
        return []


def get_status() -> dict:
    """{'ok': bool, 'reason': 'auth_expired'|'network_error'|'http_error'|'no_token'|'bad_response'|None}"""
    return _provider.status
