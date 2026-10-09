#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\marker_chunk.py —— 用 marker 的 Python API 转换一个页区间。

为什么不用 marker_single：
  1) 需要注入两个自研处理器（行内公式缺口恢复 + OCR 重写保底），并给
     `/document/Text/0` 这类块挂上"pdftext 影子原文"；
  2) 配置里有 keep_pageheader_in_output 等开关，Windows PowerShell 5.1 传 JSON/引号
     容易出错，走 Python dict 最稳；
  3) 输出目录结构要与 marker_single 完全一致（<out>/<stem>/<stem>.md|_meta.json|图片），
     这样 merge.py / qa.py 不用改。

用法（一般由 convert.ps1 调用）：
  python marker_chunk.py --pdf <pdf> --start 12 --end 14 --output-dir <chunk 目录>
      [--mode balanced] [--paginate-output] [--no-keep-page-margins]
      [--no-inline-formula-recovery] [--no-ocr-guard] [--flatten-pdf]
      [--report <json 路径>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from marker.config.parser import ConfigParser  # noqa: E402
from marker.converters.pdf import PdfConverter  # noqa: E402
from marker.logger import configure_logging, get_logger  # noqa: E402
from marker.models import create_model_dict  # noqa: E402
from marker.output import save_output  # noqa: E402
from marker.renderers.markdown import MarkdownRenderer  # noqa: E402
from marker.util import classes_to_strings  # noqa: E402

from marker_ext import (  # noqa: E402
    InlineFormulaRecoveryProcessor,
    OcrRewriteGuardProcessor,
    equation_guard,
)

configure_logging()
logger = get_logger()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Convert a page range with F:\\Marker extensions.")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--start", type=int, required=True, help="起始页（0 基，含）")
    ap.add_argument("--end", type=int, required=True, help="结束页（0 基，含）")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--mode", default="balanced", choices=["balanced", "fast"])
    ap.add_argument("--paginate-output", "--paginate_output", dest="paginate_output",
                    action="store_true", help="写入 {页号}--- 页标记")
    # 每个开关都同时接受连字符与下划线两种写法：convert.ps1 用下划线，
    # 手工调用时用连字符，避免 argparse 直接拒绝。
    ap.add_argument("--keep-page-margins", "--keep_page_margins", dest="keep_margins",
                    action="store_true", default=True,
                    help="保留页眉/页脚（默认开，确保一个字不丢）")
    ap.add_argument("--no-keep-page-margins", "--no_keep_page_margins", dest="keep_margins",
                    action="store_false")
    ap.add_argument("--inline-formula-recovery", "--inline_formula_recovery",
                    dest="inline_recovery", action="store_true", default=True,
                    help="恢复以图片形式嵌入的行内公式（默认开）")
    ap.add_argument("--no-inline-formula-recovery", "--no_inline_formula_recovery",
                    dest="inline_recovery", action="store_false")
    ap.add_argument("--ocr-guard", "--ocr_guard", dest="ocr_guard", action="store_true",
                    default=True, help="OCR 重写的覆盖率保底（默认开）")
    ap.add_argument("--no-ocr-guard", "--no_ocr_guard", dest="ocr_guard", action="store_false")
    ap.add_argument("--report", default="", help="扩展统计写入的 json 路径")
    ap.add_argument("--set", dest="overrides", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="覆盖任意 marker/扩展配置项，如 --set InlineFormulaRecoveryProcessor_recognition_batch_size=4"
                         "（可重复；值会按 JSON 解析，失败则当字符串）")
    ap.add_argument("--force-ocr", "--force_ocr", dest="force_ocr", action="store_true",
                    help="强制整页 OCR（走文字层不可靠的块时用；会重建整页结构）")
    ap.add_argument("--flatten-pdf", "--flatten_pdf", dest="flatten_pdf",
                    action="store_true",
                    help="显式 flatten PDF 表单/注释（默认关闭；部分 PDF 会被 PDFium 改变页面尺寸）")
    ap.add_argument("--debug", action="store_true", help="落盘 marker 调试图/json")
    return ap


def resolve_processor_list(converter_cls, inline_cls, guard_cls, eq_cls):
    """按默认顺序构造处理器列表：EquationProcessor 之后插入行内公式恢复处理器，末尾加保底。

    顺序很关键：行内公式恢复必须早于 TextProcessor / MarginaliaProcessor /
    ReferenceProcessor，否则它们已经改过块结构；保底必须在最后，才能看到所有重写结果。
    """
    out = []
    for cls in converter_cls.default_processors:
        out.append(cls)
        if cls is eq_cls:
            out.append(inline_cls)
    if guard_cls not in out:
        out.append(guard_cls)
    return out


def preflight_server() -> int:
    """确认推理服务可附着，否则**立刻失败**。

    这一步很关键：surya 的 attach-or-spawn（surya/inference/backends/spawn.py）在
    SURYA_INFERENCE_URL 未设置时，会自己去起一个 vLLM 服务并等 600 秒健康检查。
    实测忘设该变量时，整个块转换会静默卡住 10 分钟然后报
    "vllm server failed to become healthy ... within 600.0s"。
    这里提前探活 + 校验 model id，把问题在 10 秒内暴露。
    """
    import urllib.error
    import urllib.request

    url = os.environ.get("SURYA_INFERENCE_URL", "").strip()
    if not url:
        print("[marker_chunk] 环境变量 SURYA_INFERENCE_URL 未设置：surya 会自己 spawn 一个服务并"
              "等 600 秒。请先 `. F:\\Marker\\scripts\\env.ps1`，再跑 start-vllm.ps1。", file=sys.stderr)
        return 4
    base = url.rstrip("/")
    health = base[: -len("/v1")] if base.endswith("/v1") else base
    expected = os.environ.get("SURYA_MODEL_CHECKPOINT", "").strip() or "datalab-to/surya-ocr-2"

    def _get(u, timeout=10):
        with urllib.request.urlopen(u, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")

    try:
        status, _ = _get(f"{health}/health")
        if status != 200:
            raise RuntimeError(f"/health -> {status}")
    except Exception as e:
        print(f"[marker_chunk] 推理服务不可用：{health}/health（{e}）。先跑 start-vllm.ps1。", file=sys.stderr)
        return 4
    try:
        _status, body = _get(f"{base}/models")
        served = json.loads(body).get("data", [{}])[0].get("id", "")
    except Exception as e:
        print(f"[marker_chunk] 读取 {base}/models 失败：{e}", file=sys.stderr)
        return 4
    if served != expected:
        print(f"[marker_chunk] 服务模型 id 不匹配：服务={served!r} 期望={expected!r}"
              f"（必须一致，否则 surya 会抛 SpawnError）", file=sys.stderr)
        return 4
    logger.info("[marker_chunk] 推理服务就绪：%s  model=%s", base, served)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if not os.path.exists(args.pdf):
        print(f"[marker_chunk] 找不到 PDF：{args.pdf}", file=sys.stderr)
        return 2
    rc = preflight_server()
    if rc != 0:
        return rc
    page_range = f"{args.start}-{args.end}"

    config = {
        "page_range": page_range,
        "output_format": "markdown",
        "mode": args.mode,
        "output_dir": args.output_dir,
        "paginate_output": bool(args.paginate_output),
        # 页眉/页脚：同时作用于 MarginaliaProcessor 与 MarkdownRenderer（已实测两处都会生效）
        "keep_pageheader_in_output": bool(args.keep_margins),
        "keep_pagefooter_in_output": bool(args.keep_margins),
        # 我们的处理器配置（assign_config 按类名前缀也能识别，这里用全局键更直观）
        "InlineFormulaRecoveryProcessor_enabled": bool(args.inline_recovery),
        "OcrRewriteGuardProcessor_enabled": bool(args.ocr_guard),
        # 行内公式检测需要直接读 PDF 的图像 XObject（文字层完全跳过公式图片的 PDF 只能这样发现）
        "pdf_path": os.path.abspath(args.pdf),
        # 这本书部分页面在 PDFium flatten 后会从 A4 变成 Letter，导致顶部文字坐标为负数。
        # 默认保留原始页面几何；需要表单/注释 flatten 时由调用者显式打开。
        "flatten_pdf": bool(args.flatten_pdf),
    }
    if args.force_ocr:
        config["force_ocr"] = True
    # --set KEY=VALUE：值按 JSON 解析（4 -> int，true -> bool，其余当字符串）
    for item in args.overrides:
        if "=" not in item:
            print(f"[marker_chunk] --set 需要 KEY=VALUE 形式：{item!r}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        try:
            parsed = json.loads(v)
        except Exception:
            parsed = v
        config[k.strip()] = parsed
        logger.info("[marker_chunk] 配置覆盖：%s = %r", k.strip(), parsed)
    if args.debug:
        config.update({
            "debug_pdf_images": True,
            "debug_layout_images": True,
            "debug_json": True,
            "debug_data_folder": args.output_dir,
        })

    cp = ConfigParser({**config, "output_dir": args.output_dir})
    cfg = cp.generate_config_dict()

    t0 = time.time()
    models = create_model_dict()
    # InlineFormulaRecoveryProcessor 的构造参数名是 recognition_model，
    # resolve_dependencies 会按名字从 artifact_dict 里取；create_model_dict() 已包含它。
    missing = [k for k in ("recognition_model",) if k not in models]
    if missing:
        print(f"[marker_chunk] 模型字典缺少 {missing}，无法恢复行内公式", file=sys.stderr)
        return 3

    from marker.processors.equation import EquationProcessor

    # 关键：PdfConverter 的 processor_list 只接受**字符串路径**（内部走
    # marker.util.strings_to_classes），传类对象会报 "'BlockRelabelProcessor' object
    # has no attribute 'rsplit'"。renderer 同理，也要字符串。
    processor_list = classes_to_strings(
        resolve_processor_list(PdfConverter, InlineFormulaRecoveryProcessor,
                               OcrRewriteGuardProcessor, EquationProcessor)
    )

    converter = PdfConverter(
        config=cfg,
        artifact_dict=models,
        processor_list=processor_list,
        renderer=f"{MarkdownRenderer.__module__}.{MarkdownRenderer.__name__}",
    )

    inline = _find_processor(converter, InlineFormulaRecoveryProcessor)
    guard = _find_processor(converter, OcrRewriteGuardProcessor)
    _install_shadow_snapshot(converter, guard)

    logger.info(
        "[marker_chunk] 页 %s 模式 %s 保留页边=%s 行内公式恢复=%s OCR保底=%s 处理器 %d 个",
        page_range, args.mode, args.keep_margins, args.inline_recovery, args.ocr_guard,
        len(converter.processor_list),
    )

    rendered = converter(args.pdf)
    # marker 的输出目录是 <output_dir>/<stem>/（见 ConfigParser.get_output_folder）
    out_folder = cp.get_output_folder(args.pdf)
    stem = os.path.splitext(os.path.basename(args.pdf))[0]
    save_output(rendered, out_folder, stem)

    report = {
        "pdf": os.path.abspath(args.pdf),
        "page_range": page_range,
        "mode": args.mode,
        "flatten_pdf": bool(args.flatten_pdf),
        "keep_page_margins": bool(args.keep_margins),
        "inline_formula_recovery": bool(args.inline_recovery),
        "ocr_guard": bool(args.ocr_guard),
        "seconds": round(time.time() - t0, 1),
        "equation_guard_patch": equation_guard.patch_status(),
        "inline_formula": inline.report() if inline else None,
        "ocr_guard_report": guard.report() if guard else None,
    }
    report_path = args.report or os.path.join(args.output_dir, f"{stem}_ext_report.json")
    # 默认路径要和 marker 的输出层级一致（<output_dir>/<stem>/<stem>_ext_report.json）：
    # marker 的 ConfigParser.get_output_folder 会再建一层以文件名为名的子目录，
    # 直接写 args.output_dir 下的路径会 FileNotFoundError。
    os.makedirs(os.path.dirname(os.path.abspath(report_path)) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    tot = (report["inline_formula"] or {}).get("totals", {})
    gt = (report["ocr_guard_report"] or {}).get("totals", {})
    logger.info(
        "[marker_chunk] 完成：%ss；行内公式 检出 %s / 恢复 %s；OCR 保底回滚 %s 块（%s 字）→ %s",
        report["seconds"], tot.get("detected", 0), tot.get("recovered", 0),
        gt.get("rolled_back", 0), gt.get("chars_saved", 0), report_path,
    )
    return 0


def _find_processor(converter, cls):
    for p in converter.processor_list:
        if isinstance(p, cls):
            return p
    return None


def _install_shadow_snapshot(converter, guard_processor) -> None:
    """让 OcrRewriteGuardProcessor.ensure_snapshot 在处理器链**第一步**执行。

    build_document 的顺序是：DocumentBuilder（layout/line/ocr builder + 重写）
    → 然后才跑 processor_list。所以必须把快照挂在链首才会拿到"重写前"的原文。
    """
    if guard_processor is None:
        return
    chain = converter.processor_list

    def snapshot_first(document, *a, **kw):
        guard_processor.ensure_snapshot(document)

    snapshot_first.__name__ = "shadow_snapshot"
    chain.insert(0, snapshot_first)


if __name__ == "__main__":
    sys.exit(main())
