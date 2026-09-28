from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Tuple

from app.models import MarketSpec, RiskConfig


class RiskEngine:
    def __init__(self, market: MarketSpec, risk: RiskConfig) -> None:
        self.market = market
        self.risk = risk

    def check_spread(self, bid: Decimal, ask: Decimal) -> Tuple[bool, str]:
        spread = ask - bid
        min_spread = self.market.tick_size * Decimal(self.risk.min_spread_ticks)
        if spread < min_spread:
            return False, f"spread {spread} < min_spread {min_spread}"
        return True, "ok"

    def check_notional(self, price: Decimal, qty: Decimal) -> Tuple[bool, str]:
        notional = price * qty
        if self.market.min_notional and notional < self.market.min_notional:
            return False, f"notional {notional} < min_notional {self.market.min_notional}"
        return True, "ok"

    def check_top_of_book_depth(
        self,
        best_qty: Decimal,
        best_price: Decimal,
        order_qty: Decimal,
    ) -> Tuple[bool, str]:
        top_quote = best_qty * best_price
        if top_quote < self.risk.min_top_quote:
            return False, f"top_quote {top_quote} < min_top_quote {self.risk.min_top_quote}"

        max_qty = best_qty * self.risk.max_participation_of_top
        if order_qty > max_qty:
            return False, f"order_qty {order_qty} > max_qty {max_qty} (top_qty={best_qty})"
        return True, "ok"

    def check_slippage_bps(self, expected_price: Decimal, ref_price: Decimal) -> Tuple[bool, str]:
        if ref_price <= 0:
            return False, "ref_price <= 0"
        diff = (expected_price - ref_price) / ref_price
        bps = int((diff * Decimal("10000")).to_integral_value(rounding=ROUND_DOWN))
        if abs(bps) > self.risk.max_slippage_bps:
            return False, f"slippage {bps} bps > limit {self.risk.max_slippage_bps}"
        return True, "ok"
