# F:\Marker\scripts\start-vllm.ps1
# 启动 vLLM 推理容器并验证 Surya 的「附着契约」。
#
# 为什么不用 Marker 自带 spawn：surya 源码里硬编码 `docker run --runtime nvidia`，
# 而在本机（Docker Desktop / WSL2）实测 --runtime nvidia 会把 /dev/dxg 丢掉、GPU 不可用；
# `--gpus all` 才是可用路径。所以我们自己起容器，再用 SURYA_INFERENCE_URL 让 Marker 附着。

[CmdletBinding()]
param(
    [string]$Image                 = '',
    [string]$ContainerName         = 'surya-vllm-8080',
    [int]$HostPort                 = 8080,
    [int]$MaxNumSeqs               = 32,
    [int]$MaxNumBatchedTokens      = 8192,
    [int]$MaxModelLen              = 18000,
    [double]$GpuMemUtil            = 0.85,
    [int]$StartupTimeoutMinutes    = 25,
    [switch]$Mtp,        # 打开 MTP 投机解码（默认关闭，见下方注释）
    [switch]$NoMtp,      # 兼容旧写法：等价于默认行为
    [switch]$Force       # 强制重建同名容器
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"

# 默认镜像：env.ps1 里的 $VllmImageCurrent（现为 v0.20.1，可覆盖）
if (-not $Image) { $Image = $VllmImageCurrent }

# MTP 默认关闭：实测 vLLM v0.20.1 与 v0.31.0 在 MTP 投机解码下，收到成批的
# "整页图 + 大 token 预算"请求后引擎会卡死（生成吞吐掉到 0.1 token/s，
# 之后连一个 4 token 的纯文本请求也永久无响应，必须重建容器）。
# 症状与排查过程见 docs\loss-report.md 的「vLLM 升级实测」一节。
# 需要 MTP 时显式 -Mtp，并注意同时把 InlineFormulaRecoveryProcessor_recognition_batch_size 调小。

function Test-VllmHealth([string]$Base) {
    try { return (Invoke-WebRequest "$Base/health" -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 }
    catch { return $false }
}

function Write-ContainerLog([string]$Name, [int]$Tail) {
    # $ErrorActionPreference='Stop' 时，原生命令往 stderr 写一个字就抛 NativeCommandError，
    # `docker logs ... | Write-Host` 只来得及打印首行就中断 —— 容器启动失败时这恰好丢掉了
    # 唯一有用的那行（本次只剩 "usage: vllm serve ..."，真正的 argparse 错误行没打出来）。
    # 临时降为 Continue 才能看全，和 convert.ps1 处理 marker/surya stderr 的办法一致。
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & docker logs --tail $Tail $Name 2>&1 | Write-Host }
    finally { $ErrorActionPreference = $prev }
}

$base = "http://127.0.0.1:$HostPort"

# 已有同名容器时的处理
$existing = docker ps -a --filter "name=^/$ContainerName$" --format '{{.Names}}'
if ($existing) {
    if (-not $Force -and (Test-VllmHealth $base)) {
        Write-Host "[start-vllm] 容器 $ContainerName 已在运行且健康，直接复用。"
        exit 0
    }
    Write-Host "[start-vllm] 移除旧容器 $ContainerName"
    docker rm -f $ContainerName | Out-Null
}

$dockerArgs = @(
    'run', '-d', '--name', $ContainerName,   # 注意：故意不加 --rm，容器崩溃时要能 docker logs 查因
    '--gpus', 'all',            # 本机唯一可用路径（务必不要改成 --runtime nvidia）
    '--ipc=host',
    '-v', "${MarkerHf}:/root/.cache/huggingface",
    '-p', "${HostPort}:8000",
    $Image,
    # v0.31+ 建议模型名用位置参数（--model 已被标记为将来移除），两种版本都兼容
    'datalab-to/surya-ocr-2',
    '--served-model-name', 'datalab-to/surya-ocr-2',   # 必须与 SURYA_MODEL_CHECKPOINT 一致
    '--no-enforce-eager',
    '--max-num-seqs', "$MaxNumSeqs",
    '--max-num-batched-tokens', "$MaxNumBatchedTokens",
    '--dtype', 'bfloat16',
    '--max-model-len', "$MaxModelLen",
    '--gpu-memory-utilization', "$GpuMemUtil",
    '--enable-prefix-caching',
    # 必须经 ConvertTo-NativeJsonArg（见 env.ps1）：PS 5.1 会吃掉裸 JSON 里的双引号，
    # vLLM 收到 {min_pixels:3136,max_pixels:6291456} 后 json.loads 失败、argparse 报错 exit 2。
    '--mm-processor-kwargs', (ConvertTo-NativeJsonArg '{"min_pixels":3136,"max_pixels":6291456}')
)
# MTP 默认关闭（见文件头注释）。-NoMtp 保留为兼容写法。
if ($Mtp -and -not $NoMtp) {
    $dockerArgs += @('--speculative-config', (ConvertTo-NativeJsonArg '{"method":"mtp","num_speculative_tokens":2}'))
}

Write-Host "[start-vllm] docker $($dockerArgs -join ' ')"
& docker @dockerArgs
if ($LASTEXITCODE -ne 0) { throw "docker run 失败（exit $LASTEXITCODE）" }

# 等健康：首次启动要下载 1.31 GB 模型 + 加载权重，给足时间
$deadline = (Get-Date).AddMinutes($StartupTimeoutMinutes)
while ((Get-Date) -lt $deadline) {
    if (Test-VllmHealth $base) { break }
    $status = (docker ps -a --filter "name=^/$ContainerName$" --format '{{.Status}}')
    if (-not $status) {
        throw "容器 $ContainerName 已消失（未运行且不存在）"
    }
    if ($status -notmatch '^Up') {
        Write-Host "[start-vllm] 容器未在运行：$status —— 最后 80 行日志：" -ForegroundColor Red
        Write-ContainerLog $ContainerName 80
        throw "容器启动失败（$status）"
    }
    Start-Sleep -Seconds 5
}
if (-not (Test-VllmHealth $base)) {
    Write-ContainerLog $ContainerName 100
    throw "等待 $StartupTimeoutMinutes 分钟仍未健康。若日志提示 speculative/MTP 相关错误，请加 -NoMtp 重试。"
}

# 校验附着契约：/v1/models 的 id 必须等于 datalab-to/surya-ocr-2
$served = (Invoke-RestMethod "$base/v1/models" -TimeoutSec 20).data.id
if ($served -ne 'datalab-to/surya-ocr-2') {
    throw "model id 不匹配：服务返回 '$served'，但 Surya 要求 'datalab-to/surya-ocr-2'（否则 attach 会抛 SpawnError）"
}

Write-Host "[start-vllm] 就绪：$base/v1    model=$served"
try { docker exec $ContainerName nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv } catch { }
