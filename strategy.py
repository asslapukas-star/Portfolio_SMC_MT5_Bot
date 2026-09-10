"""
CHoCH + untouched H1 OB strategy - LIVE version.

Exactly matches the validated backtest logic (2026-09-10 research, see
METHODOLOGY.md) - copied WITHOUT changes (only backtest-only code was
removed: debug logging, FVG detection [removed from the validated version
- MFE analysis showed that FVG confluence HURTS rather than helps in this
context], the multi-period loop).

Logic:
  1) H4: bias (2 confirmed significant swings) + H4 OB (not used directly
     for TP/SL, only for bias direction).
  2) H1: wait for a CHoCH (a break THROUGH the last confirmed OPPOSITE-type
     swing point, direction MATCHING the H4 bias).
  3) After the CHoCH: look for an UNTOUCHED H1 OB (last opposite candle
     before the CHoCH impulse).
  4) LIMIT order at that H1 OB zone's MIDPOINT - SL beyond the zone + ATR
     buffer, dynamic TP (nearest confirmed opposite-direction H1 swing, or
     FALLBACK_RR*risk_dist if none exists).

generate_live_signal() returns the CURRENT state (for the last known bar) -
whether a trade is open, whether there's a pending order - so main.py can
compare it against the real MT5 state and synchronize.
"""
import numpy as np
import pandas as pd
from config import SWING_LOOKBACK, MIN_IMPULSE_ATR, ATR_SL_BUFFER, FALLBACK_RR


def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_fractal_swings(highs, lows, lookback=SWING_LOOKBACK):
    n = len(highs)
    swing_high = np.full(n, np.nan)
    swing_low = np.full(n, np.nan)
    for i in range(lookback, n - lookback):
        wh = highs[i - lookback:i + lookback + 1]
        wl = lows[i - lookback:i + lookback + 1]
        if highs[i] == wh.max():
            swing_high[i] = highs[i]
        if lows[i] == wl.min():
            swing_low[i] = lows[i]
    return swing_high, swing_low


def detect_h4_bias(h4_df):
    highs = h4_df['high'].values; lows = h4_df['low'].values
    atrs = calculate_atr(h4_df, 14).values
    n = len(h4_df)
    sh, sl = compute_fractal_swings(highs, lows, SWING_LOOKBACK)

    bias = np.full(n, "NEUTRAL", dtype=object)
    confirmed_highs = []; confirmed_lows = []
    for i in range(n):
        confirm_idx = i - SWING_LOOKBACK
        atr_here = atrs[confirm_idx] if 0 <= confirm_idx < len(atrs) else np.nan
        if confirm_idx >= 0 and not np.isnan(atr_here) and atr_here > 0:
            if not np.isnan(sh[confirm_idx]):
                sig = (not confirmed_lows) or (sh[confirm_idx] - confirmed_lows[-1][1] >= MIN_IMPULSE_ATR * atr_here)
                if sig:
                    confirmed_highs.append((confirm_idx, sh[confirm_idx]))
            if not np.isnan(sl[confirm_idx]):
                sig = (not confirmed_highs) or (confirmed_highs[-1][1] - sl[confirm_idx] >= MIN_IMPULSE_ATR * atr_here)
                if sig:
                    confirmed_lows.append((confirm_idx, sl[confirm_idx]))
        cur_bias = "NEUTRAL"
        if len(confirmed_highs) >= 2 and len(confirmed_lows) >= 2:
            if confirmed_highs[-1][1] > confirmed_highs[-2][1] and confirmed_lows[-1][1] > confirmed_lows[-2][1]:
                cur_bias = "BULLISH"
            elif confirmed_highs[-1][1] < confirmed_highs[-2][1] and confirmed_lows[-1][1] < confirmed_lows[-2][1]:
                cur_bias = "BEARISH"
        bias[i] = cur_bias
    return bias


def generate_live_signal(symbol, h4_df, h1_df, cost_price, max_wait_bars):
    """Replays the FULL history (h1_df) from the start, so the state (pending
    orders, ob_touched_ids, etc.) is CORRECTLY reconstructed up to 'now'.
    Returns a dict: {'in_trade': bool, 'open_trade': {...}|None,
    'pending_orders': [...]}."""
    h4_df = h4_df.reset_index(drop=True)
    h1_df = h1_df.reset_index(drop=True)
    h1_atrs = calculate_atr(h1_df, 14).values
    h4_bias = detect_h4_bias(h4_df)

    h1_highs = h1_df['high'].values; h1_lows = h1_df['low'].values
    h1_opens = h1_df['open'].values; h1_closes = h1_df['close'].values
    h1_times = h1_df['time'].values
    h4_times = h4_df['time'].values
    n1 = len(h1_df)
    sh1, sl1 = compute_fractal_swings(h1_highs, h1_lows, SWING_LOOKBACK)

    pending_orders = []
    in_trade = False
    side = entry = risk_dist = tp = sl_price = 0.0
    entry_time = None

    h4_idx_ptr = 0
    confirmed_highs1 = []; confirmed_lows1 = []
    h1_trend = "NEUTRAL"
    ob_touched_ids = set()

    for i in range(30, n1 - 1):
        t = h1_times[i]
        while h4_idx_ptr + 1 < len(h4_times) and h4_times[h4_idx_ptr + 1] < t:
            h4_idx_ptr += 1
        cur_h4_bias = h4_bias[h4_idx_ptr] if h4_times[h4_idx_ptr] < t else "NEUTRAL"

        # Swing/trend update ALWAYS runs, even while in_trade=True - otherwise
        # confirmed_highs1/confirmed_lows1 "freeze" while a trade is open and
        # the trailing stop wouldn't have fresh data (a bug found and fixed
        # in the 2026-09-10 backtests - see METHODOLOGY.md).
        confirm_idx = i - SWING_LOOKBACK
        h1_atr_here = h1_atrs[confirm_idx] if 0 <= confirm_idx < len(h1_atrs) else np.nan
        if confirm_idx >= 0 and not np.isnan(h1_atr_here) and h1_atr_here > 0:
            if not np.isnan(sh1[confirm_idx]):
                sig = (not confirmed_lows1) or (sh1[confirm_idx] - confirmed_lows1[-1][1] >= MIN_IMPULSE_ATR * h1_atr_here)
                if sig:
                    confirmed_highs1.append((confirm_idx, sh1[confirm_idx]))
            if not np.isnan(sl1[confirm_idx]):
                sig = (not confirmed_highs1) or (confirmed_highs1[-1][1] - sl1[confirm_idx] >= MIN_IMPULSE_ATR * h1_atr_here)
                if sig:
                    confirmed_lows1.append((confirm_idx, sl1[confirm_idx]))

        prev_h1_trend = h1_trend
        if len(confirmed_highs1) >= 2 and len(confirmed_lows1) >= 2:
            if confirmed_highs1[-1][1] > confirmed_highs1[-2][1] and confirmed_lows1[-1][1] > confirmed_lows1[-2][1]:
                h1_trend = "BULLISH"
            elif confirmed_highs1[-1][1] < confirmed_highs1[-2][1] and confirmed_lows1[-1][1] < confirmed_lows1[-2][1]:
                h1_trend = "BEARISH"

        if in_trade:
            closed = False
            if side == "BUY":
                if h1_lows[i] <= sl_price or h1_highs[i] >= tp:
                    closed = True
            else:
                if h1_highs[i] >= sl_price or h1_lows[i] <= tp:
                    closed = True
            if closed:
                in_trade = False
            continue

        pending_orders = [o for o in pending_orders if i <= o['expires_idx']]
        for o in list(pending_orders):
            filled = h1_lows[i] <= o['zone_top'] and h1_highs[i] >= o['zone_bottom']
            if filled:
                entry_price = (o['zone_top'] + o['zone_bottom']) / 2.0
                side = o['side']; entry = entry_price; risk_dist = abs(entry_price - o['sl'])
                sl_price = o['sl']; tp = o['tp']
                in_trade = True; entry_time = t
                pending_orders.remove(o)
                break
        if in_trade:
            continue

        if cur_h4_bias == "NEUTRAL":
            continue

        choch_bearish = (prev_h1_trend == "BULLISH" and cur_h4_bias == "BEARISH" and confirmed_lows1
                          and h1_closes[i] < confirmed_lows1[-1][1] and h1_closes[i - 1] >= confirmed_lows1[-1][1])
        choch_bullish = (prev_h1_trend == "BEARISH" and cur_h4_bias == "BULLISH" and confirmed_highs1
                          and h1_closes[i] > confirmed_highs1[-1][1] and h1_closes[i - 1] <= confirmed_highs1[-1][1])

        if not (choch_bearish or choch_bullish):
            continue

        if choch_bearish:
            j = i
            while j > 0 and h1_closes[j] <= h1_opens[j]:
                j -= 1
            if j < 0 or h1_closes[j] <= h1_opens[j]:
                continue
            ob_top, ob_bottom = h1_highs[j], h1_lows[j]
            ob_type = 'BEARISH'
        else:
            j = i
            while j > 0 and h1_closes[j] >= h1_opens[j]:
                j -= 1
            if j < 0 or h1_closes[j] >= h1_opens[j]:
                continue
            ob_top, ob_bottom = h1_highs[j], h1_lows[j]
            ob_type = 'BULLISH'

        if j in ob_touched_ids:
            continue

        already_touched = False
        for k in range(j + 1, i):
            if h1_lows[k] <= ob_top and h1_highs[k] >= ob_bottom:
                already_touched = True
                break
        if already_touched:
            continue

        atr_now = h1_atrs[i]
        if np.isnan(atr_now) or atr_now <= 0:
            continue

        if ob_type == 'BULLISH':
            sl_cand = ob_bottom - atr_now * ATR_SL_BUFFER
            entry_mid = (ob_top + ob_bottom) / 2.0
            risk_dist_cand = entry_mid - sl_cand
            if risk_dist_cand <= 0 or risk_dist_cand <= cost_price * 3.0:
                continue
            future_target = None
            for idx, price in reversed(confirmed_highs1):
                if price > ob_top:
                    future_target = price; break
            tp_cand = future_target if future_target is not None else ob_top + risk_dist_cand * FALLBACK_RR
            pending_orders.append({'side': 'BUY', 'zone_top': ob_top, 'zone_bottom': ob_bottom,
                                    'sl': sl_cand, 'tp': tp_cand, 'expires_idx': i + max_wait_bars})
        else:
            sl_cand = ob_top + atr_now * ATR_SL_BUFFER
            entry_mid = (ob_top + ob_bottom) / 2.0
            risk_dist_cand = sl_cand - entry_mid
            if risk_dist_cand <= 0 or risk_dist_cand <= cost_price * 3.0:
                continue
            future_target = None
            for idx, price in reversed(confirmed_lows1):
                if price < ob_bottom:
                    future_target = price; break
            tp_cand = future_target if future_target is not None else ob_bottom - risk_dist_cand * FALLBACK_RR
            pending_orders.append({'side': 'SELL', 'zone_top': ob_top, 'zone_bottom': ob_bottom,
                                    'sl': sl_cand, 'tp': tp_cand, 'expires_idx': i + max_wait_bars})
        ob_touched_ids.add(j)

    valid_atrs = h1_atrs[~np.isnan(h1_atrs)]
    last_atr = float(valid_atrs[-1]) if len(valid_atrs) else None

    return {
        'symbol': symbol,
        'last_bar_time': h1_times[-1] if n1 > 0 else None,
        'in_trade': in_trade,
        'open_trade': ({'side': side, 'entry': entry, 'sl': sl_price, 'tp': tp,
                         'entry_time': entry_time} if in_trade else None),
        'pending_orders': [dict(o) for o in pending_orders],
        'last_confirmed_high': confirmed_highs1[-1][1] if confirmed_highs1 else None,
        'last_confirmed_low': confirmed_lows1[-1][1] if confirmed_lows1 else None,
        'last_atr': last_atr,
    }
