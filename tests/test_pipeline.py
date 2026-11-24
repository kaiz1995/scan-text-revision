"""End-to-end regression tests on synthetic scans.

Each sample is a light-paper / dark-ink line drawn with a real CJK font,
supersampled, blurred, noised and JPEG-encoded, so it behaves like a 200 DPI
scan.  The line carries untouched neighbour glyphs ("签发日期") so the gate has
reference glyphs to compare against.

Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import glyph_engine  # noqa: E402

FONT_NAMES = ("simsun.ttc", "NotoSerifCJK-Regular.ttc", "NotoSerifCJKsc-Regular.otf", "Songti.ttc")
FONT_DIRS = [
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
    Path("/usr/share/fonts"),
    Path("/usr/local/share/fonts"),
    Path("/System/Library/Fonts"),
    Path("/Library/Fonts"),
]


def find_font() -> str | None:
    for name in FONT_NAMES:
        for root in FONT_DIRS:
            if not root.is_dir():
                continue
            direct = root / name
            if direct.is_file():
                return str(direct)
            hits = list(root.rglob(name))
            if hits:
                return str(hits[0])
    return None


FONT = find_font()
PREFIX = "签发日期"
DATE = "2025年07月30日"
NEW_DATE = "2025年08月15日"
INK = np.array([38.0, 36.0, 42.0], np.float32)


def make_scan(path: Path, size_px: int, text: str = PREFIX + DATE, invert: bool = False) -> tuple[int, int, int, int]:
    """Draw the sample and return the tight bbox (x, y, w, h) of the date part."""
    rng = np.random.default_rng(11)
    width, height, s = 60 + size_px * 16, size_px * 4, 4
    big = Image.new("L", (width * s, height * s), 0)
    draw = ImageDraw.Draw(big)
    font = ImageFont.truetype(FONT, size_px * s)
    base_x, base_y = 30 * s, int(size_px * 2.6) * s
    draw.text((base_x, base_y), text, font=font, fill=255, anchor="ls")
    prefix_w = font.getlength(PREFIX) if text.startswith(PREFIX) else 0.0
    date_part = text[len(PREFIX):] if text.startswith(PREFIX) else text
    l, t, r, b = font.getbbox(date_part, anchor="ls")
    alpha = np.asarray(big.resize((width, height), Image.LANCZOS), np.float32) / 255.0
    alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8)),
                       np.float32) / 255.0
    yy, xx = np.mgrid[0:height, 0:width]
    paper = np.stack([238 + 0.004 * xx, 234 + 0.003 * xx, 224 + 0.01 * yy], -1).astype(np.float32)
    rgb = paper * (1 - alpha[..., None]) + INK * alpha[..., None] + rng.normal(0, 2.0, (height, width, 3))
    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
    if invert:
        rgb = 255 - rgb
    Image.fromarray(rgb).save(path, quality=88)
    # prefix_w and the bbox come from the supersampled font, so they are already in big-canvas px
    x0 = int((base_x + prefix_w + l) / s) - 3
    y0 = int((base_y + t) / s) - 3
    x1 = int(np.ceil((base_x + prefix_w + r) / s)) + 3
    y1 = int(np.ceil((base_y + b) / s)) + 3
    return x0, y0, x1 - x0, y1 - y0


def run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8", env=env)


@unittest.skipUnless(FONT, "no CJK serif font (simsun / Noto Serif CJK) installed")
class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- measurement ------------------------------------------------------
    def test_dark_ink_is_measured_as_ink(self) -> None:
        src = self.tmp / "dark.jpg"
        x, y, w, h = make_scan(src, 46)
        m = glyph_engine.measure(np.asarray(Image.open(src).convert("RGB")), (x, y, x + w, y + h))
        self.assertEqual(m.get("polarity"), -1)
        self.assertLess(float(np.abs(np.asarray(m["ink_rgb"]) - INK).max()), 15.0)
        self.assertGreater(float(np.median(m["lum_bg"])), 200.0)

    def test_light_ink_still_supported(self) -> None:
        src = self.tmp / "light.jpg"
        x, y, w, h = make_scan(src, 46, invert=True)
        m = glyph_engine.measure(np.asarray(Image.open(src).convert("RGB")), (x, y, x + w, y + h))
        self.assertEqual(m.get("polarity"), 1)
        self.assertGreater(float(np.mean(m["ink_rgb"])), 180.0)

    # -- render + gate + export -------------------------------------------
    def _render(self, src: Path, bbox, page_text: str, text: str, name: str = "cand") -> dict:
        cand, mask = self.tmp / f"{name}.png", self.tmp / f"{name}_mask.png"
        r = run(str(SCRIPTS / "render_candidates.py"), str(src), "--bbox", ",".join(map(str, bbox)),
                "--page-text", page_text, "--text", text, "--output", str(cand), "--mask", str(mask))
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        info["_cand"], info["_mask"] = cand, mask
        return info

    def _export(self, src: Path, info: dict, bbox, page_text: str, text: str) -> dict:
        log = self.tmp / "edit-log.json"
        log.write_text(json.dumps({"edits": [{
            "page": 1, "bbox": list(bbox), "original_text": page_text, "replacement_text": text,
            "approved": True, "candidate": {k: v for k, v in info.items() if not k.startswith("_")},
        }]}, ensure_ascii=False), encoding="utf-8")
        out = self.tmp / "out"
        r = run(str(SCRIPTS / "export_revision.py"), "--source", str(src), "--revised", str(info["_cand"]),
                "--output-dir", str(out), "--edit-log", str(log), "--mask", str(info["_mask"]))
        qa = json.loads((out / "qa-report.json").read_text(encoding="utf-8"))
        qa["_rc"], qa["_stderr"] = r.returncode, r.stderr
        return qa

    def test_date_edit_passes_gate(self) -> None:
        src = self.tmp / "scan.jpg"
        bbox = make_scan(src, 46)
        info = self._render(src, bbox, DATE, NEW_DATE)
        self.assertEqual(info.get("segmentation"), "components")
        for ch in "205年月日":
            self.assertIn(ch, info["glyphs_borrowed"])
        qa = self._export(src, info, bbox, DATE, NEW_DATE)
        failures = qa.get("failures") or qa.get("gate", {}).get("failures")
        self.assertEqual(qa["_rc"], 0, f"{failures}\n{qa['_stderr']}")

    def test_small_text_identifies_font_size(self) -> None:
        src = self.tmp / "small.jpg"
        bbox = make_scan(src, 29)
        info = self._render(src, bbox, DATE, NEW_DATE)
        size = (info.get("measurement") or {}).get("font_size")
        self.assertIsNotNone(size)
        self.assertLess(abs(float(size) - 29.0), 1.5)

    def test_spaces_in_page_text_still_borrow(self) -> None:
        src = self.tmp / "spaced.jpg"
        line = "2025 年 07 月"
        bbox = make_scan(src, 46, text=PREFIX + line)
        info = self._render(src, bbox, line, "2025 年 08 月")
        self.assertEqual(info.get("segmentation"), "components")
        self.assertIn("年", info["glyphs_borrowed"])

    def test_loose_left_edge_does_not_shift_text(self) -> None:
        src = self.tmp / "pen.jpg"
        x, y, w, h = make_scan(src, 46)
        profiles = []
        for name, box in (("tight", (x, y, w, h)), ("loose", (x - 15, y, w + 15, h))):
            info = self._render(src, box, DATE, NEW_DATE, name=name)
            after = np.asarray(Image.open(info["_cand"]).convert("L"), np.float64)
            # columns >= x hold only the date line (the prefix ends before the 3 px margin)
            profiles.append((255.0 - after[y:y + h, x:x + w]).sum(axis=0))
        tight, loose = (p - p.mean() for p in profiles)
        shifts = range(-25, 26)
        scores = [float(np.dot(tight[max(0, s):len(tight) + min(0, s)],
                               loose[max(0, -s):len(loose) - max(0, s)])) for s in shifts]
        self.assertLessEqual(abs(list(shifts)[int(np.argmax(scores))]), 1)

    def test_one_digit_edit_is_measured(self) -> None:
        src = self.tmp / "single.jpg"
        x, y, w, h = make_scan(src, 46)
        info = self._render(src, (x, y, w, h), DATE, "2025年07月31日")
        qa = self._export(src, info, (x, y, w, h), DATE, "2025年07月31日")
        names = {c["name"]: c["status"] for c in qa.get("checks", [])}
        self.assertNotEqual(names.get("ink_delta"), "skipped")
        self.assertEqual(qa["_rc"], 0, f"{qa.get('failures')}\n{qa['_stderr']}")

    def test_default_font_is_found(self) -> None:
        # the synthesis / --legacy fallback font comes from the engine's cross-platform font folders
        import render_candidates
        path = render_candidates.default_font_path()
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).is_file())

    def test_borrowed_glyphs_use_real_side_bearing(self) -> None:
        # getbbox(anchor="ls")[0] is 0 for every glyph under raqm; simsun's "日" really
        # starts ~0.2 em right of the pen.  Using 0 shifts borrowed glyphs left and
        # opens a gap before them (seen as "月  15日" with 日 read back as "H").
        bearing = glyph_engine._ink_bearing(FONT, 46.0, "日")
        self.assertGreater(bearing, 0.1 * 46.0)
        self.assertLess(bearing, 0.35 * 46.0)


class CliSmokeTest(unittest.TestCase):
    def test_help(self) -> None:
        for script in ("glyph_engine.py", "qa_gate.py", "render_candidates.py", "export_revision.py", "inspect_scan.py"):
            r = run(str(SCRIPTS / script), "--help")
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
