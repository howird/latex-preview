"""Which document a .tex file belongs to, by following \\input and friends, as texlab does.

A root is a file with \\documentclass. Every root's includes are followed from it, and a file
belongs to the roots that reach it.
"""

import os
import re
from collections.abc import Callable
from pathlib import Path

# Directories that hold build output or tooling, not sources.
SKIP = {"out", "build", "node_modules", "result"}
# Stop scanning a workspace that is clearly not one paper (a home directory, say).
MAX_FILES = 2000

COMMENT = re.compile(r"(?<!\\)%.*")
DOCUMENTCLASS = re.compile(r"\\documentclass\b")
# \input{F}, \include{F}: relative to the root's directory, or the \import directory around them.
INPUT = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
# \subfile{F}: relative to the including file.
SUBFILE = re.compile(r"\\subfile(?:include)?\s*\{([^}]+)\}")
# \import{D}{F} and \subimport{D}{F}: F in D, which is relative to the root or the including file.
IMPORT = re.compile(r"\\(sub)?(?:import|inputfrom|includefrom)\*?\s*\{([^}]*)\}\s*\{([^}]+)\}")

# A file's text: the editor's when it is open there, else the disk's; None if unreadable.
Read = Callable[[Path], str | None]


def read_disk(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def tex_files(workspace: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP and not d.startswith("result"))
        found += [Path(dirpath, f) for f in sorted(filenames) if f.endswith(".tex")]
        if len(found) > MAX_FILES:
            break
    return found


def resolve(base: Path, name: str) -> Path | None:
    """The file \\input{name} reads from base: name.tex if it exists, else name itself."""
    name = name.strip()
    path = base / name
    for candidate in (path.with_name(path.name + ".tex"), path):
        if candidate.is_file():
            return candidate.resolve()
    return None


def includes(path: Path, text: str, root_dir: Path, base: Path) -> list[tuple[Path, Path]]:
    """The files `path` includes, each with the directory its own \\input{}s are relative to.

    `base` is where `path`'s plain \\input{}s resolve: the root's directory, or an \\import's.
    """
    text = COMMENT.sub("", text)
    found = []
    for m in INPUT.finditer(text):
        if (f := resolve(base, m[1])) is not None:
            found.append((f, base))
    for m in SUBFILE.finditer(text):
        if (f := resolve(path.parent, m[1])) is not None:
            found.append((f, f.parent))
    for m in IMPORT.finditer(text):
        dir = (path.parent if m[1] else root_dir) / m[2]
        if (f := resolve(dir, m[3])) is not None:
            found.append((f, dir.resolve()))
    return found


def reach(root: Path, read: Read) -> set[Path]:
    """The root and every file it includes, directly or not."""
    root = root.resolve()
    seen = {root}
    todo = [(root, root.parent)]
    while todo:
        path, base = todo.pop()
        text = read(path)
        if text is None:
            continue
        for f, b in includes(path, text, root.parent, base):
            if f not in seen:
                seen.add(f)
                todo.append((f, b))
    return seen


class Project:
    """The roots of a workspace and the files each reaches. Rebuild with scan() when files change."""

    def __init__(self, workspace: Path, read: Read = read_disk) -> None:
        self.workspace = workspace.resolve()
        self.read = read
        self.roots: dict[Path, set[Path]] = {}

    def scan(self) -> None:
        roots = {}
        for path in tex_files(self.workspace):
            text = self.read(path)
            if text is not None and DOCUMENTCLASS.search(COMMENT.sub("", text)):
                roots[path.resolve()] = set()
        for root in roots:
            roots[root] = reach(root, self.read)
        self.roots = roots

    def root_for(self, file: Path, current: Path | None = None) -> Path | None:
        """The root `file` belongs to: `current` if it is one of them, else main.tex, else the
        nearest one. None when no root includes it.
        """
        file = file.resolve()
        if file not in self.roots:
            # A root outside the workspace, or one added since the last scan.
            text = self.read(file)
            if text is not None and DOCUMENTCLASS.search(COMMENT.sub("", text)):
                self.roots[file] = reach(file, self.read)
        candidates = [r for r, files in self.roots.items() if file in files]
        if not candidates:
            return None
        if current in candidates:
            return current

        def distance(root: Path) -> tuple[bool, int, str]:
            hops = len(Path(os.path.relpath(file.parent, root.parent)).parts)
            return root.name != "main.tex", hops, str(root)

        return min(candidates, key=distance)
