#!/usr/bin/env bash
# Wi-Fi watchdog — run as root every 2 min by beacon-net-watchdog.timer.
#
# NetworkManager stops retrying a saved network after a few failures, and
# the Pi's Wi-Fi can drop and not come back. If wlan0 isn't connected (or
# is "connected" but can reach neither the gateway nor the internet), this
# escalates, one step per failed check:
#   checks 1-2  nmcli device connect  (NM picks the best saved network)
#   checks 3-5  Wi-Fi radio off/on
#   check  6    restart NetworkManager, then back to the radio step
# It never reboots: logging carries on offline regardless.
# Messages go to the journal:  journalctl -u beacon-net-watchdog
set -u
IFACE="${1:-wlan0}"
STATE="/run/beacon-net-watchdog.fails"

command -v nmcli >/dev/null || exit 0
[ -e "/sys/class/net/$IFACE" ] || exit 0          # no Wi-Fi hardware

ok=0
if nmcli -t -f DEVICE,STATE device 2>/dev/null | grep -q "^$IFACE:connected"; then
  gw="$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')"
  if [ -z "$gw" ] \
     || ping -c1 -W3 -I "$IFACE" "$gw" >/dev/null 2>&1 \
     || ping -c1 -W3 -I "$IFACE" 1.1.1.1 >/dev/null 2>&1; then
    ok=1
  fi
fi

fails="$(cat "$STATE" 2>/dev/null || echo 0)"
if [ "$ok" -eq 1 ]; then
  [ "$fails" -gt 0 ] && echo "Wi-Fi OK again after $fails failed check(s)"
  echo 0 >"$STATE"
  exit 0
fi

fails=$((fails + 1))
echo "$fails" >"$STATE"
if [ "$fails" -le 2 ]; then
  echo "Wi-Fi down (check $fails): reconnecting $IFACE"
  nmcli device connect "$IFACE" || true
elif [ "$fails" -le 5 ]; then
  echo "Wi-Fi down (check $fails): restarting the Wi-Fi radio"
  nmcli radio wifi off; sleep 5; nmcli radio wifi on
else
  echo "Wi-Fi down (check $fails): restarting NetworkManager"
  systemctl restart NetworkManager
  echo 2 >"$STATE"                                 # resume at the radio step
fi
