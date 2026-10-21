<#
  Run agy (Antigravity CLI) non-interactively for one task, keeping context across runs.

  - Resolves -Task to an absolute path BEFORE Set-Location and rejects any -Task path containing
    a double quote (").
  - Never passes the task file contents on the command line (Windows PowerShell 5.1 does not escape
    embedded double quotes for native executables). Instead passes a short quote-free prompt telling
    agy to read and follow the task file at its absolute path.
  - Runs from $AgyHome so GEMINI.md (which includes CONTEXT.md) is loaded automatically as rules.
  - Passes $RepoRoot (and any extra $AddDir entries) via --add-dir so agy can read the shared repo.
  - -Resume <conversation id> continues an earlier, FINISHED agy conversation (never an open one;
    refuses if presence/<id>.lock exists).
  - Saves full agy stdout/stderr to <AgyHome>\agy_run_<label>_<timestamp>.log, prints the last 15
    lines, and appends one JSON line per run to <AgyHome>\agy_runs.jsonl (including conversation id
    from last_conversations.json).

  Usage:
    powershell -NoProfile -File <AgyHome>\run_agy.ps1 `
      -Task <AgyHome>\task_x.txt -Label x -RepoRoot <RepoRoot> `
      [-AgyHome <AgyHome>] [-Resume <id>] [-Model gemini-4-argon-high] [-TimeoutSec 3600]
#>
param(
    [Parameter(Mandatory = $true)][string]$Task,
    [Parameter(Mandatory = $true)][string]$Label,
    [string]$AgyHome = $PSScriptRoot,
    [string]$RepoRoot = '',
    [string]$Resume = '',
    [string]$Model = 'gemini-4-argon-high',
    [int]$TimeoutSec = 3600,
    [string[]]$AddDir = @(),
    [string]$AgyExe = '',
    [string]$ConversationMap = (Join-Path $env:USERPROFILE '.gemini\antigravity-cli\cache\last_conversations.json')
)
$ErrorActionPreference = 'Stop'
if ($Task.Contains('"')) {
    throw "Refusing -Task path containing a double quote: $Task"
}
if (-not (Test-Path -LiteralPath $Task -PathType Leaf)) {
    throw "Task file not found: $Task"
}
$taskPath = (Resolve-Path -LiteralPath $Task).ProviderPath
if ($taskPath.Contains('"')) {
    throw "Refusing resolved -Task path containing a double quote: $taskPath"
}
$AgyHome = (Resolve-Path -LiteralPath $AgyHome).ProviderPath
if ($AgyExe -eq '') {
    $candidate = Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe'
    $AgyExe = if (Test-Path $candidate) { $candidate } else { 'agy' }
}
if ($Resume -ne '') {
    $lockFile = Join-Path $env:USERPROFILE ".gemini\antigravity-cli\presence\$Resume.lock"
    if (Test-Path $lockFile) {
        throw "Refusing to resume open agy conversation $Resume ($lockFile exists)."
    }
}
$log = Join-Path $AgyHome 'agy_runs.jsonl'
Set-Location $AgyHome
$prompt = "Read the task file at $taskPath and follow it exactly."
$args_ = @('-p', $prompt, '--model', $Model, '--dangerously-skip-permissions', '--print-timeout', "$($TimeoutSec)s")
$dirs = @()
if ($RepoRoot -ne '') { $dirs += $RepoRoot }
$dirs += $AddDir
foreach ($d in $dirs) { $args_ += @('--add-dir', $d) }
if ($Resume -ne '') { $args_ += @('--conversation', $Resume) }
$started = (Get-Date).ToUniversalTime()
$safeLabel = ($Label -replace '[^A-Za-z0-9_.-]', '_')
$stamp = $started.ToString('yyyyMMddTHHmmssZ')
$outLog = Join-Path $AgyHome "agy_run_${safeLabel}_${stamp}.log"
$ErrorActionPreference = 'Continue'
$out = & $AgyExe @args_ 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$ended = (Get-Date).ToUniversalTime()
Set-Content -Path $outLog -Value $out -Encoding UTF8
$conversation = $null
try { $conversation = (Get-Content -Raw $ConversationMap | ConvertFrom-Json).$AgyHome } catch { $conversation = $null }
$row = [ordered]@{
    label = $Label; task = $taskPath; model = $Model; repo_root = $RepoRoot; resumed_from = $Resume
    conversation = $conversation; log_file = $outLog; started = $started.ToString('s') + 'Z'
    ended = $ended.ToString('s') + 'Z'; elapsed_s = [int]($ended - $started).TotalSeconds; exit = $code
}
Add-Content -Path $log -Value ($row | ConvertTo-Json -Compress) -Encoding UTF8
$out | Select-Object -Last 15
"run: $($row | ConvertTo-Json -Compress)"
exit $code
