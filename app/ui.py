from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
import requests
from decimal import Decimal, ROUND_DOWN
from tkinter import ttk
from typing import Dict, Optional

from app.client import MEXCClient
from app.default_mode import DefaultRunner
from app.market import compute_base_qty_from_quote, fetch_top_level_depth, parse_market_spec
from app.models import ApiCredentials, AppConfig, RiskConfig
from app.simulator import run_simulator
from app.utils import log_event

INLINE_A_KEY = ""
INLINE_A_SECRET = ""
INLINE_B_KEY = ""
INLINE_B_SECRET = ""
SETTINGS_FILE = "settings.json"


class GuiLogHandler(logging.Handler):
    def __init__(self, target_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.target_queue = target_queue

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        self.target_queue.put(msg)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("MEXC Spot Toolkit")
        self.geometry("980x680")

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self._cancel_all_fn = None
        self._cancel_lock = threading.Lock()
        self._entry_menu: Optional[tk.Menu] = None
        self._log_menu: Optional[tk.Menu] = None

        self._build_scroll_container()
        self._build_form()
        self._build_log_panel()
        self._configure_logging()

        self.bind("<F2>", lambda _: self.start())

    def _build_scroll_container(self) -> None:
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(container, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(container, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.body = ttk.Frame(self._canvas)
        self._canvas_window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")

        def on_frame_configure(_: tk.Event) -> None:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))

        def on_canvas_configure(event: tk.Event) -> None:
            self._canvas.itemconfigure(self._canvas_window, width=event.width)

        self.body.bind("<Configure>", on_frame_configure)
        self._canvas.bind("<Configure>", on_canvas_configure)

    def _configure_logging(self) -> None:
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        handler = GuiLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.handlers = [handler]
        self.after(150, self._poll_logs)

    def _poll_logs(self) -> None:
        while not self.log_queue.empty():
            message = self.log_queue.get_nowait()
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.configure(state=tk.DISABLED)
            self.log_text.see(tk.END)
        self.after(150, self._poll_logs)

    def _build_log_panel(self) -> None:
        logs = ttk.LabelFrame(self.body, text="Логи / Debug")
        logs.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)
        self.log_text = tk.Text(logs, wrap=tk.WORD, state=tk.DISABLED)
        scrollbar = ttk.Scrollbar(logs, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.bind("<Button-3>", self._show_log_menu, add="+")
        self.log_text.bind("<Shift-F10>", self._show_log_menu, add="+")

    def _init_entry_menu(self) -> None:
        if self._entry_menu is not None:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Копировать", command=self._menu_copy)
        menu.add_command(label="Вставить", command=self._menu_paste)
        self._entry_menu = menu

    def _show_entry_menu(self, event: tk.Event) -> str:
        if not isinstance(event.widget, (tk.Entry, ttk.Entry)):
            return ""
        self._menu_target = event.widget
        self._init_entry_menu()
        if self._entry_menu:
            self._entry_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _menu_copy(self) -> None:
        widget = getattr(self, "_menu_target", None)
        if isinstance(widget, (tk.Entry, ttk.Entry)):
            widget.event_generate("<<Copy>>")

    def _menu_paste(self) -> None:
        widget = getattr(self, "_menu_target", None)
        if isinstance(widget, (tk.Entry, ttk.Entry)):
            widget.event_generate("<<Paste>>")

    def _init_log_menu(self) -> None:
        if self._log_menu is not None:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Копировать", command=self._log_copy)
        self._log_menu = menu

    def _show_log_menu(self, event: tk.Event) -> str:
        if not isinstance(event.widget, tk.Text):
            return ""
        self._log_target = event.widget
        self._init_log_menu()
        if self._log_menu:
            self._log_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _log_copy(self) -> None:
        widget = getattr(self, "_log_target", None)
        if not isinstance(widget, tk.Text):
            return
        try:
            text = widget.get(tk.SEL_FIRST, tk.SEL_LAST)
        except tk.TclError:
            return
        self.clipboard_clear()
        self.clipboard_append(text)

    def _build_form(self) -> None:
        form = ttk.LabelFrame(self.body, text="Настройки")
        form.pack(fill=tk.X, padx=12, pady=8)

        def bind_entry_shortcuts() -> None:
            for seq in ("<Button-3>", "<Shift-F10>"):
                self.bind_class("Entry", seq, self._show_entry_menu, add="+")
                self.bind_class("TEntry", seq, self._show_entry_menu, add="+")

        bind_entry_shortcuts()

        self.fields: Dict[str, tk.Entry] = {}
        defaults = {
            "symbol": os.getenv("MEXC_SYMBOL", "EXAMPLEUSDT"),
            "quote_amount": os.getenv("MEXC_QUOTE_AMOUNT", "1.2"),
            "fee_rate": os.getenv("MEXC_FEE_RATE", "0.001"),
            "max_cycles": os.getenv("MEXC_MAX_CYCLES", "50"),
            "timeout_s": os.getenv("MEXC_TIMEOUT_S", "10"),
            "account_b_quote": os.getenv("MEXC_B_QUOTE_START", "20"),
            "base_url": os.getenv("MEXC_BASE_URL", "https://api.mexc.com"),
            "keys_file": os.getenv("MEXC_KEYS_FILE", "mexc_keys.txt"),
        }

        rows = ttk.Frame(form)
        rows.pack(fill=tk.X, padx=8, pady=6)

        def add_field(parent: tk.Widget, key: str, label: str, default: str, width: int = 22) -> None:
            row = ttk.Frame(parent)
            row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=label, width=width).pack(side=tk.LEFT)
            entry = ttk.Entry(row)
            entry.insert(0, default)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            entry.bind("<KeyRelease>", lambda _: self._schedule_save_settings())
            self.fields[key] = entry

        symbol_row = ttk.Frame(rows)
        symbol_row.pack(fill=tk.X, pady=3)
        ttk.Label(symbol_row, text="Symbol", width=22).pack(side=tk.LEFT)
        self.symbol_var = tk.StringVar(value=defaults["symbol"])
        symbol_entry = ttk.Entry(symbol_row, textvariable=self.symbol_var)
        symbol_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        symbol_entry.bind("<KeyRelease>", lambda _: self._filter_symbols())
        symbol_entry.bind("<FocusOut>", lambda _: self._hide_symbol_popup())
        symbol_entry.bind("<Escape>", lambda _: self._hide_symbol_popup())
        self.fields["symbol"] = symbol_entry

        self._init_symbol_popup(symbol_entry)

        self._symbols: list[str] = []
        self._symbol_map: Dict[str, dict] = {}
        self._load_symbols_async()

        add_field(rows, "quote_amount", "USDT сумма сделки", defaults["quote_amount"])
        add_field(rows, "fee_rate", "Комиссия (например 0.001)", defaults["fee_rate"])
        add_field(rows, "max_cycles", "Макс. циклов", defaults["max_cycles"])
        add_field(rows, "timeout_s", "HTTP timeout (сек)", defaults["timeout_s"])
        add_field(rows, "account_b_quote", "Баланс B (USDT) (сим)", defaults["account_b_quote"])
        add_field(rows, "base_url", "API base URL", defaults["base_url"])
        add_field(rows, "keys_file", "Файл с ключами", defaults["keys_file"])

        mode_frame = ttk.Frame(form)
        mode_frame.pack(fill=tk.X, padx=8, pady=6)
        ttk.Label(mode_frame, text="Режим", width=22).pack(side=tk.LEFT)
        self.mode_var = tk.StringVar(value=os.getenv("MEXC_MODE", "SIMULATOR"))
        self.mode_var.trace_add("write", lambda *_: self._schedule_save_settings())
        ttk.Combobox(mode_frame, textvariable=self.mode_var, values=["SIMULATOR", "DEFAULT"], state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        risk = ttk.LabelFrame(form, text="Риск-контроль / микроструктура")
        risk.pack(fill=tk.X, padx=8, pady=6)

        self.risk_fields: Dict[str, tk.Entry] = {}
        risk_defaults = {
            "min_spread_ticks": os.getenv("MEXC_MIN_SPREAD_TICKS", "2"),
            "min_top_quote": os.getenv("MEXC_MIN_TOP_QUOTE", "25"),
            "max_participation_of_top": os.getenv("MEXC_MAX_PART_OF_TOP", "0.15"),
            "max_slippage_bps": os.getenv("MEXC_MAX_SLIPPAGE_BPS", "20"),
            "spread_offset_pct": os.getenv("MEXC_SPREAD_OFFSET_PCT", "0"),
            "order_timeout_s": os.getenv("MEXC_ORDER_TIMEOUT_S", "8"),
            "max_retries": os.getenv("MEXC_MAX_RETRIES", "2"),
            "backoff_base_ms": os.getenv("MEXC_BACKOFF_MS", "250"),
        }

        def add_risk_field(key: str, label: str) -> None:
            row = ttk.Frame(risk)
            row.pack(fill=tk.X, padx=6, pady=2)
            ttk.Label(row, text=label, width=30).pack(side=tk.LEFT)
            e = ttk.Entry(row, width=20)
            e.insert(0, risk_defaults[key])
            e.pack(side=tk.LEFT, fill=tk.X, expand=True)
            e.bind("<KeyRelease>", lambda _: self._schedule_save_settings())
            self.risk_fields[key] = e

        add_risk_field("min_spread_ticks", "Мин. спред (в тиках)")
        add_risk_field("min_top_quote", "Мин. quote на лучшем уровне")
        add_risk_field("max_participation_of_top", "Макс. доля от top-level (0..1)")
        add_risk_field("max_slippage_bps", "Макс. slippage (bps)")
        add_risk_field("spread_offset_pct", "Отступ от спреда (%)")
        add_risk_field("order_timeout_s", "Таймаут ордера (сек)")
        add_risk_field("max_retries", "HTTP retries")
        add_risk_field("backoff_base_ms", "Backoff base (мс)")

        self.turbo_var = tk.BooleanVar(value=os.getenv("MEXC_TURBO", "false").lower() == "true")
        self.turbo_var.trace_add("write", lambda *_: self._schedule_save_settings())
        ttk.Checkbutton(risk, text="Turbo (агрессивный режим)", variable=self.turbo_var).pack(
            anchor="w", padx=6, pady=2
        )

        self.post_only_var = tk.BooleanVar(value=os.getenv("MEXC_POST_ONLY", "true").lower() == "true")
        self.post_only_var.trace_add("write", lambda *_: self._schedule_save_settings())
        ttk.Checkbutton(risk, text="Post-only (LIMIT_MAKER)", variable=self.post_only_var).pack(anchor="w", padx=6, pady=2)

        api_frame = ttk.LabelFrame(form, text="API ключи (A/B)")
        api_frame.pack(fill=tk.X, padx=8, pady=6)
        self.api_fields: Dict[str, tk.Entry] = {}

        for key, label, env in [
            ("a_key", "A Key", "MEXC_A_KEY"),
            ("a_secret", "A Secret", "MEXC_A_SECRET"),
            ("a_u_id", "A U_ID Token", "MEXC_A_U_ID"),
            ("b_key", "B Key", "MEXC_B_KEY"),
            ("b_secret", "B Secret", "MEXC_B_SECRET"),
            ("b_u_id", "B U_ID Token", "MEXC_B_U_ID"),
        ]:
            row = ttk.Frame(api_frame)
            row.pack(fill=tk.X, padx=8, pady=3)
            ttk.Label(row, text=label, width=22).pack(side=tk.LEFT)
            entry = ttk.Entry(row, show="*" if "secret" in key or "u_id" in key else "")
            entry.insert(0, os.getenv(env, ""))
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            entry.bind("<KeyRelease>", lambda _: self._schedule_save_settings())
            self.api_fields[key] = entry

        controls = ttk.Frame(form)
        controls.pack(fill=tk.X, padx=8, pady=8)

        ttk.Button(controls, text="Загрузить ключи", command=self._load_keys_from_file).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="Авто-настройка", command=self._auto_tune).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="Старт (F2)", command=self.start).pack(side=tk.LEFT, padx=6)
        ttk.Button(controls, text="Стоп", command=self.stop).pack(side=tk.LEFT)

        self._apply_settings()

    def _load_symbols_async(self) -> None:
        def worker() -> None:
            try:
                symbols, mapping = self._fetch_symbols()
            except Exception as exc:  # noqa: BLE001
                log_event("error", {"message": f"Failed to load symbols: {exc}"})
                return
            self.after(0, lambda: self._apply_symbol_list(symbols, mapping))

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_symbols(self) -> tuple[list[str], Dict[str, dict]]:
        url = "https://www.mexc.com/api/platform/spot/market-v2/web/symbolsV2"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        symbols = data.get("symbols", {}) if isinstance(data, dict) else {}

        symbol_map: Dict[str, dict] = {}
        symbol_list: list[str] = []

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
                    full = f"{base}{quote_str}"
                    symbol_map[full] = item
                    symbol_list.append(full)
        elif isinstance(symbols, list):
            for item in symbols:
                if not isinstance(item, dict):
                    continue
                base = str(item.get("vn") or item.get("fn") or "").upper()
                quote = str(item.get("qcc") or "USDT").upper()
                if not base:
                    continue
                full = f"{base}{quote}"
                symbol_map[full] = item
                symbol_list.append(full)

        unique = sorted(set(symbol_list))
        return unique, symbol_map

    def _apply_symbol_list(self, symbols: list[str], mapping: Dict[str, dict]) -> None:
        self._symbols = symbols
        self._symbol_map = mapping
        self._filter_symbols()

    def _filter_symbols(self) -> None:
        entry = self.fields.get("symbol")
        if not isinstance(entry, ttk.Entry):
            return
        term = entry.get().strip().upper()
        if not term:
            self._show_symbol_popup(entry, self._symbols)
            self._schedule_save_settings()
            return
        filtered = [s for s in self._symbols if term in s]
        self._show_symbol_popup(entry, filtered or self._symbols)
        self._schedule_save_settings()

    def _init_symbol_popup(self, entry: ttk.Entry) -> None:
        self._symbol_popup = tk.Toplevel(self)
        self._symbol_popup.withdraw()
        self._symbol_popup.overrideredirect(True)
        self._symbol_popup.attributes("-topmost", True)

        self._symbol_listbox = tk.Listbox(self._symbol_popup, height=8)
        self._symbol_listbox.pack(fill=tk.BOTH, expand=True)
        self._symbol_listbox.bind("<ButtonRelease-1>", lambda _: self._select_symbol(entry))
        self._symbol_listbox.bind("<Return>", lambda _: self._select_symbol(entry))
        entry.bind("<Down>", lambda _: self._focus_symbol_list())

    def _focus_symbol_list(self) -> None:
        if getattr(self, "_symbol_popup", None) is None:
            return
        if self._symbol_popup.state() == "withdrawn":
            return
        self._symbol_listbox.focus_set()

    def _show_symbol_popup(self, entry: ttk.Entry, items: list[str]) -> None:
        if not items:
            self._hide_symbol_popup()
            return
        self._symbol_listbox.delete(0, tk.END)
        for item in items[:300]:
            self._symbol_listbox.insert(tk.END, item)

        x = entry.winfo_rootx()
        y = entry.winfo_rooty() + entry.winfo_height()
        width = entry.winfo_width()
        self._symbol_popup.geometry(f"{width}x180+{x}+{y}")
        self._symbol_popup.deiconify()

    def _hide_symbol_popup(self) -> None:
        popup = getattr(self, "_symbol_popup", None)
        if popup:
            popup.withdraw()

    def _select_symbol(self, entry: ttk.Entry) -> None:
        sel = self._symbol_listbox.curselection()
        if not sel:
            return
        value = self._symbol_listbox.get(sel[0])
        entry.delete(0, tk.END)
        entry.insert(0, value)
        self._hide_symbol_popup()
        self._schedule_save_settings()

    def _load_keys_from_file(self) -> None:
        raw_path = self.fields["keys_file"].get().strip()
        if not raw_path:
            log_event("error", {"message": "Keys file path is empty"})
            return

        candidates = [raw_path]
        if not os.path.isabs(raw_path):
            candidates.append(os.path.join(os.getcwd(), raw_path))
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), raw_path))
            script_path = os.path.abspath(sys.argv[0]) if sys.argv else ""
            if script_path:
                candidates.append(os.path.join(os.path.dirname(script_path), raw_path))

        path = next((c for c in candidates if os.path.exists(c)), "")
        if not path:
            log_event("error", {"message": f"Keys file not found: {raw_path}"})
            return

        try:
            with open(path, "r", encoding="utf-8") as h:
                lines = [ln.strip() for ln in h if ln.strip() and not ln.strip().startswith("#")]
        except OSError as exc:
            log_event("error", {"message": f"Failed to read keys file: {exc}"})
            return

        values: Dict[str, str] = {}
        for ln in lines:
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            values[k.strip()] = v.strip()

        mapping = {
            "A_KEY": "a_key",
            "A_SECRET": "a_secret",
            "A_U_ID": "a_u_id",
            "B_KEY": "b_key",
            "B_SECRET": "b_secret",
            "B_U_ID": "b_u_id",
        }
        for env_key, field_key in mapping.items():
            if env_key in values:
                entry = self.api_fields[field_key]
                entry.delete(0, tk.END)
                entry.insert(0, values[env_key])

        log_event("ui", {"message": "Loaded API keys from file", "path": path})
        self._schedule_save_settings()

    def _auto_tune(self) -> None:
        threading.Thread(target=self._auto_tune_run, daemon=True).start()

    def _auto_tune_run(self) -> None:
        try:
            symbol = self.fields["symbol"].get().strip().upper()
            fee_rate = Decimal(self.fields["fee_rate"].get().strip() or "0")
            max_participation = Decimal(self.risk_fields["max_participation_of_top"].get().strip() or "0")
            if max_participation <= 0:
                max_participation = Decimal("0.15")

            client = MEXCClient(ApiCredentials("", ""), timeout_s=10, base_url=self.fields["base_url"].get().strip())
            market = parse_market_spec(client.get_exchange_info(symbol))
            (bbp, bbq), (bap, baq) = fetch_top_level_depth(client, symbol)

            top_qty_min = min(bbq, baq)
            min_trade_quote = market.min_notional if market.min_notional > 0 else Decimal("1")
            desired_quote = max(Decimal("1.2"), min_trade_quote)
            desired_quote = desired_quote.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            desired_qty = compute_base_qty_from_quote(desired_quote, bap, fee_rate, market.lot_size)
            if desired_qty <= 0:
                raise RuntimeError("Computed desired_qty <= 0")

            if top_qty_min <= 0:
                raise RuntimeError("Top of book qty is zero")

            required_participation = (desired_qty / top_qty_min).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            if required_participation > 1:
                max_participation = Decimal("1.0")
            else:
                max_participation = required_participation

            top_quote_min = min(bbp * bbq, bap * baq)
            min_top_quote = top_quote_min.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

            def apply_updates() -> None:
                self.fields["quote_amount"].delete(0, tk.END)
                self.fields["quote_amount"].insert(0, str(desired_quote))
                self.risk_fields["min_top_quote"].delete(0, tk.END)
                self.risk_fields["min_top_quote"].insert(0, str(min_top_quote))
                self.risk_fields["max_participation_of_top"].delete(0, tk.END)
                self.risk_fields["max_participation_of_top"].insert(0, str(max_participation))
                log_event("ui", {
                    "message": "Auto-tuned settings",
                    "symbol": symbol,
                    "quote_amount": str(desired_quote),
                    "min_top_quote": str(min_top_quote),
                    "min_trade_quote": str(min_trade_quote),
                    "desired_qty": str(desired_qty),
                    "max_participation_of_top": str(max_participation),
                    "top_qty_min": str(top_qty_min),
                })
                self._save_settings()

            self.after(0, apply_updates)
        except Exception as exc:  # noqa: BLE001
            log_event("error", {"message": f"Auto-tune failed: {exc}"})

    def _settings_path(self) -> str:
        return os.path.join(os.getcwd(), SETTINGS_FILE)

    def _load_settings(self) -> dict:
        path = self._settings_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as h:
                return json.load(h)
        except Exception as exc:  # noqa: BLE001
            log_event("error", {"message": f"Failed to load settings: {exc}"})
            return {}

    def _apply_settings(self) -> None:
        data = self._load_settings()
        if not data:
            return

        for key, entry in self.fields.items():
            if key in data:
                entry.delete(0, tk.END)
                entry.insert(0, str(data[key]))

        for key, entry in self.risk_fields.items():
            if key in data:
                entry.delete(0, tk.END)
                entry.insert(0, str(data[key]))

        if "mode" in data:
            self.mode_var.set(str(data["mode"]))
        if "post_only" in data:
            self.post_only_var.set(bool(data["post_only"]))
        if "turbo" in data:
            self.turbo_var.set(bool(data["turbo"]))

        for key, entry in self.api_fields.items():
            if key in data:
                entry.delete(0, tk.END)
                entry.insert(0, str(data[key]))

    def _collect_settings(self) -> dict:
        data = {k: v.get().strip() for k, v in self.fields.items()}
        data.update({k: v.get().strip() for k, v in self.risk_fields.items()})
        data.update({k: v.get().strip() for k, v in self.api_fields.items()})
        data["mode"] = self.mode_var.get().strip()
        data["post_only"] = bool(self.post_only_var.get())
        data["turbo"] = bool(self.turbo_var.get())
        return data

    def _save_settings(self) -> None:
        path = self._settings_path()
        data = self._collect_settings()
        try:
            with open(path, "w", encoding="utf-8") as h:
                json.dump(data, h, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            log_event("error", {"message": f"Failed to save settings: {exc}"})

    def _schedule_save_settings(self) -> None:
        if hasattr(self, "_save_after_id") and self._save_after_id:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(400, self._save_settings)

    def _build_config(self) -> AppConfig:
        def _clean_number(value: str) -> str:
            return value.strip().replace(",", ".")

        def dec_from(entry: tk.Entry) -> Decimal:
            return Decimal(_clean_number(entry.get()))

        def int_from(entry: tk.Entry) -> int:
            return int(entry.get().strip())

        symbol = self.fields["symbol"].get().strip().upper()
        quote_amount = dec_from(self.fields["quote_amount"])
        fee_rate = dec_from(self.fields["fee_rate"])
        max_cycles = int_from(self.fields["max_cycles"])
        timeout_s = int_from(self.fields["timeout_s"])
        account_b_quote = dec_from(self.fields["account_b_quote"])
        base_url = self.fields["base_url"].get().strip()
        keys_file = self.fields["keys_file"].get().strip()
        mode = self.mode_var.get().strip().upper()

        account_a = ApiCredentials(
            api_key=INLINE_A_KEY or self.api_fields["a_key"].get().strip(),
            api_secret=INLINE_A_SECRET or self.api_fields["a_secret"].get().strip(),
        )
        account_b = ApiCredentials(
            api_key=INLINE_B_KEY or self.api_fields["b_key"].get().strip(),
            api_secret=INLINE_B_SECRET or self.api_fields["b_secret"].get().strip(),
        )
        u_id_a = self.api_fields.get("a_u_id").get().strip() if "a_u_id" in self.api_fields else ""
        u_id_b = self.api_fields.get("b_u_id").get().strip() if "b_u_id" in self.api_fields else ""
        log_event("keys_loaded", {
            "a_key_loaded": bool(account_a.api_key),
            "a_secret_loaded": bool(account_a.api_secret),
            "b_key_loaded": bool(account_b.api_key),
            "b_secret_loaded": bool(account_b.api_secret),
            "u_id_a_loaded": bool(u_id_a),
            "u_id_b_loaded": bool(u_id_b),
        })

        risk = RiskConfig(
            min_spread_ticks=int(self.risk_fields["min_spread_ticks"].get().strip()),
            min_top_quote=Decimal(self.risk_fields["min_top_quote"].get().strip()),
            max_participation_of_top=Decimal(self.risk_fields["max_participation_of_top"].get().strip()),
            max_slippage_bps=int(self.risk_fields["max_slippage_bps"].get().strip()),
            spread_offset_pct=Decimal(self.risk_fields["spread_offset_pct"].get().strip()),
            order_timeout_s=int(self.risk_fields["order_timeout_s"].get().strip()),
            use_post_only=bool(self.post_only_var.get()),
            max_retries=int(self.risk_fields["max_retries"].get().strip()),
            backoff_base_ms=int(self.risk_fields["backoff_base_ms"].get().strip()),
            turbo_mode=bool(self.turbo_var.get()),
        )

        cfg = AppConfig(
            symbol=symbol,
            fee_rate=fee_rate,
            quote_amount=quote_amount,
            max_cycles=max_cycles,
            timeout_s=timeout_s,
            base_url=base_url,
            u_id_a=u_id_a,
            u_id_b=u_id_b,
            account_a=account_a,
            account_b=account_b,
            account_b_quote_start=account_b_quote,
            keys_file=keys_file,
            mode=mode,
            risk=risk,
        )
        log_event("ui_config", {
            "symbol": cfg.symbol,
            "quote_amount": str(cfg.quote_amount),
            "fee_rate": str(cfg.fee_rate),
            "max_cycles": cfg.max_cycles,
        })
        return cfg

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            log_event("ui", {"message": "Already running"})
            return
        cfg = self._build_config()
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        self.stop_event.clear()
        log_event("ui", {"message": "Starting", "mode": cfg.mode, "symbol": cfg.symbol})
        self.worker = threading.Thread(target=self._run_worker, args=(cfg,), daemon=True)
        self.worker.start()

    def _run_worker(self, cfg: AppConfig) -> None:
        self._set_cancel_all(None)
        try:
            if cfg.mode == "SIMULATOR":
                run_simulator(cfg, self.stop_event)
            elif cfg.mode == "DEFAULT":
                runner = DefaultRunner(cfg, self.stop_event)
                self._set_cancel_all(runner.cancel_all)
                runner.run()
            else:
                raise RuntimeError(f"Unknown mode: {cfg.mode}")
        except Exception as exc:  # noqa: BLE001
            log_event("error", {"message": str(exc)})
        finally:
            self._set_cancel_all(None)

    def _set_cancel_all(self, fn) -> None:
        with self._cancel_lock:
            self._cancel_all_fn = fn

    def stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            log_event("ui", {"message": "Stopping"})
            with self._cancel_lock:
                cancel_fn = self._cancel_all_fn
            if cancel_fn:
                threading.Thread(target=cancel_fn, daemon=True).start()
