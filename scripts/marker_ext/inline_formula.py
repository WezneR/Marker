# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\marker_ext\\inline_formula.py —— 行内公式缺口恢复 + OCR 重写保底。

背景（本书实测，见 docs\\loss-report.md）：
  1. 原 PDF 把**行内公式做成了图片 XObject**（全书 722 个、133 页）。pdftext/pymupdf
     对这些区域完全取不到字符，于是文字层里留下"缺口"：
         "目前，在关于因子投资内容的介绍中，无论研究的关注点是 [图] 还是αi，它们"
     marker 现有的行内公式判定（EquationProcessor._block_has_inline_math）只认
     **数学字体**或**数学 Unicode 字符**，纯缺口两条都不满足 → 不触发 OCR → 公式永久丢失。
  2. 一旦触发 OCR（行内公式触发、或 LineBuilder 的 block 级重试），marker 会把整块
     HTML 换成识别结果、并清空 structure。VLM 重写没有保底，会**把正文整段吃掉**：
     实测 p13 被重写后 cov(识别文本, pdftext)=0，整段中文消失。

本模块因此做三件事（都不修改 site-packages）：
  A. 缺口检测：两条互补的判据
       (i) 行内 span 几何间隙（文字层把公式留成空白的那种 PDF）；
       (ii) PDF 图像 XObject 的位置（文字层完全跳过公式图的那种 PDF，本书属于这种）。
       两者都只产出"缺口 bbox"，再按几何归属到文本块。
  B. InlineFormulaRecoveryProcessor：把每个缺口裁成局部图交给 Surya recognition 模型，
     再把公式插回原来的行/span 结构（不重写正文）。
  C. OcrRewriteGuardProcessor：在处理器链末端对任何 OCR 重写做覆盖率保底，不达标即
     回滚为 pdftext 原文。这是"一个字不丢"的最后一道闸门。

注册方式见 scripts\\marker_chunk.py：InlineFormulaRecoveryProcessor 插在默认处理器
列表的 EquationProcessor 之后（必须早于 TextProcessor / MarginaliaProcessor /
ReferenceProcessor，避免它们已经改了 structure）；OcrRewriteGuardProcessor 放在末尾，
且其快照函数挂在链首。
"""
from __future__ import annotations

import html as html_module
import os
import re
import statistics
from typing import Annotated, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from surya.layout.schema import LayoutBox, LayoutResult

from marker.logger import get_logger
from marker.processors import BaseProcessor
from marker.schema import BlockTypes
from marker.schema.document import Document
from marker.schema.labels import block_type_to_surya_label

logger = get_logger()

WHITESPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
# 段落/句读边界：缺口若紧贴这些字符，更像排版空位而非公式缺口
BOUNDARY_RE = re.compile(r"[\n。！？；：!?;，、）」』】]")

# EquationProcessor 与 InlineFormulaRecoveryProcessor 是同一处理器链里的两个实例，
# 需要共享"这个块被识别重写过、原文是什么"。用模块级注册表按 block id 存取，
# 不必改 marker 源码。
_REWRITE_REGISTRY: Dict[Tuple[int, Optional[int]], str] = {}


def _registry_key(block) -> Tuple[int, Optional[int]]:
    return (int(block.page_id or 0), block.block_id)


def register_original(block, original_text: str) -> None:
    _REWRITE_REGISTRY[_registry_key(block)] = original_text


def get_original(block) -> Optional[str]:
    return _REWRITE_REGISTRY.get(_registry_key(block))


def clear_original(block) -> None:
    _REWRITE_REGISTRY.pop(_registry_key(block), None)


def normalize(s: str) -> str:
    """去掉所有空白并反转义，用于覆盖率比对。"""
    if not s:
        return ""
    return WHITESPACE_RE.sub("", html_module.unescape(s))


def strip_tags(s: str) -> str:
    if not s:
        return ""
    return normalize(TAG_RE.sub("", s))


def coverage(candidate: str, original: str) -> float:
    """candidate 覆盖 original 的比例：original 的 6 字滑窗在 candidate 中命中的比例。

    比 difflib 便宜、对"整段被吃"极敏感（cov→0），对"少量重排"足够宽容（cov→1）。
    """
    orig = normalize(original)
    cand = normalize(candidate)
    if not orig:
        return 1.0
    if not cand:
        return 0.0
    n = 6
    if len(orig) <= n:
        return 1.0 if orig in cand else 0.0
    step = max(1, n // 2)
    windows = [orig[i:i + n] for i in range(0, len(orig) - n + 1, step)]
    if not windows:
        return 1.0 if orig in cand else 0.0
    return sum(1 for w in windows if w in cand) / len(windows)


def is_boundary_char(ch: str) -> bool:
    return bool(ch) and bool(BOUNDARY_RE.match(ch))


# --------------------------------------------------------------------------------------
# 纯几何：缺口检测（可单测，不依赖 marker 运行时）
# --------------------------------------------------------------------------------------
def detect_line_gaps(
    spans: Sequence[Sequence[float]],
    texts: Sequence[str],
    page_width: float,
    page_height: float,
    min_gap_frac: float = 0.025,
    max_span_height_frac: float = 0.03,
    rel_gap_factor: float = 1.4,
    font_sizes: Optional[Sequence[float]] = None,
) -> List[List[float]]:
    """在一条文字行内找"公式缺口"，返回缺口 bbox 列表（PDF 点坐标）。

    判据（全部满足才算缺口）：
      1. 相邻 span 的水平间距 >= max(min_gap_frac*页宽, rel_gap_factor*本行字号)。
         字号优先取 pdftext 的 font.size（最可靠）；拿不到时用"span 宽 / 字符数"估计。
         中文字距为 0，西文词距约 0.3em，所以 1.4em 的阈值对两者都安全，
         同时能覆盖本书实测的 19–25pt 公式缺口。
      2. 两侧都不以句读/空白结尾或开头（缺口紧贴标点更像排版空位）。
      3. 两侧 span 高度 <= max_span_height_frac*页高（排除跨栏、跨段大间距）。

    负例（不应触发）：西文单词间空格（约 0.3em）、中文标点字距、双栏之间的空白。
    """
    gaps: List[List[float]] = []
    if len(spans) < 2:
        return gaps

    unit = 0.0
    if font_sizes:
        sizes = [float(s) for s in font_sizes if s and float(s) > 0]
        if sizes:
            unit = statistics.median(sizes)
    if unit <= 0:
        # 退化估计：取"字符数最多"的 span 的 宽/字符数，避免被短 span（如 αi）带偏
        best = None
        for s, t in zip(spans, texts):
            n = len((t or "").strip())
            w = float(s[2]) - float(s[0])
            if n and w > 0:
                cw = w / n
                if best is None or n > best[0]:
                    best = (n, cw)
        unit = best[1] if best else 0.0

    min_gap = min_gap_frac * float(page_width)
    if unit > 0:
        min_gap = max(min_gap, rel_gap_factor * unit)
    max_h = max_span_height_frac * float(page_height)

    order = sorted(range(len(spans)), key=lambda i: spans[i][0])
    for a, b in zip(order, order[1:]):
        sa, sb = spans[a], spans[b]
        ta = (texts[a] or "").strip()
        tb = (texts[b] or "").strip()
        if not ta or not tb:
            continue
        if (float(sb[0]) - float(sa[2])) < min_gap:
            continue
        if (sa[3] - sa[1]) > max_h or (sb[3] - sb[1]) > max_h:
            continue
        if is_boundary_char(ta[-1]) or is_boundary_char(tb[0]):
            continue
        gaps.append([
            float(sa[2]),
            min(float(sa[1]), float(sb[1])),
            float(sb[0]),
            max(float(sa[3]), float(sb[3])),
        ])
    return gaps


def overlaps_any(bbox: Sequence[float], others: Sequence[Sequence[float]],
                 min_pct: float = 0.5) -> bool:
    """bbox 的面积有多少比例落在 others 里（用于去掉与几何缺口重复的图区域）。"""
    ax0, ay0, ax1, ay1 = [float(x) for x in bbox]
    area = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    if area <= 0:
        return False
    for o in others:
        bx0, by0, bx1, by1 = [float(x) for x in o]
        w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
        h = max(0.0, min(ay1, by1) - max(ay0, by0))
        if (w * h) / area >= min_pct:
            return True
    return False


def extract_inline_image_rects(
    pdf_path: str,
    page_index: int,
    max_height_pt: float = 90.0,
    max_width_frac: float = 0.75,
    min_height_pt: float = 6.0,
    top_left_origin: bool = True,
) -> List[List[float]]:
    """取一页里"像行内公式"的图像区域 bbox 列表（PDF 点坐标，左上原点）。

    判据：完全落在页面内、高度 <= max_height_pt、宽度 <= max_width_frac*页宽、
    且不是整页扫描图。行内公式图在本 PDF 里典型尺寸是 13–19pt 宽、15pt 高。

    用 pypdfium2（marker 自己的 PDF 引擎，坐标与 pdftext 的 bbox 同源），
    因此不需要额外的坐标变换；取不到时返回空列表，不影响其它流程。
    """
    rects: List[List[float]] = []
    try:
        import pypdfium2 as pdfium
        import pypdfium2.raw as pdfium_c
    except Exception:  # pragma: no cover
        return rects

    try:
        doc = pdfium.PdfDocument(pdf_path)
    except Exception:  # pragma: no cover
        return rects

    try:
        if page_index < 0 or page_index >= len(doc):
            return rects
        page = doc[page_index]
        page_w = float(page.get_width())
        page_h = float(page.get_height())
        # PdfObject.type 是 FPDF_PAGEOBJ_* 整数常量，不是 ``"image"`` 字符串。
        # get_pos() 也不是当前 pypdfium2 的 API；get_bounds() 返回 PDF 坐标系
        # (left, bottom, right, top)，下面再转换为 marker 使用的左上原点。
        for obj in page.get_objects():
            if getattr(obj, "type", None) != pdfium_c.FPDF_PAGEOBJ_IMAGE:
                continue
            try:
                left, bottom, right, top = obj.get_bounds()
            except Exception:
                continue
            width = float(right - left)
            height = float(top - bottom)
            if width <= 0 or height <= 0:
                continue
            if height < min_height_pt or height > max_height_pt:
                continue
            if width > max_width_frac * page_w:
                continue
            if top_left_origin:
                y0, y1 = page_h - float(top), page_h - float(bottom)
            else:
                y0, y1 = float(bottom), float(top)
            rects.append([float(left), y0, float(right), y1])
    except Exception:  # pragma: no cover - 任何 PDF 引擎异常都不应影响转换
        return rects
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return rects


def line_of_rect(rect: Sequence[float],
                 lines: Sequence[dict]) -> Optional[dict]:
    """找出矩形垂直居中所落的那条 pdftext 行（带可扩展容差）。"""
    cy = (float(rect[1]) + float(rect[3])) / 2.0
    for line in lines:
        lx0, ly0, lx1, ly1 = line["bbox"]
        if ly0 - 4 <= cy <= ly1 + 4 and not (lx1 < rect[0] or lx0 > rect[2]):
            return line
    return None


def crop_formula_image(
    image: Image.Image,
    page_size: Sequence[float],
    bbox: Sequence[float],
    padding_pt: float = 2.0,
    upscale: float = 2.0,
) -> Tuple[Image.Image, List[float]]:
    """Crop one formula gap from a rendered page and return its local bbox.

    ``bbox`` and ``page_size`` use the PDF/page coordinate system (top-left
    origin), while PIL uses pixels.  The recognition model gets a small image
    containing only the gap, so surrounding prose cannot be rewritten or used
    to replace the original text structure.
    """
    pw, ph = float(page_size[0]), float(page_size[1])
    iw, ih = image.size
    if pw <= 0 or ph <= 0 or iw <= 0 or ih <= 0:
        return image, [0.0, 0.0, float(iw), float(ih)]
    pad = max(0.0, float(padding_pt))
    x0 = max(0.0, float(bbox[0]) - pad)
    y0 = max(0.0, float(bbox[1]) - pad)
    x1 = min(pw, float(bbox[2]) + pad)
    y1 = min(ph, float(bbox[3]) + pad)
    px0 = max(0, min(iw - 1, round(x0 / pw * iw)))
    py0 = max(0, min(ih - 1, round(y0 / ph * ih)))
    px1 = max(px0 + 1, min(iw, round(x1 / pw * iw)))
    py1 = max(py0 + 1, min(ih, round(y1 / ph * ih)))
    crop = image.crop((px0, py0, px1, py1))
    scale = max(1.0, float(upscale))
    if scale > 1.0:
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return crop, [
        (float(bbox[0]) - x0) / (x1 - x0) * crop.width,
        (float(bbox[1]) - y0) / (y1 - y0) * crop.height,
        (float(bbox[2]) - x0) / (x1 - x0) * crop.width,
        (float(bbox[3]) - y0) / (y1 - y0) * crop.height,
    ]


def page_gap_entries(
    page_dict: dict,
    page_width: float,
    page_height: float,
    min_gap_frac: float,
    min_span_height_frac: float = 0.0,
    rel_gap_factor: float = 1.4,
    image_rects: Optional[Sequence[Sequence[float]]] = None,
    line_bboxes: Optional[Sequence[Sequence[float]]] = None,
) -> List[dict]:
    """把整页的"公式缺口"全部找出来（几何缺口 + 图像 XObject 缺口）。

    返回 [{"line_index", "span_index", "bbox", "source", "left_text", "right_text",
           "center": [x, y]}, ...]。
      - line_index 是页内"可用行"序号（与 ProviderOutput 里 Line 的顺序一致）；
        图像缺口若落在某行内，取该行序号，否则为 -1（仍可 OCR，只是插入点靠几何找）。
      - span_index 是行内"非空 span"序号，表示缺口应插在该 span 之后。
      - source: "gap"（文字层留白）或 "image"（文字层直接跳过公式图片，本书的主要形态）。
    """
    out: List[dict] = []
    lines_meta: List[dict] = []  # [{bbox, spans, texts}]
    for block in (page_dict or {}).get("blocks", []) or []:
        for ln in block.get("lines", []) or []:
            spans, texts = [], []
            for span in ln.get("spans", []) or []:
                txt = (span.get("text") or "").strip()
                bbox = span.get("bbox")
                if not txt or not bbox or len(bbox) != 4:
                    continue
                spans.append([float(x) for x in bbox])
                texts.append(txt)
            if not spans:
                continue
            lines_meta.append({
                "bbox": [float(x) for x in (ln.get("bbox") or [0, 0, 0, 0])],
                "spans": spans,
                "texts": texts,
                "sizes": [
                    float(((span.get("font") or {}).get("size") or 0))
                    for span in ln.get("spans", []) or []
                    if (span.get("text") or "").strip()
                ],
            })

    # (i) 行内几何缺口
    for line_no, line in enumerate(lines_meta):
        spans, texts = line["spans"], line["texts"]
        if len(spans) < 2:
            continue
        line_h = max(s[3] for s in spans) - min(s[1] for s in spans)
        if min_span_height_frac and line_h > min_span_height_frac * page_height:
            continue
        for g in detect_line_gaps(
            spans, texts, page_width, page_height,
            min_gap_frac, rel_gap_factor=rel_gap_factor,
            font_sizes=line.get("sizes"),
        ):
            left = max((i for i, s in enumerate(spans) if s[2] <= g[0] + 0.5), default=0)
            out.append({
                "line_index": line_no,
                "span_index": left,
                "bbox": g,
                "source": "gap",
                "left_text": texts[left],
                "right_text": texts[left + 1] if left + 1 < len(texts) else "",
                "center": [(g[0] + g[2]) / 2.0, (g[1] + g[3]) / 2.0],
            })

    # (ii) 图像 XObject 缺口（文字层完全没提到这些图，所以没有几何间隙可用）
    if image_rects:
        existing = [e["bbox"] for e in out]
        for rect in image_rects:
            if overlaps_any(rect, existing, min_pct=0.5):
                continue  # 与几何缺口重复
            cy = (float(rect[1]) + float(rect[3])) / 2.0
            line_no, span_idx, left_text, right_text = -1, 0, "", ""
            for ln_no, line in enumerate(lines_meta):
                lx0, ly0, lx1, ly1 = line["bbox"]
                if ly0 - 4 <= cy <= ly1 + 4 and not (lx1 < rect[0] or lx0 > rect[2]):
                    line_no = ln_no
                    idxs = [i for i, s in enumerate(line["spans"]) if s[2] <= float(rect[0]) + 1.0]
                    span_idx = idxs[-1] if idxs else 0
                    left_text = line["texts"][span_idx] if line["texts"] else ""
                    right_text = (
                        line["texts"][span_idx + 1]
                        if span_idx + 1 < len(line["texts"]) else ""
                    )
                    break
            if line_no < 0:
                continue  # 不在任何文字行上 → 更像插图，跳过
            out.append({
                "line_index": line_no,
                "span_index": span_idx,
                "bbox": [float(x) for x in rect],
                "source": "image",
                "left_text": left_text,
                "right_text": right_text,
                "center": [(float(rect[0]) + float(rect[2])) / 2.0, cy],
            })
    return out


# --------------------------------------------------------------------------------------
# 回填：把识别出的公式挂回原来的行/span 之后（不动原有 structure）
# --------------------------------------------------------------------------------------
def add_formula_span(document: Document, page, anchor, anchor_bbox: Sequence[float],
                     formula_html: str):
    """在 anchor span 之后插入一个承载 <math> 的 Span 块，返回该 Span。

    polygon 用缺口本身的 bbox（比复制 anchor 更贴近真实位置，便于调试与人工核对）。
    """
    from marker.schema.polygon import PolygonBox
    from marker.schema.registry import get_block_class

    SpanClass = get_block_class(BlockTypes.Span)
    span = SpanClass(
        polygon=PolygonBox.from_bbox(list(anchor_bbox), ensure_nonzero_area=True),
        text=normalize(formula_html)[:400] or "formula",
        font=getattr(anchor, "font", "") or "",
        font_weight=float(getattr(anchor, "font_weight", 0.0) or 0.0),
        font_size=float(getattr(anchor, "font_size", 0.0) or 0.0),
        minimum_position=0,
        maximum_position=0,
        formats=["plain"],
        page_id=anchor.page_id,
        text_extraction_method=anchor.text_extraction_method,
    )
    span.html = formula_html
    span.ignore_for_output = False
    page.add_full_block(span)
    return span


def insert_formula_after_span(document: Document, page, line, span_index: int,
                              formula_html: str, anchor_bbox: Sequence[float]) -> bool:
    """把公式插到 line 的第 span_index 个 span 之后。返回是否成功。"""
    spans = line.structure_blocks(document)
    if not spans:
        return False
    idx = max(0, min(span_index, len(spans) - 1))
    anchor = spans[idx]
    formula_span = add_formula_span(document, page, anchor, anchor_bbox, formula_html)
    # 插到 anchor 后面（structure 允许子块不连续）
    new_structure: List = []
    for item in line.structure or []:
        new_structure.append(item)
        if item == anchor.id:
            new_structure.append(formula_span.id)
    line.structure = new_structure
    return True


def locate_line_for_gap(document: Document, lines, gap: dict) -> Optional[int]:
    """按缺口的 y 中点定位块内行下标。"""
    gy = float(gap["center"][1])
    for li, line in enumerate(lines):
        if line.polygon.y_start - 3 <= gy <= line.polygon.y_end + 3:
            return li
    # 退化：取几何上最近的行
    if not lines:
        return None
    return min(range(len(lines)), key=lambda i: abs((lines[i].polygon.y_start + lines[i].polygon.y_end) / 2 - gy))


def locate_span_for_gap(document: Document, line, gap: dict) -> int:
    """在行内找缺口左侧那个 span（其 x_end <= 缺口左边界）。"""
    spans = line.structure_blocks(document)
    if not spans:
        return 0
    gx = float(gap["bbox"][0])
    candidates = [i for i, s in enumerate(spans) if s.polygon.x_end <= gx + 1.0]
    return candidates[-1] if candidates else 0


def text_without_gaps(document: Document, block, block_gaps: List[dict]) -> str:
    """把"缺口区域"的 span 从原文里剔掉，得到"除了公式以外必须保住"的文本。

    为什么要它：含缺口块的 VLM 重写常常"很像但不同"（数字/下标被改写），用整块
    覆盖率判会一律拒掉，导致公式也一起丢。实际上只要**缺口以外的正文**被保住就够了，
    所以用这个剔除版本做覆盖率判定，再配合"接受的是原文 span 结构 + 追加公式"的
    无损回填路径，既不丢字又拿到公式。
    """
    lines = block.structure_blocks(document)
    if not lines:
        return ""
    gaps_by_line: Dict[int, List[dict]] = {}
    for g in block_gaps or []:
        gaps_by_line.setdefault(int(g.get("line_index", -1)), []).append(g)
    if not gaps_by_line:
        return block.raw_text(document)

    kept_lines: List[str] = []
    for li, line in enumerate(lines):
        spans = line.structure_blocks(document)
        if not spans:
            continue
        glist = gaps_by_line.get(li, [])
        pieces = []
        for span in spans:
            x0, x1 = span.polygon.x_start, span.polygon.x_end
            inside = any(
                x0 >= g["bbox"][0] - 2 and x1 <= g["bbox"][2] + 2 for g in glist
            )
            if inside:
                continue
            pieces.append(getattr(span, "text", "") or "")
        if pieces:
            kept_lines.append("".join(pieces))
    text = "\n".join(kept_lines)
    return text if text.strip() else block.raw_text(document)


# --------------------------------------------------------------------------------------
# 处理器
# --------------------------------------------------------------------------------------
class InlineFormulaRecoveryProcessor(BaseProcessor):
    """检测行内公式缺口 → 局部图 OCR → 回填公式。"""

    block_types: Annotated[
        Tuple[BlockTypes, ...], "参与行内公式缺口检测的块类型。"
    ] = (
        BlockTypes.Text,
        BlockTypes.TextInlineMath,
        BlockTypes.ListItem,
        BlockTypes.Caption,
        BlockTypes.Footnote,
    )
    enabled: Annotated[bool, "总开关。关闭后只登记原文，不检测不回填。"] = True
    pdf_path: Annotated[
        str, "源 PDF 路径，用于读取行内公式图片 XObject 的页面坐标。"
    ] = ""
    inline_gap_min_frac: Annotated[
        float, "行内公式缺口的最小宽度（占页宽比例，默认 0.025 ≈ A4 上 15pt）。"
    ] = 0.025
    inline_gap_rel_factor: Annotated[
        float, "缺口宽度同时要 >= 该系数 × 本行字符中位宽（随字号自适应）。"
    ] = 1.4
    max_gaps_per_page: Annotated[
        int, "每页最多回填的缺口数（防异常页狂调 VLM）。"
    ] = 40
    inline_formula_token_budget: Annotated[
        int, "单块 OCR 的 token 预算。"
    ] = 1600
    recognition_batch_size: Annotated[
        int,
        "每次送推理服务的块数。实测：vLLM 一次收 32 个整页图请求会把引擎卡住（生成吞吐掉到 "
        "0.1 token/s 并从此不再响应）；4 个一批稳定（每批 5–20 秒）。",
    ] = 4
    min_coverage_inline: Annotated[
        float, "旧版整块 OCR 的兼容配置；局部公式识别不使用覆盖率判定。"
    ] = 0.7
    min_original_chars: Annotated[
        int, "原文短于该长度时不做覆盖率判定。"
    ] = 30
    log_samples: Annotated[int, "日志里打印的未恢复样例数。"] = 5
    inline_image_max_height_pt: Annotated[
        float, "把 PDF 图像 XObject 判为行内公式的最大高度（pt）。本书公式图约 15pt。"
    ] = 90.0
    inline_image_max_width_frac: Annotated[
        float, "把 PDF 图像 XObject 判为行内公式的最大宽度（占页宽比例）。"
    ] = 0.75
    inline_formula_crop_padding_pt: Annotated[
        float, "公式局部裁剪在四周增加的页面点数。"
    ] = 2.0
    inline_formula_crop_upscale: Annotated[
        float, "送入识别模型前公式局部图的放大倍数。"
    ] = 2.0

    def __init__(self, recognition_model, config=None):
        super().__init__(config)
        self.recognition_model = recognition_model
        self.stats: Dict[int, dict] = {}
        self._samples: List[dict] = []
        # PDF 路径：marker_chunk.py 通过 config["pdf_path"] 传入（marker 的 assign_config
        # 已在 super().__init__ 里把它设成实例属性）。取不到时只跑几何缺口检测。
        self.pdf_path: str = str(getattr(self, "pdf_path", "") or "")
        self._image_rect_cache: Dict[int, List[List[float]]] = {}

    # ---- 统计 ---------------------------------------------------------------
    def page_stat(self, page_id: int) -> dict:
        return self.stats.setdefault(
            int(page_id),
            {"detected": 0, "recovered": 0, "rolled_back": 0, "blocks_ocr": 0, "samples": []},
        )

    def report(self) -> dict:
        pages = {str(k): v for k, v in sorted(self.stats.items())}
        return {
            "totals": {
                "pages": len(pages),
                "detected": sum(v["detected"] for v in pages.values()),
                "recovered": sum(v["recovered"] for v in pages.values()),
                "rolled_back": sum(v["rolled_back"] for v in pages.values()),
                "blocks_ocr": sum(v["blocks_ocr"] for v in pages.values()),
            },
            "per_page": pages,
        }

    # ---- 缺口检测 -------------------------------------------------------------
    def _page_gaps(self, page, document: Document) -> Dict[int, List[dict]]:
        """按块归属整页缺口（独立方法，便于单测与复用）。"""
        page_dict = getattr(page, "pdftext_page", None)
        if not page_dict:
            return {}
        gaps = page_gap_entries(
            page_dict,
            float(page.polygon.width) or 1.0,
            float(page.polygon.height) or 1.0,
            self.inline_gap_min_frac,
            rel_gap_factor=self.inline_gap_rel_factor,
            image_rects=self._image_rects(page.page_id),
        )
        by_block: Dict[int, List[dict]] = {}
        total = 0
        for gap in gaps:
            owner = self._owner_block(page, document, gap)
            if owner is None:
                continue
            if total + 1 > self.max_gaps_per_page:
                break
            by_block.setdefault(id(owner), []).append(gap)
            total += 1
        return by_block

    def _image_rects(self, page_id: int) -> List[List[float]]:
        """该页"像行内公式"的图像区域（缓存；没有 PDF 路径时返回空）。"""
        key = int(page_id)
        if key in self._image_rect_cache:
            return self._image_rect_cache[key]
        rects: List[List[float]] = []
        if self.pdf_path and os.path.exists(self.pdf_path):
            rects = extract_inline_image_rects(
                self.pdf_path,
                key,
                max_height_pt=self.inline_image_max_height_pt,
                max_width_frac=self.inline_image_max_width_frac,
            )
        self._image_rect_cache[key] = rects
        return rects

    # ---- 主流程 -------------------------------------------------------------
    def __call__(self, document: Document, provider=None, *args, **kwargs):
        if not self.enabled:
            return
        targets: List[tuple] = []
        for page in document.pages:
            if not getattr(page, "pdftext_page", None):
                continue  # 整页 VLM OCR 的页没有文字层，无从比对
            by_block = self._page_gaps(page, document)
            for block in page.contained_blocks(document, self.block_types):
                if getattr(block, "removed", False) or getattr(block, "ignore_for_output", False):
                    continue
                original = "" if block.html else block.raw_text(document)
                if original and original.strip():
                    register_original(block, original)
                block_gaps = by_block.get(id(block), [])
                if not block_gaps:
                    continue
                self.page_stat(page.page_id)["detected"] += len(block_gaps)
                if block.html:
                    # EquationProcessor 已经重写过这块：交给 OCR 保底逻辑，不再重复 OCR
                    continue
                targets.append((page, block, block_gaps, original))

        if not targets:
            logger.info("[inline-formula] 没有需要回填的行内公式缺口")
            return

        # 逐缺口裁剪拼批。旧实现把整个中文文本块送去重写，模型一旦漏掉正文，
        # 覆盖率保底就会把公式一并回滚。局部裁剪让识别模型只负责公式本身。
        pending = []
        images, layout_results = [], []
        for page, block, gaps, original in targets:
            image = page.get_image(highres=True)
            if image is None:
                logger.warning("[inline-formula] %s 拿不到高分辨率页图，跳过该块", block.id)
                continue
            for gap in gaps:
                crop, local_bbox = crop_formula_image(
                    image,
                    page.polygon.size,
                    gap["bbox"],
                    padding_pt=self.inline_formula_crop_padding_pt,
                    upscale=self.inline_formula_crop_upscale,
                )
                pending.append((page, block, gap, original))
                images.append(crop)
                layout_results.append(LayoutResult(
                    bboxes=[LayoutBox(
                        polygon=[[local_bbox[0], local_bbox[1]],
                                  [local_bbox[2], local_bbox[1]],
                                  [local_bbox[2], local_bbox[3]],
                                  [local_bbox[0], local_bbox[3]]],
                        label="Text",
                        raw_label="Text",
                        position=0,
                        count=self.inline_formula_token_budget,
                    )],
                    image_bbox=[0, 0, *crop.size],
                ))

        if not images:
            return

        # 分批送推理服务：一批约 8 个块。单个超大请求（几十张整页图 + 上千 token 预算）
        # 一旦被引擎卡住，整块转换就会静默挂起；分批既能限制单请求峰值，也让
        # 日志能报出"卡在第几批"。
        total_batches = (len(pending) + self.recognition_batch_size - 1) // self.recognition_batch_size
        for bi in range(total_batches):
            lo = bi * self.recognition_batch_size
            hi = lo + self.recognition_batch_size
            batch_pending = pending[lo:hi]
            batch_results = self.recognition_model(
                images=images[lo:hi], layout_results=layout_results[lo:hi], full_page=False
            )
            logger.info(
                "[inline-formula] 批次 %d/%d 完成：%d 个块（页 %s）",
                bi + 1, total_batches, len(batch_pending),
                sorted({str(t[0].page_id) for t in batch_pending}),
            )
            self._apply_results(document, batch_pending, batch_results)

        if self._samples:
            logger.warning(
                "[inline-formula] 未恢复样例（共 %d，前 %d）: %s",
                len(self._samples),
                min(len(self._samples), self.log_samples),
                self._samples[: self.log_samples],
            )

    def _apply_results(self, document: Document, pending: List[tuple], results) -> None:
        for (page, block, gap, original), page_result in zip(pending, results):
            stat = self.page_stat(page.page_id)
            stat["blocks_ocr"] += 1
            block_result = page_result.blocks[0] if page_result.blocks else None
            if block_result is None or block_result.error or not (block_result.html or "").strip():
                stat["rolled_back"] += 1
                self._note(page, block, original, [gap], "识别为空或报错")
                continue

            html = block_result.html.strip()
            formulas = _extract_math_fragments(html)
            formula_html = formulas[0] if formulas else build_inline_math_html(html)
            if not formula_html:
                stat["rolled_back"] += 1
                self._note(page, block, original, [gap], "识别结果为空")
                continue
            lines = block.structure_blocks(document)
            li = locate_line_for_gap(document, lines, gap)
            if li is not None:
                si = locate_span_for_gap(document, lines[li], gap)
                ok = insert_formula_after_span(
                    document, page, lines[li], si, formula_html, gap["bbox"]
                )
            else:
                ok = False
            if ok:
                stat["recovered"] += 1
            else:
                stat["rolled_back"] += 1
                self._note(page, block, original, [gap], "无法定位插入点")

    # ---- 归属 ---------------------------------------------------------------
    @staticmethod
    def _owner_block(page, document: Document, gap: dict):
        """缺口属于哪个文本块（按缺口中心点落在块的 polygon 内）。"""
        gx, gy = gap["center"]
        best, best_area = None, None
        for block in page.contained_blocks(
            document,
            (
                BlockTypes.Text,
                BlockTypes.TextInlineMath,
                BlockTypes.ListItem,
                BlockTypes.Caption,
                BlockTypes.Footnote,
            ),
        ):
            if getattr(block, "removed", False):
                continue
            b = block.polygon
            if b.x_start - 2 <= gx <= b.x_end + 2 and b.y_start - 4 <= gy <= b.y_end + 4:
                if best is None or b.area < best_area:
                    best, best_area = block, b.area
        return best

    # ---- 记录 ---------------------------------------------------------------
    def _note(self, page, block, original: str, gaps: List[dict], reason: str) -> None:
        snippet = WHITESPACE_RE.sub(" ", original or "")[:80]
        self.page_stat(page.page_id)["samples"].append(
            {"block": str(block.id), "reason": reason, "text": snippet, "gaps": len(gaps)}
        )
        if len(self._samples) < 40:
            self._samples.append(
                {"page": page.page_id, "reason": reason, "text": snippet, "gaps": len(gaps)}
            )


def build_inline_math_html(html: str) -> str:
    """把识别结果包成行内公式 html（markdown 渲染器会输出 $…$）。"""
    html = (html or "").strip()
    if not html:
        return ""
    if html.startswith("<math"):
        return html
    return f'<math display="inline">{html}</math>'


# --------------------------------------------------------------------------------------
# EquationProcessor 的保底包装
# --------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------
# 核心保底：OcrBuilder 的整页/整块重写
# --------------------------------------------------------------------------------------
class OcrRewriteGuardProcessor(BaseProcessor):
    """把"整块/整页 OCR 重写"关进覆盖率笼子——本方案里最关键的保底。

    机制（按 marker 2.0.0 源码核对）：
      - LineBuilder 用 ocr-error 模型给每个文本块打 good/bad；balanced 模式下
        **任何一个块被判 bad 就把整页提升为 full-page OCR**（promote_fraction=0.0）。
        而"公式被做成图片"的段落恰好最容易被误判为 garbled（文字层有缺口，
        且 pdftext 的缺口会让行重叠统计异常）。
      - OcrBuilder 随后用 VLM 重写整页/整块：`apply_block_html` / `replace_page_structure`
        会清空旧块 structure 甚至把旧块标成 removed。VLM 少回一段，正文就永久消失。
        实测：本书 423 页里 236 页共丢 9,584 字（2.68%），p13 一次丢 437 字。
      - 元数据可以佐证：p13 是 `_meta.json` 里 text_extraction_method=surya 的页，而它的
        pdftext 文字层本身是完好的（pdftext 与 pymupdf 都取到 736 字符）。

    本处理器放在处理器链**末端**，但被注册为链上**第一个**执行（见 marker_chunk.py 的
    _install_shadow_snapshot），所以它在任何重写发生前就把每个块的 pdftext 原文记下来。
    之后按覆盖率决定是否回滚：

        块级：html 覆盖率 < min_coverage  → 回滚该块（OCR 文本 + pdftext 原文都保留）
        页级：被 OCR 重写（html-bearing）的块数 > page_replace_threshold * 原有块数
              → 整页回滚：恢复原 layout 块顺序与原文，OCR 新块按上游语义退休

    已知取舍（诚实记录）：
      * 回滚出的正文按**整块**重建行/span（一块 = 若干行），不再保留原 pdftext 的逐行
        bbox 归属。文字一个不少，但"段落切分"会比原生 pdftext 路径粗；
      * 页级回滚后，marker 的"文本块跨页续接"判定（round 2 的 has_continuation）不会
        重算，跨页断句可能少一个空格（对中文无影响）；
      * 整页 VLM OCR 的页（page.text_extraction_method == "surya" 且没有 pdftext 原文）
        没有可保的原文，只统计不干预——这是有意的：那种页本来就只能靠 OCR。

    真正的公式缺口交给 InlineFormulaRecoveryProcessor 定点回填，二者互补。
    """

    enabled: Annotated[bool, "总开关：False 时只记录不干预（A/B 对比用）。"] = True
    min_coverage: Annotated[
        float, "被 OCR 重写的块，识别文本至少要覆盖 pdftext 原文这么多，否则回滚。"
    ] = 0.9
    min_original_chars: Annotated[
        int,
        "原文短于该长度时不做覆盖率判定。实测：设为 30 会让「一两行的脚注」"
        "（p116/p151/p54 那类 [1]… [8]… 的引用）整体绕过保底而丢失，"
        "降到 12 可把这类短块也纳入保护；再低会误伤页码等极短元素。",
    ] = 12
    page_replace_threshold: Annotated[
        float, "被重写的块占比超过该值即判定为「整页被 OCR 替换」，整页回滚。"
    ] = 0.6
    keep_failed_ocr: Annotated[
        bool,
        "保守模式：True 时块回滚会额外保留 OCR 全文（可能重复，仅用于诊断对比）；"
        "False（推荐）时只保留 pdftext 原文 + OCR 摘出的公式。",
    ] = False
    max_samples: Annotated[int, "报告中保留的回滚样例数。"] = 80

    def __init__(self, config=None):
        super().__init__(config)
        self.stats: Dict[int, dict] = {}
        self.samples: List[dict] = []
        self._shadow: Dict[Tuple[int, Optional[int]], str] = {}
        self._layout_order: Dict[int, List] = {}
        self._snapshot_done = False

    # ---- 快照（链上第一件事） -------------------------------------------------
    def ensure_snapshot(self, document: Document) -> None:
        """记录每个顶层块的 pdftext 原文、原 layout 结构顺序、原 html 状态。

        只遍历 page.children 顶层（行/span 的文本由顶层块的 raw_text 汇总得到），
        避免对每个块做一次深度 contained_blocks 遍历（大文档上那是 O(n²)）。
        """
        if self._snapshot_done:
            return
        for page in document.pages:
            order = []
            for block in page.children or []:
                if getattr(block, "block_type", None) is None:
                    continue
                order.append(block.id)
                try:
                    object.__setattr__(block, "_had_html", bool(getattr(block, "html", None)))
                    object.__setattr__(block, "_pdftext_shadow", "")
                except Exception:  # pragma: no cover - pydantic 私有属性兜底
                    pass
            self._layout_order[int(page.page_id)] = order

            for block in page.children or []:
                if getattr(block, "block_type", None) is None:
                    continue
                if block.block_type in (BlockTypes.Line, BlockTypes.Span):
                    continue
                if getattr(block, "html", None):
                    # 上游已经 OCR 的块没有 pdftext 原文可保（_had_html 已置 True）
                    continue
                text = block.raw_text(document)
                try:
                    object.__setattr__(block, "_pdftext_shadow", text or "")
                except Exception:  # pragma: no cover
                    pass
                key = _registry_key(block)
                if text and text.strip():
                    self._shadow[key] = text
                    register_original(block, text)
        self._snapshot_done = True
        logger.info("[ocr-guard] 已记录 %d 个块的 pdftext 影子原文", len(self._shadow))

    # ---- 主流程 -------------------------------------------------------------
    def __call__(self, document: Document, *args, **kwargs):
        self.ensure_snapshot(document)
        if not self.enabled:
            return
        for page in document.pages:
            self._guard_page(document, page)

    def _guard_page(self, document: Document, page) -> None:
        order = self._layout_order.get(int(page.page_id), [])
        layout_blocks = [
            b for b in (page.get_block(bid) for bid in order if bid is not None)
            if b is not None
        ]
        html_blocks = [
            b for b in (page.children or [])
            if getattr(b, "html", None) and not getattr(b, "_had_html", False)
        ]
        total = max(len(layout_blocks), 1)
        page_replaced = len(html_blocks) / total >= self.page_replace_threshold

        if page_replaced:
            restored = 0
            saved = 0
            for block in layout_blocks:
                shadow = self._shadow.get(_registry_key(block), "")
                if not shadow.strip():
                    # 没有 pdftext 原文可保（纯 OCR 新块）：按上游语义退休
                    block.removed = True
                    continue
                self._rollback_block(document, block, shadow, cov=0.0, page_level=True)
                restored += 1
                saved += len(normalize(shadow))
            # 整页回滚：恢复原 layout 块顺序，并让渲染器看到块内重建的行
            page.structure = [b.id for b in layout_blocks if not b.removed]
            for block in layout_blocks:
                if getattr(block, "structure", None):
                    for child in (block.structure or []):
                        try:
                            child_block = page.get_block(child)
                        except Exception:
                            continue
                        if child_block.id not in page.structure:
                            page.structure.append(child_block.id)
            self._bump(page.page_id, restored, saved, 0.0, page_level=True)
            logger.warning(
                "[ocr-guard] p%s 判定为整页 OCR 重写（%d/%d 块带新 html）→ 整页回滚为 pdftext，"
                "恢复 %d 块 / %d 字",
                page.page_id, len(html_blocks), total, restored, saved,
            )
            return

        for block in html_blocks:
            shadow = self._shadow.get(_registry_key(block), "")
            orig_len = len(normalize(shadow))
            if orig_len < self.min_original_chars:
                continue
            cov = coverage(strip_tags(block.html), shadow)
            if cov >= self.min_coverage:
                clear_original(block)
                continue
            self._rollback_block(document, block, shadow, cov=cov, page_level=False)
            self._bump(page.page_id, 1, orig_len, cov, page_level=False)

    def _rollback_block(self, document: Document, block, shadow: str, cov: float,
                        page_level: bool) -> None:
        """回滚一个被 OCR 重写的块 —— **以 pdftext 原文为准，一个字都不丢**。

        早期实现把"OCR 全文 + 原文"两段都留着（keep_failed_ocr），实测在真实产物里
        表现为同一段内容前后重复两遍（OCR 改写版在前、原文版在后），阅读体验很差。
        现在改成：

          1. 主体用 pdftext 原文重建块结构（默认路径）；
          2. 若 OCR 结果里带有 <math> 公式，则把这些公式摘出来附在原文之后
             —— 公式是原文文字层根本给不出的信息，值得保留；
          3. OCR 的散文一律丢弃（它正是"覆盖率不足"的那部分）。

        keep_failed_ocr=True 时才退回旧的"两段都留"行为（仅用于诊断对比）。
        """
        ocr_html = getattr(block, "html", None) or ""
        if self.keep_failed_ocr and ocr_html and not _extract_math_fragments(ocr_html):
            # 没有公式可摘时，两段都留（保守模式，宁可重复也不丢）
            block.html = f"{ocr_html}\n<p>{_escape_html(shadow)}</p>"
            block.structure = []
            block.removed = False
            block.text_extraction_method = "surya"
        else:
            restore_pdftext_block(document, block, shadow)
            formulas = _extract_math_fragments(ocr_html)
            if formulas:
                # 公式是 pdftext 文字层给不出的信息，值得附在原文之后保留；
                # 但正文仍以原文为准（block.html 置空时由 structure 渲染原文）。
                block.html = (
                    f"<p>{_escape_html(shadow)}</p>" + "".join(formulas)
                )
        if len(self.samples) < self.max_samples:
            self.samples.append({
                "page": block.page_id,
                "block": str(block.id),
                "coverage": round(cov, 3),
                "chars": len(normalize(shadow)),
                "page_level": page_level,
                "text": WHITESPACE_RE.sub(" ", shadow)[:90],
            })
        if not page_level:
            logger.warning(
                "[ocr-guard] 回滚 %s：OCR 覆盖率 %.2f（原文 %d 字）：%r",
                block.id, cov, len(normalize(shadow)),
                WHITESPACE_RE.sub(" ", shadow)[:60],
            )

    def _bump(self, page_id: int, blocks: int, chars: int, cov: float,
              page_level: bool) -> None:
        stat = self.stats.setdefault(
            int(page_id),
            {"rolled_back": 0, "chars_saved": 0, "page_level": 0, "coverage": []},
        )
        stat["rolled_back"] += blocks
        stat["chars_saved"] += chars
        stat["coverage"].append(round(cov, 3))
        if page_level:
            stat["page_level"] += 1

    def report(self) -> dict:
        return {
            "totals": {
                "pages": len(self.stats),
                "rolled_back": sum(v["rolled_back"] for v in self.stats.values()),
                "chars_saved": sum(v["chars_saved"] for v in self.stats.values()),
                "page_level": sum(v["page_level"] for v in self.stats.values()),
            },
            "per_page": {
                str(k): {"rolled_back": v["rolled_back"],
                         "chars_saved": v["chars_saved"],
                         "page_level": v["page_level"],
                         "coverage": v["coverage"]}
                for k, v in sorted(self.stats.items())
            },
            "samples": self.samples,
        }


def _escape_html(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


MATH_TAG_RE = re.compile(r"<math\b[^>]*>.*?</math>", re.S)


def _extract_math_fragments(html: str) -> List[str]:
    """从 OCR 结果里摘出行内公式片段（<math …>…</math>），供回滚时保留。

    只摘 <math> 标签：公式是文字层给不出的信息；散文一律丢弃（它正是覆盖率不足的部分）。
    """
    if not html or "<math" not in html:
        return []
    out = []
    for m in MATH_TAG_RE.finditer(html):
        frag = m.group(0).strip()
        if frag and ("display=\"inline\"" in frag or "<math" in frag):
            # 统一成行内公式，避免把整篇块级公式塞进段落中间
            frag = re.sub(r'display="[^"]*"', 'display="inline"', frag)
            out.append(frag)
    return out[:20]  # 单块最多保留 20 个公式，防止异常输出刷屏


def make_equation_recover_rewrite(min_coverage: float = 0.9, min_chars: int = 30):
    """生产 EquationProcessor._recover_rewrite 的替代实现（由 equation_guard 打补丁）。

    上游 marker 若把"整块重写"统一收敛到 `_recover_rewrite`，本函数就地加覆盖率保底：
    覆盖率不足即返回 False → 调用方不写入 block.html，pdftext 原文与 structure 原样保留。
    """

    def _recover_rewrite(self, document: Document, block, html: str, gap_count: int) -> bool:
        original = get_original(block)
        if original is None:
            original = block.raw_text(document)
        if gap_count == 0 and original and len(normalize(original)) >= min_chars:
            cov = coverage(strip_tags(html), original)
            if cov < min_coverage:
                logger.warning(
                    "[equation-guard] 回滚 %s 的整块重写：覆盖率 %.2f（原文 %d 字）：%r",
                    block.id, cov, len(normalize(original)),
                    WHITESPACE_RE.sub(" ", original)[:70],
                )
                return False
        block.html = html
        block.structure = []
        block.text_extraction_method = "surya"
        clear_original(block)
        return True

    return _recover_rewrite


def restore_pdftext_block(document: Document, block, shadow: str) -> None:
    """把块恢复成"用 pdftext 原文重新建行/span 结构"的状态。

    行按原文换行切开，span 用整行文本（不需要字符级 bbox——我们对齐的是文字，
    不是版面；块 polygon 保持不变）。这样渲染出的正文与 pdftext 一字不差。
    """
    from marker.schema.polygon import PolygonBox
    from marker.schema.registry import get_block_class

    LineClass = get_block_class(BlockTypes.Line)
    SpanClass = get_block_class(BlockTypes.Span)

    block.html = None
    block.text_extraction_method = "pdftext"
    block.removed = False

    page = document.get_page(block.page_id)
    lines = [ln for ln in (shadow or "").split("\n") if ln.strip()]
    if not lines:
        lines = [shadow or ""]
    structure = []
    for ln in lines:
        poly = PolygonBox.from_bbox(block.polygon.bbox, ensure_nonzero_area=True)
        line = LineClass(polygon=poly, page_id=block.page_id)
        line.text_extraction_method = "pdftext"
        page.add_full_block(line)
        span = SpanClass(
            polygon=PolygonBox.from_bbox(block.polygon.bbox, ensure_nonzero_area=True),
            text=ln,
            font="",
            font_weight=0.0,
            font_size=0.0,
            minimum_position=0,
            maximum_position=0,
            formats=["plain"],
            page_id=block.page_id,
            text_extraction_method="pdftext",
        )
        page.add_full_block(span)
        line.structure = [span.id]
        structure.append(line.id)
    block.structure = structure
    block.ignore_for_output = False
    clear_original(block)


