<#
  Run agy (Antigravity CLI) non-interactively for one task, keeping context across runs.

  - Runs from $AgyHome so GEMINI.md (which includes CONTEXT.md) is loaded automatically as rules.
  - Passes $RepoRoot (and any extra $AddDir entries) via --add-dir so agy can read the shared repo.
  - -Resume <conversation id> continues an earlier, FINISHED agy conversation (never an open one;
    refuses if presence/<id>.lock exists).
  - Appends one JSON line per run to <AgyHome>\agy_runs.jsonl: label, task, model, times, exit
    code, the conversation it resumed, and the conversation it produced (read from agy's
    last_conversations.json).

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
$prompt = Get-Content -Raw -Encoding UTF8 $Task
$args_ = @('-p', $prompt, '--model', $Model, '--dangerously-skip-permissions', '--print-timeout', "$($TimeoutSec)s")
$dirs = @()
if ($RepoRoot -ne '') { $dirs += $RepoRoot }
$dirs += $AddDir
foreach ($d in $dirs) { $args_ += @('--add-dir', $d) }
if ($Resume -ne '') { $args_ += @('--conversation', $Resume) }
$started = (Get-Date).ToUniversalTime()
$ErrorActionPreference = 'Continue'
$out = & $AgyExe @args_ 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$ended = (Get-Date).ToUniversalTime()
$conversation = $null
try { $conversation = (Get-Content -Raw $ConversationMap | ConvertFrom-Json).$AgyHome } catch { $conversation = $null }
$row = [ordered]@{
    label = $Label; task = $Task; model = $Model; repo_root = $RepoRoot; resumed_from = $Resume
    conversation = $conversation; started = $started.ToString('s') + 'Z'; ended = $ended.ToString('s') + 'Z'
    elapsed_s = [int]($ended - $started).TotalSeconds; exit = $code
}
Add-Content -Path $log -Value ($row | ConvertTo-Json -Compress) -Encoding UTF8
$out | Select-Object -Last 15
"run: $($row | ConvertTo-Json -Compress)"
exit $code
