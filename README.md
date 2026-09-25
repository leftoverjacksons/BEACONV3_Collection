# BEACON Station

Unattended field logger for comparing the **BEACON V3** environmental sensor
against reference instruments. It runs on a Raspberry Pi 4 with the 7"
touchscreen and logs:

- **BEACON V3**: the Zephyr console stream over USB serial (`env_hs_sampler`
  bursts, about every 10 s).
- **XinFeng XF502/AB ultrasonic anemometer**: through the Teensy RS-485 bridge,
  as CSV.

It writes one sparse CSV per day and shows a live dashboard on the
touchscreen. The same dashboard can be opened from any browser on the same
network.

This replaces the laptop tool, which is kept unchanged in
[`legacy/beacon_env_logger.py`](legacy/beacon_env_logger.py).

## How it is put together

```
 BEACON ──USB──┐                                   ┌── touchscreen (Chromium kiosk)
               ├─► beacon-logger ──UDP──► beacon-web ─┤
 XF502 ─Teensy─┘   (systemd)     localhost (systemd)   └── any browser on the LAN :8080
                        │                    ▲
                        ▼                    │ reads recent history at start
                ~/beacon_data/*.csv ─────────┘
```

| Process | Job | If it dies |
|---|---|---|
| `beacon-logger` | serial → parse → CSV; auto-reconnects on unplug | systemd restarts it in 5 s |
| `beacon-web` | holds 24 h in memory, serves the dashboard and JSON API | logging is unaffected |
| `kiosk.sh` | full-screen Chromium on the touchscreen | relaunches itself |

Only the logger writes the CSV. Event markers from the touchscreen are sent
to the logger and written through the same path as samples. Apart from the
browser, the only dependency is `pyserial`; everything else is the Python
standard library, plus [uPlot](https://github.com/leeoniya/uPlot), which is
bundled in `web/vendor/` so the dashboard works offline.

## Install on a fresh Pi

1. Flash **Raspberry Pi OS (64-bit, with desktop)**. In Raspberry Pi Imager,
   set a username, Wi-Fi, and enable SSH.
2. On the Pi, clone the repository and run the installer:

   ```bash
   git clone https://github.com/leftoverjacksons/BEACONV3_Collection.git ~/BEACONV3_Collection
   cd ~/BEACONV3_Collection
   ./deploy/install.sh
   ```

   This installs `python3-serial` and Chromium, adds you to `dialout`,
   creates `config.toml`, installs and starts both services, and sets the
   kiosk to launch at desktop login.
   - If the repository is private, the Pi needs read access: a read-only
     deploy key, or an HTTPS personal access token scoped to this repository.
3. Turn on desktop auto-login: `sudo raspi-config` → *System Options* →
   *Boot / Auto Login* → *Desktop Autologin*.
4. **Set the serial ports** (next section), then reboot.

### Serial ports

On Linux, `COM3`/`COM6` become `/dev/ttyUSB0`, `/dev/ttyACM0`, and so on,
and the numbering can change between boots. Use the stable
`/dev/serial/by-id/...` names instead:

```bash
sudo systemctl stop beacon-logger          # a port can only have one reader
python3 -m tools.list_ports --sniff        # lists ports and identifies which instrument is on each
nano config.toml                           # paste the by-id paths into [beacon] port / [anemo] port
sudo systemctl start beacon-logger
```

A device that has no USB serial number (some CH340 adapters) has no by-id
entry. In that case keep it on a fixed USB socket and use the
`/dev/serial/by-path/...` name.

### Real-time clock (needed before unattended use)

The Pi has no battery-backed clock. If it boots offline, the time is wrong
until it reaches the network, and that corrupts the timestamps you merge
with the TSI/HOBO data. With a DS3231 board on the I²C pins:

```bash
echo 'dtoverlay=i2c-rtc,ds3231' | sudo tee -a /boot/firmware/config.txt
sudo apt purge -y fake-hwclock           # otherwise it restores a stale time at boot
sudo reboot
# after reboot, once online (NTP sets the time):
timedatectl                              # expect "System clock synchronized: yes"
sudo hwclock -w && sudo hwclock -r       # write the correct time to the RTC and read it back
```

Then check that the clock holds: boot with the network disconnected and
compare the time against a phone. The **System** page shows whether the
clock is NTP-synchronised, and each CSV's `.meta.json` records the sync
state at the moment the file was opened.

### Leaving the kiosk

**System** page → **Exit to desktop**. Logging continues. The button only
appears on the Pi's own screen. To reopen the kiosk, double-tap the
**BEACON Kiosk** icon on the desktop or reboot. From SSH, run
`deploy/kiosk-exit.sh` to close it.

The System page also lists the Pi's Wi-Fi network, its IP address, and its
Tailscale address, with the matching `ssh` command for each.

### Screen

- **Blanking:** `raspi-config` → *Display Options* → *Screen Blanking*. A
  tap wakes the screen.
- **Direct sun:** the **System** page has a light theme that is easier to
  read in sunlight.

## Updating

When the Pi has network access:

```bash
~/BEACONV3_Collection/deploy/update.sh
```

This runs `git pull --ff-only`, re-runs the installer (skipping apt, so it
works on a slow link), restarts both services, and reloads the kiosk.
`config.toml` is git-ignored, so updates never overwrite your settings.

Each restart of the logger starts a new CSV file, so no data is mixed across
code versions. The `.meta.json` beside each CSV records the git commit that
wrote it.

## Using the touchscreen

| Page | Shows |
|---|---|
| **Overview** | Current compensated temperature, WBGT, RH, pressure, and the three raw temperatures; wind speed, direction (compass arrow shows the flow; its tail is the bearing the wind comes *from*), and 10-minute mean and gust. |
| **Trends** | Temperature, RH, wind speed, and direction over 10 min, 1 h, 6 h, or 24 h, with a °F/°C toggle. Tap a chart to read values at that time. Event markers appear as dashed lines. |
| **Wind** | Wind rose (16 sectors × speed classes), mean, gust, prevailing direction, and calm fraction. |
| **System** | Clock sync, logger state, per-port status, current file, CPU temperature, under-voltage/throttle flags, disk free, and recent messages. |

The two chips in the top bar show instrument health as green `●` live,
amber `▲` late, or red `✕` stale/unplugged/logger down. The thresholds are
in `config.toml`.

**Mark** writes a timestamped `event` row to the CSV with a label chosen
from a preset list (TSI moved, door opened, rain, and so on; editable in
`config.toml`). Use it during comparison runs so the moments of
disturbance can be found later.

## Sharing the dashboard

| Port | Who | What |
|---|---|---|
| 8080 | the touchscreen, and you on the LAN or Tailscale | the full dashboard, including **Mark** |
| 8081 | anyone you share it with | the same dashboard, view-only: no Mark, no kiosk control, no network details |

To give someone outside your network a link (they install nothing), use
[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) on the view-only
port:

```bash
sudo tailscale funnel --bg 8081     # prints https://<host>.<tailnet>.ts.net
sudo tailscale funnel reset         # stop sharing
```

Funnel must be enabled for your tailnet first: HTTPS certificates must be
on, and the tailnet policy must grant the `funnel` attribute (see below).
Anyone who has the URL can view the dashboard. Nobody can change anything
through it.

## Data format

Files are `~/beacon_data/beacon_env_log_YYYYMMDD_HHMMSS.csv`. A new file
starts when the logger starts and at each local midnight. Each file has a
`.meta.json` alongside it recording host, git commit, ports, clock-sync
state, and units.

One row per measurement event. Columns that don't belong to the row's
`source` are left blank; nothing is forward-filled. Align sources
downstream with `merge_asof` or resampling.

| Column | Notes |
|---|---|
| `iso_time` | host time, **local, no offset**. Same format as the laptop tool. |
| `source` | `beacon`, `anemo`, or `event` |
| `TMP119_C` `SHT3x_C` `HDC3022_C` `RH_pct` `P_hPa` `comp_temp_C` `WBGT_C` | beacon rows, native °C |
| `wind_ms` `wind_deg` | anemometer rows. `0.00` is the sensor's deadband (< ~0.3 m/s), not a measurement |
| `utc_time` | *new:* same instant in UTC with `+00:00`. Unambiguous across DST and time zones; prefer it for merging |
| `dev_uptime_s` | *new:* BEACON's own uptime stamp for the `Raw:` line, i.e. the device clock, free of USB latency |
| `note` | *new:* event-marker label |

The first 11 columns are identical to the laptop tool's output, so existing
readers (for example `beacon_replay_qt.py`) continue to work. New columns
are only ever appended at the end.

The CSV is flushed after every row and `fsync`ed every 30 s. After a power
cut, at most the last line of a file can be damaged. A new file is started
on restart rather than appending to the old one.

## Development without hardware

Everything runs on a laptop (Windows, macOS, or Linux) with Python ≥ 3.11:

```bash
pip install pyserial
python -m beacon_station.logger --simulate     # synthetic BEACON (replays tests/fixtures) + anemometer
python -m beacon_station.web                   # then open http://localhost:8080/
python -m unittest discover -s tests -t .      # parser / CSV / reduction tests
```

On a laptop you can also set real COM ports in `config.toml`, for example
`port = "COM3"`.

## Resource use (Pi 4)

The Python processes were measured with 6 h of history loaded: logger
≈ 14 MB RSS, display server ≈ 27 MB. The server should reach roughly
50 MB with a full 24 h at 1 Hz.

The largest cost is Chromium. For a single page, expect about
**200–350 MB** across its processes; this is an estimate that has not been
measured on this Pi. That is comfortable on a 2 GB or larger Pi 4, and
workable on 1 GB.

## Layout

```
beacon_station/   logger.py (service), web.py (service), parsers.py, csvlog.py,
                  serial_io.py (readers + simulators), sysinfo.py, config.py
web/              dashboard: index.html, app.js, style.css, vendor/uPlot
deploy/           install.sh, update.sh, kiosk.sh, kiosk-exit.sh, systemd units
tools/            list_ports.py
tests/            unit tests + fixtures/beacon_capture.txt (real console capture)
legacy/           original laptop logger (PyQt6), unchanged
```

## Planned

- **Data sync to Google Drive**: upload finished daily files (rclone)
  whenever the Pi finds a network connection, with a status line and a
  "Sync now" button on the System page.
- **Merge tooling**: TSI and HOBO import, and alignment against this CSV.
