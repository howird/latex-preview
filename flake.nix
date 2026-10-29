{
  description = "Live LaTeX PDF preview in your browser, as a language server: updates as you type, and clicks jump between the PDF and your editor";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = ["x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin"];
    forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
  in {
    packages = forAllSystems (pkgs: let
      synctex = pkgs.texliveInfraOnly.withPackages (ps: [ps.synctex]);
    in rec {
      default = latex-preview;

      # The language server, which serves the viewer and builds as you type with tectonic;
      # pdftotext finds words for forward search.
      latex-preview = pkgs.runCommand "latex-preview" {nativeBuildInputs = [pkgs.makeWrapper];} ''
        mkdir -p $out/share/latex-preview $out/bin
        cp ${./src}/*.py ${./src/index.html} $out/share/latex-preview/
        makeWrapper ${pkgs.python3}/bin/python3 $out/bin/latex-preview \
          --add-flags $out/share/latex-preview/latex_preview.py \
          --prefix PATH : ${pkgs.lib.makeBinPath [synctex pkgs.poppler-utils pkgs.tectonic]}
      '';

      # Compile FILE into out/ with SyncTeX data for the preview.
      latex-build = pkgs.writeShellApplication {
        name = "latex-build";
        runtimeInputs = [pkgs.tectonic];
        text = ''
          if [ $# -ne 1 ]; then
            echo "usage: latex-build FILE.tex" >&2
            exit 2
          fi
          exec tectonic -X compile "$1" --synctex --keep-logs --keep-intermediates --outdir out
        '';
      };

      # Rebuild FILE whenever a TeX source changes, for editing outside an LSP that builds on save.
      latex-watch = pkgs.writeShellApplication {
        name = "latex-watch";
        runtimeInputs = [pkgs.watchexec latex-build];
        text = ''
          if [ $# -ne 1 ]; then
            echo "usage: latex-watch FILE.tex" >&2
            exit 2
          fi
          exec watchexec -e tex,sty,cls,bib,bst -w . -i out -- latex-build "$1"
        '';
      };
    });

    apps = forAllSystems (pkgs: let
      app = name: {
        type = "app";
        program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.${name}}/bin/${name}";
      };
    in rec {
      default = latex-preview;
      latex-preview = app "latex-preview";
      latex-build = app "latex-build";
      latex-watch = app "latex-watch";
    });

    # The tests, against src/. tectonic needs the network, so the fixture paper is
    # built with pdflatex here.
    checks = forAllSystems (pkgs: {
      tests =
        pkgs.runCommand "latex-preview-tests" {
          nativeBuildInputs = [
            (pkgs.python3.withPackages (ps: [ps.pytest]))
            (pkgs.texliveBasic.withPackages (ps: [ps.synctex]))
            pkgs.poppler-utils
          ];
          LATEX_PREVIEW_TEST_ENGINE = "pdflatex";
        } ''
          cp -r ${./src} src && cp -r ${./test} test && chmod -R u+w .
          pytest -q -p no:cacheprovider test
          touch $out
        '';
    });

    devShells = forAllSystems (pkgs: {
      default = pkgs.mkShell {
        nativeBuildInputs = with pkgs;
          [
            tectonic
            texlab
            tex-fmt
            (texliveInfraOnly.withPackages (ps: [ps.chktex]))
            just
            watchexec
          ]
          ++ (with self.packages.${pkgs.stdenv.hostPlatform.system}; [latex-preview latex-build latex-watch]);
      };

      # For working on latex-preview itself: `just test` runs the tests against src/.
      develop = pkgs.mkShell {
        nativeBuildInputs = with pkgs; [
          (python3.withPackages (ps: [ps.pytest]))
          tectonic
          (texliveInfraOnly.withPackages (ps: [ps.synctex]))
          poppler-utils
          just
        ];
      };
    });

    formatter = forAllSystems (pkgs: pkgs.alejandra);
  };
}
