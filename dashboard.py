"""
dashboard.py - Живой немерцающий консольный дашборд на ANSI escape-кодах (\033[H + \033[K).
По образцу Центрального Хаба V4 (hub.py).
Отображает:
- Статус 3 моделей (DLH, HRB, ARGUS)
- Балансы, эквити, валовый и чистый PnL
- Котировки ETHUSDT и BTCUSDT
- OBI, SMM, HFT метрики
- Бегущую строку последних событий (Event Ticker)
"""

import os
import sys
import time
from datetime import datetime, timezone
from collections import deque
from typing import Dict, Any, List, Optional

# Активация ANSI и UTF-8 в консоли Windows
if os.name == 'nt':
    os.system('')
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
GRAY = "\033[90m"


class ConsoleDashboard:
    def __init__(self, max_events: int = 8):
        self.max_events = max_events
        self.events: deque = deque(maxlen=max_events)
        self.start_time = time.time()
        self.first_render = True
        self.last_render_time = 0.0

    def add_event(self, text: str, tag: str = "INFO"):
        """Добавление записи в бегущую строку событий."""
        now_str = datetime.now().strftime("%H:%M:%S")
        if tag == "BUY" or tag == "WIN" or tag == "PROFIT":
            color = GREEN
        elif tag == "SELL" or tag == "LOSS" or tag == "SL":
            color = RED
        elif tag == "ALERT" or tag == "VETO":
            color = YELLOW
        elif tag == "SYS":
            color = CYAN
        else:
            color = WHITE

        prefix = f"[{now_str}] [{tag}]"
        self.events.append(f"{color}{prefix} {text}{RESET}")

    def render(
        self,
        broker,
        prices: Dict[str, float],
        market_metrics: Dict[str, Any],
        dlh_strategy=None,
        hrb_strategy=None,
        argus_strategy=None,
        jazz_strategy=None,
        force: bool = False
    ):
        """Отрисовка экрана дашборда без мерцания."""
        now = time.time()
        if not force and (now - self.last_render_time < 0.5):
            return  # Лимит частоты кадров 2 FPS для экономии CPU

        self.last_render_time = now

        if self.first_render:
            print("\033[H\033[J", end="", flush=True)
            self.first_render = False

        uptime_sec = int(now - self.start_time)
        hours, rem = divmod(uptime_sec, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

        # Агрегация балансов и эквити 4 стратегий
        total_balance = 0.0
        total_equity = 0.0
        total_realized_pnl = 0.0
        total_fees = 0.0
        total_trades = 0
        total_wins = 0

        accounts_data = {}
        for strat in ["DLH", "HRB", "ARGUS", "JAZZ"]:
            acc = broker.get_account(strat)
            pos = broker.get_position(strat)

            wb = acc.get("wallet_balance", 100.0)
            rpnl = acc.get("realized_pnl", 0.0)
            fees = acc.get("total_fees", 0.0)
            tc = acc.get("trades_count", 0)
            wc = acc.get("wins_count", 0)

            inst = pos.get("symbol") if pos else ("BTCUSDT" if strat == "ARGUS" else "ETHUSDT")
            cur_price = prices.get(inst, 0.0)

            unrealized = 0.0
            if pos is not None and cur_price > 0.0:
                if pos["direction"] == "LONG":
                    unrealized = (cur_price - pos["entry_price"]) * pos["size"]
                else:
                    unrealized = (pos["entry_price"] - cur_price) * pos["size"]

            strat_eq = wb + unrealized
            accounts_data[strat] = {
                "balance": wb,
                "equity": strat_eq,
                "rpnl": rpnl,
                "unrealized": unrealized,
                "fees": fees,
                "trades": tc,
                "wins": wc,
                "pos": pos
            }

            total_balance += wb
            total_equity += strat_eq
            total_realized_pnl += rpnl
            total_fees += fees
            total_trades += tc
            total_wins += wc

        win_rate_all = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
        total_dep = float(len(["DLH", "HRB", "ARGUS", "JAZZ"]) * getattr(broker, 'initial_deposit', 100.0))
        pnl_color = GREEN if (total_equity >= total_dep) else RED
        total_net_pnl = total_equity - total_dep
        pnl_sign = "+" if total_net_pnl >= 0 else ""

        lines = []

        # 1. ШАПКА ДАШБОРДА
        lines.append(f"{BOLD}{WHITE}╔════════════════════════════════════════════════════════════════════════════════════════════╗{RESET}\033[K")
        lines.append(f"{BOLD}{WHITE}║  🦅 ТЕНЕВОЙ МУЛЬТИСТРАТЕГИЧЕСКИЙ ПОЛИГОН V4.0 (ETHUSDT + BTCUSDT)           {CYAN}UP: {uptime_str}{RESET}{WHITE} ║{RESET}\033[K")
        lines.append(f"{BOLD}{WHITE}║  {YELLOW}[РЕЖИМ: ZERO-RISK SHADOW]{RESET} | ДЕПОЗИТ: {total_dep:6.2f}$ | БАЛАНС: {total_balance:6.2f}$ | ЭКВИТИ: {pnl_color}{total_equity:6.2f}${RESET}{WHITE} ║{RESET}\033[K")
        lines.append(f"{BOLD}{WHITE}║  ЧИСТЫЙ PnL: {pnl_color}{pnl_sign}{total_net_pnl:6.2f}${RESET} | КОМИССИИ: {total_fees:6.4f}$ | СДЕЛОК: {total_trades:2d} | WIN RATE: {win_rate_all:4.1f}%          ║{RESET}\033[K")
        lines.append(f"{BOLD}{WHITE}╠════════════════════════════════════════════════════════════════════════════════════════════╣{RESET}\033[K")

        # 2. МЕТРИКИ РЫНКА И КОТИРОВКИ
        eth_p = prices.get("ETHUSDT", 0.0)
        btc_p = prices.get("BTCUSDT", 0.0)
        eth_obi = market_metrics.get("ETH_OBI", 0.0)
        btc_obi = market_metrics.get("BTC_OBI", 0.0)
        btc_smm = market_metrics.get("BTC_SMM", 0.0)
        btc_hour_d = market_metrics.get("BTC_HOUR_DELTA", 0.0)

        obi_eth_c = GREEN if eth_obi > 0.1 else RED if eth_obi < -0.1 else GRAY
        obi_btc_c = GREEN if btc_obi > 0.1 else RED if btc_obi < -0.1 else GRAY

        lines.append(f"  {BOLD}💹 РЫНОК И HFT-МЕТРИКИ:{RESET}\033[K")
        lines.append(f"  • {BOLD}ETHUSDT:{RESET} {eth_p:7.2f}$  | OBI: {obi_eth_c}{eth_obi:+0.2f}{RESET} (Глубина 20 уровней @ 100ms)\033[K")
        lines.append(f"  • {BOLD}BTCUSDT:{RESET} {btc_p:8.2f}$ | OBI: {obi_btc_c}{btc_obi:+0.2f}{RESET} | SMM: {btc_smm:+5.1f} bps | Hour Δ: {btc_hour_d:+.3%}\033[K")
        lines.append(f"{WHITE}╟────────────────────────────────────────────────────────────────────────────────────────────╢{RESET}\033[K")

        # 3. СТАТУС 4 МОДЕЛЕЙ
        lines.append(f"  {BOLD}🤖 СТАТУС ТОРГОВЫХ АГЕНТОВ (4 МОДЕЛИ):{RESET}\033[K")

        # МОДЕЛЬ 1: DLH
        dlh = accounts_data["DLH"]
        dlh_pos = dlh["pos"]
        dlh_wr = (dlh["wins"] / max(dlh["trades"], 1)) * 100
        if dlh_pos:
            unr_c = GREEN if dlh["unrealized"] >= 0 else RED
            pos_info = f"{GREEN if dlh_pos['direction'] == 'LONG' else RED}{dlh_pos['direction']} {dlh_pos['size']} ETH @ {dlh_pos['entry_price']:.2f}${RESET} (SL: {dlh_pos['sl']}$ TP: {dlh_pos['tp']}$) PnL: {unr_c}{dlh['unrealized']:+.2f}${RESET}"
        else:
            pdh_str = f"PDH: {dlh_strategy.pdh}$ PDL: {dlh_strategy.pdl}$" if (dlh_strategy and dlh_strategy.pdh) else "Ожидание уровней"
            pos_info = f"{GRAY}FLAT ({pdh_str}){RESET}"
        lines.append(f"  {BOLD}1. DLH (ETH):{RESET}   Депо: {dlh['balance']:6.2f}$ | Eq: {dlh['equity']:6.2f}$ | WR: {dlh_wr:4.1f}% ({dlh['trades']} сд) | {pos_info}\033[K")

        # МОДЕЛЬ 2: HRB
        hrb = accounts_data["HRB"]
        hrb_pos = hrb["pos"]
        hrb_wr = (hrb["wins"] / max(hrb["trades"], 1)) * 100
        if hrb_pos:
            unr_c = GREEN if hrb["unrealized"] >= 0 else RED
            pos_info = f"{GREEN if hrb_pos['direction'] == 'LONG' else RED}{hrb_pos['direction']} {hrb_pos['size']} ETH @ {hrb_pos['entry_price']:.2f}${RESET} (SL: {hrb_pos['sl']}$ TP: {hrb_pos['tp']}$) PnL: {unr_c}{hrb['unrealized']:+.2f}${RESET}"
        else:
            regime_str = hrb_strategy.last_hub_regime if hrb_strategy else "SCANNING"
            atr_str = f"ATR: {hrb_strategy.current_atr:.2f}$" if hrb_strategy else ""
            pos_info = f"{GRAY}FLAT [{regime_str}] {atr_str}{RESET}"
        lines.append(f"  {BOLD}2. HRB (ETH):{RESET}   Депо: {hrb['balance']:6.2f}$ | Eq: {hrb['equity']:6.2f}$ | WR: {hrb_wr:4.1f}% ({hrb['trades']} сд) | {pos_info}\033[K")

        # МОДЕЛЬ 3: ARGUS BTC
        argus = accounts_data["ARGUS"]
        argus_pos = argus["pos"]
        argus_wr = (argus["wins"] / max(argus["trades"], 1)) * 100
        if argus_pos:
            unr_c = GREEN if argus["unrealized"] >= 0 else RED
            grace_flag = f" {YELLOW}[GRACE]{RESET}" if (argus_strategy and argus_strategy.grace_state.is_in_grace) else ""
            pos_info = f"{GREEN if argus_pos['direction'] == 'LONG' else RED}{argus_pos['direction']} {argus_pos['size']} BTC @ {argus_pos['entry_price']:.2f}${RESET}{grace_flag} (SL: {argus_pos['sl']}$ TP: {argus_pos['tp']}$) PnL: {unr_c}{argus['unrealized']:+.2f}${RESET}"
        else:
            ml_prob = argus_strategy.last_lgb_prob if argus_strategy else 50.0
            th = getattr(argus_strategy, 'prob_threshold', 0.55) * 100.0 if argus_strategy else 55.0
            prob_c = GREEN if ml_prob >= th else RED if ml_prob <= (100.0 - th) else WHITE
            ml_sig = argus_strategy.last_lgb_signal if argus_strategy else "NEUTRAL"
            flt_msg = argus_strategy.last_filter_msg if argus_strategy else "WAITING"
            pos_info = f"{GRAY}FLAT | ML: {prob_c}{ml_prob:.1f}% ({ml_sig}){RESET} | Статус: {flt_msg}{RESET}"
        lines.append(f"  {BOLD}3. ARGUS (BTC):{RESET} Депо: {argus['balance']:6.2f}$ | Eq: {argus['equity']:6.2f}$ | WR: {argus_wr:4.1f}% ({argus['trades']} сд) | {pos_info}\033[K")

        # МОДЕЛЬ 4: JAZZ (ETH)
        jazz = accounts_data["JAZZ"]
        jazz_pos = jazz["pos"]
        jazz_wr = (jazz["wins"] / max(jazz["trades"], 1)) * 100
        if jazz_pos:
            unr_c = GREEN if jazz["unrealized"] >= 0 else RED
            pos_info = f"{GREEN if jazz_pos['direction'] == 'LONG' else RED}{jazz_pos['direction']} {jazz_pos['size']} ETH @ {jazz_pos['entry_price']:.2f}${RESET} (SL: {jazz_pos['sl']}$ TP: {jazz_pos['tp']}$) PnL: {unr_c}{jazz['unrealized']:+.2f}${RESET}"
        else:
            corridor_str = f"H: {jazz_strategy.ref_high}$ L: {jazz_strategy.ref_low}$" if (jazz_strategy and jazz_strategy.ref_high) else "Ожидание 5m свечей"
            dz_str = f"DZ: [{jazz_strategy.dead_zone[0]}-{jazz_strategy.dead_zone[1]}$]" if (jazz_strategy and jazz_strategy.dead_zone) else ""
            pos_info = f"{GRAY}FLAT [{corridor_str}] {dz_str}{RESET}"
        lines.append(f"  {BOLD}4. JAZZ (ETH):{RESET}  Депо: {jazz['balance']:6.2f}$ | Eq: {jazz['equity']:6.2f}$ | WR: {jazz_wr:4.1f}% ({jazz['trades']} сд) | {pos_info}\033[K")

        lines.append(f"{WHITE}╠════════════════════════════════════════════════════════════════════════════════════════════╣{RESET}\033[K")

        # 4. БЕГУЩАЯ СТРОКА СОБЫТИЙ (EVENT TICKER)
        lines.append(f"  {BOLD}📜 БЕГУЩАЯ СТРОКА СОБЫТИЙ (EVENT LOG):{RESET}\033[K")
        if self.events:
            for ev in self.events:
                lines.append(f"   • {ev}\033[K")
        else:
            lines.append(f"   • {GRAY}Ожидание входящих рыночных тиков и сигналов...{RESET}\033[K")

        # Дополняем пустыми строками для фиксированной высоты окна
        event_count = len(self.events) if self.events else 1
        for _ in range(max(0, self.max_events - event_count)):
            lines.append(f"   \033[K")

        lines.append(f"{BOLD}{WHITE}╚════════════════════════════════════════════════════════════════════════════════════════════╝{RESET}\033[K")

        try:
            sys.stdout.write("\033[H" + "\n".join(lines) + "\n\033[J")
            sys.stdout.flush()
        except (OSError, IOError):
            pass
