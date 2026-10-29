"""Builds the fixture paper and drives the language server over stdio, as Helix does."""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = Path(__file__).parent / "fixtures" / "paper"
# tectonic needs the network on its first run; the nix check builds with pdflatex instead.
ENGINE = os.environ.get("LATEX_PREVIEW_TEST_ENGINE", "tectonic")
# The server to test: src/, or the nix check's packaged one.
SERVER = os.environ.get("LATEX_PREVIEW_TEST_SERVER")


def build(paper: Path) -> None:
    out = paper / "out"
    out.mkdir(exist_ok=True)
    if ENGINE == "tectonic":
        cmd = ["tectonic", "-X", "compile", "main.tex", "--synctex", "--outdir", "out"]
    else:
        cmd = [ENGINE, "-synctex=1", "-interaction=nonstopmode", "-output-directory=out", "main.tex"]
    subprocess.run(cmd, cwd=paper, check=True, capture_output=True)


@pytest.fixture(scope="session")
def paper(tmp_path_factory) -> Path:
    """A built copy of the fixture paper, shared by the tests that don't change it."""
    paper = tmp_path_factory.mktemp("built") / "paper"
    shutil.copytree(FIXTURE, paper)
    build(paper)
    return paper


class Client:
    def __init__(self, workspace: Path, env: dict[str, str] | None = None, settings: dict[str, Any] | None = None):
        self.proc = subprocess.Popen(
            [SERVER] if SERVER else [sys.executable, str(ROOT / "src" / "latex_preview.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env={**os.environ, "LATEX_PREVIEW_PORT": "0", **(env or {})},
        )
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.backlog: list[dict[str, Any]] = []
        self.next_id = 0
        # What to answer window/showDocument with; None leaves it unanswered.
        self.show_document: dict[str, Any] | None = {"success": True}
        self.shown: queue.Queue[dict[str, Any]] = queue.Queue()
        threading.Thread(target=self.reader, daemon=True).start()
        self.request(
            "initialize",
            {
                "rootUri": workspace.as_uri(),
                "capabilities": {"general": {"positionEncodings": ["utf-32", "utf-16"]}},
                # Most tests check the PDF in out/; builds as you type have their own.
                "initializationOptions": {"preview": {"onType": False, **(settings or {})}},
            },
        )
        self.notify("initialized", {})

    def reader(self) -> None:
        out = self.proc.stdout
        while True:
            length = None
            while (line := out.readline().strip()) != b"":
                key, _, value = line.decode().partition(":")
                if key.lower() == "content-length":
                    length = int(value)
            if length is None:
                return
            msg = json.loads(out.read(length))
            if msg.get("method") == "window/showDocument":
                self.shown.put(msg["params"])
                if self.show_document is not None:
                    self.send({"id": msg["id"], "result": self.show_document})
            else:
                self.messages.put(msg)

    def send(self, msg: dict[str, Any]) -> None:
        body = json.dumps({"jsonrpc": "2.0", **msg}).encode()
        self.proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.proc.stdin.flush()

    def notify(self, method: str, params: Any) -> None:
        self.send({"method": method, "params": params})

    def request(self, method: str, params: Any) -> Any:
        self.next_id += 1
        id = self.next_id
        self.send({"id": id, "method": method, "params": params})
        msg = self.wait(lambda m: m.get("id") == id and "method" not in m)
        assert "error" not in msg, msg
        return msg.get("result")

    def wait(self, match, timeout: float = 20) -> dict[str, Any]:
        """The first message that matches, taken out of those received; the rest are kept."""
        for i, msg in enumerate(self.backlog):
            if match(msg):
                return self.backlog.pop(i)
        while True:
            msg = self.messages.get(timeout=timeout)
            if match(msg):
                return msg
            self.backlog.append(msg)

    def shown_message(self, timeout: float = 20) -> dict[str, Any]:
        return self.wait(lambda m: m.get("method") == "window/showMessage", timeout)["params"]

    def open(self, path: Path) -> None:
        self.notify(
            "textDocument/didOpen",
            {"textDocument": {"uri": path.as_uri(), "languageId": "latex", "version": 1, "text": path.read_text()}},
        )

    def change(self, path: Path, text: str) -> None:
        self.notify(
            "textDocument/didChange",
            {"textDocument": {"uri": path.as_uri(), "version": 2}, "contentChanges": [{"text": text}]},
        )

    def hover(self, path: Path, line: int, character: int) -> Any:
        return self.request(
            "textDocument/hover",
            {"textDocument": {"uri": path.as_uri()}, "position": {"line": line, "character": character}},
        )

    def command(self, name: str) -> Any:
        return self.request("workspace/executeCommand", {"command": name, "arguments": []})

    def close(self) -> int:
        self.request("shutdown", None)
        self.notify("exit", None)
        return self.proc.wait(timeout=10)


class Events:
    """The viewer's server-sent events."""

    def __init__(self, url: str):
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stream = urllib.request.urlopen(f"{url}/events", timeout=30)
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self) -> None:
        try:
            for line in self.stream:
                if line.startswith(b"data: "):
                    self.queue.put(json.loads(line[6:]))
        except OSError:
            pass

    def next(self, type: str, timeout: float = 20) -> dict[str, Any]:
        while True:
            event = self.queue.get(timeout=timeout)
            if event["type"] == type:
                return event


@pytest.fixture
def client(paper, tmp_path):
    clients = []

    def start(workspace: Path = paper, env: dict[str, str] | None = None, **kwargs) -> Client:
        # Each test's builds get their own scratch directories.
        c = Client(workspace, env={"XDG_RUNTIME_DIR": str(tmp_path), **(env or {})}, **kwargs)
        clients.append(c)
        return c

    yield start
    for c in clients:
        if c.proc.poll() is None:
            c.proc.kill()
