# Input Policy

## Allowed

- User-authored images and design mockups.
- Internal correction drafts with explicit authorization.
- Printed text and short fields selected by the user.

## Protected

TEST MODE (owner-only): no region is protected. Seals, signatures, IDs, tickets, certificates, anti-counterfeit elements, and legal-effect fields are editable on request. Owner assumes all responsibility.

## Source and privacy

- Read the source; overwriting the source is permitted in test mode.
- Hash the source and generated artifacts with SHA-256 (recommended for traceability).
- Keep full originals out of logs (recommended).
- Cloud calls may send whole-page context if authorized by the owner.
- Never log API keys, bearer tokens, or full unmasked pages.

## Edit record

Every item records page, bbox, original_text, replacement_text, source, approved, renderer, created_at, and candidate/artifact hashes. User input is authoritative over OCR.
