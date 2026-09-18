import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from infra.kis.rate_limit import RestPacer, PacedTransport, shared_pacer
from infra.kis.kis_client import KisClient


class RateLimitTests(unittest.TestCase):
    def test_all_request_types_share_start_spacing(self):
        pacer = RestPacer(0.03)
        times = []
        base = Mock()
        base.request_json.side_effect = lambda *a, **kw: (times.append(time.monotonic()) or {"rt_cd": "0"})
        transport = PacedTransport(base, pacer)
        def send(path):
            transport.request_json("POST" if path == "order" else "GET",
                "https://test/" + path, headers={}, timeout=2)
        workers = [threading.Thread(target=send, args=(p,)) for p in
                   ("order", "order-rvsecncl", "inquire-balance", "inquire-time-fuopchartprice")]
        for worker in workers: worker.start()
        for worker in workers: worker.join(3)
        self.assertEqual(len(times), 4)
        self.assertTrue(all(b-a >= 0.025 for a,b in zip(times, times[1:])))

    def test_priority_classification(self):
        pacer = Mock()
        base = Mock()
        base.request_json.return_value = {"rt_cd": "0"}
        transport = PacedTransport(base, pacer)
        for path, priority in (("order", 0), ("order-rvsecncl", 0),
                               ("inquire-balance", 1), ("inquire-ccnl", 1),
                               ("inquire-ngt-balance", 1), ("inquire-ngt-ccnl", 1),
                               ("inquire-asking-price", 1),
                               ("inquire-time-fuopchartprice", 2)):
            transport.request_json("GET", "https://test/" + path, headers={}, timeout=10)
            pacer.wait.assert_called_with(priority, 10)

    @patch("infra.kis.rate_limit.time.monotonic", side_effect=[100.0, 102.5])
    def test_queue_wait_is_subtracted_from_network_timeout(self, _monotonic):
        pacer = Mock()
        base = Mock()
        base.request_json.return_value = {"rt_cd": "0"}

        PacedTransport(base, pacer).request_json(
            "GET", "https://test/inquire-balance", headers={}, timeout=10.0
        )

        self.assertEqual(base.request_json.call_args.kwargs["timeout"], 7.5)

    def test_limit_response_sets_backoff_without_replaying_order(self):
        pacer = RestPacer(0)
        base = Mock()
        base.request_json.return_value = {"rt_cd": "1", "msg_cd": "EGW00201"}
        transport = PacedTransport(base, pacer)
        transport.request_json("POST", "https://test/order", headers={}, timeout=1)
        self.assertEqual(base.request_json.call_count, 1)
        self.assertGreater(pacer.blocked_until, time.monotonic() + 1)
        with self.assertRaises(TimeoutError):
            transport.request_json("POST", "https://test/order", headers={}, timeout=0.01)
        self.assertEqual(base.request_json.call_count, 1)
        self.assertEqual(pacer.waiters, [])

    def test_http_429_sets_backoff_and_propagates(self):
        pacer = RestPacer(0)
        error = RuntimeError("limited")
        error.status = 429
        base = Mock()
        base.request_json.side_effect = error
        with self.assertRaises(RuntimeError):
            PacedTransport(base, pacer).request_json(
                "GET", "https://test/inquire-balance", headers={}, timeout=1)
        self.assertEqual(pacer.limit_streak, 1)
        pacer.limited()
        self.assertEqual(pacer.limit_streak, 2)

    def test_same_app_reuses_budget_and_endpoints_are_isolated(self):
        a = shared_pacer("test-endpoint", "test-key", 0.03)
        self.assertIs(a, shared_pacer("test-endpoint", "test-key", 0.02))
        self.assertIsNot(a, shared_pacer("other-endpoint", "test-key", 0.02))

    def test_duplicate_background_work_is_not_started(self):
        state = SimpleNamespace(
            _rest_rate_lock=threading.Lock(), _background_requests=set(),
            message_received=SimpleNamespace(emit=Mock()),
        )
        jobs = []
        class DeferredThread:
            def __init__(self, *, target, **kw): jobs.append(target)
            def start(self): pass
        operation = Mock(return_value={"rt_cd": "0"})
        with patch("infra.kis.kis_client.threading.Thread", DeferredThread):
            self.assertEqual(KisClient._background(state, operation, Mock(), request_name="BALANCE"), 0)
            self.assertEqual(KisClient._background(state, operation, Mock(), request_name="BALANCE"), -1)
            self.assertEqual(len(jobs), 1)
            jobs.pop()()
            self.assertEqual(state._background_requests, set())
            self.assertEqual(operation.call_count, 1)
            self.assertEqual(KisClient._background(state, operation, Mock(), request_name="BALANCE"), 0)
