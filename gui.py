from nicegui import ui
import plotly.graph_objects as go
logged_in=False
PASSWORD = 'Tapasyagarhwalih'

@ui.page('/')
def login_page():
    def login():
        global logged_in
        if password.value == PASSWORD:
            logged_in=True
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
    # Fake Chart
    fig = go.Figure(data=[
        go.Candlestick(
            x=['9:15','9:30','9:45','10:00','10:15','10:30'],
            open=[25000,25020,25010,25050,25040,25080],
            high=[25030,25040,25060,25070,25100,25130],
            low=[24990,25000,25000,25020,25030,25070],
            close=[25020,25010,25050,25040,25080,25120],
            increasing_fillcolor='#22C55E',
            increasing_line_color='#22C55E',
            decreasing_fillcolor='#EF4444',
            decreasing_line_color='#EF4444',
            )
        ])
    fig.update_layout(xaxis_rangeslider_visible=False,template='plotly_dark',title='BANKNIFTY(demo)',height=700,)
    # Header
    ui.label('Trading Assistant').classes('text-h2 text-center font-bold w-full').style('color:white')
    with ui.row().classes('w-full justify-center'):
        ui.label('BAJAJ BANK 24,013 ▼0.64%').style('color:#EF4444; font-size:18px; margin-right:30px')
        ui.label('TATA STEEL 57,685 ▼0.48%').style('color:#EF4444; font-size:18px; margin-right:30px' )
        ui.label('HDFC 1309 ▼1.40%').style('color:#EF4444; font-size:18px')

    # Main Layout
    with ui.row().classes('w-full'):
        with ui.column().classes('items-start'):
            with ui.row().style('margin-top:5px'):
                ui.icon('show_chart').style('color:#22C55E;font-size:22px')
                ui.label('MARKETS').style('color:white;font-size:22px')
            # WATCHLIST
            with ui.card().style('width:200px;height:650px;background:linear-gradient(180deg,#1E293B,#111827);border:1px solid #334155;border-radius:16px;' ):
                ui.label('WATCHLIST').classes('text-h6').style('color:white;font-size:16px')
                watchlist = [('NIFTY 50', '24013', '-0.64%'),('BANK NIFTY', '57685', '-0.48%'),]
                for stock, price, change in watchlist:
                    color = '#22C55E' if '+' in change else '#EF4444'
                    with ui.card().style('background:linear-gradient(0deg,#1E293B,#111827); width:100%;'):
                        ui.label(stock).style('color:white')
                        ui.label(price).style('color:white')
                        ui.label(change).style(f'color:{color}')
        # CHART
        with ui.column().classes('grow'):
            with ui.card().style('''background:linear-gradient(180deg,#1E293B,#111827;border-radius:16px;width:100%;height:720px;'''):
                ui.plotly(fig).style('width:100%;height:650px')
                with ui.row().classes('w-full justify-around'):
                    ui.label('Market Bias: Bullish').style('color:#22C55E')
                    ui.label('Volume Profile: Near VAL').style('color:#38BDF8')
                    ui.label('ADX: 28').style('color:#FACC15')
                    ui.label('Liquidity Sweep: ✓').style('color:#22C55E')
            # SIGNAL PANEL
        with ui.card().style('width:300px;height:720px;background:linear-gradient(180deg,#1E293B,#111827);border:1px solid #22C55E;box-shadow:0 0 15px rgba(34,197,94,0.25);border-radius:16px;overflow-y:auto;padding:12px;'):
            ui.label('LONG SETUP DETECTED').style('''background:#14532D;color:white;padding:12px;border-radius:10px;font-size:22px;font-weight:bold;''')
            ui.separator()
            ui.label('Confidence Score :91%').style('color:#22C55E;font-size:24px;font-weight:bold')
            ui.linear_progress(value=0.91).props('color=green')
            with ui.column().style('gap:3px'):
                with ui.expansion('Trade Protocols').style('color:white'):
                    with ui.row():
                        ui.icon('check_circle').style('color:#22C55E')
                        ui.label('Trend Filter').style('color:green')
                    with ui.row():
                        ui.icon('check_circle').style('color:#22C55E')
                        ui.label('Liquidity Sweep').style('color:green')
                    with ui.row():
                        ui.icon('check_circle').style('color:#22C55E')
                        ui.label('Volume Profile').style('color:green')
                    with ui.row():
                        ui.icon('check_circle').style('color:#22C55E')
                        ui.label('Regime Filter').style('color:green')
                    with ui.row():
                        ui.icon('check_circle').style('color:#22C55E')
                        ui.label('ADX Filter').style('color:green')
                    ui.separator()
                    ui.label('Daily Bullish ✓').style('color:green')
                    ui.label('15 min Bullish ✓').style('color:green')
                    ui.label('5m Bullish ✓').style('color:green')
                with ui.expansion('Trade Calculator', value=True).style('color:white'):
                    entry = ui.number(label='Entry Price')
                    entry.props('outlined color=green dark')
                    target = ui.label('Target: -').style('color:white')
                    sl = ui.label('Stop Loss: -').style('color:white')
                    def calc():
                        if entry.value:
                            target.set_text(f'Target: {entry.value +75}')
                            sl.set_text(f'Stop Loss: {entry.value - 37.5}')
                    ui.button('Calculate',on_click=calc)
import os
ui.run(host='0.0.0.0',port=int(os.environ.get('PORT', 8080)))
    
