import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import Events


def url_of(message: dict) -> str:
    return message["message"].split()[1]


def post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(f"{url}/inverse", data=json.dumps(body).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_announces_the_preview_and_serves_the_viewer(client):
    c = client()
    message = c.shown_message()
    assert message["type"] == 3
    assert "ssh -NL" in message["message"]
    with urllib.request.urlopen(url_of(message)) as res:
        assert b"pdf.js" in res.read()
    assert c.close() == 0


def test_preview_url_repeats_the_announcement(client):
    c = client()
    first = c.shown_message()
    assert c.command("latex.previewUrl") is None
    assert c.shown_message() == first


def test_a_taken_port_is_an_error(client):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        c = client(env={"LATEX_PREVIEW_PORT": str(port)})
        message = c.shown_message()
    assert message["type"] == 1
    assert f"127.0.0.1:{port}" in message["message"]
    # The language server keeps running.
    assert c.hover(Path(__file__), 0, 0) is None


def test_the_environment_overrides_the_config(client):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        c = client(settings={"port": taken.getsockname()[1]})
        assert c.shown_message()["type"] == 3


def test_an_included_file_shows_its_root(client, paper):
    c = client()
    url = url_of(c.shown_message())
    c.open(paper / "sec" / "intro.tex")
    assert c.hover(paper / "sec" / "intro.tex", 0, 0) is None
    with urllib.request.urlopen(f"{url}/pdf") as res:
        assert res.read() == (paper / "out" / "main.pdf").read_bytes()


def forward(c, events, file, line, word, text=None):
    """Hover on `word` in the line, as the editor has it, and scroll the preview there."""
    character = (text or file.read_text()).splitlines()[line].index(word)
    c.hover(file, line, character)
    c.command("latex.showInPreview")
    return events.next("forward")


def test_forward_search_highlights_the_line_and_word(client, paper):
    c = client()
    events = Events(url_of(c.shown_message()))
    intro = paper / "sec" / "intro.tex"
    c.open(intro)
    event = forward(c, events, intro, 1, "cat")
    assert event["boxes"] and event["words"]
    assert all(b["page"] == 1 for b in event["boxes"] + event["words"])
    # One word: narrower than the line.
    assert event["words"][0]["W"] < max(b["W"] for b in event["boxes"])


def test_double_click_opens_the_source_in_the_editor(client, paper):
    c = client()
    url = url_of(c.shown_message())
    events = Events(url)
    intro = paper / "sec" / "intro.tex"
    c.open(intro)
    word = forward(c, events, intro, 1, "cat")["words"][0]
    code, reply = post(url, {"page": 1, "x": word["h"] + word["W"] / 2, "y": word["v"] - word["H"] / 2})
    assert code == 200, reply
    shown = c.shown.get(timeout=5)
    assert shown["uri"] == intro.as_uri()
    assert shown["selection"]["start"]["line"] == 1
    assert reply["boxes"]


def test_double_click_reports_an_editor_that_refuses(client, paper):
    c = client()
    url = url_of(c.shown_message())
    events = Events(url)
    intro = paper / "sec" / "intro.tex"
    c.open(intro)
    c.show_document = {"success": False}
    word = forward(c, events, intro, 1, "cat")["words"][0]
    code, reply = post(url, {"page": 1, "x": word["h"] + 1, "y": word["v"] - 1})
    assert code == 502
    assert "couldn't open" in reply["msg"]


def test_forward_search_without_a_location_says_so(client, paper, tmp_path):
    c = client()
    events = Events(url_of(c.shown_message()))
    c.open(paper / "main.tex")
    # A file the paper doesn't include: the preview stays on the paper, which has nothing from it.
    notes = tmp_path / "notes.tex"
    notes.write_text("Notes to self.\n")
    c.open(notes)
    c.hover(notes, 0, 0)
    c.command("latex.showInPreview")
    assert "No PDF location" in events.next("say")["msg"]
