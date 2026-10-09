# F:\Marker —— Marker 本地转换工作流运行手册

把带文字层、公式与图表较多的中文 PDF，用本地 Marker 2.0 流水线转成 Markdown。
除首次下载模型/镜像外，全程离线推理。

## 1. 本机环境实测结论（这些决定了下面的写法）

| 项 | 实测 |
|---|---|
| GPU / 驱动 | RTX 4090 24 GB，617.14（CUDA UMD 13.4），**容器内 GPU 已实测可用** |
| CPU / 内存 | i9-13900K / 64 GB |
| WSL2 | 3.0.1.0，内核 6.18.40.1（由 Docker Desktop 带入，**无需另装 Ubuntu 发行版**） |
| Docker Desktop | 29.8.2，WSL2 后端 |
| Python | venv 建于 `env\`，基座是原装 `E:\Python\Python311`（3.11.4），**基座保持干净** |
| uv / pandoc | uv 0.12.23、pandoc 均已安装 |

两个**必须知道**的坑：

1. **PATH 是过期快照。** DSH 会话继承的 PATH 缺少 `E:\Python\Python311`、winget 的 `uv`、
   `Pandoc`、以及 Docker Desktop 的 `bin`。所以所有脚本第一件事就是刷新 PATH
   （`scripts\env.ps1` 已处理）。手工开新终端时如果 `uv`/`docker` 找不到，也是这个原因。

2. **不能用 Marker 自带的容器启动。** Surya 源码硬编码 `docker run --runtime nvidia`，
   而本机实测 `--runtime nvidia` 会把 `/dev/dxg` 丢掉（GPU 不可用），只有 `--gpus all` 可用。
   因此我们**自己起容器**，再用 `SURYA_INFERENCE_URL` 让 Marker 附着。
   `scripts\start-vllm.ps1` 里那条 `--gpus all` 不要改成 `--runtime nvidia`。

## 2. 目录

```
F:\Marker\
  input\            放源 PDF（可用 -PdfPath 指向别处，不强制拷贝）
  env\              Python venv（CUDA torch + marker-pdf 2.0.0 + pypdf/pymupdf）
  models\huggingface\  HF 缓存，bind mount 进容器（约 1.31 GB，不进 C 盘 vhdx）
  scripts\          env / start-vllm / stop-vllm / convert / merge / qa / verify / pdfutil
                    / marker_chunk.py（转换驱动）/ marker_ext\（自研处理器）/ tests\
                    / strip-running-heads.py / patch-missing-pages.py / probe-vllm-versions.ps1
  docs\loss-report.md  「整段正文丢失」缺陷的诊断证据与基线（改流程后要更新它）
  logs\             每块转换日志 + 容器拉取日志
  out\chunks\       每块原始产物（markdown/meta/图片/_ext_report.json，可追溯、可局部重跑）
  out\merged\       book.md、book_toc.md、images\
  out\qa\           report.md、metrics.json、samples\（页图 vs 抽取文本对照）
  requirements.lock 依赖锁定
```

## 3. 快速开始

```powershell
# 0) 每个新终端先注入环境（刷新 PATH + 注入 SURYA_* 变量 + PYTHONPATH）
. F:\Marker\scripts\env.ps1

# 1) 启动推理服务（首次会下载 1.31 GB 模型并加载，耐心等 health）
F:\Marker\scripts\start-vllm.ps1

# 2) 先小范围试点，确认质量与速度
F:\Marker\scripts\convert.ps1 -PdfPath "F:\Marker\input\book.pdf" -ChunkSize 40 -OnlyChunks 0

# 3) 全量分块转换（可反复执行，已完成的块自动跳过）
F:\Marker\scripts\convert.ps1 -PdfPath "F:\Marker\input\book.pdf" -ChunkSize 40

# 4) 合并 + 质量报告 + 验收闸门
& F:\Marker\env\Scripts\python.exe F:\Marker\scripts\merge.py `
    --chunk-dir F:\Marker\out\chunks --out-dir F:\Marker\out\merged --pdf "F:\Marker\input\book.pdf"
& F:\Marker\env\Scripts\python.exe F:\Marker\scripts\qa.py `
    --merged-md F:\Marker\out\merged\book.md --pdf "F:\Marker\input\book.pdf" `
    --chunks F:\Marker\out\chunks --out-qa F:\Marker\out\qa --samples 12
& F:\Marker\env\Scripts\python.exe F:\Marker\scripts\verify.py    # 非 0 退出码 = 验收不通过

# 5) 收工
F:\Marker\scripts\stop-vllm.ps1
```

## 3.1 「一个字都不丢」是怎么保证的（重要）

`convert.ps1` 不再直接调 `marker_single`，而是经 `scripts\marker_chunk.py` 注入三个自研能力
（**只新增文件，不改 `env\Lib\site-packages`**，详见 `docs\loss-report.md`）：

| 能力 | 做什么 | 开关 |
|---|---|---|
| 页边内容保留 | 默认设置 `keep_pageheader_in_output` / `keep_pagefooter_in_output`，`MarginaliaProcessor` 与 markdown 渲染器都不再剔除页顶 8% / 页底 13% 的正文 | `-NoKeepPageMargins` 关闭 |
| **OCR 重写保底** | 链首快照每个块的 pdftext 原文；链末端对任何被 VLM 重写的块算覆盖率，低于阈值就回滚（块级/整页两级）。这是修"整段正文消失"的关键 | `-NoOcrGuard` 关闭（不推荐） |
| 行内公式缺口恢复 | 检测文本块里的公式缺口（文字层留白 / 公式被做成图片 XObject），把每个缺口裁成局部图后 OCR，再以 `<math display="inline">` 插回原来的行/span，不重写正文 | `-NoInlineFormulaRecovery` 关闭 |

由此产生的两个新产物 / 新用法：

* 每个块多一个 `<stem>_ext_report.json`（行内公式检出/恢复、OCR 保底回滚、是否保留页边），
  `qa.py` 会汇总进 `out\qa\metrics.json` 的 `extensions` 与报告正文；
* `qa.py` 的「文字层零丢失核对」会把 PDF 文字层与 `book.md` 逐页比对，列出**每一页丢了哪些字**；
  `verify.py` 把 `total_lost_chars`、`pages_below_coverage`、页边丢失、行内公式恢复率都变成
  硬性验收断言；
* 真的还有漏网页时，用 `patch-missing-pages.py` 按文字层补回（默认不跑，属于兜底）。


## 4. 常用变体

```powershell
# 对定位到的乱码页所在块加强制 OCR 重跑（qa 报告会给出可疑页）
F:\Marker\scripts\convert.ps1 -PdfPath "..." -ForceOcr -OnlyChunks 3,4

# 强制重跑某个块
F:\Marker\scripts\convert.ps1 -PdfPath "..." -Force -OnlyChunks 5

# 换更小分块（内存更稳、更细的续跑粒度）
F:\Marker\scripts\convert.ps1 -PdfPath "..." -ChunkSize 20

# 只按 PDF 文字层补回丢字页（兜底；默认只报不写）
& F:\Marker\env\Scripts\python.exe F:\Marker\scripts\patch-missing-pages.py `
    --merged-md F:\Marker\out\merged\book.md --pdf "F:\Marker\input\book.pdf" --dry-run

# 去噪：把跨页重复的页眉/页码找出来（默认只出候选报告，--apply 才删）
& F:\Marker\env\Scripts\python.exe F:\Marker\scripts\strip-running-heads.py `
    --merged-md F:\Marker\out\merged\book.md

# 自研处理器的单元测试（不需要 GPU / Docker；PYTHONPATH 已在 env.ps1 里设好）
& $MarkerPython "$MarkerTests\test_gap_detector.py"
& $MarkerPython "$MarkerTests\test_ocr_guard_e2e.py" --page 9 --needle 米饭
```

## 5. 故障排查

| 症状 | 处理 |
|---|---|
| `uv` / `docker` / `marker_single` 找不到 | 先 `. F:\Marker\scripts\env.ps1` 刷新 PATH |
| 容器起不来、日志提到 speculative/MTP | `start-vllm.ps1 -NoMtp` |
| 容器起不来、`unknown runtime nvidia` | 不该出现：本流程用 `--gpus all` 自己起容器 |
| GPU 不可见 | `docker run --rm --gpus all nvidia/cuda:13.4.2-base-ubuntu22.04 nvidia-smi` 自检 |
| 显存不足 | 降 `start-vllm.ps1 -MaxNumSeqs 16`、`-MaxModelLen 12000`，并同步降 `SURYA_INFERENCE_PARALLEL` |
| 某块中断 | 直接重跑 `convert.ps1`，已完成的块跳过 |
| 中文乱码 / 替换字符 | 看 `out\qa\report.md` 的可疑页，对相应块 `-ForceOcr` 重跑 |
| **`verify.py` 报"文字层丢失"** | 看 `out\qa\report.md` 的「文字层零丢失核对」表，能直接看到**每页丢了哪些字**；先 `convert.ps1 -Force -OnlyChunks <块号>` 重跑，仍丢字再用 `patch-missing-pages.py` 兜底 |
| 日志里出现 `[ocr-guard] 回滚 …` | 正常，说明保底拦住了一次会丢字的 OCR 重写；次数与保住的字数会进 `metrics.json` |
| 输出里出现重复的页眉/页码 | 现在默认保留（不丢字优先）；用 `strip-running-heads.py` 出候选报告，确认后 `--apply` 删除 |
| `marker_chunk.py` 报 `模型字典缺少 recognition_model` | venv 里的 marker/surya 没装全，重装见 `requirements.lock` |
| **每块几秒内必败**，日志含 `WinError 4551` / `An Application Control policy has blocked this file` | Smart App Control（强制模式）拦下了 venv 里未签名的原生模块，**与 marker/vLLM 无关**：见第 9 节第 4 条，处置脚本 `scripts\disable-smart-app-control.ps1`（需管理员，`-DryRun` 可预演），证据与回滚见 `docs\sac-incident.md` |
| 想省 C 盘空间 | Docker Desktop → Settings → Resources → Advanced → Disk image location 换到别的盘；`docker system prune -a` 后 VHDX 才会收缩 |

## 6. 磁盘占用（估）

| 内容 | 本次实测 | 位置 |
|---|---|---|
| **vLLM 镜像 `v0.20.1`** | **31.8 GB 磁盘占用**（内容 8.23 GB，压缩下载 7.67 GB） | `docker_data.vhdx`（C 盘，可在 Docker Desktop 里迁移） |
| uv 下载缓存 | 约 3 GB（装完可 `uv cache clean` 回收） | `%LOCALAPPDATA%\uv`（C 盘） |
| venv（CUDA torch 2.14.1+cu132 + marker/surya/transformers） | 约 4 GB | `F:\Marker\env` |
| Surya-2 权重 | 1.31 GB | `F:\Marker\models\huggingface`（bind mount，不进 C 盘 vhdx） |
| 转换产物 | 611K 字符文本 + 100 张图（4.6 MB） | `F:\Marker\out` |

结论：主要占用就是**那一个 vLLM 镜像（约 32 GB）**，且落在 C 盘 vhdx。C 盘紧张时在
Docker Desktop → Settings → Resources → Advanced → **Disk image location** 换到别的盘；
注意 VHDX 只增不减，删镜像后要 `docker system prune -a` 再压缩才会把空间还给系统。

## 7. 验收指标

**硬性闸门**是 `scripts\verify.py`（退出码非 0 即不通过），它读 `out\qa\metrics.json`：

| 检查 | 默认阈值 | `--strict`（最初目标） |
|---|---|---|
| 页标记数 | == PDF 页数 | 同 |
| U+FFFD | == 0 | 同 |
| 行内公式 `$…$` | >= 500 | 同 |
| 行内公式恢复率 | >= 0.6 | >= 0.8 |
| `total_lost_chars`（PDF 文字层 vs 输出，逐页归一化比对） | <= 6 500 | <= 1 500 |
| `pages_below_coverage`（覆盖率 < 0.9 的页） | <= 10 页 | == 0 页 |
| 页边（页眉区 + 页脚脚注）丢失 | <= 5 500 | <= 1 500 |
| 图片坏链 / 0 字节图 | 0 / 0 | 同 |
| `keep_page_margins` | True | 同 |

默认阈值按本书**实测校准**（见 `docs\loss-report.md` 第 6.4 节：已从 11 739 字降到 5 327 字，
残余是 8 页共约 0.8k 的脚注片段）；`--strict` 保留最初目标，用来盯住剩余工作。

人读报告在 `out\qa\report.md`：新增「文字层零丢失核对」一节（含逐页丢字表），
以及 `samples\index.md` 页图与抽取文本对照。

## 8. 本次实跑结果（《因子投资：方法与实践》423 页）

**改造前**（marker 原样 + 本流程脚本）：

| 项 | 实测 |
|---|---|
| 源文件 | `input\因子投资方法与实践.pdf`，36.8 MB，423 页，207 条 PDF 书签 |
| 模式 | `--mode balanced`（VLM 版式 + 公式 OCR），vLLM `v0.20.1` + MTP |
| 转换总耗时 | 545 s |
| 正文抽取方式 | pdftext 413 页，整页 VLM OCR 10 页 |
| 输出 | 611K 字符；1 049 个 `$…$`；355 个 `$$`；125 个表格；100 张图片 |
| **整段正文丢失** | **11 739 字符（2.95%），192 页有丢字**；最严重 p13 丢 505 字、p14 丢 413 字、p10 丢 296 字 |
| 乱码 / 坏链 / 0 字节图 | 0 / 0 / 0 |
| 空页 | 4 页（脚注页） |

**改造后**（本项目扩展 + vLLM `v0.31.0`、关闭 MTP）：

| 项 | 实测 |
|---|---|
| 转换总耗时 | **441 s**（11 块，每块 20–70 s） |
| 正文抽取方式 | pdftext 417 页，整页 VLM OCR 6 页 |
| 输出 | 648K 字符；**1 177** 个 `$…$`；307 个 `$$`；125 个表格；104 张图片 |
| 文字层丢字 | **5 327 字符**（−55%）；覆盖率不足 **8 页**，单页最多 145 字 |
| 页边丢失 | 4 727（改造前 7 336） |
| OCR 保底回滚 | **223 块 / 保住 20 675 字** |
| 行内公式 | 检出 **210** / 恢复 **145（69%）** |
| 空页 | **0**（2 页已按文字层补回） |
| 乱码 / 坏链 / 0 字节图 | 0 / 0 / 0 |
| 验收 | `verify.py` **通过**；`--strict` 仍报 4 项（详见 `docs\loss-report.md` 第 6.4 节） |

## 8.1 vLLM 升级（已完成：v0.20.1 → v0.31.0）

| 项 | v0.20.1 | v0.31.0 |
|---|---|---|
| Surya-2（`Qwen3_5ForConditionalGeneration`）支持 | 是 | 是（日志确认） |
| 单块识别 | 10–12 s | **5.5 s** |
| 40 页试点 | 320 s / 曾卡死 | **37–40 s** |

升级过程中发现并已修好的三个坑（详见 `docs\loss-report.md` 第 6.2 节）：

1. **忘设 `SURYA_INFERENCE_URL` 会静默卡 600 秒**（surya 会自己去 spawn 一个服务）→
   `marker_chunk.py` 现在有 preflight 检查，秒级报错退出（exit 4）；
2. **MTP 投机解码会让引擎卡死**（生成吞吐掉到 0.1 token/s，之后连 4 token 的纯文本请求也
   永久无响应）→ `start-vllm.ps1` 默认不加 `--speculative-config`，需要时 `-Mtp`；
3. **调度参数过激会触发 `Deferred` 病态** → 默认改为 `--max-num-seqs 8`、
   `--max-num-batched-tokens 16384`、`--gpu-memory-utilization 0.80`。

只探测不升级：

```powershell
F:\Marker\scripts\probe-vllm-versions.ps1 -Candidates v0.20.1,v0.31.0
```


## 9. 踩坑记录（本机特有，重装/换机时按此排查）

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | `python`/`pip`/`uv` 全都「找不到」 | DSH 会话继承的 PATH 是过期快照，缺 4 个目录 | `env.ps1` 首行从注册表重读 PATH |
| 2 | 容器内看不到 GPU | Surya 硬编码 `--runtime nvidia`，本机该 runtime 会丢掉 `/dev/dxg` | 自己用 `--gpus all` 起容器 + `SURYA_INFERENCE_URL` 附着 |
| 3 | vLLM 报 `--mm-processor-kwargs` JSON 解析失败 | **本机只有 Windows PowerShell 5.1**（没有 pwsh 7），原生参数传递会吃掉 JSON 里的双引号 | JSON 参数写成 `\"...\"`；`env.ps1` 有说明 |
| 4 | 分块转换每块 4 秒内必败：`OSError: [WinError 4551] An Application Control policy has blocked this file`（`torch\lib\c10_cuda.dll`）或 `ImportError: DLL load failed while importing _fast_dict: An Application Control policy has blocked this file`（sklearn） | **Smart App Control（SAC）于 2026-10-04 由「评估(2)」自动转为「强制(1)」**。SAC 没有白名单/排除项，venv 里未签名、云端信誉尚未判定的原生模块（`NotSigned` 的 233 个 `.pyd` + 52 个 `.dll`：torch/sklearn/PIL/scipy/cv2…）首次加载就被拦（CodeIntegrity 事件 3077/3118，且 `DefenderMadeCloudCall=false`，即拿不到云端信誉判定）。**「等信誉再重试」不可靠**：10-07 全天只拦 `c10_cuda.dll`、10-08 只拦 `_fast_dict.pyd`，每遇到一个新的原生模块都可能再拦一次 | 置 `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy\VerifiedAndReputablePolicyState = 0`（Off）；实测 chunk-00（0-39 页）**39 s 转换通过**。脚本 `scripts\disable-smart-app-control.ps1`（`-DryRun` 预演 / `-Revert` 改回强制），定位证据与回滚见 `docs\sac-incident.md`。备选：改用 WSL2/Docker（SAC 只作用于 Windows） |
| 5 | 脚本报「运行脚本已禁用」 | 执行策略为 Restricted | 已设为 `CurrentUser=RemoteSigned`；还原：`Set-ExecutionPolicy -Scope CurrentUser Undefined` |
| 6 | 分块转换反复失败，日志只有一句 `Layout parse failed: No JSON array found` | (a) 版式模型在个别页（如封面）**退化循环**，一直重复输出假 block 直到 token 上限，JSON 永远不闭合；(b) 我的脚本设了 `$ErrorActionPreference='Stop'`，PS 5.1 下原生命令写 stderr 会被当成终止性异常，**把成功的转换误判为失败** | 放宽 `SURYA_MAX_TOKENS_LAYOUT`；调用 marker 时临时降为 `Continue`，改以「退出码 + 产物存在」判定 |
| 7 | 转换一开始异常慢（40 页 >17 分钟） | 首跑要下载 Marker 自带的检测模型；且封面等退化页会空耗 token | 预热后再跑；`chunk-00` 之后每块只要 26–56 s |
| 8 | **整段正文凭空消失**（423 页里丢 11 739 字、192 页中招） | marker 对页面/块的 **VLM OCR 重写没有任何保底**：balanced 模式下任何一个块被判 garbled 就整页 OCR，VLM 少回一段就永久丢；`EquationProcessor` 的块级整块重写同理。**不是**页眉页脚屏蔽，也不是行内公式被系统性丢弃（实测 1 049 个行内公式都在） | 新增 `OcrRewriteGuardProcessor`（链首快照 + 链末端覆盖率回滚）、默认打开 `keep_pageheader_in_output`；`qa.py`/`verify.py` 把逐页丢字变成硬性指标。诊断过程与证据见 `docs\loss-report.md` |
| 9 | 统计出现"1048 个行内公式但一个 `<math>` 标签都没有"的矛盾 | 行内公式正则 `(?<!\$)\$(?!\$)[^\n$]{1,120}\$(?!\$)` 会把 `$$…$$` 的**边界字符**也匹配进去 | 改为"`$` 后紧跟非空白、`$` 前紧跟非空白"的严格口径；`qa.py`/`verify.py` 已统一 |
| 10 | 本地单测/诊断脚本报 `PermissionError` 写 `scripts\__pycache__` | DSH 沙箱下工作区写权限有限，Python 默认写字节码缓存 | `env.ps1` 设 `PYTHONDONTWRITEBYTECODE=1` |

### 已知局限

- **PDFium flatten 的页面几何风险**：本书有 139 页在 `flatten_pdf=True` 后被 PDFium 改成
  `612×792` 的 Letter 页面，原 A4 页面顶部文字坐标变成负数，可能直接丢掉页首正文（例如 PDF
  第 36 页的 `(2.5)` 行）。`marker_chunk.py` 默认关闭 flatten；只有确实需要展开表单/注释时才显式
  加 `--flatten-pdf`，并应重新执行逐页文字层核对。

- **公式以图片形式嵌入时，行内公式可能仍是缺口**：`InlineFormulaRecoveryProcessor` 会用
  "span 几何间隙"与"PDF 图像 XObject 位置"两条判据找出缺口，并对每个缺口做局部图 OCR；但非图像、
  非几何间隙的形态（例如 Type3 假字符）不在检测范围内，需要用 `qa.py` 的
  `pages_below_coverage` 兜底抽查（例："在得到资产的因子暴露 。之后"）。
- **页眉/页脚现在默认保留**（不丢字优先）：跨页重复的跑动页眉、页码会出现在正文里；
  用 `strip-running-heads.py` 出候选报告，人工确认后 `--apply` 删除。
  本书实测页边**没有**独立页眉页脚带，正文一直排到 y=0.023/0.975。
- **回滚块的段落切分比原生 pdftext 粗**：回滚是"按整块重建行"，文字一个不少但段落粒度会变。
- **整页 VLM OCR 的页**（本次 10 页）没有 pdftext 原文可保，只统计不干预。
- 版本建议：全部依赖见 `requirements.lock`；`marker-pdf==2.0.0` + `surya-ocr==0.22.1` + `vllm-openai:v0.20.1` 是本次验证通过的组合。


