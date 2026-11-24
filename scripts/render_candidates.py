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
import argparse, json, sys, unicodedata
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
    font_path = font_path or default_font_path()
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

def _char_weight(ch):
    """Expected advance of ``ch`` relative to a full-width glyph (1.0)."""
    code = ord(ch)
    if code < 0x80:
        return 0.55 if ch.isalnum() else 0.3          # ASCII digits/latin vs. half-width punctuation
    if code >= 0x2E80 or unicodedata.east_asian_width(ch) in ("W", "F"):
        return 1.0                                     # CJK and full-width symbols
    return 0.55 if ch.isalpha() else 1.0

def _union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]

def _merge_by_width(groups, chars, line_h):
    """Merge neighbouring groups into one box per character (left/right halves of a character).

    Picks the contiguous partition of the groups into len(chars) segments whose widths best
    match the expected width of each character (_char_weight * line height).  Digits that merely
    sit close together are therefore not fused, while wide characters whose strokes split into
    several pieces are.  Returns the groups unchanged when there are not more groups than
    characters, or when no partition fits (worst width error above 60 percent).
    """
    n, k = len(groups), len(chars)
    if k == 0 or n <= k: return groups
    def err(i, j, c):
        expected = _char_weight(c) * line_h
        return abs(groups[j - 1][2] - groups[i][0] - expected) / expected
    inf = float('inf')
    best = [[inf] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    best[0][0] = 0.0
    for c in range(1, k + 1):
        for j in range(c, n - (k - c) + 1):
            for i in range(c - 1, j):
                if best[c - 1][i] == inf: continue
                cost = best[c - 1][i] + err(i, j, chars[c - 1]) ** 2
                if cost < best[c][j]: best[c][j], back[c][j] = cost, i
    if best[k][n] == inf: return groups
    cuts, j = [], n
    for c in range(k, 0, -1):
        i = back[c][j]
        cuts.append((i, j))
        j = i
    cuts.reverse()
    if max(err(i, j, ch) for (i, j), ch in zip(cuts, chars)) > 0.6: return groups
    merged = []
    for i, j in cuts:
        g = groups[i]
        for h in groups[i + 1:j]: g = _union(g, h)
        merged.append(list(g))
    return merged

def _split_wide(groups, mask, count, line_h):
    """Split stuck-together glyphs (数字和汉字粘连, 如 3日) at the column-projection valley.

    Only runs while there are fewer groups than characters and only touches
    groups wider than 1.3 line heights.
    """
    while len(groups) < count:
        wide = [i for i, g in enumerate(groups) if g[2] - g[0] > 1.3 * line_h]
        if not wide: return
        i = max(wide, key=lambda k: groups[k][2] - groups[k][0])
        g = groups[i]
        piece = max(2, int(round(0.25 * line_h)))
        lo, hi = g[0] + piece, g[2] - piece
        if hi <= lo: return
        proj = mask[g[1]:g[3], g[0]:g[2]].sum(axis=0).astype(np.float64)
        smooth = np.convolve(proj, np.ones(3) / 3.0, mode="same")
        window = smooth[lo - g[0]:hi - g[0]]
        ties = np.flatnonzero(window <= window.min() + 1e-9)
        cut = lo + int(ties[len(ties) // 2])
        pieces = []
        for a, b in ((g[0], cut), (cut, g[2])):
            sub = mask[g[1]:g[3], a:b]
            ys, xs = np.nonzero(sub)
            if len(xs) == 0: continue
            pieces.append([a + int(xs.min()), g[1] + int(ys.min()), a + int(xs.max()) + 1, g[1] + int(ys.max()) + 1])
        if len(pieces) < 2: return
        groups[i:i + 1] = pieces

def segment_line(alpha, roi, text, engine=None):
    """Split the measured glyph mask into one absolute box per non-blank character.

    ``measure`` returns connected components without character labels, so the
    caller pairs them with the original line text.  Whitespace never takes part
    in the pairing (there is no ink for it).  Components are grouped by
    horizontal overlap (日/月 consist of several components), halves of
    left/right characters are merged and stuck glyphs are split at the column
    valley.  If the count still disagrees, the ink span is divided in proportion
    to the expected character widths.

    Returns ``(boxes, mode)``; ``boxes`` are ``(x, y, width, height)`` in
    absolute pixels, one per non-whitespace character of ``text`` and in the
    same order; ``mode`` is ``"components"``, ``"proportional_fallback"`` or
    ``"none"`` (no ink / nothing to pair, ``boxes`` empty).
    """
    x0, y0 = roi[0], roi[1]
    chars = [c for c in text if not c.isspace()]
    count = len(chars)
    mask = np.asarray(alpha) > 0.5
    if count == 0 or not mask.any(): return [], "none"
    engine = engine or load_engine()
    n_comp, _labels, stats = engine._components(mask)
    comps = sorted((int(s[0]), int(s[1]), int(s[2]), int(s[3])) for s in stats[1:n_comp] if int(s[4]) >= 8)
    groups = []
    for cx, cy, cw, ch in comps:
        if groups and cx <= groups[-1][2]:   # overlapping/touching the previous group horizontally
            groups[-1] = _union(groups[-1], [cx, cy, cx + cw, cy + ch])
        else:
            groups.append([cx, cy, cx + cw, cy + ch])
    if groups:
        line_h = max(g[3] - g[1] for g in groups)
        groups = _merge_by_width(groups, chars, line_h)
        _split_wide(groups, mask, count, line_h)
    if groups and len(groups) == count:
        return [(x0 + g[0], y0 + g[1], g[2] - g[0], g[3] - g[1]) for g in groups], "components"
    rows, cols = np.where(mask)
    if len(cols) < 4: return [], "none"
    x_min, x_max, y_min, y_max = int(cols.min()), int(cols.max()) + 1, int(rows.min()), int(rows.max()) + 1
    weights = np.array([_char_weight(c) for c in chars], np.float64)
    edges = x_min + (x_max - x_min) * np.concatenate(([0.0], np.cumsum(weights))) / weights.sum()
    return [(x0 + float(edges[i]), y0 + y_min, float(edges[i + 1] - edges[i]), y_max - y_min)
            for i in range(count)], "proportional_fallback"

def segment_boxes(alpha, roi, text, engine=None):
    """Boxes only (see :func:`segment_line`); one per non-whitespace character."""
    return segment_line(alpha, roi, text, engine)[0]

def borrow_boxes(metrics, page_text, engine=None):
    """Build ``(char, x, y, w, h)`` entries for build_glyph_library.

    Returns ``(entries, note, segmentation)``; whitespace characters of
    ``page_text`` are skipped and the remaining ones are mapped back to their boxes.
    """
    if not page_text: return [], "no --page-text, every glyph is synthesized", "none"
    alpha = _alpha_array(metrics)
    roi = tuple(metrics.get("box") or ())
    if alpha is None or len(roi) != 4:
        return [], "measure() returned no alpha map; every glyph is synthesized", "none"
    chars = [c for c in page_text if not c.isspace()]
    boxes, mode = segment_line(alpha, roi, page_text, engine)
    if len(boxes) != len(chars) or not boxes:
        return [], "page glyphs could not be located; every glyph is synthesized", "none"
    note = "borrowed from the measured page glyphs"
    if mode == "proportional_fallback":
        note = "borrowed from width-proportional estimates (connected components did not match --page-text)"
    return [(char, x, y, w, h) for char, (x, y, w, h) in zip(chars, boxes)], note, mode

def line_left_pen(engine, tight, metrics, first_char, font_path):
    """Pen x that puts a synthesized first glyph at the original line's left ink edge."""
    lefts = [int(g["box"][0]) for g in tight.get("glyphs") or [] if g.get("box") and g.get("area", 8) >= 8]
    if not lefts: return None
    left = float(min(lefts))
    path = font_path or metrics.get("font_path")
    size = metrics.get("font_size")
    renderer = getattr(engine, "_render_glyph", None)
    if path and size and callable(renderer):
        # PIL's getbbox() starts at the pen origin, not at the ink (a "1" has ~8 px of side bearing),
        # so measure where the ink of the rendered glyph really starts relative to the pen.
        try:
            sigma = float(metrics.get("font_sigma", metrics.get("sigma", 1.0)) or 0.0)
            weight = float(metrics.get("font_weight_px", 0.0) or 0.0)
            glyph = renderer(first_char, path, float(size), sigma, weight)
            # same 0.5 coverage edge that measure() uses for the original line's ink box
            cols = np.where((np.asarray(glyph) > 0.5).any(axis=0))[0]
            if len(cols):
                canvas = int(np.ceil(float(size) * getattr(engine, "RENDER_EM", 3.2)))
                canvas += canvas % 2
                left -= float(cols.min()) - canvas / 2.0
        except Exception: pass
    return left


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
    """A CJK font from the engine's font folders (Windows, macOS, Linux), msyh/simsun/simhei first."""
    found = load_engine()._font_candidates()
    by_name = {Path(p).name.lower(): p for p in found}
    for name in ("msyh.ttc", "simsun.ttc", "simhei.ttf"):
        if name in by_name: return by_name[name]
    return found[0] if found else None

def preset_font(metrics, height_source, box, font_path):
    """Without page glyphs the engine cannot identify a font, so seed it here."""
    path = font_path or default_font_path()
    if not path: return None
    heights = [record["box"][3] for record in height_source.get("glyphs") or [] if record.get("box")]
    size = float(np.median(heights)) / 0.78 if heights else max(8.0, float(box[3]) * 0.78)
    metrics["font_path"] = path
    metrics["font_size"] = max(8.0, size)
    metrics["font_sigma"] = float(metrics.get("sigma") or 1.0)
    metrics["font_weight_px"] = float(metrics.get("font_weight_px") or 0.0)
    return path


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def remeasure_with_margin(engine, image, tight, edition, margin):
    """Measure the selected box plus ``margin`` px, or None when that window is unusable.

    The selected line's polarity is kept.  The window is rejected when the margin
    reaches paper of another tone (light text on a dark band): the background fitted
    inside the box then drifts, or a border row/column is almost all "ink", which no
    neighbouring text produces but a strip of differently toned paper does.
    """
    x0, y0, x1, y1 = edition
    box = (max(0, x0 - margin), max(0, y0 - margin), min(image.width, x1 + margin), min(image.height, y1 + margin))
    if box == edition: return None
    try: wide = engine.measure(image, box, polarity=tight["polarity"])
    except ValueError: return None
    bx, by = x0 - int(wide["box"][0]), y0 - int(wide["box"][1])
    inner = np.asarray(wide["lum_bg"])[by:by + y1 - y0, bx:bx + x1 - x0]
    if abs(float(np.median(inner)) - float(np.median(tight["lum_bg"]))) > float(getattr(engine, "ROUGH_THRESHOLD", 10.0)):
        return None
    ink = np.asarray(wide["alpha"]) > 0.5
    ring_rows = [r for r in range(ink.shape[0]) if r < by or r >= by + y1 - y0]
    ring_cols = [c for c in range(ink.shape[1]) if c < bx or c >= bx + x1 - x0]
    if any(ink[r].mean() > 0.8 for r in ring_rows) or any(ink[:, c].mean() > 0.8 for c in ring_cols):
        return None
    return wide

def render_engine(image, box, text, engine, page_text, font_path, sigma, weight, want_font_id, pen_x=None):
    image = image.convert("RGB")
    x, y, width, height = box
    # 1. Measure the selected line to locate its glyphs (character boxes).
    tight = engine.measure(image, (x, y, x + width, y + height))
    edition = tuple(int(v) for v in tight["box"])
    boxes, source_note, segmentation = borrow_boxes(tight, page_text, engine)
    # boxes cut by width proportion are not real glyph extents: do not fit a baseline on them
    digit_baseline = baseline_from_digits(boxes) if segmentation == "components" else None
    # 2. Re-measure with a margin: render() keeps borrowed mattes (which carry
    #    PAD pixels of context) inside the measured box, and the first glyph can
    #    sit a few px left of its ink origin.  The margin gives that slack; the
    #    background is still erased only inside the selected box below.
    metrics, expanded = tight, edition
    for margin in (24, 12, 6, 3):
        wide = remeasure_with_margin(engine, image, tight, edition, margin)
        if wide is not None:
            metrics, expanded = wide, tuple(int(v) for v in wide["box"])
            break
    if digit_baseline:
        # digits anchor the baseline; CJK bottoms overshoot it
        metrics["baseline_a"], metrics["baseline_b"], metrics["baseline_x0"] = digit_baseline
    elif expanded != edition:
        # keep the baseline fitted on the selected line; the margin may include
        # a sliver of the neighbouring glyph, which would bias the fit.
        for key in ("baseline_a", "baseline_b", "baseline_x0"):
            if tight.get(key) is not None: metrics[key] = tight[key]
    x0, y0, x1, y1 = (int(v) for v in metrics["box"])
    # which source glyphs had a blank after them: the engine decides per placed instance
    # whether its measured advance already covers a blank of the new text
    space_after = [i + 1 < len(page_text) and page_text[i + 1].isspace()
                   for i, ch in enumerate(page_text or "") if not ch.isspace()]
    library = engine.build_glyph_library(image, metrics, boxes, space_after=space_after) if boxes else {}
    if not isinstance(library, dict): library = dict(library)
    render_text = text.strip()
    if not render_text: raise ValueError("text must not be empty")
    borrowed = [char for char in dict.fromkeys(text) if library.get(char)]
    synthesized = [char for char in dict.fromkeys(text) if not char.isspace() and not library.get(char)]
    if page_text and segmentation == "proportional_fallback":
        print("render_candidates: warning: connected components do not match --page-text; "
              "fell back to width-proportional glyph splitting, borrowed glyphs may be misaligned", file=sys.stderr)
    if page_text and not borrowed:
        print(f"render_candidates: warning: no original glyph could be borrowed ({source_note}); "
              "every glyph is synthesized from the font", file=sys.stderr)
    if not library and synthesized and not preset_font(metrics, tight, box, font_path):
        print("render_candidates: no CJK font found to synthesise with; pass --font", file=sys.stderr)
    font_id = None
    if synthesized or want_font_id:
        font_id = identify_font(engine, metrics, library, font_path)
        if font_id and not font_path: font_path = font_id["path"]
    if sigma is not None:
        metrics["sigma"] = metrics["font_sigma"] = float(sigma)
        metrics["sigma_source"] = "override"
    if weight is not None:
        metrics["font_weight_px"] = float(weight)
    need_font = bool(synthesized or want_font_id or any(c.isspace() for c in render_text))
    if pen_x is None and not library.get(render_text[0]):
        # first glyph is synthesized: the engine would anchor it at the (margin-expanded)
        # measure; use the selected line's own left ink edge instead.
        pen_x = line_left_pen(engine, tight, metrics, render_text[0], font_path)

    patch, ink_rgb = engine.render(render_text, metrics, library, font_path=font_path if need_font else None,
                                   pen_x=None if pen_x is None else float(pen_x))
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
        # Grow the mask only right/down (longer text).  Left/top stay inside the selected box: a faint
        # matte tail one px left of it would rewrite the neighbouring glyph's blurred edge and show up
        # as a seam step in the QA gate.
        ay0, ay1 = max(ey0, y0 + int(rows.min())), min(height_px, y0 + int(rows.max()) + 1)
        ax0, ax1 = max(ex0, x0 + int(cols.min())), min(width_px, x0 + int(cols.max()) + 1)
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
        "segmentation": segmentation,
        "pen_x": None if pen_x is None else round(float(pen_x), 3),
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
            "polarity": metrics.get("polarity"),
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
    parser.add_argument("--pen-x", type=float, help="absolute x of the first new glyph's pen origin; default: align with the original first glyph")
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
                                               args.page_text, args.font, args.sigma, args.weight, args.font_id, args.pen_x)
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
