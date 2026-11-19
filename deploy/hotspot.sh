#!/usr/bin/env bash
# Save your phone's hotspot as a Wi-Fi network, so the car computer joins it by itself in the car.
#
#     printf '%s' "$PASSWORD" | sudo bash deploy/hotspot.sh "Hotspot name"
#
# The password comes on standard input, never on the command line, so it doesn't show up in the list of
# running programs. It goes into a NetworkManager file only root can read. Saving it again replaces it.
# Exit codes: 0 saved, 2 a name or password that can't be used, 3 no NetworkManager on this system.
set -euo pipefail
NAME="${1:-}"
DIR="${SLOT17_NM_DIR:-/etc/NetworkManager/system-connections}"   # tests point this elsewhere
PW="$(cat)"

[ -n "$NAME" ] || { echo "give the hotspot's name" >&2; exit 2; }
case "$NAME" in
    *$'\n'*|*';'*|*'\'*|' '*|*' ') echo "the hotspot name has characters this can't save safely; add it with nmtui instead" >&2; exit 2 ;;
esac
[ "${#NAME}" -le 32 ] || { echo "a Wi-Fi name has at most 32 characters" >&2; exit 2; }
[ "${#PW}" -ge 8 ] && [ "${#PW}" -le 63 ] || { echo "a Wi-Fi password has 8 to 63 characters" >&2; exit 2; }
case "$PW" in *$'\n'*) echo "the password can't contain a line break" >&2; exit 2 ;; esac
if [ -z "${SLOT17_NM_DIR:-}" ] && ! command -v nmcli >/dev/null; then
    echo "NetworkManager isn't installed here; add the hotspot with this system's own Wi-Fi settings" >&2
    exit 3
fi

# NetworkManager's files are key files: a backslash starts an escape, and spaces at either end are dropped.
esc="${PW//\\/\\\\}"
case "$esc" in ' '*) esc="\\s${esc:1}" ;; esac
case "$esc" in *' ') esc="${esc%' '}\\s" ;; esac

mkdir -p "$DIR"
umask 077
tmp="$(mktemp "$DIR/.slot17-hotspot.XXXXXX")"
cat >"$tmp" <<EOF
# Written by Slot17 (deploy/hotspot.sh): your phone's hotspot, so the car computer joins it in the car.
[connection]
id=slot17-hotspot
type=wifi
autoconnect=true
autoconnect-priority=10

[wifi]
mode=infrastructure
ssid=$NAME

[wifi-security]
key-mgmt=wpa-psk
psk=$esc

[ipv4]
method=auto

[ipv6]
method=auto
EOF
chmod 600 "$tmp"
mv "$tmp" "$DIR/slot17-hotspot.nmconnection"
if [ -z "${SLOT17_NM_DIR:-}" ]; then
    nmcli connection reload
fi
echo "saved the hotspot \"$NAME\""
