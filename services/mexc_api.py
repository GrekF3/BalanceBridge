import time, hmac, hashlib, urllib.parse, threading, random, requests

class MexcFuturesOpenApi:
    BASE = "https://contract.mexc.com"  # Open-API base

    def __init__(self, api_key: str, api_secret: str, recv_window: int = 5000, timeout: int = 5):
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.recv_window = recv_window
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json"})

    def _ts(self) -> str:
        return str(int(time.time() * 1000))

    def _sign(self, ts: str, param_str: str) -> str:
        msg = (self.api_key + ts + param_str).encode("utf-8")
        return hmac.new(self.api_secret, msg, hashlib.sha256).hexdigest()

    def _get(self, path: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        # GET: сортировка по ключам + url-encode значений
        qs = "&".join(
            f"{k}={urllib.parse.quote(str(params[k]), safe='')}"
            for k in sorted(params.keys())
        )
        ts = self._ts()
        sig = self._sign(ts, qs)

        headers = {
            "ApiKey": self.api_key,
            "Request-Time": ts,
            "Signature": sig,
            "Recv-Window": str(self.recv_window),
        }

        r = self.s.get(self.BASE + path, params=params, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_assets(self) -> dict:
        return self._get("/api/v1/private/account/assets")


class BalanceCache:
    def __init__(self, openapi: MexcFuturesOpenApi, refresh_sec: float = 2.0):
        self.openapi = openapi
        self.refresh_sec = refresh_sec
        self.lock = threading.Lock()
        self.usdt = None  # dict with availableOpen/equity/frozenBalance...
        self._stop = False
        self.t = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.t.start()

    def stop(self):
        self._stop = True

    def _loop(self):
        # небольшой джиттер, чтобы несколько фолловеров не били одновременно
        time.sleep(random.uniform(0.05, 0.35))
        while not self._stop:
            try:
                data = self.openapi.get_assets()
                if data.get("success") and isinstance(data.get("data"), list):
                    usdt = next((x for x in data["data"] if x.get("currency") == "USDT"), None)
                    with self.lock:
                        self.usdt = usdt
            except Exception:
                pass
            time.sleep(self.refresh_sec)

    def get_available_open(self) -> float | None:
        with self.lock:
            if not self.usdt:
                return None
            return float(self.usdt.get("availableOpen", 0.0))
