# Quality Checks

## Geometry

- Replacement is inside the selected box.
- Baseline, height, width, spacing, and orientation are consistent with neighboring text.
- No table or separator line is clipped or displaced.

## Image

- Old glyph pixels are not visible.
- No rectangular patch, halo, repeated texture, hard edge, or over-sharp new text is visible at 200% zoom.
- Watermark-like texture and paper grain remain continuous where applicable.
- Pixels outside the intended edit/crop are unchanged before final encoding.

## Text and file

- OCR read-back matches the user replacement, or the result is flagged for manual review.
- Page count, page order, dimensions, orientation, and color mode remain stable.
- PDF can be reopened and rasterized.
- Manifest hashes and edit-log count agree.

## Human review

Review original, mask, candidate, and zoom comparison. Automatic scores rank candidates only; they do not approve an edit. On any failed check, retain the source and return needs_review.
