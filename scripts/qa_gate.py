#!/usr/bin/env python3
"""qa_gate.py — objective acceptance gate for scan text revisions.

Implements the eight metrics from ``IMPLEMENTATION-PLAN.md`` section 2 and
answers the only question that matters after an edit: *can you see it?*

==================  ==================================================  =========
metric              algorithm                                           threshold
==================  ==================================================  =========
outside region      per-pixel |src-out| outside every declared region    max == 0
boundary step       second difference of column/row means at the seams     < 1.0
full-ink colour     glyph-core 99th percentile luminance, vs neighbours    < 3
stroke width        distance-transform median * 2, vs neighbours          < 0.5 px
glyph height        bbox height, digits vs digits / CJK vs matched CJK    < 1 px
baseline            source digit baseline a+b*x, per new digit residual   < 1 px
CJK baseline        ink-top offset vs shape-matched source CJK glyph      < 1 px
noise               high-pass std ratio, rebuilt vs neighbouring bg      0.7 .. 1.4
quantisation        output qtables equal to the source's                  equal
OCR                 tesseract read-back equals the target string          equal
==================  ==================================================  =========

The baseline and glyph-height metrics are class aware.  Digits sit on the
baseline while CJK glyphs overshoot a few px below it, and a 0.9 degree line
tilt lifts the baseline by several px over the width of an edit; comparing
"new glyph bottom" with "neighbour baseline at one point" therefore mixes
three different effects.  The gate instead

* splits glued components (``3`` + ``日``) at the column-density valley,
* fits ``y = a + b*x`` through the **digits** of the source line,
* compares **each new digit** with that line at its own x (tilt aware), and
* compares CJK glyphs only with **shape-matched** source CJK glyphs, via
  their ink-top offset from the same baseline (skipped when no CJK reference
  exists, never failed).

"Skipped" is a real answer: if tesseract is not installed the OCR check is
reported as ``skipped`` (never as a pass), and a lossless output without a
sibling JPEG is reported the same way for the quantisation table.

API
---
    run_gate(source_path, output_path, edits) -> dict
        edits = [{'bbox': (x, y, w, h), 'expected_text': str,
                  'region': (x0, y0, x1, y1)}]

    The result is ``{'pass': bool, 'metrics': {...}, 'failures': [...],
                     'checks': [...], 'skipped': [...], 'per_edit': [...]}``.

CLI
---
    py scripts/qa_gate.py --source scan.jpg --output out_v4.png \
        --edits edits.json --json-out gate.json

    py scripts/qa_gate.py --source scan.jpg --output out.png \
        --bbox 2186,1890,190,68 --expected-text "10月13日" \
        --region 2186,1882,2376,1958

    py scripts/qa_gate.py --help
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

from glyph_engine import (  # noqa: E402  (same-directory script module)
    _dilate,
    _gaussian,
    _measure_components,
    measure,
)

__all__ = ["run_gate", "main"]

LUM = np.array([0.299, 0.587, 0.114], np.float32)

THRESHOLDS = {
    "outside_max_diff": 0.0,
    "boundary_step": 1.0,
    "ink_delta": 3.0,
    "ink_chroma_delta": 3.0,
    "stroke_delta": 0.5,
    "glyph_height_delta": 1.0,
    "baseline_delta": 1.0,
    "noise_ratio": (0.7, 1.4),
}


def _rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """sRGB (0-255) to CIELAB, D65. Used for a perceptual ink-colour comparison:
    a luminance-only delta cannot see a channel cast (a +8 blue shift scored better
    than the correct render under the pure-luminance metric)."""
    c = np.asarray(rgb, np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124, 0.3576, 0.1805],
                  [0.2126, 0.7152, 0.0722],
                  [0.0193, 0.1192, 0.9505]])
    t = (m @ c) / np.array([0.95047, 1.0, 1.08883])
    t = np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16.0 / 116.0)
    return np.array([116.0 * t[1] - 16.0, 500.0 * (t[0] - t[1]), 200.0 * (t[1] - t[2])])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load(path: str) -> tuple[np.ndarray, dict | None]:
    with Image.open(path) as im:
        rgb = np.asarray(im.convert("RGB")).copy()
        qtables = getattr(im, "quantization", None)
        qtables = dict(qtables) if qtables else None
    return rgb, qtables


def _lum(rgb: np.ndarray) -> np.ndarray:
    return (
        rgb[:, :, 0] * LUM[0] + rgb[:, :, 1] * LUM[1] + rgb[:, :, 2] * LUM[2]
    ).astype(np.float32)


def _normalise_edit(edit: dict, width: int, height: int) -> dict:
    if "bbox" not in edit or "region" not in edit:
        raise ValueError("every edit needs 'bbox' and 'region'")
    bbox = tuple(int(round(float(v))) for v in edit["bbox"])
    region = tuple(int(round(float(v))) for v in edit["region"])
    if len(bbox) != 4 or len(region) != 4:
        raise ValueError("bbox must be (x, y, w, h) and region (x0, y0, x1, y1)")
    bx, by, bw, bh = bbox
    if bw <= 0 or bh <= 0:
        raise ValueError(f"bbox has non-positive size: {bbox}")
    rx0, ry0, rx1, ry1 = region
    rx0, rx1 = sorted((max(0, rx0), min(width, rx1)))
    ry0, ry1 = sorted((max(0, ry0), min(height, ry1)))
    if rx1 - rx0 < 4 or ry1 - ry0 < 4:
        raise ValueError(f"region is empty or too small: {region}")
    bx0, by0 = max(0, bx), max(0, by)
    bx1, by1 = min(width, bx + bw), min(height, by + bh)
    if bx1 - bx0 < 4 or by1 - by0 < 4:
        raise ValueError(f"bbox is empty or outside the image: {bbox}")
    if bx0 < rx0 or by0 < ry0 or bx1 > rx1 or by1 > ry1:
        raise ValueError(
            f"bbox {bbox} must lie inside region {(rx0, ry0, rx1, ry1)}"
        )
    return {
        "bbox": (bx0, by0, bx1, by1),
        "region": (rx0, ry0, rx1, ry1),
        "expected_text": str(edit.get("expected_text", "")),
    }


def _window(bbox: tuple[int, int, int, int], pad: int, shape) -> tuple[int, int, int, int]:
    h, w = shape
    x0, y0, x1, y1 = bbox
    return (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))


def _boundary_step(
    src: np.ndarray,
    out: np.ndarray,
    region: tuple[int, int, int, int],
    alpha_src: np.ndarray,
    alpha_out: np.ndarray,
    win: tuple[int, int, int, int],
) -> float | None:
    """Max |second difference| excess introduced at the four region seams."""
    x0, y0, x1, y1 = region
    wx0, wy0, wx1, wy1 = win
    h, w = src.shape[:2]
    sl = np.s_[wy0:wy1, wx0:wx1]
    a_s = np.asarray(alpha_src)  # already window-shaped
    a_o = np.asarray(alpha_out)
    ls, lo = _lum(src[sl]), _lum(out[sl])

    def col_means(img: np.ndarray, mask_rows: np.ndarray) -> np.ndarray:
        return img[mask_rows, :].mean(axis=0)

    def row_means(img: np.ndarray, mask_cols: np.ndarray) -> np.ndarray:
        return img[:, mask_cols].mean(axis=1)

    steps: list[float] = []

    def evaluate(profile_s, profile_o, i: int, coord0: int) -> None:
        j = i - coord0
        if j - 1 < 0 or j + 1 >= len(profile_s):
            return
        d2_s = profile_s[j + 1] - 2.0 * profile_s[j] + profile_s[j - 1]
        d2_o = profile_o[j + 1] - 2.0 * profile_o[j] + profile_o[j - 1]
        steps.append(abs(float(d2_o - d2_s)))

    # vertical seams (left / right edges), column means over glyph-free rows
    region_rows = np.zeros(a_s.shape[0], bool)
    region_rows[max(0, y0 - wy0):max(0, y1 - wy0)] = True
    for edge in (x0, x1):
        cols = [c - wx0 for c in (edge - 1, edge, edge + 1) if wx0 <= c < wx1]
        if len(cols) < 3:
            continue
        free = (
            (a_s[:, cols].max(axis=1) < 0.1)
            & (a_o[:, cols].max(axis=1) < 0.1)
            & region_rows
        )
        if int(free.sum()) < 4:
            free = region_rows
        if int(free.sum()) < 2:
            continue
        evaluate(col_means(ls, free), col_means(lo, free), edge - wx0, 0)

    # horizontal seams (top / bottom edges), row means over glyph-free columns
    region_cols = np.zeros(a_s.shape[1], bool)
    region_cols[max(0, x0 - wx0):max(0, x1 - wx0)] = True
    for edge in (y0, y1):
        rows = [r - wy0 for r in (edge - 1, edge, edge + 1) if wy0 <= r < wy1]
        if len(rows) < 3:
            continue
        free = (
            (a_s[rows, :].max(axis=0) < 0.1)
            & (a_o[rows, :].max(axis=0) < 0.1)
            & region_cols
        )
        if int(free.sum()) < 4:
            free = region_cols
        if int(free.sum()) < 2:
            continue
        evaluate(row_means(ls, free), row_means(lo, free), edge - wy0, 0)

    return round(float(max(steps)), 4) if steps else None


def _noise_ratio(
    src: np.ndarray,
    out: np.ndarray,
    alpha_src: np.ndarray,
    alpha_out: np.ndarray,
    win: tuple[int, int, int, int],
    region: tuple[int, int, int, int],
    margin: int = 4,
) -> float | None:
    """High-pass std of the rebuilt background over the intact neighbours' grain.

    Pixels within ``margin`` px of strong ink (source or output) are left out:
    glyph blur tails are structure, not grain, and would swamp the estimate.
    """
    x0, y0, x1, y1 = win
    sl = np.s_[y0:y1, x0:x1]
    ls, lo = _lum(src[sl]), _lum(out[sl])
    hh, ww = alpha_src.shape
    region_mask = np.zeros((hh, ww), bool)
    rx0, ry0, rx1, ry1 = region
    region_mask[
        max(0, ry0 - y0):max(0, ry1 - y0), max(0, rx0 - x0):max(0, rx1 - x0)
    ] = True
    old_ink = _dilate((alpha_src > 0.3).astype(np.float32), max(2, margin // 2), "square") >= 0.5
    new_ink = _dilate((alpha_out > 0.3).astype(np.float32), margin, "square") >= 0.5
    rebuilt = region_mask & (alpha_src > 0.05) & ~old_ink & ~new_ink
    if int(rebuilt.sum()) < 40:
        return None
    ref = (~region_mask) & (alpha_src < 0.02) & ~old_ink
    ref &= _dilate(region_mask.astype(np.float32), 2, "square") < 0.5
    if int(ref.sum()) < 60:
        return None
    hp_o = lo - _gaussian(lo, 6.0)
    hp_r = ls - _gaussian(ls, 6.0)
    ref_std = float(hp_r[ref].std())
    if ref_std < 1e-6:
        return None
    return round(float(hp_o[rebuilt].std()) / ref_std, 3)


def _alpha_and_window(
    image: np.ndarray, win: tuple[int, int, int, int]
) -> np.ndarray | None:
    try:
        return measure(image, win)["alpha"]
    except ValueError:
        return None


def _measure_window(image: np.ndarray, win: tuple[int, int, int, int]) -> dict | None:
    try:
        return measure(image, win)
    except ValueError:
        return None


def _centre_inside(g: dict, box: tuple[int, int, int, int]) -> bool:
    cx = g["box"][0] + g["box"][2] / 2.0
    cy = g["box"][1] + g["box"][3] / 2.0
    return box[0] <= cx < box[2] and box[1] <= cy < box[3]


def _glyph_pool(m: dict) -> list[dict]:
    """Glyph-sized components of a measured window.

    Specks, leftover fragments and glyphs cut by the window border are dropped
    before any statistics: a 10 px blot would otherwise create a fake height
    gap and turn every real glyph into "CJK".
    """
    comps = [g for g in m["glyphs"] if g["area"] >= 12 and not g["border"]]
    if not comps:
        return []
    med_h = float(np.median([g["box"][3] for g in comps]))
    return [g for g in comps if g["box"][3] >= 0.5 * med_h]


def _classify(comps: list[dict]) -> list[dict]:
    """Split glyphs into ``digit`` and ``cjk`` classes.

    CJK ideographs fill the em box and are 4-8 px taller than the digits of the
    same line, so a bimodal height split separates the classes whenever both
    are present.  In a single-class window the width/height ratio decides
    (digits are narrow, CJK glyphs are square-ish).
    """
    if not comps:
        return []
    hs = sorted(float(g["box"][3]) for g in comps)
    max_h = hs[-1]
    gap = 0.0
    cut = 0.0
    if len(hs) > 1:
        gaps = [(hs[i + 1] - hs[i], i) for i in range(len(hs) - 1)]
        gap, gi = max(gaps)
        cut = (hs[gi] + hs[gi + 1]) / 2.0
    out = []
    for g in comps:
        w, h = float(g["box"][2]), float(g["box"][3])
        if gap >= 3.0:
            kind = "cjk" if h > cut else "digit"
        else:
            kind = "cjk" if (h >= 0.9 * max_h and w / max(h, 1.0) >= 0.72) else "digit"
        gg = dict(g)
        gg["kind"] = kind
        out.append(gg)
    return out


def _split_wide(
    image: np.ndarray,
    win: tuple[int, int, int, int],
    m: dict,
    comps: list[dict],
    max_depth: int = 2,
) -> list[dict]:
    """Split components that glued two glyphs together (e.g. ``3`` + ``日``).

    A component wider than 1.6x the line's median glyph width is cut at the
    valley of its column-projection profile (the blur bridges the two glyphs,
    but the coverage density dips between them).  Every piece is re-measured
    from the coverage map so its bbox, ink and stroke are its own.
    """
    if not comps:
        return []
    alpha = np.asarray(m["alpha"], np.float32)
    wx0, wy0 = win[0], win[1]
    rgb = image[win[1]:win[3], win[0]:win[2]].astype(np.float32)
    mh = float(np.median([g["box"][3] for g in comps]))
    widths = [g["box"][2] for g in comps if g["box"][3] >= 0.5 * mh]
    mw = float(np.median(widths)) if widths else 0.0
    if mw <= 0:
        return comps
    out: list[dict] = []
    for g in comps:
        bx, by, bw, bh = g["box"]
        # too narrow to be two glyphs, or as wide as a single CJK ideograph is
        # allowed to be (w/h <= 1.2) -> keep it whole
        if bw <= 1.6 * mw or bw <= 1.2 * bh:
            out.append(g)
            continue
        ox, oy = bx - wx0, by - wy0
        if ox < 0 or oy < 0 or ox + bw > alpha.shape[1] or oy + bh > alpha.shape[0]:
            out.append(g)
            continue
        prof = alpha[oy:oy + bh, ox:ox + bw].sum(axis=0)
        if prof.size < 4 or float(prof.max()) < 4.0:
            out.append(g)
            continue
        cuts: list[int] = []

        def find(c0: int, c1: int, depth: int) -> None:
            span = c1 - c0
            if depth > max_depth or span < 1.6 * mw:
                return
            lo = c0 + max(2, int(0.3 * span))
            hi = c1 - max(2, int(0.3 * span))
            if hi <= lo:
                return
            k = lo + int(np.argmin(prof[lo:hi]))
            peaks = []
            if k > c0:
                peaks.append(float(prof[c0:k].max()))
            if k + 1 < c1:
                peaks.append(float(prof[k + 1:c1].max()))
            peak = max(peaks) if peaks else 0.0
            if peak <= 0 or float(prof[k]) > 0.6 * peak:
                return
            cuts.append(k)
            find(c0, k, depth + 1)
            find(k + 1, c1, depth + 1)

        find(0, int(bw), 0)
        if not cuts:
            out.append(g)
            continue
        bounds = [0] + sorted(set(cuts)) + [int(bw)]
        for a, b in zip(bounds, bounds[1:]):
            if b - a < 2:
                continue
            iso = np.zeros_like(alpha)
            iso[oy:oy + bh, ox + a:ox + b] = alpha[oy:oy + bh, ox + a:ox + b]
            pieces = [p for p in _measure_components(rgb, iso, wx0, wy0) if p["area"] >= 8]
            out.extend(pieces)
    return out


def _comp_stats(comps: list[dict]) -> dict | None:
    """Ink / stroke / height summary of a component set (specks dropped)."""
    if len(comps) < 2:
        return None
    med_h = float(np.median([g["box"][3] for g in comps]))
    keep = [g for g in comps if g["box"][3] >= 0.4 * med_h]
    if len(keep) < 2:
        keep = comps
    areas = np.array([g["area"] for g in keep], np.float64)
    # perceived tone, not the ringing peak: a rebuilt glyph carries smooth ink, so a
    # top-decile statistic would mark it darker than a JPEG-ringed original and reward
    # over-bright text.
    inks = np.array([g.get("ink_tone", g["ink_lum"]) for g in keep], np.float64)
    inks_rgb = np.array([g["ink"] for g in keep], np.float64)
    return {
        "n": len(keep),
        "ink": float((areas * inks).sum() / max(areas.sum(), 1e-6)),
        "ink_rgb": (areas[:, None] * inks_rgb).sum(0) / max(areas.sum(), 1e-6),
        "stroke": float(np.median([g["stroke_px"] for g in keep])),
        "height": float(np.median([g["box"][3] for g in keep])),
    }


def _fit_digit_baseline(digits: list[dict]) -> tuple[float, float, int] | None:
    """Least-squares baseline ``y = a + b*x`` through digit bottoms, trimmed.

    Only digits (which sit on the baseline) are used: CJK glyphs overshoot a
    few px below it and would drag the line down.  Outlier trimming makes the
    fit robust to a misclassified wide glyph.
    """
    pts = [(float(g["cx"]), float(g["bottom"])) for g in digits]
    if len(pts) < 3:
        return None
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    if float(xs.std()) < 1e-3:
        return None
    b, a = np.polyfit(xs, ys, 1)
    used = np.ones(len(xs), bool)
    for _ in range(4):
        r = ys - (a + b * xs)
        centre = float(np.median(r[used]))
        keep = np.abs(r - centre) <= 3.0
        if int(keep.sum()) < max(3, int(0.6 * len(pts))) or np.array_equal(keep, used):
            break
        used = keep
        b, a = np.polyfit(xs[used], ys[used], 1)
    return float(a), float(b), int(used.sum())


def _match_shapes(
    new: list[dict], src: list[dict], max_dist: float = 8.0
) -> list[tuple[dict, dict]]:
    """Greedy bbox-size matching so glyphs are compared with lookalikes.

    Comparing 年's ink top with 日's would fail by glyph shape alone; matching
    each new CJK glyph to the source glyph of the same footprint keeps the
    comparison honest.  Pairs further apart than ``max_dist`` px (very
    different characters) are dropped rather than compared.
    """
    pairs: list[tuple[dict, dict]] = []
    remaining = list(range(len(src)))
    for g in new:
        if not remaining:
            break
        gw, gh = float(g["box"][2]), float(g["box"][3])
        dists = {
            j: abs(float(src[j]["box"][2]) - gw) + abs(float(src[j]["box"][3]) - gh)
            for j in remaining
        }
        j = min(dists, key=dists.get)
        if dists[j] > max_dist:
            continue
        remaining.remove(j)
        pairs.append((g, src[j]))
    return pairs


def _quantisation_equal(source_path: str, output_path: str, qt_src, qt_out) -> dict:
    if qt_src is None:
        return {"status": "skipped", "reason": "source has no JPEG quantisation tables"}
    path_used = output_path
    if qt_out is None:
        src_ext = Path(output_path).suffix.lower()
        sidecar = None
        if src_ext in (".png", ".tif", ".tiff", ".bmp"):
            for ext in (".jpg", ".jpeg"):
                cand = Path(output_path).with_suffix(ext)
                if cand.is_file():
                    sidecar = str(cand)
                    break
        if sidecar is None:
            return {
                "status": "skipped",
                "reason": "output is lossless and has no sibling JPEG to check",
            }
        path_used = sidecar
        with Image.open(sidecar) as im:
            qt_out = getattr(im, "quantization", None)
            qt_out = dict(qt_out) if qt_out else None
    if qt_out is None:
        return {"status": "skipped", "reason": f"no quantisation tables in {path_used}"}
    same = {int(k): list(v) for k, v in qt_src.items()} == {
        int(k): list(v) for k, v in qt_out.items()
    }
    return {
        "status": "pass" if same else "fail",
        "value": same,
        "checked_file": path_used,
        "source_tables": len(qt_src),
        "output_tables": len(qt_out),
    }


def _normalise_text(text: str) -> str:
    return "".join(str(text).split())


def _ocr_available() -> str | None:
    return shutil.which("tesseract")


def _ocr_read(image: np.ndarray, bbox: tuple[int, int, int, int], exe: str) -> tuple[str | None, str | None]:
    x0, y0, x1, y1 = bbox
    pad = 8
    h, w = image.shape[:2]
    crop = image[max(0, y0 - pad): min(h, y1 + pad), max(0, x0 - pad): min(w, x1 + pad)]
    if crop.size == 0:
        return None, "empty crop"
    im = Image.fromarray(crop).convert("L")
    im = im.resize((im.width * 3, im.height * 3), Image.LANCZOS)
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "crop.png"
        im.save(png)
        for lang in ("chi_sim+eng", "eng"):
            try:
                res = subprocess.run(
                    [exe, str(png), "stdout", "-l", lang, "--psm", "7"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                return None, f"tesseract failed: {exc}"
            if res.returncode != 0:
                continue
            return res.stdout.strip(), None
    return None, "tesseract failed for every language pack"


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def run_gate(source_path: str, output_path: str, edits: list[dict]) -> dict:
    """Run the eight-metric acceptance gate.

    ``edits`` entries are ``{'bbox': (x, y, w, h), 'expected_text': str,
    'region': (x0, y0, x1, y1)}``; ``bbox`` is where the new text lives,
    ``region`` is the whole area the edit is allowed to touch.  Returns
    ``{'pass', 'metrics', 'checks', 'failures', 'skipped', 'per_edit'}``.
    """
    src, qt_src = _load(source_path)
    out, qt_out = _load(output_path)
    h, w = src.shape[:2]
    if out.shape[:2] != src.shape[:2]:
        return {
            "pass": False,
            "metrics": {"source_shape": list(src.shape[:2]), "output_shape": list(out.shape[:2])},
            "failures": [
                f"output size {out.shape[:2]} differs from source {src.shape[:2]}"
            ],
            "checks": [],
            "skipped": [],
            "per_edit": [],
        }

    norm = [_normalise_edit(e, w, h) for e in edits]
    if not norm:
        raise ValueError("at least one edit is required")

    checks: list[dict] = []
    failures: list[str] = []
    skipped: list[dict] = []
    per_edit: list[dict] = []

    def add_check(name: str, value: Any, threshold: Any, status: str, detail: str | None = None) -> None:
        entry = {"name": name, "value": value, "threshold": threshold, "status": status}
        if detail:
            entry["detail"] = detail
        checks.append(entry)
        if status == "fail":
            failures.append(f"{name}: {value} (threshold {threshold})")
        elif status == "skipped":
            skipped.append({"name": name, "reason": detail})

    # -- 1. outside the declared regions: bit-identical --------------------
    allowed = np.zeros((h, w), bool)
    for e in norm:
        x0, y0, x1, y1 = e["region"]
        allowed[y0:y1, x0:x1] = True
    outside = ~allowed
    diff_full = np.abs(out.astype(np.int16) - src.astype(np.int16)).max(axis=2)
    outside_max = int(diff_full[outside].max()) if bool(outside.any()) else 0
    outside_changed = int((diff_full[outside] > 0).sum())
    add_check("outside_max_diff", outside_max, "== 0", "pass" if outside_max == 0 else "fail")
    add_check("outside_changed_pixels", outside_changed, "== 0", "pass" if outside_changed == 0 else "fail")

    # -- 2. boundary step ---------------------------------------------------
    seam: list[float] = []
    for e in norm:
        win = _window(e["region"], 4, (h, w))
        a_s = _alpha_and_window(src, win)
        a_o = _alpha_and_window(out, win)
        if a_s is None or a_o is None:
            continue
        step = _boundary_step(src, out, e["region"], a_s, a_o, win)
        if step is not None:
            seam.append(step)
    boundary = round(max(seam), 4) if seam else None
    if boundary is None:
        add_check("boundary_step", None, "< 1.0", "skipped", "no glyph-free seam rows/columns")
    else:
        add_check(
            "boundary_step", boundary, "< 1.0",
            "pass" if boundary < THRESHOLDS["boundary_step"] else "fail",
        )

    # -- 3-7. per-edit glyph, baseline and noise metrics --------------------
    ink_deltas, stroke_deltas = [], []
    chroma_deltas: list[float] = []
    digit_height_deltas, cjk_height_deltas = [], []
    base_deltas, cjk_base_deltas, noise_ratios = [], [], []
    for idx, e in enumerate(norm):
        bbox, region = e["bbox"], e["region"]
        # one measurement window covers the whole line in each image
        ref_pad = max(170, region[2] - region[0])
        line_win = (
            max(0, region[0] - ref_pad),
            max(0, region[1] - 8),
            min(w, region[2] + ref_pad),
            min(h, region[3] + 8),
        )
        m_src = _measure_window(src, line_win)
        m_out = _measure_window(out, line_win)
        # split glued components (e.g. "3"+"日"), then tag digit / CJK
        src_pool = (
            _classify(_split_wide(src, line_win, m_src, _glyph_pool(m_src))) if m_src else []
        )
        out_pool = (
            _classify(_split_wide(out, line_win, m_out, _glyph_pool(m_out))) if m_out else []
        )
        new_comps = [g for g in out_pool if _centre_inside(g, bbox)]
        ref_comps = [g for g in src_pool if not _centre_inside(g, region)]
        # The digit baseline is fitted on *all* source digits of the line.  The
        # old glyphs inside the replaced region are still valid line geometry
        # (the source image is untouched) and they are what keeps the slope
        # well constrained: four digits outside the edit span only ~88 px, so
        # their quantised bottoms would give a 0.025 slope (2-3 px of tilt error
        # at the new glyphs) instead of the measured 0.0146.
        src_digits = [g for g in src_pool if g["kind"] == "digit"]
        ref_digits = [g for g in src_digits if not _centre_inside(g, region)]
        src_cjk = [g for g in src_pool if g["kind"] == "cjk"]
        new_digits = [g for g in new_comps if g["kind"] == "digit"]
        new_cjk = [g for g in new_comps if g["kind"] == "cjk"]
        stats_new = _comp_stats(new_comps)
        stats_ref = _comp_stats(ref_comps)
        detail = {
            "index": idx,
            "bbox": list(bbox),
            "region": list(region),
            "window": list(line_win),
            "glyphs_new": stats_new["n"] if stats_new else 0,
            "glyphs_ref": stats_ref["n"] if stats_ref else 0,
            "glyphs_new_digit": len(new_digits),
            "glyphs_new_cjk": len(new_cjk),
            "ink_delta": None,
            "stroke_delta": None,
            "glyph_height_delta": None,
            "digit_height_delta": None,
            "cjk_height_delta": None,
            "baseline_delta": None,
            "cjk_baseline_offset": None,
            "noise_ratio": None,
        }
        if stats_new and stats_ref:
            ink_d = abs(stats_new["ink"] - stats_ref["ink"])
            stroke_d = abs(stats_new["stroke"] - stats_ref["stroke"])
            chroma_d = float(
                np.linalg.norm(_rgb_to_lab(stats_new["ink_rgb"]) - _rgb_to_lab(stats_ref["ink_rgb"]))
            )
            ink_deltas.append(ink_d)
            chroma_deltas.append(chroma_d)
            stroke_deltas.append(stroke_d)
            detail.update(
                {
                    "ink_delta": round(ink_d, 3),
                    "ink_chroma_delta": round(chroma_d, 3),
                    "stroke_delta": round(stroke_d, 3),
                    "ink_new": round(stats_new["ink"], 2),
                    "ink_ref": round(stats_ref["ink"], 2),
                    "ink_rgb_new": [round(float(v), 1) for v in stats_new["ink_rgb"]],
                    "ink_rgb_ref": [round(float(v), 1) for v in stats_ref["ink_rgb"]],
                    "stroke_new": round(stats_new["stroke"], 2),
                    "stroke_ref": round(stats_ref["stroke"], 2),
                }
            )

        # -- baseline: fit the source's digits, compare every new digit ------
        fit = _fit_digit_baseline(src_digits)
        if fit and new_digits:
            a, b, n_fit = fit
            residuals = [
                abs(float(g["bottom"]) - (a + b * float(g["cx"]))) for g in new_digits
            ]
            base_d = float(np.median(residuals))
            base_deltas.append(base_d)
            detail["baseline_delta"] = round(base_d, 3)
            detail["baseline_fit"] = {
                "a": round(a, 3),
                "b": round(b, 5),
                "n_digits": n_fit,
            }
            detail["digit_residuals"] = [round(r, 2) for r in residuals]

        # -- height: digits vs digits, CJK vs shape-matched CJK --------------
        ref_h_digits = [float(g["box"][3]) for g in (ref_digits or src_digits)]
        digit_h = None
        if new_digits and len(ref_h_digits) >= 2:
            digit_h = abs(
                float(np.median([float(g["box"][3]) for g in new_digits]))
                - float(np.median(ref_h_digits))
            )
            digit_height_deltas.append(digit_h)
            detail["digit_height_delta"] = round(digit_h, 3)
        cjk_h = None
        if new_cjk and src_cjk:
            pairs = _match_shapes(new_cjk, src_cjk)
            if pairs:
                if fit:
                    a, b, _n = fit
                    cjk_tops = []
                    for gn, gs in pairs:
                        top_new = float(gn["box"][1]) - (a + b * float(gn["cx"]))
                        top_src = float(gs["box"][1]) - (a + b * float(gs["cx"]))
                        cjk_tops.append(abs(top_new - top_src))
                    cjk_top = float(np.median(cjk_tops))
                    cjk_base_deltas.append(cjk_top)
                    detail["cjk_baseline_offset"] = round(cjk_top, 3)
                    detail["cjk_pairs"] = [
                        {"new": list(gn["box"]), "src": list(gs["box"])} for gn, gs in pairs
                    ]
                cjk_h = float(
                    np.median(
                        [abs(float(gn["box"][3]) - float(gs["box"][3])) for gn, gs in pairs]
                    )
                )
                cjk_height_deltas.append(cjk_h)
                detail["cjk_height_delta"] = round(cjk_h, 3)
        heights = [h for h in (digit_h, cjk_h) if h is not None]
        if heights:
            detail["glyph_height_delta"] = round(max(heights), 3)

        if m_src is not None and m_out is not None:
            sigma = float(m_src.get("sigma_psf", 1.5))
            margin = max(2, int(math.ceil(2.5 * min(3.0, sigma))))
            detail["noise_margin"] = margin
            ratio = _noise_ratio(
                src, out, m_src["alpha"], m_out["alpha"], line_win, region, margin=margin
            )
            if ratio is not None:
                noise_ratios.append(ratio)
                detail["noise_ratio"] = ratio
        per_edit.append(detail)

    def _add_worst(
        name: str, values: list[float], threshold: float, cmp: str = "lt",
        skip_reason: str = "not enough glyphs in one of the windows",
    ) -> None:
        if not values:
            if cmp == "lt":
                thr = f"< {threshold}"
            else:
                lo, hi = THRESHOLDS[name]
                thr = f"{lo}..{hi}"
            add_check(name, None, thr, "skipped", skip_reason)
            return
        value = max(values) if cmp == "lt" else None
        if cmp == "lt":
            status = "pass" if value < threshold else "fail"
            add_check(name, round(float(value), 3), f"< {threshold}", status)
        else:
            lo, hi = THRESHOLDS[name]
            worst = max(values, key=lambda v: abs(math.log(max(v, 1e-6) / 1.0)))
            ok = all(lo <= v <= hi for v in values)
            add_check(name, round(float(worst), 3), f"{lo}..{hi}", "pass" if ok else "fail")

    _add_worst("ink_delta", ink_deltas, THRESHOLDS["ink_delta"])
    _add_worst("ink_chroma_delta", chroma_deltas, THRESHOLDS["ink_chroma_delta"])
    _add_worst("stroke_delta", stroke_deltas, THRESHOLDS["stroke_delta"])
    _add_worst(
        "glyph_height_delta",
        digit_height_deltas + cjk_height_deltas,
        THRESHOLDS["glyph_height_delta"],
    )
    _add_worst(
        "baseline_delta", base_deltas, THRESHOLDS["baseline_delta"],
        skip_reason="no comparable digit baseline (need >=3 source digits and new digits)",
    )
    _add_worst(
        "cjk_baseline_offset", cjk_base_deltas, THRESHOLDS["baseline_delta"],
        skip_reason="no CJK glyph pair to compare",
    )
    _add_worst(
        "noise_ratio", noise_ratios, THRESHOLDS["noise_ratio"], cmp="range",
        skip_reason="too few rebuilt background pixels to estimate the grain",
    )

    # -- 8. quantisation tables --------------------------------------------
    qt = _quantisation_equal(source_path, output_path, qt_src, qt_out)
    add_check(
        "qtables_equal",
        qt.get("value"),
        "== True",
        qt["status"],
        qt.get("reason") or (f"checked {qt.get('checked_file')}" if qt.get("checked_file") else None),
    )

    # -- 9. OCR read-back ---------------------------------------------------
    exe = _ocr_available()
    if exe is None:
        add_check("ocr_text", None, "== expected_text", "skipped", "tesseract is not installed")
        for e in norm:
            e["ocr"] = {"status": "skipped", "reason": "tesseract is not installed"}
    else:
        for e in norm:
            got, err = _ocr_read(out, e["bbox"], exe)
            expected = _normalise_text(e["expected_text"])
            if err is not None or got is None:
                e["ocr"] = {"status": "skipped", "reason": err or "no text read"}
                continue
            read = _normalise_text(got)
            e["ocr"] = {
                "status": "pass" if read == expected else "fail",
                "expected": expected,
                "read": got,
            }
        results = [e["ocr"] for e in norm]
        if any(r.get("status") == "fail" for r in results):
            bad = next(r for r in results if r.get("status") == "fail")
            add_check(
                "ocr_text", bad.get("read"), f"== {bad.get('expected')}", "fail",
                f"tesseract read back {bad.get('read')!r}",
            )
        elif all(r.get("status") == "skipped" for r in results):
            add_check("ocr_text", None, "== expected_text", "skipped", results[0].get("reason"))
        else:
            add_check("ocr_text", "ok", "== expected_text", "pass")

    if noise_ratios:
        worst_noise = max(noise_ratios, key=lambda v: abs(math.log(max(v, 1e-6))))
        noise_metric = round(float(worst_noise), 3)
        noise_range = [
            round(float(min(noise_ratios)), 3),
            round(float(max(noise_ratios)), 3),
        ]
    else:
        noise_metric = None
        noise_range = None
    metrics = {
        "outside_max_diff": outside_max,
        "outside_changed_pixels": outside_changed,
        "boundary_step": boundary,
        "ink_delta": round(float(max(ink_deltas)), 3) if ink_deltas else None,
        "ink_chroma_delta": round(float(max(chroma_deltas)), 3) if chroma_deltas else None,
        "stroke_delta": round(float(max(stroke_deltas)), 3) if stroke_deltas else None,
        "glyph_height_delta": round(
            float(max(digit_height_deltas + cjk_height_deltas)), 3
        ) if (digit_height_deltas or cjk_height_deltas) else None,
        "digit_height_delta": round(float(max(digit_height_deltas)), 3) if digit_height_deltas else None,
        "cjk_height_delta": round(float(max(cjk_height_deltas)), 3) if cjk_height_deltas else None,
        "baseline_delta": round(float(max(base_deltas)), 3) if base_deltas else None,
        "cjk_baseline_offset": round(float(max(cjk_base_deltas)), 3) if cjk_base_deltas else None,
        "noise_ratio": noise_metric,
        "noise_ratio_range": noise_range,
        "qtables_equal": qt.get("value"),
        "qtables_status": qt["status"],
        "ocr_status": next(c["status"] for c in checks if c["name"] == "ocr_text"),
    }
    # a gate that could not compare any glyph must not report a pass
    if not ink_deltas:
        failures.append(
            "glyph metrics: no comparable glyphs in the edited or reference window"
        )
    failing = [c for c in checks if c["status"] == "fail"]
    return {
        "pass": not failing and not failures,
        "source": str(source_path),
        "output": str(output_path),
        "metrics": metrics,
        "checks": checks,
        "failures": failures,
        "skipped": skipped,
        "per_edit": per_edit,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _parse_csv(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(round(float(v))) for v in value.replace(";", ",").split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma separated integers") from exc


def _summary(result: dict) -> str:
    lines = ["QA gate: " + ("PASS" if result["pass"] else "FAIL")]
    lines.append(f"  source  {result.get('source')}")
    lines.append(f"  output  {result.get('output')}")
    lines.append("  " + "-" * 74)
    for c in result["checks"]:
        mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}[c["status"]]
        val = "" if c["value"] is None else f" = {c['value']}"
        detail = f"   [{c['detail']}]" if c.get("detail") else ""
        lines.append(f"  {mark:4s} {c['name']:<22s} {str(c['threshold']):<14s}{val}{detail}")
    if result["failures"]:
        lines.append("  failures:")
        lines.extend(f"    - {f}" for f in result["failures"])
    if result["skipped"]:
        lines.append("  skipped:")
        lines.extend(f"    - {s['name']}: {s['reason']}" for s in result["skipped"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qa_gate.py",
        description=(
            "Objective acceptance gate for scan text revision: untouched pixels, "
            "seam step, ink colour, stroke weight, glyph height, baseline, noise, "
            "JPEG quantisation tables and OCR read-back."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "edits JSON format:\n"
            '  [{"bbox": [x, y, w, h], "expected_text": "10月13日",\n'
            '    "region": [x0, y0, x1, y1]}]\n'
        ),
    )
    parser.add_argument("--source", required=True, help="original image")
    parser.add_argument("--output", required=True, help="revised image")
    parser.add_argument("--edits", help="JSON file with the edit list")
    parser.add_argument("--bbox", type=_parse_csv, help="single edit: x,y,w,h")
    parser.add_argument("--region", type=_parse_csv, help="single edit: x0,y0,x1,y1")
    parser.add_argument("--expected-text", default="", help="single edit: expected text")
    parser.add_argument("--json-out", help="also write the full result JSON here")
    parser.add_argument("--quiet", action="store_true", help="do not print the human summary to stderr")
    args = parser.parse_args(argv)

    if args.edits:
        edits = json.loads(Path(args.edits).read_text(encoding="utf-8"))
        if isinstance(edits, dict) and "edits" in edits:
            edits = edits["edits"]
        if not isinstance(edits, list) or not edits:
            parser.error("--edits must contain a non-empty JSON array")
    else:
        if not args.bbox or not args.region:
            parser.error("provide --edits or both --bbox and --region")
        edits = [
            {
                "bbox": list(args.bbox),
                "region": list(args.region),
                "expected_text": args.expected_text,
            }
        ]

    try:
        result = run_gate(args.source, args.output, edits)
    except (ValueError, OSError) as exc:
        print(json.dumps({"pass": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2

    if not args.quiet:
        print(_summary(result), file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
