@echo off
chcp 65001 >nul
title [SHADOW ENGINE] ТЕНЕВОЙ ПОЛИГОН (ETHUSDT + BTCUSDT)
cls

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set PYTHON_EXE=D:\Telegram bot for Jarvis\venv\Scripts\python.exe

echo ======================================================================
echo    SHADOW ENGINE - ТЕНЕВОЙ МУЛЬТИСТРАТЕГИЧЕСКИЙ ПОЛИГОН V4.0
echo    Модели: DLH (ETH), HRB (ETH), ARGUS (BTC), JAZZ (ETH)
echo ======================================================================
echo.

cd /d "%~dp0"
"%PYTHON_EXE%" "%~dp0shadow_runner.py"

echo.
echo Полигон остановлен. Нажмите любую клавишу для выхода...
pause >nul
