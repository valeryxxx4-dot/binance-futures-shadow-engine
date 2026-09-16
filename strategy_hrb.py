"""
strategy_hrb.py - Стратегия Б: Hub-Regime Breakout (HRB)
Трендовая стратегия на 15M таймфрейме с фильтрацией рыночного режима через ZeroMQ (или локальный трендовый расчет) и пробоем динамического диапазона.

Правила:
- Таймфрейм: 15M свечи.
- Канал Дончяна: 20 периодов (или Азиатский боковик 03:00-09:00 MSK).
- Импульсный пробой: закрытие свечи за границей диапазона.
- Полнотелость: размер тела свечи (abs(close - open) / (high - low)) >= 60% (min_body_ratio = 0.6).
- Объемный импульс: Volume свечи >= 1.8 * SMA(Volume, 20).
- Режим рынка: получение ZMQ статуса ("BULL_TREND", "BEAR_TREND", "RANGE"). При отсутствии ZMQ - автономный расчет (EMA20 vs EMA50).
- Стоп-лосс: 1.5 * ATR(14).
- Трейлинг: Chandelier Exit (2.0 * ATR(14) от достигнутого пика / впадины).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import math
import time

try:
    import zmq
except ImportError:
    zmq = None


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    is_closed: bool


class HubRegimeBreakout:
    def __init__(self, config: dict, broker):
        self.config = config.get("strategy_hrb", {})
        self.broker = broker
        self.strategy_id = "HRB"
        self.pos_size = float(config.get("position_size_eth", 0.05))

        # Parameters
        self.donchian_period = int(self.config.get("donchian_period", 20))
        self.min_body_ratio = float(self.config.get("min_body_ratio", 0.6))
        self.min_volume_ratio = float(self.config.get("min_volume_ratio", 1.8))
        self.atr_period = int(self.config.get("atr_period", 14))
        self.sl_atr_mult = float(self.config.get("sl_atr_multiplier", 1.5))
        self.chandelier_atr_mult = float(self.config.get("chandelier_atr_multiplier", 2.0))
        self.require_hub_trend = bool(self.config.get("require_hub_trend", True))
        self.hub_zmq_endpoint = self.config.get("hub_zmq_endpoint", "tcp://127.0.0.1:5555")

        # History of 15m closed candles
        self.candles_15m: List[Candle] = []

        # ZeroMQ Client Setup (Non-blocking subscriber)
        self.zmq_context = None
        self.zmq_socket = None
        self.last_hub_regime = "OFFLINE_FALLBACK"
        self.last_hub_update_time = 0
        self._init_zmq()

        # Trade tracking
        self.extreme_price: Optional[float] = None
        self.current_atr: float = 5.0  # Fallback default

    def _init_zmq(self):
        if zmq is None:
            return
        try:
            self.zmq_context = zmq.Context()
            self.zmq_socket = self.zmq_context.socket(zmq.SUB)
            self.zmq_socket.setsockopt_string(zmq.SUBSCRIBE, "")
            self.zmq_socket.setsockopt(zmq.RCVTIMEO, 10)  # Non-blocking 10ms timeout
            self.zmq_socket.connect(self.hub_zmq_endpoint)
        except Exception:
            self.zmq_socket = None

    def __del__(self):
        try:
            if getattr(self, "zmq_socket", None) is not None:
                self.zmq_socket.close()
            if getattr(self, "zmq_context", None) is not None:
                self.zmq_context.term()
        except Exception:
            pass

    def poll_hub_status(self):
        """Проверка входящих пакетов режима от Hub с фолбэком."""
        if self.zmq_socket is not None:
            try:
                msg = self.zmq_socket.recv_string(flags=zmq.NOBLOCK)
                # Ожидается формат "REGIME:BULL_TREND" или JSON
                self.last_hub_regime = msg.strip()
                self.last_hub_update_time = time.time()
                return self.last_hub_regime
            except Exception:
                pass

        # Если хаб молчит > 120 сек, переходим в автономный режим расчета
        if time.time() - self.last_hub_update_time > 120:
            self.last_hub_regime = self._calculate_local_regime()
        return self.last_hub_regime

    def _calculate_local_regime(self) -> str:
        """Автономный расчет режима тренда (EMA20 vs EMA50)."""
        if len(self.candles_15m) < 50:
            return "NEUTRAL"
        closes = [c.close for c in self.candles_15m]
        # Fast EMA 20
        ema20 = self._calc_ema(closes, 20)
        # Slow EMA 50
        ema50 = self._calc_ema(closes, 50)
        
        if ema20 > ema50 * 1.001:
            return "BULL_TREND"
        elif ema20 < ema50 * 0.999:
            return "BEAR_TREND"
        return "RANGE"

    def _calc_ema(self, series: List[float], period: int) -> float:
        k = 2.0 / (period + 1)
        ema = series[0]
        for val in series[1:]:
            ema = (val * k) + (ema * (1 - k))
        return ema

    def calculate_indicators(self):
        """Расчет Дончяна, SMA объема и ATR."""
        if len(self.candles_15m) < max(self.donchian_period, self.atr_period) + 1:
            return None, None, None, None

        # Дончян за предыдущие N свечей (не включая текущую)
        window_donchian = self.candles_15m[-self.donchian_period-1:-1]
        donchian_high = max(c.high for c in window_donchian)
        donchian_low = min(c.low for c in window_donchian)

        # SMA объема за 20 свечей
        vols = [c.volume for c in self.candles_15m[-21:-1]]
        vol_sma = sum(vols) / len(vols) if vols else 1.0

        # ATR 14
        tr_list = []
        for i in range(-self.atr_period - 1, 0):
            curr = self.candles_15m[i]
            prev = self.candles_15m[i - 1]
            tr = max(
                curr.high - curr.low,
                abs(curr.high - prev.close),
                abs(curr.low - prev.close)
            )
            tr_list.append(tr)
        atr = sum(tr_list) / len(tr_list) if tr_list else 5.0
        self.current_atr = max(atr, 2.0)  # Минимальная планка

        return donchian_high, donchian_low, vol_sma, self.current_atr

    def on_kline_15m(self, candle: Candle):
        """Обработка 15-минутной свечи."""
        if not candle.is_closed:
            return

        self.candles_15m.append(candle)
        # Ограничиваем буфер 200 свечами
        if len(self.candles_15m) > 200:
            self.candles_15m.pop(0)

        self._evaluate_breakout_signal(candle)

    def _evaluate_breakout_signal(self, candle: Candle):
        pos = self.broker.get_position(self.strategy_id)
        if pos is not None:
            return

        donchian_high, donchian_low, vol_sma, atr = self.calculate_indicators()
        if donchian_high is None:
            return

        # Проверка полнотелости бара
        candle_range = candle.high - candle.low
        if candle_range <= 0.01:
            return
        body_size = abs(candle.close - candle.open)
        body_ratio = body_size / candle_range

        # Проверка объемного импульса
        vol_ratio = candle.volume / vol_sma if vol_sma > 0 else 1.0

        # Опрос режима
        regime = self.poll_hub_status()

        # 1. Bullish Breakout
        if candle.close > donchian_high:
            if body_ratio >= self.min_body_ratio and vol_ratio >= self.min_volume_ratio:
                # Фильтр тренда: разрешен при BULL_TREND или если хаб не требует строгого тренда
                if not self.require_hub_trend or "BULL" in regime or regime == "OFFLINE_FALLBACK":
                    entry_price = candle.close
                    sl_dist = round(self.sl_atr_mult * atr, 2)
                    sl_price = round(entry_price - sl_dist, 2)
                    tp_price = round(entry_price + (sl_dist * 3.0), 2)  # RR 1:3 базовый

                    success = self.broker.open_position(
                        strategy=self.strategy_id,
                        direction="LONG",
                        size=self.pos_size,
                        price=entry_price,
                        sl=sl_price,
                        tp=tp_price,
                        is_maker=False,
                        notes=f"HRB Long Breakout ({donchian_high}) VolRatio={vol_ratio:.1f} ATR={atr:.1f}"
                    )
                    if success:
                        self.extreme_price = entry_price
                    return

        # 2. Bearish Breakout
        if candle.close < donchian_low:
            if body_ratio >= self.min_body_ratio and vol_ratio >= self.min_volume_ratio:
                if not self.require_hub_trend or "BEAR" in regime or regime == "OFFLINE_FALLBACK":
                    entry_price = candle.close
                    sl_dist = round(self.sl_atr_mult * atr, 2)
                    sl_price = round(entry_price + sl_dist, 2)
                    tp_price = round(entry_price - (sl_dist * 3.0), 2)

                    success = self.broker.open_position(
                        strategy=self.strategy_id,
                        direction="SHORT",
                        size=self.pos_size,
                        price=entry_price,
                        sl=sl_price,
                        tp=tp_price,
                        is_maker=False,
                        notes=f"HRB Short Breakout ({donchian_low}) VolRatio={vol_ratio:.1f} ATR={atr:.1f}"
                    )
                    if success:
                        self.extreme_price = entry_price
                    return

    def on_market_tick(self, best_bid: float, best_ask: float):
        """
        Сопровождение позиции HRB трейлингом Chandelier Exit:
        - Лонг: стоп = Highest - 2.0 * ATR
        - Шорт: стоп = Lowest + 2.0 * ATR
        """
        pos = self.broker.get_position(self.strategy_id)
        if pos is None:
            self.extreme_price = None
            return

        current_price = best_bid if pos['direction'] == "LONG" else best_ask
        chandelier_dist = round(self.chandelier_atr_mult * self.current_atr, 2)

        if pos['direction'] == "LONG":
            if self.extreme_price is None or current_price > self.extreme_price:
                self.extreme_price = current_price

            chandelier_sl = round(self.extreme_price - chandelier_dist, 2)
            # Подтягиваем стоп только вверх
            if pos['sl'] is None or chandelier_sl > pos['sl']:
                # Стоп не должен быть выше текущей цены (защита от мгновенного выбивания)
                if chandelier_sl < current_price:
                    self.broker.modify_stop_loss(self.strategy_id, chandelier_sl)

        elif pos['direction'] == "SHORT":
            if self.extreme_price is None or current_price < self.extreme_price:
                self.extreme_price = current_price

            chandelier_sl = round(self.extreme_price + chandelier_dist, 2)
            # Подтягиваем стоп только вниз
            if pos['sl'] is None or chandelier_sl < pos['sl']:
                if chandelier_sl > current_price:
                    self.broker.modify_stop_loss(self.strategy_id, chandelier_sl)
