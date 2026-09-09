# Cloud Image Adapter

Cloud generation is optional and must be explicitly authorized.
It is a masked-crop fallback for complex backgrounds, not the primary editor.

## Provider

Backend: OpenAI-compatible endpoint via gpt-image-2.5 edits API.

Credentials are read from environment variables at runtime:

- SCAN_REVISION_API_KEY: API key. Never log or embed in code.
- SCAN_REVISION_BASE_URL: endpoint base URL.
- SCAN_REVISION_MODEL: model name, default gpt-image-2.5.

Store these in the working directory .env. Do not write them into skill assets, scripts, or logs.

## Interface

    generate_patch(image_crop, mask, prompt, reference_crop=None) -> candidates[]

The adapter script is scripts/cloud_inpaint.py. It:

1. Reads the cropped region PNG and alpha-mask PNG from disk.
2. Calls /v1/images/edits with the crop as image, mask as mask, and a constrained prompt.
3. Returns one or more candidate PNGs with model id, timestamps, and SHA-256 hashes.

## Prompt constraints

- Change only the masked region.
- Preserve paper texture, lighting, perspective, lines, and surrounding marks.
- Do not invent or alter unmasked content.
- Return the same pixel dimensions and color mode.
- Background-only repair: "Remove all text in the masked area. Preserve the paper texture, table lines, watermark patterns, and lighting exactly."
- Text+background (complex): "Replace the masked text with [NEW_TEXT]. Match the font style, size, weight, and color of surrounding text. Preserve paper texture and all non-text elements."

## Size handling

gpt-image-2.5 requires both edges to be multiples of 16, minimum ~810px per edge, max 3840px.
If the crop is smaller, pad with surrounding context to meet the minimum, then crop back after generation.
Record padding offsets so the final composite aligns correctly.

## Candidate record

Each candidate must include:

- image: output PNG path
- model: model identifier used
- created_at: ISO timestamp
- input_sha256: hash of the uploaded crop
- output_sha256: hash of the returned image
- confidence or error: provider response or failure reason

Do not log API keys, full source documents, or unredacted sensitive content.

## Post-processing

After return, locally:

1. Crop the candidate back to original region size (undo any padding).
2. Compare unmasked pixels against the original crop. Reject if drift exceeds threshold.
3. Run quality checks from quality-checks.md.
4. Require human approval before compositing into the page.

## Failure handling

If the cloud call fails (network, moderation refusal, timeout, or unacceptable drift), fall back to local OpenCV/Pillow inpainting. Log the failure reason without exposing credentials.
