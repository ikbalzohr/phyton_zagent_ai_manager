param([switch]$NoBuild)

$ErrorActionPreference = "Stop"
$Canonical = Join-Path $PSScriptRoot "Start-Zagent-Dev.ps1"

if (-not (Test-Path -LiteralPath $Canonical)) {
    throw "Canonical DEV launcher not found: $Canonical"
}

if ($NoBuild) {
    & $Canonical -NoBuild
} else {
    & $Canonical
}

exit $LASTEXITCODE
