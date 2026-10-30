# Quality Checks

`scripts/qa_gate.py` is the acceptance gate. It runs automatically inside `export_revision.py` and can be run standalone before export:

```bash
python scripts/qa_gate.py --source scan.png --output cand.png \
  --edits edits.json --json-out gate.json
```

Each edit is `{"bbox": [x, y, w, h], "expected_text": "...", "region": [x0, y0, x1, y1]}`: `bbox` is where the new text lives, `region` is everything the edit is allowed to touch. `bbox` must lie inside `region`.

## Class-aware glyph comparison

Digits sit on the baseline; CJK glyphs overshoot it by a few px; a slightly tilted line lifts the baseline several px across an edit. Comparing a glyph bottom with "the neighbour baseline at one point" mixes those three effects, so the gate:

- splits components that glued two glyphs together (e.g. `3` + `日`) at the valley of the column-density profile;
- classifies glyphs as `digit` or `cjk` by the height bimodality (falling back to the width/height ratio);
- fits `y = a + b·x` through the **source digits** and compares **each new digit's** bottom with that line at its own x — tilt aware;
- compares CJK glyphs only with **shape-matched** source CJK glyphs, via their ink-top offset from the same fitted baseline.

## Automatic checks

Thresholds come from `qa_gate.THRESHOLDS`.

| Check | Algorithm | Threshold / status |
| --- | --- | --- |
| `outside_max_diff` | per-pixel max channel difference outside every declared region | `== 0` |
| `outside_changed_pixels` | count of changed pixels outside the regions | `== 0` |
| `boundary_step` | max second-difference excess of column/row means at the four region seams | `< 1.0`; `skipped` when no glyph-free seam rows/columns exist |
| `ink_delta` | median luminance of the glyph core — perceived tone; the top decile is JPEG ringing, which a rebuilt glyph does not carry, so comparing it rewards over-bright text — vs intact neighbouring glyphs | `< 3` levels; `skipped` with fewer than 2 glyphs in a window |
| `ink_chroma_delta` | area-weighted ink RGB converted to CIELAB, ΔE76 vs intact neighbouring glyphs | `< 3`; the only check that sees a per-channel colour cast |
| `stroke_delta` | stroke width (distance-transform median × 2) vs neighbours | `< 0.5 px`; same skip rule |
| `glyph_height_delta` | max of digit height (new digits vs source digits) and CJK height (shape-matched pairs, bbox height difference) | `< 1 px` |
| `baseline_delta` | source-digit baseline `a + b·x`, median residual of every new digit's bottom | `< 1 px`; `skipped` without ≥3 source digits and new digits |
| `cjk_baseline_offset` | new CJK ink-top minus shape-matched source CJK ink-top, both measured from the fitted baseline | `< 1 px`; `skipped` without a shape-matched CJK pair or a digit baseline (never failed for lack of a reference) |
| `noise_ratio` | high-pass std of the rebuilt background over the neighbouring grain | `0.7 – 1.4` |
| `qtables_equal` | output JPEG quantisation tables vs the source's | equal; `skipped` when the source has none, or the output is lossless without a sibling JPEG |
| `ocr_text` | tesseract read-back of `bbox` vs `expected_text` (spaces stripped) | equal; `skipped` when tesseract is missing or the call fails (`chi_sim+eng`, then `eng`, `--psm 7`); an empty read against a non-empty expectation is a `fail` |

## How to read the result

- `pass` is `true` only when no check `fail`ed and no extra `failure` was appended.
- `skipped` is a real third state. Without tesseract the OCR check is `skipped`: it does **not** fail the gate and does **not** count as verified — never describe a skipped OCR as "text confirmed readable". The same applies to `qtables_equal` on lossless output with no sibling JPEG, and to `baseline_delta` / `cjk_baseline_offset` when the line offers nothing to compare.
- The gate refuses to pass by default: if it cannot compare any glyph ink (no `ink_delta`), it appends `glyph metrics: no comparable glyphs ...` and the result is a failure.
- Use the JSON, not the human summary, when reporting to the user: `checks[]` carries name/threshold/value/status, `skipped[]` carries reasons, `per_edit[]` carries the per-region numbers (`digit_height_delta`, `cjk_height_delta`, `baseline_fit`, `digit_residuals`, `cjk_pairs`, `noise_ratio`, …).

## What "pixels outside the edit are unchanged" actually covers

- **Lossless PNG layer** (`revised-sample.png`, or `revised-sample.<ext>` for a lossless source): identical to the decoded source outside the declared regions, `outside_max_diff == 0`. This is what the gate measures.
- **JPEG deliverable** (`revised-sample.jpg`): the source quantisation tables, chroma subsampling, progressive flag and DPI are reused, but unchanged areas still go through one re-quantisation — measured `mean|diff| ≈ 0.000`, worst single channel in the 6–8 range depending on the source tables. Do not claim bit-exactness for this file.
- **PDF**: with `pikepdf`, only the page image XObject is replaced (`DCTDecode`, source tables when present), page size and other objects untouched; without `pikepdf` the export is `lossy_fallback` (Pillow, every page re-encoded at 200 DPI) and the lossless claim does not apply.

All three assume the candidate was rendered from the same file handed to `--source`. Rendering from a re-encoded copy (PNG converted to JPEG, a resized page, a re-saved PDF raster) moves every untouched pixel and `outside_max_diff` fails — correctly.

## Still manual

The gate cannot see these; check them by eye or by reopening the file:

- page count, page order, orientation and colour mode of a multi-page export;
- whether the PDF reopens and rasterises;
- whether the new glyphs are legible when the OCR check was `skipped`;
- 200% inspection for halo, repeated grain, hard edges or an over-sharp replacement;
- paper grain and watermark-like texture continuity across the seam (only the aggregate `noise_ratio` is automated);
- whether `manifest.json` hashes and the edit count agree with the files on disk;
- on any failed check: keep the source, report `qa_failed` / needs review, never present the candidate as finished.

There are no candidate scores: `qa_gate` measures one candidate, it does not rank several. Comparison between variants is a human task.
