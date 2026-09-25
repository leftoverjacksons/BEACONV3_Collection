"""
Configuration: built-in defaults, overridden by a TOML file.

Lookup order for the file: --config argument, $BEACON_CONFIG, then
<repo>/config.toml. config.toml is git-ignored so `git pull` never touches
site-specific settings; config.example.toml documents every key.
"""

import copy
import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "beacon": {
        "enabled": True,
        # Prefer /dev/serial/by-id/... — /dev/ttyUSB*/ttyACM* numbering can
        # swap between boots. `python3 -m tools.list_ports` shows the ids.
        "port": "/dev/ttyUSB0",
        "baud": 115200,
        "stale_warn_s": 30,      # beacon bursts every ~10 s
        "stale_error_s": 120,
    },
    "anemo": {
        "enabled": True,
        "port": "/dev/ttyACM0",
        "baud": 115200,
        "stale_warn_s": 5,
        "stale_error_s": 30,
    },
    "hobo": {
        # HOBO MX2309 heard over Bluetooth LE advertisements (no pairing).
        "enabled": True,
        # "" = any Onset logger in range; set the MAC (e.g.
        # "F8:27:3E:19:81:A5") when more than one HOBO is nearby.
        "address": "",
        "heartbeat_s": 60,       # log at least this often even if unchanged
        "stale_warn_s": 60,
        "stale_error_s": 300,
    },
    "logging": {
        "data_dir": "~/beacon_data",
        "fsync_interval_s": 30,  # flush() every row, fsync() this often
    },
    "net": {
        "data_port": 47811,      # logger -> web (UDP, localhost)
        "control_port": 47812,   # web -> logger (UDP, localhost)
        "web_host": "0.0.0.0",   # 127.0.0.1 = kiosk only, no LAN viewing
        "web_port": 8080,
        # Same dashboard with all writes refused — the port to share
        # publicly (e.g. Tailscale Funnel). 0 disables it.
        "readonly_port": 8081,
    },
    "display": {
        "history_hours": 24,     # held in memory by the web server
        "event_labels": [
            "TSI moved", "HOBO moved", "beacon moved", "calibration check",
            "door opened", "rain", "maintenance", "other",
        ],
    },
}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path=None):
    cfg = copy.deepcopy(DEFAULTS)
    candidates = [path, os.environ.get("BEACON_CONFIG"),
                  REPO_ROOT / "config.toml"]
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            with open(Path(c).expanduser(), "rb") as f:
                _merge(cfg, tomllib.load(f))
            cfg["_source"] = str(Path(c).expanduser())
            break
    else:
        cfg["_source"] = "built-in defaults"
    cfg["logging"]["data_dir"] = str(
        Path(cfg["logging"]["data_dir"]).expanduser())
    return cfg
