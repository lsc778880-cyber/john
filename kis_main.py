# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import atexit
import csv
import json
import math
import os
import queue
import re
import signal
import sys
import time
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

os.environ.setdefault("PYTHONUTF8", "1")
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import PyQt5

    _qt_plugin_root = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
    if os.path.exists(os.path.join(_qt_plugin_root, "platforms", "qwindows.dll")):
        os.environ.setdefault("QT_PLUGIN_PATH", _qt_plugin_root)
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", os.path.join(_qt_plugin_root, "platforms"))
except Exception:
    pass

from PyQt5.QtCore import QObject, QLockFile, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QListWidget, QVBoxLayout, QMessageBox

from config import Config
from strategy import Strategy, Candle, price_in_closed_band
from core.kis_order_core import KisOrderCore
from core.events import ExecutionEvent
from core.history_seed_loader import HistorySeedLoader
from core.sma_cross import validate_sma10s_fire_gate
from infra.kis.kis_client import KisClient
from ui.dashboard_qt import LiveDashboardQt


_GLOBAL_CONTROLLER = None
_INSTANCE_LOCK = None
FIXED_LIVE_ACCOUNT_NO = ""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def reversal_candidate_display_stage(
    sig_state: dict[str, Any] | None,
    side: str,
    *,
    now: datetime | None = None,
) -> tuple[str | None, int | None]:
    """Return a live wait/watch label from the candidate timestamps.

    The strategy snapshot already carries a stage/countdown, but the detail view
    can be rendered between two signal evaluations.  Deriving the display value
    from the candidate window keeps the visible countdown continuous without
    changing the trading state.
    """
    state = sig_state if isinstance(sig_state, dict) else {}
    side_u = str(side or "").strip().upper()
    if side_u not in ("LONG", "SHORT"):
        return None, None

    stage = str(state.get(f"reversal_{side_u.lower()}_candidate_stage") or "").strip() or None
    countdown_raw = state.get(f"reversal_{side_u.lower()}_candidate_countdown_sec")
    try:
        countdown = int(countdown_raw) if countdown_raw is not None else None
    except Exception:
        countdown = None

    if str(state.get("reversal_active_candidate_side") or "").strip().upper() != side_u:
        return stage, countdown

    def _as_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value
        try:
            text = str(value or "").strip()
            return datetime.fromisoformat(text) if text else None
        except Exception:
            return None

    started_at = _as_datetime(state.get(f"reversal_{side_u.lower()}_candidate_started_at"))
    expires_at = _as_datetime(state.get(f"reversal_{side_u.lower()}_candidate_expires_at"))
    if started_at is None or expires_at is None:
        return stage, countdown

    display_now = now if isinstance(now, datetime) else datetime.now()
    remaining_total = float((expires_at - display_now).total_seconds())
    if remaining_total <= 0.0:
        return None, None
    try:
        wait_sec = max(0.0, float(state.get("reversal_wait_sec", state.get("entry_stage_wait_sec", 0.0)) or 0.0))
    except Exception:
        wait_sec = 0.0
    elapsed = max(0.0, float((display_now - started_at).total_seconds()))
    if elapsed < wait_sec:
        return "대기", max(1, int(math.ceil(wait_sec - elapsed)))
    return "감시", max(1, int(math.ceil(remaining_total)))


def trend_arm_timer_display_suffix(stage, wait_countdown, rolling_countdown) -> str:
    """Render the simultaneous TREND wait/rolling timers without duplicates."""
    parts = []
    if str(stage or "").strip() == "대기" and wait_countdown is not None:
        try:
            parts.append(f"대기 {int(wait_countdown)}s")
        except Exception:
            pass
    if rolling_countdown is not None:
        try:
            parts.append(f"{int(rolling_countdown)}s")
        except Exception:
            pass
    return (" " + " ".join(parts)) if parts else ""


def regime_candle_dots(labels, count: int, *, split_last_two: bool = False) -> str:
    """Render a fixed-width, oldest-to-newest completed-candle sequence."""
    count_n = max(0, int(count))
    items = [
        item for item in str(labels or "").split("/")
        if str(item).strip()
    ][-count_n:]
    items = (["UNKNOWN"] * max(0, count_n - len(items))) + items
    dot = {
        "양봉": '<span style="color:#FF4D5A;">●</span>',
        "음봉": '<span style="color:#3B82F6;">●</span>',
        "DOJI": '<span style="color:#8B95A5;">●</span>',
        "SMALL": '<span style="color:#8B95A5;">●</span>',
        "UNKNOWN": '<span style="color:#8B95A5;">●</span>',
    }
    dots = [dot.get(str(item).strip(), dot["UNKNOWN"]) for item in items]
    if split_last_two and len(dots) >= 2:
        return (
            "&nbsp;&nbsp;".join(dots[:-2])
            + "&nbsp;&nbsp;│&nbsp;&nbsp;"
            + "&nbsp;&nbsp;".join(dots[-2:])
        )
    return "&nbsp;&nbsp;".join(dots)


def regime_profile_display_status(
    auto_on: bool,
    z5_value,
    reversal_z5_abs_max: float,
    trend_z5_abs_max: float,
    trend_long_prerequisite_met: bool,
    trend_short_prerequisite_met: bool,
    trend_consecutive_same_4_blocked: bool = False,
) -> tuple[bool, bool]:
    """Return UI-only (TREND OKAY, REVERSAL OKAY) from visible inputs.

    AUTO controls order submission only, so monitoring stays live while OFF.
    """
    try:
        z5_abs = abs(float(z5_value))
        reversal_max = abs(float(reversal_z5_abs_max))
        trend_max = abs(float(trend_z5_abs_max))
    except Exception:
        return False, False
    trend_sequence_ok = bool(
        not trend_consecutive_same_4_blocked
        and
        bool(trend_long_prerequisite_met) != bool(trend_short_prerequisite_met)
    )
    return (
        bool(z5_abs <= trend_max and trend_sequence_ok),
        bool(z5_abs <= reversal_max and not trend_sequence_ok),
    )


def _diagnostic_log_path(base_dir: str | None = None) -> str:
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "runtime", "errors", f"process_diag_{datetime.now():%Y%m%d}.log")


def _write_process_diagnostic(tag: str, message: str, *, base_dir: str | None = None) -> None:
    path = _diagnostic_log_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{now_str()} [{tag}] pid={os.getpid()} {message}\n")
    except Exception:
        pass


def _acquire_single_instance_lock(base_dir: str | None = None) -> bool:
    """Keep one KIS session per workspace.

    KIS applies REST and WebSocket limits per APP KEY, not per Python process.
    Two dashboard processes therefore compete for the same quote budget and the
    second order-notice WebSocket is rejected as ``ALREADY IN USE appkey``.
    QLockFile also removes a lock whose owning process has terminated, so a
    crash does not permanently block the next watchdog start.
    """

    global _INSTANCE_LOCK
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    state_dir = os.path.join(root, "runtime", "state")
    try:
        os.makedirs(state_dir, exist_ok=True)
        lock = QLockFile(os.path.join(state_dir, "kis_main.instance.lock"))
        # Rely on the recorded PID/host check instead of treating a healthy,
        # long-running trading process as stale merely because the file is old.
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            _write_process_diagnostic(
                "DUPLICATE_INSTANCE_BLOCKED",
                "another kis_main process already owns the KIS session",
                base_dir=root,
            )
            return False
        _INSTANCE_LOCK = lock
        return True
    except Exception as exc:
        # Failing closed is safer than opening a duplicate trading session.
        _write_process_diagnostic(
            "INSTANCE_LOCK_ERROR",
            f"{type(exc).__name__}: {exc}",
            base_dir=root,
        )
        return False


def _resume_state_path(base_dir: str | None = None) -> str:
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(root, "runtime", "state", "launcher_resume_state.json")


def _load_resume_state_for_boot(base_dir: str | None = None) -> dict[str, Any]:
    path = _resume_state_path(base_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _env_flag(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "y", "yes", "on"}


def _mask_secret_tail(value: Any, keep: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= keep:
        return "*" * len(text)
    return f"***{text[-keep:]}"


def _read_local_telegram_settings(base_dir: str) -> dict[str, str]:
    """Read Telegram secrets from the git-ignored local KIS settings file."""
    path = os.path.join(str(base_dir or ""), ".env.kis.local")
    if not os.path.isfile(path):
        return {}
    text = ""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                text = handle.read()
            break
        except UnicodeError:
            continue
        except OSError:
            return {}
    values: dict[str, str] = {}
    allowed_prefixes = ("ZENITH_TELEGRAM_", "TELEGRAM_", "TG_")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith(allowed_prefixes):
            values[name] = value.strip().strip('"').strip("'")
    return values


class TelegramNotifier:
    def __init__(self, owner, enabled: bool, token: str, chat_id: str) -> None:
        self.owner = owner
        self.enabled = bool(enabled)
        self.token = str(token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue()
        self._results: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.token and self.chat_id)

    def start(self) -> bool:
        if not self.ready:
            return False
        if self._started:
            return True
        self._started = True
        self._thread = threading.Thread(target=self._worker, name="ZenithTelegramNotifier", daemon=True)
        self._thread.start()
        return True

    def notify(self, event: str, message: str) -> None:
        if not self.ready:
            return
        text = str(message or "").strip()
        if not text:
            return
        try:
            self._queue.put_nowait((str(event or "generic").strip() or "generic", text))
        except Exception:
            pass

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._queue.put_nowait(None)
        except Exception:
            pass

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            event, message = item
            try:
                self._send(event, message)
            finally:
                self._queue.task_done()

    def wait_until_idle(self, timeout_sec: float = 0.0) -> bool:
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        while time.time() < deadline:
            try:
                if int(getattr(self._queue, "unfinished_tasks", 0) or 0) <= 0:
                    return True
            except Exception:
                return False
            time.sleep(0.05)
        try:
            return int(getattr(self._queue, "unfinished_tasks", 0) or 0) <= 0
        except Exception:
            return False

    def drain_results(self) -> None:
        """Log on the controller thread, never touch Qt from the sender."""
        while True:
            try:
                code, detail = self._results.get_nowait()
            except queue.Empty:
                return
            self.owner.log("TELEGRAM", code, detail)

    def _send(self, event: str, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                result = json.load(resp)
                if int(status) < 400 and result.get("ok") is True:
                    message_id = result.get("result", {}).get("message_id", "-")
                    self._results.put(("SEND_OK", f"event={event} status={status} message_id={message_id}"))
                else:
                    self._results.put(("SEND_ERROR", f"event={event} type=API status={status} error_code={result.get('error_code', '-')}"))
        except urllib.error.HTTPError as e:
            self._results.put(("SEND_ERROR", f"event={event} type=HTTPError status={getattr(e, 'code', '-')}"))
        except Exception as e:
            # Exception text can contain the request URL and bot token.
            self._results.put(("SEND_ERROR", f"event={event} type={type(e).__name__} status=-"))


def _normalize_server_mode(raw: Any, default: str = "PAPER") -> str:
    text = str(raw or "").strip().upper()
    if text in ("PAPER", "LIVE"):
        return text
    return default


def _parse_runtime_resume(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--auto-restore", action="store_true")
    parser.add_argument("--no-dialog", action="store_true")
    parser.add_argument("--server")
    parser.add_argument("--account")
    args, unknown = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    saved_state = _load_resume_state_for_boot()
    env_watchdog = _env_flag("ZENITH_LAUNCHED_BY_WATCHDOG")
    env_auto_restore = _env_flag("ZENITH_AUTO_RESTORE")
    env_server_raw = str(os.environ.get("ZENITH_SERVER_MODE", "") or "").strip().upper()
    arg_server_raw = str(args.server or "").strip()
    arg_server = arg_server_raw.upper()
    invalid_server = arg_server_raw if arg_server_raw and arg_server not in ("PAPER", "LIVE") else ""

    if arg_server in ("PAPER", "LIVE"):
        server = arg_server
        server_source = "arg"
    elif env_server_raw in ("PAPER", "LIVE"):
        server = env_server_raw
        server_source = "env"
    else:
        saved_server = str(saved_state.get("last_server_mode") or "").strip().upper()
        if saved_server in ("PAPER", "LIVE"):
            server = saved_server
            server_source = "saved_state"
        else:
            server = "LIVE"
            server_source = "default"

    resume_flag = bool(args.resume or env_watchdog)
    auto_restore_requested = bool(args.auto_restore or env_auto_restore)

    return {
        "resume": bool(args.resume),
        "resume_flag": resume_flag,
        "auto_restore": auto_restore_requested,
        "no_dialog": bool(args.no_dialog),
        "server": server,
        "server_source": server_source,
        "account": str(args.account or os.environ.get("ZENITH_ACCOUNT", "") or "").strip(),
        "launched_by_watchdog": env_watchdog,
        "saved_state": saved_state,
        "invalid_server": invalid_server,
        "unknown_args": list(unknown),
    }


RUNTIME_RESUME = _parse_runtime_resume()


def _force_process_exit_later(delay_sec: float = 1.2) -> None:
    def _kill() -> None:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    try:
        timer = threading.Timer(max(0.0, float(delay_sec)), _kill)
        timer.daemon = True
        timer.start()
    except Exception:
        try:
            _kill()
        except Exception:
            pass


def mask_account(acc: str) -> str:
    s = str(acc or "").strip()
    if len(s) < 6:
        return s or "----****--"
    return f"{s[:4]}****{s[-2:]}"


def _select_server_popup(default_choice: str = "PAPER") -> tuple[str, bool, tuple[int, int]]:
    # Deprecated: server mode is now determined only by KIS's login session.
    # Kept for compatibility with older imports/tests, but no active path calls this.
    dlg = QDialog()
    dlg.setWindowTitle("서버 선택")
    dlg.resize(460, 228)
    layout = QVBoxLayout(dlg)
    label = QLabel("서버를 선택하세요")
    font = QFont()
    font.setPointSize(12)
    label.setFont(font)
    layout.addWidget(label)
    lst = QListWidget()
    lst.addItems(["LIVE", "PAPER"])
    lst.setCurrentRow(1 if str(default_choice).upper() == "PAPER" else 0)
    lst.setFont(font)
    layout.addWidget(lst)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    ok = dlg.exec_() == QDialog.Accepted
    choice = lst.currentItem().text() if ok and lst.currentItem() else default_choice
    return str(choice).upper(), ok, (dlg.width(), dlg.height())


def _normalize_account_no(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    # KIS ACCNO may include semicolon-delimited items or display text.
    text = text.split(";")[0].strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits or text.replace("-", "").strip()


def _fixed_account_for_server(server_type: str) -> str:
    server = _normalize_server_mode(server_type, default="PAPER")
    if server == "LIVE":
        return _normalize_account_no(FIXED_LIVE_ACCOUNT_NO)
    return ""


def _fixed_live_account() -> str:
    return _normalize_account_no(FIXED_LIVE_ACCOUNT_NO)


def _split_account_list(raw: Any) -> list[str]:
    accounts: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").replace(",", ";").split(";"):
        acc = _normalize_account_no(part)
        if acc and acc not in seen:
            accounts.append(acc)
            seen.add(acc)
    return accounts


def _server_label_kr(server_type: str) -> str:
    return "실전" if str(server_type or "").upper() == "LIVE" else "모의"


def _select_account_popup(accounts: list[str], server_type: str = "PAPER", default_account: str = "") -> tuple[str, bool, tuple[int, int]]:
    dlg = QDialog()
    dlg.setWindowTitle(f"{_server_label_kr(server_type)} 계좌 선택")
    dlg.resize(560, 300)
    layout = QVBoxLayout(dlg)
    label = QLabel(f"KIS {_server_label_kr(server_type)} 접속 계좌를 선택하세요")
    font = QFont()
    font.setPointSize(12)
    label.setFont(font)
    layout.addWidget(label)
    lst = QListWidget()
    lst.setFont(font)
    for acc in accounts:
        lst.addItem(str(acc))
    default_norm = _normalize_account_no(default_account)
    row = 0
    if default_norm and default_norm in accounts:
        row = accounts.index(default_norm)
    if accounts:
        lst.setCurrentRow(row)
    layout.addWidget(lst)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    ok = dlg.exec_() == QDialog.Accepted
    choice = _normalize_account_no(lst.currentItem().text()) if ok and lst.currentItem() else ""
    return choice, ok, (dlg.width(), dlg.height())


class ZenithZScoreController(QObject):
    def __init__(self, runtime_resume: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._shutdown_in_progress = False
        self._watchdog_mode = False
        self._auto_restore_pending = False
        self._resume_args: dict[str, Any] = {}
        self._resume_auto_restore_pending = False
        self._resume_auto_restore_attempted = False
        self._watchdog_server_mode = "PAPER"
        self._watchdog_auto_restore = False
        self._watchdog_maintenance_start = ""
        self._watchdog_maintenance_end = ""
        self._watchdog_shutdown_before_min = 0
        self._watchdog_restart_after_min = 0
        self._watchdog_auto_shutdown_time = ""
        self._watchdog_auto_restart_time = ""
        self._watchdog_status_text = "수동 실행"
        self._telegram_enabled = False
        self._telegram_token = ""
        self._telegram_chat_id = ""
        self._telegram_notifier: TelegramNotifier | None = None
        self._telegram_sent_warmup_ok = False
        self._telegram_sent_auto_restored = False
        self._telegram_sent_shutdown = False
        self._telegram_last_position_signature = ""
        self._telegram_summary_sent_key = ""
        self._telegram_last_execution_report_key = ""
        self._telegram_last_execution_report_ts = 0.0
        self._telegram_balance_confirmed_pnl_pt = 0.0
        self._telegram_balance_confirmed_trade_counts_date = ""
        self._telegram_balance_confirmed_trade_counts = {"TREND": 0, "REVERSAL": 0, "UNKNOWN": 0}
        self.cfg = Config()
        self.runtime_resume = dict(runtime_resume or RUNTIME_RESUME)
        self.app = QApplication.instance() or QApplication(sys.argv)
        try:
            self.app.setQuitOnLastWindowClosed(True)
        except Exception:
            pass
        self.client = KisClient()
        self.dashboard = LiveDashboardQt(
            on_auto=self.on_auto_clicked,
            on_cancel=self.on_cancel_clicked,
            on_exit=self.on_exit_clicked,
            on_close=self.on_dashboard_close_requested,
        )
        self.dashboard._skip_close_confirm = bool(self._should_bypass_dialogs())
        self.dashboard._should_bypass_close_confirm = self._should_bypass_dialogs
        self.dashboard.start()

        self.account = ""
        self.server_type = "-"
        self._server_type_auto_unknown = False
        self.live_code = str(getattr(self.cfg, "LIVE_SYMBOL_CODE", "") or "").strip()
        self.history_code = str(getattr(self.cfg, "HIST_SYMBOL_CODE", getattr(self.cfg, "SYMBOL_CODE", "")) or "").strip()
        # Safety hard-guard: new entries are fixed to 1 contract.
        self.order_qty = 1
        self.server_orderable_qty = 0
        self.server_orderable_qty_valid = False
        self.server_orderable_qty_source = ""
        # KIS_ORDERABLE returns different fields depending on account/position state.
        # Keep the raw server meaning separate, then expose the best contract count
        # to the dashboard as server_orderable_qty.
        self.server_closeable_qty = None
        self.server_closeable_qty_valid = False
        self.server_orderable_amount = None
        self.server_orderable_amount_valid = False
        self._orderable_price0_retry_pending = False
        self._orderable_price0_retry_count = 0
        self.server_deposit = None
        self.server_deposit_valid = False
        self.server_deposit_source = ""

        self.connected = False
        self.auto_on = False
        self.want_auto_after_login = False
        self.real_registered = False
        self.warmup_loading = False
        self.warmup_done = False
        self.warmup_pages = 0
        self.warmup_last_prev_next = '0'
        self._warmup_rows: dict[str, dict] = {}
        self._warmup_seed_checkpoint: dict[datetime, BarData] = {}
        self.warmup_started = False
        self._warmup_retry_count = 0
        self._warmup_retry_pending = False

        self.current_price = 0.0
        self.current_bid = 0.0
        self.current_ask = 0.0
        self.session_open_price = 0.0
        self.session_open_date = None
        self.session_open_confirmed = False
        self.session_high_price = 0.0
        self.session_low_price = 0.0
        self.session_high_time = "-"
        self.session_low_time = "-"
        self.last_bar_close = 0.0
        self.session_close = 0.0
        self.last_tick_time = "-"
        self.last_bar_time = "-"
        self.last_tick_ts = 0.0
        self._last_execution_notice_ts = 0.0
        self._last_server_msg_ts = 0.0
        self._last_connect_event_ts = 0.0
        self.last_market_status = "DISCONNECTED"

        self.position_side = "FLAT"   # FLAT / LONG / SHORT
        self.position_qty = 0
        self.entry_price = 0.0
        self.position_entry_time = None
        self.position_entry_z5 = None
        self.position_entry_prev_z5 = None
        self.position_entry_dz5 = None
        self.position_entry_dz5_threshold = None
        self.position_entry_dz5_direction_ok = None
        self.position_entry_macd_osci = None
        self.position_entry_regime = ""
        self.position_entry_z5_band_snapshot: dict[str, Any] = {}
        self.position_mfe_profile = ""
        self.position_peak_price = 0.0
        self.position_trough_price = 0.0
        self.position_mfe_armed = False
        self.position_mfe_armed_since_ts = 0.0
        self.position_mfe_stage_step = 0
        self._position_mfe_stage_last_bar_key = ""
        self._position_mfe_stage_last_extreme_price = 0.0
        self._entry_order_guard_active = False
        self._entry_order_guard_side = ""
        self._entry_order_guard_ts = 0.0
        self.last_exit_reason = "-"
        self.exit_reason_history: deque[str] = deque(maxlen=10)
        self.pending_exit_reason = ""
        self._pending_exit_entry_block_until_ts = 0.0
        self.last_exit_context: dict[str, Any] = {}
        self._last_exit_submit_context: dict[str, Any] = {}
        self._last_entry_submit_metrics: dict[str, Any] = {}
        self._last_entry_order_audit: dict[str, Any] = {}
        self._position_entry_audit: dict[str, Any] = {}
        self._position_entry_order_audit: dict[str, Any] = {}
        self.last_position_side_before_flat = "FLAT"
        self.last_position_qty_before_flat = 0
        self.last_flat_confirm_source = "-"
        self._pending_exit_position_side = "FLAT"
        self._pending_exit_position_qty = 0

        # 진입 시점 snapshot — 예상 청산지수 고정용
        self._entry_mu: float | None = None
        self._entry_sd: float | None = None
        self._fixed_exit_index: float | None = None

        # takeover — 시작 시 KIS_BALANCE로 기존 포지션 인계
        self.takeover_done = False
        self.takeover_requested = False
        self.takeover_inflight = False
        self.takeover_position_confirmed = False
        self.takeover_rows_last = 0
        self.takeover_retry_count = 0
        self.takeover_warmup_retry_done = False
        self.takeover_start_warmup_after_response = False
        self.unfilled_inflight = False
        self.account_snapshot_inflight = False
        self.orderable_snapshot_inflight = False
        self.unfilled_rows_last = 0
        self.server_sync_pending = False
        self.server_sync_reason = "-"
        self.server_sync_parts_pending: dict[str, bool] = {"takeover": False, "unfilled": False, "account": False, "orderable": False}
        self._server_sync_token = 0
        self._server_sync_start_warmup = False
        self._last_server_sync_request_ts = 0.0
        self._server_sync_min_interval_sec = float(getattr(self.cfg, "UNFILLED_SYNC_MIN_INTERVAL_SEC", 1.5) or 1.5)
        self._kis_open_orders_timer_busy_interval_sec = max(
            float(self._server_sync_min_interval_sec or 1.5),
            float(getattr(self.cfg, "UNFILLED_TIMER_BUSY_INTERVAL_SEC", 2.0) or 2.0),
        )
        self._kis_open_orders_timer_idle_interval_sec = max(
            self._kis_open_orders_timer_busy_interval_sec,
            float(getattr(self.cfg, "UNFILLED_TIMER_IDLE_INTERVAL_SEC", 60.0) or 60.0),
        )
        self._last_kis_open_orders_timer_check_ts = 0.0
        self._last_kis_balance_timer_check_ts = 0.0
        self._last_kis_account_timer_check_ts = 0.0
        self._last_kis_orderable_timer_check_ts = 0.0
        self._takeover_heartbeat_sec = float(getattr(self.cfg, "TAKEOVER_HEARTBEAT_SEC", 0.0) or 0.0)
        self._last_takeover_heartbeat_ts = 0.0
        self._tr_reason_cooldown_sec = max(2.0, float(getattr(self.cfg, "TR_REASON_COOLDOWN_SEC", 2.0) or 2.0))
        self._tr_reason_last_ts: dict[str, float] = {}
        self._tr_reason_inflight: dict[str, set[str]] = {"KIS_OPEN_ORDERS": set(), "KIS_BALANCE": set(), "KIS_ACCOUNT": set(), "KIS_ORDERABLE": set()}
        self._tr_active_reason: dict[str, str] = {"KIS_OPEN_ORDERS": "", "KIS_BALANCE": "", "KIS_ACCOUNT": "", "KIS_ORDERABLE": ""}
        self._tr_active_started_ts: dict[str, float] = {"KIS_OPEN_ORDERS": 0.0, "KIS_BALANCE": 0.0, "KIS_ACCOUNT": 0.0, "KIS_ORDERABLE": 0.0}
        self._tr_failure_streak: dict[str, int] = {"KIS_OPEN_ORDERS": 0, "KIS_BALANCE": 0, "KIS_ACCOUNT": 0, "KIS_ORDERABLE": 0}
        self._tr_last_status: dict[str, str] = {"KIS_OPEN_ORDERS": "", "KIS_BALANCE": "", "KIS_ACCOUNT": "", "KIS_ORDERABLE": ""}
        self._tr_last_failure_ts: dict[str, float] = {"KIS_OPEN_ORDERS": 0.0, "KIS_BALANCE": 0.0, "KIS_ACCOUNT": 0.0, "KIS_ORDERABLE": 0.0}
        self._health_recheck_last_ts = 0.0
        self._health_recheck_reason = ""
        self._reconnect_last_attempt_ts = 0.0
        self._reconnect_attempt_inflight = False
        self._reconnect_last_reason = ""
        self._login_wait_started_ts = 0.0
        self._login_wait_reason = ""
        self._login_wait_warning_sent = False
        self._login_wait_popup_sent = False
        self._login_wait_timeout_handled = False
        self._kis_login_server_notice_shown = False
        self.has_server_unfilled = False
        self.server_unfilled_qty = 0
        self.server_unfilled_order_no = ""
        self.server_unfilled_orig_order_no = ""
        self.server_unfilled_side = ""
        self.server_unfilled_symbol = ""
        self.server_unfilled_status = ""
        self.server_unfilled_last_seen_ts = ""
        self.server_unfilled_last_reason = "-"
        self._exit_skip_server_unfilled_last_key = ""
        self._exit_skip_server_unfilled_last_ts = 0.0
        self._auto_runtime_cancel_last_ts = 0.0
        self._auto_exit_skip_log_last_key = ""
        self._auto_exit_skip_log_last_ts = 0.0
        self.auto_cancel_phase = "-"
        self.last_allow_trade = False
        self.last_block_reason = "AUTO_OFF"
        self._last_entry_block_log_key = None
        self._last_exit_block_log_key = None
        self._auto_precheck_session_id = 0
        self._auto_precheck_active = False
        self._auto_precheck_started_ts = 0.0
        self._precheck_entry_filled_session_id = 0
        self._precheck_stale_position_detected = False
        self._precheck_position_conflict_detected = False
        self._precheck_takeover_existing_position = False
        self._auto_precheck_force_exit_existing_position = bool(getattr(self.cfg, "AUTO_PRECHECK_FORCE_EXIT_EXISTING_POSITION", False))
        self._precheck_stale_unfilled_ids: set[str] = set()
        self._precheck_stale_unfilled_qty = 0
        self._eod_force_exit_last_date = ""
        self._eod_force_exit_last_try_ts = 0.0
        self._eod_force_exit_submitted_date = ""
        self._last_market_holiday_log_date = ""

        self.bar_1m: dict[str, Any] | None = None
        self.bar_count_1m = 0
        self._startup_entry_gate_started_ts = time.time()
        self._startup_entry_delay_sec = max(0.0, float(getattr(self.cfg, "STARTUP_ENTRY_DELAY_SEC", 10.0) or 0.0))
        self._post_warmup_entry_delay_bars = max(0, int(getattr(self.cfg, "POST_WARMUP_ENTRY_DELAY_BARS", 5) or 0))
        self._post_warmup_live_bar_count = 0
        self._post_warmup_gate_bypassed_after_trade = False
        self._live_bars_by_minute = OrderedDict()
        self._live_1m_by_minute: OrderedDict[datetime, Candle] = OrderedDict()
        self._diag_stale_tick_skip_count = 0
        self._diag_same_minute_update_count = 0
        self._diag_new_minute_append_count = 0
        self._diag_dedupe_before_1m = 0
        self._diag_dedupe_after_1m = 0
        self._diag_dedupe_before_5m_input = 0
        self._diag_dedupe_after_5m_input = 0
        self._diag_dedupe_before_30m_input = 0
        self._diag_dedupe_after_30m_input = 0
        self._diag_last_1m_minute_key: datetime | None = None
        self._diag_last_5m_bucket_key: datetime | None = None
        self._diag_last_30m_bucket_key: datetime | None = None
        self._last_dedupe_diag_log_ts = 0.0
        self._dedupe_diag_log_min_interval_sec = 10.0
        self._last_stale_tick_log_ts = 0.0
        self._last_stale_tick_logged_count = 0
        self._stale_tick_log_min_interval_sec = 30.0
        self.live_z5_diag_state: dict[str, Any] = {}
        self._last_live_zscore_write_error_ts = 0.0
        self.st = Strategy(self.cfg)
        self._refresh_live_z5_diag_state(0, 0, 0)
        self.last_signal = "NONE"
        self.last_signal_reason = "-"
        self.last_z = None          # 호환용 = 5분 Z
        self.last_z_1m = None
        self.last_z_5m = None
        self.last_z_30m = None
        self.last_regime = "OFF" if not bool(getattr(self.cfg, "USE_REGIME_FILTER", False)) else "NORMAL"
        self.last_warmup_text = "1분 0/20 | 5분 0/20 | 30분 0/20"
        self.last_long_ok = None
        self.last_short_ok = None
        self.last_exit_ok = None
        self.last_macd_delta = None
        self.last_macd_delta_tf = "1m"
        self.last_short_enabled = bool(getattr(self.cfg, "ENABLE_SHORT", False))
        self.last_sig_state: dict[str, Any] = {}
        self.cancel_status = "미체결 없음"
        self.exit_button_status = "보유 없음"
        self.cancel_in_progress = False
        self.exit_in_progress = False
        self.entry_inflight = False
        self.exit_inflight = False
        self.has_unfilled_orders = False
        self.active_position_side = "FLAT"
        self.active_position_qty = 0
        # Start in guarded state until first server sync confirms flat/position.
        self.flat_confirmed = False
        self.order_lane_locked = False
        self.position_close_pending = False
        self.pending_entry_side = ""
        self.cancel_confirmed = False
        self._manual_cancel_check_pending = False
        self.exit_confirmed = False
        self.cancel_needs_server_check = False
        self.exit_needs_server_check = False
        self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
        _entry_cd_cfg = getattr(self.cfg, "ENTRY_SUBMIT_COOLDOWN_SEC", 3.0)
        self._entry_submit_cooldown_sec = 3.0 if _entry_cd_cfg is None else float(_entry_cd_cfg)
        _post_fill_cd_cfg = getattr(self.cfg, "POST_FILL_ACTION_COOLDOWN_SEC", 3.0)
        self._post_fill_action_cooldown_sec = 3.0 if _post_fill_cd_cfg is None else float(_post_fill_cd_cfg)
        _reverse_cd_cfg = getattr(self.cfg, "REVERSE_ENTRY_COOLDOWN_SEC", 10.0)
        self._reverse_entry_cooldown_sec = 10.0 if _reverse_cd_cfg is None else float(_reverse_cd_cfg)
        _reverse_signal_reentry_cd_cfg = getattr(self.cfg, "REVERSE_SIGNAL_REENTRY_COOLDOWN_SEC", 10.0)
        self._reverse_signal_reentry_cooldown_sec = 10.0 if _reverse_signal_reentry_cd_cfg is None else float(_reverse_signal_reentry_cd_cfg)
        _fixed_loss_reverse_cd_cfg = getattr(self.cfg, "FIXED_LOSS_REENTRY_COOLDOWN_SEC", self._reverse_entry_cooldown_sec)
        self._fixed_loss_reentry_cooldown_sec = self._reverse_entry_cooldown_sec if _fixed_loss_reverse_cd_cfg is None else float(_fixed_loss_reverse_cd_cfg)
        _mfe_cut_reentry_cd_cfg = getattr(self.cfg, "MFE_CUT_REENTRY_COOLDOWN_SEC", self._reverse_entry_cooldown_sec)
        self._mfe_cut_reentry_cooldown_sec = self._reverse_entry_cooldown_sec if _mfe_cut_reentry_cd_cfg is None else float(_mfe_cut_reentry_cd_cfg)
        self._last_entry_submit_ts = 0.0
        self._last_entry_submit_side = ""
        self._last_entry_filled_ts = 0.0
        self._last_exit_filled_ts = 0.0
        self._last_balance_recovery_entry_ts = 0.0
        self._last_balance_recovery_side = ""
        self._reverse_retry_armed = False
        self._reverse_retry_prior_side = "FLAT"
        self._reverse_retry_reason = ""
        self._reverse_retry_armed_ts = 0.0
        self._reverse_retry_attempts = 0
        self._reverse_retry_due_ts = 0.0
        self._reverse_retry_timer_active = False
        self._post_exit_server_sync_required = False
        self._reverse_after_sync_pending = False
        self._reverse_after_sync_prior_side = "FLAT"
        self._reverse_after_sync_reason = ""
        self._forced_reverse_entry_pending = False
        self._forced_reverse_entry_side = ""
        self._forced_reverse_entry_sig_state: dict[str, Any] = {}
        # Same-tick overlap lane: when MFE_TRAIL exit and the opposite normal
        # ENTRY FIRE are true on the same tick, EXIT must win first. Preserve
        # that ENTRY snapshot until broker EXIT_FILLED, then submit after a
        # short callback/server-lane stabilization delay. This avoids losing
        # the FIRE and also avoids the "미확인 진입주문" latch caused by an
        # entry submitted while the just-filled EXIT pending state is still
        # being cleaned up.
        self._post_exit_entry_pending = False
        self._post_exit_entry_side = ""
        self._post_exit_entry_prior_side = "FLAT"
        self._post_exit_entry_reason = ""
        self._post_exit_entry_sig_state: dict[str, Any] = {}
        self._post_exit_entry_armed_ts = 0.0
        self._post_exit_entry_attempts = 0
        self._post_exit_entry_send_attempts = 0
        self._post_exit_entry_timer_active = False
        self._post_exit_entry_awaiting_confirm = False
        self._post_exit_entry_last_submit_ts = 0.0
        self._post_exit_entry_window_sec = max(
            1.0,
            float(getattr(self.cfg, "POST_EXIT_OVERLAP_ENTRY_WINDOW_SEC", 12.0) or 12.0),
            float(self._reverse_entry_cooldown_sec or 0.0) + 2.0,
            float(self._reverse_signal_reentry_cooldown_sec or 0.0) + 2.0,
            float(self._fixed_loss_reentry_cooldown_sec or 0.0) + 2.0,
            float(self._mfe_cut_reentry_cooldown_sec or 0.0) + 2.0,
        )
        self._post_exit_entry_delay_ms = max(0, int(getattr(self.cfg, "POST_EXIT_OVERLAP_ENTRY_DELAY_MS", 350) or 350))
        self._post_exit_entry_unknown_clear_sec = max(0.5, float(getattr(self.cfg, "POST_EXIT_ENTRY_UNKNOWN_CLEAR_SEC", 2.2) or 2.2))
        self._post_exit_entry_max_send_attempts = max(1, int(getattr(self.cfg, "POST_EXIT_ENTRY_MAX_SEND_ATTEMPTS", 2) or 2))
        self._mfe_protect_post_exit_entry_enabled = bool(getattr(self.cfg, "MFE_PROTECT_POST_EXIT_ENTRY_ENABLED", False))
        self._mfe_protect_reset_entry_arms = bool(getattr(self.cfg, "MFE_PROTECT_RESET_ENTRY_ARMS", True))

        self.logs: deque[dict[str, str]] = deque(maxlen=120)
        self._last_view_refresh_ts = 0.0
        # EXIT 전후 버벅임 방지: 실시간 틱/체결 콜백에서 refresh_view가
        # 중복 호출되더라도 100~150ms 단위로 합쳐서 처리한다.
        self._view_refresh_min_interval_sec = float(getattr(self.cfg, "VIEW_REFRESH_MIN_INTERVAL_SEC", 0.12) or 0.12)
        self._view_refresh_defer_ms = max(0, int(getattr(self.cfg, "VIEW_REFRESH_DEFER_MS", 50) or 50))
        self._view_refresh_pending = False
        self._view_refresh_force_pending = False
        # EXIT pending 중 같은 FIXED_LOSS/MFE 신호가 틱마다 재발생하면
        # 이벤트 로그와 EXIT 상태창이 폭증/버벅일 수 있다.
        # 아래 값들은 재주문·재로그를 눌러주고, 청산 주문확인 상태에서는
        # 서버 포지션/미체결 재조회만 주기적으로 수행하게 한다.
        self._exit_submit_guard_last_key = ""
        self._exit_submit_guard_last_ts = 0.0
        self._exit_log_dedupe: dict[str, float] = {}
        self._exit_pending_recheck_last_ts = 0.0
        self._exit_pending_recheck_interval_sec = float(getattr(self.cfg, "EXIT_PENDING_RECHECK_INTERVAL_SEC", 1.2) or 1.2)
        self._exit_submit_debounce_sec = float(getattr(self.cfg, "EXIT_SUBMIT_DEBOUNCE_SEC", 0.8) or 0.8)
        self._last_live_bar_snapshot_write_ts = 0.0
        self._live_bar_snapshot_min_interval_sec = 1.0
        self.order_core: KisOrderCore | None = None
        self._shutdown_done = False
        self._resume_state_saved = False
        self._shutdown_reason = ""
        self.resume_status_text = ""
        self._resume_state: dict[str, Any] = dict(self.runtime_resume.get("saved_state") or {})

        # ---------- runtime / 파일 로그 초기화 ----------
        self._base_dir = os.path.dirname(os.path.abspath(__file__))
        self._diagnostic_log_path = _diagnostic_log_path(self._base_dir)
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._runtime_dir = os.path.join(self._base_dir, "runtime")
        self._data_dir = os.path.join(self._runtime_dir, "data")
        self._report_dir = os.path.join(self._runtime_dir, "report")
        self._state_dir = os.path.join(self._runtime_dir, "state")
        self._tests_dir = os.path.join(self._runtime_dir, "tests")
        self._errors_dir = os.path.join(self._runtime_dir, "errors")
        for _d in (self._runtime_dir, self._data_dir, self._report_dir, self._state_dir, self._tests_dir, self._errors_dir):
            os.makedirs(_d, exist_ok=True)

        self._trade_log_path = os.path.join(self._report_dir, f"trade_log_{self._session_id}.csv")
        self._event_log_path = os.path.join(self._report_dir, f"event_log_{self._session_id}.txt")
        self._event_log_csv_path = os.path.join(self._report_dir, f"event_log_{self._session_id}.csv")
        self._live_bar_path = os.path.join(self._data_dir, f"bars_1m_{self._session_id}.csv")
        self._live_zscore_path = os.path.join(self._data_dir, f"zscore_trace_{self._session_id}.csv")
        self._warmup_bar_raw_path = os.path.join(self._data_dir, f"warmup_bars_1m_raw_{self._session_id}.csv")
        self._warmup_bar_path = os.path.join(self._data_dir, f"warmup_bars_1m_{self._session_id}.csv")
        self._warmup_zscore_path = os.path.join(self._data_dir, f"warmup_zscore_trace_{self._session_id}.csv")
        self._warmup_summary_path = os.path.join(self._report_dir, f"warmup_summary_{self._session_id}.csv")
        self._warmup_gap_report_path = os.path.join(self._report_dir, f"warmup_gap_fill_{self._session_id}.csv")
        self._signal_log_path = os.path.join(self._report_dir, f"signal_log_{self._session_id}.csv")
        self._state_report_path = os.path.join(self._report_dir, f"state_report_{self._session_id}.csv")
        self._exit_trace_path = os.path.join(self._report_dir, f"exit_trace_{self._session_id}.csv")
        self._run_manifest_path = os.path.join(self._report_dir, f"run_manifest_{self._session_id}.json")
        # Persistent anchor file (cross-session): keeps filled entry z5 for reconnect takeover recovery.
        self._entry_anchor_state_path = os.path.join(self._report_dir, "position_entry_anchor_state.json")
        self._resume_state_path = _resume_state_path(self._base_dir)
        self._init_runtime_files()

        self.client.connection_changed.connect(self.on_connected)
        self.client.message_received.connect(self.on_msg)
        self.client.execution_received.connect(self.on_execution_notice)
        self.client.order_activity_received.connect(self.on_kis_order_activity)
        self.client.tick_received.connect(self.on_tick_received)
        self.client.response_received.connect(self.on_response)
        try:
            self.app.aboutToQuit.connect(self.on_app_about_to_quit)
        except Exception:
            pass

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.on_timer)
        self.timer.start(1000)

        self.log("ENGINE", "BOOT", f"컨트롤러 시작 live_code={self.live_code} hist_code={self.history_code}")
        self.log("RUNTIME", "DIRS", f"runtime={self._runtime_dir} | data={self._data_dir} | report={self._report_dir} | tests={self._tests_dir} | errors={self._errors_dir}")
        self._init_resume_runtime()
        self._init_telegram_runtime()
        if self._watchdog_mode and self._telegram_enabled and self._telegram_token and self._telegram_chat_id:
            self._telegram_queue(
                "watchdog_start",
                f"🚀 Zenith 시작\n"
                f"접속: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                f"모드: {self._watchdog_server_mode}\n"
                f"WATCH DOG: {'감시 중' if self._watchdog_mode else 'OFF'}\n"
                f"AUTO 복원: {'ON' if self._watchdog_auto_restore else 'OFF'}",
            )
            self.log("TELEGRAM", "QUEUE", "watchdog_start")
        self.refresh_view()
        self.try_login(reason="BOOT")

    # ---------- logging ----------
    def log(self, kind: str, code: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        kind_s = str(kind)
        code_s = str(code)
        message_s = str(message)
        noisy_bar_log = kind_s == "BAR" and code_s in ("LIVE_1M", "LIVE_5M")
        dedupe_log = (kind_s == "BAR" and code_s in ("DEDUPE", "STALE_TICK")) or code_s == "DEDUPE_SYNC"
        if not noisy_bar_log and not dedupe_log:
            self.logs.append({
                "ts": ts,
                "kind": kind_s,
                "code": code_s,
                "message": message_s,
            })
            try:
                self.dashboard.append_event_log(kind_s, code_s, message_s)
            except Exception:
                pass
        try:
            if not dedupe_log:
                with open(self._event_log_path, "a", encoding="utf-8") as _ef:
                    _ef.write(f"{ts} [{kind_s}][{code_s}] {message_s}\n")
        except Exception:
            pass
        if not dedupe_log:
            self._append_csv_row(self._event_log_csv_path, [datetime.now().strftime("%Y-%m-%d"), ts, kind_s, code_s, message_s])

    def _should_bypass_dialogs(self) -> bool:
        return bool(
            self.runtime_resume.get("no_dialog")
            or self.runtime_resume.get("resume")
            or self.runtime_resume.get("launched_by_watchdog")
            or self._shutdown_in_progress
        )

    def _set_resume_status(self, text: str, log_message: bool = False) -> None:
        self.resume_status_text = str(text or "").strip()
        if log_message and self.resume_status_text:
            self.log("RESUME", "STATUS", self.resume_status_text)
        try:
            self.refresh_view(force=True)
        except Exception:
            pass

    def _mark_login_wait_start(self, reason: str) -> None:
        self._login_wait_started_ts = time.time()
        self._login_wait_reason = str(reason or "KIS_OAUTH")
        self._login_wait_warning_sent = False
        self._login_wait_popup_sent = False
        self._login_wait_timeout_handled = False
        self._kis_login_server_notice_shown = False

    def _clear_login_wait(self) -> None:
        self._login_wait_started_ts = 0.0
        self._login_wait_reason = ""
        self._login_wait_warning_sent = False
        self._login_wait_popup_sent = False
        self._login_wait_timeout_handled = False
        self._kis_login_server_notice_shown = False

    def _telegram_env_value(self, *names: str) -> str:
        for name in names:
            value = str(os.environ.get(name, "") or "").strip()
            if value:
                return value
        local_values = getattr(self, "_telegram_local_settings", None)
        if local_values is None:
            local_values = _read_local_telegram_settings(self._base_dir)
            self._telegram_local_settings = local_values
        for name in names:
            value = str(local_values.get(name, "") or "").strip()
            if value:
                return value
        return ""

    def _init_telegram_runtime(self) -> None:
        enabled_raw = self._telegram_env_value("ZENITH_TELEGRAM_ENABLED", "TELEGRAM_ENABLED")
        token = self._telegram_env_value(
            "ZENITH_TELEGRAM_BOT_TOKEN",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_TOKEN",
            "TG_BOT_TOKEN",
        )
        chat_id = self._telegram_env_value(
            "ZENITH_TELEGRAM_CHAT_ID",
            "TELEGRAM_CHAT_ID",
            "TG_CHAT_ID",
        )
        self._telegram_enabled = str(enabled_raw).strip().lower() in {"1", "true", "y", "yes", "on"}
        self._telegram_token = token
        self._telegram_chat_id = chat_id
        self.log(
            "TELEGRAM",
            "CONFIG",
            f"enabled={1 if self._telegram_enabled else 0} token_present={bool(self._telegram_token)} "
            f"chat_present={bool(self._telegram_chat_id)}"
            + (f" chat_id={_mask_secret_tail(self._telegram_chat_id)}" if self._telegram_chat_id else ""),
        )
        self._telegram_notifier = TelegramNotifier(
            self,
            enabled=self._telegram_enabled,
            token=self._telegram_token,
            chat_id=self._telegram_chat_id,
        )
        if self._telegram_notifier.start():
            self.log("TELEGRAM", "WORKER", "worker_started")

    def _telegram_notify(self, message: str) -> None:
        try:
            if self._telegram_notifier is not None:
                self._telegram_notifier.notify("generic", message)
        except Exception:
            pass

    def _telegram_queue(self, event: str, message: str) -> None:
        try:
            if self._telegram_notifier is not None:
                self._telegram_notifier.notify(event, message)
        except Exception:
            pass

    def _telegram_position_summary(self) -> tuple[str, str]:
        deposit_txt = "알수없음"
        try:
            if self.server_deposit_valid and self.server_deposit is not None:
                deposit_txt = f"{int(float(self.server_deposit)):,}"
        except Exception:
            deposit_txt = str(self.server_deposit)
        orderable_txt = str(int(self.order_qty or 0))
        try:
            if self.server_orderable_qty_valid:
                orderable_txt = str(int(self.server_orderable_qty or 0))
        except Exception:
            orderable_txt = str(self.server_orderable_qty)
        if self.position_side in ("LONG", "SHORT") and int(self.position_qty or 0) > 0:
            pnl_txt = "알수없음"
            try:
                if float(self.entry_price or 0.0) > 0 and float(self.current_price or 0.0) > 0:
                    mult = 1.0 if self.position_side == "LONG" else -1.0
                    pnl_txt = f"{(float(self.current_price) - float(self.entry_price)) * mult:+.2f}pt"
            except Exception:
                pass
            current_txt = "-" if not float(self.current_price or 0.0) else f"{float(self.current_price):.2f}"
            signature = f"{self.position_side}|{int(self.position_qty or 0)}|{float(self.entry_price or 0.0):.2f}"
            text = (
                f"방향={self.position_side}\n"
                f"수량={int(self.position_qty or 0)}\n"
                f"평균가={float(self.entry_price or 0.0):.2f}\n"
                f"현재가={current_txt}\n"
                f"손익={pnl_txt}\n"
                f"주문가능={orderable_txt}\n"
                f"예수금={deposit_txt}\n"
                f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}"
            )
            return signature, text
        signature = "NONE"
        text = (
            "포지션=없음\n"
            f"주문가능={orderable_txt}\n"
            f"예수금={deposit_txt}\n"
            f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}"
        )
        return signature, text

    def _telegram_send_position_sync(self, reason: str = "") -> None:
        # Position reconciliation remains active; only its Telegram notice is disabled.
        return

    def _telegram_send_warmup_ok(self, total_rows: int, valid_rows: int, target_rows: int, ready: bool, code_tr: str) -> None:
        if self._telegram_sent_warmup_ok:
            return
        self._telegram_sent_warmup_ok = True
        self._telegram_queue(
            "warmup_ok",
            f"🟢 워밍업 완료\n"
            f"종목={self.live_code}\n"
            f"TR코드={code_tr}\n"
            f"bars={valid_rows}/{target_rows}\n"
            f"seed_rows={total_rows}\n"
            f"ready={bool(ready)}",
        )
        self.log("TELEGRAM", "QUEUE", f"warmup_ok code={self.live_code} bars={valid_rows}/{target_rows} ready={bool(ready)}")

    def _telegram_send_auto_restored(self) -> None:
        if self._telegram_sent_auto_restored:
            return
        self._telegram_sent_auto_restored = True
        _sig, position_text = self._telegram_position_summary()
        self._telegram_queue(
            "auto_restored",
            f"♻️ AUTO 복원 완료\n"
            f"WATCH DOG={'ON' if self._watchdog_mode else 'OFF'}\n"
            f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}\n"
            f"시장={self.last_market_status or '-'}\n"
            f"워밍업={bool(self.warmup_done)}\n"
            f"{position_text}\n"
            f"자동종료={self._watchdog_auto_shutdown_time or '--:--'}\n"
            f"자동재접속={self._watchdog_auto_restart_time or '--:--'}",
        )
        self.log("TELEGRAM", "QUEUE", "auto_restored")

    def _telegram_send_reconnect_attempt(self, reason: str) -> None:
        self._telegram_queue(
            "reconnect_attempt",
            f"🔄 자동 재접속 시도\n"
            f"사유={reason or '-'}\n"
            f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}\n"
            f"시장={self.last_market_status or '-'}\n"
            f"AUTO 복원={'ON' if self.want_auto_after_login else 'OFF'}",
        )
        self.log("TELEGRAM", "QUEUE", f"reconnect_attempt reason={reason or '-'}")

    def _telegram_send_auto_restore_status(self, stage: str, reason: str = "", extra: str = "") -> None:
        lines = [
            "🛠 AUTO 복원 상태",
            f"단계={stage or '-'}",
            f"사유={reason or '-'}",
            f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}",
            f"시장={self.last_market_status or '-'}",
            f"AUTO={bool(self.auto_on)}",
        ]
        if extra:
            lines.append(str(extra))
        self._telegram_queue("auto_restore_status", "\n".join(lines))
        self.log("TELEGRAM", "QUEUE", f"auto_restore_status stage={stage or '-'} reason={reason or '-'}")

    def _telegram_today_realized_pnl(self, as_of: datetime | None = None) -> float:
        """Sum today's realized EXIT PnL across every session trade log."""
        date_key = (as_of if isinstance(as_of, datetime) else datetime.now()).strftime("%Y-%m-%d")
        total = 0.0
        try:
            names = sorted(os.listdir(self._report_dir))
        except OSError:
            return total
        for name in names:
            if not (name.startswith("trade_log_") and name.endswith(".csv")):
                continue
            try:
                with open(os.path.join(self._report_dir, name), "r", encoding="utf-8-sig") as handle:
                    for row in csv.DictReader(handle):
                        if str(row.get("날짜") or "").strip() != date_key:
                            continue
                        if str(row.get("구분") or "").strip().upper() != "EXIT":
                            continue
                        try:
                            total += float(row.get("손익(pt)") or 0.0)
                        except (TypeError, ValueError):
                            continue
            except (OSError, UnicodeError, csv.Error):
                continue
        return round(total, 2)

    @staticmethod
    def _telegram_regime_label(regime: str) -> str:
        regime_u = str(regime or "").strip().upper()
        if regime_u == "TREND":
            return "트렌드형"
        if regime_u == "REVERSAL":
            return "반전형"
        return "미확인"

    def _telegram_entry_regime(self, explicit: str = "") -> str:
        audit = self._position_entry_audit if isinstance(getattr(self, "_position_entry_audit", None), dict) else {}
        regime = ZenithZScoreController._regime_from_text_hints(
            explicit,
            getattr(self, "position_entry_regime", ""),
            audit.get("entry_regime"),
            audit.get("active_entry_regime"),
            audit.get("entry_reason"),
            audit.get("entry_hl_source"),
        )
        return regime if regime in ("TREND", "REVERSAL") else "UNKNOWN"

    def _telegram_entry_reason(self, explicit: str = "", regime: str = "") -> str:
        """Return the strategy trigger, not the broker confirmation label."""

        generic_reasons = {
            "",
            "-",
            "ENTRY_FILLED",
            "BALANCE_FALLBACK",
            "BALANCE_KIS_EXECUTION_RECOVERY",
            "KIS_BALANCE_CONFIRMED",
        }

        def useful(value: Any) -> str:
            text = str(value or "").strip()
            return "" if text.upper() in generic_reasons else text

        direct = useful(explicit)
        if direct:
            return direct

        position_order = (
            self._position_entry_order_audit
            if isinstance(getattr(self, "_position_entry_order_audit", None), dict)
            else {}
        )
        last_order = (
            self._last_entry_order_audit
            if isinstance(getattr(self, "_last_entry_order_audit", None), dict)
            else {}
        )
        position_audit = (
            self._position_entry_audit
            if isinstance(getattr(self, "_position_entry_audit", None), dict)
            else {}
        )
        submit_metrics = (
            self._last_entry_submit_metrics
            if isinstance(getattr(self, "_last_entry_submit_metrics", None), dict)
            else {}
        )
        for value in (
            position_order.get("order_result_msg"),
            last_order.get("order_result_msg"),
            position_audit.get("entry_reason"),
            submit_metrics.get("entry_reason"),
        ):
            restored = useful(value)
            if restored:
                return restored

        regime_u = str(regime or "").strip().upper()
        if regime_u == "TREND":
            return "TREND_ENTRY"
        if regime_u == "REVERSAL":
            return "REVERSAL_ENTRY"
        return "-"

    def _telegram_today_trade_counts(self, as_of: datetime | None = None) -> dict[str, int]:
        """Count today's completed trades by the entry strategy regime."""
        date_key = (as_of if isinstance(as_of, datetime) else datetime.now()).strftime("%Y-%m-%d")
        counts = {"TREND": 0, "REVERSAL": 0, "UNKNOWN": 0}
        try:
            names = sorted(os.listdir(self._report_dir))
        except OSError:
            names = []
        for name in names:
            if not (name.startswith("trade_log_") and name.endswith(".csv")):
                continue
            try:
                with open(os.path.join(self._report_dir, name), "r", encoding="utf-8-sig") as handle:
                    for row in csv.DictReader(handle):
                        if str(row.get("날짜") or "").strip() != date_key:
                            continue
                        if str(row.get("구분") or "").strip().upper() != "EXIT":
                            continue
                        regime = ZenithZScoreController._regime_from_text_hints(
                            row.get("entry_regime"), row.get("사유"), row.get("entry_hl_source")
                        )
                        key = regime if regime in ("TREND", "REVERSAL") else "UNKNOWN"
                        counts[key] += 1
            except (OSError, UnicodeError, csv.Error):
                continue
        if str(getattr(self, "_telegram_balance_confirmed_trade_counts_date", "") or "") == date_key:
            extras = getattr(self, "_telegram_balance_confirmed_trade_counts", {}) or {}
            for key in counts:
                try:
                    counts[key] += int(extras.get(key, 0) or 0)
                except (TypeError, ValueError):
                    pass
        return counts

    def _telegram_record_balance_confirmed_trade(self, regime: str, as_of: datetime | None = None) -> None:
        date_key = (as_of if isinstance(as_of, datetime) else datetime.now()).strftime("%Y-%m-%d")
        if str(getattr(self, "_telegram_balance_confirmed_trade_counts_date", "") or "") != date_key:
            self._telegram_balance_confirmed_trade_counts_date = date_key
            self._telegram_balance_confirmed_trade_counts = {"TREND": 0, "REVERSAL": 0, "UNKNOWN": 0}
        key = str(regime or "").strip().upper()
        if key not in ("TREND", "REVERSAL"):
            key = "UNKNOWN"
        self._telegram_balance_confirmed_trade_counts[key] = int(
            self._telegram_balance_confirmed_trade_counts.get(key, 0) or 0
        ) + 1

    def _telegram_send_execution_report(
        self,
        *,
        report_type: str,
        side: str,
        qty: int,
        fill_price: float,
        reason: str = "",
        pnl_pt: float | None = None,
        entry_price: float | None = None,
        confirmation_source: str = "",
        entry_regime: str = "",
    ) -> None:
        report_type_u = str(report_type or "").strip().upper()
        side_u = str(side or "").strip().upper()
        report_key = f"{report_type_u}|{side_u}|{int(qty or 0)}"
        now_ts = time.time()
        if (
            report_key == str(getattr(self, "_telegram_last_execution_report_key", "") or "")
            and (now_ts - float(getattr(self, "_telegram_last_execution_report_ts", 0.0) or 0.0)) < 15.0
        ):
            self.log("TELEGRAM", "EXEC_REPORT_DEDUP", f"key={report_key} source={confirmation_source or 'CALLBACK'}")
            return
        self._telegram_last_execution_report_key = report_key
        self._telegram_last_execution_report_ts = now_ts
        side_kr = "LONG" if side_u == "LONG" else ("SHORT" if side_u == "SHORT" else (side_u or "-"))
        regime = ZenithZScoreController._telegram_entry_regime(self, entry_regime)
        regime_label = ZenithZScoreController._telegram_regime_label(regime)
        entry_reason = ZenithZScoreController._telegram_entry_reason(self, reason, regime)
        lines = [
            ("🏁 청산" if report_type_u == "EXIT" else "🟦 진입") + (" 체결(잔고확정)" if confirmation_source else ""),
            f"시각={datetime.now():%Y-%m-%d %H:%M:%S}",
            f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}",
            f"종목={self.live_code}",
            f"방향={side_kr}",
            f"수량={int(qty or 0)}",
            f"{'확인가' if confirmation_source else '체결가'}={float(fill_price or 0.0):.2f}",
        ]
        if entry_price is not None and float(entry_price or 0.0) > 0.0:
            lines.append(f"진입가={float(entry_price):.2f}")
        if report_type_u == "EXIT":
            lines.append(f"진입유형={regime_label}")
            lines.append(f"청산사유={str(reason or '-').strip() or '-'}")
            if pnl_pt is not None:
                lines.append(f"손익={float(pnl_pt):+,.2f}pt")
            counts_reader = getattr(self, "_telegram_today_trade_counts", None)
            counts = (
                counts_reader()
                if callable(counts_reader)
                else ZenithZScoreController._telegram_today_trade_counts(self)
            )
            total_count = int(sum(int(value or 0) for value in counts.values()))
            lines.append(f"당일손익={(self._telegram_today_realized_pnl() + float(getattr(self, '_telegram_balance_confirmed_pnl_pt', 0.0) or 0.0)):+,.2f}pt")
            count_text = (
                f"당일청산체결=합계 {total_count}건 "
                f"(트렌드형 {int(counts.get('TREND', 0) or 0)}건 / "
                f"반전형 {int(counts.get('REVERSAL', 0) or 0)}건)"
            )
            unknown_count = int(counts.get("UNKNOWN", 0) or 0)
            if unknown_count > 0:
                count_text += f" / 미확인 {unknown_count}건"
            lines.append(count_text)
        else:
            lines.append(f"진입유형={regime_label} ({regime})")
            lines.append(f"진입사유={entry_reason}")
        if confirmation_source:
            lines.append(f"확인경로={confirmation_source}")
        if float(self.current_price or 0.0) > 0.0:
            lines.append(f"현재가={float(self.current_price):.2f}")
        self._telegram_queue(f"{report_type_u.lower()}_report", "\n".join(lines))
        self.log(
            "TELEGRAM",
            "QUEUE",
            f"{report_type_u.lower()}_report side={side_u or '-'} qty={int(qty or 0)} "
            f"regime={regime} reason={(entry_reason if report_type_u == 'ENTRY' else (str(reason or '-').strip() or '-'))}",
        )

    def _telegram_send_daily_summary(self, reason: str) -> None:
        summary_key = f"{datetime.now():%Y-%m-%d}|{reason}"
        if self._telegram_summary_sent_key == summary_key:
            return
        self._telegram_summary_sent_key = summary_key
        trades = 0
        wins = 0
        losses = 0
        pnl_pt = 0.0
        try:
            with open(self._trade_log_path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("구분") or "").strip().upper() != "EXIT":
                        continue
                    trades += 1
                    try:
                        pnl = float(row.get("손익(pt)") or 0.0)
                    except Exception:
                        pnl = 0.0
                    pnl_pt += pnl
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
        except Exception:
            pass
        win_rate = f"{(wins / trades * 100.0):.1f}%" if trades > 0 else "알수없음"
        ending_position = "없음" if self.position_side == "FLAT" or int(self.position_qty or 0) <= 0 else f"{self.position_side}/{int(self.position_qty or 0)}"
        self._telegram_queue(
            "daily_summary",
            f"📊 일일요약\n"
            f"{datetime.now():%Y-%m-%d}: {pnl_pt:+.2f}pt\n"
            f"거래={trades} 승률={win_rate}\n"
            f"종료포지션={ending_position}\n"
            f"사유={reason or 'NORMAL_CLOSE'}",
        )
        self.log("TELEGRAM", "QUEUE", f"daily_summary reason={reason or 'NORMAL_CLOSE'} trades={trades}")

    @staticmethod
    def _parse_watchdog_hhmm(value: Any) -> tuple[int, int] | None:
        text = str(value or "").strip()
        parts = text.split(":")
        if len(parts) != 2:
            return None
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except Exception:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    @staticmethod
    def _shift_watchdog_hhmm(value: Any, delta_min: int) -> str:
        parsed = ZenithZScoreController._parse_watchdog_hhmm(value)
        if parsed is None:
            return ""
        hour, minute = parsed
        total = (hour * 60 + minute + int(delta_min)) % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"

    def _load_watchdog_schedule_defaults(self) -> dict[str, str]:
        defaults = {
            "maintenance_start": "",
            "maintenance_end": "",
            "shutdown_before_min": "",
            "restart_after_min": "",
        }
        launcher_path = os.path.join(self._base_dir, "zenith_watchdog_launcher_PC_READY_FIXED.py")
        try:
            with open(launcher_path, "r", encoding="utf-8") as f:
                text = f.read()
            patterns = {
                "maintenance_start": r'^\s*MAINTENANCE_START\s*=\s*"([^"]+)"',
                "maintenance_end": r'^\s*MAINTENANCE_END\s*=\s*"([^"]+)"',
                "shutdown_before_min": r"^\s*SHUTDOWN_BEFORE_MIN\s*=\s*([0-9]+)",
                "restart_after_min": r"^\s*RESTART_AFTER_MIN\s*=\s*([0-9]+)",
            }
            for key, pattern in patterns.items():
                match = re.search(pattern, text, re.MULTILINE)
                if match:
                    defaults[key] = str(match.group(1) or "").strip()
        except Exception:
            pass
        return defaults

    def _init_watchdog_dashboard_state(self, requested_server: str) -> None:
        env = os.environ
        schedule_defaults = self._load_watchdog_schedule_defaults()
        self._watchdog_mode = bool(
            self.runtime_resume.get("launched_by_watchdog")
            or self.runtime_resume.get("resume_flag")
            or self.runtime_resume.get("resume")
        )
        self._watchdog_server_mode = _normalize_server_mode(
            env.get("ZENITH_SERVER_MODE") or requested_server or self.runtime_resume.get("server"),
            default="PAPER",
        )
        auto_restore_env = env.get("ZENITH_AUTO_RESTORE")
        if auto_restore_env is None or str(auto_restore_env).strip() == "":
            self._watchdog_auto_restore = bool(self.runtime_resume.get("auto_restore"))
        else:
            self._watchdog_auto_restore = str(auto_restore_env).strip().lower() in {"1", "true", "y", "yes", "on"}
        self._watchdog_maintenance_start = str(
            env.get("ZENITH_WATCHDOG_MAINTENANCE_START") or schedule_defaults.get("maintenance_start") or ""
        ).strip()
        self._watchdog_maintenance_end = str(
            env.get("ZENITH_WATCHDOG_MAINTENANCE_END") or schedule_defaults.get("maintenance_end") or ""
        ).strip()
        try:
            self._watchdog_shutdown_before_min = int(
                str(env.get("ZENITH_WATCHDOG_SHUTDOWN_BEFORE_MIN") or schedule_defaults.get("shutdown_before_min") or "0").strip() or "0"
            )
        except Exception:
            self._watchdog_shutdown_before_min = 0
        try:
            self._watchdog_restart_after_min = int(
                str(env.get("ZENITH_WATCHDOG_RESTART_AFTER_MIN") or schedule_defaults.get("restart_after_min") or "0").strip() or "0"
            )
        except Exception:
            self._watchdog_restart_after_min = 0
        self._watchdog_auto_shutdown_time = str(env.get("ZENITH_WATCHDOG_AUTO_SHUTDOWN_TIME") or "").strip()
        self._watchdog_auto_restart_time = str(env.get("ZENITH_WATCHDOG_AUTO_RESTART_TIME") or "").strip()
        if not self._watchdog_auto_shutdown_time:
            self._watchdog_auto_shutdown_time = self._shift_watchdog_hhmm(
                self._watchdog_maintenance_start,
                -self._watchdog_shutdown_before_min,
            )
        if not self._watchdog_auto_restart_time:
            self._watchdog_auto_restart_time = self._shift_watchdog_hhmm(
                self._watchdog_maintenance_end,
                self._watchdog_restart_after_min,
            )
        if self._watchdog_mode:
            self._watchdog_status_text = (
                f"워치독 실행중 | {self._watchdog_server_mode} | 자동종료 {self._watchdog_auto_shutdown_time or '--:--'} | "
                f"자동재접속 {self._watchdog_auto_restart_time or '--:--'} | AUTO복원 {'ON' if self._watchdog_auto_restore else 'OFF'}"
            )
        else:
            self._watchdog_status_text = "수동 실행"
        self.log(
            "WATCHDOG",
            "DASHBOARD_STATUS",
            f"enabled={1 if self._watchdog_mode else 0} server={self._watchdog_server_mode} "
            f"auto_restore={1 if self._watchdog_auto_restore else 0} "
            f"shutdown={self._watchdog_auto_shutdown_time or '--:--'} restart={self._watchdog_auto_restart_time or '--:--'}",
        )

    def _init_resume_runtime(self) -> None:
        requested_server = str(self.runtime_resume.get("server") or "").strip().upper()
        if requested_server in ("PAPER", "LIVE"):
            try:
                self.cfg.server_mode = requested_server
            except Exception:
                pass
        self.log(
            "RESUME",
            "ARGS",
            f"resume={bool(self.runtime_resume.get('resume_flag'))} auto_restore={bool(self.runtime_resume.get('auto_restore'))} "
            f"no_dialog={bool(self.runtime_resume.get('no_dialog'))} server={requested_server or '-'} "
            f"invalid_server={self.runtime_resume.get('invalid_server') or '-'} unknown={self.runtime_resume.get('unknown_args') or []}",
        )
        if self.runtime_resume.get("invalid_server"):
            self.log("RESUME", "SERVER_MODE", f"PAPER source=default invalid={self.runtime_resume.get('invalid_server')}")
        self.log(
            "RESUME",
            "STATE_LOADED",
            f"path={self._resume_state_path} auto_was_on={bool(self._resume_state.get('auto_was_on', False))} "
            f"last_server_mode={self._resume_state.get('last_server_mode', '-')}",
        )
        self._init_watchdog_dashboard_state(requested_server)
        self._resume_auto_restore_pending = bool(
            bool(self.runtime_resume.get("auto_restore"))
            or (bool(self.runtime_resume.get("resume_flag")) and bool(self._resume_state.get("auto_was_on", False)))
        )
        self._resume_auto_restore_attempted = False
        if self._resume_auto_restore_pending:
            self.log("RESUME", "AUTO_RESTORE_PENDING", "pending=1")
            self._set_resume_status("자동복원 준비", log_message=True)

    @staticmethod
    def _normalize_shutdown_reason(reason: str) -> str:
        text = str(reason or "").strip().upper()
        if "MAINT" in text:
            return "KIS_MAINTENANCE"
        if "WATCHDOG" in text or "CTRL_BREAK" in text or "SIGTERM" in text or "SIGINT" in text or "BREAK" in text:
            return "WATCHDOG"
        return "NORMAL"

    def _save_resume_state(self, reason: str = "NORMAL", force: bool = False) -> None:
        if self._resume_state_saved and not force:
            return
        server_for_state = _normalize_server_mode(self.server_type or self.runtime_resume.get("server"), default="PAPER")
        account_for_state = _normalize_account_no(self.account)
        account_by_server = {}
        try:
            old_map = self._resume_state.get("last_account_by_server") if isinstance(self._resume_state, dict) else {}
            if isinstance(old_map, dict):
                account_by_server.update({str(k).upper(): _normalize_account_no(v) for k, v in old_map.items() if _normalize_account_no(v)})
        except Exception:
            pass
        if account_for_state:
            account_by_server[server_for_state] = account_for_state
        payload = {
            "auto_was_on": bool(self.auto_on),
            "last_server_mode": server_for_state,
            "last_account": account_for_state,
            "last_account_by_server": account_by_server,
            "last_paper_account": account_by_server.get("PAPER", ""),
            "last_live_account": account_by_server.get("LIVE", ""),
            "code": str(self.live_code or ""),
            "qty": int(self.position_qty or 0),
            "shutdown_reason": self._normalize_shutdown_reason(reason),
            "saved_at": now_str(),
        }
        try:
            os.makedirs(os.path.dirname(self._resume_state_path), exist_ok=True)
            with open(self._resume_state_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self._resume_state_saved = True
            self._resume_state = dict(payload)
            self.log("RESUME", "STATE_SAVE", f"path={self._resume_state_path} auto={payload['auto_was_on']} server={payload['last_server_mode']} reason={payload['shutdown_reason']}")
        except Exception as e:
            self.log("RESUME", "STATE_SAVE_ERROR", f"path={self._resume_state_path} error={e}")

    def _shutdown_step(self, step: str, fn) -> None:
        self.log("RESUME", "SHUTDOWN_STEP", f"step={step}")
        try:
            fn()
        except Exception as e:
            self.log("RESUME", "SHUTDOWN_STEP_ERROR", f"step={step} error={e}")

    def request_safe_shutdown(self, reason: str = "NORMAL") -> None:
        if getattr(self, "_shutdown_in_progress", False):
            return
        self._shutdown_in_progress = True
        self._shutdown_reason = str(reason or "NORMAL")
        _write_process_diagnostic(
            "REQUEST_SAFE_SHUTDOWN",
            f"reason={self._shutdown_reason} auto_on={bool(self.auto_on)} connected={bool(self.connected)} server={self.server_type or self._watchdog_server_mode or '-'}",
            base_dir=self._base_dir,
        )
        try:
            self.dashboard._skip_close_confirm = True
        except Exception:
            pass
        self.log("RESUME", "WATCHDOG_SHUTDOWN_REQUEST", f"reason={self._shutdown_reason}")
        if not self._telegram_sent_shutdown:
            self._telegram_sent_shutdown = True
            self._telegram_queue(
                "shutdown",
                f"🛑 WATCH DOG 종료\n"
                f"사유={self._shutdown_reason}\n"
                f"AUTO={bool(self.auto_on)}\n"
                f"서버={self.server_type or self._watchdog_server_mode or 'PAPER'}\n"
                f"시각={now_str()}",
            )
            self.log("TELEGRAM", "QUEUE", f"shutdown reason={self._shutdown_reason}")
        self._telegram_send_daily_summary(self._shutdown_reason)
        try:
            if self._telegram_notifier is not None:
                flushed = self._telegram_notifier.wait_until_idle(timeout_sec=1.5)
                self.log("TELEGRAM", "DRAIN", f"shutdown flushed={flushed}")
        except Exception as e:
            self.log("TELEGRAM", "SEND_ERROR", f"event=shutdown_drain type={type(e).__name__} status=- message={e}")
        self._set_resume_status("자동종료 준비", log_message=True)
        self._shutdown_step("save_state", lambda: self._save_resume_state(self._shutdown_reason, force=True))
        self._set_resume_status("상태 저장 후 종료", log_message=True)
        self._shutdown_step("disable_auto", lambda: setattr(self, "auto_on", False))
        self._shutdown_step("stop_main_timer", lambda: self.timer.stop() if getattr(self, "timer", None) is not None else None)
        self._shutdown_step("disconnect_market", lambda: self.client.unsubscribe_market() if self.real_registered else None)
        self._shutdown_step("disconnect_api", self.client.disconnect_api)
        self.real_registered = False
        self._shutdown_step("refresh_view", lambda: self.refresh_view(force=True))
        self._shutdown_step("close_dashboard", lambda: self.dashboard.close() if self.dashboard is not None else None)
        self._shutdown_step("quit_app", lambda: QTimer.singleShot(0, self.app.quit) if self.app is not None else None)
        _force_process_exit_later(3.0)
        self._shutdown_done = True
        self.log("RESUME", "SHUTDOWN_DONE", f"reason={self._shutdown_reason}")
        _write_process_diagnostic(
            "SHUTDOWN_DONE",
            f"reason={self._shutdown_reason} auto_on={bool(self.auto_on)} connected={bool(self.connected)}",
            base_dir=self._base_dir,
        )

    def on_app_about_to_quit(self) -> None:
        _write_process_diagnostic(
            "ABOUT_TO_QUIT",
            f"shutdown_in_progress={bool(self._shutdown_in_progress)} reason={self._shutdown_reason or '-'}",
            base_dir=self._base_dir,
        )
        if not self._shutdown_in_progress:
            self.request_safe_shutdown("ABOUT_TO_QUIT")
            return
        try:
            self._save_resume_state(self._shutdown_reason or "ABOUT_TO_QUIT")
        except Exception:
            pass

    def _maybe_restore_auto_after_warmup(self) -> None:
        if not getattr(self, "_resume_auto_restore_pending", False):
            return
        if getattr(self, "_resume_auto_restore_attempted", False):
            return
        if not self.connected:
            return
        if not self.warmup_done:
            self.log("RESUME", "WARMUP_WAIT", "ready=False")
            return
        self._resume_auto_restore_attempted = True
        try:
            if not self.auto_on:
                self.on_auto_clicked()
            if self.auto_on:
                self.log("RESUME", "AUTO_RESTORED", f"code={self.live_code} qty={self.position_qty}")
                self._telegram_send_auto_restore_status(
                    "RESTORED",
                    reason=self._reconnect_last_reason or "WARMUP_COMPLETE",
                    extra=f"code={self.live_code}\nqty={self.position_qty}",
                )
                if self._watchdog_mode:
                    self._telegram_send_auto_restored()
                    self._telegram_send_position_sync("auto_restored")
                self._set_resume_status("AUTO ON 복원", log_message=True)
                self._set_resume_status("장전 대기중", log_message=True)
                self._reconnect_last_reason = ""
            else:
                self.log("RESUME", "AUTO_RESTORE_FAILED", "auto_on remained False")
                self._telegram_send_auto_restore_status(
                    "FAILED",
                    reason=self._reconnect_last_reason or "AUTO_ON_FALSE",
                    extra=f"code={self.live_code}\nqty={self.position_qty}",
                )
                self._set_resume_status("자동복원 실패 - 수동확인", log_message=True)
        except Exception as e:
            self.log("RESUME", "AUTO_RESTORE_FAILED", str(e))
            self._telegram_send_auto_restore_status("FAILED", reason=self._reconnect_last_reason or "EXCEPTION", extra=str(e))
            self._set_resume_status("자동복원 실패 - 수동확인", log_message=True)

    def _today_yyyymmdd(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _is_krx_holiday(self, dt=None):
        if dt is None:
            dt = datetime.now()
        date_key = dt.strftime("%Y-%m-%d")
        holidays = getattr(self.cfg, "KRX_HOLIDAYS_2026", set())
        return date_key in holidays

    def _krx_holiday_reason(self, dt=None):
        if dt is None:
            dt = datetime.now()
        date_key = dt.strftime("%Y-%m-%d")
        if date_key == "2026-05-01":
            return "KRX_HOLIDAY_LABOR_DAY"
        if date_key in getattr(self.cfg, "KRX_HOLIDAYS_2026", set()):
            return "KRX_HOLIDAY"
        return ""

    def _is_krx_closed_day(self, dt=None) -> bool:
        if dt is None:
            dt = datetime.now()
        return bool(dt.weekday() >= 5 or self._is_krx_holiday(dt))

    def _market_phase_label(self, dt=None) -> str:
        if dt is None:
            dt = datetime.now()
        if self._is_krx_closed_day(dt):
            return "휴장"
        hhmm = dt.hour * 100 + dt.minute
        if hhmm < 830:
            return "장전"
        if hhmm < 845:
            return "동호가"
        if hhmm < 1535:
            return "장중"
        if hhmm < 1545:
            return "장마감전"
        return "장마감"

    def _is_preopen_market_phase(self, dt=None) -> bool:
        return bool(self._market_phase_label(dt) == "장전")

    def _connection_recovery_policy(self, dt=None) -> str:
        phase = self._market_phase_label(dt)
        if phase == "장중":
            return "MIDDAY_TR_AND_KIS_EXECUTION"
        if phase in {"장전", "동호가", "휴장"}:
            return "TR_ONLY"
        return "OFF"

    def _market_transport_status(self) -> str:
        if self.connected:
            if self.warmup_loading:
                return "CONNECTED / WARMUP"
            age = time.time() - float(self.last_tick_ts or 0.0)
            return "CONNECTED / WAIT_TICK" if age > 45 else "CONNECTED / LIVE"
        return "DISCONNECTED"

    def _market_status_text(self, dt=None) -> str:
        phase = self._market_phase_label(dt)
        transport = self._market_transport_status()
        return f"{phase} / {transport}"

    def _emit_market_holiday_log(self, dt=None) -> None:
        if dt is None:
            dt = datetime.now()
        if not self._is_krx_holiday(dt):
            self._last_market_holiday_log_date = ""
            return
        date_key = dt.strftime("%Y-%m-%d")
        if self._last_market_holiday_log_date == date_key:
            return
        self._last_market_holiday_log_date = date_key
        self.log("MARKET", "MARKET_HOLIDAY_DETECTED", f"date={date_key} reason={self._krx_holiday_reason(dt)}")

    def _init_runtime_files(self) -> None:
        self._ensure_csv_header(self._trade_log_path, [
            "날짜", "시간", "구분", "방향", "수량", "체결가",
            "진입가", "손익(pt)", "사유", "Z스코어",
            "entry_prev_z5", "entry_dz5", "entry_dz5_threshold", "entry_dz5_direction_ok",
            "entry_macd_osci", "exit_macd_osci",
            "entry_regime", "entry_side", "entry_basis", "entry_hl_source", "entry_hit_mode", "entry_cross_through", "entry_validation",
            "confirmed5_high", "confirmed5_low", "sample_high", "sample_low", "effective_high", "effective_low",
            "arm_anchor", "arm_level", "fire_band_min", "fire_band_max", "prev_price", "current_price",
            "trend_confirmed5_high", "trend_confirmed5_low", "trend_used_currentbar_hl", "trend_arm_level", "trend_fire_min", "trend_fire_max", "trend_hit_mode", "trend_cross_through",
            "reversal_confirmed5_high", "reversal_confirmed5_low", "reversal_sample_high", "reversal_sample_low", "reversal_effective_high", "reversal_effective_low",
            "reversal_sample_interval_sec", "reversal_sample_countdown_sec", "reversal_new_high_hit", "reversal_new_low_hit",
            "reversal_arm_anchor", "reversal_fire_min", "reversal_fire_max", "reversal_hit_mode", "reversal_cross_through",
            "order_sent", "order_type", "order_price", "order_qty", "order_result_code", "order_result_msg",
            "execution_notice_received", "filled_qty", "filled_price", "unfilled_qty", "final_order_state"
        ])
        self._ensure_csv_header(self._event_log_csv_path, ["날짜", "시간", "kind", "code", "message"])
        self._ensure_csv_header(self._live_bar_path, ["date", "time", "code", "open", "high", "low", "close", "volume", "source"])
        self._ensure_csv_header(
            self._live_zscore_path,
            [
                "date",
                "time",
                "code",
                "close",
                "z1",
                "z5",
                "z30",
                "regime",
                "bars_1m",
                "bars_5m",
                "bars_30m",
                "source",
                "stale_tick_skip_count",
                "same_minute_update_count",
                "new_minute_append_count",
                "dedupe_1m_before",
                "dedupe_1m_after",
                "dedupe_5m_input_before",
                "dedupe_5m_input_after",
                "dedupe_30m_input_before",
                "dedupe_30m_input_after",
                "last_1m_minute_key",
                "last_5m_bucket_key",
                "last_30m_bucket_key",
            ],
        )
        self._ensure_csv_header(self._warmup_bar_raw_path, ["date", "time", "code", "open", "high", "low", "close", "volume", "source", "seq"])
        self._ensure_csv_header(self._warmup_bar_path, ["date", "time", "code", "open", "high", "low", "close", "volume", "source", "seq"])
        self._ensure_csv_header(self._warmup_zscore_path, ["date", "time", "code", "close", "z1", "z5", "z30", "regime", "bars_1m", "bars_5m", "bars_30m", "source", "seq"])
        self._ensure_csv_header(self._warmup_summary_path, ["session_id", "source", "target_rows", "loaded_rows", "bars_1m", "bars_5m", "bars_30m", "ready", "last_dt", "last_close"])
        self._ensure_csv_header(self._warmup_gap_report_path, ["session_id", "source", "raw_rows", "used_rows", "filled_rows", "gap_segments", "max_gap_min"])
        self._ensure_csv_header(
            self._signal_log_path,
            [
                "date", "time", "code", "position_side", "position_qty", "last_signal", "signal_reason",
                "z1", "z5", "z30", "regime", "exit_z", "exit_ok", "short_entry_blocked", "short_block_reason",
                "warmup_seed_z5", "prev_z5", "prev_z5_valid", "continuity_valid", "dz5",
                "dz5_long", "dz5_short", "prev_dz5_long", "prev_dz5_short",
                "dz5_low_anchor_z5", "dz5_high_anchor_z5", "z5_low_5", "z5_high_5",
                "continuity_reset_reason", "continuity_seed_reason", "reentry_continuity_mode",
                "dz5_commit_mode", "update_prev_state",
                "long_armed", "short_armed", "long_arm_age", "short_arm_age",
                "long_arm_trigger_hit", "short_arm_trigger_hit",
                "entry_price_fire_long_hit", "entry_price_fire_short_hit",
                "long_arm_z5_low_anchor", "short_arm_z5_high_anchor",
                "long_arm_threshold", "long_fire_threshold", "long_fire_value",
                "short_arm_threshold", "short_fire_threshold", "short_fire_value",
                "long_entry_direction_ok", "short_entry_direction_ok",
                "long_band_ok", "short_band_ok", "long_entry_ready", "short_entry_ready",
                "z5_band_state", "static_z5_band_enabled_for_entry", "entry_reason", "entry_type",
                "price_dist_prev5_high", "price_dist_prev5_low", "price_dist_prev10_high", "price_dist_prev10_low",
                "entry_price_range_pt", "entry_price_range_ok",
                "long_ok", "short_ok", "reverse_long_ok", "reverse_short_ok",
                "reverse_long_entry_gate", "reverse_short_entry_gate",
                "exit_z_tp_enabled", "exit_z_sl_enabled", "final_signal", "allow_trade", "block_reason", "arm_cancel_reason",
                "entry_price", "current_price", "current_pnl_pt", "holding_seconds",
                "exit_z_min_hold_sec", "exit_z_time_gate_ok", "exit_z_block_reason",
                "peak_price", "trough_price", "mfe_pt", "mae_pt", "retracement_pt", "mfe_stage",
                "fixed_loss_hit", "reverse_signal_raw", "reverse_signal_allowed",
                "mfe_protect_hit", "mfe_protect_exit_price",
                "exit_z_raw", "exit_z_allowed", "mfe_trail_hit",
                "exit_candidates", "selected_exit_reason",
                "entry_side", "entry_basis", "entry_hl_source", "entry_hit_mode", "entry_cross_through", "entry_validation",
                "confirmed5_high", "confirmed5_low", "sample_high", "sample_low", "effective_high", "effective_low",
                "arm_anchor", "arm_level", "fire_band_min", "fire_band_max", "prev_price", "current_price",
                "trend_confirmed5_high", "trend_confirmed5_low", "trend_used_currentbar_hl", "trend_arm_level", "trend_fire_min", "trend_fire_max", "trend_hit_mode", "trend_cross_through",
                "reversal_confirmed5_high", "reversal_confirmed5_low", "reversal_sample_high", "reversal_sample_low", "reversal_effective_high", "reversal_effective_low",
                "reversal_sample_interval_sec", "reversal_sample_countdown_sec", "reversal_new_high_hit", "reversal_new_low_hit",
                "reversal_arm_anchor", "reversal_fire_min", "reversal_fire_max", "reversal_hit_mode", "reversal_cross_through",
                "sma10s_5", "sma10s_10", "sma10s_20", "sma10s_relation", "sma10s_regime",
                "sma10s_source", "sma10s_is_approx", "sma10s_bucket_time", "sma10s_event",
                "sma10s_event_price", "sma10s_event_previous_price", "sma10s_event_previous_line",
                "sma10s_event_current_line", "sma10s_touch_direction",
                "sma1m_5", "sma1m_10", "sma1m_20", "sma1m_relation", "sma1m_regime",
                "sma1m_source", "sma1m_event", "sma1m_event_price", "sma1m_event_line",
                "sma1m_event_offset", "sma1m_touch_direction",
            ],
        )
        self._ensure_csv_header(self._state_report_path, ["date", "time", "code", "state", "fast", "slow", "diff", "threshold", "final"])
        self._ensure_csv_header(
            self._exit_trace_path,
            [
                "date", "time", "event", "code", "position_side", "position_qty",
                "entry_price", "entry_z5", "exit_reason", "exit_rule",
                "exit_source", "exit_signal", "exit_threshold",
                "exit_z_long", "exit_z_short", "exit_delta",
                "bar_high", "bar_low", "bar_close", "fill_price", "pnl_pt",
            ],
        )
        try:
            manifest = {
                "session_id": self._session_id,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "live_code": self.live_code,
                "history_code": self.history_code,
                "runtime_dir": self._runtime_dir,
                "data_dir": self._data_dir,
                "report_dir": self._report_dir,
            }
            with open(self._run_manifest_path, "w", encoding="utf-8") as _mf:
                json.dump(manifest, _mf, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _ensure_csv_header(path: str, header: list[str]) -> None:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as _f:
            csv.writer(_f).writerow(header)

    @staticmethod
    def _fmt_num(v: object, digits: int = 6) -> str:
        if v is None:
            return ""
        try:
            return f"{float(v):.{digits}f}"
        except Exception:
            return str(v)

    @staticmethod
    def _mfe_profile_from_entry_context(ctx: dict | None) -> str:
        ctx = ctx if isinstance(ctx, dict) else {}
        raw = (
            ctx.get("mfe_profile")
            or ctx.get("entry_mfe_profile")
            or ctx.get("entry_regime")
            or ctx.get("active_entry_regime")
            or ctx.get("regime_at_entry")
            or ""
        )
        reason = str(ctx.get("entry_reason") or ctx.get("reason") or "").strip().upper()
        text = str(raw or "").strip().upper()
        if text.startswith("TREND") or reason.startswith("TREND_") or "TREND_PRICE" in reason:
            return "TREND"
        if text.startswith("REVERSAL") or reason.startswith("REVERSAL_") or "REVERSAL_PRICE" in reason:
            return "REVERSAL"
        return "REVERSAL"

    def _effective_position_mfe_profile(self) -> str:
        entry_regime = str(self.position_entry_regime or "").strip().upper()
        if entry_regime in ("TREND", "REVERSAL"):
            return entry_regime
        profile = str(self.position_mfe_profile or "").strip().upper()
        if profile in ("TREND", "REVERSAL"):
            return profile
        return ""

    @staticmethod
    def _regime_from_text_hints(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip().upper()
            if not text:
                continue
            if text in ("TREND", "REVERSAL", "BLOCKED"):
                return text
            if text.startswith("TREND_") or "TREND_PRICE" in text or "TREND_CONFIRMED" in text:
                return "TREND"
            if text.startswith("REVERSAL_") or text.startswith("REV_") or "REVERSAL_PRICE" in text:
                return "REVERSAL"
        return ""

    def _resolve_entry_regime(self, sig_state: dict | None, default: str = "") -> str:
        ss = sig_state if isinstance(sig_state, dict) else {}
        explicit_entry_regime = str(ss.get("entry_regime") or "").strip().upper()
        if explicit_entry_regime in ("TREND", "REVERSAL", "BLOCKED"):
            return explicit_entry_regime
        explicit_active_regime = str(ss.get("active_entry_regime") or "").strip().upper()
        if explicit_active_regime in ("TREND", "REVERSAL", "BLOCKED"):
            return explicit_active_regime
        regime = self._regime_from_text_hints(
            ss.get("long_arm_entry_regime"),
            ss.get("short_arm_entry_regime"),
            ss.get("entry_hl_source"),
            ss.get("trend_entry_price_anchor_source"),
            ss.get("reversal_entry_price_anchor_source"),
            ss.get("entry_reason"),
            ss.get("reason"),
            ss.get("regime_at_entry"),
        )
        if regime:
            return regime
        if bool(ss.get("trend_z5_ok", False)):
            if bool(ss.get("long_entry_ready", False) or ss.get("short_entry_ready", False)):
                return "TREND"
            if bool(ss.get("long_armed", False) or ss.get("short_armed", False)):
                long_regime = str(ss.get("long_arm_entry_regime") or "").strip().upper()
                short_regime = str(ss.get("short_arm_entry_regime") or "").strip().upper()
                if long_regime != "REVERSAL" and short_regime != "REVERSAL":
                    return "TREND"
        if bool(ss.get("reversal_z5_ok", False)):
            if bool(ss.get("reversal_long_candidate_countdown_sec") is not None or ss.get("reversal_short_candidate_countdown_sec") is not None):
                return "REVERSAL"
        default_text = str(default or "").strip().upper()
        if default_text in ("TREND", "REVERSAL", "BLOCKED"):
            return default_text
        return ""

    def _entry_observation_metrics(self, sig_state: dict | None, side_hint: str = "") -> dict[str, Any]:
        ss = sig_state if isinstance(sig_state, dict) else {}
        side_n = str(side_hint or "").upper()
        is_long = side_n == "LONG"
        regime = self._resolve_entry_regime(ss, default="")
        use_sma_entry = bool(ss.get("use_sma_cross_entry", getattr(self.cfg, "USE_SMA_CROSS_ENTRY", False)))
        sma_lane = ""
        if use_sma_entry:
            sma_lane = str(ss.get("sma10s_fire_lane") or "").strip().upper()
            if sma_lane in ("TREND", "REVERSAL"):
                regime = sma_lane
        use_price_entry = bool(ss.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
        use_z1_delta_entry = bool(ss.get("use_z1_delta_reversal_entry", False))
        use_z5_delta_entry = bool(ss.get("use_z5_delta_reversal_entry", False))
        basis = (
            "SMA1M" if use_sma_entry
            else "Z1_DELTA" if use_z1_delta_entry
            else "Z5_DELTA" if use_z5_delta_entry
            else ("PRICE" if use_price_entry else "DZ5")
        )
        prev_price = ss.get("prev_signal_price")
        current_price = ss.get("current_price")
        trend_cross = bool(ss.get("trend_long_cross_through", False) if is_long else ss.get("trend_short_cross_through", False))
        reversal_cross = bool(ss.get("reversal_long_cross_through", False) if is_long else ss.get("reversal_short_cross_through", False))
        trend_hit = bool(ss.get("entry_price_fire_long_hit", False) if is_long else ss.get("entry_price_fire_short_hit", False))
        reversal_hit = bool(ss.get("entry_price_fire_long_hit", False) if is_long else ss.get("entry_price_fire_short_hit", False))
        if regime == "TREND":
            confirmed5_high = ss.get("trend_confirmed5_high")
            confirmed5_low = ss.get("trend_confirmed5_low")
            sample_high = None
            sample_low = None
            effective_high = confirmed5_high
            effective_low = confirmed5_low
            hit_mode = ("CROSS_THROUGH" if trend_cross else ("IN_BAND" if trend_hit else ""))
            cross = trend_cross
            arm_anchor = ss.get("long_arm_z5_low_anchor") if is_long else ss.get("short_arm_z5_high_anchor")
            arm_level = ss.get("trend_entry_price_long_level") if is_long else ss.get("trend_entry_price_short_level")
            fire_band_min = ss.get("trend_entry_price_fire_long_min") if is_long else ss.get("trend_entry_price_fire_short_min")
            fire_band_max = ss.get("trend_entry_price_fire_long_max") if is_long else ss.get("trend_entry_price_fire_short_max")
            validation = "OK" if not bool(ss.get("trend_used_currentbar_hl", False)) else "FAIL_CURRENTBAR_LEAK"
        elif regime == "REVERSAL":
            confirmed5_high = ss.get("reversal_confirmed5_high")
            confirmed5_low = ss.get("reversal_confirmed5_low")
            sample_high = ss.get("reversal_sample_high")
            sample_low = ss.get("reversal_sample_low")
            effective_high = ss.get("reversal_effective_high")
            effective_low = ss.get("reversal_effective_low")
            hit_mode = ("CROSS_THROUGH" if reversal_cross else ("IN_BAND" if reversal_hit else ""))
            cross = reversal_cross
            arm_anchor = ss.get("long_arm_z5_low_anchor") if is_long else ss.get("short_arm_z5_high_anchor")
            arm_level = None
            fire_band_min = ss.get("reversal_entry_price_fire_long_min") if is_long else ss.get("reversal_entry_price_fire_short_min")
            fire_band_max = ss.get("reversal_entry_price_fire_long_max") if is_long else ss.get("reversal_entry_price_fire_short_max")
            validation = "OK"
            try:
                if (
                    effective_high is None or effective_low is None
                    or float(effective_high) != max(float(confirmed5_high), float(sample_high))
                    or float(effective_low) != min(float(confirmed5_low), float(sample_low))
                    or int(ss.get("reversal_live_sample_interval_sec", 10) or 10) != 10
                ):
                    validation = "FAIL_BAD_WINDOW"
            except Exception:
                validation = "FAIL_BAD_WINDOW"
        else:
            confirmed5_high = None
            confirmed5_low = None
            sample_high = None
            sample_low = None
            effective_high = None
            effective_low = None
            hit_mode = ""
            cross = False
            arm_anchor = None
            arm_level = None
            fire_band_min = None
            fire_band_max = None
            validation = "OK"
        if use_sma_entry:
            sma_prefix = "sma1m" if regime == "TREND" else "sma10s"
            sma_event = str(ss.get(f"{sma_prefix}_event") or "").strip().upper()
            expected_event = "REVERSAL_LONG_FIRE" if is_long else "REVERSAL_SHORT_FIRE"
            if regime == "TREND":
                expected_event = "TREND_1M_LONG_FIRE" if is_long else "TREND_1M_SHORT_FIRE"
            fire_event = bool(
                ss.get("sma10s_buy_fire_event", False)
                if is_long else ss.get("sma10s_sell_fire_event", False)
            )
            expected_stack = "BEAR" if (regime == "REVERSAL" and is_long) else "BULL" if regime == "REVERSAL" else "BULL" if is_long else "BEAR"
            sequence_ok = bool(
                fire_event
                and sma_event == expected_event
                and str(ss.get(f"{sma_prefix}_regime") or "").strip().upper() == expected_stack
            )
            hit_mode = f"{str(ss.get(f'{sma_prefix}_touch_direction') or '').strip().upper()}_TOUCH"
            cross = bool(sma_event)
            if regime == "TREND":
                arm_anchor = ss.get("sma1m_20")
                arm_level = ss.get("sma1m_20")
                fire_band_min = ss.get("sma1m_trend_buy_fire_min") if is_long else ss.get("sma1m_trend_sell_fire_min")
                fire_band_max = ss.get("sma1m_trend_buy_fire_max") if is_long else ss.get("sma1m_trend_sell_fire_max")
                prev_price = None
                current_price = ss.get("sma1m_event_price")
                validation = "OK_SMA1M_TREND_SEQUENCE" if sequence_ok else "WAIT_SMA1M_TREND_FIRE"
            else:
                arm_anchor = ss.get("sma10s_5")
                arm_level = ss.get("sma10s_5")
                fire_band_min = ss.get("sma10s_event_current_line")
                fire_band_max = ss.get("sma10s_event_current_line")
                prev_price = ss.get("sma10s_event_previous_price")
                current_price = ss.get("sma10s_event_price")
                validation = "OK_SMA10S_SEQUENCE" if sequence_ok else "WAIT_SMA10S_FIRE"
            if regime != "TREND" and sequence_ok and bool(ss.get("sma10s_is_approx", False)):
                validation += "_APPROX"
        elif use_z1_delta_entry:
            delta_fire = bool(
                ss.get("z1_delta_long_fire", False)
                if is_long else ss.get("z1_delta_short_fire", False)
            )
            hit_mode = "DELTA_FIRE" if delta_fire else ""
            cross = False
            arm_anchor = (
                ss.get("z1_delta_long_arm_value")
                if is_long else ss.get("z1_delta_short_arm_value")
            )
            arm_level = (
                ss.get("z1_delta_long_arm_price")
                if is_long else ss.get("z1_delta_short_arm_price")
            )
            fire_band_min = (
                ss.get("z1_delta_long_fire_price_min")
                if is_long else ss.get("z1_delta_short_fire_price_min")
            )
            fire_band_max = (
                ss.get("z1_delta_long_fire_price_max")
                if is_long else ss.get("z1_delta_short_fire_price_max")
            )
            validation = "OK_Z1_DELTA_SEQUENCE" if delta_fire else "WAIT_Z1_DELTA_FIRE"
        elif use_z5_delta_entry:
            delta_fire = bool(
                ss.get("z5_monitor_long_fire", False)
                if is_long else ss.get("z5_monitor_short_fire", False)
            )
            hit_mode = "DELTA_FIRE" if delta_fire else ""
            cross = False
            arm_anchor = (
                ss.get("z5_monitor_long_arm_value")
                if is_long else ss.get("z5_monitor_short_arm_value")
            )
            arm_level = (
                ss.get("z5_delta_long_arm_price")
                if is_long else ss.get("z5_delta_short_arm_price")
            )
            fire_band_min = (
                ss.get("z5_delta_long_fire_price_min")
                if is_long else ss.get("z5_delta_short_fire_price_min")
            )
            fire_band_max = (
                ss.get("z5_delta_long_fire_price_max")
                if is_long else ss.get("z5_delta_short_fire_price_max")
            )
            validation = "OK_Z5_DELTA_SEQUENCE" if delta_fire else "WAIT_Z5_DELTA_FIRE"
        elif hit_mode and arm_anchor is None:
            validation = "FAIL_FIRE_WITHOUT_ARM"
        return {
            "entry_regime": regime,
            "entry_side": side_n,
            "entry_basis": basis,
            "entry_hl_source": (
                str(ss.get("sma1m_source") or "SMA1M") if use_sma_entry and regime == "TREND"
                else str(ss.get("sma10s_source") or "SMA10S")
                if use_sma_entry
                else "Z1_DELTA_REVERSAL"
                if use_z1_delta_entry
                else "Z5_DELTA_CONTINUATION"
                if use_z5_delta_entry
                else str(ss.get("trend_entry_price_anchor_source") or "TREND_CONFIRMED_OC_BODY_HL_5")
                if regime == "TREND"
                else (str(ss.get("reversal_entry_price_anchor_source") or "REV_CONFIRMED3_PLUS_CURRENT_HL_15S_CANDIDATE") if regime == "REVERSAL" else "")
            ),
            "entry_hit_mode": hit_mode,
            "entry_cross_through": bool(cross),
            "entry_validation": validation,
            "confirmed5_high": confirmed5_high,
            "confirmed5_low": confirmed5_low,
            "sample_high": sample_high,
            "sample_low": sample_low,
            "effective_high": effective_high,
            "effective_low": effective_low,
            "arm_anchor": arm_anchor,
            "arm_level": arm_level,
            "fire_band_min": fire_band_min,
            "fire_band_max": fire_band_max,
            "prev_price": prev_price,
            "current_price": current_price,
            "trend_confirmed5_high": ss.get("trend_confirmed5_high"),
            "trend_confirmed5_low": ss.get("trend_confirmed5_low"),
            "trend_used_currentbar_hl": bool(ss.get("trend_used_currentbar_hl", False)),
            "trend_arm_level": ss.get("trend_entry_price_long_level") if is_long else ss.get("trend_entry_price_short_level"),
            "trend_fire_min": ss.get("trend_entry_price_fire_long_min") if is_long else ss.get("trend_entry_price_fire_short_min"),
            "trend_fire_max": ss.get("trend_entry_price_fire_long_max") if is_long else ss.get("trend_entry_price_fire_short_max"),
            "trend_hit_mode": ("CROSS_THROUGH" if trend_cross else ("IN_BAND" if trend_hit else "")),
            "trend_cross_through": bool(trend_cross),
            "reversal_confirmed5_high": ss.get("reversal_confirmed5_high"),
            "reversal_confirmed5_low": ss.get("reversal_confirmed5_low"),
            "reversal_sample_high": ss.get("reversal_sample_high"),
            "reversal_sample_low": ss.get("reversal_sample_low"),
            "reversal_effective_high": ss.get("reversal_effective_high"),
            "reversal_effective_low": ss.get("reversal_effective_low"),
            "reversal_sample_interval_sec": int(ss.get("reversal_live_sample_interval_sec", 10) or 10),
            "reversal_sample_countdown_sec": ss.get("reversal_live_sample_countdown_sec"),
            "reversal_new_high_hit": bool(ss.get("price_live_new_high_hit", False)),
            "reversal_new_low_hit": bool(ss.get("price_live_new_low_hit", False)),
            "reversal_arm_anchor": arm_anchor if regime == "REVERSAL" else None,
            "reversal_fire_min": ss.get("reversal_entry_price_fire_long_min") if is_long else ss.get("reversal_entry_price_fire_short_min"),
            "reversal_fire_max": ss.get("reversal_entry_price_fire_long_max") if is_long else ss.get("reversal_entry_price_fire_short_max"),
            "reversal_hit_mode": ("CROSS_THROUGH" if reversal_cross else ("IN_BAND" if reversal_hit else "")),
            "reversal_cross_through": bool(reversal_cross),
        }

    def _remember_entry_order_audit(self, ok: bool, side_hint: str, submit_reason: str) -> None:
        side_n = str(side_hint or "").upper()
        price_source = str(
            getattr(self.order_core, "last_submitted_price_source", "") if self.order_core else ""
        )
        order_type = (
            "CROSS_EVENT_LIMIT(KIS code=01)"
            if price_source == "SMA10S_CROSS_EVENT_PRICE"
            else str(self.order_core.exit_order_mode_text() if self.order_core else "LIMIT_L1(KIS code=01)")
        )
        self._last_entry_order_audit = {
            "order_sent": bool(ok),
            "order_type": order_type,
            "order_price": float(getattr(self.order_core, "last_submitted_price", 0.0) or 0.0) if self.order_core else 0.0,
            "order_price_source": price_source or "-",
            "order_qty": int(getattr(self, "order_qty", 1) or 1),
            "order_result_code": str(getattr(self.order_core, "last_order_result", "-") if self.order_core else "-"),
            "order_result_msg": str(submit_reason or "-"),
            "execution_notice_received": False,
            "filled_qty": 0,
            "filled_price": None,
            "unfilled_qty": 0,
            "final_order_state": str(getattr(self.order_core, "last_order_result", "-") if self.order_core else "-"),
            "entry_side": side_n,
        }
        try:
            obs = self._entry_observation_metrics(self.last_sig_state, side_hint=side_n)
            self.log(
                "SIGNAL",
                "ENTRY_AUDIT_SUBMIT",
                (
                    f"side={side_n or '-'} sent={int(bool(ok))} reason={submit_reason or '-'} "
                    f"regime={obs.get('entry_regime') or '-'} basis={obs.get('entry_basis') or '-'} "
                    f"hl={obs.get('entry_hl_source') or '-'} hit={obs.get('entry_hit_mode') or '-'} "
                    f"cross={int(bool(obs.get('entry_cross_through', False)))} "
                    f"validation={obs.get('entry_validation') or '-'}"
                ),
            )
        except Exception:
            pass

    def _entry_observation_csv_values(self, obs: dict[str, Any] | None) -> list[Any]:
        data = obs if isinstance(obs, dict) else {}
        return [
            str(data.get("entry_regime") or ""),
            str(data.get("entry_side") or ""),
            str(data.get("entry_basis") or ""),
            str(data.get("entry_hl_source") or ""),
            str(data.get("entry_hit_mode") or ""),
            int(bool(data.get("entry_cross_through", False))),
            str(data.get("entry_validation") or ""),
            self._fmt_num(data.get("confirmed5_high"), 4),
            self._fmt_num(data.get("confirmed5_low"), 4),
            self._fmt_num(data.get("sample_high"), 4),
            self._fmt_num(data.get("sample_low"), 4),
            self._fmt_num(data.get("effective_high"), 4),
            self._fmt_num(data.get("effective_low"), 4),
            self._fmt_num(data.get("arm_anchor"), 4),
            self._fmt_num(data.get("arm_level"), 4),
            self._fmt_num(data.get("fire_band_min"), 4),
            self._fmt_num(data.get("fire_band_max"), 4),
            self._fmt_num(data.get("prev_price"), 4),
            self._fmt_num(data.get("current_price"), 4),
            self._fmt_num(data.get("trend_confirmed5_high"), 4),
            self._fmt_num(data.get("trend_confirmed5_low"), 4),
            int(bool(data.get("trend_used_currentbar_hl", False))),
            self._fmt_num(data.get("trend_arm_level"), 4),
            self._fmt_num(data.get("trend_fire_min"), 4),
            self._fmt_num(data.get("trend_fire_max"), 4),
            str(data.get("trend_hit_mode") or ""),
            int(bool(data.get("trend_cross_through", False))),
            self._fmt_num(data.get("reversal_confirmed5_high"), 4),
            self._fmt_num(data.get("reversal_confirmed5_low"), 4),
            self._fmt_num(data.get("reversal_sample_high"), 4),
            self._fmt_num(data.get("reversal_sample_low"), 4),
            self._fmt_num(data.get("reversal_effective_high"), 4),
            self._fmt_num(data.get("reversal_effective_low"), 4),
            int(data.get("reversal_sample_interval_sec") or 0),
            (data.get("reversal_sample_countdown_sec") if data.get("reversal_sample_countdown_sec") is not None else ""),
            int(bool(data.get("reversal_new_high_hit", False))),
            int(bool(data.get("reversal_new_low_hit", False))),
            self._fmt_num(data.get("reversal_arm_anchor"), 4),
            self._fmt_num(data.get("reversal_fire_min"), 4),
            self._fmt_num(data.get("reversal_fire_max"), 4),
            str(data.get("reversal_hit_mode") or ""),
            int(bool(data.get("reversal_cross_through", False))),
        ]

    def _entry_order_audit_csv_values(self, audit: dict[str, Any] | None) -> list[Any]:
        data = audit if isinstance(audit, dict) else {}
        return [
            int(bool(data.get("order_sent", False))),
            str(data.get("order_type") or ""),
            self._fmt_num(data.get("order_price"), 4),
            int(data.get("order_qty") or 0),
            str(data.get("order_result_code") or ""),
            str(data.get("order_result_msg") or ""),
            int(bool(data.get("execution_notice_received", False))),
            int(data.get("filled_qty") or 0),
            self._fmt_num(data.get("filled_price"), 4),
            int(data.get("unfilled_qty") or 0),
            str(data.get("final_order_state") or ""),
        ]

    def _update_entry_order_audit_from_event(self, event: ExecutionEvent) -> None:
        audit = dict(self._last_entry_order_audit or {})
        raw = event.raw if isinstance(event.raw, dict) else {}
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else raw
        readable = payload.get("readable") if isinstance(payload, dict) and isinstance(payload.get("readable"), dict) else {}
        remaining_qty = (
            readable.get("미체결수량")
            or readable.get("남은수량")
            or payload.get("unfilled_qty")
            or payload.get("remaining_qty")
            or 0
        ) if isinstance(payload, dict) else 0
        audit["execution_notice_received"] = bool(raw)
        audit["filled_qty"] = max(int(event.qty or 0), 0)
        audit["filled_price"] = (float(event.price) if float(event.price or 0.0) > 0 else audit.get("filled_price"))
        try:
            audit["unfilled_qty"] = max(int(float(remaining_qty or 0)), 0)
        except Exception:
            audit["unfilled_qty"] = int(audit.get("unfilled_qty") or 0)
        audit["order_result_code"] = str(getattr(self.order_core, "last_order_result", audit.get("order_result_code", "")) or "")
        audit["order_result_msg"] = str(event.message or audit.get("order_result_msg") or "")
        audit["final_order_state"] = str(event.event_type or audit.get("final_order_state") or "")
        self._last_entry_order_audit = audit

    def _entry_dz_metrics(self, sig_state: dict | None, side_hint: str = "") -> dict[str, Any]:
        ss = sig_state if isinstance(sig_state, dict) else {}
        side_n = str(side_hint or "").upper()
        use_price_entry = bool(ss.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
        prev_z5 = None if use_price_entry else ss.get("prev_z5")
        z5_now = None if use_price_entry else ss.get("z5")
        # Entry audit must record the same PRICE FIRE value/threshold that actually
        # opened the position.  ARM thresholds are logged separately in signal_log.
        if side_n == "SHORT":
            threshold = ss.get("short_fire_threshold", ss.get("dz5_entry_short", getattr(self.cfg, "DZ5_ENTRY_SHORT", -0.30)))
        elif side_n == "LONG":
            threshold = ss.get("long_fire_threshold", ss.get("dz5_entry_long", getattr(self.cfg, "DZ5_ENTRY_LONG", 0.30)))
        else:
            threshold = ss.get("dz5")
        try:
            prev_f = float(prev_z5) if prev_z5 is not None else None
        except Exception:
            prev_f = None
        try:
            now_f = float(z5_now) if z5_now is not None else None
        except Exception:
            now_f = None
        try:
            thr_f = float(threshold) if threshold is not None else None
        except Exception:
            thr_f = None
        try:
            macd_osci_f = float(ss.get("macd_osci")) if ss.get("macd_osci") is not None else None
        except Exception:
            macd_osci_f = None
        # Persist the same side-specific FIRE value used by live signal gating.
        # Fall back to active_dz5/dz5 only when the FIRE value is unavailable.
        if side_n == "SHORT":
            dz_raw = ss.get("short_fire_value", ss.get("short_entry_active_dz5", ss.get("dz5_short")))
        elif side_n == "LONG":
            dz_raw = ss.get("long_fire_value", ss.get("long_entry_active_dz5", ss.get("dz5_long")))
        else:
            dz_raw = ss.get("dz5")
        try:
            dz_f = float(dz_raw) if dz_raw is not None else None
        except Exception:
            dz_f = None
        if dz_f is None and now_f is not None and prev_f is not None:
            dz_f = (now_f - prev_f)
        long_ok = bool(ss.get("long_entry_ready", False))
        short_ok = bool(ss.get("short_entry_ready", False))
        if side_n == "LONG":
            direction_ok = bool(ss.get("entry_price_fire_long_hit", long_ok))
        elif side_n == "SHORT":
            direction_ok = bool(ss.get("entry_price_fire_short_hit", short_ok))
        else:
            direction_ok = None
        out = {
            "entry_prev_z5": prev_f,
            "entry_z5": now_f,
            "entry_dz5": dz_f,
            "entry_dz5_threshold": thr_f,
            "entry_dz5_direction_ok": direction_ok,
            "entry_macd_osci": macd_osci_f,
            "entry_reason": str(ss.get("entry_reason") or ""),
            "entry_regime": str(ss.get("entry_regime") or ss.get("active_entry_regime") or ""),
            "active_entry_regime": str(ss.get("active_entry_regime") or ss.get("entry_regime") or ""),
            "mfe_profile": self._mfe_profile_from_entry_context(ss),
            "dz5_long_ok": long_ok,
            "dz5_short_ok": short_ok,
            "long_armed": bool(ss.get("long_armed", False)),
            "short_armed": bool(ss.get("short_armed", False)),
            "long_arm_age": int(ss.get("long_arm_age", 0) or 0),
            "short_arm_age": int(ss.get("short_arm_age", 0) or 0),
            "long_band_ok": bool(ss.get("long_band_ok", False)),
            "short_band_ok": bool(ss.get("short_band_ok", False)),
            "long_entry_ready": bool(ss.get("long_entry_ready", False)),
            "short_entry_ready": bool(ss.get("short_entry_ready", False)),
            "long_entry_z5_min": ss.get("long_entry_z5_min"),
            "long_entry_z5_max": ss.get("long_entry_z5_max"),
            "short_entry_z5_min": ss.get("short_entry_z5_min"),
            "short_entry_z5_max": ss.get("short_entry_z5_max"),
            "trend_z5_abs_min": ss.get("trend_z5_abs_min"),
            "trend_z5_abs_max": ss.get("trend_z5_abs_max"),
            "reversal_z5_abs_max": ss.get("reversal_z5_abs_max"),
            "static_z5_band_enabled_for_entry": bool(ss.get("static_z5_band_enabled_for_entry", False)),
            "final_signal": str(ss.get("final_signal") or ""),
            "block_reason": str(ss.get("block_reason") or ""),
            "arm_cancel_reason": str(ss.get("arm_cancel_reason") or ""),
        }
        out.update(self._entry_observation_metrics(ss, side_hint=side_n))
        return out

    def _remember_entry_submit_metrics(self, side_hint: str, sig_state: dict | None = None) -> None:
        self._last_entry_submit_metrics = self._entry_dz_metrics(sig_state if isinstance(sig_state, dict) else self.last_sig_state, side_hint=side_hint)

    def _append_csv_row(self, path: str, row: list[object]) -> None:
        try:
            with open(path, "a", newline="", encoding="utf-8-sig") as _f:
                csv.writer(_f).writerow(row)
        except Exception as e:
            # Do not silently lose logs/reports.  Avoid self.log() here to prevent recursion
            # when the failing file is the event log itself.
            try:
                print(f"[REPORT_WRITE_ERROR] path={path} err={e}", flush=True)
            except Exception:
                pass

    def _append_balance_confirmed_trade(
        self,
        *,
        report_type: str,
        side: str,
        qty: int,
        fill_price: float,
        reason: str,
        entry_price: float | None = None,
        pnl_pt: float | None = None,
        entry_regime: str = "",
    ) -> None:
        """Persist fills discovered by balance reconciliation, not callbacks."""
        report_type_u = str(report_type or "").strip().upper()
        if report_type_u not in ("ENTRY", "EXIT"):
            return
        now = datetime.now()
        obs = dict(getattr(self, "_position_entry_audit", {}) or {})
        if not obs and report_type_u == "ENTRY":
            obs = dict(getattr(self, "_last_entry_submit_metrics", {}) or {})
        regime = self._telegram_entry_regime(entry_regime or str(obs.get("entry_regime") or ""))
        obs["entry_regime"] = regime if regime in ("TREND", "REVERSAL") else ""
        obs.setdefault("entry_side", str(side or "").strip().upper())
        order_obs = dict(
            getattr(self, "_position_entry_order_audit", {})
            or getattr(self, "_last_entry_order_audit", {})
            or {}
        )
        order_obs.update({
            "execution_notice_received": False,
            "filled_qty": int(qty or 0),
            "filled_price": float(fill_price or 0.0),
            "unfilled_qty": 0,
            "final_order_state": f"{report_type_u}_KIS_BALANCE_CONFIRMED",
        })
        entry_price_value: object = "-"
        pnl_value: object = "-"
        if report_type_u == "EXIT":
            entry_price_value = float(entry_price or 0.0)
            pnl_value = float(pnl_pt or 0.0)
        self._append_csv_row(self._trade_log_path, [
            now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
            report_type_u, str(side or "").strip().upper(),
            int(qty or 0), float(fill_price or 0.0),
            entry_price_value, pnl_value,
            str(reason or "KIS_BALANCE_CONFIRMED"),
            f"{self.last_z:.4f}" if self.last_z is not None else "-",
            self._fmt_num(getattr(self, "position_entry_prev_z5", None), 6),
            self._fmt_num(getattr(self, "position_entry_dz5", None), 6),
            self._fmt_num(getattr(self, "position_entry_dz5_threshold", None), 6),
            ("" if getattr(self, "position_entry_dz5_direction_ok", None) is None else int(bool(self.position_entry_dz5_direction_ok))),
            self._fmt_num(getattr(self, "position_entry_macd_osci", None), 6),
            "",
        ] + self._entry_observation_csv_values(obs) + self._entry_order_audit_csv_values(order_obs))
        self.log(
            "REPORT", "TRADE_BALANCE_CONFIRMED",
            f"type={report_type_u} side={side} qty={int(qty or 0)} price={float(fill_price or 0.0):.2f} regime={regime}",
        )

    def _rewrite_live_bars_snapshot(self) -> None:
        try:
            with open(self._live_bar_path, "w", newline="", encoding="utf-8-sig") as _f:
                writer = csv.writer(_f)
                writer.writerow(["date", "time", "code", "open", "high", "low", "close", "volume", "source"])
                for row in self._live_bars_by_minute.values():
                    writer.writerow(row)
        except Exception:
            pass

    def _dump_raw_warmup_bars(self, candles: list[Candle], source: str = "RAW") -> None:
        try:
            for idx, c in enumerate(candles, start=1):
                dt = c.t if isinstance(c.t, datetime) else datetime.now()
                self._append_csv_row(self._warmup_bar_raw_path, [
                    dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), self.live_code,
                    self._fmt_num(c.o, 4), self._fmt_num(c.h, 4), self._fmt_num(c.l, 4), self._fmt_num(c.c, 4),
                    int(c.v or 0), source, idx
                ])
        except Exception as e:
            self.log("ERROR", "WARMUP_RAW_SAVE", str(e))

    def _densify_warmup_candles(self, candles: list[Candle], source: str = "WARMUP") -> tuple[list[Candle], dict[str, int]]:
        stats = {"raw_rows": len(candles or []), "used_rows": 0, "filled_rows": 0, "gap_segments": 0, "max_gap_min": 0}
        if not candles:
            return [], stats
        use_fill = bool(getattr(self.cfg, "WARMUP_FILL_MISSING_1M", True))
        max_gap = int(getattr(self.cfg, "WARMUP_FILL_MAX_GAP_MIN", 30) or 30)
        # minute normalize + same-minute overwrite
        ordered: list[Candle] = []
        by_key: dict[datetime, Candle] = {}
        for c in candles:
            dt = c.t.replace(second=0, microsecond=0) if isinstance(c.t, datetime) else c.t
            by_key[dt] = Candle(dt, float(c.o), float(c.h), float(c.l), float(c.c), int(c.v or 0))
        ordered = [by_key[k] for k in sorted(by_key.keys())]
        if not use_fill:
            stats["used_rows"] = len(ordered)
            return ordered, stats
        out: list[Candle] = []
        prev: Candle | None = None
        for cur in ordered:
            if prev is None:
                out.append(cur)
                prev = cur
                continue
            delta_min = int((cur.t - prev.t).total_seconds() // 60)
            if delta_min > 1 and prev.t.date() == cur.t.date() and delta_min <= max_gap:
                stats["gap_segments"] += 1
                stats["filled_rows"] += (delta_min - 1)
                stats["max_gap_min"] = max(stats["max_gap_min"], delta_min)
                for step_idx in range(1, delta_min):
                    fill_dt = prev.t + timedelta(minutes=step_idx)
                    px = float(prev.c)
                    out.append(Candle(fill_dt, px, px, px, px, 0))
            elif delta_min > stats["max_gap_min"]:
                stats["max_gap_min"] = delta_min
            out.append(cur)
            prev = cur
        stats["used_rows"] = len(out)
        return out, stats


    def _resolve_session_open(self, candles: list[Candle]) -> tuple[float, date | None]:
        if not candles:
            return 0.0, None
        latest_date = max((c.t.date() for c in candles), default=None)
        if latest_date is None:
            return 0.0, None
        session_rows = [c for c in candles if c.t.date() == latest_date]
        if not session_rows:
            return 0.0, latest_date
        open_anchor_raw = str(
            getattr(
                self.cfg,
                "SESSION_OPEN_TIME",
                getattr(self.cfg, "REGIME_ANCHOR_TIME", getattr(self.cfg, "TRADE_START", "08:45")),
            ) or "08:45"
        )
        open_anchor_sec = self._hhmm_to_seconds(open_anchor_raw)
        if open_anchor_sec is not None:
            anchored_rows = []
            for c in session_rows:
                sec = c.t.hour * 3600 + c.t.minute * 60 + c.t.second
                if sec >= open_anchor_sec:
                    anchored_rows.append(c)
            if anchored_rows:
                session_rows = anchored_rows
        for c in session_rows:
            try:
                return float(c.o), latest_date
            except Exception:
                return 0.0, latest_date
        return 0.0, latest_date

    def _session_open_anchor_seconds(self) -> int | None:
        anchor_raw = str(
            getattr(
                self.cfg,
                "SESSION_OPEN_TIME",
                getattr(self.cfg, "TRADE_START", "08:50"),
            ) or "08:50"
        )
        return self._hhmm_to_seconds(anchor_raw)

    def _resolve_session_range(self, candles: list[Candle]) -> tuple[float, float, str, str]:
        if not candles:
            return 0.0, 0.0, "-", "-"
        latest_date = max((c.t.date() for c in candles), default=None)
        if latest_date is None:
            return 0.0, 0.0, "-", "-"
        anchor_raw = str(getattr(self.cfg, "REGIME_ANCHOR_TIME", "08:45") or "08:45")
        anchor_sec = self._hhmm_to_seconds(anchor_raw)
        session_rows = [c for c in candles if c.t.date() == latest_date]
        if anchor_sec is not None:
            anchored = []
            for c in session_rows:
                sec = c.t.hour * 3600 + c.t.minute * 60 + c.t.second
                if sec >= anchor_sec:
                    anchored.append(c)
            if anchored:
                session_rows = anchored
        if not session_rows:
            return 0.0, 0.0, "-", "-"
        hi_c = max(session_rows, key=lambda x: float(x.h))
        lo_c = min(session_rows, key=lambda x: float(x.l))
        return float(hi_c.h), float(lo_c.l), hi_c.t.strftime("%H:%M:%S"), lo_c.t.strftime("%H:%M:%S")

    def _apply_session_boundary(
        self,
        bar_dt: datetime,
        open_price: float | None = None,
        confirm_open: bool = False,
    ) -> None:
        cur_date = bar_dt.date() if isinstance(bar_dt, datetime) else None
        if cur_date is None:
            return
        try:
            open_px = float(open_price) if open_price is not None else 0.0
        except Exception:
            open_px = 0.0
        anchor_sec = self._session_open_anchor_seconds()
        bar_sec = None
        if isinstance(bar_dt, datetime):
            bar_sec = bar_dt.hour * 3600 + bar_dt.minute * 60 + bar_dt.second
        confirm_open_allowed = bool(
            confirm_open
            and open_px > 0
            and (
                anchor_sec is None
                or bar_sec is None
                or bar_sec >= anchor_sec
            )
        )
        if self.session_open_date != cur_date:
            self.session_open_date = cur_date
            self.session_open_price = open_px if open_px > 0 else 0.0
            self.session_open_confirmed = bool(confirm_open_allowed)
            self.session_high_price = 0.0
            self.session_low_price = 0.0
            self.session_high_time = '-'
            self.session_low_time = '-'
        elif confirm_open_allowed:
            # Tick-only provisional open must be replaced by the first confirmed 1m candle open.
            self.session_open_price = open_px
            self.session_open_confirmed = True
        elif self.session_open_price <= 0 and open_px > 0:
            self.session_open_price = open_px

    @staticmethod
    def _bucket_key(dt: datetime, minutes: int) -> datetime:
        return dt.replace(minute=(dt.minute // minutes) * minutes, second=0, microsecond=0)

    @staticmethod
    def _merge_minute_candle(existing: Candle, incoming: Candle) -> Candle:
        return Candle(
            existing.t,
            float(existing.o),
            max(float(existing.h), float(incoming.h)),
            min(float(existing.l), float(incoming.l)),
            float(incoming.c),
            int(incoming.v or 0),
        )

    def _update_live_minute_cache(self, c: Candle) -> tuple[Candle, str]:
        minute_key = c.t.replace(second=0, microsecond=0)
        normalized = Candle(minute_key, float(c.o), float(c.h), float(c.l), float(c.c), int(c.v or 0))

        raw_len = len(self._live_1m_by_minute) + 1
        self._diag_dedupe_before_1m = raw_len
        self._diag_dedupe_before_5m_input = raw_len
        self._diag_dedupe_before_30m_input = raw_len
        self._diag_last_1m_minute_key = minute_key

        if minute_key in self._live_1m_by_minute:
            self._diag_same_minute_update_count += 1
            self._live_1m_by_minute[minute_key] = self._merge_minute_candle(self._live_1m_by_minute[minute_key], normalized)
            action = "SAME_MINUTE_UPDATE"
            deduped_len = len(self._live_1m_by_minute)
        else:
            self._diag_new_minute_append_count += 1
            self._live_1m_by_minute[minute_key] = normalized
            self._live_1m_by_minute = OrderedDict(sorted(self._live_1m_by_minute.items(), key=lambda item: item[0]))
            while len(self._live_1m_by_minute) > 2000:
                self._live_1m_by_minute.popitem(last=False)
            action = "NEW_MINUTE_APPEND"
            deduped_len = len(self._live_1m_by_minute)
        self._diag_dedupe_after_1m = deduped_len
        self._diag_dedupe_after_5m_input = deduped_len
        self._diag_dedupe_after_30m_input = deduped_len
        exp_1m, exp_5m, exp_30m = self._expected_live_bucket_counts()
        self._refresh_live_z5_diag_state(exp_1m, exp_5m, exp_30m)
        return self._live_1m_by_minute[minute_key], action

    def _expected_live_bucket_counts(self) -> tuple[int, int, int]:
        minute_keys = list(self._live_1m_by_minute.keys())
        bars_1m = len(minute_keys)
        if bars_1m <= 0:
            self._diag_last_5m_bucket_key = None
            self._diag_last_30m_bucket_key = None
            self._refresh_live_z5_diag_state(0, 0, 0)
            return 0, 0, 0

        bucket5 = OrderedDict()
        bucket30 = OrderedDict()
        for mk in minute_keys:
            bucket5[self._bucket_key(mk, 5)] = True
            bucket30[self._bucket_key(mk, 30)] = True
        self._diag_last_5m_bucket_key = next(reversed(bucket5)) if bucket5 else None
        self._diag_last_30m_bucket_key = next(reversed(bucket30)) if bucket30 else None
        bars_5m = max(len(bucket5) - 1, 0)
        bars_30m = max(len(bucket30) - 1, 0)
        self._refresh_live_z5_diag_state(bars_1m, bars_5m, bars_30m)
        return bars_1m, bars_5m, bars_30m

    def _resync_strategy_from_live_cache(self) -> dict[str, int]:
        self.st = Strategy(self.cfg)
        for bar in self._live_1m_by_minute.values():
            self.st.update_indicators(bar)
        return self.st.get_counts()

    def _seed_live_cache_from_candles(self, candles: list[Candle]) -> None:
        seeded = OrderedDict()
        for bar in candles:
            mk = bar.t.replace(second=0, microsecond=0)
            seeded[mk] = Candle(mk, float(bar.o), float(bar.h), float(bar.l), float(bar.c), int(bar.v or 0))
        if len(seeded) > 2000:
            keep = list(seeded.items())[-2000:]
            seeded = OrderedDict(keep)
        self._live_1m_by_minute = seeded
        self._diag_dedupe_before_1m = len(seeded)
        self._diag_dedupe_after_1m = len(seeded)
        self._diag_dedupe_before_5m_input = len(seeded)
        self._diag_dedupe_after_5m_input = len(seeded)
        self._diag_dedupe_before_30m_input = len(seeded)
        self._diag_dedupe_after_30m_input = len(seeded)
        self._diag_last_1m_minute_key = next(reversed(seeded)) if seeded else None
        self._diag_stale_tick_skip_count = 0
        self._diag_same_minute_update_count = 0
        self._diag_new_minute_append_count = 0
        self._expected_live_bucket_counts()

    def _resolve_session_open_from_live_cache(self) -> tuple[float, date | None]:
        minute_keys = list(self._live_1m_by_minute.keys())
        if not minute_keys:
            return 0.0, None
        latest_date = max((mk.date() for mk in minute_keys), default=None)
        if latest_date is None:
            return 0.0, None
        anchor_sec = self._session_open_anchor_seconds()
        for mk, bar in self._live_1m_by_minute.items():
            if mk.date() != latest_date:
                continue
            bar_sec = mk.hour * 3600 + mk.minute * 60 + mk.second
            if anchor_sec is not None and bar_sec < anchor_sec:
                continue
            try:
                return float(bar.o), latest_date
            except Exception:
                return 0.0, latest_date
        return 0.0, latest_date

    def _refresh_live_z5_diag_state(self, bars_1m: int | None = None, bars_5m: int | None = None, bars_30m: int | None = None) -> None:
        state = self.live_z5_diag_state if isinstance(getattr(self, "live_z5_diag_state", None), dict) else {}
        state["live_z5_stale_tick_skips"] = int(getattr(self, "_diag_stale_tick_skip_count", 0) or 0)
        state["live_z5_same_minute_updates"] = int(getattr(self, "_diag_same_minute_update_count", 0) or 0)
        state["live_z5_new_minute_appends"] = int(getattr(self, "_diag_new_minute_append_count", 0) or 0)
        state["live_z5_dedupe_1m_before"] = int(getattr(self, "_diag_dedupe_before_1m", 0) or 0)
        state["live_z5_dedupe_1m_after"] = int(getattr(self, "_diag_dedupe_after_1m", 0) or 0)
        state["live_z5_dedupe_5m_before"] = int(getattr(self, "_diag_dedupe_before_5m_input", 0) or 0)
        state["live_z5_dedupe_5m_after"] = int(getattr(self, "_diag_dedupe_after_5m_input", 0) or 0)
        state["live_z5_dedupe_30m_before"] = int(getattr(self, "_diag_dedupe_before_30m_input", 0) or 0)
        state["live_z5_dedupe_30m_after"] = int(getattr(self, "_diag_dedupe_after_30m_input", 0) or 0)
        if bars_1m is None:
            bars_1m = int(state.get("live_z5_bars_1m_unique", 0) or 0)
        if bars_5m is None:
            bars_5m = int(state.get("live_z5_bars_5m_unique", 0) or 0)
        if bars_30m is None:
            bars_30m = int(state.get("live_z5_bars_30m_unique", 0) or 0)
        state["live_z5_bars_1m_unique"] = int(bars_1m or 0)
        state["live_z5_bars_5m_unique"] = int(bars_5m or 0)
        state["live_z5_bars_30m_unique"] = int(bars_30m or 0)
        last_1m = getattr(self, "_diag_last_1m_minute_key", None)
        last_5m = getattr(self, "_diag_last_5m_bucket_key", None)
        last_30m = getattr(self, "_diag_last_30m_bucket_key", None)
        state["live_z5_last_1m_minute_key"] = last_1m.strftime("%Y-%m-%d %H:%M:%S") if isinstance(last_1m, datetime) else ""
        state["live_z5_last_5m_bucket_key"] = last_5m.strftime("%Y-%m-%d %H:%M:%S") if isinstance(last_5m, datetime) else ""
        state["live_z5_last_30m_bucket_key"] = last_30m.strftime("%Y-%m-%d %H:%M:%S") if isinstance(last_30m, datetime) else ""
        self.live_z5_diag_state = state

    def _prime_signal_snapshot_after_warmup(self, reason: str = "WARMUP") -> None:
        """Populate dashboard signal state once at warmup completion, even without fresh live ticks."""
        try:
            if self.st is None:
                return
            snap_price = None
            for raw in (self.last_bar_close, self.current_price, self.session_close):
                try:
                    v = float(raw or 0.0)
                except Exception:
                    v = 0.0
                if v > 0.0:
                    snap_price = v
                    break
            if snap_price is None:
                return
            snap_time = getattr(self, "_diag_last_1m_minute_key", None)
            if not isinstance(snap_time, datetime):
                snap_time = datetime.now().replace(second=0, microsecond=0)
            in_pos = 1 if self.position_side == "LONG" and self.position_qty > 0 else (-1 if self.position_side == "SHORT" and self.position_qty > 0 else 0)
            pos_meta = None
            if in_pos != 0:
                pos_meta = {
                    "side": self.position_side,
                    "qty": self.position_qty,
                    "entry_price": self.entry_price,
                    "entry_time": self.position_entry_time,
                    "peak_price": self.position_peak_price,
                    "trough_price": self.position_trough_price,
                    "entry_regime": str(self.position_entry_regime or ''),
                    "entry_z5_band_snapshot": dict(self.position_entry_z5_band_snapshot or {}),
                    "mfe_profile": str(self.position_mfe_profile or ''),
                    "current_high": float(snap_price),
                    "current_low": float(snap_price),
                }
            refresh_delta_reference = getattr(self.st, "refresh_delta_reference_from_warmup", None)
            if callable(refresh_delta_reference):
                sig_state = refresh_delta_reference(
                    float(snap_price),
                    snap_time,
                    reason=reason,
                )
                # Re-evaluate position-dependent exit fields without changing
                # the freshly warmed delta baseline.
                if pos_meta is not None:
                    sig_state = self.st.get_signal_state(
                        float(snap_price),
                        snap_time,
                        position_meta=pos_meta,
                        include_dashboard_reference_prices=True,
                    )
                    sig_state["warmup_delta_reference_refreshed"] = True
                    sig_state["warmup_delta_reference_reason"] = str(reason or "WARMUP")
            else:
                sig_state = self.st.get_signal_state(float(snap_price), snap_time, position_meta=pos_meta)
            if not isinstance(sig_state, dict):
                return
            counts = self.st.get_counts() if self.st is not None else {}
            self._refresh_live_z5_diag_state(
                int(counts.get("bars_1m", 0) or 0),
                int(counts.get("bars_5m", 0) or 0),
                int(counts.get("bars_30m", 0) or 0),
            )
            self.last_sig_state = dict(sig_state)
            self.last_sig_state["live_z5_diag"] = dict(self.live_z5_diag_state)
            self.last_regime = sig_state.get("regime", self.last_regime)
            self.last_z_1m = sig_state.get("z1", self.last_z_1m)
            self.last_z_5m = sig_state.get("z5", self.last_z_5m)
            self.last_z_30m = sig_state.get("z30", self.last_z_30m)
            self.last_z = self.last_z_5m
            allow_lane, block_reason = self._entry_lane_state(self.last_sig_state)
            self.last_allow_trade = bool(allow_lane)
            self.last_block_reason = str(block_reason or "-")
            self.last_sig_state["allow_trade"] = self.last_allow_trade
            self.last_sig_state["block_reason"] = self.last_block_reason
            self.log(
                "WARMUP",
                "SNAPSHOT",
                (
                    f"reason={reason} refreshed={int(bool(self.last_sig_state.get('warmup_delta_reference_refreshed', False)))} "
                    f"z1={self._fmt_num(self.last_z_1m, 6)} dz1={self._fmt_num(self.last_sig_state.get('dz1_prev_delta'), 6)} "
                    f"z5={self._fmt_num(self.last_z_5m, 6)} dz5={self._fmt_num(self.last_sig_state.get('dz5_prev_delta'), 6)} "
                    f"z1_arm_long={self._fmt_num(self.last_sig_state.get('z1_delta_long_arm_price'), 2)} "
                    f"z1_fire_long={self._fmt_num(self.last_sig_state.get('z1_delta_long_fire_price_min'), 2)}~{self._fmt_num(self.last_sig_state.get('z1_delta_long_fire_price_max'), 2)} "
                    f"z1_arm_short={self._fmt_num(self.last_sig_state.get('z1_delta_short_arm_price'), 2)} "
                    f"z1_fire_short={self._fmt_num(self.last_sig_state.get('z1_delta_short_fire_price_min'), 2)}~{self._fmt_num(self.last_sig_state.get('z1_delta_short_fire_price_max'), 2)}"
                ),
            )
        except Exception as e:
            self.log("ERROR", "WARMUP_SNAPSHOT", str(e))
            self.log("ERROR", "TRACE", traceback.format_exc().splitlines()[-1])

    def _log_live_dedupe_diag(self, force: bool = False) -> None:
        now_ts = time.time()
        if (not force) and (now_ts - float(self._last_dedupe_diag_log_ts or 0.0) < float(self._dedupe_diag_log_min_interval_sec or 10.0)):
            return
        self._last_dedupe_diag_log_ts = now_ts
        last_1m = self._diag_last_1m_minute_key.strftime("%Y-%m-%d %H:%M:%S") if isinstance(self._diag_last_1m_minute_key, datetime) else "-"
        last_5m = self._diag_last_5m_bucket_key.strftime("%Y-%m-%d %H:%M:%S") if isinstance(self._diag_last_5m_bucket_key, datetime) else "-"
        last_30m = self._diag_last_30m_bucket_key.strftime("%Y-%m-%d %H:%M:%S") if isinstance(self._diag_last_30m_bucket_key, datetime) else "-"
        self.log(
            "BAR",
            "DEDUPE",
            (
                f"stale_skip={self._diag_stale_tick_skip_count} "
                f"same_minute_update={self._diag_same_minute_update_count} "
                f"new_minute_append={self._diag_new_minute_append_count} "
                f"dedupe_1m={self._diag_dedupe_before_1m}->{self._diag_dedupe_after_1m} "
                f"dedupe_5m_input={self._diag_dedupe_before_5m_input}->{self._diag_dedupe_after_5m_input} "
                f"dedupe_30m_input={self._diag_dedupe_before_30m_input}->{self._diag_dedupe_after_30m_input} "
                f"last_1m={last_1m} last_5m={last_5m} last_30m={last_30m}"
            ),
        )

    def _log_stale_tick_summary(self, stale_minute_key: datetime, current_bar_key: datetime) -> None:
        now_ts = time.time()
        min_interval = float(getattr(self, "_stale_tick_log_min_interval_sec", 15.0) or 15.0)
        if now_ts - float(getattr(self, "_last_stale_tick_log_ts", 0.0) or 0.0) < min_interval:
            return
        total = int(getattr(self, "_diag_stale_tick_skip_count", 0) or 0)
        prev = int(getattr(self, "_last_stale_tick_logged_count", 0) or 0)
        delta = max(total - prev, 0)
        self._last_stale_tick_log_ts = now_ts
        self._last_stale_tick_logged_count = total
        self.log(
            "BAR",
            "STALE_TICK",
            (
                f"ignore stale tick(s) +{delta} total={total} "
                f"last_minute={stale_minute_key:%Y-%m-%d %H:%M:%S} "
                f"current_bar={current_bar_key:%Y-%m-%d %H:%M:%S}"
            ),
        )

    def _reset_server_unfilled_state(self, reason: str = "CLEAR") -> None:
        self.has_server_unfilled = False
        self.has_unfilled_orders = False
        self.server_unfilled_qty = 0
        self.server_unfilled_order_no = ""
        self.server_unfilled_orig_order_no = ""
        self.server_unfilled_side = ""
        self.server_unfilled_symbol = ""
        self.server_unfilled_status = ""
        self.server_unfilled_last_reason = str(reason or "CLEAR")
        self.auto_cancel_phase = "SERVER_UNFILLED_CLEARED"
        self.log("SYNC", "UNFILLED_CLEARED", "entry_block=OFF")

    def _refresh_lane_snapshot(self) -> None:
        self.active_position_side = str(self.position_side or "FLAT")
        self.active_position_qty = int(self.position_qty or 0)
        if self.active_position_side in ("LONG", "SHORT") and self.active_position_qty > 0:
            self.flat_confirmed = False
        self.order_lane_locked = bool(
            self.entry_inflight
            or self.exit_inflight
            or self.cancel_in_progress
            or self.position_close_pending
            or (self.order_core and self.order_core.pending.active)
        )

    def _mark_unfilled_detected(self) -> None:
        was = bool(self.has_unfilled_orders)
        self.has_unfilled_orders = True
        if not was:
            self.log("SYNC", "UNFILLED_DETECTED", "entry_block=ON")

    def _local_entry_unfilled_cancel_eligible(self) -> bool:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        if pending is None:
            return False
        pending_action = str(getattr(pending, "action", "") or "").upper()
        if pending_action != "ENTRY" or not bool(getattr(pending, "active", False)):
            return False
        if bool(getattr(pending, "server_recheck_required", False)):
            return True
        remaining = int(getattr(pending, "remaining_qty", 0) or 0)
        if remaining <= 0:
            return False
        if bool(getattr(pending, "cancel_requested", False)):
            return True
        now_ts = time.time()
        sent_ts = float(getattr(pending, "sent_ts", 0.0) or 0.0)
        first_unfilled_ts = float(getattr(pending, "first_unfilled_ts", 0.0) or 0.0)
        accepted_seen = bool(getattr(pending, "accepted_seen", False))
        msg_seen = bool(getattr(pending, "msg_seen", False))
        grace_sec = max(
            float(getattr(self.order_core, "server_clear_wait_fill_grace_sec", 2.0) or 2.0),
            float(getattr(self.order_core, "entry_unfilled_cancel_delay_sec", 0.0) or 0.0),
        )
        if (accepted_seen or msg_seen) and sent_ts > 0.0 and (now_ts - sent_ts) < grace_sec:
            return False
        if first_unfilled_ts > 0.0 and (now_ts - first_unfilled_ts) < float(getattr(self.order_core, "entry_unfilled_cancel_delay_sec", 0.0) or 0.0):
            return False
        return True

    def _clear_stale_local_unfilled_flag(self) -> None:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_action = str(getattr(pending, "action", "") or "").upper() if pending is not None else ""
        pending_entry_evidence = bool(
            pending is not None
            and bool(getattr(pending, "active", False))
            and pending_action == "ENTRY"
            and (
                int(getattr(pending, "remaining_qty", 0) or 0) > 0
                or bool(getattr(pending, "cancel_requested", False))
                or bool(getattr(pending, "server_recheck_required", False))
            )
        )
        if self.has_unfilled_orders and (not self.has_server_unfilled) and (not pending_entry_evidence):
            self.has_unfilled_orders = False
            self.log("SYNC", "UNFILLED_CLEARED", "entry_block=OFF local_stale=1")

    def _clear_stale_entry_guard(self, reason: str = "STALE_CLEAR") -> None:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_action = str(getattr(pending, "action", "") or "").upper() if pending is not None else ""
        pending_entry_active = bool(
            pending is not None
            and bool(getattr(pending, "active", False))
            and pending_action == "ENTRY"
        )
        if pending_entry_active:
            try:
                pending.active = False
                pending.action = ""
                pending.side = ""
                pending.qty = 0
                pending.remaining_qty = 0
                pending.cancel_requested = False
                pending.server_recheck_required = False
                pending.auto_cancel_phase = ""
                pending.unresolved_reason = ""
            except Exception:
                pass
        self._entry_order_guard_active = False
        self._entry_order_guard_side = ""
        self._entry_order_guard_ts = 0.0
        self.entry_inflight = False
        self.pending_entry_side = ""
        self.order_lane_locked = False
        self.log("SYNC", "ENTRY_GUARD_CLEARED", f"reason={reason}")

    @staticmethod
    def _is_entry_unknown_balance_reason(reason: str) -> bool:
        return str(reason or "").strip().upper() in {
            "ENTRY_UNKNOWN_BALANCE_CHECK",
            "AUTO_CANCEL_SEND_FAIL",
            "CANCEL_SEND_FAIL",
            "NO_CALLBACK_TIMEOUT",
            "NO_KIS_EXECUTION_TIMEOUT",
        }

    def _request_entry_unknown_balance_recheck(self) -> None:
        if not self.connected or not self.account:
            return
        if not (self.entry_inflight or self._entry_order_guard_active or self.cancel_needs_server_check):
            return
        if self.takeover_inflight:
            return
        self._request_server_sync(
            reason="ENTRY_UNKNOWN_BALANCE_CHECK",
            include_takeover=True,
            include_unfilled=False,
            include_account=False,
            include_orderable=False,
            start_warmup_after_response=False,
            force=True,
        )

    def _retry_unknown_order_confirmation(self, now_ts: float) -> None:
        """Keep reconciliation alive even when a one-shot retry loses a race."""
        if not self.connected or not self.account:
            return
        if not (self._entry_order_guard_active or self.cancel_needs_server_check):
            return
        if self.takeover_inflight or self.unfilled_inflight:
            return
        last_attempt = float(getattr(self, "_unknown_confirmation_retry_ts", 0.0) or 0.0)
        if now_ts - last_attempt < 5.0:
            return
        self._unknown_confirmation_retry_ts = now_ts
        if self.has_server_unfilled or self.has_unfilled_orders:
            self._request_server_sync(
                reason="AUTO_CANCEL_SEND_FAIL",
                include_takeover=False, include_unfilled=True,
                include_account=False, include_orderable=False,
                start_warmup_after_response=False, force=True,
            )
        else:
            self._request_entry_unknown_balance_recheck()

    def _clear_entry_unknown_after_server_flat(self, reason: str) -> bool:
        if not self._is_entry_unknown_balance_reason(reason):
            return False
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_entry_active = bool(
            pending is not None
            and bool(getattr(pending, "active", False))
            and str(getattr(pending, "action", "") or "").upper() == "ENTRY"
        )
        if pending_entry_active or self._has_server_position() or self.has_server_unfilled or self.has_unfilled_orders:
            return False
        if self.takeover_inflight or self.unfilled_inflight:
            return False
        if not (self.entry_inflight or self._entry_order_guard_active or self.cancel_needs_server_check):
            return False
        self._clear_stale_entry_guard(f"SERVER_FLAT_AND_NO_UNFILLED:{reason}")
        self.cancel_in_progress = False
        self.cancel_needs_server_check = False
        self.cancel_confirmed = True
        self.flat_confirmed = True
        self.auto_cancel_phase = "SERVER_UNFILLED_CLEARED"
        self._refresh_lane_snapshot()
        self.log("SYNC", "ENTRY_UNKNOWN_RESOLVED_FLAT", f"reason={reason}")
        return True

    def _clear_entry_unknown_after_server_position(self, reason: str) -> bool:
        """Resolve an uncertain entry/cancel once balance proves the fill exists."""
        if not self._is_entry_unknown_balance_reason(reason):
            return False
        if not self._has_server_position():
            return False
        if not (
            self.entry_inflight
            or self._entry_order_guard_active
            or self.cancel_in_progress
            or self.cancel_needs_server_check
        ):
            return False
        self._clear_stale_entry_guard(f"SERVER_POSITION_CONFIRMED:{reason}")
        self.cancel_in_progress = False
        self.cancel_needs_server_check = False
        self.cancel_confirmed = True
        self.auto_cancel_phase = "SERVER_POSITION_CONFIRMED"
        self._refresh_lane_snapshot()
        self.log("SYNC", "ENTRY_UNKNOWN_RESOLVED_POSITION", f"reason={reason}")
        return True

    @staticmethod
    def _looks_like_exit_reason(reason: str) -> bool:
        text = str(reason or "").strip().upper()
        if not text:
            return False
        keys = (
            "EXIT",
            "STOP_LOSS",
            "FIXED_LOSS",
            "MFE_PROTECT",
            "MAX_HOLD",
            "BAR3_CUT",
            "TRAIL",
            "MFE_RETRACE",
            "AUTO_PRECHECK",
            "MANUAL_EXIT",
        )
        return any(k in text for k in keys)

    @staticmethod
    def _is_hard_exit_reason(reason: str) -> bool:
        text = str(reason or "").strip().upper()
        return text in {"MFE_TRAIL", "MFE_PROTECT", "FIXED_LOSS"}

    @staticmethod
    def _hard_exit_priority(reason: str) -> int:
        text = str(reason or "").strip().upper()
        if text == "FIXED_LOSS":
            return 300
        if text == "MFE_TRAIL":
            return 200
        if text == "MFE_PROTECT":
            return 100
        return 0

    def _set_exit_reason(self, reason: str, source: str) -> None:
        final_reason = str(reason or "").strip()
        src = str(source or "UNKNOWN")
        if not final_reason:
            final_reason = "EXIT_CONFIRMED_NO_REASON"
        if final_reason == "EXIT_CONFIRMED_NO_REASON":
            self.log("SYNC", "EXIT_REASON_FALLBACK", "reason=EXIT_CONFIRMED_NO_REASON")
        self.last_exit_reason = final_reason
        self.last_flat_confirm_source = src
        self.last_exit_context = {
            "source": src,
            "reason": final_reason,
            "pending_exit_reason": str(self.pending_exit_reason or ""),
            "server_sync_reason": str(self.server_sync_reason or ""),
            "last_signal_reason": str(self.last_signal_reason or ""),
            "prior_side": str(self.last_position_side_before_flat or "FLAT"),
            "prior_qty": int(self.last_position_qty_before_flat or 0),
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.exit_reason_history.appendleft(f"{datetime.now().strftime('%H:%M:%S')} {final_reason}")
        self.log("SYNC", "EXIT_REASON_SET", f"source={src} reason={final_reason}")
        self.log(
            "EXIT",
            "LAST_REASON",
            f"마지막 청산사유={final_reason} source={src} prior={self.last_position_side_before_flat or 'FLAT'}/{int(self.last_position_qty_before_flat or 0)}",
        )

    def _capture_pending_exit_context(self, reason: str) -> None:
        if not self._has_server_position():
            return
        self.pending_exit_reason = str(reason or "").strip()
        self._pending_exit_position_side = str(self.position_side or "FLAT")
        self._pending_exit_position_qty = int(self.position_qty or 0)

    def _promote_pending_hard_exit_reason(self, reason: str, source: str = "") -> str:
        reason_text = str(reason or "").strip().upper()
        if not self._is_hard_exit_reason(reason_text):
            return str(self.pending_exit_reason or "").strip().upper()
        pending_text = str(self.pending_exit_reason or "").strip().upper()
        if (not pending_text) or (not self._is_hard_exit_reason(pending_text)):
            self.pending_exit_reason = reason_text
            self._arm_pending_exit_entry_block(reason_text)
            return reason_text
        if self._hard_exit_priority(reason_text) <= self._hard_exit_priority(pending_text):
            return pending_text
        self.pending_exit_reason = reason_text
        self._arm_pending_exit_entry_block(reason_text)
        if reason_text == "FIXED_LOSS" and bool(getattr(self, "_post_exit_entry_pending", False)):
            self._clear_post_exit_overlap_entry()
        self.log(
            "SYNC",
            "PENDING_EXIT_REASON_PROMOTED",
            f"from={pending_text} to={reason_text} source={source or '-'}",
        )
        return reason_text

    def _arm_pending_exit_entry_block(self, reason: str = "", hold_sec: float | None = None) -> None:
        reason_text = str(reason or self.pending_exit_reason or "").strip().upper()
        if reason_text and (not self._is_hard_exit_reason(reason_text)):
            return
        if hold_sec is None:
            recheck_delay_sec = float(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350) / 1000.0
            hold_sec = max(
                1.5,
                recheck_delay_sec + 0.8,
                float(getattr(self, "_post_exit_entry_unknown_clear_sec", 2.2) or 2.2),
            )
        self._pending_exit_entry_block_until_ts = max(
            float(getattr(self, "_pending_exit_entry_block_until_ts", 0.0) or 0.0),
            time.time() + max(float(hold_sec or 0.0), 0.0),
        )

    def _pending_exit_entry_block_active(self) -> bool:
        pending_reason = str(self.pending_exit_reason or "").strip().upper()
        if not pending_reason or (not self._is_hard_exit_reason(pending_reason)):
            return False
        if self.has_unfilled_orders or self.has_server_unfilled:
            return True
        if self._is_exit_flat_confirmation_missing():
            return True
        if self._has_server_position():
            return True
        if not bool(self.flat_confirmed):
            return True
        return time.time() < float(getattr(self, "_pending_exit_entry_block_until_ts", 0.0) or 0.0)

    def _resolve_exit_reason_for_flat(self, source: str, preferred_reason: str = "") -> str:
        preferred = str(preferred_reason or "").strip()
        if preferred:
            return preferred
        pending = str(self.pending_exit_reason or "").strip()
        if pending:
            return pending
        sig_reason = str(self.last_signal_reason or "").strip()
        if self._looks_like_exit_reason(sig_reason):
            return sig_reason
        src = str(source or "").strip().upper()
        if src == "AUTO_PRECHECK_CONFIRM":
            return "AUTO_PRECHECK_EXIT"
        if src == "MANUAL_EXIT_CHECK":
            return "MANUAL_EXIT"
        if src == "SERVER_SYNC_FLAT":
            return "SERVER_SYNC_EXIT_CONFIRM"
        sync_reason = str(self.server_sync_reason or "").strip()
        if self._looks_like_exit_reason(sync_reason):
            return sync_reason
        return "EXIT_CONFIRMED_NO_REASON"

    def _confirm_flat_exit_reason(self, source: str, prior_side: str, prior_qty: int, preferred_reason: str = "") -> None:
        p_side = str(prior_side or "FLAT").upper()
        p_qty = int(prior_qty or 0)
        if p_side not in ("LONG", "SHORT") or p_qty <= 0:
            p_side = str(self._pending_exit_position_side or "FLAT").upper()
            p_qty = int(self._pending_exit_position_qty or 0)
        if p_side not in ("LONG", "SHORT") or p_qty <= 0:
            self.log("SYNC", "EXIT_REASON_SKIP", "no prior position / no valid context")
            return
        self.last_position_side_before_flat = p_side
        self.last_position_qty_before_flat = p_qty
        resolved = self._resolve_exit_reason_for_flat(source, preferred_reason=preferred_reason)
        self._set_exit_reason(resolved, source=source)
        self.pending_exit_reason = ""
        self._pending_exit_entry_block_until_ts = 0.0
        self._pending_exit_position_side = "FLAT"
        self._pending_exit_position_qty = 0

    @staticmethod
    def _unfilled_identity(row: dict[str, Any]) -> str:
        order_no = str((row or {}).get("주문번호") or "").strip()
        orig_no = str((row or {}).get("원주문번호") or "").strip()
        if order_no:
            return f"ORD:{order_no}"
        if orig_no:
            return f"ORG:{orig_no}"
        ts = str((row or {}).get("주문/체결시간") or "").strip()
        code = str((row or {}).get("종목코드") or "").strip()
        side = str((row or {}).get("매도수구분") or "").strip()
        qty = str((row or {}).get("미체결수량") or "").strip()
        return f"FALLBACK:{code}:{side}:{qty}:{ts}"

    def _start_auto_precheck_session(self) -> None:
        self._auto_precheck_session_id += 1
        self._auto_precheck_active = True
        self._auto_precheck_started_ts = time.time()
        self._precheck_entry_filled_session_id = 0
        self._precheck_stale_position_detected = False
        self._precheck_position_conflict_detected = False
        self._precheck_takeover_existing_position = False
        self._precheck_stale_unfilled_ids.clear()
        self._precheck_stale_unfilled_qty = 0

    def _is_precheck_new_position(self) -> bool:
        if not self._auto_precheck_active:
            return False
        return int(self._precheck_entry_filled_session_id or 0) == int(self._auto_precheck_session_id or 0)

    @staticmethod
    def _hms_text_to_seconds(raw: str) -> int | None:
        text = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(text) < 6:
            return None
        hh = int(text[-6:-4])
        mm = int(text[-4:-2])
        ss = int(text[-2:])
        if hh < 0 or hh > 23 or mm < 0 or mm > 59 or ss < 0 or ss > 59:
            return None
        return hh * 3600 + mm * 60 + ss

    @staticmethod
    def _hhmm_to_seconds(raw: str) -> int | None:
        text = str(raw or "").strip()
        if ":" not in text:
            return None
        try:
            hh, mm = text.split(":", 1)
            h = int(hh)
            m = int(mm)
            if h < 0 or h > 23 or m < 0 or m > 59:
                return None
            return h * 3600 + m * 60
        except Exception:
            return None

    def _is_eod_block_active(self, now_dt: datetime | None = None) -> bool:
        if not bool(getattr(self.cfg, "EOD_BLOCK_NEW_ENTRY", True)):
            return False
        dt = now_dt if isinstance(now_dt, datetime) else datetime.now()
        cutoff_raw = str(getattr(self.cfg, "EOD_CUTOFF", getattr(self.cfg, "TRADE_END", "15:33")) or getattr(self.cfg, "TRADE_END", "15:33"))
        cutoff_sec = self._hhmm_to_seconds(cutoff_raw)
        if cutoff_sec is None:
            return False
        now_sec = dt.hour * 3600 + dt.minute * 60 + dt.second
        # Once EOD cutoff is reached, block all new entries immediately.
        # This prevents reverse-entry or fresh-entry during the same cutoff minute
        # after an EOD force-exit has been triggered.
        return bool(now_sec >= int(cutoff_sec))

    def _is_eod_force_exit_due(self, now_dt: datetime | None = None) -> bool:
        if not bool(getattr(self.cfg, "EOD_FORCE_EXIT_POSITION", True)):
            return False
        dt = now_dt if isinstance(now_dt, datetime) else datetime.now()
        cutoff_raw = str(getattr(self.cfg, "EOD_CUTOFF", getattr(self.cfg, "TRADE_END", "15:33")) or getattr(self.cfg, "TRADE_END", "15:33"))
        cutoff_sec = self._hhmm_to_seconds(cutoff_raw)
        if cutoff_sec is None:
            return False
        now_sec = dt.hour * 3600 + dt.minute * 60 + dt.second
        return bool(now_sec >= int(cutoff_sec))

    def _is_precheck_old_unfilled_row(self, row: dict[str, Any]) -> bool:
        if float(self._auto_precheck_started_ts or 0.0) <= 0:
            return True
        raw_ts = str((row or {}).get("주문/체결시간") or "").strip()
        row_sec = self._hms_text_to_seconds(raw_ts)
        if row_sec is None:
            return True
        start_dt = datetime.fromtimestamp(float(self._auto_precheck_started_ts))
        start_sec = start_dt.hour * 3600 + start_dt.minute * 60 + start_dt.second
        return row_sec <= (start_sec + 1)

    def _exit_entry_cooldown_remaining_sec(self) -> float:
        last_reason = str(self.last_exit_reason or "").strip().upper()
        cooldown = float(self._reverse_entry_cooldown_sec or 0.0)
        if last_reason == "REVERSE_SIGNAL":
            cooldown = float(self._reverse_signal_reentry_cooldown_sec or cooldown)
        elif last_reason == "FIXED_LOSS":
            cooldown = float(self._fixed_loss_reentry_cooldown_sec or cooldown)
        elif last_reason in {"MFE_TRAIL", "MFE_PROTECT"}:
            cooldown = float(self._mfe_cut_reentry_cooldown_sec or cooldown)
        if cooldown <= 0:
            return 0.0
        remain = cooldown - (time.time() - float(self._last_exit_filled_ts or 0.0))
        return remain if remain > 0.0 else 0.0

    def _entry_exit_lock_remaining_sec(self) -> float:
        # Block automatic EXIT submissions for a short window right after ENTRY fill.
        cooldown = float(self._entry_submit_cooldown_sec or 0.0)
        if cooldown <= 0:
            return 0.0
        remain = cooldown - (time.time() - float(self._last_entry_filled_ts or 0.0))
        return remain if remain > 0.0 else 0.0

    def _post_fill_action_cooldown_remaining_sec(self) -> float:
        # Common cooldown right after any fill (ENTRY/EXIT).
        cooldown = float(self._post_fill_action_cooldown_sec or 0.0)
        if cooldown <= 0:
            return 0.0
        last_fill_ts = max(float(self._last_entry_filled_ts or 0.0), float(self._last_exit_filled_ts or 0.0))
        if last_fill_ts <= 0.0:
            return 0.0
        remain = cooldown - (time.time() - last_fill_ts)
        return remain if remain > 0.0 else 0.0

    def _latest_fill_was_exit(self) -> bool:
        try:
            last_exit_ts = float(self._last_exit_filled_ts or 0.0)
            last_entry_ts = float(self._last_entry_filled_ts or 0.0)
        except Exception:
            return False
        return bool(last_exit_ts > 0.0 and last_exit_ts >= last_entry_ts)

    def _is_quiet_fixed_loss_cooldown_window(self) -> bool:
        if str(self.last_exit_reason or "").strip().upper() != "FIXED_LOSS":
            return False
        if not self._latest_fill_was_exit():
            return False
        if self._exit_entry_cooldown_remaining_sec() <= 0.0:
            return False
        if self._has_server_position() or self.has_server_unfilled or self.has_unfilled_orders:
            return False
        if self.server_sync_pending or self.takeover_inflight or self.unfilled_inflight:
            return False
        if self.entry_inflight or self.exit_inflight or self.exit_in_progress or self.position_close_pending:
            return False
        if bool(getattr(self, "_post_exit_entry_pending", False)) or bool(getattr(self, "_post_exit_entry_awaiting_confirm", False)):
            return False
        if not bool(self.flat_confirmed):
            return False
        return True

    def _release_post_exit_flat_entry_lane_if_safe(self, source: str = "") -> bool:
        """Reopen the ENTRY lane when local/server state is safely flat after EXIT.

        EXIT_FILLED intentionally waits for server flat confirmation.  In live callbacks,
        balance execution_notice/TR ordering can leave the UI in FLAT while stale EXIT latches
        (AUTO_EXIT_WAIT/AUTO_EXIT_REQUESTED, post_exit_sync, order_lane_locked,
        position_close_pending) remain set.  That blocks the next valid ENTRY even
        though there is no position and no unfilled order.
        """
        try:
            local_flat = str(self.position_side or "FLAT").upper() == "FLAT" and int(self.position_qty or 0) <= 0
            pending = getattr(getattr(self, "order_core", None), "pending", None)
            pending_active = bool(getattr(pending, "active", False))
            sync_busy = bool(
                self.server_sync_pending
                or self.takeover_inflight
                or self.unfilled_inflight
                or self.account_snapshot_inflight
                or self.orderable_snapshot_inflight
            )
            safe_flat = bool(
                local_flat
                and (not pending_active)
                and (not self.has_unfilled_orders)
                and (not self.has_server_unfilled)
                and (not self._has_server_position())
                and (not self._is_entry_state_unknown())
                and (not self._is_cancel_state_unknown())
                and (not sync_busy)
            )
            if not safe_flat:
                return False
            phase = str(self.auto_cleanup_phase or "AUTO_WAIT_SIGNAL").upper()
            stale_exit_latch = bool(
                self.exit_inflight
                or self.exit_in_progress
                or self.position_close_pending
                or self.exit_needs_server_check
                or self._post_exit_server_sync_required
                or self.order_lane_locked
                or (not bool(self.flat_confirmed))
                or phase in {
                    "AUTO_EXIT_REQUESTED",
                    "AUTO_EXIT_WAIT",
                    "AUTO_POSITION_CLEARED",
                    "AUTO_POSITION_CHECK",
                }
            )
            if not stale_exit_latch:
                return False
            self.exit_inflight = False
            self.exit_in_progress = False
            self.position_close_pending = False
            self.exit_needs_server_check = False
            self._post_exit_server_sync_required = False
            self.flat_confirmed = True
            self.order_lane_locked = False
            self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
            self._refresh_lane_snapshot()
            self.log("SYNC", "POST_EXIT_ENTRY_LANE_RELEASE", f"source={source or '-'} order_lane=REOPEN/SAFE_FLAT")
            return True
        except Exception as exc:
            try:
                self.log("WARN", "POST_EXIT_ENTRY_LANE_RELEASE_ERROR", repr(exc))
            except Exception:
                pass
            return False

    def _reset_post_warmup_entry_gate(self, reason: str) -> None:
        self._post_warmup_live_bar_count = 0
        self.log(
            "WARMUP",
            "ENTRY_GATE_RESET",
            f"reason={reason} wait_bars={int(self._post_warmup_entry_delay_bars or 0)}",
        )

    def _register_post_warmup_live_bar(self, c: Candle, allow_trade: bool) -> None:
        if not bool(allow_trade):
            return
        needed = int(self._post_warmup_entry_delay_bars or 0)
        if needed <= 0 or not bool(self.warmup_done):
            return
        self._post_warmup_live_bar_count = min(
            needed,
            int(self._post_warmup_live_bar_count or 0) + 1,
        )
        self.log(
            "WARMUP",
            "ENTRY_GATE_PROGRESS",
            f"bar={self._post_warmup_live_bar_count}/{needed} minute={c.t:%Y-%m-%d %H:%M:%S}",
        )

    def _post_warmup_entry_gate_state(self) -> tuple[bool, str]:
        if bool(self._post_warmup_gate_bypassed_after_trade):
            return True, "POST_WARMUP_BYPASSED_AFTER_TRADE"
        needed = int(self._post_warmup_entry_delay_bars or 0)
        if needed <= 0:
            return True, "POST_WARMUP_READY"
        if not bool(self.warmup_done):
            return False, "WARMUP_NOT_DONE"
        current = max(0, int(self._post_warmup_live_bar_count or 0))
        if current < needed:
            return False, f"POST_WARMUP_BARS_WAIT({current}/{needed})"
        return True, "POST_WARMUP_READY"

    def _startup_entry_gate_state(self) -> tuple[bool, str]:
        needed = max(0.0, float(getattr(self, "_startup_entry_delay_sec", 0.0) or 0.0))
        if needed <= 0.0:
            return True, "STARTUP_READY"
        started_ts = float(getattr(self, "_startup_entry_gate_started_ts", 0.0) or 0.0)
        if started_ts <= 0.0:
            return True, "STARTUP_READY"
        elapsed = max(0.0, time.time() - started_ts)
        if elapsed < needed:
            return False, f"STARTUP_DELAY({elapsed:.1f}/{needed:.1f}s)"
        return True, "STARTUP_READY"

    def _startup_entry_gate_status_text(self) -> str:
        needed = max(0.0, float(getattr(self, "_startup_entry_delay_sec", 0.0) or 0.0))
        if needed <= 0.0:
            return ""
        started_ts = float(getattr(self, "_startup_entry_gate_started_ts", 0.0) or 0.0)
        if started_ts <= 0.0:
            return ""
        remaining = max(0.0, needed - max(0.0, time.time() - started_ts))
        if remaining <= 0.0:
            return ""
        needed_sec = max(1, int(math.ceil(needed)))
        remaining_sec = max(1, int(math.ceil(remaining)))
        # Keep the first AUTO ON state as the familiar "감시대기 10s" text,
        # then count down the remaining seconds while the entry gate is active.
        return f"{needed_sec}s" if remaining_sec >= needed_sec else f"{remaining_sec}s"

    def _entry_lane_state(self, sig_state: dict[str, Any] | None = None) -> tuple[bool, str]:
        state = sig_state if isinstance(sig_state, dict) else {}
        self._refresh_lane_snapshot()
        self._clear_stale_local_unfilled_flag()
        self._release_post_exit_flat_entry_lane_if_safe("ENTRY_LANE_STATE")
        now_ts = time.time()
        in_pos = 1 if self.position_side == "LONG" and self.position_qty > 0 else (-1 if self.position_side == "SHORT" and self.position_qty > 0 else 0)
        pending_active = bool(self.order_core and self.order_core.pending.active)
        if not self.auto_on:
            return False, "AUTO_OFF"
        if not self.connected:
            return False, "DISCONNECTED"
        if not self.order_core:
            return False, "ORDER_CORE_MISSING"
        phase = str(self.auto_cleanup_phase or "AUTO_WAIT_SIGNAL")
        if phase not in ("AUTO_WAIT_SIGNAL", "AUTO_POSITION_WATCH"):
            return False, "AUTO_PRECHECK"
        # Reconnect/takeover safety: never allow new entries until position sync cycle completes.
        if not bool(self.takeover_done):
            return False, "SERVER_SYNC_PENDING"
        if self._is_eod_block_active():
            return False, "EOD_BLOCK"
        if in_pos != 0:
            return False, "POSITION_OPEN"
        if self.entry_inflight or (self._entry_order_guard_active and in_pos == 0):
            return False, "ENTRY_INFLIGHT"
        if self._pending_exit_entry_block_active():
            return False, "PENDING_EXIT_CONFIRM"
        if self.exit_inflight or self.exit_in_progress or self.position_close_pending:
            return False, "EXIT_INFLIGHT"
        if self.has_unfilled_orders or self.has_server_unfilled:
            return False, "UNFILLED_EXISTS"
        if self.cancel_in_progress:
            return False, "CANCEL_IN_PROGRESS"
        if self.order_lane_locked:
            return False, "ORDER_LANE_LOCKED"
        if pending_active:
            pending_action = str(getattr(self.order_core.pending, "action", "") or "PENDING")
            if bool(getattr(self.order_core.pending, "server_recheck_required", False)):
                return False, "ORDER_LANE_LOCKED"
            if pending_action.startswith("EXIT"):
                return False, "EXIT_INFLIGHT"
            if pending_action.startswith("ENTRY"):
                return False, "ENTRY_INFLIGHT"
            return False, "ORDER_LANE_LOCKED"
        if self.server_sync_pending:
            return False, "SERVER_SYNC_PENDING"
        if in_pos == 0 and bool(self._post_exit_server_sync_required):
            return False, "POST_EXIT_SYNC_PENDING"
        if (
            in_pos == 0
            and not bool(self.flat_confirmed)
            and bool(self.takeover_done)
            and (not self._has_server_position())
            and (not self.has_server_unfilled)
            and (not self.has_unfilled_orders)
            and (not self._exit_pending_active_for_submit_guard())
            and (not str(self.pending_exit_reason or "").strip())
            and (not self._is_entry_state_unknown())
            and (not self._is_cancel_state_unknown())
            and (not self._is_exit_flat_confirmation_missing())
        ):
            self.flat_confirmed = True
            self.order_lane_locked = False
            self._post_exit_server_sync_required = False
            self.log("SYNC", "FLAT_CONFIRMED_AUTO", "order_lane=REOPEN/ENTRY_GATE")
        if in_pos == 0 and not bool(self.flat_confirmed):
            return False, "FLAT_UNCONFIRMED"
        if in_pos == 0 and (now_ts - float(self._last_entry_submit_ts or 0.0) < float(self._entry_submit_cooldown_sec or 0.0)):
            return False, "ENTRY_SUBMIT_COOLDOWN"
        if in_pos == 0 and (not self._latest_fill_was_exit()) and self._post_fill_action_cooldown_remaining_sec() > 0.0:
            return False, "POST_FILL_COOLDOWN"
        if in_pos == 0 and self._latest_fill_was_exit() and self._exit_entry_cooldown_remaining_sec() > 0.0:
            return False, "EXIT_REENTRY_COOLDOWN"
        startup_gate_ok, startup_gate_reason = self._startup_entry_gate_state()
        if in_pos == 0 and not startup_gate_ok:
            return False, startup_gate_reason
        warmup_gate_ok, warmup_gate_reason = self._post_warmup_entry_gate_state()
        if in_pos == 0 and not warmup_gate_ok:
            return False, warmup_gate_reason
        if in_pos != 0:
            return False, "POSITION_OPEN"
        if "trade_time_ok" in state and not bool(state.get("trade_time_ok")):
            return False, "TIME_FILTER_BLOCKED"
        return True, "ALLOW_TRADE"

    def _entry_arm_gate_state(self, sig_state: dict[str, Any] | None, side: str) -> tuple[bool, str]:
        """Final hard safety check immediately before ENTRY order submission.

        strategy.signal() returns BUY/SELL_SHORT, but live order submission must still
        verify the exact ARM/FIRE/ready chain so a display mismatch or stale
        signal cannot create an entry.
        """
        state = sig_state if isinstance(sig_state, dict) else {}
        side_n = str(side or "").strip().upper()
        if bool(state.get("use_sma_cross_entry", getattr(self.cfg, "USE_SMA_CROSS_ENTRY", False))):
            return validate_sma10s_fire_gate(state, side_n)
        use_dz5_arm = bool(state.get("use_dz5_arm_entry", getattr(self.cfg, "USE_DZ5_ARM_ENTRY", True)))
        use_price_extrema = bool(state.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
        if use_price_extrema:
            try:
                current_px = float(self.current_price or 0.0)
            except Exception:
                current_px = 0.0
            if side_n in ("BUY", "LONG"):
                fire_lo = state.get("entry_price_fire_long_min")
                fire_hi = state.get("entry_price_fire_long_max")
                checks = {
                    "long_armed": bool(state.get("long_armed", False)),
                    "long_entry_timer_ready": bool(state.get("long_entry_timer_ready", False)),
                    "long_fire_hit": bool(state.get("entry_price_fire_long_hit", False)),
                    "long_fire_price_in_band": price_in_closed_band(current_px, fire_lo, fire_hi),
                    "long_entry_ready": bool(state.get("long_entry_ready", False)),
                    "long_ok": bool(state.get("long_ok", False)),
                }
            elif side_n in ("SELL_SHORT", "SHORT", "SELL"):
                fire_lo = state.get("entry_price_fire_short_min")
                fire_hi = state.get("entry_price_fire_short_max")
                checks = {
                    "short_armed": bool(state.get("short_armed", False)),
                    "short_entry_timer_ready": bool(state.get("short_entry_timer_ready", False)),
                    "short_fire_hit": bool(state.get("entry_price_fire_short_hit", False)),
                    "short_fire_price_in_band": price_in_closed_band(current_px, fire_lo, fire_hi),
                    "short_entry_ready": bool(state.get("short_entry_ready", False)),
                    "short_ok": bool(state.get("short_ok", False)),
                }
            else:
                return False, "UNKNOWN_SIDE"
            failed = [k for k, v in checks.items() if not v]
            if failed:
                return False, "PRICE_TIMER_GATE_BLOCKED:" + ",".join(failed)
            return True, "OK_PRICE_TIMER_FIRE_BAND_GATE"
        if not use_dz5_arm:
            key = "long_ok" if side_n in ("BUY", "LONG") else "short_ok"
            ok = bool(state.get(key, False))
            return (ok, "OK_NON_ARM_MODE" if ok else f"{key.upper()}_FALSE")
        if side_n in ("BUY", "LONG"):
            checks = {
                "long_armed": bool(state.get("long_armed", False)),
                "long_entry_dz5_hit": bool(state.get("entry_price_fire_long_hit", False)),
                "long_entry_ready": bool(state.get("long_entry_ready", False)),
                "long_ok": bool(state.get("long_ok", False)),
            }
        elif side_n in ("SELL_SHORT", "SHORT", "SELL"):
            checks = {
                "short_armed": bool(state.get("short_armed", False)),
                "short_entry_dz5_hit": bool(state.get("entry_price_fire_short_hit", False)),
                "short_entry_ready": bool(state.get("short_entry_ready", False)),
                "short_ok": bool(state.get("short_ok", False)),
            }
        else:
            return False, "UNKNOWN_SIDE"
        failed = [k for k, v in checks.items() if not v]
        if failed:
            # strategy.signal() already returned BUY/SELL_SHORT only after its own
            # entry conditions passed. Do not block again here with a stale or
            # display-only snapshot; keep the mismatch in the log for diagnosis.
            return True, "OK_ARM_GATE_SIGNAL_CONFIRMED_DIAG:" + ",".join(failed)
        return True, "OK_ARM_GATE"

    def _reversal_fire_order_context(
        self,
        sig: dict[str, Any] | None,
        sig_state: dict[str, Any] | None,
        order_side: str,
    ) -> dict[str, Any]:
        """Freeze the active reversal FIRE band into the pending order."""
        signal = sig if isinstance(sig, dict) else {}
        state = sig_state if isinstance(sig_state, dict) else {}
        reason_u = str(signal.get("reason") or self.last_signal_reason or "").strip().upper()
        if "REVERSAL_" not in reason_u and "Z5_DELTA_CONTINUATION" not in reason_u:
            return {}
        side_u = str(order_side or "").strip().upper()
        if bool(state.get("use_sma_cross_entry", False)) and "SMA10S_REVERSAL" in reason_u:
            return {
                "entry_profile": "SMA10S_REVERSAL",
                "fire_signal_reason": reason_u,
                "sma5": state.get("sma10s_5"),
                "sma10": state.get("sma10s_10"),
                "sma20": state.get("sma10s_20"),
                "sma_relation": state.get("sma10s_relation"),
                "sma_regime": state.get("sma10s_regime"),
                "sma_source": state.get("sma10s_source"),
                "sma_is_approx": bool(state.get("sma10s_is_approx", False)),
                "sma_event": state.get("sma10s_event"),
                "event_price": state.get("sma10s_event_price"),
                "event_previous_price": state.get("sma10s_event_previous_price"),
                "event_previous_line": state.get("sma10s_event_previous_line"),
                "event_current_line": state.get("sma10s_event_current_line"),
                "touch_direction": state.get("sma10s_touch_direction"),
                "sequence_ok": bool(
                    state.get("sma10s_buy_fire_event", False)
                    if side_u == "LONG" else state.get("sma10s_sell_fire_event", False)
                ),
            }
        if bool(state.get("use_z1_delta_reversal_entry", False)) and "Z1_DELTA_REVERSAL" in reason_u:
            return {
                "entry_profile": "Z1_DELTA_REVERSAL",
                "fire_signal_reason": reason_u,
                "dz1": state.get("dz1_prev_delta"),
                "prehit_threshold": state.get("z1_delta_reversal_prehit"),
                "arm_threshold": state.get("z1_delta_reversal_arm"),
                "fire_threshold": state.get("z1_delta_reversal_fire_min"),
                "fire_threshold_min": state.get("z1_delta_reversal_fire_min"),
                "fire_threshold_max": state.get("z1_delta_reversal_fire_max"),
                "sequence_ok": bool(
                    state.get("z1_delta_long_fire", False)
                    if side_u == "LONG" else state.get("z1_delta_short_fire", False)
                ),
            }
        if bool(state.get("use_z5_delta_reversal_entry", False)) and "Z5_DELTA_CONTINUATION" in reason_u:
            return {
                "entry_profile": "Z5_DELTA_CONTINUATION",
                "fire_signal_reason": reason_u,
                "dz5": state.get("dz5_prev_delta"),
                "prehit_threshold": state.get("z5_delta_reversal_prehit"),
                "arm_threshold": state.get("z5_delta_reversal_arm"),
                "fire_threshold": state.get("z5_delta_reversal_fire_min"),
                "fire_threshold_min": state.get("z5_delta_reversal_fire_min"),
                "fire_threshold_max": state.get("z5_delta_reversal_fire_max"),
                "sequence_ok": bool(
                    state.get("z5_monitor_long_fire", False)
                    if side_u == "LONG" else state.get("z5_monitor_short_fire", False)
                ),
            }
        if side_u == "LONG":
            low = state.get("reversal_entry_price_fire_long_min")
            high = state.get("reversal_entry_price_fire_long_max")
            if low is None or high is None:
                low = state.get("entry_price_fire_long_min")
                high = state.get("entry_price_fire_long_max")
        elif side_u == "SHORT":
            low = state.get("reversal_entry_price_fire_short_min")
            high = state.get("reversal_entry_price_fire_short_max")
            if low is None or high is None:
                low = state.get("entry_price_fire_short_min")
                high = state.get("entry_price_fire_short_max")
        else:
            return {}
        try:
            low_f, high_f = sorted((float(low), float(high)))
        except (TypeError, ValueError):
            return {}
        return {
            "entry_profile": "REVERSAL",
            "fire_band_low": low_f,
            "fire_band_high": high_f,
            "fire_signal_reason": reason_u,
        }

    def _invalidate_pending_reversal_outside_fire_band(self) -> bool:
        """Cancel an unfilled reversal entry as soon as price/quote leaves FIRE."""
        pending = getattr(getattr(self, "order_core", None), "pending", None)
        if (
            pending is None
            or not bool(getattr(pending, "active", False))
            or str(getattr(pending, "action", "") or "").upper() != "ENTRY"
            or bool(getattr(pending, "cancel_requested", False))
        ):
            return False
        context = getattr(pending, "signal_context", None)
        if not isinstance(context, dict) or str(context.get("entry_profile") or "").upper() != "REVERSAL":
            return False
        if bool(context.get("fire_band_invalidated", False)):
            return False
        try:
            fire_low, fire_high = sorted((float(context.get("fire_band_low")), float(context.get("fire_band_high"))))
        except (TypeError, ValueError):
            return False
        side_u = str(getattr(pending, "side", "") or "").upper()
        live_price = float(self.current_price or 0.0)
        order_quote = float((self.current_ask if side_u == "LONG" else self.current_bid) or 0.0)
        outside_last = live_price > 0.0 and not price_in_closed_band(live_price, fire_low, fire_high)
        outside_quote = order_quote > 0.0 and not price_in_closed_band(order_quote, fire_low, fire_high)
        if not (outside_last or outside_quote):
            return False
        context["fire_band_invalidated"] = True
        context["fire_band_exit_price"] = live_price
        context["fire_band_exit_quote"] = order_quote
        pending.unresolved_reason = "REVERSAL_FIRE_BAND_EXIT"
        self.log(
            "CONTROL",
            "REVERSAL_FIRE_BAND_INVALID",
            (
                f"side={side_u or '-'} last={live_price:.2f} quote={order_quote:.2f} "
                f"band={fire_low:.2f}~{fire_high:.2f} -> CANCEL"
            ),
        )
        self._submit_cancel_cleanup("REVERSAL_FIRE_BAND_EXIT")
        return True

    def _entry_block_diag_details(self, side: str, sig: dict[str, Any] | None, sig_state: dict[str, Any] | None) -> str:
        state = sig_state if isinstance(sig_state, dict) else {}
        signal = sig if isinstance(sig, dict) else {}
        side_n = str(side or signal.get("side") or "").strip().upper()
        is_long = side_n in ("BUY", "LONG")
        pending = getattr(getattr(self, "order_core", None), "pending", None)
        pending_active = bool(getattr(pending, "active", False))
        pending_action = str(getattr(pending, "action", "") or "-") if pending is not None else "-"
        inflight_state = (
            f"entry={int(bool(self.entry_inflight))},"
            f"exit={int(bool(self.exit_inflight))},"
            f"guard={int(bool(self._entry_order_guard_active))},"
            f"close={int(bool(self.position_close_pending))}"
        )
        cooldown_state = (
            f"submit={max(0.0, float(self._entry_submit_cooldown_sec or 0.0) - (time.time() - float(self._last_entry_submit_ts or 0.0))):.3f},"
            f"post_fill={self._post_fill_action_cooldown_remaining_sec():.3f},"
            f"exit_reentry={self._exit_entry_cooldown_remaining_sec():.3f}"
        )
        return (
            f"side={side_n or '-'} "
            f"reason={signal.get('reason') or '-'} "
            f"final_signal={state.get('final_signal') or signal.get('side') or '-'} "
            f"long_armed={int(bool(state.get('long_armed', False)))} "
            f"short_armed={int(bool(state.get('short_armed', False)))} "
            f"long_fire_hit={int(bool(state.get('entry_price_fire_long_hit', False)))} "
            f"short_fire_hit={int(bool(state.get('entry_price_fire_short_hit', False)))} "
            f"long_entry_ready={int(bool(state.get('long_entry_ready', False)))} "
            f"short_entry_ready={int(bool(state.get('short_entry_ready', False)))} "
            f"long_ok={int(bool(state.get('long_ok', False)))} "
            f"short_ok={int(bool(state.get('short_ok', False)))} "
            f"pending_active={int(pending_active)} "
            f"pending_action={pending_action} "
            f"inflight={inflight_state} "
            f"cooldown={cooldown_state} "
            f"server_sync={int(bool(self.server_sync_pending or self.takeover_inflight or self.unfilled_inflight or (not bool(self.takeover_done))))} "
            f"flat_confirmed={int(bool(self.flat_confirmed))} "
            f"lane_locked={int(bool(self.order_lane_locked))} "
            f"auto_on={int(bool(self.auto_on))} "
            f"allow_trade={int(bool(state.get('allow_trade', self.last_allow_trade)))} "
            f"block_reason={state.get('block_reason') or self.last_block_reason or '-'} "
            f"entry_side_ready={int(bool(state.get('long_entry_ready' if is_long else 'short_entry_ready', False)))} "
            f"entry_side_ok={int(bool(state.get('long_ok' if is_long else 'short_ok', False)))}"
        )

    def _log_entry_blocked_before_submit(self, side: str, reason: str, sig: dict[str, Any] | None, sig_state: dict[str, Any] | None, now_dt: datetime | None = None) -> None:
        side_n = str(side or "").strip().upper()
        ts_text = now_dt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(now_dt, datetime) else ""
        block_key = (ts_text, side_n, str(reason or "UNKNOWN"))
        if getattr(self, "_last_entry_block_log_key", None) != block_key:
            self._last_entry_block_log_key = block_key
            self.log(
                "SIGNAL",
                "ENTRY_BLOCKED_BEFORE_SUBMIT",
                f"reason={reason or 'UNKNOWN'} {self._entry_block_diag_details(side_n, sig, sig_state)}",
            )

    def _reverse_entry_lane_state(self) -> tuple[bool, str]:
        self._refresh_lane_snapshot()
        self._clear_stale_local_unfilled_flag()
        self._release_post_exit_flat_entry_lane_if_safe("REVERSE_ENTRY_LANE_STATE")
        if not self.auto_on:
            return False, "AUTO_OFF"
        if not self.order_core:
            return False, "ORDER_CORE_MISSING"
        # Reconnect/takeover safety for reverse-entry as well.
        if not bool(self.takeover_done):
            return False, "SERVER_SYNC_PENDING"
        if self._auto_precheck_active:
            return False, "AUTO_PRECHECK_ACTIVE"
        if self._is_eod_block_active():
            return False, "EOD_BLOCK"
        if self.entry_inflight:
            return False, "ENTRY_INFLIGHT"
        if self.exit_inflight or self.position_close_pending:
            return False, "EXIT_INFLIGHT"
        if self.cancel_in_progress:
            return False, "CANCEL_IN_PROGRESS"
        if self.server_sync_pending or self.takeover_inflight or self.unfilled_inflight:
            return False, "SERVER_SYNC_PENDING"
        if bool(self._post_exit_server_sync_required):
            return False, "POST_EXIT_SYNC_PENDING"
        if not bool(self.flat_confirmed):
            return False, "FLAT_UNCONFIRMED"
        if self.has_unfilled_orders or self.has_server_unfilled:
            return False, "UNFILLED_EXISTS"
        if self.order_lane_locked:
            return False, "ORDER_LANE_LOCKED"
        if self.order_core.pending.active:
            return False, "ORDER_LANE_LOCKED"
        if (not self._latest_fill_was_exit()) and self._post_fill_action_cooldown_remaining_sec() > 0.0:
            return False, "POST_FILL_COOLDOWN"
        allow_same_bar_reverse = bool(getattr(self.cfg, "ALLOW_SAME_BAR_REVERSE", False))
        if (not allow_same_bar_reverse) and self._latest_fill_was_exit() and self._exit_entry_cooldown_remaining_sec() > 0.0:
            return False, "EXIT_REENTRY_COOLDOWN"
        warmup_gate_ok, warmup_gate_reason = self._post_warmup_entry_gate_state()
        if not warmup_gate_ok:
            return False, warmup_gate_reason
        return True, "ALLOW_REVERSE"

    @staticmethod
    def _auto_reverse_allowed_reasons() -> set[str]:
        # Reverse is EXIT ONLY. After REVERSE_SIGNAL exit is filled, do not place
        # an automatic opposite order from the reverse retry path. Normal ENTRY
        # logic must handle any new position.
        return set()


    def _queue_reverse_after_sync(self, prior_side: str, exit_reason: str) -> None:
        reason_text = str(exit_reason or "").strip().upper()
        prior_text = str(prior_side or "FLAT").strip().upper()
        if prior_text not in ("LONG", "SHORT"):
            self._reverse_after_sync_pending = False
            self._reverse_after_sync_prior_side = "FLAT"
            self._reverse_after_sync_reason = ""
            return
        if reason_text in self._auto_reverse_allowed_reasons():
            self._reverse_after_sync_pending = True
            self._reverse_after_sync_prior_side = prior_text
            self._reverse_after_sync_reason = reason_text
            self.log("SIGNAL", "REVERSE_DEFERRED", f"prior={prior_text} reason={reason_text} wait=POST_EXIT_SYNC")
            return
        self._reverse_after_sync_pending = False
        self._reverse_after_sync_prior_side = "FLAT"
        self._reverse_after_sync_reason = ""
        # Do not touch _post_exit_entry_* here. Reverse-after-sync and
        # post-exit overlap-entry are separate lanes.

    def _clear_reverse_after_sync(self) -> None:
        self._reverse_after_sync_pending = False
        self._reverse_after_sync_prior_side = "FLAT"
        self._reverse_after_sync_reason = ""
        # Do not clear _post_exit_entry_* here. The same-tick overlap-entry
        # lane is independent from reverse-after-sync and is scheduled after
        # EXIT_FILLED. Clearing it here reintroduces the historical bug where
        # MFE/FIXED_LOSS exit filled correctly but the saved opposite ENTRY
        # snapshot disappeared before _schedule_post_exit_overlap_entry().

    def _arm_reverse_after_server_flat_sync(self, source: str) -> None:
        if bool(self._post_exit_server_sync_required):
            return
        if not bool(self._reverse_after_sync_pending):
            return
        prior_side = str(self._reverse_after_sync_prior_side or "FLAT")
        exit_reason = str(self._reverse_after_sync_reason or "")
        self._clear_reverse_after_sync()
        self._arm_reverse_retry(prior_side, exit_reason)
        if bool(self._reverse_retry_armed):
            self.log("SIGNAL", "REVERSE_ARMED_POST_SYNC", f"source={source} prior={prior_side} reason={exit_reason}")

    def _should_block_takeover_local_override(self, reason: str, matched_side: str, matched_qty: int) -> bool:
        reason_n = self._normalize_query_reason(reason)
        local_side = str(self.position_side or "FLAT").upper()
        local_qty = int(self.position_qty or 0)
        local_has_pos = local_side in ("LONG", "SHORT") and local_qty > 0
        order_pending = bool(
            self.entry_inflight
            or self.exit_inflight
            or self.position_close_pending
            or self._is_entry_state_unknown()
            or self._is_exit_flat_confirmation_missing()
            or (self.order_core and getattr(self.order_core.pending, "active", False))
        )
        if reason_n.startswith("AUTO_PRECHECK"):
            if not local_has_pos:
                return False
            return bool((local_has_pos and (local_side != matched_side or local_qty != int(matched_qty or 0))) or order_pending or self._post_exit_server_sync_required)
        if reason_n == "EXIT_FILLED" and bool(self._post_exit_server_sync_required):
            # EXIT_FILLED must be verified against server balance.  If the server
            # still reports a position, do not block that authoritative snapshot;
            # keep/take over the position instead of leaving the UI in local FLAT.
            return False
        if reason_n in {"TIMER_RECHECK", "TIMER_HEARTBEAT", "EXIT_FILLED"}:
            return bool(local_has_pos or order_pending or self._post_exit_server_sync_required)
        return False

    def _arm_reverse_retry(self, prior_side: str, exit_reason: str) -> None:
        reason_text = str(exit_reason or "").strip().upper()
        prior_text = str(prior_side or "FLAT").strip().upper()
        if prior_text not in ("LONG", "SHORT"):
            return
        if self._is_eod_force_exit_due() or reason_text.startswith("EOD"):
            self._clear_reverse_retry()
            self.log("SIGNAL", "REVERSE_SKIP", f"reason=EOD_BLOCK exit_reason={reason_text or '-'} prior={prior_text}")
            return
        auto_reverse_reasons = self._auto_reverse_allowed_reasons()
        if reason_text not in auto_reverse_reasons:
            return

        delay_sec = max(
            0.0,
            float(
                self._reverse_signal_reentry_cooldown_sec
                if reason_text == "REVERSE_SIGNAL"
                else self._reverse_entry_cooldown_sec
            ) or 0.0,
        )
        self._reverse_retry_armed = True
        self._reverse_retry_prior_side = prior_text
        self._reverse_retry_reason = reason_text
        self._reverse_retry_armed_ts = time.time()
        self._reverse_retry_due_ts = self._reverse_retry_armed_ts + delay_sec
        self._reverse_retry_attempts = 0
        self.log("SIGNAL", "REVERSE_ARMED", f"prior={prior_text} reason={reason_text} delay={delay_sec:.1f}s")
        self._schedule_reverse_retry_timer(delay_sec)

    def _schedule_reverse_retry_timer(self, delay_sec: float | None = None) -> None:
        if not bool(self._reverse_retry_armed):
            return
        if bool(self._reverse_retry_timer_active):
            return
        now_ts = time.time()
        if delay_sec is None:
            delay_sec = max(0.0, float(self._reverse_retry_due_ts or 0.0) - now_ts)
        delay_ms = int(max(0.0, float(delay_sec or 0.0)) * 1000)
        self._reverse_retry_timer_active = True
        QTimer.singleShot(delay_ms, self._on_reverse_retry_timer)

    def _on_reverse_retry_timer(self) -> None:
        self._reverse_retry_timer_active = False
        if not bool(self._reverse_retry_armed):
            return
        now_ts = time.time()
        if float(self._reverse_retry_due_ts or 0.0) > now_ts:
            self._schedule_reverse_retry_timer(float(self._reverse_retry_due_ts or 0.0) - now_ts)
            return
        self._maybe_retry_reverse_after_exit(force=True)

    def _clear_reverse_retry(self) -> None:
        self._reverse_retry_armed = False
        self._reverse_retry_prior_side = "FLAT"
        self._reverse_retry_reason = ""
        self._reverse_retry_armed_ts = 0.0
        self._reverse_retry_due_ts = 0.0
        self._reverse_retry_attempts = 0

    def _clear_forced_reverse_pending(self) -> None:
        self._forced_reverse_entry_pending = False
        self._forced_reverse_entry_side = ""
        self._forced_reverse_entry_sig_state = {}

    def _maybe_retry_reverse_after_exit(self, force: bool = False) -> bool:
        if not bool(self._reverse_retry_armed):
            return False
        if self._is_eod_force_exit_due() or str(self.last_exit_reason or "").strip().upper().startswith("EOD"):
            self.log("SIGNAL", "REVERSE_RETRY_CLEAR", f"reason=EOD_BLOCK exit_reason={self._reverse_retry_reason or '-'}")
            self._clear_reverse_retry()
            return False
        now_ts = time.time()
        if not force and float(self._reverse_retry_due_ts or 0.0) > now_ts:
            return False
        if str(self.position_side or "FLAT").upper() != "FLAT" or int(self.position_qty or 0) != 0:
            return False
        if not self.auto_on or not self.order_core:
            return False
        pending = getattr(self.order_core, "pending", None)
        if bool(getattr(pending, "active", False)):
            return False
        reversed_ok = self._try_immediate_reverse_after_exit(self._reverse_retry_prior_side, self._reverse_retry_reason)
        self._reverse_retry_attempts = int(self._reverse_retry_attempts or 0) + 1
        if reversed_ok:
            self.log("SIGNAL", "REVERSE_RETRY_OK", f"attempt={self._reverse_retry_attempts} reason={self._reverse_retry_reason}")
            self._clear_reverse_retry()
            return True
        # If the 3-second timer fires while the server/order lane is still busy, retry shortly.
        # Do not wait for a new ARM or a fresh long_ok/short_ok signal.
        if int(self._reverse_retry_attempts or 0) >= 20:
            self.log("SIGNAL", "REVERSE_RETRY_GIVEUP", f"attempts={self._reverse_retry_attempts} reason={self._reverse_retry_reason}")
            self._clear_reverse_retry()
        else:
            self._reverse_retry_due_ts = time.time() + 1.0
            self._schedule_reverse_retry_timer(1.0)
        return False

    def _schedule_forced_reverse_entry_after_exit(self, prior_side: str, exit_reason: str) -> None:
        """Run forced reverse entry on the next Qt event-loop tick.

        This preserves immediate reverse behavior while avoiding the OrderCore
        EXIT_FILLED callback race: OrderCore resets its EXIT pending only after
        this callback returns. A zero-delay timer runs right after that cleanup.
        """
        prior_text = str(prior_side or "FLAT").strip().upper()
        reason_text = str(exit_reason or "").strip().upper()
        if reason_text != "REVERSE_SIGNAL" or prior_text not in ("LONG", "SHORT"):
            return
        if self._is_eod_block_active() or self._is_eod_force_exit_due():
            self.log("SIGNAL", "REVERSE_ENTRY_SKIP", "reason=EOD_BLOCK")
            self._clear_forced_reverse_pending()
            return
        if not bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False)):
            self.log("SIGNAL", "REVERSE_ENTRY_SKIP", "reason=REVERSE_SIGNAL_DISABLED stale_reason=1")
            return
        self._post_exit_server_sync_required = False
        self.flat_confirmed = True
        self.order_lane_locked = False
        self.entry_inflight = False
        self.exit_inflight = False
        self.position_close_pending = False
        self.server_sync_pending = False
        self._clear_reverse_after_sync()
        self._clear_reverse_retry()
        # EXIT_FILLED is an authoritative broker fill event. Preserve the FIRE
        # snapshot first, then make local state FLAT before the opposite order.
        self._clear_local_position_after_server_flat_confirm("REVERSE_EXIT_FILLED")
        if not bool(getattr(self.cfg, "ALLOW_REVERSE_SIGNAL_REENTRY", False)):
            self._clear_forced_reverse_pending()
            self.log("SIGNAL", "REVERSE_ENTRY_SKIP", "reason=REVERSE_REENTRY_DISABLED exit_only=1")
            return
        self._forced_reverse_entry_pending = True
        self._forced_reverse_entry_side = ("SHORT" if prior_text == "LONG" else "LONG")
        if not isinstance(getattr(self, "_forced_reverse_entry_sig_state", None), dict) or not self._forced_reverse_entry_sig_state:
            self._forced_reverse_entry_sig_state = dict(self.last_sig_state or {}) if isinstance(self.last_sig_state, dict) else {}
        self.log(
            "SIGNAL",
            "REVERSE_ENTRY_SCHEDULE",
            f"prior={prior_text} target={self._forced_reverse_entry_side or '-'} reason=REVERSE_SIGNAL delay=0ms snapshot={int(bool(self._forced_reverse_entry_sig_state))}",
        )

        def _run() -> None:
            sent = self._submit_forced_reverse_entry_after_exit(prior_text, reason_text)
            if sent:
                self.log("SYNC", "POST_EXIT_SYNC_BYPASS", "reason=REVERSE_SIGNAL immediate_reverse=1")
            else:
                self._clear_forced_reverse_pending()
                self.log("SIGNAL", "REVERSE_ENTRY_DIRECT_FAIL", "reason=SEND_FAIL no_retry=1")

        QTimer.singleShot(0, _run)

    def _reverse_entry_signal_ready(self, prior_side: str) -> tuple[bool, str, dict[str, Any], str]:
        sig_state = self.last_sig_state if isinstance(self.last_sig_state, dict) else {}
        prior_text = str(prior_side or "").strip().upper()
        warmup_gate_ok, warmup_gate_reason = self._post_warmup_entry_gate_state()
        if not warmup_gate_ok:
            return False, warmup_gate_reason, sig_state, ""
        if not bool(sig_state.get("trade_time_ok", True)):
            return False, "TIME_FILTER_BLOCKED", sig_state, ""
        if prior_text == "LONG":
            if not bool(getattr(self.cfg, "ENABLE_SHORT", False)):
                return False, "ENABLE_SHORT_OFF", sig_state, "SHORT"
            if not bool(sig_state.get("reverse_short_entry_gate", sig_state.get("short_ok", False))):
                return False, f"SHORT_REVERSE_NOT_READY({str(sig_state.get('block_reason') or '-').strip() or '-'})", sig_state, "SHORT"
            return True, "ALLOW_REVERSE_SHORT", sig_state, "SHORT"
        if prior_text == "SHORT":
            if not bool(sig_state.get("reverse_long_entry_gate", sig_state.get("long_ok", False))):
                return False, f"LONG_REVERSE_NOT_READY({str(sig_state.get('block_reason') or '-').strip() or '-'})", sig_state, "LONG"
            return True, "ALLOW_REVERSE_LONG", sig_state, "LONG"
        return False, f"UNKNOWN_PRIOR_SIDE({prior_text or '-'})", sig_state, ""

    def _reverse_entry_signal_ready_from_snapshot(self, prior_side: str, sig_state: dict[str, Any] | None) -> tuple[bool, str, dict[str, Any], str]:
        """Validate the saved opposite-entry snapshot that created REVERSE_SIGNAL.

        REVERSE_SIGNAL exits are generated from an opposite ARM/FIRE snapshot.
        The broker EXIT fill can arrive after one or more live ticks, so using
        self.last_sig_state at fill time can lose the original gate.  This
        helper keeps the direction gate tied to the signal snapshot that sent
        the EXIT order, while still applying current operational safety gates
        such as warmup and time filter.
        """
        ss = sig_state if isinstance(sig_state, dict) else {}
        prior_text = str(prior_side or "").strip().upper()
        warmup_gate_ok, warmup_gate_reason = self._post_warmup_entry_gate_state()
        if not warmup_gate_ok:
            return False, warmup_gate_reason, ss, ""
        if not bool(ss.get("trade_time_ok", True)):
            return False, "TIME_FILTER_BLOCKED", ss, ""
        if prior_text == "LONG":
            if not bool(getattr(self.cfg, "ENABLE_SHORT", False)):
                return False, "ENABLE_SHORT_OFF", ss, "SHORT"
            if not bool(ss.get("reverse_short_entry_gate", ss.get("short_entry_ready", ss.get("short_ok", False)))):
                return False, f"SHORT_REVERSE_SNAPSHOT_NOT_READY({str(ss.get('block_reason') or '-').strip() or '-'})", ss, "SHORT"
            return True, "ALLOW_REVERSE_SHORT_SNAPSHOT", ss, "SHORT"
        if prior_text == "SHORT":
            if not bool(ss.get("reverse_long_entry_gate", ss.get("long_entry_ready", ss.get("long_ok", False)))):
                return False, f"LONG_REVERSE_SNAPSHOT_NOT_READY({str(ss.get('block_reason') or '-').strip() or '-'})", ss, "LONG"
            return True, "ALLOW_REVERSE_LONG_SNAPSHOT", ss, "LONG"
        return False, f"UNKNOWN_PRIOR_SIDE({prior_text or '-'})", ss, ""

    def _format_reverse_gate_reason(self, gate_reason: str, sig_state: dict[str, Any] | None, reverse_side: str = "") -> str:
        ss = sig_state if isinstance(sig_state, dict) else {}
        side_t = str(reverse_side or "").strip().upper()
        reason_t = str(gate_reason or "-").strip() or "-"
        block_t = str(ss.get("block_reason") or "-").strip() or "-"
        final_t = str(ss.get("final_signal") or "-").strip() or "-"
        long_ok = int(bool(ss.get("long_ok", False)))
        short_ok = int(bool(ss.get("short_ok", False)))
        reverse_long_ok = int(bool(ss.get("reverse_long_entry_gate", False)))
        reverse_short_ok = int(bool(ss.get("reverse_short_entry_gate", False)))
        long_arm = int(bool(ss.get("long_armed", False)))
        short_arm = int(bool(ss.get("short_armed", False)))
        if side_t == "SHORT":
            return (
                f"target=SHORT reason={reason_t} short_ok={short_ok} reverse_short_ok={reverse_short_ok} short_arm={short_arm} "
                f"final={final_t} block={block_t}"
            )
        if side_t == "LONG":
            return (
                f"target=LONG reason={reason_t} long_ok={long_ok} reverse_long_ok={reverse_long_ok} long_arm={long_arm} "
                f"final={final_t} block={block_t}"
            )
        return (
            f"target=- reason={reason_t} long_ok={long_ok} short_ok={short_ok} "
            f"reverse_long_ok={reverse_long_ok} reverse_short_ok={reverse_short_ok} "
            f"long_arm={long_arm} short_arm={short_arm} final={final_t} block={block_t}"
        )

    def _clear_post_exit_overlap_entry(self) -> None:
        self._post_exit_entry_pending = False
        self._post_exit_entry_side = ""
        self._post_exit_entry_prior_side = "FLAT"
        self._post_exit_entry_reason = ""
        self._post_exit_entry_sig_state = {}
        self._post_exit_entry_armed_ts = 0.0
        self._post_exit_entry_attempts = 0
        self._post_exit_entry_send_attempts = 0
        self._post_exit_entry_timer_active = False
        self._post_exit_entry_awaiting_confirm = False
        self._post_exit_entry_last_submit_ts = 0.0

    def _post_exit_overlap_entry_age_sec(self) -> float:
        return max(0.0, time.time() - float(getattr(self, "_post_exit_entry_armed_ts", 0.0) or 0.0))

    def _is_post_exit_overlap_entry_ready(self, prior_side: str = "", exit_reason: str = "") -> bool:
        if not bool(getattr(self, "_post_exit_entry_pending", False)):
            return False
        prior_text = str(prior_side or getattr(self, "_post_exit_entry_prior_side", "") or "").strip().upper()
        reason_text = str(exit_reason or getattr(self, "_post_exit_entry_reason", "") or "").strip().upper()
        if prior_text and prior_text != str(getattr(self, "_post_exit_entry_prior_side", "") or "").strip().upper():
            return False
        if reason_text and reason_text != str(getattr(self, "_post_exit_entry_reason", "") or "").strip().upper():
            return False
        if reason_text not in {"MFE_TRAIL", "MFE_PROTECT", "FIXED_LOSS"}:
            return False
        if reason_text == "MFE_PROTECT" and not bool(getattr(self, "_mfe_protect_post_exit_entry_enabled", False)):
            return False
        return bool(self._post_exit_overlap_entry_age_sec() <= float(getattr(self, "_post_exit_entry_window_sec", 8.0) or 8.0))

    def _queue_post_exit_overlap_entry(self, prior_side: str, exit_reason: str, sig_state: dict[str, Any] | None) -> bool:
        """Preserve an ENTRY FIRE snapshot that overlaps an exit.

        EXIT is always submitted first. This queue only remembers an ENTRY
        condition that was already true on the same signal snapshot; it does
        not create a new signal after the fact.
        """
        self._clear_post_exit_overlap_entry()
        reason_text = str(exit_reason or "").strip().upper()
        prior_text = str(prior_side or "FLAT").strip().upper()
        if reason_text not in {"MFE_TRAIL", "MFE_PROTECT", "FIXED_LOSS"} or prior_text not in ("LONG", "SHORT"):
            return False
        if reason_text == "MFE_PROTECT" and not bool(getattr(self, "_mfe_protect_post_exit_entry_enabled", False)):
            self.log("SIGNAL", "POST_EXIT_ENTRY_SKIP", "reason=MFE_PROTECT_POST_EXIT_ENTRY_DISABLED reset_arms=1")
            return False
        if self._is_eod_block_active() or self._is_eod_force_exit_due():
            return False
        state = dict(sig_state) if isinstance(sig_state, dict) else {}
        target_side = ""
        if reason_text in {"MFE_TRAIL", "MFE_PROTECT"}:
            # Use the exact entry regime/side already calculated in the exit tick.
            # This preserves REVERSAL->TREND and TREND->REVERSAL switching after
            # a flat-confirmed exit instead of reusing the old position regime.
            if prior_text == "LONG" and bool(getattr(self.cfg, "ENABLE_SHORT", False)) and bool(state.get("short_ok", False)):
                target_side = "SHORT"
            elif prior_text == "SHORT" and bool(state.get("long_ok", False)):
                target_side = "LONG"
        elif reason_text == "FIXED_LOSS":
            final_signal = str(state.get("final_signal") or "").strip().upper()
            if final_signal == "BUY" and bool(state.get("long_ok", False)):
                target_side = "LONG"
            elif final_signal == "SELL_SHORT" and bool(getattr(self.cfg, "ENABLE_SHORT", False)) and bool(state.get("short_ok", False)):
                target_side = "SHORT"
            elif bool(state.get("long_ok", False)) and not bool(state.get("short_ok", False)):
                target_side = "LONG"
            elif bool(getattr(self.cfg, "ENABLE_SHORT", False)) and bool(state.get("short_ok", False)) and not bool(state.get("long_ok", False)):
                target_side = "SHORT"
        if not target_side:
            return False
        self._post_exit_entry_pending = True
        self._post_exit_entry_side = target_side
        self._post_exit_entry_prior_side = prior_text
        self._post_exit_entry_reason = reason_text
        self._post_exit_entry_sig_state = state
        self._post_exit_entry_armed_ts = time.time()
        self._post_exit_entry_attempts = 0
        self._post_exit_entry_send_attempts = 0
        self._post_exit_entry_awaiting_confirm = False
        self._post_exit_entry_last_submit_ts = 0.0
        self.log(
            "SIGNAL",
            "POST_EXIT_ENTRY_QUEUED",
            f"prior={prior_text} target={target_side} exit_reason={reason_text} regime={str(state.get('entry_regime') or state.get('active_entry_regime') or '-')} short_ok={int(bool(state.get('short_ok', False)))} long_ok={int(bool(state.get('long_ok', False)))} window={self._post_exit_entry_window_sec:.1f}s",
        )
        return True

    def _schedule_post_exit_overlap_entry(self, delay_ms: int | None = None) -> None:
        if not bool(getattr(self, "_post_exit_entry_pending", False)):
            return
        if bool(getattr(self, "_post_exit_entry_timer_active", False)):
            return
        self._post_exit_entry_timer_active = True
        if delay_ms is None:
            delay_ms = int(getattr(self, "_post_exit_entry_delay_ms", 350) or 350)
        QTimer.singleShot(max(0, int(delay_ms or 0)), self._on_post_exit_overlap_entry_timer)

    def _on_post_exit_overlap_entry_timer(self) -> None:
        self._post_exit_entry_timer_active = False
        self._submit_post_exit_overlap_entry()

    def _post_exit_entry_pending_reason(self) -> str:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        return str(getattr(pending, "reason", "") or "") if pending is not None else ""

    def _clear_stale_exit_pending_after_exit_fill(self, reason: str = "POST_EXIT_ENTRY") -> bool:
        """Clear an EXIT pending object that survived after a confirmed EXIT_FILLED callback.

        OrderCore resets its pending object after emitting EXIT_FILLED.  In very fast
        exit->entry sequences, the post-exit entry timer can occasionally observe
        the old EXIT pending object before the lane snapshot has been refreshed.
        Once local state is already FLAT and all EXIT flags are down, that pending
        object is stale and must not block the saved post-exit ENTRY.
        """
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        if pending is None or not bool(getattr(pending, "active", False)):
            return False
        pending_action = str(getattr(pending, "action", "") or "").upper()
        if not pending_action.startswith("EXIT"):
            return False
        local_flat = bool(str(self.position_side or "FLAT").upper() == "FLAT" and int(self.position_qty or 0) == 0)
        exit_flags_clear = bool((not self.exit_inflight) and (not self.exit_in_progress) and (not self.position_close_pending))
        last_exit_fill_ts = float(getattr(self, "_last_exit_filled_ts", 0.0) or 0.0)
        recent_exit_fill = bool(
            last_exit_fill_ts > 0.0
            and (time.time() - last_exit_fill_ts) <= max(3.0, float(getattr(self, "_post_exit_entry_window_sec", 8.0) or 8.0) + 2.0)
        )
        if not (local_flat and exit_flags_clear and recent_exit_fill):
            return False
        try:
            self.log(
                "SIGNAL",
                "POST_EXIT_ENTRY_CLEAR_STALE_EXIT_PENDING",
                (
                    f"reason={reason} action={getattr(pending, 'action', '-') or '-'} "
                    f"side={getattr(pending, 'side', '-') or '-'} "
                    f"pending_reason={getattr(pending, 'reason', '-') or '-'}"
                ),
            )
            pending.active = False
            pending.action = ""
            pending.side = ""
            pending.qty = 0
            pending.remaining_qty = 0
            pending.cancel_requested = False
            pending.server_recheck_required = False
            pending.auto_cancel_phase = ""
            pending.unresolved_reason = ""
            return True
        except Exception as exc:
            self.log("WARN", "POST_EXIT_ENTRY_CLEAR_STALE_EXIT_PENDING_ERROR", repr(exc))
            return False

    def _is_post_exit_mfe_entry_context(self, event: ExecutionEvent | None = None) -> bool:
        reason = self._post_exit_entry_pending_reason().upper()
        if event is not None:
            try:
                raw = event.raw if isinstance(event.raw, dict) else {}
                reason = reason or str(raw.get("reason") or "").upper()
            except Exception:
                pass
        return bool(
            str(reason or "").startswith("AUTO_POST_EXIT_MFE_ENTRY_")
            or str(reason or "").startswith("AUTO_POST_EXIT_MFE_PROTECT_ENTRY_")
            or str(reason or "").startswith("AUTO_POST_EXIT_FIXED_LOSS_ENTRY_")
            or str(reason or "").startswith("AUTO_POST_EXIT_EXIT_Z_ENTRY_")
            or bool(getattr(self, "_post_exit_entry_awaiting_confirm", False))
            or bool(getattr(self, "_post_exit_entry_pending", False))
        )

    def _schedule_post_exit_entry_unknown_recheck(self, delay_ms: int | None = None) -> None:
        if delay_ms is None:
            delay_ms = int(max(200, float(getattr(self, "_post_exit_entry_unknown_clear_sec", 2.2) or 2.2) * 1000.0))
        QTimer.singleShot(max(0, int(delay_ms or 0)), self._on_post_exit_entry_unknown_recheck)

    def _on_post_exit_entry_unknown_recheck(self) -> None:
        if not self._is_post_exit_mfe_entry_context():
            return
        if not self.server_sync_pending:
            self._request_server_sync(
                reason="POST_EXIT_ENTRY_UNKNOWN",
                include_takeover=True,
                include_unfilled=True,
                start_warmup_after_response=False,
                force=True,
            )

    def _release_or_retry_post_exit_entry_unknown(self, reason: str = "SERVER_CLEAR") -> bool:
        """Release the special post-exit-entry guard when server shows no evidence.

        This is deliberately limited to AUTO_POST_EXIT_* overlap-entry orders. A
        normal ENTRY timeout still keeps the conservative unknown-order guard.
        """
        if not self._is_post_exit_mfe_entry_context():
            return False
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        sent_ts = float(getattr(pending, "sent_ts", 0.0) or getattr(self, "_post_exit_entry_last_submit_ts", 0.0) or 0.0)
        guard_ts = float(getattr(self, "_entry_order_guard_ts", 0.0) or 0.0)
        ref_ts = max(sent_ts, guard_ts, float(getattr(self, "_post_exit_entry_last_submit_ts", 0.0) or 0.0))
        age = time.time() - ref_ts if ref_ts > 0 else self._post_exit_overlap_entry_age_sec()
        if age < float(getattr(self, "_post_exit_entry_unknown_clear_sec", 2.2) or 2.2):
            return False
        if self._has_server_position() or self.has_server_unfilled or self.has_unfilled_orders:
            return False
        retry_ok = bool(
            self._is_post_exit_overlap_entry_ready()
            and int(getattr(self, "_post_exit_entry_send_attempts", 0) or 0) < int(getattr(self, "_post_exit_entry_max_send_attempts", 2) or 2)
        )
        self.log(
            "SIGNAL",
            "POST_EXIT_ENTRY_UNKNOWN_CLEAR",
            f"reason={reason} age={age:.2f}s retry={int(retry_ok)} attempts={int(getattr(self, '_post_exit_entry_send_attempts', 0) or 0)}",
        )
        self._clear_stale_entry_guard(f"POST_EXIT_ENTRY_UNKNOWN_CLEAR:{reason}")
        self.cancel_needs_server_check = False
        self.cancel_in_progress = False
        self.server_sync_pending = False
        if retry_ok:
            self._post_exit_entry_awaiting_confirm = False
            self._post_exit_entry_last_submit_ts = 0.0
            self._schedule_post_exit_overlap_entry(250)
        else:
            self._clear_post_exit_overlap_entry()
        return True

    def _submit_post_exit_overlap_entry(self) -> bool:
        if not self._is_post_exit_overlap_entry_ready():
            self.log("SIGNAL", "POST_EXIT_ENTRY_CLEAR", "reason=EXPIRED_OR_MISMATCH")
            self._clear_post_exit_overlap_entry()
            return False
        target_side = str(getattr(self, "_post_exit_entry_side", "") or "").strip().upper()
        prior_side = str(getattr(self, "_post_exit_entry_prior_side", "") or "").strip().upper()
        reason_text = str(getattr(self, "_post_exit_entry_reason", "") or "").strip().upper()
        sig_state = dict(getattr(self, "_post_exit_entry_sig_state", {}) or {})
        if reason_text == "MFE_PROTECT" and not bool(getattr(self, "_mfe_protect_post_exit_entry_enabled", False)):
            self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", "reason=MFE_PROTECT_POST_EXIT_ENTRY_DISABLED")
            self._clear_post_exit_overlap_entry()
            if bool(getattr(self, "_mfe_protect_reset_entry_arms", True)):
                self._clear_entry_signal_state_after_flat("MFE_PROTECT_POST_EXIT_ENTRY_DISABLED")
            return False
        self._post_exit_entry_attempts = int(getattr(self, "_post_exit_entry_attempts", 0) or 0) + 1
        if self._is_eod_block_active() or self._is_eod_force_exit_due():
            self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=EOD_BLOCK prior={prior_side} target={target_side}")
            self._clear_post_exit_overlap_entry()
            return False
        if (not self.auto_on) or (not self.connected) or (not self.account) or (not self.order_core):
            self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=NOT_READY auto={int(bool(self.auto_on))} conn={int(bool(self.connected))} account={int(bool(self.account))}")
            self._clear_post_exit_overlap_entry()
            return False
        if str(self.position_side or "FLAT").upper() != "FLAT" or int(self.position_qty or 0) != 0:
            if self._post_exit_entry_attempts < 20:
                self.log("SIGNAL", "POST_EXIT_ENTRY_WAIT", f"reason=POSITION_NOT_FLAT pos={self.position_side}/{self.position_qty} attempt={self._post_exit_entry_attempts}")
                self._schedule_post_exit_overlap_entry(100)
            else:
                self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=POSITION_NOT_FLAT_GIVEUP pos={self.position_side}/{self.position_qty}")
                self._clear_post_exit_overlap_entry()
            return False
        pending = getattr(self.order_core, "pending", None)
        if bool(getattr(pending, "active", False)):
            pending_action = str(getattr(pending, "action", "") or "-").upper()
            if pending_action.startswith("EXIT") and self._clear_stale_exit_pending_after_exit_fill("POST_EXIT_OVERLAP_ENTRY"):
                pending = getattr(self.order_core, "pending", None)
                pending_action = str(getattr(pending, "action", "") or "-").upper() if pending is not None else ""
            if bool(getattr(pending, "active", False)):
                if self._post_exit_entry_attempts < 20 and pending_action.startswith("EXIT"):
                    self.log("SIGNAL", "POST_EXIT_ENTRY_WAIT", f"reason=PENDING_EXIT_CLEANUP action={pending_action} attempt={self._post_exit_entry_attempts}")
                    self._schedule_post_exit_overlap_entry(100)
                    return False
                self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=PENDING_ACTIVE action={pending_action}")
                self._clear_post_exit_overlap_entry()
                return False
        if self.has_unfilled_orders or self.has_server_unfilled:
            self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=UNFILLED_EXISTS local={int(bool(self.has_unfilled_orders))} server={int(bool(self.has_server_unfilled))}")
            self._clear_post_exit_overlap_entry()
            return False
        cooldown_remain = self._exit_entry_cooldown_remaining_sec()
        if cooldown_remain > 0.0:
            self.log("SIGNAL", "POST_EXIT_ENTRY_WAIT", f"reason=EXIT_REENTRY_COOLDOWN remain={cooldown_remain:.1f}s attempt={self._post_exit_entry_attempts}")
            self._schedule_post_exit_overlap_entry(max(200, int(cooldown_remain * 1000)))
            return False
        self.exit_inflight = False
        self.exit_in_progress = False
        self.position_close_pending = False
        self.exit_needs_server_check = False
        self._post_exit_server_sync_required = False
        self.server_sync_pending = False
        self.order_lane_locked = False
        self.flat_confirmed = True
        self.last_sig_state = sig_state
        def _v9_float(_value):
            try:
                if _value is None:
                    return None
                return float(_value)
            except Exception:
                return None
        z5_now = None
        for _candidate in (
            sig_state.get("z5"),
            sig_state.get("z5_now"),
            sig_state.get("current_z5"),
            getattr(self, "last_z_5m", None),
        ):
            z5_now = _v9_float(_candidate)
            if z5_now is not None:
                break
        trend_floor = _v9_float(getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MIN"))
        trend_max = _v9_float(getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MAX"))
        if (
            bool(getattr(self.cfg, "USE_TREND_Z5_ZONE_OPPOSITE_ENTRY_BLOCK_V9", False))
            and z5_now is not None
            and trend_floor is not None
            and trend_max is not None
        ):
            trend_floor = abs(float(trend_floor))
            trend_max = abs(float(trend_max))
            if trend_floor > trend_max:
                trend_floor, trend_max = trend_max, trend_floor
            upper_trend_zone = bool(z5_now >= trend_floor and z5_now <= trend_max)
            lower_trend_zone = bool(z5_now <= -trend_floor and z5_now >= -trend_max)
            if target_side == "SHORT" and upper_trend_zone:
                self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=TREND_Z5_ZONE_SHORT_BLOCK_V9 z5={z5_now:+.3f}")
                self._clear_post_exit_overlap_entry()
                return False
            if target_side == "LONG" and lower_trend_zone:
                self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=TREND_Z5_ZONE_LONG_BLOCK_V9 z5={z5_now:+.3f}")
                self._clear_post_exit_overlap_entry()
                return False
        self._post_exit_entry_send_attempts = int(getattr(self, "_post_exit_entry_send_attempts", 0) or 0) + 1
        ok = False
        if target_side == "SHORT":
            if not bool(sig_state.get("short_ok", False)):
                self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", "reason=SHORT_SNAPSHOT_NOT_READY")
                self._clear_post_exit_overlap_entry()
                return False
            self.log("SIGNAL", "POST_EXIT_ENTRY_FORCE", f"prior={prior_side} side=SELL_SHORT reason={reason_text} attempt={self._post_exit_entry_send_attempts}")
            if reason_text == "FIXED_LOSS":
                _submit_reason = "AUTO_POST_EXIT_FIXED_LOSS_ENTRY_SHORT"
            elif reason_text == "MFE_PROTECT":
                _submit_reason = "AUTO_POST_EXIT_MFE_PROTECT_ENTRY_SHORT"
            else:
                _submit_reason = "AUTO_POST_EXIT_MFE_ENTRY_SHORT"
            ok = bool(self.order_core.submit_short(self.order_qty, reason=f"{_submit_reason}/{self.last_signal_reason}"))
            self._remember_entry_order_audit(ok, "SHORT", f"{_submit_reason}/{self.last_signal_reason}")
        elif target_side == "LONG":
            if not bool(sig_state.get("long_ok", False)):
                self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", "reason=LONG_SNAPSHOT_NOT_READY")
                self._clear_post_exit_overlap_entry()
                return False
            self.log("SIGNAL", "POST_EXIT_ENTRY_FORCE", f"prior={prior_side} side=BUY reason={reason_text} attempt={self._post_exit_entry_send_attempts}")
            if reason_text == "FIXED_LOSS":
                _submit_reason = "AUTO_POST_EXIT_FIXED_LOSS_ENTRY_LONG"
            elif reason_text == "MFE_PROTECT":
                _submit_reason = "AUTO_POST_EXIT_MFE_PROTECT_ENTRY_LONG"
            else:
                _submit_reason = "AUTO_POST_EXIT_MFE_ENTRY_LONG"
            ok = bool(self.order_core.submit_long(self.order_qty, reason=f"{_submit_reason}/{self.last_signal_reason}"))
            self._remember_entry_order_audit(ok, "LONG", f"{_submit_reason}/{self.last_signal_reason}")
        else:
            self.log("SIGNAL", "POST_EXIT_ENTRY_BLOCKED", f"reason=UNKNOWN_TARGET target={target_side or '-'}")
            self._clear_post_exit_overlap_entry()
            return False
        if ok:
            now_ts = time.time()
            self._last_entry_submit_ts = now_ts
            self._last_entry_submit_side = target_side
            self._remember_entry_submit_metrics(target_side, sig_state)
            self.entry_inflight = True
            self.pending_entry_side = target_side
            self.order_lane_locked = True
            self.flat_confirmed = False
            self._entry_order_guard_active = True
            self._entry_order_guard_side = target_side
            self._entry_order_guard_ts = now_ts
            self._post_exit_entry_awaiting_confirm = True
            self._post_exit_entry_last_submit_ts = now_ts
            self._refresh_lane_snapshot()
            self.log("SIGNAL", "POST_EXIT_ENTRY", f"side={'SELL_SHORT' if target_side == 'SHORT' else 'BUY'} trigger={reason_text}_OVERLAP mode=SAVED_SNAPSHOT attempt={self._post_exit_entry_send_attempts}")
            self._schedule_post_exit_entry_unknown_recheck()
            return True
        self.log("SIGNAL", "POST_EXIT_ENTRY_SEND_FAIL", f"target={target_side or '-'} reason={reason_text or '-'}")
        if int(getattr(self, "_post_exit_entry_send_attempts", 0) or 0) >= int(getattr(self, "_post_exit_entry_max_send_attempts", 2) or 2):
            self._clear_post_exit_overlap_entry()
        else:
            self._schedule_post_exit_overlap_entry(250)
        if not self.server_sync_pending:
            self._request_server_sync(reason="POST_EXIT_ENTRY_SEND_FAIL", include_takeover=True, include_unfilled=True, start_warmup_after_response=False, force=True)
        return False

    def _clear_entry_signal_state_after_flat(self, reason: str = "") -> None:
        """Clear only stale entry candidates after server-confirmed FLAT.

        Keep the live signal/watch engine itself active.  Clearing reverse,
        post-exit scheduling, entry guards, or the whole signal snapshot here
        can leave the next flat-cycle watcher unable to restart.
        """
        reset_reason = str(reason or "SERVER_FLAT_CONFIRMED")
        try:
            self.st._clear_arms(f"FLAT_AFTER_EXIT:{reset_reason}")
        except Exception:
            pass
        if not isinstance(self.last_sig_state, dict):
            self.last_sig_state = {}
        ss = dict(self.last_sig_state)
        for key in (
            "long_arm", "short_arm",
            "long_armed", "short_armed",
            "long_fire_pending", "short_fire_pending",
            "long_fire_trigger_hit", "short_fire_trigger_hit",
            "long_arm_trigger_hit", "short_arm_trigger_hit",
            "long_entry_ready", "short_entry_ready",
            "long_ok", "short_ok",
        ):
            ss[key] = False
        ss["final_signal"] = "NONE"
        ss["entry_reason"] = ""
        ss["arm_cancel_reason"] = reset_reason
        self.last_sig_state = ss
        self.last_long_ok = False
        self.last_short_ok = False

    def _reset_entry_watch_after_exit_fill(self, reason: str = "") -> None:
        """Reset stale entry wait/watch state immediately after an exit fill.

        EXIT_FILLED means the prior position's ARM/FIRE watcher must not carry
        into the next flat-cycle entry. Keep the local/server position state
        intact until broker flat confirmation arrives, but restart the entry
        wait/watch countdown from this exit timestamp.
        """
        reset_reason = str(reason or "EXIT_FILLED")
        self._clear_entry_signal_state_after_flat(f"EXIT_FILLED:{reset_reason}")
        self.log("SIGNAL", "ENTRY_WATCH_RESET", f"source=EXIT_FILLED reason={reset_reason} stale_arm/fire=CLEAR watcher=RESTART")

    def _submit_forced_reverse_entry_after_exit(self, prior_side: str, exit_reason: str) -> bool:
        """Submit the saved opposite order immediately after a REVERSE_SIGNAL exit fill.

        This path must follow the same current opposite-entry gate as normal entry.
        """
        reason_text = str(exit_reason or "").strip().upper()
        prior_text = str(prior_side or "").strip().upper()
        if reason_text != "REVERSE_SIGNAL":
            self._clear_forced_reverse_pending()
            return False
        if self._is_eod_block_active() or self._is_eod_force_exit_due():
            self._clear_forced_reverse_pending()
            self.log("SIGNAL", "REVERSE_ENTRY_BLOCKED", "reason=EOD_BLOCK")
            return False
        if not bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False)):
            self._clear_forced_reverse_pending()
            self.log("SIGNAL", "REVERSE_ENTRY_SKIP", "reason=REVERSE_SIGNAL_DISABLED stale_reason=1 submit=0")
            return False
        if not bool(getattr(self.cfg, "ALLOW_REVERSE_SIGNAL_REENTRY", False)):
            self._clear_forced_reverse_pending()
            self.log("SIGNAL", "REVERSE_ENTRY_SKIP", "reason=REVERSE_REENTRY_DISABLED submit=0")
            return False
        if not self.order_core:
            self._clear_forced_reverse_pending()
            self.log("SIGNAL", "REVERSE_ENTRY_BLOCKED", "reason=ORDER_CORE_MISSING")
            return False
        saved_sig_state = dict(getattr(self, "_forced_reverse_entry_sig_state", {}) or {})
        if saved_sig_state:
            gate_ok, gate_reason, sig_state, reverse_side = self._reverse_entry_signal_ready_from_snapshot(prior_text, saved_sig_state)
        else:
            gate_ok, gate_reason, sig_state, reverse_side = self._reverse_entry_signal_ready(prior_text)
        if not gate_ok:
            self._clear_forced_reverse_pending()
            self.log("SIGNAL", "REVERSE_ENTRY_BLOCKED", self._format_reverse_gate_reason(gate_reason, sig_state, reverse_side))
            return False
        self.entry_inflight = False
        self.exit_inflight = False
        self.position_close_pending = False
        self.order_lane_locked = False
        self.server_sync_pending = False
        self._post_exit_server_sync_required = False
        self.flat_confirmed = True
        self._clear_reverse_after_sync()
        self._clear_reverse_retry()
        # Clear stale EXIT pending/cooldown before direct reverse entry. The EXIT
        # fill is already confirmed here, so the previous EXIT lane must not block
        # the opposite ENTRY order.
        try:
            pending = getattr(self.order_core, "pending", None)
            if pending is not None and bool(getattr(pending, "active", False)):
                self.log(
                    "SIGNAL",
                    "REVERSE_ENTRY_CLEAR_PENDING",
                    f"action={getattr(pending, 'action', '-') or '-'} side={getattr(pending, 'side', '-') or '-'}",
                )
                pending.active = False
                pending.action = ""
                pending.side = ""
                pending.qty = 0
                pending.remaining_qty = 0
                pending.cancel_requested = False
                pending.server_recheck_required = False
                pending.auto_cancel_phase = ""
        except Exception as exc:
            self.log("WARN", "REVERSE_ENTRY_CLEAR_PENDING_ERROR", repr(exc))
        try:
            self.order_core.entry_cooldown_until = 0.0
        except Exception:
            pass
        if reverse_side == "SHORT":
            self.log("SIGNAL", "REVERSE_ENTRY_FORCE", "prior=LONG side=SELL_SHORT reason=REVERSE_SIGNAL")
            ok = self.order_core.submit_short(self.order_qty, reason=f"AUTO_REVERSE_DIRECT_{reason_text}/{self.last_signal_reason}")
            self._remember_entry_order_audit(bool(ok), "SHORT", f"AUTO_REVERSE_DIRECT_{reason_text}/{self.last_signal_reason}")
            if ok:
                self._clear_forced_reverse_pending()
                self._last_entry_submit_ts = time.time()
                self._last_entry_submit_side = "SHORT"
                self._remember_entry_submit_metrics("SHORT", sig_state)
                self.entry_inflight = True
                self.pending_entry_side = "SHORT"
                self.order_lane_locked = True
                self.flat_confirmed = False
                self._entry_order_guard_active = True
                self._entry_order_guard_side = "SHORT"
                self._entry_order_guard_ts = time.time()
                self._refresh_lane_snapshot()
                self.log("SIGNAL", "REVERSE_ENTRY", "side=SELL_SHORT trigger=REVERSE_SIGNAL mode=FORCED_DIRECT")
            else:
                self._clear_forced_reverse_pending()
                self.log("SIGNAL", "REVERSE_ENTRY_SEND_FAIL", "side=SELL_SHORT trigger=REVERSE_SIGNAL")
            return bool(ok)
        if reverse_side == "LONG":
            self.log("SIGNAL", "REVERSE_ENTRY_FORCE", "prior=SHORT side=BUY reason=REVERSE_SIGNAL")
            ok = self.order_core.submit_long(self.order_qty, reason=f"AUTO_REVERSE_DIRECT_{reason_text}/{self.last_signal_reason}")
            self._remember_entry_order_audit(bool(ok), "LONG", f"AUTO_REVERSE_DIRECT_{reason_text}/{self.last_signal_reason}")
            if ok:
                self._clear_forced_reverse_pending()
                self._last_entry_submit_ts = time.time()
                self._last_entry_submit_side = "LONG"
                self._remember_entry_submit_metrics("LONG", sig_state)
                self.entry_inflight = True
                self.pending_entry_side = "LONG"
                self.order_lane_locked = True
                self.flat_confirmed = False
                self._entry_order_guard_active = True
                self._entry_order_guard_side = "LONG"
                self._entry_order_guard_ts = time.time()
                self._refresh_lane_snapshot()
                self.log("SIGNAL", "REVERSE_ENTRY", "side=BUY trigger=REVERSE_SIGNAL mode=FORCED_DIRECT")
            else:
                self._clear_forced_reverse_pending()
                self.log("SIGNAL", "REVERSE_ENTRY_SEND_FAIL", "side=BUY trigger=REVERSE_SIGNAL")
            return bool(ok)
        self._clear_forced_reverse_pending()
        self.log("SIGNAL", "REVERSE_ENTRY_BLOCKED", self._format_reverse_gate_reason(gate_reason, sig_state, reverse_side))
        return False

    def _try_immediate_reverse_after_exit(self, prior_side: str, exit_reason: str) -> bool:
        reason_text = str(exit_reason or "").strip().upper()
        if self._is_eod_force_exit_due() or reason_text.startswith("EOD") or str(self.last_exit_reason or "").strip().upper().startswith("EOD"):
            self.log("SIGNAL", "REVERSE_BLOCKED", f"reason=EOD_BLOCK exit_reason={reason_text or '-'} prior={prior_side}")
            return False
        auto_reverse_reasons = self._auto_reverse_allowed_reasons()
        if reason_text not in auto_reverse_reasons:
            return False
        lane_ok, lane_reason = self._reverse_entry_lane_state()
        if not lane_ok:
            self.log("SIGNAL", "REVERSE_BLOCKED", f"reason={lane_reason} exit_reason={reason_text} prior={prior_side}")
            return False

        gate_ok, gate_reason, sig_state, reverse_side = self._reverse_entry_signal_ready(prior_side)
        if not gate_ok:
            self.log("SIGNAL", "REVERSE_BLOCKED", f"{self._format_reverse_gate_reason(gate_reason, sig_state, reverse_side)} exit_reason={reason_text}")
            return False

        if reverse_side == "SHORT":
            self.log("SIGNAL", "ENTRY_ALLOWED", "reason=REVERSE_SIGNAL_FLAT_CONFIRMED side=SELL_SHORT")
            ok = self.order_core.submit_short(self.order_qty, reason=f"AUTO_REVERSE_AFTER_{reason_text}/{self.last_signal_reason}")
            self._remember_entry_order_audit(bool(ok), "SHORT", f"AUTO_REVERSE_AFTER_{reason_text}/{self.last_signal_reason}")
            if ok:
                self._last_entry_submit_ts = time.time()
                self._last_entry_submit_side = "SHORT"
                self._remember_entry_submit_metrics("SHORT", sig_state)
                self.entry_inflight = True
                self.pending_entry_side = "SHORT"
                self.order_lane_locked = True
                self.flat_confirmed = False
                self._entry_order_guard_active = True
                self._entry_order_guard_side = "SHORT"
                self._entry_order_guard_ts = time.time()
                self._clear_reverse_retry()
                self._refresh_lane_snapshot()
                self.log("SIGNAL", "REVERSE_ENTRY", f"side=SELL_SHORT trigger={reason_text}")
            return bool(ok)

        if reverse_side == "LONG":
            self.log("SIGNAL", "ENTRY_ALLOWED", "reason=REVERSE_SIGNAL_FLAT_CONFIRMED side=BUY")
            ok = self.order_core.submit_long(self.order_qty, reason=f"AUTO_REVERSE_AFTER_{reason_text}/{self.last_signal_reason}")
            self._remember_entry_order_audit(bool(ok), "LONG", f"AUTO_REVERSE_AFTER_{reason_text}/{self.last_signal_reason}")
            if ok:
                self._last_entry_submit_ts = time.time()
                self._last_entry_submit_side = "LONG"
                self._remember_entry_submit_metrics("LONG", sig_state)
                self.entry_inflight = True
                self.pending_entry_side = "LONG"
                self.order_lane_locked = True
                self.flat_confirmed = False
                self._entry_order_guard_active = True
                self._entry_order_guard_side = "LONG"
                self._entry_order_guard_ts = time.time()
                self._clear_reverse_retry()
                self._refresh_lane_snapshot()
                self.log("SIGNAL", "REVERSE_ENTRY", f"side=BUY trigger={reason_text}")
            return bool(ok)
        self.log("SIGNAL", "REVERSE_BLOCKED", f"{self._format_reverse_gate_reason(gate_reason, sig_state, reverse_side)} exit_reason={reason_text}")
        return False

    def _mark_server_sync_part_done(self, part: str, token: int | None = None) -> None:
        if token is not None and int(token) != int(self._server_sync_token):
            return
        if part in self.server_sync_parts_pending:
            self.server_sync_parts_pending[part] = False
        if any(self.server_sync_parts_pending.values()):
            self.server_sync_pending = True
            return
        self.server_sync_pending = False
        self._last_server_sync_request_ts = time.time()
        # BOOT is also used after a same-process reconnect.  warmup_done may
        # still be True from the previous connection, but an explicit
        # start_warmup_after_response request must rebuild the seed/continuity.
        if self._server_sync_start_warmup and not self.warmup_loading and self.warmup_started:
            self._server_sync_start_warmup = False
            QTimer.singleShot(0, self.start_warmup)

    @staticmethod
    def _normalize_query_reason(reason: str) -> str:
        text = str(reason or "SYNC").strip().upper()
        return text or "SYNC"

    @staticmethod
    def _is_exit_sync_recheck_reason(reason: str) -> bool:
        r = str(reason or "").strip().upper()
        return r in {
            "EXIT_NEEDS_SERVER_CHECK",
            "EXIT_SEND_FAIL",
            "EXIT_NO_CALLBACK_TIMEOUT",
            "EXIT_NO_KIS_EXECUTION_TIMEOUT",
            "EXIT_PENDING_RECHECK",
        }

    def _exit_pending_active_for_submit_guard(self) -> bool:
        try:
            pending = getattr(getattr(self, "order_core", None), "pending", None)
            pending_exit = bool(
                pending is not None
                and bool(getattr(pending, "active", False))
                and str(getattr(pending, "action", "") or "").upper().startswith("EXIT")
            )
        except Exception:
            pending_exit = False
        return bool(self.exit_inflight or self.position_close_pending or pending_exit)

    def _log_exit_guard_once(self, code: str, message: str, key: str | None = None, interval_sec: float = 1.2) -> None:
        """Deduplicate noisy EXIT guard logs during tick-by-tick fixed-loss/MFE retries."""
        try:
            code_s = str(code or "EXIT_GUARD").strip() or "EXIT_GUARD"
            key_s = f"{code_s}|{str(key if key is not None else message)}"
            now_ts = time.time()
            last_ts = float(self._exit_log_dedupe.get(key_s, 0.0) or 0.0)
            if now_ts - last_ts < max(float(interval_sec or 0.0), 0.0):
                return
            self._exit_log_dedupe[key_s] = now_ts
            self.log("SYNC", code_s, str(message or ""))
        except Exception:
            try:
                self.log("SYNC", str(code or "EXIT_GUARD"), str(message or ""))
            except Exception:
                pass

    def _request_exit_balance_after_unfilled(self) -> None:
        """Confirm balance only after a successful empty open-order response."""
        if not (self.connected and self.account):
            return
        if not (self._exit_pending_active_for_submit_guard() or self.exit_needs_server_check):
            return
        if self.takeover_inflight or self.unfilled_inflight:
            return
        if self.has_server_unfilled or self.has_unfilled_orders:
            return
        self._request_server_sync(
            reason="EXIT_PENDING_RECHECK", include_takeover=True,
            include_unfilled=False, include_account=False, include_orderable=False,
            start_warmup_after_response=False, force=True,
        )

    def _maybe_request_exit_pending_recheck(self, reason: str = "EXIT_PENDING_RECHECK") -> None:
        """While an EXIT order is in 주문확인/체결대기, avoid resubmitting and poll server state sparsely."""
        try:
            if not (self._exit_pending_active_for_submit_guard() or self.exit_needs_server_check):
                return
            if not (self.connected and bool(self.account)):
                return
            now_ts = time.time()
            interval = max(float(getattr(self, "_exit_pending_recheck_interval_sec", 1.2) or 1.2), 5.0)
            if now_ts - float(getattr(self, "_exit_pending_recheck_last_ts", 0.0) or 0.0) < interval:
                return
            self._exit_pending_recheck_last_ts = now_ts
            # 이미 다른 TR sync가 돌고 있으면 새 요청을 얹지 않는다. 로그만 저빈도로 남긴다.
            if self.takeover_inflight or self.unfilled_inflight:
                self._log_exit_guard_once(
                    "EXIT_PENDING_RECHECK_WAIT",
                    f"reason={reason} sync_pending={self.server_sync_reason or '-'}",
                    key=f"wait|{self.server_sync_reason or '-'}",
                    interval_sec=3.0,
                )
                return
            QTimer.singleShot(
                int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                lambda: self._request_server_sync(
                    reason="EXIT_PENDING_RECHECK",
                    include_takeover=False,
                    include_unfilled=True,
                    include_account=False,
                    include_orderable=False,
                    start_warmup_after_response=False,
                    force=True,
                ),
            )
            self._log_exit_guard_once(
                "EXIT_PENDING_RECHECK",
                f"reason={reason} inflight={int(bool(self.exit_inflight))} close_pending={int(bool(self.position_close_pending))}",
                key="request",
                interval_sec=3.0,
            )
        except Exception as _e:
            self._log_exit_guard_once("EXIT_PENDING_RECHECK_ERR", str(_e), key=type(_e).__name__, interval_sec=5.0)

    @staticmethod
    def _tr_reason_key(trcode: str, reason: str) -> str:
        return f"{str(trcode or '').upper()}::{str(reason or '').upper()}"

    def _mark_tr_query_complete(self, trcode: str, reason: str, status: str = "OK") -> None:
        tr = str(trcode or "").upper()
        r = self._normalize_query_reason(reason)
        if tr in self._tr_reason_inflight:
            self._tr_reason_inflight[tr].discard(r)
        if self._tr_active_reason.get(tr, "") == r:
            self._tr_active_reason[tr] = ""
            active_started_map = getattr(self, "_tr_active_started_ts", None)
            if isinstance(active_started_map, dict):
                active_started_map[tr] = 0.0
        self._tr_reason_last_ts[self._tr_reason_key(tr, r)] = time.time()
        status_u = str(status or "").upper()
        self._tr_last_status[tr] = status_u
        if status_u == "OK":
            self._tr_failure_streak[tr] = 0
            self._tr_last_failure_ts[tr] = 0.0
        else:
            self._tr_failure_streak[tr] = int(self._tr_failure_streak.get(tr, 0) or 0) + 1
            self._tr_last_failure_ts[tr] = time.time()
        self.log("SYNC", "QUERY_COMPLETE", f"TR={tr} QUERY_REASON={r} status={status}")

    def _get_active_tr_query_reason(self, trcode: str) -> str:
        tr = str(trcode or "").upper()
        return str(self._tr_active_reason.get(tr, "") or "")

    def _tr_timeout_owns_active_query(self, trcode: str, reason: str) -> bool:
        tr = str(trcode or "").upper()
        requested = self._normalize_query_reason(reason)
        active = self._get_active_tr_query_reason(tr)
        if active == requested:
            return True
        self.log(
            "SYNC",
            "STALE_TIMEOUT_IGNORED",
            f"TR={tr} timeout_reason={requested} active_reason={active or '-'}",
        )
        return False

    def _tr_stale_timeout_sec(self, trcode: str) -> float:
        tr = str(trcode or "").upper()
        timeout_ms = {
            "KIS_BALANCE": getattr(self.cfg, "TAKEOVER_TIMEOUT_MS", 5000),
            "KIS_OPEN_ORDERS": getattr(self.cfg, "UNFILLED_SYNC_TIMEOUT_MS", 5000),
            "KIS_ACCOUNT": getattr(self.cfg, "ACCOUNT_SNAPSHOT_TIMEOUT_MS", 5000),
            "KIS_ORDERABLE": getattr(self.cfg, "ORDERABLE_SNAPSHOT_TIMEOUT_MS", 5000),
        }.get(tr, 5000)
        try:
            return max(7.0, float(timeout_ms or 5000) / 1000.0 + 2.0)
        except Exception:
            return 7.0

    def _try_begin_tr_query(self, trcode: str, reason: str) -> tuple[bool, str]:
        tr = str(trcode or "").upper()
        r = self._normalize_query_reason(reason)
        now_ts = time.time()
        key = self._tr_reason_key(tr, r)
        if r in self._tr_reason_inflight.get(tr, set()):
            self.log("SYNC", "QUERY_SKIP_DUPLICATE", f"TR={tr} QUERY_REASON={r}")
            return False, r
        last_ts = float(self._tr_reason_last_ts.get(key, 0.0) or 0.0)
        cooldown = float(self._tr_reason_cooldown_sec or 2.0)
        if (now_ts - last_ts) < cooldown:
            wait_sec = max(cooldown - (now_ts - last_ts), 0.0)
            self.log("SYNC", "QUERY_SKIP_COOLDOWN", f"TR={tr} QUERY_REASON={r} wait={wait_sec:.2f}s")
            return False, r
        active_reason = str(self._tr_active_reason.get(tr, "") or "")
        if active_reason:
            active_started_map = getattr(self, "_tr_active_started_ts", None)
            if not isinstance(active_started_map, dict):
                active_started_map = {}
                self._tr_active_started_ts = active_started_map
            active_started = float(active_started_map.get(tr, 0.0) or 0.0)
            active_age = now_ts - active_started if active_started > 0.0 else 0.0
            if active_started > 0.0 and active_age >= self._tr_stale_timeout_sec(tr):
                self.log("SYNC", "QUERY_STALE_RECOVER", f"TR={tr} stale_reason={active_reason} age={active_age:.2f}s next_reason={r}")
                self._mark_tr_query_complete(tr, active_reason, status="STALE_TIMEOUT")
            else:
                self.log("SYNC", "QUERY_INFLIGHT", f"TR={tr} QUERY_REASON={r} active_reason={active_reason}")
                return False, r
        self._tr_reason_inflight.setdefault(tr, set()).add(r)
        self._tr_active_reason[tr] = r
        self._tr_active_started_ts[tr] = now_ts
        self.log("SYNC", "QUERY_INFLIGHT", f"TR={tr} QUERY_REASON={r} state=START")
        return True, r

    def _is_entry_state_unknown(self) -> bool:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_action = str(getattr(pending, "action", "") or "").upper()
        pending_entry = bool(getattr(pending, "active", False) and pending_action.startswith("ENTRY"))
        pending_recheck = bool(getattr(pending, "server_recheck_required", False) and pending_action.startswith("ENTRY"))
        return bool(self.entry_inflight or self._entry_order_guard_active or pending_entry or pending_recheck)

    def _is_cancel_state_unknown(self) -> bool:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_action = str(getattr(pending, "action", "") or "").upper()
        pending_cancel = bool(
            getattr(pending, "cancel_requested", False)
            or (getattr(pending, "active", False) and ("CANCEL" in pending_action))
        )
        return bool(self.cancel_in_progress or self.cancel_needs_server_check or pending_cancel)

    def _is_exit_flat_confirmation_missing(self) -> bool:
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_action = str(getattr(pending, "action", "") or "").upper()
        pending_exit = bool(getattr(pending, "active", False) and pending_action.startswith("EXIT"))
        return bool(self.exit_in_progress or self.exit_inflight or self.position_close_pending or self.exit_needs_server_check or self._post_exit_server_sync_required or pending_exit)

    def _should_query_kis_open_orders(self, reason: str) -> bool:
        r = self._normalize_query_reason(reason)
        if self._is_exit_sync_recheck_reason(r):
            return True
        entry_or_cancel_unknown = bool(self._is_entry_state_unknown() or self._is_cancel_state_unknown())
        unfilled_active = bool(self.has_server_unfilled or self.has_unfilled_orders or self.cancel_in_progress)
        if r == "TIMER_RECHECK":
            now_ts = time.time()
            preopen_phase = bool(self._is_preopen_market_phase())
            interval_sec = (
                self._kis_open_orders_timer_idle_interval_sec
                if preopen_phase
                else (
                    self._kis_open_orders_timer_busy_interval_sec
                    if (entry_or_cancel_unknown or unfilled_active or self._auto_precheck_active)
                    else self._kis_open_orders_timer_idle_interval_sec
                )
            )
            if now_ts - float(self._last_kis_open_orders_timer_check_ts or 0.0) < float(interval_sec):
                return False
            self._last_kis_open_orders_timer_check_ts = now_ts
            if preopen_phase:
                return True
        # KIS_OPEN_ORDERS: keep polling not only for unknown entry/cancel states,
        # but also for runtime unfilled detection/verification.
        if entry_or_cancel_unknown:
            return True
        if unfilled_active:
            return True
        if r in {
            "BOOT",
            "POST_WARMUP",
            "EXIT_FILLED",
            "KIS_EXECUTION_BALANCE_FLAT",
            "MANUAL_CANCEL",
            "MANUAL_CANCEL_CHECK",
            "AUTO_RUNTIME_CANCEL",
            "AUTO_CANCEL_REQUESTED",
            "CANCEL_CONFIRMED",
            "CANCEL_SEND_FAIL",
            "HEALTH_RECHECK",
            "KIS_WS_ORDER_ACTIVITY",
        }:
            return True
        if r.startswith("AUTO_PRECHECK"):
            return True
        if r.startswith("POST_EXIT_ENTRY"):
            return True
        return False

    def _should_query_kis_balance(self, reason: str) -> bool:
        r = self._normalize_query_reason(reason)
        if r == "ENTRY_UNKNOWN_BALANCE_CHECK":
            return True
        if r == "TIMER_RECHECK":
            if not self._is_preopen_market_phase():
                return False
            now_ts = time.time()
            interval_sec = float(self._kis_open_orders_timer_idle_interval_sec or 60.0)
            if now_ts - float(self._last_kis_balance_timer_check_ts or 0.0) < interval_sec:
                return False
            self._last_kis_balance_timer_check_ts = now_ts
            return True
        # KIS_BALANCE: event-driven only. Allow takeover sync only for boot/post-warmup,
        # explicit auto-precheck, and explicit exit-confirmation paths.
        if r in {"BOOT", "POST_WARMUP", "EXIT_FILLED", "KIS_EXECUTION_BALANCE_FLAT", "KIS_WS_ORDER_ACTIVITY", "MANUAL_EXIT_CHECK", "HEALTH_RECHECK"}:
            return True
        if self._is_exit_sync_recheck_reason(r):
            return True
        if r.startswith("AUTO_PRECHECK"):
            return True
        if r.startswith("POST_EXIT_ENTRY"):
            return True
        return False

    def _should_query_kis_account(self, reason: str) -> bool:
        r = self._normalize_query_reason(reason)
        if r == "TIMER_RECHECK":
            if not self._is_preopen_market_phase():
                return False
            now_ts = time.time()
            interval_sec = float(self._kis_open_orders_timer_idle_interval_sec or 60.0)
            last_ts = float(getattr(self, "_last_kis_account_timer_check_ts", 0.0) or 0.0)
            if now_ts - last_ts < interval_sec:
                return False
            self._last_kis_account_timer_check_ts = now_ts
            return True
        if r in {
            "BOOT",
            "POST_WARMUP",
            "ENTRY_FILLED",
            "EXIT_FILLED",
            "KIS_EXECUTION_BALANCE_FLAT",
            "MANUAL_EXIT_CHECK",
            "MANUAL_CANCEL",
            "MANUAL_CANCEL_CHECK",
            "AUTO_CANCEL_REQUESTED",
            "CANCEL_CONFIRMED",
            "CANCEL_SEND_FAIL",
            "HEALTH_RECHECK",
        }:
            return True
        if self._is_exit_sync_recheck_reason(r):
            return True
        if r.startswith("AUTO_PRECHECK"):
            return True
        if r.startswith("POST_EXIT_ENTRY"):
            return True
        return False

    def _should_query_kis_orderable(self, reason: str) -> bool:
        r = self._normalize_query_reason(reason)
        if r == "TIMER_RECHECK":
            if not self._is_preopen_market_phase():
                return False
            now_ts = time.time()
            interval_sec = float(self._kis_open_orders_timer_idle_interval_sec or 60.0)
            last_ts = float(getattr(self, "_last_kis_orderable_timer_check_ts", 0.0) or 0.0)
            if now_ts - last_ts < interval_sec:
                return False
            self._last_kis_orderable_timer_check_ts = now_ts
            return True
        if r in {
            "BOOT",
            "POST_WARMUP",
            "ENTRY_FILLED",
            "EXIT_FILLED",
            "KIS_EXECUTION_BALANCE_FLAT",
            "MANUAL_EXIT_CHECK",
            "MANUAL_CANCEL",
            "MANUAL_CANCEL_CHECK",
            "AUTO_CANCEL_REQUESTED",
            "CANCEL_CONFIRMED",
            "CANCEL_SEND_FAIL",
            "HEALTH_RECHECK",
        }:
            return True
        if self._is_exit_sync_recheck_reason(r):
            return True
        if r.startswith("AUTO_PRECHECK"):
            return True
        if r.startswith("POST_EXIT_ENTRY"):
            return True
        return False

    def _request_server_sync(
        self,
        reason: str,
        *,
        include_takeover: bool = True,
        include_unfilled: bool = True,
        include_account: bool = True,
        include_orderable: bool = True,
        start_warmup_after_response: bool = False,
        force: bool = False,
    ) -> None:
        now_ts = time.time()
        if not force and now_ts - float(self._last_server_sync_request_ts or 0.0) < float(self._server_sync_min_interval_sec or 0.0):
            return
        reason_n = self._normalize_query_reason(reason)
        allowed_takeover = bool(include_takeover and self._should_query_kis_balance(reason_n))
        allowed_unfilled = bool(include_unfilled and self._should_query_kis_open_orders(reason_n))
        allowed_account = bool(include_account and self._should_query_kis_account(reason_n))
        allowed_orderable = bool(include_orderable and self._should_query_kis_orderable(reason_n))
        if include_takeover and (not allowed_takeover):
            self.log("SYNC", "QUERY_SKIP_POLICY", f"TR=KIS_BALANCE QUERY_REASON={reason_n}")
        if include_unfilled and (not allowed_unfilled):
            self.log("SYNC", "QUERY_SKIP_POLICY", f"TR=KIS_OPEN_ORDERS QUERY_REASON={reason_n}")
        if include_account and (not allowed_account):
            self.log("SYNC", "QUERY_SKIP_POLICY", f"TR=KIS_ACCOUNT QUERY_REASON={reason_n}")
        if include_orderable and (not allowed_orderable):
            self.log("SYNC", "QUERY_SKIP_POLICY", f"TR=KIS_ORDERABLE QUERY_REASON={reason_n}")
        self._server_sync_token += 1
        token = int(self._server_sync_token)
        self.server_sync_reason = str(reason_n or "SYNC")
        self.server_sync_parts_pending = {"takeover": False, "unfilled": False, "account": False, "orderable": False}
        self.server_sync_pending = False
        # Do not let a later AUTO_PRECHECK replace the pending BOOT warmup.
        # This previously left the dashboard indefinitely at "워밍업대기".
        self._server_sync_start_warmup = bool(
            self._server_sync_start_warmup or start_warmup_after_response
        )
        self._last_server_sync_request_ts = now_ts

        if allowed_takeover and self.connected and bool(self.account) and (not self.takeover_inflight):
            takeover_started = self._request_takeover(reason=reason_n, start_warmup_after_response=False, sync_token=token)
            if takeover_started:
                self.server_sync_parts_pending["takeover"] = True
                self.server_sync_pending = True
        if allowed_unfilled and self.connected and bool(self.account) and (not self.unfilled_inflight):
            unfilled_started = self._request_unfilled_sync(reason=reason_n, sync_token=token)
            if unfilled_started:
                self.server_sync_parts_pending["unfilled"] = True
                self.server_sync_pending = True
        if allowed_account and self.connected and bool(self.account) and (not self.account_snapshot_inflight):
            account_started = self._request_account_snapshot(reason=reason_n, sync_token=token)
            if account_started:
                self.server_sync_parts_pending["account"] = True
                self.server_sync_pending = True
        if allowed_orderable and self.connected and bool(self.account) and (not self.orderable_snapshot_inflight):
            orderable_started = self._request_orderable_snapshot(reason=reason_n, sync_token=token)
            if orderable_started:
                self.server_sync_parts_pending["orderable"] = True
                self.server_sync_pending = True

        if not self.server_sync_pending:
            if self._server_sync_start_warmup:
                self._server_sync_start_warmup = False
                QTimer.singleShot(0, self.start_warmup)
            return
        self.log(
            "SYNC",
            "REQ",
            f"reason={self.server_sync_reason} takeover={'Y' if self.server_sync_parts_pending['takeover'] else 'N'} "
            f"unfilled={'Y' if self.server_sync_parts_pending['unfilled'] else 'N'} "
            f"account={'Y' if self.server_sync_parts_pending['account'] else 'N'} "
            f"orderable={'Y' if self.server_sync_parts_pending['orderable'] else 'N'}",
        )

    def _request_account_snapshot(self, reason: str = "SYNC", sync_token: int | None = None) -> bool:
        reason_n = self._normalize_query_reason(reason)
        self.log("SYNC", "ACCOUNT_START", f"reason={reason} connected={self.connected} account={self.account} inflight={self.account_snapshot_inflight}")
        if not self.connected or not self.account:
            self.log("SYNC", "ACCOUNT_SKIP", "미연결 또는 계좌없음")
            self._mark_server_sync_part_done("account", sync_token)
            return False
        if self.account_snapshot_inflight:
            self.log("SYNC", "ACCOUNT_SKIP", f"이미 조회 진행중 reason={reason}")
            self._mark_server_sync_part_done("account", sync_token)
            return False
        can_start, reason_n = self._try_begin_tr_query("KIS_ACCOUNT", reason_n)
        if not can_start:
            self._mark_server_sync_part_done("account", sync_token)
            return False
        try:
            rqname = str(getattr(self.cfg, "ACCOUNT_SNAPSHOT_RQNAME", "REQ_ACCOUNT_SNAPSHOT") or "REQ_ACCOUNT_SNAPSHOT")
            self.account_snapshot_inflight = True
            ret = self.client.request_account_snapshot(request_name=rqname)
            self.log("SYNC", "ACCOUNT_REQ", f"KIS 계좌조회 reason={reason_n} account={mask_account(self.account)} ret={ret}")
            if int(ret) != 0:
                self.account_snapshot_inflight = False
                self.log("SYNC", "ACCOUNT_ERR", f"KIS 계좌조회 실패 ret={ret}")
                self._mark_tr_query_complete("KIS_ACCOUNT", reason_n, status=f"REQ_FAIL_{ret}")
                self._mark_server_sync_part_done("account", sync_token)
                return False
            timeout_ms = int(getattr(self.cfg, "ACCOUNT_SNAPSHOT_TIMEOUT_MS", 5000) or 5000)
            QTimer.singleShot(timeout_ms, lambda: self._account_snapshot_timeout(reason=reason_n, sync_token=sync_token))
            return True
        except Exception as e:
            self.account_snapshot_inflight = False
            self.log("SYNC", "ACCOUNT_ERR", f"요청 예외 reason={reason}: {e}")
            self._mark_tr_query_complete("KIS_ACCOUNT", reason_n, status="EXCEPTION")
            self._mark_server_sync_part_done("account", sync_token)
            return False

    def _account_snapshot_timeout(self, reason: str = "SYNC", sync_token: int | None = None) -> None:
        if self.account_snapshot_inflight and self._tr_timeout_owns_active_query("KIS_ACCOUNT", reason):
            self.account_snapshot_inflight = False
            self.log("SYNC", "ACCOUNT_TIMEOUT", f"KIS 계좌응답 없음 reason={reason}")
            self._mark_tr_query_complete("KIS_ACCOUNT", reason, status="TIMEOUT")
            self._mark_server_sync_part_done("account", sync_token)

    def _kis_orderable_side_code(self) -> str:
        side = str(self.position_side or "FLAT").upper()
        if side == "LONG":
            return "1"
        return "2"

    def _kis_orderable_price_text(self) -> str:
        price = 0.0
        for candidate in (
            self.current_price,
            self.last_bar_close,
            self.session_close,
            self.session_open_price,
            self.entry_price,
        ):
            try:
                value = float(candidate or 0.0)
            except Exception:
                value = 0.0
            if value > 0.0:
                price = value
                break
        if price <= 0.0:
            try:
                if getattr(self, "st", None) is not None and getattr(self.st, "cl1", None):
                    seq = list(self.st.cl1)
                    if seq:
                        price = float(seq[-1] or 0.0)
            except Exception:
                price = 0.0
        if price <= 0.0:
            return "0"
        try:
            return str(int(round(float(price) * 1000.0)))
        except Exception:
            return "0"

    def _request_orderable_snapshot(self, reason: str = "SYNC", sync_token: int | None = None) -> bool:
        reason_n = self._normalize_query_reason(reason)
        self.log("SYNC", "ORDERABLE_START", f"reason={reason} connected={self.connected} account={self.account} inflight={self.orderable_snapshot_inflight}")
        if not self.connected or not self.account:
            self.log("SYNC", "ORDERABLE_SKIP", "미연결 또는 계좌없음")
            self._mark_server_sync_part_done("orderable", sync_token)
            return False
        if self.orderable_snapshot_inflight:
            self.log("SYNC", "ORDERABLE_SKIP", f"이미 조회 진행중 reason={reason}")
            self._mark_server_sync_part_done("orderable", sync_token)
            return False
        can_start, reason_n = self._try_begin_tr_query("KIS_ORDERABLE", reason_n)
        if not can_start:
            self._mark_server_sync_part_done("orderable", sync_token)
            return False
        try:
            rqname = str(getattr(self.cfg, "ORDERABLE_SNAPSHOT_RQNAME", "REQ_ORDERABLE_SNAPSHOT") or "REQ_ORDERABLE_SNAPSHOT")
            side_code = self._kis_orderable_side_code()
            order_price = self._kis_orderable_price_text()
            if str(order_price or "0") == "0":
                self.log("SYNC", "ORDERABLE_SKIP_PRICE0", f"reason={reason_n} warmup_done={int(bool(self.warmup_done))} current={self.current_price or 0} last_bar={self.last_bar_close or 0}")
                self._schedule_orderable_price0_retry(reason_n)
                self._mark_tr_query_complete("KIS_ORDERABLE", reason_n, status="SKIP_PRICE0")
                self._mark_server_sync_part_done("orderable", sync_token)
                return False
            self._orderable_price0_retry_pending = False
            self._orderable_price0_retry_count = 0
            self.orderable_snapshot_inflight = True
            ret = self.client.request_orderable(
                symbol=self.live_code,
                side=("SELL" if side_code == "1" else "BUY"),
                price=float(order_price) / 1000.0,
                request_name=rqname,
            )
            self.log("SYNC", "ORDERABLE_REQ", f"KIS 주문가능조회 reason={reason_n} side={side_code} price={order_price} ret={ret}")
            if int(ret) != 0:
                self.orderable_snapshot_inflight = False
                self.log("SYNC", "ORDERABLE_ERR", f"KIS 주문가능조회 실패 ret={ret}")
                self._mark_tr_query_complete("KIS_ORDERABLE", reason_n, status=f"REQ_FAIL_{ret}")
                self._mark_server_sync_part_done("orderable", sync_token)
                return False
            timeout_ms = int(getattr(self.cfg, "ORDERABLE_SNAPSHOT_TIMEOUT_MS", 5000) or 5000)
            QTimer.singleShot(timeout_ms, lambda: self._orderable_snapshot_timeout(reason=reason_n, sync_token=sync_token))
            return True
        except Exception as e:
            self.orderable_snapshot_inflight = False
            self.log("SYNC", "ORDERABLE_ERR", f"요청 예외 reason={reason}: {e}")
            self._mark_tr_query_complete("KIS_ORDERABLE", reason_n, status="EXCEPTION")
            self._mark_server_sync_part_done("orderable", sync_token)
            return False

    def _schedule_orderable_price0_retry(self, reason: str = "SYNC") -> None:
        try:
            max_retries = int(getattr(self.cfg, "ORDERABLE_PRICE0_RETRY_MAX", 5) or 5)
        except Exception:
            max_retries = 5
        if self._orderable_price0_retry_pending:
            return
        if int(self._orderable_price0_retry_count or 0) >= max(0, max_retries):
            return
        self._orderable_price0_retry_pending = True
        self._orderable_price0_retry_count = int(self._orderable_price0_retry_count or 0) + 1
        try:
            delay_ms = int(getattr(self.cfg, "ORDERABLE_PRICE0_RETRY_MS", 2500) or 2500)
        except Exception:
            delay_ms = 2500
        delay_ms = max(delay_ms, 2100)
        self.log("SYNC", "ORDERABLE_PRICE0_RETRY", f"reason={reason} attempt={self._orderable_price0_retry_count}/{max_retries} delay_ms={delay_ms}")
        QTimer.singleShot(delay_ms, lambda r=str(reason or "SYNC"): self._retry_orderable_snapshot_after_price0(r))

    def _retry_orderable_snapshot_after_price0(self, reason: str = "SYNC") -> None:
        self._orderable_price0_retry_pending = False
        if not self.connected or not self.account:
            return
        if self.orderable_snapshot_inflight:
            return
        self._request_orderable_snapshot(reason=reason)

    def _orderable_snapshot_timeout(self, reason: str = "SYNC", sync_token: int | None = None) -> None:
        if self.orderable_snapshot_inflight and self._tr_timeout_owns_active_query("KIS_ORDERABLE", reason):
            self.orderable_snapshot_inflight = False
            self.log("SYNC", "ORDERABLE_TIMEOUT", f"KIS 주문가능응답 없음 reason={reason}")
            self._mark_tr_query_complete("KIS_ORDERABLE", reason, status="TIMEOUT")
            self._mark_server_sync_part_done("orderable", sync_token)

    def _request_unfilled_sync(self, reason: str = "SYNC", sync_token: int | None = None) -> bool:
        reason_n = self._normalize_query_reason(reason)
        self.log("SYNC", "UNFILLED_START", f"reason={reason} connected={self.connected} account={self.account} inflight={self.unfilled_inflight}")
        if not self.connected or not self.account:
            self.log("SYNC", "UNFILLED_SKIP", "미연결 또는 계좌없음")
            self._mark_server_sync_part_done("unfilled", sync_token)
            return False
        if self.unfilled_inflight:
            self.log("SYNC", "UNFILLED_SKIP", f"이미 조회 진행중 reason={reason}")
            self._mark_server_sync_part_done("unfilled", sync_token)
            return False
        can_start, reason_n = self._try_begin_tr_query("KIS_OPEN_ORDERS", reason_n)
        if not can_start:
            self._mark_server_sync_part_done("unfilled", sync_token)
            return False
        try:
            rqname = str(getattr(self.cfg, "UNFILLED_SYNC_RQNAME", "REQ_UNFILLED_SYNC") or "REQ_UNFILLED_SYNC")
            self.unfilled_inflight = True
            ret = self.client.request_open_orders(request_name=rqname)
            self.log("SYNC", "UNFILLED_REQ", f"KIS 미체결조회 reason={reason_n} account={mask_account(self.account)} ret={ret}")
            if int(ret) != 0:
                self.unfilled_inflight = False
                self.log("SYNC", "UNFILLED_ERR", f"KIS 미체결조회 실패 ret={ret}")
                self._mark_tr_query_complete("KIS_OPEN_ORDERS", reason_n, status=f"REQ_FAIL_{ret}")
                self._mark_server_sync_part_done("unfilled", sync_token)
                return False
            timeout_ms = int(getattr(self.cfg, "UNFILLED_SYNC_TIMEOUT_MS", 5000) or 5000)
            QTimer.singleShot(timeout_ms, lambda: self._unfilled_sync_timeout(reason=reason_n, sync_token=sync_token))
            return True
        except Exception as e:
            self.unfilled_inflight = False
            self.log("SYNC", "UNFILLED_ERR", f"요청 예외 reason={reason}: {e}")
            self._mark_tr_query_complete("KIS_OPEN_ORDERS", reason_n, status="EXCEPTION")
            self._mark_server_sync_part_done("unfilled", sync_token)
            return False

    def _safe_get_connect_state(self) -> int:
        try:
            if self.client is None:
                return 0
            return int(self.client.get_connection_state() or 0)
        except Exception:
            return 0

    def _is_midday_reconnect_stuck(self, now_ts: float) -> tuple[bool, str]:
        reason_u = str(self.server_sync_reason or "").strip().upper()
        timeout_reasons = {
            "NO_CALLBACK_TIMEOUT",
            "NO_KIS_EXECUTION_TIMEOUT",
            "EXIT_NO_CALLBACK_TIMEOUT",
            "EXIT_NO_KIS_EXECUTION_TIMEOUT",
            "EXIT_NEEDS_SERVER_CHECK",
            "EXIT_SEND_FAIL",
        }
        pending_state = bool(
            self.server_sync_pending
            or self.entry_inflight
            or self.exit_inflight
            or self.exit_in_progress
            or self.cancel_in_progress
            or self.position_close_pending
            or self.exit_needs_server_check
            or self.cancel_needs_server_check
        )
        if not pending_state:
            return False, ""
        execution_notice_age = now_ts - float(self._last_execution_notice_ts or 0.0)
        msg_age = now_ts - float(self._last_server_msg_ts or 0.0)
        sync_age = now_ts - float(self._last_server_sync_request_ts or 0.0)
        if reason_u in timeout_reasons and sync_age >= 5.0:
            return True, reason_u
        if (
            (self._tr_failure_streak.get("KIS_OPEN_ORDERS", 0) >= 2 or self._tr_failure_streak.get("KIS_BALANCE", 0) >= 2)
            and sync_age >= 5.0
            and execution_notice_age >= 8.0
            and msg_age >= 8.0
        ):
            return True, "TR_AND_RESPONSE_STALL"
        return False, ""

    def _health_recheck_cooldown_sec(self, policy: str) -> float:
        policy_u = str(policy or "").upper()
        if policy_u == "MIDDAY_TR_AND_KIS_EXECUTION":
            # A zero cooldown turns a transient KIS REST slowdown into a tight
            # balance/open-order retry loop.  That loop competes with the very
            # reconciliation request needed to clear an unknown order.
            return max(
                15.0,
                float(getattr(self.cfg, "AUTO_RECONNECT_HEALTH_RECHECK_COOLDOWN_MIDDAY_SEC", 15.0) or 15.0),
            )
        if policy_u == "TR_ONLY":
            return float(getattr(self.cfg, "AUTO_RECONNECT_HEALTH_RECHECK_OFFHOURS_SEC", 20.0) or 20.0)
        return float(getattr(self.cfg, "AUTO_RECONNECT_HEALTH_RECHECK_COOLDOWN_SEC", 12.0) or 12.0)

    def _run_health_recheck(self, *, now_ts: float, reason: str, connect_state: int) -> bool:
        sync_age = now_ts - float(self._last_server_sync_request_ts or 0.0)
        if sync_age < 3.0:
            return False
        if self.unfilled_inflight or self.takeover_inflight:
            return False
        self._health_recheck_last_ts = now_ts
        self._health_recheck_reason = str(reason or "")
        self.log("HEALTH", "HEALTH_RECHECK", f"reason={reason} connect_state={connect_state}")
        self._request_server_sync(
            reason="HEALTH_RECHECK",
            include_takeover=True,
            include_unfilled=True,
            start_warmup_after_response=False,
            force=True,
        )
        return True

    def _attempt_api_reconnect(self, reason: str, *, now_ts: float | None = None) -> bool:
        if self._shutdown_in_progress:
            return False
        now_ts = time.time() if now_ts is None else float(now_ts)
        cooldown_sec = float(getattr(self.cfg, "AUTO_RECONNECT_COOLDOWN_SEC", 30.0) or 30.0)
        if now_ts - float(self._reconnect_last_attempt_ts or 0.0) < cooldown_sec:
            return False
        if self._reconnect_attempt_inflight:
            return False
        self._reconnect_last_attempt_ts = now_ts
        self._reconnect_attempt_inflight = True
        self._reconnect_last_reason = str(reason or "")
        self.want_auto_after_login = bool(self.auto_on or self.want_auto_after_login)
        self.connected = False
        self.real_registered = False
        self.server_sync_pending = True
        state = self._safe_get_connect_state()
        self.log("HEALTH", "RECONNECT_ATTEMPT", f"reason={reason} connect_state={state} auto_restore={int(bool(self.want_auto_after_login))}")
        self._telegram_send_reconnect_attempt(str(reason or ""))
        try:
            self.client.connect_api()
            return True
        except Exception as e:
            self._reconnect_attempt_inflight = False
            self.log("HEALTH", "RECONNECT_ATTEMPT_FAIL", f"reason={reason} error={e}")
            return False

    def _maybe_auto_reconnect(self) -> None:
        now_ts = time.time()
        policy = self._connection_recovery_policy()
        if policy == "OFF" or self._shutdown_in_progress:
            return
        connect_state = self._safe_get_connect_state()
        tr_fail_threshold = int(getattr(self.cfg, "AUTO_RECONNECT_TR_FAIL_THRESHOLD", 2) or 2)
        open_orders_fail = int(self._tr_failure_streak.get("KIS_OPEN_ORDERS", 0) or 0)
        balance_fail = int(self._tr_failure_streak.get("KIS_BALANCE", 0) or 0)
        tr_failure_hit = bool(open_orders_fail >= tr_fail_threshold or balance_fail >= tr_fail_threshold)
        recheck_cooldown = self._health_recheck_cooldown_sec(policy)
        if policy == "TR_ONLY":
            if tr_failure_hit:
                if now_ts - float(self._health_recheck_last_ts or 0.0) >= recheck_cooldown:
                    if self._run_health_recheck(
                        now_ts=now_ts,
                        reason=f"{self._market_phase_label()}_TR_FAIL kis_open_orders={open_orders_fail} kis_balance={balance_fail}",
                        connect_state=connect_state,
                    ):
                        return
                # REST failures do not mean the KIS session or order websocket
                # is disconnected. Reconnecting a live session starts warmup
                # and several more REST calls, worsening a throttle/timeout.
                if connect_state != 1:
                    self._attempt_api_reconnect(f"{self._market_phase_label()}_TR_FAIL kis_open_orders={open_orders_fail} kis_balance={balance_fail} state={connect_state}", now_ts=now_ts)
            return
        stuck, stuck_reason = self._is_midday_reconnect_stuck(now_ts)
        if not stuck:
            return
        if now_ts - float(self._health_recheck_last_ts or 0.0) >= recheck_cooldown:
            if self._run_health_recheck(now_ts=now_ts, reason=stuck_reason, connect_state=connect_state):
                return
        if connect_state != 1:
            self._attempt_api_reconnect(f"MIDDAY_STUCK reason={stuck_reason} kis_open_orders={open_orders_fail} kis_balance={balance_fail} state={connect_state}", now_ts=now_ts)

    def _check_login_wait(self, now_ts: float) -> None:
        started_ts = float(getattr(self, "_login_wait_started_ts", 0.0) or 0.0)
        if started_ts <= 0.0 or self.connected or self._shutdown_in_progress:
            return
        elapsed = now_ts - started_ts
        warn_sec = max(3.0, float(getattr(self.cfg, "LOGIN_WAIT_WARN_SEC", 8.0) or 8.0))
        timeout_sec = max(warn_sec + 3.0, float(getattr(self.cfg, "LOGIN_WAIT_TIMEOUT_SEC", 25.0) or 25.0))
        connect_state = self._safe_get_connect_state()
        reason = str(getattr(self, "_login_wait_reason", "") or "KIS_OAUTH")
        if elapsed >= warn_sec and not self._login_wait_warning_sent:
            self._login_wait_warning_sent = True
            self.log("HEALTH", "LOGIN_WAIT", f"reason={reason} elapsed_sec={elapsed:.1f} connect_state={connect_state} watchdog={int(bool(self._watchdog_mode))}")
        if elapsed >= warn_sec and not self._login_wait_popup_sent and (self._watchdog_mode or self._should_bypass_dialogs()):
            self._login_wait_popup_sent = True
            self.log("HEALTH", "LOGIN_WAIT", "KIS OAuth 응답 대기")
        if elapsed < timeout_sec or self._login_wait_timeout_handled:
            return
        self._login_wait_timeout_handled = True
        self.log("HEALTH", "LOGIN_TIMEOUT", f"reason={reason} elapsed_sec={elapsed:.1f} connect_state={connect_state}")
        _write_process_diagnostic(
            "LOGIN_TIMEOUT",
            f"reason={reason} elapsed_sec={elapsed:.1f} connect_state={connect_state} watchdog={int(bool(self._watchdog_mode))}",
            base_dir=self._base_dir,
        )
        if self._telegram_enabled and self._telegram_token and self._telegram_chat_id:
            self._telegram_queue(
                "kis_login_timeout",
                f"⚠️ KIS OAuth 접속 지연\n사유={reason}\n경과초={elapsed:.1f}\n서버=PAPER",
            )
            self.log("TELEGRAM", "QUEUE", f"login_timeout reason={reason}")
        if self._watchdog_mode:
            self.request_safe_shutdown("LOGIN_TIMEOUT_RESTART")
            return
        self._reconnect_attempt_inflight = False
        self.try_login(reason="LOGIN_TIMEOUT_RETRY")

    def _unfilled_sync_timeout(self, reason: str = "SYNC", sync_token: int | None = None) -> None:
        if self.unfilled_inflight and self._tr_timeout_owns_active_query("KIS_OPEN_ORDERS", reason):
            self.unfilled_inflight = False
            self.log("SYNC", "UNFILLED_TIMEOUT", f"KIS_OPEN_ORDERS 응답 없음 reason={reason}")
            self._mark_tr_query_complete("KIS_OPEN_ORDERS", reason, status="TIMEOUT")
            self._mark_server_sync_part_done("unfilled", sync_token)

    @staticmethod
    def _normalize_server_side(row: dict[str, Any]) -> str:
        candidates = [
            row.get("매도수구분"), row.get("매매구분"), row.get("구분"), row.get("주문구분"),
        ]
        for raw in candidates:
            text = str(raw or "").strip().upper().replace("+", "").replace("-", "")
            if not text:
                continue
            if text in ("1", "매도", "SELL", "SHORT", "SELLSHORT"):
                return "SHORT"
            if text in ("2", "매수", "BUY", "LONG"):
                return "LONG"
            if "매도" in text or "SELL" in text or "SHORT" in text:
                return "SHORT"
            if "매수" in text or "BUY" in text or "LONG" in text:
                return "LONG"
        return ""

    def _infer_balance_execution_notice_side(self, readable: dict[str, Any], pending_side: str = "") -> str:
        for candidate in (
            pending_side,
            getattr(self, "pending_entry_side", ""),
            getattr(self, "_entry_order_guard_side", ""),
        ):
            side_norm = str(candidate or "").upper()
            if side_norm in ("LONG", "SHORT"):
                return side_norm
        side = self._normalize_server_side(readable or {})
        if side in ("LONG", "SHORT"):
            return side
        try:
            net_qty = int(float(str((readable or {}).get("당일순매수수량") or "").replace(",", "").strip() or "0"))
            if net_qty < 0:
                return "SHORT"
            if net_qty > 0:
                return "LONG"
        except Exception:
            pass
        return ""

    def _balance_execution_notice_entry_price(self, readable: dict[str, Any]) -> float:
        for key in ("매입단가", "평균단가", "평균가", "체결가", "주문가격", "현재가"):
            try:
                price = float(self._to_float((readable or {}).get(key)))
            except Exception:
                price = 0.0
            if price > 0:
                return price
        return 0.0

    def _recover_entry_fill_from_balance_execution_notice(
        self,
        *,
        payload: dict,
        readable: dict[str, Any],
        bal_qty: int,
        pending_side: str = "",
        pending_code: str = "",
        pending_order_no: str = "",
    ) -> bool:
        if int(bal_qty or 0) <= 0:
            return False
        pending = getattr(self.order_core, "pending", None) if self.order_core else None
        pending_active = bool(
            pending is not None
            and bool(getattr(pending, "active", False))
        )
        pending_action = str(getattr(pending, "action", "") or "").upper() if pending is not None else ""
        pending_is_entry = bool(pending_active and pending_action == "ENTRY")
        # EXIT 체결 직후 서버 FLAT 확인 대기 구간의 잔고 체잔은 신규 진입 복구가 아니다.
        # 이 구간을 ENTRY_FILLED 로 승격하면 EXIT 화면이 고정되고 포지션이 반대로 복구된다.
        if (
            (self._post_exit_server_sync_required or self.exit_in_progress or self.exit_inflight)
            and (not pending_is_entry)
        ):
            self.log(
                "SYNC",
                "ENTRY_FILL_RECOVERY_SKIP",
                (
                    f"reason=EXIT_SYNC_HOLD qty={bal_qty} "
                    f"pending={pending_action or '-'} "
                    f"post_exit_sync={int(bool(self._post_exit_server_sync_required))}"
                ),
            )
            return False
        recent_exit_sec = float(getattr(self.cfg, "KIS_EXECUTION_EXIT_RECOVERY_BLOCK_SEC", 3.0) or 3.0)
        if (
            (time.time() - float(self._last_exit_filled_ts or 0.0)) <= max(0.0, recent_exit_sec)
            and (not pending_is_entry)
            and (not self._entry_order_guard_active)
        ):
            self.log(
                "SYNC",
                "ENTRY_FILL_RECOVERY_SKIP",
                f"reason=RECENT_EXIT_GUARD qty={bal_qty} sec={recent_exit_sec:.1f}",
            )
            return False
        recent_fill_sec = float(getattr(self.cfg, "KIS_EXECUTION_ENTRY_RECOVERY_GUARD_SEC", 1.0) or 1.0)
        if (time.time() - float(self._last_entry_filled_ts or 0.0)) <= max(0.0, recent_fill_sec):
            return False
        local_side = str(self.position_side or "FLAT").upper()
        local_qty = int(self.position_qty or 0)
        needs_recovery = bool(
            local_side not in ("LONG", "SHORT")
            or local_qty <= 0
            or self.entry_inflight
            or self._entry_order_guard_active
        )
        if not needs_recovery:
            return False
        side = self._infer_balance_execution_notice_side(readable, pending_side=pending_side)
        if side not in ("LONG", "SHORT"):
            return False
        price = self._balance_execution_notice_entry_price(readable)
        event_ts = str((readable or {}).get("주문/체결시간") or "")
        order_no = str((readable or {}).get("주문번호") or pending_order_no or "").strip()
        code = str((readable or {}).get("종목코드") or pending_code or self.live_code or "").strip()
        if self.order_core:
            try:
                if (
                    pending is not None
                    and bool(getattr(pending, "active", False))
                    and str(getattr(pending, "action", "") or "").upper() == "ENTRY"
                ):
                    promoted = bool(self.order_core.promote_entry_fill_from_balance(
                        side=side,
                        qty=bal_qty,
                        price=price,
                        payload=payload,
                        ts=event_ts,
                        reason="BALANCE_KIS_EXECUTION_RECOVERY",
                    ))
                    if promoted:
                        self.log("SYNC", "ENTRY_FILL_FALLBACK", f"side={side} qty={bal_qty} via=BALANCE_KIS_EXECUTION")
                        return True
            except Exception:
                pass
        self.on_execution_event(ExecutionEvent(
            "ENTRY_FILLED",
            side=side,
            qty=int(bal_qty or 0),
            price=float(price or 0.0),
            code=code,
            order_no=order_no,
            ts=event_ts,
            message="BALANCE_KIS_EXECUTION_RECOVERY",
            raw={"payload": payload or {}, "balance_execution_notice_recovery": True},
        ))
        try:
            if self.order_core and getattr(self.order_core, "pending", None) is not None:
                pending = self.order_core.pending
                if str(getattr(pending, "action", "") or "").upper() == "ENTRY":
                    self.order_core.pending = type(pending)()
                    self.order_core.last_order_result = "ENTRY_FILLED_BALANCE_KIS_EXECUTION_RECOVERY"
        except Exception:
            pass
        self.log("SYNC", "ENTRY_FILL_RECOVERED", f"side={side} qty={bal_qty} via=BALANCE_KIS_EXECUTION_FORCE")
        return True

    @staticmethod
    def _to_abs_int(value: Any) -> int:
        try:
            return abs(int(float(str(value or "").replace(",", "").replace("+", "").strip() or "0")))
        except Exception:
            return 0

    def _on_unfilled_tr(self, payload: dict, sync_token: int | None = None) -> None:
        try:
            self.unfilled_inflight = False
            rows = payload.get("unfilled_rows") or []
            debug = payload.get("unfilled_debug") or {}
            self.unfilled_rows_last = len(rows)
            matched = []
            target_codes = {self.live_code.strip(), self.live_code.strip().lstrip("A")}
            for row in rows:
                full_code = str(row.get("종목코드") or "").strip()
                code_norm = full_code.lstrip("A")
                if full_code and full_code not in target_codes and code_norm not in target_codes:
                    continue
                remaining_qty = self._to_abs_int(row.get("미체결수량"))
                if remaining_qty <= 0:
                    continue
                matched.append((row, remaining_qty))

            effective_pairs = list(matched)
            if self._auto_precheck_active:
                seen_ids: set[str] = set()
                for row, qty in matched:
                    uid = self._unfilled_identity(row)
                    seen_ids.add(uid)
                    if str(self.server_sync_reason or "") == "AUTO_PRECHECK":
                        if self._is_precheck_old_unfilled_row(row):
                            if uid not in self._precheck_stale_unfilled_ids:
                                self._precheck_stale_unfilled_ids.add(uid)
                                self.log("SYNC", "PRECHECK_TARGET_STALE_UNFILLED", f"id={uid} qty={qty}")
                        else:
                            self.log("SYNC", "PRECHECK_SKIP_NEW_UNFILLED", f"id={uid} qty={qty} (after_auto_on)")
                stale_pairs = []
                new_pairs = []
                for row, qty in matched:
                    uid = self._unfilled_identity(row)
                    if uid in self._precheck_stale_unfilled_ids:
                        stale_pairs.append((row, qty))
                    else:
                        new_pairs.append((row, qty))
                self._precheck_stale_unfilled_ids.intersection_update(seen_ids)
                self._precheck_stale_unfilled_qty = sum(qty for _, qty in stale_pairs)
                if new_pairs:
                    for row, qty in new_pairs:
                        self.log("SYNC", "PRECHECK_SKIP_NEW_UNFILLED", f"id={self._unfilled_identity(row)} qty={qty}")
                effective_pairs = stale_pairs

            if effective_pairs:
                qty_sum = sum(qty for _, qty in effective_pairs)
                top_row = effective_pairs[0][0]
                self.has_server_unfilled = True
                self._mark_unfilled_detected()
                self.server_unfilled_qty = qty_sum
                self.server_unfilled_order_no = str(top_row.get("주문번호") or "").strip()
                self.server_unfilled_orig_order_no = str(top_row.get("원주문번호") or "").strip()
                self.server_unfilled_side = self._normalize_server_side(top_row)
                self.server_unfilled_symbol = str(top_row.get("종목코드") or self.live_code).strip() or self.live_code
                self.server_unfilled_status = str(top_row.get("주문상태") or "").strip()
                self.server_unfilled_last_seen_ts = str(top_row.get("주문/체결시간") or datetime.now().strftime("%H:%M:%S")).strip()
                self.server_unfilled_last_reason = self.server_sync_reason
                self.auto_cancel_phase = "SERVER_UNFILLED_STILL_OPEN"
                self.log(
                    "SYNC",
                    "UNFILLED_OPEN",
                    f"rows={len(effective_pairs)} qty={qty_sum} order_no={self.server_unfilled_order_no or '-'} "
                    f"orig={self.server_unfilled_orig_order_no or '-'} side={self.server_unfilled_side or '-'} "
                    f"status={self.server_unfilled_status or '-'} debug={debug or {}}",
                )
                self.cancel_confirmed = False
                self.cancel_needs_server_check = False
                if self.cancel_in_progress:
                    self.auto_cleanup_phase = "AUTO_CANCEL_WAIT"
                if self.order_core:
                    self.order_core.apply_server_unfilled_state(
                        True,
                        qty=qty_sum,
                        order_no=self.server_unfilled_order_no,
                        original_order_no=self.server_unfilled_orig_order_no,
                        reason=self.server_sync_reason,
                    )
            else:
                self._precheck_stale_unfilled_qty = 0 if self._auto_precheck_active else self._precheck_stale_unfilled_qty
                self._reset_server_unfilled_state(reason=self.server_sync_reason)
                self.server_unfilled_last_seen_ts = datetime.now().strftime("%H:%M:%S")
                self.log("SYNC", "UNFILLED_CLEAR", f"rows={len(rows)} debug={debug or {}}")
                if self.cancel_in_progress:
                    self.cancel_in_progress = False
                    self.cancel_confirmed = str(self.server_sync_reason or "") in {"MANUAL_CANCEL", "AUTO_PRECHECK_CANCEL", "AUTO_CANCEL_REQUESTED", "CANCEL_CONFIRMED"}
                    self.cancel_needs_server_check = False
                    self._manual_cancel_check_pending = False
                if self.auto_on:
                    self.auto_cleanup_phase = "AUTO_UNFILLED_CLEARED"
                if self.order_core:
                    self.order_core.apply_server_unfilled_state(False, reason=self.server_sync_reason)
                self._release_or_retry_post_exit_entry_unknown(reason=str(self.server_sync_reason or "UNFILLED_CLEAR"))
                reason_u = str(self.server_sync_reason or "").strip().upper()
                if self._is_exit_sync_recheck_reason(reason_u):
                    QTimer.singleShot(
                        int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                        self._request_exit_balance_after_unfilled,
                    )
                if (
                    reason_u in {"AUTO_CANCEL_SEND_FAIL", "CANCEL_SEND_FAIL", "NO_CALLBACK_TIMEOUT", "NO_KIS_EXECUTION_TIMEOUT"}
                    and (self.entry_inflight or self._entry_order_guard_active or self.cancel_needs_server_check)
                    and (not self.has_server_unfilled)
                    and (not self.has_unfilled_orders)
                ):
                    QTimer.singleShot(
                        int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                        self._request_entry_unknown_balance_recheck,
                    )
                pending_exit = str(self.pending_exit_reason or "").strip()
                if (
                    self.auto_on
                    and reason_u in {"CANCEL_CONFIRMED", "AUTO_RUNTIME_CANCEL"}
                    and self._has_server_position()
                    and self._looks_like_exit_reason(pending_exit)
                    and (not self.exit_inflight)
                    and (not self.position_close_pending)
                    and (not self.exit_needs_server_check)
                ):
                    self.log("SYNC", "EXIT_RETRY_AFTER_CANCEL", f"reason={pending_exit} side={self.position_side} qty={self.position_qty}")
                    QTimer.singleShot(
                        int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                        lambda: self._submit_exit_cleanup(pending_exit),
                    )
        finally:
            reason_active = self._get_active_tr_query_reason("KIS_OPEN_ORDERS")
            if reason_active:
                self._mark_tr_query_complete("KIS_OPEN_ORDERS", reason_active, status="OK")
            self._mark_server_sync_part_done("unfilled", sync_token)
            if self.cancel_in_progress and self.has_server_unfilled and ((reason_active == "MANUAL_CANCEL_CHECK") or bool(self._manual_cancel_check_pending)):
                self._manual_cancel_check_pending = False
                self._submit_cancel_cleanup("MANUAL_CANCEL")
            elif (
                self.cancel_in_progress
                and self.has_server_unfilled
                and str(self.server_sync_reason or "").strip().upper().startswith("REVERSAL_FIRE_BAND_EXIT")
            ):
                self._submit_cancel_cleanup("REVERSAL_FIRE_BAND_EXIT")
            self._refresh_cleanup_statuses()
            if self.auto_on or self.cancel_in_progress:
                self._advance_auto_cleanup()
            self.refresh_view()

    def _record_live_bar_and_z(self, c: Candle, source: str = "LIVE", counts_override: dict[str, int] | None = None) -> None:
        try:
            dt = c.t if isinstance(c.t, datetime) else datetime.now()
            counts = dict(counts_override or self.st.get_counts() or {})
            zmap = self.st.get_realtime_z_scores(float(c.c), dt if "dt" in locals() else c.t)
            regime = self.st.check_regime(dt, current_price=float(c.c))
            minute_key = dt.replace(second=0, microsecond=0)
            self._live_bars_by_minute[minute_key] = [
                dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), self.live_code,
                self._fmt_num(c.o, 2), self._fmt_num(c.h, 2), self._fmt_num(c.l, 2), self._fmt_num(c.c, 2), int(c.v or 0), source,
            ]
            self._live_bars_by_minute = OrderedDict(sorted(self._live_bars_by_minute.items(), key=lambda item: item[0]))
            now_ts = time.time()
            min_interval = float(getattr(self, "_live_bar_snapshot_min_interval_sec", 0.0) or 0.0)
            last_write = float(getattr(self, "_last_live_bar_snapshot_write_ts", 0.0) or 0.0)
            if min_interval <= 0.0 or now_ts - last_write >= min_interval:
                self._rewrite_live_bars_snapshot()
                self._last_live_bar_snapshot_write_ts = now_ts
            self._append_csv_row(self._live_zscore_path, [
                dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S"), self.live_code,
                self._fmt_num(c.c, 2),
                self._fmt_num(zmap.get("z1"), 6), self._fmt_num(zmap.get("z5"), 6), self._fmt_num(zmap.get("z30"), 6),
                regime, counts.get("bars_1m", 0), counts.get("bars_5m", 0), counts.get("bars_30m", 0), source,
                int(getattr(self, "_diag_stale_tick_skip_count", 0) or 0),
                int(getattr(self, "_diag_same_minute_update_count", 0) or 0),
                int(getattr(self, "_diag_new_minute_append_count", 0) or 0),
                int(getattr(self, "_diag_dedupe_before_1m", 0) or 0),
                int(getattr(self, "_diag_dedupe_after_1m", 0) or 0),
                int(getattr(self, "_diag_dedupe_before_5m_input", 0) or 0),
                int(getattr(self, "_diag_dedupe_after_5m_input", 0) or 0),
                int(getattr(self, "_diag_dedupe_before_30m_input", 0) or 0),
                int(getattr(self, "_diag_dedupe_after_30m_input", 0) or 0),
                getattr(self, "_diag_last_1m_minute_key", None).strftime("%Y-%m-%d %H:%M:%S") if isinstance(getattr(self, "_diag_last_1m_minute_key", None), datetime) else "",
                getattr(self, "_diag_last_5m_bucket_key", None).strftime("%Y-%m-%d %H:%M:%S") if isinstance(getattr(self, "_diag_last_5m_bucket_key", None), datetime) else "",
                getattr(self, "_diag_last_30m_bucket_key", None).strftime("%Y-%m-%d %H:%M:%S") if isinstance(getattr(self, "_diag_last_30m_bucket_key", None), datetime) else "",
            ])
        except Exception as e:
            now_ts = time.time()
            if now_ts - float(getattr(self, "_last_live_zscore_write_error_ts", 0.0) or 0.0) >= 5.0:
                self._last_live_zscore_write_error_ts = now_ts
                self.log("ERROR", "LIVE_ZSCORE_SAVE", str(e))

    def _dump_warmup_history(self, candles: list[Candle], source: str, target_rows: int) -> dict[str, Any]:
        try:
            if not candles:
                self._append_csv_row(self._warmup_summary_path, [self._session_id, source, target_rows, 0, 0, 0, 0, False, "", ""])
                return {
                    "session_date": None,
                    "min_z5": None,
                    "max_z5": None,
                    "last_z5": None,
                    "recent_z5": [],
                    "min_price": None,
                    "min_dt": None,
                }
            st_dump = Strategy(self.cfg)
            last_dt = None
            last_close = None
            counts = {"bars_1m": 0, "bars_5m": 0, "bars_30m": 0}
            min_z5 = None
            max_z5 = None
            last_z5 = None
            recent_z5 = []
            min_z5_price = None
            min_z5_dt = None
            for seq, c in enumerate(candles, start=1):
                st_dump.update_indicators(c)
                counts = st_dump.get_counts()
                zmap = st_dump.get_realtime_z_scores(float(c.c), c.t)
                regime = st_dump.check_regime(c.t, current_price=float(c.c))
                try:
                    _z5v = zmap.get("z5")
                    if _z5v is not None:
                        _z5f = float(_z5v)
                        last_z5 = _z5f
                        if min_z5 is None or _z5f < min_z5:
                            min_z5 = _z5f
                            min_z5_price = float(c.c)
                            min_z5_dt = c.t
                        if max_z5 is None or _z5f > max_z5:
                            max_z5 = _z5f
                        recent_z5.append(_z5f)
                        if len(recent_z5) > 3:
                            recent_z5 = recent_z5[-3:]
                except Exception:
                    pass
                last_dt = c.t
                last_close = float(c.c)
                self._append_csv_row(self._warmup_bar_path, [
                    c.t.strftime("%Y-%m-%d"), c.t.strftime("%H:%M:%S"), self.live_code,
                    self._fmt_num(c.o, 2), self._fmt_num(c.h, 2), self._fmt_num(c.l, 2), self._fmt_num(c.c, 2), int(c.v or 0), source, seq,
                ])
                self._append_csv_row(self._warmup_zscore_path, [
                    c.t.strftime("%Y-%m-%d"), c.t.strftime("%H:%M:%S"), self.live_code,
                    self._fmt_num(c.c, 2),
                    self._fmt_num(zmap.get("z1"), 6), self._fmt_num(zmap.get("z5"), 6), self._fmt_num(zmap.get("z30"), 6),
                    regime, counts.get("bars_1m", 0), counts.get("bars_5m", 0), counts.get("bars_30m", 0), source, seq,
                ])
            lookback = int(getattr(self.cfg, "LOOKBACK", 20) or 20)
            ready = counts.get("bars_5m", 0) >= lookback
            self._append_csv_row(self._warmup_summary_path, [
                self._session_id, source, int(target_rows or 0), len(candles), counts.get("bars_1m", 0), counts.get("bars_5m", 0), counts.get("bars_30m", 0), ready,
                last_dt.strftime("%Y-%m-%d %H:%M:%S") if last_dt else "", self._fmt_num(last_close, 2) if last_close is not None else "",
            ])
            self.log("WARMUP", "SAVE", f"warmup dump saved rows={len(candles)} source={source} -> {os.path.basename(self._warmup_bar_path)}")
            session_date = (last_dt.date() if isinstance(last_dt, datetime) else None)
            return {
                "session_date": session_date,
                "min_z5": (float(min_z5) if min_z5 is not None else None),
                "max_z5": (float(max_z5) if max_z5 is not None else None),
                "last_z5": (float(last_z5) if last_z5 is not None else None),
                "recent_z5": [float(v) for v in recent_z5[-3:]],
                "min_price": (float(min_z5_price) if min_z5_price is not None else None),
                "min_dt": min_z5_dt,
            }
        except Exception as e:
            self.log("ERROR", "WARMUP_SAVE", str(e))
            return {
                "session_date": None,
                "min_z5": None,
                "max_z5": None,
                "last_z5": None,
                "recent_z5": [],
                "min_price": None,
                "min_dt": None,
            }

    def _calc_live_target_exit_index(self) -> float | None:
        """현재 라이브 EXIT_Z 목표 가격을 계산."""
        try:
            if self.position_side not in ("LONG", "SHORT"):
                return None
            if not bool(getattr(self.cfg, "USE_EXIT_Z", False)):
                return None
            return self._signed_exit_z()
        except Exception:
            return None

    def _signed_exit_z(self) -> float | None:
        try:
            if not bool(getattr(self.cfg, "USE_EXIT_Z", False)):
                return None
            _state = self.last_sig_state if isinstance(self.last_sig_state, dict) else {}
            long_thr = _state.get('exit_z_long')
            short_thr = _state.get('exit_z_short')
            if self.position_side == "LONG":
                if long_thr is not None:
                    return float(long_thr)
                return None
            if self.position_side == "SHORT":
                if short_thr is not None:
                    return float(short_thr)
                return None
            return None
        except Exception:
            return None

    def _stop_loss_threshold_pt(self) -> float:
        try:
            return float(getattr(self.cfg, 'BT_STOP_LOSS_PT', getattr(self.cfg, 'SL', 3.5)) or 3.5)
        except Exception:
            return 3.5

    def _has_server_position(self) -> bool:
        return bool(self.position_side in ("LONG", "SHORT") and self.position_qty > 0)

    def _clear_local_position_after_server_flat_confirm(self, reason: str = "SERVER_FLAT_CONFIRMED") -> None:
        """Clear local position only after server/broker balance confirms FLAT."""
        self.position_side = "FLAT"
        self.position_qty = 0
        self.active_position_side = "FLAT"
        self.active_position_qty = 0
        self.entry_price = 0.0
        self.position_entry_time = None
        self.position_entry_z5 = None
        self.position_entry_prev_z5 = None
        self.position_entry_dz5 = None
        self.position_entry_dz5_threshold = None
        self.position_entry_dz5_direction_ok = None
        self.position_entry_macd_osci = None
        self.position_entry_regime = ""
        self.position_entry_z5_band_snapshot = {}
        self.position_mfe_profile = ""
        self.position_peak_price = 0.0
        self.position_trough_price = 0.0
        self.position_mfe_armed = False
        self.position_mfe_armed_since_ts = 0.0
        self.position_mfe_stage_step = 0
        self._position_mfe_stage_last_bar_key = ""
        self._position_mfe_stage_last_extreme_price = 0.0
        self._entry_mu = None
        self._entry_sd = None
        self._fixed_exit_index = None
        # Do not carry a pre-exit ARM/FIRE/timer into the next flat cycle.
        # A fresh entry after EXIT_FILLED must restart its own wait/watch window.
        self._clear_entry_signal_state_after_flat(str(reason or "SERVER_FLAT_CONFIRMED"))
        self.log("SIGNAL", "ENTRY_WATCH_RESET", "source=SERVER_FLAT_CONFIRMED stale_arm/fire=CLEAR watcher=KEEP")
        try:
            self._persist_entry_anchor_state(str(reason or "SERVER_FLAT_CONFIRMED"))
        except Exception:
            pass

    def _position_side_for_cancel(self) -> str:
        if self.server_unfilled_side in ("LONG", "SHORT"):
            return self.server_unfilled_side
        if self.order_core and getattr(self.order_core.pending, "side", "") in ("LONG", "SHORT"):
            return str(self.order_core.pending.side)
        return "LONG"

    def _derive_cancel_status(self) -> str:
        if self.cancel_needs_server_check:
            return "취소 실패 - 서버확인 필요"
        if self.cancel_in_progress:
            if self.server_sync_pending or self.has_server_unfilled:
                return "자동취소 중"
            return "취소 대기"
        if self.cancel_confirmed and not self.has_server_unfilled:
            return "취소 완료"
        if self.has_server_unfilled:
            return f"미체결 {int(self.server_unfilled_qty or 0)}건" if int(self.server_unfilled_qty or 0) > 0 else "미체결 있음"
        if self.has_unfilled_orders:
            return "미체결 감지(서버확인 대기)"
        return "미체결 없음"

    def _derive_exit_status(self) -> str:
        if bool(getattr(self, "_post_exit_entry_awaiting_confirm", False)):
            return "청산완료 / 재진입 확인중"
        if bool(getattr(self, "_post_exit_entry_pending", False)):
            return "청산완료 / 재진입 대기"
        if bool(getattr(self, "_post_exit_server_sync_required", False)):
            return "청산 확인중"
        if self._exit_pending_active_for_submit_guard():
            return "청산 주문확인중"
        if self.exit_needs_server_check:
            return "청산 실패 - 서버확인 필요"
        if self.exit_in_progress:
            return "자동청산 중" if self.auto_on else "청산 대기"
        if self.exit_confirmed and not self._has_server_position():
            return "청산 완료"
        if self._has_server_position():
            return f"보유 {int(self.position_qty or 0)}계약"
        return "보유 없음"

    def _refresh_cleanup_statuses(self) -> None:
        self.cancel_status = self._derive_cancel_status()
        self.exit_button_status = self._derive_exit_status()

    def _kis_paper_orders_enabled(self) -> bool:
        try:
            client = getattr(self, "client", None)
            if client is None:
                return True
            settings = getattr(client, "settings", None)
            # Test/legacy client doubles without KIS settings predate the
            # paper-order safety switch and retain their prior behavior.
            return True if settings is None else bool(settings.order_enabled)
        except Exception:
            return False

    def _submit_cancel_cleanup(self, reason: str) -> bool:
        if not self.order_core or not self.connected or not self.account:
            self.cancel_needs_server_check = True
            self._refresh_cleanup_statuses()
            return False
        reason_s = str(reason or "")
        pending = getattr(self.order_core, "pending", None)
        pending_entry_recheck = bool(
            pending is not None
            and bool(getattr(pending, "active", False))
            and str(getattr(pending, "action", "") or "").upper() == "ENTRY"
            and bool(getattr(pending, "server_recheck_required", False))
        )
        if pending_entry_recheck:
            # A failed cancel is an unknown broker state, not permission to
            # retry on every tick/event.  Wait for KIS balance/open-orders sync
            # to decide whether the order filled, remained open, or vanished.
            self.cancel_in_progress = False
            self.cancel_needs_server_check = True
            self.server_sync_pending = True
            self.auto_cancel_phase = "UNFILLED_RECHECK_REQUIRED"
            self._refresh_lane_snapshot()
            self._refresh_cleanup_statuses()
            return False
        if not ZenithZScoreController._kis_paper_orders_enabled(self):
            self.cancel_in_progress = False
            self.cancel_needs_server_check = False
            self.server_sync_pending = False
            self.auto_cleanup_phase = "AUTO_BLOCKED_ORDER_LOCKED"
            self._log_exit_guard_once(
                "CANCEL_BLOCKED_ORDER_LOCKED",
                f"reason={reason_s or '-'} unfilled_qty={int(self.server_unfilled_qty or 0)}",
                key="KIS_PAPER_ORDER_ENABLED=0",
                interval_sec=30.0,
            )
            self._refresh_cleanup_statuses()
            return False
        if reason_s == "AUTO_PRECHECK_CANCEL":
            if (not self._auto_precheck_active) or int(self._precheck_stale_unfilled_qty or 0) <= 0:
                self.log("SYNC", "PRECHECK_SKIP_NEW_UNFILLED", "AUTO_PRECHECK_CANCEL blocked (stale unfilled not confirmed)")
                return False
            self.log("SYNC", "AUTO_PRECHECK_CANCEL", f"stale_confirmed qty={int(self._precheck_stale_unfilled_qty or 0)}")
        order_candidates: list[str] = []
        for cand in (self.server_unfilled_order_no, self.server_unfilled_orig_order_no):
            norm = str(cand or "").strip()
            if norm and norm not in order_candidates:
                order_candidates.append(norm)
        qty = max(int(self.server_unfilled_qty or 0), 0)
        if pending is not None:
            for cand in (getattr(pending, "order_no", ""), getattr(pending, "original_order_no", "")):
                norm = str(cand or "").strip()
                if norm and norm not in order_candidates:
                    order_candidates.append(norm)
            try:
                pending_qty = max(int(getattr(pending, "remaining_qty", 0) or 0), 0)
            except Exception:
                pending_qty = 0
            if qty <= 0 and pending_qty > 0:
                qty = pending_qty
            try:
                if qty <= 0 and str(getattr(pending, "action", "") or "").upper() == "ENTRY" and bool(getattr(pending, "active", False)):
                    qty = max(int(getattr(pending, "qty", 0) or 0), 0)
            except Exception:
                pass
        if (not order_candidates) or qty <= 0:
            self.server_sync_pending = True
            self.cancel_in_progress = True
            self.cancel_needs_server_check = False
            self.cancel_confirmed = False
            self._manual_cancel_check_pending = bool(reason_s.startswith("MANUAL_CANCEL"))
            self._refresh_lane_snapshot()
            self._refresh_cleanup_statuses()
            self._request_server_sync(reason=f"{reason_s}_CHECK", include_takeover=False, include_unfilled=True, start_warmup_after_response=False, force=True)
            return True
        side = self._position_side_for_cancel()
        ok = False
        used_order_no = ""
        for cand in order_candidates:
            ok = bool(self.order_core.submit_cancel(cand, side, qty=qty))
            self.log("CONTROL", "CANCEL_ATTEMPT", f"reason={reason_s} order_no={cand} side={side} qty={qty} ok={ok}")
            if ok:
                used_order_no = cand
                break
        self.cancel_in_progress = bool(ok)
        self.cancel_confirmed = False
        self.cancel_needs_server_check = not bool(ok)
        self.server_sync_pending = True
        self._manual_cancel_check_pending = False
        self.auto_cancel_phase = 'AUTO_CANCEL_REQUESTED' if ok else 'UNFILLED_RECHECK_REQUIRED'
        if used_order_no:
            self.server_unfilled_order_no = used_order_no
        self._refresh_lane_snapshot()
        self._refresh_cleanup_statuses()
        if ok:
            QTimer.singleShot(
                int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                lambda: self._request_server_sync(reason=reason_s, include_takeover=False, include_unfilled=True, start_warmup_after_response=False, force=True),
            )
        return bool(ok)

    def _submit_exit_cleanup(self, reason: str) -> bool:
        if not self.order_core or not self.connected or not self.account:
            self.exit_needs_server_check = True
            self._refresh_cleanup_statuses()
            return False
        reason_s = str(reason or "").strip().upper()
        hard_exit_reasons = {"MFE_TRAIL", "MFE_PROTECT", "FIXED_LOSS"}
        if reason_s in hard_exit_reasons:
            reason_s = self._promote_pending_hard_exit_reason(reason_s, source="SUBMIT")
        # 청산 주문이 이미 주문확인/체결대기 상태이면 같은 FIXED_LOSS 신호가
        # 틱마다 다시 들어와도 재주문/이벤트 로그를 만들지 않는다.
        # 이 가드는 hard-exit cooldown override 로그보다 먼저 위치해야 로그 폭증을 막는다.
        if self._exit_pending_active_for_submit_guard():
            try:
                pending = getattr(getattr(self, "order_core", None), "pending", None)
                pending_action = str(getattr(pending, "action", "") or "") if pending is not None else ""
                pending_reason = str(getattr(pending, "reason", "") or "") if pending is not None else ""
            except Exception:
                pending_action = ""
                pending_reason = ""
            self._log_exit_guard_once(
                "EXIT_SKIP_PENDING_CONFIRM",
                (
                    f"reason={reason_s or str(reason or '-')} inflight={int(bool(self.exit_inflight))} "
                    f"close_pending={int(bool(self.position_close_pending))} pending={pending_action or '-'} "
                    f"pending_reason={pending_reason or '-'}"
                ),
                key=f"{reason_s}|{pending_action}|{pending_reason}|{int(bool(self.exit_inflight))}|{int(bool(self.position_close_pending))}",
                interval_sec=1.5,
            )
            self._maybe_request_exit_pending_recheck("SUBMIT_GUARD")
            self._refresh_cleanup_statuses()
            return False
        if self.has_server_unfilled:
            now_ts = time.time()
            skip_key = f"{reason_s}|{self.server_unfilled_order_no}|{int(self.server_unfilled_qty or 0)}"
            if (
                skip_key != str(getattr(self, "_exit_skip_server_unfilled_last_key", "") or "")
                or (now_ts - float(getattr(self, "_exit_skip_server_unfilled_last_ts", 0.0) or 0.0) >= 1.0)
            ):
                self._exit_skip_server_unfilled_last_key = skip_key
                self._exit_skip_server_unfilled_last_ts = now_ts
                self.log(
                    "SYNC",
                    "EXIT_SKIP_SERVER_UNFILLED",
                    f"reason={reason_s or '-'} order_no={self.server_unfilled_order_no or '-'} qty={int(self.server_unfilled_qty or 0)}",
                )
            if (not self.cancel_in_progress) and self.auto_on and (
                now_ts - float(getattr(self, "_auto_runtime_cancel_last_ts", 0.0) or 0.0) >= 1.5
            ):
                self._auto_runtime_cancel_last_ts = now_ts
                self._submit_cancel_cleanup("AUTO_RUNTIME_CANCEL")
            self._refresh_cleanup_statuses()
            return False
        # Manual EXIT is explicitly excluded from entry->exit cooldown lock.
        if reason_s != "MANUAL_EXIT" and reason_s not in hard_exit_reasons:
            lock_remain = self._entry_exit_lock_remaining_sec()
            if lock_remain > 0.0:
                self.log("SYNC", "EXIT_SKIP_ENTRY_COOLDOWN", f"reason={reason_s} remain={lock_remain:.1f}s")
                self._refresh_cleanup_statuses()
                return False
            fill_cd_remain = self._post_fill_action_cooldown_remaining_sec()
            if fill_cd_remain > 0.0:
                self.log("SYNC", "EXIT_SKIP_POST_FILL_COOLDOWN", f"reason={reason_s} remain={fill_cd_remain:.1f}s")
                self._refresh_cleanup_statuses()
                return False
        elif reason_s in hard_exit_reasons:
            self._log_exit_guard_once("EXIT_OVERRIDE_COOLDOWN", f"reason={reason_s} hard_exit=Y", key=reason_s, interval_sec=2.0)
        # If previous EXIT result is unresolved, block AUTO re-submit until server sync confirms state.
        if self.exit_needs_server_check and reason_s != "MANUAL_EXIT":
            # FIXED_LOSS is a hard risk-control exit: allow a direct submit path
            # when we still hold server position and no exit is currently inflight.
            _exit_recheck_active = bool(
                self.server_sync_pending
                and self._is_exit_sync_recheck_reason(str(self.server_sync_reason or "").strip().upper())
            )
            if (
                reason_s in hard_exit_reasons
                and (not _exit_recheck_active)
                and (not self.exit_inflight)
                and (not self.position_close_pending)
                and self._has_server_position()
            ):
                self._log_exit_guard_once("EXIT_OVERRIDE_NEEDS_SERVER_CHECK", f"reason={reason_s} hard_risk_exit=Y", key=reason_s, interval_sec=2.0)
            else:
                self._log_exit_guard_once("EXIT_SKIP_NEEDS_SERVER_CHECK", f"reason={reason} server_sync_pending={self.server_sync_pending}", key=f"{reason_s}|{int(bool(self.server_sync_pending))}", interval_sec=1.5)
                if not self.server_sync_pending:
                    self._request_server_sync(
                        reason="EXIT_NEEDS_SERVER_CHECK",
                        include_takeover=True,
                        include_unfilled=False,
                        start_warmup_after_response=False,
                        force=True,
                    )
                self._refresh_cleanup_statuses()
                return False
        if self.exit_inflight or self.position_close_pending:
            self._log_exit_guard_once("EXIT_SKIP_INFLIGHT", f"reason={reason} inflight={self.exit_inflight} close_pending={self.position_close_pending}", key=f"{reason_s}|{int(bool(self.exit_inflight))}|{int(bool(self.position_close_pending))}", interval_sec=1.5)
            self._maybe_request_exit_pending_recheck("INFLIGHT_GUARD")
            self._refresh_cleanup_statuses()
            return False
        if self.order_core and bool(getattr(self.order_core.pending, "active", False)) and str(getattr(self.order_core.pending, "action", "") or "").upper().startswith("EXIT"):
            self._log_exit_guard_once("EXIT_SKIP_PENDING", f"reason={reason} pending_action={getattr(self.order_core.pending, 'action', '')}", key=f"{reason_s}|{getattr(self.order_core.pending, 'action', '')}", interval_sec=1.5)
            self._maybe_request_exit_pending_recheck("ORDERCORE_PENDING_GUARD")
            self._refresh_cleanup_statuses()
            return False
        # Entry state is unresolved: block additional EXIT submissions to avoid accidental side-flip opens.
        if self.entry_inflight or self._entry_order_guard_active:
            self.log("SYNC", "EXIT_SKIP_ENTRY_PENDING", f"reason={reason} entry_inflight={self.entry_inflight} guard={self._entry_order_guard_active}")
            self.exit_needs_server_check = True
            self._refresh_cleanup_statuses()
            return False
        if str(reason or "") == "AUTO_PRECHECK_EXIT":
            precheck_exit_allowed = bool(
                self._auto_precheck_force_exit_existing_position
                or self._precheck_stale_position_detected
                or self._precheck_position_conflict_detected
            )
            if (not self._auto_precheck_active) or (not precheck_exit_allowed) or self._is_precheck_new_position():
                self.log("SYNC", "PRECHECK_SKIP_NEW_POSITION", "AUTO_PRECHECK_EXIT blocked (stale position not confirmed)")
                return False
            cause = (
                "conflict"
                if self._precheck_position_conflict_detected
                else ("stale" if self._precheck_stale_position_detected else "force_exit_existing")
            )
            self.log("SYNC", "AUTO_PRECHECK_EXIT", f"only if stale/conflict confirmed cause={cause}")
        if not self._has_server_position():
            self.exit_confirmed = False
            self.exit_in_progress = False
            self.exit_needs_server_check = False
            self._refresh_cleanup_statuses()
            return False
        _submit_key = f"{reason_s}|{self.position_side}|{int(self.position_qty or 0)}"
        _now_submit_ts = time.time()
        if (
            _submit_key == str(getattr(self, "_exit_submit_guard_last_key", "") or "")
            and (_now_submit_ts - float(getattr(self, "_exit_submit_guard_last_ts", 0.0) or 0.0) < float(getattr(self, "_exit_submit_debounce_sec", 0.8) or 0.8))
        ):
            self._log_exit_guard_once(
                "EXIT_SUBMIT_DEBOUNCE",
                f"reason={reason_s or '-'} side={self.position_side} qty={int(self.position_qty or 0)}",
                key=_submit_key,
                interval_sec=1.5,
            )
            self._refresh_cleanup_statuses()
            return False
        self._exit_submit_guard_last_key = _submit_key
        self._exit_submit_guard_last_ts = _now_submit_ts
        self._capture_pending_exit_context(str(reason or "EXIT"))
        try:
            _exit_mode = self.order_core.exit_order_mode_text(reason=reason_s) if self.order_core else "LIMIT_L1(KIS code=01)"
        except Exception:
            _exit_mode = "LIMIT_L1(KIS code=01)"
        self.log(
            "ORDER",
            "EXIT_SUBMIT_LIMIT_L1",
            f"reason={reason_s or str(reason or 'EXIT')} side={self.position_side} qty={int(self.position_qty or 0)} mode={_exit_mode} trigger_price_is_signal_only=Y",
        )
        ok = self.order_core.submit_exit(self.position_side, qty=self.position_qty, reason=reason)
        self.exit_in_progress = bool(ok)
        self.exit_inflight = bool(ok)
        self.position_close_pending = bool(ok)
        if ok:
            if reason_s in hard_exit_reasons:
                self._arm_pending_exit_entry_block(reason_s)
            self.flat_confirmed = False
            self.order_lane_locked = True
            self._exit_pending_recheck_last_ts = 0.0
            self.log("SYNC", "EXIT_SUBMIT_LOCK", "order_lane=LOCKED")
            if reason_s in hard_exit_reasons:
                # Hard risk exits should not wait for the slower generic health cycle
                # before checking whether the broker accepted but left the order unfilled.
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                lambda: self._request_server_sync(
                    reason="EXIT_PENDING_RECHECK",
                    include_takeover=False,
                    include_unfilled=True,
                    include_account=False,
                    include_orderable=False,
                    start_warmup_after_response=False,
                    force=True,
                ),
            )
                self._log_exit_guard_once(
                    "EXIT_HARD_RECHECK_ARMED",
                    f"reason={reason_s} delay_ms={int(getattr(self.cfg, 'UNFILLED_RECHECK_DELAY_MS', 350) or 350)}",
                    key=reason_s,
                    interval_sec=1.0,
                )
        self.exit_confirmed = False
        self.exit_needs_server_check = not bool(ok)
        self.server_sync_pending = self.server_sync_pending or bool(ok)
        self.auto_cleanup_phase = 'AUTO_EXIT_REQUESTED' if ok else self.auto_cleanup_phase
        self._refresh_lane_snapshot()
        self._refresh_cleanup_statuses()
        return bool(ok)

    def _advance_auto_cleanup(self) -> None:
        if not self.auto_on:
            self._auto_precheck_active = False
            self._precheck_stale_position_detected = False
            self._precheck_position_conflict_detected = False
            self._precheck_takeover_existing_position = False
            self._precheck_stale_unfilled_ids.clear()
            self._precheck_stale_unfilled_qty = 0
            self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
            self._refresh_cleanup_statuses()
            return
        if not self.connected or not self.account or not self.order_core:
            self.auto_cleanup_phase = "AUTO_PRECHECK"
            self._refresh_cleanup_statuses()
            return
        if self.has_server_unfilled and not ZenithZScoreController._kis_paper_orders_enabled(self):
            # Do not spin on every market tick when the safety lock correctly
            # rejects cancellation.  Keep entry blocked and show a stable,
            # explicit state until the operator enables paper orders or clears
            # the broker-side unfilled orders.
            self.cancel_in_progress = False
            self.cancel_needs_server_check = False
            self.server_sync_pending = False
            self.auto_cleanup_phase = "AUTO_BLOCKED_ORDER_LOCKED"
            self._log_exit_guard_once(
                "AUTO_BLOCKED_ORDER_LOCKED",
                f"unfilled_qty={int(self.server_unfilled_qty or 0)} order_enabled=0",
                key="AUTO_BLOCKED_ORDER_LOCKED",
                interval_sec=30.0,
            )
            self._refresh_cleanup_statuses()
            return
        if not self._auto_precheck_active:
            # Runtime safety path:
            # after initial AUTO_PRECHECK session is closed, server-side unfilled can still occur.
            # Keep auto-cancel alive for this path too.
            self._clear_stale_local_unfilled_flag()
            self._release_post_exit_flat_entry_lane_if_safe("AUTO_CLEANUP")
            pending = getattr(self.order_core, "pending", None) if self.order_core else None
            pending_action = str(getattr(pending, "action", "") or "").upper()
            pending_entry_unfilled = bool(
                pending is not None
                and getattr(pending, "active", False)
                and pending_action.startswith("ENTRY")
                and self._local_entry_unfilled_cancel_eligible()
            )
            runtime_unfilled_active = bool(self.has_server_unfilled or pending_entry_unfilled)
            if runtime_unfilled_active:
                if self.cancel_in_progress:
                    self.auto_cleanup_phase = "AUTO_CANCEL_WAIT"
                else:
                    self.auto_cleanup_phase = "AUTO_UNFILLED_CHECK"
                    self._submit_cancel_cleanup("AUTO_RUNTIME_CANCEL")
                self._refresh_cleanup_statuses()
                return
            if (
                str(self.auto_cleanup_phase or "AUTO_WAIT_SIGNAL") not in ("AUTO_WAIT_SIGNAL", "AUTO_POSITION_WATCH")
                and (not self.server_sync_pending)
                and (not self.takeover_inflight)
                and (not self.unfilled_inflight)
                and (not self.cancel_in_progress)
                and (not self.exit_in_progress)
            ):
                self.auto_cleanup_phase = "AUTO_POSITION_WATCH" if self._has_server_position() else "AUTO_WAIT_SIGNAL"
            self._refresh_cleanup_statuses()
            return
        if self.server_sync_pending or self.takeover_inflight or self.unfilled_inflight:
            self.auto_cleanup_phase = "AUTO_PRECHECK"
            self._refresh_cleanup_statuses()
            return
        if self.has_server_unfilled and int(self._precheck_stale_unfilled_qty or 0) > 0:
            if self.cancel_in_progress:
                self.auto_cleanup_phase = "AUTO_CANCEL_WAIT"
            else:
                self.auto_cleanup_phase = "AUTO_UNFILLED_CHECK"
                self._submit_cancel_cleanup("AUTO_PRECHECK_CANCEL")
            self._refresh_cleanup_statuses()
            return
        precheck_exit_allowed = bool(
            self._auto_precheck_force_exit_existing_position
            or self._precheck_stale_position_detected
            or self._precheck_position_conflict_detected
        )
        if self._has_server_position() and precheck_exit_allowed and (not self._is_precheck_new_position()):
            if self.exit_in_progress or (self.order_core and getattr(self.order_core.pending, "action", "") == "EXIT"):
                self.auto_cleanup_phase = "AUTO_EXIT_WAIT"
            else:
                self.auto_cleanup_phase = "AUTO_POSITION_CHECK"
                self._submit_exit_cleanup("AUTO_PRECHECK_EXIT")
            self._refresh_cleanup_statuses()
            return
        if self._has_server_position() and (self._is_precheck_new_position() or (not precheck_exit_allowed)):
            self.log("SYNC", "PRECHECK_SKIP_EXIT_EXISTING_POSITION", f"side={self.position_side} qty={self.position_qty}")
            self.auto_cleanup_phase = "AUTO_POSITION_WATCH"
            self._auto_precheck_active = False
            self.cancel_in_progress = False
            self.exit_in_progress = False
            self.cancel_needs_server_check = False
            if not self._has_server_position():
                self.exit_needs_server_check = False
            self._precheck_stale_position_detected = False
            self._precheck_position_conflict_detected = False
            self._precheck_takeover_existing_position = False
            self._precheck_stale_unfilled_ids.clear()
            self._precheck_stale_unfilled_qty = 0
            self.log("SYNC", "TAKEOVER_ENTER_EXIT_WATCH", f"phase={self.auto_cleanup_phase} side={self.position_side} qty={self.position_qty}")
            self._refresh_cleanup_statuses()
            return
        if self.has_server_unfilled and int(self._precheck_stale_unfilled_qty or 0) <= 0:
            self.log("SYNC", "PRECHECK_SKIP_NEW_UNFILLED", f"qty={int(self.server_unfilled_qty or 0)}")
        self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
        self._auto_precheck_active = False
        self.cancel_in_progress = False
        self.exit_in_progress = False
        self.cancel_needs_server_check = False
        if not self._has_server_position():
            self.exit_needs_server_check = False
        self._precheck_stale_position_detected = False
        self._precheck_position_conflict_detected = False
        self._precheck_takeover_existing_position = False
        self._precheck_stale_unfilled_ids.clear()
        self._precheck_stale_unfilled_qty = 0
        self.log("SYNC", "PRECHECK_COMPLETE", "AUTO_WAIT_SIGNAL")
        self._refresh_cleanup_statuses()

    # ---------- connection / setup ----------
    def try_login(self, reason: str = "KIS_OAUTH") -> None:
        if self._shutdown_in_progress:
            return
        try:
            self._mark_login_wait_start(reason)
            self.log("ENGINE", "LOGIN", "KIS 모의투자 OAuth 접속")
            self.client.connect_api()
        except Exception as e:
            self._clear_login_wait()
            self.log("ERROR", "LOGIN", f"KIS OAuth 접속 실패: {e}")

    def on_connected(self, err_code: int) -> None:
        self._clear_login_wait()
        self.connected = int(err_code) == 0
        self._last_connect_event_ts = time.time()
        self._reconnect_attempt_inflight = False
        if not self.connected:
            self.log("ENGINE", "LOGIN", f"로그인 실패 err={err_code}")
            self._telegram_queue(
                "kis_connect_failed",
                f"❌ KIS 연결 실패\n오류코드={err_code}\n서버=PAPER\nWATCH DOG={'ON' if self._watchdog_mode else 'OFF'}",
            )
            self.log("TELEGRAM", "QUEUE", f"connect_failed err={err_code}")
            if self._watchdog_mode and int(err_code) == -106:
                self.log("WATCHDOG", "RESTART_ON_CONNECT_FAIL", "err=-106 -> safe shutdown for watchdog restart")
                self.request_safe_shutdown("CONNECT_FAIL_-106_RESTART")
            self.refresh_view()
            return
        self.server_type = self.client.get_server_mode()
        self._server_type_auto_unknown = False
        try:
            self.cfg.server_mode = self.server_type
        except Exception:
            pass
        self._watchdog_server_mode = self.server_type
        self.log("ENGINE", "LOGIN", f"SERVER_TYPE {self.server_type} source=KIS_PAPER_ENDPOINT")
        if self._should_bypass_dialogs():
            self._set_resume_status(f"KIS {self.server_type} 서버 재접속 완료", log_message=True)
        self.account = self._select_login_account(self.server_type)
        if not self.account:
            self.log("ENGINE", "LOGIN_BLOCKED", f"server={self.server_type} reason=NO_ACCOUNT_SELECTED")
            self.refresh_view()
            return
        self._resume_state_saved = False
        self._save_resume_state("ACCOUNT_SELECTED", force=True)
        # Re-apply hard-guard on reconnect as well.
        self.order_qty = 1
        _order_mode = str(getattr(self.cfg, "LIVE_ORDER_EXECUTION_MODE", "LIMIT_L1") or "LIMIT_L1").strip().upper()
        _fixed_loss_exit_order_mode = str(
            getattr(self.cfg, "FIXED_LOSS_EXIT_ORDER_MODE", "MARKET") or "MARKET"
        )
        _mfe_protect_exit_order_mode = str(
            getattr(self.cfg, "MFE_PROTECT_EXIT_ORDER_MODE", "DEFAULT") or "DEFAULT"
        )
        self.order_core = KisOrderCore(
            self.client,
            self.account,
            self.live_code,
            qty_default=1,
            on_event=self.on_execution_event,
            order_mode=_order_mode,
            fixed_loss_exit_order_mode=_fixed_loss_exit_order_mode,
            mfe_protect_exit_order_mode=_mfe_protect_exit_order_mode,
        )
        self.order_core.set_market_quote(self.current_bid, self.current_ask, self.current_price)
        self.log("ENGINE", "ORDER_MODE", f"mode={self.order_core.order_mode} detail={self.order_core.exit_order_mode_text()}")
        # Reconnect guard: block new entries until server sync (takeover/unfilled) completes.
        self._clear_reverse_retry()
        self.flat_confirmed = False
        self.order_lane_locked = True
        self.server_sync_pending = True
        self.log("ENGINE", "LOGIN", f"연결성공 account={self.account or '-'} server={self.server_type}")
        self._telegram_queue(
            "kis_connected",
            f"✅ KIS 연결 완료\n"
            f"서버={self.server_type}\n"
            f"계좌={mask_account(self.account)}\n"
            f"종목={self.live_code}\n"
            f"WATCH DOG={'ON' if self._watchdog_mode else 'OFF'}\n"
            f"AUTO 복원={'ON' if self._watchdog_auto_restore else 'OFF'}",
        )
        self.log("TELEGRAM", "QUEUE", "connect_ok")
        if self._reconnect_last_reason:
            self.log("RESUME", "RECONNECT_SUCCESS", f"reason={self._reconnect_last_reason} auto_restore={int(bool(self.want_auto_after_login))}")
        self.ensure_real_registered()
        if self.want_auto_after_login:
            self.log("RESUME", "AUTO_RESTORE_RECONNECT_START", f"reason={self._reconnect_last_reason or '-'}")
            self._telegram_send_auto_restore_status("RECONNECT_START", reason=self._reconnect_last_reason or "-", extra=f"account={mask_account(self.account)}")
            self.auto_on = True
            self._startup_entry_gate_started_ts = time.time()
            self.want_auto_after_login = False
            self.log("AUTO", "MODE", "AUTO ON")
            # Keep the same precheck flow as manual AUTO ON to avoid stale-state entries.
            self._start_auto_precheck_session()
            self.auto_cleanup_phase = "AUTO_PRECHECK"
            self.cancel_confirmed = False
            self.exit_confirmed = False
            self.cancel_needs_server_check = False
            self.exit_needs_server_check = False
            self.takeover_retry_count += 1
        # warmup must start immediately after login, based on the 2nd package seed-loader structure
        if not self.warmup_started:
            self.warmup_started = True
            # startup sync: takeover + server unfilled 상태를 함께 맞춘 뒤 warmup 진행
            QTimer.singleShot(150, lambda: self._request_server_sync(reason='BOOT', include_takeover=True, include_unfilled=True, start_warmup_after_response=True, force=True))
        else:
            # Reconnect path: always rebuild dz5 continuity from fresh warmup seed.
            QTimer.singleShot(150, lambda: self._request_server_sync(reason='BOOT', include_takeover=True, include_unfilled=True, start_warmup_after_response=True, force=True))
        self.refresh_view()

    def _login_accounts(self) -> list[str]:
        try:
            accounts = list(self.client.get_accounts())
            if getattr(self, "_server_type_auto_unknown", False):
                inferred = "PAPER" if len(accounts) <= 1 else "LIVE"
                if inferred != self.server_type:
                    old_server = self.server_type
                    self.server_type = inferred
                    self._watchdog_server_mode = inferred
                    try:
                        self.cfg.server_mode = inferred
                    except Exception:
                        pass
                    self.log(
                        "ENGINE",
                        "SERVER_TYPE_INFERRED",
                        f"raw=UNKNOWN old={old_server} inferred={inferred} account_count={len(accounts)}",
                    )
            self.log("ENGINE", "ACCOUNT_LIST", f"server={self.server_type or '-'} count={len(accounts)} accounts={','.join(mask_account(a) for a in accounts) if accounts else '-'}")
            return accounts
        except Exception as e:
            self.log("ENGINE", "ACCOUNT_LIST_ERROR", str(e))
            return []

    def _saved_account_for_server(self, server_type: str) -> str:
        server = _normalize_server_mode(server_type, default="LIVE")
        state = self._resume_state if isinstance(self._resume_state, dict) else {}
        by_server = state.get("last_account_by_server") if isinstance(state.get("last_account_by_server"), dict) else {}
        current_session_account = ""
        try:
            if _normalize_server_mode(self.server_type, default="LIVE") == server:
                current_session_account = _normalize_account_no(self.account)
        except Exception:
            current_session_account = ""
        candidates = [
            current_session_account,
            self.runtime_resume.get("account"),
            os.environ.get("ZENITH_ACCOUNT"),
            by_server.get(server),
            state.get("last_live_account") if server == "LIVE" else state.get("last_paper_account"),
            state.get("last_account"),
        ]
        for value in candidates:
            acc = _normalize_account_no(value)
            if acc:
                return acc
        return ""

    def _select_login_account(self, server_type: str) -> str:
        accounts = self._login_accounts()
        if not accounts:
            self.log("ENGINE", "ACCOUNT_BLOCKED", f"server={server_type} reason=ACCNO_EMPTY")
            return ""
        saved = self._saved_account_for_server(server_type)
        if saved and saved in accounts:
            self.log("ENGINE", "ACCOUNT_SELECTED", f"server={server_type} account={mask_account(saved)} source=saved_or_arg")
            return saved
        # A KIS paper adapter exposes only its configured account.  A stale
        # account left by the former broker must never force a selection popup.
        if len(accounts) == 1:
            if saved and saved != accounts[0]:
                self.log(
                    "ENGINE",
                    "ACCOUNT_STALE_IGNORED",
                    f"server={server_type} requested={mask_account(saved)} configured={mask_account(accounts[0])}",
                )
            self.log("ENGINE", "ACCOUNT_SELECTED", f"server={server_type} account={mask_account(accounts[0])} source=single_ACCNO")
            return accounts[0]
        if saved and saved not in accounts:
            self.log("ENGINE", "ACCOUNT_DIALOG_REQUIRED", f"server={server_type} requested={mask_account(saved)} reason=requested_not_in_ACCNO")
        else:
            self.log("ENGINE", "ACCOUNT_DIALOG_REQUIRED", f"server={server_type} reason=multiple_accounts")
        # Account dialog is intentionally allowed even when --no-dialog is set,
        # KIS credentials normally provide one configured paper account.
        selected, ok, size = _select_account_popup(accounts, server_type=server_type, default_account=saved or (accounts[0] if accounts else ""))
        self.log("ENGINE", "ACCOUNT_DIALOG_SIZE", f"width={size[0]} height={size[1]}")
        if ok and selected in accounts:
            self.log("ENGINE", "ACCOUNT_SELECTED", f"server={server_type} account={mask_account(selected)} source=ACCOUNT_DIALOG")
            return selected
        self.log("ENGINE", "ACCOUNT_BLOCKED", f"server={server_type} reason=ACCOUNT_DIALOG_CANCELLED_OR_INVALID")
        return ""

    def ensure_real_registered(self) -> None:
        if not self.connected or self.real_registered:
            return
        try:
            ret = self.client.subscribe_market(self.live_code)
            self.real_registered = (int(ret) == 0)
            self.log("ENGINE", "MARKET", f"KIS 시세 구독 code={self.live_code} ret={ret}")
        except Exception as e:
            self.log("ERROR", "MARKET", f"KIS 시세 구독 실패: {e}")

    # ---------- takeover (KIS 선물옵션 잔고 인계) ----------
    def _request_takeover(self, reason: str = 'BOOT', start_warmup_after_response: bool = True, sync_token: int | None = None) -> bool:
        """KIS REST로 현재 보유 선물 잔고를 조회한다."""
        reason_n = self._normalize_query_reason(reason)
        self.log('TAKEOVER', 'START', f'_request_takeover 진입 reason={reason} connected={self.connected} account={self.account} inflight={self.takeover_inflight}')
        if not self.connected or not self.account:
            self.log('TAKEOVER', 'SKIP', '미연결 또는 계좌없음')
            self._mark_server_sync_part_done("takeover", sync_token)
            if start_warmup_after_response:
                QTimer.singleShot(0, self.start_warmup)
            return False
        if self.takeover_inflight:
            self.log('TAKEOVER', 'SKIP', f'이미 조회 진행중 reason={reason}')
            self._mark_server_sync_part_done("takeover", sync_token)
            return False
        can_start, reason_n = self._try_begin_tr_query("KIS_BALANCE", reason_n)
        if not can_start:
            self._mark_server_sync_part_done("takeover", sync_token)
            if self._is_entry_unknown_balance_reason(reason_n) and (self.entry_inflight or self._entry_order_guard_active):
                QTimer.singleShot(2000, self._request_entry_unknown_balance_recheck)
            return False
        try:
            rqname = str(getattr(self.cfg, 'TAKEOVER_RQNAME', 'REQ_TAKEOVER'))
            self.takeover_requested = True
            self.takeover_done = False
            self.takeover_inflight = True
            self.takeover_start_warmup_after_response = bool(start_warmup_after_response)
            ret = self.client.request_balance(request_name=rqname)
            self.log('TAKEOVER', 'REQ', f'KIS 잔고조회 reason={reason_n} account={mask_account(self.account)} ret={ret}')
            if int(ret) != 0:
                self.takeover_inflight = False
                self.takeover_done = True
                self.log('TAKEOVER', 'ERR', f'KIS 잔고조회 실패 ret={ret}')
                self._mark_tr_query_complete("KIS_BALANCE", reason_n, status=f"REQ_FAIL_{ret}")
                self._mark_server_sync_part_done("takeover", sync_token)
                if self._is_entry_unknown_balance_reason(reason_n) and (self.entry_inflight or self._entry_order_guard_active):
                    QTimer.singleShot(2000, self._request_entry_unknown_balance_recheck)
                if start_warmup_after_response:
                    QTimer.singleShot(0, self.start_warmup)
                return False
            timeout_ms = int(getattr(self.cfg, 'TAKEOVER_TIMEOUT_MS', 5000) or 5000)
            QTimer.singleShot(timeout_ms, lambda: self._takeover_timeout(reason_n, start_warmup_after_response, sync_token))
            return True
        except Exception as e:
            import traceback as _tb
            self.takeover_inflight = False
            self.takeover_done = True
            self.log('TAKEOVER', 'ERR', f'요청 예외 reason={reason}: {e} | {_tb.format_exc().splitlines()[-1]}')
            self._mark_tr_query_complete("KIS_BALANCE", reason_n, status="EXCEPTION")
            self._mark_server_sync_part_done("takeover", sync_token)
            if self._is_entry_unknown_balance_reason(reason_n) and (self.entry_inflight or self._entry_order_guard_active):
                QTimer.singleShot(2000, self._request_entry_unknown_balance_recheck)
            if start_warmup_after_response:
                QTimer.singleShot(0, self.start_warmup)
            return False

    def _takeover_timeout(self, reason: str = 'BOOT', start_warmup_after_response: bool = True, sync_token: int | None = None) -> None:
        """KIS 잔고 응답이 timeout이면 boot 단계는 warmup으로 진행한다."""
        if self.takeover_inflight and self._tr_timeout_owns_active_query("KIS_BALANCE", reason):
            self.takeover_inflight = False
            self.takeover_done = True
            self.log('TAKEOVER', 'TIMEOUT', f'KIS 잔고응답 없음 reason={reason}')
            self._mark_tr_query_complete("KIS_BALANCE", reason, status="TIMEOUT")
            self._mark_server_sync_part_done("takeover", sync_token)
            if self._is_entry_unknown_balance_reason(reason) and (self.entry_inflight or self._entry_order_guard_active):
                QTimer.singleShot(2000, self._request_entry_unknown_balance_recheck)
            if start_warmup_after_response:
                QTimer.singleShot(0, self.start_warmup)

    @staticmethod
    def _kis_pick(summary: dict, names: list[str], cast: str = "float") -> tuple[int | float | None, str]:
        for name in names:
            raw = summary.get(name)
            if raw in (None, ""):
                continue
            try:
                text = str(raw).replace(",", "").replace("+", "").strip()
                return (int(float(text)) if cast == "int" else float(text)), name
            except Exception:
                continue
        return None, ""

    @classmethod
    def _kis_display_deposit(cls, summary: dict) -> tuple[int | float | None, str]:
        """Return cash available after futures margin, with safe fallbacks."""
        return cls._kis_pick(
            summary,
            [
                "ord_psbl_cash",
                "pprt_ord_psbl_cash",
                "wdrw_psbl_tot_amt",
                "dnca_tot_amt",
                "tot_dncl_amt",
                "tot_asst_amt",
            ],
            cast="float",
        )

    def _on_account_snapshot_tr(self, payload: dict, sync_token: int | None = None) -> None:
        reason = self._get_active_tr_query_reason("KIS_ACCOUNT") or self.server_sync_reason
        try:
            self.account_snapshot_inflight = False
            summary = dict((payload or {}).get("account_summary") or {})
            # The dashboard's deposit value is intentionally the spendable
            # cash after margin.  tot_dncl_amt is the gross settlement deposit
            # and does not decrease when a futures position reserves margin.
            deposit, deposit_source = self._kis_display_deposit(summary)
            orderable_qty, _ = self._kis_pick(summary, ["ord_psbl_qty", "max_ord_psbl_qty"], cast="int")
            if deposit is not None:
                self.server_deposit = float(deposit or 0.0)
                self.server_deposit_valid = True
                self.server_deposit_source = str(deposit_source or "")
            if orderable_qty is not None:
                self.server_orderable_qty = int(orderable_qty or 0)
                self.server_orderable_qty_valid = True
                self.server_orderable_qty_source = "KIS_ACCOUNT"
            deposit_txt = "-"
            if self.server_deposit_valid and self.server_deposit is not None:
                try:
                    deposit_txt = f"{self.server_deposit:.0f}"
                except Exception:
                    deposit_txt = str(self.server_deposit)
            orderable_txt = "-"
            if self.server_orderable_qty_valid:
                try:
                    orderable_txt = str(int(self.server_orderable_qty or 0))
                except Exception:
                    orderable_txt = str(self.server_orderable_qty)
            self.log("SYNC", "ACCOUNT_RAW", f"source=KIS_REST fields={summary or {}}")
            self.log(
                "SYNC",
                "ACCOUNT_OK",
                f"deposit={deposit_txt} source={self.server_deposit_source or '-'} orderable={orderable_txt}",
            )
            self._mark_tr_query_complete("KIS_ACCOUNT", reason, status="OK")
            self._mark_server_sync_part_done("account", sync_token)
            # Account TR can restore deposit/orderable values without any
            # immediate tick/execution_notice event afterwards. Push a view refresh here
            # so the dashboard reflects restored balances as soon as the TR lands.
            self._schedule_refresh_view(force=True, delay_ms=0)
        except Exception as e:
            self.account_snapshot_inflight = False
            self.log("SYNC", "ACCOUNT_ERR", f"응답 파싱 예외: {e}")
            self._mark_tr_query_complete("KIS_ACCOUNT", reason, status="EXCEPTION")
            self._mark_server_sync_part_done("account", sync_token)

    def _on_orderable_snapshot_tr(self, payload: dict, sync_token: int | None = None) -> None:
        reason = self._get_active_tr_query_reason("KIS_ORDERABLE") or self.server_sync_reason
        try:
            self.orderable_snapshot_inflight = False
            summary = dict((payload or {}).get("orderable_summary") or {})
            orderable_qty, _ = self._kis_pick(summary, ["ord_psbl_qty", "max_ord_psbl_qty"], cast="int")
            closeable_qty, _ = self._kis_pick(summary, ["lqd_psbl_qty", "ord_psbl_qty"], cast="int")
            orderable_amount, _ = self._kis_pick(summary, ["ord_psbl_amt", "ord_psbl_cash"], cast="float")
            if closeable_qty is not None:
                self.server_closeable_qty = int(closeable_qty or 0)
                self.server_closeable_qty_valid = True
            if orderable_amount is not None:
                self.server_orderable_amount = float(orderable_amount or 0.0)
                self.server_orderable_amount_valid = True
            if orderable_qty is not None:
                self.server_orderable_qty = int(orderable_qty or 0)
                self.server_orderable_qty_valid = True
                self.server_orderable_qty_source = "KIS_ORDERABLE"
            elif closeable_qty is not None:
                self.server_orderable_qty = int(closeable_qty or 0)
                self.server_orderable_qty_valid = True
                self.server_orderable_qty_source = "KIS_ORDERABLE_CLOSEABLE"
            self.log("SYNC", "ORDERABLE_RAW", f"source=KIS_REST fields={summary or {}}")
            orderable_txt = "-"
            if self.server_orderable_qty_valid:
                try:
                    orderable_txt = str(int(self.server_orderable_qty or 0))
                except Exception:
                    orderable_txt = str(self.server_orderable_qty)
            closeable_txt = "-"
            if self.server_closeable_qty_valid:
                try:
                    closeable_txt = str(int(self.server_closeable_qty or 0))
                except Exception:
                    closeable_txt = str(self.server_closeable_qty)
            amount_txt = "-"
            if self.server_orderable_amount_valid:
                try:
                    amount_txt = f"{float(self.server_orderable_amount or 0.0):.0f}"
                except Exception:
                    amount_txt = str(self.server_orderable_amount)
            self.log("SYNC", "ORDERABLE_OK", f"orderable={orderable_txt} closeable={closeable_txt} amount={amount_txt} source={self.server_orderable_qty_source or '-'}")
            self._mark_tr_query_complete("KIS_ORDERABLE", reason, status="OK")
            self._mark_server_sync_part_done("orderable", sync_token)
            # Orderable amount/qty is also a pure TR-side restoration path.
            # Refresh immediately so reconnect/resume shows recovered funds
            # even before the next market event arrives.
            self._schedule_refresh_view(force=True, delay_ms=0)
        except Exception as e:
            self.orderable_snapshot_inflight = False
            self.log("SYNC", "ORDERABLE_ERR", f"응답 파싱 예외: {e}")
            self._mark_tr_query_complete("KIS_ORDERABLE", reason, status="EXCEPTION")
            self._mark_server_sync_part_done("orderable", sync_token)

    def _on_takeover_tr(self, payload: dict, sync_token: int | None = None) -> None:
        """KIS 잔고 응답을 파싱해 해당 종목 포지션을 인계한다."""
        try:
            self.takeover_inflight = False
            prior_side_before_sync = str(self.position_side or "FLAT")
            prior_qty_before_sync = int(self.position_qty or 0)
            prior_entry_price_before_sync = float(self.entry_price or 0.0)
            pending_before_sync = getattr(self.order_core, "pending", None) if self.order_core else None
            pending_action_before_sync = str(getattr(pending_before_sync, "action", "") or "").upper()
            entry_confirmation_pending = bool(
                self.entry_inflight
                or self._entry_order_guard_active
                or (getattr(pending_before_sync, "active", False) and pending_action_before_sync.startswith("ENTRY"))
            )
            rows = payload.get('takeover_rows') or []
            debug = payload.get('takeover_debug') or {}
            cnt = len(rows)
            self.takeover_rows_last = cnt
            self.server_orderable_qty = 0
            self.server_orderable_qty_valid = False
            self.server_orderable_qty_source = ""
            self.server_closeable_qty = None
            self.server_closeable_qty_valid = False
            self.server_orderable_amount = None
            self.server_orderable_amount_valid = False
            if debug:
                self.log('TAKEOVER', 'RESP', f"KIS_BALANCE 잔고행수={cnt} record_key={debug.get('record_key') or '-'} candidates={debug.get('record_candidates') or []}")
            else:
                self.log('TAKEOVER', 'RESP', f'KIS_BALANCE 잔고행수={cnt}')

            matched_side: str | None = None
            matched_qty: int = 0
            matched_price: float = 0.0
            target = self.live_code.strip().lstrip('A')

            def _num(v: object) -> float:
                try:
                    return abs(float(str(v or '').replace(',', '').replace('+', '').strip() or '0'))
                except Exception:
                    return 0.0

            def _norm_side_from_row(row: dict) -> tuple[str | None, str, str]:
                # KIS 매도매수 구분은 01=매도, 02=매수이다.
                raw_sellbuy = row.get('매도수구분')
                s_sellbuy = str(raw_sellbuy or '').strip().upper().replace('+', '').replace('-', '')
                if s_sellbuy in ('1', '01', '매도', 'SELL', 'SHORT'):
                    return 'SHORT', '매도수구분', str(raw_sellbuy or '')
                if s_sellbuy in ('2', '02', '매수', 'BUY', 'LONG'):
                    return 'LONG', '매도수구분', str(raw_sellbuy or '')

                for field in ('매매구분', '구분', '주문구분'):
                    raw = row.get(field)
                    s = str(raw or '').strip().upper().replace('+', '').replace('-', '')
                    if not s:
                        continue
                    if any(x in s for x in ('매수', 'BUY', 'LONG')):
                        return 'LONG', field, str(raw or '')
                    if any(x in s for x in ('매도', 'SELL', 'SHORT')):
                        return 'SHORT', field, str(raw or '')
                    # numeric fallback: 일부 응답이 숫자 1/2만 줄 경우에만 마지막 보조 해석으로 사용
                    # KIS numeric fallback: 01=매도(SHORT), 02=매수(LONG)
                    if s in ('1', '01', '2', '02'):
                        return ('SHORT' if s in ('1', '01') else 'LONG'), field + '_numeric', str(raw or '')
                return None, '', ''

            for i, row in enumerate(rows):
                full_code = str(row.get('종목코드') or '').strip()
                code_norm = full_code.lstrip('A').strip()
                raw_gb = row.get('매매구분')
                raw_sellbuy = row.get('매도수구분')
                raw_qty = row.get('잔고수량')
                raw_closeable_qty = row.get('청산가능수량')
                raw_orderable_qty = row.get('주문가능수량')
                raw_price = row.get('평균단가')
                raw_debug_nonempty_fields = row.get('_debug_nonempty_fields') or {}
                raw_debug_qty_fields = row.get('_debug_qty_fields') or {}
                side, side_field, side_raw = _norm_side_from_row(row)
                qty = int(_num(raw_qty))
                if qty <= 0:
                    qty = int(_num(raw_closeable_qty))
                if qty <= 0:
                    qty = int(_num(raw_orderable_qty))
                price = _num(raw_price)

                self.log('TAKEOVER', 'ROW', f'[{i}] code={full_code or "-"} sellbuy_raw={raw_sellbuy or "-"} gb_raw={raw_gb or "-"} qty_raw={raw_qty or "-"} closeable_raw={raw_closeable_qty or "-"} orderable_raw={raw_orderable_qty or "-"} price_raw={raw_price or "-"}')

                if not code_norm or code_norm != target:
                    continue
                if raw_debug_nonempty_fields:
                    self.log('TAKEOVER', 'RAW_NONEMPTY', f'[{i}] target row nonempty={raw_debug_nonempty_fields}')
                if raw_debug_qty_fields:
                    self.log('TAKEOVER', 'RAW_QTY', f'[{i}] target row qty_fields={raw_debug_qty_fields}')
                try:
                    self.server_orderable_qty = int(_num(raw_orderable_qty))
                    self.server_orderable_qty_valid = (raw_orderable_qty not in (None, ""))
                    if self.server_orderable_qty_valid:
                        self.server_orderable_qty_source = "KIS_BALANCE"
                except Exception:
                    self.server_orderable_qty = 0
                    self.server_orderable_qty_valid = False
                if qty <= 0:
                    self.log('TAKEOVER', 'WARN', f'[{i}] target match but qty<=0 → skip')
                    continue
                if side is None:
                    self.log('TAKEOVER', 'WARN', f'[{i}] target match but side 미인식: sellbuy={raw_sellbuy} gb={raw_gb}')
                    continue

                matched_side = side
                matched_qty = qty
                matched_price = price
                self.log('TAKEOVER', 'MATCH', f'code={full_code} side={side} qty={qty} avg={price:.2f} side_field={side_field} side_raw={side_raw}')
                break

            if matched_side and matched_qty > 0:
                prev_side = str(self.position_side or "FLAT")
                prev_qty = int(self.position_qty or 0)
                sync_reason_u = str(self.server_sync_reason or "").strip().upper()
                if self._should_block_takeover_local_override(sync_reason_u, matched_side, matched_qty):
                    self.log("TAKEOVER", "LOCAL_OVERRIDE_BLOCKED", f"reason={sync_reason_u or '-'} local={prev_side}/{prev_qty} server={matched_side}/{matched_qty} avg={matched_price:.2f}")
                    if self._auto_precheck_active and (not self._is_precheck_new_position()):
                        self._precheck_takeover_existing_position = True
                        self._precheck_stale_position_detected = False
                        self._precheck_position_conflict_detected = (
                            prev_side in ("LONG", "SHORT")
                            and prev_qty > 0
                            and (prev_side != matched_side or prev_qty != matched_qty)
                        )
                        if self._precheck_position_conflict_detected:
                            self.log("SYNC", "PRECHECK_TARGET_STALE_POSITION", f"conflict side={matched_side} qty={matched_qty} prev={prev_side}/{prev_qty}")
                    self.takeover_position_confirmed = self._has_server_position()
                else:
                    prev_entry_time = None
                    try:
                        if prev_side == matched_side and prev_qty == matched_qty and isinstance(self.position_entry_time, datetime):
                            prev_entry_time = self.position_entry_time
                    except Exception:
                        prev_entry_time = None
                    self.position_side = matched_side
                    self.position_qty = matched_qty
                    self.entry_price = matched_price
                    self.position_entry_time = prev_entry_time
                    self.position_entry_z5 = None
                    if entry_confirmation_pending:
                        _balance_entry_metrics = dict(getattr(self, "_last_entry_submit_metrics", {}) or {})
                        try:
                            _metric_side = str(getattr(self, "_last_entry_submit_side", "") or "").upper()
                            if _metric_side and _metric_side != matched_side:
                                _balance_entry_metrics = {}
                        except Exception:
                            _balance_entry_metrics = {}
                        self._position_entry_audit = dict(_balance_entry_metrics)
                        self._position_entry_order_audit = dict(getattr(self, "_last_entry_order_audit", {}) or {})
                        self.position_entry_regime = self._resolve_entry_regime(
                            _balance_entry_metrics or self.last_sig_state,
                            default="",
                        )
                        self.position_mfe_profile = self._mfe_profile_from_entry_context(
                            _balance_entry_metrics or self.last_sig_state
                        )
                        if self.position_entry_time is None:
                            self.position_entry_time = datetime.now()
                    if self.position_entry_time is None:
                        restored_entry_time = self._load_entry_time_from_state(matched_side, matched_qty, matched_price)
                        if restored_entry_time is not None:
                            self.position_entry_time = restored_entry_time
                            self.log("TAKEOVER", "ENTRY_TIME_RESTORED", f"source=PERSIST side={matched_side} qty={matched_qty} entry_time={restored_entry_time.strftime('%Y-%m-%d %H:%M:%S')}")
                    if not entry_confirmation_pending:
                        restored_context = self._load_entry_context_from_state(matched_side, matched_qty, matched_price)
                        if restored_context:
                            self.position_entry_regime = str(restored_context.get("entry_regime") or "")
                            self.position_mfe_profile = str(restored_context.get("mfe_profile") or "")
                            self._position_entry_audit = dict(restored_context.get("entry_audit") or {})
                            self._position_entry_order_audit = dict(restored_context.get("entry_order_audit") or {})
                            self.log("TAKEOVER", "ENTRY_CONTEXT_RESTORED", f"regime={self.position_entry_regime or '-'}")
                    # Reconnect/takeover trail seed:
                    # If price already moved favorably before reconnect, seeding peak/trough with a live proxy
                    # prevents MFE retrace from being reset to zero until a new post-reconnect extreme appears.
                    seed_price = float(self.current_price or self.last_bar_close or matched_price or 0.0)
                    if seed_price <= 0.0:
                        seed_price = float(matched_price or 0.0)
                    self.position_peak_price = max(float(matched_price or 0.0), seed_price)
                    self.position_trough_price = min(float(matched_price or 0.0), seed_price)
                    _takeover_arm_pt = float(getattr(self.cfg, "EXIT_MFE_MIN_PT", 1.8) or 1.8)
                    _takeover_mfe_pt = (
                        max(0.0, float(self.position_peak_price) - float(matched_price or 0.0))
                        if matched_side == "LONG"
                        else max(0.0, float(matched_price or 0.0) - float(self.position_trough_price))
                    )
                    self.position_mfe_armed = bool(_takeover_mfe_pt >= _takeover_arm_pt)
                    self.position_mfe_armed_since_ts = time.time() if self.position_mfe_armed else 0.0
                    self.position_mfe_stage_step = 0
                    self._position_mfe_stage_last_bar_key = ""
                    self._position_mfe_stage_last_extreme_price = 0.0
                    self._persist_entry_anchor_state("TAKEOVER_MATCH")
                    self.takeover_position_confirmed = True
                    self._recalc_fixed_exit_index()
                    self.exit_confirmed = False
                    if self._auto_precheck_active:
                        if self._is_precheck_new_position():
                            self._precheck_stale_position_detected = False
                            self._precheck_position_conflict_detected = False
                            self._precheck_takeover_existing_position = False
                            self.log("SYNC", "PRECHECK_SKIP_NEW_POSITION", f"side={matched_side} qty={matched_qty} avg={matched_price:.2f}")
                        else:
                            self._precheck_takeover_existing_position = True
                            self._precheck_stale_position_detected = False
                            self._precheck_position_conflict_detected = (
                                prev_side in ("LONG", "SHORT")
                                and prev_qty > 0
                                and (prev_side != matched_side or prev_qty != matched_qty)
                            )
                            if self._precheck_position_conflict_detected:
                                self.log("SYNC", "PRECHECK_TARGET_STALE_POSITION", f"conflict side={matched_side} qty={matched_qty} prev={prev_side}/{prev_qty}")
                            self.log("SYNC", "TAKEOVER_POSITION_CONFIRMED", f"side={matched_side} qty={matched_qty} avg={matched_price:.2f}")
                    self._telegram_send_position_sync("takeover_position")
                    if prev_side not in ("LONG", "SHORT") and entry_confirmation_pending:
                        clear_monitors = getattr(getattr(self, "client", None), "clear_order_monitors", None)
                        if callable(clear_monitors):
                            clear_monitors()
                        self._append_balance_confirmed_trade(
                            report_type="ENTRY",
                            side=matched_side,
                            qty=matched_qty,
                            fill_price=matched_price,
                            reason="KIS_BALANCE_CONFIRMED",
                            entry_regime=str(self.position_entry_regime or ""),
                        )
                        self._telegram_send_execution_report(
                            report_type="ENTRY",
                            side=matched_side,
                            qty=matched_qty,
                            fill_price=matched_price,
                            reason="KIS_BALANCE_CONFIRMED",
                            confirmation_source="KIS_BALANCE",
                            entry_regime=str(self.position_entry_regime or ""),
                        )
                    entry_unknown_resolved = self._clear_entry_unknown_after_server_position(sync_reason_u)
                    if (not entry_unknown_resolved) and (self._entry_order_guard_active or self.entry_inflight):
                        self._clear_stale_entry_guard("SERVER_POSITION_CONFIRMED")
                    if self.exit_in_progress:
                        self.auto_cleanup_phase = "AUTO_EXIT_WAIT"
                    # EXIT timeout recheck: server confirmed position snapshot is consistent again.
                    # Clear the "needs server check" latch so AUTO EXIT can retry.
                    try:
                        pending_action = str(getattr(getattr(self, "order_core", None), "pending", None).action or "").upper() if self.order_core else ""
                        pending_exit = bool(self.order_core and getattr(self.order_core.pending, "active", False) and pending_action.startswith("EXIT"))
                        if (
                            self._is_exit_sync_recheck_reason(sync_reason_u)
                            and bool(self.exit_needs_server_check)
                            and (not pending_exit)
                            and (not self.exit_inflight)
                            and (not self.position_close_pending)
                        ):
                            self.exit_needs_server_check = False
                            self.log("SYNC", "EXIT_RECHECK_CLEARED", f"side={matched_side} qty={matched_qty} reason={sync_reason_u}")
                            pending_reason = str(self.pending_exit_reason or "").strip().upper()
                            if self.auto_on and self._is_hard_exit_reason(pending_reason):
                                self.log("SYNC", "EXIT_RETRY_AFTER_RECHECK", f"reason={pending_reason} side={matched_side} qty={matched_qty}")
                                QTimer.singleShot(
                                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                                    lambda r=pending_reason: self._submit_exit_cleanup(r),
                                )
                    except Exception:
                        pass
                    if sync_reason_u == "EXIT_FILLED" and bool(getattr(self, "_post_exit_server_sync_required", False)):
                        self.exit_in_progress = False
                        self.exit_inflight = False
                        self.position_close_pending = False
                        self.exit_confirmed = False
                        self.exit_needs_server_check = False
                        self._post_exit_server_sync_required = False
                        self.flat_confirmed = False
                        self.order_lane_locked = False
                        self._refresh_lane_snapshot()
                        self._refresh_cleanup_statuses()
                        self.log("SYNC", "EXIT_FILLED_SERVER_STILL_HOLDING", f"server={matched_side}/{matched_qty} avg={matched_price:.2f} -> position restored / exit can retry")
                        pending_reason = str(self.pending_exit_reason or "").strip().upper()
                        if self.auto_on and self._is_hard_exit_reason(pending_reason):
                            self.log("SYNC", "EXIT_RETRY_AFTER_SERVER_HOLD", f"reason={pending_reason} side={matched_side} qty={matched_qty}")
                            QTimer.singleShot(
                                int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                                lambda r=pending_reason: self._submit_exit_cleanup(r),
                            )
                    self.log('TAKEOVER', 'OK', f'포지션 인계 완료: {matched_side} {matched_qty}계약 @ {matched_price:.2f} peak={self.position_peak_price:.2f} trough={self.position_trough_price:.2f} 예상청산지수={self._fixed_exit_index}')
            else:
                self.takeover_position_confirmed = False
                if self._auto_precheck_active:
                    self._precheck_stale_position_detected = False
                    self._precheck_position_conflict_detected = False
                    self._precheck_takeover_existing_position = False
                sync_src = "SERVER_SYNC_FLAT"
                sync_reason = str(self.server_sync_reason or "").strip().upper()
                self._clear_entry_unknown_after_server_flat(sync_reason)
                if sync_reason == "MANUAL_EXIT_CHECK":
                    sync_src = "MANUAL_EXIT_CHECK"
                elif sync_reason.startswith("AUTO_PRECHECK_EXIT"):
                    sync_src = "AUTO_PRECHECK_CONFIRM"
                has_exit_context = bool(
                    self.exit_in_progress
                    or str(self.pending_exit_reason or "").strip()
                    or sync_reason in {"EXIT_FILLED", "MANUAL_EXIT_CHECK", "MANUAL_EXIT", "AUTO_PRECHECK_EXIT", "AUTO_PRECHECK_EXIT_CHECK"}
                )
                if (
                    prior_side_before_sync in ("LONG", "SHORT")
                    and prior_qty_before_sync > 0
                    and has_exit_context
                ):
                    # A confirmed zero broker balance must release an EXIT that has
                    # no local/server unfilled order.  position_close_pending is the
                    # state being resolved here, so using it as another defer reason
                    # creates a permanent EXIT_PENDING_RECHECK loop after a fill.
                    if self.has_unfilled_orders or self.has_server_unfilled:
                        self._post_exit_server_sync_required = True
                        self.flat_confirmed = False
                        self.order_lane_locked = True
                        self.log(
                            "SYNC",
                            "FLAT_CONFIRM_DEFER_UNFILLED",
                            (
                                f"prior={prior_side_before_sync}/{prior_qty_before_sync} "
                                f"local_unfilled={int(bool(self.has_unfilled_orders))} "
                                f"server_unfilled={int(bool(self.has_server_unfilled))} "
                                f"exit_pending={int(bool(self._exit_pending_active_for_submit_guard()))}"
                            ),
                        )
                    else:
                        clear_monitors = getattr(getattr(self, "client", None), "clear_order_monitors", None)
                        if callable(clear_monitors):
                            clear_monitors()
                        self._confirm_flat_exit_reason(sync_src, prior_side=prior_side_before_sync, prior_qty=prior_qty_before_sync, preferred_reason="")
                        _balance_exit_price = float(self.current_price or self.last_bar_close or prior_entry_price_before_sync or 0.0)
                        _balance_exit_pnl = 0.0
                        if prior_entry_price_before_sync > 0.0 and _balance_exit_price > 0.0:
                            _balance_exit_mult = 1.0 if prior_side_before_sync == "LONG" else -1.0
                            _balance_exit_pnl = round((_balance_exit_price - prior_entry_price_before_sync) * _balance_exit_mult, 2)
                        _balance_exit_reason = str(self.last_exit_reason or sync_reason or "KIS_BALANCE_CONFIRMED")
                        self._append_balance_confirmed_trade(
                            report_type="EXIT",
                            side=prior_side_before_sync,
                            qty=prior_qty_before_sync,
                            fill_price=_balance_exit_price,
                            reason=_balance_exit_reason,
                            pnl_pt=_balance_exit_pnl,
                            entry_price=prior_entry_price_before_sync,
                            entry_regime=str(self.position_entry_regime or ""),
                        )
                        self._telegram_send_execution_report(
                            report_type="EXIT",
                            side=prior_side_before_sync,
                            qty=prior_qty_before_sync,
                            fill_price=_balance_exit_price,
                            reason=_balance_exit_reason,
                            pnl_pt=_balance_exit_pnl,
                            entry_price=prior_entry_price_before_sync,
                            confirmation_source="KIS_BALANCE",
                            entry_regime=str(self.position_entry_regime or ""),
                        )
                        self.exit_in_progress = False
                        self.exit_inflight = False
                        self.position_close_pending = False
                        self._post_exit_server_sync_required = False
                        self._clear_local_position_after_server_flat_confirm("SERVER_SYNC_FLAT")
                        self.flat_confirmed = True
                        self.order_lane_locked = False
                        self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
                        self.log("SYNC", "FLAT_CONFIRMED", "order_lane=REOPEN/SERVER_VERIFIED")
                        self._telegram_send_position_sync("flat_confirmed")
                        try:
                            if self._is_post_exit_overlap_entry_ready(prior_side_before_sync, str(self._post_exit_entry_reason or self.last_exit_reason or "")):
                                self._schedule_post_exit_overlap_entry(None)
                                self.log("SIGNAL", "POST_EXIT_ENTRY_AFTER_SERVER_FLAT", f"prior={prior_side_before_sync} reason={self._post_exit_entry_reason or self.last_exit_reason or '-'}")
                        except Exception as _e:
                            self.log("SIGNAL", "POST_EXIT_ENTRY_AFTER_SERVER_FLAT_ERR", str(_e))
                        self._arm_reverse_after_server_flat_sync(sync_src)
                elif (
                    (not self._has_server_position())
                    and (not self.has_server_unfilled)
                    and (not self.has_unfilled_orders)
                    and (not self._exit_pending_active_for_submit_guard())
                    and (not str(self.pending_exit_reason or "").strip())
                    and (not self._is_entry_state_unknown())
                    and (not self._is_cancel_state_unknown())
                ):
                    # BOOT/POST_WARMUP/AUTO_PRECHECK flat path:
                    # no server position and no pending order state -> reopen entry lane.
                    self._post_exit_server_sync_required = False
                    self.flat_confirmed = True
                    self.order_lane_locked = False
                    self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
                    if self._entry_order_guard_active or self.entry_inflight:
                        self._clear_stale_entry_guard("SERVER_FLAT_CONFIRMED")
                    self.log("SYNC", "FLAT_CONFIRMED", "order_lane=REOPEN/NO_POSITION")
                    self._telegram_send_position_sync("flat_confirmed_no_position")
                    self._arm_reverse_after_server_flat_sync(sync_src)
                if self.exit_in_progress:
                    self.exit_in_progress = False
                    self.exit_confirmed = True
                    self.exit_needs_server_check = False
                    if self.exit_confirmed:
                        self.auto_cleanup_phase = "AUTO_POSITION_CLEARED"
                if cnt <= 0:
                    self.log('TAKEOVER', 'NONE', f'보유 {self.live_code} 서버응답 무행(rows=0) → FLAT 유지')
                else:
                    self.log('TAKEOVER', 'NONE', f'보유 {self.live_code} 서버응답 수량 0 또는 미인식 → FLAT 유지')
                # EXIT timeout recheck: no server position detected, clear latch as well.
                try:
                    sync_reason_u = str(self.server_sync_reason or "").strip().upper()
                    if self._is_exit_sync_recheck_reason(sync_reason_u) and bool(self.exit_needs_server_check):
                        self.exit_needs_server_check = False
                        self.log("SYNC", "EXIT_RECHECK_CLEARED", "no_server_position")
                except Exception:
                    pass

        except Exception as e:
            self.takeover_position_confirmed = False
            self.log('TAKEOVER', 'ERR', f'파싱 예외: {e}')
        finally:
            reason_active = self._get_active_tr_query_reason("KIS_BALANCE")
            if reason_active:
                self._mark_tr_query_complete("KIS_BALANCE", reason_active, status="OK")
            self.takeover_done = True
            self._mark_server_sync_part_done("takeover", sync_token)
            if self.exit_in_progress and self._has_server_position() and str(self.server_sync_reason or "") == "MANUAL_EXIT_CHECK":
                self._submit_exit_cleanup("MANUAL_EXIT")
            self._refresh_cleanup_statuses()
            if self.auto_on or self.exit_in_progress:
                self._advance_auto_cleanup()
            self.refresh_view()
            if self.takeover_start_warmup_after_response:
                self.takeover_start_warmup_after_response = False
                if not self.warmup_loading and not self.warmup_done:
                    QTimer.singleShot(200, self.start_warmup)


    def _recalc_fixed_exit_index(self) -> None:
        """cl1 현재 상태 기준으로 _fixed_exit_index를 재계산 (takeover·재시작용)."""
        try:
            if not bool(getattr(self.cfg, 'USE_EXIT_Z', False)):
                self._fixed_exit_index = None
                return
            import numpy as _np
            lb = int(self.cfg.LOOKBACK)
            if self.position_side not in ('LONG', 'SHORT') or self.position_qty <= 0:
                self._fixed_exit_index = None
                return
            if len(self.st.cl1) < lb:
                # warmup 전이면 warmup 완료 후 on_new_1m_candle에서 자연스럽게 갱신됨
                self._fixed_exit_index = None
                return
            arr = _np.array(self.st.cl1, dtype=float)[-lb:]
            mu, sd = float(arr.mean()), float(arr.std(ddof=0))
            if sd < 1e-6:
                self._fixed_exit_index = None
                return
            side_z = (
                -abs(float(getattr(self.cfg, 'EXIT_Z_LONG', 0.5) or 0.5))
                if self.position_side == 'LONG'
                else abs(float(getattr(self.cfg, 'EXIT_Z_SHORT', -0.5) or -0.5))
            )
            self._fixed_exit_index = mu + side_z * sd
            self._entry_mu = mu
            self._entry_sd = sd
        except Exception:
            self._fixed_exit_index = None

    def start_warmup(self) -> None:
        use_tr = bool(getattr(self.cfg, 'WARMUP_USE_TR', True))
        if not self.connected or self.warmup_loading:
            return
        is_retry = int(self._warmup_retry_count or 0) > 0
        self._warmup_retry_pending = False
        if not use_tr:
            self.warmup_done = True
            self._warmup_retry_count = 0
            self._reset_post_warmup_entry_gate("WARMUP_SKIP_TR")
            self._set_resume_status("Warmup 완료", log_message=True)
            self._prime_signal_snapshot_after_warmup(reason="WARMUP_SKIP_TR")
            self.ensure_real_registered()
            self._maybe_restore_auto_after_warmup()
            self.refresh_view()
            return
        if not is_retry:
            self._warmup_seed_checkpoint = {}
        self.warmup_loading = True
        self.warmup_done = False
        # The paper server has a tight per-second REST limit.  Live quote
        # polling is paused while the finite history seed is downloaded, then
        # restored on every completion/failure path below.
        if self.real_registered:
            self.client.unsubscribe_market()
            self.real_registered = False
            self.log("WARMUP", "QUOTE_PAUSE", "KIS history seed has REST priority")
        self._reset_post_warmup_entry_gate("WARMUP_START")
        self.warmup_pages = 0
        self.warmup_last_prev_next = '0'
        self._warmup_rows = {}
        self.last_market_status = 'CONNECTED / WARMUP'
        target_rows = int(getattr(self.cfg, 'WARMUP_TARGET_ROWS', 2000) or 2000)
        self.last_warmup_text = f'WARM UP 0/{target_rows}'
        self.log('WARMUP', 'START', f"SEED_LOADER start hist={self.history_code} live={self.live_code} target={target_rows}")
        self.log("RESUME", "WARMUP_WAIT", "warmup_loading=1")
        self._set_resume_status("Warmup 진행중", log_message=True)
        QTimer.singleShot(0, self._run_seed_warmup)

    def _schedule_warmup_retry(self, reason: str) -> bool:
        if (
            not self.connected
            or self.warmup_done
            or self.warmup_loading
            or self._warmup_retry_pending
            or bool(getattr(self, "_shutdown_in_progress", False))
        ):
            return False
        next_attempt = int(self._warmup_retry_count or 0) + 1
        max_attempts = max(0, int(getattr(self.cfg, "WARMUP_RETRY_MAX_ATTEMPTS", 8) or 0))
        if next_attempt > max_attempts:
            cached = len(self._warmup_seed_checkpoint)
            self.log(
                "WARMUP", "RETRY_EXHAUSTED",
                f"attempts={self._warmup_retry_count} cached={cached} reason={reason}",
            )
            self.last_warmup_text = f"Warmup failed ({cached} cached rows)"
            self._set_resume_status("Warmup 재시도 중단 - 수동확인", log_message=True)
            self.refresh_view()
            return False
        self._warmup_retry_count = next_attempt
        base_delay = max(1.0, float(getattr(self.cfg, "WARMUP_RETRY_BASE_DELAY_SEC", 5.0) or 5.0))
        max_delay = max(base_delay, float(getattr(self.cfg, "WARMUP_RETRY_MAX_DELAY_SEC", 60.0) or 60.0))
        delay_sec = min(max_delay, base_delay * (2 ** min(self._warmup_retry_count - 1, 4)))
        self._warmup_retry_pending = True
        self.last_warmup_text = f"Warmup retry in {delay_sec}s"
        self.log(
            "WARMUP", "RETRY_SCHEDULED",
            f"attempt={self._warmup_retry_count}/{max_attempts} delay={delay_sec:g}s "
            f"cached={len(self._warmup_seed_checkpoint)} reason={reason}",
        )
        self._set_resume_status(f"Warmup 재시도 대기 {delay_sec:g}초", log_message=True)

        def retry() -> None:
            self._warmup_retry_pending = False
            if (
                self.connected
                and not self.warmup_done
                and not self.warmup_loading
                and not bool(getattr(self, "_shutdown_in_progress", False))
            ):
                self.log("WARMUP", "RETRY", f"attempt={self._warmup_retry_count} reason={reason}")
                self.start_warmup()

        QTimer.singleShot(int(delay_sec * 1000), retry)
        self.refresh_view()
        return True

    def _run_seed_warmup(self) -> None:
        target_rows = int(getattr(self.cfg, 'WARMUP_TARGET_ROWS', 2000) or 2000)
        timeout_sec = float(getattr(self.cfg, 'WARMUP_TIMEOUT_SEC', 10.0) or 10.0)

        def _pump_ui() -> None:
            try:
                app = QApplication.instance()
                if app is not None:
                    app.processEvents()
            except Exception:
                pass

        def _progress(stage: str, loaded: int, valid: int, target: int) -> None:
            self.last_market_status = 'CONNECTED / WARMUP'
            self.last_warmup_text = f"{stage} {valid}/{target}"
            self.log('WARMUP', 'PAGE', f"stage={stage} loaded={loaded} valid={valid} target={target}")
            self.refresh_view()
            _pump_ui()

        def _checkpoint(bars: list[BarData]) -> None:
            self._warmup_seed_checkpoint = {bar.dt: bar for bar in bars if bar.dt is not None}
            self.log(
                "WARMUP", "CHECKPOINT",
                f"saved={len(self._warmup_seed_checkpoint)} target={target_rows}",
            )

        try:
            loader = HistorySeedLoader(
                self.client,
                timeout_sec=timeout_sec,
                logger=lambda m: self.log('WARMUP', 'SEED', m),
            )
            history_bars = loader.load_seed_bars(
                code_tr=self.history_code,
                target_rows=target_rows,
                realtime_code=self.live_code,
                progress_cb=_progress,
                initial_bars=list(self._warmup_seed_checkpoint.values()),
                checkpoint_cb=_checkpoint,
            )
            if history_bars:
                self.last_warmup_text = f"POST 0/{len(history_bars)}"
                self._set_resume_status("Warmup 후처리중", log_message=True)
                self.refresh_view()
                _pump_ui()
                self.st = Strategy(self.cfg)
                self.bar_count_1m = 0
                last_close = 0.0
                last_dt = None
                warmup_raw_candles: list[Candle] = []
                for bar in history_bars:
                    candle = Candle(bar.dt.replace(second=0, microsecond=0), float(bar.open), float(bar.high), float(bar.low), float(bar.close), int(bar.volume or 0))
                    warmup_raw_candles.append(candle)
                self._dump_raw_warmup_bars(warmup_raw_candles, source="SEED_LOADER_RAW")
                warmup_candles, fill_stats = self._densify_warmup_candles(warmup_raw_candles, source="SEED_LOADER")
                warmup_open_price, warmup_open_date = self._resolve_session_open(warmup_candles)
                if warmup_open_date is not None:
                    self.session_open_date = warmup_open_date
                    self.session_open_price = warmup_open_price
                    self.session_open_confirmed = bool(warmup_open_price > 0)
                    session_hi, session_lo, session_hi_time, session_lo_time = self._resolve_session_range(warmup_candles)
                    self.session_high_price = float(session_hi or 0.0)
                    self.session_low_price = float(session_lo or 0.0)
                    self.session_high_time = str(session_hi_time or "-")
                    self.session_low_time = str(session_lo_time or "-")
                for candle in warmup_candles:
                    self.st.update_indicators(candle)
                    self.bar_count_1m += 1
                    last_close = float(candle.c)
                    last_dt = candle.t
                self.st.seed_sma10s_from_warmup(warmup_candles)
                self.last_warmup_text = f"POST calc {len(warmup_candles)}/{len(warmup_candles)}"
                self.refresh_view()
                _pump_ui()
                self._seed_live_cache_from_candles(warmup_candles)
                self._append_csv_row(self._warmup_gap_report_path, [self._session_id, "SEED_LOADER", fill_stats.get("raw_rows", 0), fill_stats.get("used_rows", 0), fill_stats.get("filled_rows", 0), fill_stats.get("gap_segments", 0), fill_stats.get("max_gap_min", 0)])
                self.log('WARMUP', 'FILL', f"SEED_LOADER raw={fill_stats.get('raw_rows', 0)} used={fill_stats.get('used_rows', 0)} filled={fill_stats.get('filled_rows', 0)} gaps={fill_stats.get('gap_segments', 0)} max_gap={fill_stats.get('max_gap_min', 0)}m")
                self.last_warmup_text = "POST save warmup history"
                self.refresh_view()
                _pump_ui()
                _warm_seed = self._dump_warmup_history(
                    warmup_candles,
                    source="SEED_LOADER_FILLED" if fill_stats.get("filled_rows", 0) else "SEED_LOADER",
                    target_rows=target_rows,
                )
                counts = self.st.get_counts()
                lookback = int(self.cfg.LOOKBACK)
                self.warmup_loading = False
                self.warmup_done = counts.get('bars_5m', 0) >= lookback
                if self.warmup_done:
                    self._warmup_retry_count = 0
                    self._warmup_retry_pending = False
                    self._warmup_seed_checkpoint = {}
                self.last_warmup_text = f"1분 {counts.get('bars_1m', 0)}/{lookback} | 5분 {counts.get('bars_5m', 0)}/{lookback} | 30분 {counts.get('bars_30m', 0)}/{lookback}"
                self.last_bar_close = last_close
                self.session_close = last_close
                if last_dt is not None:
                    self.last_bar_time = last_dt.strftime('%H:%M:%S')
                    self.last_regime = self.st.check_regime(last_dt, current_price=float(last_close or 0.0))
                    zmap = self.st.get_realtime_z_scores(last_close or 0.0, last_dt)
                    self.last_z_1m = zmap.get('z1')
                    self.last_z_5m = zmap.get('z5')
                    self.last_z_30m = zmap.get('z30')
                    self.last_z = self.last_z_5m
                try:
                    _seed_prev = (_warm_seed or {}).get("last_z5")
                    if _seed_prev is None:
                        _seed_prev = self.last_z_5m
                    _seed_recent = (_warm_seed or {}).get("recent_z5")
                    self.st.reset_dz5_continuity(
                        "WARMUP_DONE_RESET",
                        bootstrap_bars=0,
                        seed_prev_z5=_seed_prev,
                        seed_prev_z1=self.last_z_1m,
                        seed_recent_z5=_seed_recent,
                    )
                    self.log(
                        "WARMUP",
                        "DZ5_RESET",
                        f"bootstrap=0 seed_prev_z5={self._fmt_num(_seed_prev, 6)} seed_prev_z1={self._fmt_num(self.last_z_1m, 6)} seed_recent_z5={_seed_recent}",
                    )
                except Exception:
                    pass
                self.log('WARMUP', 'DONE', f"seed_rows={len(history_bars)} bars1={counts.get('bars_1m', 0)} bars5={counts.get('bars_5m', 0)} bars30={counts.get('bars_30m', 0)} ready={self.warmup_done}")
                self._telegram_send_warmup_ok(
                    total_rows=len(history_bars),
                    valid_rows=counts.get('bars_1m', 0),
                    target_rows=target_rows,
                    ready=self.warmup_done,
                    code_tr=self.history_code,
                )
                if self.warmup_done:
                    self._set_resume_status("Warmup 완료", log_message=True)
                if self.warmup_done:
                    self._prime_signal_snapshot_after_warmup(reason="SEED_LOADER_DONE")
                    self.ensure_real_registered()
                    self._maybe_restore_auto_after_warmup()
                # takeover 포지션이 있으면 warmup 완료 후 예상 청산지수 재계산
                if self.position_side in ('LONG', 'SHORT') and self._fixed_exit_index is None:
                    self._recalc_fixed_exit_index()
                    if self._fixed_exit_index is not None:
                        self.log('TAKEOVER', 'RECALC', f'warmup 완료 후 예상청산지수 갱신: {self._fixed_exit_index:.2f}')
                if self.warmup_done and (not self.takeover_position_confirmed) and (not self.takeover_warmup_retry_done) and self.connected and bool(self.account):
                    self.takeover_warmup_retry_done = True
                    self.takeover_retry_count += 1
                    self.log('SYNC', 'RETRY', 'seed warmup 완료 후 takeover + unfilled 재조회')
                    QTimer.singleShot(250, lambda: self._request_server_sync(reason='POST_WARMUP', include_takeover=True, include_unfilled=True, start_warmup_after_response=False, force=True))
                self.refresh_view()
                if not self.warmup_done:
                    if not self._schedule_warmup_retry("INSUFFICIENT_ROWS"):
                        self.ensure_real_registered()
                return
            self.log('WARMUP', 'EMPTY', 'KIS minute-bar loader returned 0 rows')
            self.warmup_loading = False
            self.warmup_done = False
            if not self._schedule_warmup_retry("EMPTY"):
                self.ensure_real_registered()
        except Exception as e:
            self.log(
                'ERROR', 'WARMUP',
                f'SEED_LOADER 실패: {e} cached={len(self._warmup_seed_checkpoint)}/{target_rows}',
            )
            self.log('WARMUP', 'FAILED', 'KIS minute-bar warmup stopped; automatic entry remains blocked')
            self.warmup_loading = False
            self.warmup_done = False
            if not self._schedule_warmup_retry(type(e).__name__):
                self.ensure_real_registered()

    def request_warmup_page(self, prev_next: int = 0) -> None:
        # Kept as a UI retry hook. KIS pagination is handled synchronously by
        # HistorySeedLoader and does not expose a broker-specific continuation flag.
        if not self.warmup_loading:
            self.start_warmup()

    def _parse_dt14(self, dt_raw: str) -> datetime | None:
        s = ''.join(ch for ch in str(dt_raw or '') if ch.isdigit())
        if len(s) >= 14:
            s = s[:14]
            try:
                return datetime.strptime(s, '%Y%m%d%H%M%S')
            except Exception:
                return None
        return None

    def _finalize_warmup(self) -> None:
        try:
            self.last_warmup_text = f"POST parse {len(self._warmup_rows)}"
            self._set_resume_status("Warmup 후처리중", log_message=True)
            self.refresh_view()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        except Exception:
            pass
        rows = list(self._warmup_rows.values())
        parsed = []
        for row in rows:
            dt = self._parse_dt14(row.get('dt'))
            if dt is None:
                continue
            parsed.append((dt, row))
        parsed.sort(key=lambda x: x[0])

        self.st = Strategy(self.cfg)
        self.bar_count_1m = 0
        last_close = None
        last_dt = None
        warmup_raw_candles: list[Candle] = []
        for dt, row in parsed:
            c = Candle(dt, float(row.get('open') or row.get('close') or 0.0), float(row.get('high') or row.get('close') or 0.0), float(row.get('low') or row.get('close') or 0.0), float(row.get('close') or 0.0), int(row.get('volume') or 0))
            warmup_raw_candles.append(c)
        self._dump_raw_warmup_bars(warmup_raw_candles, source="KIS_MINUTE_BARS_RAW")
        warmup_candles, fill_stats = self._densify_warmup_candles(warmup_raw_candles, source="KIS_MINUTE_BARS")
        warmup_open_price, warmup_open_date = self._resolve_session_open(warmup_candles)
        if warmup_open_date is not None:
            self.session_open_date = warmup_open_date
            self.session_open_price = warmup_open_price
            self.session_open_confirmed = bool(warmup_open_price > 0)
            session_hi, session_lo, session_hi_time, session_lo_time = self._resolve_session_range(warmup_candles)
            self.session_high_price = float(session_hi or 0.0)
            self.session_low_price = float(session_lo or 0.0)
            self.session_high_time = str(session_hi_time or "-")
            self.session_low_time = str(session_lo_time or "-")
        for c in warmup_candles:
            self.st.update_indicators(c)
            self.bar_count_1m += 1
            last_close = float(c.c)
            last_dt = c.t
        self.st.seed_sma10s_from_warmup(warmup_candles)
        try:
            self.last_warmup_text = f"POST calc {len(warmup_candles)}/{len(warmup_candles)}"
            self.refresh_view()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        except Exception:
            pass
        self._seed_live_cache_from_candles(warmup_candles)
        self._append_csv_row(self._warmup_gap_report_path, [self._session_id, "KIS_MINUTE_BARS", fill_stats.get("raw_rows", 0), fill_stats.get("used_rows", 0), fill_stats.get("filled_rows", 0), fill_stats.get("gap_segments", 0), fill_stats.get("max_gap_min", 0)])
        self.log('WARMUP', 'FILL', f"KIS_MINUTE_BARS raw={fill_stats.get('raw_rows', 0)} used={fill_stats.get('used_rows', 0)} filled={fill_stats.get('filled_rows', 0)} gaps={fill_stats.get('gap_segments', 0)} max_gap={fill_stats.get('max_gap_min', 0)}m")
        try:
            self.last_warmup_text = "POST save warmup history"
            self.refresh_view()
            app = QApplication.instance()
            if app is not None:
                app.processEvents()
        except Exception:
            pass
        _warm_seed = self._dump_warmup_history(
            warmup_candles,
            source="KIS_MINUTE_BARS_FILLED" if fill_stats.get("filled_rows", 0) else "KIS_MINUTE_BARS",
            target_rows=int(getattr(self.cfg, 'WARMUP_TARGET_ROWS', 2000) or 2000),
        )

        counts = self.st.get_counts()
        lookback = int(self.cfg.LOOKBACK)
        self.warmup_loading = False
        self.warmup_done = counts.get('bars_5m', 0) >= lookback
        self._reset_post_warmup_entry_gate("WARMUP_DONE")
        self.last_warmup_text = (
            f"1분 {counts.get('bars_1m', 0)}/{lookback} | "
            f"5분 {counts.get('bars_5m', 0)}/{lookback} | "
            f"30분 {counts.get('bars_30m', 0)}/{lookback}"
        )
        if last_close is not None:
            self.last_bar_close = last_close
            self.session_close = last_close
        if last_dt is not None:
            self.last_bar_time = last_dt.strftime('%H:%M:%S')
            self.last_regime = self.st.check_regime(last_dt, current_price=float(last_close or 0.0))
            zmap = self.st.get_realtime_z_scores(last_close or 0.0, last_dt)
            self.last_z_1m = zmap.get('z1')
            self.last_z_5m = zmap.get('z5')
            self.last_z_30m = zmap.get('z30')
            self.last_z = self.last_z_5m
        try:
            _seed_prev = (_warm_seed or {}).get("last_z5")
            if _seed_prev is None:
                _seed_prev = self.last_z_5m
            _seed_recent = (_warm_seed or {}).get("recent_z5")
            self.st.reset_dz5_continuity(
                "WARMUP_DONE_RESET",
                bootstrap_bars=0,
                seed_prev_z5=_seed_prev,
                seed_prev_z1=self.last_z_1m,
                seed_recent_z5=_seed_recent,
            )
            self.log(
                "WARMUP",
                "DZ5_RESET",
                f"bootstrap=0 seed_prev_z5={self._fmt_num(_seed_prev, 6)} seed_prev_z1={self._fmt_num(self.last_z_1m, 6)} seed_recent_z5={_seed_recent}",
            )
        except Exception:
            pass

        self.log('WARMUP', 'DONE', f"rows={len(parsed)} bars1={counts.get('bars_1m', 0)} bars5={counts.get('bars_5m', 0)} bars30={counts.get('bars_30m', 0)} ready={self.warmup_done}")
        self._telegram_send_warmup_ok(
            total_rows=len(parsed),
            valid_rows=counts.get('bars_1m', 0),
            target_rows=int(getattr(self.cfg, 'WARMUP_TARGET_ROWS', 2000) or 2000),
            ready=self.warmup_done,
            code_tr=self.history_code,
        )
        if self.warmup_done:
            self._set_resume_status("Warmup 완료", log_message=True)
        self._prime_signal_snapshot_after_warmup(reason="KIS_MINUTE_BARS_DONE")
        self.ensure_real_registered()
        self._maybe_restore_auto_after_warmup()
        # takeover 포지션이 있으면 warmup 완료 후 예상 청산지수 재계산
        if self.position_side in ('LONG', 'SHORT') and self._fixed_exit_index is None:
            self._recalc_fixed_exit_index()
            if self._fixed_exit_index is not None:
                self.log('TAKEOVER', 'RECALC', f'warmup 완료 후 예상청산지수 갱신: {self._fixed_exit_index:.2f}')
        if (not self.takeover_position_confirmed) and (not self.takeover_warmup_retry_done) and self.connected and bool(self.account):
            self.takeover_warmup_retry_done = True
            self.takeover_retry_count += 1
            self.log('SYNC', 'RETRY', 'warmup 완료 후 takeover + unfilled 재조회')
            QTimer.singleShot(250, lambda: self._request_server_sync(reason='POST_WARMUP', include_takeover=True, include_unfilled=True, start_warmup_after_response=False, force=True))
        self.refresh_view()

    # ---------- dashboard button handlers ----------
    def on_auto_clicked(self) -> None:
        try:
            self.log("AUTO", "CLICK", f"button received connected={self.connected} auto_on={self.auto_on} pos={self.position_side}/{self.position_qty} guard={self._entry_order_guard_active}")
            if not self.connected:
                self.want_auto_after_login = True
                self.log("AUTO", "MODE", "not connected - schedule AUTO ON after login")
                self.try_login()
                self.refresh_view(force=True)
                return
            self.auto_on = not self.auto_on
            self.log("AUTO", "MODE", f"AUTO {'ON' if self.auto_on else 'OFF'}")
            # Render the already-live ARM/FIRE snapshot immediately on mode
            # transition; do not wait for the next tick or server-sync callback.
            self.refresh_view(force=True)
            if self.auto_on:
                self._startup_entry_gate_started_ts = time.time()
                self._start_auto_precheck_session()
                self.auto_cleanup_phase = "AUTO_PRECHECK"
                self.cancel_confirmed = False
                self.exit_confirmed = False
                self.cancel_needs_server_check = False
                self.exit_needs_server_check = False
                self.takeover_retry_count += 1
                self.log('SYNC', 'AUTO_RETRY', 'AUTO ON - request takeover + unfilled cleanup sync')
                QTimer.singleShot(50, lambda: self._request_server_sync(reason='AUTO_PRECHECK', include_takeover=True, include_unfilled=True, start_warmup_after_response=False, force=True))
            else:
                self._auto_precheck_active = False
                self._precheck_stale_position_detected = False
                self._precheck_position_conflict_detected = False
                self._precheck_takeover_existing_position = False
                self._precheck_stale_unfilled_ids.clear()
                self._precheck_stale_unfilled_qty = 0
                self.auto_cleanup_phase = "AUTO_WAIT_SIGNAL"
            self._refresh_cleanup_statuses()
            self.refresh_view(force=True)
        except Exception as e:
            self.log("ERROR", "AUTO_BUTTON", f"AUTO button handler failed: {e}")
            self.log("ERROR", "TRACE", traceback.format_exc().splitlines()[-1])
            try:
                self.refresh_view(force=True)
            except Exception:
                pass

    def on_cancel_clicked(self) -> None:
        self.log("CONTROL", "CANCEL_CLICK", f"connected={self.connected} account={bool(self.account)} known_unfilled={self.has_server_unfilled} qty={self.server_unfilled_qty}")
        ok = self._submit_cancel_cleanup("MANUAL_CANCEL")
        if ok:
            self.log("CONTROL", "CANCEL", "manual cancel cleanup requested")
        else:
            self.log("CONTROL", "CANCEL", "manual cancel cleanup blocked or server check required")
        self.refresh_view(force=True)

    def on_exit_clicked(self) -> None:
        self.log("CONTROL", "EXIT_CLICK", f"connected={self.connected} account={bool(self.account)} pos={self.position_side}/{self.position_qty}")
        if self.connected and self.account and not self._has_server_position():
            self.pending_exit_reason = "MANUAL_EXIT"
            self.exit_in_progress = True
            self.exit_confirmed = False
            self.exit_needs_server_check = False
            self.auto_cleanup_phase = "AUTO_POSITION_CHECK" if self.auto_on else self.auto_cleanup_phase
            self._refresh_cleanup_statuses()
            self._request_server_sync(reason='MANUAL_EXIT_CHECK', include_takeover=True, include_unfilled=False, start_warmup_after_response=False, force=True)
            self.refresh_view(force=True)
            return
        ok = self._submit_exit_cleanup("MANUAL_EXIT")
        if ok:
            self.log("CONTROL", "EXIT", "manual exit cleanup requested")
        else:
            self.log("CONTROL", "EXIT", "manual exit cleanup blocked or server check required")
        self.refresh_view(force=True)

    def on_dashboard_close_requested(self) -> None:
        self.request_safe_shutdown("MANUAL_CLOSE")

    def _can_order(self, manual: bool = False) -> bool:
        if not self.connected:
            self.log("WARN", "ORDER", "연결 전입니다")
            return False
        if not self.account:
            self.log("WARN", "ORDER", "계좌를 찾지 못했습니다")
            return False
        if not self.order_core:
            self.log("WARN", "ORDER", "주문코어 미초기화")
            return False
        allow_trade, block_reason = self._entry_lane_state(self.last_sig_state)
        if not allow_trade and not manual:
            self.log("WARN", "ORDER", f"entry lane blocked reason={block_reason}")
            return False
        if not allow_trade and manual and (
            block_reason == "SERVER_SYNC_PENDING"
            or block_reason == "UNFILLED_EXISTS"
            or block_reason == "ENTRY_INFLIGHT"
            or block_reason == "ORDER_LANE_LOCKED"
        ):
            self.log("WARN", "ORDER", f"manual order blocked reason={block_reason}")
            return False
        return True

    # ---------- events from KIS ----------
    def on_msg(self, screen: str, rqname: str, trcode: str, msg: str) -> None:
        self._last_server_msg_ts = time.time()
        self.log("SERVER_MSG", rqname or trcode or "MSG", f"screen={screen} {msg}")
        if self.order_core:
            self.order_core.handle_msg(screen, rqname, trcode, msg)
        self.refresh_view()

    def on_execution_notice(self, payload: dict) -> None:
        self._last_execution_notice_ts = time.time()
        readable = payload.get("readable", {}) if isinstance(payload, dict) else {}
        self.log("KIS_EXECUTION", readable.get("주문번호", "-"), str(readable))
        try:
            # Balance execution_notice can arrive before/without TR sync. If server says qty=0, force local flat sync.
            live_norm = str(self.live_code or "").strip().lstrip("A")
            row_code = str((readable or {}).get("종목코드") or "").strip()
            row_norm = row_code.lstrip("A")
            raw_balance_qty = (readable or {}).get("보유수량")
            raw_orderable_qty = (readable or {}).get("주문가능수량")
            if live_norm and row_norm and row_norm == live_norm and raw_orderable_qty not in (None, ""):
                try:
                    self.server_orderable_qty = int(self._to_abs_int(raw_orderable_qty))
                    self.server_orderable_qty_valid = True
                    self.server_orderable_qty_source = "KIS_EXECUTION_933"
                    self.log("SYNC", "KIS_EXECUTION_933", f"orderable_qty={self.server_orderable_qty} code={row_code or '-'}")
                    self.refresh_view(force=True)
                except Exception:
                    pass
            if live_norm and row_norm and row_norm == live_norm and raw_balance_qty not in (None, ""):
                bal_qty = int(self._to_abs_int(raw_balance_qty))
                _flat_guard_sec = float(getattr(self.cfg, "KIS_EXECUTION_BALANCE_FLAT_GUARD_SEC", 2.0) or 0.0)
                _entry_recent = (time.time() - float(self._last_entry_filled_ts or 0.0)) <= max(0.0, _flat_guard_sec)
                _entry_pending = False
                try:
                    if self.order_core:
                        _p = getattr(self.order_core, "pending", None)
                        _entry_pending = bool(
                            _p is not None
                            and bool(getattr(_p, "active", False))
                            and str(getattr(_p, "action", "") or "").upper() == "ENTRY"
                        )
                except Exception:
                    _entry_pending = False
                _recovery_guard_window = max(float(getattr(self, "_entry_submit_cooldown_sec", 3.0) or 3.0), 3.0)
                _balance_recovery_recent = (
                    self._last_balance_recovery_entry_ts > 0
                    and (time.time() - float(self._last_balance_recovery_entry_ts or 0.0)) <= _recovery_guard_window
                )
                _recovery_side_matches = str(self._last_balance_recovery_side or "").upper() == str(self.position_side or "").upper()
                _allow_recovery_flat_sync = _balance_recovery_recent and _recovery_side_matches and (not _entry_pending)
                if bal_qty <= 0 and self._has_server_position() and ((not _entry_recent) or _allow_recovery_flat_sync) and (not _entry_pending):
                    prior_side = str(self.position_side or "FLAT")
                    prior_qty = int(self.position_qty or 0)
                    self._confirm_flat_exit_reason(
                        "KIS_EXECUTION_BALANCE_FLAT",
                        prior_side=prior_side,
                        prior_qty=prior_qty,
                        preferred_reason=str(self.pending_exit_reason or self.last_exit_reason or "").strip(),
                    )
                    self.position_side = "FLAT"
                    self.position_qty = 0
                    self.active_position_side = "FLAT"
                    self.active_position_qty = 0
                    self.entry_price = 0.0
                    self.position_entry_time = None
                    self.position_entry_z5 = None
                    self.position_entry_prev_z5 = None
                    self.position_entry_dz5 = None
                    self.position_entry_dz5_threshold = None
                    self.position_entry_dz5_direction_ok = None
                    self.position_entry_macd_osci = None
                    self.position_peak_price = 0.0
                    self.position_trough_price = 0.0
                    self.position_mfe_armed = False
                    self.position_mfe_armed_since_ts = 0.0
                    self.position_mfe_stage_step = 0
                    self._position_mfe_stage_last_bar_key = ""
                    self._position_mfe_stage_last_extreme_price = 0.0
                    self._entry_mu = None
                    self._entry_sd = None
                    self._fixed_exit_index = None
                    self._persist_entry_anchor_state("KIS_EXECUTION_BALANCE_FLAT")
                    self.exit_in_progress = False
                    self.exit_inflight = False
                    self.position_close_pending = False
                    self.exit_confirmed = True
                    self.exit_needs_server_check = False
                    self._post_exit_server_sync_required = True
                    self.flat_confirmed = False
                    self.order_lane_locked = True
                    self._last_balance_recovery_entry_ts = 0.0
                    self._last_balance_recovery_side = ""
                    self.log("SYNC", "KIS_EXECUTION_FLAT_SYNC", f"side={prior_side} qty={prior_qty} -> FLAT / wait=POST_EXIT_SYNC")
                    QTimer.singleShot(
                        int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                        lambda: self._request_server_sync(
                            reason="KIS_EXECUTION_BALANCE_FLAT",
                            include_takeover=True,
                            include_unfilled=True,
                            include_account=True,
                            include_orderable=True,
                            start_warmup_after_response=False,
                            force=True,
                        ),
                    )
                elif bal_qty <= 0 and self._has_server_position() and (_entry_recent or _entry_pending):
                    why = "ENTRY_RECENT" if _entry_recent else "ENTRY_PENDING"
                    self.log("SYNC", "KIS_EXECUTION_FLAT_SKIP", f"guard={why} side={self.position_side} qty={self.position_qty}")
        except Exception:
            pass
        if self.order_core:
            pending_before = getattr(self.order_core, "pending", None)
            pending_before_active = bool(
                pending_before is not None
                and bool(getattr(pending_before, "active", False))
                and str(getattr(pending_before, "action", "") or "").upper() == "ENTRY"
            )
            pending_before_side = str(getattr(pending_before, "side", "") or "").upper() if pending_before is not None else ""
            pending_before_code = str(getattr(pending_before, "code", "") or "").strip() if pending_before is not None else ""
            pending_before_order_no = str(getattr(pending_before, "order_no", "") or "").strip() if pending_before is not None else ""
            self.order_core.handle_execution_notice(payload)
            try:
                live_norm = str(self.live_code or '').strip().lstrip('A')
                row_code = str((readable or {}).get('종목코드') or '').strip()
                row_norm = row_code.lstrip('A')
                raw_balance_qty = (readable or {}).get('보유수량')
                if live_norm and row_norm and row_norm == live_norm and raw_balance_qty not in (None, ''):
                    bal_qty = int(self._to_abs_int(raw_balance_qty))
                    pending = getattr(self.order_core, 'pending', None)
                    pending_side = str(getattr(pending, 'side', '') or '').upper() if pending is not None else ""
                    recovered = self._recover_entry_fill_from_balance_execution_notice(
                        payload=payload,
                        readable=readable,
                        bal_qty=bal_qty,
                        pending_side=(pending_side or pending_before_side),
                        pending_code=(str(getattr(pending, 'code', '') or '').strip() if pending is not None else pending_before_code),
                        pending_order_no=(str(getattr(pending, 'order_no', '') or '').strip() if pending is not None else pending_before_order_no),
                    )
                    if recovered and pending_before_active:
                        self.log('SYNC', 'ENTRY_FILL_RECOVERY_CONTEXT', f'prior_pending_side={pending_before_side or "-"} qty={bal_qty}')
            except Exception:
                pass
            self.auto_cancel_phase = str(getattr(self.order_core.pending, "auto_cancel_phase", "") or self.auto_cancel_phase or "-")
            remaining = int(getattr(self.order_core.pending, "remaining_qty", 0) or 0)
            cancel_req = bool(getattr(self.order_core.pending, "cancel_requested", False))
            if remaining > 0 or cancel_req:
                self._mark_unfilled_detected()
                self.server_sync_pending = True
            elif self.has_unfilled_orders and (not self.has_server_unfilled):
                self.has_unfilled_orders = False
                self.log("SYNC", "UNFILLED_CLEARED", "entry_block=OFF")
        self._refresh_lane_snapshot()
        self.refresh_view()

    def on_kis_order_activity(self, payload: dict) -> None:
        """Refresh paper HTS/API order state immediately, independently of AUTO."""

        source = str((payload or {}).get("source") or "KIS_WS_ORDER_ACTIVITY")
        self.log("SYNC", "KIS_WS_ORDER_ACTIVITY", f"source={source} auto={int(bool(self.auto_on))}")
        if getattr(self, "_kis_ws_order_sync_pending", False):
            return
        self._kis_ws_order_sync_pending = True

        def run_sync() -> None:
            if not self.connected or not self.account:
                self._kis_ws_order_sync_pending = False
                return
            if self.server_sync_pending or self.unfilled_inflight or self.takeover_inflight:
                QTimer.singleShot(300, run_sync)
                return
            self._kis_ws_order_sync_pending = False
            self._request_server_sync(
                reason="KIS_WS_ORDER_ACTIVITY",
                include_takeover=True,
                include_unfilled=True,
                include_account=False,
                include_orderable=False,
                start_warmup_after_response=False,
                force=True,
            )

        QTimer.singleShot(100, run_sync)

    def on_response(self, payload: dict) -> None:
        rqname = str((payload or {}).get("request_name") or "KIS_RESPONSE")
        trcode = str((payload or {}).get("response_type") or "")
        prev_next = str((payload or {}).get('prev_next') or '0').strip()
        rows = (payload or {}).get('rows') or []

        # ===== KIS REST normalized responses =====
        takeover_rq = str(getattr(self.cfg, 'TAKEOVER_RQNAME', 'REQ_TAKEOVER'))
        if rqname == takeover_rq and trcode.upper() == 'KIS_BALANCE':
            self._on_takeover_tr(payload)
            return
        unfilled_rq = str(getattr(self.cfg, 'UNFILLED_SYNC_RQNAME', 'REQ_UNFILLED_SYNC'))
        if rqname == unfilled_rq and trcode.upper() == 'KIS_OPEN_ORDERS':
            self._on_unfilled_tr(payload)
            return
        account_rq = str(getattr(self.cfg, 'ACCOUNT_SNAPSHOT_RQNAME', 'REQ_ACCOUNT_SNAPSHOT'))
        if rqname == account_rq and trcode.upper() == 'KIS_ACCOUNT':
            self._on_account_snapshot_tr(payload)
            return
        orderable_rq = str(getattr(self.cfg, 'ORDERABLE_SNAPSHOT_RQNAME', 'REQ_ORDERABLE_SNAPSHOT'))
        if rqname == orderable_rq and trcode.upper() == 'KIS_ORDERABLE':
            self._on_orderable_snapshot_tr(payload)
            return

        self.log("KIS", rqname, f"response_type={trcode}")

    def on_tick_received(self, payload: dict) -> None:
        try:
            price = float(payload.get("price") or 0.0)
            if price <= 0:
                return
            tick_time_raw = str(payload.get("tick_time_raw") or "")
            tick_dt = self._tick_dt(tick_time_raw)
            mkey = tick_dt.replace(second=0, microsecond=0)
            if self.bar_1m is not None and mkey < self.bar_1m["t"]:
                self._diag_stale_tick_skip_count += 1
                self._diag_last_1m_minute_key = self.bar_1m["t"]
                if hasattr(self, "_refresh_live_z5_diag_state"):
                    try:
                        exp_1m, exp_5m, exp_30m = self._expected_live_bucket_counts()
                        self._refresh_live_z5_diag_state(exp_1m, exp_5m, exp_30m)
                    except Exception:
                        pass
                self._log_stale_tick_summary(mkey, self.bar_1m["t"])
                self._log_live_dedupe_diag(force=False)
                self._schedule_refresh_view(delay_ms=50)
                return
            self.current_price = price
            self.current_bid = abs(float(payload.get("bid") or 0.0))
            self.current_ask = abs(float(payload.get("ask") or 0.0))
            if self.order_core is not None:
                self.order_core.set_market_quote(self.current_bid, self.current_ask, self.current_price)
                self._invalidate_pending_reversal_outside_fire_band()
            self.last_tick_time = self._fmt_hms(tick_time_raw)
            self.last_tick_ts = time.time()
            self._apply_session_boundary(tick_dt, price, confirm_open=False)
            if self.session_high_price <= 0 or price > self.session_high_price:
                self.session_high_price = price
                self.session_high_time = self.last_tick_time
            if self.session_low_price <= 0 or price < self.session_low_price:
                self.session_low_price = price
                self.session_low_time = self.last_tick_time

            tick_volume = int(float(payload.get("volume") or 0.0))
            if self.bar_1m is None:
                self.bar_1m = {"t": mkey, "o": price, "h": price, "l": price, "c": price, "v": tick_volume}
            else:
                if self.bar_1m["t"] != mkey:
                    prev = self.bar_1m
                    self.on_new_1m_candle(Candle(prev["t"], prev["o"], prev["h"], prev["l"], prev["c"], prev["v"]), allow_trade=True, eval_time=tick_dt)
                    self.bar_1m = {"t": mkey, "o": price, "h": price, "l": price, "c": price, "v": tick_volume}
                else:
                    self.bar_1m["h"] = max(self.bar_1m["h"], price)
                    self.bar_1m["l"] = min(self.bar_1m["l"], price)
                    self.bar_1m["c"] = price
                    self.bar_1m["v"] = int(self.bar_1m.get("v") or 0) + tick_volume
            cur = self.bar_1m
            self.on_new_1m_candle(Candle(cur["t"], cur["o"], cur["h"], cur["l"], cur["c"], cur["v"]), allow_trade=False, eval_time=tick_dt)
            self.last_market_status = "CONNECTED / TICK"
            self._schedule_refresh_view(delay_ms=50)
        except Exception as e:
            self.log("ERROR", "TICK", f"실시간 처리 실패: {e}")
            self.log("ERROR", "TRACE", traceback.format_exc())

    # ---------- execution events ----------
    def on_execution_event(self, event: ExecutionEvent) -> None:
        msg = f"type={event.event_type} side={event.side} qty={event.qty} price={event.price:.2f} msg={event.message}"
        self.log("EXEC", event.event_type, msg)

        if event.event_type in ("ENTRY_FILLED", "ENTRY_PARTIAL"):
            self._clear_forced_reverse_pending()
            self._last_entry_filled_ts = time.time()
            self._post_warmup_gate_bypassed_after_trade = True
            self._last_exit_submit_context = {}
            self._entry_order_guard_active = False
            self._entry_order_guard_side = ""
            self._entry_order_guard_ts = 0.0
            self.entry_inflight = False
            self.pending_entry_side = ""
            self.order_lane_locked = False
            self.exit_inflight = False
            self.position_close_pending = False
            self.pending_exit_reason = ""
            self._pending_exit_position_side = "FLAT"
            self._pending_exit_position_qty = 0
            if self._auto_precheck_active:
                self._precheck_entry_filled_session_id = int(self._auto_precheck_session_id or 0)
                self._precheck_stale_position_detected = False
                self.log("SYNC", "PRECHECK_SKIP_NEW_POSITION", f"entry_filled side={event.side} qty={event.qty}")
            self.server_sync_pending = False
            self.server_sync_reason = "ENTRY_FILLED"
            if event.event_type == "ENTRY_FILLED":
                self._reset_server_unfilled_state(reason="ENTRY_FILLED")
                clear_monitors = getattr(getattr(self, "client", None), "clear_order_monitors", None)
                if callable(clear_monitors):
                    clear_monitors()
            self.position_side = str(event.side or self.position_side or "FLAT").upper()
            self.position_qty = max(int(event.qty or self.position_qty or 0), 1)
            if str(event.message or "").strip().upper() == "BALANCE_KIS_EXECUTION_RECOVERY":
                self._last_balance_recovery_entry_ts = time.time()
                self._last_balance_recovery_side = self.position_side
            else:
                self._last_balance_recovery_entry_ts = 0.0
                self._last_balance_recovery_side = ""
            self._post_exit_server_sync_required = False
            self._clear_reverse_after_sync()
            self._clear_reverse_retry()
            self._clear_post_exit_overlap_entry()
            self.active_position_side = self.position_side
            self.active_position_qty = self.position_qty
            self.flat_confirmed = False
            if float(event.price or 0.0) > 0:
                self.entry_price = float(event.price)
            self.position_entry_time = datetime.now()
            _entry_metrics = dict(self._last_entry_submit_metrics or {})
            if str(self.position_side or "").upper() in ("LONG", "SHORT"):
                try:
                    _m_side = str(self._last_entry_submit_side or "").upper()
                    if _m_side and _m_side != str(self.position_side or "").upper():
                        _entry_metrics = {}
                except Exception:
                    _entry_metrics = {}
            # EXIT_Z/ENTRY are price-based in this build. Do not capture or restore Z5 anchors.
            try:
                _price_entry_mode = bool((self.last_sig_state or {}).get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
            except Exception:
                _price_entry_mode = bool(getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False))
            if _price_entry_mode:
                self.position_entry_z5 = None
            else:
                try:
                    _entry_z5_now = _entry_metrics.get("entry_z5", (self.last_sig_state or {}).get("z5"))
                    self.position_entry_z5 = (float(_entry_z5_now) if _entry_z5_now is not None else None)
                except Exception:
                    self.position_entry_z5 = None
            try:
                _entry_prev = _entry_metrics.get("entry_prev_z5")
                self.position_entry_prev_z5 = (float(_entry_prev) if _entry_prev is not None else None)
            except Exception:
                self.position_entry_prev_z5 = None
            try:
                _entry_dz = _entry_metrics.get("entry_dz5")
                self.position_entry_dz5 = (float(_entry_dz) if _entry_dz is not None else None)
            except Exception:
                self.position_entry_dz5 = None
            try:
                _entry_thr = _entry_metrics.get("entry_dz5_threshold")
                self.position_entry_dz5_threshold = (float(_entry_thr) if _entry_thr is not None else None)
            except Exception:
                self.position_entry_dz5_threshold = None
            _entry_dir_ok = _entry_metrics.get("entry_dz5_direction_ok")
            self.position_entry_dz5_direction_ok = (None if _entry_dir_ok is None else bool(_entry_dir_ok))
            try:
                _entry_macd_osci = _entry_metrics.get("entry_macd_osci", (self.last_sig_state or {}).get("macd_osci"))
                self.position_entry_macd_osci = (float(_entry_macd_osci) if _entry_macd_osci is not None else None)
            except Exception:
                self.position_entry_macd_osci = None
            try:
                self.position_entry_regime = self._resolve_entry_regime(
                    _entry_metrics or self.last_sig_state,
                    default="",
                )
            except Exception:
                self.position_entry_regime = ""
            self._update_entry_order_audit_from_event(event)
            self._position_entry_audit = dict(_entry_metrics or {})
            self._position_entry_order_audit = dict(self._last_entry_order_audit or {})
            try:
                self.position_entry_z5_band_snapshot = {
                    "long_entry_z5_min": (_entry_metrics.get("long_entry_z5_min")),
                    "long_entry_z5_max": (_entry_metrics.get("long_entry_z5_max")),
                    "short_entry_z5_min": (_entry_metrics.get("short_entry_z5_min")),
                    "short_entry_z5_max": (_entry_metrics.get("short_entry_z5_max")),
                    "trend_z5_abs_min": (_entry_metrics.get("trend_z5_abs_min")),
                    "trend_z5_abs_max": (_entry_metrics.get("trend_z5_abs_max")),
                    "reversal_z5_abs_max": (_entry_metrics.get("reversal_z5_abs_max")),
                    "static_z5_band_enabled_for_entry": bool(_entry_metrics.get("static_z5_band_enabled_for_entry", False)),
                }
            except Exception:
                self.position_entry_z5_band_snapshot = {}
            try:
                self.position_mfe_profile = self._mfe_profile_from_entry_context(_entry_metrics or self.last_sig_state)
            except Exception:
                self.position_mfe_profile = "REVERSAL"
            self.position_peak_price = float(self.entry_price or event.price or 0.0)
            self.position_trough_price = float(self.entry_price or event.price or 0.0)
            self.position_mfe_armed = False
            self.position_mfe_armed_since_ts = 0.0
            self.position_mfe_stage_step = 0
            self._position_mfe_stage_last_bar_key = ""
            self._position_mfe_stage_last_extreme_price = 0.0
            self.cancel_in_progress = False
            self.cancel_confirmed = False
            self.cancel_needs_server_check = False
            self._manual_cancel_check_pending = False
            self._refresh_lane_snapshot()
            # 진입 시점 cl1 μ/σ snapshot → 예상 청산지수 고정
            try:
                if bool(getattr(self.cfg, 'USE_EXIT_Z', False)):
                    import numpy as _np
                    lb = int(self.cfg.LOOKBACK)
                    if len(self.st.cl1) >= lb:
                        arr = _np.array(self.st.cl1, dtype=float)[-lb:]
                        mu, sd = float(arr.mean()), float(arr.std(ddof=0))
                        if sd > 1e-6:
                            self._entry_mu = mu
                            self._entry_sd = sd
                            side_z = (
                                -abs(float(getattr(self.cfg, 'EXIT_Z_LONG', 0.5) or 0.5))
                                if self.position_side == "LONG"
                                else abs(float(getattr(self.cfg, 'EXIT_Z_SHORT', -0.5) or -0.5))
                            )
                            self._fixed_exit_index = mu + side_z * sd
                    else:
                        self._fixed_exit_index = None
                else:
                    self._fixed_exit_index = None
            except Exception:
                self._fixed_exit_index = None
            try:
                _now = datetime.now()
                _entry_obs = dict(self._position_entry_audit or _entry_metrics or {})
                _order_obs = dict(self._position_entry_order_audit or self._last_entry_order_audit or {})
                self._append_csv_row(self._trade_log_path, [
                    _now.strftime("%Y-%m-%d"), _now.strftime("%H:%M:%S"),
                    "ENTRY", self.position_side,
                    int(event.qty or 0), float(event.price or 0.0),
                    "-", "-",
                    str(event.message or "-"),
                    f"{self.last_z:.4f}" if self.last_z is not None else "-",
                    self._fmt_num(self.position_entry_prev_z5, 6),
                    self._fmt_num(self.position_entry_dz5, 6),
                    self._fmt_num(self.position_entry_dz5_threshold, 6),
                    ("" if self.position_entry_dz5_direction_ok is None else int(bool(self.position_entry_dz5_direction_ok))),
                    self._fmt_num(self.position_entry_macd_osci, 6),
                    "",
                ] + self._entry_observation_csv_values(_entry_obs) + self._entry_order_audit_csv_values(_order_obs))
                self.log(
                    "SIGNAL",
                    "ENTRY_AUDIT_FILL",
                    (
                        f"side={self.position_side} regime={_entry_obs.get('entry_regime') or '-'} "
                        f"basis={_entry_obs.get('entry_basis') or '-'} hit={_entry_obs.get('entry_hit_mode') or '-'} "
                        f"cross={int(bool(_entry_obs.get('entry_cross_through', False)))} "
                        f"validation={_entry_obs.get('entry_validation') or '-'} "
                        f"fill_qty={int(_order_obs.get('filled_qty') or 0)} "
                        f"fill_price={self._fmt_num(_order_obs.get('filled_price'), 2)}"
                    ),
                )
            except Exception:
                pass
            self._persist_entry_anchor_state("ENTRY_FILLED")
            try:
                self._prime_signal_snapshot_after_warmup("ENTRY_FILLED")
            except Exception:
                pass
            if event.event_type == "ENTRY_FILLED":
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                    lambda: self._request_server_sync(
                        reason='ENTRY_FILLED',
                        include_takeover=False,
                        include_unfilled=False,
                        include_account=True,
                        include_orderable=True,
                        start_warmup_after_response=False,
                        force=True,
                    ),
                )
            if event.event_type == "ENTRY_FILLED":
                _entry_report_reason = str(event.message or "-").strip()
                if isinstance(event.raw, dict):
                    _entry_report_reason = str(
                        event.raw.get("reason") or _entry_report_reason
                    ).strip()
                self._telegram_send_execution_report(
                    report_type="ENTRY",
                    side=self.position_side,
                    qty=int(event.qty or self.position_qty or 0),
                    fill_price=float(event.price or self.entry_price or 0.0),
                    reason=_entry_report_reason,
                    entry_regime=str(self.position_entry_regime or ""),
                )
        elif event.event_type == "EXIT_FILLED":
            clear_monitors = getattr(getattr(self, "client", None), "clear_order_monitors", None)
            if callable(clear_monitors):
                clear_monitors()
            self._entry_order_guard_active = False
            self._entry_order_guard_side = ""
            self._entry_order_guard_ts = 0.0
            prior_side_for_reverse = str(self.position_side or "FLAT")
            exit_reason_for_reverse = str(event.message or "").strip()
            self._last_exit_filled_ts = time.time()
            self.exit_in_progress = False
            self.exit_inflight = False
            self.position_close_pending = False
            self.exit_confirmed = True
            self.exit_needs_server_check = False
            self.auto_cleanup_phase = "AUTO_EXIT_WAIT"
            prior_side = str(self.position_side or "FLAT")
            prior_qty = int(self.position_qty or 0)
            self._confirm_flat_exit_reason("EXIT_FILLED", prior_side=prior_side, prior_qty=prior_qty, preferred_reason=str(event.message or "").strip())
            _now = datetime.now()
            _exit_price = float(event.price or 0.0)
            _entry_price_for_report = float(self.entry_price or 0.0)
            _pnl = 0.0
            if _entry_price_for_report > 0 and _exit_price > 0:
                _mult = 1.0 if self.position_side == "LONG" else -1.0
                _pnl = round((_exit_price - _entry_price_for_report) * _mult, 2)
            try:
                try:
                    _exit_macd_osci = (self.last_sig_state or {}).get("macd_osci")
                    _exit_macd_osci = (float(_exit_macd_osci) if _exit_macd_osci is not None else None)
                except Exception:
                    _exit_macd_osci = None
                _entry_obs = dict(self._position_entry_audit or {})
                self._append_csv_row(self._trade_log_path, [
                    _now.strftime("%Y-%m-%d"), _now.strftime("%H:%M:%S"),
                    "EXIT", self.position_side,
                    int(event.qty or 0), _exit_price,
                    _entry_price_for_report, _pnl,
                    self.last_exit_reason,
                    f"{self.last_z:.4f}" if self.last_z is not None else "-",
                    self._fmt_num(self.position_entry_prev_z5, 6),
                    self._fmt_num(self.position_entry_dz5, 6),
                    self._fmt_num(self.position_entry_dz5_threshold, 6),
                    ("" if self.position_entry_dz5_direction_ok is None else int(bool(self.position_entry_dz5_direction_ok))),
                    self._fmt_num(self.position_entry_macd_osci, 6),
                    self._fmt_num(_exit_macd_osci, 6),
                ] + self._entry_observation_csv_values(_entry_obs) + self._entry_order_audit_csv_values(self._position_entry_order_audit))
                _ctx = dict(self._last_exit_submit_context or {})
                self._append_csv_row(
                    self._exit_trace_path,
                    [
                        _now.strftime("%Y-%m-%d"),
                        _now.strftime("%H:%M:%S"),
                        "EXIT_FILLED",
                        self.live_code,
                        str(_ctx.get("position_side") or self.position_side or ""),
                        int(_ctx.get("position_qty") or event.qty or 0),
                        self._fmt_num(_ctx.get("entry_price"), 2),
                        self._fmt_num(_ctx.get("entry_z5"), 6),
                        str(event.message or self.last_exit_reason or ""),
                        str(_ctx.get("exit_rule") or ""),
                        str(_ctx.get("exit_source") or ""),
                        self._fmt_num(_ctx.get("exit_signal"), 6),
                        self._fmt_num(_ctx.get("exit_threshold"), 6),
                        self._fmt_num(_ctx.get("exit_z_long"), 6),
                        self._fmt_num(_ctx.get("exit_z_short"), 6),
                        self._fmt_num(_ctx.get("exit_delta"), 6),
                        self._fmt_num(_ctx.get("bar_high"), 2),
                        self._fmt_num(_ctx.get("bar_low"), 2),
                        self._fmt_num(_ctx.get("bar_close"), 2),
                        self._fmt_num(_exit_price, 2),
                        self._fmt_num(_pnl, 2),
                    ],
                )
            except Exception:
                pass
            self._position_entry_audit = {}
            self._position_entry_order_audit = {}
            self._telegram_send_execution_report(
                report_type="EXIT",
                side=prior_side,
                qty=int(event.qty or prior_qty or 0),
                fill_price=_exit_price,
                reason=str(self.last_exit_reason or event.message or "-").strip(),
                pnl_pt=_pnl,
                entry_price=_entry_price_for_report,
                entry_regime=str(self.position_entry_regime or _entry_obs.get("entry_regime") or ""),
            )
            self.log(
                "EXIT",
                "SUMMARY",
                f"시각={_now.strftime('%H:%M:%S')} "
                f"사유={str(self.last_exit_reason or event.message or '-').strip() or '-'} "
                f"진입가={self._fmt_num(_entry_price_for_report, 2)} "
                f"청산가={self._fmt_num(_exit_price, 2)} "
                f"손익={self._fmt_num(_pnl, 2)}pt",
            )
            _exit_reason_upper_for_flow = str(exit_reason_for_reverse or "").strip().upper()
            if (
                "REVERSE_SIGNAL" in _exit_reason_upper_for_flow
                or str(self.last_exit_reason or "").strip().upper() == "REVERSE_SIGNAL"
            ):
                _exit_reason_upper_for_flow = "REVERSE_SIGNAL"
            _overlap_entry_ready = bool(self._is_post_exit_overlap_entry_ready(prior_side_for_reverse, _exit_reason_upper_for_flow))
            if _overlap_entry_ready:
                self.log("SYNC", "POST_EXIT_ENTRY_DEFER", f"reason={_exit_reason_upper_for_flow or exit_reason_for_reverse or '-'} wait=SERVER_FLAT_CONFIRM")
            self._reset_entry_watch_after_exit_fill(_exit_reason_upper_for_flow or exit_reason_for_reverse or "EXIT_FILLED")
            if _exit_reason_upper_for_flow == "REVERSE_SIGNAL":
                # The close fill itself is the broker flat confirmation needed
                # for REVERSE. Submit the preserved opposite FIRE snapshot on
                # the next event-loop turn, immediately after EXIT pending cleanup.
                self._schedule_forced_reverse_entry_after_exit(
                    prior_side_for_reverse, _exit_reason_upper_for_flow
                )
                if bool(getattr(self.cfg, "ALLOW_REVERSE_SIGNAL_REENTRY", False)):
                    self.log(
                        "SYNC",
                        "REVERSE_EXIT_FILLED_IMMEDIATE_ENTRY",
                        f"prior={prior_side_for_reverse}/{prior_qty} target={'SHORT' if prior_side_for_reverse == 'LONG' else 'LONG'}",
                    )
                else:
                    self.log(
                        "SYNC",
                        "REVERSE_EXIT_FILLED_EXIT_ONLY",
                        f"prior={prior_side_for_reverse}/{prior_qty} reentry=OFF",
                    )
            else:
                self._post_exit_server_sync_required = True
                self.flat_confirmed = False
                self.order_lane_locked = True
                self.position_close_pending = True
                self.exit_confirmed = False
                self.server_sync_pending = True
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                    lambda: self._request_server_sync(reason='EXIT_FILLED', include_takeover=True, include_unfilled=True, start_warmup_after_response=False, force=True),
                )
                self.log(
                    "SYNC",
                    "EXIT_FILLED_WAIT_SERVER_FLAT",
                    f"prior={prior_side_for_reverse}/{prior_qty} reason={_exit_reason_upper_for_flow or '-'} overlap_entry={int(_overlap_entry_ready)}",
                )
            if event.event_type == "EXIT_FILLED":
                self._clear_reverse_after_sync()
                self._clear_reverse_retry()
                # Do not clear local position/MFE state here.  Server balance must
                # confirm FLAT first; otherwise HTS can still hold a contract while
                # the dashboard shows an exited FLAT state.
                self._refresh_lane_snapshot()
                self._refresh_cleanup_statuses()
        elif event.event_type == "CANCELED":
            self._entry_order_guard_active = False
            self._entry_order_guard_side = ""
            self._entry_order_guard_ts = 0.0
            self.cancel_in_progress = True
            self.cancel_confirmed = False
            self.cancel_needs_server_check = False
            self.entry_inflight = False
            self.pending_entry_side = ""
            self.exit_in_progress = False
            self.exit_inflight = False
            self.position_close_pending = False
            self.exit_confirmed = False
            self.exit_needs_server_check = False
            self.auto_cleanup_phase = "AUTO_CANCEL_WAIT"
            self.auto_cancel_phase = "AUTO_CANCEL_WAIT"
            self._refresh_lane_snapshot()
            self._refresh_cleanup_statuses()
            QTimer.singleShot(
                int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                lambda: self._request_server_sync(reason='CANCEL_CONFIRMED', include_takeover=False, include_unfilled=True, start_warmup_after_response=False, force=True),
            )
        elif event.event_type == "EXIT_PARTIAL":
            remain = max(self.position_qty - int(event.qty or 0), 0)
            prior_side = str(self.position_side or "FLAT")
            prior_qty = int(self.position_qty or 0)
            self.position_qty = remain
            self.active_position_side = prior_side if remain > 0 else "FLAT"
            self.active_position_qty = remain
            self.exit_in_progress = True
            self.exit_inflight = True
            self.position_close_pending = True
            self.exit_confirmed = False
            if remain <= 0:
                self._confirm_flat_exit_reason("SERVER_SYNC_FLAT", prior_side=prior_side, prior_qty=prior_qty, preferred_reason="")
                self._clear_local_position_after_server_flat_confirm("EXIT_PARTIAL_FLAT")
                self.exit_in_progress = False
                self.exit_inflight = False
                self.position_close_pending = False
                self.flat_confirmed = True
                self.order_lane_locked = False
                self.log("SYNC", "FLAT_CONFIRMED", "order_lane=REOPEN")
                self._telegram_send_position_sync("exit_filled_flat")
            else:
                self._telegram_send_position_sync("exit_partial")
            self._refresh_lane_snapshot()
        elif event.event_type == "REJECTED":
            message = str(event.message or "")
            if message in ("EXIT_NO_CALLBACK_TIMEOUT", "EXIT_NO_KIS_EXECUTION_TIMEOUT"):
                self.exit_in_progress = False
                self.exit_inflight = False
                self.position_close_pending = False
                self.exit_confirmed = False
                self.exit_needs_server_check = True
                self.server_sync_pending = True
                self.server_sync_reason = message
                self.auto_cleanup_phase = "AUTO_EXIT_WAIT" if self.auto_on else self.auto_cleanup_phase
                self.log("WARN", "EXIT_GUARD", f"unconfirmed exit order; blocking re-submit until server sync reason={message}")
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                    lambda: self._request_server_sync(reason=message, include_takeover=True, include_unfilled=False, start_warmup_after_response=False, force=True),
                )
            elif message in ("NO_CALLBACK_TIMEOUT", "NO_KIS_EXECUTION_TIMEOUT", "AUTO_CANCEL_SEND_FAIL"):
                _post_exit_mfe_entry = bool(self._is_post_exit_mfe_entry_context(event))
                self._entry_order_guard_active = True
                self._entry_order_guard_side = str(event.side or self._entry_order_guard_side or "")
                self._entry_order_guard_ts = self._entry_order_guard_ts or time.time()
                self.entry_inflight = True
                self.server_sync_pending = True
                self.server_sync_reason = "POST_EXIT_ENTRY_UNKNOWN" if _post_exit_mfe_entry else message
                self.auto_cancel_phase = "UNFILLED_RECHECK_REQUIRED"
                self.cancel_in_progress = False
                self.cancel_needs_server_check = True
                if _post_exit_mfe_entry:
                    self.log("WARN", "POST_EXIT_ENTRY_GUARD", f"unconfirmed post-MFE opposite entry; server recheck side={self._entry_order_guard_side or '-'} reason={message}")
                else:
                    self.log("WARN", "ENTRY_GUARD", f"unconfirmed entry order; blocking additional entries side={self._entry_order_guard_side or '-'} reason={message}")
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                    lambda: self._request_server_sync(reason=("POST_EXIT_ENTRY_UNKNOWN" if _post_exit_mfe_entry else message), include_takeover=_post_exit_mfe_entry, include_unfilled=True, start_warmup_after_response=False, force=True),
                )
            else:
                self._entry_order_guard_active = False
                self._entry_order_guard_side = ""
                self._entry_order_guard_ts = 0.0
                self.entry_inflight = False
                self.pending_entry_side = ""
            if message.startswith(("KIS cancel request ret=", "Cancel KIS order request ret=")):
                self.cancel_in_progress = False
                self.cancel_confirmed = False
                self.cancel_needs_server_check = True
                self.server_sync_pending = True
                self.auto_cleanup_phase = "AUTO_CANCEL_WAIT" if self.auto_on else self.auto_cleanup_phase
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                    lambda: self._request_server_sync(reason='CANCEL_SEND_FAIL', include_takeover=False, include_unfilled=True, start_warmup_after_response=False, force=True),
                )
            if message.startswith("KIS order request ret=") and self.order_core and getattr(self.order_core.pending, "action", "") == "EXIT":
                self.exit_in_progress = False
                self.exit_inflight = False
                self.position_close_pending = False
                self.exit_confirmed = False
                self.exit_needs_server_check = True
                self.server_sync_pending = True
                QTimer.singleShot(
                    int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                    lambda: self._request_server_sync(reason='EXIT_SEND_FAIL', include_takeover=True, include_unfilled=False, start_warmup_after_response=False, force=True),
                )
        elif event.event_type == "CANCEL_REQUESTED":
            self.server_sync_pending = True
            self.server_sync_reason = "AUTO_CANCEL_REQUESTED"
            self.auto_cancel_phase = "AUTO_CANCEL_REQUESTED"
            self.cancel_in_progress = True
            self.cancel_confirmed = False
            self.cancel_needs_server_check = False
            self._manual_cancel_check_pending = False
            QTimer.singleShot(
                int(getattr(self.cfg, "UNFILLED_RECHECK_DELAY_MS", 350) or 350),
                lambda: self._request_server_sync(reason='AUTO_CANCEL_REQUESTED', include_takeover=False, include_unfilled=True, start_warmup_after_response=False, force=True),
            )
        self._refresh_lane_snapshot()
        self._refresh_cleanup_statuses()
        if self.auto_on:
            self._advance_auto_cleanup()
        self._schedule_refresh_view(force=True, delay_ms=50)

    def _schedule_refresh_view(self, force: bool = False, delay_ms: int | None = None) -> None:
        """Coalesce expensive dashboard refreshes during tick/EXIT bursts.

        EXIT 전후에는 execution_notice callback, server-sync 예약, 로그 추가, MFE 깜박임이
        한꺼번에 들어오므로 refresh_view를 즉시 여러 번 실행하지 않고 한 번만
        예약한다. force 요청은 예약 실행 시 보존한다.
        """
        try:
            if force:
                self._view_refresh_force_pending = True
            if self._view_refresh_pending:
                return
            self._view_refresh_pending = True
            _delay = self._view_refresh_defer_ms if delay_ms is None else max(0, int(delay_ms or 0))

            def _run() -> None:
                try:
                    _force = bool(getattr(self, "_view_refresh_force_pending", False))
                    self._view_refresh_force_pending = False
                    self._view_refresh_pending = False
                    self.refresh_view(force=_force)
                except Exception:
                    self._view_refresh_pending = False
                    self._view_refresh_force_pending = False
                    try:
                        self.refresh_view(force=force)
                    except Exception:
                        pass

            QTimer.singleShot(_delay, _run)
        except Exception:
            try:
                self.refresh_view(force=force)
            except Exception:
                pass

    def _build_position_text(self) -> tuple[str, float | None]:
        pos_text = "포지션 없음"
        realtime_pnl: float | None = None

        if self.position_side in ("LONG", "SHORT") and self.position_qty > 0:
            cp = 0.0
            for _raw_px in (self.current_price, self.last_bar_close, self.session_close, self.entry_price):
                try:
                    _px = float(_raw_px or 0.0)
                except Exception:
                    _px = 0.0
                if _px > 0.0:
                    cp = _px
                    break
            pnl_str = ""
            if self.entry_price > 0:
                if cp > 0:
                    if self.position_side == "LONG":
                        realtime_pnl = (cp - self.entry_price) * self.position_qty
                    else:
                        realtime_pnl = (self.entry_price - cp) * self.position_qty
                    pnl_str = f"  ({realtime_pnl:+.2f}pt)"
                pos_text = f"{self.position_side} {self.position_qty} @ {self.entry_price:.2f}{pnl_str}"
            else:
                pos_text = f"{self.position_side} {self.position_qty} @ -"
        elif self._entry_order_guard_active:
            guard_side = self._entry_order_guard_side or "-"
            pos_text = f"미확인 진입주문 {guard_side} 확인 필요"

        return pos_text, realtime_pnl

    # ---------- strategy ----------
    def on_new_1m_candle(self, c: Candle, allow_trade: bool = True, eval_time: datetime | None = None) -> None:
        minute_key = c.t.replace(second=0, microsecond=0)
        signal_eval_time = eval_time if isinstance(eval_time, datetime) else (c.t if isinstance(getattr(c, "t", None), datetime) else minute_key)
        canonical_candle = Candle(
            minute_key,
            float(c.o),
            float(c.h),
            float(c.l),
            float(c.c),
            int(c.v or 0),
        )
        _, minute_action = self._update_live_minute_cache(canonical_candle)
        self._apply_session_boundary(canonical_candle.t, canonical_candle.o, confirm_open=True)
        self.last_bar_close = float(canonical_candle.c)
        self.session_close = float(canonical_candle.c)
        self.last_bar_time = canonical_candle.t.strftime("%H:%M:%S")
        if self.session_high_price <= 0 or float(canonical_candle.h) > float(self.session_high_price):
            self.session_high_price = float(canonical_candle.h)
            self.session_high_time = self.last_bar_time
        if self.session_low_price <= 0 or float(canonical_candle.l) < float(self.session_low_price):
            self.session_low_price = float(canonical_candle.l)
            self.session_low_time = self.last_bar_time

        update_debug = self.st.update_indicators(canonical_candle) or {}
        counts = dict(self.st.get_counts() or {})
        exp_1m = int(counts.get("bars_1m", 0) or 0)
        exp_5m = int(counts.get("bars_5m", 0) or 0)
        exp_30m = int(counts.get("bars_30m", 0) or 0)
        self.bar_count_1m = int(exp_1m)
        self._register_post_warmup_live_bar(canonical_candle, allow_trade)
        minute_key = canonical_candle.t
        emit_bar_log = bool(allow_trade) or (minute_action == "NEW_MINUTE_APPEND")
        if emit_bar_log:
            self.log(
                "BAR",
                "LIVE_1M",
                (
                    f"minute_key={minute_key:%Y-%m-%d %H:%M:%S} "
                    f"action={minute_action}"
                ),
            )
            last5 = self._diag_last_5m_bucket_key
            last5_text = last5.strftime("%Y-%m-%d %H:%M:%S") if isinstance(last5, datetime) else "-"
            last30 = self._diag_last_30m_bucket_key
            last30_text = last30.strftime("%Y-%m-%d %H:%M:%S") if isinstance(last30, datetime) else "-"
            self.log(
                "BAR",
                "LIVE_5M",
                (
                    f"bars_1m={exp_1m} bars_5m={exp_5m} bars_30m={exp_30m} "
                    f"last_5m_bucket={last5_text} last_30m_bucket={last30_text}"
                ),
            )
        _entry_same_minute = False
        if isinstance(self.position_entry_time, datetime) and isinstance(canonical_candle.t, datetime):
            try:
                _entry_same_minute = (
                    self.position_entry_time.year == canonical_candle.t.year
                    and self.position_entry_time.month == canonical_candle.t.month
                    and self.position_entry_time.day == canonical_candle.t.day
                    and self.position_entry_time.hour == canonical_candle.t.hour
                    and self.position_entry_time.minute == canonical_candle.t.minute
                )
            except Exception:
                _entry_same_minute = False
        try:
            _live_track_px = float(self.current_price or canonical_candle.c or 0.0)
        except Exception:
            _live_track_px = float(canonical_candle.c)
        if _live_track_px <= 0.0:
            _live_track_px = float(canonical_candle.c)

        if self.position_side == 'LONG' and self.position_qty > 0:
            _prev_peak = float(self.position_peak_price or self.entry_price or canonical_candle.c)
            _peak_source = float(_live_track_px) if _entry_same_minute else float(canonical_candle.h)
            _low_source = float(_live_track_px) if _entry_same_minute else float(canonical_candle.l)
            _new_peak = max(_prev_peak, _peak_source)
            self.position_peak_price = _new_peak
            if self.position_trough_price <= 0:
                self.position_trough_price = float(self.entry_price or canonical_candle.c)
            self.position_trough_price = min(float(self.position_trough_price), _low_source)
            if _new_peak > _prev_peak + 1e-9:
                _arm_pt = float(getattr(self.cfg, "EXIT_MFE_MIN_PT", 1.8) or 1.8)
                _mfe_pt = max(0.0, float(_new_peak) - float(self.entry_price or canonical_candle.c))
                if (not bool(self.position_mfe_armed)) and _mfe_pt >= _arm_pt:
                    self.position_mfe_armed = True
                    self.position_mfe_armed_since_ts = time.time()
                    self.position_mfe_stage_step = 0
                    self._position_mfe_stage_last_bar_key = canonical_candle.t.strftime("%Y-%m-%d %H:%M:%S")
                    self._position_mfe_stage_last_extreme_price = float(_new_peak)
                elif bool(self.position_mfe_armed):
                    _last_extreme = float(self._position_mfe_stage_last_extreme_price or _prev_peak)
                    if _new_peak > _last_extreme + 1e-9:
                        self.position_mfe_stage_step = min(2, int(self.position_mfe_stage_step or 0) + 1)
                        self._position_mfe_stage_last_bar_key = canonical_candle.t.strftime("%Y-%m-%d %H:%M:%S")
                        self._position_mfe_stage_last_extreme_price = float(_new_peak)
        elif self.position_side == 'SHORT' and self.position_qty > 0:
            _prev_trough = float(self.position_trough_price or self.entry_price or canonical_candle.c)
            _low_source = float(_live_track_px) if _entry_same_minute else float(canonical_candle.l)
            _high_source = float(_live_track_px) if _entry_same_minute else float(canonical_candle.h)
            _new_trough = min(_prev_trough, _low_source)
            self.position_trough_price = _new_trough
            if self.position_peak_price <= 0:
                self.position_peak_price = float(self.entry_price or canonical_candle.c)
            self.position_peak_price = max(float(self.position_peak_price), _high_source)
            if _new_trough < _prev_trough - 1e-9:
                _arm_pt = float(getattr(self.cfg, "EXIT_MFE_MIN_PT", 1.8) or 1.8)
                _mfe_pt = max(0.0, float(self.entry_price or canonical_candle.c) - float(_new_trough))
                if (not bool(self.position_mfe_armed)) and _mfe_pt >= _arm_pt:
                    self.position_mfe_armed = True
                    self.position_mfe_armed_since_ts = time.time()
                    self.position_mfe_stage_step = 0
                    self._position_mfe_stage_last_bar_key = canonical_candle.t.strftime("%Y-%m-%d %H:%M:%S")
                    self._position_mfe_stage_last_extreme_price = float(_new_trough)
                elif bool(self.position_mfe_armed):
                    _last_extreme = float(self._position_mfe_stage_last_extreme_price or _prev_trough)
                    if _new_trough < _last_extreme - 1e-9:
                        self.position_mfe_stage_step = min(2, int(self.position_mfe_stage_step or 0) + 1)
                        self._position_mfe_stage_last_bar_key = canonical_candle.t.strftime("%Y-%m-%d %H:%M:%S")
                        self._position_mfe_stage_last_extreme_price = float(_new_trough)

        in_pos = 1 if self.position_side == "LONG" and self.position_qty > 0 else (-1 if self.position_side == "SHORT" and self.position_qty > 0 else 0)
        pos_meta = None
        if in_pos != 0:
                _meta_high = float(canonical_candle.h)
                _meta_low = float(canonical_candle.l)
                if _entry_same_minute:
                    _meta_high = float(_live_track_px)
                    _meta_low = float(_live_track_px)
                pos_meta = {
                    'side': self.position_side,
                    'qty': self.position_qty,
                    'entry_price': self.entry_price,
                    'entry_time': self.position_entry_time,
                    'peak_price': self.position_peak_price,
                    'trough_price': self.position_trough_price,
                    'entry_regime': str(self.position_entry_regime or ''),
                    'entry_z5_band_snapshot': dict(self.position_entry_z5_band_snapshot or {}),
                    'mfe_profile': self._effective_position_mfe_profile(),
                    'mfe_armed': bool(self.position_mfe_armed),
                    'mfe_armed_since_ts': float(self.position_mfe_armed_since_ts or 0.0),
                    'current_high': _meta_high,
                    'current_low': _meta_low,
                    'current_close': float(canonical_candle.c),
                    'mfe_stage_step': int(self.position_mfe_stage_step or 0),
                    'bar_confirmed': bool(allow_trade),
                }
        # ENTRY/EXIT price evaluation must use the actual incoming tick price.
        # At a minute boundary canonical_candle is the completed previous minute,
        # while _live_track_px is the first real tick of the new minute.  Using
        # canonical_candle.c here could authorize FIRE inside the old close band
        # and then submit the order at a new-tick price outside that band.
        sig_state = self.st.get_signal_state(
            float(_live_track_px),
            signal_eval_time,
            position_meta=pos_meta,
            bar_confirmed=bool(allow_trade),
        )
        # EXIT_Z is price/retrace based only; do not seed or restore entry_z5 anchors.
        self._refresh_live_z5_diag_state(exp_1m, exp_5m, exp_30m)
        _monitor_keys = (
            "z1_delta_long_armed",
            "z1_delta_short_armed",
            "z1_delta_long_fire",
            "z1_delta_short_fire",
            "z1_delta_long_blink_armed",
            "z1_delta_short_blink_armed",
            "z5_monitor_long_armed",
            "z5_monitor_short_armed",
            "z5_monitor_long_fire",
            "z5_monitor_short_fire",
        )
        _prior_monitor_state = tuple(
            bool((self.last_sig_state or {}).get(_key, False))
            for _key in _monitor_keys
        )
        self.last_sig_state = dict(sig_state or {})
        _sma10s_event = str(self.last_sig_state.get("sma10s_event") or "").strip().upper()
        if _sma10s_event:
            self.log(
                "SIGNAL",
                "SMA10S_REVERSAL_ARM_FIRE_AUDIT",
                (
                    f"event={_sma10s_event} "
                    f"price={self._fmt_num(self.last_sig_state.get('sma10s_event_price'), 4)} "
                    f"prev_price={self._fmt_num(self.last_sig_state.get('sma10s_event_previous_price'), 4)} "
                    f"prev_line={self._fmt_num(self.last_sig_state.get('sma10s_event_previous_line'), 4)} "
                    f"line={self._fmt_num(self.last_sig_state.get('sma10s_event_current_line'), 4)} "
                    f"direction={self.last_sig_state.get('sma10s_touch_direction') or '-'} "
                    f"sma5={self._fmt_num(self.last_sig_state.get('sma10s_5'), 4)} "
                    f"sma10={self._fmt_num(self.last_sig_state.get('sma10s_10'), 4)} "
                    f"sma20={self._fmt_num(self.last_sig_state.get('sma10s_20'), 4)} "
                    f"stack={self.last_sig_state.get('sma10s_relation') or '-'} "
                    f"regime={self.last_sig_state.get('sma10s_regime') or '-'} "
                    f"source={self.last_sig_state.get('sma10s_source') or '-'} "
                    f"approx={int(bool(self.last_sig_state.get('sma10s_is_approx', False)))} "
                    f"bucket={self.last_sig_state.get('sma10s_bucket_time') or '-'}"
                ),
            )
        _sma1m_event = str(self.last_sig_state.get("sma1m_event") or "").strip().upper()
        if _sma1m_event:
            self.log(
                "SIGNAL",
                "SMA1M_TREND_ARM_FIRE_AUDIT",
                (
                    f"event={_sma1m_event} "
                    f"price={self._fmt_num(self.last_sig_state.get('sma1m_event_price'), 4)} "
                    f"sma20={self._fmt_num(self.last_sig_state.get('sma1m_event_line'), 4)} "
                    f"offset={self._fmt_num(self.last_sig_state.get('sma1m_event_offset'), 4)} "
                    f"direction={self.last_sig_state.get('sma1m_touch_direction') or '-'} "
                    f"sma5={self._fmt_num(self.last_sig_state.get('sma1m_5'), 4)} "
                    f"sma10={self._fmt_num(self.last_sig_state.get('sma1m_10'), 4)} "
                    f"relation={self.last_sig_state.get('sma1m_relation') or '-'} "
                    f"regime={self.last_sig_state.get('sma1m_regime') or '-'} "
                    f"long_fire_band={self._fmt_num(self.last_sig_state.get('sma1m_trend_buy_fire_min'), 4)}"
                    f"~{self._fmt_num(self.last_sig_state.get('sma1m_trend_buy_fire_max'), 4)} "
                    f"short_fire_band={self._fmt_num(self.last_sig_state.get('sma1m_trend_sell_fire_min'), 4)}"
                    f"~{self._fmt_num(self.last_sig_state.get('sma1m_trend_sell_fire_max'), 4)} "
                    f"source={self.last_sig_state.get('sma1m_source') or '-'}"
                ),
            )
        _monitor_now = time.monotonic()
        _fire_hold_sec = max(0.0, float(getattr(
            self.cfg, "DELTA_FIRE_BLINK_HOLD_SEC", 1.6
        ) or 0.0))
        for _side_name in ("long", "short"):
            _raw_fire_key = f"z1_delta_{_side_name}_fire"
            _display_fire_key = f"z1_delta_{_side_name}_fire_display"
            _until_attr = f"_z1_delta_{_side_name}_fire_display_until"
            if bool(self.last_sig_state.get(_raw_fire_key, False)):
                setattr(self, _until_attr, _monitor_now + _fire_hold_sec)
            self.last_sig_state[_display_fire_key] = bool(
                _monitor_now < float(getattr(self, _until_attr, 0.0) or 0.0)
            )
        _current_monitor_state = tuple(
            bool(self.last_sig_state.get(_key, False))
            for _key in _monitor_keys
        )
        if _current_monitor_state != _prior_monitor_state:
            # AUTO OFF has fewer forced UI updates from order/sync callbacks.
            # Bypass the normal refresh throttle on ARM/FIRE transitions so a
            # one-tick FIRE state cannot disappear before it is rendered.
            _schedule_monitor_refresh = getattr(self, "_schedule_refresh_view", None)
            if callable(_schedule_monitor_refresh):
                _schedule_monitor_refresh(force=True, delay_ms=0)
            self.log(
                "SIGNAL",
                "Z1_ARM_FIRE_MONITOR",
                (
                    f"dz1={self._fmt_num(self.last_sig_state.get('dz1_prev_delta'), 6)} "
                    f"long_hit={int(bool(self.last_sig_state.get('z1_delta_long_prehit', False)))} "
                    f"long_arm={int(bool(self.last_sig_state.get('z1_delta_long_armed', False)))} "
                    f"long_fire={int(bool(self.last_sig_state.get('z1_delta_long_fire', False)))} "
                    f"short_hit={int(bool(self.last_sig_state.get('z1_delta_short_prehit', False)))} "
                    f"short_arm={int(bool(self.last_sig_state.get('z1_delta_short_armed', False)))} "
                    f"short_fire={int(bool(self.last_sig_state.get('z1_delta_short_fire', False)))}"
                ),
            )
        self.last_sig_state["live_z5_diag"] = dict(self.live_z5_diag_state)
        self.last_regime = sig_state.get('regime', self.st.check_regime(canonical_candle.t, current_price=float(canonical_candle.c)))
        self.last_z_1m = sig_state.get("z1")
        self.last_z_5m = sig_state.get("z5")
        self.last_z_30m = sig_state.get("z30")
        self.last_z = self.last_z_5m
        counts = {"bars_1m": exp_1m, "bars_5m": exp_5m, "bars_30m": exp_30m}
        self._record_live_bar_and_z(canonical_candle, source="LIVE_1M", counts_override=counts)
        lb = int(self.cfg.LOOKBACK)
        self.last_warmup_text = (
            f"1분 {min(exp_1m, lb)}/{lb} | "
            f"5분 {min(exp_5m, lb)}/{lb} | "
            f"30분 {min(exp_30m, lb)}/{lb}"
        )

        self.last_long_ok = bool(sig_state.get('long_ok'))
        self.last_short_ok = bool(sig_state.get('short_ok'))
        self.last_exit_ok = bool(sig_state.get('exit_ok')) if in_pos != 0 else None
        regime_now = str(sig_state.get("regime", self.last_regime) or "NORMAL").upper()
        # Regime is kept as informational context only (no hard entry blocking).
        long_regime_ok = True
        short_regime_ok = True

        dz5_commit_mode = str(getattr(self.cfg, "DZ5_PREV_COMMIT_MODE", "BAR_CLOSE") or "BAR_CLOSE").strip().upper()
        if dz5_commit_mode == "EVERY_UPDATE":
            update_prev_state = True
        else:
            # Default/legacy live behavior: commit on confirmed minute-close pass only.
            update_prev_state = bool(allow_trade)
        sig = self.st.signal(
            canonical_candle,
            in_pos,
            position_meta=pos_meta,
            sig_state=sig_state,
            update_prev_state=update_prev_state,
        )
        # ARM/FIRE now uses the current tick against confirmed anchors, and
        # live ENTRY submission follows that intrabar signal immediately.
        self.last_signal = str((sig or {}).get("side") or "NONE")
        self.last_macd_delta = sig_state.get('macd_delta')
        self.last_short_enabled = bool(getattr(self.cfg, "ENABLE_SHORT", False))
        _entry_src_for_log = str(sig_state.get("entry_source", getattr(self.cfg, "ENTRY_SIGNAL_SOURCE", "z1")) or "z1").lower()
        if _entry_src_for_log == "price":
            _entry_log_val = getattr(canonical_candle, "c", None)
            z_txt = f"price={float(_entry_log_val):.2f}" if _entry_log_val is not None else "price=-"
        else:
            z_txt = f"{_entry_src_for_log}={self.last_z:+.3f}" if self.last_z is not None else f"{_entry_src_for_log}=-"
        m_txt = f" macd1mΔ={self.last_macd_delta:+.3f}" if self.last_macd_delta is not None else " macd1mΔ=-"
        self.last_signal_reason = (sig or {}).get('reason') or (z_txt + m_txt)
        allow_lane, block_reason = self._entry_lane_state(sig_state)
        self.last_allow_trade = bool(allow_lane)
        self.last_block_reason = str(block_reason or "-")
        self.last_sig_state["allow_trade"] = self.last_allow_trade
        self.last_sig_state["block_reason"] = self.last_block_reason
        self.last_sig_state["server_sync_pending"] = bool(self.server_sync_pending)
        self.last_sig_state["has_server_unfilled"] = bool(self.has_server_unfilled)
        self.last_sig_state["has_unfilled_orders"] = bool(self.has_unfilled_orders)
        self.last_sig_state["server_unfilled_qty"] = int(self.server_unfilled_qty or 0)
        self.last_sig_state["auto_cancel_phase"] = str(self.auto_cancel_phase or "-")
        self.last_sig_state["entry_inflight"] = bool(self.entry_inflight)
        self.last_sig_state["exit_inflight"] = bool(self.exit_inflight)
        self.last_sig_state["cancel_in_progress"] = bool(self.cancel_in_progress)
        self.last_sig_state["order_lane_locked"] = bool(self.order_lane_locked)
        self.last_sig_state["flat_confirmed"] = bool(self.flat_confirmed)
        self.last_sig_state["long_regime_ok"] = bool(long_regime_ok)
        self.last_sig_state["short_regime_ok"] = bool(short_regime_ok)
        regime_state = str(sig_state.get("regime_state", regime_now) or regime_now)
        regime_fast = sig_state.get("ma_fast_30")
        regime_slow = sig_state.get("ma_slow_30")
        regime_diff = sig_state.get("ma_diff_30")
        regime_threshold = sig_state.get("regime_threshold", getattr(self.cfg, "REGIME_THRESHOLD", 0.25))
        self._append_csv_row(
            self._state_report_path,
            [
                canonical_candle.t.strftime("%Y-%m-%d"),
                canonical_candle.t.strftime("%H:%M:%S"),
                self.live_code,
                regime_state,
                self._fmt_num(regime_fast, 6),
                self._fmt_num(regime_slow, 6),
                self._fmt_num(regime_diff, 6),
                self._fmt_num(regime_threshold, 6),
                regime_now,
            ],
        )

        _warmup_seed_z5 = sig_state.get("warmup_seed_z5")
        _prev_z5_sig = sig_state.get("prev_z5")
        _prev_z5_valid = bool(sig_state.get("prev_z5_valid", False))
        _continuity_valid = bool(sig_state.get("continuity_valid", False))
        _dz5_sig = sig_state.get("dz5")
        _dz5_low_anchor = sig_state.get("dz5_low_anchor_z5")
        _dz5_high_anchor = sig_state.get("dz5_high_anchor_z5")
        _continuity_reset_reason = str(sig_state.get("continuity_reset_reason") or "")
        _continuity_seed_reason = str(sig_state.get("continuity_seed_reason") or "")
        _reentry_mode = str(sig_state.get("reentry_continuity_mode") or ("WARMUP_SEEDED_CHAIN" if _continuity_valid else "UNSEEDED"))
        if in_pos == 0 and self.last_position_side_before_flat in ("LONG", "SHORT") and bool(self.flat_confirmed):
            _reentry_mode = f"POST_EXIT:{_reentry_mode}"
        elif in_pos == 0:
            _reentry_mode = f"INITIAL:{_reentry_mode}"
        else:
            _reentry_mode = f"IN_POS:{_reentry_mode}"
        _entry_side_for_audit = ""
        if str((sig or {}).get("side") or "").upper() == "BUY":
            _entry_side_for_audit = "LONG"
        elif str((sig or {}).get("side") or "").upper() == "SELL_SHORT":
            _entry_side_for_audit = "SHORT"
        elif bool(sig_state.get("long_entry_ready", False)) and not bool(sig_state.get("short_entry_ready", False)):
            _entry_side_for_audit = "LONG"
        elif bool(sig_state.get("short_entry_ready", False)) and not bool(sig_state.get("long_entry_ready", False)):
            _entry_side_for_audit = "SHORT"
        _entry_obs = self._entry_observation_metrics(sig_state, side_hint=_entry_side_for_audit)
        self._append_csv_row(self._signal_log_path, [
            canonical_candle.t.strftime("%Y-%m-%d"), canonical_candle.t.strftime("%H:%M:%S"), self.live_code, self.position_side, self.position_qty, self.last_signal, self.last_signal_reason,
            self._fmt_num(self.last_z_1m, 6), self._fmt_num(self.last_z_5m, 6), self._fmt_num(self.last_z_30m, 6), self.last_regime,
            self._fmt_num(self._signed_exit_z(), 4), self.last_exit_ok,
            int(bool(sig_state.get("short_entry_blocked", False))), str(sig_state.get("short_block_reason") or ""),
            self._fmt_num(_warmup_seed_z5, 6), self._fmt_num(_prev_z5_sig, 6), int(_prev_z5_valid), int(_continuity_valid), self._fmt_num(_dz5_sig, 6),
            self._fmt_num(sig_state.get("dz5_long"), 6), self._fmt_num(sig_state.get("dz5_short"), 6),
            self._fmt_num(sig_state.get("prev_dz5_long"), 6), self._fmt_num(sig_state.get("prev_dz5_short"), 6),
            self._fmt_num(_dz5_low_anchor, 6), self._fmt_num(_dz5_high_anchor, 6),
            self._fmt_num(sig_state.get("z5_low_5"), 6), self._fmt_num(sig_state.get("z5_high_5"), 6),
            _continuity_reset_reason, _continuity_seed_reason, _reentry_mode,
            dz5_commit_mode, int(bool(update_prev_state)),
            int(bool(sig_state.get("long_armed", False))), int(bool(sig_state.get("short_armed", False))),
            int(sig_state.get("long_arm_age", 0) or 0), int(sig_state.get("short_arm_age", 0) or 0),
            int(bool(sig_state.get("long_arm_trigger_hit", False))), int(bool(sig_state.get("short_arm_trigger_hit", False))),
            int(bool(sig_state.get("long_entry_dz5_hit", False))), int(bool(sig_state.get("short_entry_dz5_hit", False))),
            self._fmt_num(sig_state.get("long_arm_z5_low_anchor"), 6), self._fmt_num(sig_state.get("short_arm_z5_high_anchor"), 6),
            self._fmt_num(sig_state.get("long_arm_threshold"), 6), self._fmt_num(sig_state.get("long_fire_threshold"), 6), self._fmt_num(sig_state.get("long_fire_value"), 6),
            self._fmt_num(sig_state.get("short_arm_threshold"), 6), self._fmt_num(sig_state.get("short_fire_threshold"), 6), self._fmt_num(sig_state.get("short_fire_value"), 6),
            int(bool(sig_state.get("long_entry_direction_ok", False))), int(bool(sig_state.get("short_entry_direction_ok", False))),
            int(bool(sig_state.get("long_band_ok", False))), int(bool(sig_state.get("short_band_ok", False))),
            int(bool(sig_state.get("long_entry_ready", False))), int(bool(sig_state.get("short_entry_ready", False))),
            str(sig_state.get("z5_band_state") or ""), int(bool(sig_state.get("static_z5_band_enabled_for_entry", False))),
            str(sig_state.get("entry_reason") or ""), str(sig_state.get("entry_type") or ""),
            self._fmt_num(sig_state.get("price_dist_prev5_high"), 4), self._fmt_num(sig_state.get("price_dist_prev5_low"), 4),
            self._fmt_num(sig_state.get("price_dist_prev10_high"), 4), self._fmt_num(sig_state.get("price_dist_prev10_low"), 4),
            self._fmt_num(sig_state.get("entry_price_range_pt"), 4), int(bool(sig_state.get("entry_price_range_ok", False))),
            int(bool(sig_state.get("long_ok", False))), int(bool(sig_state.get("short_ok", False))),
            int(bool(sig_state.get("reverse_long_ok", False))), int(bool(sig_state.get("reverse_short_ok", False))),
            int(bool(sig_state.get("reverse_long_entry_gate", False))), int(bool(sig_state.get("reverse_short_entry_gate", False))),
            int(bool(sig_state.get("exit_z_tp_enabled", False))), int(bool(sig_state.get("exit_z_sl_enabled", False))),
            str(sig_state.get("final_signal") or "NONE"), int(bool(allow_lane)),
            str(self.last_block_reason or block_reason or sig_state.get("block_reason") or ""),
            str(sig_state.get("arm_cancel_reason") or ""),
            self._fmt_num(sig_state.get("entry_price"), 4), self._fmt_num(canonical_candle.c, 4),
            self._fmt_num(sig_state.get("current_pnl_pt"), 4), self._fmt_num(sig_state.get("holding_seconds"), 1),
            self._fmt_num(sig_state.get("exit_z_min_hold_sec"), 1), int(bool(sig_state.get("exit_z_time_gate_ok", False))), str(sig_state.get("exit_z_block_reason") or ""),
            self._fmt_num(sig_state.get("peak_price"), 4), self._fmt_num(sig_state.get("trough_price"), 4),
            self._fmt_num(sig_state.get("mfe_pt"), 4), self._fmt_num(sig_state.get("mae_pt"), 4),
            self._fmt_num(sig_state.get("retracement_pt"), 4), str(sig_state.get("mfe_stage") or ""),
            int(bool(sig_state.get("fixed_loss_hit", False))),
            int(bool(sig_state.get("reverse_signal_raw", False))), int(bool(sig_state.get("reverse_signal_allowed", False))),
            int(bool(sig_state.get("mfe_protect_hit", False))), self._fmt_num(sig_state.get("mfe_protect_exit_price"), 4),
            int(bool(sig_state.get("exit_z_raw", False))), int(bool(sig_state.get("exit_z_allowed", False))),
            int(bool(sig_state.get("mfe_trail_hit", False))),
            str(sig_state.get("exit_candidates") or ""), str(sig_state.get("selected_exit_reason") or ""),
            str(_entry_obs.get("entry_side") or ""),
            str(_entry_obs.get("entry_basis") or ""),
            str(_entry_obs.get("entry_hl_source") or ""),
            str(_entry_obs.get("entry_hit_mode") or ""),
            int(bool(_entry_obs.get("entry_cross_through", False))),
            str(_entry_obs.get("entry_validation") or ""),
            self._fmt_num(_entry_obs.get("confirmed5_high"), 4), self._fmt_num(_entry_obs.get("confirmed5_low"), 4),
            self._fmt_num(_entry_obs.get("sample_high"), 4), self._fmt_num(_entry_obs.get("sample_low"), 4),
            self._fmt_num(_entry_obs.get("effective_high"), 4), self._fmt_num(_entry_obs.get("effective_low"), 4),
            self._fmt_num(_entry_obs.get("arm_anchor"), 4), self._fmt_num(_entry_obs.get("arm_level"), 4),
            self._fmt_num(_entry_obs.get("fire_band_min"), 4), self._fmt_num(_entry_obs.get("fire_band_max"), 4),
            self._fmt_num(_entry_obs.get("prev_price"), 4), self._fmt_num(_entry_obs.get("current_price"), 4),
            self._fmt_num(_entry_obs.get("trend_confirmed5_high"), 4), self._fmt_num(_entry_obs.get("trend_confirmed5_low"), 4),
            int(bool(_entry_obs.get("trend_used_currentbar_hl", False))),
            self._fmt_num(_entry_obs.get("trend_arm_level"), 4), self._fmt_num(_entry_obs.get("trend_fire_min"), 4), self._fmt_num(_entry_obs.get("trend_fire_max"), 4),
            str(_entry_obs.get("trend_hit_mode") or ""), int(bool(_entry_obs.get("trend_cross_through", False))),
            self._fmt_num(_entry_obs.get("reversal_confirmed5_high"), 4), self._fmt_num(_entry_obs.get("reversal_confirmed5_low"), 4),
            self._fmt_num(_entry_obs.get("reversal_sample_high"), 4), self._fmt_num(_entry_obs.get("reversal_sample_low"), 4),
            self._fmt_num(_entry_obs.get("reversal_effective_high"), 4), self._fmt_num(_entry_obs.get("reversal_effective_low"), 4),
            int(_entry_obs.get("reversal_sample_interval_sec") or 0), (_entry_obs.get("reversal_sample_countdown_sec") if _entry_obs.get("reversal_sample_countdown_sec") is not None else ""),
            int(bool(_entry_obs.get("reversal_new_high_hit", False))), int(bool(_entry_obs.get("reversal_new_low_hit", False))),
            self._fmt_num(_entry_obs.get("reversal_arm_anchor"), 4), self._fmt_num(_entry_obs.get("reversal_fire_min"), 4), self._fmt_num(_entry_obs.get("reversal_fire_max"), 4),
            str(_entry_obs.get("reversal_hit_mode") or ""), int(bool(_entry_obs.get("reversal_cross_through", False))),
            self._fmt_num(sig_state.get("sma10s_5"), 4), self._fmt_num(sig_state.get("sma10s_10"), 4), self._fmt_num(sig_state.get("sma10s_20"), 4),
            str(sig_state.get("sma10s_relation") or ""), str(sig_state.get("sma10s_regime") or ""),
            str(sig_state.get("sma10s_source") or ""), int(bool(sig_state.get("sma10s_is_approx", False))), str(sig_state.get("sma10s_bucket_time") or ""),
            str(sig_state.get("sma10s_event") or ""), self._fmt_num(sig_state.get("sma10s_event_price"), 4),
            self._fmt_num(sig_state.get("sma10s_event_previous_price"), 4), self._fmt_num(sig_state.get("sma10s_event_previous_line"), 4),
            self._fmt_num(sig_state.get("sma10s_event_current_line"), 4), str(sig_state.get("sma10s_touch_direction") or ""),
            self._fmt_num(sig_state.get("sma1m_5"), 4), self._fmt_num(sig_state.get("sma1m_10"), 4), self._fmt_num(sig_state.get("sma1m_20"), 4),
            str(sig_state.get("sma1m_relation") or ""), str(sig_state.get("sma1m_regime") or ""),
            str(sig_state.get("sma1m_source") or ""), str(sig_state.get("sma1m_event") or ""),
            self._fmt_num(sig_state.get("sma1m_event_price"), 4), self._fmt_num(sig_state.get("sma1m_event_line"), 4),
            self._fmt_num(sig_state.get("sma1m_event_offset"), 4), str(sig_state.get("sma1m_touch_direction") or ""),
        ])

        if self._maybe_retry_reverse_after_exit():
            self._log_live_dedupe_diag(force=False)
            self.refresh_view()
            return

        if not sig:
            self._log_live_dedupe_diag(force=False)
            self.refresh_view()
            return

        if not self.order_core:
            self._log_live_dedupe_diag(force=False)
            self.refresh_view()
            return

        if not self.auto_on:
            side = str((sig or {}).get("side") or "")
            # AUTO OFF hard block:
            # never submit signal-based EXIT/ENTRY orders while AUTO is disabled.
            # (manual EXIT button remains available via on_exit_clicked -> MANUAL_EXIT)
            if in_pos != 0 and side in ("EXIT_LONG", "EXIT_SHORT") and self.position_qty > 0:
                exit_reason = str((sig or {}).get('reason') or side)
                block_key = (canonical_candle.t.strftime("%Y-%m-%d %H:%M:%S"), side, "AUTO_OFF")
                if self._last_exit_block_log_key != block_key:
                    self._last_exit_block_log_key = block_key
                    self.log("SIGNAL", "EXIT_BLOCKED", f"reason=AUTO_OFF side={side} exit_reason={exit_reason}")
            elif side in ("BUY", "SELL_SHORT"):
                lane_reason = "AUTO_OFF"
                block_key = (canonical_candle.t.strftime("%Y-%m-%d %H:%M:%S"), side, lane_reason)
                if self._last_entry_block_log_key != block_key:
                    self._last_entry_block_log_key = block_key
                    self.log("SIGNAL", "ENTRY_BLOCKED", f"reason={lane_reason} side={side}")
                self._log_entry_blocked_before_submit(side, lane_reason, sig, sig_state, canonical_candle.t)
            self._log_live_dedupe_diag(force=False)
            self.refresh_view()
            return

        side = str(sig.get("side") or "")
        if self.order_core.pending.active:
            if side in ("BUY", "SELL_SHORT"):
                self._log_entry_blocked_before_submit(side, "ORDER_CORE_PENDING_ACTIVE", sig, sig_state, canonical_candle.t)
            self._log_live_dedupe_diag(force=False)
            self.refresh_view()
            return
        if side in ("BUY", "SELL_SHORT") and bool(getattr(self, "_forced_reverse_entry_pending", False)):
            forced_side = str(getattr(self, "_forced_reverse_entry_side", "") or "").strip().upper()
            side_norm = ("LONG" if side == "BUY" else "SHORT")
            if (not forced_side) or forced_side == side_norm:
                self.log("SIGNAL", "ENTRY_BLOCKED", f"reason=FORCED_REVERSE_PENDING side={side}")
                self._log_entry_blocked_before_submit(side, "FORCED_REVERSE_PENDING", sig, sig_state, canonical_candle.t)
                self._log_live_dedupe_diag(force=False)
                self.refresh_view()
                return
        if not allow_lane and side in ("BUY", "SELL_SHORT"):
            if self.auto_on:
                self.log("SIGNAL", "ENTRY_BLOCKED", f"reason={self.last_block_reason} side={side}")
                self._log_entry_blocked_before_submit(side, self.last_block_reason or "LANE_BLOCKED", sig, sig_state, canonical_candle.t)
            self._log_live_dedupe_diag(force=False)
            self.refresh_view()
            return

        if side in ("BUY", "LONG", "SELL_SHORT", "SHORT", "SELL"):
            local_pos_active = bool(str(self.position_side or "FLAT").upper() in ("LONG", "SHORT") and int(self.position_qty or 0) > 0)
            active_pos_known = bool(str(self.active_position_side or "FLAT").upper() in ("LONG", "SHORT") and int(self.active_position_qty or 0) > 0)
            if local_pos_active or active_pos_known:
                hold_side = str(self.position_side or self.active_position_side or "FLAT").upper()
                hold_qty = int(self.position_qty or self.active_position_qty or 0)
                block_reason = f"POSITION_ALREADY_HELD_{hold_side}_{hold_qty}"
                self.log("SIGNAL", "ENTRY_BLOCKED", f"reason={block_reason} side={side}")
                self._log_entry_blocked_before_submit(side, block_reason, sig, sig_state, canonical_candle.t)
                self._log_live_dedupe_diag(force=False)
                self.refresh_view()
                return

        if in_pos == 0 and side in ("BUY", "LONG", "SELL_SHORT", "SHORT", "SELL"):
            def _v9_float(_value):
                try:
                    if _value is None:
                        return None
                    return float(_value)
                except Exception:
                    return None

            _sig_state = sig_state if isinstance(sig_state, dict) else {}
            z5_now = None
            for _candidate in (
                _sig_state.get("z5"),
                _sig_state.get("z5_now"),
                _sig_state.get("current_z5"),
                getattr(self, "last_z_5m", None),
            ):
                z5_now = _v9_float(_candidate)
                if z5_now is not None:
                    break

            trend_floor = _v9_float(getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MIN"))
            trend_max = _v9_float(getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MAX"))
            use_trend_z5_zone_opposite_entry_block_v9 = bool(
                getattr(self.cfg, "USE_TREND_Z5_ZONE_OPPOSITE_ENTRY_BLOCK_V9", False)
            )
            if (
                not bool(_sig_state.get("use_sma_cross_entry", getattr(self.cfg, "USE_SMA_CROSS_ENTRY", False)))
                and
                use_trend_z5_zone_opposite_entry_block_v9
                and (
                    z5_now is not None
                    and trend_floor is not None
                    and trend_max is not None
                )
            ):
                trend_floor = abs(float(trend_floor))
                trend_max = abs(float(trend_max))
                if trend_floor > trend_max:
                    trend_floor, trend_max = trend_max, trend_floor

                upper_trend_zone = bool(
                    z5_now >= trend_floor and z5_now <= trend_max
                )
                lower_trend_zone = bool(
                    z5_now <= -trend_floor and z5_now >= -trend_max
                )

                block_reason = None
                if side in ("SELL_SHORT", "SHORT", "SELL") and upper_trend_zone:
                    block_reason = "TREND_Z5_ZONE_SHORT_BLOCK_V9"
                    _sig_state["short_entry_ready"] = False
                    _sig_state["short_ok"] = False
                    _sig_state["short_entry_blocked"] = True
                    _sig_state["short_block_reason"] = block_reason
                elif side in ("BUY", "LONG") and lower_trend_zone:
                    block_reason = "TREND_Z5_ZONE_LONG_BLOCK_V9"
                    _sig_state["long_entry_ready"] = False
                    _sig_state["long_ok"] = False
                    _sig_state["long_entry_blocked"] = True
                    _sig_state["long_block_reason"] = block_reason

                if block_reason:
                    self.log("SIGNAL", "ENTRY_BLOCKED", f"reason={block_reason} side={side} z5={z5_now:+.3f}")
                    self._log_entry_blocked_before_submit(side, block_reason, sig, sig_state, canonical_candle.t)
                    self._log_live_dedupe_diag(force=False)
                    self.refresh_view()
                    return

        if in_pos == 0:
            if side == "BUY":
                arm_gate_ok, arm_gate_reason = self._entry_arm_gate_state(sig_state, "BUY")
                self.log(
                    "SIGNAL",
                    "ENTRY_CHECK",
                    (
                        f"side=BUY allow={int(bool(arm_gate_ok))} reason={arm_gate_reason} "
                        f"armed={int(bool(sig_state.get('long_armed', False)))} "
                        f"fire_hit={int(bool(sig_state.get('entry_price_fire_long_hit', False)))} "
                        f"dir={int(bool(sig_state.get('long_entry_direction_ok', False)))} "
                        f"ready={int(bool(sig_state.get('long_entry_ready', False)))} "
                        f"ok={int(bool(sig_state.get('long_ok', False)))}"
                    ),
                )
                if not arm_gate_ok:
                    self.log("SIGNAL", "ENTRY_BLOCKED", f"reason={arm_gate_reason} side=BUY")
                    self._log_entry_blocked_before_submit("BUY", arm_gate_reason, sig, sig_state, canonical_candle.t)
                    self._log_live_dedupe_diag(force=False)
                    self.refresh_view()
                    return
                if self.last_allow_trade and str(self.auto_cleanup_phase or "AUTO_WAIT_SIGNAL") == "AUTO_WAIT_SIGNAL" and (not self.server_sync_pending):
                    self.log("SIGNAL", "ENTRY_ALLOWED", "reason=FLAT_AND_CLEAR side=BUY")
                try:
                    reversal_context = self._reversal_fire_order_context(sig, sig_state, "LONG")
                    ok = self.order_core.submit_long(
                        self.order_qty,
                        reason=f"AUTO_ENTRY_LONG/{self.last_signal_reason}",
                        signal_context=reversal_context,
                        trigger_price=self.current_price,
                        trigger_ts=canonical_candle.t.isoformat(),
                    )
                    self._remember_entry_order_audit(bool(ok), "LONG", f"AUTO_ENTRY_LONG/{self.last_signal_reason}")
                except Exception as e:
                    self._remember_entry_order_audit(False, "LONG", f"AUTO_ENTRY_LONG/{self.last_signal_reason}")
                    self.log("SIGNAL", "ENTRY_SUBMIT_FAILED", f"side=BUY reason=SUBMIT_EXCEPTION error={e}")
                    self._log_entry_blocked_before_submit("BUY", "SUBMIT_EXCEPTION", sig, sig_state, canonical_candle.t)
                    self._log_live_dedupe_diag(force=False)
                    self.refresh_view()
                    return
                if ok:
                    self._last_entry_submit_ts = time.time()
                    self._last_entry_submit_side = "LONG"
                    self._remember_entry_submit_metrics("LONG", sig_state)
                    self.entry_inflight = True
                    self.pending_entry_side = "LONG"
                    self.order_lane_locked = True
                    self.flat_confirmed = False
                    self._entry_order_guard_active = True
                    self._entry_order_guard_side = "LONG"
                    self._entry_order_guard_ts = time.time()
                    self._clear_reverse_retry()
                    self._refresh_lane_snapshot()
                    try:
                        self.st._clear_arms("ENTRY_LONG_SUBMITTED")
                    except Exception:
                        pass
                else:
                    self.log("SIGNAL", "ENTRY_SUBMIT_FAILED", "side=BUY reason=SUBMIT_RETURN_FALSE")
                    self._log_entry_blocked_before_submit("BUY", "SUBMIT_RETURN_FALSE", sig, sig_state, canonical_candle.t)
                self.log("SIGNAL", "LONG", f"AUTO LONG {'전송' if ok else '무시'}")
            elif side == "SELL_SHORT":
                arm_gate_ok, arm_gate_reason = self._entry_arm_gate_state(sig_state, "SELL_SHORT")
                self.log(
                    "SIGNAL",
                    "ENTRY_CHECK",
                    (
                        f"side=SELL_SHORT allow={int(bool(arm_gate_ok))} reason={arm_gate_reason} "
                        f"armed={int(bool(sig_state.get('short_armed', False)))} "
                        f"fire_hit={int(bool(sig_state.get('entry_price_fire_short_hit', False)))} "
                        f"dir={int(bool(sig_state.get('short_entry_direction_ok', False)))} "
                        f"ready={int(bool(sig_state.get('short_entry_ready', False)))} "
                        f"ok={int(bool(sig_state.get('short_ok', False)))}"
                    ),
                )
                if not arm_gate_ok:
                    self.log("SIGNAL", "ENTRY_BLOCKED", f"reason={arm_gate_reason} side=SELL_SHORT")
                    self._log_entry_blocked_before_submit("SELL_SHORT", arm_gate_reason, sig, sig_state, canonical_candle.t)
                    self._log_live_dedupe_diag(force=False)
                    self.refresh_view()
                    return
                if self.last_allow_trade and str(self.auto_cleanup_phase or "AUTO_WAIT_SIGNAL") == "AUTO_WAIT_SIGNAL" and (not self.server_sync_pending):
                    self.log("SIGNAL", "ENTRY_ALLOWED", "reason=FLAT_AND_CLEAR side=SELL_SHORT")
                try:
                    reversal_context = self._reversal_fire_order_context(sig, sig_state, "SHORT")
                    ok = self.order_core.submit_short(
                        self.order_qty,
                        reason=f"AUTO_ENTRY_SHORT/{self.last_signal_reason}",
                        signal_context=reversal_context,
                        trigger_price=self.current_price,
                        trigger_ts=canonical_candle.t.isoformat(),
                    )
                    self._remember_entry_order_audit(bool(ok), "SHORT", f"AUTO_ENTRY_SHORT/{self.last_signal_reason}")
                except Exception as e:
                    self._remember_entry_order_audit(False, "SHORT", f"AUTO_ENTRY_SHORT/{self.last_signal_reason}")
                    self.log("SIGNAL", "ENTRY_SUBMIT_FAILED", f"side=SELL_SHORT reason=SUBMIT_EXCEPTION error={e}")
                    self._log_entry_blocked_before_submit("SELL_SHORT", "SUBMIT_EXCEPTION", sig, sig_state, canonical_candle.t)
                    self._log_live_dedupe_diag(force=False)
                    self.refresh_view()
                    return
                if ok:
                    self._last_entry_submit_ts = time.time()
                    self._last_entry_submit_side = "SHORT"
                    self._remember_entry_submit_metrics("SHORT", sig_state)
                    self.entry_inflight = True
                    self.pending_entry_side = "SHORT"
                    self.order_lane_locked = True
                    self.flat_confirmed = False
                    self._entry_order_guard_active = True
                    self._entry_order_guard_side = "SHORT"
                    self._entry_order_guard_ts = time.time()
                    self._clear_reverse_retry()
                    self._refresh_lane_snapshot()
                    try:
                        self.st._clear_arms("ENTRY_SHORT_SUBMITTED")
                    except Exception:
                        pass
                else:
                    self.log("SIGNAL", "ENTRY_SUBMIT_FAILED", "side=SELL_SHORT reason=SUBMIT_RETURN_FALSE")
                    self._log_entry_blocked_before_submit("SELL_SHORT", "SUBMIT_RETURN_FALSE", sig, sig_state, canonical_candle.t)
                self.log("SIGNAL", "SHORT", f"AUTO SHORT {'전송' if ok else '무시'}")
        else:
            if side in ("EXIT_LONG", "EXIT_SHORT") and self.position_qty > 0:
                exit_reason = str((sig or {}).get('reason') or side)
                exit_rule = str((sig or {}).get("rule") or "")
                exit_source = str(sig_state.get("exit_z_source", "price") or "price").upper()
                exit_signal_val = sig_state.get("exit_z_signal")
                exit_z_long = sig_state.get("exit_z_long")
                exit_z_short = sig_state.get("exit_z_short")
                exit_z_long_entry = sig_state.get("exit_z_long_entry")
                exit_z_short_entry = sig_state.get("exit_z_short_entry")
                exit_delta = None
                entry_z5_anchor = None
                exit_threshold = sig_state.get("exit_z_threshold_used", (exit_z_long if self.position_side == "LONG" else exit_z_short))
                exit_z_trigger_mode = str(sig_state.get("exit_z_trigger_mode") or "")
                self._last_exit_submit_context = {
                    "date": canonical_candle.t.strftime("%Y-%m-%d"),
                    "time": canonical_candle.t.strftime("%H:%M:%S"),
                    "code": self.live_code,
                    "position_side": str(self.position_side or ""),
                    "position_qty": int(self.position_qty or 0),
                    "entry_price": float(self.entry_price or 0.0),
                    "entry_z5": None,
                    "exit_reason": exit_reason,
                    "exit_rule": exit_rule,
                    "exit_source": exit_source,
                    "exit_signal": (None if exit_signal_val is None else float(exit_signal_val)),
                    "exit_threshold": (None if exit_threshold is None else float(exit_threshold)),
                    "exit_z_long": (None if exit_z_long is None else float(exit_z_long)),
                    "exit_z_short": (None if exit_z_short is None else float(exit_z_short)),
                    "exit_z_long_entry": (None if exit_z_long_entry is None else float(exit_z_long_entry)),
                    "exit_z_short_entry": (None if exit_z_short_entry is None else float(exit_z_short_entry)),
                    "exit_delta": (None if exit_delta is None else float(exit_delta)),
                    "exit_z_trigger_mode": exit_z_trigger_mode,
                    "entry_z5_anchor": (None if entry_z5_anchor is None else float(entry_z5_anchor)),
                    "bar_high": float(canonical_candle.h),
                    "bar_low": float(canonical_candle.l),
                    "bar_close": float(canonical_candle.c),
                }
                _exit_trace_row = [
                    canonical_candle.t.strftime("%Y-%m-%d"),
                    canonical_candle.t.strftime("%H:%M:%S"),
                    "EXIT_SIGNAL",
                    self.live_code,
                    str(self.position_side or ""),
                    int(self.position_qty or 0),
                    self._fmt_num(self.entry_price, 2),
                    "",
                    exit_reason,
                    exit_rule,
                    exit_source,
                    self._fmt_num(exit_signal_val, 6),
                    self._fmt_num(exit_threshold, 6),
                    self._fmt_num(exit_z_long, 6),
                    self._fmt_num(exit_z_short, 6),
                    self._fmt_num(exit_delta, 6),
                    self._fmt_num(canonical_candle.h, 2),
                    self._fmt_num(canonical_candle.l, 2),
                    self._fmt_num(canonical_candle.c, 2),
                    "",
                    "",
                ]
                # 주문 전송을 먼저 수행하고, 진단용 EXIT_SIGNAL 기록은 UI 루프 뒤로 미룬다.
                ok = self._submit_exit_cleanup(exit_reason)
                try:
                    QTimer.singleShot(0, lambda row=list(_exit_trace_row): self._append_csv_row(self._exit_trace_path, row))
                except Exception:
                    self._append_csv_row(self._exit_trace_path, _exit_trace_row)
                if ok:
                    if str(exit_reason or "").strip().upper() == "REVERSE_SIGNAL":
                        # Preserve the exact signal snapshot that generated the
                        # reverse EXIT.  Do not let a later live tick overwrite
                        # LONG/SHORT reverse readiness before EXIT_FILLED arrives.
                        self._forced_reverse_entry_side = ("SHORT" if str(self.position_side or "").upper() == "LONG" else "LONG")
                        self._forced_reverse_entry_sig_state = dict(sig_state or {}) if isinstance(sig_state, dict) else {}
                        self.log(
                            "SIGNAL",
                            "REVERSE_ENTRY_SNAPSHOT",
                            (
                                f"prior={self.position_side or '-'} target={self._forced_reverse_entry_side or '-'} "
                                f"long_rev={int(bool(self._forced_reverse_entry_sig_state.get('reverse_long_entry_gate', False)))} "
                                f"short_rev={int(bool(self._forced_reverse_entry_sig_state.get('reverse_short_entry_gate', False)))}"
                            ),
                        )
                    self._queue_post_exit_overlap_entry(str(self.position_side or "FLAT"), exit_reason, sig_state)
                else:
                    self._clear_post_exit_overlap_entry()
                    if str(exit_reason or "").strip().upper() == "REVERSE_SIGNAL":
                        self._clear_forced_reverse_pending()
                mfe_suffix = ""
                if str(exit_reason or "").upper() == "MFE_TRAIL":
                    _mfe = sig_state.get("mfe_retrace") if isinstance(sig_state, dict) else None
                    if isinstance(_mfe, dict):
                        try:
                            _mfe_pt = _mfe.get("mfe_pt")
                            _cut = _mfe.get("cut_price")
                            _gap = _mfe.get("required_retracement_pt", _mfe.get("retrace_gap_pt"))
                            _stage = str(_mfe.get("mfe_stage") or "-")
                            _min_pnl = _mfe.get("min_profit_pt")
                            mfe_suffix = (
                                f" mfe[mfe={self._fmt_num(_mfe_pt, 2)} cut={self._fmt_num(_cut, 2)} "
                                f"stage={_stage} gap={self._fmt_num(_gap, 2)} minpnl={self._fmt_num(_min_pnl, 2)}]"
                            )
                        except Exception:
                            mfe_suffix = ""
                _skip_diag = ""
                if not ok:
                    try:
                        _skip_diag = (
                            f" skip[connected={int(bool(self.connected))}"
                            f" account={int(bool(self.account))}"
                            f" server_pos={int(bool(self._has_server_position()))}"
                            f" server_unfilled={int(bool(self.has_server_unfilled))}"
                            f" entry_inflight={int(bool(self.entry_inflight))}"
                            f" exit_inflight={int(bool(self.exit_inflight))}"
                            f" close_pending={int(bool(self.position_close_pending))}"
                            f" needs_server_check={int(bool(self.exit_needs_server_check))}"
                            f" pending={int(bool(getattr(getattr(self.order_core, 'pending', None), 'active', False)))}"
                            f" pending_action={str(getattr(getattr(self.order_core, 'pending', None), 'action', '') or '-')}"
                            f"]"
                        )
                    except Exception:
                        _skip_diag = ""
                _exit_log_msg = (
                    f"AUTO EXIT {'전송' if ok else '무시'} reason={exit_reason} "
                    f"src={exit_source} price={self._fmt_num(exit_signal_val, 4)} "
                    f"thr={self._fmt_num(exit_threshold, 4)} "
                    f"(L={self._fmt_num(exit_z_long, 4)}/S={self._fmt_num(exit_z_short, 4)}/mode={exit_z_trigger_mode or '-'})"
                    f"{mfe_suffix}{_skip_diag}"
                )
                _should_log_exit = True
                if (not ok) and (bool(self.has_server_unfilled) or self._exit_pending_active_for_submit_guard() or bool(self.exit_needs_server_check)):
                    try:
                        _pending_action = str(getattr(getattr(self.order_core, "pending", None), "action", "") or "-")
                    except Exception:
                        _pending_action = "-"
                    _exit_skip_key = (
                        f"{str(exit_reason or '').strip().upper()}|"
                        f"{self.server_unfilled_order_no or '-'}|{int(self.server_unfilled_qty or 0)}|"
                        f"{int(bool(self.exit_needs_server_check))}|{int(bool(self.cancel_in_progress))}|"
                        f"{int(bool(self.exit_inflight))}|{int(bool(self.position_close_pending))}|{_pending_action}"
                    )
                    _now_ts = time.time()
                    if (
                        _exit_skip_key == str(getattr(self, "_auto_exit_skip_log_last_key", "") or "")
                        and (_now_ts - float(getattr(self, "_auto_exit_skip_log_last_ts", 0.0) or 0.0) < 1.5)
                    ):
                        _should_log_exit = False
                    else:
                        self._auto_exit_skip_log_last_key = _exit_skip_key
                        self._auto_exit_skip_log_last_ts = _now_ts
                if _should_log_exit:
                    self.log("SIGNAL", "EXIT", _exit_log_msg)
        self._log_live_dedupe_diag(force=False)
        self.refresh_view()

    def compute_zscore(self, price: float) -> float | None:
        try:
            if bool(getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)):
                return None
            src = str(getattr(self.cfg, "ENTRY_SIGNAL_SOURCE", "z1") or "z1").strip().lower()
            key = "z1" if src == "z1" else "z5"
            return self.st.get_realtime_z_scores(float(price), datetime.now()).get(key)
        except Exception:
            return None

    # ---------- periodic timer ----------
    def on_timer(self) -> None:
        now_ts = time.time()
        notifier = getattr(self, "_telegram_notifier", None)
        if notifier is not None:
            notifier.drain_results()
        self._check_login_wait(now_ts)
        self._retry_unknown_order_confirmation(now_ts)
        if self.order_core:
            now_dt = datetime.now()
            today_key = now_dt.strftime("%Y-%m-%d")
            if str(getattr(self, "_eod_force_exit_last_date", "") or "") != today_key:
                self._eod_force_exit_last_date = today_key
                self._eod_force_exit_last_try_ts = 0.0
            if str(getattr(self, "_eod_force_exit_submitted_date", "") or "") != today_key:
                self._eod_force_exit_submitted_date = ""
            if (
                self._is_eod_force_exit_due(now_dt)
                and self.connected
                and bool(self.account)
                and self.position_side in ("LONG", "SHORT")
                and int(self.position_qty or 0) > 0
                and (not self.exit_in_progress)
                and (not self.exit_inflight)
                and (not self.position_close_pending)
                and (str(getattr(self, "_eod_force_exit_submitted_date", "") or "") != today_key)
                and (now_ts - float(getattr(self, "_eod_force_exit_last_try_ts", 0.0) or 0.0) >= 3.0)
            ):
                self._eod_force_exit_last_try_ts = now_ts
                self._clear_reverse_retry()
                self.log("SYNC", "EOD_FORCE_EXIT", f"time={now_dt.strftime('%H:%M:%S')} side={self.position_side} qty={self.position_qty}")
                ok = self._submit_exit_cleanup("EOD_FORCE_EXIT")
                if ok:
                    self._eod_force_exit_submitted_date = today_key
                self.log("SYNC", "EOD_FORCE_EXIT_SUBMIT", f"ok={ok}")
            self.order_core.check_auto_cancel(now_ts)
            self.order_core.check_timeout(now_ts, timeout_sec=3.0)
            self._maybe_request_exit_pending_recheck("TIMER")
            quiet_fixed_loss_cd = self._is_quiet_fixed_loss_cooldown_window()
            if (
                not quiet_fixed_loss_cd
                and not self.warmup_loading
                and not self._warmup_retry_pending
            ):
                # Keep bar dedupe/tick handling decoupled from TR query traffic.
                needs_unfilled_sync = self._should_query_kis_open_orders("TIMER_RECHECK")
                needs_takeover_sync = self._should_query_kis_balance("TIMER_RECHECK")
                if (needs_unfilled_sync or needs_takeover_sync) and self.connected and bool(self.account) and (not self.unfilled_inflight) and (not self.takeover_inflight):
                    if now_ts - float(self._last_server_sync_request_ts or 0.0) >= float(self._server_sync_min_interval_sec or 0.0):
                        self._request_server_sync(reason='TIMER_RECHECK', include_takeover=needs_takeover_sync, include_unfilled=needs_unfilled_sync, start_warmup_after_response=False, force=False)
            self._maybe_auto_reconnect()

        if (
            self.connected
            and not self.real_registered
            and not self.warmup_loading
            and not self._warmup_retry_pending
        ):
            self.ensure_real_registered()

        self.last_market_status = self._market_status_text()
        if self.auto_on:
            self._advance_auto_cleanup()
        self.refresh_view()

    # ---------- view model ----------
    def refresh_view(self, force: bool = False) -> None:
        try:
            now_ts = time.time()
            min_interval = float(getattr(self, "_view_refresh_min_interval_sec", 0.0) or 0.0)
            if (not force) and min_interval > 0.0 and now_ts - float(getattr(self, "_last_view_refresh_ts", 0.0) or 0.0) < min_interval:
                return
            self._last_view_refresh_ts = now_ts
            lookback = int(self.cfg.LOOKBACK)
            counts = self.st.get_counts()
            sig_state = self.last_sig_state if isinstance(self.last_sig_state, dict) else {}
            block_reason = str(self.last_block_reason or "-")
            warmup_ready = self.warmup_done or (counts.get('bars_5m', 0) >= lookback)
            warmup_state = '워밍업중' if self.warmup_loading else ('워밍업완료' if warmup_ready else '워밍업대기')
            self._refresh_cleanup_statuses()
            try:
                _live_open_price, _live_open_date = self._resolve_session_open_from_live_cache()
                if _live_open_date is not None and float(_live_open_price or 0.0) > 0.0:
                    self.session_open_date = _live_open_date
                    self.session_open_price = float(_live_open_price)
                    self.session_open_confirmed = True
            except Exception:
                pass

            z1_val = sig_state.get("z1", self.last_z_1m)
            z5_val = sig_state.get("z5", self.last_z_5m)
            z30_val = sig_state.get("z30", self.last_z_30m)
            z1_text = "-" if (z1_val is None) else f"{float(z1_val):+.3f}"
            z5_text = "-" if (z5_val is None) else f"{float(z5_val):+.3f}"
            z30_text = "-" if (z30_val is None) else f"{float(z30_val):+.3f}"
            try:
                _price_entry_mode_disp = bool(sig_state.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
            except Exception:
                _price_entry_mode_disp = bool(getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False))
            _entry_src_cfg = "price" if _price_entry_mode_disp else str(getattr(self.cfg, "ENTRY_SIGNAL_SOURCE", "z1") or "z1").lower()
            _exit_src_cfg = "price"
            entry_sig_val = None if _entry_src_cfg == "price" else (z1_val if _entry_src_cfg == "z1" else z5_val)
            exit_sig_val = sig_state.get("exit_z_signal") if isinstance(sig_state, dict) else None
            entry_sig_text = "-" if (entry_sig_val is None) else f"{float(entry_sig_val):+.3f}"
            exit_sig_text = "-" if (exit_sig_val is None) else f"{float(exit_sig_val):+.3f}"

            pos_text, realtime_pnl = self._build_position_text()
            in_pos = 1 if self.position_side == "LONG" and self.position_qty > 0 else (-1 if self.position_side == "SHORT" and self.position_qty > 0 else 0)
            mfe_retrace_info = sig_state.get("mfe_retrace") if isinstance(sig_state, dict) else None
            _mfe_trigger_mode = str(getattr(self.cfg, "EXIT_MFE_TRIGGER_MODE", "CLOSE") or "CLOSE").strip().upper()
            if self.auto_on and in_pos != 0 and self.st is not None and _mfe_trigger_mode == "TOUCH":
                try:
                    _live_px = None
                    for _raw_px in (self.current_price, self.last_bar_close, self.entry_price):
                        try:
                            _v_px = float(_raw_px or 0.0)
                        except Exception:
                            _v_px = 0.0
                        if _v_px > 0.0:
                            _live_px = _v_px
                            break
                    if _live_px is not None:
                        _pos_meta_live = {
                            "side": self.position_side,
                            "qty": self.position_qty,
                            "entry_price": self.entry_price,
                            "entry_time": self.position_entry_time,
                            "entry_z5": self.position_entry_z5,
                            "peak_price": self.position_peak_price,
                            "trough_price": self.position_trough_price,
                            "mfe_armed": bool(self.position_mfe_armed),
                            "mfe_armed_since_ts": float(self.position_mfe_armed_since_ts or 0.0),
                            "mfe_stage_step": int(self.position_mfe_stage_step or 0),
                            # 2번 파일 기준: TOUCH 표시/판정은 실시간 현재가만 사용한다.
                            # 현재봉 high/low를 재사용하면 새 peak와 과거 low가 한 봉 안에서 결합되어
                            # 실제 되돌림 없이 MFE_TRAIL이 선발동할 수 있다.
                            "current_high": float(_live_px),
                            "current_low": float(_live_px),
                            "current_close": float(_live_px),
                            "current_price": float(_live_px),
                            "bar_confirmed": False,
                        }
                        _live_mfe = self.st._mfe_retrace_info(_pos_meta_live, current_time=datetime.now())
                        if isinstance(_live_mfe, dict):
                            mfe_retrace_info = _live_mfe
                            if isinstance(sig_state, dict):
                                sig_state["mfe_retrace"] = dict(_live_mfe)
                                sig_state["mfe_stage"] = str(_live_mfe.get("mfe_stage") or "")
                except Exception:
                    pass
            bar3_left_text = "대기"
            _mfe_arm_min_pt = float(getattr(self.cfg, "EXIT_MFE_MIN_PT", 1.8) or 1.8)

            def _mfe_stage_cfg_value(name: str, default: float) -> float:
                try:
                    return float(getattr(self.cfg, name, default) or default)
                except Exception:
                    return float(default)

            def _fmt_stage_threshold(value: float) -> str:
                try:
                    v = float(value)
                except Exception:
                    return str(value)
                if abs(v - int(v)) < 1e-9:
                    return str(int(v))
                return f"{v:.1f}"

            def _fmt_stage_pct(value: float) -> str:
                try:
                    return f"{float(value) * 100.0:.0f}%"
                except Exception:
                    return "-"

            def _cfg_num(_name, _default):
                try:
                    return float(getattr(self.cfg, _name, _default) or _default)
                except Exception:
                    return float(_default)

            _mfe_stage1_pt = _mfe_stage_cfg_value("EXIT_MFE_STAGE1_PT", _mfe_arm_min_pt)
            _mfe_stage2_pt = _mfe_stage_cfg_value("EXIT_MFE_STAGE2_PT", _mfe_stage_cfg_value("EXIT_MFE_STAGE2_AT_PT", 5.0))
            _mfe_stage3_pt = _mfe_stage_cfg_value("EXIT_MFE_STAGE3_PT", _mfe_stage_cfg_value("EXIT_MFE_STAGE3_AT_PT", 8.0))
            _mfe_stage4_pt = _mfe_stage_cfg_value("EXIT_MFE_STAGE4_PT", _mfe_stage_cfg_value("EXIT_MFE_STAGE4_AT_PT", 11.0))
            _mfe_stage1_pct_min = float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1_MIN", 0.20) or 0.20)
            _mfe_stage1_pct_max = float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1_MAX", 0.25) or 0.25)
            _mfe_stage2_pct = float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2", 0.30) or 0.30)
            _mfe_stage3_pct = float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3", 0.20) or 0.20)
            _mfe_stage4_pct = float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4", 0.10) or 0.10)
            _mfe_stage1_enabled = bool(getattr(self.cfg, "EXIT_MFE_STAGE1_ENABLED", True))
            _mfe_stage1_criteria = (
                f"S1≥{_fmt_stage_threshold(_mfe_stage1_pt)}/{_fmt_stage_pct(_mfe_stage1_pct_min)}~{_fmt_stage_pct(_mfe_stage1_pct_max)}"
                if _mfe_stage1_enabled
                else "S1 OFF"
            )
            _mfe_stage_criteria_text = (
                f"{_mfe_stage1_criteria}    "
                f"S2≥{_fmt_stage_threshold(_mfe_stage2_pt)}/{_fmt_stage_pct(_mfe_stage2_pct)}    "
                f"S3≥{_fmt_stage_threshold(_mfe_stage3_pt)}/{_fmt_stage_pct(_mfe_stage3_pct)}    "
                f"S4≥{_fmt_stage_threshold(_mfe_stage4_pt)}/{_fmt_stage_pct(_mfe_stage4_pct)}"
            )
            bar3_right_text = "PEAK/TROUGH -    /    CUT -"
            bar3_active = None
            _mfe_stage_display = ""
            _mfe_stage_hit = False
            _mfe_stage_armed = False
            _mfe_stage_mfe_pt = None
            _mfe_stage_cut = None
            _mfe_stage_cut_band_low = None
            _mfe_stage_cut_band_high = None
            _mfe_stage_gap = None
            _mfe_stage_pct = None
            _mfe_extreme_label = "PEAK/TROUGH"
            _mfe_extreme_price = None
            if self.auto_on and in_pos != 0 and isinstance(mfe_retrace_info, dict):
                retrace_gap_pt = float(mfe_retrace_info.get("required_retracement_pt", mfe_retrace_info.get("retrace_gap_pt", 0.0)) or 0.0)
                min_profit_pt = float(mfe_retrace_info.get("min_profit_pt", 0.0) or 0.0)
                mfe_stage_label = str(mfe_retrace_info.get("mfe_stage") or "WAIT")
                _mfe_stage_num = ""
                if mfe_stage_label.startswith("STAGE"):
                    _mfe_stage_num = mfe_stage_label.replace("STAGE", "").strip()
                _mfe_stage_display = f"S{_mfe_stage_num}" if _mfe_stage_num else ""
                peak = float(mfe_retrace_info.get("peak_price", self.position_peak_price or self.entry_price or 0.0) or 0.0)
                trough = float(mfe_retrace_info.get("trough_price", self.position_trough_price or self.entry_price or 0.0) or 0.0)
                mfe_pt = float(mfe_retrace_info.get("mfe_pt", 0.0) or 0.0)
                cut = float(mfe_retrace_info.get("cut_price", 0.0) or 0.0)
                retrace_pct = mfe_retrace_info.get("retrace_pct", mfe_retrace_info.get("retrace_pct_stage", None))
                if retrace_pct is None:
                    retrace_pct = mfe_retrace_info.get("required_retracement_pct", None)
                _mfe_stage_mfe_pt = float(mfe_pt)
                _mfe_stage_cut = float(cut) if cut else None
                _mfe_stage_gap = float(retrace_gap_pt)
                try:
                    _mfe_stage_pct = float(retrace_pct)
                except Exception:
                    _mfe_stage_pct = None
                armed = bool(mfe_retrace_info.get("armed", False))
                hit = bool(mfe_retrace_info.get("hit", False))
                _mfe_stage_armed = bool(armed)
                _mfe_stage_hit = bool(hit)
                if in_pos > 0:
                    _mfe_extreme_label = "PEAK"
                    _mfe_extreme_price = float(peak) if peak > 0.0 else None
                elif in_pos < 0:
                    _mfe_extreme_label = "TROUGH"
                    _mfe_extreme_price = float(trough) if trough > 0.0 else None
                _extreme_txt = "-" if _mfe_extreme_price is None else f"{_mfe_extreme_price:.2f}"
                _cut_txt = "-" if cut <= 0.0 else f"{cut:.2f}"
                _stage_band_key = (_mfe_stage_display or "").strip().upper()
                _stage_band_pct_min = None
                _stage_band_pct_max = None
                _stage_band_min_retrace = None
                try:
                    _stage_band_pct_map = {
                        "S1": (
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1_MIN", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1", 0.45)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1", 0.45)),
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1_MAX", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1", 0.45)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE1", 0.45)),
                            float(getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT_STAGE1", getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 0.8)) or getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 0.8)),
                        ),
                        "S2": (
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2_MIN", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2", 0.30)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2", 0.30)),
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2_MAX", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2", 0.30)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE2", 0.30)),
                            float(getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT_STAGE2", getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 0.8)) or getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 0.8)),
                        ),
                        "S3": (
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3_MIN", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3", 0.20)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3", 0.20)),
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3_MAX", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3", 0.20)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE3", 0.20)),
                            float(getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT_STAGE3", getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 1.0)) or getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 1.0)),
                        ),
                        "S4": (
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4_MIN", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4", 0.10)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4", 0.10)),
                            float(getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4_MAX", getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4", 0.10)) or getattr(self.cfg, "EXIT_MFE_RETRACE_PCT_STAGE4", 0.10)),
                            float(getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT_STAGE4", getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 1.2)) or getattr(self.cfg, "EXIT_MFE_MIN_RETRACE_PT", 1.2)),
                        ),
                    }
                    if _stage_band_key in _stage_band_pct_map:
                        _stage_band_pct_min, _stage_band_pct_max, _stage_band_min_retrace = _stage_band_pct_map[_stage_band_key]
                except Exception:
                    _stage_band_pct_min = None
                    _stage_band_pct_max = None
                    _stage_band_min_retrace = None
                if (
                    _mfe_extreme_price is not None
                    and _mfe_stage_mfe_pt is not None
                    and _stage_band_pct_min is not None
                    and _stage_band_pct_max is not None
                    and _stage_band_min_retrace is not None
                ):
                    try:
                        _req_low = max(float(_stage_band_min_retrace), float(_mfe_stage_mfe_pt) * float(_stage_band_pct_min))
                        _req_high = max(float(_stage_band_min_retrace), float(_mfe_stage_mfe_pt) * float(_stage_band_pct_max))
                        if in_pos > 0:
                            _cut_band_a = float(_mfe_extreme_price) - float(_req_low)
                            _cut_band_b = float(_mfe_extreme_price) - float(_req_high)
                        else:
                            _cut_band_a = float(_mfe_extreme_price) + float(_req_low)
                            _cut_band_b = float(_mfe_extreme_price) + float(_req_high)
                        _mfe_stage_cut_band_low = min(float(_cut_band_a), float(_cut_band_b))
                        _mfe_stage_cut_band_high = max(float(_cut_band_a), float(_cut_band_b))
                        _cut_txt = f"{_mfe_stage_cut_band_low:.2f}~{_mfe_stage_cut_band_high:.2f}"
                    except Exception:
                        _mfe_stage_cut_band_low = None
                        _mfe_stage_cut_band_high = None
                if armed:
                    bar3_left_text = "CUT"
                    bar3_right_text = f"{_mfe_extreme_label} {_extreme_txt}    /    CUT {_cut_txt}"
                else:
                    bar3_left_text = "미무장"
                    bar3_right_text = f"{_mfe_extreme_label} {_extreme_txt}    /    CUT -    /    ARM {_mfe_arm_min_pt:.1f}"
                # MFE ARM(1.8pt) 이후부터 MFE CUT 행이 깜박이도록 명시한다.
                bar3_active = True if hit else ("monitoring_blink" if armed else None)
            # MFE PROTECT is one continuous band rule; do not expose the retired
            # S1/S2 labels in the detail row.
            _use_mfe_protect_exit = bool(getattr(self.cfg, "USE_MFE_PROTECT_EXIT", False))
            protect_info = sig_state.get("mfe_protect") if (isinstance(sig_state, dict) and _use_mfe_protect_exit) else None
            protect_max_mfe_pt = float(getattr(self.cfg, "MFE_PROTECT_MAX_MFE_PT", 0.0) or 0.0)
            protect_handoff_to_mfe_cut = bool(
                isinstance(mfe_retrace_info, dict)
                and mfe_retrace_info.get("armed", False)
            )
            def _mfe_protect_criteria_text(active_stage=""):
                try:
                    _rules = list(getattr(self.cfg, "MFE_PROTECT_RULES", ((1.6, 0.6, 1.2),)) or [])
                except Exception:
                    _rules = []
                _bands = []
                for _item in _rules:
                    try:
                        if isinstance(_item, (tuple, list)):
                            _mfe = float(_item[0])
                            _low = float(_item[1])
                            _high = float(_item[2]) if len(_item) >= 3 else float(_item[1])
                        else:
                            continue
                        _bands.append((_mfe, min(_low, _high), max(_low, _high)))
                    except Exception:
                        pass
                if not _bands:
                    _bands = [(0.4, -1.4, -1.0)]
                _parts = []
                for _mfe, _low, _high in _bands:
                    _txt = f"MFE≥{_mfe:.1f} / 보호 {_low:+.1f}~{_high:+.1f}"
                    _parts.append(_txt)
                return "    ".join(_parts)
            protect_criteria_text = _mfe_protect_criteria_text()
            protect_left_text = "대기"
            protect_right_text = protect_criteria_text
            protect_state_text = "대기중"
            protect_active = None
            if not _use_mfe_protect_exit:
                protect_left_text = "OFF"
                protect_right_text = "MFE PROTECT OFF"
                protect_state_text = "OFF"
            _protect_stage = ""
            _protect_threshold_mfe_pt = 0.0
            _protect_threshold_mae_pt = 0.0
            _protect_floor_pnl_pt = 0.0
            _protect_floor_pnl_trigger_pt = 0.0
            _protect_mfe_pt = None
            _protect_mae_pt = None
            _protect_current_pnl_pt = None
            _protect_exit_price = None
            if (not _use_mfe_protect_exit):
                protect_active = None
            elif self.auto_on and in_pos != 0:
                if isinstance(protect_info, dict):
                    try:
                        _protect_mfe_pt = float(protect_info.get("mfe_pt", 0.0) or 0.0)
                    except Exception:
                        _protect_mfe_pt = 0.0
                    try:
                        _protect_mae_pt = float(protect_info.get("mae_pt", 0.0) or 0.0)
                    except Exception:
                        _protect_mae_pt = 0.0
                    try:
                        _protect_current_pnl_pt = float(protect_info.get("current_pnl_pt", realtime_pnl if realtime_pnl is not None else 0.0) or 0.0)
                    except Exception:
                        _protect_current_pnl_pt = realtime_pnl
                    _protect_stage = str(protect_info.get("display_stage") or "").strip().upper()
                    try:
                        _protect_threshold_mfe_pt = float(protect_info.get("threshold_mfe_pt", 0.0) or 0.0)
                    except Exception:
                        _protect_threshold_mfe_pt = 0.0
                    try:
                        _protect_threshold_mae_pt = float(
                            protect_info.get("threshold_mae_pt", protect_info.get("threshold_value_pt", 0.0)) or 0.0
                        )
                    except Exception:
                        _protect_threshold_mae_pt = 0.0
                    try:
                        _protect_floor_pnl_pt = float(protect_info.get("floor_pnl_pt", 0.0) or 0.0)
                    except Exception:
                        _protect_floor_pnl_pt = 0.0
                    try:
                        _protect_floor_pnl_trigger_pt = float(
                            protect_info.get("floor_pnl_trigger_pt", _protect_floor_pnl_pt) or 0.0
                        )
                    except Exception:
                        _protect_floor_pnl_trigger_pt = _protect_floor_pnl_pt
                    try:
                        _protect_exit_price = float(protect_info.get("exit_price")) if protect_info.get("exit_price") is not None else None
                    except Exception:
                        _protect_exit_price = None
                    _protect_armed = bool(protect_info.get("armed", False))
                    _protect_hit = bool(protect_info.get("hit", False))
                    _protect_rule = str(protect_info.get("rule") or "").strip().upper()
                    if protect_handoff_to_mfe_cut or _protect_rule == "MFE_PROTECT_PASSED_TO_MFE_TRAIL":
                        protect_left_text = "통과"
                        protect_right_text = _mfe_protect_criteria_text()
                        protect_state_text = "통과"
                        protect_active = None
                    elif _protect_armed:
                        _protect_stage = "PROTECT"
                        protect_left_text = "발동" if _protect_hit else "감시"
                        protect_right_text = _mfe_protect_criteria_text()
                        protect_state_text = "발동" if _protect_hit else "감시중"
                        # MFE가 protect 상한(6.0pt)에 도달하면 위 분기에서 통과 처리되어
                        # PROTECT 깜박임은 멈추고 MFE CUT 행으로 깜박임이 이전된다.
                        protect_active = True if _protect_hit else "monitoring_blink"
                    else:
                        protect_left_text = "대기"
                        protect_right_text = _mfe_protect_criteria_text()
                        protect_state_text = "대기중"
                        protect_active = None
                else:
                    # Preserve the existing fallback renderer while making its
                    # pass/wait decision follow the actual MFE CUT handoff.
                    _cur_mfe = 0.0 if protect_handoff_to_mfe_cut else -1.0
                    _cur_pnl = realtime_pnl if realtime_pnl is not None else None
                    protect_left_text = "통과" if _cur_mfe >= protect_max_mfe_pt else "대기"
                    protect_right_text = _mfe_protect_criteria_text()
                    protect_state_text = "통과" if _cur_mfe >= protect_max_mfe_pt else "계산대기"
                    protect_active = None
            elif self.auto_on:
                protect_left_text = "대기"
                protect_right_text = _mfe_protect_criteria_text()
                protect_state_text = "포지션 없음"
            else:
                protect_left_text = "대기"
                protect_right_text = _mfe_protect_criteria_text()
                protect_state_text = "대기중"

            stop_loss_threshold_pt = float(sig_state.get("stop_loss_threshold_pt", self._stop_loss_threshold_pt()) or self._stop_loss_threshold_pt())
            stop_loss_enabled = bool(sig_state.get("stop_loss_enabled", stop_loss_threshold_pt > 0.0))
            stop_loss_current_pnl_pt = sig_state.get("stop_loss_current_pnl_pt", realtime_pnl)
            if stop_loss_current_pnl_pt is None and in_pos != 0:
                stop_loss_current_pnl_pt = realtime_pnl
            stop_loss_triggered = bool(sig_state.get("stop_loss_triggered", False))
            if in_pos != 0 and stop_loss_current_pnl_pt is not None:
                try:
                    stop_loss_triggered = stop_loss_triggered or (float(stop_loss_current_pnl_pt) <= -float(stop_loss_threshold_pt))
                except Exception:
                    pass
            if not self.auto_on:
                stop_loss_text = f"현재 - / 기준 {stop_loss_threshold_pt:.2f}pt / 대기중"
                stop_loss_active = None
            elif in_pos == 0:
                stop_loss_text = f"현재 - / 기준 {stop_loss_threshold_pt:.2f}pt / 모니터링 대기중"
                stop_loss_active = None
            elif not stop_loss_enabled:
                stop_loss_text = f"현재 - / 기준 {stop_loss_threshold_pt:.2f}pt / 비활성"
                stop_loss_active = None
            else:
                cur_txt = "-" if stop_loss_current_pnl_pt is None else f"{float(stop_loss_current_pnl_pt):+.2f}pt"
                state_txt = "충족" if stop_loss_triggered else "미충족"
                stop_loss_text = f"현재 {cur_txt} / 기준 {stop_loss_threshold_pt:.2f}pt / {state_txt}"
                stop_loss_active = bool(stop_loss_triggered)
            fixed_loss_band_low_pt = float(sig_state.get("fixed_loss_band_low_pt", getattr(self.cfg, "FIXED_LOSS_BAND_LOW_PT", getattr(self.cfg, "FIXED_LOSS_PT", 1.8))) or getattr(self.cfg, "FIXED_LOSS_BAND_LOW_PT", getattr(self.cfg, "FIXED_LOSS_PT", 1.8)))
            fixed_loss_band_high_pt = float(sig_state.get("fixed_loss_band_high_pt", getattr(self.cfg, "FIXED_LOSS_BAND_HIGH_PT", fixed_loss_band_low_pt)) or getattr(self.cfg, "FIXED_LOSS_BAND_HIGH_PT", fixed_loss_band_low_pt))
            fixed_loss_selection_ready = bool(
                sig_state.get("fixed_loss_selection_ready", False)
                and in_pos != 0
            )
            _fixed_loss_selected_raw = sig_state.get(
                "fixed_loss_selected_pt",
                sig_state.get("fixed_loss_trigger_pt", sig_state.get("fixed_loss_threshold_pt")),
            )
            fixed_loss_threshold_pt = None
            if fixed_loss_selection_ready:
                try:
                    fixed_loss_threshold_pt = float(_fixed_loss_selected_raw)
                except Exception:
                    fixed_loss_selection_ready = False
                    fixed_loss_threshold_pt = None
            fixed_loss_cfg_enabled = bool(getattr(self.cfg, "USE_FIXED_LOSS_EXIT", False))
            fixed_loss_enabled = bool(sig_state.get("fixed_loss_enabled", fixed_loss_cfg_enabled))
            if not fixed_loss_cfg_enabled:
                fixed_loss_enabled = False
            fixed_loss_triggered = bool(sig_state.get("fixed_loss_triggered", False))
            if not fixed_loss_enabled:
                fixed_loss_triggered = False
            if str(self.last_exit_reason or "").upper() == "FIXED_LOSS":
                fixed_loss_triggered = True
            if in_pos != 0 and fixed_loss_selection_ready and fixed_loss_threshold_pt is not None and stop_loss_current_pnl_pt is not None:
                try:
                    fixed_loss_triggered = fixed_loss_triggered or (float(stop_loss_current_pnl_pt) <= -float(fixed_loss_threshold_pt))
                except Exception:
                    pass
            fixed_loss_band_text = (
                f"-{fixed_loss_band_low_pt:.2f}~-{fixed_loss_band_high_pt:.2f}pt"
                if float(fixed_loss_band_high_pt) > float(fixed_loss_band_low_pt)
                else f"-{fixed_loss_band_low_pt:.2f}pt"
            )
            fixed_loss_selected_text = "밴드 진입 즉시"
            if not self.auto_on:
                fixed_loss_text = f"현재 - / 기준 {fixed_loss_band_text} / {fixed_loss_selected_text} / 대기중"
                fixed_loss_active = None
            elif in_pos == 0:
                fixed_loss_text = f"현재 - / 기준 {fixed_loss_band_text} / {fixed_loss_selected_text} / 모니터링 대기중"
                fixed_loss_active = None
            elif not fixed_loss_enabled:
                fixed_loss_text = f"현재 - / 기준 {fixed_loss_band_text} / {fixed_loss_selected_text} / 비활성"
                fixed_loss_active = None
            else:
                cur_txt = "-" if stop_loss_current_pnl_pt is None else f"{float(stop_loss_current_pnl_pt):+.2f}pt"
                state_txt = "충족" if fixed_loss_triggered else "미충족"
                fixed_loss_text = f"현재 {cur_txt} / 기준 {fixed_loss_band_text} / {fixed_loss_selected_text} / {state_txt}"
                fixed_loss_active = bool(fixed_loss_triggered)
            fixed_loss_cd_remaining = 0.0
            try:
                if str(self.last_exit_reason or "").strip().upper() == "FIXED_LOSS":
                    fixed_loss_cd_remaining = float(self._exit_entry_cooldown_remaining_sec() or 0.0)
            except Exception:
                fixed_loss_cd_remaining = 0.0
            if fixed_loss_cd_remaining > 0.0:
                fixed_loss_text = f"{int(math.ceil(fixed_loss_cd_remaining))}s"
                fixed_loss_active = "monitoring_blink"

            signed_exit_z = self._signed_exit_z()
            live_target_exit_index = self._calc_live_target_exit_index()
            realtime_target_index = self.current_price or self.last_bar_close or None
            pending_action = getattr(self.order_core.pending, 'action', '-') if self.order_core else '-'
            trade_start = str(getattr(self.cfg, "TRADE_START", "08:50") or "08:50")
            trade_end = str(getattr(self.cfg, "TRADE_END", "15:30") or "15:30")
            eod_cutoff = str(getattr(self.cfg, "EOD_CUTOFF", trade_end) or trade_end)
            trade_time_target = f"{trade_start}~{trade_end} / EOD {eod_cutoff}"
            trade_time_ok = (bool(sig_state.get("trade_time_ok")) if "trade_time_ok" in sig_state else None)
            allow_lane, block_reason = self._entry_lane_state(sig_state)
            gate_ready = bool(allow_lane)
            entry_display_ready = bool(warmup_ready)
            time_state = ("IN" if trade_time_ok is True else ("OUT" if trade_time_ok is False else "-"))
            _entry_src = "price" if bool(sig_state.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False))) else str(getattr(self.cfg, "ENTRY_SIGNAL_SOURCE", "z1") or "z1").lower()
            _use_price_extrema_entry_disp = bool(sig_state.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
            _exit_src = "price"
            _use_exit_z_cfg = bool(getattr(self.cfg, "USE_EXIT_Z", True))
            _exit_z_long = -abs(float(sig_state.get("exit_z_long", getattr(self.cfg, "EXIT_Z_LONG", 0.5)) or getattr(self.cfg, "EXIT_Z_LONG", 0.5)))
            _exit_z_short = abs(float(sig_state.get("exit_z_short", getattr(self.cfg, "EXIT_Z_SHORT", -0.5)) or getattr(self.cfg, "EXIT_Z_SHORT", -0.5)))
            _exit_z_delta = float(sig_state.get("exit_z_retrace_delta", 0.0) or 0.0)
            _exit_z_pnl_gate = bool(sig_state.get("exit_z_pnl_gate_enabled", getattr(self.cfg, "USE_EXIT_Z_PNL_GATE", True)))
            _exit_z_max_pnl_pt = float(sig_state.get("exit_z_max_pnl_pt", getattr(self.cfg, "EXIT_Z_MAX_PNL_PT", 0.0)) or getattr(self.cfg, "EXIT_Z_MAX_PNL_PT", 0.0))
            _entry_dz = float(sig_state.get("dz5_arm_long", getattr(self.cfg, "DZ5_ARM_LONG", 0.65)) or getattr(self.cfg, "DZ5_ARM_LONG", 0.65))
            _entry_dz_order = float(sig_state.get("dz5_entry_long", getattr(self.cfg, "DZ5_ENTRY_LONG", getattr(self.cfg, "DZ5_ARM_LONG_MAX", 1.0))) or getattr(self.cfg, "DZ5_ENTRY_LONG", getattr(self.cfg, "DZ5_ARM_LONG_MAX", 1.0)))
            _entry_dz_short = float(sig_state.get("dz5_arm_short", getattr(self.cfg, "DZ5_ARM_SHORT", -0.65)) or getattr(self.cfg, "DZ5_ARM_SHORT", -0.65))
            _entry_dz_short_order = float(sig_state.get("dz5_entry_short", getattr(self.cfg, "DZ5_ENTRY_SHORT", getattr(self.cfg, "DZ5_ARM_SHORT_MIN", -1.0))) or getattr(self.cfg, "DZ5_ENTRY_SHORT", getattr(self.cfg, "DZ5_ARM_SHORT_MIN", -1.0)))
            _fixed_loss_pt = float(getattr(self.cfg, "FIXED_LOSS_PT", 3.5) or 3.5)
            _base_cd_sec = float(getattr(self.cfg, "REVERSE_ENTRY_COOLDOWN_SEC", 60.0) or 60.0)
            _fixed_loss_cd_sec = float(getattr(self.cfg, "FIXED_LOSS_REENTRY_COOLDOWN_SEC", self._reverse_entry_cooldown_sec) or self._reverse_entry_cooldown_sec)
            _mfe_trigger_mode = "STAGED"
            _use_macd_osci = bool(getattr(self.cfg, "USE_MACD_OSCI_FILTER", False))
            _macd_osci_lmin = float(getattr(self.cfg, "MACD_OSCI_LONG_MIN", 0.01) or 0.01)
            _macd_osci_smax = float(getattr(self.cfg, "MACD_OSCI_SHORT_MAX", -0.01) or -0.01)
            _macd_osci_tag = (f"MOSC(L>{_macd_osci_lmin:+.3f}/S<{_macd_osci_smax:+.3f})" if _use_macd_osci else "MOSC-OFF")
            _entry_formula_tag = "PRICE 최종: 추세=H/L 터치 즉시 ARM -> 완료15초 진행방향 H/L 롤링 -> FIRE / 반전=5초 대기 -> 3초 FIRE 감시" if _use_price_extrema_entry_disp else "DZ5 반전 ARM/FIRE(L <= -0.15 -> >= +0.30 / S >= +0.15 -> <= -0.30)"
            _exit_formula_tag = (
                f"XZ[PRICE](LONG->PEAK-{float(_exit_z_delta):.2f} / SHORT->TROUGH+{float(_exit_z_delta):.2f},"
                f"pnl<={_exit_z_max_pnl_pt:+.2f}{' ON' if _exit_z_pnl_gate else ' OFF'})"
                if _use_exit_z_cfg else "XZ[OFF]"
            )
            _exit_mode_upper = str(sig_state.get("exit_z_trigger_mode") or "").upper()
            _exit_long_label = "PEAK"
            _exit_short_label = "TROUGH"
            _mfe_s1_rule_text = (
                (
                    f"s1 {float(getattr(self.cfg, 'EXIT_MFE_STAGE1_PT', getattr(self.cfg, 'EXIT_MFE_MIN_PT', 3.0)) or 3.0):.1f}/"
                    f"{float(getattr(self.cfg, 'EXIT_MFE_RETRACE_PCT_STAGE1_MIN', 0.20) or 0.20):.0%}~"
                    f"{float(getattr(self.cfg, 'EXIT_MFE_RETRACE_PCT_STAGE1_MAX', 0.25) or 0.25):.0%}/"
                    f"{float(getattr(self.cfg, 'EXIT_MFE_MIN_RETRACE_PT_STAGE1', getattr(self.cfg, 'EXIT_MFE_MIN_RETRACE_PT', 0.8)) or 0.8):.1f}"
                )
                if bool(getattr(self.cfg, 'EXIT_MFE_STAGE1_ENABLED', True))
                else "s1 OFF"
            )
            _rule_tag = (
                f"EN[{_entry_src.upper()}|{_entry_formula_tag}] {_exit_formula_tag} "
                f"FL{_fixed_loss_pt:.1f} CD{int(_base_cd_sec)}s/{int(_fixed_loss_cd_sec//60)}m "
                f"MFE_TRAIL[{_mfe_trigger_mode}: "
                f"{_mfe_s1_rule_text}, "
                f"s2 {float(getattr(self.cfg, 'EXIT_MFE_STAGE2_PT', 6.0) or 6.0):.1f}/"
                f"{float(getattr(self.cfg, 'EXIT_MFE_RETRACE_PCT_STAGE2', 0.30) or 0.30):.0%}/"
                f"{float(getattr(self.cfg, 'EXIT_MFE_MIN_RETRACE_PT_STAGE2', getattr(self.cfg, 'EXIT_MFE_MIN_RETRACE_PT', 0.8)) or 0.8):.1f}, "
                f"s3 {float(getattr(self.cfg, 'EXIT_MFE_STAGE3_PT', 9.0) or 9.0):.1f}/"
                f"{float(getattr(self.cfg, 'EXIT_MFE_RETRACE_PCT_STAGE3', 0.20) or 0.20):.0%}/"
                f"{float(getattr(self.cfg, 'EXIT_MFE_MIN_RETRACE_PT_STAGE3', 1.0) or 1.0):.1f}, "
                f"s4 {float(getattr(self.cfg, 'EXIT_MFE_STAGE4_PT', 12.0) or 12.0):.1f}/"
                f"{float(getattr(self.cfg, 'EXIT_MFE_RETRACE_PCT_STAGE4', 0.10) or 0.10):.0%}/"
                f"{float(getattr(self.cfg, 'EXIT_MFE_MIN_RETRACE_PT_STAGE4', 1.2) or 1.2):.1f}] {_macd_osci_tag}"
            )
            holiday_active = self._is_krx_closed_day()
            holiday_reason = self._krx_holiday_reason() if holiday_active else ""
            self._emit_market_holiday_log()
            _startup_gate_ok, _startup_gate_reason = self._startup_entry_gate_state()
            _startup_gate_status = self._startup_entry_gate_status_text()
            if (not _startup_gate_ok) and (not _startup_gate_status):
                _startup_gate_fallback = max(
                    0.0,
                    float(getattr(self.cfg, "STARTUP_ENTRY_DELAY_SEC", 10.0) or 0.0),
                )
                if _startup_gate_fallback > 0.0:
                    _startup_gate_status = f"{int(math.ceil(_startup_gate_fallback))}s"
            if not self.auto_on:
                auto_status = "수동모드"
            elif not self.connected:
                auto_status = "연결대기"
            elif not _startup_gate_ok:
                auto_status = f"감시대기 {_startup_gate_status}"
            elif self.warmup_loading:
                auto_status = "워밍업중"
            elif not warmup_ready:
                auto_status = "워밍업대기"
            elif not self.real_registered:
                auto_status = "실시간대기"
            else:
                auto_status = "주문감시중"
            _mfe_cut_cd_remaining = 0.0
            try:
                if str(self.last_exit_reason or "").strip().upper() in {"MFE_TRAIL", "MFE_PROTECT"}:
                    _mfe_cut_cd_remaining = float(self._exit_entry_cooldown_remaining_sec() or 0.0)
            except Exception:
                _mfe_cut_cd_remaining = 0.0
            if _mfe_cut_cd_remaining > 0.0:
                auto_status = f"MFE 컷 쿨다운 {int(math.ceil(_mfe_cut_cd_remaining))}s"
            if self.resume_status_text:
                auto_status = f"{self.resume_status_text} | {auto_status}"
            pending_phase = str(getattr(self.order_core.pending, "auto_cancel_phase", "") or self.auto_cancel_phase or "-") if self.order_core else (self.auto_cancel_phase or "-")
            sync_state = "PENDING" if self.server_sync_pending else "CLEAR"
            unfilled_state = (f"Y({self.server_unfilled_qty})" if self.has_server_unfilled else "N")
            gate_current = (
                f"AUTO=ON | CORE={'Y' if self.order_core is not None else 'N'} | "
                f"PENDING={pending_action if pending_action else '-'} | "
                f"SYNC={sync_state} | UNFILLED={unfilled_state} | "
                f"POS={'FLAT' if in_pos == 0 else 'HOLD'} | "
                f"TIME={time_state}({trade_time_target}) | BLOCK={block_reason} | PHASE={pending_phase}"
            ) if self.auto_on else "-"
            gate_target = f"AUTO=ON & CORE=Y & PENDING=- & SYNC=CLEAR & UNFILLED=N & POS=FLAT & TIME=IN({trade_time_target})" if self.auto_on else "대기중"
            # Dashboard ENTRY rows: show the active entry model.
            _prev_z5 = sig_state.get("prev_z5")
            _arm_long = float(sig_state.get("dz5_arm_long", getattr(self.cfg, "DZ5_ARM_LONG", 1.5)) or getattr(self.cfg, "DZ5_ARM_LONG", 1.5))
            _entry_long_thr = float(sig_state.get("dz5_entry_long", getattr(self.cfg, "DZ5_ENTRY_LONG", getattr(self.cfg, "DZ5_ARM_LONG_MAX", 1.0))) or getattr(self.cfg, "DZ5_ENTRY_LONG", getattr(self.cfg, "DZ5_ARM_LONG_MAX", 1.0)))
            _arm_short = float(sig_state.get("dz5_arm_short", getattr(self.cfg, "DZ5_ARM_SHORT", -1.5)) or getattr(self.cfg, "DZ5_ARM_SHORT", -1.5))
            _entry_short_thr = float(sig_state.get("dz5_entry_short", getattr(self.cfg, "DZ5_ENTRY_SHORT", getattr(self.cfg, "DZ5_ARM_SHORT_MIN", -1.0))) or getattr(self.cfg, "DZ5_ENTRY_SHORT", getattr(self.cfg, "DZ5_ARM_SHORT_MIN", -1.0)))
            _arm_expiry = int(sig_state.get("dz5_arm_expiry_bars", getattr(self.cfg, "DZ5_ARM_EXPIRY_BARS", 5)) or getattr(self.cfg, "DZ5_ARM_EXPIRY_BARS", 5))
            _entry_arm_swapped = bool(sig_state.get("entry_arm_band_swapped", getattr(self.cfg, "SWAP_ENTRY_ARM_BAND_SIDE", False)))
            _use_price_extrema_entry = bool(sig_state.get("use_price_extrema_entry", getattr(self.cfg, "USE_PRICE_EXTREMA_ENTRY", False)))
            _price_entry_lb = int(sig_state.get("price_extrema_lookback_bars", getattr(self.cfg, "PRICE_EXTREMA_LOOKBACK_BARS", 3)) or getattr(self.cfg, "PRICE_EXTREMA_LOOKBACK_BARS", 3))
            if str(sig_state.get("active_entry_regime") or sig_state.get("entry_regime") or "").upper() == "TREND":
                _price_entry_lb = int(sig_state.get("trend_price_extrema_lookback_bars", getattr(self.cfg, "TREND_PRICE_EXTREMA_LOOKBACK_BARS", 3)) or getattr(self.cfg, "TREND_PRICE_EXTREMA_LOOKBACK_BARS", 3))
            _price_entry_off = abs(float(
                sig_state.get("price_extrema_offset_pt", getattr(self.cfg, "PRICE_EXTREMA_OFFSET_PT", 0.5))
            ))
            _price_fire_min = abs(float(sig_state.get("price_fire_band_min_pt", getattr(self.cfg, "PRICE_FIRE_BAND_MIN_PT", 0.1)) or getattr(self.cfg, "PRICE_FIRE_BAND_MIN_PT", 0.1)))
            _price_fire_max = abs(float(sig_state.get("price_fire_band_max_pt", getattr(self.cfg, "PRICE_FIRE_BAND_MAX_PT", 0.4)) or getattr(self.cfg, "PRICE_FIRE_BAND_MAX_PT", 0.4)))
            _price_range_on = bool(sig_state.get("use_entry_price_range_filter", getattr(self.cfg, "USE_ENTRY_PRICE_RANGE_FILTER", False)))
            _price_range_lb = int(sig_state.get("entry_price_range_lookback_bars", getattr(self.cfg, "ENTRY_PRICE_RANGE_LOOKBACK_BARS", 5)) or getattr(self.cfg, "ENTRY_PRICE_RANGE_LOOKBACK_BARS", 5))
            _price_range_min = float(sig_state.get("entry_price_range_min_pt", getattr(self.cfg, "ENTRY_PRICE_RANGE_MIN_PT", 2.5)) or getattr(self.cfg, "ENTRY_PRICE_RANGE_MIN_PT", 2.5))
            _price_prev_low = sig_state.get("entry_price_prev_low")
            _price_prev_high = sig_state.get("entry_price_prev_high")
            _price_range_pt = sig_state.get("entry_price_range_pt")
            _price_long_level = sig_state.get("entry_price_long_level")
            _price_short_level = sig_state.get("entry_price_short_level")
            _price_long_fire_min = sig_state.get("entry_price_fire_long_min")
            _price_long_fire_max = sig_state.get("entry_price_fire_long_max")
            _price_short_fire_min = sig_state.get("entry_price_fire_short_min")
            _price_short_fire_max = sig_state.get("entry_price_fire_short_max")
            _price_long_hit = bool(sig_state.get("entry_price_long_hit", False))
            _price_short_hit = bool(sig_state.get("entry_price_short_hit", False))
            _price_long_fire_hit = bool(sig_state.get("entry_price_fire_long_hit", False))
            _price_short_fire_hit = bool(sig_state.get("entry_price_fire_short_hit", False))
            _long_min = float(sig_state.get("long_entry_z5_min", getattr(self.cfg, "Z5_LONG_BAND_LOW", getattr(self.cfg, "LONG_ENTRY_Z5_MIN", -3.0))) or getattr(self.cfg, "Z5_LONG_BAND_LOW", getattr(self.cfg, "LONG_ENTRY_Z5_MIN", -3.0)))
            _long_max = float(sig_state.get("long_entry_z5_max", getattr(self.cfg, "Z5_LONG_BAND_HIGH", getattr(self.cfg, "LONG_ENTRY_Z5_MAX", 3.0))) or getattr(self.cfg, "Z5_LONG_BAND_HIGH", getattr(self.cfg, "LONG_ENTRY_Z5_MAX", 3.0)))
            _short_min = float(sig_state.get("short_entry_z5_min", getattr(self.cfg, "Z5_SHORT_BAND_LOW", getattr(self.cfg, "SHORT_ENTRY_Z5_MIN", -3.0))) or getattr(self.cfg, "Z5_SHORT_BAND_LOW", getattr(self.cfg, "SHORT_ENTRY_Z5_MIN", -3.0)))
            _short_max = float(sig_state.get("short_entry_z5_max", getattr(self.cfg, "Z5_SHORT_BAND_HIGH", getattr(self.cfg, "SHORT_ENTRY_Z5_MIN", -3.0))) or getattr(self.cfg, "Z5_SHORT_BAND_HIGH", getattr(self.cfg, "SHORT_ENTRY_Z5_MIN", -3.0)))
            _z5_deadzone_abs = abs(float(sig_state.get("z5_entry_deadzone_abs", getattr(self.cfg, "Z5_ENTRY_DEADZONE_ABS", 0.0)) or getattr(self.cfg, "Z5_ENTRY_DEADZONE_ABS", 0.0)))
            long_band_ok = (bool(sig_state.get("long_band_ok")) if entry_display_ready else None)
            short_band_ok = (bool(sig_state.get("short_band_ok")) if entry_display_ready else None)
            long_armed = bool(sig_state.get("long_armed", False))
            short_armed = bool(sig_state.get("short_armed", False))
            long_arm_age = int(sig_state.get("long_arm_age", 0) or 0)
            short_arm_age = int(sig_state.get("short_arm_age", 0) or 0)
            long_arm_trigger_current = bool(sig_state.get("long_arm_trigger_hit", False))
            short_arm_trigger_current = bool(sig_state.get("short_arm_trigger_hit", False))
            long_entry_dz5_current = bool(sig_state.get("long_entry_dz5_hit", False))
            short_entry_dz5_current = bool(sig_state.get("short_entry_dz5_hit", False))
            # Dashboard must follow strategy.py's final signal-state, not re-decide
            # order readiness from partial ARM rows.  This prevents the title/status
            # from diverging from the actual BUY/SELL_SHORT gate.
            long_order_condition_hit = bool(sig_state.get("long_ok", False))
            short_order_condition_hit = bool(sig_state.get("short_ok", False))
            long_entry_ready_state = bool(sig_state.get("long_entry_ready", False))
            short_entry_ready_state = bool(sig_state.get("short_entry_ready", False))
            _entry_regime_live = self._resolve_entry_regime(sig_state, default="")
            # Physical Z5 zone and active rule profile can differ.  DETAIL must
            # display the H/L anchor actually used by the active rule profile.
            _detail_entry_regime_live = str(
                sig_state.get("active_entry_regime") or _entry_regime_live or ""
            ).strip().upper()
            # Dashboard ARM blink must be a *read-only mirror* of strategy.py.
            # Do not re-interpret broad raw price levels such as entry_price_*_hit
            # or long/short_arm_trigger_hit here.  In price-extrema mode those raw
            # levels can both stay true after reconnect or inside a narrow range,
            # which caused false blinking and also suppressed the opposite side.
            # The ARM row should blink only while the strategy has an actual ARM
            # latch, or on the exact tick where the latched ARM has already reached
            # FIRE/order-ready.  Pending/position gates below still stop the blink
            # after the real order flow takes over.
            long_arm_ui_state = bool(long_armed or long_entry_ready_state or long_entry_dz5_current)
            short_arm_ui_state = bool(short_armed or short_entry_ready_state or short_entry_dz5_current)
            long_arm_raw_display_hit = bool(long_arm_ui_state)
            short_arm_raw_display_hit = bool(short_arm_ui_state)
            long_arm_display_hit = bool(long_arm_ui_state and entry_display_ready and self.auto_on and gate_ready)
            short_arm_display_hit = bool(short_arm_ui_state and entry_display_ready and self.auto_on and gate_ready)
            long_regime_ok = None
            short_regime_ok = None
            regime_now_display = str(self.last_regime or "-").upper()
            regime_price_disp = sig_state.get("regime_price_now")
            prev5_hi = None
            prev5_lo = None
            prev5_hi_time = "-"
            prev5_lo_time = "-"
            prev_pool = []
            _price_anchor_source_disp = str(
                sig_state.get(
                    "price_extrema_anchor_source",
                    getattr(self.cfg, "PRICE_EXTREMA_ANCHOR_SOURCE", "close_1m_confirmed"),
                )
                or getattr(self.cfg, "PRICE_EXTREMA_ANCHOR_SOURCE", "close_1m_confirmed")
            ).strip().lower()
            _price_anchor_use_confirmed_close_disp = bool(
                _price_anchor_source_disp in ("close_1m_confirmed", "confirmed_close", "close")
            )
            try:
                bars1_items = list(getattr(self.st, "_bars1", {}).items())
                bars1 = [bar for _, bar in bars1_items]
                if bars1:
                    prev_pool = list(bars1)
                    _market_lb = max(1, int(_price_entry_lb))
                    prev_bars = prev_pool[-_market_lb:] if len(prev_pool) >= _market_lb else list(prev_pool)
                    if prev_bars:
                        if _detail_entry_regime_live == "TREND":
                            _market_hi_val = lambda b: max(float(b.o), float(b.c))
                            _market_lo_val = lambda b: min(float(b.o), float(b.c))
                        else:
                            _market_hi_val = (lambda b: float(b.c) if _price_anchor_use_confirmed_close_disp else float(b.h))
                            _market_lo_val = (lambda b: float(b.c) if _price_anchor_use_confirmed_close_disp else float(b.l))
                        _hi_bar5 = max(prev_bars, key=_market_hi_val)
                        _lo_bar5 = min(prev_bars, key=_market_lo_val)
                        prev5_hi = _market_hi_val(_hi_bar5)
                        prev5_lo = _market_lo_val(_lo_bar5)
                        try:
                            prev5_hi_time = _hi_bar5.t.strftime("%H:%M:%S") if isinstance(_hi_bar5.t, datetime) else str(_hi_bar5.t or "-")
                        except Exception:
                            prev5_hi_time = "-"
                        try:
                            prev5_lo_time = _lo_bar5.t.strftime("%H:%M:%S") if isinstance(_lo_bar5.t, datetime) else str(_lo_bar5.t or "-")
                        except Exception:
                            prev5_lo_time = "-"
            except Exception:
                prev5_hi = None
                prev5_lo = None
                prev5_hi_time = "-"
                prev5_lo_time = "-"
            if _use_price_extrema_entry:
                try:
                    if _detail_entry_regime_live == "TREND":
                        _sig_prev5_hi = sig_state.get("trend_entry_price_anchor_high")
                        _sig_prev5_lo = sig_state.get("trend_entry_price_anchor_low")
                    elif _detail_entry_regime_live == "REVERSAL":
                        _sig_prev5_hi = sig_state.get("reversal_entry_price_anchor_high")
                        _sig_prev5_lo = sig_state.get("reversal_entry_price_anchor_low")
                        _sample_t = sig_state.get("reversal_live_sample_time")
                        if _sample_t:
                            prev5_hi_time = str(_sample_t)
                            prev5_lo_time = str(_sample_t)
                    else:
                        _sig_prev5_hi = sig_state.get("entry_price_anchor_high")
                        _sig_prev5_lo = sig_state.get("entry_price_anchor_low")
                    if _sig_prev5_hi is not None:
                        prev5_hi = float(_sig_prev5_hi)
                    if _sig_prev5_lo is not None:
                        prev5_lo = float(_sig_prev5_lo)
                except Exception:
                    pass
            _detail_high_label = "5봉 기준 최고"
            _detail_low_label = "5봉 기준 최저"
            _detail_high_time_label = "최고 기준시각"
            _detail_low_time_label = "최저 기준시각"
            if _detail_entry_regime_live == "TREND":
                # TREND watches a fixed five-completed-body O/C H/L anchor. It has no
                # 15s timeout; the anchor resets only when confirmed body H/L changes.
                _hl_mode_txt = "추세형 확정몸통5"
                _detail_high_label = "추세 확정몸통5 최고"
                _detail_low_label = "추세 확정몸통5 최저"
                _detail_high_time_label = "추세 최고 시각"
                _detail_low_time_label = "추세 최저 시각"
            elif _detail_entry_regime_live == "REVERSAL":
                # REVERSAL displays the effective current H/L basis.  The 15s
                # countdown is NOT a permanent market-state mode; it exists only
                # after the first fresh high/low opens an actual candidate window.
                # Showing "+15초" while idle made the dashboard look as if the
                # timer were stuck after the close.
                _rev_active_side = str(sig_state.get("reversal_active_candidate_side") or "").upper()
                _rev_has_active_candidate = bool(
                    (_rev_active_side == "LONG" and (sig_state.get("reversal_long_candidate_anchor") is not None or (long_armed and _long_arm_regime == "REVERSAL")))
                    or (_rev_active_side == "SHORT" and (sig_state.get("reversal_short_candidate_anchor") is not None or (short_armed and _short_arm_regime == "REVERSAL")))
                )
                _hl_mode_txt = "반전형 현재H/L" + ("+진행방향15초후보" if _rev_has_active_candidate else "")
                _detail_high_label = "반전 현재기준 최고"
                _detail_low_label = "반전 현재기준 최저"
                _detail_high_time_label = "반전 최고 시각"
                _detail_low_time_label = "반전 최저 시각"
            else:
                _hl_mode_txt = "5봉"

            # Legacy state field names still contain "confirmed5"; show the
            # configured and actually applied windows in the DETAIL labels.
            if _detail_entry_regime_live == "TREND":
                _hl_mode_txt = "\ucd94\uc138\ud615 \ucd5c\uadfc3\ubd09 \ubab8\ud1b5H/L"
                _detail_high_label = "\ucd94\uc138\ud615 3\ubd09 \ubab8\ud1b5 \ucd5c\uace0"
                _detail_low_label = "\ucd94\uc138\ud615 3\ubd09 \ubab8\ud1b5 \ucd5c\uc800"
                _detail_high_time_label = "\ucd94\uc138\ud615 3\ubd09 \ucd5c\uace0 \uc2dc\uac01"
                _detail_low_time_label = "\ucd94\uc138\ud615 3\ubd09 \ucd5c\uc800 \uc2dc\uac01"
            elif _detail_entry_regime_live == "REVERSAL":
                _hl_mode_txt = "\ubc18\uc804\ud615 \ucd5c\uadfc6\ubd09 H/L"
                _detail_high_label = "\ubc18\uc804\ud615 6\ubd09 \ucd5c\uace0"
                _detail_low_label = "\ubc18\uc804\ud615 6\ubd09 \ucd5c\uc800"
                _detail_high_time_label = "\ubc18\uc804\ud615 6\ubd09 \ucd5c\uace0 \uc2dc\uac01"
                _detail_low_time_label = "\ubc18\uc804\ud615 6\ubd09 \ucd5c\uc800 \uc2dc\uac01"

            if self.auto_on:
                price_now_disp = regime_price_disp
                if price_now_disp is None:
                    price_now_disp = self.current_price or self.last_bar_close
                if prev5_hi is not None and prev5_lo is not None:
                    hi_txt = f"{float(prev5_hi):.2f}"
                    lo_txt = f"{float(prev5_lo):.2f}"
                    hi_time_txt = str(prev5_hi_time or "-")
                    lo_time_txt = str(prev5_lo_time or "-")
                    if price_now_disp is not None:
                        px_txt = f"{float(price_now_disp):.2f}"
                        regime_current_display = f"{regime_now_display} (현재 {px_txt} / {_hl_mode_txt} 최고 {hi_txt}@{hi_time_txt} 최저 {lo_txt}@{lo_time_txt})"
                    else:
                        regime_current_display = f"{regime_now_display} ({_hl_mode_txt} 최고 {hi_txt}@{hi_time_txt} 최저 {lo_txt}@{lo_time_txt})"
                else:
                    regime_current_display = f"{regime_now_display} (값 대기)"
                long_regime_current = regime_current_display
                short_regime_current = regime_current_display
                regime_target_display_long = "현재가가 5봉 H/L FIRE 밴드 충족"
                regime_target_display_short = "현재가가 5봉 H/L FIRE 밴드 충족"
                regime_display = regime_current_display
            else:
                long_regime_current = "-"
                short_regime_current = "-"
                regime_target_display_long = "대기중"
                regime_target_display_short = "대기중"
                regime_display = "-"
            entry_src = str(sig_state.get("entry_source", "z1") or "z1").lower()
            # HOTFIX: _ext_lb_disp is used by the Z5 display text below.
            # The previous display patch assigned it later in refresh_view(), which
            # caused UnboundLocalError before AUTO ON / before a full sig_state cycle.
            try:
                _ext_lb_disp = int(
                    sig_state.get(
                        "dz5_entry_extrema_lookback_bars",
                        getattr(self.cfg, "DZ5_ENTRY_EXTREMA_LOOKBACK_BARS", 20),
                    )
                    or getattr(self.cfg, "DZ5_ENTRY_EXTREMA_LOOKBACK_BARS", 20)
                )
            except Exception:
                _ext_lb_disp = int(getattr(self.cfg, "DZ5_ENTRY_EXTREMA_LOOKBACK_BARS", 20) or 20)
            _prev_z5_txt = "-" if _prev_z5 is None else f"{float(_prev_z5):+.3f}"
            _z5_display_val = sig_state.get("z5")
            _z5_now_txt = "-" if _z5_display_val is None else f"{float(_z5_display_val):+.3f}"
            _prev5_low_z5 = sig_state.get("dz5_low_anchor_z5")
            _prev5_high_z5 = sig_state.get("dz5_high_anchor_z5")
            try:
                _prev5_low_txt = "-" if _prev5_low_z5 is None else f"{float(_prev5_low_z5):+.3f}"
            except Exception:
                _prev5_low_txt = "-"
            try:
                _prev5_high_txt = "-" if _prev5_high_z5 is None else f"{float(_prev5_high_z5):+.3f}"
            except Exception:
                _prev5_high_txt = "-"
            try:
                long_z5_display_ok = bool(
                    z5_val is not None
                    and _prev5_high_z5 is not None
                    and float(z5_val) > float(_prev5_high_z5)
                )
            except Exception:
                long_z5_display_ok = False
            try:
                short_z5_display_ok = bool(
                    z5_val is not None
                    and _prev5_low_z5 is not None
                    and float(z5_val) < float(_prev5_low_z5)
                )
            except Exception:
                short_z5_display_ok = False
            if z5_val is None or _prev5_high_z5 is None:
                long_z5_current_text = f"{z5_text} / 이전{_ext_lb_disp}봉H(-)"
                long_z5_display_ok = None
            else:
                long_z5_current_text = f"{z5_text} / 이전{_ext_lb_disp}봉H({_prev5_high_txt})"
            if z5_val is None or _prev5_low_z5 is None:
                short_z5_current_text = f"{z5_text} / 이전{_ext_lb_disp}봉L(-)"
                short_z5_display_ok = None
            else:
                short_z5_current_text = f"{z5_text} / 이전{_ext_lb_disp}봉L({_prev5_low_txt})"
            _dz_long_txt = "-"
            _dz_short_txt = "-"
            _prev_dz_long_txt = "-"
            _prev_dz_short_txt = "-"
            _dz_long_sig = sig_state.get("long_entry_active_dz5", sig_state.get("dz5_long"))
            _dz_short_sig = sig_state.get("short_entry_active_dz5", sig_state.get("dz5_short"))
            _prev_dz_long_sig = sig_state.get("prev_dz5_long")
            _prev_dz_short_sig = sig_state.get("prev_dz5_short")
            if _dz_long_sig is not None:
                try:
                    _dz_long_txt = f"{float(_dz_long_sig):+.3f}"
                except Exception:
                    _dz_long_txt = "-"
            if _dz_short_sig is not None:
                try:
                    _dz_short_txt = f"{float(_dz_short_sig):+.3f}"
                except Exception:
                    _dz_short_txt = "-"
            if _prev_dz_long_sig is not None:
                try:
                    _prev_dz_long_txt = f"{float(_prev_dz_long_sig):+.3f}"
                except Exception:
                    _prev_dz_long_txt = "-"
            if _prev_dz_short_sig is not None:
                try:
                    _prev_dz_short_txt = f"{float(_prev_dz_short_sig):+.3f}"
                except Exception:
                    _prev_dz_short_txt = "-"
            _use_band_filter_disp = bool(sig_state.get("use_entry_band_filter", getattr(self.cfg, "USE_Z5_ENTRY_BAND_FILTER", True)))
            _use_anchor_polarity_disp = bool(sig_state.get("use_anchor_polarity_filter", getattr(self.cfg, "USE_DZ5_ANCHOR_POLARITY_FILTER", True)))
            try:
                gap_current_text = "-" if _price_range_pt is None else f"GAP {float(_price_range_pt):.2f}"
            except Exception:
                gap_current_text = "-"
            if not _price_range_on:
                gap_target_text = "OFF"
                gap_gate_ok = None
            else:
                gap_target_text = f"최근{_price_range_lb}봉 H-L >= {_price_range_min:.2f}"
                try:
                    gap_gate_ok = None if _price_range_pt is None else bool(float(_price_range_pt) >= float(_price_range_min))
                except Exception:
                    gap_gate_ok = bool(sig_state.get("entry_price_range_ok", False))
            # READY/title activation should use the same final strategy decision.
            # gate_ready still controls external execution availability display, but
            # the condition-satisfied message comes from strategy.py long_ok/short_ok.
            long_ready_display = bool(sig_state.get("long_ok", False) and gate_ready)
            short_ready_display = bool(sig_state.get("short_ok", False) and gate_ready)
            if _use_price_extrema_entry:
                _price_now_display = None
                for _px_candidate in (self.current_price, self.last_bar_close, self.session_close):
                    try:
                        if _px_candidate not in (None, 0) and float(_px_candidate) > 0.0:
                            _price_now_display = float(_px_candidate)
                            break
                    except Exception:
                        pass
                if _price_now_display in (None, 0):
                    try:
                        _bars1_values = list(getattr(self.st, "_bars1", {}).values())
                        if _bars1_values:
                            _last_bar_px = float(_bars1_values[-1].c)
                            if _last_bar_px > 0.0:
                                _price_now_display = _last_bar_px
                    except Exception:
                        pass
                _price_now_txt = "-" if _price_now_display in (None, 0) else f"{float(_price_now_display):.2f}"
                _long_anchor_display = sig_state.get("entry_price_anchor_high") if _entry_regime_live == "TREND" else sig_state.get("entry_price_anchor_low")
                _short_anchor_display = sig_state.get("entry_price_anchor_low") if _entry_regime_live == "TREND" else sig_state.get("entry_price_anchor_high")
                _long_lvl_txt = "-" if _long_anchor_display is None else f"{float(_long_anchor_display):.2f}"
                _short_lvl_txt = "-" if _short_anchor_display is None else f"{float(_short_anchor_display):.2f}"
                _long_fire_min_txt = "-" if _price_long_fire_min is None else f"{float(_price_long_fire_min):.2f}"
                _long_fire_max_txt = "-" if _price_long_fire_max is None else f"{float(_price_long_fire_max):.2f}"
                _short_fire_min_txt = "-" if _price_short_fire_min is None else f"{float(_price_short_fire_min):.2f}"
                _short_fire_max_txt = "-" if _price_short_fire_max is None else f"{float(_price_short_fire_max):.2f}"
                _range_txt = "-" if _price_range_pt is None else f"{float(_price_range_pt):.2f}"
                _prev_low_txt = "-" if _price_prev_low is None else f"{float(_price_prev_low):.2f}"
                _prev_high_txt = "-" if _price_prev_high is None else f"{float(_price_prev_high):.2f}"
                long_arm_state_text = (
                    "주문실행조건충족" if long_order_condition_hit else (
                        "진입권-실행대기" if long_arm_display_hit else (
                            "READY" if long_ready_display else "IDLE"
                        )
                    )
                )
                short_arm_state_text = (
                    "주문실행조건충족" if short_order_condition_hit else (
                        "진입권-실행대기" if short_arm_display_hit else (
                            "READY" if short_ready_display else "IDLE"
                        )
                    )
                )
                if _entry_regime_live == "TREND":
                    long_arm_target = (
                        f"기준: 직전{_price_entry_lb}봉 최고가 / 진입권 [{_long_fire_min_txt}, {_long_fire_max_txt}]"
                    )
                    short_arm_target = (
                        f"기준: 직전{_price_entry_lb}봉 최저가 / 진입권 [{_short_fire_min_txt}, {_short_fire_max_txt}]"
                    )
                    long_arm_current = (
                        f"현재 {_price_now_txt} / 기준H {_long_lvl_txt}"
                    )
                    short_arm_current = (
                        f"현재 {_price_now_txt} / 기준L {_short_lvl_txt}"
                    )
                else:
                    long_arm_target = (
                        f"기준: 직전{_price_entry_lb}봉 최저가 / 진입권 [{_long_fire_min_txt}, {_long_fire_max_txt}]"
                    )
                    short_arm_target = (
                        f"기준: 직전{_price_entry_lb}봉 최고가 / 진입권 [{_short_fire_min_txt}, {_short_fire_max_txt}]"
                    )
                    long_arm_current = (
                        f"현재 {_price_now_txt} / 기준L {_long_lvl_txt}"
                    )
                    short_arm_current = (
                        f"현재 {_price_now_txt} / 기준H {_short_lvl_txt}"
                    )
                if bool(sig_state.get("static_z5_band_enabled_for_entry", False)):
                    _long_band_low = min(float(_long_min), float(_long_max))
                    _long_band_high = max(float(_long_min), float(_long_max))
                    _short_band_low = min(float(_short_min), float(_short_max))
                    _short_band_high = max(float(_short_min), float(_short_max))
                    _use_hybrid_entry_regime_disp = bool(sig_state.get("use_hybrid_entry_regime", getattr(self.cfg, "USE_HYBRID_ENTRY_REGIME", False)))
                    if _use_hybrid_entry_regime_disp and _use_price_extrema_entry:
                        _rev_z5_min_disp = float(sig_state.get("reversal_z5_abs_min", _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MIN"))) or _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MIN")))
                        _rev_z5_max_disp = float(sig_state.get("reversal_z5_abs_max", _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MAX"))) or _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MAX")))
                        _trend_z5_min_disp = float(sig_state.get("trend_z5_abs_min", _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MIN"))) or _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MIN")))
                        _trend_z5_max_disp = float(sig_state.get("trend_z5_abs_max", _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MAX"))) or _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MAX")))
                        if _entry_regime_live == "REVERSAL":
                            _regime_target = f"|Z5| ≤ {_rev_z5_max_disp:.2f}"
                            _regime_ok = None if _z5_display_val is None else bool(sig_state.get("reversal_z5_ok", False))
                        elif _entry_regime_live == "TREND":
                            _regime_target = f"|Z5| ≤ {_trend_z5_max_disp:.2f}"
                            _regime_ok = None if _z5_display_val is None else bool(self.auto_on and _entry_regime_live == "TREND")
                        else:
                            _regime_target = f"|Z5| ≤ {_rev_z5_max_disp:.2f}"
                            _regime_ok = None
                        long_z5_target = _regime_target
                        short_z5_target = _regime_target
                        long_z5_current_text = f"{_z5_now_txt}"
                        short_z5_current_text = f"{_z5_now_txt}"
                        long_z5_display_ok = _regime_ok
                        short_z5_display_ok = _regime_ok
                    elif _z5_deadzone_abs > 0.0:
                        long_z5_target = f"{_long_band_low:+.2f} ~ {_long_band_high:+.2f} / |z5| >= {_z5_deadzone_abs:.2f}"
                        short_z5_target = f"{_short_band_low:+.2f} ~ {_short_band_high:+.2f} / |z5| >= {_z5_deadzone_abs:.2f}"
                    else:
                        long_z5_target = f"{_long_band_low:+.2f} ~ {_long_band_high:+.2f}"
                        short_z5_target = f"{_short_band_low:+.2f} ~ {_short_band_high:+.2f}"
                    if not (_use_hybrid_entry_regime_disp and _use_price_extrema_entry):
                        long_z5_current_text = f"{_z5_now_txt}"
                        short_z5_current_text = f"{_z5_now_txt}"
                        long_z5_display_ok = (None if _z5_display_val is None else bool(sig_state.get("long_band_ok", False)))
                        short_z5_display_ok = (None if _z5_display_val is None else bool(sig_state.get("short_band_ok", False)))
                else:
                    long_z5_target = gap_target_text if _price_range_on else "OFF"
                    short_z5_target = gap_target_text if _price_range_on else "OFF"
                    if _price_range_on:
                        long_z5_current_text = (
                            f"range {_range_txt} / prevL {_prev_low_txt} / 진입 {int(_price_long_fire_hit)}"
                        )
                        short_z5_current_text = (
                            f"range {_range_txt} / prevH {_prev_high_txt} / 진입 {int(_price_short_fire_hit)}"
                        )
                        long_z5_display_ok = gap_gate_ok
                        short_z5_display_ok = gap_gate_ok
                    else:
                        long_z5_current_text = "OFF"
                        short_z5_current_text = "OFF"
                        long_z5_display_ok = None
                        short_z5_display_ok = None
            else:
                long_arm_state_text = (
                    "주문실행조건충족" if long_order_condition_hit else (
                        "진입권-실행대기" if long_arm_display_hit else (
                            "READY" if long_ready_display else "IDLE"
                        )
                    )
                )
                short_arm_state_text = (
                    "주문실행조건충족" if short_order_condition_hit else (
                        "진입권-실행대기" if short_arm_display_hit else (
                            "READY" if short_ready_display else "IDLE"
                        )
                    )
                )
                if _entry_arm_swapped:
                    long_arm_target = f"기준: 이전{_ext_lb_disp}봉 z5 최저    /    ARM dz5 <= -0.15 / FIRE dz5 >= +0.30"
                    short_arm_target = f"기준: 이전{_ext_lb_disp}봉 z5 최고    /    ARM dz5 >= +0.15 / FIRE dz5 <= -0.30"
                    long_arm_current = f"FIRE {_dz_long_txt}    /    ARM {int(long_armed)} / age {long_arm_age}/{_arm_expiry}"
                    short_arm_current = f"FIRE {_dz_short_txt}    /    ARM {int(short_armed)} / age {short_arm_age}/{_arm_expiry}"
                else:
                    long_arm_target = f"기준: 이전{_ext_lb_disp}봉 z5 최저    /    ARM dz5 <= {_arm_long:+.2f} / FIRE dz5 >= {_entry_long_thr:+.2f}"
                    short_arm_target = f"기준: 이전{_ext_lb_disp}봉 z5 최고    /    ARM dz5 >= {_arm_short:+.2f} / FIRE dz5 <= {_entry_short_thr:+.2f}"
                    long_arm_current = f"FIRE {_dz_long_txt}    /    ARM {int(long_armed)} / age {long_arm_age}/{_arm_expiry}"
                    short_arm_current = f"FIRE {_dz_short_txt}    /    ARM {int(short_armed)} / age {short_arm_age}/{_arm_expiry}"
                if bool(sig_state.get("static_z5_band_enabled_for_entry", False)):
                    _long_band_low = min(float(_long_min), float(_long_max))
                    _long_band_high = max(float(_long_min), float(_long_max))
                    _short_band_low = min(float(_short_min), float(_short_max))
                    _short_band_high = max(float(_short_min), float(_short_max))
                    if _z5_deadzone_abs > 0.0:
                        long_z5_target = f"{_long_band_low:+.2f} ~ {_long_band_high:+.2f} / |z5| >= {_z5_deadzone_abs:.2f}"
                        short_z5_target = f"{_short_band_low:+.2f} ~ {_short_band_high:+.2f} / |z5| >= {_z5_deadzone_abs:.2f}"
                    else:
                        long_z5_target = f"{_long_band_low:+.2f} ~ {_long_band_high:+.2f}"
                        short_z5_target = f"{_short_band_low:+.2f} ~ {_short_band_high:+.2f}"
                    long_z5_current_text = f"{_z5_now_txt}"
                    short_z5_current_text = f"{_z5_now_txt}"
                    long_z5_display_ok = None
                    short_z5_display_ok = None
                else:
                    long_z5_target = "Z5 BAND: 표시용 / ENTRY 차단 OFF"
                    short_z5_target = "Z5 BAND: 표시용 / ENTRY 차단 OFF"
                    long_z5_current_text = f"{_z5_now_txt}"
                    short_z5_current_text = f"{_z5_now_txt}"
                    long_z5_display_ok = None
                    short_z5_display_ok = None

            # 청산 상태 텍스트 (MFE/손절/정체 기준)
            if not self.auto_on:
                dmi_text = "대기중"
            elif self.position_side not in ("LONG", "SHORT"):
                dmi_text = "포지션 없음"
            elif self.last_exit_ok is None:
                if isinstance(mfe_retrace_info, dict) and float(mfe_retrace_info.get("mfe_pt", 0.0) or 0.0) > 0.0:
                    cut_px = float(mfe_retrace_info.get("cut_price", 0.0) or 0.0)
                    mfe_pt = float(mfe_retrace_info.get("mfe_pt", 0.0) or 0.0)
                    retrace_gap_pt = float(mfe_retrace_info.get("required_retracement_pt", mfe_retrace_info.get("retrace_gap_pt", 0.0)) or 0.0)
                    state = "추적중" if bool(mfe_retrace_info.get("armed", False)) else "대기중"
                    _stage = str(mfe_retrace_info.get("mfe_stage") or "WAIT")
                    if bool(mfe_retrace_info.get("armed", False)):
                        dmi_text = f"MFE_TRAIL {state} / {_stage} / cut {cut_px:.2f} / mfe {mfe_pt:.2f} / gap {retrace_gap_pt:.2f}"
                    else:
                        dmi_text = f"MFE_TRAIL {state} / {_stage} / cut 대기 / mfe {mfe_pt:.2f} / gap {retrace_gap_pt:.2f}"
                else:
                    dmi_text = "청산 대기"
            elif self.last_exit_ok:
                dmi_text = "청산조건 충족"
            else:
                if isinstance(mfe_retrace_info, dict) and float(mfe_retrace_info.get("mfe_pt", 0.0) or 0.0) > 0.0:
                    cut_px = float(mfe_retrace_info.get("cut_price", 0.0) or 0.0)
                    mfe_pt = float(mfe_retrace_info.get("mfe_pt", 0.0) or 0.0)
                    retrace_gap_pt = float(mfe_retrace_info.get("required_retracement_pt", mfe_retrace_info.get("retrace_gap_pt", 0.0)) or 0.0)
                    state = "미충족" if bool(mfe_retrace_info.get("armed", False)) else "대기중"
                    _stage = str(mfe_retrace_info.get("mfe_stage") or "WAIT")
                    if bool(mfe_retrace_info.get("armed", False)):
                        dmi_text = f"MFE_TRAIL {state} / {_stage} / cut {cut_px:.2f} / mfe {mfe_pt:.2f} / gap {retrace_gap_pt:.2f}"
                    else:
                        dmi_text = f"MFE_TRAIL {state} / {_stage} / cut 대기 / mfe {mfe_pt:.2f} / gap {retrace_gap_pt:.2f}"
                else:
                    dmi_text = "청산조건 미충족"

            # EXIT_Z / REVERSE_SIGNAL rows were removed from the dashboard EXIT panel.
            exit_z_status_text = ""
            exit_z_status_active = None
            exit_z_long_status_payload = None
            exit_z_short_status_payload = None
            reverse_signal_text = ""
            reverse_signal_active = None

            def _fmt_entry_level(_v):
                try:
                    return "-" if _v is None else f"{float(_v):.2f}"
                except Exception:
                    return "-"

            def _fmt_entry_float(_v, _digits=2, _signed=False):
                try:
                    if _v is None:
                        return "-"
                    return (f"{float(_v):+.{_digits}f}" if _signed else f"{float(_v):.{_digits}f}")
                except Exception:
                    return "-"

            entry_zone_regime = self._resolve_entry_regime(sig_state, default="BLOCKED") or "BLOCKED"
            entry_regime = str(
                sig_state.get("active_entry_regime") or entry_zone_regime
            ).strip().upper()
            if entry_regime not in ("TREND", "REVERSAL", "BLOCKED"):
                entry_regime = entry_zone_regime
            # AUTO controls order submission only. Keep the live monitoring
            # profile visible so ARM/FIRE values continue updating while OFF.
            entry_regime_for_display = entry_regime
            entry_regime_label = str(sig_state.get("entry_regime_label") or "")
            if not entry_regime_label:
                entry_regime_label = {
                    "TREND": "BAND2 / 추세형",
                    "REVERSAL": "BAND1 / 반전형",
                    "BLOCKED": "BAND BLOCK",
                    "OFF": "AUTO OFF",
                }.get(entry_regime_for_display, entry_regime_for_display)

            rev_z5_min = float(sig_state.get("reversal_z5_abs_min", _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MIN"))) or _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MIN")))
            rev_z5_max = float(sig_state.get("reversal_z5_abs_max", _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MAX"))) or _cfg_num("ENTRY_REGIME_REVERSAL_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_REVERSAL_Z5_ABS_MAX")))
            trend_z5_min = float(sig_state.get("trend_z5_abs_min", _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MIN"))) or _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MIN", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MIN")))
            trend_z5_max = float(sig_state.get("trend_z5_abs_max", _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MAX"))) or _cfg_num("ENTRY_REGIME_TREND_Z5_ABS_MAX", getattr(self.cfg, "ENTRY_REGIME_TREND_Z5_ABS_MAX")))
            rev_off = float(sig_state.get("reversal_price_extrema_offset_pt", _cfg_num("REVERSAL_PRICE_EXTREMA_OFFSET_PT", 0.00)))
            rev_fire_min = float(sig_state.get("reversal_price_fire_band_min_pt", _cfg_num("REVERSAL_PRICE_FIRE_BAND_MIN_PT", 0.30)) or _cfg_num("REVERSAL_PRICE_FIRE_BAND_MIN_PT", 0.30))
            rev_fire_max = float(sig_state.get("reversal_price_fire_band_max_pt", _cfg_num("REVERSAL_PRICE_FIRE_BAND_MAX_PT", 0.70)) or _cfg_num("REVERSAL_PRICE_FIRE_BAND_MAX_PT", 0.70))
            trend_off = float(sig_state.get("trend_price_extrema_offset_pt", _cfg_num("TREND_PRICE_EXTREMA_OFFSET_PT", 0.60)))
            trend_fire_min = float(sig_state.get("trend_price_fire_band_min_pt", _cfg_num("TREND_PRICE_FIRE_BAND_MIN_PT", 0.20)) or _cfg_num("TREND_PRICE_FIRE_BAND_MIN_PT", 0.20))
            trend_fire_max = float(sig_state.get("trend_price_fire_band_max_pt", _cfg_num("TREND_PRICE_FIRE_BAND_MAX_PT", 0.20)) or _cfg_num("TREND_PRICE_FIRE_BAND_MAX_PT", 0.20))
            _entry_lb_disp = int(
                sig_state.get(
                    "trend_price_extrema_lookback_bars" if entry_regime_for_display == "TREND" else "price_extrema_lookback_bars",
                    getattr(self.cfg, "TREND_PRICE_EXTREMA_LOOKBACK_BARS", 3) if entry_regime_for_display == "TREND" else getattr(self.cfg, "PRICE_EXTREMA_LOOKBACK_BARS", 3),
                )
                or (getattr(self.cfg, "TREND_PRICE_EXTREMA_LOOKBACK_BARS", 3) if entry_regime_for_display == "TREND" else getattr(self.cfg, "PRICE_EXTREMA_LOOKBACK_BARS", 3))
            )
            _price_now_for_entry = None
            for _cand in (sig_state.get("current_price"), self.current_price, self.last_bar_close):
                try:
                    if _cand is not None:
                        _price_now_for_entry = float(_cand)
                        break
                except Exception:
                    pass
            prev5_hi_txt = _fmt_entry_level(prev5_hi)
            prev5_lo_txt = _fmt_entry_level(prev5_lo)
            trend_active = bool(entry_regime == "TREND")
            reversal_active = bool(entry_regime == "REVERSAL")
            common_target = f"Z5 {_z5_now_txt}"

            _display_candle_labels = str(
                sig_state.get("entry_prev_closed_candle_display_labels")
                or sig_state.get("trend_prev_closed_candle_labels")
                or ""
            )
            _trend_long_prereq_regime = bool(sig_state.get("trend_long_prerequisite_met", False))
            _trend_short_prereq_regime = bool(sig_state.get("trend_short_prerequisite_met", False))
            _trend_prev_prereq_enabled_regime = bool(
                sig_state.get("trend_prev_closed_candle_prerequisite_enabled", True)
            )
            _trend_four_same_blocked_regime = bool(
                sig_state.get("trend_consecutive_same_4_blocked", False)
            )
            _trend_any_direction_allowed_regime = bool(
                not _trend_four_same_blocked_regime
                and (
                    not _trend_prev_prereq_enabled_regime
                    or _trend_long_prereq_regime
                    or _trend_short_prereq_regime
                )
            )
            # ENTRY REGIME shows the physical Z5 zone.  The execution profile is
            # deliberately shown separately in ENTRY H/L DETAIL below.
            _trend_regime_ok = bool(
                entry_zone_regime == "TREND"
                and _trend_any_direction_allowed_regime
            )
            _reversal_regime_ok = bool(entry_zone_regime == "REVERSAL")
            _regime_active_any = bool(_trend_regime_ok or _reversal_regime_ok)
            if _trend_regime_ok:
                common_current = "추세형"
                entry_regime_label = "추세형"
            elif _reversal_regime_ok:
                common_current = "반전형"
                entry_regime_label = "반전형"
            else:
                common_current = "BLOCK"
                entry_regime_label = "BLOCK"
            _trend_dots = regime_candle_dots(
                _display_candle_labels, 5, split_last_two=True
            )
            _reversal_dot_count = max(
                1,
                int(
                    sig_state.get(
                        "reversal_prev_closed_candle_lookback_bars",
                        getattr(self.cfg, "REVERSAL_PREV_CANDLE_LOOKBACK_BARS", 3),
                    )
                    or 3
                ),
            )
            _reversal_dots = regime_candle_dots(
                _display_candle_labels, _reversal_dot_count
            )
            trend_current = (
                f"{_trend_dots} | "
                f"{'OKAY' if _trend_regime_ok else 'BLOCK'}"
            )
            # Same-direction completed candles can promote TREND throughout the
            # shared entry cap, including the physical REVERSAL band.
            trend_target = f"{trend_z5_min:.2f} < |Z5| ≤ {trend_z5_max:.2f}"
            reversal_target = f"|Z5| ≤ {rev_z5_max:.2f}"
            reversal_current = (
                f"{_reversal_dots} | "
                f"{'OKAY' if _reversal_regime_ok else 'BLOCK'}"
            )
            def _fmt_z_metric(value):
                try:
                    return f"{float(value):+.3f}" if value is not None else "-"
                except Exception:
                    return "-"

            _order_source_label = str(
                sig_state.get("entry_source")
                or getattr(self.cfg, "ENTRY_SIGNAL_SOURCE", "z1")
                or "z1"
            ).strip().upper()
            _prev_long_allowed = bool(
                sig_state.get("prev_candle_long_entry_allowed", True)
            )
            _prev_short_allowed = bool(
                sig_state.get("prev_candle_short_entry_allowed", True)
            )
            _prev_candle_block_enabled = bool(
                sig_state.get("use_prev_candle_opposite_entry_block", False)
            )
            _prev_candle_dots = regime_candle_dots(
                _display_candle_labels, 3
            )
            _long_delta_active = bool(
                sig_state.get("z1_delta_long_armed", False)
                or sig_state.get("z1_delta_long_fire", False)
                or sig_state.get("z5_monitor_long_armed", False)
                or sig_state.get("z5_monitor_long_fire", False)
            )
            _short_delta_active = bool(
                sig_state.get("z1_delta_short_armed", False)
                or sig_state.get("z1_delta_short_fire", False)
                or sig_state.get("z5_monitor_short_armed", False)
                or sig_state.get("z5_monitor_short_fire", False)
            )
            _prev_candle_row_ok = (
                None if not _prev_candle_block_enabled
                else _prev_long_allowed if _long_delta_active and not _short_delta_active
                else _prev_short_allowed if _short_delta_active and not _long_delta_active
                else None
            )
            _prev_candle_current = (
                f"{_prev_candle_dots} | "
                f"{'OFF' if not _prev_candle_block_enabled else ('BLOCK' if _prev_candle_row_ok is False else 'OKAY')}"
            )

            entry_regime_rows = {
                "common": {
                    "current": "추세형 DELTA",
                    "target": f"주문소스 {_order_source_label}",
                    "ok": bool(entry_zone_regime in {"TREND", "REVERSAL"}),
                },
                "z1": {
                    "current": f"Δ {_fmt_z_metric(sig_state.get('dz1_prev_delta'))}",
                    "target": f"Z1 {_fmt_z_metric(sig_state.get('z1'))}",
                    "ok": bool(sig_state.get('z1') is not None),
                },
                "prev_candle": {
                    "current": _prev_candle_current,
                    "target": ("몸통 |C-O| ≥ 0.60pt" if _prev_candle_block_enabled else "OFF"),
                    "ok": _prev_candle_row_ok,
                },
            }

            def _entry_detail_row(active: bool, armed: bool, fire: bool, current: str, target: str):
                if fire:
                    _ok = True
                elif active and armed:
                    # ARM blink must remain visible before FIRE.  If the strategy
                    # releases the ARM latch before FIRE, armed=False and this row
                    # immediately turns off.
                    _ok = True
                elif active:
                    _ok = False
                else:
                    _ok = None
                _fire_band = str(target or "-").strip()
                _integrated = f"{current} | F:{_fire_band}"
                return {"current": _integrated, "target": "", "ok": _ok, "blink": bool(active and armed and not fire)}

            # Dashboard condition colors must mirror the final strategy gate used
            # for ENTRY submission.  Raw FIRE hit can be true while final long_ok/
            # short_ok is false due to both-fire overlap, Z5 band, MACD/EMA/range
            # filters, or other strategy-side gates.
            _entry_long_final_ok = bool(sig_state.get("long_ok", False))
            _entry_short_final_ok = bool(sig_state.get("short_ok", False))
            _long_arm_regime = str(sig_state.get("long_arm_entry_regime") or "-").upper()
            _short_arm_regime = str(sig_state.get("short_arm_entry_regime") or "-").upper()
            _trend_anchor_src = str(sig_state.get("trend_entry_price_anchor_source") or "TREND_CONFIRMED5_OC_BODY_HL_EX_CURRENT")
            _rev_anchor_src = str(sig_state.get("reversal_entry_price_anchor_source") or "REV_CONFIRMED3_PLUS_CURRENT_HL_15S_CANDIDATE")
            def _entry_anchor_delta_txt(_anchor):
                try:
                    if _anchor is None or _price_now_for_entry is None:
                        return "-"
                    return f"{(_price_now_for_entry - float(_anchor)):+.2f}"
                except Exception:
                    return "-"

            _trend_h = sig_state.get('trend_entry_price_anchor_high')
            _trend_l = sig_state.get('trend_entry_price_anchor_low')
            _rev_h = sig_state.get('reversal_entry_price_anchor_high')
            _rev_l = sig_state.get('reversal_entry_price_anchor_low')

            _rev_long_armed = bool(long_armed and _long_arm_regime == 'REVERSAL')
            _rev_short_armed = bool(short_armed and _short_arm_regime == 'REVERSAL')
            _rev_rolling_enabled = bool(sig_state.get("reversal_arm_rolling_15s_enabled", False))
            _rev_15s_countdown = sig_state.get("reversal_15s_countdown_sec")

            # ENTRY DETAIL mirrors strategy.py without merging the DETAIL columns.
            # IDLE: show confirmed H/L plus the first-touch ARM line.
            # ACTIVE: show the active anchor only: 신저/신고 or 신ARM + 대기/감시 countdown.
            def _entry_level_add(_anchor, _delta):
                try:
                    if _anchor is None:
                        return "-"
                    return _fmt_entry_level(float(_anchor) + float(_delta))
                except Exception:
                    return "-"

            _trend_long_arm_price = _entry_level_add(_trend_h, -trend_off)
            _trend_short_arm_price = _entry_level_add(_trend_l, trend_off)
            _trend_long_armed = bool(long_armed and _long_arm_regime == 'TREND')
            _trend_short_armed = bool(short_armed and _short_arm_regime == 'TREND')
            _trend_long_countdown = sig_state.get("trend_long_candidate_countdown_sec")
            _trend_short_countdown = sig_state.get("trend_short_candidate_countdown_sec")
            _trend_15s_countdown = sig_state.get("trend_15s_countdown_sec")
            _trend_long_stage = sig_state.get("trend_long_candidate_stage")
            _trend_short_stage = sig_state.get("trend_short_candidate_stage")
            _trend_long_active_anchor = sig_state.get("long_arm_z5_low_anchor")
            _trend_short_active_anchor = sig_state.get("short_arm_z5_high_anchor")
            _trend_wait_watch_enabled = bool(sig_state.get("trend_arm_wait_watch_enabled", False))
            _trend_15s_bucket_high = sig_state.get("trend_15s_bucket_high")
            _trend_15s_bucket_low = sig_state.get("trend_15s_bucket_low")
            _trend_rule_active = bool(
                str(sig_state.get("active_entry_regime") or "").strip().upper() == "TREND"
            )
            _trend_prev_label = str(
                sig_state.get("trend_prev_closed_candle_labels")
                or sig_state.get("trend_prev_closed_candle_label")
                or ""
            )
            _trend_prev_prereq_enabled = bool(
                sig_state.get("trend_prev_closed_candle_prerequisite_enabled", True)
            )
            _trend_prev_condition_mode = str(
                sig_state.get("trend_prev_closed_candle_condition_mode") or "FORWARD"
            ).strip().upper()
            _trend_long_prereq = bool(sig_state.get("trend_long_prerequisite_met", False))
            _trend_short_prereq = bool(sig_state.get("trend_short_prerequisite_met", False))

            # Only TREND rows may show the completed-candle prerequisite.
            # When REVERSAL is the active rule, both TREND rows stay on confirmed H/L.
            if _trend_rule_active and _trend_prev_prereq_enabled and _trend_long_prereq and _trend_prev_label in {"양봉", "음봉"}:
                _trend_long_value = (
                    _fmt_entry_level(_trend_long_active_anchor)
                    if _trend_long_armed else _trend_long_arm_price
                )
                trend_long_current = f"A:{_trend_long_value}"
            elif _trend_rule_active and _trend_long_armed:
                trend_long_current = (
                    f"WAIT:{'ON' if _trend_wait_watch_enabled else 'OFF'} "
                    f"A:{_fmt_entry_level(_trend_long_active_anchor)} "
                    f"R15H:{_fmt_entry_level(_trend_15s_bucket_high)}"
                )
            else:
                trend_long_current = f"H:{_fmt_entry_level(_trend_h)} A:{_trend_long_arm_price}"
            if _trend_long_armed and _trend_15s_countdown is not None:
                trend_long_current = f"{trend_long_current}{trend_arm_timer_display_suffix(_trend_long_stage, _trend_long_countdown, _trend_15s_countdown)}"
            _trend_long_activation_stage = (
                "비활성" if not _trend_rule_active else
                ("FIRE" if _entry_long_final_ok else
                ("직전봉 차단" if not _trend_long_prereq else
                 ("ARM15" if _trend_long_armed else "ARM 대기")))
            )
            trend_long_current = (
                f"{_trend_long_activation_stage} | "
                f"{trend_long_current}"
            )

            if _trend_rule_active and _trend_prev_prereq_enabled and _trend_short_prereq and _trend_prev_label in {"양봉", "음봉"}:
                _trend_short_value = (
                    _fmt_entry_level(_trend_short_active_anchor)
                    if _trend_short_armed else _trend_short_arm_price
                )
                trend_short_current = f"A:{_trend_short_value}"
            elif _trend_rule_active and _trend_short_armed:
                trend_short_current = (
                    f"WAIT:{'ON' if _trend_wait_watch_enabled else 'OFF'} "
                    f"A:{_fmt_entry_level(_trend_short_active_anchor)} "
                    f"R15L:{_fmt_entry_level(_trend_15s_bucket_low)}"
                )
            else:
                trend_short_current = f"L:{_fmt_entry_level(_trend_l)} A:{_trend_short_arm_price}"
            if _trend_short_armed and _trend_15s_countdown is not None:
                trend_short_current = f"{trend_short_current}{trend_arm_timer_display_suffix(_trend_short_stage, _trend_short_countdown, _trend_15s_countdown)}"
            _trend_short_activation_stage = (
                "비활성" if not _trend_rule_active else
                ("FIRE" if _entry_short_final_ok else
                ("직전봉 차단" if not _trend_short_prereq else
                 ("ARM15" if _trend_short_armed else "ARM 대기")))
            )
            trend_short_current = (
                f"{_trend_short_activation_stage} | "
                f"{trend_short_current}"
            )

            _rev_sample_countdown = sig_state.get("reversal_live_sample_countdown_sec")
            _rev_sample_side = str(sig_state.get("reversal_extrema_display_side") or "").upper()
            _rev_long_countdown = sig_state.get("reversal_long_candidate_countdown_sec")
            _rev_short_countdown = sig_state.get("reversal_short_candidate_countdown_sec")
            _rev_long_stage = sig_state.get("reversal_long_candidate_stage")
            _rev_short_stage = sig_state.get("reversal_short_candidate_stage")
            _rev_active_side = str(sig_state.get("reversal_active_candidate_side") or "").upper()
            # Recalculate only the visible countdown from the candidate window.
            # This prevents the DETAIL row from losing/fixing the 대기→감시 text
            # when a render occurs between strategy snapshot updates.
            _rev_long_stage, _rev_long_countdown = reversal_candidate_display_stage(sig_state, "LONG")
            _rev_short_stage, _rev_short_countdown = reversal_candidate_display_stage(sig_state, "SHORT")
            _rev_long_candidate_anchor = sig_state.get("reversal_long_candidate_anchor") if _rev_active_side == "LONG" else None
            _rev_short_candidate_anchor = sig_state.get("reversal_short_candidate_anchor") if _rev_active_side == "SHORT" else None
            _rev_long_arm_anchor = sig_state.get("long_arm_z5_low_anchor")
            _rev_short_arm_anchor = sig_state.get("short_arm_z5_high_anchor")
            _rev_long_armed = bool(long_armed and _long_arm_regime == 'REVERSAL')
            _rev_short_armed = bool(short_armed and _short_arm_regime == 'REVERSAL')
            _rev_long_arm15_countdown = None
            _rev_short_arm15_countdown = None
            if _rev_rolling_enabled and _rev_long_armed:
                _rev_long_stage, _rev_long_countdown = "ARM15", None
                _rev_long_arm15_countdown = _rev_15s_countdown
            if _rev_rolling_enabled and _rev_short_armed:
                _rev_short_stage, _rev_short_countdown = "ARM15", None
                _rev_short_arm15_countdown = _rev_15s_countdown
            _rev_long_idle_arm = _entry_level_add(_rev_l, rev_off)
            _rev_short_idle_arm = _entry_level_add(_rev_h, -rev_off)
            _rev_prev_label = str(
                sig_state.get("reversal_prev_closed_candle_labels")
                or sig_state.get("reversal_prev_closed_candle_label")
                or ""
            )
            _rev_prev_condition_mode = str(
                sig_state.get("reversal_prev_closed_candle_condition_mode") or "OFF"
            ).strip().upper()
            _rev_long_prereq = bool(sig_state.get("reversal_long_prerequisite_met", False))
            _rev_short_prereq = bool(sig_state.get("reversal_short_prerequisite_met", False))

            def _reversal_stage_text(_active, _fire, _allowed, _armed, _anchor, _stage, _countdown, _idle):
                if not _active:
                    return "비활성"
                if _fire:
                    return "FIRE"
                if not _allowed:
                    return "직전봉 차단"
                _stage_text = str(_stage or "").strip()
                if _stage_text:
                    if _countdown is not None:
                        return f"{_stage_text} {int(_countdown)}s"
                    return _stage_text
                if _armed:
                    return "ARM"
                if _anchor is not None:
                    return "감시 준비"
                return _idle

            if _rev_active_side == "LONG" and (_rev_long_armed or _rev_long_candidate_anchor is not None):
                _a = _rev_long_arm_anchor if _rev_long_armed and _rev_long_arm_anchor is not None else _rev_long_candidate_anchor
                rev_long_current = f"A:{_fmt_entry_level(_a)}"
            else:
                rev_long_current = f"L:{_fmt_entry_level(_rev_l)} A:{_rev_long_idle_arm}"
            if _rev_sample_side == "LONG" and _rev_sample_countdown is not None:
                rev_long_current = f"{rev_long_current} {int(_rev_sample_countdown)}s"
            if _rev_long_armed and _rev_long_arm15_countdown is not None:
                rev_long_current = f"{rev_long_current} {int(_rev_long_arm15_countdown)}s"
            _rev_long_activation_stage = _reversal_stage_text(
                reversal_active,
                _entry_long_final_ok,
                _rev_long_prereq,
                _rev_long_armed,
                _rev_long_candidate_anchor,
                _rev_long_stage,
                _rev_long_countdown,
                "신저가 대기",
            )
            rev_long_current = (
                f"{_rev_long_activation_stage} | "
                f"{rev_long_current}"
            )

            if _rev_active_side == "SHORT" and (_rev_short_armed or _rev_short_candidate_anchor is not None):
                _a = _rev_short_arm_anchor if _rev_short_armed and _rev_short_arm_anchor is not None else _rev_short_candidate_anchor
                rev_short_current = f"A:{_fmt_entry_level(_a)}"
            else:
                rev_short_current = f"H:{_fmt_entry_level(_rev_h)} A:{_rev_short_idle_arm}"
            if _rev_sample_side == "SHORT" and _rev_sample_countdown is not None:
                rev_short_current = f"{rev_short_current} {int(_rev_sample_countdown)}s"
            if _rev_short_armed and _rev_short_arm15_countdown is not None:
                rev_short_current = f"{rev_short_current} {int(_rev_short_arm15_countdown)}s"
            _rev_short_activation_stage = _reversal_stage_text(
                reversal_active,
                _entry_short_final_ok,
                _rev_short_prereq,
                _rev_short_armed,
                _rev_short_candidate_anchor,
                _rev_short_stage,
                _rev_short_countdown,
                "신고가 대기",
            )
            rev_short_current = (
                f"{_rev_short_activation_stage} | "
                f"{rev_short_current}"
            )

            def _arm_price_value(_active_anchor, _idle_anchor, _idle_delta):
                try:
                    if _active_anchor is not None:
                        return float(_active_anchor)
                    if _idle_anchor is not None:
                        return float(_idle_anchor) + float(_idle_delta)
                except Exception:
                    return None
                return None

            def _absolute_fire_band(_arm_price, _delta_a, _delta_b):
                try:
                    _arm = float(_arm_price)
                    _p1 = _arm + float(_delta_a)
                    _p2 = _arm + float(_delta_b)
                    _low, _high = sorted((_p1, _p2))
                    return f"{_fmt_entry_level(_low)}~{_fmt_entry_level(_high)}"
                except Exception:
                    return "계산대기"

            _trend_long_fire_arm = _arm_price_value(
                _trend_long_active_anchor if _trend_long_armed else None,
                _trend_h,
                -trend_off,
            )
            _trend_short_fire_arm = _arm_price_value(
                _trend_short_active_anchor if _trend_short_armed else None,
                _trend_l,
                trend_off,
            )
            _rev_long_fire_arm = _arm_price_value(
                (_rev_long_arm_anchor if _rev_long_armed else _rev_long_candidate_anchor),
                _rev_l,
                rev_off,
            )
            _rev_short_fire_arm = _arm_price_value(
                (_rev_short_arm_anchor if _rev_short_armed else _rev_short_candidate_anchor),
                _rev_h,
                -rev_off,
            )

            trend_long_target = _absolute_fire_band(_trend_long_fire_arm, trend_fire_min, trend_fire_max)
            trend_short_target = _absolute_fire_band(_trend_short_fire_arm, -trend_fire_max, -trend_fire_min)
            rev_long_target = _absolute_fire_band(_rev_long_fire_arm, rev_fire_min, rev_fire_max)
            rev_short_target = _absolute_fire_band(_rev_short_fire_arm, -rev_fire_max, -rev_fire_min)

            _reversal_zone_uses_trend_detail = bool(
                sig_state.get(
                    "use_trend_rules_in_reversal_z5_zone",
                    getattr(self.cfg, "USE_TREND_RULES_IN_REVERSAL_Z5_ZONE", False),
                )
            )
            _trend_zone_detail_active = bool(
                self.auto_on
                and entry_zone_regime == "TREND"
                and entry_regime == "TREND"
            )
            _reversal_zone_trend_detail_active = bool(
                self.auto_on
                and entry_zone_regime == "REVERSAL"
                and entry_regime == "TREND"
                and _reversal_zone_uses_trend_detail
            )
            _inactive_trend_long_current = (
                f"\ube44\ud65c\uc131 | H:{_fmt_entry_level(_trend_h)} "
                f"A:{_trend_long_arm_price}"
            )
            _inactive_trend_short_current = (
                f"\ube44\ud65c\uc131 | L:{_fmt_entry_level(_trend_l)} "
                f"A:{_trend_short_arm_price}"
            )

            entry_detail_rows = {
                "trend_long": _entry_detail_row(
                    _trend_zone_detail_active,
                    bool(_trend_zone_detail_active and _trend_long_armed),
                    bool(_trend_zone_detail_active and _entry_long_final_ok),
                    trend_long_current if _trend_zone_detail_active else _inactive_trend_long_current,
                    trend_long_target,
                ),
                "trend_short": _entry_detail_row(
                    _trend_zone_detail_active,
                    bool(_trend_zone_detail_active and _trend_short_armed),
                    bool(_trend_zone_detail_active and _entry_short_final_ok),
                    trend_short_current if _trend_zone_detail_active else _inactive_trend_short_current,
                    trend_short_target,
                ),
                "reversal_long": _entry_detail_row(
                    _reversal_zone_trend_detail_active if _reversal_zone_uses_trend_detail else reversal_active,
                    bool(
                        (_reversal_zone_trend_detail_active and _trend_long_armed)
                        if _reversal_zone_uses_trend_detail
                        else (long_armed and _long_arm_regime == "REVERSAL")
                    ),
                    bool(
                        (_reversal_zone_trend_detail_active and _entry_long_final_ok)
                        if _reversal_zone_uses_trend_detail
                        else (reversal_active and _entry_long_final_ok)
                    ),
                    (
                        trend_long_current
                        if _reversal_zone_trend_detail_active
                        else _inactive_trend_long_current
                    ) if _reversal_zone_uses_trend_detail else rev_long_current,
                    trend_long_target if _reversal_zone_uses_trend_detail else rev_long_target,
                ),
                "reversal_short": _entry_detail_row(
                    _reversal_zone_trend_detail_active if _reversal_zone_uses_trend_detail else reversal_active,
                    bool(
                        (_reversal_zone_trend_detail_active and _trend_short_armed)
                        if _reversal_zone_uses_trend_detail
                        else (short_armed and _short_arm_regime == "REVERSAL")
                    ),
                    bool(
                        (_reversal_zone_trend_detail_active and _entry_short_final_ok)
                        if _reversal_zone_uses_trend_detail
                        else (reversal_active and _entry_short_final_ok)
                    ),
                    (
                        trend_short_current
                        if _reversal_zone_trend_detail_active
                        else _inactive_trend_short_current
                    ) if _reversal_zone_uses_trend_detail else rev_short_current,
                    trend_short_target if _reversal_zone_uses_trend_detail else rev_short_target,
                ),
            }

            # Keep the existing four-row DETAIL geometry, but expose the two
            # delta reversal state machines directly. Z1 is the live entry
            # source; Z5 remains visible as a read-only reference.
            _dz1_display = sig_state.get("dz1_prev_delta")
            _dz5_display = sig_state.get("dz5_prev_delta")
            _z1_arm = abs(float(sig_state.get(
                "z1_delta_reversal_arm",
                getattr(self.cfg, "Z1_DELTA_REVERSAL_ARM", 0.2),
            ) or 0.2))
            _z1_fire_raw = sig_state.get(
                "z1_delta_reversal_fire",
                getattr(self.cfg, "Z1_DELTA_REVERSAL_FIRE", 1.0),
            )
            _z1_fire = abs(float(1.0 if _z1_fire_raw is None else _z1_fire_raw))
            _z1_fire_min = abs(float(sig_state.get(
                "z1_delta_reversal_fire_min",
                getattr(self.cfg, "Z1_DELTA_REVERSAL_FIRE_MIN", 0.1),
            ) or 0.1))
            _z1_fire_max = abs(float(sig_state.get(
                "z1_delta_reversal_fire_max",
                getattr(self.cfg, "Z1_DELTA_REVERSAL_FIRE_MAX", _z1_fire),
            ) or _z1_fire))
            _z1_fire_min, _z1_fire_max = sorted((_z1_fire_min, _z1_fire_max))
            _z5_arm = abs(float(getattr(self.cfg, "Z5_DELTA_REVERSAL_ARM", 0.2) or 0.2))
            _z5_fire_raw = getattr(self.cfg, "Z5_DELTA_REVERSAL_FIRE", 1.0)
            _z5_fire = abs(float(1.0 if _z5_fire_raw is None else _z5_fire_raw))
            _z5_fire_min = abs(float(sig_state.get(
                "z5_delta_reversal_fire_min",
                getattr(self.cfg, "Z5_DELTA_REVERSAL_FIRE_MIN", 0.1),
            ) or 0.1))
            _z5_fire_max = abs(float(sig_state.get(
                "z5_delta_reversal_fire_max",
                getattr(self.cfg, "Z5_DELTA_REVERSAL_FIRE_MAX", _z5_fire),
            ) or _z5_fire))
            _z5_fire_min, _z5_fire_max = sorted((_z5_fire_min, _z5_fire_max))

            # REVERSE_SIGNAL uses the opposite normal ENTRY FIRE.  Keep the
            # EXIT dashboard on the same active delta source and thresholds;
            # never display a stale hard-coded Z5 band while Z1 owns ENTRY.
            _reverse_exit_uses_z1 = bool(sig_state.get(
                "use_z1_delta_reversal_entry",
                getattr(self.cfg, "USE_Z1_DELTA_REVERSAL_ENTRY", False),
            ))
            if _reverse_exit_uses_z1:
                _reverse_exit_delta_label = "ΔZ1"
                _reverse_exit_delta_value = _dz1_display
                _reverse_exit_fire_min = _z1_fire_min
                _reverse_exit_fire_max = _z1_fire_max
            else:
                _reverse_exit_delta_label = "ΔZ5"
                _reverse_exit_delta_value = _dz5_display
                _reverse_exit_fire_min = _z5_fire_min
                _reverse_exit_fire_max = _z5_fire_max
            if in_pos > 0:
                _reverse_exit_fire_text = f"-{_reverse_exit_fire_max:.2f}~-{_reverse_exit_fire_min:.2f}"
            elif in_pos < 0:
                _reverse_exit_fire_text = f"+{_reverse_exit_fire_min:.2f}~+{_reverse_exit_fire_max:.2f}"
            else:
                _reverse_exit_fire_text = f"±{_reverse_exit_fire_min:.2f}~{_reverse_exit_fire_max:.2f}"

            def _delta_detail_payload(
                delta_value,
                arm_text: str,
                fire_text: str,
                arm_price,
                fire_price_min,
                fire_price_max,
                first_touch: bool,
                armed: bool,
                fire: bool,
                *,
                active_entry: bool,
                entry_allowed: bool,
                blink_armed: bool,
            ):
                if not entry_allowed:
                    status = "BLOCK"
                elif not active_entry:
                    status = "OFF"
                elif fire:
                    status = "FIRE"
                elif blink_armed or armed:
                    status = "ARM"
                elif first_touch:
                    status = "2차 터치 대기"
                else:
                    status = "감시"
                _fire_prices = sorted(
                    (float(fire_price_min), float(fire_price_max))
                ) if fire_price_min is not None and fire_price_max is not None else None
                _fire_price_text = (
                    f"{_fmt_entry_level(_fire_prices[0])}~{_fmt_entry_level(_fire_prices[1])}"
                    if _fire_prices is not None else "-"
                )
                current = (
                    f"Δ {_fmt_z_metric(delta_value)} | "
                    f"ARM {arm_text} @{_fmt_entry_level(arm_price)} | "
                    f"FIRE {fire_text} @{_fire_price_text} | {status}"
                )
                # The live order ARM latch is authoritative.  The dashboard
                # direction latch supplements it after reconnects/intrabar
                # transitions, but must never suppress blinking for a real ARM.
                reached = bool(armed or blink_armed or fire)
                return {
                    "current": current,
                    "target": "",
                    "ok": (False if active_entry and reached and not entry_allowed else (reached if active_entry else None)),
                    # The configured live source can blink; the reference row
                    # keeps its ARM/FIRE state and prices visible without blinking.
                    # Signal monitoring remains visible even while AUTO is
                    # OFF; AUTO controls order submission, not this indicator.
                    "blink": bool(active_entry and entry_allowed and reached),
                }

            entry_detail_rows = {
                "z1_long": _delta_detail_payload(
                    _dz1_display,
                    f"+{_z1_arm:.2f}",
                    f"+{_z1_fire_min:.2f}~+{_z1_fire_max:.2f}",
                    sig_state.get("z1_delta_long_arm_price"),
                    sig_state.get("z1_delta_long_fire_price_min"),
                    sig_state.get("z1_delta_long_fire_price_max"),
                    bool(sig_state.get("z1_delta_long_first_touch", False)),
                    bool(sig_state.get("z1_delta_long_armed", False)),
                    bool(sig_state.get(
                        "z1_delta_long_fire_display",
                        sig_state.get("z1_delta_long_fire", False),
                    )),
                    active_entry=True,
                    entry_allowed=_prev_long_allowed,
                    blink_armed=bool(sig_state.get("z1_delta_long_blink_armed", False)),
                ),
                "z1_short": _delta_detail_payload(
                    _dz1_display,
                    f"-{_z1_arm:.2f}",
                    f"-{_z1_fire_max:.2f}~-{_z1_fire_min:.2f}",
                    sig_state.get("z1_delta_short_arm_price"),
                    sig_state.get("z1_delta_short_fire_price_min"),
                    sig_state.get("z1_delta_short_fire_price_max"),
                    bool(sig_state.get("z1_delta_short_first_touch", False)),
                    bool(sig_state.get("z1_delta_short_armed", False)),
                    bool(sig_state.get(
                        "z1_delta_short_fire_display",
                        sig_state.get("z1_delta_short_fire", False),
                    )),
                    active_entry=True,
                    entry_allowed=_prev_short_allowed,
                    blink_armed=bool(sig_state.get("z1_delta_short_blink_armed", False)),
                ),
                "z5_long": _delta_detail_payload(
                    _dz5_display,
                    f"+{_z5_arm:.2f}",
                    f"+{_z5_fire_min:.2f}~+{_z5_fire_max:.2f}",
                    sig_state.get("z5_delta_long_arm_price"),
                    sig_state.get("z5_delta_long_fire_price_min"),
                    sig_state.get("z5_delta_long_fire_price_max"),
                    bool(sig_state.get("z5_monitor_long_first_touch", False)),
                    bool(sig_state.get("z5_monitor_long_armed", False)),
                    bool(sig_state.get("z5_monitor_long_fire", False)),
                    active_entry=True,
                    entry_allowed=_prev_long_allowed,
                    blink_armed=bool(sig_state.get("z5_delta_long_blink_armed", False)),
                ),
                "z5_short": _delta_detail_payload(
                    _dz5_display,
                    f"-{_z5_arm:.2f}",
                    f"-{_z5_fire_max:.2f}~-{_z5_fire_min:.2f}",
                    sig_state.get("z5_delta_short_arm_price"),
                    sig_state.get("z5_delta_short_fire_price_min"),
                    sig_state.get("z5_delta_short_fire_price_max"),
                    bool(sig_state.get("z5_monitor_short_first_touch", False)),
                    bool(sig_state.get("z5_monitor_short_armed", False)),
                    bool(sig_state.get("z5_monitor_short_fire", False)),
                    active_entry=True,
                    entry_allowed=_prev_short_allowed,
                    blink_armed=bool(sig_state.get("z5_delta_short_blink_armed", False)),
                ),
            }

            def _two_touch_header(source: str) -> str:
                source_n = str(source or "z1").lower()
                if source_n == "z5":
                    long_fire = bool(sig_state.get("z5_monitor_long_fire", False))
                    short_fire = bool(sig_state.get("z5_monitor_short_fire", False))
                    long_armed = bool(sig_state.get("z5_monitor_long_armed", False))
                    short_armed = bool(sig_state.get("z5_monitor_short_armed", False))
                    long_first = bool(sig_state.get("z5_monitor_long_first_touch", False))
                    short_first = bool(sig_state.get("z5_monitor_short_first_touch", False))
                else:
                    long_fire = bool(sig_state.get("z1_delta_long_fire", False))
                    short_fire = bool(sig_state.get("z1_delta_short_fire", False))
                    long_armed = bool(sig_state.get("z1_delta_long_armed", False))
                    short_armed = bool(sig_state.get("z1_delta_short_armed", False))
                    long_first = bool(sig_state.get("z1_delta_long_first_touch", False))
                    short_first = bool(sig_state.get("z1_delta_short_first_touch", False))
                if long_fire:
                    return "매수 FIRE" if _prev_long_allowed else "매수 BLOCK"
                if short_fire:
                    return "매도 FIRE" if _prev_short_allowed else "매도 BLOCK"
                if long_armed:
                    return "매수 ARM" if _prev_long_allowed else "매수 BLOCK"
                if short_armed:
                    return "매도 ARM" if _prev_short_allowed else "매도 BLOCK"
                if long_first:
                    return "매수 2차 터치 대기"
                if short_first:
                    return "매도 2차 터치 대기"
                return "1차 터치 대기"

            _z1_touch_header = _two_touch_header("z1")
            _z5_touch_header = _two_touch_header("z5")

            # Parallel SMA layout: 10-second reversal and one-minute trend.
            def _sma_text(value):
                try:
                    if value is None:
                        return "-"
                    truncated = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    return f"{truncated:.2f}"
                except (InvalidOperation, TypeError, ValueError):
                    return "-"

            _sma5 = sig_state.get("sma10s_5")
            _sma10 = sig_state.get("sma10s_10")
            _sma20 = sig_state.get("sma10s_20")
            _sma_ready = bool(_sma5 is not None and _sma10 is not None and _sma20 is not None)
            _sma_relation = str(sig_state.get("sma10s_relation") or "5 ? 10 ? 20")
            _sma_regime_label = str(sig_state.get("sma10s_regime_label") or "계산 대기")
            _sma_source = str(sig_state.get("sma10s_source") or "NONE")
            _sma_source_label = (
                "10초봉 WARMUP(근사)" if _sma_source == "WARMUP_10S_APPROX"
                else "실시간 10초봉+WARMUP" if _sma_source == "LIVE_10S_WITH_WARMUP"
                else "실시간 10초봉"
            )
            _sma1m5 = sig_state.get("sma1m_5")
            _sma1m10 = sig_state.get("sma1m_10")
            _sma1m20 = sig_state.get("sma1m_20")
            _sma1m_ready = bool(_sma1m5 is not None and _sma1m10 is not None and _sma1m20 is not None)
            _sma1m_relation = str(sig_state.get("sma1m_relation") or "5 ? 20 / 10 ? 20")
            _sma1m_regime = str(sig_state.get("sma1m_regime") or "WARMUP")
            _sma_buy_armed = bool(sig_state.get("sma10s_buy_armed", False))
            _sma_sell_armed = bool(sig_state.get("sma10s_sell_armed", False))
            _sma_buy_fire = bool(sig_state.get("sma10s_buy_fire", False))
            _sma_sell_fire = bool(sig_state.get("sma10s_sell_fire", False))
            _rev_buy_armed = bool(sig_state.get("sma10s_reversal_buy_armed", False))
            _rev_sell_armed = bool(sig_state.get("sma10s_reversal_sell_armed", False))
            _rev_buy_confirmed = bool(sig_state.get("sma10s_reversal_buy_confirmed", False))
            _rev_sell_confirmed = bool(sig_state.get("sma10s_reversal_sell_confirmed", False))
            _rev_buy_fire = bool(sig_state.get("sma10s_reversal_buy_fire", False))
            _rev_sell_fire = bool(sig_state.get("sma10s_reversal_sell_fire", False))
            _rev_buy_high = sig_state.get("sma10s_reversal_buy_break_high")
            _rev_sell_low = sig_state.get("sma10s_reversal_sell_break_low")
            _trend_buy_qualified = bool(sig_state.get("sma1m_trend_buy_qualified", False))
            _trend_sell_qualified = bool(sig_state.get("sma1m_trend_sell_qualified", False))
            _trend_buy_armed = bool(sig_state.get("sma1m_trend_buy_armed", False))
            _trend_sell_armed = bool(sig_state.get("sma1m_trend_sell_armed", False))
            _trend_buy_fire = bool(sig_state.get("sma1m_trend_buy_fire", False))
            _trend_sell_fire = bool(sig_state.get("sma1m_trend_sell_fire", False))
            _trend_buy_min = sig_state.get("sma1m_trend_buy_fire_min")
            _trend_buy_max = sig_state.get("sma1m_trend_buy_fire_max")
            _trend_sell_min = sig_state.get("sma1m_trend_sell_fire_min")
            _trend_sell_max = sig_state.get("sma1m_trend_sell_fire_max")
            try:
                _sma_trend_fire_min = abs(float(getattr(self.cfg, "SMA_TREND_1M_FIRE_MIN_PT", 0.30) or 0.30))
                _sma_trend_fire_max = abs(float(getattr(self.cfg, "SMA_TREND_1M_FIRE_MAX_PT", 0.50) or 0.50))
            except Exception:
                _sma_trend_fire_min, _sma_trend_fire_max = 0.30, 0.50
            _sma_fire_lane = str(sig_state.get("sma10s_fire_lane") or "")
            _sma_buy_phase = str(sig_state.get("sma10s_buy_phase") or "WAIT_LONG_ARM")
            _sma_sell_phase = str(sig_state.get("sma10s_sell_phase") or "WAIT_SHORT_ARM")
            _sma_phase_labels = {
                "WAIT_LONG_ARM": "대기",
                "WAIT_SHORT_ARM": "대기",
                "REVERSAL_WAIT_5_10_UP_CROSS": "반전 LONG ARM · 5선/10선 상향크로스 대기",
                "REVERSAL_WAIT_5_10_DOWN_CROSS": "반전 SHORT ARM · 5선/10선 하향크로스 대기",
                "TREND_WAIT_PLUS_03_05": "추세 LONG ARM · 20선대비 +0.3~+0.5 대기",
                "TREND_WAIT_MINUS_03_05": "추세 SHORT ARM · 20선대비 -0.5~-0.3 대기",
                "REVERSAL_FIRE": "반전 FIRE",
                "TREND_FIRE": "추세 FIRE",
                "FIRE": "FIRE",
            }
            _sma_buy_phase_display = _sma_phase_labels.get(_sma_buy_phase, _sma_buy_phase)
            _sma_sell_phase_display = _sma_phase_labels.get(_sma_sell_phase, _sma_sell_phase)
            if _trend_buy_armed or _trend_buy_fire:
                _sma_buy_phase_display = _sma_phase_labels.get(str(sig_state.get("sma1m_buy_phase") or ""), "추세 LONG")
            if _trend_sell_armed or _trend_sell_fire:
                _sma_sell_phase_display = _sma_phase_labels.get(str(sig_state.get("sma1m_sell_phase") or ""), "추세 SHORT")
            def _sma_compare(left, right):
                try:
                    left_f = float(left)
                    right_f = float(right)
                except Exception:
                    return "?"
                if left_f > right_f + 1e-12:
                    return ">"
                if left_f < right_f - 1e-12:
                    return "<"
                return "="

            _sma_rel_5_20 = _sma_compare(_sma5, _sma20)
            _sma_rel_10_20 = _sma_compare(_sma10, _sma20)
            _sma_price_text = _sma_text(self.current_price or sig_state.get("current_price"))
            entry_regime_rows = {
                "sma_values": {
                    "current": f"반전 10초 5 {_sma_text(_sma5)} / 10 {_sma_text(_sma10)} / 20 {_sma_text(_sma20)} | 추세 1분 5 {_sma_text(_sma1m5)} / 10 {_sma_text(_sma1m10)} / 20 {_sma_text(_sma1m20)}",
                    "target": f"반전 10초 {_sma_regime_label} · 추세 1분 {_sma1m_regime}",
                    "ok": (None if not (_sma_ready and _sma1m_ready) else (sig_state.get("sma10s_regime") in {"BULL", "BEAR"} and _sma1m_regime in {"BULL", "BEAR", "MIXED"})),
                },
            }
            entry_detail_rows = {
                "reversal_long": {
                    "arm": (
                        "10>20 유지 · 현재가 10선 하향터치 LONG ARM"
                        if _rev_buy_confirmed
                        else f"5 {_sma_compare(_sma5, _sma10)} 10 {_sma_rel_10_20} 20 · 현재가 10선 하향터치 대기"
                    ),
                    "fire": (
                        "ARM 후 5선이 10선 상향크로스한 틱에 LONG FIRE"
                        if _rev_buy_fire
                        else f"ARM 후 현재 5선 {_sma_text(_sma5)} / 10선 {_sma_text(_sma10)} · 상향크로스 대기"
                    ),
                    "arm_active": bool(_rev_buy_armed) if _sma_ready else None,
                    "fire_active": bool(_rev_buy_fire) if _sma_ready else None,
                },
                "reversal_short": {
                    "arm": (
                        "10<20 유지 · 현재가 10선 상향터치 SHORT ARM"
                        if _rev_sell_confirmed
                        else f"5 {_sma_compare(_sma5, _sma10)} 10 {_sma_rel_10_20} 20 · 현재가 10선 상향터치 대기"
                    ),
                    "fire": (
                        "ARM 후 5선이 10선 하향크로스한 틱에 SHORT FIRE"
                        if _rev_sell_fire
                        else f"ARM 후 현재 5선 {_sma_text(_sma5)} / 10선 {_sma_text(_sma10)} · 하향크로스 대기"
                    ),
                    "arm_active": bool(_rev_sell_armed) if _sma_ready else None,
                    "fire_active": bool(_rev_sell_fire) if _sma_ready else None,
                },
                "trend_long": {
                    "arm": (
                        "1분봉 현재가 20선 터치 LONG ARM"
                        if _trend_buy_armed
                        else f"1분 SMA10>SMA20 (SMA5 무관) · 현재 {_sma_price_text} / 20선 {_sma_text(_sma1m20)} 터치 대기"
                    ),
                    "fire": (
                        f"1분 20선대비 +{_sma_trend_fire_min:.2f}~+{_sma_trend_fire_max:.2f} LONG FIRE"
                        if _trend_buy_fire
                        else f"ARM 후 현재 {_sma_price_text} · FIRE {_sma_text(_trend_buy_min)}~{_sma_text(_trend_buy_max)}"
                    ),
                    "arm_active": bool(_trend_buy_armed) if _sma1m_ready else None,
                    "fire_active": bool(_trend_buy_fire) if _sma1m_ready else None,
                },
                "trend_short": {
                    "arm": (
                        "1분봉 현재가 20선 터치 SHORT ARM"
                        if _trend_sell_armed
                        else f"1분 SMA10<SMA20 (SMA5 무관) · 현재 {_sma_price_text} / 20선 {_sma_text(_sma1m20)} 터치 대기"
                    ),
                    "fire": (
                        f"1분 20선대비 -{_sma_trend_fire_max:.2f}~-{_sma_trend_fire_min:.2f} SHORT FIRE"
                        if _trend_sell_fire
                        else f"ARM 후 현재 {_sma_price_text} · FIRE {_sma_text(_trend_sell_min)}~{_sma_text(_trend_sell_max)}"
                    ),
                    "arm_active": bool(_trend_sell_armed) if _sma1m_ready else None,
                    "fire_active": bool(_trend_sell_fire) if _sma1m_ready else None,
                },
            }
            _sma_regime_header = f"현재가 {_sma_price_text} | 반전 10초 {_sma_relation} | 추세 1분 {_sma1m_relation}"
            _sma_detail_header = f"LONG {_sma_buy_phase_display} / SHORT {_sma_sell_phase_display}"

            _sma_final_result = {}
            _pending_entry_side_display = str(
                self.pending_entry_side or self._last_entry_submit_side or ""
            ).strip().upper()
            if bool(self.entry_inflight):
                _sma_final_result = {
                    "kind": "ORDER",
                    "text": f"ENTRY ORDER · {_pending_entry_side_display or '확인대기'} · 주문 확인대기",
                }
            elif _sma_buy_fire or _sma_sell_fire:
                _fire_side_display = "LONG" if _sma_buy_fire else "SHORT"
                if not bool(allow_lane):
                    _sma_final_result = {
                        "kind": "BLOCK",
                        "text": f"ENTRY BLOCK · {_fire_side_display} FIRE · {block_reason or '주문차단'}",
                    }
                else:
                    _sma_final_result = {
                        "kind": "FIRE",
                        "text": f"FINAL FIRE · {_fire_side_display} · {_sma_fire_lane or 'ENTRY'}",
                    }

            _display_account = _normalize_account_no(self.account)
            _display_server = self.server_type or "LIVE"
            vm = {
                "header": {
                    "summary": (
                        f"{datetime.now():%Y-%m-%d (%a)} | 계좌 {mask_account(self.account)} | "
                        f"서버 {self.server_type} | {self.live_code} | takeover={'Y' if self.takeover_position_confirmed else 'N'} | "
                        f"server_unfilled={unfilled_state} | time={time_state}({trade_time_target}) | block={block_reason}"
                    ),
                    "server": self.server_type,
                    "code": self.live_code,
                },
                "market": {
                    "status_value": (
                        f"휴장 / {holiday_reason or 'KRX_CLOSED'} | {self.last_market_status}"
                        if holiday_active else self.last_market_status
                    ),
                    "current_price": self.current_price or None,
                    "open_price": self.session_open_price or None,
                    "session_high": (float(self.session_high_price) if self.session_high_price else None),
                    "session_low": (float(self.session_low_price) if self.session_low_price else None),
                    "session_high_time": self.session_high_time,
                    "session_low_time": self.session_low_time,
                    "prev5_high": (float(prev5_hi) if prev5_hi is not None else None),
                    "prev5_low": (float(prev5_lo) if prev5_lo is not None else None),
                    "prev5_high_time": str(prev5_hi_time or "-"),
                    "prev5_low_time": str(prev5_lo_time or "-"),
                    # Dashboard market-state top panel should show the exact H/L
                    # basis currently used by the active entry regime.
                    "detail_high": (float(prev5_hi) if prev5_hi is not None else None),
                    "detail_low": (float(prev5_lo) if prev5_lo is not None else None),
                    "detail_high_time": str(prev5_hi_time or "-"),
                    "detail_low_time": str(prev5_lo_time or "-"),
                    "detail_high_label": _detail_high_label,
                    "detail_low_label": _detail_low_label,
                    "detail_high_time_label": _detail_high_time_label,
                    "detail_low_time_label": _detail_low_time_label,
                    "last_bar_close": self.last_bar_close or None,
                    "session_close": self.session_close or None,
                    "last_tick_time": self.last_tick_time,
                    "last_bar_time": self.last_bar_time,
                },
                "account": {
                    "account_no": self.account,
                    "account_no_masked": mask_account(self.account),
                    "deposit": self.server_deposit,
                    "deposit_valid": bool(self.server_deposit_valid and self.server_deposit is not None),
                    "orderable_qty": (
                        0
                        if (
                            in_pos != 0
                            or bool(self.entry_inflight)
                            or bool(self.exit_inflight)
                            or bool(self.position_close_pending)
                            or bool(self.has_unfilled_orders)
                            or bool(self.has_server_unfilled)
                            or bool(self.cancel_in_progress)
                            or bool(self.exit_in_progress)
                        )
                        else int(getattr(self.cfg, "LIVE_QTY", 1) or 1)
                    ),
                    "orderable_qty_valid": True,
                    "orderable_qty_source": "ENGINE_FIXED",
                    "closeable_qty": (
                        int(self.server_closeable_qty)
                        if self.server_closeable_qty_valid and self.server_closeable_qty is not None
                        else None
                    ),
                    "closeable_qty_valid": bool(self.server_closeable_qty_valid),
                    "orderable_amount": self.server_orderable_amount,
                    "orderable_amount_valid": bool(self.server_orderable_amount_valid and self.server_orderable_amount is not None),
                },
                "strategy": {
                    "long": {
                        "armed": False,
                        "ready": False,
                        "state": _sma_regime_header,
                        "rows": entry_regime_rows,
                    },
                    "short": {
                        "armed": bool(_sma_buy_armed or _sma_sell_armed),
                        "ready": bool(_sma_buy_fire or _sma_sell_fire),
                        "state": _sma_detail_header,
                        "rows": entry_detail_rows,
                        "final_result": _sma_final_result,
                    },
                },
                "exit": {
                    "position_text": pos_text,
                    "z_score": (None if _use_price_extrema_entry_disp else (exit_sig_val if self.auto_on else None)),
                    "current_z": (None if _use_price_extrema_entry_disp else (exit_sig_val if self.auto_on else None)),
                    "exit_z": None,
                    "regime": regime_display,
                    "target_exit_index": None,
                    "expected_exit_index": None,
                    "calc_exit_index": None,
                    "dmi": {"text": dmi_text, "active": self.last_exit_ok if self.auto_on else None},
                    "entry_z5": None,
                    "stop_loss": {"text": stop_loss_text, "active": stop_loss_active},
                    "stop_loss_enabled": bool(stop_loss_enabled),
                    "stop_loss_threshold_pt": float(stop_loss_threshold_pt),
                    "stop_loss_current_pnl_pt": (float(stop_loss_current_pnl_pt) if stop_loss_current_pnl_pt is not None else None),
                    "stop_loss_triggered": bool(stop_loss_triggered),
                    "fixed_loss": {"text": fixed_loss_text, "active": fixed_loss_active},
                    "fixed_loss_enabled": bool(fixed_loss_enabled),
                    "fixed_loss_threshold_pt": (float(fixed_loss_threshold_pt) if fixed_loss_selection_ready and fixed_loss_threshold_pt is not None else None),
                    "fixed_loss_selected_pt": (float(fixed_loss_threshold_pt) if fixed_loss_selection_ready and fixed_loss_threshold_pt is not None else None),
                    "fixed_loss_selection_ready": bool(fixed_loss_selection_ready),
                    "fixed_loss_band_low_pt": float(fixed_loss_band_low_pt),
                    "fixed_loss_band_high_pt": float(fixed_loss_band_high_pt),
                    "fixed_loss_current_pnl_pt": (float(stop_loss_current_pnl_pt) if stop_loss_current_pnl_pt is not None else None),
                    "fixed_loss_triggered": bool(fixed_loss_triggered),
                    "has_position": bool(in_pos != 0),
                    "reverse_exit_enabled": bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False)),
                    "reverse_exit": {
                        "left": (
                            "OFF"
                            if not bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False))
                            else (
                                "발동"
                                if bool(sig_state.get("reverse_signal_allowed", False))
                                else ("감시" if in_pos != 0 else "대기")
                            )
                        ),
                        "right": (
                            "REVERSE EXIT OFF"
                            if not bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False))
                            else (
                                f"{('SELL' if in_pos > 0 else 'BUY') if in_pos != 0 else '-'} "
                                f"{_reverse_exit_delta_label} {_fmt_z_metric(_reverse_exit_delta_value)} | "
                                f"FIRE {_reverse_exit_fire_text} | 최우선 청산"
                            )
                        ),
                        "text": (
                            "OFF"
                            if not bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False))
                            else ("REVERSE_SIGNAL" if bool(sig_state.get("reverse_signal_allowed", False)) else "감시중")
                        ),
                        "active": (
                            True
                            if bool(getattr(self.cfg, "USE_REVERSE_SIGNAL_EXIT", False))
                            and bool(sig_state.get("reverse_signal_allowed", False))
                            else None
                        ),
                        "target_side": "SHORT" if in_pos > 0 else ("LONG" if in_pos < 0 else ""),
                        "signal_raw": bool(sig_state.get("reverse_signal_raw", False)),
                        "allowed": bool(sig_state.get("reverse_signal_allowed", False)),
                    },
                    "mfe_protect_enabled": bool(_use_mfe_protect_exit),
                    "mfe_protect": {
                        "left": protect_left_text,
                        "right": protect_right_text,
                        "text": protect_state_text,
                        "active": protect_active,
                        "criteria": protect_criteria_text,
                        "display_stage": _protect_stage,
                        "stage": _protect_stage,
                        "threshold_mfe_pt": float(_protect_threshold_mfe_pt or 0.0),
                        "threshold_mae_pt": float(_protect_threshold_mae_pt or 0.0),
                        "mae_pt": (float(_protect_mae_pt) if _protect_mae_pt is not None else None),
                        "floor_pnl_pt": float(_protect_floor_pnl_pt or 0.0),
                        "floor_pnl_trigger_pt": float(_protect_floor_pnl_trigger_pt or _protect_floor_pnl_pt or 0.0),
                        "floor_pnl_band_low_pt": (
                            float(protect_info.get("floor_pnl_band_low_pt"))
                            if isinstance(protect_info, dict) and protect_info.get("floor_pnl_band_low_pt") is not None
                            else float(_protect_floor_pnl_pt or 0.0)
                        ),
                        "floor_pnl_band_high_pt": (
                            float(protect_info.get("floor_pnl_band_high_pt"))
                            if isinstance(protect_info, dict) and protect_info.get("floor_pnl_band_high_pt") is not None
                            else float(_protect_floor_pnl_pt or 0.0)
                        ),
                        "max_mfe_pt": float(protect_max_mfe_pt),
                        "handoff_to_mfe_cut": bool(protect_handoff_to_mfe_cut),
                        "mfe_pt": (float(_protect_mfe_pt) if _protect_mfe_pt is not None else None),
                        "current_pnl_pt": (float(_protect_current_pnl_pt) if _protect_current_pnl_pt is not None else None),
                        "exit_price": _protect_exit_price,
                        "hit": bool(protect_active is True),
                        "armed": bool(protect_active in (True, "monitoring", "monitoring_blink")),
                    },
                    "mfe_protect_max_mfe_pt": float(protect_max_mfe_pt),
                    "mfe_stage_criteria": _mfe_stage_criteria_text,
                    "mfe_stage": {
                        "left": _mfe_stage_display or ("미무장" if in_pos != 0 else "대기"),
                        "right": _mfe_stage_criteria_text,
                        "text": "발동" if _mfe_stage_hit else ("활성" if _mfe_stage_armed else "대기중"),
                        "stage": _mfe_stage_display,
                        "mfe_stage": str(mfe_retrace_info.get("mfe_stage") if isinstance(mfe_retrace_info, dict) else ""),
                        "hit": bool(_mfe_stage_hit),
                        "armed": bool(_mfe_stage_armed),
                        # MFE STAGE is a criteria/status row. Keep color active but do not blink.
                        # Blink handoff remains: MFE PROTECT -> MFE CUT.
                        "active": True if (_mfe_stage_hit or _mfe_stage_armed) else None,
                    },
                    "trail": {
                        "left": bar3_left_text,
                        "right": bar3_right_text,
                        "text": "발동" if _mfe_stage_hit else ("활성" if _mfe_stage_armed else "대기중"),
                        "arm_pt": float(_mfe_arm_min_pt),
                        "stage": _mfe_stage_display,
                        "mfe_stage": str(mfe_retrace_info.get("mfe_stage") if isinstance(mfe_retrace_info, dict) else ""),
                        "mfe_pt": (float(_mfe_stage_mfe_pt) if _mfe_stage_mfe_pt is not None else None),
                        "cut_price": _mfe_stage_cut,
                        "cut_band_low": _mfe_stage_cut_band_low,
                        "cut_band_high": _mfe_stage_cut_band_high,
                        "required_retracement_pt": _mfe_stage_gap,
                        "retrace_pct": _mfe_stage_pct,
                        "extreme_label": _mfe_extreme_label,
                        "extreme_price": _mfe_extreme_price,
                        "peak_price": (float(_mfe_extreme_price) if _mfe_extreme_label == "PEAK" and _mfe_extreme_price is not None else None),
                        "trough_price": (float(_mfe_extreme_price) if _mfe_extreme_label == "TROUGH" and _mfe_extreme_price is not None else None),
                        "hit": bool(_mfe_stage_hit),
                        "armed": bool(_mfe_stage_armed),
                        "active": bar3_active,
                    },
                    "trail_arm_pt": float(_mfe_arm_min_pt),
                    "hard": {"text": "AUTO OFF" if not self.auto_on else "장중 감시중", "active": self.auto_on},
                    "last_reason": self.last_exit_reason,
                    "history_lines": [{"line": x} for x in list(self.exit_reason_history)],
                },
                "control": {
                    "auto_on": self.auto_on,
                    "auto_status": auto_status,
                    "eod_cutoff": eod_cutoff,
                    "watchdog_enabled": bool(self._watchdog_mode),
                    "watchdog_server_mode": self._watchdog_server_mode,
                    "watchdog_auto_restore": bool(self._watchdog_auto_restore),
                    "watchdog_auto_shutdown_time": self._watchdog_auto_shutdown_time,
                    "watchdog_auto_restart_time": self._watchdog_auto_restart_time,
                    "watchdog_status_text": self._watchdog_status_text,
                    "cancel_enabled": bool(self.connected and bool(self.account)),
                    "exit_enabled": bool(self.connected and bool(self.account)),
                    "cancel_status": self.cancel_status,
                    "exit_status": self.exit_button_status,
                    "position_text": pos_text,
                    "allow_trade": bool(allow_lane),
                    "block_reason": block_reason,
                    "auto_cleanup_phase": self.auto_cleanup_phase,
                    "server_sync_pending": bool(self.server_sync_pending),
                    "has_server_unfilled": bool(self.has_server_unfilled),
                    "server_unfilled_qty": int(self.server_unfilled_qty or 0),
                    "has_server_position": bool(self._has_server_position()),
                    "server_position_qty": int(self.position_qty or 0) if self._has_server_position() else 0,
                    "cancel_in_progress": bool(self.cancel_in_progress),
                    "exit_in_progress": bool(self.exit_in_progress),
                    "cancel_confirmed": bool(self.cancel_confirmed),
                    "exit_confirmed": bool(self.exit_confirmed),
                },
                "diag": {
                    "footer_text": (
                        f"hist={self.history_code} | bar_1m={self.bar_count_1m} | "
                        f"z5={z5_text} z30={z30_text} | TIME={time_state}({trade_time_target}) | "
                        f"1m={counts.get('bars_1m', 0)} 5m={counts.get('bars_5m', 0)} 30m={counts.get('bars_30m', 0)} | "
                        f"warmup={'Y' if warmup_ready else ('LOAD' if self.warmup_loading else 'N')} | "
                        f"pending={getattr(self.order_core.pending, 'action', '-') if self.order_core else '-'} | "
                        f"sync={sync_state} | server_unfilled={unfilled_state} | block={block_reason} | phase={pending_phase} | "
                        f"takeover={'Y' if self.takeover_position_confirmed else 'N'}"
                    )
                },
                "event_log_rows": list(self.logs),
            }
            self.dashboard.update_view_model(vm)
        except Exception as e:
            self.log("ERROR", "VIEW", f"refresh_view 실패: {e}")
            self.log("ERROR", "TRACE", traceback.format_exc())

    # ---------- helpers ----------

    def _persist_entry_anchor_state(self, reason: str = "") -> None:
        try:
            payload = {
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "reason": str(reason or ""),
                "side": str(self.position_side or "FLAT"),
                "qty": int(self.position_qty or 0),
                "entry_price": (float(self.entry_price) if float(self.entry_price or 0.0) > 0.0 else None),
                "entry_z5": None,
                "entry_regime": str(self.position_entry_regime or ""),
                "mfe_profile": str(self.position_mfe_profile or ""),
                "entry_audit": dict(self._position_entry_audit or {}),
                "entry_order_audit": dict(self._position_entry_order_audit or {}),
                "entry_z5_band_snapshot": dict(self.position_entry_z5_band_snapshot or {}),
                "entry_time": (
                    self.position_entry_time.strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(self.position_entry_time, datetime)
                    else None
                ),
            }
            with open(self._entry_anchor_state_path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log("ERROR", "ANCHOR_SAVE", f"{e}")

    def _load_entry_anchor_from_state(self, side: str, qty: int, entry_price: float) -> float | None:
        # Deprecated: EXIT_Z no longer uses entry_z5/current_z5 anchors.
        return None

    def _load_entry_time_from_state(self, side: str, qty: int, entry_price: float) -> datetime | None:
        try:
            if not os.path.exists(self._entry_anchor_state_path):
                return None
            with open(self._entry_anchor_state_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
            p_side = str(payload.get("side") or "").upper()
            p_qty = int(payload.get("qty") or 0)
            if p_side != str(side or "").upper():
                return None
            if p_qty != int(qty or 0):
                return None
            p_price_raw = payload.get("entry_price")
            if p_price_raw not in (None, "") and float(entry_price or 0.0) > 0.0:
                p_price = float(p_price_raw)
                if abs(p_price - float(entry_price)) > 0.30:
                    return None
            for key in ("entry_time", "updated_at"):
                raw = str(payload.get(key) or "").strip()
                if not raw:
                    continue
                try:
                    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
            return None
        except Exception:
            return None

    def _load_entry_context_from_state(self, side: str, qty: int, entry_price: float) -> dict[str, Any]:
        try:
            if not os.path.exists(self._entry_anchor_state_path):
                return {}
            with open(self._entry_anchor_state_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
            if str(payload.get("side") or "").upper() != str(side or "").upper():
                return {}
            if int(payload.get("qty") or 0) != int(qty or 0):
                return {}
            saved_price = payload.get("entry_price")
            if saved_price not in (None, "") and float(entry_price or 0.0) > 0.0:
                if abs(float(saved_price) - float(entry_price)) > 0.30:
                    return {}
            regime = str(payload.get("entry_regime") or "").strip().upper()
            if regime not in ("TREND", "REVERSAL"):
                regime = ""
            return {
                "entry_regime": regime,
                "mfe_profile": str(payload.get("mfe_profile") or regime),
                "entry_audit": dict(payload.get("entry_audit") or {}),
                "entry_order_audit": dict(payload.get("entry_order_audit") or {}),
            }
        except Exception:
            return {}

    def _load_entry_anchor_from_trade_logs(self, side: str, entry_price: float) -> float | None:
        # Deprecated: trade logs may contain legacy Z-score columns, but ENTRY/EXIT must not restore them.
        return None

    def _restore_entry_anchor(self, side: str, qty: int, entry_price: float) -> float | None:
        # Deprecated: Z5 anchors are intentionally disabled.
        return None

    @staticmethod
    def _fmt_hms(raw_hms: str) -> str:
        s = str(raw_hms or "").strip()
        if len(s) == 6 and s.isdigit():
            return f"{s[:2]}:{s[2:4]}:{s[4:6]}"
        return datetime.now().strftime("%H:%M:%S")

    @staticmethod
    def _tick_dt(raw_hms: str) -> datetime:
        now = datetime.now()
        s = str(raw_hms or "").strip()
        if len(s) == 6 and s.isdigit():
            try:
                return now.replace(hour=int(s[:2]), minute=int(s[2:4]), second=int(s[4:6]), microsecond=0)
            except Exception:
                return now
        return now


def _signal_reason(sig_num: int) -> str:
    try:
        return signal.Signals(sig_num).name
    except Exception:
        return str(sig_num)


def _handle_process_signal(sig_num, _frame) -> None:
    controller = _GLOBAL_CONTROLLER
    reason = f"WATCHDOG_{_signal_reason(int(sig_num))}"
    _write_process_diagnostic("PROCESS_SIGNAL", f"signal={_signal_reason(int(sig_num))} reason={reason}")
    if controller is not None:
        try:
            controller.request_safe_shutdown(reason)
            return
        except Exception:
            try:
                controller._save_resume_state(reason, force=True)
            except Exception:
                pass


def _atexit_safe_save() -> None:
    controller = _GLOBAL_CONTROLLER
    _write_process_diagnostic("ATEXIT", f"reason={(getattr(controller, '_shutdown_reason', '') or 'ATEXIT') if controller is not None else 'ATEXIT_NO_CONTROLLER'}")
    if controller is not None:
        try:
            controller._save_resume_state(controller._shutdown_reason or "ATEXIT")
        except Exception:
            pass


def _global_excepthook(exc_type, exc_value, exc_tb) -> None:
    try:
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb)).strip()
    except Exception:
        tb_text = f"{getattr(exc_type, '__name__', 'Exception')}: {exc_value}"
    _write_process_diagnostic("UNCAUGHT_EXCEPTION", tb_text)
    controller = _GLOBAL_CONTROLLER
    if controller is not None:
        try:
            controller.log("FATAL", "EXCEPTHOOK", tb_text)
        except Exception:
            pass
        try:
            controller._save_resume_state("UNCAUGHT_EXCEPTION", force=True)
        except Exception:
            pass
    try:
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    except Exception:
        pass


def _threading_excepthook(args) -> None:
    try:
        tb_text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)).strip()
    except Exception:
        tb_text = f"{getattr(args, 'thread', None)} thread exception"
    thread_name = getattr(getattr(args, "thread", None), "name", "unknown")
    _write_process_diagnostic("THREAD_EXCEPTION", f"thread={thread_name} {tb_text}")
    controller = _GLOBAL_CONTROLLER
    if controller is not None:
        try:
            controller.log("FATAL", "THREAD", f"thread={thread_name} error={tb_text}")
        except Exception:
            pass


def _install_process_handlers() -> None:
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig_obj = getattr(signal, sig_name, None)
        if sig_obj is None:
            continue
        try:
            signal.signal(sig_obj, _handle_process_signal)
        except Exception:
            pass


atexit.register(_atexit_safe_save)
_install_process_handlers()
sys.excepthook = _global_excepthook
if hasattr(threading, "excepthook"):
    threading.excepthook = _threading_excepthook


def main() -> int:
    global _GLOBAL_CONTROLLER, _INSTANCE_LOCK
    controller = None
    if not _acquire_single_instance_lock():
        try:
            print("[KIS] another ZENITH process is already running; duplicate start blocked.", flush=True)
        except Exception:
            pass
        return 0
    try:
        controller = ZenithZScoreController(RUNTIME_RESUME)
        _GLOBAL_CONTROLLER = controller
        return int(controller.app.exec_() or 0)
    except KeyboardInterrupt:
        if controller is not None:
            try:
                controller.request_safe_shutdown("WATCHDOG_CTRL_BREAK")
            except Exception:
                try:
                    controller._save_resume_state("WATCHDOG_CTRL_BREAK", force=True)
                except Exception:
                    pass
        return 0
    except Exception as e:
        fatal_msg = f"{type(e).__name__}: {e}"
        _write_process_diagnostic("MAIN_EXCEPTION", fatal_msg)
        try:
            print(f"[FATAL] {fatal_msg}", flush=True)
        except Exception:
            pass
        if controller is not None:
            try:
                controller.log("FATAL", "MAIN", fatal_msg)
            except Exception:
                pass
            try:
                controller._save_resume_state("FATAL", force=True)
            except Exception:
                pass
        return 1
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        if _INSTANCE_LOCK is not None:
            try:
                _INSTANCE_LOCK.unlock()
            except Exception:
                pass
            _INSTANCE_LOCK = None


if __name__ == "__main__":
    sys.exit(main())
