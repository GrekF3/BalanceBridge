from __future__ import annotations

import random
from decimal import Decimal
from typing import Any, Dict, Optional

from app.client import MEXCClient
from services import MexcSpotWebSDK, MexcSpotError
from app.models import MarketSpec, RiskConfig
from app.user_stream import OrderTracker, TERMINAL_ORDER_STATUSES
from app.utils import format_decimal, log_event, round_down


class OrderManager:
    def __init__(
        self,
        client: MEXCClient,
        symbol: str,
        market: MarketSpec,
        risk: RiskConfig,
        tracker: Optional[OrderTracker] = None,
        spot_web: Optional[MexcSpotWebSDK] = None,
        name: str = "",
    ) -> None:
        self.client = client
        self.symbol = symbol
        self.market = market
        self.risk = risk
        self.tracker = tracker
        self.spot_web = spot_web
        self.name = name or "?"

    def _client_id(self) -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(random.choice(alphabet) for _ in range(32))

    def cancel_all_open_orders(self) -> int:
        try:
            orders = self.client.open_orders(self.symbol)
        except Exception as exc:  # noqa: BLE001
            log_event("cancel_all_failed", {"error": str(exc)})
            return 0

        canceled = 0
        for o in orders:
            oid = str(o.get("orderId", ""))
            if not oid:
                continue
            try:
                self.client.cancel_order(self.symbol, order_id=oid)
                canceled += 1
            except Exception as exc:  # noqa: BLE001
                log_event("cancel_failed", {"orderId": oid, "error": str(exc)})

        return canceled

    def place_limit_and_wait(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        debug: bool = True,
        post_only: Optional[bool] = None,
        timeout_s: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.tracker:
            raise RuntimeError("OrderTracker is required for live mode (WebSocket updates).")

        order_id, _client_id, _price_str, _qty_str = self._place_limit(
            side,
            price,
            qty,
            debug=debug,
            post_only=post_only,
        )
        status = self.tracker.wait_for_terminal(order_id, timeout_s or self.risk.order_timeout_s)
        if status and status.get("status") in TERMINAL_ORDER_STATUSES:
            return status

        try:
            self.client.cancel_order(self.symbol, order_id=order_id)
            log_event("order_timeout_cancel", {"orderId": order_id})
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "order cancelled" in msg.lower() or "-2011" in msg:
                log_event("order_timeout_cancel", {"orderId": order_id, "note": "already_cancelled"})
            else:
                log_event("order_timeout_cancel_failed", {"orderId": order_id, "error": msg})

        final_status = self.tracker.wait_for_terminal(order_id, 3)
        return final_status or {"orderId": order_id, "status": "UNKNOWN"}

    def place_limit(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        debug: bool = True,
        post_only: Optional[bool] = None,
    ) -> Dict[str, Any]:
        order_id, client_id, price_str, qty_str = self._place_limit(
            side,
            price,
            qty,
            debug=debug,
            post_only=post_only,
        )
        return {
            "orderId": order_id,
            "clientOrderId": client_id,
            "price": price_str,
            "qty": qty_str,
        }

    def _place_limit(
        self,
        side: str,
        price: Decimal,
        qty: Decimal,
        debug: bool = True,
        post_only: Optional[bool] = None,
    ) -> tuple[str, str, str, str]:
        if not self.tracker:
            raise RuntimeError("OrderTracker is required for live mode (WebSocket updates).")

        price = round_down(price, self.market.tick_size)
        qty = round_down(qty, self.market.lot_size)

        price_str = format_decimal(price, self.market.price_precision)
        qty_str = format_decimal(qty, self.market.qty_precision)

        use_post_only = self.risk.use_post_only if post_only is None else post_only
        order_type = "LIMIT_MAKER" if use_post_only else "LIMIT"
        cid = self._client_id()

        if debug:
            log_event("order_place", {
                "account": self.name,
                "side": side,
                "type": order_type,
                "price": price_str,
                "qty": qty_str,
                "clientId": cid,
            })

        if self.spot_web:
            trade_type = "BUY" if side.upper() == "BUY" else "SELL"
            order_type_web = "LIMIT_ORDER"
            try:
                resp = self.spot_web.create_order_by_symbol(
                    symbol=self.symbol,
                    tradeType=trade_type,
                    orderType=order_type_web,
                    price=price_str,
                    quantity=qty_str,
                )
                log_event("order_web_response", {"account": self.name, "resp": resp})
            except MexcSpotError as exc:
                raise RuntimeError(f"Spot WEB order failed: {exc}") from exc
        else:
            resp = self.client.new_order(
                self.symbol,
                side,
                order_type,
                quantity=qty_str,
                price=price_str,
                new_client_order_id=cid,
            )

        order_id = _extract_order_id(resp)
        if not order_id:
            raise RuntimeError(f"Order placement returned no orderId: {resp}")
        return order_id, cid, price_str, qty_str

    def place_market_buy_by_quote(
        self,
        quote_amount: Decimal,
        *,
        price: Optional[Decimal] = None,
        debug: bool = True,
    ) -> Dict[str, Any]:
        if debug:
            log_event("order_place", {"account": self.name, "side": "BUY", "type": "MARKET", "quote": str(quote_amount)})
        if self.spot_web:
            try:
                if price is None:
                    raise RuntimeError("Price is required for WEB market order")
                qty = round_down(quote_amount / price, self.market.lot_size)
                if qty <= 0:
                    raise RuntimeError("Computed market qty <= 0")
                price_str = format_decimal(price, self.market.price_precision)
                qty_str = format_decimal(qty, self.market.qty_precision)
                resp = self.spot_web.create_order_by_symbol(
                    symbol=self.symbol,
                    tradeType="BUY",
                    orderType="LIMIT_ORDER",
                    quantity=qty_str,
                    price=price_str,
                )
                log_event("order_web_response", {"account": self.name, "resp": resp})
            except MexcSpotError as exc:
                raise RuntimeError(f"Spot WEB order failed: {exc}") from exc
            return resp
        return self.client.new_order(
            self.symbol,
            "BUY",
            "MARKET",
            quantity=None,
            price=None,
            quote_amount=str(quote_amount),
        )


def _extract_order_id(resp: Dict[str, Any]) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in ("orderId", "orderIdStr", "id"):
        if key in resp:
            return str(resp.get(key) or "")
    data = resp.get("data")
    if isinstance(data, dict):
        for key in ("orderId", "orderIdStr", "id"):
            if key in data:
                return str(data.get(key) or "")
    return ""
