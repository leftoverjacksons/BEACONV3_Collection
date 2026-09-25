#!/usr/bin/env bash
# Full-screen dashboard on the Pi touchscreen. Started at desktop login via
# ~/.config/autostart/beacon-kiosk.desktop, or by the "BEACON Kiosk" desktop
# icon (both installed by install.sh). Exits if another copy is running.
#
# Relaunches the browser if it crashes. The dashboard's System page has an
# "Exit kiosk" button: it creates $STOP_FLAG and closes the browser, and this
# script then exits instead of relaunching. From SSH:  deploy/kiosk-exit.sh

URL="http://127.0.0.1:${BEACON_WEB_PORT:-8080}/"
PROFILE="$HOME/.config/beacon-kiosk-profile"
STOP_FLAG="/tmp/beacon-kiosk.stop"   # must match beacon_station/web.py

exec 9>"/tmp/beacon-kiosk.lock"
flock -n 9 || exit 0
rm -f "$STOP_FLAG"

BROWSER="$(command -v chromium-browser || command -v chromium)"
if [ -z "$BROWSER" ]; then
  echo "chromium not installed" >&2
  exit 1
fi

# Wait (up to ~2 min) for beacon-web to answer.
for _ in $(seq 60); do
  curl -fs -o /dev/null "$URL" && break
  sleep 2
done

while true; do
  # --password-store=basic: with desktop auto-login the GNOME keyring is
  # never unlocked, and Chromium would otherwise prompt for a keyring
  # password on every boot.
  # A kill/crash leaves "exited uncleanly" in the profile, which makes
  # Chromium show a restore bubble; clear it.
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
    "$PROFILE/Default/Preferences" 2>/dev/null
  "$BROWSER" \
    --user-data-dir="$PROFILE" \
    --kiosk "$URL" \
    --lang=en-US \
    --password-store=basic \
    --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --disable-features=Translate \
    --overscroll-history-navigation=0 --disable-pinch \
    --check-for-update-interval=31536000 \
    >/dev/null 2>&1
  if [ -e "$STOP_FLAG" ]; then
    rm -f "$STOP_FLAG"
    exit 0
  fi
  sleep 5
done
