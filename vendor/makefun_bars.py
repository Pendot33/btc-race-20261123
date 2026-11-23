"""Line-protocol client for the make.fun bars replay (agent-fun/bars-replay/v1).

The harness speaks newline-delimited JSON: it writes one ``hello``, then one
``batch`` per bar interval, then one ``done``; the bot answers every batch
with exactly one ``reply`` echoing the batch's ``seq`` before the next batch
arrives (lockstep).  Amounts travel as canonical decimal strings of integers:

- a price integer ``p`` of a symbol is ``p / 10**price_scale`` USDC per whole
  unit of the asset (scales are per symbol, announced in the hello);
- a quantity ``q`` is ``q / 10**qty_scale`` whole units of the asset;
- a quote amount (``*_quote_units``) is ``a / 10**quote_scale`` USDC;
- a bar's pool depths are USDC raw units (10**-6 USDC) per hop of the
  asset's price path, whatever the quote scale is.

Orders decided on batch k fill at the open of bar k + 1 and are reported in
batch k + 1's ``fills`` next to the account they produced, so a snapshot
always shows the final outcome of every earlier order.

This module is only the wire layer: parsing, value arithmetic and reply
writing.  It keeps no strategy state and reads nothing but the two streams
it is handed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction

PROTOCOL = "agent-fun/bars-replay/v1"

# Depths are quoted in USDC raw units: 1 raw unit is 10**-6 USDC.
USDC_RAW_SCALE = 6


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    mint: str
    price_scale: int
    qty_scale: int


@dataclass(frozen=True)
class Bar:
    symbol: str
    t: int
    o: int
    h: int
    l: int
    c: int
    v: int | None
    o_depth: tuple[int, ...]
    c_depth: tuple[int, ...]


@dataclass(frozen=True)
class Fill:
    client_id: str
    symbol: str
    side: str
    status: str  # "filled" or "rejected"
    reason: str | None
    decided_bar: int
    fill_bar: int
    price: int
    qty: int
    notional_quote_units: int
    fee_quote_units: int

    @property
    def filled(self) -> bool:
        return self.status == "filled"


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    cost_quote_units: int


@dataclass(frozen=True)
class Account:
    cash_quote_units: int
    positions: tuple[Position, ...]
    equity_quote_units: int

    def position(self, symbol: str) -> Position | None:
        for pos in self.positions:
            if pos.symbol == symbol:
                return pos
        return None


@dataclass(frozen=True)
class Hello:
    seed: int
    symbols: dict[str, SymbolInfo]
    quote_scale: int
    start_quote_units: int
    bar_interval: str
    first_close_ms: int
    max_open_positions: int
    max_orders_per_bar: int
    max_weight_bps: int
    fee_bps: int
    slippage_bps: int
    min_notional_quote_units: int
    warmup_bars: int  # 0 when the hello carries no warm-up


@dataclass(frozen=True)
class Batch:
    seq: int
    t: int
    bars: dict[str, Bar]
    fills: tuple[Fill, ...]
    account: Account


@dataclass(frozen=True)
class Done:
    seq: int
    reason: str
    account: Account


def value_of(qty: int, price: int, info: SymbolInfo, quote_scale: int) -> int:
    """Quote units ``qty`` is worth at ``price`` (the protocol's value())."""
    numerator = qty * price * 10**quote_scale
    return numerator // 10 ** (info.qty_scale + info.price_scale)


def combined_depth(depths: tuple[int, ...]) -> Fraction:
    """Effective depth of the hops in series: D = 1 / (1/D1 + 1/D2 + ...).

    The hops of a price path are constant-product pools in series and trade
    exactly as one pool of this depth.  A single hop is its own depth.
    """
    if len(depths) == 1:
        return Fraction(depths[0])
    return Fraction(1, 1) / sum(Fraction(1, d) for d in depths)


def buy_order(client_id: str, symbol: str, notional_quote_units: int,
              max_slippage_bps: int) -> dict:
    return {
        "side": "buy",
        "client_id": client_id,
        "symbol": symbol,
        "notional_quote_units": str(notional_quote_units),
        "max_slippage_bps": max_slippage_bps,
    }


def sell_order(client_id: str, symbol: str, qty: int,
               max_slippage_bps: int) -> dict:
    return {
        "side": "sell",
        "client_id": client_id,
        "symbol": symbol,
        "qty": str(qty),
        "max_slippage_bps": max_slippage_bps,
    }


def _parse_account(raw: dict) -> Account:
    return Account(
        cash_quote_units=int(raw["cash_quote_units"]),
        positions=tuple(
            Position(symbol=p["symbol"], qty=int(p["qty"]),
                     cost_quote_units=int(p["cost_quote_units"]))
            for p in raw["positions"]
        ),
        equity_quote_units=int(raw["equity_quote_units"]),
    )


def _parse_hello(raw: dict) -> Hello:
    symbols = {
        s["symbol"]: SymbolInfo(symbol=s["symbol"], mint=s["mint"],
                                price_scale=int(s["price_scale"]),
                                qty_scale=int(s["qty_scale"]))
        for s in raw["symbols"]
    }
    caps = raw["caps"]
    fees = raw["fees"]
    capital = raw["capital"]
    return Hello(
        seed=int(raw["seed"]),
        symbols=symbols,
        quote_scale=int(capital["quote_scale"]),
        start_quote_units=int(capital["quote_units"]),
        bar_interval=raw["bar_interval"],
        first_close_ms=int(raw["first_close_ms"]),
        max_open_positions=int(caps["max_open_positions"]),
        max_orders_per_bar=int(caps["max_orders_per_bar"]),
        max_weight_bps=int(caps["max_weight_bps"]),
        fee_bps=int(fees["fee_bps"]),
        slippage_bps=int(fees["slippage_bps"]),
        min_notional_quote_units=int(fees["min_notional_quote_units"]),
        warmup_bars=int(raw.get("warmup_bars", 0)),
    )


def _parse_batch(raw: dict) -> Batch:
    bars = {}
    for b in raw["bars"]:
        bars[b["symbol"]] = Bar(
            symbol=b["symbol"],
            t=int(b["t"]),
            o=int(b["o"]),
            h=int(b["h"]),
            l=int(b["l"]),
            c=int(b["c"]),
            v=None if b["v"] is None else int(b["v"]),
            o_depth=tuple(int(d) for d in b["o_depth"]),
            c_depth=tuple(int(d) for d in b["c_depth"]),
        )
    fills = tuple(
        Fill(
            client_id=f["client_id"],
            symbol=f["symbol"],
            side=f["side"],
            status=f["status"],
            reason=f["reason"],
            decided_bar=int(f["decided_bar"]),
            fill_bar=int(f["fill_bar"]),
            price=int(f["price"]),
            qty=int(f["qty"]),
            notional_quote_units=int(f["notional_quote_units"]),
            fee_quote_units=int(f["fee_quote_units"]),
        )
        for f in raw["fills"]
    )
    return Batch(seq=int(raw["seq"]), t=int(raw["t"]), bars=bars,
                 fills=fills, account=_parse_account(raw["account"]))


class Session:
    """Lockstep exchange with the harness over two text streams."""

    def __init__(self, instream, outstream):
        self._in = instream
        self._out = outstream

    def read_hello(self) -> Hello:
        line = self._in.readline()
        if not line:
            raise EOFError("harness closed the stream before the hello")
        raw = json.loads(line)
        if raw.get("type") != "hello":
            raise ValueError(f"expected hello, got {raw.get('type')!r}")
        return _parse_hello(raw)

    def messages(self):
        """Yield Batch objects, then one Done, then stop (EOF also stops)."""
        for line in self._in:
            if not line.strip():
                continue
            raw = json.loads(line)
            kind = raw.get("type")
            if kind == "batch":
                yield _parse_batch(raw)
            elif kind == "done":
                yield Done(seq=int(raw["seq"]), reason=raw["reason"],
                           account=_parse_account(raw["account"]))
                return
            else:
                raise ValueError(f"unexpected harness message {kind!r}")

    def reply(self, seq: int, orders: list) -> None:
        line = json.dumps({"type": "reply", "seq": seq, "orders": orders},
                          separators=(",", ":"))
        self._out.write(line + "\n")
        self._out.flush()
