import unittest
from types import SimpleNamespace
from unittest.mock import patch

from infra.kis.kis_client import KisClient


class KisQuoteBackoffTests(unittest.TestCase):
    def test_quote_failures_use_capped_exponential_backoff(self):
        state = SimpleNamespace(
            _quote_generation=4,
            _quote_inflight=True,
            _quote_error_streak=2,
            _quote_backoff_until=0.0,
        )

        with patch("infra.kis.kis_client.time.monotonic", return_value=100.0):
            KisClient._record_quote_failure(state, 4)

        self.assertFalse(state._quote_inflight)
        self.assertEqual(state._quote_error_streak, 3)
        self.assertEqual(state._quote_backoff_until, 102.0)

        state._quote_inflight = True
        state._quote_error_streak = 10
        with patch("infra.kis.kis_client.time.monotonic", return_value=200.0):
            KisClient._record_quote_failure(state, 4)
        self.assertEqual(state._quote_backoff_until, 202.0)

    def test_stale_quote_failure_does_not_backoff_new_subscription(self):
        state = SimpleNamespace(
            _quote_generation=5,
            _quote_inflight=True,
            _quote_error_streak=0,
            _quote_backoff_until=0.0,
        )

        KisClient._record_quote_failure(state, 4)

        self.assertTrue(state._quote_inflight)
        self.assertEqual(state._quote_error_streak, 0)

    def test_poll_is_suppressed_during_backoff(self):
        state = SimpleNamespace(
            _quote_inflight=False,
            _connected=True,
            _market_symbol="A05610",
            _quote_backoff_until=101.0,
        )

        with patch("infra.kis.kis_client.time.monotonic", return_value=100.0):
            KisClient._poll_quote(state)

        self.assertFalse(state._quote_inflight)


if __name__ == "__main__":
    unittest.main()
