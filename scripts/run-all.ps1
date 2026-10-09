# F:\Marker\scripts\run-all.ps1
# 一键跑完整流程：起服务 -> 分块转换 -> 合并 -> 质检。
#
# 用法：
#   .\run-all.ps1 -PdfPath "F:\Marker\input\book.pdf"
#   .\run-all.ps1 -PdfPath "..." -ChunkSize 40 -SkipVllmStart -KeepVllmRunning

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [int]$ChunkSize = 40,
    [int]$Samples   = 12,
    [string]$Mode   = 'balanced',
    [switch]$SkipVllmStart,
    [switch]$KeepVllmRunning,
    [switch]$SkipConvert
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"

if (-not (Test-Path $PdfPath)) { throw "找不到 PDF：$PdfPath" }
$PdfPath = (Resolve-Path $PdfPath).Path

if (-not $SkipVllmStart) {
    Write-Host "`n===== [1/4] 启动 vLLM 推理服务 =====" -ForegroundColor Cyan
    & "$PSScriptRoot\start-vllm.ps1"
} else {
    Write-Host "`n===== [1/4] 跳过启动（-SkipVllmStart），校验服务可用性 =====" -ForegroundColor Cyan
    $ok = $false
    try { $ok = (Invoke-WebRequest "$($env:SURYA_INFERENCE_URL -replace '/v1$','')/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { }
    if (-not $ok) { throw "推理服务不可用：$env:SURYA_INFERENCE_URL（先跑 start-vllm.ps1）" }
    Write-Host "服务健康：$env:SURYA_INFERENCE_URL"
}

if (-not $SkipConvert) {
    Write-Host "`n===== [2/4] 分块转换 =====" -ForegroundColor Cyan
    & "$PSScriptRoot\convert.ps1" -PdfPath $PdfPath -ChunkSize $ChunkSize -Mode $Mode
} else {
    Write-Host "`n===== [2/4] 跳过转换（-SkipConvert）=====" -ForegroundColor Cyan
}

Write-Host "`n===== [3/5] 合并 =====" -ForegroundColor Cyan
& $MarkerPython "$PSScriptRoot\merge.py" --chunk-dir $MarkerChunks --out-dir $MarkerMerged --pdf $PdfPath

Write-Host "`n===== [4/5] 质检（含文字层零丢失核对）=====" -ForegroundColor Cyan
& $MarkerPython "$PSScriptRoot\qa.py" --merged-md (Join-Path $MarkerMerged 'book.md') --pdf $PdfPath `
    --chunks $MarkerChunks --out-qa $MarkerQa --samples $Samples

Write-Host "`n===== [5/5] 验收闸门 =====" -ForegroundColor Cyan
& $MarkerPython "$PSScriptRoot\verify.py" --merged $MarkerMerged --qa $MarkerQa
$verifyExit = $LASTEXITCODE
if ($verifyExit -ne 0) {
    Write-Warning "验收未通过（exit $verifyExit）：看 $MarkerQa\report.md 的「文字层零丢失核对」一节，"
    Write-Warning "必要时用 patch-missing-pages.py 按文字层补回丢字页。"
}

if (-not $KeepVllmRunning) {
    Write-Host "`n===== 停止推理服务 =====" -ForegroundColor Cyan
    & "$PSScriptRoot\stop-vllm.ps1"
} else {
    Write-Host "`n推理服务保持运行（-KeepVllmRunning）。收工请执行 stop-vllm.ps1。" -ForegroundColor Yellow
}

Write-Host "`n完成。产物：$MarkerMerged\book.md ；报告：$MarkerQa\report.md" -ForegroundColor Green
if ($verifyExit -ne 0) { exit $verifyExit }
