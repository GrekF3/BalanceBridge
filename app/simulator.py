from __future__ import annotations

import dataclasses
import threading
import time
from decimal import Decimal

from app.client import MEXCClient
from app.market import compute_base_qty_from_quote, fetch_best_bid_ask, fetch_top_level_depth, parse_market_spec
from app.models import AppConfig, SimulatedAccount
from app.risk import RiskEngine
from app.utils import log_event, round_down


def run_simulator(cfg: AppConfig, stop_event: threading.Event) -> None:
    client_a = MEXCClient(cfg.account_a, cfg.timeout_s, cfg.base_url, cfg.risk.max_retries, cfg.risk.backoff_base_ms)
    market_info = client_a.get_exchange_info(cfg.symbol)
    market = parse_market_spec(market_info)
    risk_engine = RiskEngine(market, cfg.risk)

    log_event("market_spec", dataclasses.asdict(market))
    log_event("mode", {"mode": cfg.mode})

    account_a = SimulatedAccount(name="A", base_balance=Decimal("0"), quote_balance=cfg.quote_amount)
    account_b = SimulatedAccount(name="B", base_balance=Decimal("0"), quote_balance=cfg.account_b_quote_start)

    bid, ask = fetch_best_bid_ask(client_a, cfg.symbol)
    seed_qty = compute_base_qty_from_quote(cfg.quote_amount, ask, cfg.fee_rate, market.lot_size)
    if seed_qty <= 0:
        raise RuntimeError("Computed base quantity is zero; increase quote_amount or choose another symbol.")
    ok_notional, msg_notional = risk_engine.check_notional(ask, seed_qty)
    if not ok_notional:
        raise RuntimeError(msg_notional)

    account_b.apply_buy(ask, seed_qty, cfg.fee_rate)
    log_event("sim_init_buy_b", {"price": str(ask), "qty": str(seed_qty), "b": dataclasses.asdict(account_b)})

    for cycle in range(1, cfg.max_cycles + 1):
        if stop_event.is_set():
            log_event("stop", {"cycle": cycle})
            break

        (bbp, bbq), (bap, baq) = fetch_top_level_depth(client_a, cfg.symbol)
        ok_spread, msg_spread = risk_engine.check_spread(bbp, bap)
        if not ok_spread:
            log_event("skip", {"reason": msg_spread, "cycle": cycle})
            time.sleep(0.25)
            continue

        base_qty = compute_base_qty_from_quote(cfg.quote_amount, bap, cfg.fee_rate, market.lot_size)
        if base_qty <= 0:
            log_event("stop", {"cycle": cycle, "reason": "base_qty<=0"})
            break

        ok_depth_sell, msg_depth_sell = risk_engine.check_top_of_book_depth(best_qty=bbq, best_price=bbp, order_qty=base_qty)
        ok_depth_buy, msg_depth_buy = risk_engine.check_top_of_book_depth(best_qty=baq, best_price=bap, order_qty=base_qty)

        if not ok_depth_sell or not ok_depth_buy:
            log_event("skip", {"cycle": cycle, "reason": "depth_guard", "sell": msg_depth_sell, "buy": msg_depth_buy})
            time.sleep(0.25)
            continue

        sell_price = round_down(bbp, market.tick_size)
        buy_price = round_down(bap, market.tick_size)

        ok_slip_s, msg_slip_s = risk_engine.check_slippage_bps(sell_price, bbp)
        ok_slip_b, msg_slip_b = risk_engine.check_slippage_bps(buy_price, bap)
        if not ok_slip_s or not ok_slip_b:
            log_event("skip", {"cycle": cycle, "reason": "slippage_guard", "sell": msg_slip_s, "buy": msg_slip_b})
            time.sleep(0.25)
            continue

        if account_b.base_balance >= base_qty:
            account_b.apply_sell(sell_price, base_qty, cfg.fee_rate)
            account_a.apply_buy(buy_price, base_qty, cfg.fee_rate)
            log_event("sim_cycle", {
                "cycle": cycle,
                "sell_price": str(sell_price),
                "buy_price": str(buy_price),
                "A": dataclasses.asdict(account_a),
                "B": dataclasses.asdict(account_b),
            })
        else:
            log_event("sim_insufficient_base", {"cycle": cycle, "b_base": str(account_b.base_balance)})
            break

        time.sleep(0.2)
