from locate import source_words


def words(text: str) -> list[str]:
    return [w for _, _, w in source_words(text)]


def test_accents_stay_in_their_word():
    assert words('Schr\\"odinger and gar\\c{c}on') == ["schrodinger", "and", "garcon"]


def test_citations_labels_math_and_comments_are_not_words():
    assert words("as shown \\cite{key} in $x^2$ \\label{sec:a} done % not this") == ["as", "shown", "in", "done"]


def test_columns_point_into_the_source():
    text = "see \\emph{this} word"
    assert [text[s:e] for s, e, _ in source_words(text)] == ["see", "this", "word"]
