#!/usr/bin/env python3
"""Export an approved revised image/PDF task without overwriting the source.

Every export is gated: ``qa_gate.run_gate`` runs before a manifest is marked
approved.  A failed or blocked gate exits non-zero and still writes
``qa-report.json`` plus a ``review-report.md`` built from the gate's measured
values (no hard-coded template lines).

Images are re-encoded with the *source* JPEG quantisation tables and chroma
subsampling, so untouched areas go through no second quality loss.  PDF input
is repacked by replacing only the page image XObject when ``pikepdf`` is
installed; otherwise the export is explicitly labelled ``lossy_fallback``.
"""
from __future__ import annotations
import argparse, hashlib, io, json, shutil, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, JpegImagePlugin

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path: sys.path.insert(0, str(SCRIPT_DIR))

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".tif", ".tiff", ".bmp", ".webp"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def open_rgb(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB")


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size


def edit_bbox(edit: dict):
    """Return (x, y, width, height) from the shapes edit-logs actually use."""
    value = edit.get("bbox")
    if isinstance(value, dict):
        try: return (int(value["x"]), int(value["y"]), int(value["width"]), int(value["height"]))
        except (KeyError, TypeError, ValueError): return None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try: return tuple(int(round(float(v))) for v in value)
        except (TypeError, ValueError): return None
    if {"x", "y", "width", "height"} <= edit.keys():
        try: return (int(edit["x"]), int(edit["y"]), int(edit["width"]), int(edit["height"]))
        except (TypeError, ValueError): return None
    if {"x", "y", "w", "h"} <= edit.keys():
        try: return (int(edit["x"]), int(edit["y"]), int(edit["w"]), int(edit["h"]))
        except (TypeError, ValueError): return None
    return None


def char_sources(edit: dict) -> dict:
    """Glyph provenance recorded by render_candidates, when the log carries it."""
    meta = {}
    for key in ("render", "candidate"):
        value = edit.get(key)
        if isinstance(value, dict): meta.update(value)
    for key in ("glyphs_borrowed", "glyphs_synthesized"):
        if key in edit: meta.setdefault(key, edit[key])
    borrowed = [str(char) for char in meta.get("glyphs_borrowed") or []]
    synthesized = [str(char) for char in meta.get("glyphs_synthesized") or []]
    if borrowed or synthesized:
        return {"borrowed": borrowed, "synthesized": synthesized, "origin": "edit-log"}
    return {"borrowed": [], "synthesized": [],
            "origin": "unknown: edit-log records no glyph provenance; manual attribution required"}


def region_digest(image: Image.Image | None, box):
    if image is None or box is None: return None
    x, y, width, height = box
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(image.width, x + width), min(image.height, y + height)
    if x1 <= x0 or y1 <= y0: return None
    return hashlib.sha256(np.asarray(image.crop((x0, y0, x1, y1))).tobytes()).hexdigest()


def mask_bbox(path: Path):
    """Bounding box of the edited region actually touched by the mask."""
    with Image.open(path) as image:
        array = np.asarray(image.convert("L"))
    rows, cols = np.where(array > 0)
    if len(rows) == 0: return None
    return (int(cols.min()), int(rows.min()),
            int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1))


def union_bbox(first, second):
    if first is None: return second
    if second is None: return first
    x0, y0 = min(first[0], second[0]), min(first[1], second[1])
    x1 = max(first[0] + first[2], second[0] + second[2])
    y1 = max(first[1] + first[3], second[1] + second[3])
    return (x0, y0, x1 - x0, y1 - y0)


def image_encoding(path: Path) -> dict:
    with Image.open(path) as image:
        qtables = getattr(image, "quantization", None) if image.format == "JPEG" else None
        sampling = JpegImagePlugin.get_sampling(image) if image.format == "JPEG" else None
        return {"format": image.format or path.suffix.lstrip(".").upper(),
                "qtables": {int(k): list(v) for k, v in (qtables or {}).items()} or None,
                "sampling": sampling,
                "progressive": bool(image.info.get("progressive")),
                "dpi": list(image.info["dpi"]) if image.info.get("dpi") else None}


def save_revised_image(source: Path, revised: Path, out_dir: Path):
    """Write the revised pixels, reusing the source JPEG tables when present."""
    source_meta = image_encoding(source)
    pixels = open_rgb(revised)
    if source_meta["qtables"]:
        deliverable = out_dir / "revised-sample.jpg"
        options = {"qtables": source_meta["qtables"],
                   "subsampling": source_meta["sampling"] if source_meta["sampling"] is not None else 0}
        if source_meta["progressive"]: options["progressive"] = True
        if source_meta["dpi"]: options["dpi"] = tuple(source_meta["dpi"])
        pixels.save(deliverable, "JPEG", **options)
        # Same stem as the JPEG: qa_gate's quantisation check looks for the
        # sibling encoded file of a lossless layer.
        layer = out_dir / "revised-sample.png"
        pixels.save(layer)
        output_meta = image_encoding(deliverable)
        match = output_meta["qtables"] == source_meta["qtables"] and output_meta["sampling"] == source_meta["sampling"]
        encoding = {"encoding": "jpeg_source_qtables", "jpeg_qtables_match": match,
                    "source_qtables": source_meta, "output_qtables": output_meta}
        return deliverable, layer, encoding
    suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".tif", ".tiff", ".bmp"} else ".png"
    deliverable = out_dir / ("revised-sample" + suffix)
    pixels.save(deliverable)
    encoding = {"encoding": "lossless_source_format", "jpeg_qtables_match": None, "source_qtables": source_meta}
    return deliverable, deliverable, encoding


# --------------------------------------------------------------------------- #
# PDF repacking
# --------------------------------------------------------------------------- #
def _pdf_page_image(page, pikepdf):
    resources = page.get("/Resources")
    if resources is None: return None
    xobjects = resources.get("/XObject")
    if xobjects is None: return None
    best = None
    for _, obj in xobjects.items():
        if obj.get("/Subtype") != pikepdf.Name("/Image"): continue
        area = int(obj.get("/Width", 0) or 0) * int(obj.get("/Height", 0) or 0)
        if best is None or area > best[0]: best = (area, obj)
    return best[1] if best else None


def _jpeg_meta_from_bytes(data: bytes):
    try:
        with Image.open(io.BytesIO(data)) as image:
            qtables = {int(k): list(v) for k, v in (image.quantization or {}).items()} or None
            return qtables, JpegImagePlugin.get_sampling(image)
    except Exception:
        return None, None


def _replace_pdf_images(source: Path, pages: list[Path], out_dir: Path):
    import pikepdf
    deliverable = out_dir / "revised-sample.pdf"
    with pikepdf.open(source) as pdf:
        page_objects = list(pdf.pages)
        if len(page_objects) != len(pages):
            raise RuntimeError(f"PDF has {len(page_objects)} page(s) but {len(pages)} revised page image(s) were supplied")
        for index, (page, page_path) in enumerate(zip(page_objects, pages), 1):
            target = _pdf_page_image(page, pikepdf)
            if target is None:
                raise RuntimeError(f"page {index} has no image XObject to replace; refusing a page re-encode")
            qtables, sampling = _jpeg_meta_from_bytes(bytes(target.read_raw_bytes()))
            revised = Image.open(page_path).convert("RGB")
            buffer = io.BytesIO()
            if qtables:
                revised.save(buffer, "JPEG", qtables=qtables,
                             subsampling=sampling if sampling is not None else 0)
            else:
                revised.save(buffer, "JPEG", quality=95)
            for stale in ("/DecodeParms", "/SMask", "/Mask", "/Matte"):
                if stale in target: del target[stale]
            target.write(buffer.getvalue(), filter=pikepdf.Name("/DCTDecode"))
            target["/Width"], target["/Height"] = revised.width, revised.height
            target["/ColorSpace"] = pikepdf.Name("/DeviceRGB")
            target["/BitsPerComponent"] = 8
        pdf.save(str(deliverable))
    return deliverable, "image_object_replacement", None


def _lossy_pdf(source: Path, pages: list[Path], out_dir: Path):
    loaded = [open_rgb(page) for page in pages]
    deliverable = out_dir / "revised-sample.pdf"
    loaded[0].save(deliverable, "PDF", save_all=True, append_images=loaded[1:], resolution=200.0)
    mode = "lossy_fallback" if source.suffix.lower() == ".pdf" else "rebuilt_from_pages"
    warning = ("pikepdf is not installed: the PDF was reassembled from page images with Pillow, "
               "which re-encodes every page; the lossless claim does not apply to this file")
    return deliverable, mode, warning


def write_pdf(source: Path, pages: list[Path], out_dir: Path):
    try:
        import pikepdf  # noqa: F401
    except ImportError:
        return _lossy_pdf(source, pages, out_dir)
    deliverable, mode, warning = _replace_pdf_images(source, pages, out_dir)
    return deliverable, mode, warning


def pdf_page_qtables(path: Path) -> list[dict] | None:
    try:
        import pikepdf
    except ImportError:
        return None
    try:
        with pikepdf.open(path) as pdf:
            tables = []
            for page in pdf.pages:
                target = _pdf_page_image(page, pikepdf)
                if target is None: return None
                qtables, sampling = _jpeg_meta_from_bytes(bytes(target.read_raw_bytes()))
                tables.append({"qtables": qtables, "sampling": sampling})
            return tables
    except Exception:
        return None


def pdf_reference_pages(source: Path) -> tuple[list[Image.Image], str | None]:
    """Decode the page image XObjects so page exports can be gated pixel-wise."""
    try:
        import pikepdf
    except ImportError:
        return [], "pikepdf is not installed, so the source PDF pages cannot be decoded for a pixel gate"
    try:
        rasters = []
        with pikepdf.open(source) as pdf:
            for index, page in enumerate(pdf.pages, 1):
                target = _pdf_page_image(page, pikepdf)
                if target is None:
                    return [], f"page {index} of the source PDF has no image XObject to compare against"
                rasters.append(Image.open(io.BytesIO(bytes(target.read_raw_bytes()))).convert("RGB"))
        return rasters, None
    except Exception as exc:  # malformed embedded image, unsupported filter, ...
        return [], f"could not decode the source PDF page images: {exc}"


# --------------------------------------------------------------------------- #
# QA gate
# --------------------------------------------------------------------------- #
def run_gate(source, output, edits):
    try:
        import qa_gate
    except ImportError as exc:
        raise RuntimeError("qa_gate.py is not available; export is blocked because QA cannot run") from exc
    return qa_gate.run_gate(str(source), str(output), edits)


def format_value(value) -> str:
    if value is None: return "—"
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, float): return f"{value:.4g}"
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False)
    return str(value)


def normalise_metrics(metrics) -> dict:
    """The gate may return a dict keyed by check or a list of check records."""
    if isinstance(metrics, dict): return metrics
    if isinstance(metrics, list):
        result = {}
        for index, item in enumerate(metrics, 1):
            if isinstance(item, dict):
                result[str(item.get("name") or item.get("check") or f"check{index}")] = item
            else:
                result[f"check{index}"] = item
        return result
    return {"value": metrics} if metrics is not None else {}


def jsonable(value):
    """Convert numpy scalars/arrays from gate metrics into JSON-safe values."""
    if isinstance(value, dict): return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)): return [jsonable(item) for item in value]
    if isinstance(value, np.generic): return value.item()
    if isinstance(value, np.ndarray): return value.tolist()
    if value is None or isinstance(value, (str, int, float, bool)): return value
    return str(value)


def failure_list(report: dict) -> list[str]:
    value = report.get("failures") or []
    return [value] if isinstance(value, str) else [str(item) for item in value]


def metric_rows(metrics: dict):
    for name, value in metrics.items():
        threshold = measured = result = "—"
        if isinstance(value, dict):
            for key in ("threshold", "limit", "target", "max", "max_allowed"):
                if key in value: threshold = format_value(value[key]); break
            for key in ("measured", "value", "actual"):
                if key in value: measured = format_value(value[key]); break
            passed = value.get("pass", value.get("ok", value.get("passed")))
            result = "pass" if passed is True else "fail" if passed is False else "—"
            if measured == "—" and passed is None:
                measured = format_value(value)
        else:
            measured = format_value(value)
        yield name, threshold, measured, result


def build_review_report(context: dict) -> str:
    qa = context["qa"]
    lines = ["# Revision Review Report", "",
             f"- Generated: {context['created_at']}",
             f"- Status: {context['status']}",
             f"- Source: `{context['source']}`",
             f"- Deliverable: `{context['deliverable']}`",
             f"- Edits: {context['edit_count']}",
             f"- QA gate: {'PASS' if qa['pass'] else 'FAIL'}",
             f"- JPEG quantisation tables match source: {format_value(context['jpeg_qtables_match'])}",
             f"- PDF repack: {context['pdf_repack']}",
             f"- Source SHA-256: `{context['source_sha256']}`",
             f"- Revised SHA-256: `{context['revised_sha256']}`", ""]
    if context["warnings"]:
        lines += ["## Warnings", ""] + [f"- {warning}" for warning in context["warnings"]] + [""]
    lines += ["## QA metrics", "", "| Check | Threshold | Measured | Result |", "| --- | --- | --- | --- |"]
    checks = qa.get("checks") or []
    if checks:
        rows = [(check.get("name", "—"), format_value(check.get("threshold")),
                 format_value(check.get("value")), str(check.get("status", "—")))
                for check in checks]
    else:
        rows = list(metric_rows(normalise_metrics(qa.get("metrics"))))
    if rows:
        lines += [f"| {name} | {threshold} | {measured} | {result} |" for name, threshold, measured, result in rows]
    else:
        lines.append("| (gate returned no metrics) | — | — | — |")
    lines += ["", "## Failures", ""]
    lines += [f"- {failure}" for failure in qa.get("failures") or []] or ["- None."]
    if qa.get("skipped"):
        lines += ["", "## Skipped checks", ""]
        lines += [f"- {item.get('name')}: {item.get('reason')}" for item in qa["skipped"]]
    lines += ["", "## Regions", "",
              "| # | Page | bbox (x,y,w,h) | Borrowed glyphs | Synthesized glyphs | Revised region SHA-256 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for region in context["regions"]:
        bbox = ",".join(str(v) for v in region["bbox"]) if region["bbox"] else "—"
        sources = region["char_sources"]
        borrowed = ", ".join(sources["borrowed"]) or "—"
        synthesized = ", ".join(sources["synthesized"]) or "—"
        digest_value = region["revised_region_sha256"] or "—"
        lines.append(f"| {region['index']} | {region['page']} | {bbox} | "
                     f"{borrowed} | {synthesized} | `{digest_value}` |")
    lines += ["", "## Manual review", "",
              "Automatic scores rank candidates; they do not approve an edit. Review the original, "
              "mask, candidate and zoom comparison before use. Keep the source and treat this file "
              "as a revision sample, not as evidence.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path); parser.add_argument("--revised", type=Path); parser.add_argument("--page-images", nargs="+", type=Path, help="approved page PNG/JPG/TIFF files, in final page order"); parser.add_argument("--output-dir", required=True, type=Path); parser.add_argument("--edit-log", required=True, type=Path); parser.add_argument("--mask", type=Path)
    parser.add_argument("--source-pages", nargs="+", type=Path, help="reference page rasters of the source, for pixel QA of multi-page exports")
    args = parser.parse_args()
    source, log = args.source.resolve(), args.edit_log.resolve()
    if not source.is_file() or not log.is_file(): parser.error("source and edit-log must be existing files")
    if bool(args.revised) == bool(args.page_images): parser.error("provide exactly one of --revised or --page-images")
    revised = args.revised.resolve() if args.revised else None
    pages = [page.resolve() for page in args.page_images] if args.page_images else []
    if revised and not revised.is_file(): parser.error("revised must be an existing file")
    if revised and source.suffix.lower() == ".pdf":
        parser.error("--revised cannot be used with a PDF source; export page images with --page-images")
    if pages and any(not page.is_file() for page in pages): parser.error("every --page-images path must be an existing file")
    if revised and source == revised: parser.error("source and revised must be different files")
    if args.mask and not args.mask.is_file(): parser.error(f"mask is not a file: {args.mask}")
    data = json.loads(log.read_text(encoding="utf-8")); edits = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits, list) or not edits: parser.error("edit-log must contain a non-empty edits array")
    if any(not isinstance(edit, dict) or edit.get("approved") is not True for edit in edits): parser.error("every edit must be explicitly approved")
    if any(edit_bbox(edit) is None for edit in edits): parser.error("every edit needs a bbox (x,y,w,h) for QA")
    # The replacement may legitimately run past the selected box (a longer
    # string); the mask is the authoritative edited region.  qa_gate needs both
    # bbox (where the new text lives) and region (everything it may touch).
    edited_bbox = mask_bbox(args.mask) if args.mask else None
    qa_edits = []
    for edit in edits:
        box = edit_bbox(edit)
        region = union_bbox(box, edited_bbox)
        entry = dict(edit)
        entry["region"] = [region[0], region[1], region[0] + region[2], region[1] + region[3]]
        entry.setdefault("expected_text", edit.get("replacement_text") or edit.get("text") or "")
        qa_edits.append(entry)
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()
    source_copy = out / ("original-reference" + source.suffix.lower())
    shutil.copy2(source, source_copy)
    warnings: list[str] = []
    blocking: list[str] = []
    layer = None

    # ---- build the deliverable and the page pairs the gate will compare ----
    if revised:
        deliverable, layer, encoding = save_revised_image(source, revised, out)
        jpeg_qtables_match = encoding["jpeg_qtables_match"]
        pdf_repack = "n/a"
        gate_pairs = [(source, layer if layer != deliverable else deliverable)]
    else:
        try:
            deliverable, pdf_repack, warning = write_pdf(source, pages, out)
        except RuntimeError as exc:
            print(f"export_revision: PDF repack failed: {exc}", file=sys.stderr)
            return 2
        if warning: warnings.append(warning)
        for index, page in enumerate(pages, 1): shutil.copy2(page, out / f"page-{index:03d}-revised{page.suffix.lower()}")
        if args.source_pages:
            references = [open_rgb(page) for page in args.source_pages]
            reference_note = None
            if len(references) != len(pages):
                blocking.append(f"--source-pages has {len(references)} file(s) but {len(pages)} revised page image(s) were supplied")
        elif source.suffix.lower() == ".pdf":
            references, reference_note = pdf_reference_pages(source)
        elif len(pages) == 1:
            references, reference_note = [open_rgb(source)], None
        else:
            references, reference_note = [], "multi-page export from a single image source has no per-page reference"
        if reference_note: blocking.append(reference_note)
        elif len(references) != len(pages):
            blocking.append(f"source produced {len(references)} reference page(s) for {len(pages)} revised page image(s)")
        gate_pairs = []
        if not reference_note:
            for index, (reference, page) in enumerate(zip(references, pages), 1):
                page_size = image_size(page)
                if reference.size != page_size:
                    blocking.append(f"page {index}: reference is {reference.size[0]}x{reference.size[1]} but the revised page is "
                                    f"{page_size[0]}x{page_size[1]}; QA cannot compare them")
                    gate_pairs = []; break
                gate_pairs.append((reference, page))
        encoding = {"encoding": "pdf_repack", "jpeg_qtables_match": None}
        source_tables, output_tables = pdf_page_qtables(source), pdf_page_qtables(deliverable)
        if source_tables is not None and output_tables is not None:
            encoding["jpeg_qtables_match"] = source_tables == output_tables
        jpeg_qtables_match = encoding["jpeg_qtables_match"]
        if pdf_repack == "lossy_fallback":
            warnings.append("PDF page images were re-encoded; page encoders may differ from the source. Verify visually at 200%.")
    if args.mask:
        shutil.copy2(args.mask, out / "revision-mask.png")

    # ---- forced QA gate ----
    gate_reports = []
    if not blocking:
        for index, (reference, output) in enumerate(gate_pairs, 1):
            page_edits = [edit for edit in qa_edits if int(edit.get("page", 1) or 1) == index] or (qa_edits if len(gate_pairs) == 1 else [])
            try:
                if isinstance(reference, Image.Image):
                    with tempfile.TemporaryDirectory() as tmp:
                        reference_path = Path(tmp) / f"reference-{index:03d}.png"
                        reference.save(reference_path)
                        report = run_gate(reference_path, output, page_edits)
                else:
                    report = run_gate(reference, output, page_edits)
            except Exception as exc:
                report = {"pass": False, "metrics": {},
                          "failures": [f"qa_gate could not run: {type(exc).__name__}: {exc}"]}
            if not isinstance(report, dict):
                report = {"pass": False, "metrics": {},
                          "failures": [f"qa_gate returned {type(report).__name__}, not a result dict"]}
            gate_reports.append(report)
    combined_metrics: dict = {}
    failures: list[str] = list(blocking)
    combined_checks: list = []
    combined_skipped: list = []
    combined_per_edit: list = []
    if len(gate_reports) == 1:
        combined_metrics = normalise_metrics(gate_reports[0].get("metrics"))
        combined_checks = list(gate_reports[0].get("checks") or [])
        combined_skipped = list(gate_reports[0].get("skipped") or [])
        combined_per_edit = list(gate_reports[0].get("per_edit") or [])
    else:
        for index, report in enumerate(gate_reports, 1):
            for name, value in normalise_metrics(report.get("metrics")).items():
                combined_metrics[f"page{index}.{name}"] = value
            for check in report.get("checks") or []:
                combined_checks.append({**check, "name": f"page{index}.{check.get('name')}"})
            for skipped in report.get("skipped") or []:
                combined_skipped.append({**skipped, "name": f"page{index}.{skipped.get('name')}"})
            for item in report.get("per_edit") or []:
                combined_per_edit.append({**item, "page": index})
    for index, report in enumerate(gate_reports, 1):
        prefix = f"page {index}: " if len(gate_reports) > 1 else ""
        failures += [f"{prefix}{failure}" for failure in failure_list(report)]
    passed = bool(gate_reports) and not blocking and all(report.get("pass") for report in gate_reports)
    qa = {"pass": passed, "metrics": combined_metrics, "failures": failures,
          "checks": combined_checks, "skipped": combined_skipped,
          "per_edit": combined_per_edit, "pages": len(gate_reports), "report": "qa-report.json"}

    # ---- provenance ----
    regions = []
    source_image = open_rgb(source) if source.suffix.lower() in IMAGE_SUFFIXES else None
    deliverable_image = open_rgb(deliverable) if deliverable.suffix.lower() in IMAGE_SUFFIXES else None
    for index, edit in enumerate(edits, 1):
        box = edit_bbox(edit)
        region = union_bbox(box, edited_bbox)
        regions.append({"index": index, "page": int(edit.get("page", 1) or 1), "bbox": list(box) if box else None,
                        "region": [region[0], region[1], region[0] + region[2], region[1] + region[3]] if region else None,
                        "char_sources": char_sources(edit),
                        "hash_algorithm": "sha256/raw-rgb-crop",
                        "source_region_sha256": region_digest(source_image, box),
                        "revised_region_sha256": region_digest(deliverable_image, box)})
    if any(region["bbox"] is None for region in regions):
        warnings.append("one or more edits have no parsable bbox; their region hashes are null")
    status = "approved_revision_sample" if passed else "qa_failed"
    manifest = {
        "created_at": created_at,
        "source_sha256": digest(source),
        "revised_sha256": digest(deliverable),
        "edit_count": len(edits),
        "status": status,
        "source_copy": source_copy.name,
        "revised_copy": deliverable.name,
        "page_images": len(pages) if pages else None,
        "regions": regions,
        "mask_bbox": list(edited_bbox) if edited_bbox else None,
        "qa": {"pass": passed, "failure_count": len(failures), "failures": failures,
               "report": "qa-report.json", "metrics": combined_metrics,
               "checks": combined_checks, "skipped": combined_skipped, "pages": len(gate_reports)},
        "jpeg_qtables_match": jpeg_qtables_match,
        "pdf_repack": pdf_repack,
        "lossless_layer": layer.name if revised and layer != deliverable else None,
        "lossless_layer_sha256": digest(layer) if revised and layer != deliverable else None,
        "encoding": encoding,
    }
    context = {"created_at": created_at, "status": status, "source": str(source),
               "deliverable": str(deliverable), "edit_count": len(edits), "qa": qa,
               "jpeg_qtables_match": jpeg_qtables_match, "pdf_repack": pdf_repack,
               "source_sha256": manifest["source_sha256"], "revised_sha256": manifest["revised_sha256"],
               "regions": regions, "warnings": warnings}
    qa_report = {"generated_at": created_at, "source": str(source), "output": str(deliverable),
                 "edit_count": len(edits), **qa}
    (out / "qa-report.json").write_text(json.dumps(jsonable(qa_report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(jsonable(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "edit-log.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "review-report.md").write_text(build_review_report(context), encoding="utf-8")
    print(json.dumps({"status": status, "qa_pass": passed, "output_dir": str(out),
                      "files": sorted(path.name for path in out.iterdir()),
                      "failures": failures}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__": raise SystemExit(main())
