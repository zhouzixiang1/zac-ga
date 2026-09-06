#!/usr/bin/env python3
"""Render the built manuscript into root build/paper_zh/preview only."""

from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    output = root / "build" / "paper_zh"
    pdf = output / "paper_zh.pdf"
    if not pdf.is_file():
        raise SystemExit("Build the paper first with make paper.")
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
                    str(output / "figures" / "overall_framework.pdf"),
                    str(preview / "overall_framework")], check=True)
    print(f"Preview: {output / 'page_overview.png'}")


if __name__ == "__main__":
    main()
