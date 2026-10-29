# Run the tests against src/, building the fixture paper with tectonic.
test *args:
    pytest -q test {{args}}

# The nix build and its tests, with pdflatex.
check:
    nix flake check -L
