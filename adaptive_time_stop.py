"""
adaptive_time_stop.py - Adaptive Reversal Grace-Period Time-Stop Module
Адаптивное продление удержания позиции на 90-й минуте при наличии локального разворота 15m.
Strict Zero-Stub Policy. Production-ready implementation.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

class TimeStopAction(Enum):
    HOLD = "HOLD"
    ENTER_GRACE_PERIOD = "ENTER_GRACE_PERIOD"
    UPDATE_TRAILING_IN_GRACE = "UPDATE_TRAILING_IN_GRACE"
    EXIT_TIME_STOP = "EXIT_TIME_STOP"
    EXIT_BROKEN_PULLBACK = "EXIT_BROKEN_PULLBACK"

@dataclass
class GracePeriodState:
    is_in_grace: bool = False
    grace_start_ms: int = 0
    adverse_extremum: float = 0.0 # Локальный максимум для SHORT / минимум для LONG во время Grace
    breakeven_sl: float = 0.0

@dataclass(frozen=True)
class TimeStopDecision:
    action: TimeStopAction
    new_sl: float
    reason: str
    is_in_grace: bool
    grace_elapsed_min: float

class AdaptiveTimeStopEngine:
    """
    Адаптивный тайм-стоп:
    1. Базовый порог удержания: 90 минут.
    2. Если PnL <= 0, проверяем 15m свечу:
       - Для SHORT: если Close_15m < Open_15m или P_cur < P_ref_15m -> включаем Grace Period (до 45 мин).
       - Для LONG: если Close_15m > Open_15m или P_cur > P_ref_15m -> включаем Grace Period.
    3. В Grace Period стоп подтягивается в безубыток Entry +/- 0.05%.
    4. Если цена обновляет экстремум против позиции (Broken Pullback) -> немедленный выход.
    """
    def __init__(
        self,
        base_timeout_ms: int = 90 * 60 * 1000,   # 90 минут
        max_grace_period_ms: int = 45 * 60 * 1000 # 45 минут (3 свечи 15m)
    ):
        self.base_timeout_ms = base_timeout_ms
        self.max_grace_period_ms = max_grace_period_ms

    def evaluate(
        self,
        side: str,                  # "LONG" | "SHORT"
        entry_price: float,
        current_price: float,
        current_sl: float,
        open_time_ms: int,
        current_time_ms: int,
        candle_15m_open: float,
        candle_15m_close: float,
        grace_state: GracePeriodState
    ) -> TimeStopDecision:
        duration_ms = current_time_ms - open_time_ms
        side_upper = side.upper()
        is_long = side_upper == "LONG"
        be_offset = entry_price * 0.0005 # +0.05%

        # 1. Позиция находится в активном Grace Period (наивысший приоритет сопровождения)
        if grace_state.is_in_grace:
            grace_elapsed_ms = current_time_ms - grace_state.grace_start_ms
            grace_elapsed_min = grace_elapsed_ms / 60000.0

            # 1.1 Проверка истечения максимального Grace Period (45 минут)
            if grace_elapsed_ms >= self.max_grace_period_ms:
                grace_state.is_in_grace = False
                return TimeStopDecision(
                    action=TimeStopAction.EXIT_TIME_STOP,
                    new_sl=current_sl,
                    reason=f"TIME_STOP: Grace Period 45m истек (Elapsed={grace_elapsed_min:.1f}m)",
                    is_in_grace=False,
                    grace_elapsed_min=grace_elapsed_min
                )

            # 1.2 Проверка слома разворота (Broken Pullback): пробой базы отскока против позиции
            if is_long:
                if current_price < grace_state.adverse_extremum:
                    grace_state.is_in_grace = False
                    return TimeStopDecision(
                        action=TimeStopAction.EXIT_BROKEN_PULLBACK,
                        new_sl=current_sl,
                        reason=f"BROKEN_PULLBACK: Цена обновила базу отскока против LONG ({current_price:.2f} < {grace_state.adverse_extremum:.2f})",
                        is_in_grace=False,
                        grace_elapsed_min=grace_elapsed_min
                    )
                # Подтягивание безубытка
                be_sl = round(entry_price + be_offset, 2)
                new_sl = max(current_sl, be_sl)
            else:
                if current_price > grace_state.adverse_extremum:
                    grace_state.is_in_grace = False
                    return TimeStopDecision(
                        action=TimeStopAction.EXIT_BROKEN_PULLBACK,
                        new_sl=current_sl,
                        reason=f"BROKEN_PULLBACK: Цена обновила базу отскока против SHORT ({current_price:.2f} > {grace_state.adverse_extremum:.2f})",
                        is_in_grace=False,
                        grace_elapsed_min=grace_elapsed_min
                    )
                # Подтягивание безубытка для шорта
                be_sl = round(entry_price - be_offset, 2)
                new_sl = min(current_sl, be_sl) if current_sl > 0.0 else be_sl

            return TimeStopDecision(
                action=TimeStopAction.UPDATE_TRAILING_IN_GRACE,
                new_sl=new_sl,
                reason=f"GRACE_ACTIVE: Сопровождение разворота ({grace_elapsed_min:.1f}m / 45m)",
                is_in_grace=True,
                grace_elapsed_min=grace_elapsed_min
            )

        # 2. Если позиция еще моложе 90 минут — обычное удержание
        if duration_ms < self.base_timeout_ms:
            return TimeStopDecision(
                action=TimeStopAction.HOLD,
                new_sl=current_sl,
                reason="DURATION_UNDER_BASE_TIMEOUT",
                is_in_grace=False,
                grace_elapsed_min=0.0
            )

        # 3. Наступило 90 минут. Проверка PnL позиции
        pnl = (current_price - entry_price) if is_long else (entry_price - current_price)
        if pnl > be_offset:
            return TimeStopDecision(
                action=TimeStopAction.HOLD,
                new_sl=current_sl,
                reason=f"HOLD_PROFITABLE: Позиция в профите (+{pnl:.2f}$), тайм-стоп не требуется",
                is_in_grace=False,
                grace_elapsed_min=0.0
            )

        # 4. Позиция в убытке или нуле на 90-й минуте: оценка 15m динамики на вход в Grace Period
        # Для SHORT: свеча 15m красная (close < open) или текущая цена падает в нашу сторону
        is_short_reversal = (not is_long) and (candle_15m_close < candle_15m_open or current_price < candle_15m_open)
        # Для LONG: свеча 15m зеленая (close > open) или текущая цена растет
        is_long_reversal = is_long and (candle_15m_close > candle_15m_open or current_price > candle_15m_open)

        if is_short_reversal or is_long_reversal:
            grace_state.is_in_grace = True
            grace_state.grace_start_ms = current_time_ms
            # Фиксируем базу отскока (экстремум): не текущий пик, а база свечи разворота
            grace_state.adverse_extremum = min(candle_15m_open, current_price) if is_long else max(candle_15m_open, current_price)
            
            be_sl = round(entry_price + be_offset if is_long else entry_price - be_offset, 2)
            grace_state.breakeven_sl = be_sl

            return TimeStopDecision(
                action=TimeStopAction.ENTER_GRACE_PERIOD,
                new_sl=be_sl,
                reason="ADAPTIVE_GRACE_ACTIVATED: 15m свеча разворачивается в сторону позиции",
                is_in_grace=True,
                grace_elapsed_min=0.0
            )

        # 5. Если на 90-й минуте разворотной динамики нет и позиция в нуле/минусе — закрываем по TIME_STOP
        return TimeStopDecision(
            action=TimeStopAction.EXIT_TIME_STOP,
            new_sl=current_sl,
            reason="TIME_STOP: 90 минут истекло, импульс не развился (Scratch Exit)",
            is_in_grace=False,
            grace_elapsed_min=0.0
        )
