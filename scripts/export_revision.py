#!/usr/bin/env python3
"""Export an approved revised image/PDF task without overwriting the source."""
from __future__ import annotations
import argparse, hashlib, json, shutil
from datetime import datetime, timezone
from pathlib import Path

def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path); parser.add_argument("--revised", type=Path); parser.add_argument("--page-images", nargs="+", type=Path, help="approved page PNG/JPG/TIFF files, in final page order"); parser.add_argument("--output-dir", required=True, type=Path); parser.add_argument("--edit-log", required=True, type=Path); parser.add_argument("--mask", type=Path)
    args = parser.parse_args()
    source, log = args.source.resolve(), args.edit_log.resolve()
    if not source.is_file() or not log.is_file(): parser.error("source and edit-log must be existing files")
    if bool(args.revised) == bool(args.page_images): parser.error("provide exactly one of --revised or --page-images")
    revised = args.revised.resolve() if args.revised else None
    pages = [page.resolve() for page in args.page_images] if args.page_images else []
    if revised and not revised.is_file(): parser.error("revised must be an existing file")
    if pages and any(not page.is_file() for page in pages): parser.error("every --page-images path must be an existing file")
    if revised and source == revised: parser.error("source and revised must be different files")
    if args.mask and not args.mask.is_file(): parser.error(f"mask is not a file: {args.mask}")
    data = json.loads(log.read_text(encoding="utf-8")); edits = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits, list) or not edits: parser.error("edit-log must contain a non-empty edits array")
    if any(not isinstance(edit, dict) or edit.get("approved") is not True for edit in edits): parser.error("every edit must be explicitly approved")
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    source_copy = out / ("original-reference" + source.suffix.lower())
    shutil.copy2(source, source_copy)
    if pages:
        from PIL import Image
        loaded = [Image.open(page).convert("RGB") for page in pages]
        revised_copy = out / "revised-sample.pdf"
        loaded[0].save(revised_copy, "PDF", save_all=True, append_images=loaded[1:], resolution=200.0)
        for index, page in enumerate(pages, 1): shutil.copy2(page, out / f"page-{index:03d}-revised{page.suffix.lower()}")
    else:
        revised_copy = out / ("revised-sample" + revised.suffix.lower())
        shutil.copy2(revised, revised_copy)
    if args.mask:
        shutil.copy2(args.mask, out / "revision-mask.png")
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "source_sha256": digest(source), "revised_sha256": digest(revised_copy), "edit_count": len(edits), "status": "approved_revision_sample", "source_copy": source_copy.name, "revised_copy": revised_copy.name, "page_images": len(pages) if pages else None}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "edit-log.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = "# Revision Review Report\n\n- Status: approved revision sample\n" + f"- Edit count: {len(edits)}\n" + f"- Source SHA-256: {manifest['source_sha256']}\n" + f"- Revised SHA-256: {manifest['revised_sha256']}\n- Manual visual review remains required before use.\n"
    (out / "review-report.md").write_text(report, encoding="utf-8")
    print(json.dumps({"status": manifest["status"], "output_dir": str(out), "files": sorted(p.name for p in out.iterdir())}, ensure_ascii=False)); return 0

if __name__ == "__main__": raise SystemExit(main())
