#!/usr/bin/env python3
"""Render a local, user-selected text replacement candidate.

Primary path delegates to ``glyph_engine``::

    measure -> build_glyph_library -> render -> compose

so the replacement inherits the measured baseline, stroke width, blur sigma and
ink colour of the source page, and borrows the original glyph pixels whenever
the line already contains the character.  ``--legacy`` keeps the old
``cv2.inpaint`` block repaint as a documented fallback for images the model does
not fit (textured backgrounds, non-text layouts).
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path: sys.path.insert(0, str(SCRIPT_DIR))


# --------------------------------------------------------------------------- #
# Legacy path (--legacy): whole-crop repaint.  Kept unchanged so existing
# call sites and screenshots stay reproducible, but no longer the default.
# --------------------------------------------------------------------------- #
def rebuild_background(crop: Image.Image) -> Image.Image:
    """Remove dark glyphs from a tight text crop; keep a deterministic fallback."""
    try:
        import cv2
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

def render_legacy(image: Image.Image, box: tuple[int, int, int, int], text: str, font_path: str | None, blur: float):
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


# --------------------------------------------------------------------------- #
# glyph_engine adapter.  The engine is a sibling module written separately; the
# helpers below only normalise inputs/outputs and never re-implement its maths.
# --------------------------------------------------------------------------- #
def load_engine():
    try:
        import glyph_engine
    except ImportError as exc:
        raise SystemExit("glyph_engine.py is not available next to this script; "
                         "run with --legacy or install the engine module") from exc
    return glyph_engine

def _result_image(result):
    if isinstance(result, Image.Image): return result.convert("RGB")
    return Image.fromarray(np.clip(np.rint(np.asarray(result)), 0, 255).astype(np.uint8), "RGB")

def _alpha_array(metrics):
    alpha = metrics.get("alpha") if isinstance(metrics, dict) else None
    if alpha is None: return None
    array = np.asarray(alpha, np.float32)
    return array[..., 0] if array.ndim > 2 else array

def _ink_list(value):
    array = np.asarray(value, np.float32).ravel()
    if array.size == 1: array = np.repeat(array, 3)
    return [int(round(float(v))) for v in array[:3]]

def segment_boxes(alpha, roi, text):
    """Split the measured glyph mask into one absolute box per character.

    ``measure`` returns connected components without character labels, so the
    caller pairs them with ``--page-text``.  Components are grouped by
    horizontal overlap (日/月 consist of several components) and, if the count
    still disagrees with the text, the ink span is split evenly as a last
    resort.  Returns ``(x, y, width, height)`` boxes in absolute pixels.
    """
    x0, y0, x1, y1 = roi
    mask = (alpha > 0.5).astype(np.uint8)
    groups = []
    try:
        import cv2
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        components = sorted((stats[i, 0], stats[i, 1], stats[i, 2], stats[i, 3])
                            for i in range(1, count) if stats[i, 4] >= 8)
        for cx, cy, cw, ch in components:
            for group in groups:
                if cx <= group[2]:  # overlapping the current group horizontally
                    group[0], group[1] = min(group[0], cx), min(group[1], cy)
                    group[2], group[3] = max(group[2], cx + cw), max(group[3], cy + ch)
                    break
            else:
                groups.append([cx, cy, cx + cw, cy + ch])
    except ImportError:
        groups = []
    if len(groups) == len(text):
        return [(x0 + g[0], y0 + g[1], g[2] - g[0], g[3] - g[1]) for g in groups]
    rows, cols = np.where(mask > 0)
    if len(cols) < 4: return []
    x_min, x_max, y_min, y_max = cols.min(), cols.max() + 1, rows.min(), rows.max() + 1
    edges = np.linspace(x_min, x_max, len(text) + 1)
    return [(x0 + edges[i], y0 + y_min, edges[i + 1] - edges[i], y_max - y_min) for i in range(len(text))]

def borrow_boxes(metrics, page_text):
    """Build ``(char, x, y, w, h)`` entries for build_glyph_library."""
    if not page_text: return [], "no --page-text, every glyph is synthesized"
    alpha = _alpha_array(metrics)
    roi = tuple(metrics.get("box") or ())
    if alpha is None or len(roi) != 4: return [], "measure() returned no alpha map; every glyph is synthesized"
    boxes = segment_boxes(alpha, roi, page_text)
    if len(boxes) != len(page_text):
        return [], "page glyphs could not be located; every glyph is synthesized"
    return [(char, x, y, w, h) for char, (x, y, w, h) in zip(page_text, boxes)], \
           "borrowed from the measured page glyphs"


def baseline_from_digits(boxes):
    """Fit the baseline through non-CJK glyph bottoms.

    CJK glyphs overshoot the baseline by a few px; including 月/日 in a short
    line biases the slope upward and drops the whole replacement a couple of
    px.  Digits/latin share the true ink bottom, so the typed PageText gives a
    robust anchor when it contains at least two of them.
    """
    points = [(x + w / 2.0, y + h) for ch, x, y, w, h in boxes if ord(ch) < 0x2E80]
    if len(points) < 2: return None
    xs = np.array([p[0] for p in points], np.float64)
    ys = np.array([p[1] for p in points], np.float64)
    if float(xs.std()) < 6.0: return None
    slope, intercept = np.polyfit(xs, ys, 1)
    x_ref = float(xs.min())
    return float(intercept + slope * x_ref), float(slope), x_ref


# --------------------------------------------------------------------------- #
# Font identification (the engine owns the template matching; this only
# packages the result and keeps --font-id optional).
# --------------------------------------------------------------------------- #
def identify_font(engine, metrics, library, font_path=None):
    finder = getattr(engine, "identify_font", None)
    if not callable(finder): return None
    try:
        path = finder(metrics, library, font_path=font_path)
    except (TypeError, ValueError, RuntimeError):
        return None
    if not path: return None
    return {"path": str(path), "family": Path(path).stem,
            "score": round(float(metrics.get("font_ncc") or 0.0), 4),
            "size": metrics.get("font_size"), "sigma": metrics.get("font_sigma"),
            "weight_px": metrics.get("font_weight_px"),
            "advance_rmse": metrics.get("font_advance_rmse")}

def default_font_path():
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
        candidate = font_dir / name
        if candidate.is_file(): return str(candidate)
    return None

def preset_font(metrics, height_source, box, font_path):
    """Without page glyphs the engine cannot identify a font, so seed it here."""
    path = font_path or default_font_path()
    if not path: return None
    heights = [record["box"][3] for record in height_source.get("glyphs") or [] if record.get("box")]
    size = float(np.median(heights)) / 0.78 if heights else max(8.0, float(box[3]) * 0.78)
    metrics["font_path"] = path
    metrics["font_size"] = max(8.0, size)
    metrics["font_sigma"] = float(metrics.get("sigma") or 1.0)
    metrics["font_weight_px"] = float(metrics.get("weight") or 0.0)
    return path


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_engine(image, box, text, engine, page_text, font_path, sigma, weight, want_font_id):
    image = image.convert("RGB")
    x, y, width, height = box
    # 1. Measure the selected line to locate its glyphs (character boxes).
    tight = engine.measure(image, (x, y, x + width, y + height))
    edition = tuple(int(v) for v in tight["box"])
    boxes, source_note = borrow_boxes(tight, page_text)
    digit_baseline = baseline_from_digits(boxes)
    # 2. Re-measure with a margin: render() keeps borrowed mattes (which carry
    #    PAD pixels of context) inside the measured box, and the first glyph can
    #    sit a few px left of its ink origin.  The margin gives that slack; the
    #    background is still erased only inside the selected box below.
    margin = 24
    expanded = (max(0, edition[0] - margin), max(0, edition[1] - margin),
                min(image.width, edition[2] + margin), min(image.height, edition[3] + margin))
    metrics = engine.measure(image, expanded) if expanded != edition else tight
    if digit_baseline:
        # digits anchor the baseline; CJK bottoms overshoot it
        metrics["baseline_a"], metrics["baseline_b"], metrics["baseline_x0"] = digit_baseline
    elif expanded != edition:
        # keep the baseline fitted on the selected line; the margin may include
        # a sliver of the neighbouring glyph, which would bias the fit.
        for key in ("baseline_a", "baseline_b", "baseline_x0"):
            if tight.get(key) is not None: metrics[key] = tight[key]
    x0, y0, x1, y1 = (int(v) for v in metrics["box"])
    library = engine.build_glyph_library(image, metrics, boxes) if boxes else {}
    if not isinstance(library, dict): library = dict(library)
    borrowed = [char for char in dict.fromkeys(text) if library.get(char)]
    synthesized = [char for char in dict.fromkeys(text) if not library.get(char)]
    if not library and synthesized and not preset_font(metrics, tight, box, font_path):
        print("render_candidates: no CJK font found to synthesise with; pass --font", file=sys.stderr)
    font_id = None
    if font_path or synthesized or want_font_id or sigma is not None or weight is not None:
        font_id = identify_font(engine, metrics, library, font_path)
        if font_id and not font_path: font_path = font_id["path"]
    if sigma is not None:
        metrics["sigma"] = metrics["font_sigma"] = float(sigma)
        metrics["sigma_source"] = "override"
    if weight is not None:
        metrics["weight"] = metrics["font_weight_px"] = float(weight)

    patch, ink_rgb = engine.render(text, metrics, library, font_path=font_path, pen_x=float(edition[0]))
    patch = np.asarray(patch, np.float32)
    if patch.ndim > 2: patch = patch[..., 0]

    # The engine composites glyph coverage only.  Old ink is erased first by
    # replacing the selected box with the fitted background plus borrowed
    # residual noise (bg + noise_field); the new glyphs are laid over that.
    base = np.asarray(image).copy()
    clean = np.asarray(metrics["bg"], np.float32) + np.asarray(metrics["noise_field"], np.float32)
    ex0, ey0, ex1, ey1 = edition
    base[ey0:ey1, ex0:ex1] = np.clip(
        np.rint(clean[ey0 - y0:ey1 - y0, ex0 - x0:ex1 - x0]), 0, 255).astype(np.uint8)
    height_px, width_px = image.height, image.width
    mask = np.zeros((height_px, width_px), np.uint8)
    mask[ey0:ey1, ex0:ex1] = 255
    rows, cols = np.where(patch > 0.02)
    if len(rows):
        ay0, ay1 = max(0, y0 + int(rows.min())), min(height_px, y0 + int(rows.max()) + 1)
        ax0, ax1 = max(0, x0 + int(cols.min())), min(width_px, x0 + int(cols.max()) + 1)
        mask[ay0:ay1, ax0:ax1] = 255
    ink_noise = None
    if hasattr(engine, "noise_field"):
        try: ink_noise = engine.noise_field(metrics, patch.shape[:2])
        except Exception: ink_noise = None
    composed = engine.compose(base, patch, ink_rgb, float(x0), float(y0), mask=mask, ink_noise=ink_noise)
    result = _result_image(composed)
    if result.size != image.size: raise ValueError("compose() returned an image with changed dimensions")
    before, after = np.asarray(image), np.asarray(result)
    outside = mask == 0
    if np.abs(before.astype(np.int16) - after.astype(np.int16)).max(axis=2)[outside].max(initial=0) > 0:
        raise ValueError("compose() changed pixels outside the declared mask")

    info = {
        "mode": "glyph_engine",
        "glyph_source": source_note,
        "glyphs_borrowed": borrowed,
        "glyphs_synthesized": synthesized,
        "sigma": round(float(metrics.get("sigma_psf", metrics.get("sigma", 0.0)) or 0.0), 3),
        "render_sigma": round(float(metrics.get("font_sigma", metrics.get("sigma", 0.0)) or 0.0), 3),
        "stroke_px": round(float(metrics.get("stroke_px", 0.0) or 0.0), 3),
        "ink_rgb": _ink_list(metrics.get("ink_rgb", ink_rgb)),
        "baseline": {"a": round(float(metrics.get("baseline_a", 0.0)), 3),
                     "b": round(float(metrics.get("baseline_b", 0.0)), 5),
                     "x0": round(float(metrics.get("baseline_x0", x0)), 3)},
        "roi": [x0, y0, x1, y1],
        "font": font_path or metrics.get("font_path"),
        "page_text": page_text,
        "measurement": {
            "glyph_components": len(metrics.get("glyphs") or []),
            "noise_std": [round(float(v), 3) for v in np.asarray(metrics.get("noise_std", [0, 0, 0])).ravel()[:3]],
            "font_size": metrics.get("font_size"),
            "font_weight_px": metrics.get("font_weight_px"),
            "font_ncc": metrics.get("font_ncc"),
        },
    }
    if font_id: info["font_id"] = font_id
    return result, Image.fromarray(mask, "L"), info

def save_candidate(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in {".jpg", ".jpeg", ".jpe", ".jfif"}:
        image.save(path, "JPEG", quality=95)
    else:
        image.save(path)

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--bbox", required=True, type=parse_box)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mask", type=Path)
    parser.add_argument("--font")
    parser.add_argument("--blur", type=float, default=0.25, help="legacy path only: gaussian blur radius")
    parser.add_argument("--page-text", help="original text on the page line; its glyphs are borrowed when available")
    parser.add_argument("--font-id", action="store_true", help="identify the page typeface by template matching")
    parser.add_argument("--sigma", type=float, help="override the measured/render blur sigma")
    parser.add_argument("--weight", type=float, help="override the glyph stroke-weight calibration (px)")
    parser.add_argument("--legacy", action="store_true", help="use the old inpaint block repaint instead of glyph_engine")
    args = parser.parse_args()
    if not args.input.is_file(): parser.error(f"input is not a file: {args.input}")
    if args.font and not Path(args.font).is_file(): parser.error(f"font is not a file: {args.font}")
    if args.text.strip() == "": parser.error("text must not be empty")
    image = Image.open(args.input)
    if args.legacy:
        result, mask = render_legacy(image, args.bbox, args.text, args.font, args.blur)
        info = {"mode": "legacy_inpaint", "glyph_source": "none",
                "glyphs_borrowed": [], "glyphs_synthesized": list(dict.fromkeys(args.text)),
                "sigma": None, "stroke_px": None, "ink_rgb": None, "font": args.font}
    else:
        engine = load_engine()
        try:
            result, mask, info = render_engine(image, args.bbox, args.text, engine,
                                               args.page_text, args.font, args.sigma, args.weight, args.font_id)
        except (ValueError, RuntimeError) as exc:
            print(f"render_candidates: glyph_engine failed: {exc}\n"
                  "  hint: pass --page-text for the original line, or use --legacy for the old inpaint path",
                  file=sys.stderr)
            return 1
    save_candidate(result, args.output)
    if args.mask:
        args.mask.parent.mkdir(parents=True, exist_ok=True); mask.save(args.mask)
    payload = {"status": "candidate", "output": str(args.output.resolve()),
               "mask": str(args.mask.resolve()) if args.mask else None,
               "approved": False, "bbox": list(args.bbox), "text": args.text, **info}
    print(json.dumps(payload, ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
