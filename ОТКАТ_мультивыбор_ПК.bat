@echo off
chcp 65001 >nul
title Откат: иконка автобуса и версия 1.3.56 (ПК)
echo.
echo  Вернуть приложение на ПК к состоянию ДО правок 31.07.2026
echo  (своя иконка автобуса в бейдже "время в пути", версия 1.3.56).
echo.
echo  Твои настройки, вакансии и поданные заявки НЕ трогаются —
echo  они лежат отдельно, в папке данных WexFlow.
echo.
set /p ok="Откатить? (y/n): "
if /I not "%ok%"=="y" goto :end
cd /d C:\saling
git reset --hard mnogovybor-base-20260731
echo.
echo  Готово. Закрой WexFlow и запусти заново.
:end
pause
