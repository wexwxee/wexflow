@echo off
setlocal
cd /d "%~dp0"
echo [1/3] Checking build deps...
".venv\Scripts\python.exe" -m pip install --quiet email_validator dnspython typer rich
echo [2/3] Building app (PyInstaller), 2-5 min...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean WexFlow_dist.spec
if errorlevel 1 ( echo Build failed. See messages above. & pause & exit /b 1 )
echo [3/3] Packing zip...
".venv\Scripts\python.exe" package_dist.py
if errorlevel 1 ( pause & exit /b 1 )
echo.
echo Done. Folder: dist\WexFlow   Zip for friend: dist\WexFlow-<version>.zip
pause
