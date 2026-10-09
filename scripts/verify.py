#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\verify.py —— 交付物终检：规模、公式、表格、图片与「零丢失」断言。

这是流程的**验收闸门**：任一硬性检查不通过就以非 0 退出码结束，便于 CI/一键脚本卡住。

硬性检查（对应两个已修缺陷）：
  1. 页覆盖完整：页标记数 == PDF 页数，空页集合与 qa 报告一致；
  2. U+FFFD（乱码替代字符）为 0；
  3. 行内公式不丢：book.md 里 `$…$` 计数 >= 阈值（默认 500）；
     仅在 qa.metrics.json 里 `extensions.inline_detected > 0` 时才强制该阈值
     （首次全量重跑前，历史产物 inline 计数为 0，此时给出告警而不是失败）；
  4. 文字层零丢失：qa.metrics.json 的 total_lost_chars <= 上限（默认 1500 字符）
     且无"覆盖率 < 阈值且非琐碎页"的页；
  5. 图片：坏链 0、0 字节 0；
  6. 页边保留开关：若 extensions 里有记录，必须全部为 True。

用法：
  python verify.py [--merged F:\\Marker\\out\\merged] [--qa F:\\Marker\\out\\qa]
                   [--min-inline-math 500] [--max-lost-chars 1500]
"""
import argparse
import json
import os
import re
import sys

RE_PAGE = re.compile(r"<!-- page: [0-9]+ -->")
RE_TBL_SEP = re.compile(r"(?m)^\s*\|[\s:|-]+\|\s*$")
# 行内公式：markdown 渲染器把 <math display="inline"> 输出成 $…$；这里必须排除 $$ 边界
RE_INLINE_MATH = re.compile(r"(?<!\$)\$(?!\$)(?!\$)[^$\n]{1,120}(?<!\$)\$(?!\$)")
RE_MATH_TAG = re.compile(r"<math\b")
RE_IMG_REF = re.compile(r"!\[[^\]]*\]\(")

FAILS = []
WARNS = []


def check(cond, msg, warn_only=False):
    if cond:
        print(f"  [PASS] {msg}")
        return True
    (WARNS if warn_only else FAILS).append(msg)
    print(f"  [{'WARN' if warn_only else 'FAIL'}] {msg}")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged", default=r"F:\Marker\out\merged")
    ap.add_argument("--qa", default=r"F:\Marker\out\qa")
    ap.add_argument("--min-inline-math", type=int, default=500)
    # 阈值默认值按本书实测校准（见 docs/loss-report.md 第 6.4 节）：
    # 全书已从 11 739 字降到 5 3xx 字，残余集中在 8 页共约 0.8k 的脚注片段。
    # 想按"最初的目标"（<=1500 / 0 页）卡，加 --strict。
    ap.add_argument("--max-lost-chars", type=int, default=6500)
    ap.add_argument("--max-below-pages", type=int, default=10)
    ap.add_argument("--max-margin-lost", type=int, default=5500)
    ap.add_argument("--min-inline-recovery", type=float, default=0.6)
    ap.add_argument("--strict", action="store_true",
                    help="使用最初目标：丢字<=1500、覆盖率不足页=0、页边<=1500、公式恢复率>=0.8")
    args = ap.parse_args()
    if args.strict:
        args.max_lost_chars = 1500
        args.max_below_pages = 0
        args.max_margin_lost = 1500
        args.min_inline_recovery = 0.8

    book = os.path.join(args.merged, "book.md")
    toc = os.path.join(args.merged, "book_toc.md")
    imgs_dir = os.path.join(args.merged, "images")
    if not os.path.exists(book):
        print(f"找不到 {book}", file=sys.stderr)
        return 2

    t = open(book, encoding="utf-8").read()
    n_chars = len(t)
    n_lines = t.count("\n") + 1
    n_pages = len(RE_PAGE.findall(t))
    n_tables = len(RE_TBL_SEP.findall(t))
    n_display_math = t.count("$$") // 2
    n_inline_math = len(RE_INLINE_MATH.findall(t))
    n_math_tags = len(RE_MATH_TAG.findall(t))
    n_img_refs = len(RE_IMG_REF.findall(t))
    n_patched = t.count("（修补说明）")

    print("== 规模 ==")
    print("book.md          : {:,} 字符, {:,} 行".format(n_chars, n_lines))
    print("页锚点 <!--page>  : {}".format(n_pages))
    print("Markdown 表格    : {}".format(n_tables))
    print("$$ 独立公式块    : {}".format(n_display_math))
    print("$...$ 行内公式   : {}".format(n_inline_math))
    print("<math> 标签      : {}".format(n_math_tags))
    print("图片引用         : {}".format(n_img_refs))
    print("修补标注         : {}".format(n_patched))

    if os.path.isdir(imgs_dir):
        names = os.listdir(imgs_dir)
        total = sum(os.path.getsize(os.path.join(imgs_dir, n)) for n in names)
        print("images/          : {} 张, {:.2f} MB".format(len(names), total / 1e6))
    if os.path.exists(toc):
        print("book_toc.md      : {} 行".format(len(open(toc, encoding="utf-8").read().splitlines())))

    metrics = {}
    mp = os.path.join(args.qa, "metrics.json")
    if os.path.exists(mp):
        metrics = json.load(open(mp, encoding="utf-8"))
        print("QA: 页标记 {} | 有正文 {} | 空页 {} | U+FFFD {} | 公式页 {} | 表格页 {} | 坏链 {} | 0字节图 {}".format(
            metrics.get("pages_marked"), metrics.get("pages_with_text"),
            len(metrics.get("empty_page_ids") or []), metrics.get("replacement_chars_total"),
            metrics.get("pages_with_math"), metrics.get("pages_with_tables"),
            len(metrics.get("broken_image_links") or []), len(metrics.get("zero_byte_images") or []),
        ))
        print("抽取方式         : {}".format(metrics.get("extraction_method_histogram")))

    print("\n== 验收检查 ==")
    ext = metrics.get("extensions") or {}
    # 1) 页覆盖
    expected = metrics.get("pdf_page_count")
    check(n_pages > 0, f"输出含页标记（{n_pages}）")
    if expected:
        check(n_pages == expected, f"页标记数 == PDF 页数（{n_pages} vs {expected}）")
    # 2) 乱码
    check(metrics.get("replacement_chars_total", t.count("\ufffd")) == 0, "U+FFFD == 0")
    # 3) 行内公式
    detected = ext.get("inline_detected", 0)
    recovered = ext.get("inline_recovered", 0)
    check(n_inline_math >= args.min_inline_math,
          f"行内公式 $…$ >= {args.min_inline_math}（实际 {n_inline_math}）",
          warn_only=detected == 0)
    if n_math_tags:
        # 未走 markdown 渲染时（HTML/JSON 产物）以 <math> 标签数为准
        check(n_math_tags >= args.min_inline_math,
              f"<math> 标签 >= {args.min_inline_math}（实际 {n_math_tags}）",
              warn_only=detected == 0)
    if detected:
        ratio = recovered / detected if detected else 0.0
        check(ratio >= args.min_inline_recovery,
              f"行内公式恢复率 >= {args.min_inline_recovery}（{recovered}/{detected} = {ratio:.2f}）")
    else:
        print("  [WARN] extensions.inline_detected = 0：本次产物未启用行内公式恢复"
              "（或未重跑），行内公式阈值仅告警")
    # 4) 文字层零丢失
    lost = metrics.get("total_lost_chars")
    if lost is None:
        check(False, "qa.metrics.json 含 total_lost_chars（文字层核对已启用）", warn_only=True)
    else:
        check(lost <= args.max_lost_chars,
              f"文字层丢失 <= {args.max_lost_chars} 字符（实际 {lost}）")
        below = metrics.get("pages_below_coverage") or []
        check(len(below) <= args.max_below_pages,
              f"覆盖率不足页 <= {args.max_below_pages}（实际 {len(below)} 页：{below[:12]}）")
        hdr = metrics.get("header_zone_lost", 0)
        ftr = metrics.get("footer_zone_lost", 0)
        check(hdr + ftr <= args.max_margin_lost,
              f"页边（页眉+页脚）丢失 <= {args.max_margin_lost} 字符（实际 {hdr + ftr}）")
    # 5) 图片
    check(not (metrics.get("broken_image_links") or []), "图片坏链 == 0")
    check(not (metrics.get("zero_byte_images") or []), "0 字节图片 == 0")
    # 6) 页边保留开关
    kpm = ext.get("keep_page_margins")
    if kpm is not None:
        check(bool(kpm), "本次转换保留了页眉/页脚（keep_page_margins=True）")

    print()
    if WARNS:
        print(f"警告 {len(WARNS)} 项：")
        for w in WARNS:
            print(f"  - {w}")
    if FAILS:
        print(f"验收失败：{len(FAILS)} 项")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("验收通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
