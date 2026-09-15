$ErrorActionPreference = "Stop"
$runtimeDirectory = Join-Path $env:TEMP "kipu-alert-reviewer-mvp"
$pidPath = Join-Path $runtimeDirectory "worker.pid"

if (-not (Test-Path $pidPath)) {
    Write-Output "The AWS MVP worker has no PID file."
    exit 0
}

$workerProcessId = [int](Get-Content $pidPath)
$process = Get-Process -Id $workerProcessId -ErrorAction SilentlyContinue
if ($null -ne $process) {
    Stop-Process -Id $workerProcessId
    $process.WaitForExit(10000)
}

Remove-Item -LiteralPath $pidPath -Force
Write-Output "Stopped AWS MVP worker process $workerProcessId."
