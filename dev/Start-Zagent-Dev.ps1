param([switch]$NoBuild)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$StateRoot = Join-Path $PSScriptRoot "state"
$DataRoot = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $StateRoot,$DataRoot | Out-Null
$RuntimeInfo = (& (Join-Path $PSScriptRoot "Prepare-Zagent-DevRuntime.ps1")) | ConvertFrom-Json
$Toolchain = Get-Content (Join-Path $Root "toolchain.json") -Raw | ConvertFrom-Json
$Dotnet = $Toolchain.dotnet.executable
$env:DOTNET_ROOT = $Toolchain.dotnet.root
$env:PATH = $env:DOTNET_ROOT + ";" + $env:PATH
if (-not $NoBuild) { & $Dotnet build (Join-Path $Root "Zagent.slnx") -c Debug --no-restore; if ($LASTEXITCODE -ne 0) { throw "DEV build failed." } }

$managerExe = Join-Path $Root "src\Zagent.Manager\bin\Debug\net10.0-windows\Zagent.Manager.exe"
$coreExe = Join-Path $Root "src\Zagent.Core\bin\Debug\net10.0\Zagent.Core.exe"
if (-not (Test-Path $managerExe)) { throw "Manager DEV executable not found." }
if (-not (Test-Path $coreExe)) { throw "Core DEV executable not found." }

$existingDevManagers = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -eq "Zagent.Manager.exe" -and $_.ExecutablePath -eq $managerExe })
if ($existingDevManagers.Count -gt 0) { throw ("Zagent DEV Manager is already running with PID(s): " + (($existingDevManagers.ProcessId) -join ", ")) }

$pidFile = Join-Path $StateRoot "manager.pid"
if (Test-Path $pidFile) {
    try { $oldPid=[int](Get-Content $pidFile -Raw); if (Get-Process -Id $oldPid -ErrorAction SilentlyContinue) { throw "Zagent DEV already running with PID $oldPid." } } catch { if ($_.Exception.Message -like "Zagent DEV already*") { throw } }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

$filesProfile = $Root
$cur = Get-Item $Root
while ($null -ne $cur) { if ($cur.Name -ieq "appBridge") { $filesProfile=$cur.FullName; break }; $cur=$cur.Parent }

$arcMapDataRoot = Join-Path $DataRoot "arcmap"
$arcMapConfigDir = Join-Path $arcMapDataRoot "config"
$arcMapIpcRoot = Join-Path $arcMapDataRoot "ipc"
$arcMapBridgeDir = Join-Path $Root "agents\arcmap\engine\arcmap_bridge"
$arcMapBridgeLocator = Join-Path $arcMapBridgeDir "bridge_runtime.json"

New-Item -ItemType Directory -Force -Path $arcMapDataRoot,$arcMapConfigDir,$arcMapIpcRoot | Out-Null

$arcMapLocatorJson = [ordered]@{
    schema_version = "1.0"
    environment = "DEVELOPMENT"
    data_root = $arcMapDataRoot
    config_dir = $arcMapConfigDir
    ipc_root = $arcMapIpcRoot
    generated_by = "Start-Zagent-Dev.ps1"
} | ConvertTo-Json -Depth 3

[System.IO.File]::WriteAllText(
    $arcMapBridgeLocator,
    $arcMapLocatorJson,
    [System.Text.UTF8Encoding]::new($false))

# DEV must never create a second ArcMap UI-thread bridge owner while
# Production already owns ArcMap. Instead, DEV may observe the same ArcMap
# instance through the Production IPC channel in explicit read-only mode.
$agentRoot = (Resolve-Path (Join-Path $Root "..\..\..\..")).Path
$productionArcMapLocator = Join-Path $agentRoot "ZohrSystem\Zagent\agents\arcmap\engine\arcmap_bridge\bridge_runtime.json"
$env:ZAGENT_ARCMAP_UPSTREAM_DATA_ROOT = $null
$env:ZAGENT_ARCMAP_UPSTREAM_IPC_ROOT = $null
$env:ZAGENT_ARCMAP_UPSTREAM_MODE = $null
if (Test-Path $productionArcMapLocator) {
    try {
        $productionArcMap = Get-Content $productionArcMapLocator -Raw | ConvertFrom-Json
        if (
            $productionArcMap.environment -eq "PRODUCTION" -and
            $productionArcMap.data_root -and
            $productionArcMap.ipc_root
        ) {
            $env:ZAGENT_ARCMAP_UPSTREAM_DATA_ROOT = [string]$productionArcMap.data_root
            $env:ZAGENT_ARCMAP_UPSTREAM_IPC_ROOT = [string]$productionArcMap.ipc_root
            $env:ZAGENT_ARCMAP_UPSTREAM_MODE = "READ_ONLY"
        }
    }
    catch {
        Write-Warning ("Could not configure DEV ArcMap upstream: " + $_.Exception.Message)
    }
}

$env:ZAGENT_ENVIRONMENT = "DEVELOPMENT"
$env:ZAGENT_ROOT = $Root
$env:ZAGENT_DATA_ROOT = $DataRoot
$env:ZAGENT_FILES_DEFAULT_PROFILE = $filesProfile
$env:ZAGENT_MANAGER_PIPE = "Zagent.Manager.Development"
$env:ZAGENT_SUPERVISOR_LEASE_NAME = "Local\Zagent.v2.Supervisor.Development"
$env:ZAGENT_BIND_HOST = "127.0.0.3"
$env:ZAGENT_TUNNEL_PORT = "19010"
$env:ZAGENT_TUNNEL_HEALTH_PORT = "19011"
$env:ZAGENT_FILES_PORT = "19020"
$env:ZAGENT_WEB_PORT = "19030"
$env:ZAGENT_ARCMAP_PORT = "19040"

$bridgeTargets = @(
    (Join-Path $Root "agents\web\engine\extension"),
    (Join-Path $Root "agents\web\engine\artifacts\chrome-bridge")
)
& (Join-Path $Root "packaging\scripts\Set-ChromeBridgeEnvironment.ps1") -Environment "DEVELOPMENT" -BindHost $env:ZAGENT_BIND_HOST -Port ([int]$env:ZAGENT_WEB_PORT) -TargetRoots $bridgeTargets | Out-Null

$env:ZAGENT_CORE_EXE = $coreExe
$env:ZAGENT_FILES_PYTHON = $RuntimeInfo.files_python
$env:ZAGENT_TUNNEL_PYTHON = $RuntimeInfo.tunnel_python
$env:ZAGENT_WEB_NODE = $RuntimeInfo.web_node
$env:ZAGENT_ARCMAP_PYTHON = $RuntimeInfo.arcmap_python
$env:ZAGENT_MANAGER_SKIP_CLOSE_CONFIRMATION = $null

$p = Start-Process -FilePath $managerExe -WorkingDirectory $Root -PassThru
Set-Content -LiteralPath $pidFile -Value $p.Id -Encoding ASCII
[pscustomobject]@{ok=$true;environment="DEVELOPMENT";manager_pid=$p.Id;pipe=$env:ZAGENT_MANAGER_PIPE;supervisor_lease=$env:ZAGENT_SUPERVISOR_LEASE_NAME;tunnel="http://127.0.0.3:19010/mcp";files="http://127.0.0.3:19020/mcp";web="http://127.0.0.3:19030/mcp";arcmap="http://127.0.0.3:19040/mcp";data_root=$DataRoot;files_profile=$filesProfile} | ConvertTo-Json -Depth 3