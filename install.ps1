# 一键安装 / 更新 -skill- 仓库里的技能
#
#   powershell -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/ggggoq/-skill-/main/install.ps1 -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1"
#   ... -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1 -Skill minimax-sku-video
#
# 做三件事：下载仓库 ZIP → 解到临时目录 → 复制到 ~/.workbuddy/skills/
# 更新时先备份旧目录，且只覆盖不删除（保留你自己的 tools\ffmpeg.exe 等）。
param(
    [string]$Repo = "ggggoq/-skill-",
    [string]$Branch = "main",
    [string]$Skill = ""          # 只装某一个技能；留空=全部
)
$ErrorActionPreference = "Stop"

$repoName = $Repo.Split('/')[-1]
$url = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$tmp = Join-Path $env:TEMP ("skillpull-" + $stamp)
$zip = "$tmp.zip"
$dstRoot = Join-Path $env:USERPROFILE ".workbuddy\skills"

Write-Host "== 下载 $url" -ForegroundColor Cyan
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

Write-Host "== 解压到 $tmp"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    # 指定 UTF8：PS 5.1 自带的 Expand-Archive 会把中文文件名解成乱码
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $tmp, [System.Text.Encoding]::UTF8)
} catch {
    Write-Host "   回退到 Expand-Archive（中文文件名可能异常）" -ForegroundColor Yellow
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
if (-not $names) { throw "在 $src 里没找到任何含 SKILL.md 的技能目录" }

New-Item -ItemType Directory -Path $dstRoot -Force | Out-Null
foreach ($n in $names) {
    $from = Join-Path $src $n
    if (-not (Test-Path $from)) { Write-Host "== 跳过 $n（仓库里没有）" -ForegroundColor Yellow; continue }
    $to = Join-Path $dstRoot $n
    if (Test-Path $to) {
        $bak = "$to.bak-$stamp"
        Copy-Item $to $bak -Recurse -Force
        Write-Host "== $n：旧版已备份到 $bak" -ForegroundColor DarkGray
    }
    New-Item -ItemType Directory -Path $to -Force | Out-Null
    Copy-Item (Join-Path $from "*") $to -Recurse -Force
    $cnt = (Get-ChildItem $to -Recurse -File).Count
    Write-Host "== $n：已更新（$cnt 个文件）" -ForegroundColor Green
}

Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $zip -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "完成。技能目录：$dstRoot" -ForegroundColor Green
Write-Host "下一步：打开对应技能的 SKILL.md 或 assets/manual/环境配置手册.html"
