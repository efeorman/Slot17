#!/usr/bin/env bash
# Slot17 image, first start: set the car computer up with what the person wrote in slot17.txt, using the
# normal installer and the release that is already on the card (no download needed). Runs as root from
# slot17-firstboot.service until it has worked once. Everything it prints also goes to
# slot17-setup.log on the boot partition, so it can be read on any computer.
#
# Exit codes: 0 done, 75 no user yet (systemd tries again shortly), 1 the setup stopped (tried again at
# the next start, after the person has fixed slot17.txt).
set -uo pipefail
BOOT=/boot/firmware
CONF="$BOOT/slot17.txt"
LOG="$BOOT/slot17-setup.log"
OPT=/opt/slot17
DONE=/var/lib/slot17/firstboot-done
INSTALLED=/var/lib/slot17/firstboot-installed
SUDOERS=/etc/sudoers.d/90-slot17-firstboot

exec > >(tee -a "$LOG") 2>&1
echo
echo "== Slot17 first start (the clock may be wrong until the car computer is online)"

# The installer's sudo grant lives only while the installer runs. A power cut while it ran left it on the
# card (found in review, 24 November 2026), so it goes first, whatever happens next; the image's tmpfiles
# rule (slot17-firstboot.tmpfiles) also removes it early at every start, before anyone can log in.
rm -f "$SUDOERS"
trap 'rm -f "$SUDOERS"' EXIT

# Raspberry Pi Imager creates the user during the first start; wait for it.
user="$(getent passwd 1000 | cut -d: -f1)"
if [ -z "$user" ]; then
    echo "waiting for the user that Raspberry Pi Imager sets up..."
    exit 75
fi
home="$(getent passwd "$user" | cut -d: -f6)"
echo "user: $user"

if [ -f "$INSTALLED" ]; then
    echo "the setup itself finished at an earlier start; removing the secrets from slot17.txt is all that is left"
else
    eval "$(python3 "$OPT/firstboot_config.py" read "$CONF")"
    args=(car --yes --no-calibrate)
    if [ -n "$S17_SERVER" ] && [ -n "$S17_CAR_KEY" ]; then
        args+=(--server "$S17_SERVER" --pi-key "$S17_CAR_KEY")
    else
        echo "note: slot17.txt has no server address and car key. Slot17 works, but the app can't see it until you"
        echo "      add them. This setup runs only once, so writing them in slot17.txt later changes nothing. Log in"
        echo "      over SSH instead and run: sudo systemctl stop tetra-detector.service"
        echo "      then: bash ~/Slot17/setup.sh car --no-calibrate --no-start"
        args+=(--no-display)
    fi
    if [ -n "$S17_HOTSPOT_NAME" ] && [ -n "$S17_HOTSPOT_PASSWORD" ]; then
        args+=(--hotspot "$S17_HOTSPOT_NAME")
    else
        args+=(--no-hotspot)
    fi
    # Bluetooth is on unless slot17.txt says no; setup skips it by itself where there is no adapter.
    case "$(printf '%s' "$S17_BLUETOOTH" | tr '[:upper:]' '[:lower:]')" in
        no|off|false|0) args+=(--no-bluetooth) ;;
        *) args+=(--bluetooth) ;;
    esac
    if [ -n "$S17_BLUETOOTH_NAME" ]; then args+=(--bluetooth-name "$S17_BLUETOOTH_NAME"); fi
    # The receiver: the one slot17.txt names, else setup finds it on USB.
    case "$(printf '%s' "$S17_RECEIVER" | tr '[:upper:]' '[:lower:]')" in
        "") ;;
        airspy|rsp1b|rsp1a|rtlsdr2) args+=(--receiver "$(printf '%s' "$S17_RECEIVER" | tr '[:upper:]' '[:lower:]')") ;;
        *) echo "note: slot17.txt names an unknown receiver ($S17_RECEIVER); setup looks for one on USB instead." ;;
    esac
    # SDRplay's driver only when the person wrote that they accept SDRplay's licence.
    case "$(printf '%s' "$S17_SDRPLAY_LICENCE" | tr '[:upper:]' '[:lower:]')" in
        accepted|accept|yes) args+=(--accept-sdrplay-licence) ;;
    esac
    # Start the detector now only if a receiver is plugged in; it starts at every power-up anyway.
    if lsusb 2>/dev/null | grep -q -i -E "ID (1d50:60a1|1df7:3050|1df7:3000|0bda:2838|0bda:2832)"; then args+=(--start); else args+=(--no-start); fi

    # The installer runs as the normal user and uses sudo for a few steps: allow that for this run only.
    printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user" > "$SUDOERS"
    chmod 440 "$SUDOERS"
    runuser -u "$user" -- env HOME="$home" SLOT17_TARBALL="$OPT/slot17.tar.gz" \
        SLOT17_HOTSPOT_PASSWORD="$S17_HOTSPOT_PASSWORD" bash "$OPT/install.sh" "${args[@]}"
    rc=$?
    rm -f "$SUDOERS"
    unset S17_CAR_KEY S17_HOTSPOT_PASSWORD

    if [ "$rc" != 0 ]; then
        echo "== the setup stopped (code $rc). Fix what it says above, then start the car computer again."
        exit 1
    fi
    mkdir -p "$(dirname "$INSTALLED")"
    touch "$INSTALLED"
fi

# Finished only when the card no longer holds the secrets: firstboot_config.py reads the file back to check.
if ! python3 "$OPT/firstboot_config.py" forget "$CONF"; then
    echo "== the key and the hotspot password are saved on the car computer, but couldn't be removed from"
    echo "   slot17.txt on this card. This is tried again at the next start; or delete those two lines yourself."
    exit 1
fi
mkdir -p "$(dirname "$DONE")"
touch "$DONE"
echo "== done. The key and the hotspot password are saved on the car computer and removed from this card."
runuser -u "$user" -- python3 "$home/Slot17/deploy/doctor.py" 2>&1 | head -40 || true
exit 0
