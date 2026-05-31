param(
    [string]$Prompt = "检查 D:\code\ESP32S3\Claude_Watch_Buddy 项目结构",
    [switch]$BypassSandbox,
    [switch]$VerboseCodex,
    [switch]$VerboseStderr,
    [int]$ScanRetries = 3
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$Args = @(".\codex_watch_bridge.py", "--prompt", $Prompt)
$Args += @("--scan-retries", $ScanRetries)
if ($BypassSandbox) {
    $Args += "--bypass-sandbox"
}
if ($VerboseCodex) {
    $Args += "--verbose-codex"
}
if ($VerboseStderr) {
    $Args += "--verbose-stderr"
}
& $Python @Args
