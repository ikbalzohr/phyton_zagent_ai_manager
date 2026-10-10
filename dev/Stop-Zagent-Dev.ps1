param()

$ErrorActionPreference = "SilentlyContinue"
$PipeName = "Zagent.Manager.Development"
$StateRoot = Join-Path $PSScriptRoot "state"
$pidFile = Join-Path $StateRoot "manager.pid"
$shutdownRequested = $false

try {
    $pipe = [System.IO.Pipes.NamedPipeClientStream]::new(".", $PipeName, [System.IO.Pipes.PipeDirection]::InOut)
    $pipe.Connect(1500)
    $reader = [System.IO.StreamReader]::new($pipe,[System.Text.UTF8Encoding]::new($false),$false,16384,$true)
    $writer = [System.IO.StreamWriter]::new($pipe,[System.Text.UTF8Encoding]::new($false),16384,$true)
    $writer.AutoFlush = $true
    $req = @{protocolVersion="1.0";requestId=[Guid]::NewGuid().ToString("N");method="system.shutdown";parameters=$null} | ConvertTo-Json -Compress
    $writer.WriteLine($req)
    [void]$reader.ReadLine()
    $shutdownRequested = $true
    $reader.Dispose()
    $writer.Dispose()
    $pipe.Dispose()
} catch {}

Start-Sleep -Milliseconds 700

if (Test-Path $pidFile) {
    try {
        $managerPidValue = [int](Get-Content $pidFile -Raw)
        $managerProcess = Get-Process -Id $managerPidValue -ErrorAction SilentlyContinue
        if ($managerProcess) { Stop-Process -Id $managerPidValue -Force -ErrorAction SilentlyContinue }
    } catch {}
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

# Fallback for stale/orphan DEV processes when a PID file was lost.
$devProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*appBridge\production\Zagent\v3*" -and
    $_.Name -match "^(Zagent\.|python|node)"
})
foreach ($item in $devProcesses) {
    Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Milliseconds 500
$remaining = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*appBridge\production\Zagent\v3*" -and
    $_.Name -match "^(Zagent\.|python|node)"
} | Select-Object ProcessId,ParentProcessId,Name,CommandLine)

[pscustomobject]@{
    ok = ($remaining.Count -eq 0)
    shutdown_requested = $shutdownRequested
    remaining_processes = $remaining
} | ConvertTo-Json -Depth 5