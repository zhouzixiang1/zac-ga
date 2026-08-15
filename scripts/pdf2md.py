"""PDF → Markdown 转换器（含图片提取）。

用法:  .venv_qmap/bin/python documents/md/pdf2md.py <input.pdf> [-o out.md]

策略（针对学术论文 + 笔记型 PDF）：
  1. 文本：PyMuPDF 按阅读顺序(sort=True)逐块提取，双栏论文的顺序基本正确；
     行合并成段落时保留缩进层级，粗体/标题用字体大小推断。
  2. 图片：对每一页
       - 收集"非文本可视内容"的区域：内嵌位图块 + 矢量绘图簇
         (cluster_drawings)，合并重叠/相邻的簇；
       - 每个区域按 2.5 倍缩放渲染成 PNG（矢量图也能转，位图不劣化）；
       - 用随后的文本块里匹配 "Fig./Figure/图/表/Table" 前缀的行作为图注。
  3. 组装：文本块与图片块按页面纵向顺序交错输出 Markdown。
  已知局限：数学公式会乱（PDF 文本层无公式语义）；表格退化为文本行。
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pymupdf

ZOOM = 2.5
CAPTION_RE = re.compile(r"^(Fig(ure)?\.?|图\s*\d|表\s*\d|Table\s+\d+)", re.I)


def merge_rects(rects, gap=8):
    """合并重叠或间距很近的矩形（一次近似迭代即可）。"""
    rects = [r for r in map(pymupdf.Rect, rects) if not r.is_empty]
    rects.sort(key=lambda r: (r.y0, r.x0))
    merged = []
    for r in rects:
        if merged and (merged[-1].intersects(r + (-gap, -gap, gap, gap)) or
                       (abs(merged[-1].y0 - r.y0) < gap and merged[-1].x1 > r.x0 - gap)):
            merged[-1] |= r
        else:
            merged.append(r)
    return merged


def figure_regions(page):
    """一页里的图形区域 = 位图块 ∪ 矢量绘图簇（去掉纯文本区域）。"""
    cands = [b["bbox"] for b in page.get_image_info()]
    for c in page.cluster_drawings():
        cands.append(c)
    # 过滤太小的（噪点/装饰线）
    cands = [r for r in cands if (r[2] - r[0]) > 60 and (r[3] - r[1]) > 40]
    # 剔除大部分面积被文本覆盖的区域（避免把正文截成图）
    text_area = 0.0
    for b in page.get_text("blocks"):
        text_area += pymupdf.Rect(b[:4]).get_area()
    regions = []
    for r in merge_rects(cands):
        cover = 0.0
        for b in page.get_text("blocks"):
            inter = pymupdf.Rect(b[:4]) & r
            cover += inter.get_area()
        if cover < 0.45 * r.get_area():
            regions.append(r)
    return regions


def convert(pdf_path: Path, out_dir: Path) -> None:
    doc = pymupdf.open(pdf_path)
    stem = pdf_path.stem
    assets = out_dir / f"{stem}_assets"
    assets.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    lines = [f"# {stem}", ""]
    img_idx = 0

    for pno, page in enumerate(doc, start=1):
        # --- 图片先渲染好，记录其纵向位置 ---
        figures = []
        for r in figure_regions(page):
            img_idx += 1
            pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=r)
            name = f"p{pno:02d}_fig{img_idx:02d}.png"
            pix.save(assets / name)
            figures.append((r.y0, name, r))
        # --- 文本块与图片块按纵向位置交错 ---
        blocks = [b for b in page.get_text("blocks") if b[6] == 0]
        blocks.sort(key=lambda b: (round(b[1], 1), b[0]))
        items = [(b[1], "text", b) for b in blocks] + \
                [(y, "fig", (name, rect)) for (y, name, rect) in figures]
        items.sort(key=lambda t: (round(t[0], 1),))

        page_text = []
        for y, kind, payload in items:
            if kind == "fig":
                name, rect = payload
                page_text.append(f"![{name}]({assets.name}/{name})")
                page_text.append("")
            else:
                txt = payload[4].replace("\u00ad", "").strip()
                if not txt:
                    continue
                # 图注 → 斜体行
                if CAPTION_RE.match(txt):
                    page_text.append(f"*{txt}*")
                    page_text.append("")
                    continue
                size = payload[5] if len(payload) > 5 and isinstance(payload[5], (int, float)) else 0
                # 字号显著大 → 标题
                if size > 13:
                    page_text.append(f"## {txt}")
                else:
                    page_text.append(txt)
                page_text.append("")
        if page_text:
            lines.append(f"<!-- page {pno} -->")
            lines.append("")
            lines.extend(page_text)

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"{md_path}  ({img_idx} figures)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default="documents/md")
    a = ap.parse_args()
    convert(Path(a.pdf), Path(a.out))
