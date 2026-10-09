#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F:\\Marker\\scripts\\make-test-pdf.py —— 生成中文冒烟测试 PDF（3 页）。

目的：在正式那本书到位之前，先把整条链路（vLLM 附着 -> 中文版式 -> 公式 -> 表格 -> 图表导出）
验证一遍。内容为合成的金融分析片段，包含：
  P1 中文正文 + 行内/独立公式（含希腊字母与上下标）
  P2 一张三线表
  P3 一张折线图（矢量绘制，Marker 应识别为 Figure/Picture 并导出图片）

用法：python make-test-pdf.py [输出路径]
"""
import os
import sys

DEFAULT_OUT = r"F:\Marker\input\_smoketest-zh.pdf"

CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
]
LATIN_FONT_CANDIDATES = [r"C:\Windows\Fonts\times.ttf", r"C:\Windows\Fonts\arial.ttf"]

P1_TITLE = "第三章　资产定价模型与实证检验"
P1_BODY = [
    "本章讨论资本资产定价模型（CAPM）在中国A股市场的适用性，并给出基于月度收益率数据的实证结果。",
    "我们使用沪深300指数作为市场组合的代理变量，样本区间为2015年1月至2024年12月，共120个月度观测值。",
    "对个股i在t月的超额收益，估计如下时序回归模型：",
    "其中R_it为个股收益率，R_ft为无风险利率（用一年期国债收益率代替），R_mt为市场组合收益率，ε_it为残差项。",
    "表3-1报告了按市值分组的组合回归结果。结果显示，小市值组合的β系数显著大于1，而大市值组合的β系数接近0.9，",
    "这与Fama-French三因子模型中的规模效应一致。调整后的R²介于0.35至0.62之间，说明市场因子能够解释约四成的收益变异。",
]
P1_FORMULA_DISPLAY = "R_it − R_ft = α_i + β_i (R_mt − R_ft) + ε_it"
P1_FORMULA_2 = "Sharpe_p = ( E[R_p] − R_f ) / σ_p"

TABLE_TITLE = "表3-1　按市值分组的CAPM回归结果"
TABLE_HEADER = ["市值分组", "α（年化）", "β", "t(β)", "调整R²", "样本数"]
TABLE_ROWS = [
    ["小市值（前30%）", "0.0412", "1.253", "5.87", "0.512", "40"],
    ["中市值（中间40%）", "0.0128", "1.041", "7.42", "0.604", "40"],
    ["大市值（后30%）", "-0.0063", "0.887", "9.15", "0.623", "40"],
]

CHART_TITLE = "图3-1　三组组合的累计净值走势（2015=1.00）"
CHART_SERIES = [
    ("小市值", [(0, 1.0), (1, 1.42), (2, 1.31), (3, 1.88), (4, 2.14), (5, 2.02), (6, 2.66)]),
    ("中市值", [(0, 1.0), (1, 1.18), (2, 1.22), (3, 1.41), (4, 1.52), (5, 1.49), (6, 1.72)]),
    ("大市值", [(0, 1.0), (1, 1.09), (2, 1.14), (3, 1.21), (4, 1.28), (5, 1.24), (6, 1.36)]),
]
CHART_XLABELS = ["2015", "2016", "2017", "2018", "2019", "2020", "2021"]


def pick_font(cands):
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    os.makedirs(os.path.dirname(out), exist_ok=True)

    import fitz  # PyMuPDF

    cjk = pick_font(CJK_FONT_CANDIDATES)
    latin = pick_font(LATIN_FONT_CANDIDATES)
    if not cjk:
        print("[test-pdf] 找不到中文字体，无法生成中文测试 PDF", file=sys.stderr)
        return 2

    doc = fitz.open()
    W, H = fitz.paper_size("a4")
    margin = 64.0
    line_h = 20.0

    def new_page():
        p = doc.new_page(width=W, height=H)
        return p

    def cjk_text(page, x, y, text, size=11.5):
        page.insert_text((x, y), text, fontsize=size, fontfile=cjk, fontname="cjk")

    def latin_text(page, x, y, text, size=11.5):
        if latin:
            page.insert_text((x, y), text, fontsize=size, fontfile=latin, fontname="lat")
        else:
            page.insert_text((x, y), text, fontsize=size, fontname="helv")

    # ---- P1: 正文 + 公式 ----
    p1 = new_page()
    y = margin + 20
    cjk_text(p1, margin, y, P1_TITLE, size=16)
    y += 34
    for para in P1_BODY:
        cjk_text(p1, margin, y, para)
        y += line_h
        if para.endswith("：") or para.endswith("模型："):
            # 独立居中公式
            y += 10
            latin_text(p1, margin + 120, y, P1_FORMULA_DISPLAY, size=12.5)
            y += 26
    y += 6
    cjk_text(p1, margin, y, "夏普比率定义为：")
    latin_text(p1, margin + 110, y, P1_FORMULA_2, size=11.5)
    y += 30
    cjk_text(p1, margin, y, "注：β系数均在1%水平上显著；标准误经Newey-West调整。")

    # ---- P2: 表格 ----
    p2 = new_page()
    y = margin + 20
    cjk_text(p2, margin, y, TABLE_TITLE, size=14)
    y += 30
    col_w = [130, 78, 62, 62, 78, 62]
    row_h = 26
    x0 = margin
    # 表头 + 三线表样式
    x = x0
    for i, h in enumerate(TABLE_HEADER):
        cjk_text(p2, x + 4, y + 17, h, size=10.5)
        x += col_w[i]
    p2.draw_line((x0, y + 4), (x0 + sum(col_w), y + 4), width=1.1)
    p2.draw_line((x0, y + row_h - 2), (x0 + sum(col_w), y + row_h - 2), width=0.6)
    y += row_h
    for row in TABLE_ROWS:
        x = x0
        for i, cell in enumerate(row):
            cjk_text(p2, x + 4, y + 17, cell, size=10.5)
            x += col_w[i]
        y += row_h
    p2.draw_line((x0, y - 2), (x0 + sum(col_w), y - 2), width=1.1)
    y += 24
    cjk_text(p2, margin, y, "注：α为年化后截距项，括号内为t统计量；数据来源：Wind，作者计算。")

    # ---- P3: 图表 ----
    p3 = new_page()
    y = margin + 20
    cjk_text(p3, margin, y, CHART_TITLE, size=14)
    box = fitz.Rect(margin, y + 24, W - margin, y + 24 + 300)
    p3.draw_rect(box, color=(0.75, 0.75, 0.75), width=0.6)
    # 坐标轴
    ax_x0, ax_y0 = box.x0 + 58, box.y1 - 44
    ax_x1, ax_y1 = box.x1 - 24, box.y0 + 20
    p3.draw_line((ax_x0, ax_y0), (ax_x1, ax_y0), width=0.8)
    p3.draw_line((ax_x0, ax_y0), (ax_x0, ax_y1), width=0.8)
    vmax, vmin = 2.8, 0.9

    def sx(i):
        return ax_x0 + (ax_x1 - ax_x0) * i / (len(CHART_XLABELS) - 1)

    def sy(v):
        return ax_y0 - (ax_y0 - ax_y1) * (v - vmin) / (vmax - vmin)

    # y 轴刻度
    for v in (1.0, 1.5, 2.0, 2.5):
        yy = sy(v)
        p3.draw_line((ax_x0 - 4, yy), (ax_x0, yy), width=0.6)
        latin_text(p3, ax_x0 - 34, yy + 4, f"{v:.1f}", size=8.5)
    for i, lab in enumerate(CHART_XLABELS):
        latin_text(p3, sx(i) - 12, ax_y0 + 14, lab, size=8.5)
    # 三条曲线
    styles = [((0.85, 0.2, 0.2), 1.4), ((0.15, 0.45, 0.85), 1.4), ((0.2, 0.6, 0.25), 1.4)]
    for (name, pts), (color, wd) in zip(CHART_SERIES, styles):
        poly = [fitz.Point(sx(i), sy(v)) for i, v in pts]
        p3.draw_polyline(poly, color=color, width=wd)
    # 图例
    ly = box.y0 + 34
    for (name, _pts), (color, _w) in zip(CHART_SERIES, styles):
        p3.draw_line((box.x1 - 150, ly), (box.x1 - 132, ly), color=color, width=2.0)
        cjk_text(p3, box.x1 - 128, ly + 4, name, size=9.5)
        ly += 16
    y = box.y1 + 24
    cjk_text(p3, margin, y, "注：净值已做分红再投资调整；小市值组在2019年后显著跑赢。")

    doc.save(out, deflate=True)
    doc.close()
    print(f"[test-pdf] 已生成 {out}（{os.path.getsize(out) / 1024:.1f} KB，3 页）")
    print(f"[test-pdf] 中文字体：{cjk}；西文字体：{latin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
