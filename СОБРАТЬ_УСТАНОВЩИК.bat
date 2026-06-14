@echo off
setlocal
cd /d "%~dp0"
echo Building WexFlow-Setup.exe (small web installer for friends)...
".venv\Scripts\python.exe" -m PyInstaller --onefile --windowed --noconfirm --name WexFlow-Setup --icon app.ico --add-data "installer\assets;assets" --distpath dist --workpath build_installer installer\installer.py
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )
echo.
echo Done: dist\WexFlow-Setup.exe
echo Share this single file with friends. It downloads and installs the latest WexFlow.
pause
