#!/bin/sh
# Install (or upgrade) anci: one self-contained binary, no Python needed.
#
#   curl -fsSL https://codeberg.org/zwa/ancisite/raw/branch/main/anci-install.sh | sh
#
# Settings (environment):
#   ANCI_VERSION   release tag to install (default: the latest release)
#   ANCI_BIN_DIR   where to put `anci` (default: ~/.local/bin)
#   ANCI_BASE_URL  where release files are downloaded from (default: Codeberg;
#                  a test server serves <version>/<file> under it)
set -eu

REPO="zwa/ancisite"
BIN_DIR="${ANCI_BIN_DIR:-$HOME/.local/bin}"
BASE_URL="${ANCI_BASE_URL:-https://codeberg.org/$REPO/releases/download}"

say() { printf '  %s\n' "$*"; }
die() { printf 'anci-install: %s\n' "$*" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"

# 1. which binary: <os>-<arch>
case "$(uname -s)" in
  Linux)  os=linux ;;
  Darwin) os=macos ;;
  *) die "unsupported OS: $(uname -s) (anci runs on Linux and macOS)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  arch=x86_64 ;;
  aarch64|arm64) arch=aarch64 ;;
  *) die "unsupported CPU: $(uname -m) (anci ships x86_64 and aarch64)" ;;
esac
target="$os-$arch"

# 2. which version: pinned, or the latest release
version="${ANCI_VERSION:-}"
if [ -z "$version" ]; then
  version=$(curl -fsL "https://codeberg.org/api/v1/repos/$REPO/releases/latest" \
            | sed -n 's/.*"tag_name":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1) \
    || die "couldn't look up the latest release (set ANCI_VERSION to pick one)"
  [ -n "$version" ] || die "no release found (set ANCI_VERSION to pick one)"
fi
say "installing anci $version ($target)"

# 3. download into a temp dir that's always cleaned up
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM
curl -fsSL -o "$tmp/anci" "$BASE_URL/$version/anci-$target" \
  || die "download failed: $BASE_URL/$version/anci-$target"
curl -fsSL -o "$tmp/SHA256SUMS" "$BASE_URL/$version/SHA256SUMS" \
  || die "download failed: $BASE_URL/$version/SHA256SUMS"

# 4. verify before installing anything
want=$(awk -v f="anci-$target" '$2 == f { print $1 }' "$tmp/SHA256SUMS")
[ -n "$want" ] || die "SHA256SUMS has no entry for anci-$target"
if command -v sha256sum >/dev/null 2>&1; then
  got=$(sha256sum "$tmp/anci" | awk '{ print $1 }')
else
  got=$(shasum -a 256 "$tmp/anci" | awk '{ print $1 }')
fi
[ "$got" = "$want" ] || die "checksum mismatch for anci-$target: nothing was installed"

# 5. install: atomic replace, so re-running this script is the upgrade
mkdir -p "$BIN_DIR"
chmod 0755 "$tmp/anci"
mv -f "$tmp/anci" "$BIN_DIR/anci.new"
mv -f "$BIN_DIR/anci.new" "$BIN_DIR/anci"
say "installed $BIN_DIR/anci"

# 6. PATH hint (profiles are never edited)
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "add it to your PATH:  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

# 7. what anci shells out to — warnings only
for tool in ssh rsync; do
  command -v "$tool" >/dev/null 2>&1 || say "warning: '$tool' isn't installed; anci needs it"
done
command -v virsh >/dev/null 2>&1 \
  || say "note: no libvirt here — only needed for mode = 'full' on this machine"

say "$("$BIN_DIR/anci" --version) — next: anci init"
