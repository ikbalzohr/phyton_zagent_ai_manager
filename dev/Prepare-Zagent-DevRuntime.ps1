param()

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeRoot = Join-Path $PSScriptRoot "runtime"
New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null

function Test-JunctionTarget([string]$Path,[string]$ExpectedTarget) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.LinkType -ne "Junction") { return $false }
    $actual = [System.IO.Path]::GetFullPath([string]$item.Target).TrimEnd("\")
    $expected = [System.IO.Path]::GetFullPath($ExpectedTarget).TrimEnd("\")
    return $actual.Equals($expected,[StringComparison]::OrdinalIgnoreCase)
}

function Ensure-PythonOverlay([string]$Name,[string]$SourceRuntime,[string]$EngineRoot) {
    $target = Join-Path $RuntimeRoot $Name
    $source = (Resolve-Path $SourceRuntime).Path
    $engine = (Resolve-Path $EngineRoot).Path
    $pth = Join-Path $target "python311._pth"

    $overlayValid = (Test-Path (Join-Path $target "python.exe")) -and (Test-Path $pth)
    if ($overlayValid) {
        $content = Get-Content $pth -Raw
        $overlayValid = $content.IndexOf($engine,[StringComparison]::OrdinalIgnoreCase) -ge 0
    }
    if ($overlayValid) {
        foreach ($sourceDir in @(Get-ChildItem -LiteralPath $source -Directory)) {
            if (-not (Test-JunctionTarget (Join-Path $target $sourceDir.Name) $sourceDir.FullName)) {
                $overlayValid = $false
                break
            }
        }
    }
    if ($overlayValid) {
        return (Join-Path $target "python.exe")
    }

    Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Get-ChildItem -LiteralPath $source -Directory | ForEach-Object {
        New-Item -ItemType Junction -Path (Join-Path $target $_.Name) -Target $_.FullName | Out-Null
    }
    Get-ChildItem -LiteralPath $source -File | ForEach-Object {
        if ($_.Name -ine "python311._pth") {
            New-Item -ItemType HardLink -Path (Join-Path $target $_.Name) -Target $_.FullName | Out-Null
        }
    }
    @("python311.zip",".","Lib\site-packages",$engine,"import site") | Set-Content -LiteralPath $pth -Encoding ASCII
    return (Join-Path $target "python.exe")
}

$filesPython = Ensure-PythonOverlay "files-python-3.11.9" (Join-Path $Root "packaging\runtime\files-python-3.11.9") (Join-Path $Root "agents\files\engine")
$tunnelPython = Ensure-PythonOverlay "tunnel-python-3.11.9" (Join-Path $Root "packaging\runtime\tunnel-python-3.11.9") (Join-Path $Root "agents\tunnel\engine")
$arcMapPython = Ensure-PythonOverlay "arcmap-python-3.11.9" (Join-Path $Root "packaging\runtime\arcmap-python-3.11.9") (Join-Path $Root "agents\arcmap\engine")

$webRoot = Join-Path $RuntimeRoot "web"
$webNodeDir = Join-Path $webRoot "node"
$webBrowsers = Join-Path $webRoot "browsers"
$nodeSource = Join-Path $Root "packaging\runtime\node-24.19.0"
$browserSource = Join-Path $Root "packaging\runtime\playwright-browsers-1.63.0-final"
$webModulesSource = Join-Path $Root "packaging\runtime\web-prod\node_modules"
$webEngineModules = Join-Path $Root "agents\web\engine\node_modules"

New-Item -ItemType Directory -Force -Path $webNodeDir | Out-Null
foreach ($name in @("node.exe","LICENSE")) {
    $src = Join-Path $nodeSource $name
    $dst = Join-Path $webNodeDir $name
    if (-not (Test-Path $dst)) {
        New-Item -ItemType HardLink -Path $dst -Target $src | Out-Null
    }
}

if (-not (Test-JunctionTarget $webBrowsers $browserSource)) {
    Remove-Item $webBrowsers -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Junction -Path $webBrowsers -Target $browserSource | Out-Null
}

if (-not (Test-Path -LiteralPath $webModulesSource)) {
    throw "Web production dependency runtime is missing: $webModulesSource"
}
if (Test-Path -LiteralPath $webEngineModules) {
    $webModulesItem = Get-Item -LiteralPath $webEngineModules -Force
    if ($webModulesItem.LinkType -eq "Junction") {
        if (-not (Test-JunctionTarget $webEngineModules $webModulesSource)) {
            Remove-Item $webEngineModules -Recurse -Force
            New-Item -ItemType Junction -Path $webEngineModules -Target $webModulesSource | Out-Null
        }
    }
    elseif (-not (Test-Path -LiteralPath (Join-Path $webEngineModules "@modelcontextprotocol\node"))) {
        throw "Existing Web node_modules is incomplete and is not a managed DEV junction: $webEngineModules"
    }
}
else {
    New-Item -ItemType Junction -Path $webEngineModules -Target $webModulesSource | Out-Null
}

& $filesPython -c "import mcp" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Files DEV Python runtime dependency smoke failed." }

$webNode = Join-Path $webNodeDir "node.exe"
$webEngineRoot = Join-Path $Root "agents\web\engine"
Push-Location $webEngineRoot
try {
    & $webNode -e "require.resolve('@modelcontextprotocol/node',{paths:[process.cwd()]})" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Web DEV Node runtime dependency smoke failed." }
}
finally {
    Pop-Location
}

[pscustomobject]@{files_python=$filesPython;tunnel_python=$tunnelPython;arcmap_python=$arcMapPython;web_node=$webNode} | ConvertTo-Json -Compress