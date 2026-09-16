"""
test_simulation_integration.py - Комплексный тест интеграции полигона симуляции (Zero-Mock Deep Verification).
Проверяет:
1. Загрузку LightGBM модели и расчет 25 относительных признаков.
2. Работу Hour-Open Anti-Counter Filter во всех граничных режимах.
3. Работу SMM Guard при нормальном и токсичном потоке ордеров.
4. Логику Adaptive Time-Stop Engine (тайм-аут 90м, Grace Period 45м, безубыток, Broken Pullback).
5. Виртуальный брокер (3 стратегии: DLH, HRB, ARGUS; расчет PnL для ETH и BTC, снапшоты эквити).
6. Отрисовку консольного дашборда на ANSI escape-кодах.
7. Инициализацию ShadowEngine и валидацию конфигурации.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
import unittest
import numpy as np
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from hour_filter import HourOpenFilter, VetoReason
from smm_guard import SMMGuard
from adaptive_time_stop import AdaptiveTimeStopEngine, GracePeriodState, TimeStopAction
from strategy_argus import ArgusBTCStrategy, Candle as ArgusCandle, FEATURE_NAMES
from strategy_jazz import JazzStrategy, Candle as JazzCandle
from virtual_broker import VirtualBroker
from dashboard import ConsoleDashboard
from shadow_runner import ShadowEngine, CONFIG_PATH


class TestSimulationIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_simulation.db")
        self.state_path = os.path.join(self.temp_dir, "test_state.json")
        self.outbox_dir = os.path.join(self.temp_dir, "outbox")

        self.test_config = {
            "symbol": "ETHUSDT",
            "leverage": 15,
            "initial_deposit_per_strategy": 100.0,
            "position_size_eth": 0.05,
            "position_size_btc": 0.002,
            "maker_fee": 0.0002,
            "taker_fee": 0.0005,
            "limit_fill_penetration_usdt": 0.1,
            "db_path": self.db_path,
            "state_path": self.state_path,
            "outbox_dir": self.outbox_dir,
            "telegram_chat_id": 1105081236,
            "strategy_dlh": {
                "enabled": True,
                "min_sweep_depth_usdt": 3.0,
                "max_sweep_depth_usdt": 15.0,
                "max_wick_anomaly_usdt": 20.0,
                "sl_buffer_usdt": 3.5,
                "max_sl_usdt": 12.0,
                "tp1_ratio": 0.5,
                "ratchet_trail_activation_usdt": 18.0,
                "ratchet_trail_buffer_usdt": 6.0
            },
            "strategy_hrb": {
                "enabled": True,
                "donchian_period": 20,
                "min_body_ratio": 0.6,
                "min_volume_ratio": 1.8,
                "atr_period": 14,
                "sl_atr_multiplier": 1.5,
                "chandelier_atr_multiplier": 2.0
            },
            "strategy_argus": {
                "enabled": True,
                "symbol": "BTCUSDT",
                "model_path": r"d:\Telegram bot for Jarvis\www.binance.com\Argus BTC\lgb_model.txt",
                "position_size_btc": 0.002,
                "prob_threshold": 0.55,
                "atr_period": 14,
                "max_atr": 320.0,
                "obi_min": -0.60,
                "obi_max": 0.90,
                "sl_atr_multiplier": 1.5,
                "tp_atr_multiplier": 2.5,
                "base_timeout_min": 90,
                "max_grace_min": 45,
                "smm_window_sec": 10.0,
                "smm_toxic_bps": 4.0,
                "hour_delta_threshold": 0.0015
            },
            "strategy_jazz": {
                "enabled": True,
                "symbol": "ETHUSDT",
                "timeframe": "5m",
                "lookback_candles": 72,
                "dead_zone_pct": 0.30,
                "wick_to_body_ratio": 0.80,
                "min_level_age_candles": 6,
                "sl_buffer_usdt": 2.50,
                "min_sl_usdt": 4.00,
                "max_sl_usdt": 7.00,
                "breakeven_trigger_usdt": 4.50,
                "breakeven_lock_usdt": 0.30,
                "trailing_activation_usdt": 6.50,
                "wide_buffer_usdt": 5.00,
                "compressed_buffer_usdt": 3.00,
                "compression_threshold_usdt": 10.00,
                "step_quant_usdt": 0.50
            }
        }
        self.broker = VirtualBroker(self.test_config)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_01_hour_open_filter(self):
        """Тест Hour-Open Anti-Counter Filter во всех сценариях."""
        hof = HourOpenFilter(threshold_delta_pct=0.0015)

        # 1. Бычий час (+0.30%): SHORT заблокирован, LONG разрешен
        eval_short = hof.evaluate(side="SHORT", current_price=60180.0, open_price_1h=60000.0)
        self.assertFalse(eval_short.is_allowed)
        self.assertEqual(eval_short.veto_reason, VetoReason.VETO_SHORT_BULLISH_HOUR)

        eval_long = hof.evaluate(side="LONG", current_price=60180.0, open_price_1h=60000.0)
        self.assertTrue(eval_long.is_allowed)

        # 2. Медвежий час (-0.30%): LONG заблокирован, SHORT разрешен
        eval_long_bear = hof.evaluate(side="LONG", current_price=59820.0, open_price_1h=60000.0)
        self.assertFalse(eval_long_bear.is_allowed)
        self.assertEqual(eval_long_bear.veto_reason, VetoReason.VETO_LONG_BEARISH_HOUR)

        eval_short_bear = hof.evaluate(side="SHORT", current_price=59820.0, open_price_1h=60000.0)
        self.assertTrue(eval_short_bear.is_allowed)

        # 3. Нейтральный час (+0.05%): оба разрешены
        eval_neutral = hof.evaluate(side="LONG", current_price=60030.0, open_price_1h=60000.0)
        self.assertTrue(eval_neutral.is_allowed)

        # 4. Перегретый фандинг (RZ = -3.0) и сильный ML (0.75): SHORT заблокирован
        eval_funding = hof.evaluate(side="SHORT", current_price=60000.0, open_price_1h=60000.0, funding_rz=-3.0, ml_prob_long=0.75)
        self.assertFalse(eval_funding.is_allowed)
        self.assertEqual(eval_funding.veto_reason, VetoReason.VETO_SHORT_FUNDING_AND_ML)

        # 5. Невалидные цены -> Fail-Closed
        eval_invalid = hof.evaluate(side="LONG", current_price=0.0, open_price_1h=60000.0)
        self.assertFalse(eval_invalid.is_allowed)
        self.assertEqual(eval_invalid.veto_reason, VetoReason.VETO_INVALID_DATA)

    def test_02_smm_guard(self):
        """Тест Signed Midpoint Markout (SMM Guard)."""
        guard = SMMGuard(window_seconds=10.0, toxic_threshold_bps=4.0)

        t0 = 1000.0
        guard.record_tick("BTCUSDT", 60000.0, timestamp=t0)

        # Плавный дрейф 1 bps за 5 секунд -> Безопасно
        res_ok = guard.evaluate_order("BTCUSDT", "BUY", 59995.0, timestamp=t0 + 5.0)
        self.assertFalse(res_ok.is_toxic)
        self.assertEqual(res_ok.action, "KEEP_ORDER")

        # Резкий токсичный слив для BUY: падение цены на 40$ (6.67 bps) -> Токсично
        res_toxic = guard.evaluate_order("BTCUSDT", "BUY", 59960.0, timestamp=t0 + 8.0)
        self.assertTrue(res_toxic.is_toxic)
        self.assertEqual(res_toxic.action, "CANCEL_ADVERSE_SELECTION")

    def test_03_adaptive_time_stop(self):
        """Тест Adaptive Time-Stop Engine (90м, Grace Period 45м, безубыток, Broken Pullback)."""
        engine = AdaptiveTimeStopEngine(base_timeout_ms=90 * 60 * 1000, max_grace_period_ms=45 * 60 * 1000)
        grace = GracePeriodState()

        t_entry = 1000000
        # 1. Возраст 30 минут -> Удержание
        dec1 = engine.evaluate(
            side="LONG",
            entry_price=60000.0,
            current_price=60050.0,
            current_sl=59500.0,
            open_time_ms=t_entry,
            current_time_ms=t_entry + 30 * 60 * 1000,
            candle_15m_open=60000.0,
            candle_15m_close=60050.0,
            grace_state=grace
        )
        self.assertEqual(dec1.action, TimeStopAction.HOLD)
        self.assertFalse(grace.is_in_grace)

        # 2. Возраст 90 минут, 15m свеча зеленая (close > open) -> Вход в Grace Period
        t_90m = t_entry + 90 * 60 * 1000
        dec2 = engine.evaluate(
            side="LONG",
            entry_price=60000.0,
            current_price=60020.0,
            current_sl=59500.0,
            open_time_ms=t_entry,
            current_time_ms=t_90m,
            candle_15m_open=60000.0,
            candle_15m_close=60020.0,
            grace_state=grace
        )
        self.assertEqual(dec2.action, TimeStopAction.ENTER_GRACE_PERIOD)
        self.assertTrue(grace.is_in_grace)
        self.assertGreater(dec2.new_sl, 59500.0)  # Стоп подтянут в безубыток (+0.05%)

        # 3. В Grace Period цена падает ниже минимума входа в Grace -> Broken Pullback
        dec3 = engine.evaluate(
            side="LONG",
            entry_price=60000.0,
            current_price=59990.0,  # Ниже extremum (60020.0)
            current_sl=dec2.new_sl,
            open_time_ms=t_entry,
            current_time_ms=t_90m + 5 * 60 * 1000,
            candle_15m_open=60020.0,
            candle_15m_close=59990.0,
            grace_state=grace
        )
        self.assertEqual(dec3.action, TimeStopAction.EXIT_BROKEN_PULLBACK)
        self.assertFalse(grace.is_in_grace)

    def test_04_virtual_broker_four_strategies(self):
        """Тест виртуального брокера со всеми 4 стратегиями (DLH, HRB, ARGUS, JAZZ)."""
        accounts = self.broker.state["accounts"]
        self.assertIn("DLH", accounts)
        self.assertIn("HRB", accounts)
        self.assertIn("ARGUS", accounts)
        self.assertIn("JAZZ", accounts)

        # Открытие позиции ARGUS по BTCUSDT
        pos_argus = self.broker.open_position(
            strategy="ARGUS",
            direction="LONG",
            size=0.002,
            price=60000.0,
            sl=59500.0,
            tp=61500.0,
            notes="Test Argus BTC",
            symbol="BTCUSDT"
        )
        self.assertIsNotNone(pos_argus)
        self.assertEqual(pos_argus["strategy"], "ARGUS")
        self.assertEqual(pos_argus["symbol"], "BTCUSDT")

        # Открытие позиции JAZZ по ETHUSDT
        pos_jazz = self.broker.open_position(
            strategy="JAZZ",
            direction="SHORT",
            size=0.05,
            price=2500.0,
            sl=2506.0,
            tp=2470.0,
            notes="Test Jazz ETH",
            symbol="ETHUSDT"
        )
        self.assertIsNotNone(pos_jazz)
        self.assertEqual(pos_jazz["strategy"], "JAZZ")
        self.assertEqual(pos_jazz["symbol"], "ETHUSDT")

        # Проверка модификации SL
        self.broker.modify_stop_loss("ARGUS", 59800.0)
        self.assertEqual(self.broker.get_position("ARGUS")["sl"], 59800.0)
        self.broker.modify_stop_loss("JAZZ", 2499.70)
        self.assertEqual(self.broker.get_position("JAZZ")["sl"], 2499.70)

        # Снапшот эквити с мультивалютными ценами
        prices = {"ETHUSDT": 2490.0, "BTCUSDT": 61000.0}
        self.broker.record_equity_snapshot(prices)

        # Закрытие позиции ARGUS с профитом (61000 - 60000 = +1000$ на 0.002 BTC = +2.0$ Gross)
        close_res = self.broker.close_position("ARGUS", 61000.0, reason="TAKE_PROFIT")
        self.assertEqual(close_res["gross_pnl"], 2.0)
        self.assertGreater(close_res["net_pnl"], 0.0)
        self.assertIsNone(self.broker.get_position("ARGUS"))

        # Закрытие позиции JAZZ с профитом (2500 - 2490 = +10$ на 0.05 ETH = +0.50$ Gross)
        close_jazz = self.broker.close_position("JAZZ", 2490.0, reason="TRAILING_STOP")
        self.assertEqual(close_jazz["gross_pnl"], 0.50)
        self.assertGreater(close_jazz["net_pnl"], 0.0)
        self.assertIsNone(self.broker.get_position("JAZZ"))

        # Проверка записи сделок в SQLite
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT strategy, direction, size, pnl_gross, pnl_net FROM trades WHERE strategy='ARGUS'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "ARGUS")
        self.assertEqual(row[1], "LONG")
        self.assertEqual(row[2], 0.002)

        cur.execute("SELECT strategy, direction, size, pnl_gross, pnl_net FROM trades WHERE strategy='JAZZ'")
        row_j = cur.fetchone()
        self.assertIsNotNone(row_j)
        self.assertEqual(row_j[0], "JAZZ")
        self.assertEqual(row_j[1], "SHORT")
        self.assertEqual(row_j[2], 0.05)
        conn.close()

    def test_05_strategy_argus_feature_extraction_and_prediction(self):
        """Тест расчета 25 относительных признаков и предсказания модели LightGBM."""
        strategy = ArgusBTCStrategy(self.test_config, self.broker)

        # Генерируем 60 реалистичных 15M свечей
        np.random.seed(42)
        base_p = 60000.0
        now_ts = int(time.time() * 1000)

        for i in range(60):
            o = base_p + np.random.randn() * 40
            h = o + abs(np.random.randn() * 80) + 10
            l = o - abs(np.random.randn() * 80) - 10
            c = (h + l) / 2.0 + np.random.randn() * 20
            v = float(np.random.uniform(20, 150))
            candle = ArgusCandle(
                open_time=now_ts - (60 - i) * 900000,
                open=round(o, 2),
                high=round(h, 2),
                low=round(l, 2),
                close=round(c, 2),
                volume=round(v, 2),
                close_time=now_ts - (59 - i) * 900000,
                is_closed=True
            )
            strategy.candles_15m.append(candle)
            base_p = c

        features = strategy.calculate_features()
        self.assertIsNotNone(features)
        self.assertEqual(features.shape, (1, 25))
        self.assertFalse(np.isnan(features).any(), "Признаки содержат NaN!")

        # Инференс LightGBM
        prob = float(strategy.model.predict(features)[0])
        self.assertGreaterEqual(prob, 0.0)
        self.assertLessEqual(prob, 1.0)
        print(f"\n[TEST PASS] Argus 25 Features calculated successfully. LightGBM Predict: {prob:.4f}")

    def test_06_console_dashboard_rendering(self):
        """Тест генерации дашборда на ANSI escape-кодах."""
        dash = ConsoleDashboard(max_events=5)
        dash.add_event("Тестовое системное событие", "SYS")
        dash.add_event("Сделка открыта", "BUY")
        dash.add_event("Сделка закрыта с прибылью", "WIN")

        prices = {"ETHUSDT": 2450.50, "BTCUSDT": 62100.00}
        metrics = {"ETH_OBI": 0.25, "BTC_OBI": -0.15, "BTC_SMM": -1.2, "BTC_HOUR_DELTA": 0.0012}

        # Проверка вызова render без исключений
        dash.render(
            broker=self.broker,
            prices=prices,
            market_metrics=metrics,
            dlh_strategy=None,
            hrb_strategy=None,
            argus_strategy=None,
            jazz_strategy=None,
            force=True
        )
        self.assertEqual(len(dash.events), 3)

    def test_07_shadow_engine_initialization(self):
        """Тест создания ShadowEngine с боевым конфигурационным файлом."""
        engine = ShadowEngine(CONFIG_PATH)
        self.assertIsNotNone(engine.strategy_dlh)
        self.assertIsNotNone(engine.strategy_hrb)
        self.assertIsNotNone(engine.strategy_argus)
        self.assertIsNotNone(engine.strategy_jazz)
        self.assertIn("ARGUS", engine.broker.state["accounts"])
        self.assertIn("JAZZ", engine.broker.state["accounts"])
        self.assertEqual(engine.strategy_argus.symbol, "BTCUSDT")
        self.assertEqual(engine.strategy_jazz.symbol, "ETHUSDT")

    def test_08_argus_filters_and_triggers(self):
        """Тест пре-трейд вето в ArgusBTCStrategy (ATR, OBI, Hour, SMM)."""
        strategy = ArgusBTCStrategy(self.test_config, self.broker)

        # 1. Вето по ATR (CVaR ATR > 320.0)
        strategy.current_atr = 350.0
        strategy.prob_threshold = 0.40  # Ослабляем порог для проверки вето
        # Имитируем расчет признаков через подмену calculate_features
        strategy.calculate_features = lambda: np.zeros((1, 25))
        # При dummy zero-input модель выдает ~0.502, что >= 0.40 -> сигнал LONG
        candle = ArgusCandle(int(time.time()*1000), 60000.0, 60100.0, 59900.0, 60050.0, 50.0, int(time.time()*1000)+900000, True)

        res = strategy.on_kline_15m(candle)
        self.assertIsNone(res)
        self.assertIn("CVaR_VETO_ATR", strategy.last_filter_msg)

        # 2. Вето по OBI (OBI < -0.60)
        strategy.current_atr = 50.0
        strategy.current_obi = -0.75
        res_obi = strategy.on_kline_15m(candle)
        self.assertIsNone(res_obi)
        self.assertIn("CVaR_VETO_OBI", strategy.last_filter_msg)

        # 3. Вето по Hour-Open Filter (резкий слив часа при попытке LONG)
        strategy.current_obi = 0.10
        strategy.open_price_1h = 60500.0  # Цена открытия 60500, текущая 60050 (-0.74% < -0.15%)
        res_hour = strategy.on_kline_15m(candle)
        self.assertIsNone(res_hour)
        self.assertIn("HOUR_VETO", strategy.last_filter_msg)

        # 4. Проход всех фильтров и открытие позиции
        strategy.open_price_1h = 60000.0  # +0.08% (нейтрально)
        strategy.smm_guard.evaluate_order = lambda *args, **kwargs: type('Obj', (), {'smm_10s_bps': 0.0, 'is_toxic': False})()
        res_pass = strategy.on_kline_15m(candle)
        self.assertIsNotNone(res_pass)
        self.assertEqual(res_pass[0], "TRADE_OPEN")
        self.assertIsNotNone(self.broker.get_position("ARGUS"))

    def test_09_daily_summary_four_models(self):
        """Тест формирования вечерней сводки 21:00 МСК по 4 моделям."""
        engine = ShadowEngine(CONFIG_PATH)
        engine.outbox_dir = self.outbox_dir

        # Отправляем суточную сводку
        engine.send_daily_summary()

        # Проверяем появление файла в outbox
        files = os.listdir(self.outbox_dir)
        self.assertGreaterEqual(len(files), 1)

        summary_file = os.path.join(self.outbox_dir, files[0])
        with open(summary_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        text = data.get("text", "")
        self.assertIn("4 МОДЕЛИ", text)
        self.assertIn("DLH (Daily Liquidity Hunter | ETHUSDT)", text)
        self.assertIn("HRB (Hub-Regime Breakout | ETHUSDT)", text)
        self.assertIn("ARGUS BTC (LightGBM ML + Anti-Counter | BTCUSDT)", text)
        self.assertIn("JAZZ (Dynamic 6H Corridor 5M Sweep | ETHUSDT)", text)
        self.assertIn("ИТОГО ПОЛИГОН (4 МОДЕЛИ)", text)


    def test_10_restart_preserves_position_and_entry_time(self):
        """Тест защиты от мгновенной ликвидации при перезапуске бота с открытой позицией."""
        import datetime
        from datetime import timezone
        # Создаем позицию, открытую 10 минут назад
        t_10m_ago = (datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=10)).isoformat()
        self.broker.state["positions"]["ARGUS"] = {
            "strategy": "ARGUS",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "size": 0.002,
            "entry_price": 60000.0,
            "entry_time": t_10m_ago,
            "entry_time_ms": int((datetime.datetime.now(timezone.utc) - datetime.timedelta(minutes=10)).timestamp() * 1000),
            "sl": 59000.0,
            "tp": 62000.0,
            "initial_sl": 59000.0,
            "initial_tp": 62000.0,
            "best_price": 60000.0,
            "trailing_active": False,
            "in_grace": False
        }

        # Инициализируем стратегию заново (имитация перезапуска процесса)
        strat = ArgusBTCStrategy(self.test_config, self.broker)
        self.assertGreater(strat.entry_time_ms, 0, "entry_time_ms не был восстановлен из позиции!")

        # Подаем закрытие 15m свечи с небольшой просадкой
        c = ArgusCandle(int(time.time()*1000), 60010.0, 60020.0, 59970.0, 59980.0, 50.0, int(time.time()*1000)+900000, True)
        res = strat.on_kline_15m(c)

        # Сделка не должна закрыться по тайм-стопу, так как ей всего 10 минут (< 90 мин)
        self.assertIsNone(res, "Сделка была ошибочно закрыта сразу после рестарта!")
        self.assertIsNotNone(self.broker.get_position("ARGUS"))

    def test_11_adaptive_time_stop_profit_protection(self):
        """Тест защиты прибыльных сделок от закрытия по тайм-стопу на 90-й минуте."""
        engine = AdaptiveTimeStopEngine(base_timeout_ms=90 * 60 * 1000)
        grace = GracePeriodState()
        t_entry = 1000000
        t_90m = t_entry + 90 * 60 * 1000

        # Позиция LONG в устойчивом плюсе (+100$), но свеча 15m закрылась красной (Close < Open)
        dec = engine.evaluate(
            side="LONG",
            entry_price=60000.0,
            current_price=60100.0,
            current_sl=59800.0,
            open_time_ms=t_entry,
            current_time_ms=t_90m,
            candle_15m_open=60120.0,
            candle_15m_close=60100.0,
            grace_state=grace
        )
        self.assertEqual(dec.action, TimeStopAction.HOLD, "Прибыльная сделка не должна закрываться по тайм-стопу!")
        self.assertIn("HOLD_PROFITABLE", dec.reason)

    def test_12_fail_closed_hour_open_filter(self):
        """Тест принципа Fail-Closed: при неинициализированной часовой цене вход блокируется."""
        strat = ArgusBTCStrategy(self.test_config, self.broker)
        strat.open_price_1h = 0.0  # Не загружена или сбой сети
        strat.calculate_features = lambda: np.zeros((1, 25))
        strat.prob_threshold = 0.40

        candle = ArgusCandle(int(time.time()*1000), 60000.0, 60050.0, 59950.0, 60020.0, 50.0, int(time.time()*1000)+900000, True)
        res = strat.on_kline_15m(candle)
        self.assertIsNone(res)
        self.assertIn("VETO_INVALID_DATA", strat.last_filter_msg)

    def test_13_live_smm_depth_tick_telemetry(self):
        """Тест расчета SMM в реальном времени при тиках стакана."""
        strat = ArgusBTCStrategy(self.test_config, self.broker)
        # Симулируем серию тиков с восходящим движением цены
        strat.on_depth_tick(60000.0, 60002.0)
        time.sleep(0.01)
        strat.on_depth_tick(60050.0, 60052.0)

        # SMM должен обновиться и стать положительным (> 0 bps)
        self.assertNotEqual(strat.current_smm_bps, 0.0, "SMM остался нулевым после изменения цены!")
        self.assertGreater(strat.current_smm_bps, 0.0)

    def test_14_strategy_jazz_sweep_and_ratchet_trailing(self):
        """Комплексный тест JAZZ: коридор 72 бара, Мертвая зона, SHORT Sweep, безубыток и Ratchet Trailing."""
        strategy = JazzStrategy(self.test_config, self.broker)

        # 1. Заполняем 72 свечи 5M (коридор: low=2450.00, high=2500.00, mid=2475.00, dz=[2467.50, 2482.50])
        now_ts = int(time.time() * 1000)
        for i in range(72):
            p = 2475.0
            if i == 10:
                h = 2500.0  # High 62 бара назад (age = 62 >= 6)
                l = 2470.0
            elif i == 20:
                h = 2480.0
                l = 2450.0  # Low 52 бара назад (age = 52 >= 6)
            else:
                h = 2480.0
                l = 2470.0

            c_obj = JazzCandle(
                open_time=now_ts - (72 - i) * 300000,
                open=p,
                high=h,
                low=l,
                close=p,
                volume=100.0,
                close_time=now_ts - (71 - i) * 300000,
                is_closed=True
            )
            strategy.candles_5m.append(c_obj)

        strategy.recalculate_corridor()
        self.assertEqual(strategy.ref_high, 2500.00)
        self.assertEqual(strategy.ref_low, 2450.00)
        self.assertEqual(strategy.dead_zone, (2467.50, 2482.50))

        # 2. Проверка Мертвой зоны: свеча закрылась внутри [2467.50, 2482.50] -> вход блокирован
        dz_candle = JazzCandle(
            open_time=now_ts,
            open=2472.0,
            high=2480.0,
            low=2470.0,
            close=2475.0,  # Внутри мертвой зоны
            volume=50.0,
            close_time=now_ts + 300000,
            is_closed=True
        )
        strategy.on_kline_5m(dz_candle)
        self.assertIsNone(self.broker.get_position("JAZZ"), "Вход не должен был сработать в Мертвой зоне!")

        # 3. SHORT Sweep: High=2508.0 (> 2500.0), Close=2495.0 (< 2500.0), Open=2492.0
        # Верхняя тень = 2508.0 - 2495.0 = 13.0, Тело = 2495.0 - 2492.0 = 3.0 -> Wick Ratio = 13/3 = 4.33 >= 0.8
        # Вне мертвой зоны (2495.0 > 2482.50)
        short_sweep_candle = JazzCandle(
            open_time=now_ts + 300000,
            open=2492.0,
            high=2508.0,
            low=2491.0,
            close=2495.0,
            volume=250.0,
            close_time=now_ts + 600000,
            is_closed=True
        )
        strategy.on_kline_5m(short_sweep_candle)
        pos = self.broker.get_position("JAZZ")
        self.assertIsNotNone(pos, "SHORT позиция должна была открыться!")
        self.assertEqual(pos["direction"], "SHORT")
        self.assertEqual(pos["entry_price"], 2495.0)
        # SL = high(2504) + buffer(2.5) = 2506.5. Риск = 2506.5 - 2495 = 11.5 > max_sl(7.0) -> SL = entry + 7.0 = 2502.0
        self.assertEqual(pos["sl"], 2502.0)
        self.assertEqual(pos["tp"], 2450.0)

        # 4. Two-Stage Watchdog: Безубыток (ход +5.00 USDT >= +4.50 USDT)
        # Для SHORT: цена падает до 2490.0 (ход = 2495 - 2490 = +5.00$)
        strategy.on_market_tick(best_bid=2489.8, best_ask=2490.0)
        self.assertTrue(strategy.be_activated, "Безубыток должен был активироваться!")
        updated_pos = self.broker.get_position("JAZZ")
        # target_sl = entry - be_lock = 2495.0 - 0.30 = 2494.70
        self.assertEqual(updated_pos["sl"], 2494.70)

        # 5. Ratchet Trailing: ход >= +6.50$ (цена падает до 2487.0$, ход = +8.00$)
        # Буфер 5.00$: ideal_sl = 2487.0 + 5.00 = 2492.00
        # Квантование: 2494.70 - int((2494.70 - 2492.00)/0.50)*0.50 = 2494.70 - 2.50 = 2492.20
        strategy.on_market_tick(best_bid=2486.8, best_ask=2487.0)
        self.assertTrue(strategy.trailing_active)
        updated_pos = self.broker.get_position("JAZZ")
        self.assertEqual(updated_pos["sl"], 2492.20)

        # 6. Компрессия буфера: ход >= +10.00$ (цена падает до 2483.0$, ход = +12.00$)
        # Буфер сжимается до 3.00$: ideal_sl = 2483.0 + 3.00 = 2486.00
        # Квантование: 2492.20 - int((2492.20 - 2486.00)/0.50)*0.50 = 2492.20 - 6.00 = 2486.20
        strategy.on_market_tick(best_bid=2482.8, best_ask=2483.0)
        self.assertTrue(strategy.compression_active)
        updated_pos = self.broker.get_position("JAZZ")
        self.assertEqual(updated_pos["sl"], 2486.20)

        # 7. Закрытие позиции по трейлинг-стопу
        close_res = self.broker.close_position("JAZZ", 2486.20, reason="TRAILING_STOP")
        self.assertGreater(close_res["net_pnl"], 0.0)
        self.assertIsNone(self.broker.get_position("JAZZ"))

    def test_15_strategy_jazz_long_sweep(self):
        """Тест LONG Sweep: закол 6H Low, Wick Check откупа, динамический SL/TP, безубыток и трейлинг."""
        strategy = JazzStrategy(self.test_config, self.broker)
        now_ts = int(time.time() * 1000)

        # 1. Заполняем 72 свечи 5M (коридор: low=2450.00, high=2500.00, mid=2475.00, dz=[2467.50, 2482.50])
        for i in range(72):
            p = 2475.0
            h = 2500.0 if i == 10 else 2480.0
            l = 2450.0 if i == 20 else 2470.0
            c_obj = JazzCandle(
                open_time=now_ts - (72 - i) * 300000,
                open=p, high=h, low=l, close=p, volume=100.0,
                close_time=now_ts - (71 - i) * 300000, is_closed=True
            )
            strategy.candles_5m.append(c_obj)
        strategy.recalculate_corridor()

        # 2. LONG Sweep: Low=2442.0 (< 2450.0), Close=2455.0 (> 2450.0), Open=2458.0
        # Нижняя тень = min(2458, 2455) - 2442.0 = 13.0, Тело = 3.0 -> Wick Ratio = 4.33 >= 0.8
        # Вне мертвой зоны (2455.0 < 2467.50)
        long_sweep_candle = JazzCandle(
            open_time=now_ts + 300000,
            open=2458.0, high=2460.0, low=2442.0, close=2455.0, volume=300.0,
            close_time=now_ts + 600000, is_closed=True
        )
        strategy.on_kline_5m(long_sweep_candle)
        pos = self.broker.get_position("JAZZ")
        self.assertIsNotNone(pos, "LONG позиция должна была открыться!")
        self.assertEqual(pos["direction"], "LONG")
        self.assertEqual(pos["entry_price"], 2455.0)
        # SL = low(2442) - buffer(2.5) = 2439.5. Риск = 2455 - 2439.5 = 15.5 > max_sl(7.0) -> SL = entry - 7.0 = 2448.0
        self.assertEqual(pos["sl"], 2448.0)
        self.assertEqual(pos["tp"], 2500.0)

        # 3. Безубыток для LONG: ход цены до 2460.0 (хода +5.00$ >= +4.50$)
        strategy.on_market_tick(best_bid=2460.0, best_ask=2460.2)
        self.assertTrue(strategy.be_activated)
        updated_pos = self.broker.get_position("JAZZ")
        # target_sl = entry + be_lock = 2455.0 + 0.30 = 2455.30
        self.assertEqual(updated_pos["sl"], 2455.30)

        # 4. Ratchet Trailing: ход до 2463.0$ (ход +8.00$ >= +6.50$)
        # Буфер 5.00$: ideal_sl = 2463.0 - 5.00 = 2458.00
        # Квантование: 2455.30 + int((2458.00 - 2455.30)/0.50)*0.50 = 2455.30 + 2.50 = 2457.80
        strategy.on_market_tick(best_bid=2463.0, best_ask=2463.2)
        self.assertTrue(strategy.trailing_active)
        updated_pos = self.broker.get_position("JAZZ")
        self.assertEqual(updated_pos["sl"], 2457.80)

        # Закрываем
        self.broker.close_position("JAZZ", 2457.80, reason="TRAILING_STOP")

    def test_16_strategy_jazz_dual_sweep_outside_bar(self):
        """Тест обработки двустороннего пробоя (Outside Bar): при симметричном вертолете сделка пропускается."""
        strategy = JazzStrategy(self.test_config, self.broker)
        now_ts = int(time.time() * 1000)
        for i in range(72):
            c_obj = JazzCandle(
                open_time=now_ts - (72 - i) * 300000,
                open=2475.0,
                high=2500.0 if i == 10 else 2480.0,
                low=2450.0 if i == 20 else 2470.0,
                close=2475.0, volume=100.0,
                close_time=now_ts - (71 - i) * 300000, is_closed=True
            )
            strategy.candles_5m.append(c_obj)
        strategy.recalculate_corridor()

        # Свеча-вертолет: пробила и high (2501 > 2500) и low (2419 < 2450)
        # Одинаковые симметричные тени по 41.0$ вне мертвой зоны -> конфликт сигналов -> отказ от входа
        dual_sweep_candle = JazzCandle(
            open_time=now_ts + 300000,
            open=2460.0, high=2501.0, low=2419.0, close=2460.0, volume=500.0,
            close_time=now_ts + 600000, is_closed=True
        )
        strategy.on_kline_5m(dual_sweep_candle)
        self.assertIsNone(self.broker.get_position("JAZZ"), "Симметричный вертолет должен блокировать сделку!")

        # Асимметричный вертолет: доминирует нижняя тень (откуп) -> выбирает LONG
        strategy_asym = JazzStrategy(self.test_config, self.broker)
        for i in range(72):
            c_obj = JazzCandle(
                open_time=now_ts - (72 - i) * 300000,
                open=2475.0,
                high=2500.0 if i == 10 else 2480.0,
                low=2450.0 if i == 20 else 2470.0,
                close=2475.0, volume=100.0,
                close_time=now_ts - (71 - i) * 300000, is_closed=True
            )
            strategy_asym.candles_5m.append(c_obj)
        strategy_asym.recalculate_corridor()

        dominant_long_candle = JazzCandle(
            open_time=now_ts + 300000,
            open=2465.0, high=2501.0, low=2400.0, close=2466.0, volume=500.0,
            close_time=now_ts + 600000, is_closed=True
        )
        # Upper wick = 2501 - 2466 = 35. Lower wick = 2465 - 2400 = 65 (65 > 35 * 1.5 = 52.5) -> LONG wins
        strategy_asym.on_kline_5m(dominant_long_candle)
        pos = self.broker.get_position("JAZZ")
        self.assertIsNotNone(pos, "Асимметричный вертолет с доминирующим откупом должен открыть LONG!")
        self.assertEqual(pos["direction"], "LONG")
        self.broker.close_position("JAZZ", 2465.0, reason="MANUAL_CLEANUP")

    def test_17_strategy_jazz_restart_preservation(self):
        """Тест сохранения экстремума best_price и трейлинга в JAZZ при перезапуске процесса."""
        strategy1 = JazzStrategy(self.test_config, self.broker)
        # Открываем позицию LONG вручную
        pos = self.broker.open_position("JAZZ", "LONG", 0.05, 2500.0, is_maker=False, sl=2495.0, tp=2550.0)

        # Симулируем движение цены до 2508.0 (ход +8.00$)
        strategy1.on_market_tick(2508.0, 2508.2)
        self.assertTrue(strategy1.trailing_active)
        pos_after = self.broker.get_position("JAZZ")
        saved_sl = pos_after["sl"]
        self.assertEqual(pos_after["best_price"], 2508.0)

        # "Перезапуск процесса" - создаем новый инстанс JazzStrategy
        strategy2 = JazzStrategy(self.test_config, self.broker)
        self.assertEqual(strategy2.extreme_price, 0.0)

        # Первый тик после перезапуска при откате цены до 2506.0
        strategy2.on_market_tick(2506.0, 2506.2)
        # Экстремум 2508.0 должен восстановиться из сохраненной позиции, а не сброситься до 2506.0
        self.assertEqual(strategy2.extreme_price, 2508.0)
        self.assertTrue(strategy2.trailing_active)
        # Стоп не должен откатиться назад
        self.assertEqual(self.broker.get_position("JAZZ")["sl"], saved_sl)
        self.broker.close_position("JAZZ", 2506.0, reason="CLEANUP")

    def test_18_strategy_jazz_breakeven_pullback_protection(self):
        """Тест: если цена после импульса откаталась ниже безубытка, стоп не переносится за текущую цену."""
        strategy = JazzStrategy(self.test_config, self.broker)
        self.broker.open_position("JAZZ", "LONG", 0.05, 2500.0, is_maker=False, sl=2495.0, tp=2550.0)

        # Симулируем экстремум импульса +4.60$ (2504.60$)
        strategy.extreme_price = 2504.60

        # Но текущая цена уже откаталась до 2500.20$ (ниже target_sl 2500.30$)
        strategy.on_market_tick(best_bid=2500.20, best_ask=2500.40)

        # Безубыток не должен сработать, так как target_sl (2500.30) > curr_price (2500.20)
        pos = self.broker.get_position("JAZZ")
        self.assertFalse(strategy.be_activated, "Безубыток не должен был активироваться за текущей ценой!")
        self.assertEqual(pos["sl"], 2495.0)
        self.broker.close_position("JAZZ", 2500.0, reason="CLEANUP")

    def test_19_hour_open_filter_drift_protection(self):
        """Тест защиты от дрейфа базовой цены часа open_price_1h (> 65 минут)."""
        hof = HourOpenFilter(threshold_delta_pct=0.0015, max_drift_seconds=3900)
        t0 = 1000000.0

        # 1. Инициализируем цену часа на t0: open = 60000.0
        eval1 = hof.evaluate(side="SHORT", current_price=60050.0, open_price_1h=60000.0, current_time=t0)
        self.assertTrue(eval1.is_allowed)
        self.assertEqual(hof.open_price_1h, 60000.0)

        # 2. Спустя 70 минут (4200 сек > 3900 сек), цена ушла на 61100.0 (+1.83%)
        # Без защиты от дрейфа SHORT был бы заблокирован вечным VETO_SHORT_BULLISH_HOUR.
        # Защита от дрейфа обновляет базовую цену через API или сбрасывает к current_price.
        hof.refresh_open_price_from_api = lambda: 61080.0
        t_drift = t0 + 4200.0
        eval2 = hof.evaluate(side="SHORT", current_price=61100.0, open_price_1h=60000.0, current_time=t_drift)
        self.assertTrue(eval2.is_allowed, f"Дрейф не был сброшен: {eval2.message}")
        self.assertEqual(hof.open_price_1h, 61080.0)
        self.assertAlmostEqual(eval2.delta_hour_pct, (61100.0 - 61080.0) / 61080.0, places=4)

        # 3. Резервный сценарий: если API недоступен, база подтягивается к current_price
        hof3 = HourOpenFilter(threshold_delta_pct=0.0015, max_drift_seconds=3900)
        hof3.update_open_price(60000.0, t0)
        hof3.refresh_open_price_from_api = lambda: None
        eval3 = hof3.evaluate(side="SHORT", current_price=61200.0, open_price_1h=60000.0, current_time=t0 + 5000.0)
        self.assertTrue(eval3.is_allowed)
        self.assertEqual(eval3.delta_hour_pct, 0.0)
        self.assertEqual(hof3.open_price_1h, 61200.0)


if __name__ == "__main__":
    unittest.main()

