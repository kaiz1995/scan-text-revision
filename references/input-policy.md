# Input Policy

## Scope

- User-authored scans, screenshots, photos of documents/screens, design mockups.
- Internal correction drafts with explicit authorization.
- A single text line selected by the user, with the replacement text supplied by the user.

## Sensitive documents

The code enforces no content-type restrictions: nothing in `render_candidates.py` or `export_revision.py` looks at what the page contains. The restriction therefore lives here, in the procedure, and must be applied by whoever runs the skill:

- **Identity documents, seals/stamps, tickets, receipts and any document with legal effect require an explicit confirmation first** — the user must acknowledge that the output is a revision sample with no validity as an original.
- Record that confirmation in the delivery message, and label the output as a revision sample: keep the `revised-sample.*` names, keep `manifest.json`'s `status` (`approved_revision_sample` only means the gate passed, not that the document is authentic), and keep `review-report.md` beside the result.
- The engineering goal called "seamless" is *no visible splice* (seam step, stroke weight, ink colour, grain). It is **not** the removal of audit trails. Do not delete, rewrite or hand-edit the manifest, edit log or gate results to make a delivery look cleaner.

## Source and privacy

- The source is read-only. `export_revision.py` refuses a deliverable path equal to the source and copies the source into the output directory as `original-reference.*`; nothing in the toolchain overwrites an input in place.
- SHA-256 of the source and of the deliverable are written to `manifest.json` (`sha256` of the raw file); per-region hashes are `sha256/raw-rgb-crop` of the bbox in the source and in the deliverable.
- Cloud calls upload exactly two files: the crop PNG and the mask PNG. Small crops are padded to at least 832 px per edge (multiple of 16) with a white border, so the upload can include page content adjacent to the crop — keep the crop tight if that matters. There is no whole-page upload path in `cloud_inpaint.py`.
- Never log or persist `SCAN_REVISION_API_KEY`, bearer tokens, or unredacted sensitive crops.

## Edit record

`export_revision.py --edit-log` requires a JSON file with a non-empty `edits` array. Per edit it needs:

- `page` (defaults to 1), `bbox` as `[x, y, w, h]` — impossible to export without it;
- `approved: true` — the exporter rejects any edit that is not explicitly approved;
- `replacement_text` (or `text`) — also used as `expected_text` for the OCR read-back check;
- `original_text` — recommended, not required;
- the render record from `render_candidates.py`, under `candidate` or `render`, or at least its `glyphs_borrowed` / `glyphs_synthesized` keys.

The log is copied verbatim to `edit-log.json` in the output directory. Provenance is not enriched there; it is assembled into `manifest.json`, which records per region: `index`, `page`, `bbox`, `region` (bbox ∪ mask bbox), `char_sources` (`borrowed` / `synthesized`, or `origin: unknown` when the log carries no provenance), `source_region_sha256`, `revised_region_sha256`, plus the top-level `qa.checks`, `jpeg_qtables_match` and `pdf_repack` mode.

User input is authoritative over OCR: OCR never selects a region, never edits text, and only appears as the `ocr_text` read-back check.
