#!/usr/bin/env bash
# The one place release and image versions are computed (auto-release.yml,
# docker-image.yml). Run from a full-history checkout (fetch-depth: 0).
#
#   release-version.sh next BRANCH [BUMP]
#       The version auto-release cuts for a push to BRANCH: stable releases
#       bump the highest stable tag (BUMP = patch|minor|major, default minor);
#       staging releases are BUMP-independent .dev0 versions.
#   release-version.sh stamp BRANCH
#       The version an image built from HEAD on BRANCH reports. A staging
#       image is built in parallel with the staging release cut from the
#       same commit, so it stamps that release's version instead of waiting
#       for its tag. Elsewhere: the tag at HEAD, else a valid PEP 440
#       successor of the nearest tag (X.Y.Z.postN past a stable tag,
#       X.Y.Z.devN past a X.Y.Z.devM staging tag; X.Y.Z.devM.postN is
#       not PEP 440 and broke the build).
set -euo pipefail

mode="${1:?usage: release-version.sh next|stamp BRANCH [BUMP]}"
branch="${2:?usage: release-version.sh next|stamp BRANCH [BUMP]}"
bump="${3:-minor}"

# Highest tag after stripping a .devN suffix and dropping a/b/rc/- tags.
highest_tag() { # $1 = 1 to include .dev tags
    git tag --list 'v[0-9]*' \
        | sed -e 's/^v//' \
        | { if [[ "$1" == 1 ]]; then sed -e 's/\.dev[0-9]*$//'; else grep -Ev '\.dev'; fi; } \
        | grep -Ev '([0-9](a|b|rc)[0-9])|-' \
        | sort -t. -k1,1n -k2,2n -k3,3n \
        | tail -1
}

next_version() {
    local latest major minor patch
    if [[ "$branch" == "staging" ]]; then
        # Staging releases are PEP 440 dev releases: GitHub prereleases,
        # routed to the staging channel by the .dev marker (which also keeps
        # them out of beta resolution). The tag must end in exactly .dev0 —
        # setuptools-scm refuses to version commits past any other .devN
        # tag, which would break every from-source build (uv tool install
        # git+..., dev workspace syncs). So each staging release patch-bumps
        # past the highest existing tag of any kind; stable keeps
        # minor-bumping, so the next stable still sorts above every staging
        # build.
        latest="$(highest_tag 1)"
        IFS=. read -r major minor patch <<< "${latest:-0.0.0}"
        echo "${major}.${minor}.$((patch + 1)).dev0"
        return
    fi
    latest="$(highest_tag 0)"
    IFS=. read -r major minor patch <<< "${latest:-0.0.0}"
    case "$bump" in
        major) major=$((major + 1)); minor=0; patch=0 ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        patch) patch=$((patch + 1)) ;;
        *) echo "unknown bump: $bump" >&2; exit 2 ;;
    esac
    echo "${major}.${minor}.${patch}"
}

stamp_version() {
    local exact tag count base
    exact="$(git describe --tags --exact-match --match 'v*' HEAD 2>/dev/null || true)"
    if [[ -n "$exact" ]]; then
        echo "${exact#v}"
        return
    fi
    if [[ "$branch" == "staging" ]]; then
        next_version
        return
    fi
    tag="$(git describe --tags --abbrev=0 --match 'v*' 2>/dev/null || true)"
    if [[ -n "$tag" ]]; then
        count="$(git rev-list --count "${tag}..HEAD")"
    else
        tag="v0.0.0"
        count="$(git rev-list --count HEAD)"
    fi
    base="${tag#v}"
    if [[ "$base" =~ ^(.*)\.dev[0-9]+$ ]]; then
        echo "${BASH_REMATCH[1]}.dev${count}"
    else
        echo "${base}.post${count}"
    fi
}

case "$mode" in
    next) next_version ;;
    stamp) stamp_version ;;
    *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac
