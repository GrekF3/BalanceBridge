import time
import json
import uuid
import hmac
import hashlib
import urllib.parse
import requests
from decimal import Decimal, ROUND_DOWN
from collections import OrderedDict
from typing import Any, Dict, Optional, List, Union, Tuple


Json = Dict[str, Any]


# =========================
# CRYPTO
# =========================
def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def generate_chash(order: Dict[str, Any]) -> str:
    """
    Рабочий вариант (как у тебя): chash = md5(JSON.stringify(sorted business payload))
    Исключаем: chash, ts
    """
    base = {k: order[k] for k in order if k not in ("chash", "ts")}
    ordered = OrderedDict(sorted(base.items()))
    body = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    return md5_hex(body)


def sign_web(token: str, nonce_ts: str, body: str) -> str:
    """
    x-mxc-sign = md5(ts + body + md5(token + ts).substring(7))
    """
    tail = md5_hex(token + nonce_ts)[7:]
    return md5_hex(nonce_ts + body + tail)


# =========================
# ERRORS
# =========================
class MexcFuturesError(RuntimeError):
    def __init__(self, code: Any, message: str, payload: Optional[dict] = None):
        super().__init__(f"(code={code}) {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}


class MexcSpotError(RuntimeError):
    def __init__(self, code: Any, message: str, payload: Optional[dict] = None):
        super().__init__(f"(code={code}) {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}


# =========================
# HELPERS
# =========================
def _now_ms_str() -> str:
    return str(int(time.time() * 1000))


def _uuid32() -> str:
    # externalOid max 32 chars
    return uuid.uuid4().hex


def _json_dumps(obj: Any) -> str:
    # максимально похоже на JSON.stringify без пробелов
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _to_decimal(x: Any) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    q = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    # normalize to avoid exponent
    return q.quantize(step, rounding=ROUND_DOWN) if step < 1 else q


def _ensure_json_number(x: Decimal) -> Union[int, float]:
    # vol/price лучше отправлять числом, но без научной нотации
    s = format(x, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s == "":
        s = "0"
    return int(s) if s.isdigit() else float(s)


# =========================
# SDK
# =========================
class MexcFuturesSDK:
    """
    Private (WEB token) base:
      https://futures.mexc.com/api/v1/private

    Public market base:
      https://contract.mexc.com/api/v1/contract
    """

    PRIVATE_BASE = "https://futures.mexc.com/api/v1/private"
    PUBLIC_BASE = "https://contract.mexc.com/api/v1/contract"

    def __init__(
        self,
        authorization: str,
        *,
        u_id: Optional[str] = None,
        debug: bool = False,
        timeout_sec: int = 30,
    ):
        self.authorization = authorization
        self.u_id = u_id
        self.debug = debug
        self.timeout_sec = timeout_sec

        self.session = requests.Session()
        # базовые заголовки (как в твоём рабочем варианте)
        headers = {
            "accept": "*/*",
            "content-type": "application/json; charset=utf-8",
            "origin": "https://futures.mexc.com",
            "user-agent": "Mozilla/5.0",
            "authorization": authorization,
            "language": "English",
        }
        if u_id:
            headers["u-id"] = u_id
            headers["u_id"] = u_id
        self.session.headers.update(headers)

        # простой кэш под contract detail/ticker
        self._contract_cache: Dict[str, Json] = {}
        self._contract_cache_ts: float = 0.0

        self._ticker_cache: Dict[str, Json] = {}
        self._ticker_cache_ts: float = 0.0

    # -------------------------
    # Debug
    # -------------------------
    def _dbg(self, *args):
        if self.debug:
            print(*args)

    # -------------------------
    # Public endpoints (market)
    # -------------------------
    def server_time(self) -> int:
        r = requests.get(f"{self.PUBLIC_BASE}/ping", timeout=self.timeout_sec)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise MexcFuturesError(j.get("code"), j.get("message", "ping failed"), j)
        return int(j["data"])

    def contract_detail(self, symbol: Optional[str] = None, *, cache_ttl_sec: int = 5) -> Json:
        """
        GET /contract/detail
        Возвращает либо один объект, либо массив — поэтому нормализуем в dict по symbol.
        """
        now = time.time()
        if self._contract_cache and (now - self._contract_cache_ts) < cache_ttl_sec:
            if symbol:
                if symbol in self._contract_cache:
                    return self._contract_cache[symbol]
            else:
                # вернем "сырой" кэш (по всем символам)
                return dict(self._contract_cache)

        r = requests.get(f"{self.PUBLIC_BASE}/detail", timeout=self.timeout_sec)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise MexcFuturesError(j.get("code"), j.get("message", "detail failed"), j)

        data = j.get("data")
        cache: Dict[str, Json] = {}

        if isinstance(data, dict) and "symbol" in data:
            cache[data["symbol"]] = data
        elif isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and "symbol" in it:
                    cache[it["symbol"]] = it
        else:
            raise MexcFuturesError(-1, "Unexpected contract/detail response shape", j)

        self._contract_cache = cache
        self._contract_cache_ts = now

        if symbol:
            if symbol not in cache:
                raise MexcFuturesError(-1, f"Symbol not found in contract/detail: {symbol}")
            return cache[symbol]
        return dict(cache)

    def ticker(self, symbol: Optional[str] = None, *, cache_ttl_sec: int = 2) -> Union[Json, List[Json]]:
        """
        GET /contract/ticker?symbol=...
        Может вернуть list или объект — нормализуем.
        """
        now = time.time()
        if symbol and self._ticker_cache and (now - self._ticker_cache_ts) < cache_ttl_sec:
            if symbol in self._ticker_cache:
                return self._ticker_cache[symbol]

        params = {}
        if symbol:
            params["symbol"] = symbol
        r = requests.get(f"{self.PUBLIC_BASE}/ticker", params=params, timeout=self.timeout_sec)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise MexcFuturesError(j.get("code"), j.get("message", "ticker failed"), j)

        data = j.get("data")

        # кэшируем если это список
        if isinstance(data, list):
            cache: Dict[str, Json] = {}
            for it in data:
                if isinstance(it, dict) and "symbol" in it:
                    cache[it["symbol"]] = it
            if cache:
                self._ticker_cache = cache
                self._ticker_cache_ts = now
            if symbol:
                if symbol not in cache:
                    raise MexcFuturesError(-1, f"Symbol not found in ticker: {symbol}")
                return cache[symbol]
            return data

        # если объект
        if isinstance(data, dict) and symbol and data.get("symbol") == symbol:
            self._ticker_cache[symbol] = data
            self._ticker_cache_ts = now
        return data

    def fair_price(self, symbol: str) -> Decimal:
        r = requests.get(f"{self.PUBLIC_BASE}/fair_price/{symbol}", timeout=self.timeout_sec)
        r.raise_for_status()
        j = r.json()
        if not j.get("success"):
            raise MexcFuturesError(j.get("code"), j.get("message", "fair_price failed"), j)
        return _to_decimal(j["data"]["fairPrice"])

    # -------------------------
    # Size helpers (USDT -> vol)
    # -------------------------
    def vol_from_notional_usdt(
        self,
        symbol: str,
        notional_usdt: Union[int, float, str, Decimal],
        *,
        price: Optional[Union[int, float, str, Decimal]] = None,
        price_source: str = "fair",  # "fair" | "last"
    ) -> Decimal:
        """
        notional_usdt ≈ vol * contractSize * price
        => vol = notional_usdt / (contractSize * price)
        округляем вниз по volUnit и не меньше minVol
        """
        info = self.contract_detail(symbol)
        contract_size = _to_decimal(info["contractSize"])
        vol_unit = _to_decimal(info.get("volUnit", 1))
        min_vol = _to_decimal(info.get("minVol", 1))

        if price is None:
            if price_source == "last":
                t = self.ticker(symbol)
                price = _to_decimal(t["lastPrice"])
            else:
                price = self.fair_price(symbol)

        p = _to_decimal(price)
        n = _to_decimal(notional_usdt)

        raw_vol = n / (contract_size * p)
        vol = _floor_to_step(raw_vol, vol_unit)
        if vol < min_vol:
            vol = min_vol
        return vol

    def vol_from_margin_usdt(
        self,
        symbol: str,
        margin_usdt: Union[int, float, str, Decimal],
        leverage: int,
        *,
        price: Optional[Union[int, float, str, Decimal]] = None,
        price_source: str = "fair",
    ) -> Decimal:
        """
        margin_usdt -> notional_usdt = margin_usdt * leverage
        """
        notional = _to_decimal(margin_usdt) * _to_decimal(leverage)
        return self.vol_from_notional_usdt(symbol, notional, price=price, price_source=price_source)

    # -------------------------
    # Private request (WEB signed)
    # -------------------------
    def _private_headers(self, ts: str, body: str) -> Dict[str, str]:
        sign = sign_web(self.authorization, ts, body)

        # ВАЖНО: заголовки оставляем как у тебя
        headers = {
            "authorization": self.authorization,
            "x-mxc-nonce": ts,
            "x-mxc-sign": sign,
            "trochilus-trace-id": str(uuid.uuid4()),
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/json; charset=utf-8",
            "origin": "https://futures.mexc.com",
            "referer": "https://futures.mexc.com/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "content-length": str(len(body.encode("utf-8"))),
            "cookie": "",
        }
        if self.u_id:
            headers["u-id"] = self.u_id
            headers["u_id"] = self.u_id
        return headers

    def _post_private(self, path: str, payload: Any) -> Json:
        ts = _now_ms_str()
        body = _json_dumps(payload)
        headers = self._private_headers(ts, body)

        if self.debug:
            self._dbg("\n===== REQUEST =====")
            self._dbg("POST", self.PRIVATE_BASE + path)
            self._dbg("HEADERS:", headers)
            self._dbg("BODY:", body)

        resp = self.session.post(
            self.PRIVATE_BASE + path,
            headers=headers,
            data=body,
            timeout=self.timeout_sec,
        )

        if self.debug:
            self._dbg("STATUS:", resp.status_code)
            self._dbg("RESPONSE:", resp.text)

        resp.raise_for_status()
        j = resp.json()
        if not j.get("success", False):
            raise MexcFuturesError(j.get("code"), j.get("message", "request failed"), j)
        return j

    def _get_private(self, path: str, params: Optional[Dict[str, Any]] = None) -> Json:
        """
        По гайду WEB/APP подпись обязательна для POST.
        GET обычно проходит с Authorization, но если вдруг упрется — можно легко
        переделать на signed GET.
        """
        url = self.PRIVATE_BASE + path
        if self.debug:
            self._dbg("\n===== REQUEST =====")
            self._dbg("GET", url)
            self._dbg("PARAMS:", params or {})

        resp = self.session.get(url, params=params or {}, timeout=self.timeout_sec)
        if self.debug:
            self._dbg("STATUS:", resp.status_code)
            self._dbg("RESPONSE:", resp.text)

        resp.raise_for_status()
        j = resp.json()
        if not j.get("success", False):
            raise MexcFuturesError(j.get("code"), j.get("message", "request failed"), j)
        return j

    # -------------------------
    # Orders: create (our working flow)
    # -------------------------
    def create_order(
        self,
        *,
        symbol: str,
        side: int,
        vol: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,          # 1 isolated, 2 cross
        type_: int = 5,             # 5 market, 1 limit
        positionMode: int = 2,      # 2 one-way
        price: Optional[Union[int, float, str, Decimal]] = None,
        externalOid: Optional[str] = None,
        reduceOnly: Optional[bool] = None,
        priceProtect: Optional[str] = None,
        marketCeiling: Optional[bool] = None,
    ) -> Json:
        """
        POST /private/order/create

        ВАЖНО:
        - chash считаем по payload БЕЗ chash/ts
        - ts кладем в body строкой (как в сети), равен nonce
        - для market price можно не передавать
        """
        ts = _now_ms_str()

        v = _to_decimal(vol)
        payload = OrderedDict([
            ("symbol", symbol),
            ("vol", _ensure_json_number(v)),
            ("leverage", int(leverage)),
            ("side", int(side)),
            ("type", int(type_)),
            ("openType", int(openType)),
            ("positionMode", int(positionMode)),
        ])

        if type_ != 5:
            if price is None:
                raise ValueError("Limit order requires price")
            payload["price"] = _ensure_json_number(_to_decimal(price))

        if externalOid is None:
            externalOid = _uuid32()
        payload["externalOid"] = externalOid

        if reduceOnly is not None:
            payload["reduceOnly"] = bool(reduceOnly)
        if priceProtect is not None:
            payload["priceProtect"] = str(priceProtect)
        if marketCeiling is not None:
            payload["marketCeiling"] = bool(marketCeiling)

        # chash + ts (ts в body — как у тебя)
        payload["chash"] = generate_chash(payload)
        payload["ts"] = ts

        # отправляем уже готовый payload (подпись на него)
        return self._post_private("/order/create", payload)

    # side mapping:
    # 1 = open long
    # 3 = open short
    # 4 = close long
    # 2 = close short
    def long(
        self,
        symbol: str,
        *,
        vol: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        market: bool = True,
        price: Optional[Union[int, float, str, Decimal]] = None,
        **kwargs,
    ) -> Json:
        return self.create_order(
            symbol=symbol,
            side=1,
            vol=vol,
            leverage=leverage,
            openType=openType,
            positionMode=positionMode,
            type_=5 if market else 1,
            price=price,
            **kwargs,
        )

    def short(
        self,
        symbol: str,
        *,
        vol: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        market: bool = True,
        price: Optional[Union[int, float, str, Decimal]] = None,
        **kwargs,
    ) -> Json:
        return self.create_order(
            symbol=symbol,
            side=3,
            vol=vol,
            leverage=leverage,
            openType=openType,
            positionMode=positionMode,
            type_=5 if market else 1,
            price=price,
            **kwargs,
        )

    def close_long(
        self,
        symbol: str,
        *,
        vol: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        market: bool = True,
        price: Optional[Union[int, float, str, Decimal]] = None,
        **kwargs,
    ) -> Json:
        return self.create_order(
            symbol=symbol,
            side=4,
            vol=vol,
            leverage=leverage,
            openType=openType,
            positionMode=positionMode,
            type_=5 if market else 1,
            price=price,
            **kwargs,
        )

    def close_short(
        self,
        symbol: str,
        *,
        vol: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        market: bool = True,
        price: Optional[Union[int, float, str, Decimal]] = None,
        **kwargs,
    ) -> Json:
        return self.create_order(
            symbol=symbol,
            side=2,
            vol=vol,
            leverage=leverage,
            openType=openType,
            positionMode=positionMode,
            type_=5 if market else 1,
            price=price,
            **kwargs,
        )

    # -------------------------
    # Convenience: trade by USDT
    # -------------------------
    def long_by_margin(
        self,
        symbol: str,
        *,
        margin_usdt: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        price_source: str = "fair",
        **kwargs,
    ) -> Json:
        vol = self.vol_from_margin_usdt(symbol, margin_usdt, leverage, price_source=price_source)
        return self.long(symbol, vol=vol, leverage=leverage, openType=openType, positionMode=positionMode, **kwargs)

    def short_by_margin(
        self,
        symbol: str,
        *,
        margin_usdt: Union[int, float, str, Decimal],
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        price_source: str = "fair",
        **kwargs,
    ) -> Json:
        vol = self.vol_from_margin_usdt(symbol, margin_usdt, leverage, price_source=price_source)
        return self.short(symbol, vol=vol, leverage=leverage, openType=openType, positionMode=positionMode, **kwargs)

    # -------------------------
    # Positions / close_all (official endpoints)
    # -------------------------
    def open_positions(self, symbol: Optional[str] = None) -> Json:
        """
        GET /private/position/open_positions?symbol=...
        """
        params = {"symbol": symbol} if symbol else {}
        return self._get_private("/position/open_positions", params=params)

    def close_all_positions(self) -> Json:
        """
        POST /private/position/close_all
        """
        return self._post_private("/position/close_all", {})

    def close_all_symbol(
        self,
        symbol: str,
        *,
        leverage: int,
        openType: int = 1,
        positionMode: int = 2,
        market: bool = True,
    ) -> Dict[str, Any]:
        """
        Закрыть всё по символу через open_positions + order/create.
        """
        pos = self.open_positions(symbol)
        data = pos.get("data") or []
        out: Dict[str, Any] = {"closed": [], "positions": data}

        for p in data:
            # По докам там есть positionType: 1 long, 2 short (часто так)
            pt = p.get("positionType") or p.get("type") or p.get("side")
            hold_vol = p.get("holdVol") or p.get("vol") or p.get("positionVol")
            if hold_vol is None:
                continue

            v = _to_decimal(hold_vol)
            if v <= 0:
                continue

            if pt == 1:
                r = self.close_long(symbol, vol=v, leverage=leverage, openType=openType, positionMode=positionMode, market=market)
                out["closed"].append({"side": "close_long", "vol": str(v), "resp": r})
            elif pt == 2:
                r = self.close_short(symbol, vol=v, leverage=leverage, openType=openType, positionMode=positionMode, market=market)
                out["closed"].append({"side": "close_short", "vol": str(v), "resp": r})

        return out

    # -------------------------
    # Cancel / Orders (official endpoints)
    # -------------------------
    def cancel_orders(self, order_ids: List[Union[int, str]]) -> Json:
        """
        POST /private/order/cancel
        Body: [orderId1, orderId2, ...]
        """
        body = [int(x) for x in order_ids]
        return self._post_private("/order/cancel", body)

    def cancel_by_external(self, symbol: str, external_oid: str) -> Json:
        """
        POST /private/order/cancel_with_external
        Body: [{"symbol":"BTC_USDT","externalOid":"ext_11"}]
        """
        body = [{"symbol": symbol, "externalOid": external_oid}]
        return self._post_private("/order/cancel_with_external", body)

    def batch_cancel_by_external(self, items: List[Tuple[str, str]]) -> Json:
        """
        POST /private/order/batch_cancel_with_external
        items: [(symbol, externalOid), ...]
        """
        body = [{"symbol": s, "externalOid": eo} for s, eo in items]
        return self._post_private("/order/batch_cancel_with_external", body)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> Json:
        """
        POST /private/order/cancel_all
        Body: {"symbol": "..."} или {}
        """
        body = {"symbol": symbol} if symbol else {}
        return self._post_private("/order/cancel_all", body)


class MexcSpotSDK:
    """
    Spot Open API base:
      https://api.mexc.com
    """

    BASE = "https://api.mexc.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        u_id: Optional[str] = None,
        source: Optional[str] = None,
        debug: bool = False,
        timeout_sec: int = 30,
        recv_window: int = 5000,
    ):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.u_id = u_id
        self.source = source
        self.debug = debug
        self.timeout_sec = timeout_sec
        self.recv_window = recv_window

        self.session = requests.Session()
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if u_id:
            headers["u-id"] = u_id
            headers["u_id"] = u_id
        if source:
            headers["source"] = source
        self.session.headers.update(headers)

    def _dbg(self, *args):
        if self.debug:
            print(*args)

    def _sign(self, query: str) -> str:
        return hmac.new(self.api_secret, query.encode("utf-8"), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, *, signed: bool = False) -> Json:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers: Dict[str, str] = {}

        if signed:
            if not self.api_key or not self.api_secret:
                raise MexcSpotError(-1, "Missing API credentials for signed request")
            params["timestamp"] = int(time.time() * 1000)
            params.setdefault("recvWindow", self.recv_window)
            query_wo_sig = urllib.parse.urlencode(params, doseq=True)
            params["signature"] = self._sign(query_wo_sig)
            headers["X-MEXC-APIKEY"] = self.api_key

        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{self.BASE}{path}"
        if query:
            url = f"{url}?{query}"

        if self.debug:
            self._dbg("\n===== REQUEST =====")
            self._dbg(method.upper(), url)
            self._dbg("HEADERS:", headers)

        resp = self.session.request(method.upper(), url, headers=headers, timeout=self.timeout_sec)

        if self.debug:
            self._dbg("STATUS:", resp.status_code)
            self._dbg("RESPONSE:", resp.text)

        resp.raise_for_status()
        j = resp.json()
        if isinstance(j, dict) and "code" in j and str(j.get("code")) not in ("0", "200"):
            raise MexcSpotError(j.get("code"), j.get("msg", "request failed"), j)
        return j

    def server_time(self) -> Json:
        return self._request("GET", "/api/v3/time")

    def exchange_info(self, symbol: Optional[str] = None) -> Json:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/api/v3/exchangeInfo", params=params)

    def book_ticker(self, symbol: str) -> Json:
        return self._request("GET", "/api/v3/ticker/bookTicker", params={"symbol": symbol})

    def account(self) -> Json:
        return self._request("GET", "/api/v3/account", signed=True)

    def create_order(
        self,
        *,
        symbol: str,
        side: str,
        type_: str,
        quantity: Optional[Union[int, float, str, Decimal]] = None,
        price: Optional[Union[int, float, str, Decimal]] = None,
        quote_amount: Optional[Union[int, float, str, Decimal]] = None,
        new_client_order_id: Optional[str] = None,
        **kwargs,
    ) -> Json:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": type_.upper(),
        }
        if new_client_order_id:
            payload["newClientOrderId"] = new_client_order_id
        if quantity is not None:
            payload["quantity"] = str(quantity)
        if payload["type"] in ("LIMIT", "LIMIT_MAKER"):
            if price is None:
                raise ValueError("Limit order requires price")
            payload["price"] = str(price)
            payload["timeInForce"] = "GTC"
        if payload["type"] == "MARKET" and payload["side"] == "BUY":
            if quote_amount is None:
                raise ValueError("Market BUY requires quote_amount (quoteOrderQty)")
            payload["quoteOrderQty"] = str(quote_amount)
        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value
        return self._request("POST", "/api/v3/order", params=payload, signed=True)

    def buy(
        self,
        symbol: str,
        *,
        quantity: Optional[Union[int, float, str, Decimal]] = None,
        price: Optional[Union[int, float, str, Decimal]] = None,
        quote_amount: Optional[Union[int, float, str, Decimal]] = None,
        market: bool = True,
        **kwargs,
    ) -> Json:
        return self.create_order(
            symbol=symbol,
            side="BUY",
            type_="MARKET" if market else "LIMIT",
            quantity=quantity,
            price=price,
            quote_amount=quote_amount,
            **kwargs,
        )

    def sell(
        self,
        symbol: str,
        *,
        quantity: Union[int, float, str, Decimal],
        price: Optional[Union[int, float, str, Decimal]] = None,
        market: bool = True,
        **kwargs,
    ) -> Json:
        return self.create_order(
            symbol=symbol,
            side="SELL",
            type_="MARKET" if market else "LIMIT",
            quantity=quantity,
            price=price,
            **kwargs,
        )

    # -------------------------
    # Futures-like aliases (spot)
    # -------------------------
    def open_long(
        self,
        symbol: str,
        *,
        quantity: Optional[Union[int, float, str, Decimal]] = None,
        price: Optional[Union[int, float, str, Decimal]] = None,
        quote_amount: Optional[Union[int, float, str, Decimal]] = None,
        market: bool = True,
        **kwargs,
    ) -> Json:
        return self.buy(
            symbol,
            quantity=quantity,
            price=price,
            quote_amount=quote_amount,
            market=market,
            **kwargs,
        )

    def close_long(
        self,
        symbol: str,
        *,
        quantity: Union[int, float, str, Decimal],
        price: Optional[Union[int, float, str, Decimal]] = None,
        market: bool = True,
        **kwargs,
    ) -> Json:
        return self.sell(
            symbol,
            quantity=quantity,
            price=price,
            market=market,
            **kwargs,
        )

    def open_short(self, *args, **kwargs) -> Json:
        raise MexcSpotError(-1, "Spot short is not supported (no leverage)")

    def close_short(self, *args, **kwargs) -> Json:
        raise MexcSpotError(-1, "Spot short is not supported (no leverage)")

    def get_order(self, symbol: str, order_id: Union[int, str]) -> Json:
        return self._request("GET", "/api/v3/order", params={"symbol": symbol, "orderId": order_id}, signed=True)

    def cancel_order(self, symbol: str, order_id: Union[int, str]) -> Json:
        return self._request("DELETE", "/api/v3/order", params={"symbol": symbol, "orderId": order_id}, signed=True)

    def open_orders(self, symbol: Optional[str] = None) -> Json:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/api/v3/openOrders", params=params, signed=True)


class MexcSpotWebSDK:
    """
    Spot WEB endpoint base:
      https://www.mexc.com/api/platform/spot
    """

    PRIVATE_BASE = "https://www.mexc.com/api/platform/spot"

    def __init__(
        self,
        authorization: str,
        *,
        u_id: Optional[str] = None,
        debug: bool = False,
        timeout_sec: int = 30,
        cookie: Optional[str] = None,
    ):
        self.authorization = authorization
        self.u_id = u_id
        self.debug = debug
        self.timeout_sec = timeout_sec
        self.cookie = cookie

        self.session = requests.Session()
        self.session.headers.update({
            "accept": "*/*",
            "content-type": "application/json; charset=utf-8",
            "origin": "https://www.mexc.com",
            "user-agent": "Mozilla/5.0",
            "authorization": authorization,
            "language": "en-US",
        })

    def _dbg(self, *args):
        if self.debug:
            print(*args)

    def _private_headers(self, ts: str, body: str) -> Dict[str, str]:
        sign = sign_web(self.authorization, ts, body)
        headers = {
            "authorization": self.authorization,
            "x-mxc-nonce": ts,
            "x-mxc-sign": sign,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.5",
            "content-type": "application/json; charset=utf-8",
            "origin": "https://www.mexc.com",
            "referer": "https://www.mexc.com/",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            "content-length": str(len(body.encode("utf-8"))),
        }
        if self.u_id:
            headers["u-id"] = self.u_id
            headers["u_id"] = self.u_id
        if self.cookie:
            headers["cookie"] = self.cookie
        elif self.u_id:
            headers["cookie"] = f"u_id={self.u_id}"
        return headers

    def _post_private(self, path: str, payload: Any, ts: Optional[str] = None) -> Json:
        nonce = ts or _now_ms_str()
        body = _json_dumps(payload)
        headers = self._private_headers(nonce, body)

        if self.debug:
            self._dbg("\n===== REQUEST =====")
            self._dbg("POST", self.PRIVATE_BASE + path)
            self._dbg("HEADERS:", headers)
            self._dbg("BODY:", body)

        resp = self.session.post(
            self.PRIVATE_BASE + path,
            headers=headers,
            data=body,
            timeout=self.timeout_sec,
        )

        if self.debug:
            self._dbg("STATUS:", resp.status_code)
            self._dbg("RESPONSE:", resp.text)

        resp.raise_for_status()
        j = resp.json()
        if isinstance(j, dict) and str(j.get("code")) not in ("0", "200"):
            raise MexcSpotError(j.get("code"), j.get("msg", "request failed"), j)
        return j

    def create_order(
        self,
        *,
        price: Optional[Union[int, float, str, Decimal]] = None,
        quantity: Optional[Union[int, float, str, Decimal]] = None,
        amount: Optional[Union[int, float, str, Decimal]] = None,
        orderType: str,
        currencyId: str,
        tradeType: str,
        marketCurrencyId: str,
    ) -> Json:
        payload = OrderedDict([
            ("orderType", str(orderType)),
            ("currencyId", str(currencyId)),
            ("tradeType", str(tradeType)),
            ("marketCurrencyId", str(marketCurrencyId)),
        ])
        if price is not None:
            payload["price"] = str(price)
        if quantity is not None:
            payload["quantity"] = str(quantity)
        if amount is not None:
            payload["amount"] = str(amount)
        ts = _now_ms_str()
        payload["chash"] = generate_chash(payload)
        payload["ts"] = ts
        return self._post_private("/order/place", payload, ts=ts)

    def _fetch_symbols_map(self) -> Dict[str, Tuple[str, str]]:
        url = "https://www.mexc.com/api/platform/spot/market-v2/web/symbolsV2"
        resp = requests.get(url, timeout=self.timeout_sec)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        symbols = data.get("symbols", {}) if isinstance(data, dict) else {}

        symbol_map: Dict[str, Tuple[str, str]] = {}
        if isinstance(symbols, dict):
            for quote, arr in symbols.items():
                if not isinstance(arr, list):
                    continue
                quote_str = str(quote).upper()
                for item in arr:
                    if not isinstance(item, dict):
                        continue
                    base = str(item.get("vn") or item.get("fn") or "").upper()
                    if not base:
                        continue
                    cd = str(item.get("cd") or "")
                    mcd = str(item.get("mcd") or "")
                    if not cd or not mcd:
                        continue
                    symbol_map[f"{base}{quote_str}"] = (cd, mcd)
        return symbol_map

    def get_symbol_ids(self, symbol: str) -> Tuple[str, str]:
        if not hasattr(self, "_symbols_cache"):
            self._symbols_cache = {}
            self._symbols_cache_ts = 0.0
        now = time.time()
        if self._symbols_cache and (now - self._symbols_cache_ts) < 300:
            cached = self._symbols_cache.get(symbol)
            if cached:
                return cached

        symbol_map = self._fetch_symbols_map()
        self._symbols_cache = symbol_map
        self._symbols_cache_ts = now
        if symbol in symbol_map:
            return symbol_map[symbol]
        raise MexcSpotError(-1, f"Symbol not found in symbolsV2: {symbol}")

    def create_order_by_symbol(
        self,
        *,
        symbol: str,
        tradeType: str,
        orderType: str,
        price: Optional[Union[int, float, str, Decimal]] = None,
        quantity: Optional[Union[int, float, str, Decimal]] = None,
        amount: Optional[Union[int, float, str, Decimal]] = None,
    ) -> Json:
        cd, mcd = self.get_symbol_ids(symbol.upper())
        return self.create_order(
            price=price,
            quantity=quantity,
            amount=amount,
            orderType=orderType,
            currencyId=cd,
            tradeType=tradeType,
            marketCurrencyId=mcd,
        )
