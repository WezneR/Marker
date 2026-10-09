# -*- coding: utf-8 -*-
"""诊断：InlineFormulaRecoveryProcessor 的块级 OCR 请求为什么会卡住。

逐步加压，每一步都打印耗时与输出长度：
  1) 单页（20 页中的 1 页）单个文本块识别；
  2) 同页 2 个块；
  3) 4 个块（复现卡住时的并发度）；
  4) 全部命中页（更大的批）。

用法：
  python scripts\\tests\\probe_recognition_batch.py [--pages 9-15] [--sizes 1,2,4]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

PDF = r"F:\Marker\input\因子投资方法与实践.pdf"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=PDF)
    ap.add_argument("--pages", default="9-15")
    ap.add_argument("--sizes", default="1,2,4")
    ap.add_argument("--token-budget", type=int, default=1600)
    args = ap.parse_args()
    lo, hi = [int(x) for x in args.pages.split("-")]

    from surya.layout.schema import LayoutBox, LayoutResult

    from marker.builders.document import DocumentBuilder
    from marker.builders.layout import LayoutBuilder
    from marker.builders.line import LineBuilder
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.providers.registry import provider_from_filepath
    from marker.schema import BlockTypes
    from marker.schema.labels import block_type_to_surya_label

    config = {
        "page_range": list(range(lo, hi + 1)),
        "mode": "balanced",
        "disable_ocr": True,
        "LayoutBuilder_disable_ocr": True,
        "force_layout_block": "Text",
        "output_format": "markdown",
    }
    conv = PdfConverter(config=config, artifact_dict=create_model_dict())
    provider = provider_from_filepath(args.pdf)(args.pdf, config)
    document = DocumentBuilder(config)(
        provider,
        conv.resolve_dependencies(LayoutBuilder),
        conv.resolve_dependencies(LineBuilder),
        None,
    )
    rec = conv.artifact_dict["recognition_model"]

    # 收集候选块：(page, block)
    candidates = []
    for page in document.pages:
        for block in page.children or []:
            if block.block_type in (BlockTypes.Text, BlockTypes.TextInlineMath):
                if (block.raw_text(document) or "").strip():
                    candidates.append((page, block))
    print(f"候选块 {len(candidates)} 个（页 {lo}-{hi}）")

    for size in [int(x) for x in args.sizes.split(",")]:
        batch = candidates[:size]
        if not batch:
            continue
        images, layouts = [], []
        for page, block in batch:
            image = page.get_image(highres=True)
            image_size = image.size
            poly = block.polygon.rescale(page.polygon.size, image_size).fit_to_bounds(
                (0, 0, *image_size))
            images.append(image)
            layouts.append(LayoutResult(
                bboxes=[LayoutBox(polygon=poly.polygon,
                                  label=block_type_to_surya_label(block.block_type),
                                  raw_label=str(block.block_type),
                                  position=0,
                                  count=block.layout_token_count or args.token_budget)],
                image_bbox=[0, 0, *image_size],
            ))
        print(f"\n--- 批大小 {size}：图片尺寸 {[im.size for im in images]}，"
              f"token 预算 {args.token_budget} ---")
        t0 = time.time()
        try:
            results = rec(images=images, layout_results=layouts, full_page=False)
        except Exception as e:
            print(f"    异常：{type(e).__name__}: {str(e)[:200]}  ({time.time()-t0:.1f}s)")
            continue
        dt = time.time() - t0
        for (page, block), r in zip(batch, results):
            blocks = getattr(r, "blocks", [])
            html = (blocks[0].html if blocks else "") or ""
            print(f"    p{page.page_id} {type(r).__name__} blocks={len(blocks)} "
                  f"html={len(html)} 字，前 60 字符={html[:60]!r}")
        print(f"    用时 {dt:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
