# Workflow

## Inspect

Record input SHA-256, format, page count, page size, image dimensions, DPI, color mode, and whether a PDF contains a usable text layer. A scan-only PDF normally has one raster image per page and no embedded fonts/text.

## Analyze

Represent page regions as text, line, table, watermark, stamp, signature, or photo. Each region has bbox, confidence, and editable. Protected or uncertain regions are not editable.

## Select

The user supplies page, bbox, original_text when known, and replacement_text. Keep OCR as a suggestion. Reject empty boxes, boxes outside the page, and replacements that do not fit unless the user approves a new layout.

## Candidate generation

Use local image operations first. Expand the mask only enough to remove old glyph pixels; preserve nearby lines and patterns through a separate line mask. Render replacement text with measured baseline, height, width, spacing, color, blur, and noise. Produce up to three candidates when font matching is uncertain.

## Approval and export

Show original, mask, candidate, and a zoomed comparison. Only explicit approval makes an edit exportable. Export a revised sample plus provenance, hashes, edit log, and review report. Keep the original unchanged.

## Cloud fallback

For complex texture, send a cropped region and mask through the adapter in cloud-image-adapter.md. In test mode, whole-page context may be sent if the owner authorizes it. Composite the returned patch locally and validate that pixels outside the crop are unchanged.
