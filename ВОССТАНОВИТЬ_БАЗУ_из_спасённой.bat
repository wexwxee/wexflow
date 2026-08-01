@echo off
chcp 65001 >nul
title Починка базы WexFlow (убрать несовместимый журнал)
set DATA=%APPDATA%\WexFlow\salling
set SPARE=C:\saling\_dbrescue\jobs_salvaged.db
echo.
echo  Разбор показал: сама база ЦЕЛАЯ.
echo    - вакансий 5787, заявок 1231, данные до 01.08 02:58;
echo    - ломает её только журнал jobs.db-wal, который разошёлся с базой.
echo.
echo  Что сделает эта кнопка:
echo    - закроет WexFlow,
echo    - уберёт в сторону только журнал (jobs.db-wal и jobs.db-shm),
echo    - саму базу НЕ ТРОГАЕТ — ничего не теряется,
echo    - запустит версию 1.3.79 из C:\saling\dist.
echo.
echo  Если вдруг не поможет, рядом лежит запасная спасённая копия:
echo  %SPARE%
echo.
set /p ok="Починить? (y/n): "
if /I not "%ok%"=="y" goto :end
taskkill /F /IM WexFlow.exe >nul 2>&1
timeout /t 3 /nobreak >nul
if exist "%DATA%\jobs.db-wal" move /Y "%DATA%\jobs.db-wal" "%DATA%\_badwal_20260801_jobs.db-wal" >nul
if exist "%DATA%\jobs.db-shm" move /Y "%DATA%\jobs.db-shm" "%DATA%\_badwal_20260801_jobs.db-shm" >nul
echo.
echo  Журнал убран. Запускаю WexFlow 1.3.79...
start "" "C:\saling\dist\WexFlow\WexFlow.exe"
:end
pause
