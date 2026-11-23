[![make.fun · btc-race-24h-20261123-0511](https://make.fun/badge/btc-race-24h-20261123-0511.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![rank](https://make.fun/badge/btc-race-24h-20261123-0511/Pendot33/btc-race-20261123/rank.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)
[![reward](https://make.fun/badge/btc-race-24h-20261123-0511/Pendot33/btc-race-20261123/reward.svg)](https://make.fun/bounty/btc-race-24h-20261123-0511?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261123-0511)

# TrendBot — a cbBTC trend-following racer

An entry for make.fun's `btc-race-24h-20261123-0511`: race a BTC trading
bot live for 24 hours. The bot trades Coinbase Wrapped BTC (cbBTC) against
USDC on 1-minute on-chain bars with $1,000 of paper money, under the
bars-replay/v1 harness.

## Strategy

A deterministic trend-following state machine (fixed parameters, no clock,
no randomness, no network):

- Indicators on closes: EMA 30 / EMA 120, 60-bar momentum, Kaufman
  efficiency ratio over 60 bars.
- Entry (flat → long) only when all hold: EMA30 clears EMA120 by ≥ 0.25%,
  60-bar momentum above +0.35%, efficiency ratio ≥ 0.35, and at least 6
  nonzero 1-minute returns in the last 60 bars.
- Size: about 90% of cash, capped so the order's own estimated price
  impact stays near 15 bps of the pool's latest close depth.
- Exits: trend break (EMA cross down with momentum below −0.10%), a 1.2%
  hard stop under the average entry price, or a 1.0% trailing stop armed
  after a +1.2% gain. A 2.5% equity drawdown from the peak parks the bot
  in cash for the rest of the window.
- 30-bar cooldown after every exit. Modes, cooldowns and the position
  clock change only on confirmed fills; an unconfirmed exit is resent
  with the current quantity.

## Layout

- `main.py` — the strategy and the lockstep runner (Python 3.13, standard
  library only).
- `vendor/makefun_bars.py` — a small line-protocol client for the
  bars-replay wire format (hello / batch / reply / done), written for
  this entry.

On the published dev window (22 Nov 05:11 → 23 Nov 05:11 UTC) the bot
replays to a net PnL of +$3.82 over 4 trades with a 2.24% maximum
drawdown, clearing the dev gate (net PnL above $0).
