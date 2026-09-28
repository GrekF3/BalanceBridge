from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class ApiCredentials:
    api_key: str
    api_secret: str


@dataclass
class MarketSpec:
    tick_size: Decimal
    lot_size: Decimal
    min_notional: Decimal
    price_precision: int
    qty_precision: int
    base_asset: str
    quote_asset: str


@dataclass
class SimulatedAccount:
    name: str
    base_balance: Decimal
    quote_balance: Decimal

    def apply_buy(self, price: Decimal, qty: Decimal, fee_rate: Decimal) -> None:
        cost = price * qty
        fee = qty * fee_rate
        self.quote_balance -= cost
        self.base_balance += qty - fee

    def apply_sell(self, price: Decimal, qty: Decimal, fee_rate: Decimal) -> None:
        proceeds = price * qty
        fee = proceeds * fee_rate
        self.base_balance -= qty
        self.quote_balance += proceeds - fee


@dataclass
class RiskConfig:
    min_spread_ticks: int
    min_top_quote: Decimal
    max_participation_of_top: Decimal
    max_slippage_bps: int
    order_timeout_s: int
    use_post_only: bool
    max_retries: int
    backoff_base_ms: int
    spread_offset_pct: Decimal
    turbo_mode: bool


@dataclass
class AppConfig:
    symbol: str
    fee_rate: Decimal
    quote_amount: Decimal
    max_cycles: int
    timeout_s: int
    base_url: str
    u_id_a: str
    u_id_b: str
    account_a: ApiCredentials
    account_b: ApiCredentials
    account_b_quote_start: Decimal
    keys_file: str
    mode: str
    risk: RiskConfig
