@echo off
chcp 65001 >nul
title Восстановление базы WexFlow
echo.
echo  База вакансий повреждена (SQLite: "database disk image is malformed"),
echo  из-за этого приложение не запускается.
echo.
echo  Что сделает восстановление:
echo    - битую базу уберёт в сторону (НЕ удалит),
echo    - поставит последний целый бэкап,
echo    - вернёт отметки "подано" по скринам-квитанциям (их 30),
echo    - список вакансий приложение наполнит само при первом поиске.
echo.
echo  Твои настройки, документы, фильтры и ответы анкет НЕ трогаются.
echo.
set /p ok="Восстановить? (y/n): "
if /I not "%ok%"=="y" goto :end
taskkill /IM WexFlow.exe /F >nul 2>&1
timeout /t 3 /nobreak >nul
"C:\saling\.venv\Scripts\python.exe" -X utf8 "C:\saling\tools\restore_jobs_db.py" --apply
echo.
echo  Запускаю WexFlow...
start "" "C:\Users\Public\WexFlow\WexFlow.exe"
:end
pause
