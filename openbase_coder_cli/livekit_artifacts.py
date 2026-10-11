"""Verified macOS engine artifacts, independent of the CLI update channel.

Upstream publishes no Darwin binaries. When changing the engine pin, add a
verified standalone package containing that engine here. A tagged URL alone
is not immutable: both archive and extracted executable digests are pinned.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DarwinLiveKitArtifact:
    url: str
    archive_sha256: str
    binary_sha256: str
    member: str = "bin/livekit-server"


# Keys deliberately include the engine version: a future pin must never reuse
# an older executable merely because the CLI release channel has not caught up.
DARWIN_LIVEKIT_ARTIFACTS = {
    ("1.13.8", "aarch64"): DarwinLiveKitArtifact(
        url=(
            "https://github.com/openbase-community/openbase/releases/download/"
            "v0.51.37.dev0/openbase-coder-package-aarch64-apple-darwin.tar.gz"
        ),
        archive_sha256=(
            "4f0c5b3eecdac0b947a3e7128a370fdd923cfd19d14b76d1590d3d245130b24e"
        ),
        binary_sha256=(
            "623e6922abf5bdd0a9bf0b9ccd0238fc9496c3b65bb8cbc9440e176743180e0d"
        ),
    ),
}
