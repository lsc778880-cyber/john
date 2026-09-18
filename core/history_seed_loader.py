from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from core.models import BarData


def _first(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


class HistorySeedLoader:
    """Load KIS domestic futures one-minute bars for strategy warmup."""

    def __init__(self, client, timeout_sec: float = 10.0, logger: Optional[Callable[[str], None]] = None) -> None:
        self.client = client
        self.timeout_sec = float(timeout_sec or 10.0)
        self.logger = logger

    def _log(self, message: str) -> None:
        if self.logger:
            try:
                self.logger(message)
            except Exception:
                pass

    def load_seed_bars(
        self,
        code_tr: str,
        target_rows: int,
        realtime_code: str = "",
        progress_cb=None,
        *,
        initial_bars: Iterable[BarData] | None = None,
        checkpoint_cb: Callable[[list[BarData]], None] | None = None,
    ) -> list[BarData]:
        symbol = str(code_tr or realtime_code or "").strip()
        if not symbol:
            raise ValueError("KIS_FUTURES_SYMBOL is required for warmup")
        target = max(1, int(target_rows or 2000))
        cursor_dt = datetime.now().replace(second=0, microsecond=0)
        rows: dict[datetime, BarData] = {
            bar.dt: bar
            for bar in (initial_bars or [])
            if isinstance(bar, BarData) and bar.dt is not None
        }
        if rows:
            cursor_dt = min(rows).replace(second=0, microsecond=0)
            if progress_cb:
                progress_cb(stage="KIS_BARS_RESUME", loaded=len(rows), valid=len(rows), target=target)
            if len(rows) >= target:
                return sorted(rows.values(), key=lambda bar: bar.dt)[-target:]
        max_pages = min(60, (target // 100) + 8)
        for _page_no in range(max_pages):
            payload = None
            for attempt in range(1, 6):
                try:
                    candidate = self.client.request_minute_bars(
                        symbol,
                        date=cursor_dt.strftime("%Y%m%d"),
                        time_text=cursor_dt.strftime("%H%M%S"),
                    )
                    message = str(candidate.get("msg1") or "")
                    if str(candidate.get("rt_cd", "0")) not in {"", "0"}:
                        if "초당 거래건수" in message and attempt < 5:
                            self._log(f"KIS minute bars rate-limited; retry {attempt}/5")
                            time.sleep(1.1 * attempt)
                            continue
                        raise RuntimeError(message or "KIS minute-bar request rejected")
                    payload = candidate
                    break
                except Exception as exc:
                    if "초당 거래건수" in str(exc) and attempt < 5:
                        self._log(f"KIS minute bars rate-limited; retry {attempt}/5")
                        time.sleep(1.1 * attempt)
                        continue
                    raise
            if payload is None:
                raise RuntimeError("KIS minute-bar request retries exhausted")
            raw_rows = payload.get("output2") or []
            if isinstance(raw_rows, dict):
                raw_rows = [raw_rows]
            page: list[BarData] = []
            for raw in raw_rows if isinstance(raw_rows, list) else []:
                if not isinstance(raw, dict):
                    continue
                dt = self._parse_datetime(raw)
                if dt is None:
                    continue
                close = self._float(_first(raw, "futs_prpr", "stck_prpr", "close"))
                if close <= 0:
                    continue
                page.append(BarData(
                    code=symbol,
                    dt=dt,
                    open=self._float(_first(raw, "futs_oprc", "stck_oprc", "open", default=close)) or close,
                    high=self._float(_first(raw, "futs_hgpr", "stck_hgpr", "high", default=close)) or close,
                    low=self._float(_first(raw, "futs_lwpr", "stck_lwpr", "low", default=close)) or close,
                    close=close,
                    volume=self._float(_first(raw, "cntg_vol", "acml_vol", "volume")),
                    oi=None,
                ))
            for bar in page:
                rows[bar.dt] = bar
            if checkpoint_cb:
                checkpoint_cb(sorted(rows.values(), key=lambda bar: bar.dt))
            if progress_cb:
                progress_cb(stage="KIS_BARS", loaded=len(rows), valid=len(rows), target=target)
            if not page or len(rows) >= target:
                break
            oldest = min(bar.dt for bar in page)
            if oldest >= cursor_dt:
                break
            cursor_dt = oldest.replace(second=0, microsecond=0)
            time.sleep(0.12)
        result = sorted(rows.values(), key=lambda bar: bar.dt)
        if len(result) > target:
            result = result[-target:]
        if progress_cb:
            progress_cb(stage="DONE", loaded=len(result), valid=len(result), target=target)
        return result

    @staticmethod
    def _float(value: Any) -> float:
        try:
            return abs(float(str(value or "0").replace(",", "").replace("+", "").strip()))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _parse_datetime(row: dict[str, Any]) -> datetime | None:
        day = "".join(ch for ch in str(_first(row, "stck_bsop_date", "bsop_date", "date")) if ch.isdigit())
        clock = "".join(ch for ch in str(_first(row, "stck_cntg_hour", "futs_cntg_hour", "cntg_hour", "time")) if ch.isdigit())
        combined = day[:8] + clock[:6].ljust(6, "0")
        try:
            return datetime.strptime(combined, "%Y%m%d%H%M%S") if len(day) >= 8 else None
        except ValueError:
            return None
