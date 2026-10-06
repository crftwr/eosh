VENV := .venv

ifeq ($(OS),Windows_NT)
    # Windows: venv scripts live in Scripts/, executables end in .exe.
    # Prefer python3.14 if it's already on PATH; otherwise fall back to the
    # `py` launcher.
    PY_ON_PATH := $(shell where python3.14 2>/dev/null)
    ifneq ($(strip $(PY_ON_PATH)),)
        PYTHON_BOOTSTRAP := python3.14
    else
        PYTHON_BOOTSTRAP := py
    endif
    VENV_BIN := $(VENV)/Scripts
    PYTHON := $(VENV_BIN)/python.exe
    PIP := $(VENV_BIN)/pip.exe
    PYTEST := $(VENV_BIN)/pytest.exe
    VENV_STAMP := $(VENV_BIN)/activate
else
    # Unix: venv binaries live in bin/
    PYTHON_BOOTSTRAP := python3.14
    VENV_BIN := $(VENV)/bin
    PYTHON := $(VENV_BIN)/python
    PIP := $(VENV_BIN)/pip
    PYTEST := $(VENV_BIN)/pytest
    VENV_STAMP := $(VENV_BIN)/activate
endif

$(info Using Python bootstrap: $(PYTHON_BOOTSTRAP))

.PHONY: help install test clean run demo banner install-launcher build publish-testpypi tag release-github release-whl release-status

help:
	@echo "Eolith Shell utility commands:"
	@echo "  make install          - create the venv and install eosh (editable, with dev + awsut deps)"
	@echo "  make test             - run the test suite"
	@echo "  make run              - run eosh from the venv"
	@echo "  make install-launcher - put an eosh launcher on PATH"
	@echo "  make demo             - re-record doc/images/demo.gif (needs vhs)"
	@echo "  make banner           - render doc/images/banner.svg to the Pages JPEGs (needs Chrome)"
	@echo "  make clean            - remove the venv, build artifacts and caches"
	@echo ""
	@echo "Release (run in this order):"
	@echo "  make tag VERSION=x.y.z - bump __version__, commit, tag, push (no publishing)"
	@echo "  make release-github    - open the GitHub Release at that tag"
	@echo "  make release-whl       - upload sdist + wheel to PyPI, and to the Release"
	@echo "  make release-status    - show which artifacts have landed so far"
	@echo ""
	@echo "  The release-* targets are re-runnable. Supporting targets:"
	@echo "  make build             - build the sdist + wheel into dist/ (installs build/twine as needed)"
	@echo "  make publish-testpypi  - rehearsal: upload dist/* to TestPyPI ([testpypi] token in ~/.pypirc)"

$(VENV_STAMP):
	"$(PYTHON_BOOTSTRAP)" -m venv $(VENV)
	"$(PYTHON)" -m pip install --upgrade pip
	"$(PYTHON)" -m pip install -e ".[dev]"

install: $(VENV_STAMP)

test: $(VENV_STAMP)
	"$(PYTHON)" -m pytest tests/ -v

run: $(VENV_STAMP)
	"$(PYTHON)" -m eosh

# README demo GIF.  Records scripts/demo/demo.tape with VHS against a
# throwaway HOME (scripts/demo/setup.sh), so no real history or config shows.
demo: $(VENV_STAMP)
	scripts/demo/setup.sh
	EOSH="$(CURDIR)/$(VENV_BIN)/eosh" vhs scripts/demo/demo.tape

# GitHub Pages banner.  banner.svg is the source; link previews ignore SVG in
# og:image, so the site serves JPEGs rendered from it (headless Chrome + sips).
banner:
	python3 scripts/render_banner.py

install-launcher: $(VENV_STAMP)
	"$(PYTHON)" scripts/install_launcher.py

# --- Packaging / release ----------------------------------------------------
# `build` and `twine` are release-time tooling, not needed to run or develop
# Eolith Shell, so they are installed on demand here rather than bloating the base
# venv. Invoked as `python -m ...` (not the venv's console scripts) so the same
# recipe works on Windows, where those scripts live in Scripts/ and end in .exe.
# The PyPI long description (README.pypi.md) is generated here on the fly and
# never committed: README.md keeps repo-relative image/link targets for GitHub,
# and gen_pypi_readme.py rewrites them to version-tagged GitHub URLs so they
# render on the PyPI page. `twine check --strict` promotes twine's
# "description missing" warning to a failure, so a build that somehow skipped
# generation can never upload an empty description.
build: $(VENV_STAMP)
	"$(PIP)" install --quiet build twine
	rm -rf dist build src/eosh.egg-info
	"$(PYTHON)" scripts/gen_pypi_readme.py
	"$(PYTHON)" -m build
	"$(PYTHON)" -m twine check --strict dist/*

# The safe rehearsal for release-whl: same build and upload path, but a bad
# TestPyPI version costs nothing. Deliberately NOT named release-* — it needs
# neither a tag nor a GitHub Release and publishes nothing permanent, so it is
# a pre-release smoke test rather than a step of the pipeline. Depends on
# `build` (not on the file target below) so it always builds fresh.
publish-testpypi: build
	"$(PYTHON)" -m twine upload -r testpypi dist/*

# --- Release pipeline (mirrors puikit's) -------------------------------------
# Releasing is one target per step, each independently re-runnable:
#
#   make tag VERSION=x.y.z   bump __version__, commit, tag, push
#   make release-github      open the GitHub Release at that tag
#   make release-whl         sdist + wheel -> PyPI (+ the Release)
#   make release-status      what has landed so far
#
# Order matters only twice: `tag` first (everything else names the tag it
# creates), then `release-github` (release-whl uploads into the Release it
# opens).
#
# The version's single source of truth is src/eosh/__init__.py's __version__;
# pyproject.toml derives it (dynamic version = attr). EOSH_VERSION below
# reads that same literal, so every release-* target acts on the release the
# checkout is actually on — only `tag` takes a VERSION=. Override it on the
# others to target a different release (e.g. re-uploading an asset for an
# older tag).
EOSH_VERSION := $(if $(VERSION),$(VERSION),$(shell sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' src/eosh/__init__.py 2>/dev/null | head -1))

# Guards shared by the release-* targets, kept in one place so they cannot
# drift into checking different things. Used as $(call ...) inside a recipe;
# each expands to a single multi-line shell test.
#
# check_gh:             a resolvable version and a usable `gh`.
# check_release_exists: the above, plus the GitHub Release to upload into.
define check_gh
test -n "$(EOSH_VERSION)" || { echo "ERROR: could not determine version; pass VERSION=x.y.z"; exit 1; }; \
command -v gh >/dev/null 2>&1 || { echo "ERROR: 'gh' not found. Install the GitHub CLI first."; exit 1; }; \
gh auth status >/dev/null 2>&1 || { echo "ERROR: 'gh' is not authenticated. Run 'gh auth login'."; exit 1; }
endef

define check_release_exists
$(check_gh); \
gh release view v$(EOSH_VERSION) >/dev/null 2>&1 || { \
	echo "ERROR: GitHub Release v$(EOSH_VERSION) does not exist."; \
	echo "       Open it first with 'make release-github'."; \
	exit 1; \
}
endef

# --- tag: the one target that changes the version ---------------------------
# Usage: make tag VERSION=1.0.11
#
# Pure version + git work: bump __version__, commit, tag, push. It publishes
# nothing and needs no `gh` and no PyPI token — the release-* targets do the
# publishing, each with its own credentials.
#
# bump_version.py rewrites the single __version__ line, which is why the commit
# stages __init__.py rather than pyproject.toml.
#
# release_preflight.py runs FIRST and aborts before any mutation if the tree is
# dirty, the version is stale, or the tag exists — so a failed precondition
# never leaves a half-cut release. The test suite must pass before anything is
# built.
#
# `make build` runs before the pushes purely as a gate: it proves the sdist and
# wheel build and pass `twine check` while the tag is still local and
# retractable. It also leaves dist/ ready for `make release-whl`.
tag: $(VENV_STAMP)
	@test -n "$(VERSION)" || { echo "ERROR: set VERSION, e.g. make tag VERSION=1.0.11"; exit 1; }
	"$(PYTHON)" scripts/release_preflight.py "$(VERSION)"
	$(MAKE) test
	"$(PYTHON)" scripts/bump_version.py "$(VERSION)"
	git add src/eosh/__init__.py
	git commit -m "Releasing $(VERSION)"
	git tag -a v$(VERSION) -m "$(VERSION)"
	$(MAKE) build
	git push
	git push origin v$(VERSION)
	@echo ""
	@echo "Tagged $(VERSION): commit + tag v$(VERSION), both pushed ✓"
	@echo "Next:"
	@echo "  make release-github    # open the GitHub Release at v$(VERSION)"
	@echo "  make release-whl       # sdist + wheel -> PyPI + the Release"

# --- release-github: open the Release the artifacts upload into -------------
# Reads the version from the checkout, so the usual path is `make tag` then
# `make release-github` with no arguments. --verify-tag refuses to invent a tag
# GitHub does not already have, which is why `tag` pushes it first.
#
# Idempotent on purpose: an existing Release is reported and left alone rather
# than erroring, so re-running the pipeline from the top costs nothing.
release-github:
	@$(call check_gh)
	@git ls-remote --exit-code --tags origin "v$(EOSH_VERSION)" >/dev/null 2>&1 || { \
		echo "ERROR: tag v$(EOSH_VERSION) is not on origin."; \
		echo "       Push it with 'make tag VERSION=$(EOSH_VERSION)' (or 'git push origin v$(EOSH_VERSION)')."; \
		exit 1; \
	}
	@if gh release view v$(EOSH_VERSION) >/dev/null 2>&1; then \
		echo "GitHub Release v$(EOSH_VERSION) already exists; leaving it as is."; \
	else \
		gh release create v$(EOSH_VERSION) --title "v$(EOSH_VERSION)" --generate-notes --verify-tag && \
		echo "Opened GitHub Release v$(EOSH_VERSION) ✓"; \
	fi

# The filenames setuptools gives the sdist + wheel, derived from the same
# version literal as EOSH_VERSION. Naming them explicitly (rather than
# globbing dist/*) means a stale artifact left from an earlier version can
# never be swept into an upload.
PYPI_SDIST := dist/eosh-$(EOSH_VERSION).tar.gz
PYPI_WHEEL := dist/eosh-$(EOSH_VERSION)-py3-none-any.whl

# File target so release-whl builds the distributions on demand when they are
# missing (e.g. after `make clean`). `make build` wipes dist/ and writes both
# files, so the sdist alone is enough of a prerequisite to trigger it; the
# recipe below then asserts the wheel landed too. Existing artifacts are NOT
# rebuilt — publishing the exact bytes that were verified is the point.
$(PYPI_SDIST):
	@echo "Python distributions for $(EOSH_VERSION) not found; building them first..."
	@$(MAKE) build

# --- release-whl: publish the Python distributions --------------------------
# Uploads BOTH the sdist and the wheel — the target is named for the headline
# artifact, not the whole payload.
#
# A PyPI version can never be re-uploaded, so this refuses to publish a build
# that is not the tagged one: HEAD must sit exactly on vX.Y.Z. `make tag`
# leaves the checkout there, so the usual path is `make tag` then
# `make release-whl`; publishing an older release means checking out its tag
# first.
#
# Also attaches both files to the GitHub Release, so the release page lists
# every artifact. --clobber replaces same-named assets on a re-run.
# Prereqs: a [pypi] token in ~/.pypirc and an authenticated `gh`.
release-whl: $(PYPI_SDIST)
	@$(call check_release_exists)
	@git rev-parse -q --verify "v$(EOSH_VERSION)^{commit}" >/dev/null || { \
		echo "ERROR: tag v$(EOSH_VERSION) not found locally. Cut it with 'make tag VERSION=$(EOSH_VERSION)' or fetch it."; \
		exit 1; \
	}
	@test "$$(git rev-parse HEAD)" = "$$(git rev-parse "v$(EOSH_VERSION)^{commit}")" || { \
		echo "ERROR: HEAD is not at tag v$(EOSH_VERSION); the upload would not match the tag."; \
		echo "       Check the tag out first: git checkout v$(EOSH_VERSION)"; \
		exit 1; \
	}
	@# Both files, not just the sdist that triggered the build: a VERSION= override
	@# that disagrees with __version__ builds different filenames entirely, and
	@# this is where that shows up as a clear error instead of a twine traceback.
	@for f in "$(PYPI_SDIST)" "$(PYPI_WHEEL)"; do \
		test -f "$$f" || { echo "ERROR: $$f missing; run 'make build' from a checkout at v$(EOSH_VERSION)."; exit 1; }; \
	done
	@echo "Uploading $(notdir $(PYPI_SDIST)) + $(notdir $(PYPI_WHEEL)) to PyPI..."
	"$(PYTHON)" -m twine upload "$(PYPI_SDIST)" "$(PYPI_WHEEL)"
	gh release upload v$(EOSH_VERSION) "$(PYPI_SDIST)" "$(PYPI_WHEEL)" --clobber
	@echo "Published $(EOSH_VERSION) to PyPI and attached both distributions to release v$(EOSH_VERSION) ✓"

# --- release-status: read-only progress check -------------------------------
# One place to see which artifacts have landed for the version the checkout is
# on.
release-status:
	@test -n "$(EOSH_VERSION)" || { echo "ERROR: could not determine version; pass VERSION=x.y.z"; exit 1; }
	@echo "Release v$(EOSH_VERSION):"
	@# Asset names only: gh renders JSON numbers in Go's default float format, so
	@# {{.size}} would print sizes as 8.8917854e+07.
	@gh release view v$(EOSH_VERSION) --json assets \
		--template '{{range .assets}}  GitHub asset: {{.name}}{{"\n"}}{{end}}' \
		2>/dev/null || echo "  (no GitHub Release yet — run 'make release-github')"
	@"$(PYTHON)" -c "import json,urllib.request as u; \
		v='$(EOSH_VERSION)'; \
		d=json.load(u.urlopen('https://pypi.org/pypi/eosh/json')); \
		print('  PyPI: ' + ('published' if v in d['releases'] else 'NOT published'))" \
		2>/dev/null || echo "  PyPI: unknown (needs the venv and network access)"


clean:
	"$(PYTHON_BOOTSTRAP)" -c "import shutil, pathlib; shutil.rmtree('$(VENV)', ignore_errors=True); [shutil.rmtree(p, ignore_errors=True) for p in ('src/eosh.egg-info', 'build', 'dist', '.pytest_cache')]; pathlib.Path('README.pypi.md').unlink(missing_ok=True); [shutil.rmtree(p, ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
