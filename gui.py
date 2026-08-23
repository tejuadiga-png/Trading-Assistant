from nicegui import ui, run, app
import plotly.graph_objects as go
import random
import os
import time
from datetime import datetime, timedelta
import pytz

from upstox import get_live_nifty, get_historical_nifty, get_option_chain, get_expiries, get_status
from pipeline_adapter import compute_conviction_real
import risk

# ─────────────────────────────────────────────────────────────────────────
# NSE / Nifty config
# ─────────────────────────────────────────────────────────────────────────
IST = pytz.timezone("Asia/Kolkata")
PASSWORD    = os.getenv("DASHBOARD_PASSWORD", "model2026")
SYMBOL      = "NIFTY 50"
NIFTY_LOT   = 75          # update per NSE circular
STRIKE_STEP = 50
CHAIN_WIDTH = 10          # strikes above & below ATM

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
        atm = round(spot / STRIKE_STEP) * STRIKE_STEP
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
            td += (f'<td style="{cbg}"><span class="tv-oi-val">{r["call_oi"]}</span></td>'
                   f'<td style="{cbg}"><span class="tv-iv-val">{r["call_iv"]}</span></td>'
                   f'<td style="{cbg}"><span class="{cc}">{r["call_chg"]:+.1f}%</span></td>'
                   f'<td style="{cbg}"><span class="tv-iv-val">{r["call_delta"]}</span></td>'
                   f'<td style="{cbg}"><span class="tv-call-ltp">{r["call_ltp"]}</span></td>')
        sk = f'{int(r["strike"])}' + (' <span style="font-size:8px;color:#2196F3">ATM</span>' if r["atm"] else "")
        td += f'<td class="strike-col">{sk}</td>'
        if show_p:
            pc  = "tv-chg-up" if r["put_chg"]>=0 else "tv-chg-dn"
            pbg = "background:#EF53500F;" if r["itm_put"] and not r["atm"] else ""
            td += (f'<td style="{pbg}"><span class="tv-put-ltp">{r["put_ltp"]}</span></td>'
                   f'<td style="{pbg}"><span class="tv-iv-val">{r["put_delta"]}</span></td>'
                   f'<td style="{pbg}"><span class="{pc}">{r["put_chg"]:+.1f}%</span></td>'
                   f'<td style="{pbg}"><span class="tv-iv-val">{r["put_iv"]}</span></td>'
                   f'<td style="{pbg}"><span class="tv-oi-val">{r["put_oi"]}</span></td>')
        body += f'<tr class="{rc}">{td}</tr>'

    return (f'<table class="tv-chain-table"><thead><tr>{hd}</tr></thead>'
            f'<tbody>{body}</tbody></table>')


# ─────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────
TV_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
* { box-sizing: border-box; }
body { background:#131722; font-family:'Inter',-apple-system,sans-serif; color:#D1D4DC; margin:0; padding:0; height:100vh; overflow:hidden; }
html { height:100%; overflow:hidden; }
.q-page,.q-page-container,.nicegui-content,body>div { height:100%!important; min-height:unset!important; padding:0!important; margin:0!important; }
.tv-topbar { display:flex; align-items:center; justify-content:space-between; background:#1E222D; border-bottom:1px solid #2A2E39; padding:0 16px; height:48px; width:100%; position:sticky; top:0; z-index:100; box-sizing:border-box; }
.tv-logo { font-size:14px; font-weight:700; color:#2196F3; letter-spacing:.08em; text-transform:uppercase; }
.tv-ticker-strip { display:flex; gap:24px; font-size:12px; }
.tv-ticker-item { display:flex; flex-direction:column; align-items:flex-end; }
.tv-ticker-name { color:#787B86; font-size:10px; }
.tv-ticker-price { color:#D1D4DC; font-weight:600; }
.tv-ticker-change-up { color:#26A69A; font-size:10px; }
.tv-ticker-change-dn { color:#EF5350; font-size:10px; }
.tv-time { color:#787B86; font-size:12px; }
.tv-api-badge { font-size:10px; padding:2px 7px; border-radius:3px; font-weight:600; letter-spacing:.03em; }
.tv-api-ok { color:#26A69A; background:#26A69A1A; }
.tv-api-down { color:#EF5350; background:#EF53501A; }
.tv-layout { display:flex; height:calc(100vh - 48px); overflow:hidden; width:100%; }
.tv-sidebar { width:420px; min-width:260px; max-width:760px; background:#1E222D; display:flex; flex-direction:column; overflow:hidden; flex-shrink:0; }
.tv-resize-handle { width:5px; min-width:5px; cursor:col-resize; background:#2A2E39; flex-shrink:0; position:relative; z-index:20; transition:background .1s; }
.tv-resize-handle:hover,.tv-resize-handle.dragging { background:#2196F3; }
.tv-resize-handle::after { content:""; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); width:3px; height:28px; border-radius:2px; background:#787B86; }
.tv-resize-handle:hover::after,.tv-resize-handle.dragging::after { background:#fff; }
.tv-sidebar-header { padding:10px 12px 8px; font-size:10px; font-weight:600; letter-spacing:.1em; color:#787B86; text-transform:uppercase; border-bottom:1px solid #2A2E39; display:flex; align-items:center; justify-content:space-between; }
.tv-sidebar-sub { font-size:10px; color:#787B86; text-transform:none; letter-spacing:normal; }
.tv-chain-tabs { display:flex; gap:6px; padding:10px 12px; border-bottom:1px solid #2A2E39; background:#1E222D; }
.tv-chain-tab { flex:1; text-align:center; padding:7px 4px; font-size:11px; font-weight:700; letter-spacing:.03em; border-radius:4px; cursor:pointer; color:#787B86; background:#131722; transition:background .15s,color .15s; user-select:none; }
.tv-chain-tab:hover { background:#2A2E39; color:#D1D4DC; }
.tv-chain-tab.active { color:#fff; }
.tv-chain-tab.active.tab-all { background:#2962FF; }
.tv-chain-tab.active.tab-call { background:#26A69A; }
.tv-chain-tab.active.tab-put { background:#EF5350; }
.tv-expiry-tabs { display:flex; gap:6px; padding:8px 12px; border-bottom:1px solid #2A2E39; background:#1E222D; overflow-x:auto; }
.tv-expiry-tab { flex-shrink:0; padding:5px 10px; font-size:10px; font-weight:600; letter-spacing:.02em; border-radius:4px; cursor:pointer; color:#787B86; background:#131722; transition:background .15s,color .15s; user-select:none; white-space:nowrap; }
.tv-expiry-tab:hover { background:#2A2E39; color:#D1D4DC; }
.tv-expiry-tab.active { color:#fff; background:#2962FF; }
.tv-chain-scroll { flex:1; overflow-y:auto; overflow-x:hidden; }
.tv-chain-table { width:100%; border-collapse:collapse; font-size:11px; }
.tv-chain-table thead th { position:sticky; top:0; background:#1E222D; color:#787B86; font-weight:600; font-size:9px; text-transform:uppercase; letter-spacing:.04em; padding:7px 5px; border-bottom:1px solid #2A2E39; text-align:right; z-index:5; }
.tv-chain-table thead th.strike-col { text-align:center; }
.tv-chain-table thead th.call-hdr { color:#26A69A99; }
.tv-chain-table thead th.put-hdr { color:#EF535099; }
.tv-chain-table td { padding:5px; border-bottom:1px solid #2A2E3944; text-align:right; white-space:nowrap; }
.tv-chain-table td.strike-col { text-align:center; font-weight:700; color:#D1D4DC; background:#1A1E27; }
.tv-chain-row:hover td { background:#2A2E39; }
.tv-chain-row.atm td { background:#2196F314; }
.tv-chain-row.atm td.strike-col { background:#2196F32A; color:#2196F3; }
.tv-call-ltp { color:#26A69A; font-weight:600; }
.tv-put-ltp { color:#EF5350; font-weight:600; }
.tv-chg-up { color:#26A69A; }
.tv-chg-dn { color:#EF5350; }
.tv-oi-val { color:#D1D4DC; }
.tv-iv-val { color:#787B86; }
.tv-chain-footer { padding:8px 12px; border-top:1px solid #2A2E39; font-size:10px; color:#787B86; display:flex; justify-content:space-between; }
.tv-main { flex:1; display:flex; flex-direction:column; overflow:hidden; background:#131722; min-width:0; min-height:0; }
.tv-chart-toolbar { display:flex; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #2A2E39; background:#1E222D; }
.tv-symbol-name { font-size:15px; font-weight:700; color:#D1D4DC; }
.tv-price-live { font-size:14px; font-weight:600; color:#26A69A; }
.tv-tf-btn { background:none; border:none; color:#787B86; font-size:12px; padding:3px 7px; border-radius:3px; cursor:pointer; font-family:'Inter',sans-serif; transition:background .15s,color .15s; }
.tv-tf-btn:hover { background:#2A2E39; color:#D1D4DC; }
.tv-tf-btn.active { background:#2962FF; color:#fff; }
.tv-tf-divider { width:1px; height:16px; background:#2A2E39; margin:0 4px; }
.tv-chart-wrap { flex:1; overflow:hidden; min-height:0; }
.tv-stat-bar { display:flex; gap:20px; padding:7px 12px; border-top:1px solid #2A2E39; background:#1E222D; font-size:11px; flex-wrap:wrap; }
.tv-stat-item { display:flex; gap:5px; align-items:center; }
.tv-stat-label { color:#787B86; }
.tv-stat-val { color:#D1D4DC; font-weight:500; }
.tv-stat-val.up { color:#26A69A; }
.tv-stat-val.dn { color:#EF5350; }
.tv-stat-val.warn { color:#F59E0B; }
.tv-signal-panel { width:280px; min-width:280px; background:#1E222D; border-left:1px solid #2A2E39; display:flex; flex-direction:column; overflow-y:auto; }
.tv-panel-section { border-bottom:1px solid #2A2E39; padding:12px 14px; }
.tv-panel-section-title { font-size:10px; font-weight:600; letter-spacing:.1em; color:#787B86; text-transform:uppercase; margin-bottom:10px; }
.tv-verdict { display:flex; align-items:center; justify-content:space-between; padding:10px 14px; border-radius:6px; margin-bottom:8px; }
.tv-verdict-text { font-size:15px; font-weight:700; letter-spacing:.05em; }
.tv-verdict-dir { font-size:11px; font-weight:600; opacity:.8; }
.tv-score-row { display:flex; align-items:center; justify-content:space-between; margin-bottom:6px; }
.tv-score-big { font-size:28px; font-weight:700; }
.tv-score-label { font-size:10px; color:#787B86; }
.tv-bar-track { height:4px; background:#2A2E39; border-radius:2px; overflow:hidden; margin-bottom:12px; }
.tv-bar-fill { height:100%; border-radius:2px; transition:width .4s ease; }
.tv-check-row { display:flex; align-items:flex-start; gap:8px; padding:5px 0; border-bottom:1px solid #2A2E3944; }
.tv-check-icon { font-size:13px; margin-top:1px; flex-shrink:0; }
.tv-check-name { font-size:12px; color:#D1D4DC; line-height:1.3; }
.tv-check-sub { font-size:10px; color:#787B86; line-height:1.3; }
.tv-calc-result { display:flex; justify-content:space-between; background:#131722; border-radius:4px; padding:8px 10px; margin-top:6px; }
.tv-calc-col { display:flex; flex-direction:column; gap:2px; }
.tv-calc-key { font-size:10px; color:#787B86; }
.tv-calc-num { font-size:13px; font-weight:600; }
.tv-login-wrap { min-height:100vh; display:flex; align-items:center; justify-content:center; background:#131722; }
.tv-login-card { width:340px; background:#1E222D; border:1px solid #2A2E39; border-radius:8px; padding:32px 28px; display:flex; flex-direction:column; gap:14px; }
.tv-login-logo { font-size:18px; font-weight:700; color:#2196F3; letter-spacing:.08em; text-align:center; }
.tv-login-sub { font-size:12px; color:#787B86; text-align:center; margin-top:-8px; }
.tv-login-error { color:#EF5350; font-size:12px; text-align:center; min-height:16px; }
::-webkit-scrollbar { width:4px; }
::-webkit-scrollbar-track { background:#131722; }
::-webkit-scrollbar-thumb { background:#2A2E39; border-radius:2px; }
"""


# ─────────────────────────────────────────────────────────────────────────
# Candlestick chart builder
# ─────────────────────────────────────────────────────────────────────────
def make_candlestick() -> go.Figure:
    c = live_state["candles"]
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=c["times"], open=c["opens"], high=c["highs"], low=c["lows"], close=c["closes"],
        increasing=dict(line=dict(color="#26A69A", width=1.2),fillcolor="#26A69A"),
        decreasing=dict(line=dict(color="#EF5350", width=1.2),fillcolor="#EF5350"),
        name="",showlegend=False,
    ))
    vc = ["rgba(38,166,154,0.20)" if cl>=op else "rgba(239,83,80,0.20)"
          for op,cl in zip(c["opens"],c["closes"])]
    fig.add_trace(go.Bar(x=c["times"],y=c["volumes"],marker_color=vc,name="Vol",yaxis="y2",showlegend=False))
    fig.update_layout(
        template="plotly_dark",paper_bgcolor="#131722",plot_bgcolor="#131722",
        xaxis=dict(type="date",showgrid=True,gridcolor="#1F2430",tickfont=dict(size=10, color="#787B86"),rangeslider=dict(visible=False),showline=False,),
        xaxis_tickformat="%d %b\n%H:%M",
        yaxis=dict(showgrid=True,gridcolor="#1F2430",tickfont=dict(size=10,color="#787B86"),side="right",domain=[0.25,1.0]),
        yaxis2=dict(domain=[0,0.2],tickfont=dict(size=9,color="#787B86"),showgrid=False,side="right"),
        margin=dict(l=5, r=50, t=10, b=5), autosize=True, dragmode="pan",
        legend=dict(font=dict(size=10,color="#787B86"),bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(bgcolor="#1E222D",bordercolor="#2A2E39",font=dict(color="#D1D4DC",size=11),),
        hovermode="x unified",
        uirevision="live_chart",
        )
    return fig


# ─────────────────────────────────────────────────────────────────────────
# Login page
# ─────────────────────────────────────────────────────────────────────────
@ui.page("/")
def login_page():
    ui.add_head_html(f"<style>{TV_CSS}</style>")
    ui.add_head_html("""<style>
    .nicegui-input .q-field__control{background:#131722!important;border:1px solid #2A2E39;border-radius:4px;}
    .nicegui-input .q-field__native{color:#D1D4DC!important;font-family:"Inter",sans-serif;}
    .nicegui-input .q-field__label{color:#787B86!important;}
    </style>""")
    with ui.element("div").classes("tv-login-wrap"):
        with ui.element("div").classes("tv-login-card"):
            ui.html('<div class="tv-login-logo">Trading Assistant</div>')
            ui.html('<div class="tv-login-sub">Institutional Signal Engine &middot; NSE &middot; Nifty 50</div>')
            pwd = ui.input("Access Key",password=True,password_toggle_button=True).classes("w-full nicegui-input").props("outlined dark color=blue-5")
            err = ui.label("").classes("tv-login-error")
            def do_login():
                if pwd.value == PASSWORD:
                    # per-browser-session flag instead of a shared module
                    # global — otherwise one person logging in unlocks the
                    # dashboard for every visitor.
                    app.storage.user["logged_in"] = True
                    ui.navigate.to("/dashboard")
                else:
                    err.set_text("Incorrect access key")
                    pwd.set_value("")
            pwd.on("keydown.enter", do_login)
            ui.button("Unlock Dashboard",on_click=do_login).classes("w-full").style("background:#2196F3;color:white;border-radius:4px;font-weight:600;margin-top:4px;font-size:13px;").props("flat")


# ─────────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────────
@ui.page("/dashboard")
def dashboard():
    # check per-session storage instead of a shared global bool.
    if not app.storage.user.get("logged_in"):
        ui.navigate.to("/")
        return

    ui.dark_mode()
    ui.add_head_html(f"<style>{TV_CSS}</style>")
    ui.add_head_html("""<style>
    html,body{height:100%!important;overflow:hidden!important;margin:0!important;padding:0!important;}
    .q-page-container,.q-layout,.q-page{height:100%!important;min-height:unset!important;padding:0!important;}
    .nicegui-content{padding:0!important;height:100%!important;}
    </style>""")

    init_live_state()

    expiries = get_expiries()  # all available expiry dates, ascending
    state = {
        "bar_id":      0,
        "tf":          DEFAULT_TF,
        "option_type": "ALL",
        "conviction":  None,
        "expiries":        expiries,
        "selected_expiry": expiries[0] if expiries else None,  # None = provider's nearest-expiry default
        "chain_rows":  make_option_chain(live_state["spot"] or 24892, expiries[0] if expiries else None),
        "position":    None,  # open position tracked for risk.manage_trade()'s lifecycle; see refresh_position_panel()
    }
    refs = {}

    # ── signal panel ────────────────────────────────────────────────────
    def refresh_signal_panel():
        conv = compute_conviction(SYMBOL, state["bar_id"])
        state["conviction"] = conv
        c  = conv["verdict_color"]
        bg = {"#26A69A":"#26A69A22","#F59E0B":"#F59E0B22","#EF5350":"#EF535022"}.get(c,"#2A2E3944")
        bd = {"#26A69A":"#26A69A55","#F59E0B":"#F59E0B55","#EF5350":"#EF535055"}.get(c,"#2A2E3988")
        chop_tag = ' <span style="opacity:.7;font-size:10px;">(CHOP)</span>' if conv["chop"] else ""

        opt = state["option_type"]
        disp_dir = "LONG" if opt=="CALL" else "SHORT" if opt=="PUT" else conv["direction"]

        refs["verdict_div"].set_content(
            f'<div class="tv-verdict" style="background:{bg};border:1px solid {bd}">'
            f'<span class="tv-verdict-text" style="color:{c}">{conv["verdict"]}{chop_tag}</span>'
            f'<span class="tv-verdict-dir" style="color:{c}">&#9679; {disp_dir}</span></div>'
        )
        bc = "#26A69A" if conv["score"]>=TRADE_THRESHOLD else "#F59E0B" if conv["score"]>=WATCH_THRESHOLD else "#EF5350"
        tt = "READY TO TRADE" if (conv["score"]>=TRADE_THRESHOLD and not conv["chop"]) else f'{max(TRADE_THRESHOLD-conv["score"],0):.0f} pts to TRADE'
        refs["score_div"].set_content(
            f'<div class="tv-score-row"><span class="tv-score-big" style="color:{bc}">{conv["score"]}</span>'
            f'<div><div class="tv-score-label">/ 100</div><div class="tv-score-label" style="color:{bc}">{tt}</div></div></div>'
        )
        refs["progress_div"].set_content(
            f'<div class="tv-bar-track"><div class="tv-bar-fill" style="width:{conv["score"]}%;background:{bc}"></div></div>'
        )
        refs["checklist_box"].clear()
        with refs["checklist_box"]:
            for label,sub,passed in conv["reasons"]:
                icon,ic,nc = ("\u2713","#26A69A","#D1D4DC") if passed else ("\u2717","#EF5350","#787B86")
                ui.html(f'<div class="tv-check-row"><span class="tv-check-icon" style="color:{ic}">{icon}</span>'
                        f'<div><div class="tv-check-name" style="color:{nc}">{label}</div>'
                        f'<div class="tv-check-sub">{sub}</div></div></div>')
        s = conv["raw"]
        bc2  = "up" if disp_dir=="LONG" else "dn" if disp_dir=="SHORT" else "warn"
        bt   = {"LONG":"Bullish","SHORT":"Bearish"}.get(disp_dir,"Mixed")
        fs   = "+" if s["fii_net"]>=0 else ""
        fc2  = "up" if s["fii_net"]>=0 else "dn"
        iw   = "warn" if s["iv_percentile"]>0.7 else "up"
        sc2  = "up" if s["sweep_confirmed"] else "dn"
        st2  = "Confirmed" if s["sweep_confirmed"] else "Not seen"
        ac   = "warn" if conv["chop"] else "up"
        ms   = "Open" if is_market_open() else "Closed"
        msc  = "up" if ms=="Open" else "dn"
        refs["stat_bar"].set_content(
            '<div class="tv-stat-bar">'
            f'<div class="tv-stat-item"><span class="tv-stat-label">Bias</span><span class="tv-stat-val {bc2}">{bt}</span></div>'
            f'<div class="tv-stat-item"><span class="tv-stat-label">VP</span><span class="tv-stat-val warn">Near {s["near_poc_or_val"]}</span></div>'
            f'<div class="tv-stat-item"><span class="tv-stat-label">ADX</span><span class="tv-stat-val {ac}">{s["adx"]:.0f}</span></div>'
            f'<div class="tv-stat-item"><span class="tv-stat-label">Sweep</span><span class="tv-stat-val {sc2}">{st2}</span></div>'
            f'<div class="tv-stat-item"><span class="tv-stat-label">IV Pct</span><span class="tv-stat-val {iw}">{s["iv_percentile"]:.0%}</span></div>'
            f'<div class="tv-stat-item"><span class="tv-stat-label">FII</span><span class="tv-stat-val {fc2}">{fs}{s["fii_net"]:,.0f} Cr</span></div>'
            f'<div class="tv-stat-item"><span class="tv-stat-label">NSE</span><span class="tv-stat-val {msc}">{ms}</span></div>'
            '</div>'
        )

    # ── timeframe switching (actually reloads historical data) ───────────
    def select_tf(tf: str):
        state["tf"] = tf
        live_state["tf"] = tf
        live_state["last_bucket"] = None
        live_state["candles"] = fetch_candles_for_tf(tf)
        refs["plot"].figure = make_candlestick()
        refs["plot"].update()
        # update button active states
        for t in TF_MAP:
            cls = f"tv-tf-btn {'active' if t==tf else ''}"
            refs[f"tf_{t}"].set_content(f'<button class="{cls}" style="background:none;border:none;cursor:pointer;font-family:Inter,sans-serif;font-size:12px;padding:3px 7px;border-radius:3px;color:{"#fff" if t==tf else "#787B86"};{"background:#2962FF;" if t==tf else ""}">{t}</button>')

    # ── option chain ─────────────────────────────────────────────────────
    def _fmt_expiry_label(exp: str) -> str:
        try:
            return datetime.strptime(exp, "%Y-%m-%d").strftime("%d %b")
        except Exception:
            return exp

    def render_chain_and_tabs():
        refs["chain_table"].set_content(render_option_chain(state["chain_rows"], state["option_type"]))
        for code in ("ALL","CALL","PUT"):
            lab = {"ALL":"All","CALL":"Call (CE)","PUT":"Put (PE)"}[code]
            cls = f"tv-chain-tab tab-{code.lower()}" + (" active" if code==state["option_type"] else "")
            refs[f"tab_{code}"].set_content(f'<div class="{cls}">{lab}</div>')

        # Expiry row — one tappable pill per available expiry date. Both
        # calls and puts come from the same get_option_chain(expiry_date=...)
        # payload, so a single selector drives both sides of the table.
        refs["expiry_tabs_box"].clear()
        with refs["expiry_tabs_box"]:
            if not state["expiries"]:
                ui.html('<span style="font-size:10px;color:#787B86;padding:4px 0;">No expiries loaded</span>')
            for exp in state["expiries"]:
                active = exp == state["selected_expiry"]
                cls = "tv-expiry-tab active" if active else "tv-expiry-tab"
                btn = ui.html(f'<div class="{cls}">{_fmt_expiry_label(exp)}</div>')
                btn.on("click", lambda e=exp: select_expiry(e))

        spot = live_state["spot"] or 24892
        refs["chain_footer_spot"].set_content(f'<span>Spot {spot:,.1f}</span>')
        exp_label = _fmt_expiry_label(state["selected_expiry"]) if state["selected_expiry"] else "—"
        refs["sidebar_header"].set_content(
            f'<div class="tv-sidebar-header">Option Chain'
            f'<span class="tv-sidebar-sub">{SYMBOL} · {exp_label}</span></div>'
        )

    def select_option(code: str):
        state["option_type"] = code
        render_chain_and_tabs()
        refresh_signal_panel()

    def select_expiry(exp: str):
        state["selected_expiry"] = exp
        spot = live_state["spot"] or 24892
        state["chain_rows"] = make_option_chain(spot, exp)
        render_chain_and_tabs()
        refresh_signal_panel()

    def refresh_chain():
        """Refresh the option-chain sidebar from live provider data only,
        for whichever expiry is currently selected. While NSE is closed,
        still attempt a fetch (the provider will just keep returning the
        last traded closing quotes); if it fails, the panel keeps showing
        whatever it last had (or the empty-state message) rather than
        fabricating data."""
        spot = live_state["spot"] or 24892
        exp = state["selected_expiry"]

        if not is_market_open():
            fresh = _fetch_live_chain(spot, exp)
            if fresh:
                state["chain_rows"] = fresh
            render_chain_and_tabs()
            return

        current_atm = round(state["chain_rows"][len(state["chain_rows"])//2]["strike"]/STRIKE_STEP)*STRIKE_STEP if state["chain_rows"] else None
        new_atm = round(spot/STRIKE_STEP)*STRIKE_STEP
        if current_atm is None or new_atm != current_atm:
            state["chain_rows"] = make_option_chain(spot, exp)
        else:
            state["chain_rows"] = update_option_chain(state["chain_rows"], spot, exp)
        render_chain_and_tabs()

    # ── 2-second master refresh ──────────────────────────────────────────
    async def refresh_all():
        market_open = is_market_open()
        if market_open:
            await run.io_bound(update_live_candle)
        spot = live_state["spot"] or 24892
        refs["price_label"].set_content(f'<span class="tv-price-live">{spot:,.2f}</span>')
        new_fig = make_candlestick()
        if refs["plot"].figure.to_plotly_json() != new_fig.to_plotly_json():
            refs["plot"].figure = new_fig
            refs["plot"].update()
        if market_open:
            state["bar_id"] += 1
        refresh_signal_panel()
        refresh_chain()
        refresh_position_panel()
        # keep the API-status badge in the top bar honest, and specific
        # about *why* it's down when it is (auth expired vs network vs...)
        ok = live_state.get("api_ok", True)
        reason = live_state.get("api_reason")
        label = "API LIVE" if ok else {
            "auth_expired": "TOKEN EXPIRED",
            "no_token":     "NO TOKEN SET",
            "network_error":"NETWORK ERROR",
        }.get(reason, "API DOWN · MOCK")
        refs["api_badge"].set_content(
            f'<span class="tv-api-badge {"tv-api-ok" if ok else "tv-api-down"}">{label}</span>'
        )

    ui.timer(2.0, refresh_all)

    # ── trade calculator (Phase 4: reads strategy/risk's real numbers
    #    when the live pipeline produced them; falls back to the old
    #    0.3%-of-price placeholder only when it didn't, e.g. the mock
    #    fallback engine is active) ─────────────────────────────────────
    def do_calc():
        ev = refs["entry"].value
        if not ev:
            return
        conv = state.get("conviction")
        if conv and conv.get("chop"):
            refs["target_lbl"].set_content('<div style="font-size:11px;color:#EF5350;padding:4px 0">Market is choppy — trade not recommended.</div>')
            refs["sl_lbl"].set_content("<div></div>")
            refs["rr_lbl"].set_content("<div></div>")
            return
        if conv and conv.get("direction")=="-":
            refs["target_lbl"].set_content('<div style="font-size:11px;color:#EF5350;padding:4px 0">No directional consensus — signals split.</div>')
            refs["sl_lbl"].set_content("<div></div>")
            refs["rr_lbl"].set_content("<div></div>")
            return

        opt = state["option_type"]
        risk_plan = conv.get("risk_plan") if conv else None
        trade_levels = conv.get("trade_levels") if conv else None
        score = conv["score"] if conv else "—"

        if risk_plan and trade_levels:
            # Real numbers from strategy.compute_trade_levels() + risk.py's
            # fixed-target plan — direction comes from the actual signal,
            # not the option-type toggle (a CALL/PUT filter selection
            # shouldn't override what the engine actually detected).
            direction = trade_levels["direction"]
            tgt1, tgt2 = risk_plan["target_1"], risk_plan["target_2"]
            sl = trade_levels["stop_loss"]
            rr1, rr2 = risk_plan["target_1_rr"], risk_plan["target_2_rr"]
            tc, sc = ("#26A69A", "#EF5350") if direction == "long" else ("#EF5350", "#26A69A")
            refs["target_lbl"].set_content(
                f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span style="color:#787B86">Target 1 (50pt)</span><span style="color:{tc};font-weight:600">{tgt1}</span></div>'
                f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span style="color:#787B86">Target 2 (100pt)</span><span style="color:{tc};font-weight:600">{tgt2}</span></div>'
            )
            refs["sl_lbl"].set_content(f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span style="color:#787B86">Stop Loss</span><span style="color:{sc};font-weight:600">{sl}</span></div>')
            refs["rr_lbl"].set_content(
                f'<div style="font-size:10px;color:#787B86;margin-top:4px;padding:6px 8px;background:#131722;border-radius:4px;">'
                f'R:R to T1 {rr1}:1 &middot; T2 {rr2}:1 &middot; Lots {risk_plan["lots"]} &middot; Conviction {score}%</div>'
            )
        else:
            # Fallback: mock engine active, no real trade_levels/risk_plan
            # available — keep the original placeholder math so the
            # calculator still works, just without pretending it's the
            # real strategy's numbers.
            direction = "LONG" if opt=="CALL" else "SHORT" if opt=="PUT" else (conv["direction"] if conv else "LONG")
            risk_pts = round(float(ev)*0.003, 1)
            if direction in ("SHORT", "short"):
                tgt,sl,tc,sc = round(float(ev)-3*risk_pts,1), round(float(ev)+risk_pts,1), "#EF5350","#26A69A"
            else:
                tgt,sl,tc,sc = round(float(ev)+3*risk_pts,1), round(float(ev)-risk_pts,1), "#26A69A","#EF5350"
            refs["target_lbl"].set_content(f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span style="color:#787B86">Target</span><span style="color:{tc};font-weight:600">{tgt}</span></div>')
            refs["sl_lbl"].set_content(f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0"><span style="color:#787B86">Stop Loss</span><span style="color:{sc};font-weight:600">{sl}</span></div>')
            refs["rr_lbl"].set_content(f'<div style="font-size:10px;color:#787B86;margin-top:4px;padding:6px 8px;background:#131722;border-radius:4px;">Risk {risk_pts} pts &middot; \u20b9{risk_pts*NIFTY_LOT:,.0f}/lot (lot={NIFTY_LOT}) &middot; 1:3 R:R (placeholder) &middot; Conviction {score}%</div>')

    # ── position manager (Phase 4b: tracks an OPEN position through
    #    risk.manage_trade()'s T1/T2/theta lifecycle — separate from the
    #    pre-entry calculator above, which only plans a trade that hasn't
    #    been taken yet) ───────────────────────────────────────────────
    def enter_position():
        conv = state.get("conviction")
        trade_levels = conv.get("trade_levels") if conv else None
        if not trade_levels:
            refs["position_box"].clear()
            with refs["position_box"]:
                ui.html('<div style="font-size:11px;color:#EF5350;padding:4px 0">No active trade signal to enter.</div>')
            return
        state["position"] = {
            "entry": trade_levels["entry"],
            "stop_loss": trade_levels["stop_loss"],
            "direction": trade_levels["direction"],
            "entry_time": datetime.now(IST),
            "target_1_hit": False,
            "target_2_hit": False,
            "stop_hit": False,
        }
        refresh_position_panel()

    def close_position():
        state["position"] = None
        refresh_position_panel()

    def refresh_position_panel():
        pos = state.get("position")
        refs["position_box"].clear()
        with refs["position_box"]:
            if not pos:
                conv = state.get("conviction")
                can_enter = bool(conv and conv.get("trade_levels") and conv.get("verdict") == "TRADE")
                ui.html('<div style="font-size:11px;color:#787B86;padding:4px 0">No open position.</div>')
                btn = ui.button("Enter Trade (from current signal)", on_click=enter_position).classes("w-full")
                btn.style("background:#2196F3!important;color:#fff!important;border-radius:4px;font-weight:600;font-size:11px;margin-top:4px;")
                if not can_enter:
                    btn.props("disable")
                return

            spot = live_state["spot"] or pos["entry"]
            plan = risk.manage_trade(
                pos["entry"], pos["direction"], pos["entry_time"],
                now=datetime.now(IST),
                target_1_hit=pos["target_1_hit"], target_2_hit=pos["target_2_hit"], stop_hit=pos["stop_hit"],
                current_price=spot, stop_loss=pos["stop_loss"],
            )
            # persist any auto-detected hits back onto the tracked position
            # so re-entering MANAGE state doesn't keep re-deriving from
            # scratch each 2s tick in a way that could flap
            pos["target_1_hit"] = plan["target_1_hit"]
            pos["target_2_hit"] = plan["target_2_hit"]
            pos["stop_hit"] = plan["stop_hit"]

            action_color = {"EXIT": "#EF5350", "MANAGE": "#F59E0B", "HOLD": "#26A69A"}.get(plan["action"], "#787B86")
            dir_label = "LONG" if pos["direction"] == "long" else "SHORT"

            ui.html(
                f'<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;">'
                f'<span style="font-size:12px;color:#D1D4DC;font-weight:600;">{dir_label} @ {pos["entry"]}</span>'
                f'<span style="font-size:11px;font-weight:700;color:{action_color};">{plan["action"]}</span></div>'
            )
            ui.html(f'<div style="font-size:10px;color:#787B86;padding:2px 0 6px;">{plan["reason"]}</div>')
            ui.html(
                f'<div class="tv-calc-result"><div class="tv-calc-col"><span class="tv-calc-key">Remaining Lots</span>'
                f'<span class="tv-calc-num">{plan["remaining_lots"]}/{plan["lots"]}</span></div>'
                f'<div class="tv-calc-col"><span class="tv-calc-key">Trading Days Held</span>'
                f'<span class="tv-calc-num">{plan["trading_days_held"]}/{plan["max_holding_days"]}</span></div>'
                f'<div class="tv-calc-col"><span class="tv-calc-key">Theta Risk</span>'
                f'<span class="tv-calc-num" style="color:{"#EF5350" if plan["theta_risk"]=="HIGH" else "#F59E0B" if plan["theta_risk"]=="ELEVATED" else "#26A69A"}">{plan["theta_risk"]}</span></div></div>'
            )
            if plan["suggested_stop_loss"] is not None:
                ui.html(f'<div style="font-size:10px;color:#787B86;padding:6px 0 0;">Suggested stop: <span style="color:#D1D4DC;font-weight:600;">{plan["suggested_stop_loss"]}</span></div>')

            btn = ui.button("Close Position", on_click=close_position).classes("w-full")
            btn.style("background:#EF5350!important;color:#fff!important;border-radius:4px;font-weight:600;font-size:11px;margin-top:8px;")

    # ── TOP BAR ──────────────────────────────────────────────────────────
    with ui.element("div").classes("tv-topbar"):
        ui.html('<span class="tv-logo">Trading Assistant &middot; NSE</span>')
        with ui.row().style("align-items:center;gap:20px;"):
            spot0 = live_state["spot"] or 24892
            ui.html(f'<div class="tv-ticker-item"><span class="tv-ticker-name">{SYMBOL}</span>'
                    f'<span class="tv-ticker-price">{spot0:,.2f}</span>'
                    f'<span class="tv-ticker-change-up">&#9650; LIVE</span></div>')
            refs["api_badge"] = ui.html(
                f'<span class="tv-api-badge {"tv-api-ok" if live_state.get("api_ok", True) else "tv-api-down"}">'
                f'{"API LIVE" if live_state.get("api_ok", True) else "API DOWN · MOCK"}</span>'
            )
            clock = ui.html(f'<span class="tv-time">{datetime.now(IST).strftime("%H:%M:%S")} IST</span>')
            ui.timer(1.0, lambda: clock.set_content(f'<span class="tv-time">{datetime.now(IST).strftime("%H:%M:%S")} IST</span>'))

    # ── LAYOUT ───────────────────────────────────────────────────────────
    with ui.element("div").classes("tv-layout"):

        # LEFT — option chain
        with ui.element("div").classes("tv-sidebar"):
            refs["sidebar_header"] = ui.html(
                f'<div class="tv-sidebar-header">Option Chain'
                f'<span class="tv-sidebar-sub">{SYMBOL} · '
                f'{_fmt_expiry_label(state["selected_expiry"]) if state["selected_expiry"] else "—"}</span></div>'
            )
            # Expiry-date row — same call/put payload just re-fetched for
            # whichever expiry is selected here (see select_expiry above).
            refs["expiry_tabs_box"] = ui.row().classes("tv-expiry-tabs").style("gap:6px;flex-wrap:nowrap;")
            with refs["expiry_tabs_box"]:
                for exp in state["expiries"]:
                    active = exp == state["selected_expiry"]
                    cls = "tv-expiry-tab active" if active else "tv-expiry-tab"
                    btn = ui.html(f'<div class="{cls}">{_fmt_expiry_label(exp)}</div>')
                    btn.on("click", lambda e=exp: select_expiry(e))
            with ui.element("div").classes("tv-chain-tabs"):
                for code in ("ALL","CALL","PUT"):
                    lab = {"ALL":"All","CALL":"Call (CE)","PUT":"Put (PE)"}[code]
                    cls = f"tv-chain-tab tab-{code.lower()}" + (" active" if code==state["option_type"] else "")
                    tab = ui.html(f'<div class="{cls}">{lab}</div>')
                    tab.on("click", lambda c=code: select_option(c))
                    refs[f"tab_{code}"] = tab
            with ui.element("div").classes("tv-chain-scroll"):
                refs["chain_table"] = ui.html(render_option_chain(state["chain_rows"], state["option_type"]))
            with ui.element("div").classes("tv-chain-footer"):
                refs["chain_footer_spot"] = ui.html(f'<span>Spot {spot0:,.1f}</span>')
                rb = ui.html('<span style="cursor:pointer;color:#2196F3">\u21bb Refresh</span>')
                rb.on("click", refresh_chain)

        # RESIZE HANDLE
        ui.html('<div class="tv-resize-handle" id="tvr"></div>')
        ui.add_body_html("""<script>
        (function(){
          function init(){
            var h=document.getElementById("tvr"),p=document.querySelector(".tv-sidebar");
            if(!h||!p){setTimeout(init,150);return;}
            var drag=false,sx=0,sw=0;
            h.addEventListener("mousedown",function(e){drag=true;sx=e.clientX;sw=p.getBoundingClientRect().width;h.classList.add("dragging");document.body.style.userSelect="none";document.body.style.cursor="col-resize";e.preventDefault();});
            document.addEventListener("mousemove",function(e){if(!drag)return;p.style.width=Math.min(760,Math.max(260,sw+e.clientX-sx))+"px";});
            document.addEventListener("mouseup",function(){if(!drag)return;drag=false;h.classList.remove("dragging");document.body.style.userSelect="";document.body.style.cursor="";});
            h.addEventListener("dblclick",function(){p.style.width="420px";});
          }init();
        })();
        </script>""")

        # CENTRE — chart
        with ui.element("div").classes("tv-main"):
            with ui.element("div").classes("tv-chart-toolbar"):
                ui.html(f'<span class="tv-symbol-name">{SYMBOL}</span>')
                refs["price_label"] = ui.html(f'<span class="tv-price-live">{spot0:,.2f}</span>')
                ui.html('<div class="tv-tf-divider"></div>')
                for tf in TF_MAP:
                    btn = ui.html(f'<button class="tv-tf-btn {"active" if tf==DEFAULT_TF else ""}" '
                                  f'style="background:none;border:none;cursor:pointer;font-family:Inter,sans-serif;font-size:12px;padding:3px 7px;border-radius:3px;'
                                  f'color:{"#fff" if tf==DEFAULT_TF else "#787B86"};{"background:#2962FF;" if tf==DEFAULT_TF else ""}">{tf}</button>')
                    btn.on("click", lambda t=tf: select_tf(t))
                    refs[f"tf_{tf}"] = btn

            with ui.element("div").classes("tv-chart-wrap"):
                refs["plot"] = ui.plotly(make_candlestick()).style("width:100%;height:100%")

            refs["stat_bar"] = ui.html('<div class="tv-stat-bar"><div class="tv-stat-item"><span class="tv-stat-label">Loading...</span></div></div>')

        # RIGHT — signal panel
        with ui.element("div").classes("tv-signal-panel"):
            with ui.element("div").classes("tv-panel-section"):
                ui.html('<div class="tv-panel-section-title">Signal Engine</div>')
                refs["verdict_div"] = ui.html('<div class="tv-verdict" style="background:#26A69A22;border:1px solid #26A69A44"><span class="tv-verdict-text" style="color:#26A69A">&mdash;</span><span class="tv-verdict-dir" style="color:#26A69A">&mdash;</span></div>')

            with ui.element("div").classes("tv-panel-section"):
                ui.html('<div class="tv-panel-section-title">Conviction Score</div>')
                refs["score_div"]    = ui.html('<div class="tv-score-row"><span class="tv-score-big" style="color:#787B86">&mdash;</span><div><div class="tv-score-label">/ 100</div></div></div>')
                refs["progress_div"] = ui.html('<div class="tv-bar-track"><div class="tv-bar-fill" style="width:0%;background:#787B86"></div></div>')
                ui.button("\u21bb  Recalculate", on_click=refresh_signal_panel).classes("w-full").style("background:#2196F3!important;color:#fff!important;border-radius:4px;font-weight:600;font-size:12px;margin-top:6px;")

            with ui.element("div").classes("tv-panel-section"):
                ui.html('<div class="tv-panel-section-title">Trade Protocols</div>')
                refs["checklist_box"] = ui.column().style("gap:0;width:100%")

            with ui.element("div").classes("tv-panel-section"):
                ui.html('<div class="tv-panel-section-title">Trade Calculator</div>')
                refs["entry"] = ui.number(label="Entry Price",placeholder="e.g. 24850").classes("w-full").props("outlined dark color=blue-5 dense")
                refs["entry"].style("margin-bottom:6px")
                refs["target_lbl"] = ui.html('<div style="font-size:12px;color:#787B86;padding:3px 0">Target: &mdash;</div>')
                refs["sl_lbl"]     = ui.html('<div style="font-size:12px;color:#787B86;padding:3px 0">Stop Loss: &mdash;</div>')
                refs["rr_lbl"]     = ui.html('<div></div>')
                ui.button("Calculate Levels", on_click=do_calc).classes("w-full").style("background:#2196F3!important;color:#fff!important;border-radius:4px;font-weight:600;font-size:12px;margin-top:6px;")

            with ui.element("div").classes("tv-panel-section"):
                ui.html('<div class="tv-panel-section-title">Position Manager</div>')
                refs["position_box"] = ui.column().style("gap:0;width:100%")

    refresh_signal_panel()
    refresh_position_panel()


ui.run(
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8080)),
    storage_secret=os.getenv("STORAGE_SECRET", "change-me-in-private-env"),
)
