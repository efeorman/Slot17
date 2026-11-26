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
# Not sim/: the hardware-free test environment is for development, not for a car computer (found in review,
# F36). Left out here rather than by export-ignore, so other archives of the tree (a test copy for the Pi) keep it.
git archive --format=tar --prefix=slot17/ "$ref" -- . ':(exclude)sim' > dist/slot17.tar
# The page for the car computer's own screen, where signed updates put it too (p0/screen.py prefers p0/web/:
# signed updates replace p0/ but never display/). The same bytes as display/page.html.
web="$(mktemp -d)"
mkdir -p "$web/slot17/p0/web"
git show "$ref:display/page.html" > "$web/slot17/p0/web/page.html"
tar --append -f dist/slot17.tar -C "$web" slot17/p0/web/page.html
rm -rf "$web"
gzip -9 dist/slot17.tar
git show "$ref:install.sh" > dist/install.sh
(cd dist && sha256sum slot17.tar.gz install.sh > SHA256SUMS)
echo "Built from $(git rev-parse --short "$ref"), version $(git show "$ref:VERSION"):"
ls -l dist
