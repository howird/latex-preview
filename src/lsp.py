"""The language server Helix runs: serves the preview, scrolls it to the last hover on
`latex.showInPreview`, and opens the source of a double-click in the editor.

The preview shows the PDF of one root document (a file with \\documentclass). Opening or hovering a
file the current root includes keeps that root; a file only another root includes switches to it.
"""

import getpass
import json
import os
import socket
import sys
import threading
import traceback
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlparse

from locate import pdf_location, source_at
from preview import Preview
from project import Project

SHOW_IN_PREVIEW = "latex.showInPreview"
PREVIEW_URL = "latex.previewUrl"
# window/showMessage types.
ERROR, INFO = 1, 3
# How long a double-click waits for the editor to open the file.
SHOW_DOCUMENT_TIMEOUT = 5


def to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def pdf_of(root: Path) -> Path:
    """Where latex-build writes the root's PDF."""
    return root.parent / "out" / f"{root.stem}.pdf"


class Connection:
    """JSON-RPC over stdio, as LSP frames it. Requests and notifications may be sent from any thread."""

    def __init__(self, inp: BinaryIO, out: BinaryIO) -> None:
        self.inp, self.out = inp, out
        self.lock = threading.Lock()
        self.pending: dict[int, Future[Any]] = {}
        self.next_id = 0

    def read(self) -> dict[str, Any] | None:
        length = None
        while True:
            line = self.inp.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            key, _, value = line.decode("ascii").partition(":")
            if key.lower() == "content-length":
                length = int(value)
        if length is None:
            return None
        return json.loads(self.inp.read(length))

    def write(self, msg: dict[str, Any]) -> None:
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
        with self.lock:
            self.out.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            self.out.flush()

    def notify(self, method: str, params: Any) -> None:
        self.write({"method": method, "params": params})

    def request(self, method: str, params: Any) -> Future[Any]:
        future: Future[Any] = Future()
        with self.lock:
            self.next_id += 1
            id = self.next_id
            self.pending[id] = future
        self.write({"id": id, "method": method, "params": params})
        return future

    def resolve(self, msg: dict[str, Any]) -> None:
        """Settle the request a response from the editor answers."""
        with self.lock:
            future = self.pending.pop(msg.get("id"), None)
        if future is None:
            return
        if "error" in msg:
            future.set_exception(RuntimeError(msg["error"].get("message", "error")))
        else:
            future.set_result(msg.get("result"))


class Server:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.preview = Preview(self.inverse)
        self.url: str | None = None
        self.settings: dict[str, Any] = {}
        # Columns arrive in UTF-16 code units unless the editor agrees to code points.
        self.utf32 = False
        self.project: Project | None = None
        # Open documents' text, by path.
        self.docs: dict[Path, str] = {}
        # The document the preview shows, and where the last hover was: (file, line, column), 1-based.
        self.root: Path | None = None
        self.hover: tuple[Path, int, int] | None = None
        self.shutdown = False

    # Messages to the editor.

    def log(self, message: str) -> None:
        self.conn.notify("window/logMessage", {"type": INFO, "message": f"[latex-preview] {message}"})

    def show_message(self, type: int, message: str) -> None:
        self.conn.notify("window/showMessage", {"type": type, "message": message})

    def say(self, message: str) -> None:
        """Show a message in the viewer's status line."""
        self.preview.broadcast({"type": "say", "msg": message})

    # Roots.

    def read(self, path: Path) -> str | None:
        if path in self.docs:
            return self.docs[path]
        try:
            return path.read_text(errors="replace")
        except OSError:
            return None

    def follow(self, file: Path) -> None:
        """Show the root `file` belongs to, unless it belongs to none."""
        override = self.settings.get("root")
        if isinstance(override, str) and self.project is not None:
            root: Path | None = (self.project.workspace / override).resolve()
        elif self.project is not None:
            root = self.project.root_for(file, self.root)
        else:
            root = None
        if root is not None and root != self.root:
            self.root = root
            self.log(f"previewing {root}")
            self.preview.show(pdf_of(root))

    # Requests from the viewer.

    def inverse(self, page: int, x: float, y: float) -> tuple[int, str, list[dict[str, Any]]]:
        if self.root is None:
            return 404, "Open a .tex file in your editor first", []
        pdf = pdf_of(self.root)
        hit = source_at(pdf, page, x, y)
        if hit is None:
            return 404, "No source for that spot", []
        src, line = hit
        # Show which source line the click resolved to, whether or not the editor gets it.
        span, lines, _ = pdf_location(pdf, src, line)
        boxes = span or lines
        start = {"line": line - 1, "character": 0}
        future = self.conn.request(
            "window/showDocument",
            {"uri": Path(src).resolve().as_uri(), "takeFocus": True, "selection": {"start": start, "end": start}},
        )
        try:
            result = future.result(timeout=SHOW_DOCUMENT_TIMEOUT)
        except TimeoutError:
            return 504, "The editor didn't answer", boxes
        except RuntimeError as e:
            return 502, f"The editor couldn't open {src}: {e}", boxes
        if not (result or {}).get("success"):
            return 502, f"The editor couldn't open {src}", boxes
        return 200, f"{src}:{line}", boxes

    # Requests and notifications from the editor.

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        options = params.get("initializationOptions") or {}
        preview = options.get("preview") if isinstance(options, dict) else None
        self.settings = preview if isinstance(preview, dict) else {}
        encodings = params.get("capabilities", {}).get("general", {}).get("positionEncodings", [])
        self.utf32 = "utf-32" in encodings
        workspace = params.get("rootUri")
        folders = params.get("workspaceFolders") or []
        if not workspace and folders:
            workspace = folders[0]["uri"]
        root = to_path(workspace) if workspace else Path.cwd()
        self.project = Project(root, self.read)
        return {
            "capabilities": {
                "positionEncoding": "utf-32" if self.utf32 else "utf-16",
                "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
                "hoverProvider": True,
                "executeCommandProvider": {"commands": [SHOW_IN_PREVIEW, PREVIEW_URL]},
            },
            "serverInfo": {"name": "latex-preview"},
        }

    def initialized(self, _: Any) -> None:
        host = os.environ.get("LATEX_PREVIEW_HOST") or self.settings.get("host") or "127.0.0.1"
        port = os.environ.get("LATEX_PREVIEW_PORT") or self.settings.get("port") or 0
        try:
            self.url = self.preview.listen(str(host), int(port))
        except (OSError, ValueError) as e:
            self.show_message(ERROR, f"latex-preview: can't listen on {host}:{port}: {e}")
            return
        self.show_message(INFO, self.describe_url())
        if self.project is not None:
            self.project.scan()

    def describe_url(self) -> str:
        if self.url is None:
            return "latex-preview: not listening; see the error when it started"
        port = self.url.rsplit(":", 1)[1]
        host = urlparse(self.url).hostname
        tunnel = f"ssh -NL {port}:{host}:{port} {getpass.getuser()}@{socket.gethostname()}"
        return f"latex-preview: {self.url} (from your laptop: {tunnel})"

    def did_open(self, params: dict[str, Any]) -> None:
        doc = params["textDocument"]
        path = to_path(doc["uri"])
        self.docs[path] = doc["text"]
        if self.project is not None and path.suffix == ".tex":
            self.project.scan()
            self.follow(path)

    def did_change(self, params: dict[str, Any]) -> None:
        path = to_path(params["textDocument"]["uri"])
        changes = params["contentChanges"]
        if changes:
            self.docs[path] = changes[-1]["text"]

    def did_save(self, params: dict[str, Any]) -> None:
        if self.project is not None and to_path(params["textDocument"]["uri"]).suffix == ".tex":
            self.project.scan()

    def did_close(self, params: dict[str, Any]) -> None:
        self.docs.pop(to_path(params["textDocument"]["uri"]), None)

    def on_hover(self, params: dict[str, Any]) -> None:
        path = to_path(params["textDocument"]["uri"])
        pos = params["position"]
        self.hover = (path, pos["line"] + 1, self.column(path, pos["line"], pos["character"]) + 1)
        self.follow(path)
        # The hover page is texlab's; this only notes where the cursor is.
        return None

    def column(self, path: Path, line: int, character: int) -> int:
        """A position's character offset as a Python string index."""
        if self.utf32:
            return character
        lines = (self.read(path) or "").splitlines()
        text = lines[line] if line < len(lines) else ""
        units = 0
        for i, c in enumerate(text):
            if units >= character:
                return i
            units += 2 if ord(c) > 0xFFFF else 1
        return len(text)

    def execute_command(self, params: dict[str, Any]) -> None:
        command = params.get("command")
        if command == PREVIEW_URL:
            self.show_message(INFO, self.describe_url())
        elif command == SHOW_IN_PREVIEW:
            self.forward()

    def forward(self) -> None:
        if self.hover is None or self.root is None:
            return
        file, line, col = self.hover
        span, lines, words = pdf_location(pdf_of(self.root), str(file), line, col)
        if not lines:
            name = os.path.relpath(file, self.root.parent)
            return self.say(f"No PDF location for {name}:{line}; is the PDF built from it?")
        self.preview.broadcast({"type": "forward", "boxes": span or lines, "words": words})

    # The loop.

    def handle(self, msg: dict[str, Any]) -> None:
        method, params, id = msg.get("method"), msg.get("params"), msg.get("id")
        requests = {
            "initialize": self.initialize,
            "shutdown": lambda _: setattr(self, "shutdown", True),
            "textDocument/hover": self.on_hover,
            "workspace/executeCommand": self.execute_command,
        }
        notifications = {
            "initialized": self.initialized,
            "textDocument/didOpen": self.did_open,
            "textDocument/didChange": self.did_change,
            "textDocument/didSave": self.did_save,
            "textDocument/didClose": self.did_close,
        }
        try:
            if id is None:
                if method in notifications:
                    notifications[method](params)
            elif method in requests:
                self.conn.write({"id": id, "result": requests[method](params)})
            else:
                self.conn.write({"id": id, "error": {"code": -32601, "message": f"unknown method {method}"}})
        except Exception as e:
            traceback.print_exc()
            if id is not None:
                self.conn.write({"id": id, "error": {"code": -32603, "message": str(e)}})

    def run(self) -> int:
        while (msg := self.conn.read()) is not None:
            method = msg.get("method")
            if method is None:
                self.conn.resolve(msg)
            elif method == "exit":
                return 0 if self.shutdown else 1
            elif method == "workspace/executeCommand":
                # Forward search runs synctex and pdftotext; keep reading meanwhile.
                threading.Thread(target=self.handle, args=(msg,), daemon=True).start()
            else:
                self.handle(msg)
        return 0 if self.shutdown else 1


def main() -> None:
    # The protocol owns stdout: anything else printed, here or by a child process, goes to stderr.
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    sys.exit(Server(Connection(sys.stdin.buffer, out)).run())
