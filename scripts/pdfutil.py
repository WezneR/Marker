#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\pdfutil.py —— PDF 小工具：页数、书签目录、抽页渲染。

用法：
  python pdfutil.py pages  <pdf>                      # 打印页数
  python pdfutil.py toc    <pdf>                      # 打印书签 JSON（0 基页码）
  python pdfutil.py render <pdf> <outdir> <n> [dpi]   # 均匀抽 n 页渲染 PNG
"""
import json
import os
import sys

try:                      # PyMuPDF >= 1.24 推荐包名，旧版仍是 fitz
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None


def pages(path: str) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(path).pages)
    except Exception:
        if fitz is None:
            raise
        with fitz.open(path) as doc:
            return doc.page_count


def toc(path: str):
    if fitz is None:
        return []
    out = []
    try:
        with fitz.open(path) as doc:
            for level, title, page in doc.get_toc():
                out.append({"level": level, "title": title, "page": max(page - 1, 0)})
    except Exception:
        pass
    return out


def render(path: str, outdir: str, count: int, dpi: int = 150):
    if fitz is None:
        raise RuntimeError("PyMuPDF 不可用，无法渲染抽样页")
    os.makedirs(outdir, exist_ok=True)
    written = []
    with fitz.open(path) as doc:
        total = doc.page_count
        if count >= total:
            wanted = list(range(total))
        else:
            step = total / float(count)
            wanted = sorted({int(i * step) for i in range(count)})
        for pno in wanted:
            pix = doc[pno].get_pixmap(dpi=dpi)
            name = f"page-{pno + 1:04d}.png"
            pix.save(os.path.join(outdir, name))
            written.append({"page": pno, "file": name})
    return written


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, path = sys.argv[1], sys.argv[2]
    if cmd == "pages":
        print(pages(path))
    elif cmd == "toc":
        print(json.dumps(toc(path), ensure_ascii=False, indent=2))
    elif cmd == "render":
        outdir = sys.argv[3]
        count = int(sys.argv[4]) if len(sys.argv) > 4 else 12
        dpi = int(sys.argv[5]) if len(sys.argv) > 5 else 150
        print(json.dumps(render(path, outdir, count, dpi), ensure_ascii=False, indent=2))
    else:
        print(f"unknown command: {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
