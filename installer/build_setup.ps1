param(
  [Parameter(Mandatory=$true)]
  [string]$Version
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Dist = Join-Path $Root "dist"
$Zip = Join-Path $Dist "WexFlow-$Version.zip"
$Work = Join-Path $Dist "setup_work"
$Out = Join-Path $Dist "WexFlow-Setup-$Version.exe"
$Template = Join-Path $PSScriptRoot "install_wexflow.cmd"
$InstallCmd = Join-Path $Work "install_wexflow.cmd"
$Sed = Join-Path $Work "wexflow_setup.sed"

if (!(Test-Path $Zip)) {
  throw "Missing $Zip. Build/package the app first."
}

if (Test-Path $Work) {
  Remove-Item -LiteralPath $Work -Recurse -Force
}
New-Item -ItemType Directory -Path $Work | Out-Null
Copy-Item -LiteralPath $Zip -Destination (Join-Path $Work (Split-Path -Leaf $Zip)) -Force

(Get-Content -LiteralPath $Template -Raw).Replace("__VERSION__", $Version) |
  Set-Content -LiteralPath $InstallCmd -Encoding ASCII

if (Test-Path $Out) {
  Remove-Item -LiteralPath $Out -Force
}

$zipName = Split-Path -Leaf $Zip

@"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=1
HideExtractAnimation=1
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles
[SourceFiles]
SourceFiles0=$Work
[SourceFiles0]
%FILE0%=
%FILE1%=
[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=WexFlow installed.
TargetName="$Out"
FriendlyName="WexFlow Setup"
AppLaunched="cmd /c install_wexflow.cmd"
PostInstallCmd="<None>"
AdminQuietInstCmd=
UserQuietInstCmd=
FILE0="install_wexflow.cmd"
FILE1="$zipName"
"@ | Set-Content -LiteralPath $Sed -Encoding ASCII

& iexpress.exe /N /Q $Sed
$deadline = (Get-Date).AddMinutes(10)
while (!(Test-Path $Out) -and (Get-Date) -lt $deadline) {
  Start-Sleep -Seconds 2
}
if (!(Test-Path $Out)) {
  throw "IExpress did not create $Out"
}

Write-Host "Created $Out"
