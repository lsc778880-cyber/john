from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from .adapter import KisPaperAdapter
from .settings import KisConfigurationError


@dataclass(frozen=True)
class KisFuturesOrder:
    side: str
    symbol: str
    quantity: int
    price: Decimal = Decimal("0")
    order_type: str = "LIMIT"
    condition: str = "NONE"

    def validate(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not self.symbol or not self.symbol.isalnum():
            raise ValueError("a KIS futures symbol is required")
        if int(self.quantity) <= 0:
            raise ValueError("quantity must be positive")
        if self.order_type not in {"LIMIT", "MARKET", "BEST"}:
            raise ValueError("order_type must be LIMIT, MARKET or BEST")
        if self.condition not in {"NONE", "IOC", "FOK"}:
            raise ValueError("condition must be NONE, IOC or FOK")
        if self.order_type == "LIMIT" and self.price <= 0:
            raise ValueError("LIMIT orders require a positive price")


class KisPaperFuturesApi:
    ORDER_PATH = "/uapi/domestic-futureoption/v1/trading/order"
    CANCEL_PATH = "/uapi/domestic-futureoption/v1/trading/order-rvsecncl"
    BALANCE_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-balance"
    ORDERS_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-ccnl"
    QUOTE_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-asking-price"
    ORDERABLE_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-psbl-order"
    MINUTE_BARS_PATH = "/uapi/domestic-futureoption/v1/quotations/inquire-time-fuopchartprice"

    def __init__(self, adapter: KisPaperAdapter) -> None:
        self.adapter = adapter
        self.settings = adapter.settings

    @staticmethod
    def _price_text(value: Decimal) -> str:
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @staticmethod
    def _order_codes(order: KisFuturesOrder) -> tuple[str, str, str]:
        type_code = {"LIMIT": "01", "MARKET": "02", "BEST": "04"}[order.order_type]
        condition_code = {"NONE": "0", "IOC": "3", "FOK": "4"}[order.condition]
        if order.condition == "NONE":
            division_code = type_code
        else:
            division_code = {
                ("LIMIT", "IOC"): "10",
                ("LIMIT", "FOK"): "11",
                ("MARKET", "IOC"): "12",
                ("MARKET", "FOK"): "13",
                ("BEST", "IOC"): "14",
                ("BEST", "FOK"): "15",
            }[(order.order_type, order.condition)]
        return type_code, condition_code, division_code

    def _require_account(self) -> None:
        self.settings.validate(require_account=True)

    def _require_order_enabled(self) -> None:
        self._require_account()
        if not self.settings.order_enabled:
            raise KisConfigurationError(
                "KIS paper orders are disabled; set KIS_PAPER_ORDER_ENABLED=1 only after account checks"
            )

    def place_order(self, order: KisFuturesOrder) -> dict[str, Any]:
        self._require_order_enabled()
        order.validate()
        nmpr_type, krx_condition, order_division = self._order_codes(order)
        unit_price = Decimal("0") if order.order_type in {"MARKET", "BEST"} else order.price
        return self.adapter.request_json(
            "POST",
            self.ORDER_PATH,
            tr_id="VTTO1101U",
            body={
                "ORD_PRCS_DVSN_CD": "02",
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "SLL_BUY_DVSN_CD": "02" if order.side == "BUY" else "01",
                "SHTN_PDNO": order.symbol,
                "ORD_QTY": str(int(order.quantity)),
                "UNIT_PRICE": self._price_text(unit_price),
                "NMPR_TYPE_CD": nmpr_type,
                "KRX_NMPR_CNDT_CD": krx_condition,
                "ORD_DVSN_CD": order_division,
                "CTAC_TLNO": "",
                "FUOP_ITEM_DVSN_CD": "",
            },
        )

    def cancel_order(self, order_no: str, *, quantity: int = 0) -> dict[str, Any]:
        self._require_order_enabled()
        normalized = str(order_no or "").strip()
        if not normalized or not normalized.isdigit():
            raise ValueError("a numeric original KIS order number is required")
        return self.adapter.request_json(
            "POST",
            self.CANCEL_PATH,
            tr_id="VTTO1103U",
            body={
                "ORD_PRCS_DVSN_CD": "02",
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "RVSE_CNCL_DVSN_CD": "02",
                "ORGN_ODNO": normalized,
                "ORD_QTY": str(max(0, int(quantity))),
                "UNIT_PRICE": "0",
                "NMPR_TYPE_CD": "02",
                "KRX_NMPR_CNDT_CD": "0",
                "RMN_QTY_YN": "Y",
                "ORD_DVSN_CD": "01",
                "FUOP_ITEM_DVSN_CD": "",
            },
        )

    def inquire_balance(self) -> dict[str, Any]:
        self._require_account()
        return self.adapter.request_json(
            "GET",
            self.BALANCE_PATH,
            tr_id="VTFO6118R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "MGNA_DVSN": "01",
                "EXCC_STAT_CD": "1",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )

    def inquire_orders(
        self,
        *,
        date: str | None = None,
        unfilled_only: bool = False,
    ) -> dict[str, Any]:
        self._require_account()
        day = str(date or datetime.now().strftime("%Y%m%d"))
        if len(day) != 8 or not day.isdigit():
            raise ValueError("date must be YYYYMMDD")
        return self.adapter.request_json(
            "GET",
            self.ORDERS_PATH,
            tr_id="VTTO5201R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "STRT_ORD_DT": day,
                "END_ORD_DT": day,
                "SLL_BUY_DVSN_CD": "00",
                # KIS: 00=all, 01=filled, 02=unfilled.  The open-order
                # synchronizer must request 02; the execution monitor keeps
                # the default all-history view.
                "CCLD_NCCS_DVSN": "02" if unfilled_only else "00",
                "SORT_SQN": "DS",
                "PDNO": "",
                "STRT_ODNO": "",
                "MKET_ID_CD": "",
                "CTX_AREA_FK200": "",
                "CTX_AREA_NK200": "",
            },
        )

    def inquire_quote(self, symbol: str, market_code: str = "F") -> dict[str, Any]:
        self.settings.validate()
        symbol_text = str(symbol or "").strip()
        if not symbol_text:
            raise ValueError("a KIS futures symbol is required")
        return self.adapter.request_json(
            "GET",
            self.QUOTE_PATH,
            tr_id="FHMIF10010000",
            params={
                "FID_COND_MRKT_DIV_CODE": str(market_code or "F"),
                "FID_INPUT_ISCD": symbol_text,
            },
            timeout_sec=self.settings.quote_timeout_sec,
        )

    def inquire_orderable(
        self,
        symbol: str,
        *,
        side: str,
        price: Decimal,
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        self._require_account()
        symbol_text = str(symbol or "").strip()
        side_text = str(side or "").strip().upper()
        order_type_text = str(order_type or "LIMIT").strip().upper()
        if not symbol_text:
            raise ValueError("a KIS futures symbol is required")
        if side_text not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if order_type_text not in {"LIMIT", "MARKET", "BEST"}:
            raise ValueError("order_type must be LIMIT, MARKET or BEST")
        order_code = {"LIMIT": "01", "MARKET": "02", "BEST": "04"}[order_type_text]
        unit_price = Decimal("0") if order_type_text != "LIMIT" else Decimal(price)
        return self.adapter.request_json(
            "GET",
            self.ORDERABLE_PATH,
            tr_id="VTTO5105R",
            params={
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "PDNO": symbol_text,
                "SLL_BUY_DVSN_CD": "02" if side_text == "BUY" else "01",
                "UNIT_PRICE": self._price_text(unit_price),
                "ORD_DVSN_CD": order_code,
            },
        )

    def inquire_minute_bars(
        self,
        symbol: str,
        *,
        date: str,
        time_text: str = "153500",
        include_previous: bool = True,
        market_code: str = "F",
    ) -> dict[str, Any]:
        self.settings.validate()
        symbol_text = str(symbol or "").strip()
        day = str(date or "").strip()
        clock = str(time_text or "").strip()
        if not symbol_text:
            raise ValueError("a KIS futures symbol is required")
        if len(day) != 8 or not day.isdigit():
            raise ValueError("date must be YYYYMMDD")
        if len(clock) != 6 or not clock.isdigit():
            raise ValueError("time_text must be HHMMSS")
        return self.adapter.request_json(
            "GET",
            self.MINUTE_BARS_PATH,
            tr_id="FHKIF03020200",
            params={
                "FID_COND_MRKT_DIV_CODE": str(market_code or "F"),
                "FID_INPUT_ISCD": symbol_text,
                "FID_HOUR_CLS_CODE": "60",
                "FID_PW_DATA_INCU_YN": "Y" if include_previous else "N",
                "FID_FAKE_TICK_INCU_YN": "N",
                "FID_INPUT_DATE_1": day,
                "FID_INPUT_HOUR_1": clock,
            },
        )
