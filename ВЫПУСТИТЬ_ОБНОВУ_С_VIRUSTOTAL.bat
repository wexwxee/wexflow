@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\release_after_virustotal.py
pause
