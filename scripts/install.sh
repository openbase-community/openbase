#!/bin/sh

set -eu

RELEASE="${OPENBASE_CODER_RELEASE:-latest}"
REPO="${OPENBASE_CODER_RELEASE_REPO:-openbase-community/openbase}"
BIN_DIR="${OPENBASE_CODER_INSTALL_BIN_DIR:-$HOME/.local/bin}"
BASE_DIR="${OPENBASE_CODER_HOME:-$HOME/.openbase}"
PACKAGE_ROOT="$BASE_DIR/packages/standalone"
RELEASES_DIR="$PACKAGE_ROOT/releases"
CURRENT_LINK="$PACKAGE_ROOT/current"
BIN_PATH="$BIN_DIR/openbase-coder"

step() {
  printf '==> %s\n' "$1"
}

download_text() {
  url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -O - "$url"
    return
  fi
  echo "curl or wget is required to install Openbase Coder." >&2
  exit 1
}

download_file() {
  url="$1"
  output="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$output"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -O "$output" "$url"
    return
  fi
  echo "curl or wget is required to install Openbase Coder." >&2
  exit 1
}

file_sha256() {
  path="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
    return
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return
  fi
  echo "shasum or sha256sum is required to verify Openbase Coder." >&2
  exit 1
}

package_metadata_version() {
  sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -n 1
}

normalize_version() {
  case "$1" in
    "" | latest) printf 'latest\n' ;;
    v*) printf '%s\n' "${1#v}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

resolve_version() {
  version="$(normalize_version "$RELEASE")"
  if [ "$version" != "latest" ]; then
    printf '%s\n' "$version"
    return
  fi
  release_json="$(download_text "https://api.github.com/repos/$REPO/releases/latest")"
  resolved="$(printf '%s\n' "$release_json" | sed -n 's/.*"tag_name":[[:space:]]*"v\{0,1\}\([^"]*\)".*/\1/p' | head -n 1)"
  if [ -z "$resolved" ]; then
    echo "Unable to resolve latest Openbase Coder release." >&2
    exit 1
  fi
  printf '%s\n' "$resolved"
}

detect_target() {
  os="$(uname -s)"
  arch="$(uname -m)"
  if [ "$os" != "Darwin" ]; then
    echo "install.sh currently supports macOS only." >&2
    exit 1
  fi
  case "$arch" in
    arm64 | aarch64) printf 'aarch64-apple-darwin\n' ;;
    x86_64 | amd64) printf 'x86_64-apple-darwin\n' ;;
    *)
      echo "Unsupported architecture: $arch" >&2
      exit 1
      ;;
  esac
}

pick_profile() {
  case "${SHELL:-}" in
    */zsh) printf '%s\n' "$HOME/.zprofile" ;;
    */bash) printf '%s\n' "$HOME/.bash_profile" ;;
    *) printf '%s\n' "$HOME/.profile" ;;
  esac
}

add_to_path() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) return ;;
  esac
  profile="$(pick_profile)"
  begin="# >>> Openbase Coder installer >>>"
  end="# <<< Openbase Coder installer <<<"
  if [ -f "$profile" ] && grep -F "$begin" "$profile" >/dev/null 2>&1; then
    return
  fi
  {
    printf '\n%s\n' "$begin"
    printf 'export PATH="%s:$PATH"\n' "$BIN_DIR"
    printf '%s\n' "$end"
  } >>"$profile"
  step "Added $BIN_DIR to PATH in $profile"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --release)
        RELEASE="${2:?--release requires a value}"
        shift 2
        ;;
      -h | --help)
        echo "Usage: install.sh [--release VERSION]"
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 1
        ;;
    esac
  done
}

parse_args "$@"
target="$(detect_target)"
tarball="${OPENBASE_CODER_INSTALL_TARBALL:-}"
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT INT TERM

mkdir -p "$RELEASES_DIR" "$BIN_DIR"

if [ -n "$tarball" ]; then
  # Local-source mode: install from a pre-built tarball (used by CI to verify
  # install.sh against the just-built package before publishing).
  if [ ! -f "$tarball" ]; then
    echo "Local tarball not found: $tarball" >&2
    exit 1
  fi
  version="${OPENBASE_CODER_INSTALL_VERSION:-}"
  step "Installing Openbase Coder from local tarball $tarball"
else
  version="$(resolve_version)"
  asset="openbase-coder-package-$target.tar.gz"
  manifest="openbase-coder-package_SHA256SUMS"
  download_base="https://github.com/$REPO/releases/download/v$version"

  step "Installing Openbase Coder $version for $target"

  download_file "$download_base/$manifest" "$tmp_dir/$manifest"
  expected="$(awk -v asset="$asset" '$2 == asset { print $1 }' "$tmp_dir/$manifest" | head -n 1)"
  if [ -z "$expected" ]; then
    echo "Could not find checksum for $asset." >&2
    exit 1
  fi

  download_file "$download_base/$asset" "$tmp_dir/$asset"
  actual="$(file_sha256 "$tmp_dir/$asset")"
  if [ "$actual" != "$expected" ]; then
    echo "Checksum mismatch for $asset." >&2
    exit 1
  fi
  tarball="$tmp_dir/$asset"
fi

stage="$RELEASES_DIR/.staging.$$"
rm -rf "$stage"
mkdir -p "$stage"
tar -xzf "$tarball" -C "$stage"
chmod 0755 "$stage/bin/openbase-coder" "$stage/bin/livekit-server"

if [ -z "$version" ]; then
  version="$(package_metadata_version "$stage/openbase-coder-package.json")"
  if [ -z "$version" ]; then
    echo "Could not determine version from openbase-coder-package.json." >&2
    exit 1
  fi
fi

release_dir="$RELEASES_DIR/$version-$target"
rm -rf "$release_dir"
mv "$stage" "$release_dir"
ln -sfn "$release_dir" "$CURRENT_LINK"
# A wrapper script, not a symlink: the packaged launcher locates its package
# root from $0, which a symlink would resolve to $BIN_DIR instead.
rm -f "$BIN_PATH"
printf '#!/bin/sh\nexec "%s" "$@"\n' "$CURRENT_LINK/bin/openbase-coder" >"$BIN_PATH"
chmod 0755 "$BIN_PATH"
add_to_path

"$BIN_PATH" --version >/dev/null
step "Openbase Coder installed at $BIN_PATH"
printf 'Run: openbase-coder setup\n'
