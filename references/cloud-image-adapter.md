# Cloud Image Adapter

Cloud generation is optional, must be explicitly authorized, and is a masked-crop fallback for backgrounds the local model cannot fit — not the primary editor. It is a manual step: the adapter generates candidate crops, it does not place them on the page.

## Provider

Backend: an OpenAI-compatible `/v1/images/edits` endpoint (default model name `gpt-image-2.5`).

Credentials are read from the environment, optionally seeded from a `.env` file in the working directory (`--env` for another path). Only keys prefixed `SCAN_REVISION_` are imported:

- `SCAN_REVISION_API_KEY` — API key. Never log it or embed it in code.
- `SCAN_REVISION_BASE_URL` — endpoint base URL; `cloud_inpaint.py` appends `/v1/images/edits`.
- `SCAN_REVISION_MODEL` — model name, default `gpt-image-2.5`.

Missing key or base URL produces an `{"status": "error"}` record, not an exception.

## Interface

```python
cloud_inpaint(crop_path, mask_path, prompt, output_dir, n=1,
              quality="medium", env_path=None) -> list[dict]
```

```bash
python scripts/cloud_inpaint.py crop.png mask.png \
  --prompt "..." --output-dir ./candidates --n 1 --quality medium [--env .env]
```

`--quality` accepts `low|medium|high`. `n` is forwarded to the API and can return more than one image, but this is provider-side only — there is no local scoring of them.

## Mask semantics

The mask is a PNG; white (255) means "edit", black means "keep". A grayscale mask is converted to RGBA with `alpha = 255 − value`, because the endpoint expects transparent = editable, opaque = keep. Passing an `RGBA` mask is also accepted. The crop is uploaded with the mask; `httpx` is required (a clear error record is returned if it is missing).

## Size handling

- Both edges are padded up to at least **832 px** and rounded up to a multiple of **16**; the padded size is sent explicitly as `size`.
- Padding is white RGBA around the crop; mask padding is opaque (no edit).
- If the returned image is not the padded canvas size, it is resized to that canvas with LANCZOS, then cropped back to the original region, so the returned file always matches the crop that was sent.
- The code does not clamp to any maximum edge length; a request above the provider's limit fails at the API and comes back as an error record.
- `padded_size` is recorded, so the padding offsets can be reconstructed.

## Candidate record

Each successful item is:

```json
{
  "status": "candidate",
  "image": "<abs path>/cloud_candidate_0.png",
  "model": "gpt-image-2.5",
  "created_at": "<ISO-8601 UTC>",
  "input_sha256": "<sha256 of the crop file on disk>",
  "output_sha256": "<sha256 of the raw response bytes>",
  "padded_size": "1024x832",
  "approved": false
}
```

Note what the hashes cover: `input_sha256` is the hash of the crop file as read from disk, **not** of the padded upload, and `output_sha256` is the hash of the bytes returned by the API before the resize/crop step. Failures are `{"status": "error", "error": "...", "index": n?}`. Exit code is 0 only when at least one candidate was produced. There is no `confidence` field — the provider response is either an image or an error.

## Prompt constraints

- Change only the masked region.
- Preserve paper texture, lighting, perspective, lines and surrounding marks.
- Do not invent or alter unmasked content.
- Return the same pixel dimensions and colour mode.
- Background-only repair: "Remove all text in the masked area. Preserve the paper texture, table lines, watermark patterns, and lighting exactly."
- Text + background: "Replace the masked text with [NEW_TEXT]. Match the font style, size, weight, and color of surrounding text. Preserve paper texture and all non-text elements."

## Post-processing

Automated by the adapter: resize to the padded canvas, crop back to the original region, write `cloud_candidate_<i>.png`.

Not automated — the caller must do it:

1. Composite the returned crop onto the page at the recorded offset. No script in this repository performs that step.
2. Visually compare the unmasked part of the crop with the original. **No drift check exists** — the adapter does not compare unmasked pixels and does not reject a candidate. This is a Roadmap item.
3. Run the acceptance gate (`scripts/qa_gate.py`, or `export_revision.py` which forces it) on the finished page, with `bbox`/`region`/`expected_text` for the edit.
4. Obtain explicit human approval before export.

Do not log API keys, full source documents, or unredacted sensitive content.

## Failure handling

Network errors, non-200 responses, moderation refusals, timeouts (120 s) and empty `b64_json` all become error records in the returned list; the CLI exits 1 when no candidate was produced. There is **no automatic fallback** to local inpainting: to retry locally, run `render_candidates.py` (default engine path, or `--legacy`), then gate and export as usual. Log the failure reason without exposing credentials.

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
