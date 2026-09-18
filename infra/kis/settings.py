from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class KisConfigurationError(ValueError):
    """Raised when required KIS paper credentials are missing or malformed."""


def _env_text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default) or "").strip()


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _env_text(env, name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def _read_key_value_file(path: Path) -> dict[str, str]:
    """Read a small local settings file without logging its contents."""

    if not path.is_file():
        return {}
    text = _read_text_file(path)
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("KIS_"):
            values[name] = value.strip().strip('"').strip("'")
    return values


def _read_text_file(path: Path) -> str:
    last_error: UnicodeError | None = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError as exc:
            last_error = exc
    if last_error is not None:
        raise KisConfigurationError(f"Cannot decode KIS credential file: {path.name}") from last_error
    return ""


def _read_credentials(path: Path) -> tuple[str, str]:
    """Extract APP KEY/SECRET from either labelled or simple text files."""

    if not path.is_file():
        return "", ""
    lines = [line.strip() for line in _read_text_file(path).splitlines() if line.strip()]
    labelled: dict[str, str] = {}
    for line in lines:
        separator = "=" if "=" in line else (":" if ":" in line else "")
        if not separator:
            continue
        name, value = line.split(separator, 1)
        normalized = "".join(ch for ch in name.upper() if ch.isalnum())
        if "SECRET" in normalized:
            labelled["secret"] = value.strip()
        elif "APPKEY" in normalized or normalized == "KEY":
            labelled["key"] = value.strip()
    if labelled.get("key") and labelled.get("secret"):
        return labelled["key"], labelled["secret"]

    # KIS portal copy files commonly contain a short title followed by the
    # 36-character APP KEY and the longer APP SECRET on separate lines.
    candidates = [line for line in lines if len(line) >= 20 and " " not in line]
    if len(candidates) >= 2:
        return candidates[0], candidates[1]
    return "", ""


@dataclass(frozen=True)
class KisPaperSettings:
    """KIS virtual-trading connection settings.

    Secrets are deliberately sourced from environment variables and are hidden
    from ``repr`` so logs cannot accidentally expose them.
    """

    app_key: str
    app_secret: str
    account_no: str = ""
    product_code: str = "03"
    hts_id: str = ""
    rest_url: str = "https://openapivts.koreainvestment.com:29443"
    websocket_url: str = "ws://ops.koreainvestment.com:31000/tryitout"
    customer_type: str = "P"
    # The KIS paper endpoint regularly needs more than 10 seconds during busy
    # periods. Keep enough headroom for a single queued REST request while the
    # controller remains fail-closed until account/warmup data is confirmed.
    request_timeout_sec: float = 30.0
    # Paper quotes have been observed taking over 15 seconds to return.
    # A shorter timeout discards valid responses; this is not a refresh rate.
    quote_timeout_sec: float = 30.0
    rest_min_interval_sec: float = 1.05
    websocket_subscription_delay_sec: float = 0.5
    order_enabled: bool = False
    futures_symbol: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "KisPaperSettings":
        source = dict(os.environ if env is None else env)
        if env is None:
            local_file = Path(__file__).resolve().parents[2] / ".env.kis.local"
            for name, value in _read_key_value_file(local_file).items():
                source.setdefault(name, value)

        credential_file = _env_text(source, "KIS_CREDENTIAL_FILE")
        if not credential_file and env is None:
            default_file = Path.home() / "Desktop" / "모의계좌 app.txt"
            if default_file.is_file():
                credential_file = str(default_file)
        if credential_file and (
            not _env_text(source, "KIS_PAPER_APP_KEY")
            or not _env_text(source, "KIS_PAPER_APP_SECRET")
        ):
            app_key, app_secret = _read_credentials(Path(credential_file).expanduser())
            if app_key:
                source.setdefault("KIS_PAPER_APP_KEY", app_key)
            if app_secret:
                source.setdefault("KIS_PAPER_APP_SECRET", app_secret)
        return cls(
            app_key=_env_text(source, "KIS_PAPER_APP_KEY"),
            app_secret=_env_text(source, "KIS_PAPER_APP_SECRET"),
            account_no=_env_text(source, "KIS_PAPER_ACCOUNT_NO"),
            product_code=_env_text(source, "KIS_PAPER_PRODUCT_CODE", "03"),
            hts_id=_env_text(source, "KIS_HTS_ID"),
            rest_url=_env_text(
                source,
                "KIS_PAPER_REST_URL",
                "https://openapivts.koreainvestment.com:29443",
            ).rstrip("/"),
            websocket_url=_env_text(
                source,
                "KIS_PAPER_WEBSOCKET_URL",
                "ws://ops.koreainvestment.com:31000/tryitout",
            ),
            customer_type=_env_text(source, "KIS_CUSTOMER_TYPE", "P").upper(),
            request_timeout_sec=float(
                _env_text(source, "KIS_REQUEST_TIMEOUT_SEC", "30.0")
            ),
            quote_timeout_sec=float(
                _env_text(source, "KIS_QUOTE_TIMEOUT_SEC", "30.0")
            ),
            rest_min_interval_sec=float(
                _env_text(source, "KIS_REST_MIN_INTERVAL_SEC", "1.05")
            ),
            websocket_subscription_delay_sec=float(
                _env_text(source, "KIS_WS_SUBSCRIPTION_DELAY_SEC", "0.5")
            ),
            order_enabled=_env_bool(source, "KIS_PAPER_ORDER_ENABLED", False),
            futures_symbol=_env_text(source, "KIS_FUTURES_SYMBOL"),
        )

    def validate(
        self,
        *,
        require_account: bool = False,
        require_hts_id: bool = False,
    ) -> None:
        missing = []
        if not self.app_key:
            missing.append("KIS_PAPER_APP_KEY")
        if not self.app_secret:
            missing.append("KIS_PAPER_APP_SECRET")
        if require_account and not self.account_no:
            missing.append("KIS_PAPER_ACCOUNT_NO")
        if require_hts_id and not self.hts_id:
            missing.append("KIS_HTS_ID")
        if missing:
            raise KisConfigurationError(
                "Missing KIS paper environment variables: " + ", ".join(missing)
            )
        if self.account_no and (not self.account_no.isdigit() or len(self.account_no) != 8):
            raise KisConfigurationError("KIS_PAPER_ACCOUNT_NO must be exactly 8 digits")
        if not self.product_code.isdigit() or len(self.product_code) != 2:
            raise KisConfigurationError("KIS_PAPER_PRODUCT_CODE must be exactly 2 digits")
        if self.customer_type not in {"P", "B"}:
            raise KisConfigurationError("KIS_CUSTOMER_TYPE must be P or B")
        if not self.rest_url.startswith("https://"):
            raise KisConfigurationError("KIS_PAPER_REST_URL must use https://")
        if not self.websocket_url.startswith(("ws://", "wss://")):
            raise KisConfigurationError(
                "KIS_PAPER_WEBSOCKET_URL must use ws:// or wss://"
            )
        if self.request_timeout_sec <= 0:
            raise KisConfigurationError("KIS_REQUEST_TIMEOUT_SEC must be positive")
        if self.quote_timeout_sec <= 0:
            raise KisConfigurationError("KIS_QUOTE_TIMEOUT_SEC must be positive")
        if self.rest_min_interval_sec < 0:
            raise KisConfigurationError("KIS_REST_MIN_INTERVAL_SEC cannot be negative")
        if self.websocket_subscription_delay_sec < 0:
            raise KisConfigurationError(
                "KIS_WS_SUBSCRIPTION_DELAY_SEC cannot be negative"
            )

    @property
    def account_product(self) -> str:
        return f"{self.account_no}-{self.product_code}" if self.account_no else ""

    def safe_summary(self) -> dict[str, object]:
        return {
            "mode": "paper",
            "credentials_configured": bool(self.app_key and self.app_secret),
            "account_configured": bool(self.account_no),
            "product_code": self.product_code,
            "hts_id_configured": bool(self.hts_id),
            "rest_url": self.rest_url,
            "websocket_url": self.websocket_url,
            "quote_timeout_sec": self.quote_timeout_sec,
            "order_enabled": self.order_enabled,
            "futures_symbol_configured": bool(self.futures_symbol),
        }

    def __repr__(self) -> str:
        summary = self.safe_summary()
        fields = ", ".join(f"{key}={value!r}" for key, value in summary.items())
        return f"KisPaperSettings({fields})"
