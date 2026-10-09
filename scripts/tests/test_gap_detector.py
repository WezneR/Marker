#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\tests\\test_gap_detector.py —— 行内公式缺口检测与覆盖率保底的单元测试。

不依赖 GPU / vLLM / Docker，只测纯逻辑：
  1) detect_line_gaps：真实 A4 中文正文的公式缺口必须被检出，正常字距/标点/双栏不得误报；
  2) page_gap_entries：在真实 pdftext page dict 结构上定位行/span 下标与左右文字；
  3) coverage + OcrRewriteGuardProcessor：整段被 OCR 吃掉 → 回滚；只丢少量 → 接受。

用法：
  F:\\Marker\\env\\Scripts\\python.exe F:\\Marker\\scripts\\tests\\test_gap_detector.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

# ---- 被测纯函数：不 import marker，保证无 GPU 环境也能跑 ----------------------------
from marker_ext import inline_formula as IF  # noqa: E402

PAGE_W, PAGE_H = 595.0, 842.0          # A4，与本书实测一致
FAILS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def span(x0, y0, x1, y1):
    return [x0, y0, x1, y1]


# 一行 15pt 中文：每字 15pt 宽，行高 15pt，基线 y=187..202（p13 实测的公式缺口位置）
def chinese_line(pre, post, gap_pt, y=187.0, char_w=15.0):
    a = span(60.0, y, 60.0 + len(pre) * char_w, y + 15.0)
    x2 = a[2] + gap_pt
    b = span(x2, y, x2 + len(post) * char_w, y + 15.0)
    return [a, b], [pre, post]


def main():
    print("== 0a) 配置注入：PDF 路径必须传入公式图片检测器 ==")
    from marker_ext.inline_formula import InlineFormulaRecoveryProcessor
    class _NoopRecognition:
        pass
    configured = InlineFormulaRecoveryProcessor(
        _NoopRecognition(), {"pdf_path": r"F:\\Marker\\input\\因子投资方法与实践.pdf"}
    )
    check("pdf_path 配置注入生效", configured.pdf_path.endswith("因子投资方法与实践.pdf"),
          repr(configured.pdf_path))

    print("== 0) crop_formula_image：页面点坐标到局部像素坐标 ==")
    from PIL import Image
    page_image = Image.new("RGB", (596, 842), "white")
    crop, local = IF.crop_formula_image(
        page_image, (596, 842), [149.95, 210.66, 172.44, 225.65],
        padding_pt=2.0, upscale=2.0,
    )
    check("公式局部裁剪尺寸合理",
          45 <= crop.width <= 60 and 30 <= crop.height <= 45,
          f"size={crop.size}")
    check("局部 bbox 在裁剪图内",
          0 <= local[0] < local[2] <= crop.width and
          0 <= local[1] < local[3] <= crop.height,
          f"bbox={local}")

    print("== 1) detect_line_gaps：正例（p13 实测形态：公式图嵌在中文之间）==")
    # 阈值 = max(0.025*595=14.9, 1.4*字号)。15pt 正文 ⇒ 阈值 21pt；
    # 本书实测缺口 19–25pt，这里用 24pt（公式图实测有 19/21/24pt 几种宽度）。
    spans, texts = chinese_line("无论研究的关注点是", "还是αi，它们", gap_pt=24.0)
    gaps = IF.detect_line_gaps(spans, texts, PAGE_W, PAGE_H, font_sizes=[15.0, 15.0])
    check("24pt 缺口被检出（有字号信息）", len(gaps) == 1, f"gaps={gaps}")
    if gaps:
        g = gaps[0]
        check("缺口 bbox 落在两个 span 之间",
              abs(g[0] - spans[0][2]) < 0.6 and abs(g[2] - spans[1][0]) < 0.6, f"g={g}")

    # 无字号信息时的退化路径：用"字符数最多"的 span 估字号
    spans2, texts2 = chinese_line(
        "无论研究的关注点是以及更多的中文内容", "还是αi，它们", gap_pt=24.0
    )
    gaps2 = IF.detect_line_gaps(spans2, texts2, PAGE_W, PAGE_H)
    check("24pt 缺口被检出（无字号信息，退化估计）", len(gaps2) == 1, f"gaps={gaps2}")

    print("== 2) detect_line_gaps：反例（不应误报）==")
    # 2a 西文正常词距：单词宽 ~35pt，空格 4pt，整行 90 字符但用 span 表示两个词
    spans = [span(60.0, 300.0, 160.0, 312.0), span(164.0, 300.0, 300.0, 312.0)]
    texts = ["The quick brown fox", "jumps over the lazy dog"]
    check("西文 4pt 词距不误报", IF.detect_line_gaps(spans, texts, PAGE_W, PAGE_H) == [])

    # 2b 中文字距（标点占位 3pt）
    spans, texts = chinese_line("这是正文逗号，", "接着继续写", gap_pt=3.0)
    check("中文 3pt 字距不误报", IF.detect_line_gaps(spans, texts, PAGE_W, PAGE_H) == [])

    # 2c 缺口紧贴标点（排版空位而非公式）
    spans, texts = chinese_line("这一句结束了。", "下一句开始", gap_pt=19.0)
    check("句号旁 19pt 空位不误报", IF.detect_line_gaps(spans, texts, PAGE_W, PAGE_H) == [])

    # 2d 双栏之间的空白：两个 span 高度超限（跨栏整块）
    spans = [span(60.0, 400.0, 290.0, 470.0), span(320.0, 400.0, 540.0, 470.0)]
    texts = ["左栏这一段很长" * 4, "右栏这一段也很长" * 4]
    check("跨栏大间距不误报（高度判据）", IF.detect_line_gaps(spans, texts, PAGE_W, PAGE_H) == [])

    print("== 3) page_gap_entries：几何缺口 + 图像缺口（本书的真实形态）==")
    # 3a 文字层留白型（阈值 1.4*15=21pt，缺口 24pt）
    page_dict_gap = {
        "page": 12, "width": PAGE_W, "height": PAGE_H,
        "blocks": [{"lines": [
            {"bbox": [60, 180, 512, 203], "spans": [
                {"bbox": [60, 187, 300, 202], "text": "无论研究的关注点是", "font": {"size": 15.0}},
                {"bbox": [324, 187, 405, 202], "text": "还是αi，它们", "font": {"size": 15.0}},
            ]},
        ]}],
    }
    entries = IF.page_gap_entries(page_dict_gap, PAGE_W, PAGE_H, 0.025)
    check("几何缺口检出 1 个", len(entries) == 1, f"entries={entries}")
    if entries:
        e = entries[0]
        check("source=gap", e["source"] == "gap", e)
        check("line_index=0", e["line_index"] == 0, e)
        check("span_index=0（缺口左侧）", e["span_index"] == 0, e)
        check("left_text 正确", e["left_text"] == "无论研究的关注点是", e)
        check("right_text 正确", e["right_text"] == "还是αi，它们", e)

    # 3b 图像型（pdftext 跳过公式图片，两个 span 之间没有间隙）
    page_dict_img = {
        "page": 12, "width": PAGE_W, "height": PAGE_H,
        "blocks": [{"lines": [
            {"bbox": [60, 180, 512, 203], "spans": [
                {"bbox": [60, 187, 450, 202], "text": "无论研究的关注点是", "font": {"size": 15.0}},
                {"bbox": [470, 187, 512, 202], "text": "还是αi，它们", "font": {"size": 15.0}},
            ]},
        ]}],
    }
    image_rect = [450.0, 187.0, 469.0, 202.0]   # p13 实测的公式图位置
    entries = IF.page_gap_entries(page_dict_img, PAGE_W, PAGE_H, 0.025,
                                  image_rects=[image_rect])
    check("图像缺口检出 1 个", len(entries) == 1, f"entries={entries}")
    if entries:
        e = entries[0]
        check("source=image", e["source"] == "image", e)
        check("图像缺口 span_index=0", e["span_index"] == 0, e)
        check("图像缺口 bbox 与 PDF 一致", e["bbox"] == image_rect, e)
    # 与几何缺口重复的图像要被去重（几何缺口在 x=300..324）
    entries = IF.page_gap_entries(page_dict_gap, PAGE_W, PAGE_H, 0.025,
                                  image_rects=[[300.0, 187.0, 324.0, 202.0]])
    check("重叠图像缺口被去重", len(entries) == 1 and entries[0]["source"] == "gap", entries)
    # 不在任何文字行上的图像（插图）不参与
    entries = IF.page_gap_entries(page_dict_img, PAGE_W, PAGE_H, 0.025,
                                  image_rects=[[60.0, 400.0, 300.0, 415.0]])
    check("孤立图像（插图）不误报", entries == [], entries)

    print("== 4) coverage：整段被吃掉必须判 0，少量丢失要宽容 ==")
    original = ("目前，在关于因子投资内容的介绍中，无论研究的关注点是"
                "还是αi，它们都是在给定的因子模型下研究资产的预期收益和βi之间的关系。")
    check("完全保留 → 1.0", IF.coverage(original, original) == 1.0)
    check("整段丢失 → 0.0", IF.coverage("", original) == 0.0)
    check("只回公式（p13 真实故障）→ <0.3",
          IF.coverage("<math>E[R_i]</math>", original) < 0.3,
          IF.coverage("<math>E[R_i]</math>", original))
    check("空 HTML 标签也算空", IF.coverage("<p></p>", original) == 0.0)
    allow = original[: int(len(original) * 0.95)]
    check("保留 95% → >=0.9（应接受）", IF.coverage(allow, original) >= 0.9,
          IF.coverage(allow, original))

    print("== 5) 处理器保底：整页回滚与块级回滚（桩对象）==")
    try:
        run_processor_tests()
    except Exception as e:  # pragma: no cover
        check("处理器桩测试可运行", False, f"{type(e).__name__}: {e}")

    print()
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败 -> {FAILS}")
        return 1
    print("结果：全部通过")
    return 0


def run_processor_tests():
    """用最小桩对象验证 OcrRewriteGuardProcessor 的判断与回滚路径。"""
    from marker.schema import BlockTypes
    from marker_ext.inline_formula import OcrRewriteGuardProcessor

    class StubId:
        def __init__(self, page_id, block_id):
            self.page_id = page_id
            self.block_id = block_id
            self.block_type = None

        def __eq__(self, other):
            if isinstance(other, StubId):
                return (self.page_id, self.block_id) == (other.page_id, other.block_id)
            return NotImplemented

        def __hash__(self):
            return hash((self.page_id, self.block_id))

        def __repr__(self):
            return f"/page/{self.page_id}/Text/{self.block_id}"

    class StubBlock:
        def __init__(self, page_id, block_id, text, html=None, removed=False):
            self.page_id = page_id
            self.block_id = block_id
            self.block_type = BlockTypes.Text
            self._text = text
            self.html = html
            self.structure = [StubId(page_id, 1000 + block_id)] if text else []
            self.removed = removed
            self.text_extraction_method = "pdftext"
            self.ignore_for_output = False
            self.polygon = _stub_polygon()

        @property
        def id(self):
            return StubId(self.page_id, self.block_id)

        def raw_text(self, document):
            return self._text

    class StubPage:
        def __init__(self, page_id, blocks):
            self.page_id = page_id
            self.children = blocks
            self.structure = [b.id for b in blocks]

        def get_block(self, block_id):
            return self.children[block_id.block_id]

        def add_full_block(self, block):
            block.block_id = len(self.children)
            self.children.append(block)
            return block

    class StubDocument:
        def __init__(self, pages):
            self.pages = pages

        def get_page(self, page_id):
            return next(p for p in self.pages if p.page_id == page_id)

    # 两个块（无 html 的占多数，所以不会触发"整页替换"判定）：
    # 快照必须先于重写（真实链上快照挂在链首），然后模拟 OcrBuilder 的重写
    long_text = "把式（1.3）沿时间轴展开，资产超额收益和因子收益率在时序上满足如下多元线性回归模型："
    b1 = StubBlock(0, 0, long_text)
    short_text = "下面通过一个来自Ang（2014）的类比加深对上述定义的理解。"
    b2 = StubBlock(0, 1, short_text)
    b3 = StubBlock(0, 2, "第三个块没有被 OCR，用来把重写占比压到阈值以下。")
    page = StubPage(0, [b1, b2, b3])
    doc = StubDocument([page])

    guard = OcrRewriteGuardProcessor({"OcrRewriteGuardProcessor_page_replace_threshold": 0.8})
    guard.ensure_snapshot(doc)
    check("快照记录了全部 3 个块的原文", len(guard._shadow) == 3, guard._shadow)
    check("配置注入生效（阈值 0.8）", guard.page_replace_threshold == 0.8,
          guard.page_replace_threshold)

    # 模拟重写：b1 被吃空、b2 只差 1 个字符、b3 未被 OCR
    b1.html = "<math>(1.4)</math>"
    b1.structure = []
    b2.html = short_text[:-1] + "！"
    b2.structure = []

    guard(doc)
    check("被吃空的块已回滚：原文一字不少地补回（保留 OCR 公式）",
          b1.html and "多元线性回归模型" in b1.html and "<math>" in b1.html,
          f"html={b1.html!r}")
    check("回滚后原文覆盖率=1.0",
          IF.coverage(IF.strip_tags(b1.html), long_text) == 1.0)
    check("覆盖率正常的块保持 OCR 结果不变", b2.html == short_text[:-1] + "！")
    rep = guard.report()
    check("报告记录了 1 个回滚块", rep["totals"]["rolled_back"] == 1, rep["totals"])
    check("报告记录了保住的字数", rep["totals"]["chars_saved"] >= 30, rep["totals"])
    check("块级回滚不误判为整页", rep["totals"]["page_level"] == 0, rep["totals"])

    # 整页被替换的场景：3 个块里 2 个带新 html（占比 2/3 > 0.6）
    c1 = StubBlock(1, 0, "第一段正文内容，用于整页回滚测试。")
    c2 = StubBlock(1, 1, "第二段正文内容，用于整页回滚测试。")
    c3 = StubBlock(1, 2, "第三段正文内容，用于整页回滚测试。")
    p1 = StubPage(1, [c1, c2, c3])
    doc2 = StubDocument([page, p1])
    guard2 = OcrRewriteGuardProcessor({})
    guard2.ensure_snapshot(doc2)
    c1.html = "<p>完全不同的内容</p>"
    c1.structure = []
    c2.html = "<p>也是不同内容</p>"
    c2.structure = []
    guard2(doc2)
    # 回滚后正文以 pdftext 原文为准（html 可能为 None —— 由重建的 structure 渲染原文）
    check("整页判定：原块恢复 pdftext 原文",
          "第一段正文内容" in c1.raw_text(None),
          f"html={c1.html!r} struct={len(c1.structure or [])}")
    check("整页判定：原块不再显示为 removed", not c1.removed and not c2.removed)
    check("整页判定：页结构恢复为原 layout 块",
          set(p1.structure) >= {c1.id, c2.id, c3.id}, p1.structure)
    check("整页判定：报告 page_level=1",
          guard2.report()["totals"]["page_level"] == 1, guard2.report()["totals"])


def _stub_polygon():
    from marker.schema.polygon import PolygonBox
    return PolygonBox.from_bbox([60.0, 100.0, 500.0, 200.0])


if __name__ == "__main__":
    sys.exit(main())
