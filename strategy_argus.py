"""
strategy_argus.py - Стратегия В: Argus BTC (LightGBM ML + Robust Filters)
Математический ML-движок без внешних сервисов (Zero-Ollama).
25 относительных признаков, Hour-Open Anti-Counter Filter, SMM Guard, Adaptive Time-Stop.
"""

import os
import time
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
import numpy as np
import pandas as pd
import lightgbm as lgb

from hour_filter import HourOpenFilter, VetoReason
from smm_guard import SMMGuard
from adaptive_time_stop import AdaptiveTimeStopEngine, GracePeriodState, TimeStopAction, TimeStopDecision


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


FEATURE_NAMES = [
    'body_pct', 'upper_wick_pct', 'lower_wick_pct', 'candle_range_pct',
    'atr_pct',
    'return', 'abs_return', 'return_lag1', 'return_lag2', 'return_lag3',
    'dbs', 'sma7_ratio', 'sma7_to_21', 'sma12_slope',
    'rsi_14',
    'vol_ratio', 'vol_z', 'ret_z',
    'whale_score', 'fr_score', 'oi_intensity',
    'delta_e', 'smoothed_energy', 'energy_lag1', 'energy_lag2',
]


class ArgusBTCStrategy:
    def __init__(self, config: dict, broker):
        self.config = config.get("strategy_argus", {})
        self.broker = broker
        self.strategy_id = "ARGUS"
        self.symbol = self.config.get("symbol", "BTCUSDT").upper()
        self.pos_size = float(self.config.get("position_size_btc", config.get("position_size_btc", 0.002)))

        # Model configuration
        model_path = self.config.get("model_path", r"d:\Telegram bot for Jarvis\www.binance.com\Argus BTC\lgb_model.txt")
        if not os.path.isabs(model_path):
            model_path = os.path.abspath(model_path)

        if not os.path.exists(model_path):
            # Fallback path lookup
            alt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Argus BTC", "lgb_model.txt")
            if os.path.exists(alt_path):
                model_path = alt_path

        self.model_path = model_path
        print(f"[ARGUS INIT] Loading LightGBM model from {self.model_path}...")
        self.model = lgb.Booster(model_file=self.model_path)
        print(f"[ARGUS INIT] Model loaded successfully. Num features: {self.model.num_feature()}")

        # Strategy thresholds
        self.prob_threshold = float(self.config.get("prob_threshold", 0.55))
        self.atr_period = int(self.config.get("atr_period", 14))
        self.max_atr = float(self.config.get("max_atr", 320.0))
        self.obi_min = float(self.config.get("obi_min", -0.60))
        self.obi_max = float(self.config.get("obi_max", 0.90))
        self.sl_atr_mult = float(self.config.get("sl_atr_multiplier", 1.5))
        self.tp_atr_mult = float(self.config.get("tp_atr_multiplier", 2.5))
        self.hour_delta_threshold = float(self.config.get("hour_delta_threshold", 0.0015))

        # Risk and execution guards
        self.hour_filter = HourOpenFilter(
            threshold_delta_pct=self.hour_delta_threshold,
            funding_rz_critical=-2.5,
            ml_prob_threshold=0.70,
            symbol=self.symbol
        )
        self.smm_guard = SMMGuard(
            window_seconds=float(self.config.get("smm_window_sec", 10.0)),
            toxic_threshold_bps=float(self.config.get("smm_toxic_bps", 4.0))
        )
        self.time_stop_engine = AdaptiveTimeStopEngine(
            base_timeout_ms=int(self.config.get("base_timeout_min", 90)) * 60 * 1000,
            max_grace_period_ms=int(self.config.get("max_grace_min", 45)) * 60 * 1000
        )

        # Candle buffers
        self.candles_15m: List[Candle] = []
        self.open_price_1h: float = 0.0

        # State tracking
        self.grace_state = GracePeriodState()
        self.entry_time_ms: int = 0
        self.current_atr: float = 50.0  # default fallback for BTC
        self.current_obi: float = 0.0
        self.current_smm_bps: float = 0.0
        self.last_lgb_prob: float = 50.0
        self.last_lgb_signal: str = "NEUTRAL"
        self.last_filter_msg: str = "WAITING_DATA"

        self._sync_position_state()

    def _sync_position_state(self):
        """Синхронизация времени входа и стейта Grace Period с сохраненной позицией брокера."""
        pos = self.broker.get_position(self.strategy_id)
        if pos is not None:
            if self.entry_time_ms <= 0:
                if pos.get("entry_time_ms"):
                    self.entry_time_ms = int(pos["entry_time_ms"])
                elif pos.get("entry_time"):
                    try:
                        from datetime import datetime, timezone
                        dt = datetime.fromisoformat(pos["entry_time"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        self.entry_time_ms = int(dt.timestamp() * 1000)
                    except Exception:
                        self.entry_time_ms = int(time.time() * 1000)
            if pos.get("in_grace", False) and not self.grace_state.is_in_grace:
                self.grace_state.is_in_grace = True
                self.grace_state.grace_start_ms = pos.get("grace_start_ms", int(time.time() * 1000))
                self.grace_state.adverse_extremum = pos.get("adverse_extremum", pos["entry_price"])
                self.grace_state.breakeven_sl = pos.get("sl", pos["entry_price"])
        else:
            self.entry_time_ms = 0
            self.grace_state = GracePeriodState()

    def update_hourly_open(self, open_p: float):
        self.open_price_1h = round(open_p, 2)
        self.hour_filter.update_open_price(self.open_price_1h)

    def on_kline_1h(self, candle: Candle):
        self.open_price_1h = round(candle.open, 2)
        self.hour_filter.update_open_price(self.open_price_1h)

    def check_hour_drift(self, current_price: float = 0.0) -> None:
        """Проверка и обновление базовой цены часа при дрейфе (> 65 минут)."""
        now = time.time()
        if self.hour_filter.last_update_time > 0 and (now - self.hour_filter.last_update_time) > self.hour_filter.max_drift_seconds:
            new_open = self.hour_filter.refresh_open_price_from_api(self.symbol)
            if new_open and new_open > 0:
                self.open_price_1h = new_open
            elif current_price > 0:
                self.hour_filter.update_open_price(current_price, now)
                self.open_price_1h = current_price

    def evaluate_preloaded_data(self):
        """Первичный расчет признаков и вероятности ML на предзагруженной истории свечей."""
        if len(self.candles_15m) >= 60:
            features = self.calculate_features()
            if features is not None:
                prob_up = float(self.model.predict(features)[0])
                self.last_lgb_prob = round(prob_up * 100, 1)
                if prob_up >= self.prob_threshold:
                    self.last_lgb_signal = f"LONG ({prob_up*100:.1f}%)"
                    self.last_filter_msg = "FILTERS_READY"
                elif prob_up <= (1.0 - self.prob_threshold):
                    self.last_lgb_signal = f"SHORT ({(1.0 - prob_up)*100:.1f}%)"
                    self.last_filter_msg = "FILTERS_READY"
                else:
                    self.last_lgb_signal = f"NEUTRAL ({self.last_lgb_prob}%)"
                    self.last_filter_msg = f"PROB_UNDER_THRESHOLD ({self.last_lgb_prob}% vs {self.prob_threshold*100:.0f}%)"

    def calculate_features(self) -> Optional[np.ndarray]:
        """Расчет 25 нормализованных относительных признаков по каноническому правилу."""
        if len(self.candles_15m) < 60:
            return None

        # Собираем данные в DataFrame
        df = pd.DataFrame([{
            'open': c.open,
            'high': c.high,
            'low': c.low,
            'close': c.close,
            'volume': c.volume
        } for c in self.candles_15m])

        df['body_pct']       = (df['close'] - df['open']) / df['open'] * 100
        df['upper_wick_pct'] = (df['high'] - df[['open', 'close']].max(axis=1)) / df['open'] * 100
        df['lower_wick_pct'] = (df[['open', 'close']].min(axis=1) - df['low']) / df['open'] * 100
        df['candle_range_pct'] = (df['high'] - df['low']) / df['open'] * 100

        df['prev_close'] = df['close'].shift(1)
        df['tr'] = df[['high', 'prev_close']].max(axis=1) - df[['low', 'prev_close']].min(axis=1)
        atr_series = df['tr'].rolling(self.atr_period).mean()
        df['atr_pct'] = (atr_series / df['close']) * 100

        # Обновляем текущий ATR
        latest_atr = atr_series.iloc[-1]
        if not pd.isna(latest_atr) and latest_atr > 0.0:
            self.current_atr = round(float(latest_atr), 2)

        df['return']     = df['close'].pct_change() * 100
        df['abs_return'] = df['return'].abs()

        df['return_lag1'] = df['return'].shift(1)
        df['return_lag2'] = df['return'].shift(2)
        df['return_lag3'] = df['return'].shift(3)

        df['sma_7']  = df['close'].rolling(7).mean()
        df['sma_12'] = df['close'].rolling(12).mean()
        df['sma_21'] = df['close'].rolling(21).mean()

        df['dbs']         = df['close'] / df['sma_12']
        df['sma7_ratio']  = df['close'] / df['sma_7']
        df['sma7_to_21']  = df['sma_7'] / df['sma_21']
        df['sma12_slope'] = (df['sma_12'] - df['sma_12'].shift(5)) / df['sma_12'].shift(5) * 100

        delta = df['close'].diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / (loss + 1e-8)
        df['rsi_14'] = 100 - (100 / (1 + rs))

        df['vol_ratio'] = df['volume'] / (df['volume'].rolling(20).mean() + 1e-8)

        window = 30
        rolling_median_vol = df['volume'].rolling(window).median()
        mad_vol = (df['volume'] - rolling_median_vol).abs().rolling(window).median()
        df['vol_z'] = (df['volume'] - rolling_median_vol) / (1.4826 * mad_vol + 1e-8)

        rolling_median_ret = df['return'].rolling(window).median()
        mad_ret = (df['return'] - rolling_median_ret).abs().rolling(window).median()
        df['ret_z'] = (df['return'] - rolling_median_ret) / (1.4826 * mad_ret + 1e-8)

        df['whale_score']    = df['vol_z'] + df['ret_z'].abs()
        df['fr_score']       = df['ret_z'] * df['vol_z']
        df['oi_intensity']   = (df['vol_z'] * df['ret_z']).abs()

        df['energy']         = df['abs_return'] * df['volume']
        df['energy_prev']    = df['energy'].shift(1)
        df['delta_e']        = np.where(df['energy_prev'] > 0, (df['energy'] - df['energy_prev']) / df['energy_prev'] * 100, 0)
        df['smoothed_energy'] = df['delta_e'].rolling(4).mean()

        df['energy_lag1'] = df['smoothed_energy'].shift(1)
        df['energy_lag2'] = df['smoothed_energy'].shift(2)

        last_features = df[FEATURE_NAMES].iloc[-1:].to_numpy()
        if np.isnan(last_features).any():
            return None
        return last_features

    def on_depth_tick(self, best_bid: float, best_ask: float, depth_data: Optional[dict] = None) -> Optional[Tuple[str, Any]]:
        """Обработка тика стакана: ведение SMM, OBI и Chandelier trailing stop."""
        mid = round((best_bid + best_ask) / 2.0, 2)
        self.smm_guard.record_tick(self.symbol, mid)

        self._sync_position_state()
        pos = self.broker.get_position(self.strategy_id)
        side_for_smm = pos["direction"] if pos else "LONG"
        self.current_smm_bps = self.smm_guard.get_current_smm(self.symbol, side_for_smm, mid)

        if depth_data:
            bids = depth_data.get("b", [])
            asks = depth_data.get("a", [])
            if bids and asks:
                b_vol = sum(float(b[1]) for b in bids)
                a_vol = sum(float(a[1]) for a in asks)
                if b_vol + a_vol > 0:
                    self.current_obi = round((b_vol - a_vol) / (b_vol + a_vol), 4)

        if pos is None:
            return None

        direction = pos["direction"]
        cur_p = best_bid if direction == "LONG" else best_ask

        # 1. Отслеживание локального экстремума (пика)
        best_p = pos.get("best_price", pos["entry_price"])
        if direction == "LONG":
            if cur_p > best_p:
                pos["best_price"] = cur_p
                best_p = cur_p
        else:
            if cur_p < best_p:
                pos["best_price"] = cur_p
                best_p = cur_p

        # 2. Chandelier Trailing Stop (при профите >= 1.0 * ATR)
        profit = (best_p - pos["entry_price"]) if direction == "LONG" else (pos["entry_price"] - best_p)
        if profit >= 1.0 * self.current_atr:
            pos["trailing_active"] = True
            if direction == "LONG":
                new_sl = round(best_p - self.sl_atr_mult * self.current_atr, 2)
                if pos["sl"] is None or new_sl > pos["sl"]:
                    self.broker.modify_stop_loss(self.strategy_id, new_sl)
            else:
                new_sl = round(best_p + self.sl_atr_mult * self.current_atr, 2)
                if pos["sl"] is None or new_sl < pos["sl"]:
                    self.broker.modify_stop_loss(self.strategy_id, new_sl)

        # 3. Проверка Broken Pullback во время активного Grace Period
        if self.grace_state.is_in_grace:
            c_open = self.candles_15m[-1].open if self.candles_15m else pos["entry_price"]
            decision = self.time_stop_engine.evaluate(
                side=direction,
                entry_price=pos["entry_price"],
                current_price=cur_p,
                current_sl=pos["sl"],
                open_time_ms=self.entry_time_ms,
                current_time_ms=int(time.time() * 1000),
                candle_15m_open=c_open,
                candle_15m_close=cur_p,
                grace_state=self.grace_state
            )
            if decision.action in (TimeStopAction.EXIT_BROKEN_PULLBACK, TimeStopAction.EXIT_TIME_STOP):
                res = self.broker.close_position(self.strategy_id, cur_p, reason=decision.action.value, is_maker=False)
                self.grace_state = GracePeriodState()
                self.entry_time_ms = 0
                return ("TIME_STOP_EXIT", res)
            elif decision.action == TimeStopAction.UPDATE_TRAILING_IN_GRACE:
                if pos["sl"] is None or (pos["direction"] == "LONG" and decision.new_sl > pos["sl"]) or (pos["direction"] == "SHORT" and decision.new_sl < pos["sl"]):
                    self.broker.modify_stop_loss(self.strategy_id, decision.new_sl)

        return None

    def on_kline_15m(self, candle: Candle) -> Optional[Tuple[str, Any]]:
        """Обработка 15-минутной свечи BTC."""
        if not candle.is_closed:
            return None

        self.candles_15m.append(candle)
        if len(self.candles_15m) > 150:
            self.candles_15m = self.candles_15m[-120:]

        self._sync_position_state()
        pos = self.broker.get_position(self.strategy_id)

        # 1. Если позиция открыта — проверяем логику Adaptive Time-Stop на закрытии 15m
        if pos is not None:
            current_time_ms = int(time.time() * 1000)
            decision = self.time_stop_engine.evaluate(
                side=pos["direction"],
                entry_price=pos["entry_price"],
                current_price=candle.close,
                current_sl=pos["sl"],
                open_time_ms=self.entry_time_ms,
                current_time_ms=current_time_ms,
                candle_15m_open=candle.open,
                candle_15m_close=candle.close,
                grace_state=self.grace_state
            )

            if decision.action in (TimeStopAction.EXIT_TIME_STOP, TimeStopAction.EXIT_BROKEN_PULLBACK):
                res = self.broker.close_position(self.strategy_id, candle.close, reason=decision.action.value, is_maker=False)
                self.grace_state = GracePeriodState()
                self.entry_time_ms = 0
                return ("TIME_STOP_CLOSE", res)

            elif decision.action == TimeStopAction.ENTER_GRACE_PERIOD:
                self.broker.modify_stop_loss(self.strategy_id, decision.new_sl)
                self.last_filter_msg = f"GRACE_ACTIVATED: SL->{decision.new_sl}"
                pos["in_grace"] = True
                pos["grace_start_ms"] = self.grace_state.grace_start_ms
                pos["adverse_extremum"] = self.grace_state.adverse_extremum
                self.broker._save_state()
                return ("GRACE_ENTERED", decision)

            elif decision.action == TimeStopAction.UPDATE_TRAILING_IN_GRACE:
                if pos["sl"] is None or (pos["direction"] == "LONG" and decision.new_sl > pos["sl"]) or (pos["direction"] == "SHORT" and decision.new_sl < pos["sl"]):
                    self.broker.modify_stop_loss(self.strategy_id, decision.new_sl)
                return None

            return None

        # 2. Если позиция закрыта — оцениваем сигналы входа на базе LightGBM
        features = self.calculate_features()
        if features is None:
            self.last_filter_msg = f"COLLECTING_HISTORY ({len(self.candles_15m)}/60)"
            return None

        prob_up = float(self.model.predict(features)[0])
        self.last_lgb_prob = round(prob_up * 100, 1)

        signal_side = None
        prob_signal = 0.0
        if prob_up >= self.prob_threshold:
            signal_side = "LONG"
            prob_signal = prob_up
        elif prob_up <= (1.0 - self.prob_threshold):
            signal_side = "SHORT"
            prob_signal = 1.0 - prob_up

        if signal_side is None:
            self.last_lgb_signal = f"NEUTRAL ({self.last_lgb_prob}%)"
            self.last_filter_msg = f"PROB_UNDER_THRESHOLD ({self.last_lgb_prob}% vs {self.prob_threshold*100:.0f}%)"
            return None

        self.last_lgb_signal = f"{signal_side} ({prob_signal*100:.1f}%)"

        # 3. Фильтры пре-трейда
        # 3.1 CVaR ATR Filter (защита от аномальной волатильности)
        if self.current_atr > self.max_atr:
            self.last_filter_msg = f"CVaR_VETO_ATR ({self.current_atr:.1f} > {self.max_atr})"
            return None

        # 3.2 CVaR OBI Filter (защита от токсичного стакана)
        if not (self.obi_min <= self.current_obi <= self.obi_max):
            self.last_filter_msg = f"CVaR_VETO_OBI ({self.current_obi:+.2f} not in [{self.obi_min}, {self.obi_max}])"
            return None

        # 3.3 Hour-Open Anti-Counter Filter (Fail-Closed)
        hour_eval = self.hour_filter.evaluate(
            side=signal_side,
            current_price=candle.close,
            open_price_1h=self.open_price_1h,
            funding_rz=0.0,
            ml_prob_long=prob_up
        )
        self.open_price_1h = self.hour_filter.open_price_1h
        if not hour_eval.is_allowed:
            self.last_filter_msg = f"HOUR_VETO ({hour_eval.veto_reason.value})"
            return None

        # 3.4 SMM Guard (Signed Midpoint Markout)
        smm_eval = self.smm_guard.evaluate_order(
            symbol=self.symbol,
            side=signal_side,
            current_midpoint=candle.close
        )
        self.current_smm_bps = smm_eval.smm_10s_bps
        if smm_eval.is_toxic:
            self.last_filter_msg = f"SMM_VETO ({smm_eval.reason})"
            return None

        self.last_filter_msg = "FILTERS_PASSED"

        # 4. Динамический сайзинг по уверенности
        size_mult = 1.0
        if prob_signal >= 0.88:
            size_mult = 2.0
        elif prob_signal >= 0.78:
            size_mult = 1.5
        final_size = round(self.pos_size * size_mult, 4)

        # 5. Расчет начальных SL и TP
        entry_price = candle.close
        if signal_side == "LONG":
            sl = round(entry_price - self.sl_atr_mult * self.current_atr, 2)
            tp = round(entry_price + self.tp_atr_mult * self.current_atr, 2)
        else:
            sl = round(entry_price + self.sl_atr_mult * self.current_atr, 2)
            tp = round(entry_price - self.tp_atr_mult * self.current_atr, 2)

        notes = f"Argus ML {prob_signal*100:.1f}% | ATR {self.current_atr:.1f} | OBI {self.current_obi:+.2f} | {size_mult:.1f}x"
        pos = self.broker.open_position(
            strategy=self.strategy_id,
            direction=signal_side,
            size=final_size,
            price=entry_price,
            is_maker=False,
            sl=sl,
            tp=tp,
            notes=notes,
            symbol=self.symbol
        )
        self.entry_time_ms = int(time.time() * 1000)
        self.grace_state = GracePeriodState()
        return ("TRADE_OPEN", pos)
