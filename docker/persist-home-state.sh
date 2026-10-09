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
    mkdir -p "$(dirname "$home_path")"
    if [ -L "$home_path" ]; then
        [ "$(readlink "$home_path")" = "$data_path" ] && return 0
        rm "$home_path"
    elif [ -d "$home_path" ]; then
        if [ -e "$data_path" ]; then
            local parked
            parked="$data_path.replaced-$(date -u +%Y%m%dT%H%M%SZ)"
            mv "$data_path" "$parked"
            echo "[persist-home-state] kept the previous $data_path as $parked"
        fi
        mkdir -p "$(dirname "$data_path")"
        # cp then rm, not mv: $HOME and the volume are different filesystems,
        # and a half-finished cross-device mv must not lose the live copy.
        cp -a "$home_path" "$data_path.adopting"
        mv "$data_path.adopting" "$data_path"
        rm -rf "$home_path"
        echo "[persist-home-state] adopted $home_path into $data_path"
    elif [ -e "$home_path" ]; then
        echo "[persist-home-state] $home_path is not a directory; leaving it" >&2
        return 0
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
    rm -f "$legacy_projects"
fi
