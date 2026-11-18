#!/usr/bin/env bash
# Slot17 installer for Debian-based Linux (Raspberry Pi OS, Debian, Ubuntu).
#
# It downloads the latest release into ~/Slot17 and starts the setup, which asks
# what this machine is. Run it as your normal user, not as root:
#
#     curl -fsSL https://github.com/efeorman/Slot17/releases/latest/download/install.sh | bash
#
# Anything after "bash -s --" is passed to the setup, for example:
#
#     curl -fsSL .../install.sh | bash -s -- car
#     curl -fsSL .../install.sh | bash -s -- server
#
# Running it again updates the code and keeps your settings, recordings and keys.
#
# Environment, mainly for testing:
#   SLOT17_DIR       where to install (default ~/Slot17)
#   SLOT17_TARBALL   release archive to use, a URL or a file path
set -euo pipefail

REPO="efeorman/Slot17"
DIR="${SLOT17_DIR:-$HOME/Slot17}"
TARBALL="${SLOT17_TARBALL:-https://github.com/$REPO/releases/latest/download/slot17.tar.gz}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mStopped: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run this as your normal user, not as root or with sudo. It asks for your password when it needs it."
command -v apt-get >/dev/null || die "this installer needs a Debian-based system (Raspberry Pi OS, Debian or Ubuntu)."
command -v tar >/dev/null || die "the 'tar' command is missing."
if systemctl is-active --quiet tetra-detector.service 2>/dev/null; then
    die "the detector is running. Stop it first with: sudo systemctl stop tetra-detector.service"
fi

say "Downloading Slot17"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
case "$TARBALL" in
    http://*|https://*)
        if command -v curl >/dev/null; then
            curl -fsSL "$TARBALL" -o "$work/slot17.tar.gz" || die "could not download $TARBALL"
        elif command -v wget >/dev/null; then
            wget -qO "$work/slot17.tar.gz" "$TARBALL" || die "could not download $TARBALL"
        else
            die "neither curl nor wget is installed. Install one with: sudo apt install curl"
        fi ;;
    *) cp "$TARBALL" "$work/slot17.tar.gz" || die "could not read $TARBALL" ;;
esac
tar -xzf "$work/slot17.tar.gz" -C "$work" || die "the download is damaged; try again"
[ -f "$work/slot17/setup.sh" ] || die "the download does not look like a Slot17 release"

if [ -d "$DIR" ]; then
    echo "Updating $DIR. Your settings, recordings and logs are kept."
else
    echo "Installing into $DIR"
fi
mkdir -p "$DIR"
# Copy over the old code. Files that only exist on this machine, such as recordings,
# logs and this machine's calibration, are not in the release and so are left alone.
cp -a "$work/slot17/." "$DIR/"
echo "Version: $(cat "$DIR/VERSION" 2>/dev/null || echo unknown)"

cd "$DIR"
if [ -t 0 ]; then
    exec bash setup.sh "$@"
elif [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
    # Piped from curl: the setup's questions are read from the keyboard instead.
    exec bash setup.sh "$@" </dev/tty
else
    exec bash setup.sh "$@"
fi
