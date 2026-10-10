#!/bin/bash
# Keep agent state that tools write under $HOME inside the persistent data
# volume. On Maritime only /data survives a redeploy: the image layer, $HOME
# included, is replaced, and the Super Agents registry (thread ids, the
# Dispatcher's thread, Super Agent names) lived in $HOME and was lost on the
# 2026-10-09 in-place image upgrade of a staging workspace. Each directory
# becomes a symlink into $DATA_DIR.
#
# A real directory found in $HOME is adopted into $DATA_DIR only when the
# volume holds nothing for it or an older copy (which is set aside, never
# deleted). A workspace that wakes on an older image than the one last
# deployed brings back that image layer's stores, older than the volume's
# (2026-10-10, staging workspace 374): the volume copy then wins and the
# $HOME store is retired in the image layer. Freshness is the newest
# modification time anywhere in each tree; a tie keeps the volume copy.
# Idempotent; run before any service starts.
#
# Usage: persist-home-state.sh <home> <data-dir>
set -euo pipefail

home="$1"
data_dir="$2"

# GNU stat (the image) and BSD stat (the test host) spell the format
# differently. Same helper as docker/pre-upgrade-copy-home-state.sh, which
# is sent as self-contained script text through the Maritime exec API and
# so cannot share a file with this one.
if stat -c '%.9Y' "$home" >/dev/null 2>&1; then mtime_format=(-c '%.9Y'); else mtime_format=(-f '%Fm'); fi

# Newest modification time in a tree (the root entry included; symlinks are
# not followed), as integer nanoseconds since the epoch so that copies made
# within the same second still compare correctly.
newest_mtime() {
    local newest=0 line seconds fraction
    while IFS= read -r line; do
        seconds="${line%%.*}"
        fraction="${line#*.}"
        if [ "$fraction" = "$line" ]; then fraction=""; fi
        fraction="$(printf '%-9.9s' "$fraction" | tr ' ' 0)"
        line=$((seconds * 1000000000 + 10#$fraction))
        if [ "$line" -gt "$newest" ]; then newest="$line"; fi
    done < <(find "$1" -exec stat "${mtime_format[@]}" {} +)
    echo "$newest"
}

persist_dir() {
    local home_path="$1" data_path="$2"
    mkdir -p "$(dirname "$home_path")" "$(dirname "$data_path")"
    if [ -L "$home_path" ] && { [ "$(readlink "$home_path")" = "$data_path" ] || [ "$home_path" -ef "$data_path" ]; }; then
        mkdir -p "$data_path"
        chmod 0700 "$data_path"
        return 0
    fi
    local verdict=""
    if [ -d "$home_path" ]; then
        local adopt=1
        if [ -e "$data_path" ] || [ -L "$data_path" ]; then
            local home_mtime data_mtime
            home_mtime="$(newest_mtime "$home_path")"
            data_mtime="$(newest_mtime "$data_path")"
            if [ "$data_mtime" -ge "$home_mtime" ]; then
                adopt=0
                verdict="kept $data_path: the volume copy is at least as new as $home_path (an older image layer's store)"
            else
                verdict="adopted $home_path into $data_path: it is newer than the volume copy"
            fi
        else
            verdict="adopted $home_path into $data_path: the volume held no copy"
        fi
        if [ "$adopt" = 1 ]; then
            local adopting
            adopting="$(mktemp -d "$data_path.adopting-XXXXXX")"
            cp -a "$home_path/." "$adopting/"
            if [ -e "$data_path" ] || [ -L "$data_path" ]; then
                local parked
                parked="$(mktemp -d "$data_path.replaced-XXXXXX")"
                mv "$data_path" "$parked/state"
                verdict="$verdict, kept as $parked/state"
            fi
            mv "$adopting" "$data_path"
        fi
    elif [ -e "$home_path" ]; then
        echo "[persist-home-state] $home_path is not a directory; refusing to start" >&2
        return 1
    fi
    if [ -e "$home_path" ] || [ -L "$home_path" ]; then
        local retired
        retired="$(mktemp -d "$home_path.migrated-XXXXXX")"
        mv "$home_path" "$retired/state"
        verdict="${verdict:+$verdict; }retired $home_path as $retired/state"
    fi
    mkdir -p "$data_path"
    chmod 0700 "$data_path"
    ln -s "$data_path" "$home_path"
    if [ -n "$verdict" ]; then echo "[persist-home-state] $verdict"; fi
}

# Super Agents: shared registry (state, queues, approvals, backend provenance)
# and the Claude Code store (sessions, turns, logs).
persist_dir "$home/.super-agents" "$data_dir/super-agents"
persist_dir "$home/.local/share/super-agents-claude-code" "$data_dir/super-agents-claude-code"

# The projects cache now lives in the data dir; adopt one left in $HOME.
legacy_projects="$home/.openbase/coder-projects.json"
if [ "$home/.openbase" != "$data_dir" ] && [ -f "$legacy_projects" ] && [ ! -L "$legacy_projects" ]; then
    [ -e "$data_dir/coder-projects.json" ] || cp -p "$legacy_projects" "$data_dir/coder-projects.json"
fi
