param(
    [string]$ControlProfile = "ia-dev-payments-intelligence",
    [string]$WorkerProfile = "kipu-alert-reviewer-mvp",
    [string]$StackName = "pmt-intel-kipu-alert-reviewer-mvp",
    [string]$Region = "us-east-1"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDirectory = Join-Path $env:TEMP "kipu-alert-reviewer-mvp"
$pidPath = Join-Path $runtimeDirectory "worker.pid"
$stdoutPath = Join-Path $runtimeDirectory "worker.stdout.log"
$stderrPath = Join-Path $runtimeDirectory "worker.stderr.log"

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null

if (Test-Path $pidPath) {
    $existingProcessId = Get-Content $pidPath -ErrorAction SilentlyContinue
    $existingProcess = Get-Process -Id $existingProcessId -ErrorAction SilentlyContinue
    if ($null -ne $existingProcess) {
        [PSCustomObject]@{
            ProcessId = $existingProcess.Id
            Running = $true
            Stdout = $stdoutPath
            Stderr = $stderrPath
        }
        exit 0
    }
}

$stackJson = aws cloudformation describe-stacks `
    --profile $ControlProfile `
    --region $Region `
    --stack-name $StackName `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Could not read CloudFormation stack $StackName"
}

$stack = ($stackJson | ConvertFrom-Json).Stacks[0]
$outputs = @{}
foreach ($output in $stack.Outputs) {
    $outputs[$output.OutputKey] = $output.OutputValue
}

$env:AWS_PROFILE = $WorkerProfile
$env:AWS_REGION = $Region
$env:LOG_LEVEL = "INFO"
$env:SQS_INPUT_QUEUE_URL = $outputs["InputQueueUrl"]
$env:SQS_IDEMPOTENCY_TABLE = $outputs["IdempotencyTableName"]
$env:SQS_WAIT_TIME_SECONDS = "20"
$env:SQS_VISIBILITY_TIMEOUT_SECONDS = "120"
$env:SQS_MAX_MESSAGES = "10"
$env:SQS_IDEMPOTENCY_TTL_HOURS = "168"
$env:ALERT_OCCURRENCE_TTL_HOURS = "720"
$env:EVENTBRIDGE_OUTPUT_BUS_NAME = $outputs["EventBusName"]
$env:EVENTBRIDGE_OUTPUT_SOURCE = "acceptance.reviewer"
$env:EVENTBRIDGE_OUTPUT_DETAIL_TYPE = "Anomaly Validated v1"
$env:FILTER_POLICY_PATH = Join-Path $projectRoot "config\filter_policy.yaml"
$env:PYTHONUNBUFFERED = "1"

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$process = Start-Process `
    -FilePath $python `
    -ArgumentList @("-m", "alert_reviewer.worker") `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

Set-Content -Path $pidPath -Value $process.Id
Start-Sleep -Seconds 5
$process.Refresh()

[PSCustomObject]@{
    ProcessId = $process.Id
    Running = -not $process.HasExited
    Stdout = $stdoutPath
    Stderr = $stderrPath
}

if (Test-Path $stdoutPath) {
    Get-Content $stdoutPath -Tail 20
}
if (Test-Path $stderrPath) {
    Get-Content $stderrPath -Tail 20
}
