@echo off
rem WexFlow - Podacha (BETA). Otdelnyy modul, staroe (Salling/7-Eleven) ne trogaet.
rem Zapuskaet lokalnyy server i sam otkryvaet brauzer. Zakroy okno - beta ostanovitsya.
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title WexFlow - Podacha (BETA)
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0tools\teamtailor_app.py"
echo.
echo (beta ostanovlena) Nazhmi lyubuyu klavishu chtoby zakryt okno.
pause >nul
