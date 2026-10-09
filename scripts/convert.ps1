# F:\Marker\scripts\convert.ps1
# 分块转换：把大 PDF 按 --page_range 切块调用 marker（经 scripts\marker_chunk.py 注入扩展），
# 幂等可续跑。
#
# 为什么分块：单文件 423 页没有断点续跑，异常/超时要么整本重来，要么丢进度；
# 按块跑还能把峰值内存压住。输出仍然落在 <output_dir>\<文件名>\<文件名>.md，
# 与 marker_single 完全一致，merge.py / qa.py 不用改。
#
# 为什么改用 marker_chunk.py 而不是 marker_single：
#   1) 注入两个自研处理器 —— 行内公式缺口恢复 + OCR 重写覆盖率保底（见 scripts\marker_ext\）；
#   2) 打开 --keep_pageheader_in_output / --keep_pagefooter_in_output，页边内容不再被剔除；
#   3) 配置走 Python dict，绕开 Windows PowerShell 5.1 的引号/JSON 转义坑；
#   4) 额外产出 <stem>_ext_report.json，qa.py 会汇总成"行内公式恢复/回滚"指标。
#
# 用法：
#   .\convert.ps1 -PdfPath "F:\Marker\input\book.pdf"
#   .\convert.ps1 -PdfPath "..." -ChunkSize 30 -Force
#   .\convert.ps1 -PdfPath "..." -ForceOcr -OnlyChunks 3,4     # 对乱码块重跑
#   .\convert.ps1 -PdfPath "..." -NoInlineFormulaRecovery      # A/B 对比：关掉公式恢复

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [int]$ChunkSize     = 40,
    [string]$ChunkDir   = '',
    [ValidateSet('balanced', 'fast')][string]$Mode = 'balanced',
    [string[]]$ExtraArgs = @(),
    [int[]]$OnlyChunks  = @(),
    [int]$MaxRetries    = 3,
    [switch]$Force,               # 重跑已完成的块
    [switch]$ForceOcr,            # 对该次所有块加 --force_ocr
    [switch]$NoPaginate,
    [switch]$NoKeepPageMargins,   # 关掉页眉/页脚保留（A/B 对比用）
    [switch]$NoInlineFormulaRecovery,  # 关掉行内公式缺口恢复（A/B 对比用）
    [switch]$NoOcrGuard           # 关掉 OCR 重写覆盖率保底（A/B 对比用，危险）
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"

if (-not (Test-Path $PdfPath)) { throw "找不到 PDF：$PdfPath" }
$PdfPath = (Resolve-Path $PdfPath).Path
$stem    = [System.IO.Path]::GetFileNameWithoutExtension($PdfPath)
if (-not $ChunkDir) { $ChunkDir = $MarkerChunks }

$total = [int](& $MarkerPython "$MarkerScripts\pdfutil.py" pages $PdfPath)
if ($total -le 0) { throw "读取页数失败：$PdfPath" }
Write-Host "[convert] 文件: $PdfPath"
Write-Host "[convert] 页数: $total   分块: $ChunkSize 页/块   模式: $Mode"
Write-Host ("[convert] 扩展：页边保留={0} 行内公式恢复={1} OCR保底={2}" -f `
    (-not $NoKeepPageMargins), (-not $NoInlineFormulaRecovery), (-not $NoOcrGuard))

# ---- Smart App Control 预检 ----
# 本机 SAC 于 2026-10-04 由「评估(2)」自动转为「强制(1)」后，venv 里「未签名 + 云端信誉未判定」的
# 原生模块（torch 的 c10_cuda.dll、sklearn 的 _fast_dict.pyd …）在 import 阶段就被拦，表现是每块
# 4 秒内必败、日志里只有 WinError 4551 / "An Application Control policy has blocked this file"。
# 这时 3 次重试不可能成功，所以提前把状态与处置写进终端和块日志。详见 docs\sac-incident.md。
$sacRaw  = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' `
    -Name 'VerifiedAndReputablePolicyState' -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
$sacText = switch ($sacRaw) { 0 { 'Off(关闭)' } 1 { 'On(强制)' } 2 { 'Evaluate(评估)' } default { "未知($sacRaw)" } }
Write-Host "[convert] Smart App Control: $sacText"
if ($sacRaw -eq 1) {
    Write-Warning ('[convert] Smart App Control 处于强制模式：venv 里未签名的原生模块会在导入阶段被拦，' +
        '分块转换多半每块几秒内必败（WinError 4551）。处置：以管理员身份运行 ' +
        'scripts\disable-smart-app-control.ps1 关闭 SAC 后重启，或改用 WSL2；详见 docs\sac-incident.md')
}

# 构造块区间
$ranges = @()
for ($start = 0; $start -lt $total; $start += $ChunkSize) {
    $end = [Math]::Min($start + $ChunkSize - 1, $total - 1)
    $ranges += , @{ Index = [int]($start / $ChunkSize); Start = $start; End = $end }
}
Write-Host "[convert] 共 $($ranges.Count) 块：$(($ranges | ForEach-Object { "$($_.Start)-$($_.End)" }) -join ', ')"

$runner = Join-Path $MarkerScripts 'marker_chunk.py'
if (-not (Test-Path $runner)) { throw "找不到 $runner" }
if (-not (Test-Path $MarkerPython)) { throw "找不到 venv python：$MarkerPython" }

$summary = @()
foreach ($r in $ranges) {
    $idx      = $r.Index
    $chunkOut = Join-Path $ChunkDir ('chunk-{0:D2}' -f $idx)
    $mdPath   = Join-Path (Join-Path $chunkOut $stem) "$stem.md"
    $reportPath = Join-Path (Join-Path $chunkOut $stem) "${stem}_ext_report.json"
    $logPath  = Join-Path $MarkerLogs ('chunk-{0:D2}.log' -f $idx)

    if ($OnlyChunks.Count -gt 0 -and $OnlyChunks -notcontains $idx) { continue }

    if ((Test-Path $mdPath) -and (Test-Path $reportPath) -and -not $Force -and -not $ForceOcr) {
        Write-Host "[convert] chunk-$('{0:D2}' -f $idx) 已完成，跳过（-Force 可重跑）"
        $summary += [pscustomobject]@{ Chunk = $idx; Range = "$($r.Start)-$($r.End)"; Status = 'skipped'; Seconds = 0 }
        continue
    }

    New-Item -ItemType Directory -Force -Path $chunkOut | Out-Null

    $args = @(
        $runner,
        '--pdf', $PdfPath,
        '--start', "$($r.Start)",
        '--end', "$($r.End)",
        '--output-dir', $chunkOut,
        '--mode', $Mode,
        '--report', $reportPath
    )
    if (-not $NoPaginate)            { $args += '--paginate_output' }
    # 注意：marker_chunk.py 的参数名用下划线（--no_keep_page_margins 等），
    # 与 marker_single 的连字符风格不同；写错会被 argparse 直接拒绝。
    if ($NoKeepPageMargins)          { $args += '--no_keep_page_margins' }
    if ($NoInlineFormulaRecovery)    { $args += '--no_inline_formula_recovery' }
    if ($NoOcrGuard)                 { $args += '--no_ocr_guard' }
    if ($ForceOcr)                   { $args += @('--force_ocr') }
    $args += $ExtraArgs

    $ok = $false
    for ($attempt = 1; $attempt -le $MaxRetries; $attempt++) {
        $t0 = Get-Date
        $header = "[convert] chunk-$('{0:D2}' -f $idx)  页 $($r.Start)-$($r.End)  第 $attempt/$MaxRetries 次尝试  $(Get-Date -Format s)"
        Write-Host $header
        $header | Out-File -FilePath $logPath -Encoding utf8 -Append

        # 关键：Windows PowerShell 5.1 下，原生命令只要往 stderr 写东西就会产生错误记录；
        # 脚本开头设了 $ErrorActionPreference='Stop'，于是 marker/surya 的普通警告
        # （如 "Layout inference failed for page 0; leaving page empty."）会被当成终止性异常，
        # 把成功的转换误判为失败。这里临时降为 Continue，并以「退出码 + 产物存在」判定成功。
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 0
        $exit = -1
        try {
            # 不要用 *>>：PS 5.1 的重定向固定按 UTF-16LE 写，和上面 "Out-File -Encoding utf8"
            # 写的头部混在一起，会得到一个夹着 NUL 的混合编码文件，grep/read 会直接当二进制拒绝
            # （本次排查 SAC 时就被它卡过）。显式管道 + UTF-8 追加，日志才是人能读的。
            & $MarkerPython @args 2>&1 | Out-File -FilePath $logPath -Encoding utf8 -Append
            $exit = $LASTEXITCODE
        }
        catch {
            "[convert] 调用异常：$($_.Exception.Message)" | Out-File -FilePath $logPath -Encoding utf8 -Append
        }
        finally {
            $ErrorActionPreference = $prevEAP
        }

        $secs = [int]((Get-Date) - $t0).TotalSeconds
        if ($exit -eq 0 -and (Test-Path $mdPath)) {
            $ok = $true
            Write-Host "[convert] chunk-$('{0:D2}' -f $idx) 完成，用时 ${secs}s"
            $summary += [pscustomobject]@{ Chunk = $idx; Range = "$($r.Start)-$($r.End)"; Status = 'ok'; Seconds = $secs }
            break
        }
        "[convert] 退出码 $exit，产物存在=$(Test-Path $mdPath)" | Out-File -FilePath $logPath -Encoding utf8 -Append
        Write-Warning "[convert] chunk-$('{0:D2}' -f $idx) 失败（用时 ${secs}s），见 $logPath"
        Start-Sleep -Seconds 10
    }
    if (-not $ok) {
        $summary += [pscustomobject]@{ Chunk = $idx; Range = "$($r.Start)-$($r.End)"; Status = 'FAILED'; Seconds = 0 }
    }
}

$summary | Format-Table -AutoSize | Out-String | Write-Host
$summary | ConvertTo-Json -Depth 3 | Set-Content -Encoding utf8 (Join-Path $MarkerOut 'convert-summary.json')

$failed  = @($summary | Where-Object { $_.Status -eq 'FAILED' }).Count
$okCnt   = @($summary | Where-Object { $_.Status -eq 'ok' }).Count
$skipCnt = @($summary | Where-Object { $_.Status -eq 'skipped' }).Count
$totalSec = ($summary | Measure-Object -Property Seconds -Sum).Sum
Write-Host "[convert] 汇总：成功 $okCnt 块，跳过 $skipCnt 块，失败 $failed 块，总耗时 ${totalSec}s"
if ($failed -gt 0) { exit 1 }
