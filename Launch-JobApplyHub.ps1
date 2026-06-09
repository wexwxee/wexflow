$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logPath = Join-Path $root "hub_launcher.log"

function Write-HubLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-LocalWeb {
    param([int]$Port)
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Wait-LocalWeb {
    param(
        [int]$Port,
        [int]$Seconds = 25
    )

    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-LocalWeb -Port $Port) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    return $false
}

function Start-AppIfNeeded {
    param(
        [string]$Name,
        [int]$Port,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory
    )

    if (Test-LocalWeb -Port $Port) {
        Write-HubLog "$Name already running on port $Port."
        return $true
    }

    Write-HubLog "Starting $Name on port $Port."
    Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -WindowStyle Minimized

    if (Wait-LocalWeb -Port $Port) {
        Write-HubLog "$Name is ready on port $Port."
        return $true
    }

    Write-HubLog "$Name did not answer on port $Port before timeout."
    return $false
}

try {
    Write-HubLog "Launcher started."

    # приоритет: .venv проекта -> системный Python 3.11 -> python из PATH
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    $defaultPython = "C:\Users\ivanm\AppData\Local\Programs\Python\Python311\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $python = $venvPython
    } elseif (Test-Path -LiteralPath $defaultPython) {
        $python = $defaultPython
    } else {
        $python = "python"
    }

    $sevenRoot = "C:\seven11-apply"
    $sevenPython = Join-Path $sevenRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $sevenPython)) {
        $sevenPython = $python
    }

    $sevenOk = $false
    if (Test-Path -LiteralPath $sevenRoot) {
        $sevenOk = Start-AppIfNeeded -Name "7-Eleven" -Port 7111 -FilePath $sevenPython -ArgumentList @("web_app.py") -WorkingDirectory $sevenRoot
    } else {
        Write-HubLog "7-Eleven folder was not found: $sevenRoot"
    }

    $sallingOk = Start-AppIfNeeded -Name "Salling" -Port 8000 -FilePath $python -ArgumentList @("-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000") -WorkingDirectory $root

    # единый адрес (reverse-proxy) на 8080 — нужен поднятый Salling
    $hubOk = $false
    if ($sallingOk) {
        $hubOk = Start-AppIfNeeded -Name "Hub" -Port 8080 -FilePath $python -ArgumentList @("-m", "uvicorn", "hub:app", "--host", "127.0.0.1", "--port", "8080") -WorkingDirectory $root
    }

    if ($hubOk) {
        Start-Process "http://127.0.0.1:8080/"
    } elseif ($sallingOk) {
        Start-Process "http://127.0.0.1:8000/"
    } elseif ($sevenOk) {
        Start-Process "http://127.0.0.1:7111/"
    } else {
        throw "Neither app answered after startup."
    }

    Write-HubLog "Launcher finished."
    exit 0
} catch {
    Write-HubLog ("Launcher failed: " + $_.Exception.Message)
    Write-Host "Job Apply Hub failed. Details are in $logPath"
    Write-Host $_.Exception.Message
    exit 1
}
