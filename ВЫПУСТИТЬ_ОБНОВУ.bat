@echo off
setlocal
cd /d "%~dp0"
echo Building fresh distributive (2-5 min)...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean WexFlow_dist.spec
if errorlevel 1 ( echo Build failed. & pause & exit /b 1 )
".venv\Scripts\python.exe" package_dist.py
if errorlevel 1 ( pause & exit /b 1 )
echo.
".venv\Scripts\python.exe" publish_release.py
pause
