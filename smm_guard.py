"""
smm_guard.py - Signed Midpoint Markout (SMM Guard) Module
Защита лимитного исполнения и входа от токсичного потока и неблагоприятного отбора (Adverse Selection).
Strict Zero-Stub Policy. Production-ready implementation.
"""

import time
import math
import logging
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List

logger = logging.getLogger("SMMGuard")

@dataclass(frozen=True, slots=True)
class MidpointTick:
    timestamp: float
    midpoint: float

@dataclass(frozen=True, slots=True)
class SMMCheckResult:
    action: str              # "KEEP_ORDER" | "CANCEL_ADVERSE_SELECTION"
    smm_10s_bps: float       # Signed Midpoint Markout за 10 секунд (в bps)
    price_drift_bps: float   # Дрейф середины рынка (в bps)
    is_toxic: bool           # Превышен ли порог токсичности
    history_len: int         # Число тиков в окне
    time_delta_sec: float    # Фактическое окно времени
    reason: str

class SMMGuard:
    """
    Модуль 10-секундного Signed Midpoint Markout:
    SMM_10s = 10^4 * q_e * (m_t - m_{t-10s}) / m_{t-10s} [bps]
    где q_e = +1 для BUY/LONG, -1 для SELL/SHORT.
    
    Если цена резко летит навстречу лимитному ордеру (снос стакана):
    - Для BUY ордера цена падает (m_t < m_{t-10s}) -> markout отрицательный.
    - Для SELL ордера цена взлетает (m_t > m_{t-10s}) -> markout отрицательный.
    
    При импульсе против ордера > 4.0 bps (т.е. SMM <= -4.0 bps)
    триггерится экстренная блокировка/отмена SMM_TOXIC_ADVERSE_SELECTION_CANCEL.
    """
    def __init__(
        self,
        window_seconds: float = 10.0,
        toxic_threshold_bps: float = 4.0, # 4.0 bps порог токсичности
        buffer_maxlen: int = 100
    ):
        self.window_seconds = float(window_seconds)
        self.toxic_threshold_bps = float(toxic_threshold_bps)
        self.buffer_maxlen = int(buffer_maxlen)
        self._history: Dict[str, deque] = {}

    def _get_buffer(self, symbol: str) -> deque:
        sym = symbol.upper()
        if sym not in self._history:
            self._history[sym] = deque(maxlen=self.buffer_maxlen)
        return self._history[sym]

    def record_tick(self, symbol: str, midpoint: float, timestamp: Optional[float] = None) -> None:
        """Запись тика цены midpoint в кольцевой буфер символа."""
        if midpoint is None or midpoint <= 0.0:
            return
        ts = timestamp if timestamp is not None else time.time()
        buf = self._get_buffer(symbol)
        buf.append(MidpointTick(timestamp=ts, midpoint=midpoint))

    def evaluate_order(
        self,
        symbol: str,
        side: str, # "BUY" | "SELL" | "LONG" | "SHORT"
        current_midpoint: float,
        timestamp: Optional[float] = None
    ) -> SMMCheckResult:
        now = timestamp if timestamp is not None else time.time()
        buf = self._get_buffer(symbol)

        # Добавляем текущий тик
        self.record_tick(symbol, current_midpoint, now)

        if len(buf) < 2:
            return SMMCheckResult(
                action="KEEP_ORDER",
                smm_10s_bps=0.0,
                price_drift_bps=0.0,
                is_toxic=False,
                history_len=len(buf),
                time_delta_sec=0.0,
                reason="SMM_INSUFFICIENT_HISTORY"
            )

        # Поиск самого старого тика в пределах окна window_seconds
        cutoff = now - self.window_seconds
        reference_tick = buf[0]
        for tick in buf:
            if tick.timestamp >= cutoff:
                reference_tick = tick
                break

        time_delta = now - reference_tick.timestamp
        ref_mid = reference_tick.midpoint

        if ref_mid <= 0.0 or time_delta <= 0.0:
            return SMMCheckResult(
                action="KEEP_ORDER",
                smm_10s_bps=0.0,
                price_drift_bps=0.0,
                is_toxic=False,
                history_len=len(buf),
                time_delta_sec=time_delta,
                reason="SMM_ZERO_REFERENCE"
            )

        # Относительный дрейф середины стакана
        raw_drift = (current_midpoint - ref_mid) / ref_mid
        price_drift_bps = raw_drift * 10000.0

        # Определение знака стороны (q_e)
        side_upper = side.upper()
        if side_upper in ["BUY", "LONG"]:
            q_e = 1.0
        elif side_upper in ["SELL", "SHORT"]:
            q_e = -1.0
        else:
            q_e = 1.0

        # Signed Midpoint Markout
        smm_10s_bps = q_e * price_drift_bps

        # Токсичный снос стакана навстречу нашему ордеру
        is_toxic = smm_10s_bps <= -self.toxic_threshold_bps

        if is_toxic:
            action = "CANCEL_ADVERSE_SELECTION"
            reason = f"SMM_TOXIC_ADVERSE_SELECTION_CANCEL (SMM={smm_10s_bps:.2f} bps <= -{self.toxic_threshold_bps:.1f} bps)"
        else:
            action = "KEEP_ORDER"
            reason = "SMM_FLOW_BENIGN"

        return SMMCheckResult(
            action=action,
            smm_10s_bps=round(smm_10s_bps, 2),
            price_drift_bps=round(price_drift_bps, 2),
            is_toxic=is_toxic,
            history_len=len(buf),
            time_delta_sec=round(time_delta, 3),
            reason=reason
        )

    def get_current_smm(self, symbol: str, side: str, current_midpoint: float, timestamp: Optional[float] = None) -> float:
        """Получение текущего 10-секундного SMM (в bps) без мутации буфера тиков."""
        if current_midpoint is None or current_midpoint <= 0.0:
            return 0.0
        buf = self._get_buffer(symbol)
        if len(buf) < 2:
            return 0.0
        now = timestamp if timestamp is not None else time.time()
        cutoff = now - self.window_seconds
        ref_mid = buf[0].midpoint
        for tick in buf:
            if tick.timestamp >= cutoff:
                ref_mid = tick.midpoint
                break
        if ref_mid <= 0.0:
            return 0.0
        raw_drift = (current_midpoint - ref_mid) / ref_mid
        price_drift_bps = raw_drift * 10000.0
        q_e = 1.0 if side.upper() in ["BUY", "LONG"] else -1.0
        return round(q_e * price_drift_bps, 2)

    def reset_buffer(self, symbol: str) -> None:
        """Очистка буфера тиков символа (например, при завершении ордера)."""
        sym = symbol.upper()
        if sym in self._history:
            self._history[sym].clear()
