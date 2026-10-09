#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\patch-missing-pages.py —— 按 PDF 文字层补回输出中丢失的文字。

用途（最后一道兜底）：
  * 空页（marker 输出里整页没内容）；
  * qa.py 报出的"覆盖率不足页"（`metrics.json.pages_below_coverage`）；
  * `--pages` 手工指定的任意页。

补回方式：把该页 PDF 文字层按行插到对应的 `<!-- page: N -->` 之后，并在前面加
`> **（修补说明）**` 标注，便于事后区分"原生转换"与"文字层补回"。

不要用它掩盖流程缺陷：先 `convert.ps1 -Force -OnlyChunks <块号>` 重跑；
只有重跑后仍丢字（例如公式图片导致的缺口）才用本脚本兜底。

用法：
  python patch-missing-pages.py --merged-md <book.md> --pdf <pdf> [--pages 53,115]
                                [--metrics <metrics.json>] [--dry-run] [--append]
"""
import argparse
import html
import json
import os
import re
import sys

NOTE_EMPTY = ("> **（修补说明）** 该页在 Marker 输出中为空——原文仅含脚注被版式模型判为页眉而整体剔除，"
              "或版式未识别出任何块。以下内容按 PDF 文字层直接补回，未经版式重排。")
NOTE_LOSS = ("> **（修补说明）** 该页在 Marker 输出中有缺字（可能来自整块 OCR 重写或公式图片缺口）。"
             "以下内容按 PDF 文字层原样补回，未经版式重排，可能与上文重复。")


def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_page_text(pdf: str, page_index: int) -> str:
    import pymupdf
    doc = pymupdf.open(pdf)
    raw = doc[page_index].get_text("text")
    # 合并硬换行：中文行尾不需要空格，英文单词间断行补空格
    lines = [ln.rstrip() for ln in raw.split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    out = []
    for ln in lines:
        if out and re.search(r"[A-Za-z0-9,;:\-]$", out[-1]) and re.match(r"^[A-Za-z0-9(]", ln):
            out[-1] = out[-1] + " " + ln
        else:
            out.append(ln)
    return "\n\n".join(out).strip()


def _longest_run(probe: str, target: str, min_len: int = 8) -> int:
    """probe 里最长的、能在 target 中找得到的子串长度。"""
    if not probe or not target:
        return 0
    for n in range(len(probe), min_len - 1, -1):
        for i in range(0, len(probe) - n + 1):
            if probe[i:i + n] in target:
                return n
    return 0


def locate_physical_page(doc, text: str, marker: int, search: int = 8):
    """把 marker 页号定位到 PDF 物理页号（返回 0 基下标或 None）。

    为什么不能拿 marker 当物理页下标：本书的页标签（marker 写的 data-page-id）与物理页
    索引**不是恒定偏移**——扉页/空页会让偏移在正文中变化（实测 marker 369 ↔ 物理页 370，
    marker 115 ↔ 物理页 116）。所以用邻页内容去比对：

      probe = 上一页尾部 80 字 + 下一页头部 80 字；在候选物理页里找最长命中块，
      两个方向任一命中 >= 12 字才认账，避免贴错页。
    """
    marks = list(re.finditer(r"<!--\s*page:\s*(\d+)\s*-->", text))
    seg = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        seg[int(m.group(1))] = text[m.end():end]

    def _sq(s):
        return re.sub(r"\s+", "", html.unescape(s or ""))

    before = _sq(re.sub(r"<!--.*?-->", "", seg.get(marker - 1, "")))[-80:]
    after = _sq(re.sub(r"<!--.*?-->", "", seg.get(marker + 1, "")))[:80]
    return _pick(doc, before, after, marker, search)


def _pick(doc, before: str, after: str, marker: int, search: int = 8):
    best, best_score = None, (0, 0)
    for p in range(max(0, marker - search), min(doc.page_count, marker + search + 1)):
        pt = re.sub(r"\s+", "", doc[p].get_text("text"))
        score = (_longest_run(before, pt), _longest_run(after, pt))
        if score > best_score:
            best, best_score = p, score
    if best is not None and max(best_score) >= 12:
        return best
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged-md", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="", help="0 基页号，逗号分隔")
    ap.add_argument("--metrics", default="", help="qa.py 的 metrics.json（默认取 ../qa/metrics.json）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--append", action="store_true",
                    help="追加到页末而不是插在页标记之后（默认插入，便于与原文对照）")
    args = ap.parse_args()

    pages = []
    if args.pages:
        pages = [int(x) for x in args.pages.split(",") if x.strip() != ""]
    else:
        mp = args.metrics
        if not mp:
            mp = os.path.abspath(os.path.join(os.path.dirname(args.merged_md), "..", "qa", "metrics.json"))
        with open(mp, "r", encoding="utf-8") as f:
            m = json.load(f)
        empty = [int(x) for x in (m.get("empty_page_ids") or [])]
        below = [int(x) for x in (m.get("pages_below_coverage") or [])]
        pages = sorted(set(empty) | set(below))

    if not pages:
        print("[patch] 没有需要补回的页。")
        return 0

    text = load_text(args.merged_md)
    import pymupdf
    doc = pymupdf.open(args.pdf)
    report = []
    for p in pages:
        marker = re.compile(r"(<!--\s*page:\s*%d\s*-->)" % p)
        m = marker.search(text)
        if not m:
            print(f"[patch] 页标记 {p} 找不到，跳过")
            continue
        # 关键：marker 页号 ≠ PDF 物理页下标，必须按邻页内容定位
        phys = locate_physical_page(doc, text, p)
        if phys is None:
            print(f"[patch] 页标记 {p}：无法用邻页内容定位到 PDF 物理页，跳过（避免贴错页）")
            continue
        body = extract_page_text(args.pdf, phys)
        if not body:
            print(f"[patch] 页标记 {p}（物理页 {phys+1}）文字层也是空的，跳过")
            continue
        # 已补过的页不重复补
        tail_probe = text[m.end(): m.end() + 400]
        if "（修补说明）" in tail_probe and body.split("\n")[0][:20] in tail_probe:
            print(f"[patch] 页标记 {p}（物理页 {phys+1}）看起来已补过，跳过")
            continue
        is_empty = not re.sub(r"<!--[^>]*-->|\s", "", tail_probe)[:80]
        note = NOTE_EMPTY if is_empty else NOTE_LOSS
        insert = f"\n\n{note}\n\n{body}\n"
        if args.dry_run:
            report.append((p, phys, body, True))
            continue
        if args.append:
            # 追加到下一个页标记之前
            nxt = re.compile(r"<!--\s*page:\s*\d+\s*-->").search(text, m.end())
            pos = nxt.start() if nxt else len(text)
            text = text[:pos] + insert + text[pos:]
        else:
            text = text[: m.end()] + insert + text[m.end():]
        report.append((p, phys, body, False))

    if not args.dry_run:
        with open(args.merged_md, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    for p, phys, body, dry in report:
        tag = "（dry-run）" if dry else "已补回"
        print(f"[patch] 页标记 {p}（物理页 {phys+1}）{tag}，{len(body)} 字符: {body[:60]!r}")
    print(f"[patch] 共处理 {len(report)} 页{'（未写文件）' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
