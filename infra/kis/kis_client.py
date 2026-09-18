from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .adapter import KisPaperAdapter
from .futures import KisFuturesOrder
from .websocket import PAPER_FUTURES_NOTICE_TR_ID, KisWsSubscription


def _first(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def _number(value: Any) -> float:
    try:
        return abs(float(str(value or "0").replace(",", "").replace("+", "").strip()))
    except (TypeError, ValueError):
        return 0.0


class KisClient(QObject):
    """Qt bridge for the KIS paper REST API.

    The strategy layer sees normalized account, order, execution and quote
    events. No ActiveX control, desktop login window, or locally saved account
    password is used.
    """

    connection_changed = pyqtSignal(int)
    message_received = pyqtSignal(str, str, str, str)
    execution_received = pyqtSignal(dict)
    order_activity_received = pyqtSignal(dict)
    tick_received = pyqtSignal(dict)
    response_received = pyqtSignal(dict)

    # Order fills are primarily delivered by the private websocket and are
    # independently reconciled from balance/open-order snapshots.  The REST
    # monitor is only a short fallback; leaving it alive forever can starve
    # paper quote polling when KIS starts timing out.
    ORDER_MONITOR_MAX_AGE_SEC = 60.0
    ORDER_MONITOR_MISSING_GRACE_SEC = 15.0
    ORDER_MONITOR_MISSING_LIMIT = 3

    def __init__(self, adapter: KisPaperAdapter | None = None) -> None:
        super().__init__()
        self.adapter = adapter or KisPaperAdapter.from_env()
        self.settings = self.adapter.settings
        self._connected = False
        self._rest_rate_lock = threading.Lock()
        self._background_requests: set[str] = set()
        self._last_rest_request_started = 0.0
        self._order_ws_lock = threading.Lock()
        self._order_ws_thread: threading.Thread | None = None
        self._order_ws_loop: asyncio.AbstractEventLoop | None = None
        self._order_ws_task: asyncio.Task | None = None
        self._order_ws_stop = threading.Event()
        self._market_symbol = ""
        self._quote_inflight = False
        self._quote_error_streak = 0
        self._quote_backoff_until = 0.0
        # Invalidates callbacks from a quote request that outlived an
        # unsubscribe/reconnect.  Without this guard a stale request can leave
        # the new subscription permanently stuck in the inflight state.
        self._quote_generation = 0
        self._quote_timer = QTimer(self)
        # Paper futures quotes are REST-polled. Keep the timer just above the
        # shared REST start interval so normal quote traffic does not queue on
        # every cycle and collide with account/order safety requests.
        quote_interval_ms = max(
            1100,
            int((float(self.settings.rest_min_interval_sec) + 0.05) * 1000),
        )
        self._quote_timer.setInterval(quote_interval_ms)
        self._quote_timer.timeout.connect(self._poll_quote)
        self._order_poll_inflight = False
        self._monitored_orders: dict[str, dict[str, Any]] = {}
        self._order_timer = QTimer(self)
        self._order_timer.setInterval(750)
        self._order_timer.timeout.connect(self._poll_orders)

    def clear_order_monitors(self) -> None:
        """Stop fallback REST order polling after server reconciliation."""
        self._monitored_orders.clear()
        self._order_timer.stop()

    def _expire_stale_order_monitors(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        for order_no, state in list(self._monitored_orders.items()):
            started_at = float(state.get("started_at", current) or current)
            if current - started_at >= self.ORDER_MONITOR_MAX_AGE_SEC:
                self._monitored_orders.pop(order_no, None)

    def _background(
        self,
        operation: Callable[[], dict[str, Any]],
        on_success: Callable[[dict[str, Any]], None],
        *,
        request_name: str,
        on_error: Callable[[], None] | None = None,
    ) -> int:
        with self._rest_rate_lock:
            if request_name in self._background_requests:
                return -1
            self._background_requests.add(request_name)
        def run() -> None:
            try:
                result = operation()
                if str(result.get("rt_cd", "0")) not in {"", "0"}:
                    raise RuntimeError(str(result.get("msg1") or "KIS request rejected"))
                on_success(result)
            except Exception as exc:
                if on_error is not None:
                    try:
                        on_error()
                    except Exception:
                        pass
                self.message_received.emit("KIS", request_name, "KIS_REST", str(exc))
            finally:
                with self._rest_rate_lock:
                    self._background_requests.discard(request_name)

        threading.Thread(target=run, name=f"kis-{request_name.lower()}", daemon=True).start()
        return 0

    def _wait_for_rest_slot(self) -> None:
        """Apply one shared KIS paper REST pace across every request path."""

        with self._rest_rate_lock:
            elapsed = time.monotonic() - self._last_rest_request_started
            delay = float(self.settings.rest_min_interval_sec) - elapsed
            if delay > 0:
                time.sleep(delay)
            self._last_rest_request_started = time.monotonic()

    def connect_api(self) -> None:
        def run() -> None:
            try:
                # REST access token is sufficient to establish the paper
                # account session. A WebSocket approval key is requested only
                # when a supported streaming subscription is started.
                self.adapter.auth.get_access_token()
                self._connected = True
                if self.settings.hts_id:
                    self._start_order_stream()
                else:
                    self.message_received.emit(
                        "KIS", "ORDER_WS", PAPER_FUTURES_NOTICE_TR_ID,
                        "disabled: KIS_HTS_ID is not configured; REST order polling remains active",
                    )
                self.connection_changed.emit(0)
            except Exception as exc:
                self._connected = False
                self.message_received.emit("KIS", "AUTH", "OAUTH", str(exc))
                self.connection_changed.emit(-1)

        threading.Thread(target=run, name="kis-oauth", daemon=True).start()

    def disconnect_api(self) -> None:
        # Mark the market feed disconnected before waiting for the order
        # stream.  A late REST quote callback must not be delivered into the
        # strategy after reconnect has started.
        self._connected = False
        self._quote_generation += 1
        self._market_symbol = ""
        self._quote_inflight = False
        self._quote_timer.stop()
        self._stop_order_stream()
        self._order_timer.stop()

    def get_connection_state(self) -> int:
        return int(self._connected)

    def get_accounts(self) -> list[str]:
        return [self.settings.account_no] if self.settings.account_no else []

    def get_server_mode(self) -> str:
        return "PAPER"

    def _start_order_stream(self) -> None:
        self._stop_order_stream()
        stop_event = threading.Event()
        self._order_ws_stop = stop_event

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(self._order_stream_loop(stop_event))
            with self._order_ws_lock:
                self._order_ws_loop = loop
                self._order_ws_task = task
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
            finally:
                with self._order_ws_lock:
                    if self._order_ws_task is task:
                        self._order_ws_task = None
                        self._order_ws_loop = None
                        self._order_ws_thread = None
                loop.close()

        thread = threading.Thread(target=run, name="kis-paper-order-ws", daemon=True)
        with self._order_ws_lock:
            self._order_ws_thread = thread
        thread.start()

    def _stop_order_stream(self) -> None:
        self._order_ws_stop.set()
        with self._order_ws_lock:
            loop = self._order_ws_loop
            task = self._order_ws_task
            thread = self._order_ws_thread
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=1.0)

    async def _order_stream_loop(self, stop_event: threading.Event) -> None:
        retry_count = 0
        subscription = KisWsSubscription(PAPER_FUTURES_NOTICE_TR_ID, self.settings.hts_id)
        while not stop_event.is_set():
            try:
                stream = self.adapter.websocket.stream([subscription])
                async for frame in stream:
                    if stop_event.is_set():
                        return
                    if frame.get("kind") == "system":
                        data = frame.get("data") or {}
                        body = data.get("body") if isinstance(data, dict) else {}
                        if isinstance(body, dict) and str(body.get("rt_cd", "0")) not in {"", "0"}:
                            raise RuntimeError(str(body.get("msg1") or "KIS paper WebSocket subscription rejected"))
                        if str(frame.get("tr_id") or "") == PAPER_FUTURES_NOTICE_TR_ID:
                            self.message_received.emit(
                                "KIS", "ORDER_WS", PAPER_FUTURES_NOTICE_TR_ID,
                                "connected to paper futures order notices",
                            )
                        continue
                    if str(frame.get("tr_id") or "") == PAPER_FUTURES_NOTICE_TR_ID:
                        self.order_activity_received.emit({
                            "source": "KIS_WS_H0IFCNI9",
                            "tr_id": PAPER_FUTURES_NOTICE_TR_ID,
                            "received_at": datetime.now().strftime("%H%M%S"),
                        })
                        retry_count = 0
                if not stop_event.is_set():
                    raise RuntimeError("KIS paper order WebSocket stream closed")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if stop_event.is_set():
                    return
                retry_count += 1
                delay = min(30.0, float(2 ** min(retry_count - 1, 5)))
                self.message_received.emit(
                    "KIS", "ORDER_WS", PAPER_FUTURES_NOTICE_TR_ID,
                    f"{exc}; reconnect in {delay:.0f}s",
                )
                await asyncio.sleep(delay)

    def subscribe_market(self, symbol: str) -> int:
        symbol_text = str(symbol or "").strip()
        if not self._connected or not symbol_text:
            return -1
        # Start a fresh quote generation.  A request from the previous
        # connection may still be winding down in a worker thread.
        self._quote_generation += 1
        self._market_symbol = symbol_text
        self._quote_inflight = False
        self._quote_error_streak = 0
        self._quote_backoff_until = 0.0
        self._quote_timer.start()
        self._poll_quote()
        return 0

    def unsubscribe_market(self) -> None:
        self._quote_timer.stop()
        self._quote_generation += 1
        self._market_symbol = ""
        self._quote_inflight = False
        self._quote_error_streak = 0
        self._quote_backoff_until = 0.0

    def _record_quote_failure(self, generation: int) -> None:
        if generation != self._quote_generation:
            return
        self._quote_inflight = False
        self._quote_error_streak = int(self._quote_error_streak or 0) + 1
        # Quote requests have their own short timeout.  Keep failure backoff
        # equally short so the visible price recovers quickly after a transient
        # paper-server stall, while still avoiding a tight retry loop.
        delay_sec = min(2.0, float(2 ** min(self._quote_error_streak - 1, 1)))
        self._quote_backoff_until = time.monotonic() + delay_sec

    def _poll_quote(self) -> None:
        if (
            self._quote_inflight
            or not self._connected
            or not self._market_symbol
            or time.monotonic() < float(self._quote_backoff_until or 0.0)
        ):
            return
        self._quote_inflight = True
        symbol = self._market_symbol
        generation = self._quote_generation

        def success(data: dict[str, Any]) -> None:
            try:
                if (
                    generation != self._quote_generation
                    or not self._connected
                    or symbol != self._market_symbol
                ):
                    return
                output1 = data.get("output1") or {}
                output2 = data.get("output2") or {}
                if isinstance(output1, list):
                    output1 = output1[0] if output1 else {}
                if isinstance(output2, list):
                    output2 = output2[0] if output2 else {}
                merged = {**(output1 if isinstance(output1, dict) else {}), **(output2 if isinstance(output2, dict) else {})}
                price = _number(_first(merged, "futs_prpr", "stck_prpr", "fuop_prpr"))
                bid = _number(_first(merged, "bidp1", "bidp_1", "futs_bidp1"))
                ask = _number(_first(merged, "askp1", "askp_1", "futs_askp1"))
                volume = _number(_first(merged, "cntg_vol", "acml_vol", "futs_cntg_vol"))
                tick_time = str(_first(merged, "stck_cntg_hour", "futs_cntg_hour", default=datetime.now().strftime("%H%M%S")))
                if price > 0:
                    self._quote_error_streak = 0
                    self._quote_backoff_until = 0.0
                    self.tick_received.emit({
                        "symbol": symbol,
                        "price": price,
                        "bid": bid,
                        "ask": ask,
                        "volume": volume,
                        "tick_time_raw": tick_time,
                        "source": "KIS_REST_QUOTE",
                    })
            finally:
                if generation == self._quote_generation:
                    self._quote_inflight = False

        def error() -> None:
            self._record_quote_failure(generation)

        operation = lambda: self.adapter.futures.inquire_quote(symbol)
        started = self._background(
            operation,
            success,
            request_name="QUOTE",
            on_error=error,
        )
        # A previous generation's QUOTE worker can still own the duplicate
        # request key.  Treat that as a retryable miss instead of leaving the
        # new feed permanently marked inflight; the 1-second timer will retry.
        if int(started) != 0 and generation == self._quote_generation:
            self._quote_inflight = False

    @staticmethod
    def _balance_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        raw_rows = data.get("output1") or []
        if isinstance(raw_rows, dict):
            raw_rows = [raw_rows]
        rows: list[dict[str, Any]] = []
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            side_code = str(_first(raw, "sll_buy_dvsn_cd", "trad_dvsn", default=""))
            side_name = str(_first(raw, "sll_buy_dvsn_name", "trad_dvsn_name", default=""))
            rows.append({
                "종목코드": str(_first(raw, "shtn_pdno", "fuop_item_code", "pdno")),
                "매도수구분": side_name or side_code,
                "매매구분": side_name or side_code,
                "잔고수량": _first(raw, "cblc_qty", "ccld_qty_smtl", "hldg_qty", "ord_qty"),
                "청산가능수량": _first(raw, "ord_psbl_qty", "lqd_psbl_qty"),
                "주문가능수량": _first(raw, "ord_psbl_qty", "lqd_psbl_qty"),
                "평균단가": _first(raw, "ccld_avg_unpr1", "avg_unpr", "pchs_avg_pric", "ccld_avg_unpr"),
                "_kis_raw": raw,
            })
        return rows

    @staticmethod
    def _order_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        raw_rows = data.get("output1") or []
        if isinstance(raw_rows, dict):
            raw_rows = [raw_rows]
        rows: list[dict[str, Any]] = []
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            trade_name = str(_first(raw, "trad_dvsn_name", default="")).strip()
            # KIS includes historical 매수취소/매도취소 rows even when
            # CCLD_NCCS_DVSN=02.  Their qty is the cancelled quantity, not an
            # outstanding quantity.
            if "취소" in trade_name:
                continue
            order_qty = int(_number(_first(raw, "ord_qty")))
            filled_qty = int(_number(_first(raw, "tot_ccld_qty", "ccld_qty")))
            remaining = int(_number(_first(
                raw,
                "nccs_qty",
                "rmn_qty",
                "qty",
                default=max(order_qty - filled_qty, 0),
            )))
            if remaining <= 0:
                continue
            rows.append({
                "종목코드": str(_first(raw, "shtn_pdno", "pdno")),
                "주문번호": str(_first(raw, "odno")).strip(),
                "원주문번호": str(_first(raw, "orgn_odno")).strip(),
                "주문수량": order_qty,
                "체결수량": filled_qty,
                "미체결수량": remaining,
                "매도수구분": str(_first(raw, "sll_buy_dvsn_cd", "sll_buy_dvsn_name")),
                "주문상태": str(_first(raw, "ord_dvsn_name", "trad_dvsn_name", default="미체결")),
                "주문/체결시간": str(_first(raw, "ord_tmd", "ord_tmd_hms")),
                "_kis_raw": raw,
            })
        return rows

    def request_balance(self, *, request_name: str = "KIS_BALANCE") -> int:
        def success(data: dict[str, Any]) -> None:
            self.response_received.emit({
                "request_name": request_name,
                "response_type": "KIS_BALANCE",
                "takeover_rows": self._balance_rows(data),
                "raw": data,
            })
        return self._background(self.adapter.futures.inquire_balance, success, request_name=request_name)

    def request_open_orders(self, *, request_name: str = "KIS_OPEN_ORDERS") -> int:
        def success(data: dict[str, Any]) -> None:
            rows = self._order_rows(data)
            self.response_received.emit({
                "request_name": request_name,
                "response_type": "KIS_OPEN_ORDERS",
                "unfilled_rows": rows,
                "unfilled_debug": {"source": "KIS_REST", "rows": len(rows)},
                "raw": data,
            })
        operation = lambda: self.adapter.futures.inquire_orders(unfilled_only=True)
        return self._background(operation, success, request_name=request_name)

    def request_account_snapshot(self, *, request_name: str = "KIS_ACCOUNT") -> int:
        def success(data: dict[str, Any]) -> None:
            summary = data.get("output2") or {}
            if isinstance(summary, list):
                summary = summary[0] if summary else {}
            self.response_received.emit({
                "request_name": request_name,
                "response_type": "KIS_ACCOUNT",
                "account_summary": summary if isinstance(summary, dict) else {},
                "raw": data,
            })
        return self._background(self.adapter.futures.inquire_balance, success, request_name=request_name)

    def request_orderable(self, *, symbol: str, side: str, price: float, request_name: str = "KIS_ORDERABLE") -> int:
        operation = lambda: self.adapter.futures.inquire_orderable(
            symbol,
            side=side,
            price=Decimal(str(price)),
            order_type="LIMIT",
        )
        def success(data: dict[str, Any]) -> None:
            summary = data.get("output") or {}
            if isinstance(summary, list):
                summary = summary[0] if summary else {}
            self.response_received.emit({
                "request_name": request_name,
                "response_type": "KIS_ORDERABLE",
                "orderable_summary": summary if isinstance(summary, dict) else {},
                "raw": data,
            })
        return self._background(operation, success, request_name=request_name)

    def request_minute_bars(self, symbol: str, *, date: str, time_text: str = "153500") -> dict[str, Any]:
        return self.adapter.futures.inquire_minute_bars(symbol, date=date, time_text=time_text)

    def _poll_orders(self) -> None:
        self._expire_stale_order_monitors()
        if self._order_poll_inflight or not self._connected or not self._monitored_orders:
            if not self._monitored_orders:
                self._order_timer.stop()
            return
        self._order_poll_inflight = True

        def success(data: dict[str, Any]) -> None:
            try:
                rows = data.get("output1") or []
                if isinstance(rows, dict):
                    rows = [rows]
                by_order = {
                    str(_first(row, "odno")): row
                    for row in rows if isinstance(row, dict) and str(_first(row, "odno"))
                }
                completed: list[str] = []
                now = time.monotonic()
                for order_no, state in list(self._monitored_orders.items()):
                    row = by_order.get(order_no)
                    if row is None:
                        state["missing_successes"] = int(state.get("missing_successes", 0) or 0) + 1
                        started_at = float(state.get("started_at", now) or now)
                        if (
                            now - started_at >= self.ORDER_MONITOR_MISSING_GRACE_SEC
                            and state["missing_successes"] >= self.ORDER_MONITOR_MISSING_LIMIT
                        ):
                            completed.append(order_no)
                        continue
                    state["missing_successes"] = 0
                    order_qty = int(_number(_first(row, "ord_qty", default=state.get("quantity", 0))))
                    filled_total = int(_number(_first(row, "tot_ccld_qty", "ccld_qty")))
                    remaining = int(_number(_first(
                        row,
                        "nccs_qty",
                        "rmn_qty",
                        "qty",
                        default=max(order_qty - filled_total, 0),
                    )))
                    reported = int(state.get("reported_filled", 0) or 0)
                    fill_delta = max(filled_total - reported, 0)
                    last_remaining = state.get("remaining")
                    if fill_delta > 0 or last_remaining != remaining:
                        state["reported_filled"] = filled_total
                        state["remaining"] = remaining
                        self.execution_received.emit({
                            "readable": {
                                "주문상태": str(_first(row, "ord_dvsn_name", "ccld_dvsn_name", default="체결" if fill_delta else "접수")),
                                "주문번호": order_no,
                                "주문수량": order_qty,
                                "미체결수량": remaining,
                                "체결수량": fill_delta,
                                "체결가": _first(row, "avg_prvs", "avg_ccld_unpr", "ccld_unpr", "ord_unpr"),
                                "주문/체결시간": str(_first(row, "ord_tmd", "ccld_tmd", default=datetime.now().strftime("%H%M%S"))),
                            },
                            "raw": row,
                        })
                    if remaining <= 0 and filled_total >= order_qty > 0:
                        completed.append(order_no)
                for order_no in completed:
                    self._monitored_orders.pop(order_no, None)
            finally:
                self._order_poll_inflight = False

        def failed() -> None:
            self._order_poll_inflight = False
            self._expire_stale_order_monitors()

        self._background(
            self.adapter.futures.inquire_orders,
            success,
            request_name="ORDER_MONITOR",
            on_error=failed,
        )

    def submit_futures_order(
        self,
        *,
        side: str,
        symbol: str,
        quantity: int,
        order_type: str,
        price: float,
    ) -> int:
        try:
            result = self.adapter.futures.place_order(KisFuturesOrder(
                side=str(side).upper(),
                symbol=str(symbol),
                quantity=int(quantity),
                price=Decimal(str(price)),
                order_type=str(order_type).upper(),
            ))
        except Exception as exc:
            self.message_received.emit("KIS", "ORDER", "VTTO1101U", str(exc))
            return -1
        if str(result.get("rt_cd", "")) != "0":
            self.message_received.emit("KIS", "ORDER", "VTTO1101U", str(result.get("msg1") or "KIS order rejected"))
            return -1
        output = result.get("output") or {}
        order_no = str(_first(output, "ODNO", "odno"))
        if order_no:
            self._monitored_orders[order_no] = {
                "quantity": int(quantity),
                "reported_filled": 0,
                "remaining": int(quantity),
                "started_at": time.monotonic(),
                "missing_successes": 0,
            }
            self._order_timer.start()
        notice = {
            "readable": {
                "주문상태": "접수",
                "주문번호": order_no,
                "주문수량": int(quantity),
                "미체결수량": int(quantity),
                "체결수량": 0,
                "주문가격": float(price),
                "주문/체결시간": datetime.now().strftime("%H%M%S"),
            },
            "raw": result,
        }
        # Defer the Qt notification until KisOrderCore has installed its
        # pending-order state after this method returns.
        QTimer.singleShot(0, lambda payload=notice: self.execution_received.emit(payload))
        return 0

    def cancel_futures_order(self, order_no: str, *, quantity: int = 0) -> int:
        try:
            result = self.adapter.futures.cancel_order(order_no, quantity=quantity)
        except Exception as exc:
            self.message_received.emit("KIS", "CANCEL", "VTTO1103U", str(exc))
            return -1
        if str(result.get("rt_cd", "")) != "0":
            self.message_received.emit("KIS", "CANCEL", "VTTO1103U", str(result.get("msg1") or "KIS cancel rejected"))
            return -1
        self._monitored_orders.pop(str(order_no), None)
        notice = {
            "readable": {
                "주문상태": "취소확인",
                "원주문번호": str(order_no),
                "미체결수량": 0,
                "체결수량": 0,
                "주문/체결시간": datetime.now().strftime("%H%M%S"),
            },
            "raw": result,
        }
        QTimer.singleShot(0, lambda payload=notice: self.execution_received.emit(payload))
        return 0
