param(
  [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
)

$ErrorActionPreference = "Stop"

$agentRoot = "D:\3-Ikbalzohr\Agent"
$cache = Join-Path $agentRoot "appBridge\toolchain\cache"
$runtimeRoot = Join-Path $Root "packaging\runtime"
$basePython = "C:\Users\fazrin\AppData\Local\Programs\Python\Python311\python.exe"
$pythonZip = Join-Path $cache "python-3.11.9-embeddable-amd64.zip"
$nodeZip = Join-Path $cache "node-v24.19.0-win-x64.zip"
$pythonExpected = "33b448f95fecb7c6f802157dbd5e6b40a2ad9bfc8b95ca634a06ba4073ad1ac0"
$nodeExpected = "57f71ab3652e797d84acddc79c81cc9ff1c6ddb2a1974cdb83f00fee9bff4c73"

function Assert-Hash([string]$Path, [string]$Expected) {
  if (-not (Test-Path $Path)) { throw "Missing required archive: $Path" }
  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $Expected) {
    throw "SHA256 mismatch for $Path expected=$Expected actual=$actual"
  }
}

function Ensure-EmbeddedPython([string]$Name, [string]$Requirements) {
  $dest = Join-Path $runtimeRoot $Name
  if (-not (Test-Path (Join-Path $dest "python.exe"))) {
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -LiteralPath $pythonZip -DestinationPath $dest -Force
  }

  $pth = Join-Path $dest "python311._pth"
  @(
    "python311.zip"
    "."
    "Lib\site-packages"
    "import site"
  ) | Set-Content -LiteralPath $pth -Encoding ASCII

  $site = Join-Path $dest "Lib\site-packages"
  New-Item -ItemType Directory -Force -Path $site | Out-Null

  & $basePython -m pip install --disable-pip-version-check --no-warn-script-location --upgrade --target $site -r $Requirements
  if ($LASTEXITCODE -ne 0) { throw "pip target install failed for $Name" }

  & (Join-Path $dest "python.exe") -c "import sys; print(sys.version); print(sys.executable)"
  if ($LASTEXITCODE -ne 0) { throw "Portable Python smoke failed for $Name" }

  return $dest
}

Assert-Hash $pythonZip $pythonExpected
Assert-Hash $nodeZip $nodeExpected
if (-not (Test-Path $basePython)) { throw "Developer Python 3.11 base not found." }

New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null

$filesPy = Ensure-EmbeddedPython "files-python-3.11.9" (Join-Path $Root "agents\files\engine\requirements.txt")
$tunnelPy = Ensure-EmbeddedPython "tunnel-python-3.11.9" (Join-Path $Root "agents\tunnel\engine\requirements.txt")
$arcmapPy = Ensure-EmbeddedPython "arcmap-python-3.11.9" (Join-Path $Root "agents\arcmap\engine\requirements-mcp.txt")

$nodeRoot = Join-Path $runtimeRoot "node-24.19.0"
$nodeExe = Join-Path $nodeRoot "node.exe"
if (-not (Test-Path $nodeExe)) {
  $extract = Join-Path $runtimeRoot "node-extract-24.19.0"
  New-Item -ItemType Directory -Force -Path $extract | Out-Null
  Expand-Archive -LiteralPath $nodeZip -DestinationPath $extract -Force
  $inner = Get-ChildItem $extract -Directory | Select-Object -First 1
  if (-not $inner) { throw "Node archive layout invalid." }
  New-Item -ItemType Directory -Force -Path $nodeRoot | Out-Null
  Copy-Item -Path (Join-Path $inner.FullName "*") -Destination $nodeRoot -Recurse -Force
}
& $nodeExe --version
if ($LASTEXITCODE -ne 0) { throw "Portable Node smoke failed." }

$webProd = Join-Path $runtimeRoot "web-prod"
New-Item -ItemType Directory -Force -Path $webProd | Out-Null
Copy-Item -LiteralPath (Join-Path $Root "agents\web\engine\package.json") -Destination $webProd -Force
Copy-Item -LiteralPath (Join-Path $Root "agents\web\engine\package-lock.json") -Destination $webProd -Force

$npmCmd = Join-Path $nodeRoot "npm.cmd"
& $npmCmd ci --omit=dev --ignore-scripts --prefix $webProd
if ($LASTEXITCODE -ne 0) { throw "Production npm dependency install failed." }

function SizeMB([string]$Path) {
  [math]::Round(((Get-ChildItem $Path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB),2)
}

[pscustomobject]@{
  files_python_mb = SizeMB $filesPy
  tunnel_python_mb = SizeMB $tunnelPy
  arcmap_python_mb = SizeMB $arcmapPy
  node_mb = SizeMB $nodeRoot
  web_prod_modules_mb = SizeMB (Join-Path $webProd "node_modules")
  files_python = (Join-Path $filesPy "python.exe")
  tunnel_python = (Join-Path $tunnelPy "python.exe")
  arcmap_python = (Join-Path $arcmapPy "python.exe")
  node = $nodeExe
} | ConvertTo-Json
