#!/usr/bin/env bash
# Full-screen dashboard on the Pi touchscreen. Started at desktop login via
# ~/.config/autostart/beacon-kiosk.desktop (installed by install.sh).
# Relaunches the browser if it exits; exits if another copy is running.

URL="http://127.0.0.1:${BEACON_WEB_PORT:-8080}/"
PROFILE="$HOME/.config/beacon-kiosk-profile"

exec 9>"/tmp/beacon-kiosk.lock"
flock -n 9 || exit 0

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
  # A kill/crash leaves "exited uncleanly" in the profile, which makes
  # Chromium show a restore bubble; clear it.
  sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
    "$PROFILE/Default/Preferences" 2>/dev/null
  "$BROWSER" \
    --user-data-dir="$PROFILE" \
    --kiosk "$URL" \
    --lang=en-US \
    --noerrdialogs --disable-infobars --no-first-run \
    --disable-session-crashed-bubble --disable-features=Translate \
    --overscroll-history-navigation=0 --disable-pinch \
    --check-for-update-interval=31536000 \
    >/dev/null 2>&1
  sleep 5
done
