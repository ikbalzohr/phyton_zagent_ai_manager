param()

$ErrorActionPreference = "Stop"
$DevRoot = (Resolve-Path $PSScriptRoot).Path
$Root = (Resolve-Path (Join-Path $DevRoot "..")).Path

$srcPattern = (Join-Path $Root "src") + "*"
$runtimePattern = (Join-Path $DevRoot "runtime") + "*"
$tunnelTransportPattern = (Join-Path $Root "agents\tunnel\transport") + "*"
$managerDebugPattern = "*" + (Join-Path $Root "src\Zagent.Manager\bin\Debug") + "*"
$managerPipePattern = "*Zagent.Manager.Dev.v1*"

function Get-ZagentDevProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $path = $_.ExecutablePath
        $cmd = $_.CommandLine

        ($path -and $path -like $srcPattern) -or
        ($path -and $path -like $runtimePattern) -or
        ($path -and $path -like $tunnelTransportPattern) -or
        ($cmd -and $cmd -like $managerDebugPattern) -or
        ($cmd -and $cmd -like $managerPipePattern)
    }
}

$targets = @(Get-ZagentDevProcesses)
$pids = @($targets | Select-Object -ExpandProperty ProcessId -Unique | Sort-Object -Descending)

foreach ($pidValue in $pids) {
    try {
        Stop-Process -Id $pidValue -Force -ErrorAction Stop
        Write-Output "STOPPED pid=$pidValue"
    }
    catch {
        Write-Output "STOP_SKIP pid=$pidValue message=$($_.Exception.Message)"
    }
}

Start-Sleep -Milliseconds 500

$remaining = @(Get-ZagentDevProcesses)
if ($remaining.Count -gt 0) {
    $remaining | Select-Object ProcessId,Name,ExecutablePath,CommandLine | Format-Table -AutoSize
    throw "Zagent DEV cleanup left one or more processes alive."
}

Remove-Item -LiteralPath (Join-Path $DevRoot "state\manager.pid") -Force -ErrorAction SilentlyContinue
Write-Output "ZAGENT_DEV_STOPPED clean=true"
