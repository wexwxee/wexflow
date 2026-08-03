@echo off
chcp 65001 >nul
title Откат: мультивыбор Lidl и версия 1.3.80 (ПК)
echo.
echo  Вернуть приложение на ПК к состоянию ДО правок 03.08.2026
echo  (у карточек Lidl не будет флажка пакетного выбора, версия 1.3.80).
echo.
echo  Твои настройки, вакансии и поданные заявки НЕ трогаются —
echo  они лежат отдельно, в папке данных WexFlow.
echo.
set /p ok="Откатить? (y/n): "
if /I not "%ok%"=="y" goto :end
cd /d C:\saling
git reset --hard lidl-multivybor-base-20260803
echo.
echo  Готово. Закрой WexFlow и запусти заново.
:end
pause
