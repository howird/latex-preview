import queue
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import pytest

from conftest import ENGINE, Events
from test_lsp import forward, post, url_of

pytestmark = pytest.mark.skipif(
    ENGINE != "tectonic" or shutil.which("tectonic") is None, reason="builds as you type use tectonic"
)


def pdf(url: str) -> bytes:
    """The PDF the viewer would load; none before the server knows the document."""
    try:
        with urllib.request.urlopen(f"{url}/pdf") as res:
            return res.read()
    except urllib.error.HTTPError:
        return b""


def text(data: bytes) -> str:
    if not data:
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        return subprocess.run(["pdftotext", f.name, "-"], capture_output=True, check=True).stdout.decode()


def until(check, timeout: float = 30):
    end = time.time() + timeout
    while time.time() < end:
        if result := check():
            return result
        time.sleep(0.1)
    raise AssertionError("timed out")


@pytest.fixture
def live(client, paper):
    """A server building as you type, with sec/intro.tex open and edited to say "zebracorn"."""
    c = client(settings={"onType": True})
    url = url_of(c.shown_message())
    events = Events(url)
    intro = paper / "sec" / "intro.tex"
    edited = intro.read_text().replace("cat", "zebracorn")
    c.open(intro)
    c.change(intro, edited)
    until(lambda: "zebracorn" in text(pdf(url)))
    return c, url, events, intro, edited


def test_the_preview_shows_unsaved_text(live, paper):
    _, _, _, intro, _ = live
    # Nothing was written to the paper.
    assert "zebracorn" not in intro.read_text()
    assert "zebracorn" not in text((paper / "out" / "main.pdf").read_bytes())


def test_a_build_says_how_long_it_took(live):
    c, _, events, intro, edited = live
    # Opening the file switched the preview to it, untimed; skip that and the fixture's builds.
    with pytest.raises(queue.Empty):
        while True:
            events.queue.get(timeout=1)
    c.change(intro, edited.replace("zebracorn", "unicorn"))
    assert isinstance(events.next("reload")["ms"], int)


def test_an_error_keeps_the_last_pdf_and_says_why(live):
    c, url, events, intro, edited = live
    before = pdf(url)
    c.change(intro, edited.replace("zebracorn", "\\undefinedmacro"))
    assert any("Undefined control sequence" in e for e in events.next("errors")["errors"])
    assert pdf(url) == before
    c.change(intro, edited.replace("zebracorn", "unicorn"))
    assert events.next("errors")["errors"] == []
    until(lambda: "unicorn" in text(pdf(url)))


def test_search_both_ways_maps_scratch_files_to_real_ones(live):
    c, url, events, intro, edited = live
    event = forward(c, events, intro, 1, "zebracorn", edited)
    word = event["words"][0]
    code, reply = post(url, {"page": 1, "x": word["h"] + word["W"] / 2, "y": word["v"] - word["H"] / 2})
    assert code == 200, reply
    shown = c.shown.get(timeout=5)
    assert shown["uri"] == intro.as_uri()
    assert shown["selection"]["start"]["line"] == 1


def test_edits_during_a_build_are_built_after_it(live):
    c, url, _, intro, edited = live
    for i in range(10):
        c.change(intro, edited.replace("zebracorn", f"edit{i}x"))
        time.sleep(0.05)
    until(lambda: "edit9x" in text(pdf(url)))
