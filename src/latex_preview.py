"""latex-preview: a language server that serves a live PDF preview with SyncTeX in both directions.

Helix starts it for .tex files; see the README for its configuration.
"""

import sys

from lsp import main

USAGE = "usage: latex-preview [--stdio]   run the language server (your editor starts this)"

if __name__ == "__main__":
    args = sys.argv[1:]
    if args in ([], ["--stdio"]):
        main()
    print(USAGE, file=sys.stderr)
    sys.exit(0 if args in (["-h"], ["--help"]) else 2)
