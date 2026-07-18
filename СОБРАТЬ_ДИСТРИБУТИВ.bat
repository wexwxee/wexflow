@echo off
setlocal
cd /d "%~dp0"
echo [1/4] Checking build deps...
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt email_validator dnspython typer rich
if errorlevel 1 ( echo Dependency setup failed. & pause & exit /b 1 )
echo [2/4] Running quality checks...
".venv\Scripts\python.exe" tools\run_quality_checks.py
if errorlevel 1 ( echo Quality checks failed. Build stopped. & pause & exit /b 1 )
echo [3/4] Building app (PyInstaller), 2-5 min...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean WexFlow_dist.spec
if errorlevel 1 ( echo Build failed. See messages above. & pause & exit /b 1 )
echo [4/4] Packing zip...
".venv\Scripts\python.exe" package_dist.py
if errorlevel 1 ( pause & exit /b 1 )
echo.
echo Done. Folder: dist\WexFlow   Zip for friend: dist\WexFlow-<version>.zip
pause
