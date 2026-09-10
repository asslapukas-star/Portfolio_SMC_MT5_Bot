import MetaTrader5 as mt5
import logging
import pandas as pd
import time
from config import MT5_TERMINAL_PATH

class MT5Interface:
    def __init__(self, magic_number=245577):
        self.magic_number = magic_number

    def connect(self):
        terminal_path = MT5_TERMINAL_PATH
        if not mt5.initialize(path=terminal_path):
            logging.error(f"Failed to connect to MT5. Error: {mt5.last_error()}")
            return False
        return True

    def get_account_info(self):
        account_info = mt5.account_info()
        if account_info is None:
            logging.error(f"Failed to get account info. Error: {mt5.last_error()}")
            return None
        return account_info._asdict()

    def get_tick(self, symbol):
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return tick._asdict()

    def resolve_symbol(self, symbol):
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            return symbol
        if not symbol_info.visible:
            if not mt5.symbol_select(symbol, True):
                logging.error(f"Failed to enable symbol {symbol}")
        return symbol

    def get_data(self, symbol, timeframe, n=1000):
        self.resolve_symbol(symbol)
        tf_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
        }
        rates = mt5.copy_rates_from_pos(symbol, tf_map.get(timeframe, mt5.TIMEFRAME_H1), 0, n)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df.sort_values('time').drop_duplicates('time').reset_index(drop=True)
        return df

    def get_positions(self):
        positions = mt5.positions_get()
        if positions is None:
            logging.error(f"❌ mt5.positions_get() returned None (API/connection error): {mt5.last_error()}")
            return None
        return [p._asdict() for p in positions]

    def get_pending_orders(self):
        """Returns ALL pending (not yet filled) orders with THIS bot's
        magic_number. None = API error, [] = confirmed none exist."""
        orders = mt5.orders_get()
        if orders is None:
            logging.error(f"❌ mt5.orders_get() returned None (API/connection error): {mt5.last_error()}")
            return None
        return [o._asdict() for o in orders if o.magic == self.magic_number]

    def place_pending_order(self, symbol, side, lot, entry_price, sl, tp, expiration_dt, comment="ChochOB"):
        """Places a REAL LIMIT pending order (BUY_LIMIT/SELL_LIMIT) - NOT a
        market order. entry_price - the OB zone's midpoint price (matches
        the backtest assumption). expiration_dt - datetime when the order
        automatically expires (if it wasn't filled in time) - MT5 handles
        this itself, the bot doesn't need to track it manually."""
        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            logging.error(f"Symbol {symbol} not found in the local terminal.")
            return {"error": f"Symbol {symbol} not found"}

        digits = symbol_info.digits
        entry_price = round(float(entry_price), digits)
        sl = round(float(sl), digits)
        tp = round(float(tp), digits)

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"error": "Failed to get tick"}

        if side.upper() == "BUY":
            order_type = mt5.ORDER_TYPE_BUY_LIMIT if entry_price < tick.ask else mt5.ORDER_TYPE_BUY_STOP
        else:
            order_type = mt5.ORDER_TYPE_SELL_LIMIT if entry_price > tick.bid else mt5.ORDER_TYPE_SELL_STOP

        filling_type = mt5.ORDER_FILLING_IOC
        if symbol_info.filling_mode & 1:
            filling_type = mt5.ORDER_FILLING_FOK
        elif symbol_info.filling_mode & 2:
            filling_type = mt5.ORDER_FILLING_IOC

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": float(lot),
            "type": order_type,
            "price": entry_price,
            "sl": sl,
            "tp": tp,
            "magic": self.magic_number,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_SPECIFIED,
            "expiration": int(expiration_dt.timestamp()),
            "type_filling": filling_type,
        }

        logging.info(f"📌 [PENDING ORDER] {symbol} {side} @ {entry_price} SL={sl} TP={tp} "
                     f"lot={lot} valid until {expiration_dt}")
        result = mt5.order_send(request)
        if result is None:
            logging.error(f"MT5 terminal did not respond (None) for symbol {symbol}")
            return {"error": "Terminal did not respond"}
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"❌ Pending order error {symbol}: {result.comment} ({result.retcode})")
            return {"error": f"{result.comment} ({result.retcode})"}

        logging.info(f"✅ [PENDING ORDER PLACED] {symbol} Ticket: {result.order}")
        return {"success": True, "ticket": result.order}

    def cancel_pending_order(self, ticket):
        request = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)}
        result = mt5.order_send(request)
        if result is None:
            logging.error(f"cancel_pending_order: terminal did not respond, ticket={ticket}")
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"cancel_pending_order error ticket={ticket}: {result.comment} ({result.retcode})")
            return False
        logging.info(f"🗑️ [PENDING ORDER CANCELLED] Ticket: {ticket}")
        return True

    def modify_sl(self, ticket, symbol, new_sl, tp):
        sym_info = mt5.symbol_info(symbol)
        if not sym_info:
            logging.error(f"modify_sl: Failed to get sym_info for pair {symbol} | Ticket: {ticket}")
            return False

        new_sl_rounded = round(float(new_sl), sym_info.digits)
        tp_rounded = round(float(tp), sym_info.digits) if tp else 0.0

        positions = mt5.positions_get(ticket=ticket)
        if positions and len(positions) > 0:
            pos = positions[0]._asdict()
            current_sl_rounded = round(pos['sl'], sym_info.digits)
            current_tp_rounded = round(pos['tp'], sym_info.digits) if pos['tp'] else 0.0
            if new_sl_rounded == current_sl_rounded and tp_rounded == current_tp_rounded:
                return True

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": symbol,
            "sl": new_sl_rounded,
            "tp": tp_rounded
        }
        result = mt5.order_send(request)
        if result is None:
            logging.error(f"modify_sl: Local MT5 terminal did not respond (None) | Ticket: {ticket}")
            return False
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logging.error(f"modify_sl error: {result.comment} ({result.retcode}) | Ticket: {ticket}")
            return False
        return True
