param()

$StateRoot = Join-Path $PSScriptRoot "state"
$pidFile = Join-Path $StateRoot "manager.pid"
$managerPid=$null; $managerRunning=$false
if (Test-Path $pidFile) { try { $managerPid=[int](Get-Content $pidFile -Raw); $managerRunning=$null -ne (Get-Process -Id $managerPid -ErrorAction SilentlyContinue) } catch {} }
$ports=[ordered]@{tunnel=19010;tunnel_health=19011;files=19020;web=19030;arcmap=19040}
$listeners=@{}
foreach($e in $ports.GetEnumerator()){ $listeners[$e.Key]=[bool](Get-NetTCPConnection -State Listen -LocalPort $e.Value -ErrorAction SilentlyContinue) }
$processes=@(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*appBridge\production\Zagent\v3*" -and $_.Name -match "Zagent|python|node" } | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine)
[pscustomobject]@{environment="DEVELOPMENT";manager_pid=$managerPid;manager_running=$managerRunning;pipe="Zagent.Manager.Development";listeners=$listeners;processes=$processes} | ConvertTo-Json -Depth 6