#!/usr/bin/env bash
# Upstream has no macOS binaries. Build the exact product pin rather than
# allowing Homebrew's latest version to decide what a release can package.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output="${1:?Usage: build-pinned-livekit.sh OUTPUT_BINARY}"
pinned="$(sed -n 's/^LIVEKIT_SERVER_PINNED_VERSION = "\(.*\)"$/\1/p' "$repo_root/openbase_coder_cli/livekit_version.py")"
[[ "$pinned" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid LiveKit version pin" >&2; exit 1; }
source_dir="$(mktemp -d)"
trap 'rm -rf "$source_dir"' EXIT
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "v${pinned}" https://github.com/livekit/livekit.git "$source_dir"
mkdir -p "$(dirname -- "$output")"
output="$(cd -- "$(dirname -- "$output")" && pwd)/$(basename -- "$output")"
(cd "$source_dir" && go build -o "$output" ./cmd/server)
actual="$("$output" --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
[[ "$actual" == "$pinned" ]] || { echo "Built LiveKit $actual does not match pinned $pinned" >&2; exit 1; }
echo "Built pinned livekit-server ${pinned}"
