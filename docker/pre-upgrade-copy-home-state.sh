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

copy_dir() {
    local src="$1" dest="$2"
    if [ -L "$src" ] || [ ! -d "$src" ]; then echo "skip $src (not a real directory)"; return 0; fi
    if [ -e "$dest" ]; then echo "skip $src ($dest already exists)"; return 0; fi
    cp -a "$src" "$dest.copying"
    # Replace any SQLite files with consistent online backups.
    find "$src" -maxdepth 1 -name '*.sqlite3' -type f | while read -r db; do
        python3 -c 'import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()' "$db" "$dest.copying/$(basename "$db")"
        rm -f "$dest.copying/$(basename "$db")-wal" "$dest.copying/$(basename "$db")-shm"
    done
    mv "$dest.copying" "$dest"
    echo "copied $src -> $dest"
}

copy_dir "$home/.super-agents" "$data_dir/super-agents"
copy_dir "$home/.local/share/super-agents-claude-code" "$data_dir/super-agents-claude-code"
if [ -f "$home/.openbase/coder-projects.json" ] && [ ! -e "$data_dir/coder-projects.json" ] && [ "$home/.openbase" != "$data_dir" ]; then
    cp -p "$home/.openbase/coder-projects.json" "$data_dir/coder-projects.json"
    echo "copied coder-projects.json"
fi
