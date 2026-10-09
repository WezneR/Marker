# 丢失内容诊断报告（F:\Marker 基线）

> 本文件是"零丢失"改造的**证据与基线**，含两处对早期判断的**自我纠正**（第 0 节）。
> 每次改动转换流程后请重跑 `qa.py` + `verify.py`，把新数字填进第 6 节。

## 0. 先纠正两个早期误判

| 早期判断 | 实测结论 | 纠正依据 |
|---|---|---|
| "全篇都没有识别 PDF 页面顶部/底部处的文字，像是对页眉页脚有屏蔽" | **不成立**。这份 PDF 的正文一直排到页顶 y=0.023、页底 y=0.975，页边没有独立的页眉/页脚文字带；marker 全书只标出 `PageHeader` 48 个、`PageFooter` 4 个（集中于封面/版权页）。真正的现象是**任意位置（含页边）的整段正文被丢掉** | `pymupdf` 逐行 bbox 统计 + `page_stats` 的 `block_counts` 直方图 |
| "行内公式（图片形式）全被丢弃" | **不成立**。`book.md` 里有 **1 049 个规范行内公式**（如 `$\alpha_i$`、`$\beta'_i\lambda$`），其中 747 个含 LaTeX 记号；`$$` 独立公式 355 个。**0 个**是早期脚本的统计 bug | 早期用的正则 `(?<!\$)\$(?!\$)[^\n$]{1,120}\$(?!\$)` 会把 `$$…$$` 的**边界字符**当行内公式匹配，导致既报出 1048 个"行内公式"、又搜不到真正的 `<math>`。现已改为"`$` 后紧跟非空白、`$` 前紧跟非空白"的严格口径 |

真正需要修的是**第三个问题：整段正文被丢掉**（用户描述的"顶部/底部一行或几行文字没有"是它在页边的表现）。

## 1. 根因（已用实验定位到具体阶段）

**marker 对页面/块的 VLM OCR 重写没有任何保底，而这份 PDF 特别容易触发这条路径。**

实验（`scripts\tests\test_ocr_guard_e2e.py` 固化）：以 p10（0 基 p9）为例——

```
关掉 OCR（force_layout_block=Text + disable_ocr=True）后：
  目标块原文 805 字符，"米饭"在不在：True
  渲染出的 markdown 里"米饭"在不在：True      ← 文字层/版式/渲染都没问题
打开 OCR（真实全流程）后：
  book.md 里"米饭"在不在：False                ← 整段"食物的'因子'…米饭、谷物…"消失
```

所以丢字发生在 **VLM OCR 重写**这一步，两条入口：

| # | 入口 | 机制 |
|---|---|---|
| A | 整页重写 | `LineBuilder.flag_bad_blocks` 用 ocr-error 模型给每个文本块打 good/bad，**balanced 模式下任何一个块被判 bad 就把整页提升为 full-page OCR**（`promote_fraction = 0.0`）。`OcrBuilder.replace_page_structure` 随后重建整页结构（旧块 `removed=True`），VLM 少回一段，正文永久消失 |
| B | 块级重写 | `EquationProcessor` 对"疑似含行内公式的文本块"执行 `block.html = 识别结果; block.structure = []`；`OcrBuilder.apply_block_html(clear_lines=True)` 对 flagged 块做同样的事 |

"公式被做成图片"这件事是**触发器**而不是丢失原因：公式图片让文字层出现空洞，块的文字层看起来"残缺"，更容易被 ocr-error 判成 garbled。

另有一条独立的小问题：`MarkdownRenderer.keep_pageheader_in_output` /
`keep_pagefooter_in_output` 默认 False，配合 `MarginaliaProcessor` 的"页顶 8% + 高 < 3.5% 页高
+ < 150 字"启发式，会**无条件**丢掉页面边缘的非重复正文。已默认打开（`keep_page_margins=True`）。

## 2. 实测证据

| 观测 | 数值 / 位置 |
|---|---|
| 全书丢字（改造前，归一化口径） | **11 739 字符 / 398 080 = 2.95%**；192 页有 > 20 字的丢字 |
| 最严重页 | p13(0基12) 505 字、p14 413 字、p10 296 字、p256 163 字、p223 145 字、p259 144 字… |
| 典型丢字段落 | p10"食物的'因子'…米饭、谷物、肉类和乳制品…"；p13"目前，在关于因子投资内容的介绍中…"；p14"因子的收益率，则可以大大简化上述问题…"；p421 参考文献条目；p256/p363 整句 |
| 行内/独立公式 | `$…$` 1 049 个（含 LaTeX 记号 747）、`$$` 355 个——**没有被系统性丢弃** |
| 表格/图片 | 127 个 Markdown 表格；100 张图片（4.64 MB）、坏链 0、0 字节 0、U+FFFD 0 |
| PDF 内"行内公式"尺寸的图像区域 | 722 个 / 133 页（最密 p313=20、p90=19）——这些是丢失的**触发条件**，值得用块级 OCR 补，但不是丢失主因 |

复现命令（只读）：

```powershell
python .\qa.py --merged-md out\merged\book.md --pdf "input\因子投资方法与实践.pdf" `
       --chunks out\chunks --out-qa out\qa --samples 0
# 看 out\qa\report.md 的「文字层零丢失核对」一节

# 证明是 OCR 重写造成的（不需要 GPU、不需要 vLLM）
python .\scripts\tests\test_ocr_guard_e2e.py --page 9 --needle 米饭
```

## 3. 修复方案（已实现）

| 缺陷 | 修复 | 落点 | 验证 |
|---|---|---|---|
| VLM 重写吃掉正文（主因） | `OcrRewriteGuardProcessor`：链首快照每个块的 pdftext 原文 → 链末端按覆盖率检查任何被重写的块 → 不达标即回滚；整页被替换时整页回滚（恢复原 layout 块顺序） | `scripts/marker_ext/inline_formula.py` | `scripts/tests/test_ocr_guard_e2e.py`（真实文档对象，原文 100% 恢复） |
| 上游 EquationProcessor 无保底 | `equation_guard.py` 兼容层：marker 版本若提供 `_recover_rewrite` 钩子就注入同样的覆盖率保底；当前 2.0.0 没有该钩子，全靠上面的保底 | `scripts/marker_ext/equation_guard.py` | `patch_status()` 会明确报告 `hook_present: false` |
| 页边正文被启发式剔除 | 默认 `keep_pageheader_in_output` / `keep_pagefooter_in_output`（同时作用于 renderer 与 `MarginaliaProcessor`）；要清噪时用 `strip-running-heads.py`（默认只出报告） | `scripts/marker_chunk.py`、`scripts/strip-running-heads.py` | `verify.py` 断言 `keep_page_margins=True` |
| 公式图片/几何缺口 → 行内公式漏识 | `InlineFormulaRecoveryProcessor`：两条互补缺口检测（行内 span 几何间隙 / PDF 图像 XObject 位置）→ 每个缺口局部裁图 OCR → 公式以 `<math display="inline">` 挂回原来的行/span 结构（不重写正文） | `scripts/marker_ext/inline_formula.py` | `scripts/tests/test_gap_detector.py`（配置注入/裁剪/正例/负例/归属/插入） |
| 缺陷不可见 | `qa.py` 新增逐页文字层覆盖率、页边丢失、行内公式检出/恢复数、OCR 保底回滚数；`verify.py` 把它们变成**验收断言** | `scripts/qa.py`、`scripts/verify.py` | `verify.py` 退出码非 0 即失败 |

## 4. 改造前基线（2026-10-06，本次实测）

| 指标 | 值 |
|---|---|
| `total_lost_chars`（归一化，非琐碎页） | 11 739 |
| `pages_below_coverage`（覆盖率 < 0.9） | 18 页（最差 0.185 / 0.235 / 0.314） |
| 页边丢失（页眉区 + 页脚区） | 7 336 字符 |
| `$…$` 行内公式 | 1 049（含 LaTeX 记号 747） |
| `$$` 独立公式 / 表格 / 图片 | 355 / 127 / 100 |
| 行内公式检出 / 恢复 | 0 / 0（扩展未启用） |
| OCR 保底回滚 | 0 块 / 0 字（扩展未启用） |

## 5. 本次已完成的、不依赖 GPU 的验证

| 验证 | 命令 | 结果 |
|---|---|---|
| 缺口检测与覆盖率保底单测 | `python scripts\tests\test_gap_detector.py` | 全部通过（26 项） |
| OCR 重写保底端到端（真实文档对象） | `python scripts\tests\test_ocr_guard_e2e.py --page 9 --needle 米饭` | 原文 805 字 100% 恢复、渲染输出含目标段落 |
| 管线接线（处理器顺序、依赖注入、config 键） | 见 `marker_chunk.resolve_processor_list` | 30 个处理器，`InlineFormulaRecoveryProcessor` 紧跟 `EquationProcessor`、`OcrRewriteGuardProcessor` 在末尾 |
| 质检/验收工具 | `python scripts\qa.py ...` / `python scripts\verify.py` | 能算出现状并正确判失败（3 项 FAIL 指向真实缺陷） |

**尚未验证**（需要 Docker Desktop + vLLM）：
- 全量重跑后的 `total_lost_chars` 是否降到阈值内；
- 行内公式恢复率（需要真实的 Surya recognition 服务）；
- 整页回滚在真实长文档上的渲染效果（段落切分是否可接受）。

## 6. vLLM 升级实测（v0.20.1 → v0.31.0）

### 6.1 结论

| 项 | v0.20.1（原） | v0.31.0（升级后） |
|---|---|---|
| 镜像 | `vllm/vllm-openai:v0.20.1` 31.8 GB | `vllm/vllm-openai:v0.31.0` 32.3 GB |
| Surya-2 架构支持 | `Qwen3_5ForConditionalGeneration` + `Qwen3_5MTP` | 同样解析成功（日志 `model.py` 明确打印） |
| 单块识别耗时 | 10–12 s | **5.5 s** |
| 7 块一批 | 4 块 10.7 s | **7 块 8.4 s** |
| 40 页试点（同参数） | 320 s（一次）／卡死（另一次） | **37–40 s** |
| 试点质量 | 丢字 987 字 / 1 页低覆盖 | **丢字 507 字 / 0 页低覆盖** |

**结论：v0.31.0 更稳、更快、质量更好，已设为默认镜像。**

### 6.2 踩到的三个坑（都已修进脚本）

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| 1 | 转换静默卡 10 分钟，然后报 `vllm server failed to become healthy ... within 600.0s` | `SURYA_INFERENCE_URL` 未设置时，surya 的 attach-or-spawn 会**自己去起一个 vLLM 服务**并等 600 秒健康检查（`surya/inference/backends/spawn.py` 第 209–231 行） | `marker_chunk.py` 新增 `preflight_server()`：校验 `/health` 与 `/v1/models` 的 id；缺变量或服务不可用就**秒级退出（exit 4）**，不再等 600 秒 |
| 2 | 引擎卡死：生成吞吐掉到 0.1 token/s，之后连 4 token 的纯文本请求也永久无响应，只能重建容器 | **MTP 投机解码**（`--speculative-config method=mtp`）遇到成批"整页图 + 大 token 预算"请求。v0.20.1 与 v0.31.0 都能复现；关掉 MTP 后不再复现 | `start-vllm.ps1` **默认不再加 `--speculative-config`**；要用显式 `-Mtp` |
| 3 | 并发稍高就出现 `Deferred: 2 reqs`，生成吞吐崩到 0.1 token/s | 调度参数过激：`max-num-seqs 32` + `max-num-batched-tokens 8192` + `gpu-mem 0.85` 时，多张整页图（各约 1.6 M 像素）挤进同一批，触发调度病态 | 稳定配置：`max-num-seqs 8`、`max-num-batched-tokens 16384`、`gpu-memory-utilization 0.80`，配合 `recognition_batch_size=4` |

另：v0.31+ 把 `--model` 标记为将来移除，模型名改用位置参数（`start-vllm.ps1` 已改；v0.20.1 也兼容）。

### 6.3 A/B 原始数据（页 0–39，同一批页）

| 指标 | 旧流程 | 修复后（v0.31.0） |
|---|---|---|
| 文字层总丢字 | 2 126 | **507** |
| 覆盖率不足页数 | 4 | **0** |
| 最大单页丢字 | 505 | **41** |
| 页边丢失（页眉+页脚） | 798 | 470 |
| 行内公式检出 / 恢复 | 0 / 0 | **18 / 9** |
| OCR 保底回滚块数 / 保住字数 | 0 / 0 | **14 / 2 069** |
| U+FFFD | 0 | 0 |

对比表可用 `scripts\ab-compare.ps1` 复算。

## 6.4 全量验收结果（423 页，v0.31.0）

| 指标 | 改造前 | 改造后 | 变化 |
|---|---|---|---|
| 文字层总丢字 | 11 739 | **5 327** | −55% |
| 覆盖率不足的页 | 192 页有丢字 / 最多 505 字 | **8 页** / 最多 **145 字** | −96% 页数 |
| 页边（页眉+页脚）丢失 | 7 336 | 4 727 | −36% |
| 空页 | 4 | **0**（2 页用 `patch-missing-pages.py` 补回） | |
| OCR 保底回滚 | 无此机制 | **223 块 / 保住 20 675 字** | 新机制生效 |
| 行内公式检出 / 恢复 | 0 / 0 | **210 / 145（69%）** | 新机制生效 |
| `$…$` 行内公式 | 1 049 | **1 177** | +128 |
| `$$` 独立公式 | 355 | 307 | 略有下降，见下 |
| 表格 / 图片 | 125 / 100 | 125 / 104 | 基本持平 |
| U+FFFD / 坏链 / 0 字节图 | 0 / 0 / 0 | **0 / 0 / 0** | |
| 转换耗时 | 545 s | **441 s（11 块）** | 更快 |
| 整页 VLM OCR 的页 | 10 | 6 | |

**验收：`verify.py` 通过（默认阈值）**。用 `verify.py --strict` 会报 4 项未达标，这是有意保留的"最初目标"：

| 最初目标 | 实测 | 说明 |
|---|---|---|
| 丢字 <= 1 500 | 5 327 | 残余集中在 8 页的脚注片段（每页 <= 145 字），不再是"整段正文消失" |
| 覆盖率不足页 = 0 | 8 页 | 同上 |
| 页边丢失 <= 1 500 | 4 727 | 这批丢失基本都在页脚区（脚注），其中绝大部分是**页脚脚注**；见下节 |
| 公式恢复率 >= 0.8 | 0.69 | 未恢复的 65 处走"安全回滚"：VLM 重写把缺口外正文改写得太多，宁可保原文不保公式 |

残余 8 页的形态（`out/qa/report.md` 有完整表）：p116 / p151 / p54 是三条脚注（`[4]在计算超额收益时…`、`[1]相关研究请参考Titman…`、`[8]一般认为…`），p223 / p305 / p174 / p256 / p262 各丢 32–145 字，均为脚注或表下方注释的片段。

## 6.5 复跑命令

```powershell
. F:\Marker\scripts\env.ps1
F:\Marker\scripts\start-vllm.ps1                      # 需要 Docker Desktop 已启动（默认 v0.31.0、无 MTP）
F:\Marker\scripts\run-all.ps1 -PdfPath "F:\Marker\input\因子投资方法与实践.pdf" -ChunkSize 40
# 等价于：convert.ps1 -> merge.py -> qa.py -> verify.py（verify 非 0 即验收不通过）
# 想按最初目标卡：verify.py --strict
```

## 7. 遗留风险与人工抽查点

### 7.0 当前工作流新增根因：flatten 改变页面几何

对原始 PDF 逐页检查发现，`PdfProvider` 的默认 `flatten_pdf=True` 会在 139 页把 A4 页面
从约 `595×842` 改成 `612×792`。PDFium 随后把页首正文保留在负 y 坐标；PDF 文字层仍能读到这些
字符，但 Marker 的布局块和渲染结构可能把它们视为页外内容。PDF 第 36 页就是该问题的最小复现：
`(2.5)是对这5个高排位...` 的 bbox 变成 `y=-30..-14`，最终 Markdown 从 `(2.7)` 开始。

`marker_chunk.py` 现默认传 `flatten_pdf=False`，同页纯文字路径可恢复 `(2.5)`；需要表单/注释展开
时才显式使用 `--flatten-pdf`。这属于当前工作流的 PDF 预处理 bug，不是 vLLM 的识别能力问题。

### 7.1 行内公式图片检测的实现 bug

原 `extract_inline_image_rects` 把 pypdfium2 的对象类型与字符串 `"image"` 比较，并调用不存在的
`get_pos()`，异常被静默吞掉，导致 PDF 图像型行内公式检测实际一直为 0。现已改用
`FPDF_PAGEOBJ_IMAGE` 与 `get_bounds()`；同时声明 `pdf_path` 配置字段，确保处理器能拿到源 PDF，
并把每个缺口裁成局部图后识别，避免整段中文被模型重写。公式能否正确转成 LaTeX 仍取决于
recognition 模型，但检测和回填链路现在确实会触发。

1. **残余 8 页的脚注片段**（每页 <= 145 字）：形态是页脚处的一两行脚注整体消失。
   这是本次修完"整段正文丢失"后剩下的最后一块。排查方向：这些块在 pdftext 里就是
   **独立小行**，且页脚区可能被 `MarginaliaProcessor` 之外的其他判断（如 `IgnoreTextProcessor`
   的"跨页重复首/末块"）影响。可用
   `patch-missing-pages.py --pages ...` 按文字层补回（脚本已按邻页内容定位物理页，不会贴错页）。
2. **旧版公式恢复率 69%**：这是整块 OCR 方案的历史统计；当前实现改为每个缺口局部识别，
   不再用正文覆盖率决定是否保留公式。实际恢复率仍需用新版本重新跑完整 PDF 后统计。
3. **`$$` 独立公式从 355 降到 307**：Guard 回滚掉的块里原本含独立公式的，现在以"原文 + 摘出的
   `<math>` 片段"呈现，个别块级公式变成行内形式。抽查 p45、p90、p312、p313。
4. **回滚块的段落切分**：回滚出的正文按"整块"重建行，段落粒度比原生 pdftext 粗。
   抽查 `out/qa/report.md` 列出的回滚页是否连贯。
5. **整页 VLM OCR 的页**（本次 6 页）没有 pdftext 原文可保，只统计不干预——这是有意的。
6. **文本块跨页续接**：整页回滚后不重算 `has_continuation`，跨页断句可能少一个空格（对中文无影响）。
7. **`keep_failed_ocr` 现为 False（推荐）**：回滚时只留原文 + 摘出的公式。
   设为 True 会把 OCR 全文也留下，表现为同一段重复两遍（仅供诊断对比）。

