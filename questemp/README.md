# QUESTemp 34/36 serial toolkit

Standalone tools for the TSI QUESTemp 34/36 heat stress monitor. Nothing in
`beacon_station/` imports this package. It will be connected to the station
logger only if a live data path is found.

The only dependency is `pyserial` (`pip install pyserial`). It runs on
Windows, macOS, and Linux.

```
python -m questemp ports                 # which COM port is the cable on?
python -m questemp listen COM9           # record + auto-parse Print reports
python -m questemp probe  COM9           # poke the unit, record any reply
python -m questemp bridge COM9 COM21     # record DMS <-> unit conversation
python -m questemp parse  capture.txt    # old captures -> CSV
```

Output goes to `./questemp_captures/` by default (`--out` to change):

| File | Content |
|---|---|
| `*.log` | every byte, timestamped to the millisecond, hex + ASCII, with direction (`RX`, `TX`; bridge: `H>D` = DMS→unit, `D>H` = unit→DMS) |
| `*.rx.bin`, `*.host2dev.bin`, `*.dev2host.bin` | the raw byte streams, unmodified |
| `*.reportN.txt` | one completed Print report |
| `*.reportN.long.csv` | one row per sensor bar per logged minute, in °C |
| `*.reportN.wide.csv` | one row per minute: `s1_wbgt_in`, `s2_globe`, `wavg_wbgt_in`, … |

## What the manual does and does not say

Source: *QUESTemp 34/36 User Manual*, 056-663 Rev J/K.

- The RS-232 port is described only as a **printer/download port**. "Print"
  sends the logged sessions as a formatted ASCII report at **9600 baud,
  fixed**. The report is started and stopped from the keypad.
- Data is stored only at the **log rate**, which is 1, 2, 5, 10, 15, 30, or
  60 min. The best resolution in memory is therefore 1 min, with HH:MM
  timestamps.
- DMS can **download** sessions and push **setup** to the unit. That means
  there is an undocumented command protocol on the same port. The manual
  says nothing about it and lists no command for live readings.

No published live-output mode or command set was found. A live stream may not
exist in this firmware. The steps below test that question directly instead
of guessing.

## Plan of attack (cheapest first)

### 1. Passive: does the unit ever talk on its own?

```
python -m questemp listen COM9
```

Leave it running and go through each state on the unit: power on, View,
**Run** (asterisk shown), stop, and each menu entry. If anything appears
outside Print, that is the live path, and the log shows its exact timing.
Use `--dtr off` or `--rts off` once each in case the cable's line state
gates the output.

This mode also replaces the current manual workflow: start Print on the
unit, and when the report finishes (10 s of silence) the tool writes the
report and its CSVs.

### 2. Record what DMS says (most informative)

DMS talks to the unit, so its traffic shows the command protocol: framing,
identification, download, and set-clock. A command for live readings, if one
exists, would appear here too.

1. Install [com0com](https://sourceforge.net/projects/com0com/) (signed
   build 2.2.2.0 on 64-bit Windows). Create a pair, e.g. `COM20 <-> COM21`.
2. `python -m questemp bridge COM9 COM20`
3. In DMS, select **COM21** and do one step at a time, each in its own
   capture where practical: *connect / identify*, *download*, *send setup*.
   Look at every DMS screen for anything labelled live, real-time, or
   "instrument status".
4. Commit the `.log` files into `questemp/captures/` so they can be
   decoded.

If DMS rejects the virtual port (some programs probe for a real UART), the
fallback is a hardware tap: two USB-serial adapters receiving on the TX and
RX lines of the cable (RX only, with ground connected), each recorded with
`listen`.

### 3. Active probing (only after step 2, and only if it was inconclusive)

```
python -m questemp probe COM9                  # small conservative list
python -m questemp probe COM9 --send 3f0d      # specific bytes, in hex
python -m questemp probe COM9 --sweep          # every byte 0x00-0xFF
```

The bytes the unit acts on are not documented, so an unknown byte could
trigger an action, for example a memory reset. **Print or download the
logged data before probing.** Probing without the DMS capture is a blind
search; step 2 usually makes it unnecessary.

### Fallback if no live path exists

Set the log rate to 1 min, keep Run on, and capture a Print report at
intervals with `listen`. That gives 1-minute data with a latency of one
download. Each download is still started by hand unless step 2 shows the
command DMS uses to request a download, which the bridge log would let us
replay.

## Report parser

`printout.py` reads the column set from each table header, so tables with or
without FLOW, stay times (L/M/H/VH), HI/HX, and the two-line weighted-average
(W-AVG) header all parse. Rows carry HH:MM only. The date comes from the
session's `Start:` line and advances when the clock goes backwards. CSVs are
in °C by default (`parse --fahrenheit` keeps the printed units). A heat index
of `0` means the unit did not compute one, and it is kept as 0.

The test fixture is the sample report from the manual, retyped. The
fixed-width layout from a real unit may differ slightly (form feeds, page
headers). Please add a real capture to `tests/fixtures/` when you have one.
