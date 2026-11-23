"""Trend-following bot for the make.fun BTC race (bars-replay/v1).

The bot trades CBBTC (Coinbase Wrapped BTC on Solana, priced in USDC from
its Orca Whirlpool pool) minute by minute with $1,000 of paper money,
starting flat and going long only while a confirmed uptrend holds:

- Entries: EMA30 clears EMA120 by 0.25% or more, 60-bar momentum above
  +0.35%, a Kaufman efficiency ratio of at least 0.35 over 60 bars, and at
  least 6 nonzero 1-minute returns in those 60 bars. Position size is about
  90% of cash, capped so the order's own estimated price impact stays near
  15 bps of the pool's latest close depth.
- Exits: trend break (EMA cross down with negative momentum), a 1.2% stop
  under the average entry price, or a 1.0% trailing stop armed after a
  1.2% gain. A 2.5% equity drawdown from the peak parks the bot in cash
  for the rest of the window.
- 30-bar cooldown after every exit.

The bot is deterministic: fixed parameters, no clock, no randomness, no
environment access. It never orders during the harness warm-up, though it
keeps learning the price history there.

Order lifecycle: the protocol resolves every order of batch k at batch
k+1's open and reports the fills in batch k+1, so a snapshot always shows
the final outcome of earlier orders. The state machine relies on that
guarantee: modes, cooldowns and the position clock change only on
confirmed flat/long transitions, an unconfirmed exit is resent with the
current quantity, and the drawdown breaker latches independently — it
blocks new entries forever but never blocks the liquidation of an open
position.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars as bars  # noqa: E402

SYMBOL = "CBBTC"

# --- strategy parameters (fixed; tuned only against the published dev window)
EMA_FAST = 30
EMA_SLOW = 120
MOM_LAG = 60
ENTRY_EMA_GAP = 0.0025      # EMA30 must clear EMA120 by 0.25%
ENTRY_MOM = 0.0035          # 60-minute momentum above +0.35%
ENTRY_ER = 0.35             # Kaufman efficiency ratio floor
ENTRY_MIN_MOVES = 6         # at least 6 nonzero 1-minute returns in 60 bars
EXIT_MOM = -0.001           # trend-break exit needs momentum under -0.10%
STOP_LOSS = 0.012           # hard stop: 1.2% under average entry price
TRAIL_ARM = 0.012           # trailing stop arms after a +1.2% gain
TRAIL_DROP = 0.010          # trailing stop: 1.0% under the peak since entry
BREAKER_DD = 0.025          # 2.5% equity drawdown from peak parks the bot
COOLDOWN_BARS = 30          # bars in cash after every exit
CASH_FRACTION = 0.90        # an entry spends about 90% of cash
IMPACT_TARGET_BPS = 15      # size so own price impact stays near 15 bps
BUY_SLIPPAGE_BPS = 100      # an entry must not chase a runaway open
SELL_SLIPPAGE_BPS = 300     # an exit must almost always go through

BPS = 10_000

# the fractional parameters above as whole basis points, for integer compares
STOP_LOSS_BPS = round(STOP_LOSS * BPS)
TRAIL_ARM_BPS = round(TRAIL_ARM * BPS)
TRAIL_DROP_BPS = round(TRAIL_DROP * BPS)
BREAKER_DD_BPS = round(BREAKER_DD * BPS)


def _bps_le(value: int, reference: int, bps_off: int) -> bool:
    """value <= reference * (10000 - bps_off) / 10000, in integers."""
    return value * BPS <= reference * (BPS - bps_off)


def _bps_ge(value: int, reference: int, bps_off: int) -> bool:
    """value >= reference * (10000 + bps_off) / 10000, in integers."""
    return value * BPS >= reference * (BPS + bps_off)


class TrendBot:
    def __init__(self, hello: bars.Hello):
        self.info = hello.symbols[SYMBOL]
        self.quote_scale = hello.quote_scale
        self.min_notional = hello.min_notional_quote_units
        self.warmup_bars = hello.warmup_bars
        # indicator state (learns during warm-up too)
        self.closes: deque[int] = deque(maxlen=EMA_SLOW + MOM_LAG + 2)
        self.ema_fast: float | None = None
        self.ema_slow: float | None = None
        self.last_depth: tuple[int, ...] | None = None
        # trading state (changes only on confirmed transitions)
        self.mode = "flat"              # "flat" | "long"
        self.pending: tuple[str, str] | None = None  # (side, client_id)
        self.exit_wanted = False
        self.cooldown_left = 0
        self.bars_held = 0
        self.trail_peak: int | None = None
        # drawdown breaker
        self.peak_equity = hello.start_quote_units
        self.breaker_latched = False
        self._buy_count = 0
        self._sell_count = 0

    # ------------------------------------------------------------ indicators
    def _learn(self, close: int) -> None:
        self.closes.append(close)
        kf = 2.0 / (EMA_FAST + 1)
        ks = 2.0 / (EMA_SLOW + 1)
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
        else:
            self.ema_fast += (close - self.ema_fast) * kf
            self.ema_slow += (close - self.ema_slow) * ks

    def _momentum(self) -> float | None:
        if len(self.closes) < MOM_LAG + 1:
            return None
        return self.closes[-1] / self.closes[-MOM_LAG - 1] - 1.0

    def _efficiency_ratio(self) -> float | None:
        if len(self.closes) < MOM_LAG + 1:
            return None
        window = list(self.closes)[-MOM_LAG - 1:]
        travelled = sum(abs(b - a) for a, b in zip(window, window[1:]))
        if travelled == 0:
            return 0.0
        return abs(window[-1] - window[0]) / travelled

    def _nonzero_moves(self) -> int:
        window = list(self.closes)[-MOM_LAG - 1:]
        return sum(1 for a, b in zip(window, window[1:]) if b != a)

    # ------------------------------------------------------------ fills
    def _reconcile(self, batch: bars.Batch) -> None:
        """Fold this batch's fills into the state machine.

        A fill is the harness's word that an order decided on the previous
        batch did or did not trade; only a filled order moves the mode.
        """
        if self.pending is None:
            return
        side, client_id = self.pending
        for fill in batch.fills:
            if fill.client_id != client_id:
                continue
            self.pending = None
            if side == "buy":
                if fill.filled:
                    self.mode = "long"
                    self.bars_held = 0
                    self.trail_peak = None
            else:  # sell
                if fill.filled:
                    self.mode = "flat"
                    self.exit_wanted = False
                    self.cooldown_left = COOLDOWN_BARS
                    self.bars_held = 0
                    self.trail_peak = None
                else:
                    self.exit_wanted = True
            return

    # ------------------------------------------------------------ sizing
    def _entry_notional(self, cash: int) -> int:
        notional = int(cash * CASH_FRACTION)
        if self.last_depth is not None:
            depth = bars.combined_depth(self.last_depth)
            cap_raw = depth * IMPACT_TARGET_BPS // BPS  # USDC raw units
            cap_quote = int(cap_raw * 10**self.quote_scale
                            // 10**bars.USDC_RAW_SCALE)
            notional = min(notional, cap_quote)
        return notional

    def _next_id(self, side: str) -> str:
        if side == "buy":
            self._buy_count += 1
            return f"b{self._buy_count}"
        self._sell_count += 1
        return f"s{self._sell_count}"

    # ------------------------------------------------------------ decision
    def on_batch(self, batch: bars.Batch) -> dict | None:
        bar = batch.bars.get(SYMBOL)
        close = bar.c if bar is not None else None
        if bar is not None:
            self._learn(bar.c)
            self.last_depth = bar.c_depth
        self._reconcile(batch)

        account = batch.account
        pos = account.position(SYMBOL)
        qty = pos.qty if pos is not None else 0
        cost = pos.cost_quote_units if pos is not None else 0

        if batch.seq < self.warmup_bars:
            return None  # warm-up: learn, never order

        # drawdown breaker: peak and latch update on every scored batch
        if account.equity_quote_units > self.peak_equity:
            self.peak_equity = account.equity_quote_units
        if not self.breaker_latched and _bps_le(
                account.equity_quote_units, self.peak_equity,
                BREAKER_DD_BPS):
            self.breaker_latched = True

        if self.mode == "long":
            return self._decide_exit(close, qty, cost)
        return self._decide_entry(close, account.cash_quote_units)

    def _decide_exit(self, close: int | None, qty: int, cost: int) -> dict | None:
        if qty == 0:
            return None  # nothing held (defensive; mode follows fills)
        if self.pending is not None:
            return None  # an exit is already in flight
        if close is not None:
            self.bars_held += 1
            if self.trail_peak is None or close > self.trail_peak:
                self.trail_peak = close
        exit_now = self.exit_wanted or self.breaker_latched
        if close is not None and qty > 0:
            avg_entry = (cost * 10 ** (self.info.qty_scale + self.info.price_scale)
                         // (qty * 10 ** self.quote_scale))
            momentum = self._momentum()
            if (self.ema_fast is not None and self.ema_slow is not None
                    and self.ema_fast < self.ema_slow
                    and momentum is not None and momentum < EXIT_MOM):
                exit_now = True  # trend break
            if _bps_le(close, avg_entry, STOP_LOSS_BPS):
                exit_now = True  # hard stop
            if (self.trail_peak is not None and self.bars_held >= 1
                    and _bps_ge(self.trail_peak, avg_entry, TRAIL_ARM_BPS)
                    and _bps_le(close, self.trail_peak, TRAIL_DROP_BPS)):
                exit_now = True  # trailing stop
        if not exit_now:
            return None
        client_id = self._next_id("sell")
        self.pending = ("sell", client_id)
        return bars.sell_order(client_id, SYMBOL, qty, SELL_SLIPPAGE_BPS)

    def _decide_entry(self, close: int | None, cash: int) -> dict | None:
        if self.pending is not None:
            return None  # a buy is already in flight
        if self.breaker_latched:
            return None  # parked in cash for the rest of the window
        if self.cooldown_left > 0:
            self.cooldown_left -= 1
            return None
        if close is None or len(self.closes) < EMA_SLOW + 1:
            return None  # indicators not ready
        momentum = self._momentum()
        er = self._efficiency_ratio()
        if momentum is None or er is None:
            return None
        if self.ema_fast < self.ema_slow * (1.0 + ENTRY_EMA_GAP):
            return None
        if momentum <= ENTRY_MOM:
            return None
        if er < ENTRY_ER:
            return None
        if self._nonzero_moves() < ENTRY_MIN_MOVES:
            return None
        notional = self._entry_notional(cash)
        if notional < self.min_notional or notional > cash:
            return None
        client_id = self._next_id("buy")
        self.pending = ("buy", client_id)
        return bars.buy_order(client_id, SYMBOL, notional, BUY_SLIPPAGE_BPS)


def run() -> int:
    session = bars.Session(sys.stdin, sys.stdout)
    hello = session.read_hello()
    bot = TrendBot(hello)
    for message in session.messages():
        if isinstance(message, bars.Done):
            break
        order = bot.on_batch(message)
        session.reply(message.seq, [order] if order is not None else [])
    return 0


if __name__ == "__main__":
    sys.exit(run())
