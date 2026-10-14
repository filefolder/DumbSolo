"""
DumbSolo: a simplified, cross-platform SoloLite-style harvester for IGU-16HR & BD3C-5 seismic nodes

Version 0.1 ~ Oct 12 2026

    pip install PySide6 obspy
    python dumbsolo.py

Reads node volumes (device.ini / sct_par.xml / DigiSolo.LOG / MiniSEED) and:
  * reads node MiniSEED and/or the DLD format automatically
    (DLD decoding assisted from: https://github.com/TobbeTripitaka/node_toolbox)
  * harvests to SDS: decode node data, remove preamp gain, flip polarity,
    optional extra rotation of N/E, round to int32, Steim2; exact UTC day files,
    idempotent merging, parallel (process pool), optional time window
  * tracks what has been harvested (SQLite DB in the SDS root)
  * writes FDSN StationXML with responses built in code (SENSOR_TYPES)
  * archives whole node volumes to .tar.gz and loads them back
  * wipes node data files (with harvest safety checks)
  * organises everything in project folders (Project menu)

Todo (might happen, might not):
  * Add feature to program firmware (though won't be able to check if compatible)
  * Add feature to program settings (would only be able to apply a pre-made xml-
    unlikely this software will ever allow it to be made from scratch).

Hardcoded settings are in the DATA TRANSFORM block below. Making changes here
is intentionally difficult to reduce user error. 

A BIG change is now also flipping polarity of the BD3C-5's. Previously we did not do
this to the data but used the response to flip it; now it is the same as the IGU-16's.
The reason for this is so that the raw data _appears_ to be the correct polarity without
having to remove the response. This makes tools that don't remove instrument response
such as SCOLV or ML based polarity tools work as expected.
"""

from __future__ import annotations

__version__ = "0.1"

import configparser
import datetime as dt
import json
import math
import os
import re
import statistics
import struct
import sys
import traceback
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import (Qt, QThread, QTimer, Signal, QStorageInfo, QSettings,
                            QDateTime, QTimeZone)
from PySide6.QtGui import QAction, QColor, QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLineEdit, QComboBox, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QFileDialog, QPlainTextEdit, QProgressBar, QSplitter, QCheckBox,
    QSpinBox, QMessageBox, QLabel, QAbstractItemView, QToolBar, QDateTimeEdit)


# --------------------------------------------------------------------------- #
# Node metadata
# --------------------------------------------------------------------------- #

#  seisNNN[XYZ].MiniSeed / .DLD. Single-channel (1C) nodes write the file
#  without a component letter, e.g. seis000.DLD.
COMPONENT_RE = re.compile(r"^seis(\d+)([XYZ]?)\.(miniseed|dld)$", re.IGNORECASE)
#  Component assumed for files without a letter (1C nodes; vertical geophone).
SINGLE_CHANNEL_COMPONENT = "Z"


@dataclass
class ChannelFile:
    component: str          # 'X', 'Y' or 'Z' (from file name)
    index: int              # file sequence number (seis000, seis001 ...)
    path: str
    size: int
    fmt: str = "mseed"                         # 'mseed' or 'dld'
    first_time: Optional[dt.datetime] = None   # start of first record
    last_time: Optional[dt.datetime] = None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class SensorTest:
    resistance: Optional[float] = None
    resonance_hz: Optional[float] = None
    damping: Optional[float] = None
    sensitivity: Optional[float] = None
    rms_noise: Optional[float] = None


@dataclass
class Node:
    path: str
    serial: str = ""
    firmware: str = ""
    device_type: str = ""
    project: str = ""
    script_name: str = ""     # <script_file_name> used to program the node
    rotation: float = 0.0     # deg, extra rotation applied to N/E data (clockwise +); 0 = none
    sample_rate: Optional[float] = None       # from MiniSEED header (authoritative)
    sample_rate_par: Optional[float] = None   # from sct_par.xml (cross-check)
    gains_db: Dict[int, float] = field(default_factory=dict)  # Channel_n_Gain
    params: Dict[str, str] = field(default_factory=dict)      # raw sct_par.xml
    files: List[ChannelFile] = field(default_factory=list)
    start: Optional[dt.datetime] = None
    end: Optional[dt.datetime] = None
    # position: outlier-cleaned mean of the GPS fixes (see _set_position)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    elevation: Optional[float] = None
    n_fixes: int = 0                  # fixes used
    n_fixes_rejected: int = 0         # outliers removed
    n_fixes_outside: int = 0          # logged before/after recording, not used
    pos_scatter_m: Optional[float] = None       # 1-sigma horizontal scatter (used fixes)
    alt_scatter_m: Optional[float] = None
    compass_north: Optional[float] = None   # median eCompass reading
    tilt: Optional[float] = None
    sensor_tests: Dict[int, SensorTest] = field(default_factory=dict)  # last boot
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)       # informational
    dld_header: Optional[dict] = None
    max_sync_age_s: Optional[float] = None    # DLD: longest run without a GPS sync
    # GPS fixes from the log: (time, lat, lon, alt or None)
    fixes: List[tuple] = field(default_factory=list)
    # data gaps longer than MIN_GAP_S: (end of data before, start of data after)
    gaps: List[Tuple[dt.datetime, dt.datetime]] = field(default_factory=list)
    # set on deployment segments (see split_deployments)
    segment: int = 0
    n_segments: int = 1
    base: Optional["Node"] = None             # the whole node this segment came from

    @property
    def key(self) -> str:
        """Unique id of a table row / deployment."""
        return f"{self.path}#{self.segment}"

    @property
    def job_tag(self) -> str:
        return f"{self.serial}#{self.segment + 1}" if self.n_segments > 1 else self.serial

    @property
    def whole(self) -> "Node":
        return self.base or self

    @property
    def data_bytes(self) -> int:
        """Bytes of data in this node / segment (pro rata for split files)."""
        if self.base is None or self.start is None:
            return sum(f.size for f in self.files)
        tot = 0.0
        for f in self.files:
            if not (f.first_time and f.last_time):
                continue
            span = (f.last_time - f.first_time).total_seconds()
            a, b = max(f.first_time, self.start), min(f.last_time, self.end)
            ov = (b - a).total_seconds()
            tot += f.size * (1.0 if span <= 0 else max(0.0, min(1.0, ov / span)))
        return int(tot)

    @property
    def gain_db(self) -> float:
        return self.gains_db.get(1, 0.0)

    @property
    def gains_consistent(self) -> bool:
        return len(set(self.gains_db.get(n) for n in (1, 2, 3) if n in self.gains_db)) <= 1

    def default_station(self) -> str:
        """SEED station codes are max 5 chars; default to last 5 serial digits."""
        return self.serial[-5:] if self.serial else "NODE"


def find_node_dirs(root: str, max_depth: int = 2) -> List[str]:
    """Return directories under root (inclusive) that contain device.ini."""
    hits = []
    root = os.path.abspath(root)
    base_depth = root.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.count(os.sep) - base_depth >= max_depth:
            dirnames[:] = []
        lower = {f.lower() for f in filenames}
        if "device.ini" in lower:
            hits.append(dirpath)
            dirnames[:] = []   # don't descend into a node
        # skip obvious system junk on removable drives
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "$", "System Volume"))]
    return hits


def _ci_path(folder: str, name: str) -> Optional[str]:
    """Case-insensitive file lookup (FAT volumes + case-sensitive OSes)."""
    target = name.lower()
    try:
        for f in os.listdir(folder):
            if f.lower() == target:
                return os.path.join(folder, f)
    except OSError:
        pass
    return None


def read_device_ini(node: Node) -> None:
    p = _ci_path(node.path, "device.ini")
    if not p:
        node.warnings.append("device.ini missing")
        return
    cp = configparser.ConfigParser()
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        cp.read_string(fh.read())
    sec = cp["deviceInfo"] if "deviceInfo" in cp else cp[cp.sections()[0]]
    node.serial = sec.get("sn", "").strip()
    node.firmware = sec.get("firmwareVersion", "").strip()


def read_sct_par(node: Node) -> None:
    p = _ci_path(node.path, "sct_par.xml") or _ci_path(node.path, "SCT_INT.XML")
    if not p:
        node.warnings.append("sct_par.xml missing")
        return
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        txt = fh.read().strip()
    root = ET.fromstring(txt)
    node.params = {child.tag: (child.text or "").strip() for child in root}
    node.device_type = node.params.get("device_type", "")
    node.project = node.params.get("project_name", "")
    node.script_name = node.params.get("script_file_name", "")
    for n in range(1, 5):
        g = node.params.get(f"Channel_{n}_Gain")
        if g not in (None, ""):
            node.gains_db[n] = float(g)
    # Sample_Rate is the sample interval in units of 10 us (400 -> 4 ms -> 250 Hz)
    try:
        v = float(node.params.get("Sample_Rate", ""))
        node.sample_rate_par = 1e5 / v if v > 0 else None
    except ValueError:
        pass
    nch = int(node.params.get("ChNumber", "3") or 3)
    used = {node.gains_db.get(n) for n in range(1, nch + 1)}
    if len(used) > 1:
        node.warnings.append(f"channel gains differ: {node.gains_db}")


_LOG_SECTION = re.compile(r"^\[([A-Za-z]+)(\d+)\]\s*$")
_LOG_KV = re.compile(r"^\s*([^=]+?)\s*=\s*(.*?)\s*$")


def parse_log(path: str):
    """Yield (section_type, dict) for each [SectionNNNNN] block in DigiSolo.LOG."""
    sec, data = None, {}
    with open(path, "r", encoding="latin-1") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            m = _LOG_SECTION.match(line)
            if m:
                if sec:
                    yield sec, data
                sec, data = m.group(1), {}
                continue
            if sec:
                kv = _LOG_KV.match(line)
                if kv:
                    data[kv.group(1).strip()] = kv.group(2).strip().strip('"')
        if sec:
            yield sec, data


def _robust_keep(vals: List[float], floor: float, k: float) -> List[bool]:
    """Iteratively keep values within k robust sigmas (>= floor) of the median."""
    keep = [True] * len(vals)
    while True:
        used = [v for v, kp in zip(vals, keep) if kp]
        if len(used) < 3:
            return keep
        med = statistics.median(used)
        mad = statistics.median(abs(v - med) for v in used)
        tol = max(k * 1.4826 * mad, floor)
        new = [kp and abs(v - med) <= tol for v, kp in zip(vals, keep)]
        if new == keep:
            return keep
        keep = new


def _set_position(node: Node, fixes: list, span=None) -> None:
    """Position from GPS fixes (time, lat, lon, alt|None): only fixes logged
    while the node was recording (span = (start, end); all fixes if none fall
    inside it), outliers removed, then the mean."""
    node.n_fixes = node.n_fixes_rejected = node.n_fixes_outside = 0
    node.latitude = node.longitude = node.elevation = None
    node.pos_scatter_m = node.alt_scatter_m = None
    if span and span[0] and span[1]:
        inside = [f for f in fixes if f[0] and span[0] <= f[0] <= span[1]]
        if len(inside) >= min(POSITION_MIN_FIXES, len(fixes)):
            node.n_fixes_outside = len(fixes) - len(inside)
            fixes = inside
    if not fixes:
        return
    lat0 = statistics.median(f[1] for f in fixes)
    lon0 = statistics.median(f[2] for f in fixes)
    ky, kx = 110540.0, 111320.0 * math.cos(math.radians(lat0))
    north = [(f[1] - lat0) * ky for f in fixes]
    east = [(f[2] - lon0) * kx for f in fixes]
    k = POSITION_OUTLIER_SIGMA
    keep_n = _robust_keep(north, POSITION_MIN_TOL_M, k)
    keep_e = _robust_keep(east, POSITION_MIN_TOL_M, k)
    alts = [f[3] for f in fixes]
    have_alt = [a is not None for a in alts]
    keep_a = [True] * len(fixes)
    if sum(have_alt) >= 3:
        ka = _robust_keep([a for a in alts if a is not None], ALTITUDE_MIN_TOL_M, k)
        it = iter(ka)
        keep_a = [next(it) if h else True for h in have_alt]
    keep = [a and b and c for a, b, c in zip(keep_n, keep_e, keep_a)]
    used = [f for f, kp in zip(fixes, keep) if kp] or fixes
    node.n_fixes = len(used)
    node.n_fixes_rejected = len(fixes) - len(used)
    node.latitude = statistics.fmean(f[1] for f in used)
    node.longitude = statistics.fmean(f[2] for f in used)
    ualts = [f[3] for f in used if f[3] is not None]
    node.elevation = statistics.fmean(ualts) if ualts else None
    if len(used) > 1:
        node.pos_scatter_m = math.hypot(statistics.pstdev(f[1] for f in used) * ky,
                                        statistics.pstdev(f[2] for f in used) * kx)
    if len(ualts) > 1:
        node.alt_scatter_m = statistics.pstdev(ualts)


def read_log(node: Node) -> None:
    p = _ci_path(node.path, "DigiSolo.LOG")
    if not p:
        node.warnings.append("DigiSolo.LOG missing - no position")
        return
    lats, lons, alts, compass, tilts = [], [], [], [], []
    last_dev = None
    for sec, d in parse_log(p):
        if sec == "GPS" and "Latitude" in d and "Longitude" in d:
            try:
                la, lo = float(d["Latitude"]), float(d["Longitude"])
                if la == 0 and lo == 0:
                    continue
                lats.append(la); lons.append(lo)
                al = float(d["Altitude"]) if "Altitude" in d else None
                if al is not None:
                    alts.append(al)
                ts = d.get("Latest Fix UTC Time") or d.get("UTC Time")
                try:
                    t = dt.datetime.strptime(ts, "%Y/%m/%d,%H:%M:%S") if ts else None
                except ValueError:
                    t = None
                node.fixes.append((t, la, lo, al))
            except ValueError:
                pass
        if sec == "GPS":
            for k, lst in (("eCompass North", compass), ("Tilted Angle", tilts)):
                if k in d:
                    try:
                        lst.append(float(d[k]))
                    except ValueError:
                        pass
        if sec == "DeviceInfo":
            last_dev = d
    if lats:
        _set_position(node, node.fixes)
    else:
        node.warnings.append("no GPS fix in log")
    if compass:
        node.compass_north = statistics.median(compass)
    if tilts:
        node.tilt = statistics.median(tilts)
    if last_dev:
        if not node.device_type and last_dev.get("Device Type"):
            node.device_type = last_dev["Device Type"].strip()
            node.notes.append(f"device type '{node.device_type}' taken from DigiSolo.LOG")
        if not node.script_name:
            node.script_name = last_dev.get("Script Name", "")
        if not node.firmware or node.firmware.lower() == "unknown":
            node.firmware = last_dev.get("Firmware Version", node.firmware)
        for ch in (1, 2, 3, 4):
            def f(key):
                v = last_dev.get(f"Ch{ch} {key}")
                if v is None:
                    return None
                try:
                    return float(v.split(",")[0])
                except ValueError:
                    return None
            t = SensorTest(f("Resistance"), f("Resonate Freq"), f("Damping"),
                           f("Sensitivity"), f("RMS Noise"))
            if any(v is not None for v in vars(t).values()):
                node.sensor_tests[ch] = t


# --------------------------------------------------------------------------- #
# MiniSEED record handling (SEED 2.4 fixed header)
# --------------------------------------------------------------------------- #

_HDR = struct.Struct(">HHBBBxHHhh")   # btime + nsamp + srfact + srmult at 20..36


def _record_length(rec_head: bytes) -> int:
    """Find blockette 1000 and return record length (default 512)."""
    if len(rec_head) < 48:                 # empty / truncated file
        return 512
    nblk = rec_head[39]
    off = struct.unpack(">H", rec_head[46:48])[0]
    for _ in range(nblk):
        if off == 0 or off + 8 > len(rec_head):
            break
        btype, nxt = struct.unpack(">HH", rec_head[off:off + 4])
        if btype == 1000:
            return 2 ** rec_head[off + 6]
        off = nxt
    return 512


def _rec_start(rec: bytes) -> Tuple[dt.datetime, int, float]:
    yr, doy, hh, mm, ss, frac, nsamp, fact, mult = _HDR.unpack(rec[20:36])
    t = (dt.datetime(yr, 1, 1) + dt.timedelta(days=doy - 1, hours=hh, minutes=mm,
                                               seconds=ss, microseconds=frac * 100))
    if fact > 0 and mult > 0:
        sr = fact * mult
    elif fact > 0 and mult < 0:
        sr = -fact / mult
    elif fact < 0 and mult > 0:
        sr = -mult / fact
    elif fact < 0 and mult < 0:
        sr = 1.0 / (fact * mult)
    else:
        sr = 0.0
    return t, nsamp, sr


# --------------------------------------------------------------------------- #
# DLD format (vendor raw format; layout per node_toolbox docs/DLD_FORMAT.md,
# reverse-engineered, verified here against huddle tests with M8.AUANU & S1.AUANU)
#
#   0x000  512 B   header (magic 'DTCCSZ-TEC-FTS-0', serial, versions, ...)
#   then   n x [1000 samples int24 little-endian (3000 B) + 72 B time tag]
#
#   Tag: +4 int64 tick (GPS week << 32 | ms), +12 'HHMMSS.ss', +23 'YYYYMMDD',
#        +32 float64 lat, +48 float64 lon, +56 text GPS time-of-week (ms).
#        UTC = GPS week + TOW + DLD_TOW_OFFSET_S - leap seconds (see the
#        settings block for how the offset was determined). The text label
#        can lag when the receiver doesn't know the leap seconds yet, so it
#        is only used to pick the GPS week. Samples are raw ADC counts
#        including the preamp gain, raw node polarity (same as MiniSEED).
# --------------------------------------------------------------------------- #

DLD_MAGIC = b"DTCCSZ-TEC-FTS"                     # followed by a version digit
DLD_HDR, DLD_TAG = 512, 72
GPS_EPOCH = dt.datetime(1980, 1, 6)
_DLD_TAG_RE = re.compile(rb"\d{6}\.\d\d\x00\x00\d{8}\x00")     # 'HHMMSS.ss\0\0YYYYMMDD\0'
_LEAPS = [(dt.datetime(1999, 1, 1), 13), (dt.datetime(2006, 1, 1), 14),
          (dt.datetime(2009, 1, 1), 15), (dt.datetime(2012, 7, 1), 16),
          (dt.datetime(2015, 7, 1), 17), (dt.datetime(2017, 1, 1), 18)]
_layout_cache: Dict[Tuple[str, int], Tuple[int, int]] = {}


def gps_utc_leap(t: dt.datetime) -> int:
    """GPS - UTC seconds at time t. GPS_UTC_LEAP_S applies from 2017 on."""
    off = 13
    for d, v in _LEAPS:
        if t >= d:
            off = v
    return GPS_UTC_LEAP_S if t >= _LEAPS[-1][0] else off


def is_dld(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(DLD_MAGIC)) == DLD_MAGIC
    except OSError:
        return False


def dld_layout(path: str) -> Tuple[int, int]:
    """(samples per block, bytes per block+tag), found by locating the first
    time tag rather than assuming 1000 samples per block."""
    key = (path, os.path.getsize(path))
    if key not in _layout_cache:
        with open(path, "rb") as fh:
            fh.seek(DLD_HDR)
            buf = fh.read(1 << 17)
        m = _DLD_TAG_RE.search(buf)
        if m is None:
            raise ValueError("no DLD time tag found")
        block = m.start() - 12                            # label sits at tag +12
        if block <= 0 or block % 3:
            raise ValueError(f"unexpected DLD block size {block} bytes")
        _layout_cache[key] = (block // 3, block + DLD_TAG)
    return _layout_cache[key]


def dld_header(path: str) -> dict:
    with open(path, "rb") as fh:
        h = fh.read(DLD_HDR)
    txt = lambda a, n: h[a:a + n].split(b"\0")[0].decode("latin-1").strip()
    return dict(serial=txt(0x20, 16), project=txt(0x40, 32), bootloader=txt(0x70, 16),
                firmware=txt(0x80, 16), script=txt(0x140, 80),
                leap_known=struct.unpack("<i", h[0x104:0x108])[0],
                altitude=struct.unpack("<f", h[0x11C:0x120])[0],
                longitude=struct.unpack("<d", h[0x190:0x198])[0],
                latitude=struct.unpack("<d", h[0x198:0x1A0])[0])


def _dld_tag(tag: bytes):
    """(UTC, lat, lon, tick_ms, seconds since last GPS sync) of one time tag;
    UTC is None if the tag can't be read."""
    lat = struct.unpack("<d", tag[32:40])[0]
    lon = struct.unpack("<d", tag[48:56])[0]
    tick = struct.unpack("<q", tag[4:12])[0]
    sync_age = struct.unpack("<i", tag[44:48])[0] / 100.0       # 10 ms units
    tow_txt = tag[56:72].split(b"\0")[0]
    tow_ms = int(tow_txt) if tow_txt.isdigit() else (tick & 0xFFFFFFFF)
    try:
        label = dt.datetime.strptime((tag[23:31] + tag[12:18]).decode("ascii"),
                                     "%Y%m%d%H%M%S")
    except (ValueError, UnicodeDecodeError):
        label = None
    if label is not None:
        leap = gps_utc_leap(label)
        w0 = int(((label - GPS_EPOCH).total_seconds() + leap) // (7 * 86400))
        cands = [GPS_EPOCH + dt.timedelta(weeks=w, milliseconds=tow_ms)
                 + dt.timedelta(seconds=DLD_TOW_OFFSET_S - leap)
                 for w in (w0 - 1, w0, w0 + 1)]
        t = min(cands, key=lambda c: abs((c - label).total_seconds()))
    else:
        week = tick >> 32
        if not 500 < week < 10000:
            return None, lat, lon, tick, sync_age
        g = GPS_EPOCH + dt.timedelta(weeks=week, milliseconds=tow_ms)
        t = g + dt.timedelta(seconds=DLD_TOW_OFFSET_S - gps_utc_leap(g))
    return t, lat, lon, tick, sync_age


def _dld_step(times: list) -> Optional[float]:
    """Seconds per block (median of tag differences)."""
    d = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]) if a and b)
    d = [x for x in d if x > 0]
    return d[len(d) // 2] if d else None


def dld_extent(path: str, sr_hint: Optional[float] = None
               ) -> Tuple[Optional[dt.datetime], Optional[dt.datetime], float, int]:
    """Start of first block, last sample time, sample rate (reads 3 tags).
    A file with a single block has no tag spacing to measure; sr_hint (the
    rate from sct_par.xml) is used then."""
    ns, period = dld_layout(path)
    size = os.path.getsize(path)
    nblk = (size - DLD_HDR) // period
    if nblk < 1:
        return None, None, 0.0, period
    with open(path, "rb") as fh:
        def tag_at(k):
            fh.seek(DLD_HDR + k * period + ns * 3)
            return _dld_tag(fh.read(DLD_TAG))[0]
        t_first = tag_at(0)
        t_second = tag_at(1) if nblk > 1 else None
        t_last = tag_at(nblk - 1)
    step = _dld_step([t_first, t_second]) or (ns / sr_hint if sr_hint else 1.0)
    sr = ns / step
    shift = dt.timedelta(seconds=step if DLD_TAG_MARKS == "block_end" else 0)
    rem = (size - DLD_HDR) - nblk * period
    extra = min(rem, ns * 3) // 3                      # trailing samples without a tag
    t0 = t_first - shift if t_first else None
    t1 = (t_last - shift + dt.timedelta(seconds=(ns + extra - 1) / sr)) if t_last else None
    return t0, t1, sr, period


def dld_chunks(path: str, sr: float, w0=None, w1=None, blocks_per_chunk: int = 600,
               cancelled=lambda: False, progress=None, tags_only: bool = False,
               fix_every: int = 0, fixes: Optional[list] = None,
               stats: Optional[dict] = None):
    """Yield (start time, int32 samples) for each continuous run of blocks.
    Blocks are continuous when both the tag times and the tag ticks advance
    by exactly one block. w0/w1 (naive UTC) skip chunks outside a window.
    tags_only: yield (start time, n samples) without decoding samples.
    fix_every: append (time, lat, lon, None) of every n-th tag to fixes.
    stats: updated with max_sync_age_s (longest time since a GPS sync)."""
    import numpy as np
    ns, period = dld_layout(path)
    step = ns / sr
    shift = dt.timedelta(seconds=step if DLD_TAG_MARKS == "block_end" else 0)
    size = os.path.getsize(path)
    nblk = (size - DLD_HDR) // period
    k0 = 0
    last_t = None
    with open(path, "rb") as fh:
        fh.seek(DLD_HDR)
        while k0 < nblk:
            if cancelled():
                raise Cancelled()
            n = min(blocks_per_chunk, nblk - k0)
            buf = fh.read(n * period)
            if progress:
                progress(len(buf))
            n = len(buf) // period
            if n == 0:
                break
            arr = np.frombuffer(buf[:n * period], np.uint8).reshape(n, period)
            times, ticks = [], []
            for i in range(n):
                t, la, lo, tick, age = _dld_tag(arr[i, ns * 3:].tobytes())
                times.append(t - shift if t else None)
                ticks.append(tick)
                if stats is not None and age > stats.get("max_sync_age_s", 0):
                    stats["max_sync_age_s"] = age
                if fix_every and fixes is not None and t and (k0 + i) % fix_every == 0 \
                        and not (la == 0 and lo == 0):
                    fixes.append((t, la, lo, None))
            k0 += n
            if w1 is not None and times[0] and times[0] >= w1:
                break
            if w0 is not None and times[-1] and \
                    times[-1] + dt.timedelta(seconds=step) <= w0:
                last_t = times[-1]
                continue
            i = 0
            while i < n:                                # continuous runs of blocks
                if times[i] is None:
                    i += 1
                    continue
                j = i + 1
                while j < n and times[j] and \
                        abs((times[j] - times[j - 1]).total_seconds() - step) < 0.0005 and \
                        abs((ticks[j] - ticks[j - 1]) - step * 1000) <= 1:
                    j += 1
                if tags_only:
                    yield times[i], (j - i) * ns
                else:
                    raw = arr[i:j, :ns * 3].reshape(-1, 3).astype(np.int32)
                    v = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
                    yield times[i], (v ^ 0x800000) - 0x800000
                i = j
            last_t = times[-1] or last_t
        # trailing samples after the last tag (file cut short)
        rem = fh.read(ns * 3)
        if progress and rem:
            progress(len(rem))
        nrem = len(rem) // 3
        if nrem and last_t is not None:
            t = last_t + dt.timedelta(seconds=step)
            if tags_only:
                yield t, nrem
            else:
                raw = np.frombuffer(rem[:nrem * 3], np.uint8).reshape(-1, 3).astype(np.int32)
                v = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
                yield t, (v ^ 0x800000) - 0x800000


def file_extent(path: str, sr_hint: Optional[float] = None) -> Tuple[Optional[dt.datetime], Optional[dt.datetime], float, int]:
    """First start / last end time, sample rate, record length - reading only
    the first and last record (records are fixed length on these nodes)."""
    if is_dld(path):
        return dld_extent(path, sr_hint)
    size = os.path.getsize(path)
    if size < 64:
        return None, None, 0.0, 0
    with open(path, "rb") as fh:
        head = fh.read(256)
        reclen = _record_length(head)
        t0, _, sr = _rec_start(head)
        nrec = size // reclen
        fh.seek((nrec - 1) * reclen)
        last = fh.read(64)
    t1, n1, sr1 = _rec_start(last)
    t_end = t1 + dt.timedelta(seconds=(n1 - 1) / sr1 if sr1 else 0)
    return t0, t_end, sr, reclen


def scan_files(node: Node) -> None:
    found: Dict[Tuple[str, int], List[ChannelFile]] = {}
    unknown = []
    for f in os.listdir(node.path):
        p = os.path.join(node.path, f)
        m = COMPONENT_RE.match(f)
        if not m:
            if f.lower().endswith((".dld", ".miniseed", ".mseed")) and os.path.isfile(p):
                unknown.append(f)
            continue
        fmt = "dld" if m.group(3).lower() == "dld" else "mseed"
        comp = m.group(2).upper()
        if not comp:
            comp = SINGLE_CHANNEL_COMPONENT
            nch = node.params.get("ChNumber", "")
            if nch and nch.strip() not in ("1", ""):
                node.warnings.append(f"{f}: no component letter but ChNumber={nch} - "
                                     f"assumed {comp}")
            else:
                node.notes.append(f"{f}: single-channel file, assumed component {comp}")
        cf = ChannelFile(comp, int(m.group(1)), p, os.path.getsize(p), fmt)
        found.setdefault((cf.component, cf.index), []).append(cf)
    if unknown:
        node.warnings.append("data file(s) with unrecognised names, not read: " +
                             ", ".join(sorted(unknown)))
    files = []
    for key, cands in found.items():
        cands.sort(key=lambda c: c.fmt != PREFER_FORMAT)
        files.append(cands[0])
        if len(cands) > 1:
            node.notes.append(f"seis{key[1]:03d}{key[0]}: both MiniSEED and DLD present - "
                              f"using {cands[0].fmt.upper()}")
    files.sort(key=lambda c: (c.component, c.index))
    node.files = files
    if not files:
        node.warnings.append("no data files (MiniSeed or DLD)")
        return
    dlds = [f for f in files if f.fmt == "dld"]
    if dlds:
        try:                                       # fill gaps in metadata from the DLD header
            h = dld_header(dlds[0].path)
            node.dld_header = h
            if not node.serial:
                node.serial = h["serial"]
            if not node.firmware or node.firmware.lower() == "unknown":
                node.firmware = h["firmware"] or node.firmware
            if not node.script_name:
                node.script_name = h["script"]
            if not node.device_type and h.get("project") and sensor_type(h["project"]):
                node.device_type = h["project"]       # some firmware puts it here
                node.notes.append(f"device type '{node.device_type}' taken from DLD header")
        except Exception as e:  # noqa
            node.warnings.append(f"{dlds[0].name}: header unreadable ({e})")
    starts, ends, rates = [], [], set()
    for cf in files:
        try:
            t0, t1, sr, _ = file_extent(cf.path, node.sample_rate_par)
        except Exception as e:  # noqa
            node.warnings.append(f"{os.path.basename(cf.path)}: {e}")
            continue
        cf.first_time, cf.last_time = t0, t1
        if t0:
            starts.append(t0); ends.append(t1); rates.add(round(sr, 6))
    if starts:
        node.start, node.end = min(starts), max(ends)
    if rates:
        node.sample_rate = sorted(rates)[0]
        if len(rates) > 1:
            node.warnings.append(f"multiple sample rates: {rates}")
        if node.sample_rate_par and abs(node.sample_rate_par - node.sample_rate) > 1e-6:
            node.warnings.append(f"header SR {node.sample_rate} Hz != sct_par "
                                 f"{node.sample_rate_par:g} Hz")


MIN_GAP_S = 1.0       # gaps shorter than this are ignored entirely


def scan_gaps(node: Node, chunk_bytes: int = 8 << 20) -> None:
    """Find data gaps by reading every record header of one component (Z if
    present). Records are fixed length, so this is a fast sequential read."""
    comps = {f.component for f in node.files}
    comp = "Z" if "Z" in comps else (sorted(comps)[0] if comps else None)
    if comp is None:
        return
    gaps, prev_end = [], None
    tag_fixes: list = []
    sync_stats: dict = {}
    for cf in [f for f in node.files if f.component == comp]:
        if cf.fmt == "dld":
            try:
                _, _, sr, _ = dld_extent(cf.path, node.sample_rate_par)
                if not sr:
                    continue
                every = max(1, int(round(60 * sr / dld_layout(cf.path)[0])))  # ~1 fix/min
                for t0, n in dld_chunks(cf.path, sr, tags_only=True, fix_every=every,
                                        fixes=tag_fixes, blocks_per_chunk=3000,
                                        stats=sync_stats):
                    if prev_end is not None and (t0 - prev_end).total_seconds() > MIN_GAP_S:
                        gaps.append((prev_end, t0))
                    prev_end = t0 + dt.timedelta(seconds=n / sr)
            except Exception as e:  # noqa
                node.warnings.append(f"{cf.name}: {e}")
            continue
        try:
            with open(cf.path, "rb") as fh:
                reclen = _record_length(fh.read(256))
                fh.seek(0)
                step = max(reclen, chunk_bytes - chunk_bytes % reclen)
                while True:
                    buf = fh.read(step)
                    if not buf:
                        break
                    for i in range(0, len(buf) - reclen + 1, reclen):
                        if buf[i + 6:i + 7] not in (b"D", b"R", b"Q", b"M"):
                            continue
                        t0, n, sr = _rec_start(buf[i:i + 64])
                        if not sr:
                            continue
                        if prev_end is not None and \
                                (t0 - prev_end).total_seconds() > MIN_GAP_S:
                            gaps.append((prev_end, t0))
                        prev_end = t0 + dt.timedelta(seconds=n / sr)
        except OSError as e:
            node.warnings.append(f"{cf.name}: {e}")
    node.gaps = gaps
    if "max_sync_age_s" in sync_stats:
        node.max_sync_age_s = sync_stats["max_sync_age_s"]
    if tag_fixes and not node.fixes:
        # no usable DigiSolo.LOG: use the positions stored in the DLD time tags
        node.fixes = tag_fixes
        _set_position(node, tag_fixes)
        node.warnings = [w for w in node.warnings if "no GPS fix" not in w and
                         "no position" not in w]
        node.notes.append(f"position from {len(tag_fixes)} DLD time tags (no log fixes)")


def load_node(path: str, gaps: bool = True) -> Node:
    node = Node(path=os.path.abspath(path))
    read_device_ini(node)
    read_sct_par(node)
    read_log(node)
    scan_files(node)
    if not node.device_type:
        node.warnings.insert(0, "device type unknown (no sct_par.xml / log entry) - "
                             "no instrument response")
    elif sensor_type(node.device_type) is None:
        node.warnings.insert(0, f"no response for device type '{node.device_type}' - "
                             f"add it to SENSOR_TYPES")
    if gaps:
        scan_gaps(node)
    if not node.fixes:
        _dld_fixes_fallback(node)
    if node.fixes:
        _set_position(node, node.fixes, (node.start, node.end))
    return node


def _dld_fixes_fallback(node: Node) -> None:
    """No log fixes (and none picked up during the gap scan): read the
    positions in the DLD time tags of any component."""
    fixes: list = []
    for cf in [f for f in node.files if f.fmt == "dld"]:
        try:
            _, _, sr, _ = dld_extent(cf.path, node.sample_rate_par)
            every = max(1, int(round(60 * sr / dld_layout(cf.path)[0])))
            for _ in dld_chunks(cf.path, sr, tags_only=True, fix_every=every, fixes=fixes,
                                blocks_per_chunk=3000):
                pass
        except Exception:  # noqa
            continue
    if fixes:
        node.fixes = fixes
        _set_position(node, fixes)
        node.warnings = [w for w in node.warnings if "no GPS fix" not in w and
                         "no position" not in w]
        node.notes.append(f"position from {len(fixes)} DLD time tags (no log fixes)")


def split_deployments(node: Node, gap_hours: float, forced=()) -> List[Node]:
    """Split a node at data gaps longer than gap_hours. Each segment is a
    shallow copy with its own start/end, files and position (GPS fixes inside
    the segment; all fixes if none). An unsplit node is returned as is."""
    import copy
    big = sorted({g for g in node.gaps if (g[1] - g[0]).total_seconds() > gap_hours * 3600}
                 | set(forced))
    if not big or node.start is None:
        node.segment, node.n_segments, node.base = 0, 1, None
        return [node]
    last = dt.timedelta(seconds=1.0 / (node.sample_rate or 1.0))
    bounds, t = [], node.start
    for a, b in big:                 # a = end of data before the gap (exclusive)
        bounds.append((t, a - last))
        t = b
    bounds.append((t, node.end))
    segs = []
    for i, (a, b) in enumerate(bounds):
        sg = copy.copy(node)
        sg.segment, sg.n_segments, sg.base = i, len(bounds), node
        sg.start, sg.end = a, b
        sg.files = [f for f in node.files if f.first_time and f.last_time
                    and f.first_time <= b and f.last_time >= a]
        sg.warnings = list(node.warnings)
        inside = [f for f in node.fixes if f[0] and a <= f[0] <= b]
        _set_position(sg, node.fixes, (a, b))
        if not inside and node.fixes:
            sg.warnings.append("no GPS fix during this deployment - using all fixes")
        segs.append(sg)
    return segs


def effective_settings(node: Node, s: "Settings") -> "Settings":
    """Settings with the time window narrowed to a deployment segment."""
    import dataclasses
    if node.base is None or node.start is None:
        return s
    sr = node.sample_rate or 1.0
    a, b = node.start, node.end + dt.timedelta(seconds=1.0 / sr)    # end exclusive
    t0 = max(a, s.t_start) if s.t_start else a
    t1 = min(b, s.t_end) if s.t_end else b
    return dataclasses.replace(s, t_start=t0, t_end=t1)


# --------------------------------------------------------------------------- #
# Station-name schedule (CSV: serial, station, start, end)
# --------------------------------------------------------------------------- #

_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                 "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d,%H:%M:%S",
                 "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y%m%d")


def parse_time(txt: str, is_end: bool = False) -> Optional[dt.datetime]:
    """Parse a UTC time; blank -> None (open-ended). A date without a time is
    the start of that day, or for an end time the end of that day."""
    t = (txt or "").strip().rstrip("Zz").strip()
    if not t:
        return None
    for f in _TIME_FORMATS:
        try:
            return dt.datetime.strptime(t, f)
        except ValueError:
            pass
    for f in _DATE_FORMATS:
        try:
            d = dt.datetime.strptime(t, f)
            return d + dt.timedelta(days=1) if is_end else d
        except ValueError:
            pass
    raise ValueError(f"unrecognised time '{txt}'")


def load_schedule(path: str) -> Tuple[List[dict], List[str]]:
    """Read serial,station,start,end rows (header optional; comma, semicolon or
    tab separated). Returns (entries, problems)."""
    import csv
    with open(path, newline="", encoding="utf-8-sig") as fh:
        text = fh.read()
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(text.splitlines(), dialect) if any(c.strip() for c in r)]
    entries, problems = [], []
    if rows and not re.fullmatch(r"\d+", rows[0][0].strip()):
        rows = rows[1:]                                  # header line
    for i, r in enumerate(rows, 1):
        r = [c.strip() for c in r] + [""] * 4
        serial, station, a, b = r[:4]
        if not serial or not station:
            problems.append(f"line {i}: missing serial or station")
            continue
        try:
            t0, t1 = parse_time(a), parse_time(b, is_end=True)
        except ValueError as e:
            problems.append(f"line {i}: {e}")
            continue
        if t0 and t1 and t1 <= t0:
            problems.append(f"line {i}: end before start")
            continue
        entries.append(dict(serial=serial, station=station.upper()[:5],
                            start=t0.isoformat() if t0 else "",
                            end=t1.isoformat() if t1 else ""))
    return entries, problems


def _entry_times(e: dict):
    return (dt.datetime.fromisoformat(e["start"]) if e.get("start") else None,
            dt.datetime.fromisoformat(e["end"]) if e.get("end") else None)


def schedule_splits(node: Node, entries: List[dict]) -> Tuple[list, List[str]]:
    """For consecutive schedule entries of this node, find the real data gap
    nearest each boundary (within SCHEDULE_SNAP_HOURS) to split at.
    Returns (gaps to force, notes)."""
    mine = sorted((e for e in entries if e["serial"] == node.serial),
                  key=lambda e: _entry_times(e)[0] or dt.datetime.min)
    forced, notes = [], []
    tol = SCHEDULE_SNAP_HOURS * 3600
    for e1, e2 in zip(mine, mine[1:]):
        end1, start2 = _entry_times(e1)[1], _entry_times(e2)[0]
        lo, hi = end1 or start2, start2 or end1
        if lo is None:
            continue
        lo, hi = min(lo, hi), max(lo, hi)
        if node.start and (hi < node.start or lo > node.end):
            continue                                     # boundary outside this node's data

        def dist(g):
            if g[1] >= lo and g[0] <= hi:
                return 0.0
            return min(abs((g[0] - hi).total_seconds()), abs((lo - g[1]).total_seconds()))
        cands = sorted(node.gaps, key=lambda g: (dist(g), -(g[1] - g[0]).total_seconds()))
        if cands and dist(cands[0]) <= tol:
            g = cands[0]
            forced.append(g)
            notes.append(f"{node.serial}: {e1['station']}->{e2['station']} boundary "
                         f"({lo:%Y-%m-%d %H:%M}) moved to data gap "
                         f"{g[0]:%Y-%m-%d %H:%M:%S} - {g[1]:%Y-%m-%d %H:%M:%S}")
        else:
            notes.append(f"{node.serial}: no data gap within {SCHEDULE_SNAP_HOURS:g} h of the "
                         f"{e1['station']}->{e2['station']} boundary ({lo:%Y-%m-%d %H:%M}) "
                         f"- not split")
    return forced, notes


def schedule_station(node: Node, entries: List[dict]) -> Optional[dict]:
    """The schedule entry for this node that overlaps its deployment most."""
    best, best_ov = None, 0.0
    a, b = node.start, node.end
    if a is None:
        return None
    for e in entries:
        if e["serial"] != node.serial:
            continue
        s0, s1 = _entry_times(e)
        lo, hi = max(a, s0) if s0 else a, min(b, s1) if s1 else b
        ov = (hi - lo).total_seconds()
        if ov > best_ov or (best is None and ov >= 0 and a == b):
            best, best_ov = e, ov
    return best


# --------------------------------------------------------------------------- #
# Channel naming (FDSN/SEED)
# --------------------------------------------------------------------------- #

def band_code(sr: float, corner_period_s: float) -> str:
    """FDSN band code from the sample rate (Hz) and the sensor's lower-bound
    (corner) period in seconds, following the FDSN source-identifier table
    https://docs.fdsn.org/projects/source-identifiers/en/latest/channel-codes.html

        sample rate        corner < 10 s          corner >= 10 s
        >= 5000            J                      J
        1000 - <5000       G                      F
        250 - <1000        D                      C
        80 - <250          E (extremely short p.) H (high broadband)
        10 - <80           S (short period)       B (broadband)
        >1 - <10           M (mid period)
        ~1                 L (long period)
        0.1 - <1           V,  0.01 - <0.1  U,  0.001 - <0.01  W,
        1e-4 - <1e-3  R,  1e-5 - <1e-4  P,  1e-6 - <1e-5  T,  <1e-6  Q
    """
    if sr is None or sr <= 0:
        raise ValueError(f"invalid sample rate {sr!r}")
    long_period = corner_period_s >= 10.0
    if sr >= 5000:
        return "J"
    if sr >= 1000:
        return "F" if long_period else "G"
    if sr >= 250:
        return "C" if long_period else "D"
    if sr >= 80:
        return "H" if long_period else "E"
    if sr >= 10:
        return "B" if long_period else "S"
    if abs(sr - 1.0) < 1e-6:
        return "L"
    if sr > 1:
        return "M"
    for lo, code in ((0.1, "V"), (0.01, "U"), (0.001, "W"), (0.0001, "R"),
                     (0.00001, "P"), (0.000001, "T")):
        if sr >= lo:
            return code
    return "Q"


# =========================================================================== #
#  DATA TRANSFORM - SOURCE-CODE-ONLY SETTINGS
#
#  These two switches change the sample values written to the archive. They are
#  deliberately NOT exposed in the GUI or on the command line, so a whole
#  experiment can't be harvested inconsistently by an accidental click.
#
#  To change them, edit the values below and restart the program. Then
#  re-harvest into a NEW, empty SDS root. Existing day files hold data
#  processed the old way; merging new data into them would mix the two.
#  Also update the instrument response to match:
#    * REMOVE_PREAMP_GAIN = False -> the preamp gain must go into the response
#    * FLIP_POLARITY = False      -> the response needs a negative sign/gain
#
#  The values in force are recorded per source file in the harvest DB
#  (columns gain_removed, polarity_flipped).
# =========================================================================== #
REMOVE_PREAMP_GAIN = True   # divide samples by 10^(Channel_n_Gain dB / 20)
FLIP_POLARITY = True        # multiply samples by -1 (node -> western convention)

#  Output MiniSEED record length in bytes (Steim2). 4096 is standard for SDS
#  archives; 512 is also common. Must be a power of two (256 ... 8192).
MSEED_RECORD_LENGTH = 4096

#  "Wipe" deletes EVERYTHING on a node (all files and sub-folders) except the
#  files named here (case-insensitive, in the node's top folder). Folders the
#  operating system manages on the volume (ARCHIVE_SKIP) are also left alone.
WIPE_KEEP = ("device.ini",)

#  Data file formats. A node may hold MiniSEED (seisNNN?.MiniSeed) and/or the
#  vendor DLD format (seisNNN?.DLD); both are read automatically. If the same
#  file number and component exists in both, this format is used.
PREFER_FORMAT = "mseed"            # "mseed" or "dld"
#  DLD timing: GPS-UTC leap seconds from 2017-01-01 on (earlier dates use the
#  historical table in gps_utc_leap),
#  and whether a time tag marks the first sample of the block before it
#  ("block_start", the working hypothesis) or after it ("block_end").
GPS_UTC_LEAP_S = 18
DLD_TAG_MARKS = "block_start"
#  Seconds added to the tags' GPS time-of-week. The format notes treat the
#  stored TOW as that of the NEXT GPS pulse (offset -1), but a 250 Hz huddle
#  test against two independently timed sensors showed DLD data 1 s early
#  with -1 and in sync with 0. At 250 Hz a block is 4 s, so this 1 s could not
#  come from DLD_TAG_MARKS - it also supports "block_start".
DLD_TOW_OFFSET_S = 0

#  Raw node backups: whole node volume -> one .tar.gz per node, with a SHA-256
#  manifest that is checked when the archive is loaded back. MiniSEED is
#  already Steim-compressed, so gzip only gains ~15-20 % on it; level 1 gets
#  nearly all of that at a fraction of the CPU cost of level 6-9.
ARCHIVE_COMPRESSLEVEL = 1
#  Imported station-name schedules (serial,station,start,end): a boundary between
#  two consecutive entries of one node is moved to the nearest real data gap
#  within this many hours (and the node is split there).
SCHEDULE_SNAP_HOURS = 12.0

#  Station position = mean of the GPS fixes logged while the node was recording,
#  after removing outliers: a fix is rejected if it is more than
#  POSITION_OUTLIER_SIGMA robust standard deviations (1.4826 x MAD) from the
#  median, horizontally (north and east separately) or in altitude. The
#  tolerance never goes below the floors, so a tight cluster of good fixes isn't
#  trimmed for ordinary GPS jitter. Repeated until nothing more is rejected.
POSITION_OUTLIER_SIGMA = 3.0
POSITION_MIN_FIXES = 10           # fewer fixes during recording -> use all fixes
POSITION_MIN_TOL_M = 5.0          # horizontal floor (m)
ALTITUDE_MIN_TOL_M = 10.0         # vertical floor (m)

ARCHIVE_SKIP = {"System Volume Information", "$RECYCLE.BIN", ".Spotlight-V100",
                ".Trashes", ".fseventsd", ".TemporaryItems"}
# =========================================================================== #


# node file component -> (orientation code, azimuth, dip).  X = North, Y = East.
COMPONENTS = {"Z": ("Z", 0.0, -90.0), "X": ("N", 0.0, 0.0), "Y": ("E", 90.0, 0.0)}
#  Used for node types not listed in SENSOR_TYPES (a warning is logged):
DEFAULT_INSTRUMENT_CODE = "P"     # geophone / very short period seismometer
DEFAULT_CORNER_PERIOD_S = 0.2     # 5 Hz -> short-period band codes


@dataclass
class Settings:
    network: str = "XX"
    location: str = ""
    skip_harvested: bool = True
    # optional time window (naive UTC datetimes); start inclusive, end exclusive
    t_start: Optional[dt.datetime] = None
    t_end: Optional[dt.datetime] = None

    def channel_code(self, comp: str, sr: float, device_type: str = "") -> str:
        return channel_prefix(sr, device_type) + COMPONENTS[comp][0]

    def scale_factor(self, gain_db: float) -> float:
        # see REMOVE_PREAMP_GAIN / FLIP_POLARITY above (source-code only)
        f = -1.0 if FLIP_POLARITY else 1.0
        if REMOVE_PREAMP_GAIN:
            f /= 10 ** (gain_db / 20.0)
        return f


def file_window(cf: ChannelFile, s: "Settings"):
    """Part of a file's time span inside the settings window, as (start, end)
    naive-UTC datetimes, or None if it doesn't overlap. Window end is exclusive."""
    if cf.first_time is None or cf.last_time is None:
        return None
    if s.t_start and cf.last_time < s.t_start:
        return None
    if s.t_end and cf.first_time >= s.t_end:
        return None
    a = max(cf.first_time, s.t_start) if s.t_start else cf.first_time
    b = min(cf.last_time, s.t_end) if s.t_end else cf.last_time
    return (a, b)


def transform_description() -> str:
    parts = ["preamp gain removed" if REMOVE_PREAMP_GAIN else "preamp gain KEPT",
             "polarity flipped" if FLIP_POLARITY else "polarity NOT flipped",
             f"Steim2 {MSEED_RECORD_LENGTH}-byte records"]
    return "Data transform: " + ", ".join(parts) + " [set in dumbsolo.py]"


class Cancelled(Exception):
    pass


def sds_path(root, net, sta, loc, cha, year, doy) -> str:
    return os.path.join(root, f"{year}", net, sta, f"{cha}.D",
                        f"{net}.{sta}.{loc}.{cha}.D.{year}.{doy:03d}")


# --------------------------------------------------------------------------- #
# Harvest job (runs in a worker process)
# --------------------------------------------------------------------------- #

def make_jobs(node: Node, station: str, s: Settings, db: "HarvestDB" = None) -> List[dict]:
    """Jobs for one node. Z is always its own job. X and Y are separate jobs,
    unless node.rotation != 0, in which case they form one 'XY' job that reads
    both in lockstep and applies the rotation to N/E (see rotate_ne).
    Already-harvested files (for this window and rotation) are dropped if
    s.skip_harvested."""
    jobs = []
    sr = node.sample_rate or 0.0
    rot = float(node.rotation or 0.0)
    comps = sorted({f.component for f in node.files}, key="ZXY".index)
    rotate = rot != 0.0 and "X" in comps and "Y" in comps

    def todo(f):
        if not file_window(f, s):
            return False
        if not (s.skip_harvested and db is not None):
            return True
        r = rot if (rotate and f.component in "XY") else 0.0
        return not db.file_done(node.serial, f, s, r, node.whole.gaps)

    groups = [["Z"]] if "Z" in comps else []
    groups += [["X", "Y"]] if rotate else [[c] for c in comps if c != "Z"]
    for group in groups:
        files = [f for f in node.files if f.component in group and file_window(f, s)]
        if len(group) == 2:
            # keep X/Y files in pairs (same file index) so they can be aligned
            idx = {f.index for f in files if todo(f)}
            files = [f for f in files if f.index in idx]
        else:
            files = [f for f in files if todo(f)]
        if not files:
            continue
        jobs.append(dict(
            id=f"{node.job_tag}.{''.join(group)}", node_path=node.path, serial=node.serial,
            components=group, network=s.network.upper(), station=station.upper(),
            location=s.location.upper(),
            channels={c: s.channel_code(c, sr, node.device_type) for c in group},
            rotation=rot if len(group) == 2 else 0.0,
            sample_rate=sr, gain_db=node.gain_db, factor=s.scale_factor(node.gain_db),
            reclen=MSEED_RECORD_LENGTH, t_start=s.t_start, t_end=s.t_end,
            files=[(f.path, f.size, f.name, f.first_time, file_window(f, s), f.component)
                   for f in files],
            bytes=sum(f.size for f in files)))
    return jobs


def _valid_records(buf: bytes, reclen: int) -> bytes:
    """Drop blank/unwritten records (FIFO flash leaves zero-filled tails)."""
    n = len(buf) // reclen
    keep = [buf[i * reclen:(i + 1) * reclen] for i in range(n)
            if buf[i * reclen + 6:i * reclen + 7] in (b"D", b"R", b"Q", b"M")]
    return b"".join(keep)


def _flush_day(job, channel, key, pieces, sds_root) -> int:
    """Write one channel-day as Steim2. New data replaces any existing data in
    the day file over the same time span (so a re-harvest, e.g. with a new
    rotation, is authoritative); existing data outside that span is kept."""
    import numpy as np
    from obspy import Stream, Trace, read
    year, doy = key
    sr = job["sample_rate"]
    path = sds_path(sds_root, job["network"], job["station"], job["location"],
                    channel, year, doy)
    new = Stream()
    for t0, data in pieces:
        tr = Trace(data=data)
        tr.stats.update(dict(network=job["network"], station=job["station"],
                             location=job["location"], channel=channel,
                             sampling_rate=sr, starttime=t0))
        new.append(tr)
    new.merge(method=-1)
    st = Stream()
    if os.path.exists(path):
        half = 0.5 / sr
        for tr in read(path, format="MSEED"):
            keep = [tr]
            for n in new:
                nxt = []
                for k in keep:
                    if k.stats.endtime < n.stats.starttime or k.stats.starttime > n.stats.endtime:
                        nxt.append(k)
                        continue
                    if n.stats.starttime - half > k.stats.starttime:
                        nxt.append(k.slice(k.stats.starttime, n.stats.starttime - half))
                    if n.stats.endtime + half < k.stats.endtime:
                        nxt.append(k.slice(n.stats.endtime + half, k.stats.endtime))
                    nxt = [x for x in nxt if x.stats.npts]
                keep = nxt
            st.extend(keep)
    st.extend(list(new))
    for tr in st:
        tr.data = np.ascontiguousarray(tr.data, dtype=np.int32)
    st.merge(method=-1)
    st.sort()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    st.write(tmp, format="MSEED", encoding="STEIM2", reclen=job["reclen"], byteorder=">")
    os.replace(tmp, path)
    return sum(len(p[1]) for p in pieces)


def rotate_ne(n, e, deg: float):
    """Apply an additional rotation to horizontal data that is already +N / +E
    (the sensor is assumed to be oriented to north, 0/360).

    The horizontal motion vector is rotated by `deg` clockwise as seen from
    above (from N towards E); negative = counter-clockwise:
        N' = N cos(deg) - E sin(deg)
        E' = N sin(deg) + E cos(deg)
    e.g. deg = +90: northward motion ends up on +E (E' = N, N' = -E).
    """
    import numpy as np
    t = np.radians(deg)
    c, s = np.cos(t), np.sin(t)
    return n * c - e * s, n * s + e * c


def harvest_job(job: dict, sds_root: str, queue=None, cancel=None,
                chunk_records: int = 16384) -> dict:
    """Decode node MiniSEED, apply gain/polarity (and rotation for XY jobs),
    write SDS day files."""
    import io
    import numpy as np
    from obspy import read, UTCDateTime

    def emit(*msg):
        if queue is not None:
            queue.put(msg)

    sr = job["sample_rate"]
    factor = job["factor"]
    w0 = UTCDateTime(job["t_start"]) if job.get("t_start") else None
    w1 = UTCDateTime(job["t_end"]) if job.get("t_end") else None
    days: Dict[Tuple[str, int, int], list] = {}       # (channel, year, doy) -> pieces
    warnings: List[str] = []
    progress = {"bytes": 0}
    totals = {"samples": 0}
    days_written = set()

    def clip(t0, data):
        i0, i1 = 0, len(data)
        if w0 is not None and w0 > t0:
            i0 = min(i1, int(math.ceil(round((w0 - t0) * sr, 6))))
        if w1 is not None:
            i1 = max(0, min(i1, int(math.ceil(round((w1 - t0) * sr, 6)))))
        if i1 <= i0:
            return t0, data[:0]
        return t0 + i0 / sr, data[i0:i1]

    def to_int(y):
        return np.clip(np.rint(y), -2**31, 2**31 - 1).astype(np.int32)

    def add(channel, t0, data):
        """Append int32 samples, split at UTC midnights."""
        while len(data):
            nxt = UTCDateTime(t0.year, t0.month, t0.day) + 86400
            n = int(math.ceil(round((nxt - t0) * sr, 6)))
            days.setdefault((channel, t0.year, t0.julday), []).append((t0, data[:n]))
            data = data[n:]
            t0 = t0 + n / sr

    def flush(older_than=None):
        for key in sorted(days):
            if older_than is None or key[1:] < older_than:
                ch = key[0]
                totals["samples"] += _flush_day(job, ch, key[1:], days.pop(key), sds_root)
                days_written.add(key)

    def stream(comp):
        """Yield (t0, float64 samples after gain/polarity) for one component."""
        for path, size, name, _, _, c in job["files"]:
            if c != comp:
                continue
            if is_dld(path):
                _, _, fsr, _ = dld_extent(path, sr)
                if abs(fsr - sr) > 1e-6:
                    warnings.append(f"{name}: SR {fsr} != {sr}")

                def prog(b):
                    progress["bytes"] += b
                    emit("progress", job["id"], progress["bytes"])
                wa = w0.datetime if w0 is not None else None
                wb = w1.datetime if w1 is not None else None
                for t0, v in dld_chunks(path, fsr, wa, wb, progress=prog,
                                        cancelled=lambda: cancel is not None and cancel.is_set()):
                    t0, y = clip(UTCDateTime(t0), v.astype(np.float64) * factor)
                    if len(y):
                        yield t0, y
                continue
            with open(path, "rb") as fh:
                reclen = _record_length(fh.read(256))
                fh.seek(0)
                read_bytes = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    buf = fh.read(reclen * chunk_records)
                    if not buf:
                        break
                    read_bytes += len(buf)
                    progress["bytes"] += len(buf)
                    emit("progress", job["id"], progress["bytes"])
                    good = _valid_records(buf, reclen)
                    if good and (w0 is not None or w1 is not None):
                        first_t = UTCDateTime(_rec_start(good[:64])[0])
                        lt, ln, lsr = _rec_start(good[-reclen:][:64])
                        last_t = UTCDateTime(lt) + (ln / lsr if lsr else 0)
                        if w1 is not None and first_t >= w1:
                            progress["bytes"] += size - read_bytes
                            emit("progress", job["id"], progress["bytes"])
                            break
                        if w0 is not None and last_t <= w0:
                            continue
                    if not good:
                        continue
                    for tr in read(io.BytesIO(good), format="MSEED"):
                        if abs(tr.stats.sampling_rate - sr) > 1e-6:
                            warnings.append(f"{name}: SR {tr.stats.sampling_rate} != {sr}")
                        t0, y = clip(tr.stats.starttime, tr.data.astype(np.float64) * factor)
                        if len(y):
                            yield t0, y

    # ---------------- single component ----------------
    if len(job["components"]) == 1:
        comp = job["components"][0]
        ch = job["channels"][comp]
        for t0, y in stream(comp):
            add(ch, t0, to_int(y))
            end = t0 + (len(y) - 1) / sr
            flush(older_than=(end.year, end.julday))
        flush()
        return dict(id=job["id"], samples=totals["samples"], days=len(days_written),
                    warnings=warnings)

    # ---------------- X+Y rotated (lockstep) ----------------
    rot = job["rotation"]
    # which node component is +N and which is +E (from COMPONENTS)
    compN = next(c for c in ("X", "Y") if COMPONENTS[c][0] == "N")
    compE = next(c for c in ("X", "Y") if COMPONENTS[c][0] == "E")
    chN, chE = job["channels"][compN], job["channels"][compE]
    gens = {"X": stream("X"), "Y": stream("Y")}
    segs = {"X": [], "Y": []}          # pending [t0, float array] per component
    reached = {"X": None, "Y": None}   # end time of last data read
    done = {"X": False, "Y": False}
    dropped = {"X": 0, "Y": 0}
    misaligned = [False]

    def push(comp, t0, y):
        sg = segs[comp]
        if sg:
            lt0, ly = sg[-1]
            if abs((lt0 + len(ly) / sr) - t0) < 0.5 / sr:      # contiguous
                sg[-1][1] = np.concatenate([ly, y])
                return
        sg.append([t0, y])

    def drop_head(comp, n):
        t0, y = segs["X" if comp == "X" else "Y"][0]
        n = min(n, len(y))
        dropped[comp] += n
        if n == len(y):
            segs[comp].pop(0)
        else:
            segs[comp][0] = [t0 + n / sr, y[n:]]

    def align():
        while segs["X"] and segs["Y"]:
            (tx, x), (ty, y) = segs["X"][0], segs["Y"][0]
            off = (ty - tx) * sr
            if abs(off - round(off)) > 0.01 and not misaligned[0]:
                misaligned[0] = True
                warnings.append(f"X/Y samples not aligned (offset {off:.3f} samples); "
                                "pairing to nearest sample")
            off = int(round(off))
            if off > 0:              # X starts earlier: X head has no Y partner
                drop_head("X", off); continue
            if off < 0:
                drop_head("Y", -off); continue
            n = min(len(x), len(y))
            arr = {"X": x[:n], "Y": y[:n]}
            nn, ee = rotate_ne(arr[compN], arr[compE], rot)
            add(chN, tx, to_int(nn))
            add(chE, tx, to_int(ee))
            for comp, (t0, a) in (("X", (tx, x)), ("Y", (ty, y))):
                if n == len(a):
                    segs[comp].pop(0)
                else:
                    segs[comp][0] = [t0 + n / sr, a[n:]]

    while not (done["X"] and done["Y"]):
        # read from whichever component is behind
        cands = [c for c in "XY" if not done[c]]
        comp = min(cands, key=lambda c: reached[c].timestamp if reached[c] else -1e18)
        try:
            t0, y = next(gens[comp])
        except StopIteration:
            done[comp] = True
            continue
        push(comp, t0, y)
        reached[comp] = t0 + len(y) / sr
        align()
        ends = [reached[c] for c in "XY" if reached[c] is not None]
        if len(ends) == 2:
            m = min(ends)
            flush(older_than=(m.year, m.julday))
    for comp in "XY":
        dropped[comp] += sum(len(a) for _, a in segs[comp])
    flush()
    for comp in "XY":
        if dropped[comp]:
            warnings.append(f"{dropped[comp]} {comp} samples had no "
                            f"{'Y' if comp == 'X' else 'X'} partner and were not written")
    return dict(id=job["id"], samples=totals["samples"], days=len(days_written),
                warnings=warnings)


def _worker(job, sds_root, queue, cancel):
    try:
        return harvest_job(job, sds_root, queue, cancel)
    except Cancelled:
        return dict(id=job["id"], cancelled=True)
    except Exception as e:  # noqa
        import traceback
        return dict(id=job["id"], error=f"{e}\n{traceback.format_exc()}")


def _worker_group(jobs, sds_root, queue, cancel):
    """Run jobs that write the same SDS channel files one after another."""
    out = []
    for j in jobs:
        if cancel is not None and cancel.is_set():
            out.append(dict(id=j["id"], cancelled=True))
            continue
        out.append(_worker(j, sds_root, queue, cancel))
    return out


def group_conflicting(jobs: List[dict]) -> List[List[dict]]:
    """Jobs that write the same N.S.L.C (e.g. two deployments of one node with
    the same station code) end up in one group, so they never write the same
    day file at the same time. Order within a group is kept."""
    parent = list(range(len(jobs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    owner: Dict[tuple, int] = {}
    for i, j in enumerate(jobs):
        for ch in j["channels"].values():
            k = (j["network"], j["station"], j["location"], ch)
            if k in owner:
                parent[find(i)] = find(owner[k])
            else:
                owner[k] = i
    groups: Dict[int, List[dict]] = {}
    for i, j in enumerate(jobs):
        groups.setdefault(find(i), []).append(j)
    return list(groups.values())


def run_jobs(jobs: List[dict], sds_root: str, workers: int = 4,
             on_event: Callable[..., None] = lambda *a: None, cancel=None) -> List[dict]:
    """Run jobs in a process pool. on_event('progress', id, bytes) and
    on_event('result', result_dict) are called from the calling thread."""
    import multiprocessing as mp
    import queue as _q
    from concurrent.futures import ProcessPoolExecutor
    ctx = mp.get_context("spawn")          # same behaviour on all platforms
    results = []
    with ctx.Manager() as mgr:
        q = mgr.Queue()
        ev = mgr.Event()
        with ProcessPoolExecutor(max_workers=max(1, workers), mp_context=ctx) as ex:
            futs = [ex.submit(_worker_group, g, sds_root, q, ev)
                    for g in group_conflicting(jobs)]
            pending = set(futs)
            while pending:
                if cancel is not None and cancel() and not ev.is_set():
                    ev.set()
                try:
                    while True:
                        on_event(*q.get(timeout=0.2))
                except _q.Empty:
                    pass
                for f in [f for f in pending if f.done()]:
                    pending.discard(f)
                    for r in f.result():
                        results.append(r)
                        on_event("result", r)
            try:
                while True:
                    on_event(*q.get_nowait())
            except _q.Empty:
                pass
    return results


# --------------------------------------------------------------------------- #
# Harvest database (lives in the SDS root, written only by the main process)
# --------------------------------------------------------------------------- #

def dld_time_cal() -> str:
    """Identifies the DLD timing settings data was harvested with."""
    return f"tow{DLD_TOW_OFFSET_S:+g}/{DLD_TAG_MARKS}"


def remove_span(sds_root: str, net: str, sta: str, loc: str, cha: str,
                t0: dt.datetime, t1: dt.datetime) -> int:
    """Delete samples in [t0, t1] from SDS day files. Returns samples removed."""
    import numpy as np
    from obspy import Stream, read, UTCDateTime
    removed = 0
    day = dt.datetime(t0.year, t0.month, t0.day)
    a, b = UTCDateTime(t0), UTCDateTime(t1)
    while day <= t1:
        path = sds_path(sds_root, net, sta, loc, cha, day.year, day.timetuple().tm_yday)
        day += dt.timedelta(days=1)
        if not os.path.exists(path):
            continue
        st, out = read(path, format="MSEED"), Stream()
        for tr in st:
            half = 0.5 / tr.stats.sampling_rate
            if tr.stats.endtime < a or tr.stats.starttime > b:
                out.append(tr)
                continue
            n_before = tr.stats.npts
            parts = []
            if a - half > tr.stats.starttime:
                parts.append(tr.slice(tr.stats.starttime, a - half))
            if b + half < tr.stats.endtime:
                parts.append(tr.slice(b + half, tr.stats.endtime))
            parts = [x for x in parts if x.stats.npts]
            removed += n_before - sum(x.stats.npts for x in parts)
            out.extend(parts)
        if not out:
            os.remove(path)
            continue
        for tr in out:
            tr.data = np.ascontiguousarray(tr.data, dtype=np.int32)
        tmp = path + ".part"
        out.write(tmp, format="MSEED", encoding="STEIM2", reclen=MSEED_RECORD_LENGTH,
                  byteorder=">")
        os.replace(tmp, path)
    return removed


class HarvestDB:
    NAME = "dumbsolo_harvest.sqlite"

    def __init__(self, sds_root: str):
        import sqlite3
        os.makedirs(sds_root, exist_ok=True)
        self.path = os.path.join(sds_root, self.NAME)
        self.con = sqlite3.connect(self.path)
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS harvested (
            serial TEXT, fname TEXT, size INTEGER, first_time TEXT,
            win_start TEXT, win_end TEXT, rotation REAL,
            network TEXT, station TEXT, location TEXT, channel TEXT,
            gain_db REAL, polarity_flipped INTEGER, gain_removed INTEGER,
            samples INTEGER, harvested_at TEXT, time_cal TEXT,
            PRIMARY KEY (serial, fname, size, first_time, win_start, win_end, rotation));
        CREATE TABLE IF NOT EXISTS deployments (
            serial TEXT, start TEXT, "end" TEXT, network TEXT, station TEXT,
            location TEXT, latitude REAL, longitude REAL, elevation REAL,
            sample_rate REAL, gain_db REAL, device_type TEXT, components TEXT,
            n_fixes INTEGER, rotation REAL, harvested_at TEXT,
            PRIMARY KEY (serial, start));
        """)
        cols = [r[1] for r in self.con.execute("PRAGMA table_info(harvested)")]
        if "time_cal" not in cols:             # databases from before DLD timing was fixed
            self.con.execute("ALTER TABLE harvested ADD COLUMN time_cal TEXT")
        self.con.commit()

    @staticmethod
    def _t(t):
        return t.isoformat() if t else ""

    @staticmethod
    def _is_dld(name: str) -> bool:
        return name.lower().endswith(".dld")

    def stale_dld_rows(self, serial: str, name: str, size: int) -> List[dict]:
        """Rows for a DLD file harvested with different DLD timing settings
        (e.g. before the 1 s correction); their data sits at the wrong times."""
        if not self._is_dld(name):
            return []
        cur = self.con.execute(
            "SELECT rowid, * FROM harvested WHERE serial=? AND fname=? AND size=? "
            "AND (time_cal IS NULL OR time_cal != ?)", (serial, name, size, dld_time_cal()))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def drop_rows(self, rowids: List[int]):
        self.con.executemany("DELETE FROM harvested WHERE rowid=?", [(r,) for r in rowids])
        self.con.commit()

    def drop_deployments_within(self, serial: str, t0: str, t1: str):
        self.con.execute('DELETE FROM deployments WHERE serial=? AND start>=? AND "end"<=?',
                         (serial, t0, t1))
        self.con.commit()

    def file_done(self, serial: str, cf: ChannelFile, s: Optional["Settings"] = None,
                  rotation: Optional[float] = 0.0, gaps: Optional[list] = None) -> bool:
        """True if the part of cf inside the window (whole file if no window)
        is covered by previously harvested intervals with the same rotation.
        Known data gaps (node.gaps) don't need to be covered."""
        need = file_window(cf, s or Settings())
        if need is None:
            return True
        q = ("SELECT win_start, win_end FROM harvested WHERE serial=? AND fname=? AND size=? "
             "AND first_time=?")
        args = [serial, cf.name, cf.size, self._t(cf.first_time)]
        if self._is_dld(cf.name):
            q += " AND time_cal = ?"
            args.append(dld_time_cal())
        if rotation is not None:            # None = any rotation
            q += " AND abs(rotation - ?) < 1e-9"
            args.append(float(rotation))
        rows = self.con.execute(q + " ORDER BY win_start", args).fetchall()
        tol = dt.timedelta(seconds=1)

        def skip_gaps(t):
            for ga, gb in gaps or ():
                if ga - tol <= t < gb:
                    t = gb
            return t
        reach = skip_gaps(need[0])
        if reach >= need[1]:
            return True
        for a, b in rows:
            a, b = dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b)
            if a > reach + tol:
                break
            reach = skip_gaps(max(reach, b))
            if reach >= need[1]:
                return True
        return False

    def node_status(self, node: Node, s: Optional["Settings"] = None) -> str:
        s = s or Settings()
        files = [f for f in node.files if file_window(f, s)]
        if not files:
            return "no data in range" if node.files else "no data"
        rot = float(node.rotation or 0.0)
        gaps = node.whole.gaps
        n = sum(self.file_done(node.serial, f, s, rot if f.component in "XY" else 0.0, gaps)
                for f in files)
        if n == 0:
            any_rows = any(self.con.execute(
                "SELECT 1 FROM harvested WHERE serial=? AND fname=? AND size=? AND first_time=?",
                (node.serial, f.name, f.size, self._t(f.first_time))).fetchone() for f in files)
            return "partial" if any_rows else "new"
        return "new" if n == 0 else ("harvested" if n == len(files) else
                                    f"partial ({n}/{len(files)} files)")

    def fully_harvested(self, node: Node) -> bool:
        """Every data file harvested over its whole span (any rotation).
        Used to decide whether wiping a node is safe."""
        node = node.whole
        return bool(node.files) and all(
            self.file_done(node.serial, f, Settings(), rotation=None, gaps=node.gaps)
            for f in node.files)

    def record_job(self, job: dict, result: dict, s: Settings):
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        for path, size, name, first, win, comp in job["files"]:
            self.con.execute(
                "INSERT OR REPLACE INTO harvested VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["serial"], name, size, self._t(first), self._t(win[0]), self._t(win[1]),
                 float(job["rotation"]),
                 job["network"], job["station"], job["location"], job["channels"][comp],
                 job["gain_db"],
                 int(FLIP_POLARITY), int(REMOVE_PREAMP_GAIN), result.get("samples", 0), now,
                 dld_time_cal() if self._is_dld(name) else ""))
        self.con.commit()

    def record_deployment(self, node: Node, station: str, s: Settings):
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        comps = "".join(sorted({f.component for f in node.files}, key="ZXY".index))
        self.con.execute(
            "INSERT OR REPLACE INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (node.serial, self._t(node.start), self._t(node.end), s.network.upper(),
             station.upper(), s.location.upper(), node.latitude, node.longitude,
             node.elevation, node.sample_rate, node.gain_db, node.device_type, comps,
             node.n_fixes, float(node.rotation or 0.0), now))
        self.con.commit()

    def deployments(self) -> List[dict]:
        cur = self.con.execute("SELECT * FROM deployments ORDER BY network, station, start")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self):
        self.con.close()


def deployment_from_node(node: Node, station: str, s: Settings) -> dict:
    return dict(serial=node.serial, start=HarvestDB._t(node.start), end=HarvestDB._t(node.end),
                network=s.network.upper(), station=station.upper(), location=s.location.upper(),
                latitude=node.latitude, longitude=node.longitude, elevation=node.elevation,
                sample_rate=node.sample_rate, gain_db=node.gain_db,
                device_type=node.device_type,
                components="".join(sorted({f.component for f in node.files}, key="ZXY".index)),
                n_fixes=node.n_fixes, rotation=float(node.rotation or 0.0))


# --------------------------------------------------------------------------- #
# StationXML
# --------------------------------------------------------------------------- #

# =========================================================================== #
#  SENSOR TYPES AND INSTRUMENT RESPONSES
#
#  SENSOR_TYPES (below the builders) is keyed by <device_type> exactly as
#  written in the node's sct_par.xml. Each entry gives the FDSN instrument
#  code, the sensor's corner period (which selects short-period vs broadband
#  band codes) and a response builder: function(sample_rate) -> (Response,
#  sensor Equipment, datalogger Equipment). Responses must describe the data
#  as DumbSolo writes it, i.e. after the DATA TRANSFORM above (preamp gain
#  removed, polarity flipped). To add a node type, write a builder and add an
#  entry to SENSOR_TYPES.
# =========================================================================== #

def _paz_adc_response(sample_rate: float, *, zeros, poles, norm_factor: float,
                      norm_freq: float, sensor_gain: float, adc_gain: float,
                      sensitivity_freq: float):
    """Two-stage response: sensor poles/zeros (M/S -> V) + ADC (V -> COUNTS)."""
    from obspy.core.inventory.response import (
        Response, PolesZerosResponseStage, CoefficientsTypeResponseStage,
        InstrumentSensitivity)
    stages = [
        PolesZerosResponseStage(
            1, sensor_gain, norm_freq, "M/S", "V",
            input_units_description="Velocity in Meters per Second",
            pz_transfer_function_type="LAPLACE (RADIANS/SECOND)",
            normalization_frequency=norm_freq, normalization_factor=norm_factor,
            zeros=list(zeros), poles=list(poles)),
        CoefficientsTypeResponseStage(
            2, adc_gain, 0.0, "V", "COUNTS", "DIGITAL", numerator=[], denominator=[],
            decimation_input_sample_rate=sample_rate, decimation_factor=1,
            decimation_offset=0, decimation_delay=0.0, decimation_correction=0.0),
    ]
    resp = Response(
        instrument_sensitivity=InstrumentSensitivity(
            sensor_gain * adc_gain, sensitivity_freq, "M/S", "COUNTS",
            input_units_description="Velocity in Meters per Second"),
        response_stages=stages)
    resp.recalculate_overall_sensitivity(sensitivity_freq)
    return resp

def _resp_igu16_1c_5hz(sample_rate: float):
    """IGU-16 1C, 5 Hz geophone, 76.6 V/(m/s), ADC 3355500 counts/V.
    Values as for the IGU-16HR 3C 5Hz; the 1C reports device_type
    'IGU-16 1C 5Hz' (no 'HR')."""
    from obspy.core.inventory import Equipment
    resp = _paz_adc_response(
        sample_rate,
        zeros=[0j, 0j],
        poles=[complex(-22.211059, 22.217768), complex(-22.211059, -22.217768)],
        norm_factor=1.0, norm_freq=50.0, sensor_gain=76.6,
        adc_gain=3355500.0, sensitivity_freq=50.0)
    sensor = Equipment(type="Geophone", description="IGU-16 1C 5Hz node",
                       model="IGU-16 1C 5 Hz geophone, 76.6 V/m/s")
    logger = Equipment(model="IGU-16 1C 5Hz (counts)")
    return resp, sensor, logger


def _resp_igu16hr_3c_5hz(sample_rate: float):
    """IGU-16HR 3C, 5 Hz geophone, 76.6 V/(m/s), ADC 3355500 counts/V."""
    from obspy.core.inventory import Equipment
    resp = _paz_adc_response(
        sample_rate,
        zeros=[0j, 0j],
        poles=[complex(-22.211059, 22.217768), complex(-22.211059, -22.217768)],
        norm_factor=1.0, norm_freq=50.0, sensor_gain=76.6,
        adc_gain=3355500.0, sensitivity_freq=50.0)
    sensor = Equipment(type="Geophone", description="IGU-16HR 3C 5Hz node",
                       model="IGU-16HR 3C 5 Hz geophone, 76.6 V/m/s")
    logger = Equipment(model="IGU-16HR 3C 5Hz (counts)")
    return resp, sensor, logger


def _resp_igu16_bd3c_5s(sample_rate: float):
    """IGU-16 BD3C, 5 s (0.2 Hz) sensor, 209.4042 V/(m/s), ADC 3355478.9 counts/V."""
    from obspy.core.inventory import Equipment
    resp = _paz_adc_response(
        sample_rate,
        zeros=[complex(14164, 0), complex(-7162, 0), 0j, 0j],
        poles=[complex(-1720.4, 0), complex(-1.2, 0.9), complex(-1.2, -0.9)],
        norm_factor=-1.6972550464130542e-05, norm_freq=10.0, sensor_gain=209.4042,
        adc_gain=3355478.9, sensitivity_freq=10.0)
    sensor = Equipment(type="Short-period seismometer",
                       description="IGU-16 BD3C 5s node",
                       model="IGU-16 BD3C 5s, 209.4 V/m/s")
    logger = Equipment(model="IGU-16 BD3C 5s (counts)")
    return resp, sensor, logger


@dataclass(frozen=True)
class SensorType:
    """What DumbSolo needs to know about one node type.

    instrument: FDSN source (instrument) code. Seismometers:
        H = high gain seismometer, L = low gain seismometer,
        N = accelerometer, M = mass position,
        P = geophone / very short period seismometer (natural frequency
            5-10 Hz or higher)
    corner_period_s: the sensor's lower-bound (corner) period in seconds.
        < 10 s  -> short-period band codes (G, D, E, S at 1000/250/80/10 Hz)
        >= 10 s -> broadband band codes  (F, C, H, B at 1000/250/80/10 Hz)
    response: function(sample_rate) -> (Response, sensor, datalogger)
    """
    instrument: str
    corner_period_s: float
    response: Callable[[float], tuple]


#  device_type (exactly as in sct_par.xml) -> SensorType.
#  E.g. at 250 Hz: IGU-16HR (5 Hz geophone)  -> DPZ / DPN / DPE
#                  BD3C (5 s, short period)  -> DHZ / DHN / DHE
#  A future broadband node (say 120 s, high gain) would be added as
#      "...": SensorType("H", 120.0, _resp_...)  -> CHZ at 250 Hz, HHZ at 100 Hz
SENSOR_TYPES: Dict[str, SensorType] = {
    "IGU-16 1C 5Hz": SensorType("P", 0.2, _resp_igu16_1c_5hz),
    "IGU-16HR 3C 5Hz": SensorType("P", 0.2, _resp_igu16hr_3c_5hz),
    "IGU-16 BD3C 5s": SensorType("H", 5.0, _resp_igu16_bd3c_5s),
}


def _type_key(device_type: str) -> str:
    """Compare device types ignoring case, spaces, '-' and '_'
    ('IGU-16 1C 5Hz' == 'igu16_1c_5hz')."""
    return re.sub(r"[\s_\-]+", "", device_type or "").lower()


def sensor_type(device_type: str) -> Optional[SensorType]:
    st = SENSOR_TYPES.get((device_type or "").strip())
    if st is None and device_type:
        k = _type_key(device_type)
        st = next((v for name, v in SENSOR_TYPES.items() if _type_key(name) == k), None)
    return st


def instrument_code(device_type: str) -> str:
    st = sensor_type(device_type)
    return st.instrument if st else DEFAULT_INSTRUMENT_CODE


def corner_period(device_type: str) -> float:
    st = sensor_type(device_type)
    return st.corner_period_s if st else DEFAULT_CORNER_PERIOD_S


def channel_prefix(sample_rate: float, device_type: str) -> str:
    """Band + instrument code, e.g. 'DP' (250 Hz geophone)."""
    return band_code(sample_rate, corner_period(device_type)) + instrument_code(device_type)


def get_response(device_type: str, sample_rate: float):
    """(response, sensor, datalogger) for a node type, or None if unknown."""
    st = sensor_type(device_type)
    return st.response(sample_rate) if st else None


def build_inventory(deployments: List[dict], s: Settings, source: str = "DumbSolo",
                    warn: Callable[[str], None] = print):
    """One station epoch per deployment (serial + start). Responses come from
    SENSOR_TYPES by node type; if none matches, warn and write without one."""
    warned = set()
    from obspy import UTCDateTime
    from obspy.core.inventory import (Inventory, Network, Station, Channel, Site,
                                      Equipment)
    nets: Dict[str, list] = {}
    for d in deployments:
        if d.get("latitude") is None or d.get("longitude") is None:
            raise ValueError(f"{d['serial']}: no coordinates")
        t0 = UTCDateTime(d["start"]) if d.get("start") else None
        t1 = UTCDateTime(d["end"]) if d.get("end") else None
        sr = d.get("sample_rate") or 0.0
        elev = d.get("elevation") or 0.0
        chans = []
        for comp in d.get("components") or "ZXY":
            code, az, dip = COMPONENTS[comp]
            got = get_response(d.get("device_type"), sr)
            if got:
                resp, sensor, logger = got
            else:
                resp = sensor = logger = None
                if d["serial"] not in warned:
                    warned.add(d["serial"])
                    warn(f"WARNING {d['network']}.{d['station']} ({d['serial']}): no response "
                         f"for device type '{d.get('device_type')}' - channels written "
                         f"without response")
            sensor = sensor or Equipment(type="Sensor")
            logger = logger or Equipment(type="Datalogger")
            sensor.serial_number = logger.serial_number = d["serial"]
            if not sensor.model:
                sensor.model = d.get("device_type")
            chans.append(Channel(
                code=channel_prefix(sr, d.get("device_type")) + code,
                location_code=d.get("location") or "",
                latitude=d["latitude"], longitude=d["longitude"], elevation=elev, depth=0.0,
                azimuth=az, dip=dip, sample_rate=sr, start_date=t0, end_date=t1,
                sensor=sensor, data_logger=logger, types=["CONTINUOUS", "GEOPHYSICAL"],
                response=resp))
        nets.setdefault(d["network"], []).append(Station(
            code=d["station"], latitude=d["latitude"], longitude=d["longitude"],
            elevation=elev, start_date=t0, end_date=t1, creation_date=t0,
            site=Site(name=f"Node SN {d['serial']}"), channels=chans))
    networks = []
    for code, stas in sorted(nets.items()):
        stas.sort(key=lambda x: (x.code, x.start_date or UTCDateTime(0)))
        starts = [x.start_date for x in stas if x.start_date]
        ends = [x.end_date for x in stas if x.end_date]
        networks.append(Network(code=code, stations=stas,
                                start_date=min(starts) if starts else None,
                                end_date=max(ends) if ends else None))
    return Inventory(networks=networks, source=source, module=f"DumbSolo {__version__}")


# --------------------------------------------------------------------------- #
# Wipe
# --------------------------------------------------------------------------- #

def wipe_targets(node: Node) -> List[str]:
    """Every file on the node (recursively) except WIPE_KEEP in the top folder
    and OS-managed folders. Empty list if the folder doesn't look like a node,
    so a wrong path can never be wiped."""
    if not node_present(node.path):
        return []
    keep = {k.lower() for k in WIPE_KEEP}
    out = []
    for dirpath, dirnames, filenames in os.walk(node.path):
        top = os.path.normpath(dirpath) == os.path.normpath(node.path)
        dirnames[:] = sorted(d for d in dirnames if d not in ARCHIVE_SKIP)
        for f in sorted(filenames):
            if top and f.lower() in keep:
                continue
            p = os.path.join(dirpath, f)
            if os.path.isfile(p) and not os.path.islink(p):
                out.append(p)
    return out


def wipe_node(node: Node, log: Callable[[str], None] = print) -> Tuple[int, int]:
    """Delete wipe_targets(node), then the sub-folders left empty.
    Returns (files deleted, bytes freed)."""
    n = freed = 0
    for p in wipe_targets(node):
        try:
            size = os.path.getsize(p)
            os.remove(p)
            n += 1
            freed += size
        except OSError as e:
            log(f"{node.serial}: could not delete {os.path.relpath(p, node.path)}: {e}")
    for dirpath, dirnames, _ in os.walk(node.path, topdown=False):
        if os.path.normpath(dirpath) == os.path.normpath(node.path):
            continue
        rel = os.path.relpath(dirpath, node.path).replace(os.sep, "/").split("/")
        if rel[0] in ARCHIVE_SKIP:
            continue
        try:
            os.rmdir(dirpath)                    # only succeeds if empty
        except OSError:
            pass
    return n, freed



# --------------------------------------------------------------------------- #
# High-level harvest used by CLI and GUI
# --------------------------------------------------------------------------- #

def harvest(pairs: List[Tuple[Node, str]], sds_root: str, s: Settings, workers: int = 4,
            on_event: Callable[..., None] = lambda *a: None, cancel=None,
            log: Callable[[str], None] = print) -> bool:
    """Harvest nodes in parallel and record successes in the harvest DB."""
    db = HarvestDB(sds_root)
    all_jobs, by_node = [], {}
    for node, sta in pairs:
        if not node.gains_consistent:
            log(f"{node.job_tag}: SKIPPED - channel gains differ {node.gains_db}")
            continue
        if sensor_type(node.device_type) is None:
            log(f"{node.job_tag}: unknown device type '{node.device_type}' - channel codes "
                f"assume instrument '{DEFAULT_INSTRUMENT_CODE}', corner "
                f"{DEFAULT_CORNER_PERIOD_S:g} s; add it to SENSOR_TYPES")
        es = effective_settings(node, s)
        jobs = make_jobs(node, sta, es, db)
        if not jobs:
            in_range = any(file_window(f, es) for f in node.files)
            log(f"{node.job_tag}: " + ("nothing new to harvest" if in_range
                                       else "no data in time range"))
            db.record_deployment(node, sta, s)   # refresh metadata anyway
            continue
        by_node[node.job_tag] = (node, sta, es, {j["id"] for j in jobs})
        all_jobs += jobs
    # DLD files harvested with older timing settings: their data is at the wrong
    # times, so remove it (and its deployment records) before re-harvesting
    purged = set()
    for j in all_jobs:
        for path, size, name, first, win, comp in j["files"]:
            if (j["serial"], name, size) in purged:
                continue
            purged.add((j["serial"], name, size))
            rows = db.stale_dld_rows(j["serial"], name, size)
            if not rows:
                continue
            n = 0
            for r in rows:
                n += remove_span(sds_root, r["network"], r["station"], r["location"],
                                 r["channel"], dt.datetime.fromisoformat(r["win_start"]),
                                 dt.datetime.fromisoformat(r["win_end"]))
                db.drop_deployments_within(j["serial"], r["win_start"], r["win_end"])
            db.drop_rows([r["rowid"] for r in rows])
            log(f"{j['serial']} {name}: removed {n} samples harvested with old DLD timing "
                f"({len(rows)} record(s)); re-harvesting with {dld_time_cal()}")
    jobs_by_id = {j["id"]: j for j in all_jobs}
    ok_ids, ok = set(), True

    def handle(kind, *args):
        nonlocal ok
        if kind == "result":
            r = args[0]
            if r.get("error"):
                ok = False
                log(f"{r['id']}: ERROR {r['error']}")
            elif r.get("cancelled"):
                ok = False
                log(f"{r['id']}: cancelled")
            else:
                tag = r["id"].rsplit(".", 1)[0]
                db.record_job(jobs_by_id[r["id"]], r, by_node[tag][2])
                ok_ids.add(r["id"])
                log(f"{r['id']}: {r['samples']} samples, {r['days']} day file(s)")
                for w in r["warnings"]:
                    log(f"   WARNING: {w}")
                node, sta, _, ids = by_node[tag]
                if ids <= ok_ids:
                    db.record_deployment(node, sta, s)
                    log(f"{tag}: harvest complete")
        on_event(kind, *args)

    if all_jobs:
        log(f"Harvesting {len(all_jobs)} channel job(s) from {len(by_node)} deployment(s) "
            f"with {workers} worker(s)")
        run_jobs(all_jobs, sds_root, workers, handle, cancel)
    db.close()
    return ok



# --------------------------------------------------------------------------- #
# Raw archives (.tar.gz of a whole node volume)
# --------------------------------------------------------------------------- #

MANIFEST_NAME = "MANIFEST.sha256"
EXTRACTED_MARK = ".dumbsolo_extracted"


def archive_name(node: Node) -> str:
    node = node.whole
    if node.start and node.end:
        return f"{node.serial}_{node.start:%Y%m%d}-{node.end:%Y%m%d}.tar.gz"
    return f"{node.serial}_nodata.tar.gz"


def archive_path(node: Node, raw_dir: str) -> str:
    return os.path.join(raw_dir, archive_name(node))


def _archive_files(root: str) -> List[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in ARCHIVE_SKIP)
        for f in sorted(filenames):
            if f.startswith("._"):            # macOS resource-fork litter
                continue
            out.append(os.path.join(dirpath, f))
    return out


class _HashingReader:
    """File wrapper that hashes and reports progress as tarfile reads it."""

    def __init__(self, fh, progress):
        import hashlib
        self.fh, self.h, self.progress = fh, hashlib.sha256(), progress

    def read(self, n=-1):
        b = self.fh.read(n)
        self.h.update(b)
        if b and self.progress:
            self.progress(len(b))
        return b


class NotMounted(Exception):
    pass


def node_present(path: str) -> bool:
    """The node folder still exists and still looks like a node."""
    return os.path.isdir(path) and _ci_path(path, "device.ini") is not None


def is_volume_root(path: str) -> bool:
    """True if path is the root of a mounted volume (a USB node), as opposed
    to a folder of copied node data."""
    from PySide6.QtCore import QStorageInfo
    path = os.path.normpath(os.path.abspath(path))
    info = QStorageInfo(path)
    if not info.isValid():
        return False
    root = os.path.normpath(info.rootPath())
    if root != path:
        return False
    if sys.platform.startswith("win"):
        return len(path) <= 3                 # 'E:\\'
    return path not in ("/",) and not path.startswith(("/proc", "/sys", "/dev", "/run/user"))


def unmount_volume(path: str, timeout: float = 30.0) -> Tuple[bool, str]:
    """Unmount / eject one node volume. Returns (ok, message)."""
    import subprocess
    import time
    from PySide6.QtCore import QStorageInfo
    path = os.path.normpath(os.path.abspath(path))

    def gone() -> bool:
        if sys.platform.startswith("win"):
            return not os.path.exists(path)
        return not os.path.ismount(path)

    if sys.platform.startswith("win"):
        drive = path[:2]                                      # 'E:'
        ps = ("$d=(New-Object -ComObject Shell.Application).Namespace(17).ParseName('%s');"
              "if($d){$d.InvokeVerb('Eject')}" % drive)
        cmds = [["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]]
    elif sys.platform == "darwin":
        cmds = [["diskutil", "unmount", path]]
    else:
        dev = bytes(QStorageInfo(path).device()).decode(errors="replace")
        cmds = ([["udisksctl", "unmount", "-b", dev]] if dev.startswith("/dev/") else []) + \
               [["gio", "mount", "-u", path], ["umount", path]]
    last = ""
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            last = (r.stderr or r.stdout).strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            last = str(e)
            continue
        for _ in range(20):                                   # eject is asynchronous on Windows
            if gone():
                return True, "unmounted"
            time.sleep(0.25)
    return False, last or "unmount failed"


def archive_node(node: Node, raw_dir: str, progress: Callable[[int], None] = None,
                 cancelled: Callable[[], bool] = lambda: False) -> Tuple[str, bool]:
    """Write the whole node volume to raw_dir/<archive_name>. Returns
    (path, created); created is False if the archive already existed."""
    import tarfile
    node = node.whole
    if not node_present(node.path):
        raise NotMounted(f"{node.serial}: {node.path} is not mounted / no longer exists")
    files = _archive_files(node.path)
    if not files:
        raise NotMounted(f"{node.serial}: no files found in {node.path}")
    os.makedirs(raw_dir, exist_ok=True)
    dest = archive_path(node, raw_dir)
    if os.path.exists(dest):
        return dest, False
    tmp = dest + ".part"
    manifest = []
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=ARCHIVE_COMPRESSLEVEL) as tar:
            for p in files:
                if cancelled():
                    raise Cancelled()
                rel = os.path.relpath(p, node.path).replace(os.sep, "/")
                arc = f"{node.serial}/{rel}"
                ti = tar.gettarinfo(p, arcname=arc)
                with open(p, "rb") as fh:
                    r = _HashingReader(fh, progress)
                    tar.addfile(ti, r)
                manifest.append(f"{r.h.hexdigest()}  {rel}")
            data = ("\n".join(manifest) + "\n").encode()
            import io
            ti = tarfile.TarInfo(f"{node.serial}/{MANIFEST_NAME}")
            ti.size, ti.mtime = len(data), int(dt.datetime.now().timestamp())
            tar.addfile(ti, io.BytesIO(data))
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return dest, True


def extract_archive(path: str, dest_root: str, progress: Callable[[int], None] = None,
                    cancelled: Callable[[], bool] = lambda: False,
                    log: Callable[[str], None] = print) -> str:
    """Extract a node archive to dest_root/<archive stem>/ (reused if already
    extracted), verifying the SHA-256 manifest. Returns the folder."""
    import hashlib
    import shutil
    import tarfile
    stem = os.path.basename(path)
    for ext in (".tar.gz", ".tgz", ".tar"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
            break
    dest = os.path.join(dest_root, stem)
    if os.path.isfile(os.path.join(dest, EXTRACTED_MARK)):
        return dest
    tmp = dest + ".part"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    hashes, manifest = {}, None
    try:
        with tarfile.open(path, "r:*") as tar:
            for m in tar:
                if cancelled():
                    raise Cancelled()
                name = m.name.replace("\\", "/")
                parts = [x for x in name.split("/") if x not in ("", ".")]
                if not m.isfile() or name.startswith("/") or ".." in parts:
                    continue                                 # dirs/links/unsafe paths
                src = tar.extractfile(m)
                if parts[-1] == MANIFEST_NAME:
                    manifest = src.read().decode()
                    continue
                target = os.path.join(tmp, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                h = hashlib.sha256()
                with open(target, "wb") as out:
                    while True:
                        b = src.read(1 << 20)
                        if not b:
                            break
                        h.update(b)
                        out.write(b)
                        if progress:
                            progress(len(b))
                os.utime(target, (m.mtime, m.mtime))
                hashes["/".join(parts[1:])] = h.hexdigest()
        if manifest is None:
            log(f"{os.path.basename(path)}: no manifest - contents not verified")
        else:
            bad = [rel for line in manifest.splitlines() if line.strip()
                   for digest, rel in [line.split("  ", 1)] if hashes.get(rel) != digest]
            if bad:
                raise ValueError(f"{os.path.basename(path)}: checksum mismatch for "
                                 f"{len(bad)} file(s): {', '.join(bad[:5])}")
        open(os.path.join(tmp, EXTRACTED_MARK), "w").write(path + "\n")
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.replace(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return dest


def archive_total_bytes(path: str) -> int:
    """Uncompressed size of an archive's files (for progress)."""
    import tarfile
    with tarfile.open(path, "r:*") as tar:
        return sum(m.size for m in tar if m.isfile())


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #

PROJECT_FILE = "dumbsolo_project.json"


@dataclass
class Project:
    """A project folder. Paths default to sub-folders of the project and are
    stored relative to it when inside it, so a project folder can be moved."""
    folder: str
    network: str = "XX"
    location: str = ""
    sds: str = "sds"
    xml: str = "inventory.xml"
    raw: str = "raw_data"
    extracted: str = "raw_data/extracted"
    gap_hours: float = 1.0          # split deployments at data gaps longer than this
    archive_raw: bool = False       # back up node volumes to raw_data when harvesting
    nodes: Dict[str, dict] = field(default_factory=dict)   # deployment -> overrides
    schedule: List[dict] = field(default_factory=list)     # imported station names

    @property
    def name(self) -> str:
        return os.path.basename(os.path.normpath(self.folder))

    def abspath(self, p: str) -> str:
        if not p:
            return ""
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(self.folder, p))

    def relpath(self, p: str) -> str:
        if not p:
            return ""
        p = os.path.abspath(p)
        try:
            rel = os.path.relpath(p, self.folder)
        except ValueError:                      # different drive on Windows
            return p
        return p if rel.startswith("..") else rel.replace(os.sep, "/")

    @classmethod
    def is_project(cls, folder: str) -> bool:
        return os.path.isfile(os.path.join(folder, PROJECT_FILE))

    @classmethod
    def create(cls, folder: str) -> "Project":
        os.makedirs(folder, exist_ok=True)
        pr = cls(folder=os.path.abspath(folder))
        for sub in (pr.sds, pr.raw):
            os.makedirs(pr.abspath(sub), exist_ok=True)
        pr.save()
        return pr

    @classmethod
    def load(cls, folder: str) -> "Project":
        with open(os.path.join(folder, PROJECT_FILE), encoding="utf-8") as fh:
            d = json.load(fh)
        known = {f for f in cls.__dataclass_fields__ if f != "folder"}
        return cls(folder=os.path.abspath(folder), **{k: v for k, v in d.items() if k in known})

    def save(self):
        d = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "folder"}
        d["saved_by"] = f"DumbSolo {__version__}"
        tmp = os.path.join(self.folder, PROJECT_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, sort_keys=True)
        os.replace(tmp, os.path.join(self.folder, PROJECT_FILE))



# =========================================================================== #
#  GUI
# =========================================================================== #

COLS = ["Serial", "Station", "Harvest", "Status", "Size\n(MB)", "Script", "Firmware",
        "Start\n(UTC)", "End\n(UTC)", "Latitude", "Longitude", "Elev\n(m)", "Fixes",
        "SR\n(Hz)", "Gain\n(dB)", "Apply\nrotation\n(deg)", "Path"]
(C_SN, C_STA, C_HARV, C_STAT, C_SIZE, C_SCRIPT, C_FW, C_T0, C_T1, C_LAT, C_LON, C_ELV,
 C_FIX, C_SR, C_GAIN, C_ROT, C_PATH) = range(len(COLS))
NUMERIC = {C_SIZE, C_LAT, C_LON, C_ELV, C_FIX, C_SR, C_GAIN, C_ROT}
EDITABLE = {C_STA, C_ROT, C_LAT, C_LON, C_ELV}
ORANGE, GREEN, PINK = QColor("#b36b00"), QColor("#2a7a2a"), QColor(255, 220, 220)
DUP_RED = QColor(255, 120, 120)
KEY_ROLE = Qt.UserRole + 1          # row key stored on the Serial cell


class Item(QTableWidgetItem):
    """Centered table cell; numeric columns sort by value, not text."""
    def __init__(self, text="", numeric=False):
        super().__init__(text)
        self.numeric = numeric
        self.setTextAlignment(Qt.AlignCenter)

    def __lt__(self, other):
        if self.numeric:
            def key(t):
                try:
                    return (0, float(t))
                except ValueError:
                    return (1, 0.0)          # blanks / text sort last
            return key(self.text()) < key(other.text())
        return self.text().lower() < other.text().lower()


def mb(n: int) -> str:
    return f"{n / 1e6:.1f}"


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_dur(seconds: float) -> str:
    seconds = int(round(seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def to_qdt(t: dt.datetime) -> QDateTime:
    secs = int(t.replace(tzinfo=dt.timezone.utc).timestamp())
    return QDateTime.fromSecsSinceEpoch(secs, QTimeZone.utc())


def from_qdt(q: QDateTime) -> dt.datetime:
    return dt.datetime.fromtimestamp(q.toSecsSinceEpoch(), tz=dt.timezone.utc).replace(tzinfo=None)


def override_key(node: Node) -> str:
    """Per-deployment key for saved user edits: serial@deployment-start."""
    return f"{node.serial}@{node.start:%Y-%m-%dT%H:%M:%S}" if node.start else node.serial


# --------------------------------------------------------------------------- #
class HarvestWorker(QThread):
    log = Signal(str)
    event = Signal(object)             # tuples from harvest
    done = Signal(bool)

    def __init__(self, pairs, settings: Settings, sds_root: str, do_harvest: bool,
                 xml_path: str, workers: int, xml_all: bool,
                 archive: Optional[List[Node]] = None, raw_dir: str = ""):
        super().__init__()
        self.pairs, self.s, self.sds_root, self.xml_path = pairs, settings, sds_root, xml_path
        self.do_harvest, self.workers, self.xml_all = do_harvest, workers, xml_all
        self.archive, self.raw_dir = archive or [], raw_dir
        self._cancel = False

    def _archive_all(self) -> bool:
        """Back up node volumes (threads; I/O bound). Returns False on errors."""
        from concurrent.futures import ThreadPoolExecutor
        import threading
        lock = threading.Lock()
        done = [0]
        ok = [True]

        def prog(b):
            with lock:
                done[0] += b
                self.event.emit(("archive_progress", done[0]))

        def one(n):
            if self._cancel:
                return
            self.event.emit(("status", n.path, "archiving..."))
            try:
                path, made = archive_node(n, self.raw_dir, prog, lambda: self._cancel)
                self.log.emit(f"{n.serial}: " + (f"archived to {os.path.basename(path)} "
                                                 f"({mb(os.path.getsize(path))} MB)" if made else
                                                 f"already archived ({os.path.basename(path)})"))
                self.event.emit(("status", n.path, "archived"))
            except Cancelled:
                pass
            except NotMounted as e:
                ok[0] = False
                self.log.emit(f"Archive: {e}")
                self.event.emit(("gone", n.path))
            except Exception as e:  # noqa
                ok[0] = False
                self.log.emit(f"{n.serial}: archive FAILED: {e}")
                self.event.emit(("status", n.path, "archive error"))
        self.log.emit(f"Archiving {len(self.archive)} node volume(s) to {self.raw_dir} "
                      f"(gzip level {ARCHIVE_COMPRESSLEVEL})")
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            list(ex.map(one, self.archive))
        if self._cancel:
            raise Cancelled()
        return ok[0]

    def cancel(self):
        self._cancel = True

    def run(self):
        ok = True
        try:
            if self.archive:
                ok = self._archive_all() and ok
            if self.do_harvest:
                ok = harvest(self.pairs, self.sds_root, self.s, self.workers,
                             on_event=lambda *e: self.event.emit(e),
                             cancel=lambda: self._cancel, log=self.log.emit) and ok
            if self.xml_path and not self._cancel:
                if self.xml_all and self.sds_root:
                    db = HarvestDB(self.sds_root)
                    deps = db.deployments(); db.close()
                    have = {(d["serial"], d["start"]) for d in deps}
                    for n, sta in self.pairs:          # checked but not yet harvested
                        d = deployment_from_node(n, sta, self.s)
                        if (d["serial"], d["start"]) not in have:
                            deps.append(d)
                else:
                    deps = [deployment_from_node(n, sta, self.s) for n, sta in self.pairs]
                inv = build_inventory(deps, self.s, warn=self.log.emit)
                inv.write(self.xml_path, format="STATIONXML", validate=True)
                self.log.emit(f"StationXML written: {self.xml_path} ({len(deps)} station epoch(s))")
        except Cancelled:
            self.log.emit("Cancelled.")
            ok = False
        except Exception as e:  # noqa
            self.log.emit("ERROR: " + str(e))
            self.log.emit(traceback.format_exc())
            ok = False
        self.done.emit(ok)


class TaskWorker(QThread):
    """Runs fn(progress, cancelled, log) in a thread (archive / extract)."""
    progress = Signal(object)          # bytes done (int)
    log = Signal(str)
    done = Signal(object)              # fn's return value, or an Exception

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._cancel = False
        self._bytes = 0

    def cancel(self):
        self._cancel = True

    def _progress(self, n):
        self._bytes += n
        self.progress.emit(self._bytes)

    def run(self):
        try:
            r = self.fn(self._progress, lambda: self._cancel, self.log.emit)
        except Exception as e:  # noqa
            if not isinstance(e, Cancelled):
                self.log.emit(traceback.format_exc())
            r = e
        self.done.emit(r)


class NodeLoader(QThread):
    """Reads node volumes in the background (metadata + gap scan, which reads
    every record header of one component)."""
    loaded = Signal(object, str)       # Node, archive source ("" for volumes)
    failed = Signal(str, str)          # path, error

    def __init__(self, paths, archive_src: str = "", workers: int = 4):
        super().__init__()
        self.paths, self.archive_src, self.workers = paths, archive_src, workers

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            futs = {ex.submit(load_node, p): p for p in self.paths}
            for f in as_completed(futs):
                try:
                    self.loaded.emit(f.result(), self.archive_src)
                except Exception as e:  # noqa
                    self.failed.emit(futs[f], str(e))


# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"DumbSolo {__version__} - seismic node harvester")
        # ~20 % wider than before (1680 px), but never larger than the screen
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        w, h = 1680, 860
        if avail:
            w, h = min(w, int(avail.width() * 0.95)), min(h, int(avail.height() * 0.92))
        self.resize(w, h)
        self.cfg = QSettings("DumbSolo", "DumbSolo")
        self.bases: Dict[str, Node] = {}       # path -> whole node as read from disk
        self.rows: Dict[str, Node] = {}        # row key -> deployment (segment)
        self.worker = None
        self.loaders: List[NodeLoader] = []
        self._loading: set = set()
        self.project: Optional[Project] = None
        self._overrides: Dict[str, dict] = {}       # used when no project is open
        self._schedule: List[dict] = []             # likewise
        self._range_custom = False                  # user edited the time range
        self._status: Dict[str, str] = {}           # row key -> transient status text
        self._run_paths: set = set()                # node paths used by the running task

        self._build_menu()
        self._build_toolbar()
        self._build_ui()
        self._load_prefs()
        last = self.cfg.value("last_project", "")
        if last and Project.is_project(last):
            self.open_project(last)
        else:
            self._update_title()
            self.log("No project open - use Project > New... or Open...")

        self.auto_timer = QTimer(self, interval=4000, timeout=self.scan_drives)
        # notice nodes that were unplugged / folders that disappeared
        self.presence_timer = QTimer(self, interval=3000, timeout=self.check_present)
        self.presence_timer.start()
        self.scan_drives()

    # ---------------- UI construction ----------------
    def _build_menu(self):
        # The Project menu lives only on the toolbar's "Project: <name>" button
        # (no separate menu bar). Shortcut actions are also added to the window
        # so Ctrl+N / Ctrl+O work without opening the menu.
        from PySide6.QtWidgets import QMenu
        m = QMenu("&Project", self)
        self.project_menu = m
        for text, slot, key in [("&New...", self.new_project, "Ctrl+N"),
                                ("&Open...", self.open_project_dialog, "Ctrl+O")]:
            a = QAction(text, self); a.setShortcut(key); a.triggered.connect(slot)
            m.addAction(a); self.addAction(a)
        self.recent_menu = m.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        a = QAction("Clear imported station names", self)
        a.triggered.connect(self.clear_schedule); m.addAction(a)
        m.addSeparator()
        a = QAction("&Close project", self); a.triggered.connect(self.close_project); m.addAction(a)

    def _fill_recent(self):
        self.recent_menu.clear()
        recent = [p for p in (self.cfg.value("recent_projects") or []) if Project.is_project(p)]
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in recent:
            self.recent_menu.addAction(p, lambda p=p: self.open_project(p))

    def _build_toolbar(self):
        from PySide6.QtWidgets import QToolButton
        tb = QToolBar("Main", movable=False)
        self.addToolBar(tb)
        # Project menu also as the first toolbar button, so it's always visible
        self.btn_project = QToolButton(text="Project", popupMode=QToolButton.InstantPopup)
        self.btn_project.setMenu(self.project_menu)
        f = self.btn_project.font(); f.setBold(True); self.btn_project.setFont(f)
        tb.addWidget(self.btn_project)
        tb.addSeparator()
        for text, slot, tip in [
            ("Scan drives", self.scan_drives, "Look for mounted node volumes (device.ini)"),
            ("Add folder...", self.add_folder, "Add a folder containing node copies"),
            ("Load archive...", self.load_archives, "Load nodes from .tar.gz backups"),
            ("Import station names...", self.import_schedule,
             "CSV: serial, station, start, end (UTC) - names deployments; boundaries "
             "snap to real data gaps"),
            ("Map", self.show_map, "Plot station positions (harvest DB + loaded deployments)"),
            ("Export node table...", self.export_table, "Save the node table as CSV"),
            ("Wipe checked nodes...", self.wipe_nodes,
             "Delete data/log files from the checked node volumes"),
        ]:
            a = QAction(text, self, toolTip=tip)
            a.triggered.connect(slot)
            tb.addAction(a)
        tb.addSeparator()
        self.chk_auto = QCheckBox("Auto-detect new drives")
        self.chk_auto.toggled.connect(lambda on: self.auto_timer.start() if on else self.auto_timer.stop())
        tb.addWidget(self.chk_auto)

    def _build_ui(self):
        from PySide6.QtWidgets import QDoubleSpinBox
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)

        # ---- left: settings ----
        left = QVBoxLayout()
        g_codes = QGroupBox("Naming (FDSN)")
        f = QFormLayout(g_codes)
        self.ed_net = QLineEdit("XX", maxLength=2)
        self.ed_loc = QLineEdit("", maxLength=2, placeholderText="(blank)")
        f.addRow("Network", self.ed_net)
        f.addRow("Location", self.ed_loc)
        left.addWidget(g_codes)

        g_proc = QGroupBox("Processing")
        f = QFormLayout(g_proc)
        # read-only: changed only in dumbsolo.py (DATA TRANSFORM block)
        lbl = QLabel(
            ("Preamp gain: removed" if REMOVE_PREAMP_GAIN else "Preamp gain: KEPT") + "\n" +
            ("Polarity: flipped" if FLIP_POLARITY else "Polarity: NOT flipped") + "\n" +
            f"Steim2, {MSEED_RECORD_LENGTH}-byte records")
        lbl.setToolTip("Fixed for safety. To change, edit the DATA TRANSFORM block in "
                       "dumbsolo.py and harvest into a new SDS root.")
        self.sp_gap = QDoubleSpinBox(decimals=2, minimum=0.01, maximum=100000.0, value=1.0,
                                     suffix=" h", singleStep=0.5)
        self.sp_gap.setToolTip("A node's data is split into separate deployments wherever\n"
                               "there is a gap longer than this.")
        self.sp_gap.editingFinished.connect(self._gap_changed)
        self.chk_skip = QCheckBox("Skip already-harvested data", checked=True)
        self.sp_workers = QSpinBox(minimum=1, maximum=max(1, os.cpu_count() or 1) * 2,
                                   value=min(4, os.cpu_count() or 1))
        f.addRow("Data", lbl)
        f.addRow("Split at gaps >", self.sp_gap)
        f.addRow(self.chk_skip)
        self.chk_archive = QCheckBox("Archive raw node data (.tar.gz)")
        self.chk_archive.setToolTip(
            "When harvesting, first back up each checked node volume (everything on it)\n"
            "to the raw data folder. Nodes already archived are skipped; nodes loaded\n"
            "from folders or archives are never archived.")
        self.chk_archive.toggled.connect(lambda *_: self._save_project())
        f.addRow(self.chk_archive)
        f.addRow("Parallel workers", self.sp_workers)
        left.addWidget(g_proc)

        g_time = QGroupBox("Time range (UTC)")
        f = QFormLayout(g_time)
        self.dt_from = QDateTimeEdit(calendarPopup=True)
        self.dt_to = QDateTimeEdit(calendarPopup=True)
        for w in (self.dt_from, self.dt_to):
            w.setTimeZone(QTimeZone.utc())
            w.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            w.dateTimeChanged.connect(self._range_edited)
            w.editingFinished.connect(self.refresh_harvest_status)
        self.dt_from.setToolTip("Inclusive")
        self.dt_to.setToolTip("Exclusive")
        b = QPushButton("All loaded data")
        b.clicked.connect(self.reset_range)
        f.addRow("From", self.dt_from)
        f.addRow("To", self.dt_to)
        f.addRow(b)
        left.addWidget(g_time)

        g_out = QGroupBox("Output")
        v = QVBoxLayout(g_out)
        self.ed_sds = QLineEdit(placeholderText="SDS root folder")
        b = QPushButton("...", maximumWidth=30); b.clicked.connect(self._pick_sds)
        h = QHBoxLayout(); h.addWidget(self.ed_sds); h.addWidget(b); v.addLayout(h)
        self.ed_xml = QLineEdit(placeholderText="inventory.xml")
        b = QPushButton("...", maximumWidth=30); b.clicked.connect(self._pick_xml)
        h = QHBoxLayout(); h.addWidget(self.ed_xml); h.addWidget(b); v.addLayout(h)
        self.ed_raw = QLineEdit(placeholderText="raw data (archives) folder")
        b = QPushButton("...", maximumWidth=30); b.clicked.connect(self._pick_raw)
        h = QHBoxLayout(); h.addWidget(self.ed_raw); h.addWidget(b); v.addLayout(h)
        for w, tip in ((self.ed_sds, "SDS archive root (also holds the harvest DB)"),
                       (self.ed_xml, "StationXML output file"),
                       (self.ed_raw, "Folder for raw node backups (.tar.gz)")):
            w.setToolTip(tip)
            w.editingFinished.connect(self._save_project)
        for w in (self.ed_net, self.ed_loc):
            w.editingFinished.connect(self._save_project)
        self.chk_sds = QCheckBox("Harvest to SDS", checked=True)
        self.chk_xml = QCheckBox("Write StationXML", checked=True)
        self.cb_xml_scope = QComboBox()
        self.cb_xml_scope.addItems(["All deployments in harvest DB", "Checked nodes only"])
        v.addWidget(self.chk_sds); v.addWidget(self.chk_xml); v.addWidget(self.cb_xml_scope)
        left.addWidget(g_out)

        self.btn_go = QPushButton("Harvest checked nodes")
        self.btn_go.setMinimumHeight(40)
        fnt = self.btn_go.font(); fnt.setBold(True); self.btn_go.setFont(fnt)
        self.btn_go.clicked.connect(self.start)
        self.btn_cancel = QPushButton("Cancel", enabled=False)
        self.btn_cancel.clicked.connect(lambda: self.worker and self.worker.cancel())
        left.addWidget(self.btn_go)
        left.addWidget(self.btn_cancel)
        h = QHBoxLayout()
        self.btn_umount_sel = QPushButton("Unmount selected")
        self.btn_umount_sel.setToolTip("Safely unmount/eject the USB node volumes of the "
                                       "highlighted rows.\nNodes loaded from folders or "
                                       "archives are left alone.")
        self.btn_umount_sel.clicked.connect(lambda: self.unmount_nodes(selected_only=True))
        self.btn_umount_all = QPushButton("Unmount all")
        self.btn_umount_all.setToolTip("Safely unmount/eject every loaded USB node volume.")
        self.btn_umount_all.clicked.connect(lambda: self.unmount_nodes(selected_only=False))
        h.addWidget(self.btn_umount_sel); h.addWidget(self.btn_umount_all)
        left.addLayout(h)
        left.addStretch(1)
        lw = QWidget(); lw.setLayout(left); lw.setFixedWidth(330)
        outer.addWidget(lw)

        # ---- right: table / details / log ----
        split = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.horizontalHeaderItem(C_ROT).setToolTip(
            "Extra rotation applied to the horizontal (N/E) data, in degrees.\n"
            "Sensors are assumed oriented to north (0/360); 0 = no rotation.\n"
            "Positive = clockwise (N towards E), negative = counter-clockwise.")
        self.table.horizontalHeaderItem(C_FIX).setToolTip(
            "GPS fixes used for the position (after removing outliers and fixes\n"
            "logged before/after recording). Details pane has the breakdown.")
        self.table.horizontalHeaderItem(C_STA).setToolTip(
            "Station code (max 5 characters). Red = used by more than one row.")
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(C_PATH, QHeaderView.Stretch)
        hh.setDefaultAlignment(Qt.AlignCenter)
        hh.setSectionsMovable(True)                 # drag columns to reorder
        hh.setContextMenuPolicy(Qt.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._show_details)
        split.addWidget(self.table)

        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)   # platform monospace font
        self.details = QPlainTextEdit(readOnly=True); self.details.setFont(mono)
        self.details.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.logbox = QPlainTextEdit(readOnly=True); self.logbox.setFont(mono)
        bottom = QSplitter(Qt.Horizontal)
        bottom.addWidget(self._labelled("Node details", self.details))
        bottom.addWidget(self._labelled("Log", self.logbox))
        split.addWidget(bottom)
        split.setSizes([500, 300])

        rv = QVBoxLayout()
        rv.addWidget(split)
        self.pbar = QProgressBar(); rv.addWidget(self.pbar)
        rw = QWidget(); rw.setLayout(rv)
        outer.addWidget(rw, 1)

    def _header_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        m.addAction("Reset column order", self.reset_columns)
        m.exec(self.table.horizontalHeader().mapToGlobal(pos))

    def reset_columns(self):
        hh = self.table.horizontalHeader()
        for logical in range(len(COLS)):
            hh.moveSection(hh.visualIndex(logical), logical)

    @staticmethod
    def _labelled(title, w):
        box = QGroupBox(title); l = QVBoxLayout(box); l.addWidget(w); return box

    # ---------------- prefs ----------------
    def _load_prefs(self):
        c = self.cfg
        self.sp_workers.setValue(int(c.value("workers", self.sp_workers.value())))
        state = c.value("header_v2")
        if state is not None:
            self.table.horizontalHeader().restoreState(state)

    def closeEvent(self, ev):
        if self._busy():
            if QMessageBox.question(self, "Busy", "A task is still running. Quit anyway?",
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) != QMessageBox.Yes:
                ev.ignore(); return
            self.worker.cancel(); self.worker.wait(5000)
        for ld in self.loaders:
            ld.wait(10000)
        self._save_project()
        c = self.cfg
        c.setValue("workers", self.sp_workers.value())
        c.setValue("header_v2", self.table.horizontalHeader().saveState())
        super().closeEvent(ev)

    # ---------------- projects ----------------
    def _update_title(self):
        name = self.project.name if self.project else "no project"
        self.setWindowTitle(f"DumbSolo {__version__} - {name}")
        self.btn_project.setText(f"Project: {name}")
        self.btn_project.setToolTip(self.project.folder if self.project else
                                    "Create or open a project")

    def _busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def new_project(self):
        if self._busy():
            return
        p, _ = QFileDialog.getSaveFileName(
            self, "New project: choose a location and name for the project folder",
            self.cfg.value("project_parent", os.path.expanduser("~")), "",
            options=QFileDialog.DontConfirmOverwrite)
        if not p:
            return
        if Project.is_project(p):
            self.log(f"{p} is already a project - opening it")
        elif os.path.exists(p) and (not os.path.isdir(p) or os.listdir(p)):
            QMessageBox.warning(self, "New project", f"{p} exists and is not empty."); return
        else:
            Project.create(p)
            self.log(f"Created project {p}")
        self.cfg.setValue("project_parent", os.path.dirname(os.path.abspath(p)))
        self.open_project(p)

    def open_project_dialog(self):
        if self._busy():
            return
        d = QFileDialog.getExistingDirectory(self, "Open project folder",
                                             self.cfg.value("project_parent", ""))
        if not d:
            return
        if not Project.is_project(d):
            QMessageBox.warning(self, "Open project", f"No {PROJECT_FILE} in {d}."); return
        self.open_project(d)

    def open_project(self, folder):
        if self._busy():
            return
        try:
            pr = Project.load(folder)
        except Exception as e:  # noqa
            QMessageBox.warning(self, "Open project", f"Could not open {folder}:\n{e}"); return
        self._save_project()
        self.project = pr
        for w, v in ((self.ed_net, pr.network), (self.ed_loc, pr.location),
                     (self.ed_sds, pr.abspath(pr.sds)), (self.ed_xml, pr.abspath(pr.xml)),
                     (self.ed_raw, pr.abspath(pr.raw))):
            w.setText(v)
        self.sp_gap.setValue(float(pr.gap_hours))
        self.chk_archive.blockSignals(True)
        self.chk_archive.setChecked(bool(pr.archive_raw))
        self.chk_archive.blockSignals(False)
        recent = [pr.folder] + [p for p in (self.cfg.value("recent_projects") or []) if p != pr.folder]
        self.cfg.setValue("recent_projects", recent[:8])
        self.cfg.setValue("last_project", pr.folder)
        self._rebuild_rows()
        self._update_title()
        self.log(f"Opened project {pr.folder}")

    def close_project(self):
        if self._busy():
            return
        self._save_project()
        self.project = None
        for w in (self.ed_sds, self.ed_xml, self.ed_raw):
            w.clear()
        self.cfg.setValue("last_project", "")
        self._rebuild_rows()
        self._update_title()

    def _save_project(self):
        pr = self.project
        if not pr:
            return
        pr.network = self.ed_net.text().strip().upper() or "XX"
        pr.location = self.ed_loc.text().strip().upper()
        pr.sds = pr.relpath(self.ed_sds.text().strip())
        pr.xml = pr.relpath(self.ed_xml.text().strip())
        pr.raw = pr.relpath(self.ed_raw.text().strip())
        pr.gap_hours = float(self.sp_gap.value())
        pr.archive_raw = self.chk_archive.isChecked()
        try:
            pr.save()
        except OSError as e:
            self.log(f"Could not save project: {e}")
        self.refresh_harvest_status()

    def overrides(self) -> Dict[str, dict]:
        """Per-deployment user edits (station, rotation, coordinates), kept in the project."""
        return self.project.nodes if self.project else self._overrides

    def _set_override(self, node, **kv):
        self.overrides().setdefault(override_key(node), {}).update(kv)
        self._save_project()

    def schedule(self) -> List[dict]:
        return self.project.schedule if self.project else self._schedule

    def import_schedule(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import station names (serial, station, start, end)",
                                           self.project.folder if self.project else "",
                                           "CSV (*.csv *.txt *.tsv)")
        if not p:
            return
        try:
            entries, problems = load_schedule(p)
        except Exception as e:  # noqa
            QMessageBox.warning(self, "Import station names", str(e)); return
        for pr in problems:
            self.log(f"Station names: {pr}")
        if not entries:
            QMessageBox.warning(self, "Import station names", "No usable rows found."); return
        serials = {e["serial"] for e in entries}
        sched = [e for e in self.schedule() if e["serial"] not in serials] + entries
        if self.project:
            self.project.schedule = sched
        else:
            self._schedule = sched
        # imported names replace earlier manual station edits for these nodes
        for k, d in self.overrides().items():
            if k.split("@")[0] in serials:
                d.pop("station", None)
        self._save_project()
        self._rebuild_rows()
        self._report_schedule(entries, p)

    def clear_schedule(self):
        if self.project:
            self.project.schedule = []
        self._schedule = []
        self._save_project()
        self._rebuild_rows()
        self.log("Imported station names cleared.")

    def _report_schedule(self, entries, path):
        used = {id(sg._sched) for sg in self.rows.values() if getattr(sg, "_sched", None)}
        loaded = {n.serial for n in self.bases.values()}
        self.log(f"Imported {len(entries)} station name(s) from {path}")
        for b in self.bases.values():
            for note in getattr(b, "_sched_notes", []):
                self.log("  " + note)
        n_named = sum(1 for sg in self.rows.values() if getattr(sg, "_sched", None))
        unmatched = [e for e in self.schedule() if id(e) not in used]
        for e in unmatched:
            if e["serial"] not in loaded:
                why = "node not loaded"
            else:
                s0, s1 = _entry_times(e)
                hit = [sg for sg in self.rows.values() if sg.serial == e["serial"] and sg.start
                       and (s1 is None or sg.start < s1) and (s0 is None or sg.end > s0)]
                why = (f"its time span is inside deployment named {hit[0]._station} "
                       f"(no data gap to split at)" if hit else "no data in that time span")
            self.log(f"  not applied: {e['serial']} -> {e['station']} "
                     f"({e['start'] or '...'} - {e['end'] or '...'}): {why}")
        self.log(f"  {n_named} deployment(s) named from the import")

    def _apply_overrides(self, node):
        """Defaults from the node, then saved edits for this deployment (or, for
        older projects, edits saved under the plain serial)."""
        node.rotation = 0.0
        node._station = node.default_station()
        node._sched = schedule_station(node, self.schedule())
        if node._sched:
            node._station = node._sched["station"]
        ov = self.overrides()
        row = ov.get(override_key(node)) or ov.get(node.serial) or {}
        if row.get("station"):
            node._station = str(row["station"]).upper()[:5]
        for k in ("latitude", "longitude", "elevation"):
            if row.get(k) not in (None, ""):
                setattr(node, k, float(row[k]))
        if row.get("rotation") not in (None, ""):
            node.rotation = float(row["rotation"])

    def raw_dir(self) -> str:
        return self.ed_raw.text().strip()

    def extract_dir(self) -> str:
        if self.project:
            return self.project.abspath(self.project.extracted)
        return os.path.join(self.raw_dir(), "extracted") if self.raw_dir() else ""

    # ---------------- time range ----------------
    def _data_span(self, extra=()):
        nodes = list(self.rows.values()) + list(extra)
        starts = [n.start for n in nodes if n.start]
        ends = [n.end for n in nodes if n.end]
        if not starts:
            return None
        t0 = min(starts).replace(microsecond=0)
        t1 = max(ends).replace(microsecond=0) + dt.timedelta(seconds=1)   # 'To' is exclusive
        return t0, t1

    def _set_range(self, t0, t1):
        for w, t in ((self.dt_from, t0), (self.dt_to, t1)):
            w.blockSignals(True); w.setDateTime(to_qdt(t)); w.blockSignals(False)

    def reset_range(self):
        span = self._data_span()
        if span:
            self._set_range(*span)
        self._range_custom = False
        self.refresh_harvest_status()

    def _range_edited(self, *_):
        self._range_custom = True

    def window(self):
        if not self.rows:
            return None, None
        return from_qdt(self.dt_from.dateTime()), from_qdt(self.dt_to.dateTime())

    # ---------------- helpers ----------------
    def settings(self) -> Settings:
        t0, t1 = self.window()
        return Settings(
            network=self.ed_net.text().strip().upper() or "XX",
            location=self.ed_loc.text().strip().upper(),
            skip_harvested=self.chk_skip.isChecked(), t_start=t0, t_end=t1)

    def _db(self):
        root = self.ed_sds.text().strip()
        if not root or not os.path.isdir(root):
            return None
        try:
            return HarvestDB(root)
        except Exception:  # noqa
            return None

    def _harvest_status(self, node, db=None) -> str:
        own = db is None
        db = db or self._db()
        if not db:
            return "?"
        try:
            return db.node_status(node, effective_settings(node, self.settings()))
        finally:
            if own:
                db.close()

    def _paint_harvest(self, it):
        st = it.text()
        it.setForeground(GREEN if st == "harvested" else
                         ORANGE if st.startswith("partial") else QColor())

    def refresh_harvest_status(self):
        db = self._db()
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            node = self.rows.get(self._key(r))
            if node is None:
                continue
            it = self.table.item(r, C_HARV)
            it.setText(self._harvest_status(node, db) if db else "?")
            self._paint_harvest(it)
        self.table.blockSignals(False)
        if db:
            db.close()

    def log(self, msg):
        self.logbox.appendPlainText(msg)

    def _pick_sds(self):
        d = QFileDialog.getExistingDirectory(self, "SDS root", self.ed_sds.text())
        if d:
            self.ed_sds.setText(d); self._save_project()

    def _pick_raw(self):
        d = QFileDialog.getExistingDirectory(self, "Raw data folder", self.ed_raw.text())
        if d:
            self.ed_raw.setText(d); self._save_project()

    def _pick_xml(self):
        p, _ = QFileDialog.getSaveFileName(self, "StationXML", self.ed_xml.text() or "inventory.xml",
                                           "StationXML (*.xml)")
        if p:
            self.ed_xml.setText(p); self._save_project()

    # ---------------- discovery / loading ----------------
    def scan_drives(self):
        found = []
        for vol in QStorageInfo.mountedVolumes():
            if not vol.isValid() or not vol.isReady():
                continue
            root = vol.rootPath()
            if root in ("/",) or root.startswith(("/proc", "/sys", "/dev", "/run/user", "/snap", "/System")):
                continue
            if os.path.isfile(os.path.join(root, "device.ini")) or os.path.isfile(os.path.join(root, "DEVICE.INI")):
                found.append(os.path.abspath(root))
        new = [p for p in found if p not in self.bases and p not in self._loading]
        if new:
            self.load_paths(new)
        if new or not self.chk_auto.isChecked():
            self.log(f"Drive scan: {len(found)} node volume(s), {len(new)} new.")

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder containing node(s)")
        if not d:
            return
        dirs = find_node_dirs(d, max_depth=3)
        self.log(f"{d}: {len(dirs)} node folder(s) found")
        self.load_paths(dirs)

    def load_paths(self, paths, archive_src: str = ""):
        """Read nodes in the background; rows appear as each one finishes."""
        paths = [os.path.abspath(p) for p in paths]
        paths = [p for p in paths if p not in self.bases and p not in self._loading]
        if not paths:
            return
        self._loading.update(paths)
        ld = NodeLoader(paths, archive_src, self.sp_workers.value())
        ld.loaded.connect(self._node_loaded)
        ld.failed.connect(lambda p, e: (self._loading.discard(p),
                                        self.log(f"Failed to read {p}: {e}")))
        ld.finished.connect(lambda ld=ld: self.loaders.remove(ld) if ld in self.loaders else None)
        self.loaders.append(ld)
        self.log(f"Reading {len(paths)} node(s)...")
        ld.start()

    def add_node(self, path, archive_src: str = ""):
        """Synchronous load (used by tests / command-line folders)."""
        path = os.path.abspath(path)
        if path in self.bases:
            return
        try:
            node = load_node(path)
        except Exception as e:  # noqa
            self.log(f"Failed to read {path}: {e}")
            return
        self._node_loaded(node, archive_src)

    def _node_loaded(self, node, archive_src):
        self._loading.discard(node.path)
        if node.path in self.bases:
            return
        node._archive = archive_src
        node._volume = (not archive_src) and is_volume_root(node.path)
        self.bases[node.path] = node
        segs = self._segments(node)
        # widen the time range BEFORE filling the rows, so their Harvest status
        # (and default check mark) is computed against the new range
        if not self._range_custom:
            span = self._data_span(extra=segs)
            if span:
                self._set_range(*span)
        for sg in segs:
            self._add_row(sg)
        self._mark_duplicates()
        msg = f"{node.serial}: loaded"
        if len(segs) > 1:
            msg += f", split into {len(segs)} deployments at gaps > {self.sp_gap.value():g} h"
        self.log(msg)
        if node.warnings:
            self.log(f"{node.serial}: " + "; ".join(node.warnings))

    def _segments(self, base) -> List[Node]:
        forced, notes = schedule_splits(base, self.schedule())
        base._sched_notes = notes
        segs = split_deployments(base, self.sp_gap.value(), forced)
        out = []
        for sg in segs:
            if sg is base:                       # unsplit: work on a copy
                import copy
                sg = copy.copy(base)
                sg.base, sg.segment, sg.n_segments = base, 0, 1
            sg._archive = getattr(base, "_archive", "")
            sg._gps = {k: getattr(sg, k) for k in ("latitude", "longitude", "elevation")}
            self._apply_overrides(sg)
            out.append(sg)
        return out

    def _gap_changed(self):
        if self.project and abs(self.project.gap_hours - self.sp_gap.value()) < 1e-9:
            return
        self._save_project()
        self._rebuild_rows()

    def _rebuild_rows(self):
        """Regenerate all rows from the loaded nodes (after a project switch or
        a change of the gap threshold), keeping check marks where possible."""
        checks = {self._key(r): self.table.item(r, C_SN).checkState()
                  for r in range(self.table.rowCount())}
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.rows.clear()
        for path in sorted(self.bases):
            for sg in self._segments(self.bases[path]):
                self._add_row(sg, checks.get(sg.key))
        self.table.blockSignals(False)
        self.table.setSortingEnabled(True)
        if not self._range_custom:
            span = self._data_span()
            if span:
                self._set_range(*span)
        self.refresh_harvest_status()
        self._mark_duplicates()

    # ---------------- table ----------------
    def _key(self, r) -> str:
        it = self.table.item(r, C_SN)
        return it.data(KEY_ROLE) if it else ""

    def _row_for(self, key):
        for r in range(self.table.rowCount()):
            if self._key(r) == key:
                return r
        return -1

    def _add_row(self, node, check=None):
        self.rows[node.key] = node
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        r = self.table.rowCount()
        self.table.insertRow(r)
        self._fill_row(r, node, check)
        self.table.blockSignals(False)
        self.table.setSortingEnabled(sorting)

    def _fill_row(self, r, node, check=None):
        def item(text, editable=False, col=None):
            it = Item("" if text is None else str(text), numeric=col in NUMERIC)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
            if editable:
                flags |= Qt.ItemIsEditable
            it.setFlags(flags)
            return it
        fmt = lambda v, n: "" if v is None else f"{v:.{n}f}"
        tfmt = lambda t: t.strftime("%Y-%m-%d %H:%M:%S") if t else ""
        hstat = self._harvest_status(node)

        label = node.serial + (f" ({node.segment + 1}/{node.n_segments})" if node.n_segments > 1 else "")
        sn = item(label)                            # serial cell carries the checkbox + key
        sn.setData(KEY_ROLE, node.key)
        sn.setFlags(sn.flags() | Qt.ItemIsUserCheckable)
        no_data = not node.files or node.start is None or node.whole.data_bytes == 0
        if check is None:
            check = Qt.Checked if (not no_data and hstat != "harvested" and
                                   node.gains_consistent) else Qt.Unchecked
        sn.setCheckState(check)
        if getattr(node, "_archive", ""):
            status = "archive"
        elif self._status.get(node.key):
            status = self._status[node.key]          # e.g. wiped / unmount failed
        elif no_data:
            status = "NO DATA"
        else:
            status = "! " + node.warnings[0] if node.warnings else "ready"
        vals = {
            C_SN: sn, C_STA: item(node._station, True),
            C_HARV: item(hstat),
            C_STAT: item(status),
            C_SIZE: item(mb(node.data_bytes), col=C_SIZE),
            C_SCRIPT: item(node.script_name or "?"),
            C_FW: item(node.firmware or "?"),
            C_T0: item(tfmt(node.start)), C_T1: item(tfmt(node.end)),
            C_LAT: item(fmt(node.latitude, 6), True, C_LAT),
            C_LON: item(fmt(node.longitude, 6), True, C_LON),
            C_ELV: item(fmt(node.elevation, 1), True, C_ELV),
            C_FIX: item(node.n_fixes, col=C_FIX),
            C_SR: item(fmt(node.sample_rate, 0), col=C_SR),
            C_GAIN: item(fmt(node.gain_db, 0), col=C_GAIN),
            C_ROT: item(f"{node.rotation:g}", True, C_ROT),
            C_PATH: item(node.path),
        }
        for c, it in vals.items():
            self.table.setItem(r, c, it)
        self._paint_harvest(vals[C_HARV])
        if node.warnings and status.startswith("! "):
            vals[C_STAT].setForeground(ORANGE)
        if status == "NO DATA":
            vals[C_STAT].setForeground(QColor("#c00000"))
            f = vals[C_STAT].font(); f.setBold(True); vals[C_STAT].setFont(f)
            unrec = [w for w in node.warnings if "unrecognised names" in w]
            if unrec:
                vals[C_STAT].setText("NO DATA (?)")
            vals[C_STAT].setToolTip("No MiniSEED or DLD data on this node" +
                                    ("" if node.fixes else " and no GPS fixes") +
                                    ("\n" + "\n".join(unrec) if unrec else ""))
        if node.latitude is None:
            for c in (C_LAT, C_LON, C_ELV):
                vals[c].setBackground(PINK)

    def _mark_duplicates(self):
        """Station codes used by more than one row are highlighted red."""
        from collections import Counter
        counts = Counter(n._station for n in self.rows.values())
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            node = self.rows.get(self._key(r))
            it = self.table.item(r, C_STA)
            if node is None or it is None:
                continue
            n = counts[node._station]
            if n > 1:
                it.setBackground(DUP_RED)
                it.setToolTip(f"Station code {node._station} is used by {n} rows")
            else:
                it.setBackground(QColor(0, 0, 0, 0))
                it.setToolTip("")
        self.table.blockSignals(False)

    def _item_changed(self, it):
        r, c = it.row(), it.column()
        if c not in EDITABLE:
            return
        node = self.rows.get(self._key(r))
        if not node:
            return
        txt = it.text().strip()
        if c == C_STA:
            txt = (txt.upper()[:5]) or node.default_station()
            node._station = txt
            self.table.blockSignals(True); it.setText(txt); self.table.blockSignals(False)
            self._set_override(node, station=txt)
            self._mark_duplicates()
            return
        if c == C_ROT:
            try:
                node.rotation = float(txt) if txt else 0.0
                it.setBackground(QColor(0, 0, 0, 0))
                self._set_override(node, rotation=node.rotation)
                self.table.blockSignals(True)
                it.setText(f"{node.rotation:g}")
                h = self.table.item(r, C_HARV)
                h.setText(self._harvest_status(node)); self._paint_harvest(h)
                self.table.blockSignals(False)
                self._show_details()
            except ValueError:
                it.setBackground(PINK)
            return
        attr = {C_LAT: "latitude", C_LON: "longitude", C_ELV: "elevation"}[c]
        try:
            val = float(txt) if txt else None
            setattr(node, attr, val if val is not None else node._gps.get(attr))
            it.setBackground(QColor(0, 0, 0, 0))
            self._set_override(node, **{attr: val})
        except ValueError:
            it.setBackground(PINK)

    def _backup_text(self, node) -> str:
        if getattr(node, "_archive", ""):
            return f"loaded from {node._archive}"
        if not self.raw_dir() or not node.whole.files:
            return "-"
        p = archive_path(node, self.raw_dir())
        return f"{p} ({mb(os.path.getsize(p))} MB)" if os.path.exists(p) else "none"

    def _show_details(self):
        rows = {i.row() for i in self.table.selectedItems()}
        if len(rows) != 1:
            return
        node = self.rows.get(self._key(rows.pop()))
        if node is None:
            return
        whole = node.whole
        fmt = lambda v, n: "-" if v is None else f"{v:.{n}f}"
        L = [f"Serial        {node.serial}   firmware {node.firmware}",
             f"Device        {node.device_type}   project '{node.project}'",
             f"Script        {node.script_name}",
             f"Path          {node.path}",
             f"Raw backup    " + self._backup_text(node),
             f"Deployment    {node.segment + 1} of {node.n_segments}   "
             f"{node.start} - {node.end}",
             f"Station name  " + (f"{node._sched['station']} from import "
                                  f"({node._sched['start'] or '...'} - {node._sched['end'] or '...'})"
                                  if getattr(node, "_sched", None) else "default / edited") ,
             f"Position      {fmt(node.latitude, 6)}, {fmt(node.longitude, 6)}, {fmt(node.elevation, 1)} m "
             f"(mean of {node.n_fixes} GPS fixes; {node.n_fixes_rejected} outlier(s) removed"
             + (f", {node.n_fixes_outside} logged outside the recording ignored"
                if node.n_fixes_outside else "") + ")",
             f"Scatter       {fmt(node.pos_scatter_m, 1)} m horizontal, "
             f"{fmt(node.alt_scatter_m, 1)} m vertical (1-sigma of fixes used)",
             f"Sample rate   {fmt(node.sample_rate, 3)} Hz (header), "
             f"{fmt(node.sample_rate_par, 3)} Hz (sct_par)   gains {node.gains_db}",
             f"Rotation      {node.rotation:g} deg applied to N/E" +
             ("  (none)" if not node.rotation else
              ("  (clockwise)" if node.rotation > 0 else "  (counter-clockwise)")),
             f"eCompass N    {fmt(node.compass_north, 1)} deg   tilt {fmt(node.tilt, 2)} deg",
             ""]
        if whole.gaps:
            L.append(f"Data gaps > {MIN_GAP_S:g} s ({len(whole.gaps)}; deployments split at "
                     f"> {self.sp_gap.value():g} h):")
            for a, b in sorted(whole.gaps, key=lambda g: g[0] - g[1])[:10]:
                L.append(f"  {a:%Y-%m-%d %H:%M:%S} -> {b:%Y-%m-%d %H:%M:%S}  "
                         f"({fmt_dur((b - a).total_seconds())})")
            if len(whole.gaps) > 10:
                L.append(f"  ... {len(whole.gaps) - 10} shorter")
        else:
            L.append("Data gaps     none")
        L += ["", "Files:"]
        L += [f"  {os.path.basename(f.path):22s} {human(f.size):>10s}  "
              f"{'DLD' if f.fmt == 'dld' else 'MiniSEED'}" for f in node.files]
        if node.dld_header:
            h = node.dld_header
            L.append(f"  DLD header: firmware {h['firmware']}, bootloader {h['bootloader']}, "
                     f"leap seconds {'known' if h['leap_known'] else 'NOT yet known (labels 2 s late)'}, "
                     f"position at file close {h['latitude']:.6f}, {h['longitude']:.6f}, "
                     f"{h['altitude']:.1f} m")
            if node.max_sync_age_s is not None:
                L.append(f"  DLD timing: longest time without a GPS sync "
                         f"{fmt_dur(node.max_sync_age_s)} (scanned component)")
        if node.sensor_tests:
            L += ["", "Geophone test (last boot)   R(kOhm)    f0(Hz)  damp    sens    noise"]
            for ch, t in sorted(node.sensor_tests.items()):
                g = lambda v: f"{v:7.3f}" if v is not None else "   -   "
                L.append(f"  Ch{ch}                       {g(t.resistance)} {g(t.resonance_hz)} "
                         f"{g(t.damping)} {g(t.sensitivity)} {g(t.rms_noise)}")
        if node.notes:
            L += ["", "Notes:"] + [f"  - {w}" for w in node.notes]
        if node.warnings:
            L += ["", "Warnings:"] + [f"  - {w}" for w in node.warnings]
        L += ["", "sct_par.xml:"] + [f"  {k} = {v}" for k, v in node.params.items()]
        self.details.setPlainText("\n".join(L))

    # ---------------- map ----------------
    def show_map(self):
        """Rough plot of station positions: deployments in the harvest DB, plus
        loaded deployments that aren't in it yet."""
        from PySide6.QtWidgets import QDialog
        try:
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
        except ImportError as e:
            QMessageBox.warning(self, "Map", f"matplotlib is needed for the map: {e}"); return
        pts = []                                # (lon, lat, label, in_db)
        db = self._db()
        have = set()
        if db:
            for d in db.deployments():
                if d.get("latitude") is not None and d.get("longitude") is not None:
                    pts.append((d["longitude"], d["latitude"], d["station"], True))
                    have.add((d["serial"], d["start"]))
            db.close()
        for n in self.rows.values():
            if n.latitude is None or n.longitude is None:
                continue
            if (n.serial, HarvestDB._t(n.start)) in have:
                continue
            pts.append((n.longitude, n.latitude, n._station, False))
        if not pts:
            QMessageBox.information(self, "Map", "No positions to plot (harvest DB empty and "
                                    "no loaded nodes with coordinates)."); return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Station map - {self.project.name if self.project else 'no project'}")
        dlg.resize(820, 680)
        fig = Figure(figsize=(8, 6.5), tight_layout=True)
        ax = fig.add_subplot(111)
        for in_db, style in ((True, dict(marker="^", s=70, c="tab:blue", edgecolors="k",
                                         label="harvested (DB)")),
                             (False, dict(marker="^", s=70, facecolors="none",
                                          edgecolors="tab:orange", label="loaded, not harvested"))):
            sel = [p for p in pts if p[3] == in_db]
            if sel:
                ax.scatter([p[0] for p in sel], [p[1] for p in sel], zorder=3, **style)
        from collections import defaultdict
        labels = defaultdict(set)               # one label per position/station
        for lon, lat, sta, _ in pts:
            labels[(round(lon, 6), round(lat, 6))].add(sta)
        for (lon, lat), stas in labels.items():
            ax.annotate("/".join(sorted(stas)), (lon, lat), xytext=(4, 4),
                        textcoords="offset points", fontsize=8)
        mlat = sum(p[1] for p in pts) / len(pts)
        ax.set_aspect(1.0 / max(0.05, math.cos(math.radians(mlat))))
        ax.set_xlabel("Longitude (deg)"); ax.set_ylabel("Latitude (deg)")
        ax.ticklabel_format(useOffset=False)
        from matplotlib.ticker import MaxNLocator
        ax.xaxis.set_major_locator(MaxNLocator(5))
        ax.yaxis.set_major_locator(MaxNLocator(6))
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", fontsize=8)
        canvas = FigureCanvasQTAgg(fig)
        lay = QVBoxLayout(dlg)
        lay.addWidget(NavigationToolbar2QT(canvas, dlg))
        lay.addWidget(canvas)
        dlg.show()
        self._map_dialog = dlg

    # ---------------- export ----------------
    def export_table(self):
        import csv
        p, _ = QFileDialog.getSaveFileName(self, "Export node table", "nodes.csv", "CSV (*.csv)")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["serial", "deployment", "station", "script", "firmware", "rotate",
                        "latitude", "longitude", "elevation", "n_fixes", "sample_rate",
                        "gain_db", "start", "end", "compass_north", "tilt", "path"])
            for n in self.rows.values():
                w.writerow([n.serial, f"{n.segment + 1}/{n.n_segments}", n._station,
                            n.script_name, n.firmware, n.rotation, n.latitude, n.longitude,
                            n.elevation, n.n_fixes, n.sample_rate, n.gain_db, n.start, n.end,
                            n.compass_north, n.tilt, n.path])
        self.log(f"Node table exported to {p}")

    # ---------------- checked rows / whole nodes ----------------
    def checked_jobs(self) -> List:
        jobs = []
        for r in range(self.table.rowCount()):
            if self.table.item(r, C_SN).checkState() == Qt.Checked:
                n = self.rows.get(self._key(r))
                if n is not None:
                    jobs.append((n, n._station))
        return jobs

    def checked_bases(self) -> List[Node]:
        """Whole nodes (volumes) behind the checked rows, each once."""
        seen, out = set(), []
        for n, _ in self.checked_jobs():
            b = n.whole
            if b.path not in seen:
                seen.add(b.path)
                out.append(b)
        return out

    # ---------------- presence / unmount ----------------
    def check_present(self, quiet: bool = False) -> List[str]:
        """Remove nodes whose volume was unplugged or whose folder vanished.
        Returns the removed paths."""
        gone = []
        for path, b in list(self.bases.items()):
            if node_present(path) and not (getattr(b, "_volume", False) and
                                           not sys.platform.startswith("win") and
                                           not os.path.ismount(path)):
                continue
            if self._busy() and path in self._run_paths:
                continue                     # the running task reports it itself
            gone.append(path)
            self._remove_base(path, "no longer mounted" if getattr(b, "_volume", False)
                              else "folder no longer exists", quiet)
        return gone

    def _remove_base(self, path, reason="", quiet=False):
        b = self.bases.pop(path, None)
        if b is None:
            return
        self.table.setSortingEnabled(False); self.table.blockSignals(True)
        for r in reversed(range(self.table.rowCount())):
            if self._key(r).startswith(path + "#"):
                self.rows.pop(self._key(r), None)
                self.table.removeRow(r)
        self.table.blockSignals(False); self.table.setSortingEnabled(True)
        self._mark_duplicates()
        if not quiet:
            self.log(f"{b.serial}: {reason} - removed from the table ({path})")

    def unmount_nodes(self, selected_only: bool):
        if self._busy():
            QMessageBox.information(self, "Busy", "Wait for the running task to finish "
                                    "before unmounting."); return
        if selected_only:
            keys = {self._key(i.row()) for i in self.table.selectedItems()}
            bases = {self.rows[k].whole.path: self.rows[k].whole for k in keys if k in self.rows}
            if not bases:
                QMessageBox.information(self, "Unmount", "Select (highlight) the rows of the "
                                        "nodes to unmount."); return
        else:
            bases = dict(self.bases)
        vols = [b for b in bases.values() if getattr(b, "_volume", False)]
        if not vols:
            self.log("Unmount: no USB node volumes among those nodes (folders/archives are "
                     "left alone).")
            return
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for b in vols:
                self._set_status_path(b.path, "unmounting...")
                QApplication.processEvents()
                ok, msg = unmount_volume(b.path)
                if ok:
                    self._remove_base(b.path, "unmounted - safe to remove")
                else:
                    self._set_status_path(b.path, "unmount failed")
                    self.log(f"{b.serial}: could not unmount {b.path}: {msg}")
        finally:
            QGuiApplication.restoreOverrideCursor()

    # ---------------- wipe ----------------
    def wipe_nodes(self):
        if self._busy():
            QMessageBox.information(self, "Busy", "Wait for the running task to finish."); return
        nodes = self.checked_bases()
        skipped = [n for n in nodes if getattr(n, "_archive", "")]
        nodes = [n for n in nodes if not getattr(n, "_archive", "")]
        if skipped:
            self.log("Wipe: skipping node(s) loaded from archives: " +
                     ", ".join(n.serial for n in skipped))
        if not nodes:
            QMessageBox.information(self, "Wipe", "No node volumes checked."); return
        targets = {n.path: wipe_targets(n) for n in nodes}
        nfiles = sum(len(v) for v in targets.values())
        nbytes = sum(os.path.getsize(p) for v in targets.values() for p in v if os.path.exists(p))
        if not nfiles:
            QMessageBox.information(self, "Wipe", "Nothing to delete on the checked nodes."); return

        # harvest safety check: whole node, every file, any rotation, current SDS root
        db = self._db()
        unsafe = [n for n in nodes if n.files and not (db and db.fully_harvested(n))]
        if db:
            db.close()

        def backed_up(n):
            return bool(self.raw_dir()) and os.path.exists(archive_path(n, self.raw_dir()))

        def status(n):
            db = self._db()
            if not db:
                return "?"
            try:
                return db.node_status(n, Settings())
            finally:
                db.close()

        names = "\n".join(f"  {n.serial}  {len(targets[n.path])} files, "
                          f"raw backup: {'yes' if backed_up(n) else 'NO'}" for n in nodes)
        keep = ", ".join(WIPE_KEEP)
        if QMessageBox.question(
                self, "Wipe nodes",
                f"Permanently delete EVERYTHING except {keep} - {nfiles} file(s), "
                f"{mb(nbytes)} MB - from {len(nodes)} node volume(s)?\n\n{names}\n\n"
                f"This cannot be undone.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        if unsafe:
            root = self.ed_sds.text().strip() or "(no SDS folder set)"
            box = QMessageBox(QMessageBox.Warning, "Not harvested",
                              f"{len(unsafe)} node(s) have NOT been fully harvested to\n{root}:\n\n" +
                              "\n".join(f"  {n.serial}  ({status(n)}, raw backup: "
                                        f"{'yes' if backed_up(n) else 'NO'})" for n in unsafe) +
                              "\n\nTheir data will be lost if you wipe them.", parent=self)
            only_safe = box.addButton("Wipe harvested nodes only", QMessageBox.AcceptRole)
            anyway = box.addButton("Wipe ALL anyway", QMessageBox.DestructiveRole)
            cancel = box.addButton(QMessageBox.Cancel)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is cancel or box.clickedButton() is None:
                return
            if box.clickedButton() is only_safe:
                nodes = [n for n in nodes if n not in unsafe]
                if not nodes:
                    self.log("Wipe: no fully harvested nodes checked - nothing wiped."); return
            elif box.clickedButton() is anyway:
                self.log("Wipe: user confirmed wiping NOT fully harvested node(s): " +
                         ", ".join(n.serial for n in unsafe))
        for n in nodes:
            k, freed = wipe_node(n, log=self.log)
            self.log(f"Wiped {n.serial}: {k} file(s), {mb(freed)} MB")
            self._reload_base(n.path, status="wiped")

    def _reload_base(self, path, status=""):
        old = self.bases[path]
        try:
            node = load_node(path)
        except Exception as e:  # noqa
            self.log(f"Failed to re-read {path}: {e}"); return
        if not node.fixes:                       # log deleted: keep what we knew
            node.fixes = old.fixes
            for k in ("latitude", "longitude", "elevation", "n_fixes"):
                setattr(node, k, getattr(old, k))
        node._archive = getattr(old, "_archive", "")
        self.bases[path] = node
        for key in [k for k in self.rows if k.startswith(path + "#")]:
            self._status[key] = status
        self._rebuild_rows()
        for r in range(self.table.rowCount()):
            if self._key(r).startswith(path + "#"):
                self.table.item(r, C_SN).setCheckState(Qt.Unchecked)

    # ---------------- raw archives ----------------
    def _run_task(self, fn, total, label, on_done):
        self.pbar.setRange(0, max(1, total // 1024)); self.pbar.setValue(0)
        self.worker = TaskWorker(fn)
        self.worker.progress.connect(lambda b: self.pbar.setValue(min(b, total) // 1024))
        self.worker.log.connect(self.log)

        def finished(r):
            self.btn_go.setEnabled(True); self.btn_cancel.setEnabled(False)
            if isinstance(r, Cancelled):
                self.log(f"{label}: cancelled.")
            elif isinstance(r, Exception):
                self.log(f"{label}: ERROR {r}")
            else:
                self.pbar.setValue(self.pbar.maximum())
                on_done(r)
        self.worker.done.connect(finished)
        self.btn_go.setEnabled(False); self.btn_cancel.setEnabled(True)
        self.worker.start()

    def load_archives(self):
        if self._busy():
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Load node archives", self.raw_dir(), "Node archives (*.tar.gz *.tgz *.tar)")
        if not files:
            return
        dest = self.extract_dir()
        if not dest:
            QMessageBox.information(self, "Load archive", "Open a project or set the raw data "
                                    "folder first (archives are extracted under it)."); return
        total = sum(archive_total_bytes(f) for f in files)
        self.log(f"Loading {len(files)} archive(s) into {dest}")

        def job(progress, cancelled, log):
            out = []
            for f in files:
                d = extract_archive(f, dest, progress, cancelled, log)
                out.append((f, d))
            return out

        def done(results):
            for f, d in results:
                self.load_paths(find_node_dirs(d, max_depth=3), archive_src=f)
        self._run_task(job, total, "Load archive", done)

    # ---------------- run ----------------
    def start(self):
        if self._busy():
            return
        self.check_present()                    # drop nodes that were unplugged
        jobs = self.checked_jobs()
        if not jobs:
            QMessageBox.information(self, "Nothing to do", "No nodes checked."); return
        root = self.ed_sds.text().strip()
        do_harvest = self.chk_sds.isChecked()
        xml = self.ed_xml.text().strip() if self.chk_xml.isChecked() else ""
        xml_all = self.cb_xml_scope.currentIndex() == 0
        archive = []
        if self.chk_archive.isChecked():
            if not self.raw_dir():
                QMessageBox.information(self, "Archive", "Set the raw data folder (or open a "
                                        "project), or untick 'Archive raw node data'."); return
            archive = [b for b in self.checked_bases()
                       if getattr(b, "_volume", False) and not getattr(b, "_archive", "")
                       and not os.path.exists(archive_path(b, self.raw_dir()))]
            for b in self.checked_bases():
                if not getattr(b, "_volume", False) and not getattr(b, "_archive", ""):
                    self.log(f"{b.serial}: loaded from a folder, not a USB volume - not archived")
        t0, t1 = self.window()
        if do_harvest and not root:
            QMessageBox.information(self, "SDS", "Choose an SDS root folder."); return
        if not do_harvest and not xml and not archive:
            QMessageBox.information(self, "Nothing to do",
                                    "Enable harvesting, StationXML and/or archiving "
                                    "(or the checked nodes are already archived)."); return
        if xml and xml_all and not root:
            QMessageBox.information(self, "StationXML", "'All deployments' needs the SDS folder (harvest DB)."); return
        if do_harvest and t0 and t1 and t1 <= t0:
            QMessageBox.warning(self, "Time range", "'To' must be after 'From'."); return
        tags = [n.job_tag for n, _ in jobs]
        twice = sorted({t for t in tags if tags.count(t) > 1})
        if twice:
            QMessageBox.warning(self, "Same node twice",
                                "These deployments are checked twice (e.g. the volume and its "
                                "archive): " + ", ".join(twice)); return
        # duplicate station codes: overlapping in time = conflict; otherwise confirm
        clash, dup = [], set()
        for i, (a, sa) in enumerate(jobs):
            for b, sb in jobs[i + 1:]:
                if sa != sb:
                    continue
                dup.add(sa)
                if a.start and b.start and a.start <= b.end and b.start <= a.end:
                    clash.append(f"{sa}: {a.job_tag} and {b.job_tag}")
        if clash:
            QMessageBox.warning(self, "Duplicate stations",
                                "The same station code is used for overlapping time spans:\n\n" +
                                "\n".join(clash) + "\n\nRename one of each pair."); return
        if dup and QMessageBox.question(
                self, "Duplicate stations",
                f"Station code(s) {', '.join(sorted(dup))} are used by more than one deployment "
                "(not overlapping in time). They will be written as separate epochs of the "
                "same station. Continue?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        bad = [n.job_tag for n, _ in jobs if not n.gains_consistent]
        if bad and do_harvest:
            QMessageBox.warning(self, "Inconsistent gains",
                                "Channel gains differ on: " + ", ".join(bad) + "\nThese will be skipped.")
        if xml:
            missing = [n.job_tag for n, _ in jobs if n.latitude is None or n.longitude is None]
            if missing:
                QMessageBox.warning(self, "Missing coordinates",
                                    "Enter coordinates for: " + ", ".join(missing)); return
        self._job_bytes = {}          # job id -> bytes done
        self._tag_total = {n.job_tag: n.data_bytes for n, _ in jobs}
        self._tag_key = {n.job_tag: n.key for n, _ in jobs}
        self._archive_total = sum(sum(os.path.getsize(p) for p in _archive_files(b.path))
                                  for b in archive)
        self._archive_done = 0
        total = (sum(self._tag_total.values()) if do_harvest else 0) + self._archive_total
        self.pbar.setRange(0, max(1, total // 1024)); self.pbar.setValue(0)
        self._run_paths = {n.whole.path for n, _ in jobs}
        self.worker = HarvestWorker(jobs, self.settings(), root, do_harvest, xml,
                                    self.sp_workers.value(), xml_all, archive, self.raw_dir())
        self.worker.log.connect(self.log)
        self.worker.event.connect(self._on_event)
        self.worker.done.connect(self._finished)
        self.btn_go.setEnabled(False); self.btn_cancel.setEnabled(True)
        for n, _ in jobs:
            self._set_status(n.key, "queued")
        self.log(transform_description())
        if t0 and t1:
            self.log(f"Time range: {t0} to {t1} (UTC, end exclusive)")
        rot = [f"{n.job_tag}:{n.rotation:g} deg" for n, _ in jobs if n.rotation]
        if rot:
            self.log("Applying rotation to N/E (clockwise +): " + ", ".join(rot))
        self.worker.start()

    def _on_event(self, e):
        kind = e[0]
        if kind == "archive_progress":
            self._archive_done = e[1]
            done = self._archive_done + sum(self._job_bytes.values())
            self.pbar.setValue(min(done // 1024, self.pbar.maximum()))
        elif kind == "status":
            self._set_status_path(e[1], e[2])
        elif kind == "gone":
            self._run_paths.discard(e[1])
            self._remove_base(e[1], "no longer mounted")
        elif kind == "progress":
            jid, b = e[1], e[2]
            self._job_bytes[jid] = b
            done = self._archive_done + sum(self._job_bytes.values())
            self.pbar.setValue(min(done // 1024, self.pbar.maximum()))
            tag = jid.rsplit(".", 1)[0]
            done = sum(v for k, v in self._job_bytes.items() if k.rsplit(".", 1)[0] == tag)
            tot = self._tag_total.get(tag) or 1
            self._set_status(self._tag_key.get(tag, ""), f"harvesting {min(100, 100 * done // tot)}%")
        elif kind == "result":
            r = e[1]
            tag = r["id"].rsplit(".", 1)[0]
            if r.get("error"):
                self._set_status(self._tag_key.get(tag, ""), "error")

    def _set_status(self, key, text):
        r = self._row_for(key)
        if r >= 0:
            self.table.item(r, C_STAT).setText(text)

    def _set_status_path(self, path, text):
        for r in range(self.table.rowCount()):
            if self._key(r).startswith(path + "#"):
                self.table.item(r, C_STAT).setText(text)

    def _finished(self, ok):
        self._run_paths = set()
        self.btn_go.setEnabled(True); self.btn_cancel.setEnabled(False)
        if ok:
            self.pbar.setValue(self.pbar.maximum())
        for r in range(self.table.rowCount()):
            it = self.table.item(r, C_STAT)
            if it.text().startswith(("harvesting", "queued", "archiving")):
                it.setText("done" if ok else "incomplete")
        self.refresh_harvest_status()
        self.log("Finished." if ok else "Stopped (see log).")


def main():
    import multiprocessing
    multiprocessing.freeze_support()     # needed for PyInstaller builds on Windows
    app = QApplication(sys.argv)
    app.setApplicationName("DumbSolo")
    app.setApplicationVersion(__version__)
    w = MainWindow()
    for p in sys.argv[1:]:          # optional: folders on the command line
        w.load_paths(find_node_dirs(p, max_depth=3))
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
