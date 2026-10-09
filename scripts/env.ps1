# F:\Marker\scripts\env.ps1
# 统一环境入口。用法：. F:\Marker\scripts\env.ps1
#
# 两个必须做的事：
#  1) 刷新 PATH —— DSH 会话继承的 PATH 是过期快照，缺 E:\Python\Python311、winget uv、
#     Pandoc、以及 Docker Desktop 的 bin 目录，不刷新会出现「python/uv/docker 找不到」。
#  2) 注入 Marker/Surya 变量 —— 特别是 SURYA_INFERENCE_URL：让 Marker 附着到我们
#     自己用 --gpus all 起的 vLLM 容器，而不是走它内置的 --runtime nvidia（本机会丢 GPU）。

$env:PATH = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

# 本机只有 Windows PowerShell 5.1（没有 PowerShell 7），原生参数传递是旧规则：
# 传给 docker 的 JSON 参数里，双引号必须写成 \" 才会原样送达，否则会被吃掉，
# 例如 --mm-processor-kwargs '{"min_pixels":3136}' 变成 {min_pixels:3136}，vLLM 会报 JSON 解析错误
# （实测症状：容器 Exited (2)，docker logs 首行只有 "usage: vllm serve [model_tag] [options]"）。
# 所以原生命令的 JSON 参数一律经下面的 ConvertTo-NativeJsonArg 处理，不要在各脚本里手写转义。
# 实证：tmp\probe-args.ps1 的 [C] 裸 JSON → 引号被吃；[D] 转义后 → 原样送达。
function ConvertTo-NativeJsonArg([string]$Json) {
    if (-not $PSNativeCommandArgumentPassing -or $PSNativeCommandArgumentPassing -eq 'Legacy') {
        return ($Json -replace '"', '\"')   # PS 5.1 / Legacy：自己补上 \"
    }
    return $Json                            # PS 7 Standard 模式会正确转义，预转义反而多出反斜杠
}

# 控制台统一 UTF-8，避免中文路径/日志乱码
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# ---- 路径约定 ----
$MarkerRoot    = 'F:\Marker'
$MarkerEnv     = Join-Path $MarkerRoot 'env'
$MarkerPython  = Join-Path $MarkerEnv 'Scripts\python.exe'
$MarkerScripts = Join-Path $MarkerRoot 'scripts'
$MarkerLogs    = Join-Path $MarkerRoot 'logs'
$MarkerOut     = Join-Path $MarkerRoot 'out'
$MarkerChunks  = Join-Path $MarkerOut 'chunks'
$MarkerMerged  = Join-Path $MarkerOut 'merged'
$MarkerImages  = Join-Path $MarkerMerged 'images'
$MarkerQa      = Join-Path $MarkerOut 'qa'
$MarkerTmp     = Join-Path $MarkerRoot 'tmp'
$MarkerHf      = Join-Path $MarkerRoot 'models\huggingface'
$MarkerInput   = Join-Path $MarkerRoot 'input'

# ---- 推理服务 ----
# 版本升级实测（本次）：v0.31.0 与 v0.20.1 都能加载 Surya-2（Qwen3_5…），
# 但 v0.31.0 单块识别快一倍（5.5s vs 10–12s）、40 页试点 40s vs 320s，已切为默认。
# 注意：MTP 投机解码会让引擎卡死（见 start-vllm.ps1 注释与 docs\loss-report.md 第 6.2 节）。
$VllmImageCurrent  = 'vllm/vllm-openai:v0.31.0'
$VllmImage         = $VllmImageCurrent            # 当前使用；升级时改这里或给 start-vllm.ps1 -Image
$VllmContainer     = 'surya-vllm-8080'
$VllmHostPort      = 8080
$VllmServedModelId = 'datalab-to/surya-ocr-2'     # 必须与 SURYA_MODEL_CHECKPOINT 一致

# ---- F:\Marker 自研 marker 扩展（不改 site-packages）----
# marker_ext 放在 scripts\ 下，靠 PYTHONPATH 暴露给 marker_chunk.py / 单测。
$MarkerExt = Join-Path $MarkerScripts 'marker_ext'
$MarkerTests = Join-Path $MarkerScripts 'tests'
$env:PYTHONPATH = (@($MarkerScripts, $env:PYTHONPATH) | Where-Object { $_ }) -join ';'
$env:PYTHONDONTWRITEBYTECODE = '1'   # 避免在 scripts\ 下生成 __pycache__（工作区写权限受限）

# ---- Surya / Marker ----
$env:SURYA_INFERENCE_URL      = "http://127.0.0.1:$VllmHostPort/v1"
$env:SURYA_INFERENCE_BACKEND  = 'vllm'
$env:SURYA_MODEL_CHECKPOINT   = $VllmServedModelId
$env:SURYA_INFERENCE_PARALLEL = '32'              # 对齐 4090 的 max-num-seqs
$env:TORCH_DEVICE             = 'cuda'
$env:HF_HOME                  = $MarkerHf

# ---- 关键修复：版式 JSON 的 token 上限 ----
# surya 默认 SURYA_MAX_TOKENS_LAYOUT=3072。目录页/索引页/密集小字页需要按行输出很多 block，
# 超过 3072 会被截断成不完整 JSON，surya 抛 "Layout parse failed: No JSON array found"，
# 整个分块转换直接失败（实测 chunk-00 连续两次失败）。这里抬到 8192。
$env:SURYA_MAX_TOKENS_LAYOUT = '8192'

# 可选调节（默认值写在这里方便改动，一般不需要动）：
#   IMAGE_DPI=96            版式/文字检测用的低分辨率图（调低更快、更浅的版式）
#   IMAGE_DPI_HIGHRES=192   识别/表格用的高分辨率图（调低明显更快，细小字形精度下降）
#   SURYA_MAX_TOKENS_FULL_PAGE=12288   整页 OCR 的输出上限（无文字层页面用）

New-Item -ItemType Directory -Force -Path $MarkerHf, $MarkerLogs, $MarkerTmp | Out-Null
