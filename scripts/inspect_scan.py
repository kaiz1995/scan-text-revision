#!/usr/bin/env python3
"""Read-only inspection for images and scan-only PDFs."""
from __future__ import annotations
import argparse, hashlib, json, shutil, subprocess
from pathlib import Path
from typing import Any

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def run_optional(program: str, *args: str) -> str | None:
    if shutil.which(program) is None:
        return None
    p = subprocess.run([program, *args], capture_output=True, text=True, check=False)
    return (p.stdout + p.stderr).strip()

def render_pdf(source: Path, output_dir: Path) -> list[str]:
    if shutil.which("pdftoppm") is None:
        raise RuntimeError("pdftoppm is required to render PDF pages")
    output_dir.mkdir(parents=True, exist_ok=True)
    local_copy = output_dir / "source-reference.pdf"
    shutil.copy2(source, local_copy)
    prefix = output_dir / "page"
    process = subprocess.run(["pdftoppm", "-png", "-r", "200", str(local_copy), str(prefix)], capture_output=True, text=True, check=False)
    if process.returncode:
        raise RuntimeError((process.stderr or process.stdout).strip() or "pdftoppm failed")
    return [str(page.resolve()) for page in sorted(output_dir.glob("page-*.png"))]

def inspect(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"source": str(path.resolve()), "sha256": sha256(path), "format": path.suffix.lower().lstrip(".")}
    if path.suffix.lower() == ".pdf":
        info = run_optional("pdfinfo", str(path))
        fonts = run_optional("pdffonts", str(path))
        text = run_optional("pdftotext", str(path), "-")
        result.update({"pdfinfo": info, "embedded_fonts": bool(fonts and len(fonts.splitlines()) > 2), "text_layer_chars": len((text or "").strip()), "scan_only_hint": not bool((text or "").strip())})
        if info:
            result["pdf"] = {line.split(":", 1)[0].strip().lower().replace(" ", "_"): line.split(":", 1)[1].strip() for line in info.splitlines() if ":" in line}
    else:
        try:
            from PIL import Image
            with Image.open(path) as image:
                result["image"] = {"size": list(image.size), "mode": image.mode, "format": image.format, "dpi": list(image.info.get("dpi", ())) if image.info.get("dpi") else None}
        except ImportError:
            result["image_error"] = "Pillow is required for raster metadata"
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--render-dir", type=Path, help="copy a PDF and render pages into this task work directory")
    args = parser.parse_args()
    if not args.input.is_file():
        parser.error(f"input is not a file: {args.input}")
    if args.render_dir and args.input.suffix.lower() != ".pdf":
        parser.error("--render-dir is only valid for PDF input")
    report = inspect(args.input)
    if args.render_dir:
        try:
            report["rendered_pages"] = render_pdf(args.input, args.render_dir)
        except RuntimeError as exc:
            parser.error(str(exc))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
