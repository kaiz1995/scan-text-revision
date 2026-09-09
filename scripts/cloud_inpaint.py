#!/usr/bin/env python3
"""Cloud inpaint adapter: send a masked crop to gpt-image-2.5 edits endpoint."""
from __future__ import annotations
import argparse, base64, hashlib, json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()

def _load_env(env_path: Path | None = None):
    """Read .env from cwd or explicit path; set only SCAN_REVISION_* vars."""
    candidates = [env_path] if env_path else [Path.cwd() / ".env"]
    for p in candidates:
        if p and p.is_file():
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k.startswith("SCAN_REVISION_"):
                            os.environ.setdefault(k, v)

def _pad_to_minimum(img, min_edge=832):
    """Pad image so both edges >= min_edge (multiple of 16). Returns (padded, offsets)."""
    from PIL import Image
    w, h = img.size
    def _round_up(val, m=16):
        return val if val % m == 0 else val + (m - val % m)
    nw = max(w, min_edge)
    nh = max(h, min_edge)
    nw, nh = _round_up(nw), _round_up(nh)
    if nw == w and nh == h:
        return img, (0, 0, w, h)
    padded = Image.new("RGBA", (nw, nh), (255, 255, 255, 255))
    ox, oy = (nw - w) // 2, (nh - h) // 2
    padded.paste(img, (ox, oy))
    return padded, (ox, oy, w, h)

def _pad_mask(mask_img, target_size, offsets):
    """Expand mask to match padded image; padded area is opaque (no edit)."""
    from PIL import Image
    ox, oy, ow, oh = offsets
    tw, th = target_size
    # gpt-image-2.5 mask: transparent = edit, opaque = keep
    padded = Image.new("RGBA", (tw, th), (0, 0, 0, 255))
    # Convert grayscale mask to RGBA with alpha channel
    if mask_img.mode == "L":
        r, g, b = Image.new("L", mask_img.size, 0), Image.new("L", mask_img.size, 0), Image.new("L", mask_img.size, 0)
        # Where mask is white (255) = edit region -> make transparent
        alpha = Image.eval(mask_img, lambda x: 255 - x)
        rgba_mask = Image.merge("RGBA", (r, g, b, alpha))
    elif mask_img.mode == "RGBA":
        rgba_mask = mask_img
    else:
        rgba_mask = mask_img.convert("RGBA")
    padded.paste(rgba_mask, (ox, oy))
    return padded

def cloud_inpaint(crop_path: Path, mask_path: Path, prompt: str,
                  output_dir: Path, n: int = 1, quality: str = "medium",
                  env_path: Path | None = None) -> list[dict]:
    """Call gpt-image-2.5 edits endpoint; return candidate records."""
    from PIL import Image
    import io
    _load_env(env_path)
    api_key = os.environ.get("SCAN_REVISION_API_KEY", "")
    base_url = os.environ.get("SCAN_REVISION_BASE_URL", "").rstrip("/")
    model = os.environ.get("SCAN_REVISION_MODEL", "gpt-image-2.5")
    if not api_key:
        return [{"status": "error", "error": "SCAN_REVISION_API_KEY not set"}]
    if not base_url:
        return [{"status": "error", "error": "SCAN_REVISION_BASE_URL not set"}]

    crop_img = Image.open(crop_path).convert("RGBA")
    mask_img = Image.open(mask_path)
    input_hash = _sha256(crop_path)

    # Pad if needed
    padded_crop, offsets = _pad_to_minimum(crop_img)
    padded_mask = _pad_mask(mask_img, padded_crop.size, offsets)
    pw, ph = padded_crop.size
    size_str = f"{pw}x{ph}"

    # Save temp files for upload
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_crop = output_dir / "_tmp_crop.png"
    tmp_mask = output_dir / "_tmp_mask.png"
    padded_crop.save(tmp_crop, "PNG")
    padded_mask.save(tmp_mask, "PNG")

    try:
        import httpx
    except ImportError:
        return [{"status": "error", "error": "Dependency 'httpx' is required for cloud inpainting API calls. Run: pip install httpx"}]

    url = f"{base_url}/v1/images/edits"
    headers = {"Authorization": f"Bearer {api_key}"}

    candidates = []
    try:
        with httpx.Client(timeout=120, trust_env=False) as client:
            with open(tmp_crop, "rb") as img_f, open(tmp_mask, "rb") as mask_f:
                files = {
                    "image": ("crop.png", img_f, "image/png"),
                    "mask": ("mask.png", mask_f, "image/png"),
                }
                data = {
                    "model": model,
                    "prompt": prompt,
                    "n": str(n),
                    "size": size_str,
                    "quality": quality,
                    "response_format": "b64_json",
                }
                resp = client.post(url, headers=headers, files=files, data=data)

        if resp.status_code != 200:
            return [{"status": "error", "error": f"HTTP {resp.status_code}: {resp.text[:500]}"}]

        result = resp.json()
        ts = datetime.now(timezone.utc).isoformat()

        for i, item in enumerate(result.get("data", [])):
            b64 = item.get("b64_json", "")
            if not b64:
                candidates.append({"status": "error", "error": "empty b64_json", "index": i})
                continue
            img_bytes = base64.b64decode(b64)
            cand_img = Image.open(io.BytesIO(img_bytes))
            
            # Auto-adapt resolution if cloud model drifted from requested canvas size
            if cand_img.size != (pw, ph):
                cand_img = cand_img.resize((pw, ph), Image.Resampling.LANCZOS)
                
            # Crop back to original region
            ox, oy, ow, oh = offsets
            cand_cropped = cand_img.crop((ox, oy, ox + ow, oy + oh))
            out_path = output_dir / f"cloud_candidate_{i}.png"
            cand_cropped.save(out_path, "PNG")
            out_hash = hashlib.sha256(img_bytes).hexdigest()
            candidates.append({
                "status": "candidate",
                "image": str(out_path.resolve()),
                "model": model,
                "created_at": ts,
                "input_sha256": input_hash,
                "output_sha256": out_hash,
                "padded_size": size_str,
                "approved": False,
            })
    except Exception as exc:
        candidates.append({"status": "error", "error": str(exc)[:500]})
    finally:
        for tmp in (tmp_crop, tmp_mask):
            try: tmp.unlink(missing_ok=True)
            except OSError: pass

    return candidates

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("crop", type=Path, help="Cropped region PNG")
    parser.add_argument("mask", type=Path, help="Alpha mask PNG (white=edit)")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--quality", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--env", type=Path, help="Path to .env file")
    args = parser.parse_args()
    if not args.crop.is_file():
        parser.error(f"crop not found: {args.crop}")
    if not args.mask.is_file():
        parser.error(f"mask not found: {args.mask}")
    results = cloud_inpaint(args.crop, args.mask, args.prompt,
                            args.output_dir, args.n, args.quality, args.env)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if any(r.get("status") == "candidate" for r in results) else 1

if __name__ == "__main__":
    raise SystemExit(main())

