from pathlib import Path

from project import Project


def write(root: Path, files: dict[str, str]) -> Project:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    project = Project(root)
    project.scan()
    return project


def test_an_included_file_belongs_to_its_root(tmp_path):
    p = write(tmp_path, {"paper.tex": "\\documentclass{article}\n\\input{sec/a}\n", "sec/a.tex": "A\n\\include{sec/b}\n", "sec/b.tex": "B"})
    assert p.root_for(tmp_path / "sec" / "b.tex") == tmp_path / "paper.tex"
    assert p.root_for(tmp_path / "paper.tex") == tmp_path / "paper.tex"


def test_a_file_no_root_includes_has_none(tmp_path):
    p = write(tmp_path, {"main.tex": "\\documentclass{article}\n% \\input{stray}\n", "stray.tex": "S"})
    assert p.root_for(tmp_path / "stray.tex") is None


def test_shared_files_keep_the_current_root_else_prefer_main(tmp_path):
    p = write(tmp_path, {
        "supplement.tex": "\\documentclass{article}\n\\input{sec/a}\n",
        "main.tex": "\\documentclass{article}\n\\input{sec/a.tex}\n",
        "sec/a.tex": "A",
    })
    a = tmp_path / "sec" / "a.tex"
    assert p.root_for(a) == tmp_path / "main.tex"
    assert p.root_for(a, current=tmp_path / "supplement.tex") == tmp_path / "supplement.tex"


def test_the_nearest_root_wins_without_a_main(tmp_path):
    p = write(tmp_path, {
        "thesis.tex": "\\documentclass{book}\n\\input{ch1/body}\n",
        "ch1/standalone.tex": "\\documentclass{article}\n\\input{body}\n",
        "ch1/body.tex": "B",
    })
    assert p.root_for(tmp_path / "ch1" / "body.tex") == tmp_path / "ch1" / "standalone.tex"


def test_import_and_subimport_resolve_against_their_directories(tmp_path):
    p = write(tmp_path, {
        "main.tex": "\\documentclass{article}\n\\import{chapters/}{one}\n",
        "chapters/one.tex": "\\subimport{figs/}{plot}\n\\input{chapters/two}\n",
        "chapters/figs/plot.tex": "P",
        "chapters/two.tex": "T",
    })
    main = tmp_path / "main.tex"
    assert p.root_for(tmp_path / "chapters" / "figs" / "plot.tex") == main
    # Inside an \import, \input is relative to the imported directory: chapters/chapters/two.
    assert p.root_for(tmp_path / "chapters" / "two.tex") is None


def test_subfile_is_relative_to_the_including_file(tmp_path):
    p = write(tmp_path, {
        "main.tex": "\\documentclass{article}\n\\input{parts/index}\n",
        "parts/index.tex": "\\subfile{intro}\n",
        "parts/intro.tex": "I",
    })
    assert p.root_for(tmp_path / "parts" / "intro.tex") == tmp_path / "main.tex"


def test_build_output_is_not_scanned(tmp_path):
    p = write(tmp_path, {"main.tex": "\\documentclass{article}\n", "out/copy.tex": "\\documentclass{article}\n"})
    assert list(p.roots) == [tmp_path / "main.tex"]
