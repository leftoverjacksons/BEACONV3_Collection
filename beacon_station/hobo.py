"""
HOBO MX2309 (Temp/RH/Solar, LI-200R pyranometer) via its Bluetooth LE
advertisements — passive listening, no connection, no pairing.

The advertisement format is undocumented; the decoding below was derived
from captures (tests/test_hobo.py holds them) and is a WORKING HYPOTHESIS.
Every logged row keeps the raw payload (hobo_raw) so it can be re-decoded
if a field turns out to be misidentified.

Manufacturer data, company ID 0x00C5 (Onset Computer Corporation), arrives
as two alternating packet kinds (bytes after the company ID):

  Kind "A", 21 bytes — identity + temperature/RH
      0d9c9258 010b 2380 0052fa00 23 41c02000 00 424264
      [0:4]   u32 BE      serial number (228364888 in the capture)
      [13:17] float32 BE  air temperature, degC           (24.02)
      [18:21] float32 BE  RH %, last byte omitted          (48.60)
      rest: status/unknown

  Kind "B", 22 bytes — tagged float32 channels
      2ef4 | 010c 3faf499a | 0110 3c04e6e1 | 010d 3fa3bada | e1c1
      2-byte header, then (2-byte tag, float32 BE) blocks, 2-byte trailer
      tag 0x0c  solar irradiance, W/m2                     (1.37 indoors)
      tag 0x10  accumulated solar, MJ/m2 (monotonic)       (0.0081)
      tag 0x0d  UNIDENTIFIED (1.28-1.30). Not VPD: VPD from the logger's
                own T/RH would be ~1.54 kPa. Logged as hobo_ch0d.

How the packets are received matters. Kind A is the advertisement itself
(ADV_IND); kind B appears to be the scan response. Both carry company ID
0x00C5, and BlueZ keeps ONE manufacturer-data value per company per device,
batching its D-Bus updates — so through BlueZ/bleak the scan response
overwrites kind A before it is ever reported, and temp/RH go missing (seen
in the field: bluetoothctl only ever showed 22-byte packets; btmon saw both).

So the listener reads LE advertising reports straight off the HCI device
with a raw socket (as btmon does), which sees every packet. That needs
CAP_NET_RAW — the systemd unit grants it. BlueZ, driven through bleak with
DuplicateData on, still runs the scan itself. Without the capability, or if
the raw socket hears nothing, it falls back to the bleak path and says so.
"""

import asyncio
import math
import random
import socket
import struct
import threading
import time
from datetime import datetime

ONSET_COMPANY_ID = 0x00C5

TAG_FIELDS = {
    0x0C: "solar_Wm2",
    0x10: "solar_accum_MJm2",
    0x0D: "hobo_ch0d",
}
HOBO_FIELDS = ("hobo_T_C", "hobo_RH_pct", "solar_Wm2", "solar_accum_MJm2",
               "hobo_ch0d")


def _f32(b):
    v = struct.unpack(">f", b)[0]
    return None if math.isnan(v) or math.isinf(v) else v


def _in(v, lo, hi):
    return v if v is not None and lo <= v <= hi else None


def decode(payload):
    """Manufacturer-data bytes (company ID stripped) -> (kind, fields) or
    (None, {}) if the layout is not recognised."""
    b = bytes(payload)
    n = len(b)
    # Kind B: header(2) + k*6 tagged floats + trailer(2), tags start 0x01.
    if n >= 10 and (n - 4) % 6 == 0 and all(
            b[i] == 0x01 for i in range(2, n - 2, 6)):
        out = {}
        for i in range(2, n - 2, 6):
            name = TAG_FIELDS.get(b[i + 1])
            if name:
                out[name] = _f32(b[i + 2:i + 6])
        out["solar_Wm2"] = _in(out.get("solar_Wm2"), -50, 2500)
        return "B", out
    if n == 21:
        return "A", {
            "hobo_serial": struct.unpack(">I", b[0:4])[0],
            "hobo_T_C": _in(_f32(b[13:17]), -60, 90),
            "hobo_RH_pct": _in(_f32(b[18:21] + b"\x00"), 0, 105),
        }
    return None, {}


# ------------------------------------------------------------------ raw HCI
HCI_EVENT_PKT = 0x04
EVT_LE_META = 0x3E
LE_ADV_REPORT = 0x02
LE_EXT_ADV_REPORT = 0x0D
CAP_NET_RAW = 13


def mfr_data(ad, company=ONSET_COMPANY_ID):
    """Manufacturer-specific data for `company` from raw advertising data
    (a sequence of length/type/value structures), company ID stripped."""
    i = 0
    while i < len(ad):
        ln = ad[i]
        if ln == 0 or i + 1 + ln > len(ad):
            break
        if ad[i + 1] == 0xFF and ln >= 3 and \
                int.from_bytes(ad[i + 2:i + 4], "little") == company:
            return bytes(ad[i + 4:i + 1 + ln])
        i += 1 + ln
    return None


def _bdaddr(b):
    return ":".join(f"{x:02X}" for x in reversed(b))


def parse_hci_event(pkt):
    """Raw HCI event packet (leading 0x04 packet-type byte included) ->
    [(address, onset_payload)] from LE (extended) advertising reports.
    Reports are read sequentially, as the Linux kernel does."""
    out = []
    if len(pkt) < 5 or pkt[0] != HCI_EVENT_PKT or pkt[1] != EVT_LE_META:
        return out
    sub, body = pkt[3], pkt[4:]
    try:
        i = 1
        for _ in range(body[0]):
            if sub == LE_ADV_REPORT:
                # type(1) addr_type(1) addr(6) len(1) data rssi(1)
                addr, n = body[i + 2:i + 8], body[i + 8]
                data = body[i + 9:i + 9 + n]
                i += 10 + n
            elif sub == LE_EXT_ADV_REPORT:
                # type(2) addr_type(1) addr(6) phy(2) sid tx rssi
                # interval(2) direct_type direct_addr(6) len(1) data
                addr, n = body[i + 3:i + 9], body[i + 23]
                data = body[i + 24:i + 24 + n]
                i += 24 + n
            else:
                return out
            if len(addr) < 6 or len(data) < n:
                break
            payload = mfr_data(data)
            if payload:
                out.append((_bdaddr(addr), payload))
    except IndexError:
        pass
    return out


def has_cap_net_raw():
    """Without CAP_NET_RAW a raw HCI socket still opens, but the kernel
    silently filters out LE events — so check up front."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    return bool(int(line.split()[1], 16) >> CAP_NET_RAW & 1)
    except (OSError, ValueError):
        pass
    return False


def open_hci_raw(dev):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    s.bind((dev,))
    # struct hci_filter: type_mask, event_mask[2], opcode
    s.setsockopt(socket.SOL_HCI, socket.HCI_FILTER, struct.pack(
        "<IIIH", 1 << HCI_EVENT_PKT, 0, 1 << (EVT_LE_META - 32), 0))
    s.settimeout(1.0)
    return s


# ------------------------------------------------------------------ encoding
# Only used by the simulator and tests: builds payloads in the same layout.
def encode_a(serial, t_c, rh):
    rh_b = struct.pack(">f", rh)[:3]
    return (struct.pack(">I", serial) + bytes.fromhex("010b23800052fa0023")
            + struct.pack(">f", t_c) + b"\x00" + rh_b)


def encode_b(solar, accum, ch0d):
    body = b"".join(bytes([0x01, tag]) + struct.pack(">f", v) for tag, v in
                    ((0x0C, solar), (0x10, accum), (0x0D, ch0d)))
    return bytes([random.randrange(256), random.randrange(256)]) + body + \
        bytes([random.randrange(256), random.randrange(256)])


class HoboReader(threading.Thread):
    """Listens for MX2309 advertisements and emits ('sample', dict) onto
    `out_q`, in the same way as serial_io.InstrumentReader.

    A row is emitted when a packet kind's payload changes, or at least every
    `heartbeat_s` while the logger is still being heard — so steady values
    (e.g. zero irradiance at night) still produce a regular record."""

    RETRY_S = 10.0

    def __init__(self, cfg, out_q, simulate=False):
        super().__init__(name="reader-hobo", daemon=True)
        self.inst = "hobo"
        self.q = out_q
        self.want_addr = (cfg.get("address") or "").strip().upper()
        self.heartbeat_s = float(cfg.get("heartbeat_s", 60))
        self.hci_dev = int(cfg.get("hci_dev", 0))
        self.simulate = simulate
        self._raw_on = False     # raw HCI path active (else bleak callbacks)
        self._raw_heard = None   # monotonic time of last Onset packet via HCI
        self._dbus_heard = None  # ... and via bleak
        self._raw_started = 0.0
        self._lock = threading.Lock()   # on_advert runs on two threads
        self._halt = threading.Event()
        self._last = {}          # kind -> (payload, monotonic time emitted)
        port = "simulated" if simulate else (
            f"BLE {self.want_addr}" if self.want_addr else "BLE (any Onset)")
        self.state = {"state": "starting", "port": port, "detail": "",
                      "last_seen": None, "serial": None, "address": None}

    # ------------------------------------------------------------ plumbing
    def _set(self, **kw):
        self.state = {**self.state, **kw}

    def _note(self, text):
        self.q.put(("note", self.inst, text))

    def on_advert(self, address, payload):
        """Called for every advertisement heard (any thread)."""
        address = address.upper()
        if self.want_addr and address != self.want_addr:
            return
        now = time.monotonic()
        self._set(last_seen=time.time(), address=address)
        kind, fields = decode(payload)
        if kind is None:
            return
        if "hobo_serial" in fields:
            self._set(serial=fields.pop("hobo_serial"))
        with self._lock:
            prev = self._last.get(kind)
            if prev and prev[0] == bytes(payload) and \
                    now - prev[1] < self.heartbeat_s:
                return
            self._last[kind] = (bytes(payload), now)
        self.q.put(("sample", {
            "source": "hobo",
            "wall": datetime.now().astimezone(),
            "hobo_addr": address,
            "hobo_raw": bytes(payload).hex(),
            **fields,
        }))

    def stop(self):
        self._halt.set()

    # ------------------------------------------------------------ run
    def run(self):
        if self.simulate:
            return self._run_sim()
        try:
            from bleak import BleakScanner   # apt: python3-bleak
        except ImportError:
            self._set(state="error", detail="python3-bleak not installed "
                      "(sudo apt install python3-bleak)")
            self._note(self.state["detail"])
            return

        if has_cap_net_raw():
            threading.Thread(target=self._raw_loop, name="hobo-hci",
                             daemon=True).start()
        else:
            self._note("no CAP_NET_RAW: reading via BlueZ only — HOBO "
                       "temp/RH packets will mostly be missed")

        def cb(device, adv):
            data = adv.manufacturer_data.get(ONSET_COMPANY_ID)
            if not data:
                return
            self._dbus_heard = time.monotonic()
            if not self._raw_on:
                self.on_advert(device.address, data)

        async def scan_forever():
            # DuplicateData on: report repeats, so the raw reader sees the
            # logger's broadcasts continuously rather than once.
            async with BleakScanner(detection_callback=cb, bluez={
                    "filters": {"DuplicateData": True}}):
                self._set(state="connected", detail=self._mode())
                self._note(f"Bluetooth scan started ({self._mode()})")
                while not self._halt.is_set():
                    await asyncio.sleep(0.5)
                    self._watchdog()
                    self._set(detail=self._mode())

        while not self._halt.is_set():
            try:
                asyncio.run(scan_forever())
            except Exception as exc:  # adapter off, bluetoothd restarting…
                msg = f"scan failed: {exc}"
                if self.state["detail"] != msg:
                    self._note(msg)
                self._set(state="waiting", detail=msg)
                self._halt.wait(self.RETRY_S)
        self._set(state="stopped")

    def _mode(self):
        return "raw HCI" if self._raw_on else "BlueZ only — temp/RH may be missed"

    def _watchdog(self):
        """If BlueZ hears the logger but the raw socket has been silent for
        30 s, the raw path is not working here: fall back to bleak."""
        now = time.monotonic()
        if (self._raw_on and self._dbus_heard and now - self._dbus_heard < 10
                and (self._raw_heard is None or now - self._raw_heard > 30)
                and now - self._raw_started > 30):
            self._raw_on = False
            self._note("raw HCI socket hears nothing while BlueZ does — "
                       "falling back to BlueZ (temp/RH may be missed)")

    def _raw_loop(self):
        while not self._halt.is_set():
            try:
                sock = open_hci_raw(self.hci_dev)
            except OSError as exc:
                self._raw_on = False
                self._note(f"raw HCI socket unavailable: {exc}")
                self._halt.wait(self.RETRY_S)
                continue
            self._raw_started = time.monotonic()
            self._raw_on = True
            try:
                while not self._halt.is_set():
                    try:
                        pkt = sock.recv(512)
                    except socket.timeout:
                        continue
                    for addr, payload in parse_hci_event(pkt):
                        self._raw_heard = time.monotonic()
                        self.on_advert(addr, payload)
            except OSError as exc:       # adapter reset / removed
                self._raw_on = False
                self._note(f"raw HCI read failed: {exc}")
            finally:
                sock.close()
            self._halt.wait(self.RETRY_S)

    def _run_sim(self):
        """Synthetic MX2309: a daylight-shaped irradiance curve with cloud
        noise, integrated into the accumulated channel."""
        self._set(state="connected", detail="simulated")
        accum = 0.0
        t0 = time.monotonic()
        last = t0
        kind = 0
        while not self._halt.wait(2.0):
            now = time.monotonic()
            phase = (now - t0) / 900.0
            solar = max(0.0, 650 * math.sin(phase) ** 2
                        * (0.7 + 0.3 * random.random()))
            accum += solar * (now - last) / 1e6
            last = now
            if kind % 2:
                self.on_advert("F8:27:3E:19:81:A5",
                               encode_b(solar, accum, 1.28))
            else:
                self.on_advert("F8:27:3E:19:81:A5", encode_a(
                    228364888, 24 + 2 * math.sin(phase) + random.gauss(0, .05),
                    48 - 4 * math.sin(phase)))
            kind += 1
        self._set(state="stopped")
