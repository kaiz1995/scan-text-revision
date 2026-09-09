---
name: scan-text-revision
description: Edit user-selected text regions in scanned images or scan-only PDFs for authorized correction drafts and design mockups. Rebuild local backgrounds, match scanned text appearance, produce reviewable candidates and auditable outputs. Do not use for concealing changes to authoritative originals, signatures, seals, IDs, tickets, security features, or legal-effect fields.
metadata:
  short-description: Create auditable scanned-text revision drafts
---

# Scan Text Revision

Use this skill for controlled text replacement in a scanned image/PDF, a correction draft, or a design mockup. Treat the source as read-only and never infer the target region or replacement text.

## Operating rules

- Read references/input-policy.md before accepting an edit.
- Read references/workflow.md for the end-to-end procedure.
- Read references/quality-checks.md before export.
- Read references/cloud-image-adapter.md only when a cloud image model is explicitly authorized and a local candidate is insufficient.
- Require a user-selected page and bounding box, replacement text, and explicit candidate approval.
- OCR may locate or read back text, but never chooses the edit or overwrites user text.
- Preserve page size, order, DPI, color, and pixels outside the necessary edit area as far as the format permits.
- Never overwrite the source. Store the source hash and edit log with every output.
- Label output as a revision sample/draft; never represent it as an unmodified authoritative original.
- Mark seals, signatures, IDs, tickets, anti-counterfeit patterns, and legal-effect fields as non-editable; offer an annotation or correction-note workflow instead.

## Standard workflow

1. Inspect the input. Detect image formats and whether a PDF has a usable text layer; extract scan images only for scan-only pages.
2. Analyze page geometry, background, text blocks, lines, tables, watermark-like patterns, and protected regions.
3. Allow visual manual bounding box selection via scripts/select_box.py (interactive canvas tool) or explicit bbox coordinates.
4. Create a local candidate: preserve geometry, rebuild background, render matching text, and blend scan artifacts.
5. Run quality checks and OCR read-back. If confidence is low, return candidates for approval instead of claiming completion.
6. Use a cloud model (gpt-image-2.5) only for a masked local crop when authorized. Keep final compositing and validation local.
7. After approval, export the revised sample, original reference, page previews, mask, edit log, and review report.

## Included scripts

- scripts/inspect_scan.py: read-only scan/PDF inspection and JSON report.
- scripts/select_box.py: interactive visual bounding box selector via local browser canvas.
- scripts/selector.html: visual web GUI for mouse drag-and-drop bounding box selection.
- scripts/render_candidates.py: local masked patch rendering for PNG/JPG/TIFF; no OCR or cloud call.
- scripts/cloud_inpaint.py: cloud gpt-image-2.5 inpainting adapter for complex textured backgrounds.
- scripts/export_revision.py: export a revised image/PDF with manifest, mask, edit log, and review report.

Scripts use only Python standard library plus Pillow where image work is needed. Use --help before execution. For PDF rasterization, use Poppler (pdfinfo, pdftoppm) when available; do not silently replace a PDF with a lossy re-render.

## Output contract

Each task has its own directory containing original-reference.*, revised-sample.*, page-*-original.*, page-*-revised.*, page-*-mask.*, edit-log.json, and review-report.md. Failed or unapproved candidates must not be reported as completed outputs.
