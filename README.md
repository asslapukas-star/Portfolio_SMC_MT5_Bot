# SMC CHoCH+OB Trading Bot (MetaTrader 5)

A live-trading Python bot for MetaTrader 5 implementing a Smart Money
Concepts (SMC) strategy: multi-timeframe bias (4H) → Change of Character
confirmation (H1) → untouched Order Block entry → ATR-based structural stop
→ dynamic (swing-based) or trailing take-profit.

This is a **real, currently-deployed** bot (demo/forward-testing account),
not a toy example — it runs 24/5, syncs its internal state against live MT5
positions/orders every hour, and reports via Telegram.

## What this demonstrates

- **MetaTrader 5 Python API integration**: pending order placement (`BUY_LIMIT`/
  `SELL_LIMIT`), SL/TP modification on open positions, position/order
  reconciliation between the strategy's internal simulation and the broker's
  real state.
- **Risk management infrastructure**: per-trade risk sizing (% of equity),
  daily loss circuit breaker, consecutive-loss lockout, max concurrent
  positions, max exposure per base currency, a global drawdown kill switch.
- **Telegram integration**: trade alerts, remote commands (`/status`,
  `/pause`, `/resume`, `/stop`), all with authenticated chat-id binding.
- **Honest statistical validation**: every strategy change in this codebase
  went through permutation testing, chronological train/test splits, and
  explicit selection-bias accounting before being deployed - see
  [METHODOLOGY.md](METHODOLOGY.md) for the full, unfiltered writeup
  (including negative results - which most public EA showcases omit).

## Structure

| File | Purpose |
|---|---|
| `strategy.py` | Pure signal logic: H4 bias, H1 CHoCH detection, OB zone identification, SL/TP calculation. Stateless given price history - the same function drives both the backtest and the live bot. |
| `main.py` | Live loop: hourly re-evaluation per symbol, MT5 order sync, circuit breakers, Telegram command handling. |
| `mt5_interface.py` | Thin wrapper around the `MetaTrader5` Python package (data fetch, order placement/modification, position/order queries). |
| `risk_manager.py` | Position sizing, daily-loss/consecutive-loss circuit breakers, global drawdown kill switch, persistent state. |
| `telegram_alerts.py` | Outbound alerts + inbound remote-command polling. |
| `config.py` | All tunable parameters in one place; secrets read from environment variables. |

## Setup

```
pip install MetaTrader5 pandas numpy pytz
```

Set `TELEGRAM_BOT_TOKEN` as an environment variable, adjust `config.py` for
your symbols/risk settings, make sure your MT5 terminal is already logged
into the target account, then:

```
python main.py
```

## What I can build for you

- Convert a strategy you already trade manually (or a video/course
  methodology, like this one) into a working MT5 EA or Python bot
- Backtesting with real permutation-test significance checks and train/test
  splits - not just an equity curve
- MT5 ↔ broker order/position sync, risk management, Telegram/Discord
  alerting
- Honest strategy audits: if your existing EA's backtest numbers look too
  good, I can find out why (look-ahead bias, cost miscalculation, selection
  bias, overfitting) before you risk real money on it
