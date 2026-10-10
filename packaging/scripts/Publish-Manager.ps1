param()
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dotnet = "D:\3-Ikbalzohr\Agent\appBridge\toolchain\dotnet10\dotnet.exe"
$out = Join-Path $root "packaging\publish\manager"
$status = Join-Path $root "packaging\publish\manager-status.json"
$env:DOTNET_ROOT = "D:\3-Ikbalzohr\Agent\appBridge\toolchain\dotnet10"
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = "1"
$env:DOTNET_NOLOGO = "1"

function Save-Status([string]$state,[string]$message,[int]$code){
  [pscustomobject]@{
    state=$state
    message=$message
    exit_code=$code
    updated_at=(Get-Date).ToString("o")
  } | ConvertTo-Json | Set-Content -LiteralPath $status -Encoding UTF8
}

Save-Status "RUNNING" "Publishing Zagent Manager." -1
& $dotnet publish (Join-Path $root "src\Zagent.Manager\Zagent.Manager.csproj") -c Release -r win-x64 --self-contained true -o $out "-p:PublishSingleFile=true" "-p:IncludeNativeLibrariesForSelfExtract=true" "-p:DebugType=None" "-p:DebugSymbols=false" --nologo
$code=$LASTEXITCODE
if($code -ne 0){
  Save-Status "FAILED" "dotnet publish failed." $code
  exit $code
}
Save-Status "PASS" "Zagent Manager publish complete." 0
