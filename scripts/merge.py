#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\merge.py —— 合并分块产物为全书 Markdown。

做四件事：
  1. 按块序拼接，把 --paginate_output 写的页分隔标记统一成 <!-- page: N --> 便于溯源；
  2. 把各块图片集中到 merged/images/ 并把链接改写成 images/xxx（重名自动加 chunk-NN- 前缀）；
  3. 生成 book_toc.md（优先用 PDF 自带书签，其次用各块 _meta.json 的 table_of_contents）；
  4. 输出统计信息，并在页面编号疑似「块内相对」时给出明确警告。

用法：
  python merge.py --chunk-dir F:\\Marker\\out\\chunks --out-dir F:\\Marker\\out\\merged \\
                  --pdf F:\\Marker\\input\\book.pdf [--no-chunk-markers]
"""
import argparse
import json
import os
import re
import shutil
import sys

PAGE_MARK = re.compile(r"\{([^{}\n]+)\}-{10,}")
IMG_LINK = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\s*\"[^\"]*\")?(\))")
IMG_EXT = (".jpeg", ".jpg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def page_number(gid: str):
    """从 data-page-id 形态里取出页码：'10' 或 '/page/10/Page/366' 都能处理。"""
    gid = gid.strip()
    m = re.search(r"/page/(\d+)/", gid)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", gid)
    return int(nums[0]) if nums else None


def find_markdown(chunk_dir: str):
    """marker_single 会输出到 <out>/<stem>/<stem>.md，也兼容直接落在块根目录的情况。"""
    direct = [f for f in os.listdir(chunk_dir) if f.lower().endswith(".md")]
    if direct:
        return os.path.join(chunk_dir, direct[0])
    for name in sorted(os.listdir(chunk_dir)):
        sub = os.path.join(chunk_dir, name)
        if os.path.isdir(sub):
            mds = [f for f in os.listdir(sub) if f.lower().endswith(".md")]
            if mds:
                return os.path.join(sub, mds[0])
    return None


def find_meta(chunk_dir: str):
    for root, _dirs, files in os.walk(chunk_dir):
        for f in files:
            if f.endswith("_meta.json"):
                return os.path.join(root, f)
    return None


def load_pdf_toc(pdf: str):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from pdfutil import toc
        return toc(pdf) if pdf and os.path.exists(pdf) else []
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--pdf", default="")
    ap.add_argument("--no-chunk-markers", action="store_true")
    args = ap.parse_args()

    images_dir = os.path.join(args.out_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    chunk_names = sorted(d for d in os.listdir(args.chunk_dir)
                         if d.startswith("chunk-") and os.path.isdir(os.path.join(args.chunk_dir, d)))
    if not chunk_names:
        print(f"[merge] 在 {args.chunk_dir} 下找不到 chunk-* 目录", file=sys.stderr)
        return 2

    used_image_names = set()
    page_id_trace = []
    meta_tocs = []
    parts = []
    stats = {"chunks": [], "images_copied": 0, "images_renamed": 0, "total_chars": 0}

    for cname in chunk_names:
        cdir = os.path.join(args.chunk_dir, cname)
        md = find_markdown(cdir)
        if not md:
            print(f"[merge] 跳过 {cname}：没有 .md（该块可能没跑完）", file=sys.stderr)
            continue
        with open(md, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

        # 1) 页标记归一化，并记录页号
        ids = []

        def _sub(m):
            pno = page_number(m.group(1))
            ids.append(pno)
            return f"<!-- page: {pno if pno is not None else m.group(1)} -->"

        text = PAGE_MARK.sub(_sub, text)
        page_id_trace.append({"chunk": cname, "first": ids[0] if ids else None,
                              "last": ids[-1] if ids else None, "count": len(ids)})

        # 2) 图片集中 + 链接重写
        md_dir = os.path.dirname(md)
        local_map = {}
        for f in os.listdir(md_dir):
            if f.lower().endswith(IMG_EXT):
                target = f
                if target in used_image_names:
                    target = f"{cname}-{f}"
                    stats["images_renamed"] += 1
                used_image_names.add(target)
                shutil.copy2(os.path.join(md_dir, f), os.path.join(images_dir, target))
                local_map[f] = target
                stats["images_copied"] += 1

        def _rewrite(m):
            prefix, target, title, close = m.group(1), m.group(2), m.group(3) or "", m.group(4)
            base = os.path.basename(target)
            if base in local_map:
                return f"{prefix}images/{local_map[base]}{title}{close}"
            return m.group(0)

        text = IMG_LINK.sub(_rewrite, text)

        # 3) 块级注释（渲染时不可见，便于回溯）
        if not args.no_chunk_markers:
            text = f"<!-- === {cname} === -->\n\n" + text.strip() + "\n"
        parts.append(text.strip())
        stats["chunks"].append({"chunk": cname, "md": os.path.relpath(md, args.chunk_dir),
                                "chars": len(text), "pages": len(ids)})
        stats["total_chars"] += len(text)

        mpath = find_meta(cdir)
        if mpath:
            try:
                with open(mpath, "r", encoding="utf-8", errors="replace") as f:
                    meta = json.load(f)
                for item in (meta.get("table_of_contents") or []):
                    item = dict(item)
                    item["_chunk"] = cname
                    meta_tocs.append(item)
            except Exception as e:
                print(f"[merge] 读取 {mpath} 失败：{e}", file=sys.stderr)

    book_md = os.path.join(args.out_dir, "book.md")
    with open(book_md, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n\n".join(parts) + "\n")

    # 4) 目录
    toc_lines = ["# 目录\n"]
    pdf_toc = load_pdf_toc(args.pdf)
    if pdf_toc:
        toc_lines.append("（来源：PDF 自带书签）\n")
        for it in pdf_toc:
            indent = "  " * max(int(it.get("level", 1)) - 1, 0)
            toc_lines.append(f"{indent}- {it.get('title','')}  <!-- p{int(it.get('page',0)) + 1} -->")
    elif meta_tocs:
        toc_lines.append("（来源：Marker 识别的 document TOC）\n")
        seen = set()
        for it in meta_tocs:
            key = (it.get("title"), it.get("heading_level"))
            if key in seen:
                continue
            seen.add(key)
            indent = "  " * max(int(it.get("heading_level", 1)) - 1, 0)
            toc_lines.append(f"{indent}- {it.get('title','')}  <!-- p{int(it.get('page_id',0)) + 1} -->")
    else:
        toc_lines.append("（未找到 PDF 书签，也未识别出 TOC）")
    toc_path = os.path.join(args.out_dir, "book_toc.md")
    with open(toc_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(toc_lines) + "\n")

    # 5) 页号连续性判断
    firsts = [t["first"] for t in page_id_trace if t["first"] is not None]
    relative = len(firsts) > 1 and all(f == 0 for f in firsts[:2])
    stats["page_id_trace"] = page_id_trace
    stats["page_ids_look_chunk_relative"] = bool(relative)
    stats["book_md"] = book_md
    stats["book_toc"] = toc_path

    with open(os.path.join(args.out_dir, "merge-stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"[merge] 合并 {len(stats['chunks'])} 块 -> {book_md}（{stats['total_chars']:,} 字符）")
    print(f"[merge] 图片 {stats['images_copied']} 张（重名改写 {stats['images_renamed']} 张）-> {images_dir}")
    print(f"[merge] 目录 -> {toc_path}（{len(pdf_toc)} 条书签 / {len(meta_tocs)} 条 meta TOC）")
    for t in page_id_trace:
        print(f"[merge]   {t['chunk']}: page_id {t['first']} .. {t['last']}（{t['count']} 个页标记）")
    if relative:
        print("[merge] 注意：各块的 page_id 都从 0 开始，是块内相对页号；页覆盖核对请依据块区间。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
