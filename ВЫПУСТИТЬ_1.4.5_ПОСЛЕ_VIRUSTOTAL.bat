@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================================
echo   Выпуск WexFlow 1.4.5 — остался ОДИН твой шаг.
echo.
echo   Всё собрано и проверено: dist\WexFlow-1.4.5.zip и
echo   dist\WexFlow-Setup.exe. Код уже в GitHub (ветка redesign).
echo   Пересобирать НИЧЕГО не нужно.
echo.
echo   Осталось: проверить установщик на VirusTotal и вставить
echo   сюда ссылку на отчёт. Без неё релиз не публикуется —
echo   это защита, чтобы в заметках не было пустых обещаний.
echo ============================================================
echo.
echo Сейчас откроется сайт VirusTotal и папка dist.
echo Перетащи на сайт файл WexFlow-Setup.exe, дождись отчёта
echo и скопируй адрес страницы из браузера.
echo.
pause
start "" "https://www.virustotal.com/gui/home/upload"
start "" "%~dp0dist"
echo.
set /p VT=Вставь ссылку на отчёт VirusTotal и нажми Enter:
if "%VT%"=="" ( echo Ссылка пустая. Ничего не менял. & pause & exit /b 1 )
> "dist\WexFlow-Setup.exe.virustotal.txt" echo %VT%
echo.
echo Ссылка сохранена. Проверяю её и публикую релиз...
echo (если ссылка окажется от другого файла, публикация остановится)
echo.
".venv\Scripts\python.exe" publish_release.py
echo.
pause
