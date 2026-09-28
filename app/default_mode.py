from __future__ import annotations

import dataclasses
import threading
import time
from decimal import Decimal, ROUND_UP
from typing import Optional

from app.client import MEXCClient
from services import MexcSpotWebSDK
from app.market import compute_base_qty_from_quote, fetch_top_level_depth, parse_market_spec
from app.models import AppConfig, MarketSpec
from app.orders import OrderManager, _extract_order_id
from app.risk import RiskEngine
from app.user_stream import OrderTracker, UserStream, _extract_event
from app.utils import format_decimal, log_event, round_down


class DefaultRunner:
    _MAX_SAME_PRICE_RETRIES = 3
    _SOFT_FAIL = "soft_fail"
    _HARD_STOP = "hard_stop"
    _OK = "ok"

    def __init__(self, cfg: AppConfig, stop_event: threading.Event) -> None:
        self.cfg = cfg
        self.stop_event = stop_event
        if cfg.risk.turbo_mode:
            self._fast_order_timeout_s = 0.1
            self._loop_sleep_s = 0.001
            self._retry_sleep_s = 0.001
            self._seed_poll_sleep_s = 0.02
        else:
            self._fast_order_timeout_s = 1.0
            self._loop_sleep_s = 0.05
            self._retry_sleep_s = 0.05
            self._seed_poll_sleep_s = 0.2
        self.client_a = MEXCClient(cfg.account_a, cfg.timeout_s, cfg.base_url, cfg.risk.max_retries, cfg.risk.backoff_base_ms)
        self.client_b = MEXCClient(cfg.account_b, cfg.timeout_s, cfg.base_url, cfg.risk.max_retries, cfg.risk.backoff_base_ms)
        self.web_a = MexcSpotWebSDK(cfg.u_id_a, u_id=cfg.u_id_a) if cfg.u_id_a else None
        self.web_b = MexcSpotWebSDK(cfg.u_id_b, u_id=cfg.u_id_b) if cfg.u_id_b else None
        self.market: Optional[MarketSpec] = None
        self.tracker_a = OrderTracker()
        self.tracker_b = OrderTracker()
        self.stream_a: Optional[UserStream] = None
        self.stream_b: Optional[UserStream] = None
        self.om_a: Optional[OrderManager] = None
        self.om_b: Optional[OrderManager] = None

    def _handle_event_a(self, message: dict) -> None:
        self.tracker_a.handle_event(message)
        event, payload = _extract_event(message)
        if event == "outboundAccountPosition":
            log_event("account_a_update", {"data": payload})

    def _handle_event_b(self, message: dict) -> None:
        self.tracker_b.handle_event(message)
        event, payload = _extract_event(message)
        if event == "outboundAccountPosition":
            log_event("account_b_update", {"data": payload})

    def start_streams(self) -> None:
        self.stream_a = UserStream(self.client_a, self._handle_event_a, self.stop_event)
        self.stream_b = UserStream(self.client_b, self._handle_event_b, self.stop_event)
        self.stream_a.start()
        self.stream_b.start()

    def stop_streams(self) -> None:
        if self.stream_a:
            self.stream_a.stop()
        if self.stream_b:
            self.stream_b.stop()

    def cancel_all(self) -> None:
        if self.om_a:
            canceled_a = self.om_a.cancel_all_open_orders()
            log_event("cancel_all_a", {"canceled": canceled_a})
        if self.om_b:
            canceled_b = self.om_b.cancel_all_open_orders()
            log_event("cancel_all_b", {"canceled": canceled_b})

    def _stop_with_reason(self, reason: str, payload: Optional[dict] = None) -> None:
        reason_ru = {
            "base_qty<=0": "расчетный объем базовой монеты <= 0",
            "seed_notional_guard": "объем seed-сделки ниже минимального",
            "seed_executed_qty<=0": "market-buy не исполнился (executedQty=0)",
            "seed_base_qty<=0": "после комиссии объем seed <= 0",
            "seed_limit_qty<=0": "лимитный seed-объем <= 0",
            "seed_limit_no_fill": "лимитный seed-ордер не исполнился",
            "seed_limit_base_qty<=0": "после комиссии лимитный seed-объем <= 0",
            "maker_taken_external": "наш maker-ордер был исполнен внешним участником",
            "maker_taken_or_rejected": "maker-ордер частично исполнен/отменен/отклонен",
            "maker_not_best": "наш ордер не лучший в стакане",
            "maker_reprice_invalid": "нельзя улучшить цену (выходит за спред)",
            "maker_same_price_retry_limit": "слишком много попыток улучшить цену на том же уровне",
            "pair_not_filled": "пара ордеров не исполнилась",
            "insufficient_base_a": "на аккаунте A недостаточно базовой монеты",
            "symbol_not_supported_api": "символ недоступен через API",
        }.get(reason, reason)
        data = {"reason": reason}
        data["reason_ru"] = reason_ru
        if payload:
            data.update(payload)
        log_event("stop", data)
        self.stop_event.set()
        self.cancel_all()

    def _get_order_status(self, client: MEXCClient, order_id: str) -> dict:
        try:
            return client.get_order(self.cfg.symbol, order_id)
        except Exception as exc:  # noqa: BLE001
            log_event("order_status_error", {"orderId": order_id, "error": str(exc)})
            return {}

    def _is_undercut(self, side: str, price: Decimal) -> bool:
        (bbp, _), (bap, _) = fetch_top_level_depth(self.client_a, self.cfg.symbol)
        if side.upper() == "SELL":
            return bap < price
        if side.upper() == "BUY":
            return bbp > price
        return False

    def _cancel_order(self, client: MEXCClient, order_id: str) -> None:
        try:
            client.cancel_order(self.cfg.symbol, order_id=order_id)
            log_event("order_cancel", {"orderId": order_id})
        except Exception as exc:  # noqa: BLE001
            log_event("order_cancel_failed", {"orderId": order_id, "error": str(exc)})

    def _is_filled(self, status: Optional[dict]) -> bool:
        if not status:
            return False
        return str(status.get("status", "")).upper() == "FILLED"

    def _get_balance(self, client: MEXCClient, asset: str) -> Decimal:
        if not asset:
            return Decimal("0")
        try:
            data = client.get_account()
        except Exception as exc:  # noqa: BLE001
            log_event("balance_error", {"asset": asset, "error": str(exc)})
            return Decimal("0")
        for entry in data.get("balances", []):
            if str(entry.get("asset", "")).upper() == asset.upper():
                free = entry.get("free", "0")
                try:
                    return Decimal(str(free))
                except Exception:  # noqa: BLE001
                    return Decimal("0")
        return Decimal("0")

    def _adjust_price_after_taken(self, side: str, price: Decimal) -> Decimal:
        if side.upper() == "SELL":
            new_price = price - self.market.tick_size
        else:
            new_price = price + self.market.tick_size
        return round_down(new_price, self.market.tick_size)

    def _round_up(self, value: Decimal, step: Decimal) -> Decimal:
        if step == 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_UP) * step

    def _check_best_price(self, side: str, price: Decimal, qty: Decimal) -> tuple[str, Decimal, dict]:
        (bbp, bbq), (bap, baq) = fetch_top_level_depth(self.client_a, self.cfg.symbol)
        info = {"best_bid": str(bbp), "best_ask": str(bap), "best_bid_qty": str(bbq), "best_ask_qty": str(baq)}
        if side.upper() == "SELL":
            if bap < price:
                next_price = round_down(bap - self.market.tick_size, self.market.tick_size)
                if next_price <= bbp:
                    return "invalid_reprice", price, info
                return "not_best", next_price, info
            if bap == price and baq > qty:
                next_price = round_down(price - self.market.tick_size, self.market.tick_size)
                if next_price <= bbp:
                    return "invalid_reprice", price, info
                return "same_price", next_price, info
            return "best", price, info
        if side.upper() == "BUY":
            if bbp > price:
                next_price = round_down(bbp + self.market.tick_size, self.market.tick_size)
                if next_price >= bap:
                    return "invalid_reprice", price, info
                return "not_best", next_price, info
            if bbp == price and bbq > qty:
                next_price = round_down(price + self.market.tick_size, self.market.tick_size)
                if next_price >= bap:
                    return "invalid_reprice", price, info
                return "same_price", next_price, info
            return "best", price, info
        return "not_best", price, info

    def _execute_pair(
        self,
        maker_om: OrderManager,
        taker_om: OrderManager,
        maker_side: str,
        price: Decimal,
        qty: Decimal,
        cycle: int,
        leg: str,
    ) -> str:
        attempt = 0
        current_price = price
        while attempt < self._MAX_SAME_PRICE_RETRIES:
            attempt += 1
            maker_order = maker_om.place_limit(maker_side, current_price, qty, debug=True, post_only=True)
            maker_order_id = str(maker_order["orderId"])
            taker_side = "BUY" if maker_side.upper() == "SELL" else "SELL"
            taker_order = taker_om.place_limit(taker_side, current_price, qty, debug=True, post_only=False)
            taker_order_id = str(taker_order["orderId"])

            state, next_price, info = self._check_best_price(maker_side, current_price, qty)
            if state == "not_best":
                self._cancel_order(maker_om.client, maker_order_id)
                self._cancel_order(taker_om.client, taker_order_id)
                log_event(
                    "maker_not_best_reprice",
                    {"cycle": cycle, "leg": leg, "price": str(current_price), "next_price": str(next_price), **info},
                )
                current_price = next_price
                continue
            if state == "invalid_reprice":
                self._cancel_order(maker_om.client, maker_order_id)
                self._cancel_order(taker_om.client, taker_order_id)
                log_event("maker_reprice_invalid", {"cycle": cycle, "leg": leg, "price": str(current_price), **info})
                return self._SOFT_FAIL
            if state == "same_price":
                self._cancel_order(maker_om.client, maker_order_id)
                self._cancel_order(taker_om.client, taker_order_id)
                log_event(
                    "maker_same_price_reprice",
                    {"cycle": cycle, "leg": leg, "price": str(current_price), "next_price": str(next_price), **info},
                )
                current_price = next_price
                continue
            maker_terminal = maker_om.tracker.wait_for_terminal(maker_order_id, self._fast_order_timeout_s)
            taker_terminal = taker_om.tracker.wait_for_terminal(taker_order_id, self._fast_order_timeout_s)

            if not self._is_filled(maker_terminal):
                maker_terminal = self._get_order_status(maker_om.client, maker_order_id)
            if not self._is_filled(taker_terminal):
                taker_terminal = self._get_order_status(taker_om.client, taker_order_id)

            if not self._is_filled(taker_terminal) or not self._is_filled(maker_terminal):
                log_event(
                    "pair_not_filled",
                    {
                        "cycle": cycle,
                        "leg": leg,
                        "maker_status": str(maker_terminal.get("status") if maker_terminal else None),
                        "taker_status": str(taker_terminal.get("status") if taker_terminal else None),
                    },
                )
                return self._SOFT_FAIL

            return self._OK

        log_event("maker_same_price_retry_limit", {"cycle": cycle, "leg": leg, "attempt": attempt})
        return self._SOFT_FAIL

    def run(self) -> None:
        self.market = parse_market_spec(self.client_a.get_exchange_info(self.cfg.symbol))
        risk_engine = RiskEngine(self.market, self.cfg.risk)
        self.om_a = OrderManager(self.client_a, self.cfg.symbol, self.market, self.cfg.risk, tracker=self.tracker_a, spot_web=self.web_a, name="A")
        self.om_b = OrderManager(self.client_b, self.cfg.symbol, self.market, self.cfg.risk, tracker=self.tracker_b, spot_web=self.web_b, name="B")

        log_event("market_spec", dataclasses.asdict(self.market))
        log_event("mode", {"mode": self.cfg.mode})

        self.start_streams()
        try:
            self.cancel_all()

            (bbp, bbq), (bap, baq) = fetch_top_level_depth(self.client_a, self.cfg.symbol)
            seed_quote = self.cfg.quote_amount
            base_qty = compute_base_qty_from_quote(seed_quote, bap, self.cfg.fee_rate, self.market.lot_size)
            if base_qty <= 0:
                self._stop_with_reason("base_qty<=0")
                return

            ok_notional, msg_notional = risk_engine.check_notional(bap, base_qty)
            if not ok_notional:
                self._stop_with_reason("seed_notional_guard", {"message": msg_notional})
                return

            buy_resp = self.om_a.place_market_buy_by_quote(seed_quote, price=bap)
            if self.web_a:
                base_qty = compute_base_qty_from_quote(seed_quote, bap, self.cfg.fee_rate, self.market.lot_size)
                if base_qty <= 0:
                    self._stop_with_reason("seed_base_qty<=0", {"order": buy_resp})
                    return
                log_event(
                    "a_market_buy_seed",
                    {
                        "order": buy_resp,
                        "status": "web_assumed",
                        "price_ref": str(bap),
                        "qty": str(base_qty),
                    },
                )
            else:
                buy_order_id = _extract_order_id(buy_resp)
                buy_status = {}
                executed_qty_raw = Decimal("0")
                deadline = time.time() + max(1, self.cfg.risk.order_timeout_s)
                while time.time() < deadline:
                    buy_status = self._get_order_status(self.client_a, buy_order_id) if buy_order_id else {}
                    executed_qty_raw = Decimal(str(buy_status.get("executedQty", "0") or "0"))
                    status_text = str(buy_status.get("status", "")).upper()
                    if executed_qty_raw > 0 or status_text in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                        break
                    time.sleep(self._seed_poll_sleep_s)

                if executed_qty_raw > 0:
                    base_qty = round_down(executed_qty_raw * (Decimal("1") - self.cfg.fee_rate), self.market.lot_size)
                    if base_qty <= 0:
                        self._stop_with_reason("seed_base_qty<=0", {"order": buy_resp, "status": buy_status})
                        return
                    log_event(
                        "a_market_buy_seed",
                        {
                            "order": buy_resp,
                            "status": buy_status,
                            "price_ref": str(bap),
                            "qty": str(base_qty),
                        },
                    )
                else:
                    seed_price = round_down(bap + self.market.tick_size, self.market.tick_size)
                    seed_qty = compute_base_qty_from_quote(
                        seed_quote,
                        seed_price,
                        self.cfg.fee_rate,
                        self.market.lot_size,
                    )
                    if seed_qty <= 0:
                        self._stop_with_reason("seed_limit_qty<=0", {"order": buy_resp, "status": buy_status})
                        return
                    seed_resp = self.om_a.place_limit(
                        "BUY",
                        seed_price,
                        seed_qty,
                        debug=True,
                        post_only=False,
                    )
                    seed_order_id = str(seed_resp.get("orderId", ""))
                    seed_status = {}
                    executed_qty_raw = Decimal("0")
                    deadline = time.time() + max(1, self.cfg.risk.order_timeout_s)
                    while time.time() < deadline:
                        seed_status = self._get_order_status(self.client_a, seed_order_id) if seed_order_id else {}
                        executed_qty_raw = Decimal(str(seed_status.get("executedQty", "0") or "0"))
                        status_text = str(seed_status.get("status", "")).upper()
                        if executed_qty_raw > 0 or status_text in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}:
                            break
                        time.sleep(self._seed_poll_sleep_s)

                    if executed_qty_raw <= 0:
                        if seed_order_id:
                            self._cancel_order(self.client_a, seed_order_id)
                        self._stop_with_reason("seed_limit_no_fill", {"order": seed_resp, "status": seed_status})
                        return

                    base_qty = round_down(executed_qty_raw * (Decimal("1") - self.cfg.fee_rate), self.market.lot_size)
                    if base_qty <= 0:
                        self._stop_with_reason("seed_limit_base_qty<=0", {"order": seed_resp, "status": seed_status})
                        return
                    log_event(
                        "a_limit_buy_seed",
                        {
                            "order": seed_resp,
                            "status": seed_status,
                            "price_ref": str(seed_price),
                            "qty": str(base_qty),
                        },
                    )

            for cycle in range(1, self.cfg.max_cycles + 1):
                if self.stop_event.is_set():
                    log_event("stop", {"cycle": cycle})
                    break
                did_place_orders = False

                (bbp, bbq), (bap, baq) = fetch_top_level_depth(self.client_a, self.cfg.symbol)

                ok_spread, msg_spread = risk_engine.check_spread(bbp, bap)
                if not ok_spread:
                    log_event("skip", {"cycle": cycle, "reason": msg_spread})
                    time.sleep(self._loop_sleep_s)
                    continue

                ok_notional, msg_notional = risk_engine.check_notional(bap, base_qty)
                if not ok_notional:
                    log_event("skip", {"cycle": cycle, "reason": msg_notional})
                    time.sleep(self._loop_sleep_s)
                    continue

                ok_depth_sell, msg_depth_sell = risk_engine.check_top_of_book_depth(bbq, bbp, base_qty)
                ok_depth_buy, msg_depth_buy = risk_engine.check_top_of_book_depth(baq, bap, base_qty)
                if not ok_depth_sell or not ok_depth_buy:
                    max_qty_sell = round_down(bbq * self.cfg.risk.max_participation_of_top, self.market.lot_size)
                    max_qty_buy = round_down(baq * self.cfg.risk.max_participation_of_top, self.market.lot_size)
                    effective_qty = min(base_qty, max_qty_sell, max_qty_buy)
                    if effective_qty <= 0:
                        log_event("skip", {"cycle": cycle, "reason": "depth_guard", "sell": msg_depth_sell, "buy": msg_depth_buy})
                        time.sleep(self._loop_sleep_s)
                        continue
                    ok_notional_eff, msg_notional_eff = risk_engine.check_notional(bap, effective_qty)
                    if not ok_notional_eff:
                        log_event("skip", {"cycle": cycle, "reason": msg_notional_eff})
                        time.sleep(self._loop_sleep_s)
                        continue
                    log_event("depth_guard_adjust", {
                        "cycle": cycle,
                        "base_qty": str(base_qty),
                        "effective_qty": str(effective_qty),
                        "max_qty_sell": str(max_qty_sell),
                        "max_qty_buy": str(max_qty_buy),
                    })
                else:
                    effective_qty = base_qty

                # Core flow: A sells slightly above best bid; B sells slightly below best ask.
                tick = self.market.tick_size
                sell_price = round_down(bbp + tick, tick)
                buy_price = round_down(bap - tick, tick)
                if sell_price <= bbp or buy_price >= bap or sell_price >= buy_price:
                    log_event("skip", {"cycle": cycle, "reason": "spread_too_tight", "bid": str(bbp), "ask": str(bap)})
                    time.sleep(self._loop_sleep_s)
                    continue

                min_trade_quote = self.market.min_notional if self.market.min_notional > 0 else Decimal("0")
                req_qty_sell = self._round_up(min_trade_quote / sell_price, self.market.lot_size)
                req_qty_buy = self._round_up(min_trade_quote / buy_price, self.market.lot_size)
                min_required_qty = max(req_qty_sell, req_qty_buy)
                if effective_qty < min_required_qty:
                    max_qty_sell_full = round_down(bbq, self.market.lot_size)
                    max_qty_buy_full = round_down(baq, self.market.lot_size)
                    effective_full = min(base_qty, max_qty_sell_full, max_qty_buy_full)
                    if effective_full < min_required_qty:
                        log_event(
                            "skip",
                            {
                                "cycle": cycle,
                                "reason": "min_trade_quote_guard",
                                "min_trade_quote": str(min_trade_quote),
                                "effective_qty": str(effective_qty),
                                "min_required_qty": str(min_required_qty),
                                "effective_full": str(effective_full),
                            },
                        )
                        time.sleep(self._loop_sleep_s)
                        continue
                    effective_qty = min_required_qty
                elif effective_qty > min_required_qty:
                    effective_qty = min_required_qty

                ok_slip_s, msg_slip_s = risk_engine.check_slippage_bps(sell_price, bap)
                ok_slip_b, msg_slip_b = risk_engine.check_slippage_bps(buy_price, bbp)
                if not ok_slip_s or not ok_slip_b:
                    log_event("skip", {"cycle": cycle, "reason": "slippage_guard", "sell": msg_slip_s, "buy": msg_slip_b})
                    time.sleep(self._loop_sleep_s)
                    continue

                a_base = self._get_balance(self.client_a, self.market.base_asset)
                if a_base < effective_qty:
                    self._stop_with_reason(
                        "insufficient_base_a",
                        {"cycle": cycle, "have": str(a_base), "need": str(effective_qty)},
                    )
                    break

                try:
                    res1 = self._execute_pair(self.om_a, self.om_b, "SELL", sell_price, effective_qty, cycle, "a_sell_to_b")
                    if res1 == self._HARD_STOP:
                        break
                    if res1 == self._SOFT_FAIL:
                        time.sleep(self._retry_sleep_s)
                        continue
                    did_place_orders = True

                    res2 = self._execute_pair(self.om_b, self.om_a, "SELL", buy_price, effective_qty, cycle, "b_sell_to_a")
                    if res2 == self._HARD_STOP:
                        break
                    if res2 == self._SOFT_FAIL:
                        time.sleep(self._retry_sleep_s)
                        continue
                    did_place_orders = True

                    log_event("order_result", {"cycle": cycle, "sell_price": str(sell_price), "buy_price": str(buy_price)})
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    log_event("order_error", {"cycle": cycle, "error": err})
                    if _is_symbol_not_supported(err):
                        self._stop_with_reason("symbol_not_supported_api", {"cycle": cycle})
                        break

                if did_place_orders:
                    self.cancel_all()
                time.sleep(self._loop_sleep_s)
        finally:
            self.stop_streams()


def _is_symbol_not_supported(message: str) -> bool:
    msg = message.lower()
    return "10007" in msg or "symbol not support api" in msg
