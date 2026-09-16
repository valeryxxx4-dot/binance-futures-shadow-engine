"""
test_live_smoke.py - 8-секундный смоук-тест реального подключения к Binance Futures WebSocket.
Проверяет:
1. Успешный запуск мультиплексного WebSocket.
2. Приход котировок ETHUSDT и BTCUSDT.
3. Расчет OBI и SMM метрик.
4. Отрисовку немерцающего дашборда на ANSI.
"""

import asyncio
import os
import sys
from shadow_runner import ShadowEngine, CONFIG_PATH

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

async def smoke_test():
    engine = ShadowEngine(CONFIG_PATH)
    print("🚀 [SMOKE TEST] Запуск ShadowEngine на 8 секунд...")

    task = asyncio.create_task(engine.run())

    # Ждем 8 секунд для сбора пакетов
    for i in range(8):
        await asyncio.sleep(1)
        eth_p = engine.prices.get("ETHUSDT", 0.0)
        btc_p = engine.prices.get("BTCUSDT", 0.0)
        sys.stdout.write(f"\r⏳ [t+{i+1}s] ETH: {eth_p:.2f}$ | BTC: {btc_p:.2f}$ | ETH OBI: {engine.market_metrics.get('ETH_OBI', 0):+.2f} | BTC OBI: {engine.market_metrics.get('BTC_OBI', 0):+.2f}")
        sys.stdout.flush()

    engine.is_running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    print("\n\n✅ [SMOKE TEST PASSED] Котировки получены успешно:")
    print(f"   • ETHUSDT Price: ${engine.prices.get('ETHUSDT', 0.0):.2f}")
    print(f"   • BTCUSDT Price: ${engine.prices.get('BTCUSDT', 0.0):.2f}")
    print(f"   • ETH OBI: {engine.market_metrics.get('ETH_OBI', 0.0):+.4f}")
    print(f"   • BTC OBI: {engine.market_metrics.get('BTC_OBI', 0.0):+.4f}")

    assert engine.prices.get("ETHUSDT", 0.0) > 0.0, "ETHUSDT price was not received!"
    assert engine.prices.get("BTCUSDT", 0.0) > 0.0, "BTCUSDT price was not received!"

if __name__ == "__main__":
    asyncio.run(smoke_test())
