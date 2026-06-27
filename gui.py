from nicegui import ui
import plotly.graph_objects as go
import random
import os

logged_in = False
PASSWORD = 'model2026'

# ---------------------------------------------------------------------------
# CONVICTION ENGINE
# Lightweight, self-contained version of the institutional scoring logic.
# Each component returns 0-1 "goodness"; weights mirror the full engine
# (src/utils/config.py / src/scoring/bias_score.py) if you've dropped that
# package alongside this app -- swap compute_conviction() to import from
# there once real NSE/option-chain data is wired in. For now this runs on
# realistic mock inputs so the UI is fully functional end-to-end today.
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
    """Stand-in for a live data pull. Replace with real feed (NSE/broker API)."""
    random.seed(hash(symbol) % 1000 + random.randint(0, 5))
    return {
        "trend_aligned": random.random() > 0.3,         # daily/15m/5m agreement
        "sweep_confirmed": random.random() > 0.35,       # liquidity sweep + reclaim
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
    """Returns conviction score (0-100), a trade verdict, and the reasoning checklist."""
    s = mock_market_state(symbol)
    reasons = []

    trend_score = 1.0 if s["trend_aligned"] else 0.2
    reasons.append(("Trend Filter (Daily/15m/5m aligned)", s["trend_aligned"]))

    sweep_score = 1.0 if s["sweep_confirmed"] else 0.25
    reasons.append(("Liquidity Sweep + Reclaim", s["sweep_confirmed"]))

    vp_score = 0.9 if s["near_poc_or_val"] in ("VAL", "POC") else 0.4
    reasons.append((f"Volume Profile (near {s['near_poc_or_val']})", vp_score > 0.5))

    adx_score = min(s["adx"] / 30, 1.0)
    reasons.append((f"ADX Regime Strength ({s['adx']:.0f})", s["adx"] >= 20))

    of_score = s["order_flow_buy_fraction"]
    reasons.append((f"Order Flow (buy-fraction {of_score:.2f})", of_score >= 0.55))

    flow_combined = (s["fii_net"] + 0.5 * s["dii_net"] + 4000) / 8000
    flow_score = max(0, min(flow_combined, 1))
    reasons.append(("FII/DII Flow Supportive", flow_score > 0.55))

    vol_score = (0 if s["iv_percentile"] > 0.85 else (1 - s["iv_percentile"])) * (1 if s["term_structure_healthy"] else 0.5)
    reasons.append(("Volatility / Term Structure Healthy", vol_score > 0.4))

    breadth_score = 0.9 if s["banknifty_confirms"] else 0.2
    reasons.append(("Bank Nifty Confirms Breadth", s["banknifty_confirms"]))

    weighted = (
        trend_score * WEIGHTS["trend"] + sweep_score * WEIGHTS["liquidity_sweep"] +
        vp_score * WEIGHTS["volume_profile"] + adx_score * WEIGHTS["adx_regime"] +
        of_score * WEIGHTS["order_flow"] + flow_score * WEIGHTS["flow_fii_dii"] +
        vol_score * WEIGHTS["vol_structure"] + breadth_score * WEIGHTS["breadth"]
    )
    score = round((weighted / TOTAL_WEIGHT) * 100, 1)

    if score >= TRADE_THRESHOLD:
        verdict, verdict_color = "TRADE", "#22C55E"
    elif score >= WATCH_THRESHOLD:
        verdict, verdict_color = "WATCH ONLY", "#FACC15"
    else:
        verdict, verdict_color = "NO TRADE", "#EF4444"

    direction = "LONG" if s["trend_aligned"] and s["order_flow_buy_fraction"] >= 0.5 else "SHORT"

    return {
        "score": score, "verdict": verdict, "verdict_color": verdict_color,
        "direction": direction, "reasons": reasons, "raw": s,
    }


# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------

@ui.page('/')
def login_page():
    def login():
        global logged_in
        if password.value == PASSWORD:
            logged_in = True
            ui.navigate.to('/dashboard')
        else:
            ui.notify('Wrong password')

    password = ui.input('Password', password=True)
    ui.button('Login', on_click=login)


@ui.page('/dashboard')
def dashboard():
    global logged_in
    if not logged_in:
        ui.navigate.to('/')
        return

    ui.dark_mode()
    ui.query('body').style('''background: linear-gradient(135deg,#130721 0%,#1E1B4B 50%,#0F172A 100%);margin:0;padding:0;''')

    state = {"symbol": "NIFTY 50"}

    fig = go.Figure()
    fig.update_layout(template='plotly_dark', title='Select a Market', xaxis_rangeslider_visible=False, height=700)

    def refresh_signal_panel():
        conv = compute_conviction(state["symbol"])
        verdict_label.set_text(f'{conv["verdict"]} — {conv["direction"]}')
        verdict_label.style(f'background:{verdict_color_bg(conv["verdict_color"])};color:white;padding:12px;border-radius:10px;font-size:22px;font-weight:bold;')
        score_label.set_text(f'Conviction Score: {conv["score"]}%')
        score_label.style(f'color:{conv["verdict_color"]};font-size:24px;font-weight:bold')
        progress.set_value(conv["score"] / 100)
        progress.props(f'color={"green" if conv["score"] >= TRADE_THRESHOLD else "amber" if conv["score"] >= WATCH_THRESHOLD else "red"}')

        checklist_box.clear()
        with checklist_box:
            for label, passed in conv["reasons"]:
                with ui.row().style('align-items:center;gap:6px'):
                    ui.icon('check_circle' if passed else 'cancel').style(f'color:{"#22C55E" if passed else "#EF4444"}')
                    ui.label(label).style(f'color:{"#86efac" if passed else "#fca5a5"}')

        bias_label.set_text(f'Market Bias: {"Bullish" if conv["direction"] == "LONG" else "Bearish"}')
        bias_label.style(f'color:{"#22C55E" if conv["direction"] == "LONG" else "#EF4444"}')

        state["conviction"] = conv

    def show_chart(symbol):
        state["symbol"] = symbol
        if symbol == "BANK NIFTY":
            new_fig = go.Figure(data=[go.Candlestick(
                x=['9:15', '9:30', '9:45', '10:00', '10:15', '10:30'],
                open=[25000, 25020, 25010, 25050, 25040, 25080],
                high=[25030, 25040, 25060, 25070, 25100, 25130],
                low=[24990, 25000, 25000, 25020, 25030, 25070],
                close=[25020, 25010, 25050, 25040, 25080, 25120],
            )])
            new_fig.update_layout(template='plotly_dark', title='BANKNIFTY', xaxis_rangeslider_visible=False, height=700)
        else:
            new_fig = go.Figure(data=[go.Candlestick(
                x=['9:15', '9:30', '9:45', '10:00', '10:15', '10:30'],
                open=[24800, 24840, 24820, 24860, 24850, 24890],
                high=[24860, 24870, 24880, 24900, 24910, 24940],
                low=[24790, 24800, 24810, 24830, 24840, 24870],
                close=[24840, 24820, 24860, 24850, 24890, 24920],
            )])
            new_fig.update_layout(template='plotly_dark', title='NIFTY', xaxis_rangeslider_visible=False, height=700)
        plot.figure = new_fig
        plot.update()
        refresh_signal_panel()

    def verdict_color_bg(color_hex):
        return {"#22C55E": "#14532D", "#FACC15": "#854D0E", "#EF4444": "#7F1D1D"}.get(color_hex, "#1E293B")

    # Header
    ui.label('Trading Assistant').classes('text-base font-bold w-full').style('color:white')

    with ui.row().classes('w-full'):
        with ui.column().classes('items-start'):
            with ui.row().style('margin-top:5px'):
                ui.icon('show_chart').style('color:#22C55E;font-size:22px')
                ui.label('MARKETS').style('color:white;font-size:22px')

            with ui.card().style('width:200px;height:650px;background:linear-gradient(180deg,#1E293B,#111827);border:1px solid #334155;border-radius:16px;'):
                ui.label('WATCHLIST').classes('text-h6').style('color:white;font-size:16px')
                watchlist = [('NIFTY 50', '24013', '-0.64%'), ('BANK NIFTY', '57685', '-0.48%')]
                for stock, price, change in watchlist:
                    color = '#22C55E' if '+' in change else '#EF4444'
                    with ui.card().classes('cursor-pointer').style('background:linear-gradient(0deg,#1E293B,#111827); width:100%;') as card:
                        ui.label(stock).style('color:white')
                        ui.label(price).style('color:white')
                        ui.label(change).style(f'color:{color}')
                    card.on('click', lambda s=stock: show_chart(s))

        with ui.column().classes('grow'):
            with ui.card().style('''background:linear-gradient(180deg,#1E293B,#111827;border-radius:16px;width:100%;height:720px;'''):
                plot = ui.plotly(fig).style('width:100%;height:650px')
                with ui.row().classes('w-full justify-around'):
                    bias_label = ui.label('Market Bias: —').style('color:#22C55E')
                    ui.label('Volume Profile: Near VAL').style('color:#38BDF8')
                    ui.label('ADX: 28').style('color:#FACC15')
                    ui.label('Liquidity Sweep: ✓').style('color:#22C55E')

        with ui.card().style('width:300px;height:720px;background:linear-gradient(180deg,#1E293B,#111827);border:1px solid #22C55E;box-shadow:0 0 15px rgba(34,197,94,0.25);border-radius:16px;overflow-y:auto;padding:12px;'):
            verdict_label = ui.label('—').style('background:#1E293B;color:white;padding:12px;border-radius:10px;font-size:22px;font-weight:bold;')
            ui.separator()
            score_label = ui.label('Conviction Score: —').style('color:#22C55E;font-size:24px;font-weight:bold')
            progress = ui.linear_progress(value=0).props('color=green')
            ui.button('Recalculate Conviction', on_click=refresh_signal_panel).style('margin-top:6px')

            with ui.column().style('gap:3px'):
                with ui.expansion('Trade Protocols', value=True).style('color:white'):
                    checklist_box = ui.column().style('gap:4px')

                with ui.expansion('Trade Calculator', value=True).style('color:white'):
                    entry = ui.number(label='Entry Price')
                    entry.props('outlined color=green dark')
                    target = ui.label('Target: -').style('color:white')
                    sl = ui.label('Stop Loss: -').style('color:white')
                    risk_note = ui.label('').style('color:#94A3B8;font-size:12px')

                    def calc():
                        if not entry.value:
                            return
                        conv = state.get("conviction")
                        # Risk sized off a realistic intraday range proxy rather than a fixed magic number.
                        # In the full engine this comes from H5/L5 (first-5-min range); here we approximate
                        # with a fixed 0.3% of entry price as a stand-in until live H5/L5 data is wired in.
                        risk_points = round(entry.value * 0.003, 1)
                        if conv and conv["direction"] == "SHORT":
                            target.set_text(f'Target: {entry.value - 3 * risk_points:.1f}')
                            sl.set_text(f'Stop Loss: {entry.value + risk_points:.1f}')
                        else:
                            target.set_text(f'Target: {entry.value + 3 * risk_points:.1f}')
                            sl.set_text(f'Stop Loss: {entry.value - risk_points:.1f}')
                        risk_note.set_text(f'Risk {risk_points} pts · 1:3 R:R · conviction {conv["score"] if conv else "—"}%')

                    ui.button('Calculate', on_click=calc)

    refresh_signal_panel()


ui.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
