#!/usr/bin/env bash
# Close the touchscreen kiosk and keep it closed (e.g. from SSH).
# Reopen: "BEACON Kiosk" desktop icon, deploy/kiosk.sh, or reboot.
touch /tmp/beacon-kiosk.stop
pkill -f beacon-kiosk-profile || true
