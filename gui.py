from nicegui import ui
import plotly.graph_objects as go
logged_in=False
PASSWORD = 'model2026'

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
    fig = go.Figure()
    fig.update_layout(template='plotly_dark',title='Select a Market',xaxis_rangeslider_visible=False,height=700,)
    def show_chart(symbol):
        if symbol == "BANK NIFTY":
            fig = go.Figure(data=[go.Candlestick(
                x=['9:15','9:30','9:45','10:00','10:15','10:30'],
                open=[25000,25020,25010,25050,25040,25080],
                high=[25030,25040,25060,25070,25100,25130],
                low=[24990,25000,25000,25020,25030,25070],
                close=[25020,25010,25050,25040,25080,25120],
                )
                                  ])
            fig.update_layout(template='plotly_dark',title='BANKNIFTY',xaxis_rangeslider_visible=False,height=700,)
        elif symbol == "NIFTY 50":
            fig = go.Figure(data=[go.Candlestick(
                x=['9:15','9:30','9:45','10:00','10:15','10:30'],
                open=[24800,24840,24820,24860,24850,24890],
                high=[24860,24870,24880,24900,24910,24940],
                low=[24790,24800,24810,24830,24840,24870],
                close=[24840,24820,24860,24850,24890,24920],
                )
                                  ])
            fig.update_layout(template='plotly_dark',title='NIFTY',xaxis_rangeslider_visible=False,height=700,)
        plot.figure = fig
        plot.update()
    # Header
    ui.label('Trading Assistant').classes('text-base font-bold w-full').style('color:white')#change alignment to left corner text size small

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
                    with ui.card().classes('cursor-pointer').style('background:linear-gradient(0deg,#1E293B,#111827); width:100%;') as card:
                        ui.label(stock).style('color:white')
                        ui.label(price).style('color:white')
                        ui.label(change).style(f'color:{color}')
                    card.on('click', lambda s=stock: show_chart(s))
        # CHART
        with ui.column().classes('grow'):
            with ui.card().style('''background:linear-gradient(180deg,#1E293B,#111827;border-radius:16px;width:100%;height:720px;'''):
                plot=ui.plotly(fig).style('width:100%;height:650px')
                with ui.row().classes('w-full justify-around'):
                    ui.label('Market Bias: Bullish').style('color:#22C55E')
                    ui.label('Volume Profile: Near VAL').style('color:#38BDF8')
                    ui.label('ADX: 28').style('color:#FACC15')
                    ui.label('Liquidity Sweep: ✓').style('color:#22C55E')
            # SIGNAL PANEL
        with ui.card().style('width:300px;height:720px;background:linear-gradient(180deg,#1E293B,#111827);border:1px solid #22C55E;box-shadow:0 0 15px rgba(34,197,94,0.25);border-radius:16px;overflow-y:auto;padding:12px;'):
            ui.label('LONG SETUP DETECTED').style('''background:#14532D;color:white;padding:12px;border-radius:10px;font-size:22px;font-weight:bold;''')
            #detect if volume changed abruptly
            #also add the volume range thing(seeing the POC of the selected timeframe of market)
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
    
