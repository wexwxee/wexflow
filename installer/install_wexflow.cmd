@echo off
setlocal EnableExtensions

set "APP_NAME=WexFlow"
set "VERSION=__VERSION__"
set "ZIP_NAME=WexFlow-__VERSION__.zip"
rem Install to an ASCII, no-admin, always-writable path. Avoids the Cyrillic-path
rem pythonnet failures users hit when extracting zips into folders like "Novaya papka".
set "INSTALL_ROOT=%PUBLIC%"
set "APP_DIR=%INSTALL_ROOT%\WexFlow"
set "EXE=%APP_DIR%\WexFlow.exe"
set "LOG=%TEMP%\WexFlow-Setup.log"

echo WexFlow setup %VERSION% > "%LOG%"
echo Installing to %APP_DIR% >> "%LOG%"

where powershell.exe >nul 2>nul
if errorlevel 1 ( echo PowerShell is required to install WexFlow. & pause & exit /b 1 )

echo Installing WexFlow %VERSION%...
echo Checking Windows components (.NET, WebView2)...
call :ensure_dotnet48
call :ensure_webview2

echo Unpacking application...
if exist "%APP_DIR%" rmdir /s /q "%APP_DIR%" >>"%LOG%" 2>>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '%~dp0%ZIP_NAME%' -DestinationPath '%INSTALL_ROOT%' -Force" >>"%LOG%" 2>>&1
rem Strip mark-of-the-web so .NET can load the unsigned Python.Runtime.dll.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%APP_DIR%' -Recurse -File | Unblock-File -ErrorAction SilentlyContinue" >>"%LOG%" 2>>&1

if not exist "%EXE%" ( echo WexFlow.exe not found after unpacking. See %LOG% & pause & exit /b 1 )

echo Creating desktop shortcut...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\WexFlow.lnk'); $s.TargetPath='%EXE%'; $s.WorkingDirectory='%APP_DIR%'; $s.IconLocation='%EXE%'; $s.Save()" >>"%LOG%" 2>>&1

echo Starting WexFlow...
start "" "%EXE%"
exit /b 0

:ensure_dotnet48
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$r=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -ErrorAction SilentlyContinue).Release; if ($r -ge 528040) { exit 0 } exit 1" >>"%LOG%" 2>>&1
if not errorlevel 1 exit /b 0
echo   - Installing .NET Framework 4.8 (one-time)...
set "NET48_EXE=%TEMP%\ndp48-web.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/?linkid=2088631' -OutFile '%NET48_EXE%'" >>"%LOG%" 2>>&1
if exist "%NET48_EXE%" "%NET48_EXE%" /q /norestart >>"%LOG%" 2>>&1
exit /b 0

:ensure_webview2
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$keys=@('HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}','HKLM:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}','HKLM:\Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'); foreach ($k in $keys) { $pv=(Get-ItemProperty $k -ErrorAction SilentlyContinue).pv; if ($pv) { exit 0 } }; exit 1" >>"%LOG%" 2>>&1
if not errorlevel 1 exit /b 0
echo   - Installing Microsoft Edge WebView2 Runtime (one-time)...
set "WV2_EXE=%TEMP%\MicrosoftEdgeWebView2Setup.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile '%WV2_EXE%'" >>"%LOG%" 2>>&1
if exist "%WV2_EXE%" "%WV2_EXE%" /silent /install >>"%LOG%" 2>>&1
exit /b 0
