# -*- coding: utf-8 -*-
"""校验：覆盖率口径（6 字滑窗 vs 最长公共块）在真实 OCR 文本上的判别力。

场景：p13 的 pdftext 原文与 VLM 重写结果"很像但内容不同"（差 462 字），
旧口径给了高覆盖率从而放行；这里比较几种口径能否正确判为"不可接受"。
"""
import html
import re
import sys
import difflib

import pymupdf

PDF = r"F:\Marker\input\因子投资方法与实践.pdf"
MD = r"F:\Marker\out\ab-v0201\merged\book.md"


def norm(s):
    return re.sub(r"\s+", "", html.unescape(s or ""))


def cov_windows(cand, orig, n=6):
    o, c = norm(orig), norm(cand)
    if not o:
        return 1.0
    if not c:
        return 0.0
    step = max(1, n // 2)
    wins = [o[i:i + n] for i in range(0, len(o) - n + 1, step)]
    if not wins:
        return 1.0 if o in c else 0.0
    return sum(1 for w in wins if w in c) / len(wins)


def cov_blocks(cand, orig, min_block=12):
    """只统计长度 >= min_block 的公共块覆盖了多少原文。"""
    o, c = norm(orig), norm(cand)
    if not o:
        return 1.0
    if not c:
        return 0.0
    sm = difflib.SequenceMatcher(None, o, c, autojunk=False)
    hit = sum(b.size for b in sm.get_matching_blocks() if b.size >= min_block)
    return min(1.0, hit / len(o))


def main():
    md = open(MD, encoding="utf-8").read()
    marks = list(re.finditer(r"<!-- page: (\d+) -->", md))
    seg = {m.group(1): md[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(md))]
           for i, m in enumerate(marks)}
    doc = pymupdf.open(PDF)

    print("%-6s %8s %8s %8s %8s" % ("页", "旧w6", "块>=8", "块>=12", "块>=16"))
    for p in (9, 10, 12, 13, 14, 15, 16, 18, 31, 38):
        orig = doc[p].get_text("text")
        cand = seg.get(str(p), "")
        print("%-6d %8.3f %8.3f %8.3f %8.3f" % (
            p + 1, cov_windows(cand, orig), cov_blocks(cand, orig, 8),
            cov_blocks(cand, orig, 12), cov_blocks(cand, orig, 16)))

    # 用一个"完全相同"的极端例子做上界校验
    same = doc[9].get_text("text")
    print("\n自比（应接近 1）：w6=%.3f >=12=%.3f" % (cov_windows(same, same), cov_blocks(same, same)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
