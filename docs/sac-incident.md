# Smart App Control 拦截 Marker 转换（2026-10-04 起，2026-10-08 处理）

## 1. 结论

`convert.ps1` 每块在 4 秒内失败、汇总「成功 0 块」，**根因不在 marker/surya/vLLM，而在本机
Windows 的 Smart App Control（SAC，智能应用控制）**：

- SAC 于 **2026-10-04 17:38** 起进入**强制模式**，开始拦截未签名、且拿不到云端信誉（ISG）判定的
  原生模块；
- `env\` 这个 venv 里的 `.pyd`/`.dll` **绝大多数是未签名的**（抽样：torch `c10.dll`、
  numpy `_pocketfft_umath`、PIL `_imaging`、scipy `_hierarchy`、cv2、tokenizers、
  safetensors、pymupdf 全部 `NotSigned`），因此**每次首次加载一个新模块都可能被拦**；
- 拦在 `import` 阶段，Python 抛 `OSError [WinError 4551]` 或 `ImportError: DLL load failed`，
  marker 连入口都没进去，所以是「秒级失败」，重试 3 次没有任何意义。

处置：把 `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy\VerifiedAndReputablePolicyState`
置为 `0`（Off）。随后 `chunk-00`（0–39 页）**39 s 转换通过**，产出 114 963 字节 Markdown +
4 张图片 + 扩展报告。

## 2. 症状

```
[convert] chunk-00  页 0-39  第 1/3 次尝试  2026-10-08T07:34:18
WARNING: [convert] chunk-00 失败（用时 4s），见 F:\Marker\logs\chunk-00.log
（3 次尝试都是 4s 失败）
[convert] 汇总：成功 0 块，跳过 0 块，失败 1 块，总耗时 0s
```

`logs\chunk-00.log` 里两种错误交替出现（取决于先加载到哪个模块）：

```
OSError: [WinError 4551] An Application Control policy has blocked this file.
  Error loading "F:\Marker\env\Lib\site-packages\torch\lib\c10_cuda.dll" or one of its dependencies.

ImportError: DLL load failed while importing _fast_dict: An Application Control policy has blocked this file.
```

> 排查提示：`chunk-*.log` 是 **UTF-8 头部 + UTF-16LE 正文的混合编码**（PS 5.1 的 `*>>`
> 固定写 UTF-16LE），直接按文本读会因 NUL 字节被判为二进制。读之前先剥 NUL：
> `(Get-Content -LiteralPath logs\chunk-00.log -Raw -Encoding UTF8) -replace "`0",''`
> 这一条已在 2026-10-08 改成显式管道 + UTF-8 追加（见 `scripts\convert.ps1`）。

## 3. 定位（可复核的命令与证据）

### 3.1 权威日志：Code Integrity 事件

```powershell
Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-CodeIntegrity/Operational'; Id=3077} |
    Sort-Object TimeCreated |
    ForEach-Object { "{0:MM-dd HH:mm:ss}  {1}" -f $_.TimeCreated, ($_.Message -replace "`r?`n",' ') }
```

被拦文件（共 24 条事件，21 条文件名可解析，全部位于 `F:\Marker\env`）：

| 时间 | 文件 | 次数 |
|---|---|---|
| 10-04 17:38:51 | `…\Microsoft VS Code\Code.exe`（由 `OpenWith.exe` 加载，非本项目） | 1 |
| 10-06 03:13:13 | `sklearn\manifold\_utils.cp311-win_amd64.pyd` | 1 |
| 10-07 05:03–05:04 | （事件措辞不同，未解析出文件名） | 2 |
| 10-07 17:10:45–17:12:55 | `torch\lib\c10_cuda.dll` | 9 |
| 10-08 07:06:34 | `torch\lib\shm.dll` | 1 |
| 10-08 07:12:12–07:38:34 | `sklearn\utils\_fast_dict.cp311-win_amd64.pyd` | 10 |

配套事件 `3118 Smart App Control Block Deteails` 给出了判定过程：

```
DefenderMadeCloudCall : false      # 云信誉查询没有真正发生
TTLValid              : false
CachedDefenderTrust   : 122199
SHA256FlatHash        : C1BC818D6E1EB63FE609F3D10F57DE32065E209AFBA4CDA42CAA047CDE0CC0DB
PreviousEnablementState : 2        # 之前是「评估」
EnablementSwitchType    : 1        # 切换为「强制」
```

即：**文件未签名 + 拿不到云端信誉 → 拦**。`3077` 事件里给出的策略 ID
`{0283ac0f-fff1-49ae-ada1-8a933130cad6}` 就是 SAC 自己的策略，与
`C:\Windows\System32\CodeIntegrity\CiPolicies\Active\{0283AC0F-FFF1-49AE-ADA1-8A933130CAD6}.cip`
对应（`Get-AuthenticodeSignature` 对这些 `.pyd` 一律返回 `NotSigned`）。

### 3.2 开关状态

```powershell
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' |
    Select-Object VerifiedAndReputablePolicyState, SAC_PreviousState, SAC_EnforcementReason
```

拦截期间的取值：`VerifiedAndReputablePolicyState = 1`（On）、`SAC_PreviousState = 2`（评估）、
`SAC_EnforcementReason = 1` —— 说明是 **Windows 自行从「评估」升级为「强制」**，不是有人手动打开。

### 3.3 交叉验证

```powershell
F:\Marker\env\Scripts\python.exe -c "import torch; print(torch.__version__)"          # 07:43 通过
F:\Marker\env\Scripts\python.exe -c "import sklearn.cluster"                          # 07:36 仍被拦
```

同一个 venv、同一台机器：`c10_cuda.dll` 在 10-07 被拦了 9 次，10-08 却能加载
（信誉后来补上了），而 `_fast_dict.pyd` 还在被拦 —— 这就是「看运气」的来源，也是不能靠
「多试几次」解决的原因。

## 4. 处理

1. 2026-10-08 把 SAC 置为 Off：

   ```powershell
   # 需要管理员；重启后由 Code Integrity 按新值加载策略
   Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' `
       -Name VerifiedAndReputablePolicyState -Value 0 -Type DWord
   ```

   本次实际是**由用户在 Windows 安全中心关闭**（脚本准备的 UAC 提权调用被取消）；
   结果一致：`VerifiedAndReputablePolicyState = 0`，`SAC_EnforcementReason = 6`。
   最后一次拦截停在 `07:38:34`，`07:41` 起导入链放行。

2. 备用脚本：`scripts\disable-smart-app-control.ps1`

   ```powershell
   # 预演（只打印，不写注册表）
   powershell -NoProfile -ExecutionPolicy Bypass -File F:\Marker\scripts\disable-smart-app-control.ps1 -DryRun
   # 关闭（需管理员）
   powershell -NoProfile -ExecutionPolicy Bypass -File F:\Marker\scripts\disable-smart-app-control.ps1
   # 改回强制模式（官方 UI 关掉后不再提供开关；注册表方式可改回，属非官方途径）
   powershell -NoProfile -ExecutionPolicy Bypass -File F:\Marker\scripts\disable-smart-app-control.ps1 -Revert
   ```

   脚本写到 `F:\Marker\tmp\sac-disable.log`，只动这一个注册表值，不碰策略文件。

3. 脚本侧加固（`scripts\convert.ps1`）：
   - 启动时打印 `[convert] Smart App Control: On(强制)/Off(关闭)/Evaluate(评估)`；
   - 检测到强制模式时提前 `Write-Warning`，直说「每块几秒内必败、重试无意义」并给出处置；
   - 块日志从「UTF-8 头 + UTF-16LE 正文」改为统一 UTF-8 追加。

## 5. 验证

```
[convert] chunk-00  页 0-39  第 1/3 次尝试  2026-10-08T07:45:57
[convert] chunk-00 完成，用时 39s
[convert] 汇总：成功 1 块，跳过 0 块，失败 0 块，总耗时 39s
```

产物（`out\chunks\chunk-00\因子投资方法与实践\`）：

| 文件 | 大小 |
|---|---|
| `因子投资方法与实践.md` | 114 963 B |
| `因子投资方法与实践_ext_report.json` | 11 873 B |
| `因子投资方法与实践_meta.json` | 43 499 B |
| `_page_12_Diagram_6.jpeg` 等 4 张图 | 39–91 KB |

复现/自查命令：

```powershell
# 1) SAC 是否在强制模式
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy').VerifiedAndReputablePolicyState   # 期望 0

# 2) 近 30 分钟是否还有 App Control 拦截
Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-CodeIntegrity/Operational'; Id=3077;
    StartTime=(Get-Date).AddMinutes(-30)} -ErrorAction SilentlyContinue | Measure-Object   # 期望 Count = 0

# 3) 导入链是否放行（转换失败时最先崩在这里）
F:\Marker\env\Scripts\python.exe -c "import sklearn.cluster, sklearn.manifold, marker.converters.pdf; print('OK')"

# 4) 真正的验收
F:\Marker\scripts\convert.ps1 -PdfPath "F:\Marker\input\因子投资方法与实践.pdf" -ChunkSize 40 -OnlyChunks 0
```

## 6. 未采用的方案

- **等云端信誉判定后重试**：10-07→10-08 的实测说明不可靠（同一个 venv 里，
  `c10_cuda.dll` 会被放过，`_fast_dict.pyd` 不会），而且每遇到一个新的原生模块都要再赌一次。
- **给 SAC 加排除项**：SAC 没有白名单/排除项机制（Defender 的排除项对它无效），
  只能在「关」和「拦」之间二选一。
- **改到 WSL2/Docker 里跑 marker**：SAC 只作用于 Windows，容器内不受影响，
  是保留 SAC 时的正解；但需要重建一套 Linux 环境（本机推理已经在 vLLM 容器里，
  marker 侧改成容器运行是可行路径，只是改造量与验证量都不小）。

## 7. 附带发现：DSH 沙箱的工作区写入授权缺了一部分

排查期间在沙箱内（workspace-write）向 `F:\Marker\tmp`、`out`、`logs`、`models`、`scripts`
写文件一律 `Access to the path … is denied`，但 `F:\Marker\docs` 可以写。

- ACL 对比：沙箱写入身份 `S-1-4-110731847-16059662` 的允许项只出现在 `F:\Marker\docs`
  （以及根目录 `F:\Marker` 本身），其余子目录没有 —— 已存在的子目录没有得到这条继承授权，
  所以沙箱内写工作区被拒。
- 用 DSH 自带的 `diagnose-windows-sandbox-acl` 脚本诊断结论为 `NOT_THIS_CLASS`
  （`F:\Marker` 本身 `WRITE_DAC`/`WRITE_OWNER` 齐全、也没有外来应用包的权限项），
  报告见 `docs\acl-recovery\acl-report-*.jsonl`；该脚本不做递归改子目录权限，故未修复。
- 影响与绕开：需要写入工作区的命令（本次两次转换）改用**一次完整权限运行**即可；
  这是 DSH 沙箱侧的问题，与本项目脚本无关。
