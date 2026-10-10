param(
    [switch]$SkipWeb
)

$ErrorActionPreference = "Stop"
$DevRoot = (Resolve-Path $PSScriptRoot).Path
$Root = (Resolve-Path (Join-Path $DevRoot "..")).Path
$DotnetRoot = "D:\3-Ikbalzohr\Agent\appBridge\toolchain\dotnet10"
$Dotnet = Join-Path $DotnetRoot "dotnet.exe"

if (-not (Test-Path -LiteralPath (Join-Path $DevRoot "runtime\files-python\python.exe"))) {
    & (Join-Path $DevRoot "Prepare-ZagentDevRuntime.ps1")
}

$env:DOTNET_ROOT = $DotnetRoot
$env:PATH = $DotnetRoot + ";" + $env:PATH
$env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = "1"

& $Dotnet build (Join-Path $Root "src\Zagent.Core\Zagent.Core.csproj") -c Debug --nologo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Dotnet build (Join-Path $Root "src\Zagent.Manager\Zagent.Manager.csproj") -c Debug --nologo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not $SkipWeb) {
    $node = Join-Path $DevRoot "runtime\web\node\node.exe"
    $tsc = Join-Path $Root "agents\web\engine\node_modules\typescript\bin\tsc"
    Push-Location (Join-Path $Root "agents\web\engine")
    try {
        & $node $tsc -p tsconfig.json
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    finally {
        Pop-Location
    }
}

Write-Output "ZAGENT_DEV_BUILD_PASS"
