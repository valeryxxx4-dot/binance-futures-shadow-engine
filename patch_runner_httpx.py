import re

with open('shadow_runner.py', 'r') as f:
    content = f.read()

if 'import httpx' not in content:
    content = content.replace('import json', 'import json\nimport httpx')

old_preload = """    async def preload_history(self):
        \"\"\"Предзагрузка исторических данных для всех 3 стратегий через REST API Binance.\"\"\"
        self.dashboard.add_event("Предзагрузка исторических свечей (ETH & BTC)...", "SYS")
        try:
            # 1. ETHUSDT 1D для DLH
            url_1d = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=1d&limit=5"
            r = requests.get(url_1d, timeout=10.0)
            if r.status_code == 200:
                data = r.json()
            yesterday = data[-2]
            pdh = float(yesterday[2])
            pdl = float(yesterday[3])
            self.strategy_dlh.update_daily_levels(pdh, pdl)
            self.dashboard.add_event(f"DLH уровни: PDH=${pdh:.2f} | PDL=${pdl:.2f}", "SYS")

            # 2. ETHUSDT 15M для HRB
            url_15m_eth = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=15m&limit=60"
            r_15m = requests.get(url_15m_eth, timeout=10.0)
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
                self.strategy_hrb.on_candle_closed(c_obj)

            # 3. BTCUSDT 5m для ARGUS (LightGBM)
            url_5m_btc = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=100"
            r_5m_btc = requests.get(url_5m_btc, timeout=10.0)
            if r_5m_btc.status_code == 200:
                btc_candles = r_5m_btc.json()
                for c in btc_candles[:-1]:
                    c_obj = {"open_time": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]), "close_time": int(c[6])}
                    self.strategy_argus.on_historical_candle(c_obj)
                self.dashboard.add_event(f"ARGUS загружено {len(btc_candles)-1} свечей BTCUSDT 5m", "SYS")

            # 4. ETHUSDT 5m для JAZZ
            url_5m_eth = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=5m&limit=150"
            r_5m_eth = requests.get(url_5m_eth, timeout=10.0)
            if r_5m_eth.status_code == 200:
                eth_candles = r_5m_eth.json()
                for c in eth_candles[:-1]:
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
                    self.strategy_jazz.on_candle_closed(c_obj)
                self.dashboard.add_event(f"JAZZ загружено {len(eth_candles)-1} свечей ETHUSDT 5m", "SYS")
        except Exception as e:
            self.dashboard.add_event(f"Ошибка предзагрузки: {e}", "ALERT")"""

new_preload = """    async def preload_history(self):
        \"\"\"Предзагрузка исторических данных для всех 3 стратегий через REST API Binance.\"\"\"
        self.dashboard.add_event("Предзагрузка исторических свечей (ETH & BTC)...", "SYS")
        try:
            async with httpx.AsyncClient() as client:
                # 1. ETHUSDT 1D для DLH
                url_1d = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=1d&limit=5"
                r = await client.get(url_1d, timeout=10.0)
                if r.status_code == 200:
                    data = r.json()
                    yesterday = data[-2]
                else:
                    raise Exception(f"Failed to fetch 1d candles: {r.status_code}")
                pdh = float(yesterday[2])
                pdl = float(yesterday[3])
                self.strategy_dlh.update_daily_levels(pdh, pdl)
                self.dashboard.add_event(f"DLH уровни: PDH=${pdh:.2f} | PDL=${pdl:.2f}", "SYS")

                # 2. ETHUSDT 15M для HRB
                url_15m_eth = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=15m&limit=60"
                r_15m = await client.get(url_15m_eth, timeout=10.0)
                if r_15m.status_code == 200:
                    candles_raw = r_15m.json()
                else:
                    raise Exception(f"Failed to fetch 15m candles: {r_15m.status_code}")
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
                    self.strategy_hrb.on_candle_closed(c_obj)

                # 3. BTCUSDT 5m для ARGUS (LightGBM)
                url_5m_btc = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=5m&limit=100"
                r_5m_btc = await client.get(url_5m_btc, timeout=10.0)
                if r_5m_btc.status_code == 200:
                    btc_candles = r_5m_btc.json()
                    for c in btc_candles[:-1]:
                        c_obj = {"open_time": int(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]), "close_time": int(c[6])}
                        self.strategy_argus.on_historical_candle(c_obj)
                    self.dashboard.add_event(f"ARGUS загружено {len(btc_candles)-1} свечей BTCUSDT 5m", "SYS")
                else:
                    raise Exception(f"Failed to fetch 5m BTC candles: {r_5m_btc.status_code}")

                # 4. ETHUSDT 5m для JAZZ
                url_5m_eth = "https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=5m&limit=150"
                r_5m_eth = await client.get(url_5m_eth, timeout=10.0)
                if r_5m_eth.status_code == 200:
                    eth_candles = r_5m_eth.json()
                    for c in eth_candles[:-1]:
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
                        self.strategy_jazz.on_candle_closed(c_obj)
                    self.dashboard.add_event(f"JAZZ загружено {len(eth_candles)-1} свечей ETHUSDT 5m", "SYS")
                else:
                    raise Exception(f"Failed to fetch 5m ETH candles: {r_5m_eth.status_code}")
        except Exception as e:
            self.dashboard.add_event(f"Ошибка предзагрузки: {e}", "ALERT")"""

content = content.replace(old_preload, new_preload)

with open('shadow_runner.py', 'w') as f:
    f.write(content)
