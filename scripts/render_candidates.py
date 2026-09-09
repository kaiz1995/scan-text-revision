#!/usr/bin/env python3
"""Render a local, user-selected text replacement candidate."""
from __future__ import annotations
import argparse, json, os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

def rebuild_background(crop: Image.Image) -> Image.Image:
    """Remove dark glyphs from a tight text crop; keep a deterministic fallback."""
    try:
        import cv2
        import numpy as np
        array = np.asarray(crop.convert("RGB"))
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        threshold = min(150, int(np.percentile(gray, 35)))
        old_ink = (gray < threshold).astype(np.uint8) * 255
        old_ink = cv2.dilate(old_ink, np.ones((3, 3), np.uint8), iterations=1)
        repaired = cv2.inpaint(array, old_ink, 3, cv2.INPAINT_TELEA)
        return Image.fromarray(repaired, mode="RGB")
    except (ImportError, ValueError):
        # ponytail: Pillow-only fallback loses fine paper grain; use OpenCV when installed.
        radius = max(2, min(12, max(crop.size) // 8))
        return crop.filter(ImageFilter.GaussianBlur(radius))

def parse_box(value: str) -> tuple[int, int, int, int]:
    try: box = tuple(int(v.strip()) for v in value.split(","))
    except ValueError as exc: raise argparse.ArgumentTypeError("bbox must be x,y,width,height") from exc
    if len(box) != 4 or box[2] <= 0 or box[3] <= 0: raise argparse.ArgumentTypeError("bbox must be x,y,width,height with positive width/height")
    return box

def fit_font(text: str, box: tuple[int, int, int, int], font_path: str | None):
    _, _, width, height = box
    if not font_path:
        font_dir = os.environ.get("WINDIR", r"C:\\Windows") + r"\\Fonts"
        for name in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
            candidate = os.path.join(font_dir, name)
            if os.path.isfile(candidate):
                font_path = candidate
                break
    if not font_path: return ImageFont.load_default()
    size = max(8, int(height * 0.82))
    while size >= 8:
        font = ImageFont.truetype(font_path, size)
        left, top, right, bottom = font.getbbox(text)
        if right - left <= width and bottom - top <= height: return font
        size -= 1
    return ImageFont.truetype(font_path, 8)

def render(image: Image.Image, box: tuple[int, int, int, int], text: str, font_path: str | None, blur: float):
    x, y, width, height = box
    if x < 0 or y < 0 or x + width > image.width or y + height > image.height: raise ValueError("bbox is outside the image")
    base = image.convert("RGB")
    crop = base.crop((x, y, x + width, y + height))
    background = rebuild_background(crop)
    mask = Image.new("L", crop.size, 0)
    draw = ImageDraw.Draw(mask)
    font = fit_font(text, box, font_path)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    tw, th = right - left, bottom - top
    tx, ty = max(0, (width - tw) // 2 - left), max(0, (height - th) // 2 - top)
    draw.text((tx, ty), text, fill=255, font=font)
    ink = Image.new("RGB", crop.size, (35, 35, 35))
    if blur > 0: ink = ink.filter(ImageFilter.GaussianBlur(blur))
    result = Image.composite(ink, background, mask)
    output = base.copy(); output.paste(result, (x, y))
    full_mask = Image.new("L", base.size, 0); full_mask.paste(mask, (x, y))
    return output, full_mask

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path); parser.add_argument("--bbox", required=True, type=parse_box); parser.add_argument("--text", required=True); parser.add_argument("--output", required=True, type=Path); parser.add_argument("--mask", type=Path); parser.add_argument("--font"); parser.add_argument("--blur", type=float, default=0.25)
    args = parser.parse_args()
    if not args.input.is_file(): parser.error(f"input is not a file: {args.input}")
    result, mask = render(Image.open(args.input), args.bbox, args.text, args.font, args.blur)
    args.output.parent.mkdir(parents=True, exist_ok=True); result.save(args.output)
    if args.mask: args.mask.parent.mkdir(parents=True, exist_ok=True); mask.save(args.mask)
    print(json.dumps({"status": "candidate", "output": str(args.output.resolve()), "mask": str(args.mask.resolve()) if args.mask else None, "approved": False}, ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
