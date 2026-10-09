# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\marker_ext —— F:\\Marker 工作流的 marker 扩展包（不改 site-packages）。

包含：
  - inline_formula.InlineFormulaRecoveryProcessor
      行内公式缺口检测（PDF 把公式做成图片 XObject 时文字层会留缺口）→ 块级 OCR → 回填
  - inline_formula.GuardedEquationProcessor
      EquationProcessor 的保底子类（覆盖率不足则回滚整块重写）
  - equation_guard
      给 marker 自带 EquationProcessor 注入"覆盖率保底"的 monkey-patch（导入即生效）

用法见 scripts\\marker_chunk.py。
"""
from . import equation_guard  # noqa: F401  （导入即打保底补丁，须在处理器使用前）
from .inline_formula import (  # noqa: F401
    InlineFormulaRecoveryProcessor,
    OcrRewriteGuardProcessor,
    coverage,
    detect_line_gaps,
    page_gap_entries,
)

__all__ = [
    "InlineFormulaRecoveryProcessor",
    "OcrRewriteGuardProcessor",
    "detect_line_gaps",
    "page_gap_entries",
    "coverage",
    "equation_guard",
]
