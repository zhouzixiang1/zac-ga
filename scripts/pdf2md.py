"""PDF → Markdown 转换器 v2（含图片提取）。

用法:  .venv_qmap/bin/python scripts/pdf2md.py <input.pdf> [-o out_dir]

v1 的问题（已在 v2 修复）：
  - 双栏论文左右栏按 y 交错 → 阅读顺序错乱。v2 做栏感知排序：
    跨中线的整宽块（标题/通栏图）作为分隔，每一段落群内先左栏后右栏。
  - PDF 每行一个硬换行 → 段落碎成一行行、单词被连字符截断。
    v2 按块重排段落（去连字符断词），列表/图注保持原结构。
  - 页眉页脚/版权行混入正文且被当标题。v2 用"跨页重复块"检测 + 规则剔除。
  - 连字符乱码（ﬁdelity 之类）→ 归一化为普通字母；零宽字符剔除；
    "10um" → "10 µm"。
  - 标题判定 v1 只看字号>13，大量正文被误判成 ##。v2 用全文正文字号做
    基准，结合粗体/长度/编号（I. / A.）分级。

仍不处理（留给人工通读后手写）：数学公式的语义（下标/上标/分式）、表格。
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

import pymupdf

ZOOM = 2.5
CAPTION_RE = re.compile(r"^(Fig(ure)?\.?\s*\d|图\s*\d|表\s*\d|Table\s+\d+)", re.I)
SECTION_RE = re.compile(r"^([IVXLC]{1,5})\.\s+(\S.*)$")
SUBSEC_RE = re.compile(r"^([A-Z])\.\s+(\S.*)$")
BULLET_RE = re.compile(r"^\s*([•◦‣▪])\s*")
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
             "\ufb03": "ffi", "\ufb04": "ffl"}
INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\xad\u2060"), None)
# 页面家具：页码 / DOI / 版权行（跨页重复块另有通用规则）。
# 页码必须整块全匹配——否则 "1) Qubit Reuse Strategy:..." 这类段首编号段会被误杀。
FURNITURE_RE = re.compile(
    r"^(\d{1,4}\s*$|DOI\s*10|9[78][13]-[\d\-]+|©|\d{4} IEEE|Authorized licensed)"
)
# 段首编号小节，如 "1) Qubit Reuse Strategy: As just mentioned, ..."
RUNIN_RE = re.compile(r"^([1-9]\)\s[^:]{3,60}):\s+(.+)$", re.S)


def clean_text(s: str) -> str:
    for k, v in LIGATURES.items():
        s = s.replace(k, v)
    s = s.translate(INVISIBLE)
    s = re.sub(r"(\d)\s*um\b", r"\1 µm", s)
    s = re.sub(r"(\d)\s*us\b", r"\1 µs", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def meaningful(s: str) -> bool:
    return bool(re.search(r"[\w\u4e00-\u9fff]", s))


class TextBlock:
    def __init__(self, bbox, lines, size, bold, nchars):
        self.bbox, self.lines = bbox, lines
        self.size, self.bold, self.nchars = size, bold, nchars

    @property
    def text(self):
        return "\n".join(self.lines)


def page_blocks(page) -> list[TextBlock]:
    """用 dict 模式提取，拿到每块的行文本、字号、粗体信息。"""
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        lines, size, bold, nchars = [], 0.0, True, 0
        for ln in b["lines"]:
            t = "".join(s["text"] for s in ln["spans"])
            for s in ln["spans"]:
                if s["text"].strip():
                    size = max(size, s["size"])
                    nchars += len(s["text"].strip())
                    if not (s["flags"] & 16 or "Bold" in s["font"]):
                        bold = False
            if t.strip():
                lines.append(clean_text(t))
        if lines:
            out.append(TextBlock(b["bbox"], lines, size, bold, nchars))
    return out


def merge_rects(rects, gap=8):
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
    """图形区域 = 位图块 ∪ 矢量绘图簇，去掉被文本覆盖的区域。"""
    cands = [b["bbox"] for b in page.get_image_info()]
    cands += list(page.cluster_drawings())
    w, h = page.rect.width, page.rect.height
    cands = [r for r in cands
             if 60 < (r[2] - r[0]) < 0.95 * w and 40 < (r[3] - r[1]) < 0.95 * h]
    tblocks = [pymupdf.Rect(b[:4]) for b in page.get_text("blocks")]
    regions = []
    for r in merge_rects(cands):
        cover = sum((b & r).get_area() for b in tblocks)
        if cover < 0.45 * r.get_area():
            regions.append(r)
    return regions


def reading_order(items, width):
    """栏感知阅读顺序。items: [(bbox, payload)]，payload 为 TextBlock 或 ('fig', name)。
    整宽块（横跨中线）作为分隔；分隔之间的窄块先左栏后右栏，栏内按 y。"""
    mid = width / 2

    def full(r):
        return r.x0 < 0.35 * width and r.x1 > 0.65 * width

    if not any(full(r) for r, _ in items):
        narrow = [(r, p) for r, p in items if (r.x1 - r.x0) < 0.75 * width]
        left = sum(1 for r, _ in narrow if r.x1 < 0.58 * width)
        right = sum(1 for r, _ in narrow if r.x0 > 0.42 * width)
        two_col = left >= 3 and right >= 3 and left + right >= 0.6 * len(items)
        if not two_col:
            return [p for _, p in sorted(items, key=lambda t: (round(t[0].y0, 1), t[0].x0))]

    ordered, group = [], []

    def flush():
        if group:
            lefts = sorted([g for g in group if (g[0].x0 + g[0].x1) / 2 < mid],
                           key=lambda g: (round(g[0].y0, 1), g[0].x0))
            rights = sorted([g for g in group if g not in lefts],
                            key=lambda g: (round(g[0].y0, 1), g[0].x0))
            ordered.extend(p for _, p in lefts + rights)
            group.clear()

    for r, p in sorted(items, key=lambda t: (round(t[0].y0, 1), t[0].x0)):
        if full(r):
            flush()
            ordered.append(p)
        else:
            group.append((r, p))
    flush()
    return ordered


def join_lines(lines: list[str]) -> str:
    """块内行合并成段落：连字符断词拼回，其余以空格相连。"""
    text = ""
    for ln in lines:
        if not text:
            text = ln
        elif text.endswith("-") and ln[:1].islower():
            text = text[:-1] + ln
        else:
            text += " " + ln
    return text


def block_markdown(b: TextBlock, body_size: float) -> list[str]:
    txt = b.text
    if CAPTION_RE.match(txt):
        return [f"*{join_text_keep_captions(b.lines)}*", ""]
    # "1) Title: body..." 段首编号小节 → 小标题 + 正文
    m = RUNIN_RE.match(txt)
    if m:
        return [f"#### {m.group(1).strip()}", "", join_lines(m.group(2).split("\n")), ""]
    # "I. INTRODUCTION" 标题可能与首段同块：拆出全大写标题部分
    m = re.match(r"^([IVXLC]{1,5}\.)\s+([A-Z][A-Z0-9,\- ]{2,}?)(?:\s\s|\s(?=[A-Z][a-z]))\s*(.*)$",
                 txt, re.S)
    if m and m.group(3):
        return [f"## {m.group(1)} {m.group(2).strip()}", "",
                join_lines(m.group(3).split("\n")), ""]
    is_heading = (b.size >= body_size + 1.5 or
                  (b.bold and b.nchars <= 120 and not txt.endswith(".")))
    if b.nchars <= 300 and SECTION_RE.match(txt):
        m = SECTION_RE.match(txt)
        return [f"## {m.group(1)}. {m.group(2)}", ""]
    if b.nchars <= 300 and SUBSEC_RE.match(txt) and (is_heading or b.bold):
        m = SUBSEC_RE.match(txt)
        return [f"### {m.group(1)}. {m.group(2)}", ""]
    if b.size >= body_size + 1.5 and b.nchars <= 80 and len(b.lines) <= 2:
        return [f"## {txt}", ""]
    # 列表块：以 •/◦ 开头的行各自成条，续行并入上一条
    if any(BULLET_RE.search(ln) for ln in b.lines):
        items = []
        for ln in b.lines:
            m = BULLET_RE.search(ln)
            if m:
                indent = "  " if m.group(1) == "◦" else ""
                items.append(f"{indent}- {BULLET_RE.sub('', ln)}")
            elif items:
                items[-1] += " " + ln
        return items + [""]
    return [join_lines(b.lines), ""]


def join_text_keep_captions(lines):
    text = ""
    for ln in lines:
        if not text:
            text = ln
        else:
            text += " " + ln
    return text


def convert(pdf_path: Path, out_dir: Path) -> None:
    doc = pymupdf.open(pdf_path)
    stem = pdf_path.stem
    assets = out_dir / f"{stem}_assets"
    assets.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"

    # 第一遍：全部块，统计正文字号（按字符数加权取众数）与跨页重复文本
    all_pages, size_votes, repeat = [], Counter(), Counter()
    for page in doc:
        blocks = page_blocks(page)
        all_pages.append(blocks)
        for b in blocks:
            size_votes[round(b.size)] += b.nchars
            key = re.sub(r"\d+", "#", b.text)[:60]
            repeat[key] += 1
    body_size = float(size_votes.most_common(1)[0][0]) if size_votes else 10.0

    lines, img_idx, title_done = [], 0, False
    for pno, page in enumerate(doc, start=1):
        regions = figure_regions(page)
        figures = []
        for r in regions:
            img_idx += 1
            pix = page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=r)
            name = f"p{pno:02d}_fig{img_idx:02d}.png"
            pix.save(assets / name)
            figures.append((r, ("fig", name, r)))

        blocks = []
        for b in all_pages[pno - 1]:
            if len(re.sub(r"\s", "", b.text)) < 4:
                continue  # 单字符/空白块：多为矢量图内部散落标签
            cx, cy = (b.bbox[0] + b.bbox[2]) / 2, (b.bbox[1] + b.bbox[3]) / 2
            if b.nchars < 80 and any(r.contains((cx, cy)) for r in regions):
                continue  # 图形区域内部的短文本
            blocks.append(b)

        # 首页：最大字号（±0.6pt）的块合并为文档标题
        pre = []
        if pno == 1 and not title_done and blocks:
            tsize = max(b.size for b in blocks)
            tb = sorted([b for b in blocks if b.size >= tsize - 0.6],
                        key=lambda b: b.bbox[1])
            if tb:
                pre = [f"# {join_lines([l for b in tb for l in b.lines])}", ""]
                skip = {id(b) for b in tb}
                blocks = [b for b in blocks if id(b) not in skip]
                title_done = True

        items = [(pymupdf.Rect(b.bbox), b) for b in blocks] + figures

        page_md = [f"<!-- page {pno} -->", ""]
        page_md.extend(pre)
        for payload in reading_order(items, page.rect.width):
            if isinstance(payload, tuple) and payload[0] == "fig":
                _, name, _ = payload
                page_md.append(f"![{name}]({assets.name}/{name})")
                page_md.append("")
                continue
            b = payload
            txt = b.text
            if not meaningful(txt):
                continue
            key = re.sub(r"\d+", "#", txt)[:60]
            if repeat[key] >= 3 or FURNITURE_RE.match(txt):
                continue  # 跨页重复的页眉/页脚/页码/版权行
            page_md.extend(block_markdown(b, body_size))
        if len(page_md) > 2:
            lines.extend(page_md)
            lines.append("")

    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"{md_path}  ({img_idx} figures)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", default="documents/md")
    a = ap.parse_args()
    convert(Path(a.pdf), Path(a.out))
