@echo off
chcp 65001 >nul
title Запуск WexFlow
echo.
echo  Запускаю WexFlow (версия 1.3.59).
echo.
echo  ВАЖНО: приложение нужно запускать именно так — ярлыком или этим файлом
echo  из Проводника. Если его запустить из моей рабочей оболочки, оно попадает
echo  в песочницу и НЕ МОЖЕТ сохранять настройки и базу в %%AppData%%.
echo.
taskkill /IM WexFlow.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul
start "" "C:\Users\Public\WexFlow\WexFlow.exe"
echo  Готово. Окно WexFlow откроется через несколько секунд.
timeout /t 4 /nobreak >nul
