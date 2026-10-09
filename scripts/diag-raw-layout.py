#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\diag-raw-layout.py —— 把 surya 版式解析失败的**完整原文**抓下来。

surya 的 parse_layout 用 r"\\[.*\\]" 找 JSON 数组；报 "No JSON array found"
说明输出里根本没有结尾的 ']'（被截断），或数组之外还有问题。
本脚本在进程内给 parse_layout 打补丁，把失败原文原封不动落到 tmp 目录。

用法：python diag-raw-layout.py <pdf> <start_page> <end_page>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DUMP = r"F:\Marker\tmp"
os.makedirs(DUMP, exist_ok=True)

import surya.inference.parsers as P

_orig = P.parse_layout
_state = {"n": 0}


def _wrapped(text):
    try:
        return _orig(text)
    except Exception as e:
        _state["n"] += 1
        n = _state["n"]
        path = os.path.join(DUMP, f"layout-fail-{n}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        has_close_bracket = "]" in text
        has_close_brace = "}" in text
        n_open = text.count("[")
        n_close = text.count("]")
        print(f"[diag] #{n} 解析失败 len={len(text)} 字符 -> {path}", flush=True)
        print(f"[diag] #{n} 开头: {text[:160]!r}", flush=True)
        print(f"[diag] #{n} 结尾: {text[-400:]!r}", flush=True)
        print(f"[diag] #{n} 含']'={has_close_bracket} 含'}}'={has_close_brace} 左括号={n_open} 右括号={n_close}", flush=True)
        raise


P.parse_layout = _wrapped
try:
    import surya.layout as SL
    SL.parse_layout = _wrapped
except Exception as e:
    print(f"[diag] 重绑 surya.layout 失败: {e}", flush=True)

from marker.config.parser import ConfigParser
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict
from marker.output import text_from_rendered

pdf = sys.argv[1]
start = int(sys.argv[2])
end = int(sys.argv[3])

config = {
    "page_range": f"{start}-{end}",
    "output_format": "markdown",
    "mode": "balanced",
    "paginate_output": True,
}
cp = ConfigParser(config)
conv = PdfConverter(
    config=cp.generate_config_dict(),
    artifact_dict=create_model_dict(),
    renderer=cp.get_renderer(),
)
print(f"[diag] 开始转换页 {start}-{end}", flush=True)
try:
    rendered = conv(pdf)
    text, _ext, images = text_from_rendered(rendered)
    out = os.path.join(DUMP, f"diag-out-{start}-{end}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[diag] 转换成功，{len(text)} 字符 -> {out}", flush=True)
except Exception as e:
    print(f"[diag] 转换失败: {type(e).__name__}: {str(e)[:300]}", flush=True)

print(f"[diag] 共捕获 {_state['n']} 次版式解析失败", flush=True)
