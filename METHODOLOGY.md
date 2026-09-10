# Methodology: from a YouTube strategy to a validated, live bot

Most public "trading bot" showcases show a single glowing equity curve and
stop there. This document shows the actual process - including the dead
ends - because that process, not the equity curve, is the actual skill
being sold.

## 1. The source

The strategy was specified from a YouTube trading-education channel's
video content (Smart Money Concepts / ICT school): 4H bias, H1 Change of
Character through the last confirmed opposite-type swing, entry at the
nearest untouched Order Block, ATR-buffered structural stop, dynamic
take-profit at the next opposing swing.

Translating a video into code is not the hard part. The hard part is that
a video shows a handful of curated, hindsight-picked examples - it says
nothing about false-positive rate, cost sensitivity, or whether the
pattern generalizes. That's what the rest of this document is about.

## 2. Implementation bugs found before trusting any result

Three real bugs were caught and fixed during implementation, each of which
would have silently inflated backtest results if missed:
- A hardcoded profit override that fired regardless of actual trade outcome.
- A "confirmation" step that used look-ahead information not available at
  decision time.
- A stale experimental take-profit branch that was left active after being
  rejected, silently degrading every later comparison until caught.

This is standard practice in this codebase: any surprisingly good backtest
number gets treated as a bug report until proven otherwise, not celebrated.

## 3. Selection and validation, not just backtesting

- Tested 29 FX/metal pairs independently, 5 non-overlapping historical
  periods (~1 year total).
- Chronological train/test split (not k-fold - order matters in price
  data): older half discovers, newer half confirms.
- Only pairs positive in **both independent halves** were kept: 3 out of
  29 (GBPCAD, USDCHF, XAUUSD).
- Combined result: n=31, WR=74.2%, Suminis=+16.68R, permutation test
  (label-shuffling, 5000x) p=0.013.

**The caveat that most EA sellers omit:** those 3 pairs were *selected*
out of 29 candidates. Under pure noise, ~25% of candidates (≈7/29) would
show "positive in both halves" by chance alone. Finding only 3 is actually
*below* that chance expectation - reassuring, but the p-value as reported
does not fully correct for the search itself. This is disclosed in the
bot's own code comments, not just in marketing copy.

## 4. Extending it: trailing stop

Added a trailing stop (SL follows the latest confirmed swing point,
one-directional only - it can tighten, never loosen) and re-validated
on the same 3-pair, 5-period harness:
- n=31, WR=74.2%, Suminis=+17.29R, p=0.0145.
- Concentration check: top-5 trades = 70.3% of total profit, spread
  across all 3 symbols and all 5 periods - not one or two lucky trades
  carrying the whole result (a separate variant tested during this same
  work, with a more distant take-profit, initially looked *better*
  (+96R) until a concentration check showed 2 trades out of 65 accounted
  for 98% of the result - a single macro trend event, not a repeatable
  edge. That variant was correctly rejected.)

## 5. Extending it further: new instruments (negative result, reported honestly)

Tested whether the same exact rule set generalizes to silver, crude oil
(WTI/Brent), and equity indices (S&P500, Nasdaq100, Dow, DAX) - all
available on the same broker. Same train/test-both-halves bar used to
select the original 3 pairs:
- Silver: fails (test half negative, -5.02R over 17 trades).
- Oil: apparent large gain driven by a single 4-trade window (one
  macro event) - same "lottery" red flag as section 4's rejected variant.
- Indices: 2 of 4 marginally pass, but on thin, concentrated samples.

Reported and left out of the live bot rather than cherry-picked in,
despite the temptation of a +40R headline number for the combined set.

## 6. What this buys a client

A strategy that "backtests well" is the easy 80%. The value in this
process is the other 20%: distinguishing a repeatable statistical edge
from a backtest artifact, a lucky trade, or an overfit selection - before
capital is put behind it. That's the same rigor applied whether the
answer turns out to be "yes, deploy it" or "no, and here's the specific
reason why."
