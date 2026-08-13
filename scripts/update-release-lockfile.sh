#!/bin/sh
# Regenerate the pinned lockfile for the release build workspace.
# Run from cli/ after changing frontend dependencies in any member repo.
# The release workflow copies scripts/release-workspace/* to the build root,
# so third-party npm versions are frozen per release instead of floating.
set -eu
cli_dir="$(cd "$(dirname "$0")/.." && pwd)"
workspace_dir="$(dirname "$cli_dir")"
build_dir="$(mktemp -d)"
trap 'rm -rf "$build_dir"' EXIT

cp "$cli_dir/scripts/release-workspace/package.json" "$build_dir/"
cp "$cli_dir/scripts/release-workspace/pnpm-workspace.yaml" "$build_dir/"
# Seed the current lockfile so unchanged third-party deps keep their pinned
# versions; without it pnpm re-resolves every dependency from scratch.
if [ -f "$cli_dir/scripts/release-workspace/pnpm-lock.yaml" ]; then
  cp "$cli_dir/scripts/release-workspace/pnpm-lock.yaml" "$build_dir/"
fi
for repo in console coder-react multi-react boilersync-react; do
  mkdir -p "$build_dir/$repo"
  cp "$workspace_dir/$repo/package.json" "$build_dir/$repo/"
done

(cd "$build_dir" && pnpm install --lockfile-only)
cp "$build_dir/pnpm-lock.yaml" "$cli_dir/scripts/release-workspace/pnpm-lock.yaml"
echo "Updated scripts/release-workspace/pnpm-lock.yaml"
