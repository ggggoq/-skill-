# Install / update skills from the -skill- repo.
#
#   powershell -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/ggggoq/-skill-/main/install.ps1 -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1"
#   ... -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1 -Skill minimax-sku-video
#
# What it does: download repo ZIP -> extract to temp -> copy into ~/.workbuddy/skills/
# Update mode: back up the old folder first, then copy over. Nothing is deleted,
# so your local tools\ffmpeg.exe and project data survive.
#
# NOTE: this file is intentionally ASCII-only. PowerShell 5.1 reads BOM-less UTF-8
# as ANSI and garbles non-ASCII text (which can break parsing), and raw.githubusercontent
# may serve a cached copy. ASCII keeps it runnable everywhere.
param(
    [string]$Repo = "ggggoq/-skill-",
    [string]$Branch = "main",
    [string]$Skill = ""          # install one skill only; empty = all skills in repo
)
$ErrorActionPreference = "Stop"

$repoName = $Repo.Split('/')[-1]
$url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tmp = Join-Path $env:TEMP ("skillpull-" + $stamp)
$zip = "$tmp.zip"
$dstRoot = Join-Path $env:USERPROFILE ".workbuddy\skills"

Write-Host "[1/4] download $url" -ForegroundColor Cyan
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Write-Host ("      {0:N0} bytes" -f (Get-Item $zip).Length)

Write-Host "[2/4] extract to $tmp"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    # Force UTF8: PS 5.1 Expand-Archive mangles non-ASCII file names.
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmp, [System.Text.Encoding]::UTF8)
} catch {
    Write-Host "      fallback to Expand-Archive (non-ASCII names may break)" -ForegroundColor Yellow
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
}

$src = Join-Path $tmp "$repoName-$Branch"
if (-not (Test-Path $src)) {
    $src = (Get-ChildItem $tmp -Directory | Select-Object -First 1).FullName
}

$names = if ($Skill) { @($Skill) } else {
    Get-ChildItem $src -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") } |
        ForEach-Object { $_.Name }
}
if (-not $names) { throw "no skill folder (with SKILL.md) found in $src" }

Write-Host "[3/4] install to $dstRoot"
New-Item -ItemType Directory -Path $dstRoot -Force | Out-Null
foreach ($n in $names) {
    $from = Join-Path $src $n
    if (-not (Test-Path $from)) { Write-Host "      skip $n (not in repo)" -ForegroundColor Yellow; continue }
    $to = Join-Path $dstRoot $n
    if (Test-Path $to) {
        $bak = "$to.bak-$stamp"
        Copy-Item $to $bak -Recurse -Force
        Write-Host "      $n : old version backed up to $bak" -ForegroundColor DarkGray
    }
    New-Item -ItemType Directory -Path $to -Force | Out-Null
    Copy-Item (Join-Path $from "*") $to -Recurse -Force
    $cnt = (Get-ChildItem $to -Recurse -File).Count
    Write-Host "      $n : updated ($cnt files)" -ForegroundColor Green
}

Write-Host "[4/4] cleanup"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $zip -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "DONE. Skills dir: $dstRoot" -ForegroundColor Green
Write-Host "Next: open SKILL.md, or minimax-sku-video\assets\manual\ for the setup manual."
