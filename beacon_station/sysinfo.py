"""Host health readings. Every function degrades to None off-Pi."""

import getpass
import os
import shutil
import socket
import subprocess
import time

from .config import REPO_ROOT


def _run(args, timeout=2):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def git_version():
    rev = _run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"])
    if rev is None:
        return None
    dirty = _run(["git", "-C", str(REPO_ROOT), "status", "--porcelain",
                  "--untracked-files=no"])
    return rev + ("-dirty" if dirty else "")


def clock_synced():
    """True/False from systemd-timesyncd; None if unknown."""
    v = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    return None if v is None else v == "yes"


def cpu_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def throttled():
    """Raspberry Pi get_throttled bitmask as hex string, e.g. '0x0'.
    Bit 0 under-voltage now, 1 freq capped, 2 throttled now, 3 soft temp
    limit; bits 16-19 = the same conditions have occurred since boot."""
    v = _run(["vcgencmd", "get_throttled"])
    return v.split("=", 1)[1] if v and "=" in v else None


def disk(path):
    try:
        u = shutil.disk_usage(path)
        return {"free_gb": u.free / 1e9, "total_gb": u.total / 1e9}
    except OSError:
        return None


def uptime_s():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return None


def mem():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        return {"total_mb": info["MemTotal"] / 1e6,
                "avail_mb": info["MemAvailable"] / 1e6}
    except (OSError, KeyError, ValueError):
        return None


def _ssid():
    v = _run(["iwgetid", "-r"])
    if v:
        return v
    for line in (_run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
                 or "").splitlines():
        if line.startswith("yes:"):
            return line[4:]
    return None


def network():
    """Addresses to reach this Pi by: LAN IPs, Wi-Fi SSID, Tailscale IP."""
    ips = (_run(["hostname", "-I"]) or "").split()
    ips = [ip for ip in ips if ":" not in ip and not ip.startswith("127.")]
    ts = _run(["tailscale", "ip", "-4"])
    lan = [ip for ip in ips if ip != ts and not ip.startswith("100.")]
    return {
        "lan_ips": lan,
        "ssid": _ssid(),
        "tailscale_ip": ts.splitlines()[0] if ts else None,
        "user": getpass.getuser(),
    }


def snapshot(data_dir):
    return {
        "hostname": socket.gethostname(),
        "time_utc": time.time(),
        "clock_synced": clock_synced(),
        "cpu_temp_c": cpu_temp_c(),
        "throttled": throttled(),
        "disk": disk(data_dir),
        "mem": mem(),
        "uptime_s": uptime_s(),
        "git": git_version(),
        "pid": os.getpid(),
        "net": network(),
    }
