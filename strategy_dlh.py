"""
strategy_dlh.py - Daily Liquidity Hunter (DLH)
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional
import math


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


class DailyLiquidityHunter:
    def __init__(self, config: dict, broker):
        self.config = config.get("strategy_dlh", {})
        self.broker = broker
        self.strategy_id = "DLH"
        self.pos_size = float(config.get("position_size_eth", 0.05))
        
        # Parameters
        self.min_sweep = float(self.config.get("min_sweep_depth_usdt", 3.0))
        self.max_sweep = float(self.config.get("max_sweep_depth_usdt", 15.0))
        self.max_wick = float(self.config.get("max_wick_anomaly_usdt", 20.0))
        self.sl_buffer = float(self.config.get("sl_buffer_usdt", 3.5))
        self.max_sl = float(self.config.get("max_sl_usdt", 12.0))
        self.tp1_ratio = float(self.config.get("tp1_ratio", 0.5))
        self.ratchet_activation = float(self.config.get("ratchet_trail_activation_usdt", 18.0))
        self.ratchet_buffer = float(self.config.get("ratchet_trail_buffer_usdt", 6.0))
        
        # Level states
        self.current_day_utc: Optional[str] = None
        self.pdh: Optional[float] = None
        self.pdl: Optional[float] = None
        self.mid_day: Optional[float] = None
        
        # Daily candle collection
        self.today_candles: List[Candle] = []
        
        # Sweep states
        self.pdh_swept_today = False
        self.pdl_swept_today = False
        
        # Active trade tracking
        self.tp1_filled = False
        self.extreme_price_reached: Optional[float] = None

    def update_daily_levels(self, high: float, low: float):
        self.pdh = round(high, 2)
        self.pdl = round(low, 2)
        self.mid_day = round((self.pdh + self.pdl) / 2.0, 2)
        self.pdh_swept_today = False
        self.pdl_swept_today = False

    def on_kline_1h(self, candle: Candle):
        c_dt = datetime.fromtimestamp(candle.open_time / 1000, tz=timezone.utc)
        day_str = c_dt.strftime("%Y-%m-%d")
        
        if self.current_day_utc is not None and day_str != self.current_day_utc:
            if self.today_candles:
                calc_high = max(c.high for c in self.today_candles)
                calc_low = min(c.low for c in self.today_candles)
                self.update_daily_levels(calc_high, calc_low)
            self.today_candles = []
            self.pdh_swept_today = False
            self.pdl_swept_today = False
            
        self.current_day_utc = day_str
        
        if candle.is_closed:
            self.today_candles.append(candle)
            self._evaluate_signals_on_close(candle)

    def _evaluate_signals_on_close(self, candle: Candle):
        if self.pdh is None or self.pdl is None:
            return
            
        pos = self.broker.get_position(self.strategy_id)
        if pos is not None:
            return

        # 1. Bearish Sweep (PDH)
        if not self.pdh_swept_today:
            sweep_high = candle.high - self.pdh
            wick_len = candle.high - max(candle.open, candle.close)
            
            if (self.min_sweep <= sweep_high <= self.max_sweep and 
                candle.close < self.pdh and 
                wick_len <= self.max_wick):
                
                entry_price = candle.close
                raw_sl = candle.high + self.sl_buffer
                sl_dist = raw_sl - entry_price
                if sl_dist > self.max_sl:
                    sl_price = entry_price + self.max_sl
                else:
                    sl_price = raw_sl
                
                sl_price = round(sl_price, 2)
                tp1_price = self.mid_day if self.mid_day and self.mid_day < entry_price else round(entry_price - (sl_dist * 2.0), 2)
                
                success = self.broker.open_position(
                    strategy=self.strategy_id,
                    direction="SHORT",
                    size=self.pos_size,
                    price=entry_price,
                    sl=sl_price,
                    tp=tp1_price,
                    is_maker=False,
                    notes=f"DLH Bearish Sweep PDH ({self.pdh}) High={candle.high}"
                )
                if success:
                    self.pdh_swept_today = True
                    self.tp1_filled = False
                    self.extreme_price_reached = entry_price
                return

        # 2. Bullish Sweep (PDL)
        if not self.pdl_swept_today:
            sweep_low = self.pdl - candle.low
            wick_len = min(candle.open, candle.close) - candle.low
            
            if (self.min_sweep <= sweep_low <= self.max_sweep and 
                candle.close > self.pdl and 
                wick_len <= self.max_wick):
                
                entry_price = candle.close
                raw_sl = candle.low - self.sl_buffer
                sl_dist = entry_price - raw_sl
                if sl_dist > self.max_sl:
                    sl_price = entry_price - self.max_sl
                else:
                    sl_price = raw_sl
                    
                sl_price = round(sl_price, 2)
                tp1_price = self.mid_day if self.mid_day and self.mid_day > entry_price else round(entry_price + (sl_dist * 2.0), 2)
                
                success = self.broker.open_position(
                    strategy=self.strategy_id,
                    direction="LONG",
                    size=self.pos_size,
                    price=entry_price,
                    sl=sl_price,
                    tp=tp1_price,
                    is_maker=False,
                    notes=f"DLH Bullish Sweep PDL ({self.pdl}) Low={candle.low}"
                )
                if success:
                    self.pdl_swept_today = True
                    self.tp1_filled = False
                    self.extreme_price_reached = entry_price
                return

    def on_market_tick(self, best_bid: float, best_ask: float):
        pos = self.broker.get_position(self.strategy_id)
        if pos is None:
            self.tp1_filled = False
            self.extreme_price_reached = None
            return

        current_price = best_bid if pos['direction'] == "LONG" else best_ask
        
        # TP1 check (50% volume at Mid-Day)
        if not self.tp1_filled and self.mid_day is not None:
            if pos['direction'] == "LONG" and current_price >= self.mid_day:
                part_size = round(pos['size'] * self.tp1_ratio, 4)
                self.broker.partial_close(self.strategy_id, part_size, current_price, reason="TP1_MID_DAY")
                self.tp1_filled = True
                self.broker.modify_stop_loss(self.strategy_id, pos['entry_price'])
            elif pos['direction'] == "SHORT" and current_price <= self.mid_day:
                part_size = round(pos['size'] * self.tp1_ratio, 4)
                self.broker.partial_close(self.strategy_id, part_size, current_price, reason="TP1_MID_DAY")
                self.tp1_filled = True
                self.broker.modify_stop_loss(self.strategy_id, pos['entry_price'])

        # Ratchet Trailing
        if pos['direction'] == "LONG":
            if self.extreme_price_reached is None or current_price > self.extreme_price_reached:
                self.extreme_price_reached = current_price
                
            profit_run = self.extreme_price_reached - pos['entry_price']
            if profit_run >= self.ratchet_activation:
                target_sl = round(self.extreme_price_reached - self.ratchet_buffer, 2)
                if pos['sl'] is None or target_sl > pos['sl']:
                    self.broker.modify_stop_loss(self.strategy_id, target_sl)
                    
        elif pos['direction'] == "SHORT":
            if self.extreme_price_reached is None or current_price < self.extreme_price_reached:
                self.extreme_price_reached = current_price
                
            profit_run = pos['entry_price'] - self.extreme_price_reached
            if profit_run >= self.ratchet_activation:
                target_sl = round(self.extreme_price_reached + self.ratchet_buffer, 2)
                if pos['sl'] is None or target_sl < pos['sl']:
                    self.broker.modify_stop_loss(self.strategy_id, target_sl)
