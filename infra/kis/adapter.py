from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlencode

from .auth import JsonTransport, KisAuthClient, KisCredentials
from .settings import KisPaperSettings
from .websocket import KisWebSocketClient


class KisPaperAdapter:
    """High-level, paper-only KIS connection adapter.

    It authenticates REST and WebSocket sessions but does not submit orders.
    Order endpoints are added only after the connection and account-read path
    has been validated against the user's virtual futures/options account.
    """

    def __init__(
        self,
        settings: KisPaperSettings | None = None,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.settings = settings or KisPaperSettings.from_env()
        self.auth = KisAuthClient(self.settings, transport=transport)
        self.websocket = KisWebSocketClient(self.settings, self.auth)

    @property
    def futures(self):
        from .futures import KisPaperFuturesApi

        return KisPaperFuturesApi(self)

    @classmethod
    def from_env(cls) -> "KisPaperAdapter":
        return cls(KisPaperSettings.from_env())

    def authenticate(self, *, force: bool = False) -> KisCredentials:
        return self.auth.authenticate(force=force)

    def status(self) -> dict[str, object]:
        return self.settings.safe_summary()

    def request_json(
        self,
        method: str,
        path: str,
        *,
        tr_id: str,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Call a documented KIS REST path using paper credentials only."""

        if not str(path or "").startswith("/") or "://" in str(path):
            raise ValueError("path must be a relative KIS API path beginning with /")
        method_text = str(method or "GET").strip().upper()
        if method_text not in {"GET", "POST"}:
            raise ValueError("method must be GET or POST")
        url = f"{self.settings.rest_url}{path}"
        if params:
            url = f"{url}?{urlencode(dict(params))}"
        headers = self.auth.rest_headers(tr_id)
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        request_timeout = (
            self.settings.request_timeout_sec
            if timeout_sec is None
            else float(timeout_sec)
        )
        if request_timeout <= 0:
            raise ValueError("timeout_sec must be positive")
        return self.auth.transport.request_json(
            method_text,
            url,
            headers=headers,
            body=body,
            timeout=request_timeout,
        )
