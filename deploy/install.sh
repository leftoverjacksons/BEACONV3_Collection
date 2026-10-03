#!/usr/bin/env bash
# One-time setup on a Raspberry Pi (Raspberry Pi OS Bookworm/Trixie, desktop
# image). Safe to re-run; deploy/update.sh re-runs it after every pull.
#
#   ./deploy/install.sh              full install
#   ./deploy/install.sh --quick      skip apt unless a required package is
#                                    missing (used by update.sh; works offline)
#   ./deploy/install.sh --no-kiosk   headless: no touchscreen browser
#
# Run as the normal desktop user (not with sudo); it calls sudo itself.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
QUICK=0
KIOSK=1
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --no-kiosk) KIOSK=0 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

if [ "$(id -u)" -eq 0 ]; then
  echo "Run as your normal user, not root/sudo (it uses sudo where needed)."
  exit 1
fi
USER_NAME="$(id -un)"
cd "$REPO"

step() { printf '\n== %s\n' "$*"; }

# python3-bleak + bluez: HOBO MX2309 over Bluetooth LE (beacon_station/hobo.py)
PKGS="python3-serial python3-bleak bluez git curl"
MISSING=""
for p in $PKGS; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING="$MISSING $p"
done
if [ "$QUICK" -eq 0 ] || [ -n "$MISSING" ]; then
  step "packages${MISSING:+ (missing:$MISSING)}"
  sudo apt-get update -qq || echo "   (apt update failed — offline? continuing)"
  sudo apt-get install -y $PKGS || echo "   (some packages failed to install — see above)"
fi
if [ "$QUICK" -eq 0 ] && [ "$KIOSK" -eq 1 ] && ! command -v chromium-browser >/dev/null \
    && ! command -v chromium >/dev/null; then
  sudo apt-get install -y chromium || sudo apt-get install -y chromium-browser
fi

step "Bluetooth (for the HOBO)"
# Raspberry Pi OS ships with the radio soft-blocked; the unblock persists.
sudo rfkill unblock bluetooth 2>/dev/null || true
sudo systemctl enable --now bluetooth >/dev/null 2>&1 || true
if getent group bluetooth >/dev/null && ! id -nG "$USER_NAME" | grep -qw bluetooth; then
  sudo usermod -aG bluetooth "$USER_NAME"
  echo "   added $USER_NAME to the bluetooth group"
fi
echo "   radio: $(rfkill list bluetooth 2>/dev/null | grep -q 'Soft blocked: yes' && echo blocked || echo on)"

step "Wi-Fi resilience"
# NetworkManager gives up on a saved network after a few failed attempts
# (until reboot or manual reconnect), and Wi-Fi power save on the Pi's
# chip is a common cause of drops. For every saved Wi-Fi network: retry
# forever, power save off. (UUIDs, not names: names may contain ':'.)
# Takes effect at the next (re)connection, so this SSH session isn't cut.
if command -v nmcli >/dev/null; then
  nmcli -t -f UUID,TYPE connection show 2>/dev/null \
    | awk -F: '$2 == "802-11-wireless" {print $1}' \
    | while read -r uuid; do
        name="$(nmcli -g connection.id connection show "$uuid" 2>/dev/null)"
        if sudo nmcli connection modify "$uuid" connection.autoconnect yes \
             connection.autoconnect-retries 0 802-11-wireless.powersave 2; then
          echo "   $name: auto-reconnect forever, power save off"
        fi
      done
else
  echo "   nmcli not found — skipped"
fi

step "serial port permission (dialout group)"
if id -nG "$USER_NAME" | grep -qw dialout; then
  echo "   $USER_NAME already in dialout"
else
  sudo usermod -aG dialout "$USER_NAME"
  echo "   added $USER_NAME to dialout (takes effect at next login; the service has it already)"
fi

step "config"
if [ -f config.toml ]; then
  echo "   config.toml exists — left unchanged"
else
  cp config.example.toml config.toml
  echo "   created config.toml from config.example.toml — set the serial ports in it"
fi

step "self-test"
if python3 -m unittest discover -s tests -t . >/tmp/beacon-selftest.log 2>&1; then
  echo "   tests pass"
else
  echo "   TESTS FAILED — see /tmp/beacon-selftest.log (installing anyway)"
fi

step "systemd services"
# Only name groups that exist: systemd refuses to start a unit otherwise.
GROUPS_SVC="dialout"
getent group bluetooth >/dev/null && GROUPS_SVC="$GROUPS_SVC bluetooth"
for unit in beacon-logger beacon-web; do
  sed -e "s|@USER@|$USER_NAME|g" -e "s|@REPO@|$REPO|g" \
      -e "s|@GROUPS@|$GROUPS_SVC|g" "deploy/$unit.service" \
    | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
# Wi-Fi watchdog: checks every 2 min, reconnects if Wi-Fi has dropped.
for unit in beacon-net-watchdog.service beacon-net-watchdog.timer; do
  sed -e "s|@REPO@|$REPO|g" "deploy/$unit" \
    | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
chmod +x deploy/net-watchdog.sh
sudo systemctl daemon-reload
sudo systemctl enable beacon-logger beacon-web >/dev/null
sudo systemctl enable --now beacon-net-watchdog.timer >/dev/null
sudo systemctl restart beacon-logger beacon-web
sleep 2
systemctl --no-pager --lines=0 status beacon-logger beacon-web | grep -E '●|Active:' || true

AUTOSTART="$HOME/.config/autostart/beacon-kiosk.desktop"
if [ "$KIOSK" -eq 1 ]; then
  step "touchscreen kiosk (starts at desktop login)"
  chmod +x deploy/kiosk.sh deploy/kiosk-exit.sh
  mkdir -p "$(dirname "$AUTOSTART")"
  cat >"$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=BEACON Station kiosk
Exec=$REPO/deploy/kiosk.sh
X-GNOME-Autostart-enabled=true
EOF
  echo "   $AUTOSTART"
  echo "   (desktop auto-login must be on: raspi-config > System > Auto Login)"
  # A launcher to reopen the kiosk after "Exit kiosk" (menu + desktop icon).
  for dir in "$HOME/.local/share/applications" "$HOME/Desktop"; do
    mkdir -p "$dir"
    cat >"$dir/beacon-kiosk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BEACON Kiosk
Comment=Open the BEACON dashboard full-screen
Exec=$REPO/deploy/kiosk.sh
Icon=utilities-system-monitor
Terminal=false
Categories=Utility;
EOF
    chmod +x "$dir/beacon-kiosk.desktop"
  done
  # pcmanfm asks "execute?" for desktop launchers unless told not to.
  LIBFM="$HOME/.config/libfm/libfm.conf"
  if [ -f "$LIBFM" ] && ! grep -q '^quick_exec=1' "$LIBFM"; then
    sed -i '/^\[config\]/a quick_exec=1' "$LIBFM"
  fi
  echo "   'BEACON Kiosk' launcher on the desktop and in the menu"
else
  rm -f "$AUTOSTART" "$HOME/.local/share/applications/beacon-kiosk.desktop" \
    "$HOME/Desktop/beacon-kiosk.desktop"
fi

step "done"
cat <<EOF
   Logger log : journalctl -u beacon-logger -f
   Dashboard  : http://$(hostname).local:8080/  (also on the touchscreen)
   Data       : $(python3 -c 'from beacon_station import config; print(config.load()["logging"]["data_dir"])')
   Ports      : sudo systemctl stop beacon-logger && python3 -m tools.list_ports --sniff
EOF
