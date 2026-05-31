param(
    [string]$Workspace = "",
    [int]$ScanRetries = 5,
    [switch]$FromStart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}

$Args = @(".\codex_watch_live.py", "--scan-retries", $ScanRetries)
if ($Workspace -ne "") {
    $Args += @("--workspace", $Workspace)
}
if ($FromStart) {
    $Args += "--from-start"
}

& $Python @Args
