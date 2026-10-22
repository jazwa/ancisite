# anci build pipeline.
#
#   make build   -> dist/anci (+ dist/anci.sha256)   a single-file zipapp
#   make release -> dist/release/anci-<os>-<arch> (+ SHA256SUMS)
#                   self-contained binaries (Python included): what
#                   anci-install.sh downloads from a Codeberg release
#   make test    -> the unittest suite
#   make clean
#
# The zipapp needs Python 3.12+ on the machine that runs it (never on the
# box it deploys to: guests run your compiled binary + anci's static Go
# components). Override the build interpreter with `make PYTHON=python3`.

PYTHON ?= python3.12

DIST  := dist
STAGE := .build/zipapp
BIN   := $(DIST)/anci
SRC   := packaging/zipapp_main.py anci/versions.toml $(shell find anci -name '*.py' -o -path 'anci/render/files/*' -type f)

# Release binaries: pex "scies" — a tiny native launcher + a pinned portable
# CPython + anci, in one file per platform. All four build here, no compiler.
# `--venv prepend` makes warm starts as fast as a normal install (~20 ms); the
# first run on a machine unpacks into ~/.cache/nce (~1 s).
PEX            := uvx pex==2.103.4
SCIE_PYTHON    := 3.12
SCIE_PLATFORMS := linux-x86_64 linux-aarch64 macos-aarch64 macos-x86_64
RELEASE        := $(DIST)/release

.PHONY: build release test clean

build: $(BIN)

$(BIN): $(SRC) uv.lock
	rm -rf $(STAGE)
	mkdir -p $(STAGE) $(DIST)
	@# the locked runtime dependencies (pure Python), bundled beside anci
	uv export --quiet --frozen --no-dev --no-emit-project --no-hashes -o .build/requirements.txt
	uv pip install --quiet --target $(STAGE) --python $(PYTHON) -r .build/requirements.txt
	cp -r anci $(STAGE)/anci
	find $(STAGE) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	cp packaging/zipapp_main.py $(STAGE)/__main__.py
	$(PYTHON) -m zipapp $(STAGE) -p "/usr/bin/env python3" -o $(BIN)
	chmod 0755 $(BIN)
	sha256sum $(BIN) > $(BIN).sha256
	@echo "built $(BIN) ($$(du -h $(BIN) | cut -f1))"

release: $(SRC) pyproject.toml
	rm -rf $(RELEASE)
	mkdir -p $(RELEASE)
	$(PEX) . -c anci --venv prepend --scie eager --scie-only \
	    --scie-python-version $(SCIE_PYTHON) --scie-pbs-stripped \
	    $(addprefix --scie-platform ,$(SCIE_PLATFORMS)) \
	    --scie-name-style platform-file-suffix -o $(RELEASE)/anci
	cd $(RELEASE) && sha256sum anci-* > SHA256SUMS
	@# smoke test: this machine's binary runs and reports the source's version
	@host=$$(uname -s | tr A-Z a-z | sed 's/darwin/macos/')-$$(uname -m | sed 's/arm64/aarch64/'); \
	want=$$(sed -n 's/^__version__ = "\(.*\)"/\1/p' anci/__init__.py); \
	got=$$($(RELEASE)/anci-$$host --version); \
	[ "$$got" = "anci $$want" ] || { echo "smoke test failed: '$$got' != 'anci $$want'"; exit 1; }
	@echo "built $(RELEASE): $$(cd $(RELEASE) && ls anci-* | tr '\n' ' ')"

test:
	uv run python -m unittest discover -s tests -t .

clean:
	rm -rf $(DIST) .build
