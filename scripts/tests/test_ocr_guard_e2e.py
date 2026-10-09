#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\tests\\test_ocr_guard_e2e.py —— OCR 重写保底的端到端回归测试。

不需要 GPU / vLLM / Docker：用 marker 的文字层路径（force_layout_block=Text +
disable_ocr=True）构建**真实 Document 对象**，然后用真实的 OcrBuilder 写入路径模拟
"VLM 重写后吃掉整段正文"，再验证 OcrRewriteGuardProcessor 能把原文一字不少地救回来。

这是对"236/423 页共丢约 1.2 万字"那个缺陷的直接回归测试（见 docs/loss-report.md）。

用法：
  python scripts\\tests\\test_ocr_guard_e2e.py [--pdf <pdf>] [--page 9] [--needle 米饭]
缺省用 F:\\Marker\\input 下的第一份 PDF；找不到就跳过（exit 0）。
"""
from __future__ import annotations

import argparse
import glob
import html
import os
import re
import sys

os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def default_pdf() -> str:
    here = os.path.dirname(HERE)
    root = os.path.dirname(here)
    found = sorted(glob.glob(os.path.join(root, "input", "*.pdf")))
    return found[0] if found else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="")
    ap.add_argument("--page", type=int, default=9, help="0 基页号")
    ap.add_argument("--needle", default="米饭", help="必须被救回来的文字片段")
    args = ap.parse_args()

    pdf = args.pdf or default_pdf()
    if not pdf or not os.path.exists(pdf):
        print("[skip] 找不到 PDF（用 --pdf 指定），跳过。")
        return 0

    from marker.builders.document import DocumentBuilder
    from marker.builders.layout import LayoutBuilder
    from marker.builders.line import LineBuilder
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.providers.registry import provider_from_filepath
    from marker.renderers.markdown import MarkdownRenderer
    from marker.schema import BlockTypes

    from marker_ext.inline_formula import OcrRewriteGuardProcessor

    config = {
        "page_range": [args.page],
        "mode": "balanced",
        "disable_ocr": True,                 # 不调用 VLM
        "LayoutBuilder_disable_ocr": True,    # 走轻量版式检测
        "force_layout_block": "Text",         # 每页一个 Text 块，复现"块被整段重写"
        "output_format": "markdown",
    }
    conv = PdfConverter(config=config, artifact_dict=create_model_dict())
    provider = provider_from_filepath(pdf)(pdf, config)
    document = DocumentBuilder(config)(
        provider,
        conv.resolve_dependencies(LayoutBuilder),
        conv.resolve_dependencies(LineBuilder),
        None,
    )

    page = document.pages[0]
    target = next((b for b in page.children
                   if b.block_type in (BlockTypes.Text, BlockTypes.TextInlineMath)), None)
    if target is None:
        print("[skip] 该页没有 Text 块，换一页再试（--page）。")
        return 0

    before = re.sub(r"\s+", "", html.unescape(target.raw_text(document) or ""))
    print(f"== 页 {args.page}：目标块 {target.block_type}，原文 {len(before)} 字符，"
          f"含 {args.needle!r}: {args.needle in before} ==")
    if args.needle not in before:
        print(f"[skip] 该页文字层不含 {args.needle!r}，换 --page/--needle。")
        return 0

    # 1) 链首快照（真实链上由 marker_chunk.py 的 _install_shadow_snapshot 完成）
    guard = OcrRewriteGuardProcessor({})
    guard.ensure_snapshot(document)
    check("快照记录了 pdftext 原文", len(guard._shadow) >= 1, guard._shadow)

    # 2) 用真实的 OcrBuilder 写入路径模拟"VLM 少回内容"
    for line in target.contained_blocks(document, (BlockTypes.Line,)):
        line.removed = True
    target.html = "<p>只回了一小段不相干的识别结果。</p>"
    target.structure = []
    page.text_extraction_method = "surya"
    after = re.sub(r"\s+", "", target.raw_text(document) or "")
    check("模拟重写确实吃掉了原文", args.needle not in after,
          f"重写后仍有 {len(after)} 字符")

    # 3) 保底必须把原文救回来
    guard(document)
    restored = re.sub(r"\s+", "", html.unescape(target.raw_text(document) or ""))
    check("回滚后目标文字回到 raw_text", args.needle in restored)
    check("回滚后原文覆盖率 = 100%", before in restored,
          f"restored={len(restored)} before={len(before)}")
    check("报告记录了回滚与保住的字数",
          guard.report()["totals"]["rolled_back"] >= 1
          and guard.report()["totals"]["chars_saved"] >= len(before),
          guard.report()["totals"])

    # 4) 渲染输出也要有
    md = MarkdownRenderer(config)(document).markdown
    flat = re.sub(r"\s+", "", md)
    check("渲染出的 markdown 含目标段落", args.needle in flat, f"{len(flat)} 字符")

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败 -> {FAILS}")
        return 1
    print("结果：全部通过（OCR 重写保底有效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
