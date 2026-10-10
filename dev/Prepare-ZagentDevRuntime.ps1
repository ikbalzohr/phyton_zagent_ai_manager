param()

$ErrorActionPreference = "Stop"
$DevRoot = (Resolve-Path $PSScriptRoot).Path
$Root = (Resolve-Path (Join-Path $DevRoot "..")).Path
$RuntimeRoot = Join-Path $DevRoot "runtime"
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

function New-PythonDevRuntime {
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$SourceRuntime,
        [Parameter(Mandatory=$true)][string]$EngineRoot
    )

    $target = Join-Path $RuntimeRoot $Name
    New-Item -ItemType Directory -Force -Path $target | Out-Null

    Get-ChildItem -LiteralPath $SourceRuntime -File | ForEach-Object {
        if ($_.Name -ne "python311._pth") {
            $destination = Join-Path $target $_.Name
            if (-not (Test-Path -LiteralPath $destination)) {
                New-Item -ItemType HardLink -Path $destination -Target $_.FullName | Out-Null
            }
        }
    }

    $libTarget = Join-Path $target "Lib"
    if (-not (Test-Path -LiteralPath $libTarget)) {
        New-Item -ItemType Junction -Path $libTarget -Target (Join-Path $SourceRuntime "Lib") | Out-Null
    }

    @(
        "python311.zip",
        ".",
        "Lib\site-packages",
        [IO.Path]::GetFullPath($EngineRoot),
        "import site"
    ) | Set-Content -LiteralPath (Join-Path $target "python311._pth") -Encoding ASCII
}

New-PythonDevRuntime -Name "files-python" -SourceRuntime (Join-Path $Root "packaging\runtime\files-python-3.11.9") -EngineRoot (Join-Path $Root "agents\files\engine")
New-PythonDevRuntime -Name "tunnel-python" -SourceRuntime (Join-Path $Root "packaging\runtime\tunnel-python-3.11.9") -EngineRoot (Join-Path $Root "agents\tunnel\engine")
New-PythonDevRuntime -Name "arcmap-python" -SourceRuntime (Join-Path $Root "packaging\runtime\arcmap-python-3.11.9") -EngineRoot (Join-Path $Root "agents\arcmap\engine")

$nodeSource = Join-Path $Root "packaging\runtime\node-24.19.0"
$nodeTarget = Join-Path $RuntimeRoot "web\node"
New-Item -ItemType Directory -Force -Path $nodeTarget | Out-Null
Get-ChildItem -LiteralPath $nodeSource -File | ForEach-Object {
    $destination = Join-Path $nodeTarget $_.Name
    if (-not (Test-Path -LiteralPath $destination)) {
        New-Item -ItemType HardLink -Path $destination -Target $_.FullName | Out-Null
    }
}

$browserTarget = Join-Path $RuntimeRoot "web\browsers"
if (-not (Test-Path -LiteralPath $browserTarget)) {
    New-Item -ItemType Junction -Path $browserTarget -Target (Join-Path $Root "packaging\runtime\playwright-browsers-1.63.0-final") | Out-Null
}

New-Item -ItemType Directory -Force -Path (Join-Path $DevRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DevRoot "workspace") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DevRoot "state") | Out-Null
Write-Output "ZAGENT_DEV_RUNTIME_READY root=$RuntimeRoot"
