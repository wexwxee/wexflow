@echo off
chcp 65001 >nul
title Откат: баннер "Дополнительные компании" (ПК)
echo.
echo  Вернуть приложение на ПК к состоянию ДО правок 01.08.2026
echo  (переезд Panduro на свой домен и мягкий разбор молчащих фирм).
echo.
echo  Твои настройки, вакансии и поданные заявки НЕ трогаются —
echo  они лежат отдельно, в папке данных WexFlow.
echo.
set /p ok="Откатить? (y/n): "
if /I not "%ok%"=="y" goto :end
cd /d C:\saling
git reset --hard connector-banner-base-20260801
echo.
echo  Готово. Закрой WexFlow и запусти заново.
:end
pause
