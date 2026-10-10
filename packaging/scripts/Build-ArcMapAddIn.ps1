param([string]$OutputRoot)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$source = Join-Path $root 'agents\arcmap\addin'
if (-not $OutputRoot) { $OutputRoot = Join-Path $root 'agents\arcmap\artifacts' }
New-Item -ItemType Directory -Force $OutputRoot | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$target = Join-Path $OutputRoot 'Zagent.ArcMap.esriaddin'
$temp = $target + '.tmp'
if (Test-Path $temp) { Remove-Item $temp -Force }
[IO.Compression.ZipFile]::CreateFromDirectory($source, $temp)
Move-Item $temp $target -Force
Get-FileHash $target -Algorithm SHA256 | Select-Object Path,Hash
