#!/bin/sh
# Chromium in kiosk mode on the car computer's own screen, run by cage (deploy/slot17-kiosk.service).
#
# It waits for the page (p0/screen.py), so the screen never shows "site can't be reached" at power-up.
# Sound may play without a tap. Its profile lives in ~/.cache: a power cut costs nothing that matters,
# since drive mode or the cockpit is kept by p0/screen.py on the car computer. SLOT17_KIOSK_SCALE sets a
# zoom for a screen whose text comes out too small or too large (for example 1.25).
URL="http://127.0.0.1:${SLOT17_SCREEN_PORT:-5733}/"
i=0
while [ "$i" -lt 60 ]; do
    if python3 -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1] + 'healthz', timeout=1)" "$URL" 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    sleep 1
done
BROWSER="$(command -v chromium || command -v chromium-browser)"
exec "$BROWSER" --kiosk --ozone-platform=wayland --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --disable-features=Translate,MediaRouter,TouchpadOverscrollHistoryNavigation \
    --overscroll-history-navigation=0 --disable-pinch --autoplay-policy=no-user-gesture-required \
    --check-for-update-interval=31536000 --disable-component-update --disable-background-networking \
    --disable-sync --password-store=basic --force-device-scale-factor="${SLOT17_KIOSK_SCALE:-1}" \
    --user-data-dir="$HOME/.cache/slot17-kiosk" "$URL"
