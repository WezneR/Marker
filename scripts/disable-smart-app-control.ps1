# F:\Marker\scripts\disable-smart-app-control.ps1
# 关闭 Smart App Control（SAC / 智能应用控制）—— 本机 Marker 转换报
#   OSError: [WinError 4551] An Application Control policy has blocked this file
#   ImportError: DLL load failed while importing _fast_dict: An Application Control policy has blocked this file
# 的根因修复。
#
# 背景：SAC 于 2026-10-04 由「评估模式(2)」自动转为「强制模式(1)」，此后 venv 里
# 未签名、且云端信誉（ISG）尚未判定的原生模块首次加载就会被拦（torch 的
# c10_cuda.dll / shm.dll、sklearn 的 _fast_dict.pyd / manifold/_utils.pyd）。
# SAC 没有白名单/排除项机制，只能在「关」与「拦」之间二选一。
#
# 用法（必须以管理员身份运行）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File F:\Marker\scripts\disable-smart-app-control.ps1
#   ... -DryRun     只打印将要做什么，不写注册表
#   ... -Revert     改回强制模式（1）——官方 UI 关掉后无法再打开，但注册表可以改回
#
# 注册表取值（HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy\VerifiedAndReputablePolicyState）：
#   1 = On      强制模式，拦截未签名/无信誉的原生代码
#   2 = Evaluate 评估模式，只记录不拦截
#   0 = Off     关闭
# 注意：改完需要重启，Code Integrity 服务才会按新值加载策略。

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Revert
)

$ErrorActionPreference = 'Stop'

$keyPath  = 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy'
$valueName = 'VerifiedAndReputablePolicyState'
$target   = if ($Revert) { 1 } else { 0 }
$logPath  = 'F:\Marker\tmp\sac-disable.log'
$lines    = New-Object System.Collections.Generic.List[string]

function Say([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    $lines.Add($line)
    Write-Host $line
}

try {
    $ident = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($ident)
    $elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    Say "用户=$($ident.Name)  已提升=$elevated"

    $before = (Get-ItemProperty -Path $keyPath -Name $valueName -ErrorAction SilentlyContinue).$valueName
    Say "当前 $valueName = $before  (1=On 2=Evaluate 0=Off)"

    if (-not $elevated) {
        throw "需要管理员权限：请用「以管理员身份运行」的 PowerShell 执行本脚本。"
    }
    if ($before -eq $target) {
        Say "已是目标值 $target，无需修改。$([Environment]::NewLine)重启后生效。"
        return
    }
    if ($DryRun) {
        Say "DryRun：将会把 $valueName 由 $before 改为 $target（未写入）。"
        return
    }

    Set-ItemProperty -Path $keyPath -Name $valueName -Value $target -Type DWord
    $after = (Get-ItemProperty -Path $keyPath -Name $valueName).$valueName
    Say "已写入：$valueName = $after"

    if ($after -ne $target) {
        throw "写入后读回 $after，与目标 $target 不符，未生效。"
    }
    if ($target -eq 0) {
        Say "SAC 已置为 Off。请重启计算机，重启后未签名原生模块不再被拦。"
    }
    else {
        Say "SAC 已置回 On(1)。重启后恢复拦截。"
    }
}
catch {
    Say "失败：$($_.Exception.Message)"
    $code = 1
}
finally {
    if (Test-Path (Split-Path $logPath -Parent)) {
        $lines | Out-File -FilePath $logPath -Encoding utf8
    }
}

if ($code) { exit $code }
