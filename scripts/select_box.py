#!/usr/bin/env python3
"""Interactive browser-based bounding box selector for scan-text-revision."""
from __future__ import annotations
import argparse, http.server, json, os, socketserver, threading, webbrowser
from pathlib import Path
from PIL import Image

class SelectorServer(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, image_path: Path, html_path: Path, output_json: Path, **kwargs):
        self.image_path = image_path
        self.html_path = html_path
        self.output_json = output_json
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            with open(self.html_path, "rb") as f:
                self.wfile.write(f.read())
        elif self.path == "/image":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            with open(self.image_path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/submit":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode("utf-8"))
            
            # Format: bbox = [x, y, w, h], text = str
            b = data.get("bbox", {})
            bbox_list = [b.get("x", 0), b.get("y", 0), b.get("w", 0), b.get("h", 0)]
            result = {
                "status": "selected",
                "bbox": bbox_list,
                "bbox_str": f"{bbox_list[0]},{bbox_list[1]},{bbox_list[2]},{bbox_list[3]}",
                "text": data.get("text", "").strip(),
                "image": str(self.image_path.resolve())
            }
            with open(self.output_json, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
            
            # Shutdown server shortly after submit
            threading.Thread(target=self.server.shutdown).start()
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass  # quiet

def launch_selector(image_path: Path, output_json: Path, port: int = 8765) -> dict | None:
    html_path = Path(__file__).parent / "selector.html"
    if not html_path.is_file():
        raise FileNotFoundError(f"selector.html not found: {html_path}")
    if not image_path.is_file():
        raise FileNotFoundError(f"image not found: {image_path}")

    handler = lambda *args, **kwargs: SelectorServer(
        *args, image_path=image_path, html_path=html_path, output_json=output_json, **kwargs
    )
    
    socketserver.TCPServer.allow_reuse_address = True
    httpd = None
    actual_port = port
    for p in range(port, port + 20):
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", p), handler)
            actual_port = p
            break
        except OSError:
            continue
            
    if httpd is None:
        raise RuntimeError(f"Could not bind to any port in range {port}-{port+20}")

    with httpd:
        url = f"http://127.0.0.1:{actual_port}"
        print(f"SELECTOR_RUNNING: {url}")
        webbrowser.open(url)
        httpd.serve_forever()

    if output_json.is_file():
        with open(output_json, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def main():
    parser = argparse.ArgumentParser(description="Interactive Box Selector")
    parser.add_argument("image", type=Path, help="Path to scanned page image")
    parser.add_argument("--output", type=Path, default=Path("selected_task.json"), help="Output task JSON path")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    res = launch_selector(args.image, args.output, args.port)
    if res:
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return 1

if __name__ == "__main__":
    main()

