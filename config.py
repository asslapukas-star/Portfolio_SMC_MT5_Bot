import os
import logging
from logging.handlers import RotatingFileHandler

# ==========================================
# SMC CHoCH+OB BOT - SETTINGS
# ==========================================
# Strategy: 4H bias + H1 CHoCH (break through the last confirmed opposite-type
# swing point) + untouched H1 Order Block (last opposite candle before the CHoCH
# impulse) + LIMIT order at the OB zone midpoint + dynamic TP (nearest confirmed
# H1 swing in the trade direction, with a fallback fixed R:R).
#
# Symbol selection example: out of 29 tested FX/metal pairs, only 3 showed a
# positive result in BOTH independent halves of a chronological train/test
# split, AND a statistically significant permutation-test result combined
# (n=31, WR=74.2%, Suminis=+16.68R, p=0.013). IMPORTANT CAVEAT: those 3 pairs
# were selected out of 29 candidates, so the p-value does not fully account for
# the selection effect - this is a forward/live validation harness, not a
# confirmed edge. See METHODOLOGY.md for the full honest writeup, including a
# trailing-stop extension that was separately validated (n=31, +17.29R,
# p=0.0145, TOP-5 concentration 70.3% - i.e. broad, not a "lottery" result).

MAGIC_NUMBER = 245577  # unique EA identifier so positions/tickets don't clash
# with any other EA sharing the same MT5 terminal/account.

ACTIVE_SYMBOLS = ["GBPCAD", "USDCHF", "XAUUSD"]

# 2. RISK PER TRADE (%) - read fresh every cycle, no restart needed to change it.
RISK_PER_TRADE_PERCENT = 1.0

# 3. PORTFOLIO LIMITS
MAX_CONCURRENT_RISK_ORDERS = 3
MAX_TRADES_PER_BASE_CURRENCY = 2
MAX_RISK_MULTIPLIER_ON_MIN_LOT = 1.5

# 4. EXECUTION ECONOMICS - the live bot always uses the REAL current
# sym_info.spread at order time (not a fixed backtest assumption).
COMMISSION_POINTS = 7
AVG_SLIPPAGE_POINTS = 5

# 5. Prop-firm-challenge-style circuit breakers (strategy-independent risk logic).
MAX_CONSECUTIVE_LOSSES = 3
LOCKOUT_DURATION_MINUTES = 60
DAILY_LOSS_LIMIT_PERCENT = 3.0
DAILY_RESET_HOUR = 2
COOLDOWN_HOURS = 4

# 6. TELEGRAM - token/chat id read from environment variables, never hardcoded.
TELEGRAM_ENABLED = True
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# 7. METATRADER 5 TERMINAL PATH - the terminal must already be logged into the
# target account manually (this EA never handles login credentials directly).
MT5_TERMINAL_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"

# 8. STRATEGY PARAMETERS - must match the validated backtest; do not change
# without re-running the test suite.
SWING_LOOKBACK = 3
MIN_IMPULSE_ATR = 1.5
ATR_SL_BUFFER = 0.5
FALLBACK_RR = 3.0
H1_BARS_FOR_SIGNAL = 4000  # ~166d - within MT5's reliable position-based fetch range (~9500h)
PENDING_ORDER_EXPIRY_BARS = 1500  # ~62.5d of H1 bars


def setup_logging():
    file_handler = RotatingFileHandler(
        "bot.log",
        maxBytes=50 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            file_handler,
            logging.StreamHandler()
        ]
    )
