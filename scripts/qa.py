#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\qa.py —— 转换质量核对与抽样对照。

产出：
  out/qa/metrics.json     全部指标（机器可读）
  out/qa/report.md        人读报告（含可疑页、**文字层丢失页**、页边区域丢失）
  out/qa/samples/         均匀抽样的 PDF 页图 + index.md（页图与抽取文本对照）

本版新增（对应"页眉/页脚丢字 + 行内公式图片被丢弃"两个缺陷）：
  * 逐页文字层覆盖率：把 PDF 文字层与 book.md 去空白归一化后做序列比对，
    未匹配片段即真正的丢字（换行/全半角标点不会误报）；
  * 页边区域（页顶 8% / 页底 13%）单独统计，直接回答"页眉页脚还丢不丢"；
  * 汇总各 chunk 的 <stem>_ext_report.json：行内公式检出/恢复数与 OCR 保底回滚数。

用法：
  python qa.py --merged-md F:\\Marker\\out\\merged\\book.md --pdf <pdf> \\
               --chunks F:\\Marker\\out\\chunks --out-qa F:\\Marker\\out\\qa --samples 12
"""
import argparse
import difflib
import glob
import html
import json
import os
import re
import sys
import unicodedata

PAGE_MARK = re.compile(r"<!--\s*page:\s*([^>]+?)\s*-->")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
IMG_LINK = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.S)
# 真正的行内公式：markdown 渲染器把 <math display="inline"> 输出成 $…$，
# 且公式紧贴定界符（` convert_math ` 返回 " $" + text.strip() + "$ "）。
# 这里要求"$ 后紧跟非空白、"$ 前紧跟非空白"，才能把"$$…$$"内部误算排除掉
# （旧口径把 355 个 $$ 块算成了 1048 个"行内公式"，而 <math> 标签数其实是 0）。
INLINE_MATH = re.compile(r"(?<!\$)\$(?![\s$])(?:[^$\n]|(?<!\$)\$(?![\s$]))*?(?<![\s$])\$(?!\$)")
# Marker 的 markdown 输出用「管道表格」，不是 <table>：以分隔行（|---|---|）计一个表
MD_TABLE_SEP = re.compile(r"(?m)^\s*\|[\s:|-]+\|\s*$")

# 归一化：全角标点 → 半角，便于与 PDF 文字层对齐（marker 会把它们转成半角）
PUNCT_MAP = str.maketrans({
    "（": "(", "）": ")", "，": ",", "。": ".", "：": ":", "；": ";",
    "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",", "—": "-", "–": "-",
    "－": "-", "？": "?", "！": "!", "　": " ",
})
# 页边区域（与 marker MarginaliaProcessor 的默认区间一致）
HEADER_ZONE = 0.08
FOOTER_ZONE = 0.13


def normalize_for_diff(text: str) -> str:
    """去空白 + 全角转半角，用于"PDF 文字层 vs markdown"的序列比对。"""
    if not text:
        return ""
    text = html.unescape(text).translate(PUNCT_MAP)
    return re.sub(r"\s+", "", text)


def split_pages(text: str):
    """按 <!-- page: N --> 切页，返回 [(pageno, content), ...]"""
    out, last, last_no = [], 0, None
    for m in PAGE_MARK.finditer(text):
        if last_no is not None:
            out.append((last_no, text[last:m.start()]))
        else:
            out.append((None, text[last:m.start()]))
        last_no = m.group(1).strip()
        last = m.end()
    out.append((last_no, text[last:]))
    return out


def text_loss(pdf_text: str, md_text: str, min_seg: int = 6):
    """返回 (覆盖率, 丢失字符数, 丢失片段列表)。

    在归一化文本上做 SequenceMatcher：pdf 侧未匹配上的片段就是真正丢失的内容。
    """
    a = normalize_for_diff(pdf_text)
    b = normalize_for_diff(md_text)
    if not a:
        return 1.0, 0, []
    if not b:
        return 0.0, len(a), [a[:80]]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    lost, total = [], 0
    for tag, i1, i2, _j1, _j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            seg = a[i1:i2]
            total += len(seg)
            if len(seg) >= min_seg:
                lost.append(seg)
    return max(0.0, 1.0 - total / len(a)), total, lost


def analyze_page(content: str):
    body = PAGE_MARK.sub("", content)
    stripped = re.sub(r"\s+", "", body)
    total = len(stripped)
    cjk = len(CJK.findall(stripped))
    return {
        "chars": total,
        "cjk": cjk,
        "cjk_ratio": round(cjk / total, 4) if total else 0.0,
        "replacement_chars": body.count("\ufffd"),
        "display_math": len(DISPLAY_MATH.findall(body)),
        "inline_math_dollars": len(INLINE_MATH.findall(body)),
        "inline_math_html_tags": body.count("<math"),
        "tables": max(len(MD_TABLE_SEP.findall(body)), body.count("<table")),
        "images": len(IMG_LINK.findall(body)),
    }


def chunk_id_to_abs_page(chunks_dir: str):
    """返回 {页标记号: PDF 绝对页号}，用于把 `<!-- page: N -->` 还原成绝对页。

    实测本流程的页标记**已经是绝对页号**：convert.ps1 用 `--page_range a-b` 调
    marker_chunk.py，marker 把 data-page-id 写成绝对页号
    （chunk-00 = 0–39，chunk-01 = 40–79，…，chunk-10 = 400–422）。

    这里做"校验 + 兜底"：
      * 各块页标记区间连续无缝（实测情形）→ 恒等映射；
      * 各块都从 0 开始（块内相对页号）→ 按"块序号 × 块大小"还原；
      * 其它情形（块区间重叠）→ 返回空，调用方跳过比对并给出警告。
    """
    if not chunks_dir or not os.path.isdir(chunks_dir):
        return {}
    import glob as _glob
    from collections import Counter

    chunks = {}
    for cdir in sorted(_glob.glob(os.path.join(chunks_dir, "chunk-*"))):
        if not os.path.isdir(cdir):
            continue
        m = re.search(r"chunk-(\d+)", os.path.basename(cdir))
        if not m:
            continue
        idx = int(m.group(1))
        best = set()
        for md in _glob.glob(os.path.join(cdir, "**", "*.md"), recursive=True):
            if os.path.basename(md).startswith("_"):
                continue
            try:
                with open(md, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
            except Exception:
                continue
            marks = {int(x) for x in re.findall(r"\{(\d+)\}-{6,}", raw)}
            if len(marks) > len(best):
                best = marks
        if best:
            chunks[idx] = sorted(best)

    if not chunks:
        return {}

    all_marks = sorted({m for marks in chunks.values() for m in marks})
    starts_at_zero = all(min(marks) == 0 for marks in chunks.values())
    contiguous = (not starts_at_zero) and len(all_marks) == (all_marks[-1] - all_marks[0] + 1)

    if contiguous:
        return {m: m for m in all_marks}

    sizes = Counter(max(marks) - min(marks) + 1 for marks in chunks.values())
    size = sizes.most_common(1)[0][0]
    if size <= 0:
        return {}
    mapping = {}
    for idx, marks in chunks.items():
        base = idx * size
        for rel in marks:
            mapping[rel] = base + rel
    return mapping


def detect_page_offset(doc, md_at, lo, hi, max_offset: int = 6, sample: int = 60):
    """找出让"PDF 文字层 vs markdown"总覆盖率最大的页号偏移。

    返回 offset：用 md_at[k + offset] 去比对 PDF 物理页 k。
    offset=0 表示两边页号本来就一致。
    """
    def score(off):
        hit = miss = 0
        step = max(1, (hi - lo + 1) // sample)
        for pno in range(lo, hi + 1, step):
            a = normalize_for_diff(doc[pno].get_text("text"))
            if len(a) < 60:
                continue
            b = normalize_for_diff(md_at.get(pno + off, ""))
            if not b:
                miss += len(a)
                continue
            sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
            hit += sum(m.size for m in sm.get_matching_blocks() if m.size >= 6)
            miss += len(a)
        return (hit / miss) if miss else 0.0

    base = score(0)
    best, best_off = base, 0
    for off in range(1, max_offset + 1):
        for cand in (off, -off):
            s = score(cand)
            if s > best:
                best, best_off = s, cand
    # 提升不明显就认为本来就对齐，避免"用偏移掩盖真实丢字"
    if best_off != 0 and best > base * 1.05:
        return best_off
    return 0


def page_text_stats(pdf_path: str, pages, chunks_dir: str = "", loss_min_chars: int = 100,
                    page_range=None):
    """逐页比对 PDF 文字层与 markdown，返回 {绝对页号(str): {...}}。

    pages 是 merge 后的 [(页标记, 内容)]；用 chunk_id_to_abs_page 还原绝对页号。
    还原失败（没有 chunk 信息）时**不做比对**——宁可漏报，也不能错位比对
    （错位比对会把"页 A 的文字层 vs 页 B 的输出"当成丢字，全是假阳性）。

    page_range：(lo, hi) 闭区间，只比对这一段 PDF 页。分块试点（只跑了 chunk-00）时
    必须传，否则会把"没跑过的页"也算成丢字。
    """
    stats = {}
    try:
        import pymupdf
    except Exception as e:
        print(f"[qa] pymupdf 不可用，跳过文字层核对：{e}", file=sys.stderr)
        return stats

    rel_map = chunk_id_to_abs_page(chunks_dir)
    if not rel_map:
        print("[qa] 没有提供 --chunks（或里面没有页标记），跳过文字层零丢失核对", file=sys.stderr)
        return stats

    md_at = {}
    for pno, content in pages:
        if pno is None or not str(pno).strip().isdigit():
            continue
        rel = int(pno)
        abs_page = rel_map.get(rel)
        if abs_page is None:
            continue
        md_at[abs_page] = md_at.get(abs_page, "") + content

    doc = pymupdf.open(pdf_path)
    lo, hi = (page_range if page_range else (0, doc.page_count - 1))
    lo = max(0, int(lo))
    hi = min(doc.page_count - 1, int(hi))

    # 页号偏移校准：marker 写的 data-page-id 用的是 **PDF 自带的页标签**
    # （本项目这本书是"正文页码"，比物理页索引小），所以 `<!-- page: N -->`
    # 与 pymupdf 的物理下标可能整体差几页。逐页错位比对会把"页 N 的文字层 vs
    # 页 N+1 的输出"当成丢字。这里先在候选偏移里选"覆盖率之和最大"的那个。
    offset = detect_page_offset(doc, md_at, lo, hi)
    if offset:
        print(f"[qa] 页号偏移校准：marker 页标记 = 物理页索引 {offset:+d}（已按此对齐）")
        md_at = {k + offset: v for k, v in md_at.items()}

    for pno in range(lo, hi + 1):
        page = doc[pno]
        pdf_text = page.get_text("text")
        md = md_at.get(pno, "")
        cov, lost_chars, samples = text_loss(pdf_text, md)
        # 页边区域单独统计：把页顶/页底文本带拼起来，用同一套"字符匹配"口径算覆盖率
        ph = page.rect.height or 1.0
        zone_text = {"header": [], "footer": []}
        try:
            for blk in page.get_text("dict").get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for ln in blk.get("lines", []):
                    txt = "".join(s.get("text", "") for s in ln.get("spans", []))
                    if len(normalize_for_diff(txt)) < 4:
                        continue
                    y0, y1 = ln["bbox"][1] / ph, ln["bbox"][3] / ph
                    if y1 <= HEADER_ZONE:
                        zone_text["header"].append(txt)
                    elif y0 >= 1 - FOOTER_ZONE:
                        zone_text["footer"].append(txt)
        except Exception:
            pass
        zone = {}
        for k in ("header", "footer"):
            joined = "\n".join(zone_text[k])
            _cov, zlost, _segs = text_loss(joined, md)
            zone[k] = {"chars": len(normalize_for_diff(joined)), "lost": zlost}
        stats[str(pno)] = {
            "coverage": round(cov, 4),
            "lost_chars": lost_chars,
            "pdf_chars": len(normalize_for_diff(pdf_text)),
            "trivial": len(normalize_for_diff(pdf_text)) < loss_min_chars,
            "samples": [s[:90] for s in samples[:4]],
            "header_zone_chars": zone["header"]["chars"],
            "header_zone_lost": zone["header"]["lost"],
            "footer_zone_chars": zone["footer"]["chars"],
            "footer_zone_lost": zone["footer"]["lost"],
        }
    return stats


def collect_ext_reports(chunks_dir: str):
    """汇总各 chunk 的 marker_chunk.py 报告。"""
    agg = {
        "chunks": 0,
        "inline_detected": 0,
        "inline_recovered": 0,
        "inline_rolled_back": 0,
        "ocr_guard_rolled_back": 0,
        "ocr_guard_page_level": 0,
        "ocr_guard_chars_saved": 0,
        "keep_page_margins": [],
        "per_page": {},
    }
    for path in glob.glob(os.path.join(chunks_dir, "chunk-*", "**", "*_ext_report.json"),
                          recursive=True):
        try:
            rep = json.load(open(path, "r", encoding="utf-8"))
        except Exception:
            continue
        agg["chunks"] += 1
        inline = (rep.get("inline_formula") or {}).get("totals") or {}
        guard = (rep.get("ocr_guard_report") or {}).get("totals") or {}
        agg["inline_detected"] += inline.get("detected", 0)
        agg["inline_recovered"] += inline.get("recovered", 0)
        agg["inline_rolled_back"] += inline.get("rolled_back", 0)
        agg["ocr_guard_rolled_back"] += guard.get("rolled_back", 0)
        agg["ocr_guard_page_level"] += guard.get("page_level", 0)
        agg["ocr_guard_chars_saved"] += guard.get("chars_saved", 0)
        agg["keep_page_margins"].append(bool(rep.get("keep_page_margins")))
        for pid, st in ((rep.get("ocr_guard_report") or {}).get("per_page") or {}).items():
            agg["per_page"][pid] = st
    agg["keep_page_margins"] = (
        all(agg["keep_page_margins"]) if agg["keep_page_margins"] else None
    )
    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged-md", required=True)
    ap.add_argument("--pdf", default="")
    ap.add_argument("--chunks", default="")
    ap.add_argument("--out-qa", required=True)
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--loss-threshold", type=float, default=0.9,
                    help="页面覆盖率低于该值即列入「文字层丢失页」")
    ap.add_argument("--loss-min-chars", type=int, default=100,
                    help="PDF 文字层字符数少于该值的页不参与覆盖率判定（章节分隔页/空白页）")
    ap.add_argument("--page-range", default="",
                    help="只比对这一段 PDF 页（0 基，如 0-39）。分块试点时必须传，"
                         "否则没跑过的页会被算成丢字")
    args = ap.parse_args()

    os.makedirs(args.out_qa, exist_ok=True)
    samples_dir = os.path.join(args.out_qa, "samples")
    os.makedirs(samples_dir, exist_ok=True)

    if not os.path.exists(args.merged_md):
        print(f"[qa] 找不到 {args.merged_md}", file=sys.stderr)
        return 2
    with open(args.merged_md, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 合并前先去重页标记统计：markers 数量即页标记数
    markers = [m.group(1).strip() for m in PAGE_MARK.finditer(text)]
    pages = split_pages(text)
    rows = []
    for pno, content in pages:
        info = analyze_page(content)
        info["page_id"] = pno
        rows.append(info)

    # 图片与链接完整性
    img_dir = os.path.join(os.path.dirname(args.merged_md), "images")
    disks = {}
    if os.path.isdir(img_dir):
        for f in os.listdir(img_dir):
            p = os.path.join(img_dir, f)
            if os.path.isfile(p):
                disks[f] = os.path.getsize(p)
    links = [os.path.basename(u) for u in IMG_LINK.findall(text)]
    broken = sorted({l for l in links if l not in disks})
    zero_byte = sorted([f for f, s in disks.items() if s == 0])

    # 分块 meta：抽取方式直方图
    extraction_methods = {}
    if args.chunks and os.path.isdir(args.chunks):
        for root, _d, files in os.walk(args.chunks):
            for fn in files:
                if not fn.endswith("_meta.json"):
                    continue
                try:
                    with open(os.path.join(root, fn), "r", encoding="utf-8", errors="replace") as f:
                        meta = json.load(f)
                    for st in (meta.get("page_stats") or []):
                        m = st.get("text_extraction_method") or "unknown"
                        extraction_methods[m] = extraction_methods.get(m, 0) + 1
                except Exception:
                    pass

    total_pages_expected = None
    if args.pdf and os.path.exists(args.pdf):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from pdfutil import pages as pdf_pages
            total_pages_expected = pdf_pages(args.pdf)
        except Exception:
            pass

    page_range = None
    if args.page_range:
        try:
            a, b = args.page_range.split("-")
            page_range = (int(a), int(b))
        except Exception:
            print(f"[qa] --page-range 解析失败：{args.page_range!r}", file=sys.stderr)
            return 2

    # ---- 文字层零丢失核对 ------------------------------------------------
    page_text = (page_text_stats(args.pdf, pages, args.chunks, args.loss_min_chars, page_range)
                 if (args.pdf and os.path.exists(args.pdf)) else {})
    below = sorted(
        [dict(page=p, **v) for p, v in page_text.items()
         if v["coverage"] < args.loss_threshold and not v.get("trivial")],
        key=lambda r: (r["coverage"], -r["lost_chars"]),
    )
    trivial_pages = sorted(int(p) for p, v in page_text.items() if v.get("trivial"))
    total_lost = sum(v["lost_chars"] for v in page_text.values() if not v.get("trivial"))
    hdr_chars = sum(v["header_zone_chars"] for v in page_text.values())
    hdr_lost = sum(v["header_zone_lost"] for v in page_text.values())
    ftr_chars = sum(v["footer_zone_chars"] for v in page_text.values())
    ftr_lost = sum(v["footer_zone_lost"] for v in page_text.values())
    ext = collect_ext_reports(args.chunks) if args.chunks else {}

    n_pages = len([r for r in rows if r["page_id"] is not None and r["chars"] > 0])
    empty_pages = [r["page_id"] for r in rows if r["page_id"] is not None and r["chars"] == 0]
    # 真正可疑 = 有替换字符，或「正文不短、几乎全是西文、且没有任何公式」——后者才是可能漏 OCR 的特征。
    # 公式密集页的中文占比天然很低，不能算可疑。
    suspicious = sorted(
        [r for r in rows
         if r["replacement_chars"] > 0
         or (r["chars"] > 200 and r["cjk_ratio"] < 0.08
             and r["display_math"] == 0 and r["inline_math_dollars"] == 0)],
        key=lambda r: (-r["replacement_chars"], r["cjk_ratio"]))
    # 公式密集页只做提示，不当问题
    math_heavy = sorted([r for r in rows
                         if r["cjk_ratio"] < 0.30 and (r["display_math"] or r["inline_math_dollars"])],
                        key=lambda r: r["cjk_ratio"])
    n_math = sum(1 for r in rows if r["display_math"] or r["inline_math_dollars"])
    n_tables = sum(1 for r in rows if r["tables"])
    total_repl = sum(r["replacement_chars"] for r in rows)

    metrics = {
        "pages_marked": len(markers),
        "page_ids_unique": len(set(markers)),
        "pages_with_text": n_pages,
        "empty_page_ids": empty_pages,
        "pdf_page_count": total_pages_expected,
        "replacement_chars_total": total_repl,
        "pages_with_math": n_math,
        "pages_with_tables": n_tables,
        "pages_with_images": sum(1 for r in rows if r["images"]),
        "images_on_disk": len(disks),
        "images_total_bytes": sum(disks.values()),
        "image_links": len(links),
        "broken_image_links": broken,
        "zero_byte_images": zero_byte,
        "extraction_method_histogram": extraction_methods,
        "suspicious_pages": suspicious,
        "math_heavy_pages": [{k: r[k] for k in ("page_id", "chars", "cjk_ratio", "display_math", "inline_math_dollars")}
                             for r in math_heavy],
        # ---- 零丢失指标 ----
        "text_coverage_per_page": page_text,
        "pages_below_coverage": [r["page"] for r in below],
        "trivial_pages": trivial_pages,
        "coverage_threshold": args.loss_threshold,
        "coverage_min_chars": args.loss_min_chars,
        "total_lost_chars": total_lost,
        "max_dropped_chars_per_page": max(
            [v["lost_chars"] for v in page_text.values() if not v.get("trivial")] or [0]
        ),
        "header_zone_chars": hdr_chars,
        "header_zone_lost": hdr_lost,
        "footer_zone_chars": ftr_chars,
        "footer_zone_lost": ftr_lost,
        "extensions": ext,
        "per_page": rows,
    }
    with open(os.path.join(args.out_qa, "metrics.json"), "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    # 采样对照
    sample_rows = []
    if args.pdf and os.path.exists(args.pdf) and args.samples > 0:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from pdfutil import render
            rendered = render(args.pdf, samples_dir, args.samples, 150)
            by_page = {}
            for pno, content in pages:
                key = None
                try:
                    key = int(pno)
                except (TypeError, ValueError):
                    m = re.search(r"\d+", str(pno or ""))
                    key = int(m.group(0)) if m else None
                by_page.setdefault(key, []).append(content)
            for item in rendered:
                pno = item["page"]
                snippet = "\n".join(by_page.get(pno, []))[:1500]
                if not snippet:
                    snippet = "（该页在合并输出中未找到对应内容，页号映射可能不一致）"
                sample_rows.append({"page": pno, "file": item["file"], "snippet": snippet})
        except Exception as e:
            print(f"[qa] 抽样渲染失败：{e}", file=sys.stderr)

    # 报告
    L = []
    L.append("# 转换质量核对报告\n")
    L.append(f"- 合并文件：`{args.merged_md}`")
    if total_pages_expected:
        L.append(f"- PDF 页数：**{total_pages_expected}**；输出页标记数：**{len(markers)}**"
                 f"（唯一 {len(set(markers))}）")
    L.append(f"- 有正文的页：{n_pages}；空页：{len(empty_pages)}"
             + (f" → `{empty_pages[:30]}`" if empty_pages else ""))
    L.append(f"- U+FFFD 总数：**{total_repl}**")
    L.append(f"- 含公式的页：{n_math}；含表格的页：{n_tables}；含图片的页：{metrics['pages_with_images']}")
    L.append(f"- 图片：磁盘 {len(disks)} 张 / 共 {metrics['images_total_bytes'] / 1e6:.1f} MB；"
             f"链接 {len(links)} 个；坏链 {len(broken)} 个；0 字节 {len(zero_byte)} 个")
    if extraction_methods:
        L.append("- 正文抽取方式（来自各块 _meta.json）："
                 + "、".join(f"`{k}` {v} 页" for k, v in sorted(extraction_methods.items())))
    L.append("")

    # ---- 零丢失板块 ----
    L.append("## 文字层零丢失核对\n")
    if page_text:
        L.append(f"- 比对方式：PDF 文字层与 book.md 去空白/全半角归一化后逐页序列比对")
        L.append(f"- PDF 文字层总字符（归一化）：**{sum(v['pdf_chars'] for v in page_text.values()):,}**；"
                 f"未出现在输出中的字符：**{total_lost:,}**")
        L.append(f"- 覆盖率 < {args.loss_threshold} 的页：**{len(below)}** 页"
                 f"（最大单页丢字 {metrics['max_dropped_chars_per_page']}；"
                 f"另有 {len(trivial_pages)} 页文字层不足 {args.loss_min_chars} 字符，不参与判定）")
        L.append(f"- 页边区域（页顶 {HEADER_ZONE:.0%} / 页底 {FOOTER_ZONE:.0%}）："
                 f"共 {hdr_chars + ftr_chars:,} 字符，丢失 **{hdr_lost + ftr_lost:,}**"
                 f"（页眉 {hdr_lost}/{hdr_chars}，页脚 {ftr_lost}/{ftr_chars}）")
        if ext.get("keep_page_margins") is not None:
            L.append(f"- 处理时是否保留页眉/页脚（keep_page_margins）：**{ext['keep_page_margins']}**")
        if ext:
            L.append(f"- 行内公式：检出 **{ext.get('inline_detected', 0)}** 处 / "
                     f"恢复 **{ext.get('inline_recovered', 0)}** 处"
                     f"（未恢复 {ext.get('inline_rolled_back', 0)}）")
            L.append(f"- OCR 重写保底：回滚 **{ext.get('ocr_guard_rolled_back', 0)}** 块 / "
                     f"整页回滚 **{ext.get('ocr_guard_page_level', 0)}** 页 / "
                     f"保住 **{ext.get('ocr_guard_chars_saved', 0):,}** 字")
        if below:
            L.append("\n### 覆盖率不足的页（按覆盖率升序，取前 40）\n")
            L.append("| 页(0基) | 覆盖率 | 丢字 | PDF字符 | 页眉丢/总 | 页脚丢/总 | 缺失片段样例 |")
            L.append("|---|---|---|---|---|---|---|")
            for r in below[:40]:
                sample = " / ".join(r["samples"][:2])[:70]
                L.append(f"| {r['page']} | {r['coverage']:.3f} | {r['lost_chars']} | {r['pdf_chars']} | "
                         f"{r['header_zone_lost']}/{r['header_zone_chars']} | "
                         f"{r['footer_zone_lost']}/{r['footer_zone_chars']} | {sample} |")
            if len(below) > 40:
                L.append(f"\n（完整清单见 `metrics.json` 的 `pages_below_coverage`，共 {len(below)} 页）")
            L.append("\n> 处理建议：对丢字页所在块 `convert.ps1 -Force -OnlyChunks <块号>` 重跑；"
                     "若重跑后仍丢字，用 `patch-missing-pages.py --pages ...` 按文字层补回。")
        else:
            L.append("- **未发现整段/整行丢字：所有页覆盖率均 >= "
                     f"{args.loss_threshold}，页边区域丢失为 0。**")
    else:
        L.append("- 未提供 --pdf 或 pymupdf 不可用，跳过文字层比对。")
    L.append("")

    L.append("## 可疑页（可能漏 OCR / 乱码）\n")
    if suspicious:
        L.append("| 页 | 字符 | 中文占比 | U+FFFD | 公式块 | 表格 | 图片 |")
        L.append("|---|---|---|---|---|---|---|")
        for r in suspicious:
            L.append(f"| {r['page_id']} | {r['chars']} | {r['cjk_ratio']:.2f} | "
                     f"{r['replacement_chars']} | {r['display_math']} | {r['tables']} | {r['images']} |")
        L.append("\n建议：对这些页所在区间用 `convert.ps1 -ForceOcr -OnlyChunks <块号>` 重跑。")
    else:
        L.append("未发现：全书无替换字符（U+FFFD=0），也没有「正文不短却几乎没有中文且无公式」的页。")
    if math_heavy:
        L.append(f"\n## 公式密集页（{len(math_heavy)} 页，中文占比天然偏低，**非问题**）\n")
        L.append("| 页 | 字符 | 中文占比 | 独立公式 | 行内公式 |")
        L.append("|---|---|---|---|---|")
        for r in math_heavy[:15]:
            L.append(f"| {r['page_id']} | {r['chars']} | {r['cjk_ratio']:.2f} | "
                     f"{r['display_math']} | {r['inline_math_dollars']} |")
        if len(math_heavy) > 15:
            L.append(f"\n（仅列前 15 页，完整清单见 `metrics.json` 的 `math_heavy_pages`）")
    if empty_pages:
        L.append(f"\n## 空页（{len(empty_pages)} 页）\n")
        L.append(f"- 0 基页号：`{empty_pages}`（即 PDF 第 {[int(p) + 1 for p in empty_pages if str(p).isdigit()]} 页）")
        L.append("- 已用 `patch-missing-pages.py` 按文字层补回，文中带「（修补说明）」标注；"
                 "原因是整页被判为页眉而被剔除，或版式未识别出任何块。")
    if broken or zero_byte:
        L.append("\n## 图片问题\n")
        if broken:
            L.append(f"- 坏链（Markdown 引用但磁盘不存在）：`{broken[:20]}`")
        if zero_byte:
            L.append(f"- 0 字节图片：`{zero_byte[:20]}`")
    L.append("\n## 抽样对照\n")
    if sample_rows:
        L.append("| PDF 页（1 基） | 页图 |")
        L.append("|---|---|")
        for s in sample_rows:
            L.append(f"| {s['page'] + 1} | [打开](samples/{s['file']}) |")
        L.append("\n逐页对照见 `samples/index.md`。")
    else:
        L.append("（未生成抽样）")

    with open(os.path.join(args.out_qa, "report.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")

    if sample_rows:
        S = ["# 抽样对照（PDF 页图 vs 抽取文本）\n"]
        for s in sample_rows:
            S.append(f"## PDF 第 {s['page'] + 1} 页\n")
            S.append(f"![page {s['page'] + 1}]({s['file']})\n")
            S.append("<details><summary>抽取的 Markdown 片段</summary>\n")
            S.append("```markdown")
            S.append(s["snippet"])
            S.append("```\n</details>\n")
        with open(os.path.join(samples_dir, "index.md"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(S) + "\n")

    print(f"[qa] 页标记 {len(markers)}；有正文 {n_pages} 页；空页 {len(empty_pages)}；U+FFFD {total_repl}")
    print(f"[qa] 公式页 {n_math}；表格页 {n_tables}；图片 {len(disks)} 张；坏链 {len(broken)}")
    print(f"[qa] 丢字 {total_lost:,} 字符；覆盖率不足 {len(below)} 页；"
          f"页边丢失 {hdr_lost + ftr_lost:,} 字符")
    if ext:
        print(f"[qa] 行内公式 检出 {ext.get('inline_detected', 0)} / 恢复 {ext.get('inline_recovered', 0)}；"
              f"OCR保底 回滚 {ext.get('ocr_guard_rolled_back', 0)} 块、保住 {ext.get('ocr_guard_chars_saved', 0):,} 字")
    print(f"[qa] 报告 -> {os.path.join(args.out_qa, 'report.md')}")
    print(f"[qa] 指标 -> {os.path.join(args.out_qa, 'metrics.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

