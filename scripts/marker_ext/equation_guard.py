# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\marker_ext\\equation_guard.py —— 给 marker 的 EquationProcessor 打"同链路保底补丁"（兼容层）。

背景：marker 2.0.0 的 `EquationProcessor` 对"疑似含行内公式的文本块"直接执行

    block.html = block_result.html.strip()
    block.structure = []

整块替换且没有保底，VLM 少回一段正文就永久消失（实测 p13 cov=0）。当前版本里所有
"整块重写"都内联在 `__call__` 里，没有统一钩子；本模块的做法是：

  * 如果某个 marker 版本提供了 `EquationProcessor._recover_rewrite`（上游重构后的钩子），
    就把它替换成 inline_formula.make_equation_recover_rewrite 的保底实现；
  * 如果没有这个钩子（当前 2.0.0 就是这样），本模块只做**登记**，真正的保底交给
    `OcrRewriteGuardProcessor`（它在处理器链末端按覆盖率回滚任何 OCR 重写）。

补丁幂等，可用环境变量 `MARKER_EXT_DISABLE_GUARD=1` 完全关闭（A/B 对比用）。
"""
from __future__ import annotations

import os

from marker.logger import get_logger
from marker.processors.equation import EquationProcessor

from . import inline_formula as IF

logger = get_logger()

_PATCH_MARKER = "_marker_ext_recover_rewrite_patched"
_HAS_HOOK = hasattr(EquationProcessor, "_recover_rewrite")
DISABLE_ENV = "MARKER_EXT_DISABLE_GUARD"


def guard_enabled() -> bool:
    return str(os.environ.get(DISABLE_ENV, "")).strip() not in ("1", "true", "True", "yes")


def apply_patch(force: bool = False) -> bool:
    """给 EquationProcessor 注入覆盖率保底。返回是否真的改了方法。"""
    if getattr(EquationProcessor, _PATCH_MARKER, False):
        return False
    if not force and not guard_enabled():
        logger.warning("[equation-guard] 已被 %s 禁用，不注入保底逻辑", DISABLE_ENV)
        return False
    if not _HAS_HOOK:
        setattr(EquationProcessor, _PATCH_MARKER, True)
        logger.info(
            "[equation-guard] 当前 marker 的 EquationProcessor 没有 _recover_rewrite 钩子，"
            "跳过 monkey-patch；整块重写保底由 OcrRewriteGuardProcessor 承担"
        )
        return False
    EquationProcessor._marker_ext_original_recover_rewrite = EquationProcessor._recover_rewrite
    EquationProcessor._recover_rewrite = IF.make_equation_recover_rewrite()
    setattr(EquationProcessor, _PATCH_MARKER, True)
    logger.info("[equation-guard] 已为 EquationProcessor._recover_rewrite 注入覆盖率保底")
    return True


def patch_status() -> dict:
    return {
        "hook_present": _HAS_HOOK,
        "patched": bool(_HAS_HOOK and getattr(EquationProcessor, _PATCH_MARKER, False)),
        "checked": bool(getattr(EquationProcessor, _PATCH_MARKER, False)),
        "disabled_env": None if guard_enabled() else DISABLE_ENV,
    }


# 导入即打补丁（幂等）
apply_patch()
