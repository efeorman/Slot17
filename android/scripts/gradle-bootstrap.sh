#!/usr/bin/env bash
# Makes ./gradlew available. The repository carries the wrapper once it exists; until then (and on
# a fresh clone without it) this fetches the pinned Gradle from gradle.org, checks its published
# SHA-256 and generates the wrapper with that checksum pinned in gradle-wrapper.properties.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=8.14.6
if [ -x ./gradlew ] && [ -f gradle/wrapper/gradle-wrapper.jar ]; then
  echo "wrapper present"; exit 0
fi
DIST=.gradle-dist
mkdir -p "$DIST"
ZIP="$DIST/gradle-$VERSION-bin.zip"
if [ ! -d "$DIST/gradle-$VERSION" ]; then
  curl -fsSL -o "$ZIP" "https://services.gradle.org/distributions/gradle-$VERSION-bin.zip"
  SUM=$(curl -fsSL "https://services.gradle.org/distributions/gradle-$VERSION-bin.zip.sha256")
  echo "$SUM  $ZIP" | shasum -a 256 -c -
  unzip -q "$ZIP" -d "$DIST"
fi
SUM=$(curl -fsSL "https://services.gradle.org/distributions/gradle-$VERSION-bin.zip.sha256")
"$DIST/gradle-$VERSION/bin/gradle" --no-daemon -q wrapper --gradle-version "$VERSION" \
  --distribution-type bin --gradle-distribution-sha256-sum "$SUM"
echo "wrapper generated for Gradle $VERSION"
