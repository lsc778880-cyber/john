import unittest
from unittest.mock import patch

from core.history_seed_loader import HistorySeedLoader


def _bar(day: str, clock: str, price: str) -> dict[str, str]:
    return {
        "stck_bsop_date": day,
        "stck_cntg_hour": clock,
        "futs_oprc": price,
        "futs_hgpr": price,
        "futs_lwpr": price,
        "futs_prpr": price,
        "cntg_vol": "1",
    }


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request_minute_bars(self, symbol, *, date, time_text):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class HistorySeedLoaderTests(unittest.TestCase):
    @patch("core.history_seed_loader.time.sleep", return_value=None)
    def test_rate_limit_is_retried(self, _sleep):
        client = _Client(
            [
                {"rt_cd": "1", "msg1": "초당 거래건수를 초과하였습니다."},
                {"rt_cd": "0", "output2": [_bar("20260915", "152000", "1036.5")]},
            ]
        )
        result = HistorySeedLoader(client).load_seed_bars("A05610", 1)
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].close, 1036.5)

    def test_non_rate_limit_error_is_not_hidden(self):
        client = _Client([{"rt_cd": "1", "msg1": "invalid symbol"}])
        with self.assertRaisesRegex(RuntimeError, "invalid symbol"):
            HistorySeedLoader(client).load_seed_bars("BAD", 1)

    @patch("core.history_seed_loader.time.sleep", return_value=None)
    def test_retry_resumes_from_successful_page_checkpoint(self, _sleep):
        first_client = _Client(
            [
                {
                    "rt_cd": "0",
                    "output2": [
                        _bar("20260915", "152000", "1036.5"),
                        _bar("20260915", "151900", "1036.0"),
                    ],
                },
                TimeoutError("temporary KIS timeout"),
            ]
        )
        checkpoints = []
        with self.assertRaisesRegex(TimeoutError, "temporary KIS timeout"):
            HistorySeedLoader(first_client).load_seed_bars(
                "A05610",
                3,
                checkpoint_cb=lambda bars: checkpoints.append(list(bars)),
            )

        self.assertEqual(len(checkpoints[-1]), 2)
        second_client = _Client(
            [{"rt_cd": "0", "output2": [_bar("20260915", "151800", "1035.5")]}]
        )
        resumed = HistorySeedLoader(second_client).load_seed_bars(
            "A05610",
            3,
            initial_bars=checkpoints[-1],
        )

        self.assertEqual(second_client.calls, 1)
        self.assertEqual(len(resumed), 3)
        self.assertEqual([bar.close for bar in resumed], [1035.5, 1036.0, 1036.5])


if __name__ == "__main__":
    unittest.main()
