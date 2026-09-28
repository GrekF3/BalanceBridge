from __future__ import annotations

import json
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict


def round_down(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    quantized = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    return quantized


def format_decimal(value: Decimal, precision: int) -> str:
    quant = Decimal("1").scaleb(-precision)
    return f"{value.quantize(quant, rounding=ROUND_DOWN):.{precision}f}"


def log_event(event: str, payload: Dict[str, Any]) -> None:
    logging.info("%s | %s", event, json.dumps(payload, ensure_ascii=False, default=str))
