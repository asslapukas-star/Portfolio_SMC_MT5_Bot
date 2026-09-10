"""
SMC CHoCH+OB Bot - live/demo trading loop for the "CHoCH + untouched H1 OB"
strategy. See strategy.py's docstring for the full strategy description and
METHODOLOGY.md for the full research writeup.

IMPORTANT: this is a FORWARD/LIVE VALIDATION of a statistically significant
(p=0.013) result that was SELECTED FROM 29 CANDIDATES (GBPCAD/USDCHF/XAUUSD)
- see METHODOLOGY.md for the selection-bias caveat. NOT a confirmed edge.

Every hour (on H1 candle close), for each symbol in ACTIVE_SYMBOLS the FULL
history is recomputed via strategy.generate_live_signal() - there is NO
separate/duplicated "live" signal-calculation code path that could diverge
from the backtest. The result (pending orders / open trade) is
SYNCHRONIZED against the real MT5 state:
  - If the algorithm wants a NEW pending order that MT5 doesn't have yet ->
    a REAL BUY_LIMIT/SELL_LIMIT order is placed (with expiration).
  - If MT5 has an order the algorithm NO LONGER wants (stale / OB
    touched by newer data) -> the order is cancelled.
  - Fill/close happens NATURALLY through MT5 (SL/TP are already set on the
    order) - the bot only WATCHES and REPORTS.
"""
import time
import logging
import hashlib
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import pandas as pd
import pytz

from config import (
    ACTIVE_SYMBOLS, RISK_PER_TRADE_PERCENT, MAX_CONCURRENT_RISK_ORDERS,
    MAX_TRADES_PER_BASE_CURRENCY, COMMISSION_POINTS, AVG_SLIPPAGE_POINTS,
    MAX_CONSECUTIVE_LOSSES, LOCKOUT_DURATION_MINUTES, DAILY_LOSS_LIMIT_PERCENT,
    MAGIC_NUMBER, MT5_TERMINAL_PATH, H1_BARS_FOR_SIGNAL, PENDING_ORDER_EXPIRY_BARS,
    ATR_SL_BUFFER, setup_logging,
)
from mt5_interface import MT5Interface
from risk_manager import RiskManager
from strategy import generate_live_signal
from telegram_alerts import send_telegram_message, load_chat_id, get_updates

BROKER_TZ = pytz.timezone("Europe/Vilnius")
TELEGRAM_COMMAND_CHECK_SEC = 15


def get_initial_telegram_offset():
    updates = get_updates()
    if updates:
        return max(u["update_id"] for u in updates) + 1
    return None


def pending_order_id(o):
    """MT5's order 'comment' field is limited (~31 chars) - using a STABLE
    hash (NOT Python's built-in hash(), which is randomized per process) for
    identity across bot restarts. 'C' prefix + 12 hex chars = 13 chars,
    fits safely."""
    raw = f"{o['side']}_{o['zone_top']:.6f}_{o['zone_bottom']:.6f}_{o['sl']:.6f}"
    return "C" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def format_status_message(m5, balance, active_risk_orders, is_locked, lock_reason, trading_paused):
    lines = ["🧪 *ChochOB_Bot status*", f"Balance: {balance:.2f}",
             f"Active risk orders: {active_risk_orders}/{MAX_CONCURRENT_RISK_ORDERS}"]
    if trading_paused:
        lines.append("⏸️ PAUSED - new trade search stopped (/resume to continue)")
    lines.append(f"🔒 LOCKED: {lock_reason}" if is_locked else "✅ Running normally")

    positions = m5.get_positions()
    mine = [p for p in positions if p['magic'] == m5.magic_number] if positions else []
    if mine:
        lines.append("\nOpen positions:")
        for p in mine:
            side = "BUY" if p['type'] == mt5.POSITION_TYPE_BUY else "SELL"
            lines.append(f"  {p['symbol']} {side} vol={p['volume']} PnL={p['profit']:.2f}")
    else:
        lines.append("\nNo open positions.")

    pending = m5.get_pending_orders()
    mine_p = pending or []
    if mine_p:
        lines.append("\nPending orders:")
        for o in mine_p:
            lines.append(f"  {o['symbol']} type={o['type']} @ {o['price_open']}")
    return "\n".join(lines)


def check_remote_commands(m5, telegram_offset, authorized_chat_id, balance, active_risk_orders, is_locked, lock_reason, trading_paused):
    updates = get_updates(offset=telegram_offset)
    new_offset = telegram_offset
    should_stop = False
    pause_action = None
    for u in updates:
        new_offset = u["update_id"] + 1
        msg = u.get("message", {})
        chat = msg.get("chat", {})
        text = (msg.get("text") or "").strip().lower()

        if authorized_chat_id is None or str(chat.get("id")) != str(authorized_chat_id):
            continue

        if text == "/stop":
            send_telegram_message(
                "🛑 *ChochOB_Bot STOPPING* remotely.\n"
                "Open positions AND pending orders REMAIN - they will NOT be closed/cancelled.\n"
                "To restart: run main.py again."
            )
            should_stop = True
        elif text == "/pause":
            pause_action = "pause"
            send_telegram_message("⏸️ *ChochOB_Bot PAUSED* - new order search stopped.\nTo resume: /resume")
        elif text == "/resume":
            pause_action = "resume"
            send_telegram_message("▶️ *ChochOB_Bot RESUMED* - new order search active again.")
        elif text == "/status":
            send_telegram_message(format_status_message(m5, balance, active_risk_orders, is_locked, lock_reason, trading_paused))

    return new_offset, should_stop, pause_action


def try_notify_closed_trade(ticket, symbol):
    deals = mt5.history_deals_get(position=int(ticket))
    if not deals:
        return False
    out_deal = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
    if out_deal is None:
        return False
    profit = out_deal.profit + out_deal.commission + out_deal.swap
    emoji = "✅" if profit >= 0 else "🔻"
    send_telegram_message(f"{emoji} *ChochOB_Bot*: {symbol} trade CLOSED (Ticket: {ticket}) | PnL: {profit:+.2f}")
    logging.info(f"{emoji} [TRADE CLOSED] {symbol} (Ticket: {ticket}) PnL={profit:+.2f}")
    return True


def apply_trailing_stop(m5, symbol, live_state):
    """Trails the open trade's SL to the latest confirmed H1 swing point -
    ONLY if the new SL is MORE FAVORABLE than the current one (never
    loosens it). Validated on backtest (GBPCAD/USDCHF/XAUUSD, 5 periods):
    n=31 Total=+17.29R (without trailing: +16.68R), p=0.0145, TOP-5=70.3%
    (not a 'lottery' result - all 3 pairs contribute). See METHODOLOGY.md."""
    last_low = live_state.get('last_confirmed_low')
    last_high = live_state.get('last_confirmed_high')
    atr = live_state.get('last_atr')
    if atr is None:
        return

    positions = m5.get_positions()
    if not positions:
        return
    mine = [p for p in positions if p['symbol'] == symbol and p['magic'] == m5.magic_number]
    if not mine:
        return
    pos = mine[0]
    side = "BUY" if pos['type'] == mt5.POSITION_TYPE_BUY else "SELL"
    current_sl = pos['sl']
    current_tp = pos['tp']

    new_sl = None
    if side == "BUY" and last_low is not None:
        cand = last_low - atr * ATR_SL_BUFFER
        if current_sl == 0 or cand > current_sl:
            new_sl = cand
    elif side == "SELL" and last_high is not None:
        cand = last_high + atr * ATR_SL_BUFFER
        if current_sl == 0 or cand < current_sl:
            new_sl = cand

    if new_sl is None:
        return

    if m5.modify_sl(pos['ticket'], symbol, new_sl, current_tp):
        send_telegram_message(
            f"📈 *ChochOB_Bot*: {symbol} SL moved (trailing) {current_sl:.5f} -> {new_sl:.5f}"
        )
        logging.info(f"📈 [TRAILING SL] {symbol} (Ticket: {pos['ticket']}) {current_sl:.5f} -> {new_sl:.5f}")
    else:
        logging.error(f"❌ {symbol}: trailing SL update failed (Ticket: {pos['ticket']}).")


def sync_symbol(m5, risk_manager, symbol, balance, active_risk_orders, active_currencies, open_symbols):
    """Recomputes the signal for one pair and synchronizes it with the real
    MT5 state (adds missing pending orders, cancels ones no longer needed,
    and if a trade is already open - checks/updates the trailing SL)."""
    base, quote = symbol[:3], symbol[3:6]

    if symbol not in open_symbols:
        on_cooldown, _ = risk_manager.is_symbol_on_cooldown(symbol)
        if on_cooldown:
            return active_risk_orders, active_currencies

        if active_currencies.count(base) >= MAX_TRADES_PER_BASE_CURRENCY or \
           active_currencies.count(quote) >= MAX_TRADES_PER_BASE_CURRENCY:
            return active_risk_orders, active_currencies

    mt5.symbol_select(symbol, True)
    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        logging.warning(f"⚠️ {symbol}: symbol_info() returned no data, skipped.")
        return active_risk_orders, active_currencies

    point = sym_info.point
    real_spread = sym_info.spread
    cost_price = (real_spread + COMMISSION_POINTS + AVG_SLIPPAGE_POINTS) * point

    h1_df = m5.get_data(symbol, "H1", n=H1_BARS_FOR_SIGNAL)
    if h1_df is None or len(h1_df) < 200:
        logging.warning(f"⚠️ {symbol}: not enough H1 data, skipped.")
        return active_risk_orders, active_currencies

    h4_ohlc = h1_df.set_index('time')[['open', 'high', 'low', 'close']].resample('4h').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    h4_df = h4_ohlc.reset_index()
    if len(h4_df) < 50:
        return active_risk_orders, active_currencies

    live_state = generate_live_signal(symbol, h4_df, h1_df, cost_price, PENDING_ORDER_EXPIRY_BARS)

    if symbol in open_symbols:
        apply_trailing_stop(m5, symbol, live_state)
        return active_risk_orders, active_currencies

    # -- real MT5 pending orders (for this symbol, this magic number) --
    all_pending = m5.get_pending_orders()
    if all_pending is None:
        logging.error(f"❌ {symbol}: orders_get() error, skipping this cycle.")
        return active_risk_orders, active_currencies
    real_pending_sym = [o for o in all_pending if o['symbol'] == symbol]
    real_ids = {o['comment']: o for o in real_pending_sym}

    wanted_ids = set()
    if not live_state['in_trade']:
        for o in live_state['pending_orders']:
            if active_risk_orders >= MAX_CONCURRENT_RISK_ORDERS:
                break
            pid = pending_order_id(o)
            wanted_ids.add(pid)
            if pid in real_ids:
                continue  # already placed

            side = o['side']
            entry_price = (o['zone_top'] + o['zone_bottom']) / 2.0
            risk_dist = abs(entry_price - o['sl'])
            if risk_dist <= 0:
                continue
            sl_dist_points = risk_dist / point
            lot = risk_manager.calculate_lot_size(symbol, balance, RISK_PER_TRADE_PERCENT, sl_dist_points)
            if not lot:
                continue
            if not risk_manager.check_margin_availability(symbol, lot, side, entry_price):
                continue

            expiration_dt = datetime.now(BROKER_TZ).replace(tzinfo=None) + timedelta(hours=PENDING_ORDER_EXPIRY_BARS)
            result = m5.place_pending_order(symbol, side, lot, entry_price, o['sl'], o['tp'], expiration_dt, comment=pid)
            if result.get("success"):
                active_risk_orders += 1
                active_currencies.append(base); active_currencies.append(quote)
                send_telegram_message(
                    f"📌 *ChochOB_Bot*: NEW pending order {side} {symbol} @ {entry_price:.5f}\n"
                    f"SL: {o['sl']:.5f} | TP: {o['tp']:.5f} | Lot: {lot} | valid until {expiration_dt.date()}"
                )
            else:
                logging.error(f"❌ {symbol}: pending order error - {result.get('error')}")

    # -- cancel real orders the algorithm no longer wants --
    for pid, real_o in real_ids.items():
        if pid not in wanted_ids:
            if m5.cancel_pending_order(real_o['ticket']):
                send_telegram_message(f"🗑️ *ChochOB_Bot*: cancelled stale pending order {symbol} (Ticket: {real_o['ticket']})")

    return active_risk_orders, active_currencies


def main():
    setup_logging()
    logging.info("=====================================================")
    logging.info(" 🧪 ChochOB_Bot: CHoCH + untouched H1 OB - FORWARD/LIVE VALIDATION ")
    logging.info("=====================================================")

    m5 = MT5Interface(magic_number=MAGIC_NUMBER)
    if not m5.connect():
        return

    acc_info = m5.get_account_info()
    if not acc_info:
        return
    balance = acc_info['balance']
    logging.info(f"✅ Connected. Account: {acc_info.get('login')} ({acc_info.get('server')}) "
                 f"Balance: {balance:.2f} {acc_info.get('currency', '')}")
    # Account-type safeguards are intentionally minimal here: every account this
    # bot has run against is a demo / prop-firm challenge account, no real
    # money. The log line below is purely informational, so the Telegram
    # message and log ALWAYS show exactly which account is connected.
    trade_mode_map = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
    logging.info(f"ℹ️ Account type: {trade_mode_map.get(acc_info.get('trade_mode'), '?')} "
                 f"(trade_mode={acc_info.get('trade_mode')})")

    risk_manager = RiskManager(m5, state_file="chochob_state.json")

    authorized_chat_id = load_chat_id()
    telegram_offset = get_initial_telegram_offset()
    if authorized_chat_id:
        logging.info(f"📲 [REMOTE CONTROL] Active chat_id={authorized_chat_id}. /stop /pause /resume /status")

    send_telegram_message(
        f"🔌 *ChochOB_Bot* started successfully! "
        f"(account {acc_info.get('login')}, {trade_mode_map.get(acc_info.get('trade_mode'), '?')}, {acc_info.get('server')})\n"
        f"Tracked pairs: {', '.join(ACTIVE_SYMBOLS)} | Risk/trade: {RISK_PER_TRADE_PERCENT}%\n"
        f"Commands: /status | /pause | /resume | /stop"
    )

    last_scanned_hour_key = None
    last_heartbeat = 0
    last_telegram_check = 0
    trading_paused = False
    consecutive_api_errors = 0
    is_locked = False
    lock_reason = "OK"
    active_risk_orders = 0
    known_open_tickets = {}
    pending_closure_notify = {}

    while True:
        try:
            current_time = time.time()
            now = datetime.now(BROKER_TZ).replace(tzinfo=None)

            term_info = mt5.terminal_info()
            if term_info is None or not term_info.connected:
                consecutive_api_errors += 1
                if current_time - last_heartbeat >= 30:
                    logging.error("🔴 CONNECTION ERROR: MT5 terminal lost connection. Scanning paused...")
                    last_heartbeat = current_time
                if not mt5.initialize(path=MT5_TERMINAL_PATH):
                    logging.error(f"🔁 Reconnect attempt failed: {mt5.last_error()}")
                time.sleep(5)
                continue
            else:
                consecutive_api_errors = 0

            if current_time - last_telegram_check >= TELEGRAM_COMMAND_CHECK_SEC:
                telegram_offset, should_stop, pause_action = check_remote_commands(
                    m5, telegram_offset, authorized_chat_id, balance, active_risk_orders, is_locked, lock_reason, trading_paused
                )
                last_telegram_check = current_time
                if should_stop:
                    break
                if pause_action == "pause":
                    trading_paused = True
                elif pause_action == "resume":
                    trading_paused = False

            if current_time - last_heartbeat >= 60:
                status_suffix = " (PAUSED)" if trading_paused else ""
                logging.info(f"💓 [STANDBY] ChochOB_Bot scanning {len(ACTIVE_SYMBOLS)} pairs{status_suffix}.")
                last_heartbeat = current_time

            acc_info = m5.get_account_info()
            if acc_info:
                balance = acc_info['balance']

            is_locked, lock_reason = risk_manager.check_circuit_breakers(
                balance, DAILY_LOSS_LIMIT_PERCENT, MAX_CONSECUTIVE_LOSSES, LOCKOUT_DURATION_MINUTES
            )
            if is_locked:
                time.sleep(5)
                continue

            weekday = now.weekday()
            hour = now.hour
            is_weekend = (weekday == 4 and hour >= 20) or weekday == 5 or weekday == 6 or (weekday == 0 and hour < 9)
            if is_weekend:
                if now.minute % 30 == 0 and now.second < 2:
                    logging.info("💤 Weekend. ChochOB_Bot sleeping...")
                time.sleep(10)
                continue

            positions = m5.get_positions()
            if positions is None:
                time.sleep(1)
                continue

            active_risk_orders = 0
            active_currencies = []
            open_symbols = set()
            current_tickets = {}
            for pos in positions:
                if pos['magic'] != m5.magic_number:
                    continue
                sym = pos['symbol']
                current_tickets[str(pos['ticket'])] = sym
                open_symbols.add(sym)
                active_currencies.append(sym[:3]); active_currencies.append(sym[3:6])
                active_risk_orders += 1

            for ticket, sym in known_open_tickets.items():
                if ticket not in current_tickets and ticket not in pending_closure_notify:
                    pending_closure_notify[ticket] = {'symbol': sym, 'attempts': 0}
            known_open_tickets = current_tickets

            for ticket in list(pending_closure_notify.keys()):
                info = pending_closure_notify[ticket]
                if try_notify_closed_trade(ticket, info['symbol']):
                    del pending_closure_notify[ticket]
                else:
                    info['attempts'] += 1
                    if info['attempts'] >= 6:
                        del pending_closure_notify[ticket]

            current_hour_key = now.strftime("%Y-%m-%d %H")
            if not trading_paused and current_hour_key != last_scanned_hour_key:
                risk_manager.synchronize_active_trades()
                logging.info("\n🌍 [H1 SCAN] Checking CHoCH+OB signals...")
                for symbol in ACTIVE_SYMBOLS:
                    active_risk_orders, active_currencies = sync_symbol(
                        m5, risk_manager, symbol, balance, active_risk_orders, active_currencies, open_symbols
                    )
                last_scanned_hour_key = current_hour_key

            time.sleep(5)

        except KeyboardInterrupt:
            logging.info("⏹️ ChochOB_Bot stopped manually.")
            break
        except Exception as e:
            logging.error(f"❌ UNEXPECTED ERROR in main loop: {e}", exc_info=True)
            time.sleep(5)

    mt5.shutdown()
    logging.info("👋 ChochOB_Bot process finished.")


if __name__ == "__main__":
    main()
