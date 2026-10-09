# F:\Marker\scripts\probe-vllm-versions.ps1
# 只读探测：当前 vLLM 镜像、可用候选镜像、以及 Surya-2 权重是否被候选版本支持。
#
# 不做任何 pull / run，只输出结论，供决定是否升级（升级流程见 README「vLLM 升级」）。
#
# 用法：
#   .\probe-vllm-versions.ps1                      # 探测当前配置 + 已下载镜像
#   .\probe-vllm-versions.ps1 -Candidates v0.31.0,v0.20.1

[CmdletBinding()]
param(
    [string[]]$Candidates = @(),
    [string]$Repo = 'vllm/vllm-openai'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"

Write-Host "== 1) 当前配置 ==" -ForegroundColor Cyan
Write-Host "env.ps1 里的镜像（$VllmImage）"
Write-Host "容器名 $VllmContainer   端口 $VllmHostPort   服务模型 id $VllmServedModelId"

Write-Host "`n== 2) 本机已有镜像 ==" -ForegroundColor Cyan
$local = @()
try {
    $local = docker images $Repo --format '{{.Repository}}:{{.Tag}}  {{.Size}}  {{.CreatedSince}}'
} catch {
    Write-Warning "docker 不可用（Docker Desktop 没启动？）：$($_.Exception.Message)"
}
if ($local) { $local | Write-Host } else { Write-Host "（没有 $Repo 的本地镜像）" }

Write-Host "`n== 3) Surya-2 权重与依赖 ==" -ForegroundColor Cyan
$hf = $MarkerHf
$cfgFiles = @()
if (Test-Path $hf) {
    $cfgFiles = Get-ChildItem -Path $hf -Recurse -Filter 'config.json' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'surya' }
}
if ($cfgFiles) {
    foreach ($c in $cfgFiles) {
        try {
            $j = Get-Content $c.FullName -Raw -Encoding utf8 | ConvertFrom-Json
            $arch = if ($j.architectures) { $j.architectures -join ',' } else { '?' }
            Write-Host ("{0}`n    model_type={1}  architectures={2}  torch_dtype={3}" -f `
                $c.FullName.Replace($hf, 'models\huggingface'), $j.model_type, $arch, $j.torch_dtype)
        } catch {
            Write-Host "$($c.FullName) 解析失败：$($_.Exception.Message)"
        }
    }
} else {
    Write-Warning "在 $hf 下没找到 surya 的 config.json（权重还没下载？）"
}
Write-Host "提示：Surya-2 是 Qwen3-VL 系列，vLLM 需要能加载同一 model_type 的版本；"
Write-Host "      升级后必须验证 /v1/models 的服务模型 id 仍是 $VllmServedModelId。"

Write-Host "`n== 4) 候选镜像探测（docker manifest inspect，只读）==" -ForegroundColor Cyan
if (-not $Candidates) {
    Write-Host "未指定 -Candidates；示例：-Candidates v0.20.1,v0.31.0"
    Write-Host "（vLLM 发布页：https://github.com/vllm-project/vllm/releases）"
} else {
    foreach ($tag in $Candidates) {
        $ref = "$Repo`:$tag"
        $out = docker manifest inspect $ref 2>&1
        if ($LASTEXITCODE -eq 0) {
            $size = ''
            try {
                $json = $out | ConvertFrom-Json
                if ($json.manifests) {
                    $size = "多架构（$($json.manifests.Count) 个 manifest）"
                } elseif ($json.layers) {
                    $layers = ($json.layers | Measure-Object -Property size -Sum).Sum
                    $size = "约 {0:N1} GB" -f ($layers / 1GB)
                }
            } catch { }
            Write-Host ("  [可用] {0}  {1}" -f $ref, $size) -ForegroundColor Green
        } else {
            Write-Host ("  [不可用/拉不动] {0}：{1}" -f $ref, (($out | Select-Object -First 2) -join ' ')) -ForegroundColor Yellow
        }
    }
}

Write-Host "`n== 5) GPU 与自检命令 ==" -ForegroundColor Cyan
Write-Host "GPU 自检：docker run --rm --gpus all nvidia/cuda:13.4.2-base-ubuntu22.04 nvidia-smi"
Write-Host "升级步骤（详见 README）："
Write-Host "  1) 备份当前可用镜像 tag：$VllmImage"
Write-Host "  2) .\start-vllm.ps1 -Image <新 tag> [-NoMtp]"
Write-Host "  3) .\convert.ps1 -PdfPath <pdf> -ChunkSize 40 -OnlyChunks 0 -Force   # 40 页试点"
Write-Host "  4) python .\qa.py ... 对比 pages_below_coverage / total_lost_chars / inline_* 与耗时"
Write-Host "  5) 任一项变差就 .\start-vllm.ps1 -Image $VllmImageCurrent 回退"
