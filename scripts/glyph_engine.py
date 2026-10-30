#!/usr/bin/env python3
"""glyph_engine.py — measurement-driven glyph borrowing for seamless text edits.

The engine models a scanned/projected text line as

    I(x, y) = B(x, y) + A(x, y) * (I_ink - B(x, y))

where ``B`` is a smooth background surface, ``A`` is glyph coverage (blur
included) and ``I_ink`` is the fully covered ink colour.  Only four public
entry points are needed by callers:

    measure(image, box)                    -> dict
    build_glyph_library(image, metrics, boxes) -> {char: [glyph, ...]}
    render(text, metrics, library, font_path=None, pen_x=None) -> (alpha, ink)
    compose(image, patch_alpha, ink, left, top, mask=None) -> np.ndarray

Everything else (font identification, weight calibration, noise field,
baseline helpers) is an optional helper used by :func:`render` and the CLI.

Design notes (all numbers validated on the reference photograph):

* **Full ink colour is the 99th percentile of glyph-core pixels**, never the
  median.  The median is 5-8 levels darker on the reference image and makes a
  rewritten run visibly lower-contrast.
* **Borrowed mattes are clipped to their own connected component** dilated by
  ``CLIP_DILATE`` px.  A rectangular crop drags in the blur tails of the
  neighbouring glyphs; on the reference image that shows up as a phantom dot.
* **Missing glyphs are synthesised** at 4x supersampling, with the stroke
  weight calibrated against the measured stroke width and the blur matched
  against the source mattes, then peak-normalised.
* **Layout is baseline driven**: ``y(x) = baseline_a + baseline_b * (x - baseline_x0)``.
  A borrowed glyph keeps its vertical relation to the baseline automatically
  (``dy = baseline_b * dx``).  Borrowed glyphs additionally keep the advance
  measured in the source line (``advance="auto"``), so replaying a line from
  its own mattes is pixel-aligned; synthetic glyphs use font advances.

All work happens in 0..255 float RGB arrays.  ``cv2`` is used when available
for gaussian / dilation / connected components / distance transform /
template matching; otherwise numpy + Pillow provide equivalent (slower) paths,
so the module has no hard dependency beyond numpy and Pillow.

CLI
---
    py scripts/glyph_engine.py --image scan.jpg --box 2028,1882,2412,1978 \
        --chars "2026年9月18日" --rebuild --out rebuild.png

    py scripts/glyph_engine.py --image scan.jpg --box ... --chars "2026年9月18日" \
        --identify --json metrics.json

    py scripts/glyph_engine.py --help
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:  # optional fast path
    import cv2 as _cv2

    _HAVE_CV2 = True
except Exception:  # pragma: no cover - exercised only without opencv
    _cv2 = None
    _HAVE_CV2 = False

HAVE_CV2 = _HAVE_CV2

__all__ = [
    "measure",
    "build_glyph_library",
    "render",
    "compose",
    "identify_font",
    "baseline_y",
    "noise_field",
    "metric_summary",
    "HAVE_CV2",
]

# --------------------------------------------------------------------------
# constants validated on the reference image
# --------------------------------------------------------------------------
LUM_W = np.array([0.299, 0.587, 0.114], np.float32)

PAD = 9             # margin around a source glyph bbox when cutting a matte
CLIP_DILATE = 6     # component-clip dilation (reference implementation: 6 px)
ROUGH_THRESHOLD = 10.0
SUPERSAMPLE = 4
RENDER_EM = 3.2     # render canvas size in em (glyph + margins)
FONT_SIZE_RANGE = (38.0, 56.0)
FONT_SIGMA_RANGE = (0.6, 2.2)

_JPEG_PHASE = 8

DEFAULT_FONT_FILES = (
    "msyh.ttc", "msyhbd.ttc", "msyhl.ttc",
    "simhei.ttf",
    "NotoSansSC-VF.ttf", "NotoSansCJK-Regular.ttc", "NotoSansCJKsc-Regular.otf",
    "SourceHanSansSC-Regular.otf", "SourceHanSansSC-VF.otf",
    "Deng.ttf", "Dengb.ttf", "Dengl.ttf",
    "msjh.ttc", "msjhl.ttc",
    "simsun.ttc", "simsunb.ttf",
    "NotoSerifSC-VF.ttf",
)

_FONT_CACHE: dict[tuple[str, int], Any] = {}


# --------------------------------------------------------------------------
# tiny backend layer: cv2 when present, numpy/Pillow equivalents otherwise
# --------------------------------------------------------------------------
def _as_rgb_float(image: Any) -> np.ndarray:
    """Return an HxWx3 float32 RGB array in 0..255."""
    if isinstance(image, Image.Image):
        arr = np.asarray(image.convert("RGB"))
    else:
        arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"expected an RGB image, got shape {arr.shape}")
    return arr[:, :, :3].astype(np.float32)


def _lum(rgb: np.ndarray) -> np.ndarray:
    return (rgb[:, :, 0] * LUM_W[0] + rgb[:, :, 1] * LUM_W[1] + rgb[:, :, 2] * LUM_W[2]).astype(np.float32)


def _conv1d(a: np.ndarray, kernel: np.ndarray, axis: int) -> np.ndarray:
    rad = len(kernel) // 2
    pads = [(0, 0)] * a.ndim
    pads[axis] = (rad, rad)
    padded = np.pad(a, pads, mode="reflect")
    win = np.lib.stride_tricks.sliding_window_view(padded, len(kernel), axis=axis)
    return np.tensordot(win, kernel, axes=([-1], [0])).astype(np.float32)


def _gaussian(a: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur (float in/out) with a numpy fallback."""
    a = np.asarray(a, np.float32)
    if sigma is None or sigma <= 1e-6:
        return a
    if _HAVE_CV2:
        return _cv2.GaussianBlur(a, (0, 0), float(sigma))
    rad = max(1, int(math.ceil(3.0 * sigma)))
    x = np.arange(-rad, rad + 1, dtype=np.float32)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    k /= k.sum()
    return _conv1d(_conv1d(a, k, 1), k, 0)


def _bilinear(a: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Sample a with zero fill outside its bounds."""
    h, w = a.shape
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    fx = (xs - x0).astype(np.float32)
    fy = (ys - y0).astype(np.float32)

    def at(yy: np.ndarray, xx: np.ndarray) -> np.ndarray:
        valid = (yy >= 0) & (yy < h) & (xx >= 0) & (xx < w)
        return a[np.clip(yy, 0, h - 1), np.clip(xx, 0, w - 1)] * valid

    return (
        (1 - fy) * ((1 - fx) * at(y0, x0) + fx * at(y0, x0 + 1))
        + fy * ((1 - fx) * at(y0 + 1, x0) + fx * at(y0 + 1, x0 + 1))
    ).astype(np.float32)


def _shift(a: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Translate a by (dx, dy) px; the result is sampled at (x-dx, y-dy)."""
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return np.asarray(a, np.float32)
    h, w = a.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return _bilinear(np.asarray(a, np.float32), xx - dx, yy - dy)


def _dilate(a: np.ndarray, radius: int, shape: str = "ellipse") -> np.ndarray:
    a = np.asarray(a, np.float32)
    radius = int(radius)
    if radius <= 0:
        return a
    if _HAVE_CV2:
        kernel = _cv2.getStructuringElement(
            _cv2.MORPH_ELLIPSE if shape == "ellipse" else _cv2.MORPH_RECT,
            (2 * radius + 1, 2 * radius + 1),
        )
        return _cv2.dilate(a, kernel, iterations=1)
    out = a.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if shape == "ellipse" and dy * dy + dx * dx > radius * radius:
                continue
            out = np.maximum(out, _shift(a, dx, dy))
    return out


def _label_np(mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """8-connected labelling via run-length union-find (no scipy/cv2)."""
    m = np.asarray(mask).astype(bool)
    h, w = m.shape
    parent: list[int] = []
    boxes: list[list[int]] = []  # x0, y0, x1, y1, area

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb

    row_runs: list[list[tuple[int, int, int]]] = []
    prev: list[tuple[int, int, int]] = []
    for y in range(h):
        row = m[y]
        runs: list[tuple[int, int, int]] = []
        if row.any():
            padded = np.concatenate(([0], row.astype(np.int8), [0]))
            d = np.diff(padded)
            starts = np.flatnonzero(d == 1)
            ends = np.flatnonzero(d == -1)
            for s, e in zip(starts, ends):
                lab = len(parent)
                parent.append(lab)
                boxes.append([int(s), y, int(e), y + 1, int(e - s)])
                for ps, pe, plab in prev:
                    if ps <= e and s <= pe:  # 8-connectivity
                        union(lab, plab)
                runs.append((int(s), int(e), lab))
        prev = runs
        row_runs.append(runs)

    roots = {}
    for lab in range(len(parent)):
        roots[lab] = find(lab)
        if roots[lab] != lab:
            dst = boxes[roots[lab]]
            src = boxes[lab]
            dst[0] = min(dst[0], src[0])
            dst[1] = min(dst[1], src[1])
            dst[2] = max(dst[2], src[2])
            dst[3] = max(dst[3], src[3])
            dst[4] += src[4]
    remap: dict[int, int] = {}
    # index 0 is the background row, exactly like cv2.connectedComponentsWithStats
    stats: list[list[int]] = [[0, 0, w, h, int((~m).sum())]]
    for lab in range(len(parent)):
        root = roots[lab]
        if root not in remap:
            remap[root] = len(stats)
            b = boxes[root]
            stats.append([b[0], b[1], b[2] - b[0], b[3] - b[1], b[4]])
    labels = np.zeros((h, w), np.int32)
    for y, runs in enumerate(row_runs):
        for s, e, lab in runs:
            labels[y, s:e] = remap[roots[lab]]
    return len(stats), labels, np.asarray(stats, np.int32)


def _components(mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    m = (np.asarray(mask) > 0).astype(np.uint8)
    if _HAVE_CV2:
        n, labels, stats, _ = _cv2.connectedComponentsWithStats(m, connectivity=8)
        return int(n), labels.astype(np.int32), stats.astype(np.int32)
    return _label_np(m)


def _distance(mask: np.ndarray) -> np.ndarray:
    """Euclidean distance to the nearest zero pixel (chamfer fallback)."""
    m = (np.asarray(mask) > 0).astype(np.uint8)
    if not m.any():
        return np.zeros(m.shape, np.float32)
    # a zero border guarantees the transform is defined even when the bbox is
    # completely filled (cv2 otherwise returns FLT_MAX and medians overflow)
    padded = np.pad(m, 1, mode="constant", constant_values=0)
    if _HAVE_CV2:
        return _cv2.distanceTransform(padded, _cv2.DIST_L2, 5)[1:-1, 1:-1]
    h, w = padded.shape
    inf = 1e9
    d = np.where(padded > 0, inf, 0.0)
    a, b = 1.0, math.sqrt(2.0)
    for y in range(h):
        row = d[y]
        prev = d[y - 1] if y > 0 else None
        for x in range(w):
            if not padded[y, x]:
                continue
            v = inf
            if x > 0:
                v = min(v, row[x - 1] + a)
            if prev is not None:
                if x > 0:
                    v = min(v, prev[x - 1] + b)
                v = min(v, prev[x] + a)
                if x + 1 < w:
                    v = min(v, prev[x + 1] + b)
            row[x] = v
    for y in range(h - 1, -1, -1):
        row = d[y]
        nxt = d[y + 1] if y + 1 < h else None
        for x in range(w - 1, -1, -1):
            if not padded[y, x]:
                continue
            v = row[x]
            if x + 1 < w:
                v = min(v, row[x + 1] + a)
            if nxt is not None:
                if x + 1 < w:
                    v = min(v, nxt[x + 1] + b)
                v = min(v, nxt[x] + a)
                if x > 0:
                    v = min(v, nxt[x - 1] + b)
            row[x] = v
    return d[1:-1, 1:-1].astype(np.float32)


def _downsample(a: np.ndarray, factor: int = SUPERSAMPLE) -> np.ndarray:
    f = int(factor)
    if f <= 1:
        return np.asarray(a, np.float32)
    h = (a.shape[0] // f) * f
    w = (a.shape[1] // f) * f
    return a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3)).astype(np.float32)


def _ncc_max(image: np.ndarray, template: np.ndarray) -> float:
    """Maximum normalised cross correlation over integer translations."""
    img = np.asarray(image, np.float32)
    tpl = np.asarray(template, np.float32)
    if tpl.ndim != 2 or img.ndim != 2:
        return -1.0
    if tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
        return -1.0
    if float(tpl.std()) < 1e-6:
        return -1.0
    if _HAVE_CV2:
        res = _cv2.matchTemplate(img, tpl, _cv2.TM_CCOEFF_NORMED)
        val = float(np.nanmax(res)) if res.size else -1.0
        return val if np.isfinite(val) else -1.0
    win = np.lib.stride_tricks.sliding_window_view(img, tpl.shape)
    a = win - win.mean(axis=(-2, -1), keepdims=True)
    b = tpl - tpl.mean()
    num = (a * b).sum(axis=(-2, -1))
    den = np.sqrt((a * a).sum(axis=(-2, -1)) * float((b * b).sum()))
    with np.errstate(divide="ignore", invalid="ignore"):
        score = np.where(den > 1e-9, num / den, 0.0)
    return float(score.max())


def _poly_bg(rgb: np.ndarray, x0: int, y0: int, free: np.ndarray) -> np.ndarray:
    """Least-squares quadratic background surface, fitted on free pixels."""
    h, w = free.shape
    yy, xx = np.mgrid[0:h, 0:w]
    xs = ((xx + x0) - (x0 + w / 2.0)).astype(np.float64).ravel() / 100.0
    ys = ((yy + y0) - (y0 + h / 2.0)).astype(np.float64).ravel() / 100.0
    mask = free.ravel()
    if int(mask.sum()) < 8:
        mask = np.ones_like(mask)
    design = np.stack(
        [np.ones_like(xs), xs, ys, xs * xs, xs * ys, ys * ys], axis=1
    )
    out = np.empty(rgb.shape, np.float32)
    for c in range(3):
        v = rgb[:, :, c].ravel().astype(np.float64)
        coef, *_ = np.linalg.lstsq(design[mask], v[mask], rcond=None)
        out[:, :, c] = (design @ coef).reshape(h, w).astype(np.float32)
    return out


def _clip_box(box: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    if len(box) != 4:
        raise ValueError("box must be (x0, y0, x1, y1)")
    x0, y0, x1, y1 = (int(round(float(v))) for v in box)
    x0, x1 = max(0, min(x0, x1)), min(width, max(x0, x1))
    y0, y1 = max(0, min(y0, y1)), min(height, max(y0, y1))
    if x1 - x0 < 8 or y1 - y0 < 8:
        raise ValueError(f"box {tuple(box)} is too small or outside the image")
    return x0, y0, x1, y1


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------
def measure(image: np.ndarray, box: tuple[int, int, int, int]) -> dict:
    """Measure a text line inside ``box = (x0, y0, x1, y1)``.

    Returns a dict with the fixed keys

    ``bg``            quadratic background surface, ROI shaped, float32 (H,W,3)
    ``alpha``         glyph coverage 0..1, ROI shaped, float32 (H,W)
    ``baseline_a``    baseline y at ``baseline_x0``
    ``baseline_b``    baseline slope dy/dx
    ``ink_rgb``       full ink colour, glyph-core 99th percentile (3,)
    ``sigma``         blur sigma used for rendering (edge PSF until identified)
    ``stroke_px``     median stroke width over glyph components, px
    ``glyphs``        per-component measurements (bbox, ink, stroke, ...)

    plus support data used by :func:`build_glyph_library` and :func:`compose`
    (``box``, ``shape``, ``baseline_x0``, ``lum_bg``, ``noise_std``,
    ``noise_patch``, ``noise_field``, ``sigma_psf``) and, after font
    identification, the ``font_*`` keys.
    """
    rgb_full = _as_rgb_float(image)
    height, width = rgb_full.shape[:2]
    x0, y0, x1, y1 = _clip_box(box, width, height)
    rgb = rgb_full[y0:y1, x0:x1]

    lum = _lum(rgb)
    bg0 = _poly_bg(rgb, x0, y0, np.ones(lum.shape, bool))
    rough = (lum - _lum(bg0)) > ROUGH_THRESHOLD
    if int(rough.sum()) < 16:
        raise ValueError(
            f"no glyph ink found in box {(x0, y0, x1, y1)}; "
            "expand the box or check the threshold"
        )
    rough_d = _dilate(rough.astype(np.float32), 2, "square") > 0.5
    free = ~rough_d
    if int(free.sum()) < 40:
        free = ~rough
    if int(free.sum()) < 20:
        free = np.ones(lum.shape, bool)

    bg = _poly_bg(rgb, x0, y0, free)
    lum_bg = _lum(bg)
    ink_level = float(np.percentile(lum[rough], 96))
    bg_med = float(np.median(lum_bg))
    alpha = np.clip((lum - lum_bg) / max(ink_level - bg_med, 1e-3), 0.0, 1.0).astype(np.float32)

    glyphs = _measure_components(rgb, alpha, x0, y0)

    core_all = alpha > 0.85
    if int(core_all.sum()) < 16:
        core_all = alpha > 0.6
    ink_rgb, ink_lum = _ink_vector(rgb, core_all, lum)
    stroke_px = float(np.median([g["stroke_px"] for g in glyphs])) if glyphs else 0.0

    baseline_a, baseline_b, baseline_x0, cluster = _fit_baseline(glyphs, x0)

    sigma_psf = _estimate_sigma(alpha)

    residual = rgb - bg
    noise_std = np.array(
        [float(residual[:, :, c][free].std()) for c in range(3)], np.float32
    )
    noise_patch, noise_origin = _clean_patch(alpha, residual)
    noise_patch = _match_grain(noise_patch, residual, free)
    tiled = _tile_patch(noise_patch, noise_origin, (x0, y0), (y1 - y0, x1 - x0))
    # keep the real residual outside the glyphs (bit-exact background, no seam)
    # and grain-matched tiled noise under them; the feather sits below alpha
    # 0.025 so no old ink survives under a matte (that would double-count ink)
    blend = np.clip((alpha - 0.005) / 0.02, 0.0, 1.0)[:, :, None]
    nfield = (residual * (1.0 - blend) + tiled * blend).astype(np.float32)

    metrics = {
        "box": (x0, y0, x1, y1),
        "shape": (height, width),
        "bg": bg,
        "alpha": alpha,
        "lum_bg": lum_bg,
        "baseline_a": float(baseline_a),
        "baseline_b": float(baseline_b),
        "baseline_x0": float(baseline_x0),
        "baseline_cluster": int(cluster),
        "ink_rgb": ink_rgb,
        "ink_lum": ink_lum,
        "sigma": float(sigma_psf),
        "sigma_psf": float(sigma_psf),
        "sigma_source": "psf",
        "stroke_px": float(stroke_px),
        "glyphs": glyphs,
        "noise_std": noise_std,
        "noise_patch": noise_patch,
        "noise_origin": noise_origin,
        "noise_field": nfield,
    }
    return metrics


def _ink_vector(
    sub_rgb: np.ndarray, core: np.ndarray, lum_arr: np.ndarray
) -> tuple[np.ndarray, float]:
    """Full-coverage ink as a colour that actually occurs in the image.

    Taking a percentile *per channel* mixes the componentwise maxima of different
    pixels; on JPEG 4:2:0 material the blue channel has the widest tail, so that
    colour is bluer than anything in the picture (measured +15 on B here) and the
    composite comes out too white. Instead pick one high-luminance quantile and
    take the vector median there, so the level and the hue stay consistent.
    """
    lv = lum_arr[core]
    if lv.size == 0:
        return np.array([200.0, 210.0, 225.0], np.float32), 210.0
    sel = core & (lum_arr >= float(np.percentile(lv, 90)))
    if int(sel.sum()) < 4:
        sel = core
    vec = np.array(
        [float(np.median(sub_rgb[:, :, c][sel])) for c in range(3)], np.float32
    )
    level = float(0.299 * vec[0] + 0.587 * vec[1] + 0.114 * vec[2])
    return vec, level


def _measure_components(
    rgb: np.ndarray, alpha: np.ndarray, x0: int, y0: int
) -> list[dict]:
    n, labels, stats = _components(alpha > 0.5)
    lum = _lum(rgb)
    h, w = alpha.shape
    out: list[dict] = []
    for i in range(1, n):
        bx, by, bw, bh, area = (int(v) for v in stats[i])
        if area < 8:
            continue
        m = labels[by:by + bh, bx:bx + bw] == i
        dist = _distance(m.astype(np.uint8))
        vals = dist[m]
        sub_alpha = alpha[by:by + bh, bx:bx + bw]
        core = m & (sub_alpha > 0.85)
        if int(core.sum()) < 4:
            core = m & (sub_alpha > 0.7)
        if int(core.sum()) < 4:
            core = m
        sub_rgb = rgb[by:by + bh, bx:bx + bw]
        ink, ink_lum = _ink_vector(sub_rgb, core, lum[by:by + bh, bx:bx + bw])
        # perceived tone: median of the core. `ink_lum` is the full-coverage level
        # (top decile) and is driven by JPEG ringing, which a rebuilt glyph does not
        # reproduce -- comparing it would reward over-bright text.
        ink_tone = float(np.median(lum[by:by + bh, bx:bx + bw][core]))
        out.append(
            {
                "char": None,
                "box": (x0 + bx, y0 + by, bw, bh),
                "area": int(area),
                "ink": ink,
                "ink_lum": ink_lum,
                "ink_tone": ink_tone,
                "stroke_px": float(np.median(vals) * 2) if vals.size else 0.0,
                "stroke_mean_px": float(np.mean(vals) * 2) if vals.size else 0.0,
                "cx": float(x0 + bx + bw / 2.0),
                "bottom": int(y0 + by + bh),
                "border": bool(bx == 0 or by == 0 or bx + bw >= w or by + bh >= h),
            }
        )
    out.sort(key=lambda g: g["box"][0])
    return out


def _fit_baseline(
    glyphs: list[dict], roi_x0: int
) -> tuple[float, float, float, int]:
    """Robust baseline fit.

    Glyph bottoms are bimodal when a line mixes digits (sitting on the
    baseline) with CJK glyphs (which overshoot a few px below it).  A plain
    least-squares fit through every bottom is pulled down by 2-4 px, so the
    line is fitted with an iteratively trimmed least-squares on the dominant
    bottom cluster.
    """
    pts = [(g["cx"], float(g["bottom"])) for g in glyphs if g["area"] >= 12]
    if not pts:
        pts = [(g["cx"], float(g["bottom"])) for g in glyphs]
    if not pts:
        return 0.0, 0.0, float(roi_x0), 0
    xs = np.array([p[0] for p in pts], np.float64)
    ys = np.array([p[1] for p in pts], np.float64)
    if len(pts) >= 2 and float(xs.std()) > 1e-3:
        b, a = np.polyfit(xs, ys, 1)
    else:
        b, a = 0.0, float(np.median(ys))
    keep = np.ones(len(xs), bool)
    band = 3.0
    for _ in range(4):
        resid = ys - (a + b * xs)
        centre = float(np.median(resid[keep]))
        nxt = np.abs(resid - centre) <= band
        if int(nxt.sum()) < 2 or np.array_equal(nxt, keep):
            break
        keep = nxt
        if float(xs[keep].std()) > 1e-3:
            b, a = np.polyfit(xs[keep], ys[keep], 1)
        else:
            b, a = 0.0, float(np.median(ys[keep]))
    x_ref = float(min(g["box"][0] for g in glyphs)) if glyphs else float(roi_x0)
    a_ref = float(a + b * x_ref)
    return a_ref, float(b), x_ref, int(keep.sum())


def _estimate_sigma(alpha: np.ndarray) -> float:
    """Estimate the PSF sigma from the steepest 0.5 crossings of the alpha map.

    For an error-function edge the maximum slope is 1 / (sigma * sqrt(2*pi)),
    so ``sigma = 0.3989 / max|dA/dx|`` at each crossing.
    """
    a = np.asarray(alpha, np.float32)
    gx = np.abs(np.diff(a, axis=1))
    gy = np.abs(np.diff(a, axis=0))
    vals: list[float] = []
    lo = a[:, :-1]
    hi = a[:, 1:]
    cross = ((lo <= 0.5) & (hi > 0.5)) | ((hi <= 0.5) & (lo > 0.5))
    ys, xs = np.nonzero(cross)
    for y, x in zip(ys, xs):
        vals.append(float(gx[y, max(0, x - 3):x + 4].max()))
    lo = a[:-1, :]
    hi = a[1:, :]
    cross = ((lo <= 0.5) & (hi > 0.5)) | ((hi <= 0.5) & (lo > 0.5))
    ys, xs = np.nonzero(cross)
    for x, y in zip(xs, ys):
        vals.append(float(gy[max(0, y - 3):y + 4, x].max()))
    vals = [v for v in vals if v > 1e-4]
    if len(vals) < 8:
        return 1.0
    sigma = float(0.3989423 / np.median(vals))
    return float(np.clip(sigma, 0.3, 4.0))


def _clean_patch(alpha: np.ndarray, residual: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """Pick the largest ink-free residual patch; sample free pixels otherwise."""
    h, w = alpha.shape
    integral = np.zeros((h + 1, w + 1), np.float64)
    integral[1:, 1:] = alpha.cumsum(0).cumsum(1)
    for ph, pw in ((60, 50), (48, 40), (32, 32), (24, 24), (16, 16)):
        if ph > h or pw > w:
            continue
        sums = (
            integral[ph:, pw:]
            - integral[:-ph, pw:]
            - integral[ph:, :-pw]
            + integral[:-ph, :-pw]
        )
        if float(sums.min()) <= 0.05:
            iy, ix = np.unravel_index(int(np.argmin(sums)), sums.shape)
            return residual[iy:iy + ph, ix:ix + pw].copy(), (int(ix), int(iy))
    free = alpha < 0.05
    pool = residual[free]
    if pool.size < 300:
        pool = residual.reshape(-1, 3)
    rng = np.random.default_rng(12345)
    ph, pw = min(32, h), min(32, w)
    idx = rng.integers(0, len(pool), size=ph * pw)
    patch = pool[idx].reshape(ph, pw, 3).astype(np.float32)
    return patch, (0, 0)


def _match_grain(
    patch: np.ndarray, residual: np.ndarray, free: np.ndarray, cap: tuple[float, float] = (0.6, 3.0)
) -> np.ndarray:
    """High-pass a borrowed noise patch and scale it to the page's grain level."""
    hp = patch - _gaussian(patch, 3.0)
    target = residual - _gaussian(residual, 3.0)
    out = np.zeros_like(patch, np.float32)
    for c in range(3):
        s_patch = float(hp[:, :, c].std())
        s_target = float(target[:, :, c][free].std())
        if s_patch < 1e-3 or s_target < 1e-3:
            out[:, :, c] = hp[:, :, c]
            continue
        out[:, :, c] = hp[:, :, c] * float(np.clip(s_target / s_patch, cap[0], cap[1]))
    return out


def _tile_patch(
    patch: np.ndarray,
    origin_abs: tuple[int, int],
    roi_xy: tuple[int, int],
    shape: tuple[int, int],
) -> np.ndarray:
    """Tile a residual patch over an ROI, keeping the JPEG 8px block phase."""
    ph, pw = patch.shape[:2]
    h, w = shape
    sx = (int(origin_abs[0]) - int(roi_xy[0])) % _JPEG_PHASE
    sy = (int(origin_abs[1]) - int(roi_xy[1])) % _JPEG_PHASE
    if sx or sy:
        patch = np.roll(patch, (-sy, -sx), axis=(0, 1))
    reps = (int(math.ceil(h / ph)) + 1, int(math.ceil(w / pw)) + 1, 1)
    tiled = np.tile(patch, reps)
    return tiled[:h, :w].copy()


def noise_field(metrics: dict, shape: tuple[int, int] | None = None) -> np.ndarray:
    """Return the tiled residual noise field, optionally resized to ``shape``."""
    field = metrics["noise_field"]
    if shape is None:
        return field
    h, w = shape
    fh, fw = field.shape[:2]
    reps = (int(math.ceil(h / fh)) + 1, int(math.ceil(w / fw)) + 1, 1)
    tiled = np.tile(field, reps)
    return tiled[:h, :w].copy()


# --------------------------------------------------------------------------
# glyph library
# --------------------------------------------------------------------------
def build_glyph_library(
    image: np.ndarray,
    metrics: dict,
    boxes: list[tuple],
    pad: int = PAD,
    clip_dilate: int = CLIP_DILATE,
) -> dict[str, list[dict]]:
    """Cut glyph mattes out of the measured image.

    ``boxes`` is a list of ``(char, x, y, w, h)`` ink boxes (absolute pixels).
    Each matte is normalised by its own full-ink level, clipped to its own
    connected component dilated by ``clip_dilate`` px, and stored with its
    absolute origin and measured source advance::

        {char: [{'alpha': 2d float32, 'ink': (3,), 'origin': (x, y),
                 'box': (x, y, w, h), 'advance': float | None}, ...]}
    """
    rgb_full = _as_rgb_float(image)
    x0, y0, x1, y1 = metrics["box"]
    rgb = rgb_full[y0:y1, x0:x1]
    alpha = np.asarray(metrics["alpha"], np.float32)
    bg = np.asarray(metrics["bg"], np.float32)
    lum = _lum(rgb)
    lum_bg = _lum(bg)
    n, labels, stats = _components(alpha > 0.5)

    entries: list[dict] = []
    for item in boxes:
        if len(item) != 5:
            raise ValueError(f"glyph box must be (char, x, y, w, h), got {item!r}")
        ch, bx, by, bw, bh = item
        bx, by, bw, bh = int(round(bx)), int(round(by)), int(round(bw)), int(round(bh))
        if bw <= 0 or bh <= 0:
            raise ValueError(f"glyph box for {ch!r} has non-positive size: {item!r}")

        wx0 = max(x0, bx - pad)
        wy0 = max(y0, by - pad)
        wx1 = min(x1, bx + bw + pad)
        wy1 = min(y1, by + bh + pad)
        if wx1 <= wx0 or wy1 <= wy0:
            continue
        sl = np.s_[wy0 - y0:wy1 - y0, wx0 - x0:wx1 - x0]
        sub = rgb[sl]
        sub_alpha = alpha[sl]
        sub_lb = lum_bg[sl]

        # own connected component: the one under the box centre, else the one
        # covering most of the box (thin glyphs can have an empty centre)
        cx = int(round(bx + bw / 2.0 - x0))
        cy = int(round(by + bh / 2.0 - y0))
        li = int(labels[cy, cx]) if (0 <= cy < alpha.shape[0] and 0 <= cx < alpha.shape[1]) else 0
        if li <= 0:
            box_labels = labels[sl]
            vals, counts = np.unique(box_labels[box_labels > 0], return_counts=True)
            if vals.size:
                li = int(vals[int(np.argmax(counts))])

        own = labels[sl] == li if li > 0 else np.zeros(sub_alpha.shape, bool)
        core = own & (sub_alpha > 0.85)
        if int(core.sum()) < 6:
            core = own & (sub_alpha > 0.6)
        if int(core.sum()) < 6:
            core = sub_alpha > 0.85
        if int(core.sum()) < 4:
            core = sub_alpha > 0.5
        if int(core.sum()) < 4:
            continue
        ink, ink_lum = _ink_vector(sub, core, lum[sl])
        den = max(ink_lum - float(np.median(sub_lb)), 1e-3)
        a = np.clip((lum[sl] - sub_lb) / den, 0.0, 1.0).astype(np.float32)
        if li > 0:
            own_d = _dilate(own.astype(np.float32), clip_dilate, "square")
        else:
            own_d = _dilate((sub_alpha > 0.5).astype(np.float32), clip_dilate, "square")
        a *= own_d

        stroke = 0.0
        if li > 0:
            m = own
            dist = _distance(m.astype(np.uint8))
            vals_dt = dist[m]
            stroke = float(np.median(vals_dt) * 2) if vals_dt.size else 0.0

        entries.append(
            {
                "char": ch,
                "alpha": a,
                "ink": ink,
                "origin": (wx0, wy0),
                "box": (bx, by, bw, bh),
                "ink_left": float(bx),
                "ink_top": float(by),
                "bottom": float(by + bh),
                "cx": float(bx + bw / 2.0),
                "stroke_px": stroke,
                "advance": None,
            }
        )

    # measured advances: the source line's own tracking, ink-left to ink-left
    entries.sort(key=lambda g: g["ink_left"])
    heights = [g["box"][3] for g in entries] or [0]
    max_gap = 3.0 * max(1.0, float(np.median(heights)))
    for cur, nxt in zip(entries, entries[1:]):
        gap = nxt["ink_left"] - cur["ink_left"]
        if 0 < gap <= max_gap:
            cur["advance"] = float(gap)

    library: dict[str, list[dict]] = {}
    for g in entries:
        library.setdefault(g["char"], []).append(g)
    metrics["glyph_count"] = len(entries)
    metrics["source_text"] = "".join(g["char"] for g in entries)
    return library


# --------------------------------------------------------------------------
# fonts and rendering
# --------------------------------------------------------------------------
def _font(path: str, size: float):
    key = (str(path), int(round(float(size) * 16)))
    if key not in _FONT_CACHE:
        if len(_FONT_CACHE) > 512:
            _FONT_CACHE.clear()
        _FONT_CACHE[key] = ImageFont.truetype(str(path), float(size))
    return _FONT_CACHE[key]


def _font_candidates(font_path: str | None = None) -> list[str]:
    if font_path:
        return [str(font_path)]
    windir = os.environ.get("WINDIR", r"C:\Windows")
    font_dir = Path(windir) / "Fonts"
    found = [str(font_dir / name) for name in DEFAULT_FONT_FILES if (font_dir / name).is_file()]
    return found


def _render_glyph(
    ch: str,
    font_path: str,
    size: float,
    sigma: float,
    weight: float,
    scale: int = SUPERSAMPLE,
    canvas_em: float = RENDER_EM,
) -> np.ndarray:
    """Render one glyph at 4x, dilate (weight), blur (sigma), downsample."""
    canvas = int(math.ceil(size * canvas_em))
    canvas += canvas % 2
    big = _font(font_path, float(size) * scale)
    c = canvas * scale
    img = Image.new("L", (c, c), 0)
    ImageDraw.Draw(img).text((c / 2, c / 2), ch, font=big, fill=255, anchor="ls")
    a = np.asarray(img, np.float32) / 255.0
    if weight > 0:
        a = _dilate(a, int(round(weight * scale)), "ellipse")
    if sigma > 0:
        a = _gaussian(a, sigma * scale)
    a = _downsample(a, scale)
    peak = float(a.max())
    if peak > 1e-3:
        a = np.clip(a / peak, 0.0, 1.0)
    return a


def _tight(a: np.ndarray, thr: float = 0.02) -> np.ndarray | None:
    ys, xs = np.where(a > thr)
    if len(ys) < 5:
        return None
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1].copy()


def _median_stroke(a: np.ndarray) -> float:
    m = (np.asarray(a, np.float32) > 0.5)
    if not m.any():
        return 0.0
    dist = _distance(m.astype(np.uint8))
    vals = dist[m]
    return float(np.median(vals) * 2) if vals.size else 0.0


def _select_refs(library: dict[str, list[dict]], max_refs: int) -> list[tuple[str, np.ndarray]]:
    best: dict[str, dict] = {}
    for ch, glyphs in library.items():
        if not ch:
            continue
        area = max(g["box"][2] * g["box"][3] for g in glyphs)
        if ch not in best or area > best[ch]["area"]:
            best[ch] = {"area": area, "glyph": glyphs[0]}
    ranked = sorted(best.items(), key=lambda kv: -kv[1]["area"])
    return [(ch, info["glyph"]["alpha"]) for ch, info in ranked[:max_refs]]


def _font_score(
    font_path: str, size: float, sigma: float, refs: list[tuple[str, np.ndarray]], weight: float = 0.0
) -> float | None:
    scores = []
    for ch, ref in refs:
        cand = _tight(_render_glyph(ch, font_path, size, sigma, weight, canvas_em=1.8))
        if cand is None:
            return None
        score = _ncc_max(ref, cand)
        if score < -0.5:
            continue
        scores.append(score)
    if len(scores) < max(2, (len(refs) + 1) // 2):
        return None
    return float(np.mean(scores))


def _advance_fit(font_path: str, library: dict[str, list[dict]]) -> dict[float, float] | None:
    """RMSE between font-predicted and measured ink-left positions per size."""
    glyphs = sorted(
        (g for lst in library.values() for g in lst if g.get("advance")), key=lambda g: g["ink_left"]
    )
    if len(glyphs) < 3:
        return None
    lo, hi = FONT_SIZE_RANGE
    out: dict[float, float] = {}
    for s10 in range(int(lo * 10), int(hi * 10) + 1):
        size = s10 / 10.0
        font = _font(font_path, size)
        first = glyphs[0]
        pen = first["ink_left"] - float(font.getbbox(first["char"], anchor="ls")[0])
        errs = []
        for g in glyphs:
            bb = font.getbbox(g["char"], anchor="ls")
            errs.append(pen + float(bb[0]) - g["ink_left"])
            pen += float(font.getlength(g["char"]))
        out[size] = float(np.sqrt(np.mean(np.square(errs))))
    return out


def _calibrate_weight(
    font_path: str, size: float, sigma: float, target: float, chars: Sequence[str]
) -> float:
    if target <= 0.5:
        return 0.0
    best: tuple[float, float] | None = None
    for w10 in range(0, 21):
        weight = w10 / 10.0
        strokes = [
            _median_stroke(_render_glyph(ch, font_path, size, sigma, weight, canvas_em=1.8))
            for ch in chars
        ]
        stroke = float(np.median(strokes)) if strokes else 0.0
        delta = abs(stroke - target)
        key = (round(delta, 3), weight)
        if best is None or key < (round(best[0], 3), best[1]):
            best = (delta, weight)
    return float(best[1]) if best else 0.0


def identify_font(
    metrics: dict,
    library: dict[str, list[dict]],
    font_path: str | None = None,
    max_refs: int = 6,
) -> str | None:
    """Identify the typeface, size, sigma and stroke weight from the mattes.

    Scans the usual CJK sans fonts under ``%WINDIR%\\Fonts`` (by default),
    sizes 38..56 px and sigma 0.6..2.2 with NCC template matching, refines the
    best candidates, then cross-checks the size against the source line's own
    advances and calibrates the stroke weight.  Writes ``font_path``,
    ``font_size``, ``font_sigma``, ``font_weight_px``, ``font_ncc`` and
    related keys into ``metrics`` and returns the chosen font path.
    """
    refs = _select_refs(library, max_refs if _HAVE_CV2 else 3)
    if not refs:
        raise ValueError("cannot identify a font without labelled source glyphs")
    candidates = _font_candidates(font_path)
    if not candidates:
        raise ValueError("no CJK font found in the system Fonts directory")
    metrics["font_candidates"] = [Path(p).name for p in candidates]

    lo, hi = FONT_SIZE_RANGE
    coarse_sizes = np.arange(lo, hi + 0.01, 2.0)
    coarse_sigmas = (0.6, 1.0, 1.4, 1.8, 2.2)

    scored: list[tuple[float, str, float, float]] = []
    for path in candidates:
        for size in coarse_sizes:
            for sigma in coarse_sigmas:
                score = _font_score(path, float(size), float(sigma), refs)
                if score is not None:
                    scored.append((score, path, float(size), float(sigma)))
    if not scored:
        raise RuntimeError("font identification failed: no candidate could be rendered")
    scored.sort(key=lambda r: -r[0])

    # refine around the best two configurations
    refined: list[tuple[float, str, float, float]] = []
    seen = set()
    for base_score, path, size, sigma in scored[:2]:
        for ds in range(-3, 4):
            for dsig in (-0.4, -0.2, 0.0, 0.2, 0.4):
                s = size + ds
                g = sigma + dsig
                if not (lo <= s <= hi) or not (FONT_SIGMA_RANGE[0] <= g <= FONT_SIGMA_RANGE[1]):
                    continue
                key = (path, round(s, 2), round(g, 2))
                if key in seen:
                    continue
                seen.add(key)
                score = _font_score(path, s, g, refs)
                if score is not None:
                    refined.append((score, path, s, g))
    pool = refined or scored
    pool.sort(key=lambda r: -r[0])
    ncc_score, best_path, ncc_size, best_sigma = pool[0]

    # size: prefer the advance fit; cross-check with the NCC optimum
    adv = _advance_fit(best_path, library)
    size = ncc_size
    adv_rmse = None
    if adv:
        best_rmse = min(adv.values())
        adv_rmse = best_rmse
        if best_rmse <= 2.0:
            cands = [s for s, r in adv.items() if r <= best_rmse + 0.05]
            size = min(cands, key=lambda s: abs(s - ncc_size))

    # sigma at the chosen size
    sigma_best, sigma_score = best_sigma, -2.0
    for g10 in range(int(FONT_SIGMA_RANGE[0] * 10), int(FONT_SIGMA_RANGE[1] * 10) + 1):
        g = g10 / 10.0
        score = _font_score(best_path, size, g, refs)
        if score is not None and score > sigma_score:
            sigma_score, sigma_best = score, g

    weight = _calibrate_weight(
        best_path, size, sigma_best, float(metrics.get("stroke_px", 0.0)),
        [ch for ch, _ in refs[:4]],
    )

    metrics.update(
        {
            "font_path": best_path,
            "font_size": float(size),
            "font_sigma": float(sigma_best),
            "font_weight_px": float(weight),
            "font_ncc": float(sigma_score),
            "font_size_ncc": float(ncc_size),
            "font_advance_rmse": None if adv_rmse is None else float(adv_rmse),
            "font_id_method": "template+advance+stroke",
        }
    )
    metrics["sigma"] = float(sigma_best)
    metrics["sigma_source"] = "template"
    return best_path


def _ensure_font(
    metrics: dict, library: dict[str, list[dict]], font_path: str | None
) -> str | None:
    path = font_path or metrics.get("font_path")
    cached = metrics.get("font_path")
    if path and path == cached and metrics.get("font_size") and (
        metrics.get("font_sigma") is not None and metrics.get("font_weight_px") is not None
    ):
        return path
    if path:
        metrics.pop("font_size", None)
    try:
        return identify_font(metrics, library, font_path=path)
    except (ValueError, RuntimeError):
        return None


def baseline_y(x: float, metrics: dict) -> float:
    """Baseline y at absolute x for the measured line."""
    return float(metrics["baseline_a"]) + float(metrics["baseline_b"]) * (
        float(x) - float(metrics["baseline_x0"])
    )


def _default_pen(text: str, metrics: dict, library: dict[str, list[dict]], font) -> float:
    if not text:
        raise ValueError("text must not be empty")
    ch = text[0]
    glyphs = library.get(ch) or []
    if glyphs:
        base = float(glyphs[0]["ink_left"])
    else:
        base = float(metrics["baseline_x0"])
    if font is not None:
        base -= float(font.getbbox(ch, anchor="ls")[0])
    return base


def _vertical_offset(metrics: dict, library: dict[str, list[dict]], font) -> float:
    """Baseline correction learned by aligning font bottoms to the mattes.

    Keeps synthetic glyphs on the same optical baseline even when the measured
    baseline was fitted on a cluster that sits a few px off the font's own
    bottom metric (CJK overshoot differs from digits).
    """
    if font is None:
        return 0.0
    offs = []
    for ch, glyphs in library.items():
        if not ch:
            continue
        for g in glyphs:
            try:
                bb = font.getbbox(ch, anchor="ls")
            except Exception:
                continue
            observed = float(g["bottom"]) - baseline_y(float(g["ink_left"]), metrics)
            offs.append(observed - float(bb[3]))
    return float(np.median(offs)) if offs else 0.0


def _add_patch(dst: np.ndarray, src: np.ndarray, left: float, top: float) -> None:
    """Place src into dst at fractional (left, top), clipped to dst bounds."""
    h, w = src.shape
    ix, iy = int(math.floor(left)), int(math.floor(top))
    fx, fy = left - ix, top - iy
    if abs(fx) > 1e-6 or abs(fy) > 1e-6:
        src = _shift(src, fx, fy)
    dy0, dy1 = max(0, iy), min(dst.shape[0], iy + h)
    dx0, dx1 = max(0, ix), min(dst.shape[1], ix + w)
    if dy0 >= dy1 or dx0 >= dx1:
        return
    ys = slice(dy0 - iy, dy0 - iy + (dy1 - dy0))
    xs = slice(dx0 - ix, dx0 - ix + (dx1 - dx0))
    np.maximum(dst[dy0:dy1, dx0:dx1], src[ys, xs], out=dst[dy0:dy1, dx0:dx1])


def render(
    text: str,
    metrics: dict,
    library: dict[str, list[dict]],
    font_path: str | None = None,
    pen_x: float | None = None,
    advance: str = "auto",
    ink: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Typeset ``text`` on the measured baseline.

    Returns ``(patch_alpha, ink_rgb)``:

    * ``patch_alpha`` covers the measured box (its top-left pixel is image
      pixel ``metrics['box'][:2]``) and extends right/down if the new text is
      longer than the box.  Use ``compose(image, patch, ink, *metrics['box'][:2])``.
    * ``ink_rgb`` is the full-ink colour to composite with.  One colour has to
      serve the whole patch, so for borrowed glyphs it is the area-weighted
      mean of their measured core inks (synthetic glyphs vote with
      ``metrics['ink_rgb']``); pass ``ink=`` to override.

    Characters present in ``library`` borrow their source matte; the rest are
    synthesised from ``font_path`` (or the identified font) at 4x supersampling
    with the calibrated stroke weight and blur.  ``advance="auto"`` keeps the
    source line's own advance for borrowed glyphs (so a line replayed from its
    own mattes is pixel-aligned) and font advances for synthetic glyphs;
    ``advance="font"`` forces font metrics for everything.

    ``pen_x`` is the first glyph pen position in absolute pixels; by default
    the line starts where the measured source line starts.
    """
    if not text:
        raise ValueError("text must not be empty")
    x0, y0, x1, y1 = metrics["box"]
    img_h, img_w = metrics["shape"]
    needs_synth = any(ch not in library for ch in text)
    missing_advance = advance == "auto" and any(
        ch in library and not any(g.get("advance") for g in library[ch]) for ch in text[:-1]
    )
    font = None
    path = font_path or metrics.get("font_path")
    if path is None and (needs_synth or missing_advance):
        path = _ensure_font(metrics, library, None)
    elif path is not None and (
        metrics.get("font_size") is None or metrics.get("font_path") != path
    ):
        path = _ensure_font(metrics, library, path)
    if path is not None:
        font = _font(path, float(metrics["font_size"]))
    elif needs_synth:
        raise ValueError(
            "cannot synthesise missing glyphs without a usable font; "
            "pass font_path=... or make sure a CJK font is installed"
        )

    size = float(metrics.get("font_size", 0.0) or 0.0)
    sigma = float(metrics.get("font_sigma", metrics.get("sigma", 1.0)) or 0.0)
    weight = float(metrics.get("font_weight_px", 0.0) or 0.0)

    pen = float(pen_x) if pen_x is not None else _default_pen(text, metrics, library, font)
    v_off = _vertical_offset(metrics, library, font)
    metrics["v_offset"] = float(v_off)
    placements: list[tuple[np.ndarray, float, float, str]] = []
    ink_total = np.zeros(3, np.float64)
    ink_weight = 0.0
    for ch in text:
        bbox_left = 0.0
        adv_font = None
        if font is not None:
            bbox_left = float(font.getbbox(ch, anchor="ls")[0])
            adv_font = float(font.getlength(ch))
        glyphs = library.get(ch) or []
        if glyphs:
            dst_ink_left = pen + bbox_left
            # prefer the instance rendered nearest the destination: a line
            # replayed from its own mattes then stays pixel-aligned
            src = (
                glyphs[0]
                if len(glyphs) == 1
                else min(glyphs, key=lambda g: abs(float(g["ink_left"]) - dst_ink_left))
            )
            dx = dst_ink_left - float(src["ink_left"])
            dy = float(metrics["baseline_b"]) * dx
            placements.append((src["alpha"], src["origin"][0] + dx, src["origin"][1] + dy, "borrow"))
            weight_area = float(src["box"][2] * src["box"][3])
            ink_total += weight_area * np.asarray(src["ink"], np.float64)
            ink_weight += weight_area
            step = None if advance == "font" else src.get("advance")
            if step is None:
                step = adv_font if adv_font is not None else float(src["box"][2] + 2)
        else:
            a = _render_glyph(ch, path, size, sigma, weight)
            ys, xs = np.where(a > 0.002)
            if len(ys) == 0:
                pen += adv_font or size
                continue
            ty0, ty1 = ys.min(), ys.max() + 1
            tx0, tx1 = xs.min(), xs.max() + 1
            a = a[ty0:ty1, tx0:tx1]
            c = int(math.ceil(size * RENDER_EM))
            c += c % 2
            left = pen - c / 2.0 + tx0
            top = baseline_y(pen, metrics) + v_off - c / 2.0 + ty0
            placements.append((a, left, top, "synth"))
            weight_area = max(1.0, 0.45 * float(size) * float(size))
            ink_total += weight_area * np.asarray(metrics["ink_rgb"], np.float64)
            ink_weight += weight_area
            step = adv_font
        pen += float(step)

    if not placements:
        raise ValueError("nothing to render")
    lefts = [p[1] for p in placements]
    tops = [p[2] for p in placements]
    rights = [p[1] + p[0].shape[1] for p in placements]
    bottoms = [p[2] + p[0].shape[0] for p in placements]
    if min(lefts) < x0 - 1e-6 or min(tops) < y0 - 1e-6:
        raise ValueError(
            "rendered text overruns the measured box to the left/top; "
            "expand box and re-measure"
        )
    frame_x1 = int(min(img_w, max(x1, int(math.ceil(max(rights))))))
    frame_y1 = int(min(img_h, max(y1, int(math.ceil(max(bottoms))))))
    patch = np.zeros((frame_y1 - y0, frame_x1 - x0), np.float32)
    for a, left, top, _kind in placements:
        _add_patch(patch, a, left - x0, top - y0)
    if ink is not None:
        ink_rgb = np.asarray(ink, np.float32).reshape(3)
    elif ink_weight > 0:
        # one colour has to serve the whole patch: the area-weighted mean of the
        # borrowed glyph inks (and the measured ink for synthetic glyphs)
        ink_rgb = (ink_total / ink_weight).astype(np.float32)
    else:
        ink_rgb = np.asarray(metrics["ink_rgb"], np.float32)
    return patch, ink_rgb


# --------------------------------------------------------------------------
# composition
# --------------------------------------------------------------------------
def compose(
    image: np.ndarray,
    patch_alpha: np.ndarray,
    ink: np.ndarray,
    left: float,
    top: float,
    mask: np.ndarray | None = None,
    ink_noise: np.ndarray | None = None,
) -> np.ndarray:
    """Composite ``out = B + A * (ink - B)`` at ``(left, top)``.

    ``image`` may be uint8 or float (0..255); the returned array keeps the
    input dtype.  ``ink`` is either an RGB triple or an HxWx3 field.
    ``mask`` (full-image or patch sized, bool/0-1) restricts where pixels are
    written.  ``ink_noise`` is an optional extension: an HxWx3 residual field
    added to the ink, which keeps the re-inked glyphs as grainy as the scan
    instead of perfectly flat.
    """
    arr = np.asarray(image)
    dtype = arr.dtype
    base_img = arr.astype(np.float32)
    if base_img.ndim == 2:
        base_img = np.stack([base_img] * 3, axis=-1)
    alpha = np.asarray(patch_alpha, np.float32)
    if alpha.ndim == 3:
        alpha = alpha[:, :, 0]
    ph, pw = alpha.shape
    ix, iy = int(math.floor(left)), int(math.floor(top))
    fx, fy = left - ix, top - iy
    if abs(fx) > 1e-6 or abs(fy) > 1e-6:
        alpha = _shift(alpha, fx, fy)

    h, w = base_img.shape[:2]
    dy0, dy1 = max(0, iy), min(h, iy + ph)
    dx0, dx1 = max(0, ix), min(w, ix + pw)
    if dy0 >= dy1 or dx0 >= dx1:
        return np.clip(base_img, 0, 255).astype(dtype)
    ys = slice(dy0 - iy, dy0 - iy + (dy1 - dy0))
    xs = slice(dx0 - ix, dx0 - ix + (dx1 - dx0))
    a = alpha[ys, xs]

    base = base_img[dy0:dy1, dx0:dx1]
    ink_arr = np.asarray(ink, np.float32)
    if ink_arr.ndim == 1:
        ink_field = ink_arr.reshape(1, 1, 3)
    else:
        ink_field = ink_arr[ys, xs] if ink_arr.shape[:2] == (ph, pw) else ink_arr[dy0:dy1, dx0:dx1]
        ink_field = ink_field.reshape(dy1 - dy0, dx1 - dx0, 3)
    if ink_noise is not None:
        nz = np.asarray(ink_noise, np.float32)
        ink_field = ink_field + (nz[ys, xs] if nz.shape[:2] == (ph, pw) else nz[dy0:dy1, dx0:dx1])

    upd = base + a[:, :, None] * (ink_field - base)
    if mask is not None:
        m = np.asarray(mask)
        if m.ndim == 3:
            m = m[:, :, 0]
        if m.shape == (ph, pw):
            m = m[ys, xs]
        else:
            m = m[dy0:dy1, dx0:dx1]
        keep = m.astype(bool) if m.dtype == bool else (m > 0.5)
        upd = np.where(keep[:, :, None], upd, base)
    base_img[dy0:dy1, dx0:dx1] = upd
    if np.issubdtype(dtype, np.integer):
        return np.clip(np.rint(base_img), 0, 255).astype(dtype)
    return base_img.astype(dtype)


# --------------------------------------------------------------------------
# reporting helpers
# --------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return [round(float(v), 4) for v in value.reshape(-1)]
    if isinstance(value, (np.floating, np.integer)):
        return round(float(value), 4)
    if isinstance(value, tuple):
        return [round(float(v), 4) if isinstance(v, (int, float, np.floating)) else v for v in value]
    if isinstance(value, float):
        return round(value, 4)
    return value


def metric_summary(metrics: dict) -> dict:
    """JSON-safe view of a metrics dict (arrays dropped)."""
    keys = [
        "box", "baseline_a", "baseline_b", "baseline_x0", "ink_rgb", "ink_lum",
        "sigma", "sigma_psf", "sigma_source", "stroke_px", "noise_std",
        "glyph_count", "source_text", "font_path", "font_size", "font_sigma",
        "font_weight_px", "font_ncc", "font_size_ncc", "font_advance_rmse",
        "font_id_method",
    ]
    out = {k: _jsonable(metrics[k]) for k in keys if k in metrics}
    out["glyphs"] = [
        {
            "char": g.get("char"),
            "box": list(g["box"]),
            "area": int(g["area"]),
            "ink": [round(float(v), 1) for v in g["ink"]],
            "stroke_px": round(float(g["stroke_px"]), 2),
        }
        for g in metrics.get("glyphs", [])
    ]
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parse_box(value: str) -> tuple[int, int, int, int]:
    try:
        parts = tuple(int(round(float(v))) for v in value.replace(";", ",").split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("box must be x0,y0,x1,y1") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("box must be x0,y0,x1,y1")
    return parts


def _load_glyph_boxes(args: argparse.Namespace, metrics: dict) -> list[tuple]:
    if args.boxes_json:
        data = json.loads(Path(args.boxes_json).read_text(encoding="utf-8"))
        boxes = [tuple(item) for item in data]
        for item in boxes:
            if len(item) != 5 or not isinstance(item[0], str):
                raise ValueError("boxes-json entries must be [char, x, y, w, h]")
        return boxes
    if not args.chars:
        return []
    comps = [g for g in metrics["glyphs"] if g["area"] >= 8]
    if len(comps) != len(args.chars):
        raise SystemExit(
            f"--chars has {len(args.chars)} characters but {len(comps)} glyph "
            "components were measured; pass --boxes-json for explicit boxes"
        )
    boxes = []
    for ch, g in zip(args.chars, comps):
        bx, by, bw, bh = g["box"]
        boxes.append((ch, bx, by, bw, bh))
    return boxes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="glyph_engine.py",
        description=(
            "Measurement-driven glyph borrowing engine. Measures a text line, "
            "cuts a glyph library from it, optionally re-renders a line and "
            "reports reconstruction error."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  py scripts/glyph_engine.py --image scan.jpg --box 2028,1882,2412,1978 "
            "--chars \"2026年9月18日\" --rebuild --out rebuild.png\n"
            "  py scripts/glyph_engine.py --image scan.jpg --box ... --chars ... "
            "--identify --json metrics.json\n"
        ),
    )
    parser.add_argument("--image", required=True, help="source image path")
    parser.add_argument("--box", required=True, type=_parse_box, help="measurement box x0,y0,x1,y1")
    parser.add_argument("--chars", help="source characters, left to right")
    parser.add_argument("--boxes-json", help="JSON file with [[char,x,y,w,h], ...]")
    parser.add_argument("--text", help="text to render instead of the source text")
    parser.add_argument("--pen-x", type=float, help="first pen x position (absolute px)")
    parser.add_argument("--font", help="font file to use for missing glyphs")
    parser.add_argument("--identify", action="store_true", help="run font identification")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the source line from borrowed mattes and score it")
    parser.add_argument("--out", help="write the composed result to this PNG path")
    parser.add_argument("--json", dest="json_out", help="write metric summary JSON here")
    args = parser.parse_args(argv)

    image_path = Path(args.image)
    if not image_path.is_file():
        parser.error(f"image not found: {image_path}")
    image = np.asarray(Image.open(image_path).convert("RGB"))

    metrics = measure(image, args.box)
    boxes = _load_glyph_boxes(args, metrics)
    library = build_glyph_library(image, metrics, boxes)

    print(f"box            {metrics['box']}")
    print(f"glyphs         {len(metrics['glyphs'])} components, library {len(library)} chars")
    print(
        f"ink_rgb        {np.round(metrics['ink_rgb'], 1)}  "
        f"stroke {metrics['stroke_px']:.2f}px  sigma(psf) {metrics['sigma_psf']:.2f}"
    )
    print(
        f"baseline       y = {metrics['baseline_a']:.2f} + "
        f"{metrics['baseline_b']:+.5f}*(x-{metrics['baseline_x0']:.0f})"
    )
    print(f"noise std      {np.round(metrics['noise_std'], 2)}")

    font_path = args.font
    if args.identify or (args.text and any(ch not in library for ch in args.text)):
        ident = identify_font(metrics, library, font_path=font_path)
        print(
            f"font           {Path(ident).name if ident else None} "
            f"size={metrics.get('font_size')} sigma={metrics.get('font_sigma')} "
            f"weight={metrics.get('font_weight_px')} ncc={metrics.get('font_ncc')}"
        )

    rebuild_stats = None
    if args.rebuild:
        if not boxes:
            parser.error("--rebuild needs --chars or --boxes-json")
        src_text = "".join(b[0] for b in boxes)
        patch, ink = render(src_text, metrics, library, font_path=None)
        x0, y0, x1, y1 = metrics["box"]
        base_full = image.astype(np.float32).copy()
        base_full[y0:y1, x0:x1] = np.clip(metrics["bg"] + metrics["noise_field"], 0, 255)
        ph, pw = patch.shape
        nf = metrics["noise_field"]
        noise_ink = np.zeros((ph, pw, 3), np.float32)
        noise_ink[:min(ph, nf.shape[0]), :min(pw, nf.shape[1])] = nf[:ph, :pw]
        out_full = compose(base_full, patch, ink, x0, y0, ink_noise=noise_ink)
        src_f = image.astype(np.float32)
        alpha = metrics["alpha"]
        glyph_mask = _dilate((alpha > 0.05).astype(np.float32), 2, "square") > 0.5
        diff = np.abs(out_full[y0:y1, x0:x1] - src_f[y0:y1, x0:x1])
        vals = diff[glyph_mask] if glyph_mask.any() else diff.reshape(-1, 3)
        rmse = float(np.sqrt(np.mean(vals ** 2)))
        mean_abs = float(np.mean(np.abs(vals)))
        # window metric over the whole line box: this is the number that is
        # comparable with the reference implementation's "2-3" (a window that
        # includes the background between glyphs, not a strict ink mask)
        bx0 = max(0, min(b[1] for b in boxes) - 12)
        by0 = max(0, min(b[2] for b in boxes) - 12)
        bx1 = min(out_full.shape[1], max(b[1] + b[3] for b in boxes) + 12)
        by1 = min(out_full.shape[0], max(b[2] + b[4] for b in boxes) + 12)
        win_diff = np.abs(out_full[by0:by1, bx0:bx1] - src_f[by0:by1, bx0:bx1])
        rebuild_stats = {
            "pixels": int(glyph_mask.sum()),
            "mean_abs": round(mean_abs, 3),
            "rmse": round(rmse, 3),
            "max_abs": round(float(vals.max()), 2),
            "p99_abs": round(float(np.percentile(vals, 99)), 2),
            "window": [int(bx0), int(by0), int(bx1), int(by1)],
            "window_mean_abs": round(float(win_diff.mean()), 3),
            "window_rmse": round(float(np.sqrt(np.mean(win_diff ** 2))), 3),
        }
        print(
            f"rebuild        glyph-mask mean|diff|={mean_abs:.3f}  RMSE={rmse:.3f}  "
            f"p99={rebuild_stats['p99_abs']:.2f}  max={rebuild_stats['max_abs']:.2f}"
        )
        print(
            f"               window mean|diff|={rebuild_stats['window_mean_abs']:.3f}  "
            f"RMSE={rebuild_stats['window_rmse']:.3f}"
        )
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(np.clip(np.rint(out_full), 0, 255).astype(np.uint8)).save(out_path)
            print(f"wrote          {out_path}")
    elif args.text and args.out:
        patch, ink = render(args.text, metrics, library, font_path=font_path, pen_x=args.pen_x)
        base = image.astype(np.float32)
        mask = np.zeros(base.shape[:2], bool)
        x0, y0, _, _ = metrics["box"]
        mask[y0:y0 + patch.shape[0], x0:x0 + patch.shape[1]] = patch > 0.01
        out = compose(base, patch, ink, x0, y0, mask=mask)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8)).save(out_path)
        print(f"wrote          {out_path} (glyph pixels only; erase old ink first for a full edit)")

    if args.json_out:
        payload = {"metrics": metric_summary(metrics), "rebuild": rebuild_stats}
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote          {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
