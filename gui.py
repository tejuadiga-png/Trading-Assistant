from nicegui import ui, app
import plotly.graph_objects as go
import random
import os

logged_in = False
PASSWORD = 'model2026'

# ---------------------------------------------------------------------------
# CONVICTION ENGINE
# ---------------------------------------------------------------------------

WEIGHTS = {
    "trend": 18, "liquidity_sweep": 15, "volume_profile": 15,
    "adx_regime": 12, "order_flow": 12, "flow_fii_dii": 14,
    "vol_structure": 8, "breadth": 6,
}
TOTAL_WEIGHT = sum(WEIGHTS.values())
TRADE_THRESHOLD = 80
WATCH_THRESHOLD = 60


def mock_market_state(symbol: str) -> dict:
    random.seed(hash(symbol) % 1000 + random.randint(0, 5))
    return {
        "trend_aligned": random.random() > 0.3,
        "sweep_confirmed": random.random() > 0.35,
        "near_poc_or_val": random.choice(["VAL", "VAH", "POC", "mid-range"]),
        "adx": random.uniform(15, 38),
        "order_flow_buy_fraction": random.uniform(0.35, 0.92),
        "fii_net": random.uniform(-3000, 3500),
        "dii_net": random.uniform(-2000, 2500),
        "iv_percentile": random.uniform(0.1, 0.95),
        "term_structure_healthy": random.random() > 0.3,
        "banknifty_confirms": random.random() > 0.25,
    }


def compute_conviction(symbol: str) -> dict:
    s = mock_market_state(symbol)
    reasons = []

    trend_score = 1.0 if s["trend_aligned"] else 0.2
    reasons.append(("Trend Filter", "Daily/15m/5m aligned", s["trend_aligned"]))

    sweep_score = 1.0 if s["sweep_confirmed"] else 0.25
    reasons.append(("Liquidity Sweep", "Sweep + reclaim confirmed", s["sweep_confirmed"]))

    vp_score = 0.9 if s["near_poc_or_val"] in ("VAL", "POC") else 0.4
    reasons.append((f"Volume Profile", f"Near {s['near_poc_or_val']}", vp_score > 0.5))

    adx_score = min(s["adx"] / 30, 1.0)
    reasons.append(("ADX Regime", f"Strength {s['adx']:.0f}", s["adx"] >= 20))

    of_score = s["order_flow_buy_fraction"]
    reasons.append(("Order Flow", f"Buy-fraction {of_score:.2f}", of_score >= 0.55))

    flow_combined = (s["fii_net"] + 0.5 * s["dii_net"] + 4000) / 8000
    flow_score = max(0, min(flow_combined, 1))
    reasons.append(("FII/DII Flow", "Net institutional support", flow_score > 0.55))

    vol_score = (0 if s["iv_percentile"] > 0.85 else (1 - s["iv_percentile"])) * (1 if s["term_structure_healthy"] else 0.5)
    reasons.append(("Volatility", f"IV pct {s['iv_percentile']:.0%}", vol_score > 0.4))

    # Label changes based on symbol
    breadth_label = "Nifty Breadth" if symbol == "BANK NIFTY" else "Bank Nifty Breadth"
    breadth_sub   = "Nifty confirms signal" if symbol == "BANK NIFTY" else "Index confirms signal"
    breadth_score = 0.9 if s["banknifty_confirms"] else 0.2
    reasons.append((breadth_label, breadth_sub, s["banknifty_confirms"]))

    weighted = (
        trend_score * WEIGHTS["trend"] + sweep_score * WEIGHTS["liquidity_sweep"] +
        vp_score * WEIGHTS["volume_profile"] + adx_score * WEIGHTS["adx_regime"] +
        of_score * WEIGHTS["order_flow"] + flow_score * WEIGHTS["flow_fii_dii"] +
        vol_score * WEIGHTS["vol_structure"] + breadth_score * WEIGHTS["breadth"]
    )
    score = round((weighted / TOTAL_WEIGHT) * 100, 1)

    if score >= TRADE_THRESHOLD:
        verdict, verdict_color = "TRADE", "#26A69A"
    elif score >= WATCH_THRESHOLD:
        verdict, verdict_color = "WATCH", "#F59E0B"
    else:
        verdict, verdict_color = "NO TRADE", "#EF5350"

    direction = "LONG" if s["trend_aligned"] and s["order_flow_buy_fraction"] >= 0.5 else "SHORT"

    return {
        "score": score, "verdict": verdict, "verdict_color": verdict_color,
        "direction": direction, "reasons": reasons, "raw": s,
    }


# ---------------------------------------------------------------------------
# SHARED STYLES
# ---------------------------------------------------------------------------

TV_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* { box-sizing: border-box; }

body {
    background: #131722;
    font-family: 'Inter', -apple-system, sans-serif;
    color: #D1D4DC;
    margin: 0;
    padding: 0;
    height: 100vh;
    overflow: hidden;
}

html {
    height: 100%;
    overflow: hidden;
}

.q-page, .q-page-container, .nicegui-content, body > div {
    height: 100% !important;
    min-height: unset !important;
    padding: 0 !important;
    margin: 0 !important;
}

.tv-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #1E222D;
    border-bottom: 1px solid #2A2E39;
    padding: 0 16px;
    height: 48px;
    width: 100%;
    position: sticky;
    top: 0;
    z-index: 100;
    box-sizing: border-box;
}
.tv-logo {
    font-size: 14px;
    font-weight: 700;
    color: #2196F3;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
.tv-ticker-strip {
    display: flex;
    gap: 24px;
    font-size: 12px;
}
.tv-ticker-item { display: flex; flex-direction: column; align-items: flex-end; }
.tv-ticker-name { color: #787B86; font-size: 10px; }
.tv-ticker-price { color: #D1D4DC; font-weight: 600; }
.tv-ticker-change-up { color: #26A69A; font-size: 10px; }
.tv-ticker-change-dn { color: #EF5350; font-size: 10px; }
.tv-time { color: #787B86; font-size: 12px; }

.tv-layout {
    display: flex;
    height: calc(100vh - 48px);
    overflow: hidden;
    width: 100%;
}

.tv-sidebar {
    width: 220px;
    min-width: 220px;
    background: #1E222D;
    border-right: 1px solid #2A2E39;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.tv-sidebar-header {
    padding: 10px 12px 8px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.1em;
    color: #787B86;
    text-transform: uppercase;
    border-bottom: 1px solid #2A2E39;
}
.tv-watchlist { flex: 1; overflow-y: auto; }
.tv-watch-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border-bottom: 1px solid #2A2E39;
    cursor: pointer;
    transition: background 0.15s;
    user-select: none;
}
.tv-watch-item:hover { background: #2A2E39; }
.tv-watch-item.active { background: #2962FF1A; border-left: 2px solid #2196F3; }
.tv-watch-name { font-size: 13px; font-weight: 500; color: #D1D4DC; }
.tv-watch-price { font-size: 13px; font-weight: 600; color: #D1D4DC; }
.tv-watch-chg-up { font-size: 11px; color: #26A69A; }
.tv-watch-chg-dn { font-size: 11px; color: #EF5350; }
.tv-watch-sub { font-size: 10px; color: #787B86; }

.tv-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: #131722;
    min-width: 0;
    min-height: 0;
}
.tv-chart-toolbar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border-bottom: 1px solid #2A2E39;
    background: #1E222D;
}
.tv-symbol-name {
    font-size: 15px;
    font-weight: 700;
    color: #D1D4DC;
}
.tv-price-live {
    font-size: 14px;
    font-weight: 600;
    color: #26A69A;
}
.tv-tf-btn {
    background: none;
    border: none;
    color: #787B86;
    font-size: 12px;
    padding: 3px 7px;
    border-radius: 3px;
    cursor: pointer;
    font-family: 'Inter', sans-serif;
    transition: background 0.15s, color 0.15s;
}
.tv-tf-btn:hover { background: #2A2E39; color: #D1D4DC; }
.tv-tf-btn.active { background: #2962FF; color: #fff; }
.tv-tf-divider { width: 1px; height: 16px; background: #2A2E39; margin: 0 4px; }

.tv-chart-wrap { flex: 1; overflow: hidden; min-height: 0; }

.tv-stat-bar {
    display: flex;
    gap: 24px;
    padding: 8px 12px;
    border-top: 1px solid #2A2E39;
    background: #1E222D;
    font-size: 11px;
}
.tv-stat-item { display: flex; gap: 6px; align-items: center; }
.tv-stat-label { color: #787B86; }
.tv-stat-val { color: #D1D4DC; font-weight: 500; }
.tv-stat-val.up { color: #26A69A; }
.tv-stat-val.dn { color: #EF5350; }
.tv-stat-val.warn { color: #F59E0B; }

.tv-signal-panel {
    width: 280px;
    min-width: 280px;
    background: #1E222D;
    border-left: 1px solid #2A2E39;
    display: flex;
    flex-direction: column;
    overflow-y: auto;
}
.tv-panel-section {
    border-bottom: 1px solid #2A2E39;
    padding: 12px 14px;
}
.tv-panel-section-title {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.1em;
    color: #787B86;
    text-transform: uppercase;
    margin-bottom: 10px;
}

.tv-verdict {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    border-radius: 6px;
    margin-bottom: 8px;
}
.tv-verdict-text { font-size: 15px; font-weight: 700; letter-spacing: 0.05em; }
.tv-verdict-dir { font-size: 11px; font-weight: 600; opacity: 0.8; }

.tv-score-row { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.tv-score-big { font-size: 28px; font-weight: 700; }
.tv-score-label { font-size: 10px; color: #787B86; }

.tv-bar-track {
    height: 4px;
    background: #2A2E39;
    border-radius: 2px;
    overflow: hidden;
    margin-bottom: 12px;
}
.tv-bar-fill { height: 100%; border-radius: 2px; transition: width 0.4s ease; }

.tv-check-row {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 5px 0;
    border-bottom: 1px solid #2A2E3944;
}
.tv-check-icon { font-size: 13px; margin-top: 1px; flex-shrink: 0; }
.tv-check-name { font-size: 12px; color: #D1D4DC; line-height: 1.3; }
.tv-check-sub { font-size: 10px; color: #787B86; line-height: 1.3; }

.tv-input-row { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; }
.tv-input-label { font-size: 10px; color: #787B86; }
.tv-calc-result {
    display: flex;
    justify-content: space-between;
    background: #131722;
    border-radius: 4px;
    padding: 8px 10px;
    margin-top: 6px;
}
.tv-calc-col { display: flex; flex-direction: column; gap: 2px; }
.tv-calc-key { font-size: 10px; color: #787B86; }
.tv-calc-num { font-size: 13px; font-weight: 600; }

.tv-login-wrap {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #131722;
}
.tv-login-card {
    width: 340px;
    background: #1E222D;
    border: 1px solid #2A2E39;
    border-radius: 8px;
    padding: 32px 28px;
    display: flex;
    flex-direction: column;
    gap: 14px;
}
.tv-login-logo { font-size: 18px; font-weight: 700; color: #2196F3; letter-spacing: 0.08em; text-align: center; }
.tv-login-sub { font-size: 12px; color: #787B86; text-align: center; margin-top: -8px; }
.tv-login-input {
    width: 100%;
    background: #131722;
    border: 1px solid #2A2E39;
    border-radius: 4px;
    color: #D1D4DC;
    padding: 9px 12px;
    font-size: 13px;
    font-family: 'Inter', sans-serif;
    outline: none;
    transition: border-color 0.15s;
}
.tv-login-input:focus { border-color: #2196F3; }
.tv-login-error { color: #EF5350; font-size: 12px; text-align: center; min-height: 16px; }

::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: #131722; }
::-webkit-scrollbar-thumb { background: #2A2E39; border-radius: 2px; }
"""


# ---------------------------------------------------------------------------
# LOGIN PAGE
# ---------------------------------------------------------------------------

@ui.page('/')
def login_page():
    ui.add_head_html(f'<style>{TV_CSS}</style>')
    ui.add_head_html('''<style>
    .tv-login-wrap { min-height:100vh; display:flex; align-items:center; justify-content:center; }
    .tv-login-card { width:340px; background:#1E222D; border:1px solid #2A2E39; border-radius:8px; padding:32px 28px; }
    .nicegui-input .q-field__control { background:#131722 !important; border:1px solid #2A2E39; border-radius:4px; }
    .nicegui-input .q-field__native { color:#D1D4DC !important; font-family:"Inter",sans-serif; }
    .nicegui-input .q-field__label { color:#787B86 !important; }
    </style>''')

    with ui.element('div').classes('tv-login-wrap'):
        with ui.element('div').classes('tv-login-card'):
            ui.html('<div class="tv-login-logo" style="font-size:18px;font-weight:700;color:#2196F3;letter-spacing:0.08em;text-align:center;margin-bottom:4px;">Trading Assistant</div>')
            ui.html('<div style="font-size:12px;color:#787B86;text-align:center;margin-bottom:20px;">Institutional Signal Engine · NSE</div>')

            pwd = ui.input('Access Key', password=True, password_toggle_button=True) \
                    .classes('w-full nicegui-input') \
                    .props('outlined dark color=blue-5')

            err = ui.label('').style('color:#EF5350;font-size:12px;min-height:16px;text-align:center')

            def do_login():
                global logged_in
                if pwd.value == PASSWORD:
                    logged_in = True
                    ui.navigate.to('/dashboard')
                else:
                    err.set_text('Incorrect access key')
                    pwd.set_value('')

            pwd.on('keydown.enter', do_login)

            ui.button('Unlock Dashboard', on_click=do_login) \
              .classes('w-full') \
              .style('background:#2196F3;color:white;border-radius:4px;font-weight:600;margin-top:12px;font-size:13px;') \
              .props('flat')


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------

# Watchlist data — single source of truth
WATCHLIST = [
    ('NIFTY 50',   '24,892', '+0.41%', True,  '24,790', '25,016'),
    ('BANK NIFTY', '48,432', '-0.31%', False, '48,102', '48,680'),
]

PRICES = {
    'NIFTY 50':   ('24,892', '▲ +0.41%', '#26A69A'),
    'BANK NIFTY': ('48,432', '▼ -0.31%', '#EF5350'),
}

CHART_DATA = {
    'NIFTY 50': {
        'opens':  [24800, 24840, 24820, 24860, 24850, 24890, 24870, 24910, 24930, 24900, 24950, 24980],
        'highs':  [24860, 24870, 24880, 24900, 24910, 24940, 24920, 24960, 24970, 24970, 24990, 25020],
        'lows':   [24790, 24800, 24810, 24830, 24840, 24870, 24850, 24890, 24910, 24880, 24930, 24960],
        'closes': [24840, 24820, 24860, 24850, 24890, 24870, 24910, 24930, 24900, 24950, 24980, 25010],
        'title':  'NIFTY 50 · 5m',
    },
    'BANK NIFTY': {
        'opens':  [48200, 48250, 48220, 48300, 48280, 48340, 48310, 48380, 48400, 48370, 48430, 48460],
        'highs':  [48270, 48310, 48290, 48350, 48360, 48400, 48390, 48430, 48450, 48450, 48480, 48510],
        'lows':   [48150, 48200, 48190, 48230, 48240, 48270, 48280, 48330, 48360, 48340, 48400, 48430],
        'closes': [48250, 48220, 48300, 48280, 48340, 48310, 48380, 48400, 48370, 48430, 48460, 48490],
        'title':  'BANKNIFTY · 5m',
    },
}


def make_candlestick(symbol: str) -> go.Figure:
    d = CHART_DATA[symbol]
    times = ['09:15','09:20','09:25','09:30','09:35','09:40','09:45','09:50','09:55','10:00','10:05','10:10']

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=times,
        open=d['opens'], high=d['highs'], low=d['lows'], close=d['closes'],
        increasing=dict(line=dict(color='#26A69A', width=1), fillcolor='#26A69A'),
        decreasing=dict(line=dict(color='#EF5350', width=1), fillcolor='#EF5350'),
        name=d['title'],
    ))

    vols = [random.randint(800, 2500) for _ in times]
    vol_colors = ['rgba(38,166,154,0.33)' if c >= o else 'rgba(239,83,80,0.33)'
                  for o, c in zip(d['opens'], d['closes'])]
    fig.add_trace(go.Bar(
        x=times, y=vols, marker_color=vol_colors, name='Volume',
        yaxis='y2', showlegend=False,
    ))

    fig.update_layout(
        template='plotly_dark',
        paper_bgcolor='#131722',
        plot_bgcolor='#131722',
        xaxis=dict(
            showgrid=True, gridcolor='#2A2E39', gridwidth=1,
            tickfont=dict(size=10, color='#787B86'),
            rangeslider=dict(visible=False),
            showline=False,
        ),
        yaxis=dict(
            showgrid=True, gridcolor='#2A2E39', gridwidth=1,
            tickfont=dict(size=10, color='#787B86'),
            side='right',
            domain=[0.25, 1.0],
        ),
        yaxis2=dict(
            domain=[0, 0.2],
            tickfont=dict(size=9, color='#787B86'),
            showgrid=False,
            side='right',
        ),
        margin=dict(l=0, r=60, t=16, b=0),
        height=None,
        autosize=True,
        legend=dict(font=dict(size=10, color='#787B86'), bgcolor='rgba(0,0,0,0)'),
        hoverlabel=dict(
            bgcolor='#1E222D',
            bordercolor='#2A2E39',
            font=dict(color='#D1D4DC', size=11),
        ),
    )
    return fig


@ui.page('/dashboard')
def dashboard():
    global logged_in
    if not logged_in:
        ui.navigate.to('/')
        return

    ui.dark_mode()
    ui.add_head_html(f'<style>{TV_CSS}</style>')
    ui.add_head_html('''<style>
    html, body { height:100% !important; overflow:hidden !important; margin:0 !important; padding:0 !important; }
    .q-page-container, .q-layout, .q-page { height:100% !important; min-height:unset !important; padding:0 !important; }
    body > div:first-child { height:100% !important; }
    .nicegui-content { padding:0 !important; height:100% !important; }
    </style>''')

    state = {"symbol": "NIFTY 50", "conviction": None}
    refs = {}

    # ── Signal panel refresh ─────────────────────────────────────────────────
    def refresh_signal_panel():
        conv = compute_conviction(state["symbol"])
        state["conviction"] = conv
        c = conv["verdict_color"]
        bg_map  = {"#26A69A": "#26A69A22", "#F59E0B": "#F59E0B22", "#EF5350": "#EF535022"}
        brd_map = {"#26A69A": "#26A69A55", "#F59E0B": "#F59E0B55", "#EF5350": "#EF535055"}
        bg  = bg_map.get(c, "#2A2E3944")
        brd = brd_map.get(c, "#2A2E3988")

        refs["verdict_div"].set_content(
            f'<div class="tv-verdict" style="background:{bg};border:1px solid {brd}">' +
            f'<span class="tv-verdict-text" style="color:{c}">{conv["verdict"]}</span>' +
            f'<span class="tv-verdict-dir" style="color:{c}">● {conv["direction"]}</span></div>'
        )

        bar_color  = "#26A69A" if conv["score"] >= TRADE_THRESHOLD else "#F59E0B" if conv["score"] >= WATCH_THRESHOLD else "#EF5350"
        thresh_txt = "READY TO TRADE" if conv["score"] >= TRADE_THRESHOLD else f'{TRADE_THRESHOLD - conv["score"]:.0f} pts to TRADE'
        refs["score_div"].set_content(
            f'<div class="tv-score-row">' +
            f'<span class="tv-score-big" style="color:{bar_color}">{conv["score"]}</span>' +
            f'<div><div class="tv-score-label">/ 100</div>' +
            f'<div class="tv-score-label" style="color:{bar_color}">{thresh_txt}</div></div></div>'
        )
        refs["progress_div"].set_content(
            f'<div class="tv-bar-track">' +
            f'<div class="tv-bar-fill" style="width:{conv["score"]}%;background:{bar_color}"></div></div>'
        )

        refs["checklist_box"].clear()
        with refs["checklist_box"]:
            for label, sub, passed in conv["reasons"]:
                icon = '✓' if passed else '✗'
                iclr = '#26A69A' if passed else '#EF5350'
                nclr = '#D1D4DC' if passed else '#787B86'
                ui.html(
                    f'<div class="tv-check-row">' +
                    f'<span class="tv-check-icon" style="color:{iclr}">{icon}</span>' +
                    f'<div><div class="tv-check-name" style="color:{nclr}">{label}</div>' +
                    f'<div class="tv-check-sub">{sub}</div></div></div>'
                )

        s = conv["raw"]
        bias_cls  = "up" if conv["direction"] == "LONG" else "dn"
        bias_txt  = "Bullish" if conv["direction"] == "LONG" else "Bearish"
        fii_sign  = "+" if s["fii_net"] >= 0 else ""
        fii_cls   = "up" if s["fii_net"] >= 0 else "dn"
        iv_warn   = "warn" if s["iv_percentile"] > 0.7 else "up"
        sweep_cls = "up" if s["sweep_confirmed"] else "dn"
        sweep_txt = "Confirmed" if s["sweep_confirmed"] else "Not seen"
        adx_fmt   = "{:.0f}".format(s["adx"])
        iv_fmt    = "{:.0%}".format(s["iv_percentile"])
        vp_loc    = s["near_poc_or_val"]
        fii_fmt   = "{:,.0f}".format(s["fii_net"])
        refs["stat_bar"].set_content(
            '<div class="tv-stat-bar">' +
            f'<div class="tv-stat-item"><span class="tv-stat-label">Bias</span><span class="tv-stat-val {bias_cls}">{bias_txt}</span></div>' +
            f'<div class="tv-stat-item"><span class="tv-stat-label">Vol Profile</span><span class="tv-stat-val warn">Near {vp_loc}</span></div>' +
            f'<div class="tv-stat-item"><span class="tv-stat-label">ADX</span><span class="tv-stat-val">{adx_fmt}</span></div>' +
            f'<div class="tv-stat-item"><span class="tv-stat-label">Sweep</span><span class="tv-stat-val {sweep_cls}">{sweep_txt}</span></div>' +
            f'<div class="tv-stat-item"><span class="tv-stat-label">IV Pct</span><span class="tv-stat-val {iv_warn}">{iv_fmt}</span></div>' +
            f'<div class="tv-stat-item"><span class="tv-stat-label">FII Flow</span><span class="tv-stat-val {fii_cls}">{fii_sign}{fii_fmt} Cr</span></div>' +
            '</div>'
        )

    # ── Chart + sidebar switch ───────────────────────────────────────────────
    def show_chart(symbol: str):
        state["symbol"] = symbol
        refs["plot"].figure = make_candlestick(symbol)
        refs["plot"].update()
        p, ch, pc = PRICES.get(symbol, ('—', '—', '#787B86'))
        refs["symbol_label"].set_content(f'<span class="tv-symbol-name">{symbol}</span>')
        refs["price_label"].set_content(
            f'<span class="tv-price-live">{p} <span style="color:{pc};font-size:11px">{ch}</span></span>'
        )
        # Update active state on watchlist items
        for name, *_ in WATCHLIST:
            safe = name.replace(" ", "-")
            active_style = (
                "display:flex;justify-content:space-between;align-items:center;"
                "padding:10px 12px;border-bottom:1px solid #2A2E39;cursor:pointer;"
                "user-select:none;background:#2962FF1A;border-left:2px solid #2196F3;"
            ) if name == symbol else (
                "display:flex;justify-content:space-between;align-items:center;"
                "padding:10px 12px;border-bottom:1px solid #2A2E39;cursor:pointer;"
                "user-select:none;"
            )
            refs[f"wi_{safe}"].set_content(
                _watch_item_inner(name, *[x for x in WATCHLIST if x[0] == name][0][1:])
            )
            refs[f"wi_{safe}"].style(active_style)
        refresh_signal_panel()

    # ── Trade calculator ─────────────────────────────────────────────────────
    def do_calc():
        entry_val = refs["entry"].value
        if not entry_val:
            return
        conv      = state.get("conviction")
        risk_pts  = round(float(entry_val) * 0.003, 1)
        direction = conv["direction"] if conv else "LONG"
        score     = conv["score"]     if conv else "—"
        if direction == "SHORT":
            tgt       = round(float(entry_val) - 3 * risk_pts, 1)
            sl        = round(float(entry_val) + risk_pts, 1)
            tgt_color = "#EF5350"
            sl_color  = "#26A69A"
        else:
            tgt       = round(float(entry_val) + 3 * risk_pts, 1)
            sl        = round(float(entry_val) - risk_pts, 1)
            tgt_color = "#26A69A"
            sl_color  = "#EF5350"
        refs["target_lbl"].set_content(
            f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0">' +
            f'<span style="color:#787B86">Target</span>' +
            f'<span style="color:{tgt_color};font-weight:600">{tgt}</span></div>'
        )
        refs["sl_lbl"].set_content(
            f'<div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0">' +
            f'<span style="color:#787B86">Stop Loss</span>' +
            f'<span style="color:{sl_color};font-weight:600">{sl}</span></div>'
        )
        refs["rr_lbl"].set_content(
            f'<div style="font-size:10px;color:#787B86;margin-top:4px;padding:6px 8px;background:#131722;border-radius:4px;">' +
            f'Risk {risk_pts} pts &nbsp;·&nbsp; 1:3 R:R &nbsp;·&nbsp; Conviction {score}%</div>'
        )

    # ── Helper: inner HTML for a watchlist row ───────────────────────────────
    def _watch_item_inner(name, price, chg, up, low52, high52):
        chg_cls = 'tv-watch-chg-up' if up else 'tv-watch-chg-dn'
        return (
            f'<div><div class="tv-watch-name">{name}</div>'
            f'<div class="tv-watch-sub">L {low52} · H {high52}</div></div>'
            f'<div style="text-align:right"><div class="tv-watch-price">{price}</div>'
            f'<div class="{chg_cls}">{chg}</div></div>'
        )

    # ── TOP BAR ─────────────────────────────────────────────────────────────
    with ui.element('div').classes('tv-topbar'):
        ui.html('<span class="tv-logo">Trading Assistant</span>')
        with ui.element('div').classes('tv-ticker-strip'):
            for name, price, chg, up, *_ in WATCHLIST:
                chg_cls = 'tv-ticker-change-up' if up else 'tv-ticker-change-dn'
                ui.html(
                    f'<div class="tv-ticker-item">'
                    f'<span class="tv-ticker-name">{name}</span>'
                    f'<span class="tv-ticker-price">{price}</span>'
                    f'<span class="{chg_cls}">{chg}</span></div>'
                )
        ui.html('<span class="tv-time" id="clock"></span>')
        ui.add_body_html('''<script>
        (function tick(){
            var now=new Date();
            var t=now.toLocaleTimeString("en-IN",{hour:"2-digit",minute:"2-digit",second:"2-digit"});
            var el=document.getElementById("clock");
            if(el) el.textContent=t;
            setTimeout(tick,1000);
        })();
        </script>''')

    # ── THREE-COLUMN LAYOUT ──────────────────────────────────────────────────
    with ui.element('div').classes('tv-layout'):

        # LEFT: Watchlist — each item is a NiceGUI element with an on_click
        with ui.element('div').classes('tv-sidebar'):
            ui.html('<div class="tv-sidebar-header">Watchlist</div>')
            with ui.element('div').classes('tv-watchlist'):
                for row in WATCHLIST:
                    name, price, chg, up, low52, high52 = row
                    safe = name.replace(" ", "-")
                    is_active = (name == state["symbol"])
                    active_style = (
                        "display:flex;justify-content:space-between;align-items:center;"
                        "padding:10px 12px;border-bottom:1px solid #2A2E39;cursor:pointer;"
                        "user-select:none;background:#2962FF1A;border-left:2px solid #2196F3;"
                    ) if is_active else (
                        "display:flex;justify-content:space-between;align-items:center;"
                        "padding:10px 12px;border-bottom:1px solid #2A2E39;cursor:pointer;"
                        "user-select:none;"
                    )
                    wi = ui.html(_watch_item_inner(name, price, chg, up, low52, high52))
                    wi.style(active_style)
                    wi.on('click', lambda n=name: show_chart(n))
                    refs[f"wi_{safe}"] = wi

        # CENTRE: Chart
        with ui.element('div').classes('tv-main'):
            with ui.element('div').classes('tv-chart-toolbar'):
                refs["symbol_label"] = ui.html('<span class="tv-symbol-name">NIFTY 50</span>')
                refs["price_label"]  = ui.html(
                    '<span class="tv-price-live">24,892 '
                    '<span style="color:#26A69A;font-size:11px">▲ +0.41%</span></span>'
                )
                ui.html('<div class="tv-tf-divider"></div>')
                for tf in ['1m', '5m', '15m', '30m', '1H', 'D']:
                    active = 'active' if tf == '5m' else ''
                    ui.html(f'<button class="tv-tf-btn {active}" onclick="setTF(this)">{tf}</button>')
                ui.add_body_html('''<script>
                function setTF(el){
                    document.querySelectorAll(".tv-tf-btn").forEach(b=>b.classList.remove("active"));
                    el.classList.add("active");
                }
                </script>''')

            with ui.element('div').classes('tv-chart-wrap'):
                refs["plot"] = ui.plotly(make_candlestick("NIFTY 50")).style('width:100%;height:100%')

            refs["stat_bar"] = ui.html(
                '<div class="tv-stat-bar">'
                '<div class="tv-stat-item"><span class="tv-stat-label">Loading...</span></div>'
                '</div>'
            )

        # RIGHT: Signal panel
        with ui.element('div').classes('tv-signal-panel'):

            with ui.element('div').classes('tv-panel-section'):
                ui.html('<div class="tv-panel-section-title">Signal Engine</div>')
                refs["verdict_div"] = ui.html(
                    '<div class="tv-verdict" style="background:#26A69A22;border:1px solid #26A69A44">'
                    '<span class="tv-verdict-text" style="color:#26A69A">—</span>'
                    '<span class="tv-verdict-dir" style="color:#26A69A">—</span></div>'
                )

            with ui.element('div').classes('tv-panel-section'):
                ui.html('<div class="tv-panel-section-title">Conviction Score</div>')
                refs["score_div"]    = ui.html(
                    '<div class="tv-score-row">'
                    '<span class="tv-score-big" style="color:#787B86">—</span>'
                    '<div><div class="tv-score-label">/ 100</div></div></div>'
                )
                refs["progress_div"] = ui.html(
                    '<div class="tv-bar-track">'
                    '<div class="tv-bar-fill" style="width:0%;background:#787B86"></div></div>'
                )
                ui.button('↻  Recalculate', on_click=refresh_signal_panel) \
                  .classes('w-full') \
                  .style('background:#2196F3 !important;color:#fff !important;'
                         'border-radius:4px;font-weight:600;font-size:12px;margin-top:6px;')

            with ui.element('div').classes('tv-panel-section'):
                ui.html('<div class="tv-panel-section-title">Trade Protocols</div>')
                refs["checklist_box"] = ui.column().style('gap:0;width:100%')

            with ui.element('div').classes('tv-panel-section'):
                ui.html('<div class="tv-panel-section-title">Trade Calculator</div>')
                refs["entry"] = ui.number(label='Entry Price', placeholder='e.g. 24850') \
                                  .classes('w-full') \
                                  .props('outlined dark color=blue-5 dense')
                refs["entry"].style('margin-bottom:6px')
                refs["target_lbl"] = ui.html('<div style="font-size:12px;color:#787B86;padding:3px 0">Target: —</div>')
                refs["sl_lbl"]     = ui.html('<div style="font-size:12px;color:#787B86;padding:3px 0">Stop Loss: —</div>')
                refs["rr_lbl"]     = ui.html('<div></div>')
                ui.button('Calculate Levels', on_click=do_calc) \
                  .classes('w-full') \
                  .style('background:#2196F3 !important;color:#fff !important;'
                         'border-radius:4px;font-weight:600;font-size:12px;margin-top:6px;')

    # Initial load
    refresh_signal_panel()


ui.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
