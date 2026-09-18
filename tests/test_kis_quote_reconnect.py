import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from infra.kis.kis_client import KisClient


class KisQuoteReconnectTests(unittest.TestCase):
    def test_disconnect_invalidates_inflight_quote(self):
        state = SimpleNamespace(
            _connected=True,
            _quote_generation=7,
            _market_symbol="A05610",
            _quote_inflight=True,
            _quote_timer=SimpleNamespace(stop=Mock()),
            _order_timer=SimpleNamespace(stop=Mock()),
            _stop_order_stream=Mock(),
        )

        KisClient.disconnect_api(state)

        self.assertFalse(state._connected)
        self.assertEqual(state._quote_generation, 8)
        self.assertEqual(state._market_symbol, "")
        self.assertFalse(state._quote_inflight)
        state._quote_timer.stop.assert_called_once_with()

    def test_duplicate_old_quote_does_not_latch_new_feed_inflight(self):
        state = SimpleNamespace(
            _connected=True,
            _quote_generation=8,
            _market_symbol="A05610",
            _quote_inflight=False,
            _quote_error_streak=0,
            _quote_backoff_until=0.0,
            adapter=SimpleNamespace(
                futures=SimpleNamespace(inquire_quote=Mock())
            ),
            _background=Mock(return_value=-1),
        )

        KisClient._poll_quote(state)

        self.assertFalse(state._quote_inflight)
        state._background.assert_called_once()

    def test_stale_quote_callback_is_ignored_after_reconnect(self):
        callbacks = {}

        def capture_background(operation, success, *, request_name, on_error):
            callbacks["success"] = success
            callbacks["error"] = on_error
            return 0

        tick_signal = SimpleNamespace(emit=Mock())
        state = SimpleNamespace(
            _connected=True,
            _quote_generation=3,
            _market_symbol="A05610",
            _quote_inflight=False,
            _quote_error_streak=0,
            _quote_backoff_until=0.0,
            adapter=SimpleNamespace(
                futures=SimpleNamespace(inquire_quote=Mock())
            ),
            _background=capture_background,
            tick_received=tick_signal,
        )
        KisClient._poll_quote(state)
        self.assertTrue(state._quote_inflight)

        state._quote_generation += 1
        state._quote_inflight = False
        callbacks["success"]({"output1": {"futs_prpr": "1063.40"}})

        tick_signal.emit.assert_not_called()
        self.assertFalse(state._quote_inflight)


if __name__ == "__main__":
    unittest.main()
