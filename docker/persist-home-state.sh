#!/bin/bash
# Keep agent state that tools write under $HOME inside the persistent data
# volume. On Maritime only /data survives a redeploy: the image layer, $HOME
# included, is replaced, and the Super Agents registry (thread ids, the
# Dispatcher's thread, Super Agent names) lived in $HOME and was lost on the
# 2026-10-09 in-place image upgrade of a staging workspace. Each directory
# becomes a symlink into $DATA_DIR. A real directory found in $HOME is this
# container layer's live state, so it is adopted into $DATA_DIR (any copy
# already there is set aside, never deleted). Idempotent; run before any
# service starts.
#
# Usage: persist-home-state.sh <home> <data-dir>
set -euo pipefail

home="$1"
data_dir="$2"

persist_dir() {
    local home_path="$1" data_path="$2"
    mkdir -p "$(dirname "$home_path")" "$(dirname "$data_path")"
    if [ -L "$home_path" ] && { [ "$(readlink "$home_path")" = "$data_path" ] || [ "$home_path" -ef "$data_path" ]; }; then
        mkdir -p "$data_path"
        chmod 0700 "$data_path"
        return 0
    fi
    if [ -d "$home_path" ]; then
        local adopting
        adopting="$(mktemp -d "$data_path.adopting-XXXXXX")"
        cp -a "$home_path/." "$adopting/"
        if [ -e "$data_path" ] || [ -L "$data_path" ]; then
            local parked
            parked="$(mktemp -d "$data_path.replaced-XXXXXX")"
            mv "$data_path" "$parked/state"
            echo "[persist-home-state] kept the previous $data_path as $parked"
        fi
        mv "$adopting" "$data_path"
        echo "[persist-home-state] adopted $home_path into $data_path"
    elif [ -e "$home_path" ]; then
        echo "[persist-home-state] $home_path is not a directory; refusing to start" >&2
        return 1
    fi
    if [ -e "$home_path" ] || [ -L "$home_path" ]; then
        local retired
        retired="$(mktemp -d "$home_path.migrated-XXXXXX")"
        mv "$home_path" "$retired/state"
    fi
    mkdir -p "$data_path"
    chmod 0700 "$data_path"
    ln -s "$data_path" "$home_path"
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
