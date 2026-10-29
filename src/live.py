"""Builds of a document as it is in the editor, unsaved changes included, for the preview.

Each document gets a scratch directory:

  src/    the document's directory, mirrored: symlinks to files on disk, and the text of the
          files open in the editor
  build/  where tectonic writes, keeping .aux and .bbl so the next build takes one pass
  view/   the PDF and SyncTeX file of the last build that succeeded, which the preview reads

SyncTeX records src/ paths, so source paths are translated between src/ and the real directory.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

from project import SKIP

# tectonic's summary lines after a TeX error, which say nothing the error itself doesn't.
GENERIC = re.compile(r"the \S+ engine had an unrecoverable error|halted on potentially-recoverable error")


def scratch_base() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime or f"/tmp/latex-preview-{os.getuid()}") / "latex-preview"


def sources(dir: Path) -> list[Path]:
    """The files under dir a build may read: everything but build output and hidden files."""
    found = []
    for dirpath, dirnames, filenames in os.walk(dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP and not d.startswith("result")]
        found += [Path(dirpath, f) for f in filenames if not f.startswith(".")]
    return found


class Live:
    def __init__(self, root: Path, base: Path | None = None) -> None:
        self.root = root
        self.home = root.parent
        key = hashlib.sha1(str(root).encode()).hexdigest()[:12]
        self.dir = (base or scratch_base()) / f"{root.stem}-{key}"
        self.src, self.build, self.view = self.dir / "src", self.dir / "build", self.dir / "view"
        # Whether build/ holds the .aux and .bbl of a successful build, so one pass is enough.
        self.primed = False

    @property
    def pdf(self) -> Path:
        return self.view / f"{self.root.stem}.pdf"

    def to_scratch(self, path: Path) -> Path:
        try:
            return self.src / path.relative_to(self.home)
        except ValueError:
            return path

    def to_real(self, path: Path) -> Path:
        try:
            return self.home / path.relative_to(self.src)
        except ValueError:
            return path

    def mirror(self, docs: dict[Path, str]) -> None:
        """Make src/ the document's directory as the editor has it."""
        wanted = set()
        for real in [*sources(self.home), *(p for p in docs if p.is_relative_to(self.home))]:
            target = self.to_scratch(real)
            if target in wanted:
                continue
            wanted.add(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if real in docs:
                if target.is_symlink() or not target.exists() or target.read_text(errors="replace") != docs[real]:
                    tmp = target.with_name(f".{target.name}.tmp")
                    tmp.write_text(docs[real])
                    os.replace(tmp, target)
            elif not (target.is_symlink() and os.readlink(target) == str(real)):
                target.unlink(missing_ok=True)
                target.symlink_to(real)
        for path in sources(self.src):
            if path not in wanted:
                path.unlink()

    def run(self, docs: dict[Path, str]) -> list[str]:
        """Build the document with the editor's text; the errors, or none if it succeeded."""
        self.mirror(docs)
        self.build.mkdir(parents=True, exist_ok=True)
        self.view.mkdir(parents=True, exist_ok=True)
        cmd = ["tectonic", "-X", "compile", self.root.name, "--synctex", "--keep-logs", "-k", "--outdir", str(self.build)]
        if self.primed:
            cmd += ["-r", "0", "-Z", f"search-path={self.build}"]
        res = subprocess.run(cmd, cwd=self.src, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            # The .aux of a failed build may be half written: the next build starts over.
            self.primed = False
            errors = [line[7:] for line in res.stderr.splitlines() if line.startswith("error: ")]
            return [e for e in errors if not GENERIC.search(e)] or errors or [f"tectonic exited with {res.returncode}"]
        # The viewer never reads a PDF tectonic is still writing.
        for name in (f"{self.root.stem}.synctex.gz", f"{self.root.stem}.pdf"):
            os.replace(self.build / name, self.view / name)
        self.primed = True
        return []
