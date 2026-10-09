#!/bin/bash
# One-time step for workspaces still running an image from before
# persist-home-state.sh: run it INSIDE the running workspace (Maritime exec)
# right before an in-place image redeploy. The redeploy replaces $HOME, so the
# new image's entrypoint can no longer adopt the registry from there; this
# copies it onto the data volume first, where the new entrypoint links it.
# Safe while services run: the SQLite store is copied with the online backup
# API. Read-only for the home directory.
#
# The Maritime exec API runs commands as root, so the paths are explicit and
# never taken from $HOME (root's is /root), and everything written is given to
# the home directory's owner: a root-owned copy of the store is read-only for
# the workspace user after the redeploy (the Django API and the LiveKit agent
# then fail with "attempt to write a readonly database").
#
# Usage: pre-upgrade-copy-home-state.sh [--refresh] [home] [data-dir]
#   home      the workspace user's home (default /home/openbase)
#   data-dir  the durable data dir (default $OPENBASE_CODER_CLI_DATA_DIR or
#             /data/openbase)
#   --refresh an existing volume copy is set aside as <dest>.replaced-<stamp>
#             (never deleted) and copied again, so the copy can be made in the
#             same breath as the redeploy; without it an existing copy is kept.
set -euo pipefail

refresh=0
if [ "${1:-}" = "--refresh" ]; then refresh=1; shift; fi
home="${1:-/home/openbase}"
data_dir="${2:-${OPENBASE_CODER_CLI_DATA_DIR:-/data/openbase}}"
if [ ! -d "$home" ]; then echo "home $home is not a directory" >&2; exit 2; fi
mkdir -p "$data_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

# GNU stat (the image) and BSD stat (the test host) spell these differently.
owner_of() { stat -c '%u:%g' "$1" 2>/dev/null || stat -f '%u:%g' "$1"; }
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%OLp' "$1"; }
owner="$(owner_of "$home")"

give_to_owner() {
    chown -R "$owner" "$@"
}

set_aside() {
    local dest="$1" parked
    parked="$(mktemp -d "$dest.replaced-$stamp-XXXXXX")"
    mv "$dest" "$parked/state"
    echo "set aside $dest as $parked/state"
}

copy_dir() {
    local src="$1" dest="$2"
    if [ -L "$src" ] || [ ! -d "$src" ]; then echo "skip $src (not a real directory)"; return 0; fi
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        if [ "$refresh" = 1 ]; then set_aside "$dest"; else echo "skip $src ($dest already exists)"; return 0; fi
    fi
    local copying
    copying="$(mktemp -d "$dest.copying-XXXXXX")"
    cp -a "$src/." "$copying/"
    # Replace any SQLite files with consistent online backups, keeping the
    # source file's mode (the backup is created with the caller's umask).
    find "$src" -maxdepth 1 -name '*.sqlite3' -type f | while read -r db; do
        python3 -c 'import sqlite3, sys
from pathlib import Path
src = sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + "?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()' "$db" "$copying/$(basename "$db").backup"
        rm -f "$copying/$(basename "$db")-wal" "$copying/$(basename "$db")-shm" "$copying/$(basename "$db")-journal"
        chmod "$(mode_of "$db")" "$copying/$(basename "$db").backup"
        mv "$copying/$(basename "$db").backup" "$copying/$(basename "$db")"
    done
    chmod "$(mode_of "$src")" "$copying"
    give_to_owner "$copying"
    mv "$copying" "$dest"
    echo "copied $src -> $dest"
}

copy_dir "$home/.super-agents" "$data_dir/super-agents"
copy_dir "$home/.local/share/super-agents-claude-code" "$data_dir/super-agents-claude-code"
legacy_projects="$home/.openbase/coder-projects.json"
if [ -f "$legacy_projects" ] && [ "$home/.openbase" != "$data_dir" ]; then
    if [ -e "$data_dir/coder-projects.json" ] && [ "$refresh" = 1 ]; then
        mv "$data_dir/coder-projects.json" "$data_dir/coder-projects.json.replaced-$stamp"
        echo "set aside $data_dir/coder-projects.json as $data_dir/coder-projects.json.replaced-$stamp"
    fi
    if [ ! -e "$data_dir/coder-projects.json" ]; then
        cp -p "$legacy_projects" "$data_dir/coder-projects.json"
        give_to_owner "$data_dir/coder-projects.json"
        echo "copied coder-projects.json"
    fi
fi
