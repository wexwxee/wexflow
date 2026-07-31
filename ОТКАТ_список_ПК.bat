@echo off
chcp 65001 >nul
title Откат: список вакансий в телефоне (ПК)
echo.
echo  Вернуть приложение на ПК к состоянию ДО правок 26.07.2026
echo  (список вакансий для телефона стал как в приложении).
echo.
echo  Твои настройки, вакансии и поданные заявки НЕ трогаются —
echo  они лежат отдельно, в папке данных WexFlow.
echo.
set /p ok="Откатить? (y/n): "
if /I not "%ok%"=="y" goto :end
cd /d C:\saling
git reset --hard jobs-parity-base-20260726
echo.
echo  Готово. Закрой WexFlow и запусти заново.
:end
pause
