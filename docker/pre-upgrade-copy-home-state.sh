#!/bin/bash
# One-time step for workspaces still running an image from before
# persist-home-state.sh: run it INSIDE the running workspace (Maritime exec)
# right before an in-place image redeploy. The redeploy replaces $HOME, so the
# new image's entrypoint can no longer adopt the registry from there; this
# copies it onto the data volume first, where the new entrypoint links it.
# Safe while services run: the SQLite store is copied with the online backup
# API. Never overwrites an existing volume copy. Read-only for $HOME.
#
# Usage: pre-upgrade-copy-home-state.sh [home] [data-dir]
set -euo pipefail
home="${1:-$HOME}"
data_dir="${2:-${OPENBASE_CODER_CLI_DATA_DIR:-/data/openbase}}"
mkdir -p "$data_dir"

copy_dir() {
    local src="$1" dest="$2"
    if [ -L "$src" ] || [ ! -d "$src" ]; then echo "skip $src (not a real directory)"; return 0; fi
    if [ -e "$dest" ]; then echo "skip $src ($dest already exists)"; return 0; fi
    local copying
    copying="$(mktemp -d "$dest.copying-XXXXXX")"
    cp -a "$src/." "$copying/"
    # Replace any SQLite files with consistent online backups.
    find "$src" -maxdepth 1 -name '*.sqlite3' -type f | while read -r db; do
        python3 -c 'import sqlite3, sys
from pathlib import Path
src = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()' "$db" "$copying/$(basename "$db").backup"
        rm -f "$copying/$(basename "$db")-wal" "$copying/$(basename "$db")-shm" "$copying/$(basename "$db")-journal"
        mv "$copying/$(basename "$db").backup" "$copying/$(basename "$db")"
    done
    mv "$copying" "$dest"
    echo "copied $src -> $dest"
}

copy_dir "$home/.super-agents" "$data_dir/super-agents"
copy_dir "$home/.local/share/super-agents-claude-code" "$data_dir/super-agents-claude-code"
if [ -f "$home/.openbase/coder-projects.json" ] && [ ! -e "$data_dir/coder-projects.json" ] && [ "$home/.openbase" != "$data_dir" ]; then
    cp -p "$home/.openbase/coder-projects.json" "$data_dir/coder-projects.json"
    echo "copied coder-projects.json"
fi
