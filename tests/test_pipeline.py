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

FONT_NAMES = ("simsun.ttc", "NotoSerifCJK-Regular.ttc", "Songti.ttc")


def find_font() -> str | None:
    """A serif CJK font from the same system and user font folders the engine searches."""
    found = {Path(p).name.lower(): p for p in glyph_engine._font_candidates()}
    return next((found[n.lower()] for n in FONT_NAMES if n.lower() in found), None)


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
    def _render(self, src: Path, bbox, page_text: str, text: str, *extra: str, name: str = "cand") -> dict:
        cand, mask = self.tmp / f"{name}.png", self.tmp / f"{name}_mask.png"
        r = run(str(SCRIPTS / "render_candidates.py"), str(src), "--bbox", ",".join(map(str, bbox)),
                "--page-text", page_text, "--text", text, "--output", str(cand), "--mask", str(mask), *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        info["_cand"], info["_mask"] = cand, mask
        return info

    def _export(self, src: Path, info: dict, bbox, page_text: str, text: str, page: int = 1) -> dict:
        log = self.tmp / "edit-log.json"
        log.write_text(json.dumps({"edits": [{
            "page": page, "bbox": list(bbox), "original_text": page_text, "replacement_text": text,
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

    def test_line_starting_with_a_later_glyph_stays_at_line_start(self) -> None:
        # "07月30日" borrows its "0" from the middle of "2025年..."; the line must still start at
        # the original left ink edge, not where that "0" sat in the source line
        src = self.tmp / "later.jpg"
        x, y, w, h = make_scan(src, 46)
        info = self._render(src, (x, y, w, h), DATE, "07月30日")
        self.assertIn("0", info["glyphs_borrowed"])

        def first_ink(path) -> int:
            g = np.asarray(Image.open(path).convert("L"), np.float64)[y:y + h, x:x + w]
            return int(np.where((g < 140).any(axis=0))[0].min())

        self.assertLessEqual(abs(first_ink(info["_cand"]) - first_ink(src)), 2)

    def _band(self, extra: int) -> tuple[Path, tuple[int, int, int, int]]:
        """Light text on a dark band that reaches ``extra`` px past the box, on white paper."""
        band = self.tmp / "band_src.jpg"
        x, y, w, h = make_scan(band, 46, invert=True)
        dark = np.asarray(Image.open(band).convert("RGB"))
        page = np.full((dark.shape[0] + 80, dark.shape[1], 3), 245, np.uint8)
        top, bottom = y - extra, y + h + extra
        page[40 + top:40 + bottom] = dark[top:bottom]
        src = self.tmp / f"band{extra}.png"
        Image.fromarray(page).save(src)
        return src, (x, y + 40, w, h)

    def test_light_text_on_a_dark_band_keeps_its_polarity(self) -> None:
        # the re-measure margin reaches the white paper and must not flip the polarity
        src, box = self._band(6)
        info = self._render(src, box, DATE, NEW_DATE)
        self.assertEqual((info.get("measurement") or {}).get("polarity"), 1)

    def test_tight_band_moves_a_later_glyph_to_line_start(self) -> None:
        # with almost no margin the borrowed "0" moves to the line start; only its empty
        # matte padding leaves the box, which must not count as an overrun
        src, box = self._band(2)
        self._render(src, box, DATE, "07月30日")

    def test_replay_with_font_keeps_source_spacing(self) -> None:
        # with a font loaded the borrowed advance must not add the side-bearing difference again
        src = self.tmp / "replay.jpg"
        x, y, w, h = make_scan(src, 46)
        info = self._render(src, (x, y, w, h), DATE, DATE, "--font", FONT)

        def last_ink(path) -> int:
            g = np.asarray(Image.open(path).convert("L"), np.float64)[y:y + h, x:x + w + 40]
            return int(np.where((g < 140).any(axis=0))[0].max())

        self.assertLessEqual(abs(last_ink(info["_cand"]) - last_ink(src)), 1)

    def test_given_font_is_used_and_reported(self) -> None:
        others = [p for p in glyph_engine._font_candidates() if Path(p).name.lower() != Path(FONT).name.lower()]
        if not others:
            self.skipTest("only one CJK font installed")
        src = self.tmp / "given.jpg"
        line = "2025 年 07 月"
        bbox = make_scan(src, 46, text=PREFIX + line)
        # dropping the blanks needs the font even though every glyph is borrowed
        info = self._render(src, bbox, line, "2025年07月", "--font", others[0])
        self.assertEqual(Path(info["font"]).name.lower(), Path(others[0]).name.lower())

    def test_advance_fit_counts_source_blanks(self) -> None:
        import render_candidates
        src = self.tmp / "fit.jpg"
        line = "2025 年 07 月"
        x, y, w, h = make_scan(src, 46, text=PREFIX + line)
        img = np.asarray(Image.open(src).convert("RGB"))
        m = glyph_engine.measure(img, (x, y, x + w, y + h))
        boxes, _note, seg = render_candidates.borrow_boxes(m, line, glyph_engine)
        self.assertEqual(seg, "components")
        space_after = [i + 1 < len(line) and line[i + 1].isspace() for i, c in enumerate(line) if not c.isspace()]
        lib = glyph_engine.build_glyph_library(img, m, boxes, space_after=space_after)
        fit = glyph_engine._advance_fit(FONT, lib)
        self.assertIsNotNone(fit)
        self.assertLess(abs(min(fit, key=fit.get) - 46.0), 1.0)

    def test_space_after_a_glyph_that_had_none_is_kept(self) -> None:
        # "5" occurs twice in the source, once before a blank and once glued to "月"; a blank typed
        # after the second one is not inside its advance and must be rendered
        src = self.tmp / "space.jpg"
        line = "2025 年 5月"
        x, y, w, h = make_scan(src, 46, text=PREFIX + line)
        info = self._render(src, (x, y, w, h), line, "2025 年 5 月")
        self.assertEqual(info.get("segmentation"), "components")

        def last_gap(path) -> int:
            m = glyph_engine.measure(np.asarray(Image.open(path).convert("RGB")), (x, y, x + w + 60, y + h))
            merged: list[list[int]] = []
            for b in sorted((list(g["box"]) for g in m["glyphs"] if g["area"] >= 12), key=lambda b: b[0]):
                if merged and b[0] <= merged[-1][0] + merged[-1][2]:
                    merged[-1][2] = max(merged[-1][0] + merged[-1][2], b[0] + b[2]) - merged[-1][0]
                else:
                    merged.append(b)
            five, moon = merged[-2], merged[-1]
            return moon[0] - (five[0] + five[2])

        self.assertGreater(last_gap(info["_cand"]) - last_gap(src), 0.3 * 46)

    def test_non_integer_page_blocks_export_and_is_recorded(self) -> None:
        src = self.tmp / "badpage.jpg"
        bbox = make_scan(src, 46)
        info = self._render(src, bbox, DATE, NEW_DATE)
        qa = self._export(src, info, bbox, DATE, NEW_DATE, page="abc")
        self.assertNotEqual(qa["_rc"], 0)
        manifest = (self.tmp / "out" / "manifest.json").read_text(encoding="utf-8")
        self.assertIn('"abc"', manifest)

    def test_edit_on_a_missing_page_blocks_export(self) -> None:
        src = self.tmp / "page.jpg"
        bbox = make_scan(src, 46)
        info = self._render(src, bbox, DATE, NEW_DATE)
        qa = self._export(src, info, bbox, DATE, NEW_DATE, page=3)
        self.assertNotEqual(qa["_rc"], 0, "an edit logged on a page that does not exist must not pass")

    def test_ink_fallback_accepts_a_background_surface(self) -> None:
        # no usable glyph core: the ink is derived from the (H, W, 3) background surface and lies
        # between the paper and the extreme on the measured side
        core = np.zeros((6, 8), bool)
        light = np.full((6, 8, 3), 236.0, np.float32)
        _, level = glyph_engine._ink_vector(light, core, light[..., 0], -1, light.copy())
        self.assertLess(level, 236.0)
        dark = np.full((6, 8, 3), 30.0, np.float32)
        _, level = glyph_engine._ink_vector(dark, core, dark[..., 0], 1, dark.copy())
        self.assertGreater(level, 30.0)
        _, level = glyph_engine._ink_vector(light, core, light[..., 0], 1, None)
        self.assertLess(level, 200.0)
        _, level = glyph_engine._ink_vector(light, core, light[..., 0], -1, None)
        self.assertGreater(level, 100.0)

    def test_ocr_summary_never_reports_skipped_as_pass(self) -> None:
        import qa_gate
        skipped = {"status": "skipped", "reason": "未安装 chi_sim 语言包"}
        self.assertEqual(qa_gate._ocr_summary([{"status": "pass"}, skipped])[2], "skipped")
        failed = {"status": "fail", "read": "x", "expected": "y"}
        self.assertEqual(qa_gate._ocr_summary([skipped, failed])[2], "fail")
        self.assertEqual(qa_gate._ocr_summary([{"status": "pass"}])[2], "pass")

    def test_gate_passes_the_untouched_source(self) -> None:
        # the reference for ink is the replaced glyphs, so the source scored against itself is clean
        import qa_gate
        src = self.tmp / "self.jpg"
        x, y, w, h = make_scan(src, 46)
        res = qa_gate.run_gate(str(src), str(src), [{"bbox": (x, y, w, h), "region": (x, y, x + w, y + h),
                                                     "expected_text": DATE}])
        self.assertLess(res["metrics"]["ink_delta"], 0.5)
        self.assertLess(res["metrics"]["ink_chroma_delta"], 0.5)

    def test_ocr_vote_needs_a_majority(self) -> None:
        import qa_gate
        self.assertEqual(qa_gate._ocr_vote(["2025年", "2025征", "2025年"], "2025年"), "2025年")
        self.assertEqual(qa_gate._ocr_vote(["2025征", "2025征", "2025年"], "2025年"), "2025征")

    def test_replay_keeps_each_glyph_tone(self) -> None:
        # rendering the source line from its own mattes must not re-tone any glyph
        import qa_gate
        src = self.tmp / "tone.jpg"
        x, y, w, h = make_scan(src, 46)
        info = self._render(src, (x, y, w, h), DATE, DATE)
        s = np.asarray(Image.open(src).convert("RGB"))
        o = np.asarray(Image.open(info["_cand"]).convert("RGB"))
        win = (0, max(0, y - 8), s.shape[1], min(s.shape[0], y + h + 8))
        box = (x, y, x + w, y + h)
        tones = []
        for img, pol in ((s, None), (o, -1)):
            m = glyph_engine.measure(img, win, polarity=pol)
            pool = qa_gate._split_wide(img, win, m, qa_gate._glyph_pool(m))
            tones.append([float(g.get("ink_tone", g["ink_lum"])) for g in
                          sorted((g for g in pool if qa_gate._centre_inside(g, box)), key=lambda g: g["box"][0])])
        self.assertEqual(len(tones[0]), len(tones[1]))
        self.assertLess(max(abs(a - b) for a, b in zip(*tones)), 3.0)

    def test_gate_tells_digits_from_cjk(self) -> None:
        import qa_gate
        src = self.tmp / "kinds.jpg"
        x, y, w, h = make_scan(src, 46)
        s = np.asarray(Image.open(src).convert("RGB"))
        win = (0, max(0, y - 8), s.shape[1], min(s.shape[0], y + h + 8))
        m = glyph_engine.measure(s, win)
        pool = qa_gate._classify(qa_gate._split_wide(s, win, m, qa_gate._glyph_pool(m)))
        inside = sorted((g for g in pool if qa_gate._centre_inside(g, (x, y, x + w, y + h))), key=lambda g: g["box"][0])
        kinds = "".join("d" if g["kind"] == "digit" else "c" for g in inside)
        self.assertEqual(kinds, "ddddcddcddc")  # 2025年07月30日

    def test_borrowed_glyphs_after_a_synthesized_one_keep_their_tone(self) -> None:
        # a synthetic "8" has a fractional advance; the borrowed glyphs behind it must still be
        # placed on whole pixels, not re-interpolated ("月"/"日" came out 4-6 levels lighter)
        import qa_gate
        src = self.tmp / "after.jpg"
        x, y, w, h = make_scan(src, 46)
        info = self._render(src, (x, y, w, h), DATE, NEW_DATE)
        s = np.asarray(Image.open(src).convert("RGB"))
        o = np.asarray(Image.open(info["_cand"]).convert("RGB"))
        win = (0, max(0, y - 8), s.shape[1], min(s.shape[0], y + h + 8))
        box = (x, y, x + w, y + h)

        def tones(img, pol):
            m = glyph_engine.measure(img, win, polarity=pol)
            pool = qa_gate._classify(qa_gate._split_wide(img, win, m, qa_gate._glyph_pool(m)))
            return [float(g["ink_tone"]) for g in sorted(
                (g for g in pool if qa_gate._centre_inside(g, box) and g["kind"] != "fragment"),
                key=lambda g: g["box"][0])]

        src_t, out_t = tones(s, None), tones(o, -1)
        self.assertEqual(len(out_t), len(NEW_DATE))
        by_char: dict[str, list[float]] = {}
        for ch, v in zip(DATE, src_t):
            by_char.setdefault(ch, []).append(v)
        for ch, v in zip(NEW_DATE, out_t):
            if ch in by_char:
                self.assertLess(abs(v - float(np.mean(by_char[ch]))), 3.0, ch)

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
