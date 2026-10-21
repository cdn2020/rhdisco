"""Shared helpers: Kubernetes quantity parsing, logging callbacks, small utilities."""
import math
import re
import time
from datetime import datetime, timezone

_BIN = {"Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6}
_DEC = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
_QTY = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")


def parse_quantity(q):
    """Parse a Kubernetes resource quantity ("16Gi", "3500m", "4", "1e9") into a float."""
    if q is None:
        return None
    if isinstance(q, (int, float)):
        return float(q)
    m = _QTY.match(str(q).strip())
    if not m:
        return None
    num, suf = float(m.group(1)), m.group(2)
    if suf in _BIN:
        return num * _BIN[suf]
    if suf in _DEC:
        return num * _DEC[suf]
    return None


def parse_cpu(q):
    v = parse_quantity(q)
    return round(v, 3) if v is not None else None


def to_gib(b):
    return round(b / 1024 ** 3, 2) if b else 0


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ceil_div(a, b):
    return int(math.ceil(a / b)) if a else 0


_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def short_name(h):
    h = (h or "").strip().lower()
    if _IP.match(h) or ":" in h:
        return h  # IP addresses are kept whole
    return h.split(".")[0]


class Logger:
    """Collects log lines and optionally forwards them to a callback (used for live job progress)."""

    def __init__(self, cb=None, prefix=""):
        self.cb = cb
        self.prefix = prefix
        self.lines = []

    def __call__(self, msg, level="info"):
        line = {"t": time.strftime("%H:%M:%S"), "level": level, "msg": f"{self.prefix}{msg}"}
        self.lines.append(line)
        if self.cb:
            try:
                self.cb(line)
            except Exception:
                pass

    def warn(self, msg):
        self(msg, "warn")

    def error(self, msg):
        self(msg, "error")


def classify_os(text, guest_id=""):
    """Return (family, major_version) from an OS description / vSphere guestId."""
    t = f"{text or ''} {guest_id or ''}".lower()
    fam = "Other"
    if "coreos" in t or "rhcos" in t:
        fam = "RHCOS"
    elif "red hat" in t or "rhel" in t:
        fam = "RHEL"
    elif "centos" in t:
        fam = "CentOS"
    elif "rocky" in t:
        fam = "Rocky Linux"
    elif "alma" in t:
        fam = "AlmaLinux"
    elif "oracle" in t:
        fam = "Oracle Linux"
    elif "windows" in t:
        fam = "Windows"
    elif "ubuntu" in t:
        fam = "Ubuntu"
    elif "sles" in t or "suse" in t:
        fam = "SUSE"
    elif "linux" in t or "debian" in t:
        fam = "Other Linux"
    ver = None
    m = re.search(r"(?:enterprise linux|rhel|centos|rocky linux|almalinux|oracle linux)[^0-9]{0,12}(\d+)(?:\.(\d+))?", t)
    if m:
        ver = m.group(1) + (("." + m.group(2)) if m.group(2) else "")
    else:
        m = re.search(r"rhel_?(\d+)", t)
        if m:
            ver = m.group(1)
    return fam, ver
