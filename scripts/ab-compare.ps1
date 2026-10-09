# F:\Marker\scripts\ab-compare.ps1
# 对比两套转换产物（A/B：不同 vLLM 版本、或不同扩展开关）在同一页区间上的质量。
#
# 用法：
#   F:\Marker\scripts\ab-compare.ps1 -PdfPath "input\book.pdf" -PageRange 0-39 `
#       -A "F:\Marker\out\ab-v0201" -B "F:\Marker\out\ab-v0310"

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [string]$PageRange = '0-39',
    [Parameter(Mandatory = $true)][string]$A,
    [Parameter(Mandatory = $true)][string]$B,
    [string]$LabelA = 'A',
    [string]$LabelB = 'B',
    [string]$ChunksA = '',
    [string]$ChunksB = ''
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"

if (-not (Test-Path $PdfPath)) { throw "找不到 PDF：$PdfPath" }
$PdfPath = (Resolve-Path $PdfPath).Path

function Invoke-Side([string]$root, [string]$label, [string]$chunkOverride) {
    $chunks = if ($chunkOverride) { $chunkOverride } else { Join-Path $root 'chunks' }
    if (-not (Test-Path $chunks)) { $chunks = $MarkerChunks }   # 兼容"产物在别处、chunks 用主目录"的场景
    $merged = Join-Path $root 'merged'
    $qa     = Join-Path $root 'qa'
    if (-not (Test-Path $chunks)) { throw "$label 缺少 chunks 目录：$chunks" }

    if (-not (Test-Path (Join-Path $merged 'book.md'))) {
        Write-Host "[ab] $label 合并..." -ForegroundColor Cyan
        & $MarkerPython "$MarkerScripts\merge.py" --chunk-dir $chunks --out-dir $merged --pdf $PdfPath |
            Select-Object -First 3 | Write-Host
    }
    Write-Host "[ab] $label 质检（页 $PageRange）..." -ForegroundColor Cyan
    & $MarkerPython "$MarkerScripts\qa.py" --merged-md (Join-Path $merged 'book.md') `
        --pdf $PdfPath --chunks $chunks --out-qa $qa --samples 0 --page-range $PageRange |
        Select-Object -Last 5 | Write-Host
    return (Join-Path $qa 'metrics.json')
}

$ma = Invoke-Side $A $LabelA $ChunksA
$mb = Invoke-Side $B $LabelB $ChunksB

Write-Host "`n================ A/B 对比（页 $PageRange）================" -ForegroundColor Green
$rows = [System.Collections.Generic.List[object]]::new()

function Add-Row($name, $va, $vb, $lowerIsBetter) {
    $rows.Add([pscustomobject]@{ 指标 = $name; $LabelA = $va; $LabelB = $vb })
}

$ja = Get-Content $ma -Raw -Encoding utf8 | ConvertFrom-Json
$jb = Get-Content $mb -Raw -Encoding utf8 | ConvertFrom-Json

Add-Row '文字层总丢字' $ja.total_lost_chars $jb.total_lost_chars $true
Add-Row '覆盖率不足页数' ($ja.pages_below_coverage.Count) ($jb.pages_below_coverage.Count) $true
Add-Row '最大单页丢字' $ja.max_dropped_chars_per_page $jb.max_dropped_chars_per_page $true
Add-Row '页边丢失(页眉+页脚)' ($ja.header_zone_lost + $ja.footer_zone_lost) ($jb.header_zone_lost + $jb.footer_zone_lost) $true
Add-Row '行内公式检出' $ja.extensions.inline_detected $jb.extensions.inline_detected $false
Add-Row '行内公式恢复' $ja.extensions.inline_recovered $jb.extensions.inline_recovered $false
Add-Row 'OCR保底回滚块数' $ja.extensions.ocr_guard_rolled_back $jb.extensions.ocr_guard_rolled_back $false
Add-Row 'OCR保底保住字数' $ja.extensions.ocr_guard_chars_saved $jb.extensions.ocr_guard_chars_saved $false
Add-Row 'U+FFFD' $ja.replacement_chars_total $jb.replacement_chars_total $true
Add-Row '独立公式 $$' (($ja.per_page | Measure-Object -Property display_math -Sum).Sum) (($jb.per_page | Measure-Object -Property display_math -Sum).Sum) $false
Add-Row '行内公式 $…$' (($ja.per_page | Measure-Object -Property inline_math_dollars -Sum).Sum) (($jb.per_page | Measure-Object -Property inline_math_dollars -Sum).Sum) $false
Add-Row '表格数' (($ja.per_page | Measure-Object -Property tables -Sum).Sum) (($jb.per_page | Measure-Object -Property tables -Sum).Sum) $false

$rows | Format-Table -AutoSize | Out-String | Write-Host

$report = Join-Path (Split-Path $A -Parent) 'ab-compare.md'
$L = @("# A/B 对比（页 $PageRange）", "",
       "- A = $LabelA（$A）", "- B = $LabelB（$B）", "",
       "| 指标 | $LabelA | $LabelB | 方向 |", "|---|---|---|---|")
foreach ($r in $rows) {
    $dir = if ($r.'方向') { $r.'方向' } else { '' }
    $L += "| $($r.指标) | $($r.$LabelA) | $($r.$LabelB) | $dir |"
}
$L += "", "> 报告由 scripts\ab-compare.ps1 生成；逐页明细见各自的 qa\metrics.json。"
$L -join "`n" | Set-Content -Path $report -Encoding utf8
Write-Host "对比表 -> $report" -ForegroundColor Green
