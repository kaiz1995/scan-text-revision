# Input Policy

## Allowed

- User-authored images and design mockups.
- Internal correction drafts with explicit authorization.
- Printed text and short fields selected by the user.

## Protected

Do not automatically alter signatures, seals, identity documents, tickets, certificates, anti-counterfeit elements, legal-effect fields, or any region that overlaps them. Produce an annotation/correction note or ask the user to obtain a corrected source instead.

## Source and privacy

- Read the source; never overwrite it.
- Hash the source and generated artifacts with SHA-256.
- Keep full originals out of logs.
- Cloud calls require explicit authorization for that document and provider. Send only a local crop, mask, and minimum context.
- Never log API keys, bearer tokens, or full unmasked pages.

## Edit record

Every item records page, bbox, original_text, replacement_text, source, approved, renderer, created_at, and candidate/artifact hashes. User input is authoritative over OCR.
