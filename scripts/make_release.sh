#!/usr/bin/env bash
# Build the release files into dist/: slot17.tar.gz, install.sh and SHA256SUMS.
#
#     bash scripts/make_release.sh            from the current commit
#     bash scripts/make_release.sh v1.0.0     from a tag
#
# Only committed files go in, so nothing local (keys, recordings, calibration) can leak.
# The iPhone app (Slot17.ipa) is built on a Mac or in the cloud and added to the release by hand.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ref="${1:-HEAD}"
cd "$ROOT"
git rev-parse --verify --quiet "$ref^{commit}" >/dev/null || { echo "no such commit or tag: $ref" >&2; exit 1; }

rm -rf dist
mkdir dist
git archive --format=tar --prefix=slot17/ "$ref" | gzip -9 > dist/slot17.tar.gz
git show "$ref:install.sh" > dist/install.sh
(cd dist && sha256sum slot17.tar.gz install.sh > SHA256SUMS)
echo "Built from $(git rev-parse --short "$ref"), version $(git show "$ref:VERSION"):"
ls -l dist
