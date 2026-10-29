"""The browser preview: serves the PDF.js viewer, the PDF, and server-sent events.

  GET  /         the viewer
  GET  /pdf      the PDF of the current root
  GET  /events   server-sent events: reload (with ms, how long the build took, if it was timed), forward (boxes to highlight), say (a status line) and
                 errors (why the last build failed, or none)
  POST /inverse  {page,x,y}: open the source under a double-click in the editor
"""

import json
import queue
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).parent

# Answers a double-click at (page, x, y) with an HTTP status, a message and boxes to highlight.
Inverse = Callable[[int, float, float], tuple[int, str, list[dict[str, Any]]]]


def signature(pdf: Path | None) -> tuple[Any, ...]:
    """What changes when the PDF is rebuilt: its path, and its mtime and size if it exists."""
    try:
        st = pdf.stat() if pdf else None
    except FileNotFoundError:
        st = None
    return (pdf, st.st_mtime_ns, st.st_size) if st else (pdf,)


class Preview:
    def __init__(self, inverse: Inverse) -> None:
        self.inverse = inverse
        # The PDF the viewer shows; None until a root is known.
        self.pdf: Path | None = None
        # The PDF as the viewer last loaded it, so the watcher doesn't reload it again.
        self.seen = signature(None)
        # The last build's errors, for viewers that connect after it.
        self.errors: list[str] = []
        self.clients: list[queue.Queue[dict[str, Any]]] = []
        self.lock = threading.Lock()

    def broadcast(self, event: dict[str, Any]) -> None:
        with self.lock:
            for q in self.clients:
                q.put(event)

    def show(self, pdf: Path, ms: int | None = None) -> None:
        """Switch the viewer to another PDF, or reload this one; ms is how long building it took."""
        self.pdf = pdf
        self.seen = signature(pdf)
        self.broadcast({"type": "reload"} if ms is None else {"type": "reload", "ms": ms})

    def show_errors(self, errors: list[str]) -> None:
        if errors != self.errors:
            self.errors = errors
            self.broadcast({"type": "errors", "errors": errors})

    def listen(self, host: str, port: int) -> str:
        """Serve on host:port (0 picks a free port) and return the URL. Raises OSError if taken."""
        server = ThreadingHTTPServer((host, port), self.handler())
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        threading.Thread(target=self.watch, daemon=True).start()
        return f"http://{host}:{server.server_address[1]}"

    def watch(self) -> None:
        """Broadcast a reload once the PDF has changed and stopped growing."""
        while True:
            pdf = self.pdf
            sig = signature(pdf)
            if sig != self.seen:
                time.sleep(0.15)
                if signature(pdf) == sig and self.pdf == pdf:
                    if len(sig) > 1:
                        self.broadcast({"type": "reload"})
                    self.seen = sig
            time.sleep(0.2)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/":
                    self.send(200, (HERE / "index.html").read_bytes(), "text/html")
                elif path == "/pdf":
                    try:
                        if preview.pdf is None:
                            raise FileNotFoundError
                        self.send(200, preview.pdf.read_bytes(), "application/pdf")
                    except FileNotFoundError:
                        self.send(404, b"no pdf yet", "text/plain")
                elif path == "/events":
                    self.events()
                else:
                    self.send(404, b"not found", "text/plain")

            def do_POST(self):
                if urlparse(self.path).path != "/inverse":
                    return self.send(404, b"not found", "text/plain")
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                code, msg, boxes = preview.inverse(int(body["page"]), float(body["x"]), float(body["y"]))
                self.send(code, json.dumps({"msg": msg, "boxes": boxes}).encode(), "application/json")

            def events(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q: queue.Queue[dict[str, Any]] = queue.Queue()
                if preview.errors:
                    q.put({"type": "errors", "errors": preview.errors})
                with preview.lock:
                    preview.clients.append(q)
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
                    with preview.lock:
                        preview.clients.remove(q)

        return Handler
