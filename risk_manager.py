import logging
import MetaTrader5 as mt5
import time as py_time
from config import COOLDOWN_HOURS, MAX_RISK_MULTIPLIER_ON_MIN_LOT

import json
import os

class RiskManager:
    def __init__(self, m5_interface, state_file="trades_state.json"):
        self.m5 = m5_interface
        self.state_file = state_file
        self.active_trades = self._load_state()
        self._check_account_switch()
        self.synchronize_active_trades()

    def _check_account_switch(self):
        """
        If the MT5 account connected at bot startup differs from the one
        self.active_trades' saved state belongs to - the old account's
        kill-switch baseline (initial_deposit), daily-loss baseline
        (daily_starting_equity), and tracked tickets/cooldowns CANNOT be
        applied to the new account (different balance, different history).
        Detected automatically.
        """
        account = mt5.account_info()
        if account is None:
            logging.warning("⚠️ [ACCOUNT CHECK] Failed to get account_info() - skipping account-switch check this run.")
            return

        current_login = account.login
        stored_login = self.active_trades.get("account_login")

        if stored_login is not None and stored_login != current_login:
            logging.warning(
                f"🔄 [ACCOUNT SWITCH] Detected a different MT5 account (was: {stored_login}, now: {current_login}, "
                f"balance: {account.balance:.2f} {account.currency}). Clearing OLD state (kill-switch baseline, "
                f"daily limit, tracked tickets, cooldowns) - everything is reset from ZERO for this new account."
            )
            self.active_trades = {}

        self.active_trades["account_login"] = current_login
        self._save_state()

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.critical(
                f"‼️ STATE FILE READ ERROR: '{self.state_file}' exists but could not be "
                f"read/parsed ({type(e).__name__}: {e}). Kill-switch and daily-loss "
                f"baselines will be RESET from the current account state."
            )
            try:
                backup_path = f"{self.state_file}.corrupted_{int(py_time.time())}"
                os.replace(self.state_file, backup_path)
                logging.critical(f"‼️ Corrupted state file moved to: {backup_path} (not deleted, just relocated).")
            except Exception as backup_err:
                logging.critical(f"‼️ Failed to save a copy of the corrupted state file: {backup_err}")
            return {}

    def _save_state(self):
        tmp_file = self.state_file + ".tmp"
        try:
            with open(tmp_file, 'w') as f:
                json.dump(self.active_trades, f, indent=4)
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            logging.error(f"Failed to save state file: {e}")

    def synchronize_active_trades(self):
        """
        Synchronizes in-memory state with the actually-open positions in the
        MT5 terminal. Protects against situations where the bot was offline
        or the state file got corrupted. Also applies a cooldown to trades
        that closed while the bot was offline.
        """
        positions = self.m5.get_positions()
        if positions is None:
            logging.critical("‼️ SELF-HEAL SKIPPED at startup: mt5.positions_get() error - state left unchanged.")
            return
        active_tickets = []
        state_changed = False

        for pos in positions:
            if pos['magic'] == self.m5.magic_number:
                ticket = str(pos['ticket'])
                active_tickets.append(ticket)
                if ticket not in self.active_trades:
                    self.active_trades[ticket] = {
                        "symbol": pos['symbol'],
                        "initial_sl": pos['sl'],
                        "partial_closed": False
                    }
                    logging.info(f"🔄 SELF-HEAL: Registered a position found in MT5 {pos['symbol']} (Ticket: {ticket}, SL: {pos['sl']})")
                    state_changed = True

        for ticket in list(self.active_trades.keys()):
            if ticket.isdigit() and ticket not in active_tickets:
                try:
                    deals = mt5.history_deals_get(position=int(ticket))
                    if deals:
                        out_deal = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
                        if out_deal:
                            total_pnl = out_deal.profit + out_deal.commission + out_deal.swap
                            if total_pnl < 0:
                                symbol = self.active_trades[ticket]["symbol"]
                                self.active_trades[f"cooldown_{symbol}"] = py_time.time() + COOLDOWN_HOURS * 3600
                                logging.warning(f"📉 SELF-HEAL: Pair {symbol} (Ticket: {ticket}) closed with a loss ({total_pnl:.2f} USD) while the bot was offline. Enabling a {COOLDOWN_HOURS}h cooldown.")
                except Exception as e:
                    logging.error(f"Self-heal error check: {e}")

                del self.active_trades[ticket]
                logging.info(f"🔄 SELF-HEAL: Cleared no-longer-existing ticket {ticket} from state.")
                state_changed = True

        if state_changed:
            self._save_state()

    def register_trade(self, ticket, symbol, initial_sl):
        sym_info = mt5.symbol_info(symbol)
        digits = sym_info.digits if sym_info else 5
        self.active_trades[str(ticket)] = {
            "symbol": symbol,
            "initial_sl": round(float(initial_sl), digits),
            "partial_closed": False
        }
        self._save_state()

    def _get_symbol_suffix(self):
        from config import ACTIVE_SYMBOLS
        if ACTIVE_SYMBOLS:
            first_sym = ACTIVE_SYMBOLS[0]
            if len(first_sym) > 6:
                return first_sym[6:]
        return ""

    def get_rate_helper(self, from_cur, to_cur):
        if from_cur == to_cur:
            return 1.0
        suffix = self._get_symbol_suffix()

        pair1 = f"{from_cur}{to_cur}{suffix}"
        mt5.symbol_select(pair1, True)
        tick = mt5.symbol_info_tick(pair1)
        if tick is not None and tick.bid > 0:
            return tick.bid

        pair2 = f"{to_cur}{from_cur}{suffix}"
        mt5.symbol_select(pair2, True)
        tick = mt5.symbol_info_tick(pair2)
        if tick is not None and tick.bid > 0:
            return 1.0 / tick.bid

        return None

    def get_exchange_rate(self, profit_currency, account_currency):
        """
        Returns the exchange rate from the profit currency to the account
        currency. Guards against broker MT5 tick_value anomalies by using
        cross-conversion through USD.
        """
        if profit_currency == account_currency:
            return 1.0

        rate = self.get_rate_helper(profit_currency, account_currency)
        if rate is not None:
            return rate

        if profit_currency != "USD" and account_currency != "USD":
            rate_to_usd = self.get_rate_helper(profit_currency, "USD")
            rate_from_usd = self.get_rate_helper("USD", account_currency)
            if rate_to_usd is not None and rate_from_usd is not None:
                logging.info(f"🔀 [FX CROSS] Cross FX conversion: {profit_currency} -> USD -> {account_currency}")
                return rate_to_usd * rate_from_usd

        logging.warning(f"[LOT CALCULATOR] Could not find an exchange rate for {profit_currency}/{account_currency}. Using an approximate rate.")
        if profit_currency == "USD" and account_currency == "EUR":
            return 0.92
        if profit_currency == "EUR" and account_currency == "USD":
            return 1.08
        if profit_currency == "JPY" and account_currency == "EUR":
            return 0.0054
        if profit_currency == "JPY" and account_currency == "USD":
            return 0.0065

        raise ValueError(f"Could not find an FX rate from {profit_currency} to {account_currency} (even via USD cross-conversion).")

    def calculate_lot_size(self, symbol, balance, risk_percent, sl_dist_points):
        sym_info = mt5.symbol_info(symbol)
        if not sym_info or sl_dist_points <= 0:
            return 0.01

        account = mt5.account_info()
        if account is None:
            return 0.01

        risk_money = balance * (risk_percent / 100.0)

        point = sym_info.point
        contract_size = sym_info.trade_contract_size
        loss_per_lot_quote = sl_dist_points * point * contract_size

        profit_currency = sym_info.currency_profit
        account_currency = account.currency
        exchange_rate = self.get_exchange_rate(profit_currency, account_currency)

        loss_per_lot_acc = loss_per_lot_quote * exchange_rate

        if loss_per_lot_acc <= 0:
            return sym_info.volume_min

        lot = risk_money / loss_per_lot_acc

        step = sym_info.volume_step
        lot = round(lot / step) * step

        # If the rounded lot is SMALLER than the broker's minimum, we do NOT force it up to
        # volume_min - if the minimum lot's risk exceeds MAX_RISK_MULTIPLIER_ON_MIN_LOT times
        # the intended risk, the signal is SKIPPED (returns None) instead of being accepted
        # with higher-than-intended risk.
        if lot < sym_info.volume_min:
            risk_at_min_lot = sym_info.volume_min * loss_per_lot_acc
            risk_multiplier = risk_at_min_lot / risk_money if risk_money > 0 else float('inf')
            if risk_multiplier > MAX_RISK_MULTIPLIER_ON_MIN_LOT:
                logging.info(
                    f"⏩ [RISK LIMIT] {symbol}: theoretical lot ({risk_money / loss_per_lot_acc:.5f}) "
                    f"is below the minimum ({sym_info.volume_min}), and the minimum lot's risk "
                    f"({risk_at_min_lot:.2f} {account_currency}) would be {risk_multiplier:.2f}x larger "
                    f"than intended ({risk_money:.2f} {account_currency}) - exceeds the "
                    f"MAX_RISK_MULTIPLIER_ON_MIN_LOT={MAX_RISK_MULTIPLIER_ON_MIN_LOT}x limit. Signal skipped."
                )
                return None
            lot = sym_info.volume_min

        lot = max(sym_info.volume_min, min(sym_info.volume_max, lot))

        logging.info(
            f"[LOT CALCULATOR] Balance: {balance:.2f} {account_currency} | "
            f"Risk: {risk_percent}% ({risk_money:.2f} {account_currency}) | "
            f"SL points: {sl_dist_points} | "
            f"Quote currency: {profit_currency} | "
            f"Rate: {exchange_rate:.4f} | "
            f"Risk per 1 lot in account currency: {loss_per_lot_acc:.2f} {account_currency} | "
            f"Calculated lot: {lot:.2f}"
        )

        return round(lot, 2)

    def apply_rollover_protection(self, expansion_points=300):
        """Widens the SL of all open orders by expansion_points - protection against
        the broker's rollover-time (23:55-01:00) artificial spread spike, which
        could otherwise hit the SL regardless of real price movement."""
        positions = self.m5.get_positions()
        count = 0
        if positions is None:
            logging.error("❌ apply_rollover_protection(): mt5.positions_get() error - attempt skipped this cycle.")
            return count
        for pos in positions:
            if pos['magic'] != self.m5.magic_number:
                continue

            try:
                ticket = str(pos['ticket'])
                symbol = pos['symbol']
                current_sl = pos['sl']
                trade_type = pos['type']
                tp = pos['tp']

                if ticket not in self.active_trades:
                    self.register_trade(ticket, symbol, current_sl)

                trade_data = self.active_trades[ticket]

                if trade_data.get("rollover_expanded", False):
                    continue

                last_attempt = trade_data.get("last_expand_attempt", 0)
                retries = trade_data.get("rollover_expand_retries", 0)
                if retries >= 3:
                    if py_time.time() - last_attempt < 300:
                        continue
                else:
                    if py_time.time() - last_attempt < 30:
                        continue

                sym_info = mt5.symbol_info(symbol)
                if not sym_info: continue

                if current_sl <= 0: continue

                digits = sym_info.digits
                expansion = expansion_points * sym_info.point

                if trade_type == mt5.POSITION_TYPE_BUY:
                    new_sl = current_sl - expansion
                else:
                    new_sl = current_sl + expansion

                if new_sl < 0: new_sl = 0.00001

                new_sl_rounded = round(float(new_sl), digits)
                current_sl_rounded = round(float(current_sl), digits)

                if new_sl_rounded == current_sl_rounded:
                    trade_data["rollover_expanded"] = True
                    trade_data["pre_rollover_sl"] = current_sl_rounded
                    trade_data["rollover_expand_retries"] = 0
                    self._save_state()
                    count += 1
                    continue

                if self.m5.modify_sl(int(ticket), symbol, new_sl_rounded, tp):
                    logging.info(f"🛡️ OVERNIGHT PROTECTION: {symbol} (Ticket: {ticket}) SL widened from {current_sl_rounded} to {new_sl_rounded} (30 pips)")
                    trade_data["rollover_expanded"] = True
                    trade_data["pre_rollover_sl"] = current_sl_rounded
                    trade_data["rollover_expand_retries"] = 0
                    self._save_state()
                    count += 1
                else:
                    trade_data["rollover_expand_retries"] = retries + 1
                    trade_data["last_expand_attempt"] = py_time.time()
                    self._save_state()
                    logging.error(f"❌ OVERNIGHT PROTECTION: Failed to widen SL for pair {symbol} (Ticket: {ticket}). Attempt {retries + 1}/3.")
            except Exception as e:
                logging.error(f"❌ OVERNIGHT PROTECTION: Error processing position {pos.get('symbol', '?')} (Ticket: {pos.get('ticket', '?')}): {e}. Other positions will still be processed.")
                continue
        return count

    def remove_rollover_protection(self):
        """Returns the SL to its original place (before widening)."""
        positions = self.m5.get_positions()
        count = 0
        if positions is None:
            logging.critical("‼️ OVERNIGHT PROTECTION (MORNING) SKIPPED: mt5.positions_get() error - state left unchanged.")
            return count
        active_tickets = [str(p['ticket']) for p in positions]

        state_changed = False
        for ticket, trade_data in list(self.active_trades.items()):
            if not ticket.isdigit():
                continue

            try:
                if ticket not in active_tickets:
                    try:
                        deals = mt5.history_deals_get(position=int(ticket))
                        if deals:
                            out_deal = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT), None)
                            if out_deal:
                                total_pnl = out_deal.profit + out_deal.commission + out_deal.swap
                                if total_pnl < 0:
                                    symbol = trade_data["symbol"]
                                    self.active_trades[f"cooldown_{symbol}"] = py_time.time() + COOLDOWN_HOURS * 3600
                                    logging.warning(f"📉 LOSS: Pair {symbol} (Ticket: {ticket}) closed with a loss: {total_pnl:.2f} USD. Enabling a {COOLDOWN_HOURS}h trading cooldown.")
                    except Exception as e:
                        logging.error(f"Error checking closed-trade result for cooldown risk: {e}")

                    del self.active_trades[ticket]
                    state_changed = True
                    continue

                if trade_data.get("rollover_expanded", False):
                    symbol = trade_data["symbol"]

                    last_attempt = trade_data.get("last_remove_attempt", 0)
                    retries = trade_data.get("rollover_remove_retries", 0)
                    if retries >= 3:
                        if py_time.time() - last_attempt < 300:
                            continue
                    else:
                        if py_time.time() - last_attempt < 30:
                            continue

                    original_sl = trade_data.get("pre_rollover_sl", trade_data["initial_sl"])

                    pos = next((p for p in positions if str(p['ticket']) == ticket), None)
                    if pos:
                        tick = mt5.symbol_info_tick(symbol)
                        has_crossed_sl = False

                        if tick:
                            if pos['type'] == mt5.POSITION_TYPE_BUY and tick.bid <= original_sl:
                                has_crossed_sl = True
                            elif pos['type'] == mt5.POSITION_TYPE_SELL and tick.ask >= original_sl:
                                has_crossed_sl = True

                        if has_crossed_sl:
                            logging.warning(
                                f"🚨 OVERNIGHT PROTECTION: {symbol} (Ticket: {ticket}) price broke through the original SL level {original_sl} overnight. "
                                f"Closing the trade immediately at market price!"
                            )

                            close_type = mt5.ORDER_TYPE_SELL if pos['type'] == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
                            price = tick.bid if pos['type'] == mt5.POSITION_TYPE_BUY else tick.ask

                            filling_type = mt5.ORDER_FILLING_IOC
                            sym_info = mt5.symbol_info(symbol)
                            if sym_info:
                                if sym_info.filling_mode & 1:
                                    filling_type = mt5.ORDER_FILLING_FOK
                                elif sym_info.filling_mode & 2:
                                    filling_type = mt5.ORDER_FILLING_IOC

                            close_req = {
                                "action": mt5.TRADE_ACTION_DEAL,
                                "symbol": symbol,
                                "volume": pos['volume'],
                                "type": close_type,
                                "price": price,
                                "position": int(ticket),
                                "deviation": 20,
                                "magic": self.m5.magic_number,
                                "comment": "Rollover SL Hit Close",
                                "type_time": mt5.ORDER_TIME_GTC,
                                "type_filling": filling_type,
                            }

                            res = mt5.order_send(close_req)
                            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                                logging.info(f"✅ Successfully closed position after SL was breached during rollover: {symbol}!")
                                del self.active_trades[ticket]
                                state_changed = True
                            else:
                                logging.error(f"❌ Failed to close position after SL breach: {res.comment if res else 'No response'}")
                        else:
                            sym_info = mt5.symbol_info(symbol)
                            if sym_info:
                                digits = sym_info.digits
                                original_sl_rounded = round(float(original_sl), digits)
                                current_sl_rounded = round(pos['sl'], digits)

                                if original_sl_rounded == current_sl_rounded:
                                    logging.info(f"☀️ MORNING: {symbol} (Ticket: {ticket}) SL is already at its original place.")
                                    trade_data["rollover_expanded"] = False
                                    trade_data["rollover_remove_retries"] = 0
                                    state_changed = True
                                    count += 1
                                    continue

                                if self.m5.modify_sl(int(ticket), symbol, original_sl_rounded, pos['tp']):
                                    logging.info(f"☀️ MORNING: {symbol} (Ticket: {ticket}) SL restored to its original place: {original_sl_rounded}")
                                    trade_data["rollover_expanded"] = False
                                    trade_data["rollover_remove_retries"] = 0
                                    state_changed = True
                                    count += 1
                                else:
                                    trade_data["rollover_remove_retries"] = retries + 1
                                    trade_data["last_remove_attempt"] = py_time.time()
                                    state_changed = True
                                    logging.error(f"❌ MORNING: Failed to restore SL for pair {symbol} (Ticket: {ticket}). Attempt {retries + 1}/3.")
            except Exception as e:
                logging.error(f"❌ MORNING: Error processing ticket {ticket}: {e}. Other tickets will still be processed.")
                continue

        if state_changed:
            self._save_state()
        return count

    def is_symbol_on_cooldown(self, symbol):
        key = f"cooldown_{symbol}"
        if key in self.active_trades:
            cooldown_until = self.active_trades[key]
            if py_time.time() < cooldown_until:
                return True, cooldown_until
            else:
                del self.active_trades[key]
                self._save_state()
        return False, 0

    def check_margin_availability(self, symbol, lot, action, price):
        """
        Checks whether free margin (margin_free) is sufficient to open a new trade.
        """
        order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL

        required_margin = mt5.order_calc_margin(order_type, symbol, lot, price)
        if required_margin is None:
            logging.error(f"❌ Failed to calculate margin for {symbol}. Error: {mt5.last_error()}")
            return False

        account_info = mt5.account_info()
        if account_info is None:
            logging.error("❌ Failed to get account info for margin check.")
            return False

        margin_free = account_info.margin_free
        buffered_margin = required_margin * 1.1

        if margin_free < buffered_margin:
            logging.warning(
                f"🚫 [MARGIN] Insufficient free margin. "
                f"Free: {margin_free:.2f} {account_info.currency} | "
                f"Required (with 10% buffer): {buffered_margin:.2f} {account_info.currency} "
                f"({required_margin:.2f} without buffer) for {symbol} ({lot:.2f} lot)."
            )
            return False

        return True

    def check_circuit_breakers(self, current_balance, daily_loss_limit_pct, max_consecutive_losses, lockout_minutes):
        """
        Returns (is_locked, reason). If is_locked == True, the bot is not allowed
        to open new orders.
        """
        import time as py_time
        import pytz
        from datetime import datetime, time, timedelta
        from config import DAILY_RESET_HOUR

        broker_tz = pytz.timezone("Europe/Vilnius")

        # --- GLOBAL KILL SWITCH (92% of initial deposit protection) ---
        if "initial_deposit" not in self.active_trades:
            account = mt5.account_info()
            if account:
                self.active_trades["initial_deposit"] = account.balance
                self._save_state()
                logging.info(f"💾 Recorded initial account deposit: {account.balance:.2f} {account.currency}")

        initial_deposit = self.active_trades.get("initial_deposit")
        if initial_deposit:
            account = mt5.account_info()
            if account:
                if account.equity < initial_deposit * 0.92:
                    return True, f"Global Kill Switch: account equity ({account.equity:.2f}) dropped below 92% of the initial deposit ({initial_deposit:.2f})"

        if "lockout_until" in self.active_trades:
            lock_until = self.active_trades["lockout_until"]
            if py_time.time() < lock_until:
                mins_left = int((lock_until - py_time.time()) / 60)
                return True, f"Consecutive Loss Lockout ({mins_left} min. remaining)"
            else:
                del self.active_trades["lockout_until"]
                self._save_state()

        if "daily_lockout_until" in self.active_trades:
            lock_until = self.active_trades["daily_lockout_until"]
            if py_time.time() < lock_until:
                lock_until_str = datetime.fromtimestamp(lock_until, broker_tz).strftime('%Y-%m-%d %H:%M:%S')
                return True, f"Daily loss limit reached (Locked until {lock_until_str})"
            else:
                del self.active_trades["daily_lockout_until"]
                self._save_state()

        now_dt = datetime.now(broker_tz).replace(tzinfo=None)

        if now_dt.hour < DAILY_RESET_HOUR:
            last_reset = datetime.combine(now_dt.date() - timedelta(days=1), time(DAILY_RESET_HOUR, 0, 0))
        else:
            last_reset = datetime.combine(now_dt.date(), time(DAILY_RESET_HOUR, 0, 0))

        last_reset_str = last_reset.strftime('%Y-%m-%d %H:%M:%S')

        if "daily_equity_date" not in self.active_trades or self.active_trades["daily_equity_date"] != last_reset_str:
            account = mt5.account_info()
            if account:
                self.active_trades["daily_equity_date"] = last_reset_str
                self.active_trades["daily_starting_equity"] = account.equity
                self._save_state()
                logging.info(f"💾 Recorded new daily starting equity ({last_reset_str}): {account.equity:.2f} USD")

        starting_equity = self.active_trades.get("daily_starting_equity")

        deals = mt5.history_deals_get(last_reset, now_dt)
        consecutive_losses = 0
        if deals:
            sorted_deals = sorted(deals, key=lambda x: x.time)
            for deal in sorted_deals:
                if deal.magic == self.m5.magic_number and deal.entry == mt5.DEAL_ENTRY_OUT:
                    profit = deal.profit + deal.commission + deal.swap
                    if profit < 0:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0

        if starting_equity:
            account = mt5.account_info()
            if account:
                current_equity = account.equity
                daily_drawdown = starting_equity - current_equity

                if daily_drawdown > 0:
                    loss_percent = daily_drawdown / starting_equity * 100.0
                    if loss_percent >= daily_loss_limit_pct:
                        reset_datetime = broker_tz.localize(last_reset + timedelta(days=1))
                        self.active_trades["daily_lockout_until"] = reset_datetime.timestamp()
                        self._save_state()
                        return True, f"Daily Loss Limit reached (-{loss_percent:.2f}%). Locked until {reset_datetime.strftime('%Y-%m-%d %H:%M:%S')}."

        if consecutive_losses >= max_consecutive_losses:
            lock_time = py_time.time() + (lockout_minutes * 60)
            self.active_trades["lockout_until"] = lock_time
            self._save_state()
            return True, f"{max_consecutive_losses} consecutive losses. Locked for {lockout_minutes} min."

        return False, "OK"
