from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit


class RestPacer:
    """One bounded, priority-aware start-rate budget per endpoint/app in this process."""

    def __init__(self, interval):
        self.interval = max(0.0, float(interval))
        self.condition = threading.Condition()
        self.next_start = 0.0
        self.blocked_until = 0.0
        self.last_limit = float("-inf")
        self.limit_streak = 0
        self.waiters = []
        self.sequence = 0

    def wait(self, priority, timeout):
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self.condition:
            ticket = (priority, self.sequence)
            self.sequence += 1
            self.waiters.append(ticket)
            try:
                while True:
                    now = time.monotonic()
                    delay = max(self.next_start, self.blocked_until) - now
                    if now >= deadline:
                        raise TimeoutError("KIS local rate queue expired before sending")
                    if ticket == min(self.waiters) and delay <= 0:
                        self.next_start = now + self.interval
                        return
                    self.condition.wait(min(
                        max(delay, 0.01), max(0.001, deadline - now), 0.25
                    ))
            finally:
                self.waiters.remove(ticket)
                self.condition.notify_all()

    def limited(self):
        with self.condition:
            now = time.monotonic()
            self.limit_streak = self.limit_streak + 1 if now - self.last_limit < 60 else 1
            self.last_limit = now
            delay = min(30.0, 2.0 ** min(self.limit_streak, 5))
            self.blocked_until = max(self.blocked_until, now + delay)
            self.condition.notify_all()


_registry_lock = threading.Lock()
_registry = {}


def shared_pacer(endpoint, app_key, interval):
    with _registry_lock:
        key = (endpoint, app_key)
        if key not in _registry:
            _registry[key] = RestPacer(interval)
        pacer = _registry[key]
        with pacer.condition:
            pacer.interval = max(pacer.interval, float(interval))
        return pacer


def is_rate_limit(value, status=None):
    text = str(value)
    return status == 429 or any(token in text for token in (
        "EGW00201", "초당 거래건수", "초당 거래 건수", "Too Many Requests"
    ))


class PacedTransport:
    def __init__(self, transport, pacer):
        self.transport = transport
        self.pacer = pacer

    def request_json(self, method, url, *, headers, body=None, timeout):
        started_at = time.monotonic()
        path = urlsplit(url).path
        if path.endswith(("/order", "/order-rvsecncl")):
            priority = 0
        elif path.endswith(("/inquire-balance", "/inquire-ccnl",
                            "/inquire-ngt-balance", "/inquire-ngt-ccnl",
                            "/inquire-asking-price")):
            priority = 1
        else:
            priority = 2
        self.pacer.wait(priority, timeout)
        # Treat timeout as one end-to-end budget. Previously a request could
        # wait `timeout` seconds in the local queue and then consume another
        # full `timeout` in urlopen, leaving controller ownership timers out of
        # sync with the worker that was still running.
        remaining_timeout = max(0.001, float(timeout) - (time.monotonic() - started_at))
        try:
            result = self.transport.request_json(
                method, url, headers=headers, body=body, timeout=remaining_timeout
            )
        except Exception as exc:
            if is_rate_limit(exc, getattr(exc, "status", None)):
                self.pacer.limited()
            raise
        if is_rate_limit(result):
            self.pacer.limited()
        # Never replay POSTs (or silently repeat failed reads).
        return result
