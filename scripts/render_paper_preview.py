#!/usr/bin/env python3
"""Render either built manuscript into its root build/paper_<language>/preview."""

import argparse
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", choices=("zh", "en"), default="zh")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = root / "build" / f"paper_{args.language}"
    pdf = output / f"paper_{args.language}.pdf"
    if not pdf.is_file():
        raise SystemExit("Build the paper first with make paper" + ("-en." if args.language == "en" else "."))
    preview = output / "preview"
    preview.mkdir(parents=True, exist_ok=True)
    info = subprocess.check_output(["pdfinfo", str(pdf)], text=True)
    pages = int(next(line.split(":", 1)[1] for line in info.splitlines()
                     if line.startswith("Pages:")))
    subprocess.run(["pdftoppm", "-r", "180", "-png", str(pdf),
                    str(preview / "page")], check=True)
    width, height, gap, label_height = 600, 849, 20, 28
    rows = (pages + 2) // 3
    sheet = Image.new("RGB", (3 * width + 4 * gap,
                             rows * (height + label_height) + (rows + 1) * gap),
                      "#e8e8e8")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    digits = len(str(pages))
    expected_pages = {f"page-{index:0{digits}d}.png" for index in range(1, pages + 1)}
    # Remove only obsolete page renders from this generated preview directory.
    # A shorter rebuild must not leave a stale extra page for manual review.
    for old_page in preview.glob("page-*.png"):
        if re.fullmatch(r"page-\d+\.png", old_page.name) and old_page.name not in expected_pages:
            old_page.unlink()
    for index in range(pages):
        path = preview / f"page-{index + 1:0{digits}d}.png"
        with Image.open(path) as original:
            page = original.convert("RGB")
        page.thumbnail((width, height), Image.Resampling.LANCZOS)
        x = gap + (index % 3) * (width + gap)
        y = gap + (index // 3) * (height + label_height + gap)
        draw.text((x + 8, y + 2), f"Page {index + 1}", fill="#222222", font=font)
        sheet.paste(page, (x, y + label_height))
    sheet.save(output / "page_overview.png")
    subprocess.run(["pdftoppm", "-r", "180", "-png", "-singlefile",
                    str(root / "build/paper_zh/figures/overall_framework.pdf"),
                    str(preview / "overall_framework")], check=True)
    print(f"Preview: {output / 'page_overview.png'}")


if __name__ == "__main__":
    main()
