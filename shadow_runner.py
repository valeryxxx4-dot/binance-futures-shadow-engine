"""
shadow_runner.py - Главный оркестратор теневого мультистратегического полигона (ETHUSDT + BTCUSDT).
Мультиплексный WebSocket Binance Futures с нулевым риском бана REST API:
- ethusdt@kline_15m, ethusdt@kline_1h, ethusdt@depth20@100ms
- btcusdt@kline_15m, btcusdt@kline_1h, btcusdt@depth20@100ms

Модели:
1. DLH (Daily Liquidity Hunter, ETHUSDT 1H/15M)
2. HRB (Hub-Regime Breakout, ETHUSDT 15M)
3. ARGUS (Argus BTC LightGBM ML + Hour Filter + SMM Guard + Adaptive Time-Stop, BTCUSDT 15M)

Регламент:
- Живое немерцающее окно консоли / дашборда на ANSI escape-кодах (\033[H + \033[K).
- Реальное время для Trade Entry и Trade Exit в Telegram.
- Микро-шум отключен (трейлинги и тики пишутся только в SQLite и выводятся на дашборд).
- Суточный отчет в 21:00 МСК (18:00 UTC) со сравнительной таблицей 3 субсчетов.
"""

import os
import sys
import json
import time
import asyncio
import datetime
from datetime import timezone
import traceback
import sqlite3
import httpx
import websockets

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from virtual_broker import VirtualBroker
from strategy_dlh import DailyLiquidityHunter, Candle as DLHCandle
from strategy_hrb import HubRegimeBreakout, Candle as HRBCandle
from strategy_argus import ArgusBTCStrategy, Candle as ArgusCandle
from strategy_jazz import JazzStrategy, Candle as JazzCandle
from dashboard import ConsoleDashboard

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config_simulation.json")


class ShadowEngine:
    def __init__(self, config_file: str):
        with open(config_file, "r", encoding="utf-8") as f:
            self.config = json.load(f)

        self.outbox_dir = self.config.get("outbox_dir", r"d:\Telegram bot for Jarvis\www.binance.com\outbox")
        self.chat_id = self.config.get("telegram_chat_id", 1105081236)

        # Торговый движок и стратегии
        self.broker = VirtualBroker(self.config)
        self.strategy_dlh = DailyLiquidityHunter(self.config, self.broker)
        self.strategy_hrb = HubRegimeBreakout(self.config, self.broker)
        self.strategy_argus = ArgusBTCStrategy(self.config, self.broker)
        self.strategy_jazz = JazzStrategy(self.config, self.broker)

        # Консольный дашборд (ANSI)
        self.dashboard = ConsoleDashboard(max_events=8)

        # Рыночные котировки и метрики
        self.prices = {
            "ETHUSDT": 0.0,
            "BTCUSDT": 0.0
        }
        self.best_quotes = {
            "ETHUSDT": {"bid": 0.0, "ask": 0.0},
            "BTCUSDT": {"bid": 0.0, "ask": 0.0}
        }
        self.market_metrics = {
            "ETH_OBI": 0.0,
            "BTC_OBI": 0.0,
            "BTC_SMM": 0.0,
            "BTC_HOUR_DELTA": 0.0
        }

        self.last_equity_snapshot_hour = -1
        self.last_daily_report_day = ""
        self.is_running = True

        self.dashboard.add_event("Полигон инициализирован (4 модели: DLH, HRB, ARGUS, JAZZ).", "SYS")

    def send_telegram(self, text: str):
        """Отправка уведомления через outbox JSON."""
        try:
            os.makedirs(self.outbox_dir, exist_ok=True)
            ts = int(time.time() * 1000)
            filename = f"reply_shadow_{ts}.json"
            filepath = os.path.join(self.outbox_dir, filename)
            payload = {
                "chat_id": self.chat_id,
                "text": text
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self.dashboard.add_event(f"Telegram outbox отправлен -> {filename}", "SYS")
        except Exception as e:
            self.dashboard.add_event(f"Ошибка Telegram outbox: {e}", "ALERT")

    def preload_history(self):
        """Предзагрузка исторических данных для всех 3 стратегий через REST API Binance."""
        self.dashboard.add_event("Предзагрузка исторических свечей (ETH & BTC)...", "SYS")
        try:
            # 1. ETHUSDT 1D для DLH
            url_1d = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=1d&limit=5"
            r = httpx.get(url_1d, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
                yesterday = data[-2]
                pdh = float(yesterday[2])
                pdl = float(yesterday[3])
                self.strategy_dlh.update_daily_levels(pdh, pdl)
                self.dashboard.add_event(f"DLH уровни: PDH=${pdh:.2f} | PDL=${pdl:.2f}", "SYS")

            # 2. ETHUSDT 15M для HRB
            url_15m_eth = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=15m&limit=60"
            r_15m = httpx.get(url_15m_eth, timeout=10.0)
            if r_15m.status_code == 200:
                candles_raw = r_15m.json()
                for c in candles_raw[:-1]:
                    c_obj = HRBCandle(
                        open_time=int(c[0]),
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]),
                        close_time=int(c[6]),
                        is_closed=True
                    )
                    self.strategy_hrb.candles_15m.append(c_obj)
                self.dashboard.add_event(f"HRB загружено {len(self.strategy_hrb.candles_15m)} свечей 15M ETH", "SYS")

            # 3. BTCUSDT 1H для Hour-Open Filter (Argus)
            url_1h_btc = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=5"
            r_1h_btc = httpx.get(url_1h_btc, timeout=10.0)
            if r_1h_btc.status_code == 200:
                candles_1h = r_1h_btc.json()
                latest_1h = candles_1h[-1]
                open_1h = float(latest_1h[1])
                self.strategy_argus.update_hourly_open(open_1h)
                self.dashboard.add_event(f"ARGUS 1H Open: ${open_1h:.2f}", "SYS")

            # 4. BTCUSDT 15M для 25 признаков LightGBM (Argus)
            url_15m_btc = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=80"
            r_15m_btc = httpx.get(url_15m_btc, timeout=10.0)
            if r_15m_btc.status_code == 200:
                candles_btc = r_15m_btc.json()
                for c in candles_btc[:-1]:
                    c_obj = ArgusCandle(
                        open_time=int(c[0]),
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]),
                        close_time=int(c[6]),
                        is_closed=True
                    )
                    self.strategy_argus.candles_15m.append(c_obj)
                self.strategy_argus.evaluate_preloaded_data()
                self.dashboard.add_event(f"ARGUS загружено {len(self.strategy_argus.candles_15m)} свечей 15M BTC | Статус: {self.strategy_argus.last_filter_msg} (ML={self.strategy_argus.last_lgb_prob}%)", "SYS")

            # 5. ETHUSDT 5M для JAZZ (Donchian 72 bars corridor)
            url_5m_eth = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=5m&limit=100"
            r_5m_eth = httpx.get(url_5m_eth, timeout=10.0)
            if r_5m_eth.status_code == 200:
                candles_5m = r_5m_eth.json()
                for c in candles_5m[:-1]:
                    c_obj = JazzCandle(
                        open_time=int(c[0]),
                        open=float(c[1]),
                        high=float(c[2]),
                        low=float(c[3]),
                        close=float(c[4]),
                        volume=float(c[5]),
                        close_time=int(c[6]),
                        is_closed=True
                    )
                    self.strategy_jazz.candles_5m.append(c_obj)
                self.strategy_jazz.recalculate_corridor()
                self.dashboard.add_event(f"JAZZ загружено {len(self.strategy_jazz.candles_5m)} свечей 5M ETH (High={self.strategy_jazz.ref_high}$ | Low={self.strategy_jazz.ref_low}$)", "SYS")

        except Exception as e:
            self.dashboard.add_event(f"Ошибка предзагрузки: {e}", "ALERT")

    def on_depth_update(self, symbol: str, data: dict):
        """Обработка стакана depth20@100ms для ETHUSDT или BTCUSDT."""
        bids = data.get("b", [])
        asks = data.get("a", [])
        if not bids or not asks:
            return

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid_price = round((best_bid + best_ask) / 2.0, 2)

        self.prices[symbol] = mid_price
        self.best_quotes[symbol]["bid"] = best_bid
        self.best_quotes[symbol]["ask"] = best_ask

        # Расчет OBI для символа
        b_vol = sum(float(b[1]) for b in bids)
        a_vol = sum(float(a[1]) for a in asks)
        obi = round((b_vol - a_vol) / (b_vol + a_vol), 4) if (b_vol + a_vol) > 0 else 0.0

        if symbol == "ETHUSDT":
            self.market_metrics["ETH_OBI"] = obi

            # 1. Проверка защитных стопов и тейков для ETH стратегий (DLH, HRB, JAZZ)
            for strat in ["DLH", "HRB", "JAZZ"]:
                pos = self.broker.get_position(strat)
                if pos is not None:
                    cur_p = best_bid if pos["direction"] == "LONG" else best_ask

                    # Stop Loss / Trailing Stop
                    if pos["sl"] is not None:
                        hit_sl = (cur_p <= pos["sl"]) if pos["direction"] == "LONG" else (cur_p >= pos["sl"])
                        if hit_sl:
                            initial_sl = pos.get("initial_sl", pos["sl"])
                            is_trailing = False
                            if pos["direction"] == "LONG" and pos["sl"] > initial_sl:
                                is_trailing = True
                            elif pos["direction"] == "SHORT" and pos["sl"] < initial_sl:
                                is_trailing = True

                            reason_name = "TRAILING_STOP" if is_trailing else "STOP_LOSS"
                            header = "🛡 TRAILING STOP СРАБОТАЛ" if is_trailing else "❌ STOP-LOSS СРАБОТАЛ"

                            res = self.broker.close_position(strat, cur_p, reason=reason_name, is_maker=False)
                            self.notify_trade_close(strat, res, header)
                            self.dashboard.add_event(f"{strat} {reason_name} @ {cur_p:.2f} (Net: ${res['net_pnl']:+.2f})", "SL" if res['net_pnl'] < 0 else "PROFIT")
                            continue

                    # Take Profit
                    if pos["tp"] is not None:
                        hit_tp = (cur_p >= pos["tp"]) if pos["direction"] == "LONG" else (cur_p <= pos["tp"])
                        if hit_tp:
                            res = self.broker.close_position(strat, cur_p, reason="TAKE_PROFIT", is_maker=False)
                            self.notify_trade_close(strat, res, "🎯 TAKE-PROFIT ВЫПОЛНЕН")
                            self.dashboard.add_event(f"{strat} TAKE_PROFIT @ {cur_p:.2f} (Net: ${res['net_pnl']:+.2f})", "WIN")
                            continue

            # 2. Передача тика в стратегии для ведения трейлинга
            self.strategy_dlh.on_market_tick(best_bid, best_ask)
            self.strategy_hrb.on_market_tick(best_bid, best_ask)
            self.strategy_jazz.on_market_tick(best_bid, best_ask)

        elif symbol == "BTCUSDT":
            self.market_metrics["BTC_OBI"] = obi

            # 1. Проверка защитных стопов и тейков для ARGUS
            pos = self.broker.get_position("ARGUS")
            if pos is not None:
                cur_p = best_bid if pos["direction"] == "LONG" else best_ask

                if pos["sl"] is not None:
                    hit_sl = (cur_p <= pos["sl"]) if pos["direction"] == "LONG" else (cur_p >= pos["sl"])
                    if hit_sl:
                        initial_sl = pos.get("initial_sl", pos["sl"])
                        is_trailing = False
                        if pos["direction"] == "LONG" and pos["sl"] > initial_sl:
                            is_trailing = True
                        elif pos["direction"] == "SHORT" and pos["sl"] < initial_sl:
                            is_trailing = True

                        reason_name = "TRAILING_STOP" if is_trailing else "STOP_LOSS"
                        header = "🛡 TRAILING STOP СРАБОТАЛ" if is_trailing else "❌ STOP-LOSS СРАБОТАЛ"

                        res = self.broker.close_position("ARGUS", cur_p, reason=reason_name, is_maker=False)
                        self.notify_trade_close("ARGUS", res, header)
                        self.dashboard.add_event(f"ARGUS {reason_name} @ {cur_p:.2f} (Net: ${res['net_pnl']:+.2f})", "SL" if res['net_pnl'] < 0 else "PROFIT")
                        pos = None

                if pos is not None and pos["tp"] is not None:
                    hit_tp = (cur_p >= pos["tp"]) if pos["direction"] == "LONG" else (cur_p <= pos["tp"])
                    if hit_tp:
                        res = self.broker.close_position("ARGUS", cur_p, reason="TAKE_PROFIT", is_maker=False)
                        self.notify_trade_close("ARGUS", res, "🎯 TAKE-PROFIT ВЫПОЛНЕН")
                        self.dashboard.add_event(f"ARGUS TAKE_PROFIT @ {cur_p:.2f} (Net: ${res['net_pnl']:+.2f})", "WIN")

            # 2. Передача тика в стратегию Argus (Chandelier, SMM, Grace Broken Pullback)
            argus_event = self.strategy_argus.on_depth_tick(best_bid, best_ask, data)
            if argus_event is not None:
                event_name, res = argus_event
                if event_name == "TIME_STOP_EXIT":
                    self.notify_trade_close("ARGUS", res, f"⏱️ TIME STOP ({res['reason']})")
                    self.dashboard.add_event(f"ARGUS {res['reason']} @ {res['exit_price']:.2f} (Net: ${res['net_pnl']:+.2f})", "SL" if res['net_pnl'] < 0 else "PROFIT")

            self.strategy_argus.check_hour_drift(mid_price)
            self.market_metrics["BTC_SMM"] = self.strategy_argus.current_smm_bps
            if self.strategy_argus.open_price_1h > 0:
                self.market_metrics["BTC_HOUR_DELTA"] = (mid_price - self.strategy_argus.open_price_1h) / self.strategy_argus.open_price_1h

        # 3. Ежечасный тихий снапшот эквити в simulation.db
        now_dt = datetime.datetime.now(timezone.utc)
        if now_dt.hour != self.last_equity_snapshot_hour:
            self.broker.record_equity_snapshot(self.prices)
            self.last_equity_snapshot_hour = now_dt.hour
            self.dashboard.add_event(f"Снапшот эквити сохранен в SQLite (UTC {now_dt.strftime('%H:%M')})", "SYS")

        # 4. Суточный отчет: строго в 21:00 по Минску/МСК (18:00 UTC)
        day_str = now_dt.strftime("%Y-%m-%d")
        if now_dt.hour == 18 and now_dt.minute >= 0 and self.last_daily_report_day != day_str:
            self.send_daily_summary()
            self.last_daily_report_day = day_str

        # 5. Отрисовка живого ANSI дашборда
        self.dashboard.render(
            broker=self.broker,
            prices=self.prices,
            market_metrics=self.market_metrics,
            dlh_strategy=self.strategy_dlh,
            hrb_strategy=self.strategy_hrb,
            argus_strategy=self.strategy_argus,
            jazz_strategy=self.strategy_jazz
        )

    def on_kline_event(self, symbol: str, interval: str, kline: dict):
        """Обработка свечей kline_15m, kline_1h и kline_5m для ETH и BTC."""
        open_time = int(kline.get("t"))
        o = float(kline.get("o"))
        h = float(kline.get("h"))
        l = float(kline.get("l"))
        c = float(kline.get("c"))
        v = float(kline.get("v"))
        close_time = int(kline.get("T"))
        is_closed = bool(kline.get("x"))

        if symbol == "ETHUSDT":
            if interval == "1h":
                candle = DLHCandle(open_time, o, h, l, c, v, close_time, is_closed)
                pos_before = self.broker.get_position("DLH")
                self.strategy_dlh.on_kline_1h(candle)
                pos_after = self.broker.get_position("DLH")
                if pos_before is None and pos_after is not None:
                    self.notify_trade_open("DLH", pos_after)
                    self.dashboard.add_event(f"DLH Вход {pos_after['direction']} @ ${pos_after['entry_price']:.2f}", "BUY" if pos_after['direction'] == "LONG" else "SELL")

            elif interval == "15m":
                candle = HRBCandle(open_time, o, h, l, c, v, close_time, is_closed)
                pos_before = self.broker.get_position("HRB")
                self.strategy_hrb.on_kline_15m(candle)
                pos_after = self.broker.get_position("HRB")
                if pos_before is None and pos_after is not None:
                    self.notify_trade_open("HRB", pos_after)
                    self.dashboard.add_event(f"HRB Вход {pos_after['direction']} @ ${pos_after['entry_price']:.2f}", "BUY" if pos_after['direction'] == "LONG" else "SELL")

            elif interval == "5m":
                candle = JazzCandle(open_time, o, h, l, c, v, close_time, is_closed)
                pos_before = self.broker.get_position("JAZZ")
                self.strategy_jazz.on_kline_5m(candle)
                pos_after = self.broker.get_position("JAZZ")
                if pos_before is None and pos_after is not None:
                    self.notify_trade_open("JAZZ", pos_after)
                    self.dashboard.add_event(f"JAZZ Вход {pos_after['direction']} @ ${pos_after['entry_price']:.2f}", "BUY" if pos_after['direction'] == "LONG" else "SELL")

        elif symbol == "BTCUSDT":
            if interval == "1h":
                candle = ArgusCandle(open_time, o, h, l, c, v, close_time, is_closed)
                self.strategy_argus.on_kline_1h(candle)

            elif interval == "15m":
                candle = ArgusCandle(open_time, o, h, l, c, v, close_time, is_closed)
                pos_before = self.broker.get_position("ARGUS")
                result = self.strategy_argus.on_kline_15m(candle)
                pos_after = self.broker.get_position("ARGUS")

                if pos_before is None and pos_after is not None:
                    self.notify_trade_open("ARGUS", pos_after)
                    self.dashboard.add_event(f"ARGUS ML Вход {pos_after['direction']} @ ${pos_after['entry_price']:.2f}", "BUY" if pos_after['direction'] == "LONG" else "SELL")

                elif result is not None:
                    action_type = result[0]
                    if action_type in ("TIME_STOP_CLOSE", "TIME_STOP_EXIT"):
                        res = result[1]
                        self.notify_trade_close("ARGUS", res, f"⏱️ TIME STOP ({res['reason']})")
                        self.dashboard.add_event(f"ARGUS {res['reason']} @ ${res['exit_price']:.2f} (Net: ${res['net_pnl']:+.2f})", "SL" if res['net_pnl'] < 0 else "PROFIT")
                    elif action_type == "GRACE_ENTERED":
                        decision = result[1]
                        self.dashboard.add_event(f"ARGUS Grace Period активирован (SL={decision.new_sl})", "ALERT")

    def notify_trade_open(self, strat: str, pos: dict):
        """Алерт только при фактическом виртуальном исполнении ордера."""
        lev = self.config.get("leverage", 15)
        entry_fee = pos.get("entry_fee", round(pos["entry_price"] * pos["size"] * self.config.get("taker_fee", 0.0005), 4))
        notes = pos.get("notes", "Автоматический триггер")
        symbol = pos.get("symbol", "BTCUSDT" if strat == "ARGUS" else "ETHUSDT")
        asset = "BTC" if "BTC" in symbol else "ETH"

        lines = [
            f"🚀 [SHADOW TEST] Открыта сделка: {strat}",
            "",
            f"• Инструмент: {symbol} (Плечо {lev}x)",
            f"• Направление: {pos['direction']}",
            f"• Объем: {pos['size']} {asset}",
            f"• Цена входа: ${pos['entry_price']:.2f}",
            f"• Расчетный Stop-Loss: ${pos['sl']:.2f}",
            f"• Take-Profit: ${pos['tp']:.2f}",
            f"• Комиссия за вход: ${entry_fee:.4f} USDT",
            f"• Сетап: {notes}"
        ]
        self.send_telegram("\n".join(lines))

    def notify_trade_close(self, strat: str, res: dict, header: str):
        """Алерт закрытия сделки: пункты хода, валовый PnL, комиссии, Net PnL."""
        pnl_symbol = "+" if res["net_pnl"] >= 0 else ""
        gross_symbol = "+" if res["gross_pnl"] >= 0 else ""

        entry_p = res["entry_price"]
        exit_p = res["exit_price"]
        direction = res["direction"]
        asset = "BTC" if strat == "ARGUS" else "ETH"

        pts = (exit_p - entry_p) if direction == "LONG" else (entry_p - exit_p)
        pts_sign = "+" if pts >= 0 else ""

        lines = [
            f"⚡ [SHADOW TEST] Закрытие позиции: {strat}",
            header,
            "",
            f"• Направление: {direction}",
            f"• Объем: {res['size']} {asset}",
            f"• Вход: ${entry_p:.2f} -> Выход: ${exit_p:.2f}",
            f"• Движение цены: {pts_sign}{pts:.2f} USDT",
            f"• Валовый PnL (Gross): {gross_symbol}${res['gross_pnl']:.2f} USDT",
            f"• Комиссии биржи: ${res['exit_fee']:.4f} USDT",
            f"• Итоговый PnL (Net): {pnl_symbol}${res['net_pnl']:.2f} USDT",
            f"• Баланс субаккаунта: ${res['wallet_balance']:.2f} USDT"
        ]
        self.send_telegram("\n".join(lines))

    def send_daily_summary(self):
        """
        Суточная сводка в 21:00 по Минску (18:00 UTC):
        Сравнительная таблица эффективности 3 субсчетов (DLH, HRB, ARGUS).
        """
        init_dep = float(self.config.get("initial_deposit_per_strategy", 100.0))
        msk_time = datetime.datetime.now(timezone.utc) + datetime.timedelta(hours=3)

        strategies = ["DLH", "HRB", "ARGUS", "JAZZ"]
        strat_names = {
            "DLH": "🔹 СТРАТЕГИЯ А: DLH (Daily Liquidity Hunter | ETHUSDT)",
            "HRB": "🔸 СТРАТЕГИЯ Б: HRB (Hub-Regime Breakout | ETHUSDT)",
            "ARGUS": "🦅 СТРАТЕГИЯ В: ARGUS BTC (LightGBM ML + Anti-Counter | BTCUSDT)",
            "JAZZ": "🎷 СТРАТЕГИЯ Г: JAZZ (Dynamic 6H Corridor 5M Sweep | ETHUSDT)"
        }

        lines = [
            "📊 [SHADOW TEST] Вечерний суточный отчет полигона (4 МОДЕЛИ)",
            f"🕒 Время: {msk_time.strftime('%d.%m.%Y %H:%M')} МСК (18:00 UTC)",
            "",
            "════════════════════════════════════════════"
        ]

        total_dep = init_dep * len(strategies)
        total_balance = 0.0

        for strat in strategies:
            acc = self.broker.get_account(strat)
            stats = self._get_db_strategy_stats(strat)
            pos = self.broker.get_position(strat)

            tc = acc.get("trades_count", 0)
            wc = acc.get("wins_count", 0)
            wr = round(wc / max(tc, 1) * 100, 1)
            bal = acc.get("wallet_balance", init_dep)
            total_balance += bal

            chg = bal - init_dep
            sign_chg = "+" if chg >= 0 else ""

            fees = stats["fees"]
            gross = stats["gross"]
            ratio_str = f"{gross / fees:.2f}x" if fees > 0 else "N/A"

            asset = "BTC" if strat == "ARGUS" else "ETH"
            unrealized = 0.0
            if pos:
                inst = pos.get("symbol", "BTCUSDT" if strat == "ARGUS" else "ETHUSDT")
                cur_price = self.prices.get(inst, pos["entry_price"])
                if pos["direction"] == "LONG":
                    unrealized = (cur_price - pos["entry_price"]) * pos["size"]
                else:
                    unrealized = (pos["entry_price"] - cur_price) * pos["size"]
                state_str = f"В позиции ({pos['direction']} {pos['size']} {asset} @ ${pos['entry_price']:.2f}, PnL: ${unrealized:+.2f})"
            else:
                state_str = "Вне рынка (FLAT)"

            equity = bal + unrealized

            lines.extend([
                strat_names[strat],
                f"• Баланс: ${bal:.2f} USDT | Эквити: ${equity:.2f} USDT ({sign_chg}${chg:.2f} от ${init_dep:.2f})",
                f"• Сделок: {tc} | Win Rate: {wr}%",
                f"• Валовый PnL: ${gross:.2f} USDT",
                f"• Биржевые комиссии: ${fees:.4f} USDT",
                f"• Соотношение Gross / Fee: {ratio_str}",
                f"• Текущая позиция: {state_str}",
                "────────────────────────────────────────────"
            ])

        total_net = total_balance - total_dep
        total_sign = "+" if total_net >= 0 else ""

        lines.extend([
            f"💼 ИТОГО ПОЛИГОН (4 МОДЕЛИ):",
            f"• Совокупный депозит: ${total_dep:.2f} USDT",
            f"• Совокупный баланс: ${total_balance:.2f} USDT ({total_sign}${total_net:.2f} USDT)",
            "════════════════════════════════════════════",
            "",
            "🛡 Режим: Изолированная симуляция Zero-Risk (реальные ордера отключены)."
        ])

        self.send_telegram("\n".join(lines))
        self.dashboard.add_event("Суточный отчет 21:00 МСК успешно отправлен в Telegram.", "SYS")

    def _get_db_strategy_stats(self, strategy: str) -> dict:
        """Извлечение агрегированных метрик из trades."""
        db_path = self.config.get("db_path", "simulation.db")
        stats = {"gross": 0.0, "fees": 0.0, "net": 0.0}
        if not os.path.exists(db_path):
            return stats
        try:
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT SUM(pnl_gross), SUM(fees_paid), SUM(pnl_net) FROM trades WHERE strategy = ?", (strategy,))
            row = cur.fetchone()
            if row and row[0] is not None:
                stats["gross"] = round(row[0], 4)
                stats["fees"] = round(row[1], 4)
                stats["net"] = round(row[2], 4)
            conn.close()
        except Exception as e:
            self.dashboard.add_event(f"DB stats error: {e}", "ALERT")
        return stats

    async def run(self):
        """Основной асинхронный мультиплексный цикл WebSocket Binance Futures."""
        self.preload_history()

        # Мультиплексная подписка на ETHUSDT и BTCUSDT (kline_15m, kline_1h, kline_5m, depth20@100ms)
        streams = [
            "ethusdt@kline_15m",
            "ethusdt@kline_1h",
            "ethusdt@kline_5m",
            "ethusdt@depth20@100ms",
            "btcusdt@kline_15m",
            "btcusdt@kline_1h",
            "btcusdt@depth20@100ms"
        ]
        ws_url = f"wss://stream.binancefuture.com/stream?streams={'/'.join(streams)}"
        self.dashboard.add_event(f"Подключение к мультиплексному WebSocket: {len(streams)} потоков...", "SYS")

        while self.is_running:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self.dashboard.add_event("WebSocket подключен (ETHUSDT + BTCUSDT). Запуск дашборда...", "SYS")
                    while self.is_running:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        stream = data.get("stream", "")
                        payload = data.get("data", {})

                        # Определение символа из имени стрима
                        symbol = "ETHUSDT" if "ethusdt" in stream else "BTCUSDT"

                        if "depth20" in stream:
                            self.on_depth_update(symbol, payload)
                        elif "kline_1h" in stream:
                            k = payload.get("k", {})
                            self.on_kline_event(symbol, "1h", k)
                        elif "kline_15m" in stream:
                            k = payload.get("k", {})
                            self.on_kline_event(symbol, "15m", k)
                        elif "kline_5m" in stream:
                            k = payload.get("k", {})
                            self.on_kline_event(symbol, "5m", k)

            except asyncio.CancelledError:
                self.dashboard.add_event("Цикл остановлен пользователем.", "SYS")
                break
            except Exception as e:
                self.dashboard.add_event(f"Сбой WebSocket: {e}. Переподключение через 5с...", "ALERT")
                await asyncio.sleep(5)


def main():
    while True:
        try:
            engine = ShadowEngine(CONFIG_PATH)
            asyncio.run(engine.run())
        except KeyboardInterrupt:
            print("\n[EXIT] Shadow Engine остановлен.")
            break
        except Exception as e:
            traceback.print_exc()
            time.sleep(3)


if __name__ == "__main__":
    main()
