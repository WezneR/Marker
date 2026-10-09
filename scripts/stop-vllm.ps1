# F:\Marker\scripts\stop-vllm.ps1 —— 停止并移除 vLLM 推理容器
[CmdletBinding()]
param([string]$ContainerName = 'surya-vllm-8080')

. "$PSScriptRoot\env.ps1"

$existing = docker ps -a --filter "name=^/$ContainerName$" --format '{{.Names}}'
if (-not $existing) { Write-Host "[stop-vllm] 没有名为 $ContainerName 的容器。"; exit 0 }

docker stop $ContainerName | Out-Null
Write-Host "[stop-vllm] 已停止 $ContainerName"
try { docker rm -f $ContainerName | Out-Null } catch { }
