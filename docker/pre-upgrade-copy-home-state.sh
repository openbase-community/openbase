#!/bin/bash
# One-time step for workspaces still running an image from before
# persist-home-state.sh: run it INSIDE the running workspace (Maritime exec)
# right before an in-place image redeploy. The redeploy replaces $HOME, so the
# new image's entrypoint can no longer adopt the registry from there; this
# copies it onto the data volume first, where the new entrypoint links it.
# Safe while services run: the SQLite store is copied with the online backup
# API. Read-only for the home directory.
#
# Invariant: a volume copy is never replaced by an older store. A workspace
# that woke on an older image than the one last deployed brings back that
# image layer's $HOME stores (2026-10-10, staging workspace 374): copying
# such a store over the volume parked the newer registry and lost threads.
# An existing volume copy is therefore compared with the $HOME store by the
# newest modification time anywhere in each tree, and kept when it is at
# least as new; only --force overrides that.
#
# The Maritime exec API runs commands as root, so the paths are explicit and
# never taken from $HOME (root's is /root), and everything written is given to
# the home directory's owner: a root-owned copy of the store is read-only for
# the workspace user after the redeploy (the Django API and the LiveKit agent
# then fail with "attempt to write a readonly database").
#
# Usage: pre-upgrade-copy-home-state.sh [--refresh] [--force] [home] [data-dir]
#   home      the workspace user's home (default /home/openbase)
#   data-dir  the durable data dir (default $OPENBASE_CODER_CLI_DATA_DIR or
#             /data/openbase)
#   --refresh an existing volume copy that is older than the $HOME store is
#             set aside as <dest>.replaced-<stamp> (never deleted) and copied
#             again, so the copy can be made in the same breath as the
#             redeploy; without it an existing copy is always kept.
#   --force   replace an existing volume copy even when it is newer than the
#             $HOME store (still set aside, never deleted). Only for a
#             deliberate operator override; implies --refresh.
set -euo pipefail

refresh=0
force=0
while [ $# -gt 0 ]; do
    case "$1" in
        --refresh) refresh=1 ;;
        --force) force=1; refresh=1 ;;
        --) shift; break ;;
        -*) echo "unknown option $1" >&2; exit 2 ;;
        *) break ;;
    esac
    shift
done
home="${1:-/home/openbase}"
data_dir="${2:-${OPENBASE_CODER_CLI_DATA_DIR:-/data/openbase}}"
if [ ! -d "$home" ]; then echo "home $home is not a directory" >&2; exit 2; fi
mkdir -p "$data_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [ "$force" = 1 ]; then
    echo "WARNING: --force replaces every existing volume copy under $data_dir with the $home store even where the volume copy is newer; the replaced copies are set aside, not deleted" >&2
fi

# GNU stat (the image) and BSD stat (the test host) spell these differently.
owner_of() { stat -c '%u:%g' "$1" 2>/dev/null || stat -f '%u:%g' "$1"; }
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%OLp' "$1"; }
if stat -c '%.9Y' "$home" >/dev/null 2>&1; then mtime_format=(-c '%.9Y'); else mtime_format=(-f '%Fm'); fi
owner="$(owner_of "$home")"
chown "$owner" "$data_dir"

# Newest modification time in a tree (the root entry included; symlinks are
# not followed), as integer nanoseconds since the epoch so that copies made
# within the same second still compare correctly.
newest_mtime() {
    find "$1" -exec stat "${mtime_format[@]}" {} + | {
        local newest=0 line seconds fraction
        while IFS= read -r line; do
            seconds="${line%%.*}"
            fraction="${line#*.}"
            if [ "$fraction" = "$line" ]; then fraction=""; fi
            fraction="$(printf '%-9.9s' "$fraction" | tr ' ' 0)"
            line=$((seconds * 1000000000 + 10#$fraction))
            if [ "$line" -gt "$newest" ]; then newest="$line"; fi
        done
        echo "$newest"
    } || {
        echo "cannot determine freshness of $1; refusing to replace or retire state" >&2
        return 1
    }
}

give_to_owner() {
    chown -R "$owner" "$@"
}

set_aside() {
    local dest="$1" parked
    parked="$(mktemp -d "$dest.replaced-$stamp-XXXXXX")"
    chown "$owner" "$parked"
    mv "$dest" "$parked/state"
    echo "set aside $dest as $parked/state"
}

# Decide what to do about an existing destination: 0 = replace it (already set
# aside), 1 = keep it (a line has been printed). Never replaces a destination
# that is at least as new as the source unless --force was given.
replace_existing() {
    local src="$1" dest="$2" src_mtime dest_mtime
    if [ "$force" = 1 ]; then
        echo "replacing $dest with $src under --force (freshness not compared)"
        set_aside "$dest"
        return 0
    fi
    src_mtime="$(newest_mtime "$src")" || exit 1
    dest_mtime="$(newest_mtime "$dest")" || exit 1
    if [ "$dest_mtime" -gt "$src_mtime" ]; then
        echo "kept $dest (volume copy is newer than $src)"
        return 1
    elif [ "$dest_mtime" -eq "$src_mtime" ]; then
        echo "kept $dest (volume copy is as new as $src)"
        return 1
    elif [ "$refresh" = 1 ]; then
        set_aside "$dest"
        return 0
    fi
    echo "skip $src ($dest already exists; it is older than $src, rerun with --refresh to replace it)"
    return 1
}

copy_dir() {
    local src="$1" dest="$2"
    if [ -L "$src" ] || [ ! -d "$src" ]; then echo "skip $src (not a real directory)"; return 0; fi
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        replace_existing "$src" "$dest" || return 0
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
copy_dir "$home/.config/gh" "$data_dir/github-cli"
legacy_projects="$home/.openbase/coder-projects.json"
durable_projects="$data_dir/coder-projects.json"
if [ -f "$legacy_projects" ] && [ ! "$legacy_projects" -ef "$durable_projects" ]; then
    copy_projects=1
    if [ -e "$durable_projects" ] || [ -L "$durable_projects" ]; then
        replace_existing "$legacy_projects" "$durable_projects" || copy_projects=0
    fi
    if [ "$copy_projects" = 1 ]; then
        cp -p "$legacy_projects" "$durable_projects"
        give_to_owner "$durable_projects"
        echo "copied coder-projects.json"
    fi
fi
