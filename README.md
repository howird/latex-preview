# latex-preview

Live LaTeX PDF preview in your browser: updates as you type, and clicks jump between the PDF and
your editor.

Your PDF opens in a browser tab and updates when you pause typing, before you save. Helix starts
the preview as a language server. Double-click the PDF to jump to that line in Helix, or press a key in Helix to
scroll the PDF there and highlight the word under your cursor. Either way the PDF highlights the
text of that source line.

## Use it in a paper

```nix
# flake.nix
{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    latex-preview = {
      url = "github:howird/latex-preview";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };
  outputs = {latex-preview, ...}: {
    inherit (latex-preview) devShells;
  };
}
```

With `use flake` in `.envrc`, the shell has tectonic, texlab, tex-fmt, chktex, just, watchexec
and the three programs below.

| Program | What it does |
| --- | --- |
| `latex-preview` | The preview, as a language server Helix starts for `.tex` files |
| `latex-build FILE` | `tectonic -X compile FILE --synctex --keep-logs --keep-intermediates --outdir out` |
| `latex-watch FILE` | `latex-build` on every source change, for editors that don't build on save |

## Helix

```toml
# .helix/languages.toml
[[language]]
name = "latex"
language-servers = ["texlab", "latex-preview"]

[language-server.latex-preview]
command = "latex-preview"

# texlab builds on save into out/, and the preview reloads.
[language-server.texlab.config.texlab]
auxDirectory = "out"

[language-server.texlab.config.texlab.build]
onSave = true
forwardSearchAfter = false
executable = "latex-build"
args = ["%f"]
```

```toml
# .helix/config.toml: space v scrolls the preview to the cursor
[keys.normal.space]
v = ["hover", ":lsp-workspace-command latex.showInPreview"]
```

Hover is how the cursor position reaches the preview: Helix sends it to both servers, and the
preview answers with nothing, so the page you see is texlab's. Double-clicking the PDF opens that
line in the Helix that started the preview.

Helix runs the rest of a key's commands when one fails, so one binding can drive several
previews. With argdown-preview:

```toml
v = ["hover", ":lsp-workspace-command latex.showInPreview", ":lsp-workspace-command argdown.showInPreview"]
```

### Port

The preview listens on `127.0.0.1` and a free port. Helix shows the URL when the server starts;
`:lsp-workspace-command latex.previewUrl` shows it again. A fixed port saves the lookup and keeps
the browser tab working across restarts. Set it in `languages.toml`:

```toml
[language-server.latex-preview.config.preview]
port = 23636
host = "127.0.0.1"
```

or with `$LATEX_PREVIEW_PORT` and `$LATEX_PREVIEW_HOST`, say in the paper's `.envrc`; these
override `languages.toml`. If the port is taken, Helix shows an error and there is no preview
until you restart the server on a free one.

## What it shows

The preview shows the PDF of one document, a `.tex` file with `\documentclass`. It finds the document a file
belongs to as texlab does: it reads every `.tex` file in the workspace and follows `\input`,
`\include`, `\subfile`, `\import` and `\subimport` from each document. Opening or hovering a
file the current document includes keeps it up. A file only another document includes switches
to it; among several, it prefers `main.tex`, then the nearest. To always show one, set its path,
relative to the workspace:

```toml
[language-server.latex-preview.config.preview]
root = "main.tex"
```

### As you type

The preview builds the document with tectonic 300 ms after you stop typing, from the text in
Helix, saved or not, and shows it about half a second later. While the text has an error, the
last PDF that built stays up under a banner with tectonic's errors. These builds go to a scratch
directory, so `out/` only ever holds what texlab built from saved files: the PDF to send.

To keep the build fast, it runs TeX once, with the `.aux` and `.bbl` of the build before, so a
new `\label` or citation shows as `??` until the next build. A paper too slow to build as you
type can turn it off, and the preview shows `out/NAME.pdf` whenever texlab rebuilds it:

```toml
[language-server.latex-preview.config.preview]
onType = false
```

## How it works

`src/latex_preview.py` runs `src/lsp.py`, a stdio language server. It finds documents with
`src/project.py`, builds them as you type with `src/live.py`, and serves `src/index.html`, a
PDF.js viewer, from `src/preview.py`. `src/locate.py` maps between source lines and the PDF with
SyncTeX.

A build as you type works in `$XDG_RUNTIME_DIR/latex-preview/NAME-HASH/`. `src/` mirrors the
document's directory, with symlinks to the files on disk and the text of the files open in Helix.
tectonic writes to `build/`, keeping the `.aux` and `.bbl` for the next build, and the PDF and
SyncTeX file move to `view/` once it succeeds, so the viewer never reads a half-written PDF.
SyncTeX names files in `src/`, which the server translates to the real ones both ways. Edits made
during a build are built once it finishes.

| Endpoint | Direction |
| --- | --- |
| `POST /inverse {page,x,y}` | PDF → editor: the server sends `window/showDocument` to Helix, and returns the line's boxes to highlight |
| `GET /events` | Server-sent events: `reload`, `forward` (boxes to highlight), `say` (a status message) and `errors` (why the last build failed) |

Forward search is `latex.showInPreview`: it looks up the position of the last hover.

### Inverse search lands on the right line

`synctex edit` usually answers with the line where a paragraph *ended*, not the line under
the click. `refine_line` forward-searches every line of that paragraph and picks the one with
a position on the clicked row, just left of the click.

### Forward search finds the word

SyncTeX ignores the column, and answers with whole output lines: every one the source line
touches, including the text of the source lines before and after it. The server lists the words
on those output lines with `pdftotext -bbox` and aligns them with the source line's words. That
finds where the source line's own text starts and ends, which the viewer highlights, and, given
a column, which occurrence of the word under the cursor it is, which the viewer highlights more
strongly. Where the alignment finds nothing (math, tables, a float SyncTeX reports as one box),
the whole output lines are highlighted.

Inverse search stays at line level: SyncTeX answers a click with a line, not a column.

### End every `\input` file with a blank line and `\par`

```latex
...the last sentence of the section.

\par
```

TeX ends a paragraph at the next blank line or `\par`. If a section file ends mid-paragraph,
that happens in the *next* file (its first blank line or `\section`), and SyncTeX attributes
the whole paragraph there: clicking it jumps to the next section. Neither the server nor
`refine_line` can recover from this, because SyncTeX reports the wrong file.

The blank line ends the paragraph. The `\par` is there because `tex-fmt` strips trailing blank
lines (unconditionally, there is no setting), and keeps the paragraph ended if the blank line
is ever deleted. It has no effect on the output.

## Developing

`nix develop .#develop` has python with pytest, tectonic, synctex, pdftotext and just; `just test`
runs the tests against `src/`, and `just check` the nix build and its tests. The nix check builds
with pdflatex, since tectonic downloads its files, so it skips the tests of building as you type.

From a paper repo, point at a local checkout with
`use flake . --override-input latex-preview path:../latex-preview` in `.envrc`. After a change,
`direnv reload` and `:lsp-restart` in Helix. To skip the rebuild, set `command = "python3"` and
`args = ["../latex-preview/src/latex_preview.py"]` for `latex-preview` in
`.helix/languages.toml`; `synctex` and `pdftotext` must be on `PATH`.
