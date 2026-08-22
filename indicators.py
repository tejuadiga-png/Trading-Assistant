import numpy as np
import pandas as pd
def ema(close, period):
    """Exponential Moving Average.Parametersclose : pd.Seriesperiod : int Returns pd.Series """
    close = pd.Series(close)
    return close.ewm(span=period, adjust=False).mean()

def sma(close, period):
    """Simple Moving Average."""
    close = pd.Series(close)
    return close.rolling(window=period, min_periods=period).mean()

def rsi(close, period=14):
    """Relative Strength Index (Wilder's smoothing)."""
    close = pd.Series(close)
    delta = close.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder's smoothing = EMA with alpha = 1/period
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi_val = 100 - (100 / (1 + rs))

    # Where avg_loss is 0, RSI should be 100
    rsi_val = rsi_val.where(avg_loss != 0, 100)
    return rsi_val

def _true_range(high, low, close):
    """ Helper: True Range series used by ATR and ADX. """
    high = pd.Series(high)
    low = pd.Series(low)
    close = pd.Series(close)

    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr


def atr(high, low, close, period=14):
    """Average True Range (Wilder's smoothing)."""
    tr = _true_range(high, low, close)
    atr_val = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return atr_val

def adx(high, low, close, period=14):
    """Average Directional IndexReturns pd.Series (ADX values) """
    high = pd.Series(high)
    low = pd.Series(low)
    close = pd.Series(close)

    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = _true_range(high, low, close)
    atr_val = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_val)
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean() / atr_val)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_val = dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    return adx_val

def vwap(high, low, close, volume):
    """Volume Weighted Average Price (cumulative, typical-price based).
    Note: This is a cumulative VWAP over the entire series provided.
    If you need session-based VWAP (e.g. reset every trading day),
    group your data by date before calling this function, or pass
    only one session's worth of data at a time."""
    high = pd.Series(high)
    low = pd.Series(low)
    close = pd.Series(close)
    volume = pd.Series(volume)

    typical_price = (high + low + close) / 3
    cum_vol = volume.cumsum()
    cum_vol_price = (typical_price * volume).cumsum()

    vwap_val = cum_vol_price / cum_vol
    return vwap_val
