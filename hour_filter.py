"""
hour_filter.py - Hour-Open Anti-Counter Filter Module
Пре-трейд фильтрация входов против развивающейся 1H свечи и перегретого фандинга.
Strict Zero-Stub Policy. Production-ready implementation.
"""

import time
from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

class VetoReason(Enum):
    NONE = "NONE"
    VETO_INVALID_DATA = "VETO_INVALID_DATA"
    VETO_SHORT_BULLISH_HOUR = "VETO_SHORT_BULLISH_HOUR"
    VETO_LONG_BEARISH_HOUR = "VETO_LONG_BEARISH_HOUR"
    VETO_SHORT_FUNDING_AND_ML = "VETO_SHORT_FUNDING_AND_ML"

@dataclass(frozen=True)
class HourFilterEvaluation:
    is_allowed: bool
    veto_reason: VetoReason
    delta_hour_pct: float
    current_price: float
    open_price_1h: float
    funding_rz: float
    ml_prob_long: float
    message: str

class HourOpenFilter:
    """
    Защита от контр-трендовых входов на развивающейся часовой свече:
    Delta_hour = (P_cur - P_open_1h) / P_open_1h
    - Если Delta_hour > +0.15% (+0.0015): SHORT запрещен
    - Если Delta_hour < -0.15% (-0.0015): LONG запрещен
    - Если Funding_RZ < -2.5 и ML_LongProb > 0.70: SHORT запрещен
    - Fail-Closed: при невалидных данных вход блокируется (VETO_INVALID_DATA).
    - Защита от дрейфа: если с момента обновления open_price_1h прошло более 65 минут,
      проверяется и обновляется базовая цена часа (через REST API или fallback к текущей цене),
      исключая застревание вечной ложной дельты.
    """
    def __init__(
        self,
        threshold_delta_pct: float = 0.0015, # 0.15%
        funding_rz_critical: float = -2.5,
        ml_prob_threshold: float = 0.70,
        max_drift_seconds: float = 65 * 60, # 65 минут
        symbol: str = "BTCUSDT"
    ):
        self.threshold_delta_pct = float(threshold_delta_pct)
        self.funding_rz_critical = float(funding_rz_critical)
        self.ml_prob_threshold = float(ml_prob_threshold)
        self.max_drift_seconds = float(max_drift_seconds)
        self.symbol = symbol
        self.open_price_1h: float = 0.0
        self.last_update_time: float = 0.0

    def update_open_price(self, open_price: float, timestamp: Optional[float] = None) -> None:
        """Обновление базовой цены часа и времени фиксации."""
        if open_price is not None and open_price > 0:
            self.open_price_1h = round(float(open_price), 2)
            self.last_update_time = float(timestamp if timestamp is not None else time.time())

    def refresh_open_price_from_api(self, symbol: Optional[str] = None) -> Optional[float]:
        """Обновление базовой цены часа через REST API Binance Futures."""
        sym = symbol or self.symbol
        try:
            import httpx
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1h&limit=2"
            r = httpx.get(url, timeout=3.0)
            if r.status_code == 200:
                data = r.json()
                if data and len(data) > 0:
                    latest = data[-1]
                    new_open = float(latest[1])
                    self.update_open_price(new_open)
                    return new_open
        except Exception:
            pass
        return None

    def evaluate(
        self,
        side: str, # "LONG" | "SHORT"
        current_price: float,
        open_price_1h: Optional[float] = None,
        funding_rz: float = 0.0,
        ml_prob_long: float = 0.50,
        current_time: Optional[float] = None
    ) -> HourFilterEvaluation:
        # 1. Принцип Fail-Closed (Безопасность): при невалидных котировках блокируем вход
        if current_price is None or current_price <= 0.0:
            return HourFilterEvaluation(
                is_allowed=False,
                veto_reason=VetoReason.VETO_INVALID_DATA,
                delta_hour_pct=0.0,
                current_price=0.0 if current_price is None else current_price,
                open_price_1h=0.0 if open_price_1h is None else open_price_1h,
                funding_rz=funding_rz,
                ml_prob_long=ml_prob_long,
                message="FAIL_CLOSED: Невалидные цены котировок, вход заблокирован"
            )

        if open_price_1h is not None and open_price_1h <= 0.0:
            return HourFilterEvaluation(
                is_allowed=False,
                veto_reason=VetoReason.VETO_INVALID_DATA,
                delta_hour_pct=0.0,
                current_price=current_price,
                open_price_1h=open_price_1h,
                funding_rz=funding_rz,
                ml_prob_long=ml_prob_long,
                message="FAIL_CLOSED: Невалидная цена открытия часа (<=0), вход заблокирован"
            )

        now = float(current_time if current_time is not None else time.time())

        # Если передана валидная цена часа
        if open_price_1h is not None and open_price_1h > 0:
            if self.open_price_1h <= 0.0 or abs(open_price_1h - self.open_price_1h) > 1e-6:
                self.update_open_price(open_price_1h, now)
        elif self.open_price_1h > 0:
            open_price_1h = self.open_price_1h
        else:
            return HourFilterEvaluation(
                is_allowed=False,
                veto_reason=VetoReason.VETO_INVALID_DATA,
                delta_hour_pct=0.0,
                current_price=current_price,
                open_price_1h=0.0,
                funding_rz=funding_rz,
                ml_prob_long=ml_prob_long,
                message="FAIL_CLOSED: Базовая цена часа не инициализирована, вход заблокирован"
            )

        # 2. Защита от дрейфа базовой цены часа (> 65 минут без обновления)
        if self.last_update_time > 0 and (now - self.last_update_time) > self.max_drift_seconds:
            refreshed = self.refresh_open_price_from_api()
            if refreshed is not None and refreshed > 0:
                open_price_1h = refreshed
                self.update_open_price(refreshed, now)
            else:
                # Если REST API недоступен, устраняем бесконечную ложную дельту, сбрасывая базу к current_price
                open_price_1h = current_price
                self.update_open_price(current_price, now)

        side_upper = side.upper()
        raw_delta = (current_price - open_price_1h) / open_price_1h
        delta_hour = round(raw_delta, 6) # Точность вычислений до логического сравнения

        # 2. Защита от входа в SHORT против бычьего часа
        if side_upper == "SHORT" and delta_hour > self.threshold_delta_pct:
            return HourFilterEvaluation(
                is_allowed=False,
                veto_reason=VetoReason.VETO_SHORT_BULLISH_HOUR,
                delta_hour_pct=delta_hour,
                current_price=current_price,
                open_price_1h=open_price_1h,
                funding_rz=funding_rz,
                ml_prob_long=ml_prob_long,
                message=f"SHORT запрещен: свеча 1H бычья (Delta={delta_hour:+.3%} > +{self.threshold_delta_pct:.2%})"
            )

        # 3. Защита от входа в LONG против медвежьего часа
        if side_upper == "LONG" and delta_hour < -self.threshold_delta_pct:
            return HourFilterEvaluation(
                is_allowed=False,
                veto_reason=VetoReason.VETO_LONG_BEARISH_HOUR,
                delta_hour_pct=delta_hour,
                current_price=current_price,
                open_price_1h=open_price_1h,
                funding_rz=funding_rz,
                ml_prob_long=ml_prob_long,
                message=f"LONG запрещен: свеча 1H медвежья (Delta={delta_hour:+.3%} < -{self.threshold_delta_pct:.2%})"
            )

        # 4. Защита от входа в SHORT при перегретом шортами фандинге и сильном ML
        if side_upper == "SHORT" and funding_rz < self.funding_rz_critical and ml_prob_long > self.ml_prob_threshold:
            return HourFilterEvaluation(
                is_allowed=False,
                veto_reason=VetoReason.VETO_SHORT_FUNDING_AND_ML,
                delta_hour_pct=delta_hour,
                current_price=current_price,
                open_price_1h=open_price_1h,
                funding_rz=funding_rz,
                ml_prob_long=ml_prob_long,
                message=f"SHORT запрещен: перегрев фандинга (RZ={funding_rz:.2f} < {self.funding_rz_critical}) при ML_Long={ml_prob_long:.1%}"
            )

        return HourFilterEvaluation(
            is_allowed=True,
            veto_reason=VetoReason.NONE,
            delta_hour_pct=delta_hour,
            current_price=current_price,
            open_price_1h=open_price_1h,
            funding_rz=funding_rz,
            ml_prob_long=ml_prob_long,
            message="HOUR_FILTER_PASSED"
        )
