# latex-preview

Live LaTeX PDF preview in your browser: updates on save, and clicks jump between the PDF and
your editor.

Your PDF opens in a browser tab and reloads whenever you rebuild. Double-click the PDF to jump
to that sentence in your editor, or press a key in your editor to scroll the PDF there. It works
over SSH: write on a remote machine and read on your laptop.

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
and the three tools below.

```sh
latex-build main.tex             # compile to out/main.pdf with SyncTeX data
latex-preview serve out/main.pdf # prints the ssh tunnel and the URL to open
```

Or without a flake: `nix run github:howird/latex-preview -- serve out/main.pdf`.

| Program | What it does |
| --- | --- |
| `latex-preview serve PDF` | PDF.js preview with SyncTeX in both directions |
| `latex-preview forward FILE LINE [COL]` | Scroll the running preview to a source line |
| `latex-build FILE` | `tectonic -X compile FILE --synctex --keep-logs --outdir out` |
| `latex-watch FILE` | `latex-build` on every source change, for editors that don't build on save |

### Port

`serve` uses `--port`, else `$LATEX_PREVIEW_PORT`, else a free port. It writes the port to
`.latex-preview-port` next to the PDF. `forward` finds it by looking in each directory above
the source file, and its `out/`, so several papers can be previewed at once.

## Helix

```toml
# .helix/config.toml: space v scrolls the preview to the cursor
[keys.normal.space]
v = ":sh latex-preview forward %{buffer_name} %{cursor_line} %{cursor_column}"
```

```toml
# .helix/languages.toml: texlab builds on save, and the preview reloads
[language-server.texlab.config.texlab]
auxDirectory = "out"

[language-server.texlab.config.texlab.build]
onSave = true
forwardSearchAfter = false
executable = "latex-build"
args = ["%f"]
```

Double-clicking the PDF runs `texlab inverse-search`, which works with any editor running
texlab as its language server.

## How it works

`src/latex_preview.py` serves `src/index.html`, a PDF.js viewer that reloads whenever the PDF
changes.

| Endpoint | Direction |
| --- | --- |
| `GET /forward?file=F&line=N` | editor → PDF: scroll the viewer to `F:N` and highlight it |
| `POST /inverse {page,x,y}` | PDF → editor: open the source line via `texlab inverse-search` |
| `GET /events` | Server-sent events: `reload` and `forward` |

### Which texlab gets the jump

`texlab inverse-search` sends the jump to whichever texlab owns
`$XDG_RUNTIME_DIR/texlab.sock`, which is the one that started last. The server warns at
startup, and again when a jump fails, if:

- more than one texlab is running (clicks may land in the wrong editor), or
- your editor's texlab is a different build from the one on the server's `PATH`.

The wrapper puts texlab *last* on `PATH`, so the texlab from your shell wins. Open your editor
from the devShell and the two match.

### Inverse search lands on the right line

`synctex edit` usually answers with the line where a paragraph *ended*, not the line under
the click. `refine_line` forward-searches every line of that paragraph and picks the one with
a position on the clicked row, just left of the click.

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

From a paper repo, point at a local checkout with
`use flake . --override-input latex-preview path:../latex-preview` in `.envrc`. After a change,
`direnv reload` and restart `latex-preview serve`. To skip the rebuild,
`python3 src/latex_preview.py serve PDF` runs the checkout directly; `synctex` must be on
`PATH`.
