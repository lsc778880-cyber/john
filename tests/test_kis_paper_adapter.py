import unittest
import asyncio
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from infra.kis import (
    KisAuthClient,
    KisConfigurationError,
    KisPaperAdapter,
    KisPaperSettings,
    KisFuturesOrder,
    KisWebSocketClient,
    KisWsSubscription,
)
from infra.kis.kis_client import KisClient


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request_json(self, method, url, *, headers, body=None, timeout=10.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": dict(body or {}),
                "timeout": timeout,
            }
        )
        if url.endswith("/oauth2/tokenP"):
            return {
                "access_token": "ACCESS_TOKEN_DO_NOT_LOG",
                "token_type": "Bearer",
                "access_token_token_expired": "2026-09-16 12:00:00",
            }
        if url.endswith("/oauth2/Approval"):
            return {"approval_key": "APPROVAL_KEY_DO_NOT_LOG"}
        return {"rt_cd": "0", "output": {"ok": True}}


class KisPaperAdapterTests(unittest.TestCase):
    def setUp(self):
        self.settings = KisPaperSettings(
            app_key="paper-app-key",
            app_secret="paper-app-secret",
            account_no="12345678",
            product_code="03",
            hts_id="paper-hts-id",
        )
        self.transport = FakeTransport()

    def test_settings_load_only_paper_environment(self):
        settings = KisPaperSettings.from_env(
            {
                "KIS_PAPER_APP_KEY": "paper-key",
                "KIS_PAPER_APP_SECRET": "paper-secret",
                "KIS_PAPER_ACCOUNT_NO": "87654321",
                "KIS_HTS_ID": "hts-user",
            }
        )
        self.assertEqual(settings.product_code, "03")
        self.assertIn("openapivts.koreainvestment.com", settings.rest_url)
        self.assertIn(":31000", settings.websocket_url)
        self.assertNotIn("paper-secret", repr(settings))
        self.assertEqual(settings.request_timeout_sec, 30.0)

    def test_settings_can_read_unlabelled_portal_credential_file(self):
        with TemporaryDirectory() as temp_dir:
            credential_file = Path(temp_dir) / "paper-app.txt"
            credential_file.write_text(
                "모의계좌\n\n\n" + "K" * 36 + "\n\n" + "S" * 180 + "\n",
                encoding="utf-8",
            )
            settings = KisPaperSettings.from_env(
                {"KIS_CREDENTIAL_FILE": str(credential_file)}
            )
        self.assertEqual(settings.app_key, "K" * 36)
        self.assertEqual(settings.app_secret, "S" * 180)
        self.assertNotIn("S" * 20, repr(settings))

    def test_missing_credentials_fail_before_network(self):
        settings = KisPaperSettings(app_key="", app_secret="")
        with self.assertRaises(KisConfigurationError):
            KisAuthClient(settings, transport=self.transport).get_access_token()
        self.assertEqual(self.transport.calls, [])

    def test_authentication_uses_official_paper_auth_contracts(self):
        auth = KisAuthClient(
            self.settings,
            transport=self.transport,
            now=lambda: datetime(2026, 9, 15, 12, 0, 0),
        )
        credentials = auth.authenticate()
        self.assertTrue(credentials.safe_summary()["access_token_ready"])
        self.assertTrue(credentials.safe_summary()["websocket_approval_ready"])
        self.assertEqual(self.transport.calls[0]["url"], self.settings.rest_url + "/oauth2/tokenP")
        self.assertEqual(self.transport.calls[0]["body"]["appsecret"], "paper-app-secret")
        self.assertEqual(self.transport.calls[1]["url"], self.settings.rest_url + "/oauth2/Approval")
        self.assertEqual(self.transport.calls[1]["body"]["secretkey"], "paper-app-secret")

    def test_access_token_is_cached_in_memory(self):
        auth = KisAuthClient(
            self.settings,
            transport=self.transport,
            now=lambda: datetime(2026, 9, 15, 12, 0, 0),
        )
        self.assertEqual(auth.get_access_token(), "ACCESS_TOKEN_DO_NOT_LOG")
        self.assertEqual(auth.get_access_token(), "ACCESS_TOKEN_DO_NOT_LOG")
        self.assertEqual(len(self.transport.calls), 1)

    def test_access_token_is_reused_from_local_cache_across_processes(self):
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "token.json"
            first_transport = FakeTransport()
            first = KisAuthClient(
                self.settings,
                transport=first_transport,
                now=lambda: datetime(2026, 9, 15, 12, 0, 0),
                cache_path=cache_path,
            )
            self.assertEqual(first.get_access_token(), "ACCESS_TOKEN_DO_NOT_LOG")
            second_transport = FakeTransport()
            second = KisAuthClient(
                self.settings,
                transport=second_transport,
                now=lambda: datetime(2026, 9, 15, 12, 1, 0),
                cache_path=cache_path,
            )
            self.assertEqual(second.get_access_token(), "ACCESS_TOKEN_DO_NOT_LOG")
            self.assertEqual(second_transport.calls, [])

    def test_websocket_payload_and_frame_parser(self):
        auth = KisAuthClient(self.settings, transport=self.transport)
        websocket = KisWebSocketClient(self.settings, auth)
        payload = websocket.subscription_payload(
            KisWsSubscription("H0IFCNT0", "101V9000"),
            approval_key="approval",
        )
        self.assertEqual(payload["header"]["approval_key"], "approval")
        self.assertEqual(payload["body"]["input"]["tr_id"], "H0IFCNT0")
        frame = websocket.parse_frame("0|H0IFCNT0|1|101V9000^123.45")
        self.assertEqual(frame["kind"], "realtime")
        self.assertFalse(frame["encrypted"])
        self.assertEqual(frame["tr_id"], "H0IFCNT0")

    def test_client_subscribes_paper_futures_notice_and_emits_activity(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        client = KisClient(adapter)
        self.assertGreaterEqual(client._quote_timer.interval(), 1100)
        stop_event = threading.Event()

        class FakePaperWebSocket:
            def __init__(self):
                self.subscriptions = []

            async def stream(self, subscriptions):
                self.subscriptions = list(subscriptions)
                yield KisWebSocketClient.parse_frame(
                    "1|H0IFCNI9|1|encrypted-paper-order-notice"
                )
                stop_event.set()

        fake_ws = FakePaperWebSocket()
        adapter.websocket = fake_ws
        activities = []
        client.order_activity_received.connect(activities.append)
        asyncio.run(client._order_stream_loop(stop_event))

        self.assertEqual(len(fake_ws.subscriptions), 1)
        self.assertEqual(fake_ws.subscriptions[0].tr_id, "H0IFCNI9")
        self.assertEqual(fake_ws.subscriptions[0].tr_key, "paper-hts-id")
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["source"], "KIS_WS_H0IFCNI9")

    def test_generic_rest_call_cannot_escape_paper_host(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        with self.assertRaises(ValueError):
            adapter.request_json(
                "GET", "https://example.com/steal", tr_id="TEST"
            )

    def test_paper_futures_order_uses_kis_body_and_demo_tr_id(self):
        settings = KisPaperSettings(
            app_key="paper-app-key",
            app_secret="paper-app-secret",
            account_no="12345678",
            product_code="03",
            order_enabled=True,
        )
        adapter = KisPaperAdapter(settings, transport=self.transport)
        adapter.futures.place_order(
            KisFuturesOrder(
                side="BUY",
                symbol="101W09",
                quantity=1,
                price=__import__("decimal").Decimal("352.15"),
            )
        )
        order_call = self.transport.calls[-1]
        self.assertTrue(order_call["url"].endswith("/domestic-futureoption/v1/trading/order"))
        self.assertEqual(order_call["headers"]["tr_id"], "VTTO1101U")
        self.assertEqual(order_call["body"]["SLL_BUY_DVSN_CD"], "02")
        self.assertEqual(order_call["body"]["ORD_DVSN_CD"], "01")
        self.assertEqual(order_call["body"]["UNIT_PRICE"], "352.15")

    def test_paper_order_is_disabled_by_default(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        with self.assertRaises(KisConfigurationError):
            adapter.futures.place_order(
                KisFuturesOrder(
                    side="SELL",
                    symbol="101W09",
                    quantity=1,
                    price=__import__("decimal").Decimal("352.10"),
                )
            )

    def test_balance_query_uses_paper_futures_tr_id(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        adapter.futures.inquire_balance()
        balance_call = self.transport.calls[-1]
        self.assertEqual(balance_call["headers"]["tr_id"], "VTFO6118R")
        self.assertIn("CANO=12345678", balance_call["url"])

    def test_open_order_query_uses_unfilled_only_filter(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        adapter.futures.inquire_orders(date="20260915", unfilled_only=True)
        call = self.transport.calls[-1]
        self.assertIn("CCLD_NCCS_DVSN=02", call["url"])

    def test_orderable_query_uses_kis_paper_contract(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        adapter.futures.inquire_orderable(
            "101W09", side="BUY", price=Decimal("352.15")
        )
        call = self.transport.calls[-1]
        self.assertEqual(call["headers"]["tr_id"], "VTTO5105R")
        self.assertIn("PDNO=101W09", call["url"])
        self.assertIn("SLL_BUY_DVSN_CD=02", call["url"])

    def test_minute_bars_query_uses_kis_chart_contract(self):
        adapter = KisPaperAdapter(self.settings, transport=self.transport)
        adapter.futures.inquire_minute_bars(
            "101W09", date="20260915", time_text="153500"
        )
        call = self.transport.calls[-1]
        self.assertEqual(call["headers"]["tr_id"], "FHKIF03020200")
        self.assertIn("FID_HOUR_CLS_CODE=60", call["url"])
        self.assertIn("FID_INPUT_DATE_1=20260915", call["url"])


if __name__ == "__main__":
    unittest.main()
