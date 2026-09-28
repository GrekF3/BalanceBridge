from __future__ import annotations

import hashlib
import hmac
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from app.models import ApiCredentials


class MEXCClient:
    def __init__(
        self,
        credentials: ApiCredentials,
        timeout_s: int,
        base_url: str,
        max_retries: int = 2,
        backoff_base_ms: int = 250,
    ) -> None:
        self.credentials = credentials
        self.timeout_s = timeout_s
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base_ms = backoff_base_ms
        self._time_offset_ms: Optional[int] = None

    def _sign(self, query: str) -> str:
        return hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _sleep_backoff(self, attempt: int) -> None:
        base = self.backoff_base_ms * (2 ** max(0, attempt - 1))
        jitter = random.randint(0, 120)
        time.sleep((base + jitter) / 1000.0)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        api_key: bool = False,
    ) -> Dict[str, Any]:
        params = params or {}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        if signed:
            if not self.credentials.api_key or not self.credentials.api_secret:
                raise RuntimeError("Missing API credentials for signed request")
            params["timestamp"] = self._get_timestamp()
            params.setdefault("recvWindow", 5000)
            query_wo_sig = urllib.parse.urlencode(params, doseq=True)
            params["signature"] = self._sign(query_wo_sig)
            headers["X-MEXC-APIKEY"] = self.credentials.api_key
        elif api_key:
            if not self.credentials.api_key:
                raise RuntimeError("Missing API key for request")
            headers["X-MEXC-APIKEY"] = self.credentials.api_key

        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        req = urllib.request.Request(url, data=None, headers=headers, method=method.upper())

        last_err: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = resp.read()
                return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="ignore")
                if exc.code in (429, 500, 502, 503, 504) and attempt <= self.max_retries + 1:
                    last_err = RuntimeError(f"MEXC {method.upper()} {path} HTTP {exc.code}: {details}")
                    self._sleep_backoff(attempt)
                    continue
                raise RuntimeError(f"MEXC {method.upper()} {path} error {exc.code}: {details}") from exc
            except urllib.error.URLError as exc:
                last_err = RuntimeError(f"MEXC {method.upper()} {path} connection error: {exc}")
                if attempt <= self.max_retries + 1:
                    self._sleep_backoff(attempt)
                    continue
                raise last_err from exc

        raise last_err or RuntimeError("Unknown request error")

    def _get_timestamp(self) -> int:
        if self._time_offset_ms is None:
            st = self.get_server_time()
            local = int(time.time() * 1000)
            self._time_offset_ms = int(st["serverTime"]) - local
        return int(time.time() * 1000) + int(self._time_offset_ms)

    def get_server_time(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/time")

    def get_exchange_info(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/exchangeInfo", {"symbol": symbol})

    def get_book_ticker(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    def get_depth(self, symbol: str, limit: int = 5) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/depth", {"symbol": symbol, "limit": limit})

    def get_account(self) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/account", signed=True)

    def new_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Optional[str],
        price: Optional[str],
        quote_amount: Optional[str] = None,
        new_client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"symbol": symbol, "side": side, "type": order_type}
        if new_client_order_id:
            payload["newClientOrderId"] = new_client_order_id
        if quantity is not None:
            payload["quantity"] = quantity
        if order_type.upper() in ("LIMIT", "LIMIT_MAKER"):
            if price is None:
                raise ValueError("price required for LIMIT/LIMIT_MAKER")
            payload["price"] = price
            payload["timeInForce"] = "GTC"
        if order_type.upper() == "MARKET" and side.upper() == "BUY":
            if quote_amount is None:
                raise ValueError("quoteOrderQty required for MARKET BUY")
            payload["quoteOrderQty"] = quote_amount

        return self._request("POST", "/api/v3/order", payload, signed=True)

    def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_order(self, symbol: str, order_id: Optional[str] = None, orig_client_order_id: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        if orig_client_order_id:
            params["origClientOrderId"] = orig_client_order_id
        return self._request("DELETE", "/api/v3/order", params, signed=True)

    def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        return self._request("GET", "/api/v3/openOrders", params, signed=True)

    def create_listen_key(self) -> str:
        try:
            data = self._request("POST", "/api/v3/userDataStream", api_key=True)
        except RuntimeError as exc:
            if _is_signature_missing_error(exc):
                data = self._request("POST", "/api/v3/userDataStream", signed=True)
            else:
                raise
        listen_key = data.get("listenKey", "")
        if not listen_key:
            raise RuntimeError(f"Failed to create listenKey: {data}")
        return str(listen_key)

    def keepalive_listen_key(self, listen_key: str) -> None:
        try:
            self._request("PUT", "/api/v3/userDataStream", {"listenKey": listen_key}, api_key=True)
        except RuntimeError as exc:
            if _is_signature_missing_error(exc):
                self._request("PUT", "/api/v3/userDataStream", {"listenKey": listen_key}, signed=True)
            else:
                raise

    def close_listen_key(self, listen_key: str) -> None:
        try:
            self._request("DELETE", "/api/v3/userDataStream", {"listenKey": listen_key}, api_key=True)
        except RuntimeError as exc:
            if _is_signature_missing_error(exc):
                self._request("DELETE", "/api/v3/userDataStream", {"listenKey": listen_key}, signed=True)
            else:
                raise


def _is_signature_missing_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "signature" in msg or "700004" in msg
