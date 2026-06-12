@echo off
rem ============================================================
rem  Sborka nastolnogo prilozheniya WexFlow.exe (odin fayl).
rem  Posle sborki WexFlow.exe poyavitsya v korne C:\saling.
rem  Zapuskat dvoynym klikom po WexFlow.exe.
rem ============================================================
cd /d "%~dp0"
echo Sobirayu WexFlow.exe, eto zaymet 1-3 minuty...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --onefile --name WexFlow --icon app.ico desktop_app.py
if errorlevel 1 (
  echo.
  echo Oshibka sborki. Smotri soobshcheniya vyshe.
  pause
  exit /b 1
)
copy /Y "dist\WexFlow.exe" "WexFlow.exe" >nul
echo.
echo ================================================
echo Gotovo: WexFlow.exe lezhit v C:\saling
echo Zapuskay dvoynym klikom po WexFlow.exe
echo ================================================
pause
