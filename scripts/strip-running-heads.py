#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\strip-running-heads.py —— 识别（并按需删除）跨页重复的页眉/页脚行。

背景：现在转换默认打开 keep_pageheader_in_output / keep_pagefooter_in_output，
**一个字都不丢**；代价是真正的"跑动页眉、页码"也会进正文。本脚本负责"按需去噪"：

  1. 默认只出报告（out/qa/running-head-candidates.md），列出"在多页的页面首行/末行
     反复出现且位置一致"的行，供人工确认；
  2. 加 --apply 才真删。删除是**逐页精确删除整行**，不影响其它内容；
  3. 候选判据（保守，宁少勿多）：
     - 行出现在 >= --min-pages 个页面的首行（或末行）
     - 出现率 >= --min-ratio（占有效页比例）
     - 行长度 <= --max-chars，且不含句号/问号/感叹号（正文句子通常带这些）
     - 允许首尾页码数字差异（"第 12 页" 与 "第 13 页" 视为同一模板）

用法：
  python strip-running-heads.py --merged-md F:\\Marker\\out\\merged\\book.md \\
         [--out-report F:\\Marker\\out\\qa\\running-head-candidates.md]
         [--min-pages 20] [--min-ratio 0.2] [--max-chars 60] [--apply] [--dry-run]
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

PAGE_MARK = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
SENTENCE_PUNCT = re.compile(r"[。！？；.!?;]")
# 跳过页标记、块标记、注释行（它们天然出现在每页开头）
SKIP_LINE = re.compile(r"^\s*(<!--.*-->|```|\|.*\|)\s*$")


def template(line: str) -> str:
    """把数字替换成 #，用于把"第 12 页/第 13 页"归为同一模板。"""
    return re.sub(r"\d+", "#", line.strip())


def split_pages(text: str):
    marks = list(PAGE_MARK.finditer(text))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), m.start(), m.end(), text[m.end():end]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged-md", required=True)
    ap.add_argument("--out-report", default="")
    ap.add_argument("--min-pages", type=int, default=20)
    ap.add_argument("--min-ratio", type=float, default=0.2)
    ap.add_argument("--max-chars", type=int, default=60)
    ap.add_argument("--apply", action="store_true", help="真正删除候选行")
    ap.add_argument("--dry-run", action="store_true", help="与 --apply 一起用时只打印")
    args = ap.parse_args()

    text = open(args.merged_md, "r", encoding="utf-8", errors="replace").read()
    pages = split_pages(text)

    heads = Counter()
    tails = Counter()
    for _pno, _s, _e, body in pages:
        lines = [ln for ln in body.split("\n") if ln.strip() and not SKIP_LINE.match(ln)]
        if not lines:
            continue
        heads[template(lines[0])] += 1
        tails[template(lines[-1])] += 1

    valid_pages = sum(1 for _p, _s, _e, b in pages if b.strip())
    if valid_pages == 0:
        print("[strip-heads] 没有有效页（缺 <!-- page: N --> 标记？）")
        return 0
    min_pages = max(args.min_pages, int(valid_pages * args.min_ratio))

    def is_candidate(tpl, count):
        if count < min_pages:
            return False
        if len(tpl) > args.max_chars:
            return False
        if SENTENCE_PUNCT.search(tpl):
            return False
        return True

    head_c = {t: c for t, c in heads.items() if is_candidate(t, c)}
    tail_c = {t: c for t, c in tails.items() if is_candidate(t, c)}

    L = ["# 跑动页眉/页脚候选（人工确认后可用 --apply 删除）\n"]
    L.append(f"- 合并文件：`{args.merged_md}`")
    L.append(f"- 有效页数：{valid_pages}；判定阈值：出现 >= {min_pages} 页、长度 <= {args.max_chars}、"
             f"不含句末标点")
    L.append(f"- 候选：页首 {len(head_c)} 个模板、页尾 {len(tail_c)} 个模板\n")
    for title, bucket in (("页首候选", head_c), ("页尾候选", tail_c)):
        L.append(f"## {title}\n")
        if not bucket:
            L.append("（无）\n")
            continue
        L.append("| 模板（数字已归一为 #） | 出现页数 | 占比 |")
        L.append("|---|---|---|")
        for tpl, cnt in sorted(bucket.items(), key=lambda x: -x[1]):
            L.append(f"| `{tpl}` | {cnt} | {cnt/valid_pages:.0%} |")
        L.append("")

    report = args.out_report or os.path.join(os.path.dirname(args.merged_md), "..", "qa",
                                             "running-head-candidates.md")
    report = os.path.abspath(report)
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"[strip-heads] 候选：页首 {len(head_c)} / 页尾 {len(tail_c)} -> {report}")

    if not args.apply:
        print("[strip-heads] 默认不修改文件；确认候选后加 --apply 删除。")
        return 0

    victim = set(head_c) | set(tail_c)
    if not victim:
        print("[strip-heads] 没有候选，无需删除。")
        return 0

    out = []
    last = 0
    removed = defaultdict(int)
    for _pno, s, e, body in pages:
        out.append(text[last:s])
        out.append(text[s:e])
        lines = body.split("\n")
        # 只处理该页"非注释/非表格"的首行/末行（与候选统计口径一致）
        cand_idx = [i for i, ln in enumerate(lines)
                    if ln.strip() and not SKIP_LINE.match(ln)]
        drop = set()
        if cand_idx and template(lines[cand_idx[0]]) in head_c:
            drop.add(cand_idx[0])
        if cand_idx and template(lines[cand_idx[-1]]) in tail_c:
            drop.add(cand_idx[-1])
        new_lines = [ln for i, ln in enumerate(lines) if i not in drop]
        for i in drop:
            removed[template(lines[i])] += 1
        out.append("\n".join(new_lines))
        last = e
    out.append(text[last:])

    if args.dry_run:
        print(f"[strip-heads] （dry-run）将删除 {sum(removed.values())} 行：")
        for tpl, cnt in sorted(removed.items(), key=lambda x: -x[1]):
            print(f"   x{cnt}  {tpl}")
        return 0

    with open(args.merged_md, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(out))
    print(f"[strip-heads] 已删除 {sum(removed.values())} 行（{len(removed)} 个模板）")
    for tpl, cnt in sorted(removed.items(), key=lambda x: -x[1]):
        print(f"   x{cnt}  {tpl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
