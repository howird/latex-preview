"""PDF preview server with SyncTeX in both directions.

Serves a PDF.js viewer that reloads when the PDF changes.
  GET /forward?file=F&line=N[&col=C]  scroll the viewer to F:N, and the word at column C (editor -> pdf)
  POST /inverse {page,x,y}    open the source line in the editor via texlab (pdf -> editor)

  latex-preview serve PDF             start the server
  latex-preview forward FILE LINE [COL]  ask a running server to scroll to FILE:LINE
"""

import argparse
import functools
import getpass
import html
import json
import os
import queue
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
PORT_FILE = ".latex-preview-port"
# A PDF box from synctex: page (int) and h, v, W, H (float points).
Box = dict[str, Any]
# A PDF word: xMin, yMin, xMax, yMax, text.
Word = tuple[float, float, float, float, str]
clients: list[queue.Queue[dict[str, Any]]] = []
clients_lock = threading.Lock()


def broadcast(event: dict[str, Any]) -> None:
    with clients_lock:
        for q in clients:
            q.put(event)


def synctex(*args: str) -> list[dict[str, str]]:
    """Run synctex and return one dict per result record."""
    out = subprocess.run(["synctex", *args], capture_output=True, text=True, check=False).stdout
    records: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
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

    synctex edit often answers with the line where the paragraph ended, or a neighbouring one.
    Locate each line of that paragraph in the PDF and pick the one whose text is under the click.
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

    def spans(n: int) -> list[tuple[int, Box]]:
        return [(n, b) for b in pdf_location(pdf, src, n)[0] if b["page"] == page]

    with ThreadPoolExecutor() as pool:
        boxes = [p for ps in pool.map(spans, range(start, end + 1)) for p in ps]
    under = [
        n for n, b in boxes
        if b["v"] - b["H"] - 1 <= y <= b["v"] + 3 and b["h"] - 1 <= x <= b["h"] + b["W"] + 1
    ]
    if under:
        return under[0]

    # No source line's text found under the click (math, tables, floats, the gap between two
    # lines): fall back to synctex's own positions. They sit after a line's first word, not
    # before it, so this can pick the line above.
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


def norm(word: str) -> str:
    """A word reduced to what survives typesetting: ligatures split, no case, accents or punctuation."""
    # pdflatex accents an i by putting the accent on a dotless ı.
    word = unicodedata.normalize("NFKD", word).casefold().replace("ı", "i")
    return "".join(c for c in word if c.isalnum())


# An accent macro on a letter, as in Schr\"odinger or gar\c{c}on: the letter stays in its word.
ACCENT = re.compile(r"\\(?:[`'^\"~=.]|[cvHukr](?![a-zA-Z]) ?)\{?([a-zA-Z])\}?")

# Source text that never appears in the PDF as written: comments, math, commands and the
# arguments of commands that print something else (a citation's key, a label).
HIDDEN = re.compile(
    r"(?<!\\)%.*"
    r"|\$[^$]*\$"
    r"|\\(?:[a-z]*cite[a-z]*|[a-z]*ref|label|url|input|include|includegraphics)\*?(?:\[[^\]]*\])*\{[^}]*\}"
    r"|\\[a-zA-Z@]+\*?|\\."
    r"|[{}~]"
)


def source_words(text: str) -> list[tuple[int, int, str]]:
    """The words of a source line that reach the PDF, as (start, end, normalized word)."""
    # Keep columns: an accent macro becomes filler that norm drops, the rest becomes spaces.
    text = ACCENT.sub(lambda m: "\0" * (m.start(1) - m.start()) + m[1] + "\0" * (m.end() - m.end(1)), text)
    masked = HIDDEN.sub(lambda m: " " * len(m.group()), text)
    return [(m.start(), m.end(), w) for m in re.finditer(r"\S+", masked) if (w := norm(m.group()))]


def pdf_words(pdf: Path, page: int) -> list[Word]:
    """Every word on a page as (xMin, yMin, xMax, yMax, text), in points from the top left."""
    try:
        return page_words(str(pdf), page, pdf.stat().st_mtime_ns)
    except OSError:
        return []


@functools.lru_cache(maxsize=64)
def page_words(pdf: str, page: int, mtime: int) -> list[Word]:
    """pdf_words for one build of the PDF: a click forward-searches every line of a paragraph."""
    out = subprocess.run(
        ["pdftotext", "-bbox", "-f", str(page), "-l", str(page), pdf, "-"],
        capture_output=True, text=True, check=False,
    ).stdout
    return [
        (float(m[1]), float(m[2]), float(m[3]), float(m[4]), html.unescape(m[5]))
        for m in re.finditer(
            r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>', out
        )
    ]


def locate(pdf: Path, src: str, line: int, col: int | None, lines: list[Box]) -> tuple[list[Box], list[Box]]:
    """The boxes of a source line's own text, and of its word at column `col` (1-based).

    `lines` are the output lines synctex gave for the source line. They also hold the ends of
    neighbouring source lines, and words repeat, so the source line's words are aligned with the
    words on those lines. Either result is empty when the alignment finds nothing.
    """
    try:
        text = Path(src).read_text().splitlines()[line - 1]
    except (OSError, IndexError):
        return [], []
    src_words = source_words(text)
    if not src_words:
        return [], []

    # The output lines' words, row by row. A box many lines tall is synctex falling back to a
    # whole paragraph or float: split it into the rows of text inside it.
    rows: list[tuple[Box, list[Word]]] = []
    page_words = {p: pdf_words(pdf, p) for p in {b["page"] for b in lines}}
    for b in lines:
        inside = sorted(
            (w for w in page_words[b["page"]]
             if b["v"] - b["H"] - 1 <= (w[1] + w[3]) / 2 <= b["v"] + 3
             and b["h"] - 1 <= (w[0] + w[2]) / 2 <= b["h"] + b["W"] + 1),
            key=lambda w: w[3],
        )
        heights = [w[3] - w[1] for w in inside]
        if not heights or b["H"] <= 3 * statistics.median(heights):
            rows.append((b, sorted(inside)))
            continue
        while inside:
            top, bottom = inside[0][1], inside[0][3]
            row = [w for w in inside if top <= (w[1] + w[3]) / 2 <= bottom]
            inside = [w for w in inside if w not in row]
            y0, y1 = min(w[1] for w in row), max(w[3] for w in row)
            rows.append(({**b, "v": y1, "H": y1 - y0}, sorted(row)))

    # Words in reading order, each with its boxes and the row they sit on. A word hyphenated
    # across two rows becomes one entry with both boxes.
    toks: list[tuple[str, list[tuple[int, Box]]]] = []
    for n, (b, row) in enumerate(rows):
        hyphenated = bool(toks) and toks[-1][0].endswith("-")
        for x0, y0, x1, y1, t in row:
            box = (n, {"page": b["page"], "h": x0, "v": y1, "W": x1 - x0, "H": y1 - y0})
            if hyphenated:
                toks[-1] = (toks[-1][0][:-1] + t, [*toks[-1][1], box])
                hyphenated = False
            else:
                toks.append((t, [box]))
    out = [norm(t) for t, _ in toks]
    ctx = [w for _, _, w in src_words]

    # Runs of at least two words in order: a lone common word may as well be a neighbour's. Keep
    # the longest run and the runs chained to it without a jump, which is text from elsewhere.
    blocks = [
        b for b in SequenceMatcher(None, ctx, out, autojunk=False).get_matching_blocks()
        if b.size and b.size >= min(2, len(ctx))
    ]
    pairs = {}
    if blocks:
        lo = hi = max(range(len(blocks)), key=lambda k: blocks[k].size)

        def near(p, q) -> bool:
            return q.b - (p.b + p.size) <= q.a - (p.a + p.size) + 3

        while lo > 0 and near(blocks[lo - 1], blocks[lo]):
            lo -= 1
        while hi < len(blocks) - 1 and near(blocks[hi], blocks[hi + 1]):
            hi += 1
        pairs = {b.a + k: b.b + k for b in blocks[lo:hi + 1] for k in range(b.size)}

    def cite(t: str) -> bool:
        return t.startswith("[") or t.rstrip(".,;:").endswith("]")

    span = []
    window = range(len(out))  # where to look for a word outside the aligned runs
    if pairs:
        i0, i1 = min(pairs), max(pairs)
        # Grow over words the alignment left at its edges: one cut off by a citation, or the
        # half of a hyphenated word whose other half is on an output line synctex left out.
        while i0 > 0:
            k = pairs[i0] - 1
            while k > 0 and cite(toks[k][0]):
                k -= 1
            prev = ctx[i0 - 1]
            if k < 0 or not out[k] or not (out[k] == prev or k == 0 and len(out[k]) > 1 and prev.endswith(out[k])):
                break
            i0 -= 1
            pairs[i0] = k
        while i1 < len(ctx) - 1:
            k = pairs[i1] + 1
            while k < len(out) - 1 and cite(toks[k][0]):
                k += 1
            nxt = ctx[i1 + 1]
            if k >= len(out) or not out[k] or not (
                out[k] == nxt or k == len(out) - 1 and toks[k][0].endswith("-") and nxt.startswith(out[k])
            ):
                break
            i1 += 1
            pairs[i1] = k
        j0, j1 = pairs[i0], pairs[i1]
        # A citation at either end of the source line prints next to it.
        if "cite" in text[:src_words[i0][0]]:
            while j0 > 0 and cite(toks[j0 - 1][0]):
                j0 -= 1
        if "cite" in text[src_words[i1][1]:]:
            while j1 < len(toks) - 1 and cite(toks[j1 + 1][0]):
                j1 += 1
        window = range(max(j0 - 5, 0), min(j1 + 6, len(out)))
        hit: dict[int, list[Box]] = {}
        for _, boxes in toks[j0:j1 + 1]:
            for n, box in boxes:
                hit.setdefault(n, []).append(box)
        for n, boxes in hit.items():
            x0 = min(b["h"] for b in boxes)
            x1 = max(b["h"] + b["W"] for b in boxes)
            span.append({**rows[n][0], "h": x0, "W": x1 - x0})

    # The word under the cursor, or the one it just left (insert mode sits after a word).
    if col is None:
        return span, []
    i = next((k for k, (s, e, _) in enumerate(src_words) if s <= col - 1 < e), None)
    if i is None:
        i = next((k for k, (_, e, _) in enumerate(src_words) if e == col - 1), None)
    if i is None:
        return span, []
    j = pairs.get(i)
    if j is None:
        # Outside the aligned runs: the repeat whose neighbours match best.
        def score(j: int, i: int = i) -> int:
            return sum(
                out[j + d] == ctx[i + d]
                for d in (-3, -2, -1, 1, 2, 3)
                if 0 <= j + d < len(out) and 0 <= i + d < len(ctx)
            )

        hits = [k for k in window if out[k] == ctx[i]]
        j = max(hits, key=score) if hits else None
    return span, [box for _, box in toks[j][1]] if j is not None else []


def pdf_location(
    pdf: Path, src: str, line: int, col: int | None = None
) -> tuple[list[Box], list[Box], list[Box]]:
    """Where a source line is in the PDF, as boxes: its own text, the whole output lines it
    touches, and its word at column `col`. Its text and word are empty when not found.
    """
    hits = [r for r in synctex("view", "-i", f"{line}:{col or 1}:{src}", "-o", str(pdf)) if "Page" in r]
    if not hits:
        return [], [], []
    tuples = list({
        (int(r["Page"]), *(float(r[k]) for k in ("h", "v", "W", "H"))): None
        for r in hits
    })
    # Drop slivers like the review-mode line-number ruler next to the text.
    widest = max(b[3] for b in tuples)
    boxes: list[Box] = [
        dict(zip(("page", "h", "v", "W", "H"), b))
        for b in tuples
        if b[3] >= widest / 4
    ]
    # synctex lists a paragraph's lines in no particular order; columns, then top to bottom.
    boxes.sort(key=lambda b: (b["page"], b["h"], b["v"]))
    span, words = locate(pdf, src, line, col, boxes)
    return span, boxes, words


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
                return self.reply(404, "no source for that spot")
            src = hits[0]["Input"]
            line = refine_line(pdf, src, int(hits[0]["Line"]), body["page"], body["x"], body["y"])
            # Show which source line the click resolved to, whether or not the editor gets it.
            span, lines, _ = pdf_location(pdf, src, line)
            boxes = span or lines
            res = subprocess.run(
                ["texlab", "inverse-search", "-i", src, "--line1", str(line)],
                capture_output=True, text=True, check=False,
            )
            if res.returncode != 0:
                warnings = texlab_warnings()
                for w in warnings:
                    print(f"warning: {w}", file=sys.stderr, flush=True)
                msg = warnings[0] if warnings else "texlab is not running; open a .tex file in your editor first"
                return self.reply(502, msg, boxes)
            self.reply(200, f"{src}:{line}", boxes)

        def reply(self, code: int, msg: str, boxes: list[Box] | None = None) -> None:
            self.send(code, json.dumps({"msg": msg, "boxes": boxes or []}).encode(), "application/json")

        def forward(self, q: dict[str, list[str]]) -> None:
            file = os.path.abspath(q["file"][0])
            line = int(q["line"][0])
            col = int(q["col"][0]) if "col" in q else None
            span, lines, words = pdf_location(pdf, file, line, col)
            if not lines:
                return self.send(404, f"no pdf location for {file}:{line}\n".encode(), "text/plain")
            broadcast({"type": "forward", "boxes": span or lines, "words": words})
            self.send(200, b"", "text/plain")

        def events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            q: queue.Queue[dict[str, Any]] = queue.Queue()
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
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5, check=False).stdout
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
    params = {"file": str(file), "line": args.line}
    if args.col is not None:
        params["col"] = args.col
    query = urllib.parse.urlencode(params)
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
    fp.add_argument("col", type=int, nargs="?", help="1-based; highlights the word there too")
    fp.add_argument("--port", type=int, help="default: $LATEX_PREVIEW_PORT, else the server's port file")
    fp.set_defaults(run=forward)

    args = ap.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
