"""PDF preview server with SyncTeX in both directions.

Serves a PDF.js viewer that reloads when the PDF changes.
  GET /forward?file=F&line=N  scroll the viewer to F:N (editor -> pdf)
  POST /inverse {page,x,y}    open the source line in the editor via texlab (pdf -> editor)

  latex-preview serve PDF             start the server
  latex-preview forward FILE LINE     ask a running server to scroll to FILE:LINE
"""

import argparse
import getpass
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
PORT_FILE = ".latex-preview-port"
clients: list[queue.Queue] = []
clients_lock = threading.Lock()


def broadcast(event: dict) -> None:
    with clients_lock:
        for q in clients:
            q.put(event)


def synctex(*args: str) -> list[dict]:
    """Run synctex and return one dict per result record."""
    out = subprocess.run(["synctex", *args], capture_output=True, text=True).stdout
    records, cur = [], None
    for line in out.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        if key in ("Output", "Input") and cur is not None and key in cur:
            records.append(cur)
            cur = None
        if cur is None:
            cur = {}
        cur[key] = val
    if cur:
        records.append(cur)
    return records


def refine_line(pdf: Path, src: str, line: int, page: int, x: float, y: float) -> int:
    """Narrow a paragraph-level hit to the source line under the click.

    synctex edit often answers with the line where the paragraph ended, or a neighbouring one. Forward-search each
    line of that paragraph, and pick the one with a position on the clicked output line
    closest to the left of the click.
    """
    try:
        lines = Path(src).read_text().splitlines()
    except OSError:
        return line
    # The paragraph around the hit: synctex may answer with any of its lines.
    start = end = line
    while start > 1 and line - start < 40 and lines[start - 2].strip():
        start -= 1
    while end < len(lines) and end - line < 40 and lines[end].strip():
        end += 1
    if start == end:
        return line

    def points(n: int) -> list[tuple[int, float, float]]:
        recs = synctex("view", "-i", f"{n}:1:{src}", "-o", str(pdf))
        recs = [r for r in recs if r.get("Page") == str(page)]
        # Points on a line box's edge are synctex falling back to whole boxes, not text.
        return [
            (n, x, float(r["y"]))
            for r in recs
            if (x := float(r["x"])) - float(r["h"]) > 0.01
            and float(r["h"]) + float(r["W"]) - x > 0.01
        ]

    with ThreadPoolExecutor() as pool:
        pts = [p for ps in pool.map(points, range(start, end + 1)) for p in ps]
    if not pts:
        return line
    # Records sit on the baseline; a click lands a few points above it.
    near = min(abs(py - y - 3) for _, _, py in pts)
    if near > 8:  # nothing on the clicked row
        return line
    row = [p for p in pts if abs(p[2] - y - 3) <= near + 1]
    left = [p for p in row if p[1] <= x]
    return max(left, key=lambda p: p[1])[0] if left else min(row, key=lambda p: p[1])[0]


def watch_pdf(pdf: Path) -> None:
    """Broadcast a reload once the PDF has changed and stopped growing."""
    last = None
    while True:
        try:
            st = pdf.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            sig = None
        if sig != last:
            time.sleep(0.15)
            try:
                st = pdf.stat()
                settled = (st.st_mtime_ns, st.st_size) == sig
            except FileNotFoundError:
                settled = sig is None
            if settled:
                if last is not None and sig is not None:
                    broadcast({"type": "reload"})
                last = sig
        time.sleep(0.2)


def make_handler(pdf: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self.send(200, (HERE / "index.html").read_bytes(), "text/html")
            elif url.path == "/pdf":
                try:
                    self.send(200, pdf.read_bytes(), "application/pdf")
                except FileNotFoundError:
                    self.send(404, b"no pdf yet", "text/plain")
            elif url.path == "/events":
                self.events()
            elif url.path == "/forward":
                self.forward(parse_qs(url.query))
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path != "/inverse":
                return self.send(404, b"not found", "text/plain")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            spot = f"{body['page']}:{body['x']:.2f}:{body['y']:.2f}:{pdf}"
            hits = [r for r in synctex("edit", "-o", spot) if "Input" in r]
            if not hits:
                return self.send(404, b"no source for that spot", "text/plain")
            src = hits[0]["Input"]
            line = str(refine_line(pdf, src, int(hits[0]["Line"]), body["page"], body["x"], body["y"]))
            res = subprocess.run(
                ["texlab", "inverse-search", "-i", src, "--line1", line],
                capture_output=True, text=True,
            )
            if res.returncode != 0:
                warnings = texlab_warnings()
                for w in warnings:
                    print(f"warning: {w}", file=sys.stderr, flush=True)
                msg = warnings[0] if warnings else "texlab is not running; open a .tex file in your editor first"
                return self.send(502, msg.encode(), "text/plain")
            self.send(200, f"{src}:{line}".encode(), "text/plain")

        def forward(self, q: dict) -> None:
            file = os.path.abspath(q["file"][0])
            line = q["line"][0]
            col = q.get("col", ["1"])[0]
            hits = [r for r in synctex("view", "-i", f"{line}:{col}:{file}", "-o", str(pdf)) if "Page" in r]
            if not hits:
                return self.send(404, f"no pdf location for {file}:{line}\n".encode(), "text/plain")
            boxes = list({
                (int(r["Page"]), *(float(r[k]) for k in ("h", "v", "W", "H"))): None
                for r in hits
            })
            # Drop slivers like the review-mode line-number ruler next to the text.
            widest = max(b[3] for b in boxes)
            boxes = [
                dict(zip(("page", "h", "v", "W", "H"), b))
                for b in boxes
                if b[3] >= widest / 4
            ]
            broadcast({"type": "forward", "boxes": boxes})
            self.send(200, b"", "text/plain")

        def events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q: queue.Queue = queue.Queue()
            with clients_lock:
                clients.append(q)
            try:
                while True:
                    try:
                        msg = f"data: {json.dumps(q.get(timeout=15))}\n\n"
                    except queue.Empty:
                        msg = ": keepalive\n\n"
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with clients_lock:
                    clients.remove(q)

    return Handler


def texlab_version(exe: str) -> str:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "unknown version"
    return out.strip() or "unknown version"


def texlab_warnings() -> list[str]:
    """Explain why inverse search may reach the wrong texlab, or none at all.

    `texlab inverse-search` talks to whichever texlab owns $XDG_RUNTIME_DIR/texlab.sock: the one
    that started last. Linux only, since it reads /proc.
    """
    ours = shutil.which("texlab")
    if ours is None:
        return ["no texlab on PATH, so inverse search cannot work"]
    ours = os.path.realpath(ours)
    running = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if (proc / "comm").read_text().strip() == "texlab":
                running.append(os.path.realpath(proc / "exe"))
        except OSError:
            continue
    warnings = []
    if len(running) > 1:
        warnings.append(
            f"{len(running)} texlab processes are running; clicks may jump to the wrong editor "
            "(the one whose texlab started last)"
        )
    for exe in sorted(set(running) - {ours}):
        warnings.append(
            f"your editor's texlab ({texlab_version(exe)}, {exe}) differs from the one inverse "
            f"search runs ({texlab_version(ours)}, {ours})"
        )
    return warnings


def env_port() -> int | None:
    port = os.environ.get("LATEX_PREVIEW_PORT")
    return int(port) if port else None


def find_port_file(start: Path) -> Path | None:
    """The port file of the nearest server: in a directory above `start`, or its out/."""
    for d in [start, *start.parents]:
        for f in (d / PORT_FILE, d / "out" / PORT_FILE):
            if f.is_file():
                return f
    return None


def serve(args: argparse.Namespace) -> None:
    pdf = args.pdf.resolve()
    port = args.port or env_port() or 0
    threading.Thread(target=watch_pdf, args=(pdf,), daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(pdf))
    server.daemon_threads = True
    port = server.server_address[1]

    port_file = pdf.parent / PORT_FILE
    pdf.parent.mkdir(parents=True, exist_ok=True)
    port_file.write_text(f"{port}\n")
    print(f"on your laptop: ssh -NL {port}:127.0.0.1:{port} {getpass.getuser()}@{socket.gethostname()}")
    print(f"then open:      http://127.0.0.1:{port}", flush=True)
    for w in texlab_warnings():
        print(f"warning: {w}", file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Leave a newer server's port file alone.
        try:
            if port_file.read_text().strip() == str(port):
                port_file.unlink()
        except OSError:
            pass


def forward(args: argparse.Namespace) -> None:
    file = args.file.resolve()
    port = args.port or env_port()
    if port is None:
        port_file = find_port_file(file.parent) or find_port_file(Path.cwd())
        if port_file is None:
            sys.exit("latex-preview: no preview running (latex-preview serve out/main.pdf)")
        port = int(port_file.read_text())
    query = urllib.parse.urlencode({"file": str(file), "line": args.line, "col": args.col})
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/forward?{query}", timeout=10)
    except urllib.error.HTTPError as e:
        sys.exit(f"latex-preview: {e.read().decode().strip()}")
    except urllib.error.URLError:
        sys.exit(f"latex-preview: no preview on port {port} (latex-preview serve out/main.pdf)")


def main() -> None:
    ap = argparse.ArgumentParser(prog="latex-preview")
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="serve a live preview of PDF")
    sp.add_argument("pdf", type=Path)
    sp.add_argument("--port", type=int, help="default: $LATEX_PREVIEW_PORT, else a free port")
    sp.set_defaults(run=serve)

    fp = sub.add_parser("forward", help="scroll the running preview to FILE:LINE")
    fp.add_argument("file", type=Path)
    fp.add_argument("line", type=int)
    fp.add_argument("col", type=int, nargs="?", default=1)
    fp.add_argument("--port", type=int, help="default: $LATEX_PREVIEW_PORT, else the server's port file")
    fp.set_defaults(run=forward)

    args = ap.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
