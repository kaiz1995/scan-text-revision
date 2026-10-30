---
name: scan-text-revision
description: Replace one user-selected line of text in a scanned image, a photographed document or screen, or a scan-only PDF, keeping the page's own appearance. The engine measures the source line (quadratic background surface, glyph coverage, baseline, ink colour, stroke width, blur sigma, noise), borrows the original glyph pixels, and synthesises only the missing characters from a template-matched system font. Every export is forced through scripts/qa_gate.py — untouched-pixel diff, seam step, ink/stroke/height/baseline/noise deltas, JPEG quantisation tables, optional tesseract read-back — before anything is reported as done. Output is a revision sample with manifest, edit log and gate report, not an evidence-grade original. Use for correction drafts, OCR typo fixes and design mockups on scans, screenshots and photos of documents.
metadata:
  short-description: Measurement-driven scanned-text revision with a forced QA gate
---

# Scan Text Revision

Replace one line of text inside a bounding box the user supplies. The input is read-only: `export_revision.py` refuses to export when the deliverable path is the source, and always copies the source into the output directory as `original-reference.*`.

## Operating rules

- Read `references/input-policy.md` before accepting an edit, `references/workflow.md` for the end-to-end procedure, and `references/quality-checks.md` before export.
- Read `references/cloud-image-adapter.md` only when a cloud image model is explicitly authorized and a local candidate is insufficient.
- Require from the user: page, bounding box `x,y,w,h`, and replacement text. Never guess the region or the text.
- Ask for the **original** line text (`--page-text`). Without it no glyph can be borrowed and every character is synthesised from a font, which is visibly weaker and has no measured advance.
- Keep the box tight around the text line. The selected box is erased and rebuilt from the measured background, so a box that includes a table rule, a seal edge or a neighbouring glyph repaints it too.
- Never report an export as completed unless `export_revision.py` printed `"status": "approved_revision_sample"` / `"qa_pass": true`. If a check is `skipped`, say it was skipped; never present it as passed.
- Show the user the source, mask, candidate and a 200% comparison, and require explicit approval (`"approved": true` in the edit log) before export.

## Dependencies

- **Required:** Python 3.10+, Pillow (image I/O, font rasterisation) and numpy (all measurements and compositing).
- **Optional, faster:** OpenCV (`opencv-python`) enables the cv2 paths for gaussian blur, dilation, connected components, distance transform and template matching. Without it numpy fallbacks run (slower) and `render_candidates.py` splits character boxes evenly instead of by connected component, which can mis-assign borrowed glyphs.
- **Optional, cloud only:** `httpx` for `scripts/cloud_inpaint.py`.
- **Optional, PDF export only:** `pikepdf`. Without it a PDF is repacked by Pillow (every page re-encoded, `lossy_fallback`) and the source page images cannot be decoded for a pixel gate, so a PDF export normally ends `qa_failed` unless `--source-pages` is supplied.
- **Optional external tools:** Poppler (`pdftoppm`, `pdfinfo`, `pdffonts`, `pdftotext`) for PDF inspection and rasterisation; `tesseract` for the OCR read-back check (`chi_sim+eng`, falling back to `eng`).

## Standard workflow

1. **Inspect** — `python scripts/inspect_scan.py <file> --json inspect.json [--render-dir ./work]`. Records SHA-256, format; for PDFs the parsed `pdfinfo` fields, whether embedded fonts exist and the text-layer character count (`scan_only_hint`); for rasters size, mode and the `dpi` **metadata** if present. PDF pages are rasterised with `pdftoppm -r 200` — the DPI is a fixed constant, not a measurement. There is no layout analysis.
2. **Select** — `python scripts/select_box.py <page.png> --output task.json` opens a local browser canvas for a single rectangle and writes `{bbox: [x,y,w,h], bbox_str, text, image}`. Coordinates can also be supplied directly. One box per run.
3. **Candidate** — `python scripts/render_candidates.py <page.png> --bbox x,y,w,h --page-text "<original line>" --text "<new line>" --output cand.png --mask cand_mask.png [--font-id] [--sigma S] [--weight PX]`. Default path is measure → glyph library → baseline typesetting → coverage composite (see `references/workflow.md`). Save candidates as PNG; a `.jpg` candidate is re-encoded at quality 95. `--legacy` falls back to the old whole-crop inpaint repaint.
4. **Pre-check (optional)** — `python scripts/qa_gate.py --source <page.png> --output cand.png --bbox x,y,w,h --region x0,y0,x1,y1 --expected-text "<new line>" --json-out gate.json`.
5. **Approve** — show original / mask / candidate / 200% crop. Put the render JSON from step 3 into the edit record and set `"approved": true`; export rejects any edit that is not approved.
6. **Export** — `python scripts/export_revision.py --source <source> --revised cand.png --output-dir out --edit-log edit-log.json --mask cand_mask.png` for images, or `--source <source.pdf> --page-images page-1.png ...` (plus `--source-pages` when `pikepdf` is unavailable) for PDFs. The gate always runs; a failure still writes `qa-report.json` and `review-report.md` and exits non-zero.
7. **Report** — quote the gate's own values and statuses from `qa-report.json` / `manifest.json`, including `skipped` checks. Do not summarise a failed or partial gate as a success.

## Included scripts

- `scripts/glyph_engine.py` — measurement, glyph library, baseline typesetting and coverage compositing. Public API: `measure`, `build_glyph_library`, `render`, `compose`, plus `identify_font`, `baseline_y`, `noise_field`, `metric_summary`. CLI: `--image --box --chars/--boxes-json [--text] [--rebuild] [--identify] [--font] [--pen-x] [--out] [--json]`.
- `scripts/qa_gate.py` — the acceptance gate: 12 checks with `pass` / `fail` / `skipped` and a JSON result. Glyph height and baseline are class-aware: glued components are split, digits are compared against a baseline fitted through the source digits, and CJK glyphs against shape-matched source CJK glyphs. Ink is compared twice — perceived tone and CIELAB ΔE — because a luminance-only delta cannot see a channel cast. CLI takes `--edits edits.json` or `--bbox` + `--region` + `--expected-text`.
- `scripts/render_candidates.py` — renders one replacement candidate through the engine; `--legacy` keeps the original inpaint path. Prints a JSON record (`status: candidate`, `mode`, `glyphs_borrowed`, `glyphs_synthesized`, `ink_rgb`, `baseline`, `sigma`, `stroke_px`, `measurement`, optional `font_id`).
- `scripts/export_revision.py` — forced gate plus deliverable assembly: reuses the source JPEG quantisation tables, chroma subsampling, progressive flag and DPI; writes a lossless PNG layer; replaces only the page image XObject when `pikepdf` is installed; writes `manifest.json`, `qa-report.json`, `edit-log.json`, `review-report.md`.
- `scripts/inspect_scan.py` — read-only metadata and PDF page rasterisation.
- `scripts/select_box.py` — local HTTP server (127.0.0.1, ports 8765–8784) that serves the canvas page and writes the selection JSON.
- `scripts/selector.html` — single-box canvas UI with pixel X/Y/W/H readout, crop preview, "copy bbox args" and submit.
- `scripts/cloud_inpaint.py` — optional cloud adapter: uploads one crop plus mask to an OpenAI-compatible `/v1/images/edits` endpoint and returns `n` candidate PNG **crops**. It does not composite them into the page.

## Output contract

`export_revision.py` writes into `--output-dir`:

- `original-reference.<ext>` — copy of the source;
- `revised-sample.jpg` **and** `revised-sample.png` for a JPEG source (the PNG is the lossless layer the gate compares); a single `revised-sample.<ext>` for a lossless source;
- `revision-mask.png` when `--mask` is given;
- `page-<nnn>-revised.<ext>` and `revised-sample.pdf` for page/PDF exports;
- `manifest.json`, `qa-report.json`, `edit-log.json` (verbatim copy of the input log), `review-report.md`.

Source page previews come from `inspect_scan.py --render-dir` (`page-*.png`), not from the exporter. Columns named `page-<n>-original.*` / `page-<n>-mask.*` are not produced by any script. Failed or unapproved candidates must not be reported as completed outputs.

## Limits

- One text line at a time, typeset on the measured baseline. Multi-line or multi-column edits need one run per line.
- Font identification scans 38–56 px sizes and σ 0.6–2.2 (`FONT_SIZE_RANGE`, `FONT_SIGMA_RANGE`) among a fixed list of CJK font filenames under `%WINDIR%\Fonts`. Lines far outside that scale need an explicit `--font`, or `--sigma` / `--weight` overrides.
- The background model is a per-channel quadratic surface fitted on ink-free pixels. Strongly structured substrate (guilloche, dense rules, photos) is not modelled; use `--legacy` or a cloud candidate and review by eye.
- All glyphs in one patch share a single full-ink colour (area-weighted mean of the borrowed cores). A line with mixed ink colours will not match everywhere.
- The erase domain is the selected box, replaced by fitted background plus borrowed residual noise. Keep the box tight.
- Binarised scans (CCITT G4 / JBIG2) are not covered.
- `render_candidates.py` asserts that `compose()` changed no pixel outside its mask; that is an internal invariant, not proof of invisibility. Only the gate quantifies the result.
- DPI metadata is preserved for JPEG exports only; lossless-source exports do not rewrite DPI tags.

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

## 使用边界与合规

本工具测量原图后借用原字形合成，产出可以做到肉眼难辨，因此必须保留可追溯性：

- `manifest.json` 记录 `source_sha256`、`revised_sha256`、每处改动的 `bbox` 与允许触达的 `region`、字形来源（`char_sources.borrowed` = 借用原字形，`char_sources.synthesized` = 字体合成；记录缺失时写明 `unknown`，需人工补齐归属）、以及 `qa.checks` 中逐项门禁的阈值、实测值与状态。`export_revision.py` 同时把源文件复制为 `original-reference.*` 一并留档。
- `review-report.md` 由 `export_revision.py` 依据 `qa_gate` 的真实结果生成，没有写死的结论行；门禁未通过时 `status` 为 `qa_failed`，脚本以非零码退出。
- 身份证件、印章、票据、法律效力文件的编辑，必须先取得用户对「产物为修订样本、不具备原件效力」的显式确认，并在产物目录和交付说明中标注为修订样本（`revised-sample.*`、`manifest.json` 的 `status`、`review-report.md`）。脚本本身不拦截内容类型，拦截责任在流程层，见 `references/input-policy.md`。
- 「无痕」的工程目标是不在编辑区留下可见的拼接痕迹（接缝台阶、字重/墨色/噪声失配），不是消除审计线索。两者分开实现：前者由 `glyph_engine` 与 `qa_gate` 负责，后者由 manifest、edit-log、review-report 负责；不要为「更干净」删改这些记录，也不要手工编辑 `review-report.md` 的门禁结论。
