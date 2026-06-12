@echo off
rem Zapusk WexFlow kak prilozheniya (otdelnoe okno, bez brauzera).
rem Rabotaet bez sborki .exe - cherez pythonw, bez chernoy konsoli.
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0desktop_app.py"
