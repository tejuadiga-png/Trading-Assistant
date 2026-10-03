from nicegui import ui, run, app
import plotly.graph_objects as go
import random
import os
import time
import asyncio
from datetime import datetime, timedelta
import pytz

from upstox import get_live_nifty, get_historical_nifty, get_option_chain, get_expiries, get_status
from pipeline_adapter import compute_conviction_real
import risk

# ─────────────────────────────────────────────────────────────────────────
# NSE / Nifty config
# ─────────────────────────────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")

# No hardcoded defaults for either of these. STORAGE_SECRET signs NiceGUI's
# session cookie — a known default lets anyone forge a "logged_in" cookie
# and skip the password screen entirely, which is a worse hole than a weak
# password would be. Both are required, not just PASSWORD.
PASSWORD = os.getenv("DASHBOARD_PASSWORD")
STORAGE_SECRET = os.getenv("STORAGE_SECRET")
if not PASSWORD or not STORAGE_SECRET:
    raise RuntimeError(
        "DASHBOARD_PASSWORD and STORAGE_SECRET must both be set in the "
        "environment/private.env — no defaults are provided on purpose."
    )

SYMBOL      = "NIFTY 50"
NIFTY_LOT   = 75          # update per NSE circular
STRIKE_STEP = 50
CHAIN_WIDTH = 10          # strikes above & below ATM

import math

def _atm_strike(spot: float) -> float:
    """Deterministic ATM strike calc. Python's round() uses banker's
    rounding (rounds .5 to the nearest EVEN number), so an exact tie
    between two strikes resolves inconsistently depending on spot's
    value. This floor-based version always resolves a tie the same way."""
    return math.floor(spot / STRIKE_STEP + 0.5) * STRIKE_STEP

# Timeframe → (Upstox interval string, days of history to fetch, bars to show)
TF_MAP = {
    "1m":  ("1minute",   2,   390),   # about one trading day
    "5m":  ("5minute",   10,  500),   # ~8 trading days
    "15m": ("15minute",  30,  500),
    "30m": ("30minute",  60,  500),
    "1H":  ("60minute", 180,  500),
    "D":   ("day",      365,  365),
}
DEFAULT_TF = "5m"

def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    o = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    c = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return o <= now <= c

# ─────────────────────────────────────────────────────────────────────────
# Shared live state (updated by the 2-second timer)
# ─────────────────────────────────────────────────────────────────────────
live_state = {
    "spot":          None,
    "candles":       None,   # dict of lists: opens/highs/lows/closes/volumes/times
    "last_bucket":   None,   # datetime of current 5-min candle start
    "tf":            DEFAULT_TF,
    "api_ok":        True,   # flips False when the market data provider starts failing
    "api_reason":    None,   # 'auth_expired' | 'network_error' | 'http_error' | 'no_token' | None
}

# ─────────────────────────────────────────────────────────────────────────
# Historical candle fetching (TF-aware)
# ─────────────────────────────────────────────────────────────────────────
MOCK_BASE = {
    "opens":  [24800,24840,24820,24860,24850,24890,24870,24910,24930,24900,24950,24980,24960,24940,24920,24960,24980,25000,24990,25010,25030,25050,25020,24990,25000,25040,25060,25050,25020,25000],
    "highs":  [24860,24870,24880,24900,24910,24940,24920,24960,24970,24970,24990,25020,25010,24990,24970,25010,25020,25040,25030,25050,25070,25080,25060,25030,25040,25080,25100,25090,25060,25040],
    "lows":   [24790,24800,24810,24830,24840,24870,24850,24890,24910,24880,24930,24960,24940,24920,24900,24940,24960,24980,24970,24990,25010,25030,25000,24970,24980,25020,25040,25030,25000,24980],
    "closes": [24840,24820,24860,24850,24890,24870,24910,24930,24900,24950,24980,25010,24990,24960,24940,24980,25000,25010,25000,25020,25060,25050,25020,25000,25010,25060,25080,25050,25010,24990],
    "volumes":[1200,980,1100,1050,1300,890,1400,1150,1050,1200,1300,1100,950,1050,980,1200,1350,1100,1050,1250,1400,1300,1100,950,1050,1300,1450,1200,1050,980],
}


def _make_times(n: int, tf: str) -> list:
    """Generate n bar timestamps going back from now, spaced by tf.
    Returns real datetime objects (not pre-formatted strings) so this
    always matches the type used by real historical data and by
    update_live_candle — mixing datetime objects and strings in the same
    list confuses Plotly's date x-axis."""
    minutes = {"1m":1,"5m":5,"15m":15,"30m":30,"1H":60,"D":1440}.get(tf, 5)
    now = datetime.now(IST)
    if tf == "D":
        return [now.replace(hour=0,minute=0,second=0,microsecond=0) - timedelta(days=(n-1-i)) for i in range(n)]
    bucket_min = (now.minute // minutes) * minutes
    base = now.replace(minute=bucket_min, second=0, microsecond=0)
    return [base - timedelta(minutes=minutes*(n-1-i)) for i in range(n)]


def fetch_candles_for_tf(tf: str) -> dict:
    """Fetch historical candles for the given timeframe.
    Returns dict with opens/highs/lows/closes/volumes/times lists.
    Falls back to mock data if the provider call fails."""
    interval, days_back, n_bars = TF_MAP[tf]
    try:
        raw = get_historical_nifty(interval=interval, days=days_back)
        candles = (raw or {}).get("data", {}).get("candles")
        if candles:
            candles = list(reversed(candles))[-n_bars:]
            times = [datetime.fromisoformat(str(c[0])) for c in candles]
            live_state["api_ok"] = True
            live_state["api_reason"] = None
            return {
                "opens":   [c[1] for c in candles],
                "highs":   [c[2] for c in candles],
                "lows":    [c[3] for c in candles],
                "closes":  [c[4] for c in candles],
                "volumes": [c[5] for c in candles],
                "times":   times,
            }
    except Exception as e:
        print(f"Historical fetch failed ({tf}): {e}")

    status = get_status()
    live_state["api_ok"] = status.get("ok", False)
    live_state["api_reason"] = status.get("reason")

    # Fallback: use mock base, truncate/repeat to n_bars
    base = MOCK_BASE
    n = min(n_bars, len(base["closes"]))
    return {
        "opens":   base["opens"][-n:],
        "highs":   base["highs"][-n:],
        "lows":    base["lows"][-n:],
        "closes":  base["closes"][-n:],
        "volumes": base["volumes"][-n:],
        "times":   _make_times(n, tf),
    }


def init_live_state():
    price = get_live_nifty()
    status = get_status()
    live_state["api_ok"] = price is not None
    live_state["api_reason"] = None if price is not None else status.get("reason")
    live_state["spot"]    = price or 24892.0
    live_state["candles"] = fetch_candles_for_tf(DEFAULT_TF)
    live_state["last_bucket"] = None
    live_state["tf"] = DEFAULT_TF


def update_live_candle():
    """Poll live price, roll new candle when the current bucket changes.
    Only the current forming bar is mutated — past bars are never re-rolled.
    No-op outside NSE trading hours so the chart freezes at the last traded
    price instead of drifting on stale/mock ticks."""
    if not is_market_open():
        return
    price = get_live_nifty()
    if price is None:
        status = get_status()
        live_state["api_ok"] = False
        live_state["api_reason"] = status.get("reason")
        return
    live_state["api_ok"] = True
    live_state["api_reason"] = None
    live_state["spot"] = price
    c = live_state["candles"]
    tf = live_state["tf"]
    minutes = {"1m":1,"5m":5,"15m":15,"30m":30,"1H":60,"D":1440}.get(tf, 5)
    now = datetime.now(IST)

    if tf == "D":
        bucket = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        bucket = now.replace(minute=(now.minute//minutes)*minutes, second=0, microsecond=0)

    if live_state["last_bucket"] is None:
        live_state["last_bucket"] = bucket

    if bucket != live_state["last_bucket"]:
        # New bar
        last_close = c["closes"][-1]
        c["opens"].append(last_close)
        c["highs"].append(price)
        c["lows"].append(price)
        c["closes"].append(price)
        c["volumes"].append(random.randint(400, 900))
        c["times"].append(bucket)
        _, _, n_bars = TF_MAP[tf]
        for key in ("opens","highs","lows","closes","volumes","times"):
            c[key] = c[key][-n_bars:]
        live_state["last_bucket"] = bucket
    else:
        # Update current bar
        c["closes"][-1]  = price
        c["highs"][-1]   = max(c["highs"][-1],  price)
        c["lows"][-1]    = min(c["lows"][-1],   price)
        c["volumes"][-1] += random.randint(20, 120)


# ─────────────────────────────────────────────────────────────────────────
# Conviction engine  (all fixes: chop veto, 3-state direction, dispersion)
# ─────────────────────────────────────────────────────────────────────────
WEIGHTS = {
    "trend":18,"liquidity_sweep":15,"volume_profile":15,"adx_regime":12,
    "order_flow":12,"flow_fii_dii":14,"vol_structure":8,"breadth":6,
}
TOTAL_WEIGHT     = sum(WEIGHTS.values())
TRADE_THRESHOLD  = 55   # kept in sync with strategy.py's SIGNAL_THRESHOLDS["ACTIVE"] -- this is
                         # what actually gates a real BUY/SELL signal now, so the "pts to TRADE"
                         # display and color gradient below need to match it, not the old 80.
WATCH_THRESHOLD  = 40   # intermediate "getting close" color tier below TRADE_THRESHOLD --
                         # arbitrary choice (not tied to any strategy.py value), adjust as you like
CHOP_ADX_VETO    = 18
CHOP_SCORE_CAP   = 45
DISPERSION_VETO  = 0.7
DISPERSION_PENALTY = 0.8


def mock_market_state(symbol: str, bar_id: int = 0) -> dict:
    """Deterministic per (symbol, bar_id) — same bar always gives same reading.
    Order-flow/OI/FII-DII fields are still simulated until a real
    options-analytics endpoint is wired into a provider."""
    rng = random.Random(f"{symbol}-{bar_id}")
    return {
        "trend_aligned":          rng.random() > 0.3,
        "sweep_confirmed":        rng.random() > 0.35,
        "near_poc_or_val":        rng.choice(["VAL","VAH","POC","mid-range"]),
        "adx":                    rng.uniform(10, 38),
        "range_compression":      rng.uniform(0.08, 1.3),
        "order_flow_buy_fraction":rng.uniform(0.35, 0.92),
        "fii_net":                rng.uniform(-3000, 3500),
        "dii_net":                rng.uniform(-2000, 2500),
        "iv_percentile":          rng.uniform(0.1, 0.95),
        "term_structure_healthy": rng.random() > 0.3,
        "banknifty_confirms":     rng.random() > 0.25,
    }


def _direction(s: dict):
    bv = sum([s["trend_aligned"], s["order_flow_buy_fraction"]>=0.55, s["fii_net"]>0])
    bw = sum([not s["trend_aligned"], s["order_flow_buy_fraction"]<0.45, s["fii_net"]<0])
    if bv>=2 and bv>bw: return "LONG"
    if bw>=2 and bw>bv: return "SHORT"
    return None


def compute_conviction_mock(symbol: str, bar_id: int = 0) -> dict:
    """Fallback engine — pure synthetic mock_market_state()-based. Used
    only if the real regime.run_pipeline() engine (via pipeline_adapter.py)
    raises, e.g. insufficient candle history right after market open."""
    s = mock_market_state(symbol, bar_id)
    reasons = []
    chop = s["adx"] < CHOP_ADX_VETO or s["range_compression"] < 0.4

    ts  = 1.0 if s["trend_aligned"] else 0.2;    reasons.append(("Trend Filter",       "Daily/15m/5m aligned",          s["trend_aligned"]))
    ss  = 1.0 if s["sweep_confirmed"] else 0.25;  reasons.append(("Liquidity Sweep",    "Sweep + reclaim confirmed",      s["sweep_confirmed"]))
    vps = 0.9 if s["near_poc_or_val"] in ("VAL","POC") else 0.4; reasons.append(("Volume Profile", f"Near {s['near_poc_or_val']}", vps>0.5))
    ads = min(s["adx"]/30,1.0);                   reasons.append(("ADX Regime",          f"Strength {s['adx']:.0f}",      s["adx"]>=20))
    ofs = s["order_flow_buy_fraction"];            reasons.append(("Order Flow",          f"Buy-fraction {ofs:.2f}",       ofs>=0.55))
    fc  = (s["fii_net"]+0.5*s["dii_net"]+4000)/8000
    fs  = max(0,min(fc,1));                        reasons.append(("FII/DII Flow",        "Net institutional support",     fs>0.55))
    vs  = (0 if s["iv_percentile"]>0.85 else (1-s["iv_percentile"]))*(1 if s["term_structure_healthy"] else 0.5)
    reasons.append(("Volatility", f"IV pct {s['iv_percentile']:.0%}", vs>0.4))
    brs = 0.9 if s["banknifty_confirms"] else 0.2; reasons.append(("Bank Nifty Breadth","Index confirms signal",          s["banknifty_confirms"]))

    comp = [ts,ss,vps,ads,ofs,fs,vs,brs]
    disp = max(comp)-min(comp)

    weighted = (ts*WEIGHTS["trend"]+ss*WEIGHTS["liquidity_sweep"]+vps*WEIGHTS["volume_profile"]+
                ads*WEIGHTS["adx_regime"]+ofs*WEIGHTS["order_flow"]+fs*WEIGHTS["flow_fii_dii"]+
                vs*WEIGHTS["vol_structure"]+brs*WEIGHTS["breadth"])
    score = (weighted/TOTAL_WEIGHT)*100

    if chop:
        score = min(score, CHOP_SCORE_CAP)
        reasons.append(("Regime Filter","ADX/range compressed — chop, conviction capped",False))
    if disp > DISPERSION_VETO:
        score *= DISPERSION_PENALTY
        reasons.append(("Signal Agreement",f"High dispersion ({disp:.2f}) — conflicting signals",False))

    direction = _direction(s)
    if direction is None:
        score = min(score, WATCH_THRESHOLD-1)
        reasons.append(("Direction Consensus","Signals split — no directional edge",False))

    # Round ONCE before threshold check so displayed number and verdict always agree
    score = round(max(0,min(score,100)),1)

    if direction is None or chop:
        verdict, vc = "NO TRADE", "#EF5350"
    elif score >= TRADE_THRESHOLD:
        verdict, vc = "TRADE",    "#26A69A"
    elif score >= WATCH_THRESHOLD:
        verdict, vc = "WATCH",    "#F59E0B"
    else:
        verdict, vc = "NO TRADE", "#EF5350"

    return {"score":score,"verdict":verdict,"verdict_color":vc,
            "direction":direction or "-","reasons":reasons,"raw":s,"chop":chop,
            "trade_levels": None, "risk_plan": None, "last_close": s.get("last_close")}


def compute_conviction(symbol: str, bar_id: int = 0) -> dict:
    """Primary engine — regime.run_pipeline() (news filter -> regime
    filter -> strategy -> conviction -> signal -> trade levels), reshaped
    to this dict's shape by pipeline_adapter.py. Falls back to the mock
    scorer on any failure so the UI never crashes."""
    try:
        return compute_conviction_real(symbol, bar_id, live_state["candles"])
    except Exception as e:
        print(f"[compute_conviction] real pipeline failed, falling back to mock: {e}")
        return compute_conviction_mock(symbol, bar_id)


# ─────────────────────────────────────────────────────────────────────────
# Option chain  — live provider data only (no mock fallback)
# ─────────────────────────────────────────────────────────────────────────

def _fetch_live_chain(spot: float, expiry: str | None = None) -> list | None:
    """Try to pull a live option chain from the configured provider for a
    given expiry (None = provider's nearest-expiry default).
    Expected shape from get_option_chain(): list of dicts with keys:
        strike_price, call_options.market_data.{ltp, oi, volume, iv, greeks},
        put_options.market_data.{ltp, oi, volume, iv, greeks}
    Returns None on any failure; callers should handle the empty case
    (no mock data is generated)."""
    try:
        raw = get_option_chain(expiry_date=expiry)
        if not raw:
            return None
        atm = _atm_strike(spot)
        rows = []
        for item in raw:
            k = float(item.get("strike_price", 0))
            if abs(k - atm) > CHAIN_WIDTH * STRIKE_STEP:
                continue
            ce = (item.get("call_options") or {}).get("market_data", {})
            pe = (item.get("put_options")  or {}).get("market_data", {})
            ce_g = (item.get("call_options") or {}).get("option_greeks", {})
            pe_g = (item.get("put_options")  or {}).get("option_greeks", {})

            # Upstox's option chain response has no `net_change` field —
            # only `ltp` and `close_price` (previous close). Derive the
            # % change ourselves instead of reading a key that doesn't
            # exist (which was silently returning 0 for every row).
            def _pct_change(md: dict) -> float:
                ltp = float(md.get("ltp", 0))
                prev = float(md.get("close_price", 0))
                if not prev:
                    return 0.0
                return (ltp - prev) / prev * 100

            rows.append({
                "strike":    k,
                "atm":       k == atm,
                "itm_call":  k < spot,
                "itm_put":   k > spot,
                "call_ltp":  round(float(ce.get("ltp",  0)), 1),
                "call_oi":   round(float(ce.get("oi",   0)) / 1e5, 2),
                "call_chg":  round(_pct_change(ce), 1),
                # Upstox's `iv` is already a percentage (e.g. 50.78 = 50.78%),
                # NOT a 0-1 fraction — multiplying by 100 was the bug behind
                # the garbled IV numbers.
                "call_iv":   round(float(ce_g.get("iv", 0)), 1),
                "call_delta":round(float(ce_g.get("delta", 0)), 2),
                "put_ltp":   round(float(pe.get("ltp",  0)), 1),
                "put_oi":    round(float(pe.get("oi",   0)) / 1e5, 2),
                "put_chg":   round(_pct_change(pe), 1),
                "put_iv":    round(float(pe_g.get("iv", 0)), 1),
                "put_delta": round(float(pe_g.get("delta", 0)), 2),
            })
        rows.sort(key=lambda r: r["strike"])
        return rows or None
    except Exception as e:
        print(f"Option chain fetch failed: {e}")
        return None


def make_option_chain(spot: float, expiry: str | None = None) -> list:
    """Build strike ladder from live provider data only, for the given
    expiry (None = provider's nearest-expiry default).
    Returns an empty list if the fetch fails — no mock data is generated."""
    return _fetch_live_chain(spot, expiry) or []


def update_option_chain(rows: list, spot: float, expiry: str | None = None) -> list:
    """Refresh the option chain from live provider data only.
    No mock drift — if the live fetch fails, the previous rows are kept
    as-is until the next successful fetch."""
    fresh = _fetch_live_chain(spot, expiry)
    return fresh if fresh else rows


def render_option_chain(rows: list, opt_type: str) -> str:
    if not rows:
        status = get_status()
        reason = status.get("reason")
        reason_msg = {
            "auth_expired": "Access token has expired — regenerate UPSTOX_ACCESS_TOKEN.",
            "no_token":     "No access token configured — check private.env.",
            "network_error":"Network error reaching the data provider.",
            "http_error":   "Data provider returned an error.",
        }.get(reason, "No live option chain data available yet.")
        return (f'<div style="padding:24px 16px;text-align:center;color:#787B86;font-size:12px;">'
                f'{reason_msg}</div>')

    show_c = opt_type in ("ALL","CALL")
    show_p = opt_type in ("ALL","PUT")

    hd = ""
    if show_c: hd += '<th class="call-hdr">OI(L)</th><th class="call-hdr">IV%</th><th class="call-hdr">Chg%</th><th class="call-hdr">Delta</th><th class="call-hdr">CALL LTP</th>'
    hd += '<th class="strike-col">Strike</th>'
    if show_p: hd += '<th class="put-hdr">PUT LTP</th><th class="put-hdr">Delta</th><th class="put-hdr">Chg%</th><th class="put-hdr">IV%</th><th class="put-hdr">OI(L)</th>'

    body = ""
    for r in rows:
        rc  = "tv-chain-row atm" if r["atm"] else "tv-chain-row"
        td  = ""
        if show_c:
            cc  = "tv-chg-up" if r["call_chg"]>=0 else "tv-chg-dn"
            cbg = "background:#26A69A0F;" if r["itm_call"] and not r["atm"] else ""
            td += (f'<td style="{cbg}"><span class="t
