from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

from app.client import MEXCClient
from app.utils import log_event

try:
    import websocket  # type: ignore
except Exception as exc:  # noqa: BLE001
    websocket = None
    _WS_IMPORT_ERROR = exc


TERMINAL_ORDER_STATUSES = {
    "FILLED",
    "CANCELED",
    "CANCELLED",
    "PARTIALLY_CANCELED",
    "REJECTED",
    "EXPIRED",
}


class OrderTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._orders: Dict[str, Dict[str, Any]] = {}

    def handle_event(self, message: Dict[str, Any]) -> None:
        event, payload = _extract_event(message)
        if event != "executionReport":
            return
        order_id = str(payload.get("i") or payload.get("orderId") or "")
        if not order_id:
            return
        status = payload.get("X") or payload.get("status") or payload.get("orderStatus") or ""
        status = str(status).upper()
        client_id = payload.get("c") or payload.get("clientOrderId") or payload.get("C")
        info = {
            "orderId": order_id,
            "status": status,
            "clientOrderId": client_id,
            "raw": payload,
        }
        with self._cond:
            self._orders[order_id] = info
            self._cond.notify_all()

    def wait_for_terminal(self, order_id: str, timeout_s: int) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout_s
        with self._cond:
            while True:
                info = self._orders.get(order_id)
                if info and info.get("status") in TERMINAL_ORDER_STATUSES:
                    return info
                remaining = deadline - time.time()
                if remaining <= 0:
                    return info
                self._cond.wait(remaining)

    def get_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._orders.get(order_id)


class UserStream:
    def __init__(
        self,
        client: MEXCClient,
        on_event: Callable[[Dict[str, Any]], None],
        stop_event: threading.Event,
        keepalive_s: int = 30 * 60,
    ) -> None:
        if websocket is None:
            raise RuntimeError(f"websocket-client is required: {_WS_IMPORT_ERROR}")
        self.client = client
        self.on_event = on_event
        self.stop_event = stop_event
        self.keepalive_s = keepalive_s
        self.listen_key: Optional[str] = None
        self._ws_app: Optional[websocket.WebSocketApp] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._keepalive_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.listen_key = self.client.create_listen_key()
        url = f"wss://wbs.mexc.com/ws?listenKey={self.listen_key}"

        def on_message(_: websocket.WebSocketApp, message: str) -> None:
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                log_event("ws_message_parse_error", {"message": message})
                return
            try:
                self.on_event(payload)
            except Exception as exc:  # noqa: BLE001
                log_event("ws_event_error", {"error": str(exc)})

        def on_error(_: websocket.WebSocketApp, error: Any) -> None:
            log_event("ws_error", {"error": str(error)})

        def on_close(_: websocket.WebSocketApp, status_code: Any, msg: Any) -> None:
            log_event("ws_close", {"status": status_code, "message": str(msg)})

        self._ws_app = websocket.WebSocketApp(
            url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        self._ws_thread = threading.Thread(target=self._ws_app.run_forever, daemon=True)
        self._ws_thread.start()

        self._keepalive_thread = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive_thread.start()

        log_event("ws_started", {"listenKey": self.listen_key})

    def _keepalive_loop(self) -> None:
        while not self.stop_event.wait(self.keepalive_s):
            if not self.listen_key:
                return
            try:
                self.client.keepalive_listen_key(self.listen_key)
                log_event("ws_keepalive", {"listenKey": self.listen_key})
            except Exception as exc:  # noqa: BLE001
                log_event("ws_keepalive_error", {"error": str(exc)})

    def stop(self) -> None:
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception as exc:  # noqa: BLE001
                log_event("ws_close_error", {"error": str(exc)})
        if self.listen_key:
            try:
                self.client.close_listen_key(self.listen_key)
            except Exception as exc:  # noqa: BLE001
                log_event("ws_listenkey_close_error", {"error": str(exc)})


def _extract_event(message: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    payload = message
    if "data" in message and isinstance(message["data"], dict):
        payload = message["data"]
    event = payload.get("e") or payload.get("eventType") or payload.get("type") or ""
    return str(event), payload
