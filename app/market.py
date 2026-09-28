from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Tuple

from app.client import MEXCClient
from app.models import MarketSpec
from app.utils import round_down


def parse_market_spec(info: Dict[str, Any]) -> MarketSpec:
    filters = info["symbols"][0]["filters"]
    symbol_info = info["symbols"][0]
    base_asset = str(symbol_info.get("baseAsset", ""))
    quote_asset = str(symbol_info.get("quoteAsset", ""))
    tick_size = Decimal("0")
    lot_size = Decimal("0")
    min_notional = Decimal("0")

    for item in filters:
        if item.get("filterType") == "PRICE_FILTER":
            tick_size = Decimal(item["tickSize"])
        elif item.get("filterType") == "LOT_SIZE":
            lot_size = Decimal(item["stepSize"])
        elif item.get("filterType") == "MIN_NOTIONAL":
            min_notional = Decimal(item["minNotional"])

    price_precision = int(symbol_info.get("pricePrecision", symbol_info.get("quotePrecision", 8)))
    qty_precision = int(symbol_info.get("baseAssetPrecision", 8))

    if tick_size == 0:
        tick_size = Decimal("1").scaleb(-price_precision)
    if lot_size == 0:
        lot_size = Decimal("1").scaleb(-qty_precision)

    return MarketSpec(
        tick_size=tick_size,
        lot_size=lot_size,
        min_notional=min_notional,
        price_precision=price_precision,
        qty_precision=qty_precision,
        base_asset=base_asset,
        quote_asset=quote_asset,
    )


def compute_base_qty_from_quote(quote_amount: Decimal, price: Decimal, fee_rate: Decimal, lot_step: Decimal) -> Decimal:
    gross_qty = (quote_amount / price) * (Decimal("1") - fee_rate)
    return round_down(gross_qty, lot_step)


def fetch_best_bid_ask(client: MEXCClient, symbol: str) -> Tuple[Decimal, Decimal]:
    book = client.get_book_ticker(symbol)
    bid = Decimal(book["bidPrice"])
    ask = Decimal(book["askPrice"])
    return bid, ask


def fetch_top_level_depth(client: MEXCClient, symbol: str) -> Tuple[Tuple[Decimal, Decimal], Tuple[Decimal, Decimal]]:
    depth = client.get_depth(symbol, limit=5)
    bids = depth.get("bids", [])
    asks = depth.get("asks", [])
    if not bids or not asks:
        raise RuntimeError(f"No depth for {symbol}: {depth}")
    best_bid_price = Decimal(bids[0][0])
    best_bid_qty = Decimal(bids[0][1])
    best_ask_price = Decimal(asks[0][0])
    best_ask_qty = Decimal(asks[0][1])
    return (best_bid_price, best_bid_qty), (best_ask_price, best_ask_qty)
