@echo off
chcp 65001 >nul
title Откат: полная подача Lidl и защита от изменений сайта (ПК)
echo.
echo  Вернуть приложение на ПК к состоянию ДО правок 31.07.2026 (вечер):
echo   - полная подача Lidl (ответы для анкет + автоматическая Ansøg);
echo   - защита от изменений формы у Lidl и Salling Group;
echo   - раздел "Ответы для анкет" в профиле.
echo.
echo  Твои настройки, вакансии и поданные заявки НЕ трогаются —
echo  они лежат отдельно, в папке данных WexFlow.
echo.
set /p ok="Откатить? (y/n): "
if /I not "%ok%"=="y" goto :end
cd /d C:\saling
git reset --hard lidl-full-base-20260731
echo.
echo  Готово. Закрой WexFlow и запусти заново.
:end
pause
