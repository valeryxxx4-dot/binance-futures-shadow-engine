"""
strategy_jazz.py - Стратегия Г: JAZZ (Dynamic 6H Corridor 5M Sweep + Ratchet Trailing)
Идентичная копия боевого плагина JAZZ (market_radar.py + two_stage_watchdog.py).

Правила:
- Инструмент: ETHUSDT.
- Таймфрейм: 5M свечи.
- Динамический коридор: 72 свечи 5M (6 часов) скользящего окна.
- Мертвая зона: центральные 30% диапазона коридора (внутри зоны входы заблокированы).
- Sweep экстремума:
  * SHORT Sweep: High закрытой свечи пробивает ref_high предшествующего окна (72 бара до текущего),
    а Close возвращается ниже ref_high. Возраст уровня >= 6 баров (30 мин).
    Wick Rejection: верхняя тень >= 0.8 * размер тела свечи.
  * LONG Sweep: Low закрытой свечи пробивает ref_low предшествующего окна,
    а Close возвращается выше ref_low. Возраст уровня >= 6 баров (30 мин).
    Wick Rejection: нижняя тень >= 0.8 * размер тела свечи.
- Риск-менеджмент:
  * Начальный стоп: экстремум бара свипа +/- 2.50 USDT буфер, ограничен рамками [4.00, 7.00] USDT от цены входа.
  * Тейк-профит: противоположная граница 6-часового коридора.
- Сопровождение (Two-Stage Watchdog / Ratchet Trailing):
  * Безубыток: при ходе >= +4.50 USDT стоп переносится на вход +/- 0.30 USDT.
  * Активация трейлинга: при ходе >= +6.50 USDT.
  * Буфер: 5.00 USDT (компрессия до 3.00 USDT при ходе >= +10.00 USDT).
  * Квантование шага подтяжки: 0.50 USDT.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import numpy as np


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


class JazzStrategy:
    def __init__(self, config: dict, broker):
        self.config = config.get("strategy_jazz", {})
        self.broker = broker
        self.strategy_id = "JAZZ"
        self.symbol = self.config.get("symbol", "ETHUSDT").upper()
        self.pos_size = float(self.config.get("position_size_eth", config.get("position_size_eth", 0.05)))

        # Параметры коридора и фильтров
        self.lookback_candles = int(self.config.get("lookback_candles", 72))
        self.dead_zone_pct = float(self.config.get("dead_zone_pct", 0.30))
        self.wick_ratio = float(self.config.get("wick_to_body_ratio", 0.80))
        self.min_level_age = int(self.config.get("min_level_age_candles", 6))

        # Параметры стопа
        self.sl_buffer = float(self.config.get("sl_buffer_usdt", 2.50))
        self.min_sl = float(self.config.get("min_sl_usdt", 4.00))
        self.max_sl = float(self.config.get("max_sl_usdt", 7.00))

        # Параметры Two-Stage Watchdog / Ratchet Trailing
        self.be_trigger = float(self.config.get("breakeven_trigger_usdt", 4.50))
        self.be_lock = float(self.config.get("breakeven_lock_usdt", 0.30))
        self.trailing_act = float(self.config.get("trailing_activation_usdt", 6.50))
        self.wide_buf = float(self.config.get("wide_buffer_usdt", 5.00))
        self.comp_buf = float(self.config.get("compressed_buffer_usdt", 3.00))
        self.comp_thresh = float(self.config.get("compression_threshold_usdt", 10.00))
        self.step_quant = float(self.config.get("step_quant_usdt", 0.50))

        # История 5M свечей
        self.candles_5m: List[Candle] = []
        self.last_processed_candle_time: Optional[int] = None

        # Текущие динамические границы коридора
        self.ref_high: Optional[float] = None
        self.ref_low: Optional[float] = None
        self.dead_zone: Optional[Tuple[float, float]] = None

        # Состояние активного сопровождения позиции
        self.extreme_price: float = 0.0
        self.be_activated: bool = False
        self.trailing_active: bool = False
        self.compression_active: bool = False

    def recalculate_corridor(self):
        """Пересчет динамических границ и мертвой зоны по последним N свечам."""
        if len(self.candles_5m) < self.min_level_age:
            return

        window = self.candles_5m[-self.lookback_candles:]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        self.ref_high = round(float(np.max(highs)), 2)
        self.ref_low = round(float(np.min(lows)), 2)
        ch_height = self.ref_high - self.ref_low
        ch_mid = (self.ref_high + self.ref_low) / 2.0
        dz_half = ch_height * (self.dead_zone_pct / 2.0)
        self.dead_zone = (round(ch_mid - dz_half, 2), round(ch_mid + dz_half, 2))

    def on_kline_5m(self, candle: Candle):
        """
        Обработка 5-минутной свечи ETHUSDT.
        Анализ сетапов One-Shot Sweep производится исключительно в момент закрытия свечи (x: true).
        """
        if not candle.is_closed:
            return

        # Защита от повторной обработки той же свечи
        if self.last_processed_candle_time == candle.open_time:
            return
        self.last_processed_candle_time = candle.open_time

        # Если истории недостаточно, просто накапливаем свечи
        if len(self.candles_5m) < self.lookback_candles:
            self.candles_5m.append(candle)
            self.recalculate_corridor()
            return

        # Корректный расчет экстремумов строго ДО проверяемого бара (Zero-Mock Hotfix от 08.09)
        prior_window = self.candles_5m[-self.lookback_candles:]
        prior_highs = np.array([c.high for c in prior_window])
        prior_lows = np.array([c.low for c in prior_window])

        ref_high = float(np.max(prior_highs))
        ref_low = float(np.min(prior_lows))
        ch_height = ref_high - ref_low
        ch_mid = (ref_high + ref_low) / 2.0
        dz_half = ch_height * (self.dead_zone_pct / 2.0)
        ref_dz_low = round(ch_mid - dz_half, 2)
        ref_dz_high = round(ch_mid + dz_half, 2)

        high_first_idx = int(np.argmax(prior_highs))
        high_age = len(prior_highs) - high_first_idx
        low_first_idx = int(np.argmin(prior_lows))
        low_age = len(prior_lows) - low_first_idx

        # Сохраняем свечу в историю
        self.candles_5m.append(candle)
        if len(self.candles_5m) > 250:
            self.candles_5m = self.candles_5m[-180:]

        self.recalculate_corridor()

        # Если уже есть открытая позиция JAZZ, повторный вход исключен
        if self.broker.get_position(self.strategy_id) is not None:
            return

        # Проверка Мертвой зоны
        in_dead_zone = (ref_dz_low <= candle.close <= ref_dz_high)
        if in_dead_zone:
            return

        c_open = candle.open
        c_high = candle.high
        c_low = candle.low
        c_close = candle.close
        body_size = abs(c_close - c_open)

        # Валидация сетапов One-Shot Sweep
        short_signal = False
        long_signal = False
        upper_wick = 0.0
        lower_wick = 0.0

        # 1. Проверка SHORT Sweep: закол хая + закрытие ниже него + Wick Check + возраст уровня
        if (c_high > ref_high) and (c_close < ref_high):
            if high_age >= self.min_level_age:
                upper_wick = c_high - max(c_open, c_close)
                if upper_wick >= (body_size * self.wick_ratio):
                    short_signal = True

        # 2. Проверка LONG Sweep: закол лоя + закрытие выше него + Wick Check + возраст уровня
        if (c_low < ref_low) and (c_close > ref_low):
            if low_age >= self.min_level_age:
                lower_wick = min(c_open, c_close) - c_low
                if lower_wick >= (body_size * self.wick_ratio):
                    long_signal = True

        # Разрешение конфликта при двустороннем пробое (аномальный вертолетный бар)
        if short_signal and long_signal:
            if upper_wick > lower_wick * 1.5:
                long_signal = False
            elif lower_wick > upper_wick * 1.5:
                short_signal = False
            else:
                # Симметричный вертолет — конфликт сигналов, вход пропускается для защиты депозита
                return

        if short_signal:
            expected_entry = c_close
            target_sl = c_high + self.sl_buffer
            calc_risk = target_sl - expected_entry
            if calc_risk > self.max_sl:
                target_sl = expected_entry + self.max_sl
            elif calc_risk < self.min_sl:
                target_sl = expected_entry + self.min_sl

            sl_price = round(target_sl, 2)
            tp_price = round(ref_low, 2)

            pos = self.broker.open_position(
                strategy=self.strategy_id,
                direction="SHORT",
                size=self.pos_size,
                price=expected_entry,
                is_maker=False,
                sl=sl_price,
                tp=tp_price,
                notes=f"JAZZ SHORT Sweep (6H High {ref_high:.2f}, age={high_age}b, wick={upper_wick:.2f})",
                symbol=self.symbol
            )
            if pos:
                self.extreme_price = expected_entry
                self.be_activated = False
                self.trailing_active = False
                self.compression_active = False
                pos["best_price"] = expected_entry
                pos["be_activated"] = False
                pos["trailing_active"] = False
                pos["compression_active"] = False
                self.broker._save_state()
            return

        elif long_signal:
            expected_entry = c_close
            target_sl = c_low - self.sl_buffer
            calc_risk = expected_entry - target_sl
            if calc_risk > self.max_sl:
                target_sl = expected_entry - self.max_sl
            elif calc_risk < self.min_sl:
                target_sl = expected_entry - self.min_sl

            sl_price = round(target_sl, 2)
            tp_price = round(ref_high, 2)

            pos = self.broker.open_position(
                strategy=self.strategy_id,
                direction="LONG",
                size=self.pos_size,
                price=expected_entry,
                is_maker=False,
                sl=sl_price,
                tp=tp_price,
                notes=f"JAZZ LONG Sweep (6H Low {ref_low:.2f}, age={low_age}b, wick={lower_wick:.2f})",
                symbol=self.symbol
            )
            if pos:
                self.extreme_price = expected_entry
                self.be_activated = False
                self.trailing_active = False
                self.compression_active = False
                pos["best_price"] = expected_entry
                pos["be_activated"] = False
                pos["trailing_active"] = False
                pos["compression_active"] = False
                self.broker._save_state()
            return

    def on_market_tick(self, best_bid: float, best_ask: float):
        """
        Сопровождение открытой позиции:
        - Двухэтапный сторож (Two-Stage Watchdog)
        - Безубыток (+0.30$ при ходе +4.50$)
        - Бесконечный храповик (Ratchet Trailing: +6.50$ активация, буфер 5.00$ / компрессия 3.00$ при +10.00$)
        """
        pos = self.broker.get_position(self.strategy_id)
        if pos is None:
            self.extreme_price = 0.0
            self.be_activated = False
            self.trailing_active = False
            self.compression_active = False
            return

        side = pos["direction"]
        entry_price = pos["entry_price"]
        current_sl = pos.get("sl")

        # Бесшовное восстановление контекста сопровождения из состояния позиции при перезапуске
        if self.extreme_price == 0.0:
            self.extreme_price = float(pos.get("best_price", entry_price))
            self.be_activated = bool(pos.get("be_activated", False))
            self.trailing_active = bool(pos.get("trailing_active", False))
            self.compression_active = bool(pos.get("compression_active", False))

        if side == "LONG":
            curr_price = best_bid
            if curr_price > self.extreme_price:
                self.extreme_price = curr_price
                pos["best_price"] = curr_price
                self.broker._save_state()

            move_profit = self.extreme_price - entry_price

            # 1. Фаза раннего безубытка (ход от +$4.50 до +$6.50)
            if self.be_trigger <= move_profit < self.trailing_act:
                if not self.be_activated:
                    target_sl = round(entry_price + self.be_lock, 2)
                    if (current_sl is None or target_sl > current_sl) and target_sl < curr_price:
                        self.broker.modify_stop_loss(self.strategy_id, target_sl)
                        self.be_activated = True
                        pos["be_activated"] = True
                        self.broker._save_state()

            # 2. Фаза Ratchet Trailing (ход >= +$6.50)
            elif move_profit >= self.trailing_act:
                if not self.trailing_active:
                    self.trailing_active = True
                    pos["trailing_active"] = True
                    self.broker._save_state()

                active_buffer = self.comp_buf if move_profit >= self.comp_thresh else self.wide_buf
                if move_profit >= self.comp_thresh and not self.compression_active:
                    self.compression_active = True
                    pos["compression_active"] = True
                    self.broker._save_state()

                ideal_sl = round(self.extreme_price - active_buffer, 2)
                if current_sl is not None and (ideal_sl - current_sl) >= self.step_quant and ideal_sl < curr_price:
                    steps = int((ideal_sl - current_sl) / self.step_quant)
                    new_sl = round(current_sl + (steps * self.step_quant), 2)
                    self.broker.modify_stop_loss(self.strategy_id, new_sl)

        elif side == "SHORT":
            curr_price = best_ask
            if curr_price < self.extreme_price:
                self.extreme_price = curr_price
                pos["best_price"] = curr_price
                self.broker._save_state()

            move_profit = entry_price - self.extreme_price

            # 1. Фаза раннего безубытка (ход от +$4.50 до +$6.50)
            if self.be_trigger <= move_profit < self.trailing_act:
                if not self.be_activated:
                    target_sl = round(entry_price - self.be_lock, 2)
                    if (current_sl is None or target_sl < current_sl) and target_sl > curr_price:
                        self.broker.modify_stop_loss(self.strategy_id, target_sl)
                        self.be_activated = True
                        pos["be_activated"] = True
                        self.broker._save_state()

            # 2. Фаза Ratchet Trailing (ход >= +$6.50)
            elif move_profit >= self.trailing_act:
                if not self.trailing_active:
                    self.trailing_active = True
                    pos["trailing_active"] = True
                    self.broker._save_state()

                active_buffer = self.comp_buf if move_profit >= self.comp_thresh else self.wide_buf
                if move_profit >= self.comp_thresh and not self.compression_active:
                    self.compression_active = True
                    pos["compression_active"] = True
                    self.broker._save_state()

                ideal_sl = round(self.extreme_price + active_buffer, 2)
                if current_sl is not None and (current_sl - ideal_sl) >= self.step_quant and ideal_sl > curr_price:
                    steps = int((current_sl - ideal_sl) / self.step_quant)
                    new_sl = round(current_sl - (steps * self.step_quant), 2)
                    self.broker.modify_stop_loss(self.strategy_id, new_sl)
