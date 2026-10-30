# Workflow

## Inspect

`scripts/inspect_scan.py` is read-only. It records:

- `sha256` and `format` (from the file suffix);
- for PDFs: the parsed `pdfinfo` fields (when Poppler is installed), `embedded_fonts` (whether `pdffonts` lists any), `text_layer_chars` (character count from `pdftotext`) and `scan_only_hint` (that count is zero);
- for rasters: `size`, `mode`, `format`, and the `dpi` **metadata** tag when the file carries one.

PDF pages are rasterised only on request (`--render-dir`) with `pdftoppm -png -r 200`. The 200 DPI is a fixed constant in the script, not a measurement, and there is no "preserve 200+ DPI" logic anywhere. `inspect_scan.py` performs no layout analysis: it cannot identify text blocks, table rules, watermarks, stamps or signatures, and it produces no `confidence` or `editable` fields. Those belong to the Roadmap (see the end of this file).

## Measure

Everything the replacement needs is measured from the selected line itself (`glyph_engine.measure`):

- `bg` — per-channel quadratic background surface, least-squares fitted on ink-free pixels only;
- `alpha` — glyph coverage `clip((L − L_bg) / (L_ink − L_bg), 0, 1)`;
- `ink_rgb` / `ink_lum` — full ink colour, taken as the **99th percentile** of glyph-core pixels (`alpha > 0.85`, falling back to `0.6`); the median is 5–8 levels darker and makes a rewritten run look lower-contrast;
- `stroke_px` — median stroke width over glyph components (distance transform median × 2);
- `baseline_a` / `baseline_b` — robustly trimmed least-squares fit through the dominant glyph-bottom cluster (CJK bottoms overshoot digits by a few px);
- `sigma_psf` — blur estimate from the steepest alpha 0.5 crossings (`σ = 0.3989 / max|dA/dx|`, clipped to 0.3–4.0);
- `noise_std`, `noise_patch`, `noise_field` — residual noise outside the glyphs, grain-matched and tiled with the JPEG 8 px block phase preserved.

`measure()` raises `ValueError` when the box contains no ink; that is the signal that the box is wrong, not a reason to loosen a threshold.

## Select

The user supplies page, `bbox` (`x,y,w,h`), the original line text, and the replacement text. `scripts/select_box.py` + `selector.html` provide a single-box canvas (pixel readout, crop preview, copy `--bbox ... --text ...`, submit); coordinates can also be typed.

Constraints the code actually enforces:

- `render_candidates.py` rejects a box that is not a positive-size rectangle inside the image, and rejects empty replacement text;
- `qa_gate` rejects a `bbox` that is not fully inside its `region`, a region smaller than 4×4, or a bbox smaller than 4×4;
- `export_revision.py` rejects an edit log whose entries are not all `approved: true` or whose bbox cannot be parsed.

Keep the box tight: the erase domain is the selected box, so a box that includes a table rule, a seal edge or a neighbouring glyph repaints it. OCR is never used to locate or choose a region; the only OCR in the codebase is the optional read-back check in `qa_gate`.

## Candidate generation

The default path is one candidate per invocation:

1. `measure` the selected box, then re-measure with a 24 px margin so borrowed mattes (which carry 9 px of context) and a first glyph sitting slightly left of its ink origin still fit. The background is still erased only inside the selected box.
2. `build_glyph_library` — with `--page-text`, characters are paired with measured components (by horizontal overlap when OpenCV is present; otherwise the ink span is split evenly, which can mis-assign glyphs). Each matte is clipped to its own connected component dilated 6 px, normalised by its own full-ink level, and carries its measured advance.
3. `render` — typeset on the measured baseline `y = a + b·(x − x0)`. Borrowed glyphs keep the source line's advance and are shifted by `dy = b·dx`; missing glyphs are rendered from the identified font at 4× supersampling with calibrated stroke dilation and σ. `--sigma` / `--weight` override the measured values, `--font` forces a font file.
4. `compose` — `out = B + A·(I_ink − B)` inside a mask covering the selected box plus the new glyph span. Old ink is removed first by replacing the selected box with fitted background + borrowed residual noise. One full-ink colour serves the whole patch (area-weighted mean of the borrowed cores); `--weight` and `--sigma` change the synthetic glyphs, not the borrowed ones.

There is no automatic candidate scoring and no "up to three candidates": the JSON has `status: candidate` and no score field. To compare variants, change `--sigma`, `--weight`, `--font`, or `--text` and run again, then judge side by side. Save candidates as PNG; a `.jpg` candidate is re-encoded at quality 95 before export.

`--legacy` keeps the original fallback: whole-crop `cv2.inpaint` (or a Gaussian blur when OpenCV is missing), font shrunk until the text fits the box, fixed ink colour `(35,35,35)`, text centred in the box, `--blur` (default 0.25) applied to the ink layer. It measures nothing.

## Approval and export

Show the original, the mask, the candidate and a 200% comparison. Only an explicit `"approved": true` in the edit record makes an edit exportable. Put the render JSON under the edit's `candidate` (or `render`) key so `manifest.json` can record borrowed vs synthesised glyphs; without it the manifest says `unknown` and requires manual attribution.

`python scripts/export_revision.py ...` always runs `qa_gate`:

- images: the gate compares the source against the **lossless PNG layer**; the JPEG deliverable reuses the source quantisation tables, subsampling, progressive flag and DPI;
- PDFs: the page image XObject is replaced via `pikepdf`; without `pikepdf` the PDF is rebuilt by Pillow (`lossy_fallback`) and the source pages cannot be decoded for the gate unless `--source-pages` is supplied;
- pass `--mask` from `render_candidates.py`, otherwise the gate only allows changes inside `bbox` and a longer replacement will fail `outside_max_diff`;
- the `expected_text` used for OCR read-back comes from `replacement_text` / `text` in the edit record;
- the candidate must be rendered from **the same file** passed as `--source`. Re-encoding the source first (for example converting the PNG to JPEG and rendering from that copy) shifts every untouched pixel, and `outside_max_diff` fails even though the gate is right.

A failed gate still writes `qa-report.json`, `manifest.json` and `review-report.md` (with `status: qa_failed`) and exits non-zero. Keep the source unchanged; the exporter copies it into the output directory as `original-reference.*`.

## Cloud fallback

For complex texture, `scripts/cloud_inpaint.py` uploads one crop PNG plus its mask to an OpenAI-compatible `/v1/images/edits` endpoint and returns `n` candidate PNG crops. It is a manual step, not an automatic route:

- the adapter does **not** compare unmasked pixels, does not detect drift, and does not composite the result into the page — there is no script for that step;
- it pads small crops to at least 832 px on both edges (rounded up to a multiple of 16), and resizes a returned image back to the padded canvas with LANCZOS before cropping;
- the only automated pixel check remains `qa_gate` at export, after the candidate has been placed on the page by the caller.

See `references/cloud-image-adapter.md` for the exact interface and record fields.

## Roadmap / 未实现

以下能力曾被文档承诺，但当前代码里没有实现；在实现并接入门禁之前，不要当作可用特性引用：

1. **页面分析层 / 版面分析**：文本块、表格线、水印、印章、签名识别，以及区域的 `confidence`、`editable` 属性。`inspect_scan.py` 只输出文件哈希、格式、PDF 元数据/字体/文本层统计、栅格尺寸与 DPI 元数据，不做版面分析。
2. **多候选与自动评分**：旧文档的「最多生成三个候选」「Automatic scores rank candidates」没有对应代码。`render_candidates.py` 每次调用只产出一个候选，JSON 里只有 `status: candidate`，没有 score 字段；需要多个版本时用不同 `--sigma` / `--weight` / `--font` / `--text` 多次运行并人工比较。
3. **云端漂移检测与回填**：`cloud_inpaint.py` 不比较未掩膜区域，也没有把云候选贴回页面的代码。唯一的自动像素比对是导出时的 `qa_gate`。
4. **真实 DPI 推断**：`inspect_scan.py` 固定 `pdftoppm -r 200`；栅格图只读取元数据里的 `dpi` 字段，不做测量，也没有「保留 200+ DPI」的自动逻辑。
5. **Alpha 软羽化贴回**：旧文档描述的「羽化粘贴」不存在。现在的合成是覆盖率模型 `out = B + A·(I_ink − B)`（`glyph_engine.compose`），软边来自字形自身的 alpha，而不是对矩形补丁做羽化。
6. **二值扫描件分支**（CCITT G4 / JBIG2）：未覆盖。
7. **云端适配器的色彩校正、接缝羽化、官方参数枚举对齐**：未实现。
8. **框选 GUI 的放大镜、多框、并排对比**：`selector.html` 仍是单框（该文件未改动）。
9. **PSF 的完整 erf 拟合**：当前 `_estimate_sigma` 用 alpha 0.5 交叉处的最大梯度估计 σ（σ = 0.3989 / max|dA/dx|），不是最小二乘曲线拟合。
