@echo off
chcp 65001 >nul
title Проверка базы WexFlow
echo.
echo  Эта проверка НИЧЕГО не меняет — только смотрит и пишет отчёт.
echo  Запускать обычным двойным кликом (не из чата): тогда она видит
echo  ровно те же файлы, что и само приложение.
echo.
"C:\saling\.venv\Scripts\python.exe" -X utf8 "C:\saling\tools\check_real_db.py"
echo.
echo  Отчёт лежит в C:\saling\db_report.txt
echo.
pause
