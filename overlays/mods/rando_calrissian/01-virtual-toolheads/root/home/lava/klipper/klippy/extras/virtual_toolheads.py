# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @YOUR-GITHUB-USERNAME

# Virtual toolheads for the Snapmaker U1 (extended firmware)
#
# Calibration data (base_position) and nozzle data are stored per *virtual
# toolhead* instead of per toolhead slot.  The four slots (extruder ..
# extruder3) only point at whichever virtual toolhead is loaded in them.
#
# Design notes
#  - The stock code is left alone.  We observe the stock events instead of
#    intercepting writes:
#      * "probe_inductance_coil: update_extruder_offset" is sent after every
#        calibration save and after SET_EXTRUDER_BASE_POSITION.  We diff the
#        slots' base_position against the store and record any new value.
#      * PrinterExtruder._set_nozzle_diameter/_set_nozzle_volume_type are
#        wrapped so a nozzle change made through the stock UI is mirrored into
#        the virtual toolhead that sits in that slot.
#  - extruder_base_position.json and extruder[x]_nozzle_config.json stay
#    valid stock-format files.  They are regenerated from the store whenever
#    we change the layout, so disabling this module degrades to stock
#    behaviour with whichever toolheads are currently loaded.
#  - A toolhead without valid calibration is loaded into its slot with
#    base_position = None, exactly like a never-calibrated stock slot.

import calendar
import collections
import copy
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
import urllib.request

# Release version of this mod as a whole (see CHANGELOG.md). The store has its
# own SCHEMA_VERSION, which only changes when the file format does.
VERSION = "0.7"
SCHEMA_VERSION = 7
MCU_NAME_FMT = "mcu e%d"     # [mcu e0] .. [mcu e3] of the toolheads
SYSFS_TTY = "/sys/class/tty"
MAX_BACKUPS = 20            # oldest are deleted beyond this
BACKUP_RE = re.compile(r"^vt-\d{8}-\d{6}(-[a-z0-9]+)*\.json$")
STORE_FILENAME = "virtual_toolheads.json"
CAL_LOG_FILENAME = "virtual_toolheads_calibration_log.jsonl"
CAL_LOG_MAX = 512 * 1024      # bytes; the older half moves to ".1"
STOCK_VIEW_FILENAME = "extruder_base_position.json"
OFFSET_EVENT = "probe_inductance_coil: update_extruder_offset"

# Keep in sync with kinematics/extruder.py (VALID_NOZZLE_DIAMETERS etc.)
VALID_DIAMETERS = [0.2, 0.4, 0.6, 0.8]
VALID_VOLUME_TYPES = ['standard', 'high_flow']
# "hardened" has no stock representation; it is only tracked here.
VALID_MATERIALS = ['stainless', 'hardened']

TRACE_IGNORE = ("M105", "M114", "GET_POSITION")   # polling noise
TRACE_MAX_ENTRIES = 400

# Slot 0 is different hardware from slots 1-3: a toolhead made for one side
# does not fit the other.
SIDES = ("left", "right")
SIDE_NAMES = {"left": "Left Slot", "right": "Right Slot"}
SIDE_PREFIX = {"left": "L", "right": "R"}

# Dynamic flow calibration (stock flow_calibrator): the K value (pressure
# advance) depends on filament and nozzle, so it is stored per toolhead and
# filament. Keys: "fil:<Spoolman filament id>", "spool:<Spoolman spool id>"
# (until the filament id is known) or "tag:<vendor>|<type>|<subtype>" for a
# lane without a Spoolman spool.
FLOW_K_MAX = 1.0
FLOW_KEY_RE = re.compile(r'^(fil|spool):[0-9]{1,9}$|^tag:[^\x00-\x1f]{1,80}$')
FLOW_LOOKUP_RETRY = 300.      # s before a failed Spoolman lookup is retried
FLOW_LOOKUP_TIMEOUT = 5.      # s
FLOW_EPS = 1e-9
SWAP_MARKER_TTL = 14400.     # seconds a pending toolhead swap stays valid
SWAP_RESUME_DELAY = 6.      # seconds after ready before the screen opens
AUTO_CAL_TTL = 600.          # s a calibrate request stays open for the card
FILAMENT_ID_WAIT = 5.       # s to wait for the filament to be identified
FILAMENT_ID_POLL = 1.       # s between checks while waiting
STALE_DISMISS = 1.5         # a dismissal this soon after a dialog is stale

# Channel states of the stock filament feeder (filament_feed.py)
FEED_LOADED = 'load_finish'
FEED_NOT_LOADED = ('unload_finish', 'preload_finish', 'wait_insert')
# Lines the stock Z homing prints (G28), kept with each offset run.
HOME_LINES = (("z offset:", "z_offset"),
              ("z_overshoot_comp:", "z_overshoot_comp"),
              ("z_mesh_complete:", "z_mesh_complete"))
# Readable names for the stock feeder states shown to the user.
FEED_STATE_TEXT = {'preload_finish': 'Filament Preloaded',
                   'wait_insert': 'None Loaded'}
FEED_UNLOADING = ('unload_prepare', 'unload_homing', 'unload_picking',
                  'unload_heating', 'unload_heat_finish', 'unload_doing')
FEED_BUSY = ('preload_prepare', 'preload_feeding', 'load_prepare',
             'load_homing', 'load_picking', 'load_heating', 'load_feeding',
             'load_extruding', 'load_flushing', 'manual_sta_prepare',
             'manual_sta_homing', 'manual_sta_picking',
             'manual_sta_prepare_finish', 'manual_sta_heating',
             'manual_sta_extruding', 'manual_sta_extrude_finish',
             'manual_sta_flushing')

LABEL_RE = re.compile(r'^[A-Za-z0-9_-]{1,16}$')
POS_TOL = 1e-6
DIA_TOL = 1e-4

# PID tuning (stored per toolhead, like the nozzle and the base position).
# The stock heater code keeps one JSON file per extruder with named profiles;
# the 'default' profile is the one tuned by PID_CALIBRATE and is the one we
# store, in the same units as that file (Klipper's pid_Kp/Ki/Kd config units).
PID_PROFILE = "default"
PID_PARAM_BASE = 255.          # heaters.PID_PARAM_BASE (read live if present)
PID_REQUIRED = ("Kp", "Ki", "Kd")
PID_OPTIONAL = ("full_power_threshold", "zero_power_threshold",
                "settle_delta", "settle_slope")
PID_TOL = 1e-6                 # relative; below this two tunings are equal
DEFAULT_PID_TEMP = 210.        # standard PLA temperature
PID_TEMP_RANGE = (150., 300.)
PID_TEMP_PRESETS = (190., 200., 210., 220., 230., 250.)

# The module instance of the running Klipper session.  The class-level
# wrappers look this up dynamically so a Klipper RESTART (which keeps the
# imported modules alive) never calls into a stale instance.
_ACTIVE = None


class StoreError(Exception):
    pass


def now_str():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_label(index):
    """0 -> A, 25 -> Z, 26 -> AA, 27 -> AB ..."""
    n = index + 1
    label = ""
    while n:
        n, rem = divmod(n - 1, 26)
        label = chr(65 + rem) + label
    return label


def side_label(side, index):
    """Default label: side prefix + letter index within that side
    (left: LA, LB, ...; right: RA, RB, ...)."""
    return SIDE_PREFIX[side] + default_label(index)


def slot_side(slot):
    """Slot 0 takes Left Slot toolheads, slots 1-3 take Right Slot ones."""
    return "left" if slot == 0 else "right"


def normalize_side(text):
    t = str(text).strip().lower()
    if t in ("left", "l"):
        return "left"
    if t in ("right", "r"):
        return "right"
    raise StoreError("Side must be left or right (got '%s')" % (text,))


def _migrate_v2(data):
    """Schema 2 -> 3: per-toolhead PID tuning.

    No toolhead has a stored tuning yet; the first start adopts the tuning of
    the heaters as they are (see VirtualToolheads._sync_pid)."""
    data = copy.deepcopy(data)
    for th in data["toolheads"].values():
        th.pop("side_assumed", None)
        th.setdefault("pid", None)
    data.setdefault("pid_temp", DEFAULT_PID_TEMP)
    data.setdefault("pid_adopted", False)
    data["schema_version"] = 3
    return data


def _migrate_v3(data):
    """Schema 3 -> 4: dynamic flow (K) values per toolhead and filament."""
    data = copy.deepcopy(data)
    for th in data["toolheads"].values():
        th.setdefault("flow", {})
    data.setdefault("spool_filament", {})
    data.setdefault("flow_adopted", False)
    data["schema_version"] = 4
    return data


def pid_key(target):
    """Key of a stored PID tuning: the calibration temperature in degrees."""
    return str(int(round(float(target))))


def _migrate_v4(data):
    """Schema 4 -> 5: several PID tunings per toolhead, by temperature. The
    tuning a toolhead has (if it was calibrated at a known temperature)
    becomes the first entry of its list."""
    data = copy.deepcopy(data)
    for th in data["toolheads"].values():
        profiles = {}
        pid = th.get("pid")
        if pid and pid.get("source") == "calibrated" and pid.get("target"):
            profiles[pid_key(pid["target"])] = copy.deepcopy(pid)
        th["pid_profiles"] = profiles
    data["schema_version"] = 5
    return data


def _migrate_v5(data):
    """Schema 5 -> 6: the board (USB serial number) of each toolhead, learned
    from the connected hardware."""
    data = copy.deepcopy(data)
    for th in data["toolheads"].values():
        th.setdefault("serial", None)
    data["schema_version"] = 6
    return data


FRAME_SESSION = 300             # s: values saved this close share a frame


def new_id():
    """A random identifier (session, frame).  Identity only: it has no order
    and does not depend on the clock."""
    return str(uuid.uuid4())


def _migrate_v6(data):
    """Schema 6 -> 7: which coordinate frame each stored offset is in.

    An offset calibration measures in the frame the homing before it set,
    and values can only be compared when they share a frame.  Older data
    has no such mark, so the save times are used once, here: values saved
    within FRAME_SESSION of each other (one calibration saves all of its
    toolheads within seconds) share a frame, everything else gets its own
    (which can only flag too much, never too little).  Values that 0.7
    stored relative to T0 ("anchored") join the frame of the toolhead in
    slot 0; those measured without T0 keep their own."""
    data = copy.deepcopy(data)
    ths = data["toolheads"]

    def epoch(t):
        try:
            return calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            return None

    timed, unknown = [], []
    for th_id, th in ths.items():
        c = th["calibration"]
        if c.get("base_position") is None:
            c["frame"] = None
        elif c.get("anchored") is None and epoch(c.get("at")) is not None:
            timed.append((epoch(c["at"]), th_id))
        elif c.get("anchored") is None:
            unknown.append(th_id)           # no time (first-boot import)
    frames = {}
    last, fid = None, None
    for t, th_id in sorted(timed):
        if last is None or t - last > FRAME_SESSION:
            fid = new_id()
        frames[th_id] = fid
        last = t
    if unknown:
        fid = new_id()
        for th_id in unknown:
            frames[th_id] = fid
    layout = data.get("layout") or []
    t0 = layout[0] if layout else None
    for th_id, th in ths.items():
        c = th["calibration"]
        anchored = c.pop("anchored", None)
        if c.get("base_position") is None:
            c["frame"] = None
        elif anchored is True:
            c["frame"] = frames.get(t0) or new_id()
        else:
            c["frame"] = frames.get(th_id) or new_id()
        if c.get("base_position") is not None:
            frames.setdefault(th_id, c["frame"])
        c["session"] = None
        c["format"] = 2 if anchored is True else 1
        c["mod_version"] = "0.7" if anchored is not None else None
    data["schema_version"] = 7
    return data


def read_usb_serial(serialport, sysfs_tty=SYSFS_TTY):
    """USB serial number of the device behind a serial port path (for example
    /dev/serial/by-path/...), or None. The toolhead boards derive it from
    their chip ID, so it stays with the board when the cable moves."""
    try:
        if not isinstance(serialport, str) or not serialport.startswith("/"):
            return None
        name = os.path.basename(os.path.realpath(serialport))
        dev = os.path.realpath(os.path.join(sysfs_tty, name, "device"))
        for _ in range(5):
            path = os.path.join(dev, "serial")
            if os.path.isfile(path):
                with open(path) as f:
                    return f.read().strip() or None
            dev = os.path.dirname(dev)
    except Exception:
        pass
    return None


def short_serial(serial):
    return "..." + serial[-6:] if serial and len(serial) > 6 else serial


def _clean_pid_profiles(raw):
    """Validate a toolhead's {temperature: record} dict."""
    if not isinstance(raw, dict):
        raise StoreError("Bad PID profile list")
    out = {}
    for key, rec in raw.items():
        if not isinstance(key, str) or not key.isdigit():
            raise StoreError("Bad PID profile key %r" % (key,))
        values = _clean_pid_values(rec.get("values"))
        if values is None:
            raise StoreError("Bad PID tuning at %s C" % key)
        out[key] = {"values": values, "target": float(key),
                    "at": str(rec.get("at") or ""),
                    "source": str(rec.get("source") or "calibrated"),
                    "stale": bool(rec.get("stale"))}
    return out


def _clean_flow(raw):
    """Validate a toolhead's {key: entry} flow dict; returns the cleaned
    dict or raises StoreError."""
    if not isinstance(raw, dict):
        raise StoreError("Bad flow data")
    out = {}
    for key, e in raw.items():
        if not isinstance(key, str) or not FLOW_KEY_RE.match(key):
            raise StoreError("Bad flow key %r" % (key,))
        k = e.get("k")
        if not isinstance(k, (int, float)) or isinstance(k, bool) or \
                not 0. < k <= FLOW_K_MAX:
            raise StoreError("Bad flow value for %s" % key)
        _check_nozzle_values(e.get("diameter"), e.get("volume_type"),
                             "stainless")
        out[key] = {"k": float(k), "diameter": _canon_diameter(e["diameter"]),
                    "volume_type": e["volume_type"],
                    "at": str(e.get("at") or ""),
                    "source": str(e.get("source") or "calibrated")}
    return out


def _clean_pid_values(raw):
    """Validate a PID profile dict -> {Kp,Ki,Kd + optionals} (floats), or
    None if it is not a usable tuning."""
    import math
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in PID_REQUIRED:
        try:
            v = float(raw[key])
        except (KeyError, TypeError, ValueError):
            return None
        if not math.isfinite(v) or v < 0:
            return None
        out[key] = v
    if out["Kp"] <= 0:
        return None
    for key in PID_OPTIONAL:
        v = raw.get(key)
        if v is None:
            out[key] = None
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
            return None
        out[key] = v
    return out


def pid_values_equal(a, b):
    if a is None or b is None:
        return a is b
    for key in PID_REQUIRED + PID_OPTIONAL:
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            if x is not y:
                return False
            continue
        if abs(x - y) > PID_TOL * max(1.0, abs(x), abs(y)):
            return False
    return True


def _same_diameter(a, b):
    return abs(float(a) - float(b)) <= DIA_TOL


def _check_nozzle_values(diameter=None, volume_type=None, material=None):
    if diameter is not None and \
            not any(_same_diameter(diameter, d) for d in VALID_DIAMETERS):
        raise StoreError("Invalid nozzle diameter %s (valid: %s)"
                         % (diameter, VALID_DIAMETERS))
    if volume_type is not None and volume_type not in VALID_VOLUME_TYPES:
        raise StoreError("Invalid volume type '%s' (valid: %s)"
                         % (volume_type, VALID_VOLUME_TYPES))
    if material is not None and material not in VALID_MATERIALS:
        raise StoreError("Invalid material '%s' (valid: %s)"
                         % (material, VALID_MATERIALS))


def _canon_diameter(diameter):
    for d in VALID_DIAMETERS:
        if _same_diameter(diameter, d):
            return d
    return diameter


###########################################################################
# Pure data model (no Klipper imports, unit-testable)
###########################################################################

class VirtualToolheadStore:
    def __init__(self, data):
        self.data = data
        self.migrated = False       # set by from_dict when upgraded
        self.migrated_from = None   # the schema version it was upgraded from

    # ---- construction / validation ------------------------------------
    @classmethod
    def from_slots(cls, slots, now=None):
        """Migration: one virtual toolhead per existing slot.

        slots: list of dicts with diameter, volume_type, base_position.
        """
        now = now or now_str()
        data = {
            "schema_version": SCHEMA_VERSION,
            "mod_version": VERSION,
            "epoch": 1,
            "next_id": len(slots) + 1,
            "layout": [],
            "toolheads": {},
            "pid_temp": DEFAULT_PID_TEMP,
            "pid_adopted": False,
            "spool_filament": {},
            "flow_adopted": False,
        }
        per_side = {"left": 0, "right": 0}
        first_frame = new_id()      # imported from one stock calibration
        for i, slot in enumerate(slots):
            th_id = "th%d" % (i + 1)
            bp = slot.get("base_position")
            has_bp = bp is not None and len(bp) >= 3
            side = slot_side(i)
            data["layout"].append(th_id)
            data["toolheads"][th_id] = {
                "label": side_label(side, per_side[side]),
                "side": side,
                "nozzle": {
                    "diameter": _canon_diameter(slot.get("diameter", 0.4)),
                    "volume_type": slot.get("volume_type", "standard"),
                    "material": "stainless",
                    "installed": None,
                },
                "calibration": {
                    "base_position": [float(v) for v in bp[:3]] if has_bp else None,
                    "valid": bool(has_bp),
                    "reason": None if has_bp else "never_calibrated",
                    "at": None,
                    "slot": i if has_bp else None,
                    "epoch": 1,
                    "frame": first_frame if has_bp else None,
                    "session": None,
                    "format": 1,
                    "mod_version": None,
                },
                "pid": None,
                "pid_profiles": {},
                "flow": {},
                "serial": None,
            }
            per_side[side] += 1
        return cls(data)

    @classmethod
    def from_dict(cls, data):
        migrated = False
        try:
            version = data.get("schema_version")
            migrated_from = None
            if version == 2:
                migrated_from = 2
                data = _migrate_v2(data)
                version = 3
                migrated = True
            if version == 3:
                if migrated_from is None:
                    migrated_from = 3
                data = _migrate_v3(data)
                version = 4
                migrated = True
            if version == 4:
                if migrated_from is None:
                    migrated_from = 4
                data = _migrate_v4(data)
                version = 5
                migrated = True
            if version == 5:
                if migrated_from is None:
                    migrated_from = 5
                data = _migrate_v5(data)
                version = 6
                migrated = True
            if version == 6:
                if migrated_from is None:
                    migrated_from = 6
                data = _migrate_v6(data)
                version = 7
                migrated = True
            if version != SCHEMA_VERSION:
                raise StoreError("Unsupported schema_version %r" % (version,))
            for key in ("epoch", "next_id", "layout", "toolheads"):
                if key not in data:
                    raise StoreError("Missing key '%s'" % key)
            ths = data["toolheads"]
            labels = set()
            for th_id, th in ths.items():
                label = th["label"]
                if not LABEL_RE.match(label) or label.lower() in labels:
                    raise StoreError("Bad or duplicate label '%s'" % label)
                labels.add(label.lower())
                if th["side"] not in SIDES:
                    raise StoreError("Bad side '%s' for %s"
                                     % (th["side"], label))
                n = th["nozzle"]
                _check_nozzle_values(n["diameter"], n["volume_type"],
                                     n["material"])
                c = th["calibration"]
                bp = c["base_position"]
                if bp is not None and len(bp) != 3:
                    raise StoreError("Bad base_position for %s" % th_id)
                if th.get("pid") is not None:
                    pid = th["pid"]
                    clean = _clean_pid_values(pid.get("values"))
                    if clean is None:
                        raise StoreError("Bad PID tuning for %s" % label)
                    pid["values"] = clean
                else:
                    th["pid"] = None
                th["flow"] = _clean_flow(th.get("flow", {}))
                th["pid_profiles"] = _clean_pid_profiles(
                    th.get("pid_profiles", {}))
                ser = th.get("serial")
                if ser is not None and (not isinstance(ser, str) or
                                        not ser.strip()):
                    raise StoreError("Bad board serial for %s" % label)
                th["serial"] = ser.strip() if ser else None
            serials = [th["serial"] for th in ths.values() if th["serial"]]
            if len(set(serials)) != len(serials):
                raise StoreError("Two toolheads share a board serial")
            sf = data.get("spool_filament", {})
            if not isinstance(sf, dict) or not all(
                    str(k).isdigit() and isinstance(v, int) and v > 0
                    for k, v in sf.items()):
                raise StoreError("Bad spool_filament map")
            data["spool_filament"] = {str(k): v for k, v in sf.items()}
            data.setdefault("flow_adopted", False)
            pt = data.get("pid_temp", DEFAULT_PID_TEMP)
            if not isinstance(pt, (int, float)) or isinstance(pt, bool) or \
                    not PID_TEMP_RANGE[0] <= pt <= PID_TEMP_RANGE[1]:
                raise StoreError("Bad pid_temp %r" % (pt,))
            data["pid_temp"] = float(pt)
            data.setdefault("pid_adopted", False)
            if len(set(data["layout"])) != len(data["layout"]):
                raise StoreError("Same toolhead loaded in two slots")
            for th_id in data["layout"]:
                if th_id not in ths:
                    raise StoreError("Layout references unknown toolhead %s"
                                     % th_id)
            for slot, th_id in enumerate(data["layout"]):
                if ths[th_id]["side"] != slot_side(slot):
                    raise StoreError(
                        "%s is a %s toolhead but is loaded in slot %d"
                        % (ths[th_id]["label"],
                           SIDE_NAMES[ths[th_id]["side"]], slot))
        except StoreError:
            raise
        except (KeyError, TypeError, AttributeError) as e:
            raise StoreError("Malformed store: %r" % (e,))
        store = cls(data)
        store.migrated = migrated
        store.migrated_from = migrated_from
        return store

    def to_dict(self):
        return copy.deepcopy(self.data)

    # ---- lookup ---------------------------------------------------------
    @property
    def n_slots(self):
        return len(self.data["layout"])

    @property
    def epoch(self):
        return self.data["epoch"]

    @property
    def layout(self):
        return list(self.data["layout"])

    def ids(self):
        return list(self.data["toolheads"].keys())

    def get(self, th_id):
        return self.data["toolheads"][th_id]

    def label(self, th_id):
        return self.data["toolheads"][th_id]["label"]

    def resolve(self, ref):
        """Accept an immutable id or a label (case-insensitive)."""
        if ref in self.data["toolheads"]:
            return ref
        for th_id, th in self.data["toolheads"].items():
            if th["label"].lower() == str(ref).lower():
                return th_id
        raise StoreError("Unknown toolhead '%s'" % (ref,))

    def side(self, th_id):
        return self.data["toolheads"][th_id]["side"]

    def ids_for_slot(self, slot):
        """Toolheads that physically fit `slot`."""
        want = slot_side(slot)
        return [i for i, th in self.data["toolheads"].items()
                if th["side"] == want]

    def check_fit(self, th_id, slot):
        if self.side(th_id) != slot_side(slot):
            raise StoreError(
                "%s is a %s toolhead and cannot be loaded in slot %d "
                "(a %s)" % (self.label(th_id), SIDE_NAMES[self.side(th_id)],
                            slot, SIDE_NAMES[slot_side(slot)]))

    def slot_of(self, th_id):
        try:
            return self.data["layout"].index(th_id)
        except ValueError:
            return None

    def toolhead_in_slot(self, slot):
        return self.data["layout"][slot]

    # ---- boards (USB serial numbers) -------------------------------------
    def serial(self, th_id):
        return self.data["toolheads"][th_id].get("serial")

    def by_serial(self, serial):
        for th_id, th in self.data["toolheads"].items():
            if serial and th.get("serial") == serial:
                return th_id
        return None

    def set_serial(self, th_id, serial):
        """Record the board of a toolhead (None forgets it). A serial belongs
        to one toolhead only."""
        if serial:
            owner = self.by_serial(serial)
            if owner is not None and owner != th_id:
                raise StoreError("That board already belongs to %s"
                                 % self.label(owner))
        self.data["toolheads"][th_id]["serial"] = serial or None

    def clear_serials(self):
        for th in self.data["toolheads"].values():
            th["serial"] = None

    def spare_ids(self):
        loaded = set(self.data["layout"])
        return [i for i in self.data["toolheads"] if i not in loaded]

    # ---- calibration state ----------------------------------------------
    def is_valid(self, th_id):
        c = self.data["toolheads"][th_id]["calibration"]
        return bool(c["valid"] and c["base_position"] is not None
                    and c["epoch"] == self.data["epoch"])

    def frame_of(self, th_id):
        return self.data["toolheads"][th_id]["calibration"].get("frame")

    def frame_stale(self, th_id):
        """True when a valid calibration is in another coordinate frame than
        T0's: it was measured in a session that did not include T0 (or in
        one before the stored T0 was measured).  Its offset may then be off
        by the difference between the two frames (tens of um in Z, more in
        Y).  Offsets measured together with T0 share its frame."""
        ref = self.toolhead_in_slot(0)
        if th_id == ref or not self.is_valid(th_id) or not self.is_valid(ref):
            return False
        return self.frame_of(th_id) != self.frame_of(ref)

    def effective_base_position(self, th_id):
        if not self.is_valid(th_id):
            return None
        return list(self.data["toolheads"][th_id]["calibration"]["base_position"])

    def invalid_loaded(self):
        return [(slot, th_id) for slot, th_id in enumerate(self.data["layout"])
                if not self.is_valid(th_id)]

    def invalid_reason(self, th_id):
        c = self.data["toolheads"][th_id]["calibration"]
        if self.is_valid(th_id):
            return None
        if c["epoch"] != self.data["epoch"]:
            return "epoch"
        return c["reason"] or "invalid"

    def invalidate(self, th_id, reason):
        c = self.data["toolheads"][th_id]["calibration"]
        c["valid"] = False
        c["reason"] = reason

    def invalidate_all(self, reason="manual_all"):
        """Machine-level event (homing/XY calibration changed, new sensor...).
        Bumps the epoch so every stored calibration becomes stale."""
        self.data["epoch"] += 1
        for th_id in self.data["toolheads"]:
            self.invalidate(th_id, reason)

    def stored_base(self, th_id):
        """The stored base_position, valid or not (None if never measured)."""
        bp = self.data["toolheads"][th_id]["calibration"]["base_position"]
        return list(bp) if bp is not None else None

    def differs(self, th_id, pos):
        old = self.data["toolheads"][th_id]["calibration"]["base_position"]
        return not (old is not None and len(old) == 3 and
                    all(abs(a - float(b)) <= POS_TOL
                        for a, b in zip(old, pos[:3])))

    def record_calibration(self, th_id, pos, slot, now=None, frame=None,
                           session=None):
        """Record a base_position seen in a slot.  Returns True if it is new
        (different from what is stored, or in another frame, or the stored
        one was invalid).  `frame` is the coordinate frame the position is
        expressed in (None: a frame of its own)."""
        c = self.data["toolheads"][th_id]["calibration"]
        pos = [float(v) for v in pos[:3]]
        old = c["base_position"]
        if old is not None and len(old) == 3 and \
                frame in (None, c.get("frame")) and \
                all(abs(a - b) <= POS_TOL for a, b in zip(old, pos)):
            return False
        frame = frame or new_id()
        c["base_position"] = pos
        c["valid"] = True
        c["reason"] = None
        c["at"] = now or now_str()
        c["slot"] = slot
        c["epoch"] = self.data["epoch"]
        c["frame"] = frame
        c["session"] = session
        c["format"] = 2
        c["mod_version"] = VERSION
        return True

    # ---- PID tuning ------------------------------------------------------
    @property
    def pid_temp(self):
        return self.data["pid_temp"]

    def set_pid_temp(self, temp):
        try:
            temp = float(temp)
        except (TypeError, ValueError):
            raise StoreError("PID temperature must be a number")
        if not PID_TEMP_RANGE[0] <= temp <= PID_TEMP_RANGE[1]:
            raise StoreError("PID temperature must be between %d and %d C"
                             % PID_TEMP_RANGE)
        self.data["pid_temp"] = temp

    @property
    def pid_adopted(self):
        return bool(self.data.get("pid_adopted"))

    def mark_pid_adopted(self):
        self.data["pid_adopted"] = True

    # ---- dynamic flow (K) per toolhead and filament -----------------------
    @property
    def spool_filament(self):
        return self.data["spool_filament"]

    @property
    def flow_adopted(self):
        return bool(self.data.get("flow_adopted"))

    def mark_flow_adopted(self):
        self.data["flow_adopted"] = True

    def flow_entries(self, th_id):
        return copy.deepcopy(self.data["toolheads"][th_id].get("flow", {}))

    def flow_entry(self, th_id, key, diameter, volume_type):
        """The stored K for this filament, if it was measured with the same
        nozzle diameter and flow type the toolhead has now."""
        e = self.data["toolheads"][th_id].get("flow", {}).get(key)
        if e is None or not _same_diameter(e["diameter"], diameter) or \
                e["volume_type"] != volume_type:
            return None
        return dict(e)

    def record_flow(self, th_id, key, k, diameter, volume_type,
                    source="calibrated", now=None):
        """Store a K value. Returns True if anything changed."""
        if not FLOW_KEY_RE.match(key or ""):
            raise StoreError("Bad flow key %r" % (key,))
        if not isinstance(k, (int, float)) or not 0. < k <= FLOW_K_MAX:
            raise StoreError("Flow value must be between 0 and %g"
                             % FLOW_K_MAX)
        old = self.data["toolheads"][th_id]["flow"].get(key)
        if old is not None and abs(old["k"] - k) < FLOW_EPS and \
                _same_diameter(old["diameter"], diameter) and \
                old["volume_type"] == volume_type:
            return False
        self.data["toolheads"][th_id]["flow"][key] = {
            "k": float(k), "diameter": _canon_diameter(diameter),
            "volume_type": volume_type, "at": now or now_str(),
            "source": source}
        return True

    def forget_flow(self, th_id, key=None):
        flow = self.data["toolheads"][th_id]["flow"]
        if key is None:
            n = len(flow)
            flow.clear()
            return n
        return 1 if flow.pop(key, None) is not None else 0

    def set_spool_filament(self, spool_id, filament_id):
        """Remember which Spoolman filament a spool is. Values stored under
        the spool key move to the filament key (an existing filament value
        wins). Returns True if anything changed."""
        spool_id, filament_id = int(spool_id), int(filament_id)
        changed = self.data["spool_filament"].get(str(spool_id)) != filament_id
        self.data["spool_filament"][str(spool_id)] = filament_id
        old_key, new_key = "spool:%d" % spool_id, "fil:%d" % filament_id
        for th in self.data["toolheads"].values():
            flow = th.get("flow", {})
            if old_key in flow:
                entry = flow.pop(old_key)
                flow.setdefault(new_key, entry)
                changed = True
        return changed

    def pid(self, th_id):
        """The stored PID record of a toolhead, or None."""
        return self.data["toolheads"][th_id].get("pid")

    def pid_values(self, th_id):
        rec = self.pid(th_id)
        return dict(rec["values"]) if rec else None

    def pid_profiles(self, th_id):
        """The stored tunings of a toolhead, {temperature key: record}."""
        return self.data["toolheads"][th_id].setdefault("pid_profiles", {})

    def pid_profile_list(self, th_id):
        """[(temperature, record)] in ascending order."""
        return sorted(((float(k), v)
                       for k, v in self.pid_profiles(th_id).items()))

    def pid_profile_active(self, th_id, key):
        """True if the active tuning is the one stored under that key."""
        rec, prof = self.pid(th_id), self.pid_profiles(th_id).get(key)
        return (rec is not None and prof is not None
                and pid_values_equal(rec["values"], prof["values"]))

    def record_pid(self, th_id, values, target=None, source="calibrated",
                   now=None):
        """Store a PID tuning and make it the active one. A tuning measured
        at a temperature also goes into the toolhead's list, replacing the
        entry of the same temperature. Returns True if anything changed."""
        clean = _clean_pid_values(values)
        if clean is None:
            raise StoreError("Not a usable PID tuning")
        old = self.pid(th_id)
        changed = True
        if old is not None and pid_values_equal(old["values"], clean):
            changed = False
            if source == "calibrated" and target is not None:
                old["target"] = float(target)
                if old.get("stale"):
                    old["stale"] = False
                    changed = True
            rec = old
        else:
            rec = {
                "values": clean,
                "target": float(target) if target is not None else None,
                "at": now or now_str(),
                "source": source,
                "stale": False,
            }
            self.data["toolheads"][th_id]["pid"] = rec
        if source == "calibrated" and target is not None:
            key = pid_key(target)
            prof = self.pid_profiles(th_id).get(key)
            if prof is None or not pid_values_equal(prof["values"],
                                                    rec["values"]):
                changed = True
            self.pid_profiles(th_id)[key] = {
                "values": dict(rec["values"]), "target": float(key),
                "at": rec["at"], "source": "calibrated", "stale": False}
        return changed

    def select_pid(self, th_id, temp):
        """Make the tuning stored for a temperature the active one. Returns
        True if the active tuning changed."""
        prof = self.pid_profiles(th_id).get(pid_key(temp))
        if prof is None:
            raise StoreError("%s has no PID tuning stored for %s C"
                             % (self.label(th_id), pid_key(temp)))
        old = self.pid(th_id)
        same = old is not None and pid_values_equal(old["values"],
                                                    prof["values"])
        self.data["toolheads"][th_id]["pid"] = copy.deepcopy(prof)
        return not same

    def mark_pid_stale(self, th_id):
        """The nozzle changed: the stored tunings are kept but were measured
        with the old nozzle (shown grey until calibrated again)."""
        th = self.data["toolheads"][th_id]
        if th.get("pid"):
            th["pid"]["stale"] = True
        for prof in th.get("pid_profiles", {}).values():
            prof["stale"] = True

    def clear_pid(self, th_id):
        """Forget the active tuning and the list."""
        self.data["toolheads"][th_id]["pid"] = None
        self.data["toolheads"][th_id]["pid_profiles"] = {}

    # ---- structure changes ----------------------------------------------
    def _check_label(self, label, ignore_id=None):
        if not LABEL_RE.match(label or ""):
            raise StoreError("Label must be 1-16 chars of A-Z a-z 0-9 _ -")
        for th_id, th in self.data["toolheads"].items():
            if th_id != ignore_id and th["label"].lower() == label.lower():
                raise StoreError("Label '%s' is already used" % label)

    def next_default_label(self, side="right"):
        used = {th["label"].lower() for th in self.data["toolheads"].values()}
        i = 0
        while side_label(side, i).lower() in used:
            i += 1
        return side_label(side, i)

    def add_toolhead(self, label=None, diameter=0.4, volume_type="standard",
                     material="stainless", side="right", now=None):
        side = normalize_side(side)
        label = label or self.next_default_label(side)
        self._check_label(label)
        _check_nozzle_values(diameter, volume_type, material)
        th_id = "th%d" % self.data["next_id"]
        self.data["next_id"] += 1
        self.data["toolheads"][th_id] = {
            "label": label,
            "side": side,
            "nozzle": {"diameter": _canon_diameter(diameter),
                       "volume_type": volume_type, "material": material,
                       "installed": now or now_str()},
            "calibration": {"base_position": None, "valid": False,
                            "reason": "never_calibrated", "at": None,
                            "slot": None, "epoch": self.data["epoch"],
                            "frame": None, "session": None, "format": 2,
                            "mod_version": VERSION},
            "pid": None,
            "pid_profiles": {},
            "flow": {},
            "serial": None,
        }
        return th_id

    def set_side(self, th_id, side):
        """Fix the side of a spare toolhead. A loaded one cannot change: its
        slot decides. Returns True if the side changed. If the existing
        calibration was measured in a slot of the other side it cannot belong
        to this hardware, so it is invalidated."""
        side = normalize_side(side)
        if th_id in self.data["layout"]:
            raise StoreError(
                "%s is loaded in slot %d; put another toolhead there first"
                % (self.label(th_id), self.slot_of(th_id)))
        th = self.data["toolheads"][th_id]
        if th["side"] == side:
            return False
        th["side"] = side
        cal_slot = th["calibration"]["slot"]
        if isinstance(cal_slot, int) and slot_side(cal_slot) != side:
            self.invalidate(th_id, "side_changed")
        return True

    def rename(self, th_id, label):
        self._check_label(label, ignore_id=th_id)
        self.data["toolheads"][th_id]["label"] = label

    def delete(self, th_id):
        if th_id in self.data["layout"]:
            raise StoreError("Toolhead %s is loaded in slot %d; unload it first"
                             % (self.label(th_id), self.slot_of(th_id)))
        del self.data["toolheads"][th_id]

    def set_layout(self, refs):
        """refs: one toolhead id/label per slot.  Returns changed slot list."""
        if len(refs) != self.n_slots:
            raise StoreError("Layout needs exactly %d entries" % self.n_slots)
        ids = [self.resolve(r) for r in refs]
        if len(set(ids)) != len(ids):
            raise StoreError("A toolhead cannot be loaded in two slots")
        for slot, th_id in enumerate(ids):
            self.check_fit(th_id, slot)
        changed = [i for i, (a, b) in enumerate(zip(self.data["layout"], ids))
                   if a != b]
        self.data["layout"] = ids
        return changed

    def replace_nozzle(self, th_id, diameter=None, volume_type=None,
                       material=None, now=None):
        """A physically new nozzle: always invalidates calibration."""
        _check_nozzle_values(diameter, volume_type, material)
        n = self.data["toolheads"][th_id]["nozzle"]
        if diameter is not None:
            n["diameter"] = _canon_diameter(diameter)
        if volume_type is not None:
            n["volume_type"] = volume_type
        if material is not None:
            n["material"] = material
        n["installed"] = now or now_str()
        self.invalidate(th_id, "nozzle_replaced")
        self.mark_pid_stale(th_id)

    def set_nozzle(self, th_id, diameter=None, volume_type=None, material=None):
        """Correct the nozzle metadata.  Invalidates only if a value changes.
        Returns True if anything changed."""
        _check_nozzle_values(diameter, volume_type, material)
        n = self.data["toolheads"][th_id]["nozzle"]
        changed = False
        if diameter is not None and not _same_diameter(n["diameter"], diameter):
            n["diameter"] = _canon_diameter(diameter)
            changed = True
        if volume_type is not None and n["volume_type"] != volume_type:
            n["volume_type"] = volume_type
            changed = True
        if material is not None and n["material"] != material:
            n["material"] = material
            changed = True
        if changed:
            self.invalidate(th_id, "nozzle_changed")
            self.mark_pid_stale(th_id)
        return changed


###########################################################################
# Atomic file helpers
###########################################################################

def atomic_write_json(path, data, keep_backup=False):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
        f.flush()
        os.fsync(f.fileno())
    if keep_backup and os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            logging.exception("virtual_toolheads: could not write backup")
    os.replace(tmp, path)


###########################################################################
# UI helpers (pure, unit-testable)
###########################################################################

def assign_slot(layout, slot, label):
    """Put `label` into `slot` of a layout (list of labels).  A toolhead that
    already sits in another slot swaps places with the slot's old occupant."""
    new = list(layout)
    if label in new:
        new[new.index(label)] = new[slot]
    new[slot] = label
    return new


class _Params:
    """Stand-in for a command's parameters, to open a page from code."""
    def __init__(self, mod, **params):
        self.error = getattr(mod.gcode, 'error', None) or RuntimeError
        self._p = {k.upper(): str(v) for k, v in params.items()}

    def get(self, name, *default):
        if name in self._p:
            return self._p[name]
        if default:
            return default[0]
        raise self.error("Missing %s" % name)

    def get_float(self, name, *default, **kw):
        return float(self.get(name, *default)) if name in self._p \
            else (default[0] if default else self.get(name))

    def get_int(self, name, *default, **kw):
        return int(self.get(name, *default)) if name in self._p \
            else (default[0] if default else self.get(name))


class Prompt:
    """Builds an action:prompt dialog (Fluidd / Mainsail) as console lines."""
    def __init__(self, title, sticky=False, status=False):
        self.title = title
        self.restart = False        # offers Restart firmware (also shown in the card)
        self.texts = []
        # sticky: the wizard is waiting for the user in the middle of a
        # calibration; the dialog is shown again if it is dismissed.
        # status: progress text without buttons; also published for the
        # dashboard card.
        self.sticky = sticky
        self.status = status
        self.lines = ["prompt_begin " + title]

    def text(self, text):
        self.texts.append(text)
        self.lines.append("prompt_text " + text)

    def buttons(self, buttons, per_row=4):
        """buttons: list of (label, gcode, color)."""
        for i in range(0, len(buttons), per_row):
            self.lines.append("prompt_button_group_start")
            for label, gcode, color in buttons[i:i + per_row]:
                self.lines.append("prompt_button %s|%s|%s"
                                  % (label, gcode, color or "secondary"))
            self.lines.append("prompt_button_group_end")

    def footer(self, label, gcode, color="info"):
        self.lines.append("prompt_footer_button %s|%s|%s"
                          % (label, gcode, color))

    def finish(self):
        return self.lines + ["prompt_show"]


def _cmd(name, page, params):
    parts = [name, "PAGE=%s" % page]
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, float):
            value = "%.1f" % value
        parts.append("%s=%s" % (key.upper(), value))
    return " ".join(parts)


def ui_cmd(page, **params):
    """Build a VT_UI command line for a button."""
    return _cmd("VT_UI", page, params)


def cal_cmd(page, **params):
    """Build a VT_CAL (calibration wizard) command line for a button."""
    return _cmd("VT_CAL", page, params)


UNLOAD_NOTE = ("Note: to swap the USB connections of the toolheads, do it now. "
               "Klipper stops when a cable is pulled; no power cycle is needed. "
               "When you are done, click Restart firmware. The Calibrate screen "
               "opens after the restart if anything needs calibrating.")


SWAP_NOTE = ("Now power off and swap toolheads. Optionally you can do this "
             "without powering down, at your own risk.",
             "When you are done, power on or click Restart Firmware. New "
             "toolheads will be recognized automatically and the Calibration "
             "screen opens automatically if necessary.")


def _unl(on):
    """Button parameter for the layout screens' Unload option (omitted
    when off, so the buttons are unchanged then)."""
    return {'unload': 1} if on else {}


def csv_slots(slots):
    return ",".join(str(i) for i in slots) or "-"


def friendly_error(exc):
    """Stock errors are often '{"coded": ..., "msg": "..."}' strings."""
    text = str(exc)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and data.get("msg"):
            text = data["msg"]
    except ValueError:
        pass
    return " ".join(text.replace("|", "/").split())


###########################################################################
# Klipper glue
###########################################################################

class VirtualToolheads:
    def __init__(self, config):
        global _ACTIVE
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.store_path_cfg = config.get('store_path', None)
        self.print_guard = config.getchoice(
            'print_guard', {'off': 'off', 'warn': 'warn', 'block': 'block'},
            'warn')
        self._trace_orig = None
        self._trace_until = 0.
        self._trace_buf = collections.deque(maxlen=TRACE_MAX_ENTRIES)
        self.guard_calibration = config.getboolean('guard_calibration', True)
        self.store = None
        self.error = None
        self._applying = False
        self._wrapped = False
        self._pid_events = {}        # slot -> PID_CALIBRATE results recorded
        self._pid_hooked = False
        self._flow_hooked = False
        self._seen_results = {}      # stock slot name -> last raw result handled
        self.frame_notes = []        # what the last calibration did about T0
        self._home_hooked = False
        self._home_depth = 0
        self._home_seq = 0
        self.last_home = None        # what the last Z homing (G28) reported
        self._flow_busy = False      # stock FLOW_CALIBRATE is running
        self._flow_outcome = {}      # slot -> result of the last FLOW_CALIBRATE
        self._flow_lookup = config.getboolean('flow_filament_lookup', True)
        self._moonraker_url = config.get('moonraker_url',
                                         'http://127.0.0.1:7125')
        self._lookup_pending = set()
        self._lookup_failed = {}
        _ACTIVE = self

        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        self.printer.register_event_handler("klippy:ready",
                                            self._handle_ready)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        self.printer.register_event_handler(OFFSET_EVENT,
                                            self._handle_offset_event)
        self._resume_after_swap = False
        self._pending_load = []      # slots to load filament in after a swap
        self._identity_dialog = False
        self._detected = []          # board serial seen in each slot
        self._identity_news = None   # what the start-up identification found
        self._sticky = None
        self._notice = None
        self._notice_seq = 0
        self._card = False           # VT_UI CARD=1: results go to the card only
        self._card_seen = None       # the dashboard card announced itself
        self._auto_cal = None        # id of the calibrate request for the card
        self._auto_cal_at = 0.
        self._last_prompt = None
        self._shown_at = 0.
        self._respond_hooked = False
        reg = self.gcode.register_command
        reg("VT_STATUS", self.cmd_VT_STATUS,
            desc="Show virtual toolhead layout and calibration state")
        reg("VT_ADD", self.cmd_VT_ADD,
            desc="Add a spare virtual toolhead [SIDE=left|right, default right]")
        reg("VT_SET_SIDE", self.cmd_VT_SET_SIDE,
            desc="Set which slot type a spare toolhead fits "
                 "(SIDE=left|right; left = slot 0)")
        reg("VT_RENAME", self.cmd_VT_RENAME,
            desc="Rename a virtual toolhead")
        reg("VT_DELETE", self.cmd_VT_DELETE,
            desc="Delete a spare virtual toolhead")
        reg("VT_SET_LAYOUT", self.cmd_VT_SET_LAYOUT,
            desc="Choose which virtual toolhead sits in each slot")
        reg("VT_REPLACE_NOZZLE", self.cmd_VT_REPLACE_NOZZLE,
            desc="A new nozzle was fitted to a toolhead (invalidates calibration)")
        reg("VT_SET_NOZZLE", self.cmd_VT_SET_NOZZLE,
            desc="Correct nozzle data (invalidates only if values change)")
        reg("VT_INVALIDATE", self.cmd_VT_INVALIDATE,
            desc="Mark one toolhead (or ALL) as needing calibration")
        reg("VT_CHECK", self.cmd_VT_CHECK,
            desc="Warn/fail if a loaded toolhead needs calibration")
        self.calibrator = Calibrator(self)
        reg("VT_CALIBRATE", self.calibrator.cmd_VT_CALIBRATE,
            desc="Calibrate only the loaded toolheads that need it "
                 "[TOOLHEADS=A,B] [CONFIRM=1]")
        reg("VT_CAL", self.calibrator.cmd_VT_CAL,
            desc="Calibration wizard steps (used by the dialogs)")
        reg("VT_SET_PID_TEMP", self.cmd_VT_SET_PID_TEMP,
            desc="Set the temperature PID calibration is offered at "
                 "(TEMP=%d..%d C, default %d)"
                 % (PID_TEMP_RANGE[0], PID_TEMP_RANGE[1], DEFAULT_PID_TEMP))
        reg("VT_FLOW", self.cmd_VT_FLOW,
            desc="Show the stored dynamic flow (K) values [TOOLHEAD=]")
        reg("VT_BACKUP", self.cmd_VT_BACKUP,
            desc="Save a backup of all toolhead data [TAG=word]")
        reg("VT_BACKUPS", self.cmd_VT_BACKUPS,
            desc="List the stored backups")
        reg("VT_RESTORE", self.cmd_VT_RESTORE,
            desc="Restore a backup (NAME= from VT_BACKUPS); the current data "
                 "is backed up first")
        reg("VT_BACKUP_DELETE", self.cmd_VT_BACKUP_DELETE,
            desc="Delete a backup (NAME=)")
        reg("VT_SERIALS", self.cmd_VT_SERIALS,
            desc="Show the toolhead boards (USB serial numbers) connected "
                 "in each slot")
        reg("VT_SERIAL_LEARN", self.cmd_VT_SERIAL_LEARN,
            desc="Record the connected boards for the toolheads in the "
                 "current layout")
        reg("VT_SERIAL_FORGET", self.cmd_VT_SERIAL_FORGET,
            desc="Forget the recorded boards [TOOLHEAD=]")
        reg("VT_PID", self.cmd_VT_PID,
            desc="Show the stored PID tunings by temperature [TOOLHEAD=]")
        reg("VT_PID_SELECT", self.cmd_VT_PID_SELECT,
            desc="Use the stored PID tuning of a temperature "
                 "(TOOLHEAD= TEMP=)")
        reg("VT_FLOW_FORGET", self.cmd_VT_FLOW_FORGET,
            desc="Forget stored flow values (TOOLHEAD= [KEY=])")
        reg("VT_RESTART", self.cmd_VT_RESTART, when_not_ready=True,
            desc="Close the dialog and FIRMWARE_RESTART (works after a "
                 "shutdown); used by the Restart firmware button")
        reg("VT_CARD", self.cmd_VT_CARD,
            desc="Used by the dashboard card to announce itself")
        reg("VT_CALIBRATION_LOOPS", self.calibrator.cmd_VT_CALIBRATION_LOOPS,
            desc="Probe each toolhead N times in a row during the offset "
                 "calibration, from the menus too (N=1 is normal; DOCK=1 "
                 "docks the toolhead between probes; resets on restart). "
                 "See VT_CALIBRATION_OPTIONS for CLEAN and REF")
        reg("VT_CALIBRATION_OPTIONS", self.calibrator.cmd_VT_CALIBRATION_OPTIONS,
            desc="Options of the offset calibration: CLEAN=0 skips the "
                 "heating and cleaning steps, REF=0 does not add T0 as the "
                 "reference (both reset on restart; no arguments shows them)")
        reg("VT_CAL_LOG", self.cmd_VT_CAL_LOG,
            desc="Every calibration run is logged; show how much repeated "
                 "runs vary ([TOOLHEAD=] [KIND=offset|pid|flow|store] "
                 "[ACTION=SUMMARY|SLOTS|SHOW|CLEAR] [COUNT=])")
        reg("VT_TRACE", self.cmd_VT_TRACE,
            desc="Debug: trace every G-code command "
                 "(ACTION=ON [MINUTES=15] | OFF | DUMP [COUNT=60] | CLEAR)")
        reg("VT_UI", self.cmd_VT_UI,
            desc="Interactive menus (Fluidd/Mainsail prompts); PAGE=main|layout|nozzle|add")
        self._ui_pages = {
            'main': self._ui_main,
            'layout': self._ui_layout,
            'layout_slot': self._ui_layout_slot,
            'layout_apply': self._ui_layout_apply,
            'swap': self._ui_swap,
            'swap_apply': self._ui_swap_apply,
            'nozzle': self._ui_nozzle_pick,
            'nozzle_pick': self._ui_nozzle_pick,
            'nozzle_form': self._ui_nozzle_form,
            'nozzle_apply': self._ui_nozzle_apply,
            'add': self._ui_add_form,
            'add_form': self._ui_add_form,
            'add_apply': self._ui_add_apply,
            'pid': self._ui_pid_toolheads,
            'pid_pick': self._ui_pid_pick,
            'pid_apply': self._ui_pid_apply,
            'dock': self._ui_dock,
            'boards': self._ui_boards,
            'backup': self._ui_backup,
            'backup_apply': self._ui_backup_apply,
            'restore_pick': self._ui_restore_pick,
            'restore_confirm': self._ui_restore_confirm,
            'restore_apply': self._ui_restore_apply,
            'backup_delete_apply': self._ui_backup_delete_apply,
            'serial': self._ui_serial,
            'serial_new': self._ui_serial_new,
            'serial_is': self._ui_serial_is,
            'serial_learn_apply': self._ui_serial_learn_apply,
            'serial_forget_apply': self._ui_serial_forget_apply,
            'dock_apply': self._ui_dock_apply,
        }

    # ---- helpers ---------------------------------------------------------
    def _slots(self):
        return self.printer.lookup_object('extruder_list', [])

    # ---- calibration run log ---------------------------------------------
    def _cal_log_path(self):
        return os.path.join(os.path.dirname(self._store_path()),
                            CAL_LOG_FILENAME)

    def log_calibration(self, kind, slot, **data):
        """Append one calibration run to the log, whatever the store did with
        it (the store ignores repeats; this log keeps them so the run-to-run
        variation can be studied)."""
        try:
            if self.store is None:
                return
            th_id = self.store.toolhead_in_slot(slot)
            ex = self._slots()[slot]
            rec = {"t": now_str(), "kind": kind, "slot": slot, "ver": VERSION,
                   "toolhead": self.store.label(th_id), "id": th_id,
                   "nozzle": getattr(ex, 'nozzle_diameter', None),
                   "volume": getattr(ex, 'nozzle_volume_type', None)}
            rec.update(data)
            path = self._cal_log_path()
            try:
                if os.path.getsize(path) > CAL_LOG_MAX:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except Exception:
            logging.exception("virtual_toolheads: could not log a "
                              "calibration run")

    def _read_cal_log(self):
        rows = []
        for path in (self._cal_log_path() + ".1", self._cal_log_path()):
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
        return rows

    CAL_LOG_FIELDS = {"offset": ("x", "y", "z"),
                      "pid": ("kp", "ki", "kd"),
                      "flow": ("k",)}

    @staticmethod
    def _cal_group(r):
        if r["kind"] == "pid":
            return "%s %s @%s" % (r["toolhead"], "pid", r.get("target"))
        if r["kind"] == "flow":
            return "%s flow %s" % (r["toolhead"], r.get("key"))
        return "%s offset" % r["toolhead"]

    def cal_log_summary(self, rows):
        """Lines: per toolhead/kind the number of runs and the spread."""
        groups = collections.OrderedDict()
        for r in rows:
            if r.get("kind") == "store":
                continue                # what was stored, not a probe result
            if r.get("kind") == "flow" and r.get("outcome") in ("none", "default"):
                continue
            groups.setdefault(self._cal_group(r), []).append(r)
        lines = []
        for name, rs in groups.items():
            kind = rs[0]["kind"]
            parts = []
            for fld in self.CAL_LOG_FIELDS[kind]:
                vals = [r[fld] for r in rs if r.get(fld) is not None]
                if not vals:
                    continue
                n = len(vals)
                mean = sum(vals) / n
                sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** .5 \
                    if n > 1 else 0.
                parts.append("%s mean %.4f sd %.4f range %.4f" % (
                    fld, mean, sd, max(vals) - min(vals)))
            slots = sorted({r["slot"] for r in rs if r.get("slot") is not None})
            where = ""
            if slots:
                where = " (%s %s)" % ("slot" if len(slots) == 1 else "slots",
                                      ", ".join("T%d" % v for v in slots))
            lines.append("%s%s, %d run%s: %s" % (
                name, where, len(rs), "" if len(rs) == 1 else "s",
                "; ".join(parts)))
        return lines

    @staticmethod
    def _epoch(t):
        try:
            return calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            return 0

    def cal_log_blocks(self, rows):
        """Offset runs grouped into sessions: consecutive runs of one toolhead
        in one slot.  A new session starts at loop 1, after a gap of more than
        10 minutes, or when the toolhead or slot changes."""
        blocks = []
        last = None
        for r in rows:
            if r.get("kind") != "offset":
                continue
            key = (r["toolhead"], r.get("slot"))
            at = self._epoch(r.get("t"))
            if last is None or key != last[0] or r.get("loop") == 1 \
                    or at - last[1] > 600:
                blocks.append((key, []))
            blocks[-1][1].append(r)
            last = (key, at)
        return blocks

    @staticmethod
    def _sd(v):
        if len(v) < 2:
            return 0.
        m = sum(v) / len(v)
        return (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** .5

    @staticmethod
    def _slope(v):
        n = len(v)
        mi = (n - 1) / 2.
        mv = sum(v) / n
        den = sum((i - mi) ** 2 for i in range(n))
        return sum((i - mi) * (x - mv) for i, x in enumerate(v)) / den

    def cal_log_slot_report(self, rows):
        """Per toolhead and slot: the noise inside a session, the spread
        between sessions and the drift per run; then the shift between slots
        measured against that noise."""
        axes = ("x", "y", "z")
        slots = collections.OrderedDict()
        for key, rs in self.cal_log_blocks(rows):
            slots.setdefault(key, []).append(rs)
        stats = {}
        lines = []
        for (who, slot), blocks in sorted(slots.items(),
                                          key=lambda kv: (kv[0][0],
                                                          kv[0][1] or 0)):
            n = sum(len(b) for b in blocks)
            loops = sorted({"%s%s%s" % (
                b[0].get("loops") or 1,
                "+dock" if any(r.get("dock") for r in b) else "",
                ", not cleaned" if b[0].get("cleaned") is False else "")
                for b in blocks})
            lines.append("%s slot T%s: %d runs in %d session%s (loops: %s)" % (
                who, slot, n, len(blocks), "" if len(blocks) == 1 else "s",
                ", ".join(loops)))
            st = {}
            for a in axes:
                within = [b for b in blocks if len(b) > 1]
                num = sum((len(b) - 1) * self._sd([r[a] for r in b]) ** 2
                          for b in within)
                den = sum(len(b) - 1 for b in within)
                noise = (num / den) ** .5 if den else None
                means = [sum(r[a] for r in b) / len(b) for b in blocks]
                mean = sum(r[a] for b in blocks for r in b) / n
                between = self._sd(means) if len(means) > 1 else None
                slopes = [self._slope([r[a] for r in b]) for b in blocks
                          if len(b) >= 3]
                drift = sum(slopes) / len(slopes) if slopes else None
                st[a] = (mean, noise, between, means)
                lines.append(
                    "  %s: mean %.4f, noise %s, between sessions %s, "
                    "drift %s" % (
                        a, mean,
                        "n/a" if noise is None else "%.4f" % noise,
                        "n/a" if between is None else "%.4f" % between,
                        "n/a" if drift is None else
                        "%+.1f um/run" % (drift * 1000)))
            stats[(who, slot)] = st
        by_who = collections.OrderedDict()
        for (who, slot) in stats:
            by_who.setdefault(who, []).append(slot)
        for who, sl in by_who.items():
            sl = sorted(s_ for s_ in sl if s_ is not None)
            for other in sl[1:]:
                ref = sl[0]
                parts = []
                for a in axes:
                    d = stats[(who, other)][a][0] - stats[(who, ref)][a][0]
                    # scatter of a slot: the larger of the noise inside a
                    # session and the spread between sessions
                    ns = []
                    for k in (ref, other):
                        _m, nz, bt, _ms = stats[(who, k)][a]
                        if nz is not None or bt is not None:
                            ns.append(max(nz or 0., bt or 0.))
                    if ns:
                        scatter = (sum(v * v for v in ns) / len(ns)) ** .5
                        ratio = (" = %.1fx scatter" % (abs(d) / scatter)
                                 if scatter > 0 else "")
                    else:
                        ratio = ""
                    parts.append("%s %+.4f%s" % (a, d, ratio))
                lines.append("%s slot shift T%d vs T%d: %s" % (
                    who, other, ref, "; ".join(parts)))
        return lines

    def cmd_VT_CAL_LOG(self, gcmd):
        action = gcmd.get('ACTION', 'SUMMARY').upper()
        if action == 'CLEAR':
            for path in (self._cal_log_path(), self._cal_log_path() + ".1"):
                try:
                    os.remove(path)
                except OSError:
                    pass
            gcmd.respond_info("Calibration log cleared")
            return
        rows = self._read_cal_log()
        who = gcmd.get('TOOLHEAD', None)
        kind = gcmd.get('KIND', None)
        if who:
            rows = [r for r in rows if r.get("toolhead") == who]
        if kind:
            rows = [r for r in rows if r.get("kind") == kind.lower()]
        if not rows:
            gcmd.respond_info("No calibration runs logged yet. Log file: %s"
                              % self._cal_log_path())
            return
        if action == 'SLOTS':
            lines = self.cal_log_slot_report(rows)
            gcmd.respond_info("\n".join(lines) if lines else
                              "No offset runs logged yet")
            return
        if action == 'SHOW':
            count = gcmd.get_int('COUNT', 30, minval=1)
            for r in rows[-count:]:
                if r.get("kind") == "store":
                    slot = r.get("slot")
                    gcmd.respond_info(
                        "%s %s%s stored (%s) shift um=%s frame %s session %s"
                        % (r["t"], r["toolhead"],
                           "" if slot is None else " (T%d)" % slot,
                           r.get("how"),
                           [round(v * 1000.) for v in r.get("shift") or []],
                           str(r.get("frame"))[:8],
                           str(r.get("session"))[:8]))
                    continue
                vals = ", ".join("%s=%s" % (f, r.get(f)) for f in
                                 self.CAL_LOG_FIELDS[r["kind"]])
                extra = r.get("outcome") or r.get("target") or ""
                if r.get("loops"):
                    extra = "loop %d/%d%s" % (
                        r.get("loop", 0), r["loops"],
                        ", docked" if r.get("dock") else "")
                slot = r.get("slot")
                gcmd.respond_info("%s %s%s %s %s %s" % (
                    r["t"], r["toolhead"],
                    "" if slot is None else " (T%d)" % slot,
                    r["kind"], vals, extra))
            return
        gcmd.respond_info("\n".join(self.cal_log_summary(rows) + [
            "%d runs logged (log file: %s)" % (len(rows),
                                               self._cal_log_path())]))

    def _store_path(self):
        if self.store_path_cfg:
            return self.store_path_cfg
        base = self.printer.get_snapmaker_config_dir("persistent")
        return os.path.join(base, STORE_FILENAME)

    def _view_path(self):
        bak = self.printer.lookup_object('extruder_config_bak', None)
        if bak is not None and getattr(bak, 'base_position_config_path', None):
            return bak.base_position_config_path
        return os.path.join(self.printer.get_snapmaker_config_dir(),
                            STOCK_VIEW_FILENAME)

    def _save(self):
        self.store.data["mod_version"] = VERSION
        atomic_write_json(self._store_path(), self.store.to_dict(),
                          keep_backup=True)

    def _require_store(self, gcmd):
        if self.store is None:
            raise gcmd.error("Virtual toolheads unavailable: %s"
                             % (self.error or "not initialised yet"))

    def _require_idle(self, gcmd):
        ps = self.printer.lookup_object('print_stats', None)
        if ps is not None and getattr(ps, 'state', None) in ('printing',
                                                              'paused'):
            raise gcmd.error("Not allowed while printing")

    def _slot_label(self, slot):
        th = self.store.toolhead_in_slot(slot)
        return self.store.label(th)

    # ---- startup ---------------------------------------------------------
    # ---- resume after a toolhead swap ------------------------------------
    # After "Unload filament" the user may swap toolhead USB cables, which
    # stops Klipper, and click Restart firmware.  A small marker file records
    # that this was intended, so the restart opens the calibrate screen.
    def _swap_marker(self):
        return self._store_path() + ".swap"

    def _mark_swap_pending(self, load=()):
        try:
            with open(self._swap_marker(), "w") as f:
                json.dump({"at": time.time(),
                           "load": sorted(int(v) for v in load)}, f)
        except Exception:
            logging.exception("virtual_toolheads: could not write the swap "
                              "marker")

    def _consume_swap_marker(self):
        """True if a fresh marker was there (it is removed either way)."""
        path = self._swap_marker()
        load = []
        try:
            with open(path) as f:
                data = json.load(f)
            at = float(data.get("at", 0))
            load = [int(v) for v in data.get("load", [])]
        except Exception:
            at = None
        try:
            os.remove(path)
        except OSError:
            pass
        fresh = at is not None and 0 <= time.time() - at <= SWAP_MARKER_TTL
        # Slots whose filament is loaded again once the restart has settled.
        self._pending_load = load if fresh else []
        return fresh

    LOST_MCU = re.compile(r"Lost communication with MCU '(e(\d+))'")

    def _handle_shutdown(self):
        """A toolhead MCU went silent.  If that is not part of a swap we
        started, the likely cause is a toolhead or nozzle changed while the
        printer was on: ask, and offer the firmware restart."""
        try:
            msg = self.printer.get_state_message()[0]
        except Exception:
            return
        m = self.LOST_MCU.search(msg or "")
        if m is None or self.store is None:
            return
        try:
            if self._swap_marker_fresh():
                return          # the Swap Tool flow expects this
            slot = int(m.group(2))
            if slot < self.store.n_slots:
                who = "T%d (%s)" % (slot, self.store.label(
                    self.store.layout[slot]))
            else:
                who = "toolhead MCU %s" % m.group(1)
            self.trace_note("lost MCU %s: restart dialog" % m.group(1))
            self._ui_restart_lost(m.group(1), who)
        except Exception:
            logging.exception("virtual_toolheads: could not show the lost "
                              "MCU dialog")

    def _swap_marker_fresh(self):
        try:
            with open(self._swap_marker()) as f:
                at = float(json.load(f).get("at", 0))
        except Exception:
            return False
        return 0 <= time.time() - at <= SWAP_MARKER_TTL

    def _ui_restart_lost(self, mcu, who):
        p = Prompt("Lost communication with MCU '%s'" % mcu)
        p.text("Klipper lost contact with %s." % who)
        p.text("Did you hot-swap a toolhead or nozzle without using Swap "
               "Tool? If so, restart the firmware: swapped toolheads are "
               "recognised on restart.")
        p.text("If you did not touch it, check the toolhead cable first.")
        p.buttons([("Restart firmware", "VT_RESTART", "warning")],
                  per_row=1)
        p.restart = True
        p.buttons([("Close", ui_cmd("close"), "secondary")])
        self._show(p)

    def _handle_ready(self):
        if not self._resume_after_swap and not self._identity_news:
            return
        resume, self._resume_after_swap = self._resume_after_swap, False
        try:
            reactor = self.printer.get_reactor()
        except Exception:
            self._after_restart(resume)
            return

        def fire(eventtime):
            self._after_restart(resume)
            return reactor.NEVER
        reactor.register_timer(fire, reactor.monotonic() + SWAP_RESUME_DELAY)

    def _after_restart(self, resume=False):
        """What to show once Klipper is ready after a restart: the result of
        recognising the toolhead boards, else (after a layout swap) the
        calibrate screen if something needs it."""
        news, self._identity_news = self._identity_news, None
        if resume and news is not None and not news["unknown"] and \
                not news["conflicts"] and self.store is not None and \
                self.calibrator._auto_open_state() == 'now':
            news = None      # straight to the calibrate screen
        self._identity_dialog = False
        if news is not None and self.store is not None:
            try:
                if self._show_identity(news) and self._identity_dialog:
                    # a dialog needs the user's attention first
                    self._drop_pending_load("a toolhead dialog needs "
                                            "attention first")
                    return
            except Exception:
                logging.exception("virtual_toolheads: could not show the "
                                  "toolhead board dialog")
        self._open_calibrate_after_swap()
        self._load_after_restart()

    # ---- load filament after a swap --------------------------------------
    def _drop_pending_load(self, why):
        slots, self._pending_load = self._pending_load, []
        if slots:
            self.gcode.respond_info(
                "Filament load skipped for %s: %s." % (
                    ", ".join("T%d" % v for v in slots), why))

    def _load_after_restart(self):
        """Load filament in the slots chosen in the Swap flow.  When a
        calibration is offered the load waits for it (PID tuning has to start
        from a cold nozzle and a load heats it)."""
        if not self._pending_load or self.store is None:
            return
        try:
            offered = self.calibrator._auto_open_state() == 'now'
        except Exception:
            offered = False
        if offered:
            self.gcode.respond_info(
                "Filament load for %s waits until the calibration is "
                "finished or cancelled." % ", ".join(
                    "T%d" % v for v in self._pending_load))
            return
        lines = self._run_pending_load()
        if lines:
            self._close_dialog()
            self.gcode.respond_info("\n".join(lines))

    def _close_dialog(self):
        self._sticky = None
        self._notice = None
        self._last_prompt = None
        self.gcode.respond_raw("// action:prompt_end")

    def _run_pending_load(self):
        """Load filament in the slots left in `_pending_load`; returns the
        report lines.  Slots without filament waiting, busy ones and everything
        during a print are skipped and said so."""
        slots, self._pending_load = self._pending_load, []
        if not slots:
            return []
        if self._printing():
            return ["Filament load skipped: a print is running."]
        lines = []
        todo = []
        for slot in slots:
            state = self.slot_filament(slot)
            if state == 'loaded':
                lines.append("T%d: filament already loaded" % slot)
            elif state == 'none':
                lines.append("T%d: no filament to load (feeder state: %s)"
                             % (slot, self._feeder_state_text(slot)))
            elif state in ('unloading', 'busy'):
                lines.append("T%d: skipped, the feeder is busy" % slot)
            else:
                todo.append(slot)
        for n, slot in enumerate(todo):
            self._status_screen("Loading filament", [
                "Loading T%d: %s (%d of %d)." % (
                    slot, self.store.label(self.store.layout[slot]),
                    n + 1, len(todo)),
                "The nozzle heats up and the filament is fed in. This can "
                "take a few minutes, please wait."])
            try:
                self.gcode.run_script_from_command(
                    "AUTO_FEEDING EXTRUDER=%d LOAD=1" % slot)
                lines.append("T%d: filament loaded" % slot)
            except Exception as e:
                logging.exception("virtual_toolheads: load T%d", slot)
                lines.append("T%d: load failed (%s)" % (slot, e))
        return lines

    def _open_calibrate_after_swap(self, deadline=None):
        """Go straight to the calibrate screen if any toolhead in the layout
        lacks calibration (offset, PID or flow); otherwise do nothing.

        A toolhead whose only gap is the flow K of a filament that is not
        identified yet (the spool is read a few seconds after a restart) is
        waited for, up to FILAMENT_ID_WAIT seconds. If it is still unknown
        then the screen stays closed: the card's Calibrate button shows
        while the gap lasts."""
        if self.store is None:
            return
        state = self.calibrator._auto_open_state()
        if state is None:
            return
        if state == 'wait':
            try:
                reactor = self.printer.get_reactor()
                now = reactor.monotonic()
            except Exception:
                return
            if deadline is None:
                deadline = now + FILAMENT_ID_WAIT
            if now >= deadline:
                self.trace_note("filament not identified: calibrate screen "
                                "not opened")
                return

            def again(eventtime):
                self._open_calibrate_after_swap(deadline)
                return reactor.NEVER
            reactor.register_timer(again, now + FILAMENT_ID_POLL)
            return
        if self._card_seen is not None:
            # The card opens its own calibrate panel (no dialog).
            self._auto_cal = int(time.time() * 1000)
            self._auto_cal_at = time.monotonic()
            self.trace_note("calibrate request for the card")
            return
        try:
            self.gcode.run_script_from_command("VT_CALIBRATE")
        except Exception:
            logging.exception("virtual_toolheads: could not open the "
                              "calibrate screen after the swap")

    def _handle_connect(self):
        logging.info("virtual_toolheads v%s starting", VERSION)
        try:
            exts = self._slots()
            if not exts:
                raise StoreError("No extruders found")
            path = self._store_path()
            if os.path.exists(path):
                with open(path) as f:
                    self.store = VirtualToolheadStore.from_dict(json.load(f))
                if self.store.migrated:
                    # Upgraded from an older format: keep the original once.
                    backup = "%s.v%s" % (path, self.store.migrated_from)
                    if not os.path.exists(backup):
                        shutil.copyfile(path, backup)
                    self._save()
                    logging.info("virtual_toolheads: upgraded %s to schema "
                                 "%d (original kept as %s)", path,
                                 SCHEMA_VERSION, backup)
            else:
                slots = [{"diameter": e.nozzle_diameter,
                          "volume_type": e.nozzle_volume_type,
                          "base_position": e.base_position} for e in exts]
                self.store = VirtualToolheadStore.from_slots(slots)
                self._save()
                logging.info("virtual_toolheads: migrated %d slots to %s",
                             len(exts), path)
            if self.store.n_slots != len(exts):
                raise StoreError("Store has %d slots, printer has %d"
                                 % (self.store.n_slots, len(exts)))
            try:
                news = self._identify()
                if news["learned"] or news["changed"] or news["unknown"] \
                        or news["conflicts"]:
                    self._identity_news = news
            except Exception:
                logging.exception("virtual_toolheads: could not identify the "
                                  "toolhead boards")
            self._wrap_setters(exts[0])
            self._sync_pid()
            self._sync_flow()
            self.apply_to_slots(runtime=False)
            try:
                self._install_pid_hook()
            except Exception:
                logging.exception("virtual_toolheads: could not hook "
                                  "PID_CALIBRATE; PID calibration will not "
                                  "be tracked")
            try:
                self._install_home_hook()
            except Exception:
                logging.exception("virtual_toolheads: could not hook G28; "
                                  "homing values will not be logged")
            try:
                self._install_flow_hooks()
            except Exception:
                logging.exception("virtual_toolheads: could not hook the "
                                  "flow calibration commands; flow values "
                                  "will not be tracked")
            try:
                self._install_respond_hook()
            except Exception:
                logging.exception("virtual_toolheads: could not hook "
                                  "RESPOND; dismissed dialogs stay closed")
            self._resume_after_swap = self._consume_swap_marker()
            if not self.guard_calibration:
                self.calibrator.guard_info = "disabled in config"
            else:
                try:
                    self.calibrator.install_guards()
                except Exception as e:
                    logging.exception("virtual_toolheads: could not guard "
                                      "the stock calibration commands")
                    self.calibrator.guard_info = "FAILED (%s)" % (e,)
        except Exception as e:
            # Never stop Klipper from starting; fall back to stock behaviour.
            logging.exception("virtual_toolheads: disabled")
            self.error = str(e)
            self.store = None

    # ---- backup / restore ----------------------------------------------------
    # Copies of the whole store (nozzles, offsets, PID, flow K, boards, layout)
    # kept next to it in <store>.backups/, named vt-YYYYMMDD-HHMMSS[-tag].json.
    def _backup_dir(self):
        return self._store_path() + ".backups"

    def _backup_names(self):
        """Backup file names, newest first. The folder is listed again only
        when its modification time changed (the status asks often)."""
        d = self._backup_dir()
        try:
            stamp = os.stat(d).st_mtime_ns
        except OSError:
            self._bk_names = None
            return []
        cached = getattr(self, "_bk_names", None)
        if cached is not None and cached[0] == stamp and cached[1] == d:
            return list(cached[2])
        try:
            names = [n for n in os.listdir(d) if BACKUP_RE.match(n)]
        except OSError:
            return []
        names = sorted(names, reverse=True)
        self._bk_names = (stamp, d, names)
        return list(names)

    def _backup_path(self, name):
        if not BACKUP_RE.match(str(name)):
            raise StoreError("Bad backup name '%s'" % (name,))
        path = os.path.join(self._backup_dir(), name)
        if not os.path.isfile(path):
            raise StoreError("No backup named '%s'" % name)
        return path

    @staticmethod
    def _backup_when(name):
        m = re.match(r"^vt-(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)(\d\d)", name)
        return "%s-%s-%s %s:%s:%s" % m.groups() if m else name

    def _make_backup(self, tag=None):
        tag = re.sub(r"[^a-z0-9]+", "", str(tag or "").lower())[:20]
        os.makedirs(self._backup_dir(), exist_ok=True)
        t = time.time()
        while True:
            name = "vt-%s%s.json" % (time.strftime("%Y%m%d-%H%M%S",
                                                   time.localtime(t)),
                                     "-" + tag if tag else "")
            if not os.path.exists(os.path.join(self._backup_dir(), name)):
                break
            t += 1          # same second: take the next, keeps the order
        self.store.data["mod_version"] = VERSION
        atomic_write_json(os.path.join(self._backup_dir(), name),
                          self.store.to_dict())
        self._bk_names = None
        for old in self._backup_names()[MAX_BACKUPS:]:
            try:
                os.remove(os.path.join(self._backup_dir(), old))
            except OSError:
                pass
        self._bk_names = None
        return name

    def _read_backup(self, name):
        with open(self._backup_path(name)) as f:
            return VirtualToolheadStore.from_dict(json.load(f))

    def _backup_summary(self, name):
        try:
            st = self._read_backup(name)
        except Exception as e:
            return "unreadable (%s)" % (e,)
        return "layout " + ", ".join(st.label(i) for i in st.layout)

    def _restore(self, name):
        """Replace the store by a backup; the current one is backed up first.
        Returns the report lines."""
        new = self._read_backup(name)
        if new.n_slots != self.store.n_slots:
            raise StoreError("That backup has %d slots, the printer has %d"
                             % (new.n_slots, self.store.n_slots))
        saved = self._make_backup("beforerestore")
        self.store = new
        self._save()
        news = self._identify()
        self.apply_to_slots(runtime=True)
        lines = ["Restored %s." % self._backup_when(name),
                 "The data before the restore was saved as %s." % saved,
                 "Layout: " + ", ".join(self.store.label(i)
                                        for i in self.store.layout)]
        lines += ["Layout follows the connected toolheads: " + c
                  for c in news["changed"]]
        return lines

    def _ui_backup(self, gcmd):
        names = self._backup_names()
        p = Prompt("Backup and restore")
        p.text("A backup is a copy of all toolhead data: nozzles, offsets, "
               "PID, flow K, boards and layout.")
        p.text("%d backup%s stored (the newest %d are kept)."
               % (len(names), "" if len(names) == 1 else "s", MAX_BACKUPS))
        buttons = [("Back up now", ui_cmd("backup_apply"), "primary")]
        if names:
            buttons.append(("Restore...", ui_cmd("restore_pick"),
                            "secondary"))
        p.buttons(buttons, per_row=2)
        p.footer("Back", self._ui("main"))
        self._show(p)

    def _ui_backup_apply(self, gcmd):
        self._require_idle(gcmd)
        name = self._make_backup(gcmd.get('TAG', None))
        self._ui_result("Backup", ["Saved %s." % self._backup_when(name)])

    def _ui_restore_pick(self, gcmd):
        names = self._backup_names()[:8]
        p = Prompt("Restore a backup")
        if not names:
            p.text("There are no backups yet.")
        else:
            p.text("Pick a backup to look at (newest first).")
            p.buttons([(self._backup_when(n), ui_cmd("restore_confirm",
                                                       name=n), "secondary")
                       for n in names], per_row=2)
        p.footer("Back", ui_cmd("backup"))
        self._show(p)

    def _ui_restore_confirm(self, gcmd):
        name = gcmd.get('NAME')
        try:
            self._backup_path(name)
        except StoreError as e:
            raise gcmd.error(str(e))
        p = Prompt("Restore %s?" % self._backup_when(name))
        p.text(self._backup_summary(name))
        p.text("This replaces all current toolhead data. A backup of the "
               "current data is made first.")
        p.buttons([("Restore", ui_cmd("restore_apply", name=name), "warning"),
                   ("Delete this backup",
                    ui_cmd("backup_delete_apply", name=name), "secondary")],
                  per_row=2)
        p.footer("Back", ui_cmd("restore_pick"))
        self._show(p)

    def _ui_restore_apply(self, gcmd):
        self._require_idle(gcmd)
        name = gcmd.get('NAME')
        lines = self._store_op(gcmd, lambda: self._restore(name))
        self._ui_result("Backup restored",
                        lines + self._calibration_lines())

    def _ui_backup_delete_apply(self, gcmd):
        name = gcmd.get('NAME')
        path = self._store_op(gcmd, lambda: self._backup_path(name))
        os.remove(path)
        self._bk_names = None
        self._ui_result("Backup", ["Deleted %s." % self._backup_when(name)])

    def cmd_VT_BACKUP(self, gcmd):
        self._require_store(gcmd)
        name = self._make_backup(gcmd.get('TAG', None))
        gcmd.respond_info("Saved backup %s" % name)

    def cmd_VT_BACKUPS(self, gcmd):
        self._require_store(gcmd)
        names = self._backup_names()
        gcmd.respond_info("\n".join(
            ["%d backup(s) in %s:" % (len(names), self._backup_dir())] +
            ["  %s  %s" % (n, self._backup_summary(n)) for n in names]))

    def cmd_VT_RESTORE(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        lines = self._store_op(gcmd, lambda: self._restore(gcmd.get('NAME')))
        gcmd.respond_info("\n".join(lines))

    def cmd_VT_BACKUP_DELETE(self, gcmd):
        self._require_store(gcmd)
        name = gcmd.get('NAME')
        os.remove(self._store_op(gcmd, lambda: self._backup_path(name)))
        self._bk_names = None
        gcmd.respond_info("Deleted backup %s" % name)

    # ---- toolhead boards (USB serial numbers) ------------------------------
    # Each toolhead board has its own USB serial number, which stays with the
    # board when its cable moves to another port. Klipper's [mcu eN] is bound
    # to a port, so the serial seen on each port says which toolhead Klipper
    # really has in each slot, whatever was declared.
    def _detect_serials(self):
        out = []
        for slot in range(self.store.n_slots):
            mcu = self.printer.lookup_object(MCU_NAME_FMT % slot, None)
            port = getattr(mcu, '_serialport', None) if mcu else None
            out.append(read_usb_serial(port) if port else None)
        return out

    def _plan_identity(self, detected):
        """Work out what to do with the serials seen in the slots. Nothing is
        changed here. Returns a dict: learn [(slot, th_id, serial)], layout
        (a new layout or None), unknown [(slot, serial)], conflicts [text]."""
        s = self.store
        plan = {"learn": [], "layout": None, "unknown": [], "conflicts": []}
        if not any(detected):
            return plan
        owners = {}
        for th_id in s.ids():
            if s.serial(th_id):
                owners[s.serial(th_id)] = th_id
        for slot, ser in enumerate(detected):
            if ser and ser not in owners:
                th_id = s.toolhead_in_slot(slot)
                if s.serial(th_id) is None and th_id not in owners.values():
                    plan["learn"].append((slot, th_id, ser))
                    owners[ser] = th_id
        desired = s.layout
        for slot, ser in enumerate(detected):
            if not ser:
                continue
            if ser not in owners:
                plan["unknown"].append((slot, ser))
            else:
                desired[slot] = owners[ser]
        if plan["unknown"] or desired == s.layout:
            return plan
        if len(set(desired)) != len(desired):
            plan["conflicts"].append(
                "The connected boards do not match the stored layout and "
                "cannot be arranged (a board is missing or appears twice). "
                "Check the toolhead cables.")
            return plan
        for slot, th_id in enumerate(desired):
            if s.side(th_id) != slot_side(slot):
                plan["conflicts"].append(
                    "%s is a %s toolhead but its board is connected as "
                    "T%d (a %s). Check the toolhead cables." % (
                        s.label(th_id), SIDE_NAMES[s.side(th_id)], slot,
                        SIDE_NAMES[slot_side(slot)]))
        if not plan["conflicts"]:
            plan["layout"] = desired
        return plan

    def _identify(self):
        """Recognise the connected toolhead boards: learn the ones of the
        toolheads that have none yet, and follow the layout the hardware
        has. Saves the store if it changed; the caller applies it."""
        s = self.store
        detected = self._detect_serials()
        self._detected = detected
        plan = self._plan_identity(detected)
        news = {"learned": [], "changed": [], "unknown": plan["unknown"],
                "conflicts": plan["conflicts"]}
        for slot, th_id, ser in plan["learn"]:
            s.set_serial(th_id, ser)
            news["learned"].append((slot, s.label(th_id), ser))
        if plan["layout"]:
            old = s.layout
            s.set_layout(plan["layout"])
            for slot, (a, b) in enumerate(zip(old, s.layout)):
                if a != b:
                    news["changed"].append("T%d: %s -> %s" % (
                        slot, s.label(a), s.label(b)))
        if news["learned"] or news["changed"]:
            self._save()
        return news

    def _boards_available(self):
        return any(self._detected)

    def _show_identity(self, news):
        """Dialog for the result of the start-up identification. Returns True
        if something was shown."""
        s = self.store
        self._identity_dialog = True
        if news["unknown"]:
            self._ui_serial_unknown_for(news["unknown"][0][0], news=news)
            return True
        lines = []
        if news["changed"]:
            lines.append("The toolheads connected now differ from the "
                         "stored layout. The layout was updated:")
            lines += news["changed"]
        if news["learned"]:
            lines.append("Recorded the boards of these toolheads:")
            lines += ["T%d: %s (board %s)" % (slot, label, short_serial(ser))
                      for slot, label, ser in news["learned"]]
        lines += news["conflicts"]
        if not lines:
            return False
        if news["learned"] and not news["changed"]:
            p = Prompt("Toolhead boards recorded")
            for line in lines:
                p.text(line)
            p.text("From now on swapping toolheads and cables is "
                   "detected on restart. If this is not the layout you "
                   "have, click Undo and fix the layout first.")
            p.buttons([("Looks right", ui_cmd("close"), "primary"),
                       ("Undo", ui_cmd("serial_forget_apply"), "secondary")])
            self._show(p)
        elif news["conflicts"]:
            lines += self._calibration_lines()
            self._ui_result("Toolheads detected", lines)
        else:
            # Expected outcome of a swap: a status message, no dialog.
            self._identity_dialog = False
            self.trace_note("toolheads detected: status message")
            self.gcode.respond_info("\n".join(lines))
        return True

    def _unknown_candidates(self, slot):
        """Toolheads an unrecognised board in `slot` may be: those whose own
        board is not connected (or that have none), the slot's current
        toolhead first."""
        s = self.store
        present = set(x for x in self._detected if x)
        cur = s.toolhead_in_slot(slot)
        out = []
        for th_id in [cur] + [i for i in s.ids() if i != cur]:
            if s.serial(th_id) in present:
                continue
            if s.side(th_id) != slot_side(slot):
                continue
            out.append(th_id)
        return out

    def _ui_serial_unknown_for(self, slot, news=None, intro=None):
        s = self.store
        ser = self._detected[slot] if slot < len(self._detected) else None
        p = Prompt("New toolhead board in T%d" % slot)
        for line in (intro or []):
            p.text(line)
        p.text("An unrecognised toolhead board (%s) is connected as T%d."
               % (short_serial(ser), slot))
        p.text("Is it a new toolhead, or a replacement for one you "
               "already have?")
        buttons = [("New toolhead", ui_cmd("serial_new", slot=slot),
                    "primary")]
        for th_id in self._unknown_candidates(slot):
            word = "This is" if s.serial(th_id) is None else "Replaces"
            buttons.append(("%s %s" % (word, s.label(th_id)),
                            ui_cmd("serial_is", slot=slot,
                                   toolhead=s.label(th_id)), "secondary"))
        p.buttons(buttons, per_row=2)
        p.footer("Decide later", ui_cmd("close"))
        self._show(p)

    def _ui_serial(self, gcmd):
        self._require_idle(gcmd)
        self._detected = self._detect_serials()
        plan = self._plan_identity(self._detected)
        slot = gcmd.get_int('SLOT', None)
        if slot is None and plan["unknown"]:
            slot = plan["unknown"][0][0]
        if slot is None:
            self._ui_boards(gcmd)
            return
        self._ui_serial_unknown_for(slot)

    def _serial_slot(self, gcmd):
        slot = gcmd.get_int('SLOT', minval=0, maxval=self.store.n_slots - 1)
        self._detected = self._detect_serials()
        ser = self._detected[slot]
        if not ser:
            raise gcmd.error("No toolhead board is connected as T%d" % slot)
        if self.store.by_serial(ser) is not None:
            raise gcmd.error("That board is already known as %s"
                             % self.store.label(self.store.by_serial(ser)))
        return slot, ser

    def _serial_done(self, lines):
        """After a board was assigned: follow the boards that are known now,
        then ask about the next unknown one or show the result."""
        news = self._identify()
        self.apply_to_slots(runtime=True)
        lines = list(lines)
        for line in news["changed"]:
            lines.append("Layout: " + line)
        plan = self._plan_identity(self._detected)
        if plan["unknown"]:
            self._ui_serial_unknown_for(plan["unknown"][0][0], intro=lines)
        else:
            self._ui_result("Toolhead board", lines + self._calibration_lines())

    def _ui_serial_new(self, gcmd):
        self._require_idle(gcmd)
        slot, ser = self._serial_slot(gcmd)
        s = self.store
        nozzle = s.get(s.toolhead_in_slot(slot))["nozzle"]
        th_id = self._store_op(gcmd, lambda: s.add_toolhead(
            side=slot_side(slot), diameter=nozzle["diameter"],
            volume_type=nozzle["volume_type"], material=nozzle["material"]))
        s.set_serial(th_id, ser)
        layout = s.layout
        displaced = layout[slot]
        layout[slot] = th_id
        self._store_op(gcmd, lambda: s.set_layout(layout))
        self._save()
        self._identify()
        self.apply_to_slots(runtime=True)
        intro = ["Added %s as T%d; %s is now a spare." % (
            s.label(th_id), slot, s.label(displaced)),
            "Which nozzle does it have? (Shown: a copy of the one it "
            "replaces.)"]
        self._ui_nozzle_form(_Params(self, TOOLHEAD=s.label(th_id),
                                     MODE="edit"), intro=intro)

    def _ui_serial_is(self, gcmd):
        self._require_idle(gcmd)
        slot, ser = self._serial_slot(gcmd)
        s = self.store
        th_id = self._get_th(gcmd)
        replaced = s.serial(th_id) is not None
        s.set_serial(th_id, None)
        s.set_serial(th_id, ser)
        layout = s.layout
        j = s.slot_of(th_id)
        if j is not None:
            layout[j], layout[slot] = layout[slot], th_id
        else:
            layout[slot] = th_id
        self._store_op(gcmd, lambda: s.set_layout(layout))
        lines = ["%s is now T%d." % (s.label(th_id), slot)]
        if replaced:
            s.invalidate(th_id, "board_replaced")
            lines.append("Its board was replaced, so its offset needs "
                         "calibrating again. Nozzle, PID and flow values "
                         "were kept.")
        self._save()
        self._serial_done(lines)

    def _unknown_boards_lines(self):
        try:
            plan = self._plan_identity(self._detect_serials())
        except Exception:
            return []
        if not plan["unknown"]:
            return []
        return ["Another unrecognised toolhead board is connected: open "
                "Menu > Boards > Identify new board."]

    def _board_lines(self):
        s = self.store
        lines = []
        for slot in range(s.n_slots):
            ser = self._detected[slot] if slot < len(self._detected) else None
            th_id = s.by_serial(ser) if ser else None
            if ser is None:
                lines.append("T%d: no board identified" % slot)
            elif th_id is None:
                lines.append("T%d: unrecognised board %s"
                             % (slot, short_serial(ser)))
            else:
                lines.append("T%d: %s (board %s)"
                             % (slot, s.label(th_id), short_serial(ser)))
        known = [i for i in s.ids() if s.serial(i)]
        absent = [s.label(i) for i in known
                  if s.serial(i) not in self._detected]
        if absent:
            lines.append("Boards not connected: " + ", ".join(absent))
        return lines

    def _ui_boards(self, gcmd):
        self._detected = self._detect_serials()
        p = Prompt("Toolhead boards")
        if not self._boards_available():
            p.text("The toolhead boards cannot be identified on this "
                   "printer.")
        else:
            for line in self._board_lines():
                p.text(line)
            p.text("Boards are recognised on every restart, so swapping "
                   "toolheads and USB cables needs no extra steps.")
        buttons = []
        if self._plan_identity(self._detected)["unknown"]:
            buttons.append(("Identify new board", ui_cmd("serial"),
                            "warning"))
        buttons += [("Add a spare (not connected)", ui_cmd("add"),
                     "secondary"),
                    ("Record the connected boards",
                     ui_cmd("serial_learn_apply"), "secondary"),
                    ("Forget all boards", ui_cmd("serial_forget_apply"),
                     "secondary")]
        p.buttons(buttons, per_row=1)
        p.footer("Back", self._ui("main"))
        self._show(p)

    def cmd_VT_SERIALS(self, gcmd):
        self._require_store(gcmd)
        self._detected = self._detect_serials()
        gcmd.respond_info("\n".join(
            ["Toolhead boards:"] + self._board_lines()))

    def _serial_learn(self, gcmd):
        """Take the layout as it is: record the board connected in each slot
        for the toolhead the store has there. Returns the report lines."""
        self._require_store(gcmd)
        self._require_idle(gcmd)
        self._detected = self._detect_serials()
        if not self._boards_available():
            return ["No toolhead boards can be identified on this printer."]
        s = self.store
        s.clear_serials()
        lines = ["Recorded:"]
        for slot, ser in enumerate(self._detected):
            if ser:
                th_id = s.toolhead_in_slot(slot)
                self._store_op(gcmd, lambda: s.set_serial(th_id, ser))
                lines.append("T%d: %s (board %s)"
                             % (slot, s.label(th_id), short_serial(ser)))
        self._save()
        return lines

    def _serial_forget(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        if gcmd.get('TOOLHEAD', None):
            self.store.set_serial(self._get_th(gcmd), None)
        else:
            self.store.clear_serials()
        self._save()
        return ["Forgot the recorded toolhead boards."]

    def cmd_VT_SERIAL_LEARN(self, gcmd):
        gcmd.respond_info("\n".join(self._serial_learn(gcmd)))

    def cmd_VT_SERIAL_FORGET(self, gcmd):
        gcmd.respond_info("\n".join(self._serial_forget(gcmd)))

    def _ui_serial_learn_apply(self, gcmd):
        self._ui_result("Toolhead boards", self._serial_learn(gcmd))

    def _ui_serial_forget_apply(self, gcmd):
        self._ui_result("Toolhead boards", self._serial_forget(gcmd))

    def _close_dialog(self):
        self._sticky = None
        self._notice = None
        self._last_prompt = None
        self.gcode.respond_raw("// action:prompt_end")

    # ---- applying the store to the live slots ---------------------------
    def _apply_nozzle(self, ex, nozzle):
        changed = False
        if not _same_diameter(ex.nozzle_diameter, nozzle["diameter"]):
            ex._set_nozzle_diameter(nozzle["diameter"], False)
            changed = True
        if ex.nozzle_volume_type != nozzle["volume_type"]:
            ex._set_nozzle_volume_type(nozzle["volume_type"], False)
            changed = True
        if changed:
            try:
                ex.nozzle_config_info['version'] = \
                    self.printer.get_start_args().get('software_version')
            except Exception:
                pass
            if not self.printer.update_snapmaker_config_file(
                    ex.nozzle_config_path, ex.nozzle_config_info):
                logging.error("virtual_toolheads: failed to save %s",
                              ex.nozzle_config_path)

    def apply_to_slots(self, runtime=True):
        exts = self._slots()
        self._applying = True
        try:
            for i, ex in enumerate(exts):
                th_id = self.store.toolhead_in_slot(i)
                self._apply_nozzle(ex, self.store.get(th_id)["nozzle"])
                self._apply_pid(ex, th_id)
                self._apply_flow(i, ex, live=runtime)
                bp = self.store.effective_base_position(th_id)
                ex.base_position = list(bp) if bp is not None else None
                if bp is None:
                    # same state as a never-calibrated stock slot
                    ex.gcode_offset = [0, 0, 0] if ex.name == 'extruder' \
                        else None
            for ex in exts:
                ex.update_extruder_gcode_offset()
            self._write_slot_view()
            if runtime:
                self.printer.send_event(OFFSET_EVENT)
                self._reactivate_offset()
        finally:
            self._applying = False

    def _reactivate_offset(self):
        # Every extruder reacts to the offset event; make sure the one that
        # is actually active ends up with its own offset applied.
        try:
            th = self.printer.lookup_object('toolhead')
            ex = th.get_extruder()
            if hasattr(ex, 'active_gcode_offset'):
                ex.active_gcode_offset()
        except Exception:
            logging.exception("virtual_toolheads: could not re-apply offset")

    # ---- PID tuning ------------------------------------------------------
    # Stock keeps one file per extruder (snapmaker/extruder[x]_pid_parameters
    # .json, profiles default/pid2/pid3, values in config units) which the
    # heater controller reads once at startup. We store the 'default' profile
    # per virtual toolhead, and on a layout change write the toolhead's tuning
    # into the slot's controller and file the same way stock does.
    def _pid_base(self):
        for name in ('extras.heaters', 'heaters'):
            base = getattr(sys.modules.get(name), 'PID_PARAM_BASE', None)
            if base:
                return float(base)
        return PID_PARAM_BASE

    def _heater_control(self, ex):
        try:
            heater = ex.get_heater()
        except Exception:
            return None, None
        control = getattr(heater, 'control', None)
        profiles = getattr(control, 'pid_profiles', None)
        if not isinstance(profiles, dict) or PID_PROFILE not in profiles:
            return heater, None
        return heater, control

    def _live_pid(self, ex):
        """The tuning the slot's heater is using now (config units)."""
        _, control = self._heater_control(ex)
        if control is None:
            return None
        prof = control.pid_profiles[PID_PROFILE]
        base = self._pid_base()
        raw = dict(prof)
        try:
            for key in PID_REQUIRED:
                raw[key] = prof[key] * base
        except (KeyError, TypeError):
            return None
        return _clean_pid_values(raw)

    def _file_pid(self, control):
        path = getattr(control, 'json_filename', None)
        if not path:
            return None
        try:
            with open(path) as f:
                return _clean_pid_values(json.load(f).get(PID_PROFILE))
        except Exception:
            return None

    def _control_profile(self, rec, old, base):
        prof = {key: rec[key] / base for key in PID_REQUIRED}
        for key, fallback in (("settle_delta", 2.), ("settle_slope", .5)):
            value = rec.get(key)
            if value is None:
                value = (old or {}).get(key, fallback)
            prof[key] = value
        for key in ("full_power_threshold", "zero_power_threshold"):
            prof[key] = rec.get(key)
        return prof

    def _apply_pid(self, ex, th_id):
        """Make the slot's heater use the toolhead's stored tuning. Toolheads
        without a stored tuning leave the heater as it is."""
        rec = self.store.pid_values(th_id)
        if rec is None:
            return False
        try:
            heater, control = self._heater_control(ex)
            if control is None:
                logging.warning("virtual_toolheads: %s has no PID profiles; "
                                "tuning not applied", ex.name)
                return False
            live = self._live_pid(ex)
            if live is not None and pid_values_equal(live, rec):
                return False
            base = self._pid_base()
            old = control.pid_profiles[PID_PROFILE]
            control.pid_profiles[PID_PROFILE] = \
                self._control_profile(rec, old, base)
            if getattr(control, 'current_profile', PID_PROFILE) == PID_PROFILE:
                control.set_pid_profile(PID_PROFILE)
            self._save_pid_file(control, rec, base)
            logging.info("virtual_toolheads: %s PID tuning applied to %s "
                         "(Kp %.3f Ki %.3f Kd %.3f)",
                         self.store.label(th_id), ex.name,
                         rec["Kp"], rec["Ki"], rec["Kd"])
            return True
        except Exception:
            logging.exception("virtual_toolheads: could not apply the PID "
                              "tuning of %s to %s", self.store.label(th_id),
                              ex.name)
            return False

    def _save_pid_file(self, control, rec, base):
        """Persist the profiles the way stock does, so a restart (and the
        stock UI) sees the same tuning."""
        if getattr(control, 'ignore_pid_json', False):
            return
        path = getattr(control, 'json_filename', None)
        if not path:
            return
        profiles = {}
        for name, prof in control.pid_profiles.items():
            if name == PID_PROFILE:
                profiles[name] = {k: rec.get(k) for k in
                                  PID_REQUIRED + PID_OPTIONAL}
                continue
            entry = dict(prof)
            for key in PID_REQUIRED:
                entry[key] = prof[key] * base
            profiles[name] = entry
        save = getattr(control, '_save_heater_pid_profiles_to_json', None)
        if callable(save):
            save(path, profiles)
        else:
            atomic_write_json(path, profiles)

    def _sync_pid(self):
        """Startup: the heaters just loaded their files. A toolhead that has
        no stored tuning adopts what its slot is using (once, ever); one
        whose stored tuning differs from the slot's was retuned outside this
        module (touchscreen, edited file) and takes the slot's tuning."""
        s = self.store
        changed = False
        seen_all = True
        for slot, ex in enumerate(self._slots()):
            live = self._live_pid(ex)
            if live is None:
                seen_all = False
                continue
            th_id = s.toolhead_in_slot(slot)
            rec = s.pid_values(th_id)
            if rec is None:
                if not s.pid_adopted:
                    s.record_pid(th_id, live, None, "adopted")
                    changed = True
            elif not pid_values_equal(rec, live):
                logging.info("virtual_toolheads: PID tuning of %s changed "
                             "outside the module; taking the slot's",
                             s.label(th_id))
                s.record_pid(th_id, live, None, "observed")
                changed = True
        if seen_all and not s.pid_adopted:
            s.mark_pid_adopted()
            changed = True
        if changed:
            self._save()

    def _install_pid_hook(self):
        """Record the result of every stock PID_CALIBRATE (wizard, console or
        touchscreen) for the toolhead that sits in that heater's slot."""
        if self._pid_hooked:
            return
        old = self.gcode.register_command('PID_CALIBRATE', None)
        if old is None:
            logging.warning("virtual_toolheads: PID_CALIBRATE not found; "
                            "PID calibration will not be tracked")
            return
        helps = getattr(self.gcode, 'gcode_help', {})
        desc = helps.get('PID_CALIBRATE')

        def hooked(gcmd):
            result = old(gcmd)
            try:
                self._after_pid_calibrate(gcmd)
            except Exception:
                logging.exception("virtual_toolheads: PID hook failed")
            return result
        self.gcode.register_command('PID_CALIBRATE', hooked, desc=desc)
        self._pid_hooked = True

    def _after_pid_calibrate(self, gcmd):
        if self.store is None:
            return
        name = gcmd.get('HEATER', None)
        slots = self._slots()
        idx = next((i for i, e in enumerate(slots) if e.name == name), None)
        if idx is None:
            return
        ex = slots[idx]
        _, control = self._heater_control(ex)
        if control is None:
            return
        th_id = self.store.toolhead_in_slot(idx)
        try:
            run = self._file_pid(control) or self._live_pid(ex)
            if run is not None:
                self.log_calibration(
                    "pid", idx, target=gcmd.get_float('TARGET', None),
                    kp=run.get("Kp"), ki=run.get("Ki"), kd=run.get("Kd"))
        except Exception:
            logging.exception("virtual_toolheads: PID run not logged")
        rec = self.store.pid_values(th_id)
        chosen = None
        for cand in (self._file_pid(control), self._live_pid(ex)):
            if cand is not None and (rec is None
                                     or not pid_values_equal(cand, rec)):
                chosen = cand
                break
        if chosen is None:
            logging.warning("virtual_toolheads: PID_CALIBRATE on %s produced "
                            "no new tuning", name)
            return
        self.store.record_pid(th_id, chosen, gcmd.get_float('TARGET', None),
                              "calibrated")
        self._pid_events[idx] = self._pid_events.get(idx, 0) + 1
        self._save()
        self._apply_pid(ex, th_id)
        logging.info("virtual_toolheads: recorded PID tuning for %s (slot %d)",
                     self.store.label(th_id), idx)

    def _pid_text(self, th_id):
        rec = self.store.pid(th_id)
        if rec is None:
            return "no PID tuning"
        if rec.get("target"):
            return "PID %.0fC" % rec["target"]
        return "PID stored"

    def _select_pid(self, th_id, temp):
        """Activate the stored tuning of a temperature; the heater of the
        slot the toolhead sits in switches to it at once. Returns a text."""
        changed = self.store.select_pid(th_id, temp)
        self._save()
        slot = self.store.slot_of(th_id)
        if slot is not None:
            self._apply_pid(self._slots()[slot], th_id)
        return "%s: PID tuning for %s C %s" % (
            self.store.label(th_id), pid_key(temp),
            "is now in use" if changed else "was already in use")

    def cmd_VT_PID(self, gcmd):
        self._require_store(gcmd)
        s = self.store
        if gcmd.get('TOOLHEAD', None) is not None:
            ids = [self._get_th(gcmd)]
        else:
            ids = list(s.layout) + s.spare_ids()
        lines = []
        for th_id in ids:
            lines.append("%s  (%s)" % (s.label(th_id), self._where(th_id)))
            profiles = s.pid_profile_list(th_id)
            if not profiles:
                lines.append("  no stored PID tunings by temperature")
            for temp, prof in profiles:
                v = prof["values"]
                lines.append("  %3.0f C  Kp %.3f Ki %.3f Kd %.3f  %s%s" % (
                    temp, v["Kp"], v["Ki"], v["Kd"], prof["at"],
                    "  <- in use" if s.pid_profile_active(
                        th_id, pid_key(temp)) else ""))
        gcmd.respond_info("\n".join(lines))

    def cmd_VT_PID_SELECT(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        gcmd.respond_info(self._store_op(
            gcmd, lambda: self._select_pid(th_id, gcmd.get_float('TEMP'))))

    def cmd_VT_SET_PID_TEMP(self, gcmd):
        self._require_store(gcmd)
        self._store_op(gcmd, lambda: self.store.set_pid_temp(
            gcmd.get_float('TEMP')))
        self._save()
        gcmd.respond_info("PID calibration temperature: %.0f C"
                          % self.store.pid_temp)

    # ---- dynamic flow (K) per toolhead and filament -------------------------
    # Stock flow_calibrator keeps one K (pressure advance) per extruder in
    # snapmaker/flow_calibrator.json ('factor') and applies it with
    # FLOW_APPLY_CALIBRATE_K at print start. K depends on the filament and the
    # nozzle, so a toolhead remembers one K per filament; when a toolhead gets
    # a filament it has a K for, that K is put back into the slot's entry
    # before stock applies it.
    def _flow_calibrator(self):
        fc = self.printer.lookup_object('flow_calibrator', None)
        if fc is None or not isinstance(getattr(fc, '_current_k', None), dict):
            return None
        return fc

    def _printing(self):
        ps = self.printer.lookup_object('print_stats', None)
        return ps is not None and getattr(ps, 'state', None) in ('printing',
                                                                  'paused')

    def _task_status(self):
        ptc = self.printer.lookup_object('print_task_config', None)
        try:
            return dict(ptc.get_status()) if ptc is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _listed(st, name, slot):
        try:
            return st.get(name)[slot]
        except (TypeError, IndexError, KeyError, AttributeError):
            return None

    def _slot_spool_id(self, slot, st):
        try:
            value = int(self._listed(st, 'filament_spool_id', slot) or 0)
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    def _filament_key(self, slot, st=None, lookup=True):
        """Identify the filament in a slot: the Spoolman filament id when
        known, else the spool id, else vendor/type/subtype of the tag."""
        st = self._task_status() if st is None else st
        spool = self._slot_spool_id(slot, st)
        if spool:
            fid = self.store.spool_filament.get(str(spool))
            if fid:
                return "fil:%d" % fid
            if lookup:
                self._request_filament_lookup(spool)
                fid = self.store.spool_filament.get(str(spool))
                if fid:
                    return "fil:%d" % fid
            return "spool:%d" % spool
        main = self._listed(st, 'filament_type', slot)
        if not main or str(main).upper() in ('NONE', ''):
            return None
        parts = [str(self._listed(st, n, slot) or "").replace("|", "/").strip()
                 for n in ('filament_vendor', 'filament_type',
                           'filament_sub_type')]
        return "tag:" + "|".join(parts)[:80]

    def _filament_default_k(self, ex, slot, st):
        fp = self.printer.lookup_object('filament_parameters', None)
        if fp is None:
            return None
        try:
            return float(fp.get_flow_k(
                self._listed(st, 'filament_vendor', slot),
                self._listed(st, 'filament_type', slot),
                self._listed(st, 'filament_sub_type', slot),
                ex.nozzle_diameter, ex.nozzle_volume_type))
        except Exception:
            return None

    def _apply_flow(self, slot, ex=None, live=True, during_print=False):
        """Put the stored K of the slot's toolhead and filament into stock's
        table (and the live pressure advance). Returns True if it changed."""
        if self.store is None or self._flow_busy or \
                (self._printing() and not during_print):
            return False
        fc = self._flow_calibrator()
        if fc is None:
            return False
        try:
            ex = ex or self._slots()[slot]
            key = self._filament_key(slot)
            if key is None:
                return False
            th_id = self.store.toolhead_in_slot(slot)
            entry = self.store.flow_entry(th_id, key, ex.nozzle_diameter,
                                          ex.nozzle_volume_type)
            if entry is None:
                return False
            cur = fc._current_k.get(ex.name)
            if cur is not None and abs(cur - entry["k"]) < FLOW_EPS:
                return False
            fc._current_k[ex.name] = entry["k"]
            fc._save_config()
            if live:
                fc._apply_calibrate_k(getattr(ex, 'extruder_index', slot))
            logging.info("virtual_toolheads: flow K %.4f (%s) applied to %s "
                         "for %s", entry["k"], key, ex.name,
                         self.store.label(th_id))
            return True
        except Exception:
            logging.exception("virtual_toolheads: could not apply the flow "
                              "value to slot %s", slot)
            return False

    def _refresh_flow(self):
        if self.store is None or self._printing():
            return
        for slot in range(self.store.n_slots):
            self._apply_flow(slot)

    def _sync_flow(self):
        """Startup, once: the K the slots hold now belongs to the filament in
        them. Adopt it unless it is just the filament's default."""
        s = self.store
        fc = self._flow_calibrator()
        if fc is None or s.flow_adopted:
            return
        st = self._task_status()
        changed = False
        for slot, ex in enumerate(self._slots()):
            try:
                key = self._filament_key(slot, st)
                cur = fc._current_k.get(ex.name)
                if key is None or cur is None:
                    continue
                th_id = s.toolhead_in_slot(slot)
                if s.flow_entry(th_id, key, ex.nozzle_diameter,
                                ex.nozzle_volume_type) is not None:
                    continue
                default = self._filament_default_k(ex, slot, st)
                if default is None or abs(cur - default) < 1e-6:
                    continue
                s.record_flow(th_id, key, cur, ex.nozzle_diameter,
                              ex.nozzle_volume_type, "adopted")
                changed = True
            except Exception:
                logging.exception("virtual_toolheads: could not adopt the "
                                  "flow value of slot %d", slot)
        s.mark_flow_adopted()
        self._save()

    # ---- Z homing values -------------------------------------------------
    # The Z frame of a calibration is set by the G28 before it: the stock
    # homing probes the build plate with the active tool and prints the
    # values it used.  They are kept with each logged offset run, so runs
    # can be compared by the frame they were measured in.
    def _install_home_hook(self):
        if self._home_hooked:
            return
        old = self.gcode.register_command('G28', None)
        if old is None:
            logging.warning("virtual_toolheads: G28 not found; homing "
                            "values are not logged")
            return
        helps = getattr(self.gcode, 'gcode_help', {})
        self.gcode.register_command('G28', self._wrap_home(old),
                                    desc=helps.get('G28'))
        self._home_hooked = True

    def _wrap_home(self, old):
        def hooked(gcmd):
            grab = self._home_begin(gcmd)
            ok = False
            try:
                result = old(gcmd)
                ok = True
                return result
            finally:
                self._home_end(grab, ok)
        return hooked

    def _home_begin(self, gcmd):
        """Start listening to the G28 output.  Never raises."""
        try:
            self._home_depth += 1
            if self._home_depth > 1:
                return None             # the stock G28 calls itself
            callbacks = getattr(self.gcode, 'output_callbacks', None)
            if callbacks is None:
                return None
            seen = {}

            def listen(msg):
                try:
                    for line in str(msg).split("\n"):
                        line = line.strip()
                        if line.startswith("//"):
                            line = line[2:].strip()
                        for prefix, key in HOME_LINES:
                            if line.startswith(prefix):
                                seen[key] = float(line[len(prefix):])
                except Exception:
                    pass
            callbacks.append(listen)
            return (callbacks, listen, seen, self._home_context())
        except Exception:
            logging.exception("virtual_toolheads: G28 not observed")
            return None

    def _home_context(self):
        ctx = {}
        try:
            ex = self.printer.lookup_object('toolhead').get_extruder()
            ctx["tool"] = ex.get_name()
            off = getattr(ex, 'gcode_offset', None)
            if off is not None:
                ctx["tool_z_offset"] = float(off[2])
        except Exception:
            pass
        try:
            bed = self.printer.lookup_object('heater_bed', None)
            if bed is not None:
                st = bed.get_status(self.printer.get_reactor().monotonic())
                ctx["bed_temp"] = round(float(st["temperature"]), 1)
                ctx["bed_target"] = float(st["target"])
        except Exception:
            pass
        return ctx

    def _home_end(self, grab, ok):
        self._home_depth = max(0, self._home_depth - 1)
        if grab is None:
            return
        callbacks, listen, seen, ctx = grab
        try:
            callbacks.remove(listen)
        except ValueError:
            pass
        if not ok or not seen:
            return                  # failed, or no Z homing (G28 X Y)
        try:
            self._home_seq += 1
            rec = {"t": now_str(), "n": self._home_seq, "session": new_id()}
            rec.update(ctx)
            rec.update(seen)
            try:
                mesh = self.printer.lookup_object('bed_mesh', None)
                rec["mesh"] = bool(mesh is not None
                                   and mesh.get_mesh() is not None)
            except Exception:
                pass
            try:
                probe = self.printer.lookup_object('probe', None)
                last = probe.get_status(0)["last_z_result"]
                rec["probe_z"] = float(last)
            except Exception:
                pass
            self.last_home = rec
        except Exception:
            logging.exception("virtual_toolheads: G28 values not kept")

    def _install_flow_hooks(self):
        if self._flow_hooked:
            return
        helps = getattr(self.gcode, 'gcode_help', {})
        makers = (('FLOW_CALIBRATE', self._wrap_flow_calibrate),
                  ('FLOW_APPLY_CALIBRATE_K', self._wrap_flow_apply),
                  ('SET_PRINT_FILAMENT_CONFIG', self._wrap_filament_config))
        for name, make in makers:
            old = self.gcode.register_command(name, None)
            if old is None:
                logging.warning("virtual_toolheads: %s not found; flow "
                                "values are only partly tracked", name)
                continue
            self.gcode.register_command(name, make(old), desc=helps.get(name))
        self._flow_hooked = True

    def _wrap_flow_calibrate(self, old):
        def hooked(gcmd):
            ex = before = None
            try:
                ex = self.printer.lookup_object('toolhead').get_extruder()
                fc = self._flow_calibrator()
                before = fc._current_k.get(ex.name) if fc else None
            except Exception:
                ex = None
            self._flow_busy = True
            try:
                return old(gcmd)
            finally:
                self._flow_busy = False
                if ex is not None:
                    try:
                        self._after_flow_calibrate(ex, before)
                    except Exception:
                        logging.exception("virtual_toolheads: flow hook "
                                          "failed")
        return hooked

    def _wrap_flow_apply(self, old):
        def hooked(gcmd):
            try:
                idx = gcmd.get_int('EXTRUDER', None)
                if self.store is not None and idx is not None and \
                        0 <= idx < self.store.n_slots:
                    self._apply_flow(idx, during_print=True)
            except Exception:
                logging.exception("virtual_toolheads: flow hook failed")
            return old(gcmd)
        return hooked

    def _wrap_filament_config(self, old):
        def hooked(gcmd):
            result = old(gcmd)
            try:
                self._refresh_flow()
            except Exception:
                logging.exception("virtual_toolheads: flow hook failed")
            return result
        return hooked

    def _after_flow_calibrate(self, ex, before):
        """Record the result of a stock FLOW_CALIBRATE. The outcome per slot
        ('recorded', 'same', 'default' or 'none') tells the wizard what
        happened."""
        if self.store is None:
            return
        slot = getattr(ex, 'extruder_index', None)
        if slot is None or not 0 <= slot < self.store.n_slots:
            return
        fc = self._flow_calibrator()
        new = fc._current_k.get(ex.name) if fc else None
        outcome = self._flow_outcome
        if new is None:
            outcome[slot] = 'none'
            return
        st = self._task_status()
        key = self._filament_key(slot, st)
        if key is None:
            outcome[slot] = 'none'
            return
        th_id = self.store.toolhead_in_slot(slot)
        def fin(result):
            outcome[slot] = result
            self.log_calibration("flow", slot, k=float(new), key=key,
                                 before=before, outcome=result)
        if before is not None and abs(new - before) < FLOW_EPS:
            e = self.store.flow_entry(th_id, key, ex.nozzle_diameter,
                                      ex.nozzle_volume_type)
            same = e is not None and abs(e["k"] - new) < FLOW_EPS
            fin('same' if same else 'none')   # else: aborted run
            return
        default = self._filament_default_k(ex, slot, st)
        if default is not None and abs(new - default) < 1e-6:
            fin('default')   # out of range: stock used the default
            return
        self.store.record_flow(th_id, key, new, ex.nozzle_diameter,
                               ex.nozzle_volume_type, "calibrated")
        fin('recorded')
        self._save()
        logging.info("virtual_toolheads: recorded flow K %.4f (%s) for %s",
                     new, key, self.store.label(th_id))

    def flow_state(self, slot, lookup=True, st=None):
        """None when flow calibration is not possible for this slot (no
        flow_calibrator, no filament); else whether the toolhead already has
        a K for the filament in the slot."""
        if self.store is None or self._flow_calibrator() is None:
            return None
        key = self._filament_key(slot, st, lookup=lookup)
        if key is None:
            return None
        e = self._slots()[slot]
        return self.store.flow_entry(self.store.toolhead_in_slot(slot), key,
                                     e.nozzle_diameter,
                                     e.nozzle_volume_type) is not None

    def flow_identified(self, slot):
        """True when the filament in a slot is known by its Spoolman
        filament id (the key stored flow values are filed under)."""
        try:
            key = self._filament_key(slot, lookup=False)
        except Exception:
            return False
        return bool(key and key.startswith("fil:"))

    def flow_value(self, slot):
        """The stored K for the toolhead and filament in a slot, or None."""
        try:
            key = self._filament_key(slot, lookup=False)
            if key is None:
                return None
            e = self._slots()[slot]
            entry = self.store.flow_entry(self.store.toolhead_in_slot(slot),
                                          key, e.nozzle_diameter,
                                          e.nozzle_volume_type)
            return entry["k"] if entry else None
        except Exception:
            return None

    # ---- Spoolman filament lookup (spool id -> filament id) ----------------
    def _request_filament_lookup(self, spool_id):
        if not self._flow_lookup or spool_id in self._lookup_pending:
            return
        failed = self._lookup_failed.get(spool_id)
        if failed is not None and \
                time.monotonic() - failed < FLOW_LOOKUP_RETRY:
            return
        self._lookup_pending.add(spool_id)
        self._spawn(lambda: self._fetch_filament_id(spool_id),
                    lambda fid: self._lookup_done(spool_id, fid))

    def _spawn(self, work, done):
        """Run work() off the reactor thread, then done(result) on it."""
        reactor = self.printer.get_reactor()

        def run():
            try:
                result = work()
            except Exception as e:
                logging.info("virtual_toolheads: Spoolman lookup failed: %s",
                             e)
                result = None
            reactor.register_async_callback(lambda e: done(result))
        threading.Thread(target=run, daemon=True).start()

    def _fetch_filament_id(self, spool_id):
        body = json.dumps({"request_method": "GET",
                           "path": "/v1/spool/%d" % spool_id}).encode()
        req = urllib.request.Request(
            self._moonraker_url.rstrip('/') + "/server/spoolman/proxy",
            data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=FLOW_LOOKUP_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        spool = data.get("result", data)
        fid = (spool.get("filament") or {}).get("id")
        return int(fid) if fid else None

    def _lookup_done(self, spool_id, fid):
        self._lookup_pending.discard(spool_id)
        if not fid or self.store is None:
            self._lookup_failed[spool_id] = time.monotonic()
            return
        self._lookup_failed.pop(spool_id, None)
        if self.store.set_spool_filament(spool_id, fid):
            self._save()
        self._refresh_flow()

    def cmd_VT_FLOW(self, gcmd):
        self._require_store(gcmd)
        s = self.store
        st = self._task_status()
        loaded = {th: i for i, th in enumerate(s.layout)}
        if gcmd.get('TOOLHEAD', None) is not None:
            ids = [self._get_th(gcmd)]
        else:
            ids = list(s.layout) + s.spare_ids()
        lines = []
        for th_id in ids:
            head = s.label(th_id)
            if th_id in loaded:
                key = self._filament_key(loaded[th_id], st, lookup=False)
                head += "  (slot %d, filament %s)" % (loaded[th_id],
                                                      key or "none")
            lines.append(head)
            entries = s.flow_entries(th_id)
            if not entries:
                lines.append("  no stored flow values")
            for key in sorted(entries):
                e = entries[key]
                lines.append("  %s  K %.4f  %.1fmm %s  %s (%s)" % (
                    key, e["k"], e["diameter"], e["volume_type"], e["at"],
                    e["source"]))
        gcmd.respond_info("\n".join(lines))

    def cmd_VT_FLOW_FORGET(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        key = gcmd.get('KEY', None)
        n = self._store_op(gcmd, lambda: self.store.forget_flow(th_id, key))
        self._save()
        gcmd.respond_info("Forgot %d flow value(s) of %s"
                          % (n, self.store.label(th_id)))

    def _flow_status(self, slot_of, th_id, st):
        entries = self.store.flow_entries(th_id)
        out = {"count": len(entries), "filament": None, "k": None}
        slot = slot_of
        if slot is not None:
            key = self._filament_key(slot, st, lookup=False)
            out["filament"] = key
            ex = self._slots()[slot]
            e = self.store.flow_entry(th_id, key, ex.nozzle_diameter,
                                      ex.nozzle_volume_type) if key else None
            out["k"] = e["k"] if e else None
        return out

    def _write_slot_view(self):
        view = {}
        for i, ex in enumerate(self._slots()):
            th_id = self.store.toolhead_in_slot(i)
            view[ex.name] = {"base_position":
                             self.store.effective_base_position(th_id)}
        atomic_write_json(self._view_path(), view)

    # ---- observing stock behaviour ---------------------------------------
    def _handle_offset_event(self, *args):
        if self._applying or self.store is None:
            return
        try:
            st = self.store
            stock = self.printer.lookup_object('extruder_offset_calibration',
                                               None)
            results = getattr(stock, 'last_xyz_result', None) or {}
            raws = {}       # slot -> raw measurement of this calibration
            changed = False
            for i, ex in enumerate(self._slots()):
                bp = getattr(ex, 'base_position', None)
                if bp is None or len(bp) < 3:
                    continue
                pos = [float(v) for v in bp[:3]]
                res = results.get(ex.name)
                if res is not None and len(res) >= 3 and \
                        all(abs(a - float(b)) <= POS_TOL
                            for a, b in zip(pos, res)) and \
                        tuple(res[:3]) != self._seen_results.get(ex.name):
                    raws[i] = pos
                    self._seen_results[ex.name] = tuple(res[:3])
                    continue
                th_id = st.toolhead_in_slot(i)
                if st.record_calibration(th_id, pos, i):
                    changed = True
                    logging.info("virtual_toolheads: recorded calibration "
                                 "for %s (slot %d)", st.label(th_id), i)
            if raws:
                self._record_session(raws)
                changed = True
            if changed:
                self._save()
            if raws:
                # the stock code holds this session's raw values; put the
                # frame-corrected ones back
                self.apply_to_slots(runtime=True)
            elif changed:
                self._write_slot_view()
        except Exception:
            logging.exception("virtual_toolheads: offset event failed")

    # ---- keeping offsets relative to T0 ------------------------------------
    # An offset calibration measures every toolhead in the Z (and XY) frame
    # that the homing before it set, and that frame differs from session to
    # session (plate, bed temperature, ...).  The stock offsets are the
    # difference to T0, so values from different sessions cannot be mixed.
    # A session that includes T0 tells us its frame: the new values are
    # stored as "same difference to T0, in the frame of the stored T0", so
    # toolheads measured in other sessions stay valid.
    FRAME_BIG = 0.3      # mm: more than a frame shift should be

    @staticmethod
    def _um(v):
        return "X %+.0f, Y %+.0f, Z %+.0f um" % tuple(x * 1000. for x in v)

    def _record_session(self, raws):
        st = self.store
        ref_th = st.toolhead_in_slot(0)
        ids = list(st.ids())
        old = {t: st.stored_base(t) for t in ids}
        was_valid = {t: st.is_valid(t) for t in ids}
        th_of = {slot: st.toolhead_in_slot(slot) for slot in raws}
        session = self.session_id()
        notes = []
        stored = {}                       # slot -> (raw, stored, frame)

        def put(slot, raw, pos, frame):
            if st.record_calibration(th_of[slot], pos, slot, frame=frame,
                                     session=session):
                logging.info("virtual_toolheads: recorded calibration for %s "
                             "(slot %d)", st.label(th_of[slot]), slot)
            stored[slot] = (raw, pos, st.frame_of(th_of[slot]))

        how = "relative"
        if 0 not in raws:
            how = "no_t0"
            frame = new_id()
            for slot, raw in raws.items():
                put(slot, raw, raw, frame)
            notes.append(
                "T0 was not measured in this calibration, so the new offsets "
                "could not be tied to T0's frame (the frame of each session "
                "typically differs by tens of um in Z, and by more in Y when "
                "the bed temperature differs). They are marked as not "
                "aligned to T0. Measure T0 together with the toolhead next "
                "time: the calibration wizard does that unless "
                "VT_CALIBRATION_OPTIONS REF=0 was set.")
        else:
            raw_ref = raws[0]
            others = {k: v for k, v in raws.items() if k != 0}
            old_ref = old[ref_th]
            if old_ref is not None and was_valid[ref_th]:
                frame = st.frame_of(ref_th) or new_id()
                c = [o - r for o, r in zip(old_ref, raw_ref)]
                for slot, raw in others.items():
                    put(slot, raw, [r + d for r, d in zip(raw, c)], frame)
                if others:
                    notes.append(
                        "Offsets are stored relative to T0. This session's "
                        "frame differed from the stored T0 by %s; the new "
                        "values were corrected for it, so toolheads "
                        "calibrated earlier stay valid." % self._um(c))
                else:
                    how = "t0_only"
                    notes.append(
                        "T0 is the reference for all offsets. Its new "
                        "measurement only describes this session's frame "
                        "(%s) and was not stored; the other toolheads stay "
                        "valid. After changing T0's nozzle, use "
                        "VT_REPLACE_NOZZLE first." % self._um(c))
                if max(abs(x) for x in c) > self.FRAME_BIG:
                    notes.append(
                        "That is a large shift for a frame change. If T0 or "
                        "its nozzle was changed or knocked, tell VT "
                        "(VT_REPLACE_NOZZLE) and calibrate T0 together with "
                        "a toolhead that was not touched.")
            else:
                # T0 has no usable stored value: a new nozzle, or a toolhead
                # that was never calibrated (for example a second left-slot
                # toolhead swapped in).  The other toolheads' stored values
                # are still right, in their own frame.  A toolhead that was
                # measured with T0 and is still valid tells that frame: T0
                # (and the other measured ones) are stored in it, and the
                # toolheads that were not measured stay as they are.
                bridges = [(sl, raw) for sl, raw in others.items()
                           if was_valid[th_of[sl]] and old[th_of[sl]]]
                if bridges:
                    how = "bridge"
                    c = [0., 0., 0.]
                    for sl, raw in bridges:
                        o = old[th_of[sl]]
                        for k in range(3):
                            c[k] += (o[k] - raw[k]) / len(bridges)
                    frame = st.frame_of(th_of[bridges[0][0]]) or new_id()
                    for slot, raw in raws.items():
                        put(slot, raw, [raw[k] + c[k] for k in range(3)],
                            frame)
                    kept = [st.label(t) for t in ids
                            if t not in set(th_of.values())
                            and old[t] is not None and was_valid[t]]
                    notes.append(
                        "T0 (%s) was measured again (new nozzle or "
                        "toolhead). %s, measured with it, ties it to the "
                        "frame of the stored offsets (this session differed "
                        "by %s); %s." % (
                            st.label(ref_th),
                            ", ".join(st.label(th_of[sl])
                                      for sl, _ in bridges),
                            self._um([-x for x in c]),
                            ("%s keep their calibration" % ", ".join(kept))
                            if kept else "no other toolheads are stored"))
                else:
                    how = "first"
                    frame = new_id()
                    for slot, raw in raws.items():
                        put(slot, raw, raw, frame)
                    notes.append(
                        "T0 was measured again but no other calibrated "
                        "toolhead was measured with it, so it cannot be tied "
                        "to the stored offsets of the others. They are "
                        "marked as not aligned to T0. Calibrate T0 together "
                        "with one toolhead that was not touched (the wizard "
                        "adds one to a T0 calibration), or recalibrate the "
                        "others.")
        for slot, (raw, pos, frame) in stored.items():
            self.log_calibration(
                "store", slot, session=session, frame=frame, how=how,
                raw=[round(v, 6) for v in raw],
                base=[round(v, 6) for v in pos],
                shift=[round(p - r, 6) for p, r in zip(pos, raw)])
        self.frame_notes = notes
        try:
            self.gcode.respond_info("\n".join(notes))
        except Exception:
            pass

    def session_id(self):
        """Identifies the homing the running calibration is measured in (one
        homing sets one frame); a new id when none was seen."""
        sid = (self.last_home or {}).get("session")
        return sid or new_id()

    def take_frame_notes(self):
        notes, self.frame_notes = self.frame_notes, []
        return notes

    def _wrap_setters(self, ext_obj):
        cls = type(ext_obj)
        for name, kind in (("_set_nozzle_diameter", "diameter"),
                           ("_set_nozzle_volume_type", "volume_type")):
            orig = getattr(cls, name, None)
            if orig is None:
                logging.error("virtual_toolheads: %s.%s not found; stock UI "
                              "nozzle changes will not be tracked",
                              cls.__name__, name)
                continue
            if getattr(orig, '_vt_wrapped', False):
                continue
            setattr(cls, name, _make_wrapper(orig, kind))
        self._wrapped = True

    def on_slot_nozzle_set(self, ex, kind, value):
        """Called (via the class wrapper) after a stock nozzle setter ran."""
        if self._applying or self.store is None:
            return
        slots = self._slots()
        try:
            slot = slots.index(ex)
        except ValueError:
            return
        th_id = self.store.toolhead_in_slot(slot)
        kwargs = {kind: value}
        if self.store.set_nozzle(th_id, **kwargs):
            logging.info("virtual_toolheads: stock nozzle change on slot %d "
                         "-> %s needs calibration", slot,
                         self.store.label(th_id))
            self._save()
            # Live slot values are left alone (stock keeps the old offset
            # until the new calibration is saved); VT_CHECK uses the store.

    # ---- status ----------------------------------------------------------
    def get_status(self, eventtime=None):
        if self.store is None:
            return {"ready": False, "error": self.error, "version": VERSION}
        s = self.store
        ths = {}
        st = self._task_status()
        for th_id in s.ids():
            th = s.get(th_id)
            c = th["calibration"]
            ths[th["label"]] = {
                "id": th_id,
                "side": th["side"],
                "slot": s.slot_of(th_id),
                "nozzle": dict(th["nozzle"]),
                "valid": s.is_valid(th_id),
                "invalid_reason": s.invalid_reason(th_id),
                "calibrated_at": c["at"],
                "calibrated_slot": c["slot"],
                "frame_stale": s.frame_stale(th_id),
                "pid": self._pid_status(th_id),
                "needs": self._needs_status(s.slot_of(th_id), st),
                "serial": th.get("serial"),
                "flow": self._flow_status_safe(s.slot_of(th_id), th_id, st),
            }
        return {
            "ready": True, "error": None, "version": VERSION,
            "epoch": s.epoch,
            "calibration_guard": self.calibrator.guard_info,
            "layout": [s.label(i) for i in s.layout],
            "toolheads": ths,
            "pid_temp": s.pid_temp,
            "invalid_loaded": [s.label(i) for _, i in s.invalid_loaded()],
            "calibrate": self._calibrate_status(st),
            "auto_calibrate": self._auto_calibrate_status(),
            "backups": len(self._backup_names()),
            "backup_list": self._backup_list(),
            "options": {"diameters": list(VALID_DIAMETERS),
                        "volume_types": list(VALID_VOLUME_TYPES),
                        "materials": list(VALID_MATERIALS)},
            "boards": {"available": self._boards_available(),
                       "slots": list(self._detected),
                       "lines": self._board_lines_safe(),
                       "unknown": [slot for slot, ser in enumerate(
                           self._detected) if ser and s.by_serial(ser) is None]},
            "notice": dict(self._notice) if self._notice else None,
        }

    def _auto_calibrate_status(self):
        """The id of a pending request for the card to open its calibrate
        panel (after a swap restart); None once it is stale or done."""
        if self._auto_cal is None:
            return None
        if time.monotonic() - self._auto_cal_at > AUTO_CAL_TTL or \
                not self.calibrator._needy():
            return None
        return self._auto_cal

    def _board_lines_safe(self):
        try:
            return self._board_lines()
        except Exception:
            return []

    def _backup_list(self):
        """The newest backups for the card: name, time and layout. Summaries
        are read once per file, status is polled often."""
        cache = self.__dict__.setdefault("_backup_cache", {})
        out = []
        try:
            names = self._backup_names()[:8]
        except Exception:
            return out
        for n in names:
            if n not in cache:
                cache[n] = self._backup_summary(n)
            out.append({"name": n, "when": self._backup_when(n),
                        "summary": cache[n]})
        for n in list(cache):
            if n not in names:
                del cache[n]
        return out

    def _needs_status(self, slot, st=None):
        """What a loaded toolhead still needs: offset, PID, flow K. Reading
        the status never starts a Spoolman lookup."""
        if slot is None:
            return None
        try:
            return dict(self.calibrator._needs(slot, False, st))
        except Exception:
            return None

    def _calibrate_status(self, st=None):
        """What the dashboard card needs to offer calibration itself."""
        try:
            flow_possible = any(
                self.flow_state(slot, False, st) is not None
                for slot in range(self.store.n_slots))
        except Exception:
            flow_possible = False
        return {"flow_possible": flow_possible,
                "pid_presets": list(PID_TEMP_PRESETS),
                "pid_range": list(PID_TEMP_RANGE)}

    def _flow_status_safe(self, slot, th_id, st):
        try:
            return self._flow_status(slot, th_id, st)
        except Exception:
            return {"count": 0, "filament": None, "k": None}

    def _pid_status(self, th_id):
        rec = self.store.pid(th_id)
        profiles = [{"temp": temp, "at": prof.get("at"),
                     "stale": bool(prof.get("stale")),
                     "active": self.store.pid_profile_active(
                         th_id, pid_key(temp))}
                    for temp, prof in self.store.pid_profile_list(th_id)]
        if rec is None:
            return {"calibrated": False, "target": None, "at": None,
                    "source": None, "stale": False, "profiles": profiles}
        return {"calibrated": True, "target": rec.get("target"),
                "at": rec.get("at"), "source": rec.get("source"),
                "stale": bool(rec.get("stale")), "profiles": profiles}

    def _summary_lines(self):
        s = self.store
        lines = []
        for slot, th_id in enumerate(s.layout):
            lines.append("Slot %d (%s): %s" % (slot,
                                               SIDE_NAMES[slot_side(slot)],
                                               self._describe(th_id)))
        spare = s.spare_ids()
        if spare:
            lines.append("Spare:")
            for th_id in spare:
                lines.append("  " + self._describe(th_id))
        return lines

    def _describe(self, th_id):
        s = self.store
        n = s.get(th_id)["nozzle"]
        state = "calibrated" if s.is_valid(th_id) else \
            "NEEDS CALIBRATION (%s)" % s.invalid_reason(th_id)
        if s.frame_stale(th_id):
            state = "calibrated, NOT ALIGNED TO T0 (recalibrate)"
        return "%s %.1fmm %s/%s  %s  %s" % (s.label(th_id),
                                               n["diameter"], n["volume_type"],
                                               n["material"], state,
                                               self._pid_text(th_id))

    # ---- commands --------------------------------------------------------
    def cmd_VT_STATUS(self, gcmd):
        if self.store is None:
            gcmd.respond_info("Virtual toolheads v%s unavailable: %s"
                              % (VERSION, self.error or "not initialised yet"))
            return
        lines = ["Virtual toolheads v%s" % VERSION] + self._summary_lines()
        lines.append("Calibration guard: " + self.calibrator.guard_info)
        gcmd.respond_info("\n".join(lines))

    def _get_th(self, gcmd, name='TOOLHEAD'):
        ref = gcmd.get(name)
        try:
            return self.store.resolve(ref)
        except StoreError as e:
            raise gcmd.error(str(e))

    def _store_op(self, gcmd, fn):
        try:
            return fn()
        except StoreError as e:
            raise gcmd.error(str(e))

    def cmd_VT_ADD(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._store_op(gcmd, lambda: self.store.add_toolhead(
            label=gcmd.get('LABEL', None),
            diameter=gcmd.get_float('DIAMETER', 0.4),
            volume_type=gcmd.get('VOLUME_TYPE', 'standard'),
            material=gcmd.get('MATERIAL', 'stainless'),
            side=gcmd.get('SIDE', 'right')))
        self._save()
        gcmd.respond_info("Added %s toolhead %s"
                          % (SIDE_NAMES[self.store.side(th_id)],
                             self._describe(th_id)))

    def cmd_VT_SET_SIDE(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        side = self._store_op(gcmd, lambda: normalize_side(gcmd.get('SIDE')))
        changed = self._store_op(gcmd,
                                 lambda: self.store.set_side(th_id, side))
        self._save()
        msg = "%s is a %s toolhead. %s" % (self.store.label(th_id),
                                           SIDE_NAMES[side],
                                           self._describe(th_id))
        if changed and not self.store.is_valid(th_id):
            msg += "\n(its calibration was measured in a slot of the other " \
                   "side, so it needs calibrating again)"
        gcmd.respond_info(("Side set: " if changed else "Side confirmed: ")
                          + msg)

    def cmd_VT_RENAME(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        self._store_op(gcmd, lambda: self.store.rename(th_id,
                                                        gcmd.get('LABEL')))
        self._save()
        gcmd.respond_info("Renamed to %s" % self.store.label(th_id))

    def cmd_VT_DELETE(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        label = self.store.label(th_id)
        self._store_op(gcmd, lambda: self.store.delete(th_id))
        self._save()
        gcmd.respond_info("Deleted toolhead %s" % label)

    def cmd_VT_SET_LAYOUT(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        refs = [r.strip() for r in gcmd.get('LAYOUT').split(',')]
        unload = bool(gcmd.get_int('UNLOAD', 0))
        changed = self._store_op(gcmd, lambda: self.store.set_layout(refs))
        self._save()
        self.apply_to_slots(runtime=True)
        lines = ["Layout: " + ", ".join(self.store.label(i)
                                        for i in self.store.layout)]
        if not changed:
            lines.append("(no slot changed)")
        if unload:
            lines += self._unload_slots(changed)
        bad = self.store.invalid_loaded()
        if bad:
            lines.append("Needs calibration: " + ", ".join(
                "%s (slot %d)" % (self.store.label(i), s) for s, i in bad))
        gcmd.respond_info("\n".join(lines))

    def _slot_has_filament(self, slot):
        """True if filament is loaded in the toolhead of the slot (the print
        task's filament_exist flag is the fallback when the feeder does not
        say)."""
        state = self.slot_filament(slot)
        if state != 'unknown':
            return state == 'loaded'
        try:
            return bool(self._task_status()["filament_exist"][slot])
        except Exception:
            return False

    def _feeder_channel(self, slot):
        """The stock filament feeder's status for a slot's channel, or None."""
        for side in ("left", "right"):
            feed = self.printer.lookup_object("filament_feed " + side, None)
            if feed is None:
                continue
            try:
                ch = feed.get_status(time.monotonic()).get("extruder%d" % slot)
            except Exception:
                ch = None
            if ch:
                return ch
        return None

    def slot_filament(self, slot):
        """Where the filament of a slot is, from the stock feeder's channel
        state (not from a sensor: a sensor also sees filament that is only
        waiting in the lane or at the spool after an unload):
          'loaded'    loaded in the toolhead
          'lane'      in the feeder / on the spool, not loaded
          'none'      no filament
          'unloading' an unload is in progress
          'busy'      a load or another feeder action is in progress
          'unknown'   the printer does not say"""
        ch = self._feeder_channel(slot)
        state = ch.get("channel_state") if ch else None
        if state in FEED_UNLOADING:
            return 'unloading'
        if state in FEED_BUSY:
            return 'busy'
        if state == FEED_LOADED:
            return 'loaded'
        detected = ch.get("filament_detected") if ch else None
        if state in FEED_NOT_LOADED:
            return 'lane' if detected else 'none'
        # Not conclusive (just started, or an action failed): ask the sensor.
        sensor = self.printer.lookup_object(
            "filament_motion_sensor e%d_filament" % slot, None)
        try:
            if sensor.get_status(time.monotonic())["filament_detected"]:
                return 'loaded'
            return 'lane' if detected else 'none'
        except Exception:
            pass
        if detected is not None:
            return 'lane' if detected else 'none'
        return 'unknown'

    def _status_screen(self, title, lines):
        """A buttonless dialog for work in progress; the next dialog (the
        result screen) replaces it."""
        p = Prompt(title, status=True)
        for line in lines:
            p.text(line)
        self._show(p)

    def _tool_picked_up(self):
        """True if a toolhead is picked up (an extruder is ACTIVATE), False
        if the printer says none is, None if it does not say."""
        seen = False
        for ex in self._slots():
            try:
                state = ex.get_status(time.monotonic()).get("state")
            except Exception:
                return None
            if state is None:
                return None
            seen = True
            if state == "ACTIVATE":
                return True
        return False if seen else None

    def _dock(self, lines):
        """Dock the toolhead that is picked up (stock PARK_EXTRUDER). Nothing
        happens when no toolhead is picked up."""
        if self._tool_picked_up() is False:
            return
        self._status_screen("Docking", ["Docking the toolhead..."])
        try:
            self.gcode.run_script_from_command("PARK_EXTRUDER")
            lines.append("Toolhead docked")
        except Exception as e:
            logging.exception("virtual_toolheads: dock")
            lines.append("Docking failed (%s)" % e)

    def _unload_slots(self, slots, dock=False, note=UNLOAD_NOTE):
        """Unload the filament of each given slot, one after the other, with
        the stock `AUTO_FEEDING EXTRUDER=n UNLOAD=1`, showing a status
        screen while it works.  Once something has been unloaded the toolhead
        that is left picked up is docked (`PARK_EXTRUDER`) as the final step
        (dock=True: docked even if nothing was unloaded).
        Returns report lines; a failed unload is reported and the next slot
        is still tried."""
        lines = []
        todo = []
        for s in slots:
            state = self.slot_filament(s)
            if state in ('unloading', 'busy'):
                lines.append("T%d: skipped, the filament is already being %s"
                             % (s, "unloaded" if state == 'unloading'
                                else "loaded or moved"))
            elif self._slot_has_filament(s):
                todo.append(s)
            else:
                lines.append("T%d: no filament loaded (feeder state: %s)"
                             % (s, self._feeder_state_text(s)))
        unloaded = 0
        for n, slot in enumerate(todo):
            self._status_screen("Unloading filament", [
                "Unloading T%d: %s (%d of %d)." % (
                    slot, self.store.label(self.store.layout[slot]),
                    n + 1, len(todo)),
                "The nozzle heats up and the filament is retracted. This "
                "can take a few minutes, please wait."])
            try:
                self.gcode.run_script_from_command(
                    "AUTO_FEEDING EXTRUDER=%d UNLOAD=1" % slot)
                lines.append("T%d: filament unloaded" % slot)
                unloaded += 1
            except Exception as e:
                logging.exception("virtual_toolheads: unload T%d", slot)
                lines.append("T%d: unload failed (%s)" % (slot, e))
        if unloaded or dock:
            self._dock(lines)
        if note:
            lines.append(note)
        return lines

    def _feeder_state_text(self, slot):
        ch = self._feeder_channel(slot)
        if not ch:
            return "not reported"
        state = str(ch.get("channel_state") or "unknown")
        return FEED_STATE_TEXT.get(state, state)

    def _nozzle_args(self, gcmd):
        return dict(diameter=gcmd.get_float('DIAMETER', None),
                    volume_type=gcmd.get('VOLUME_TYPE', None),
                    material=gcmd.get('MATERIAL', None))

    def _after_toolhead_change(self, gcmd, th_id):
        self._save()
        if self.store.slot_of(th_id) is not None:
            self.apply_to_slots(runtime=True)
        msg = "%s" % self._describe(th_id)
        gcmd.respond_info(msg)

    def cmd_VT_REPLACE_NOZZLE(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        args = self._nozzle_args(gcmd)
        self._store_op(gcmd, lambda: self.store.replace_nozzle(th_id, **args))
        self._after_toolhead_change(gcmd, th_id)

    def cmd_VT_SET_NOZZLE(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        args = self._nozzle_args(gcmd)
        changed = self._store_op(gcmd,
                                 lambda: self.store.set_nozzle(th_id, **args))
        if not changed:
            gcmd.respond_info("No change: %s" % self._describe(th_id))
            return
        self._after_toolhead_change(gcmd, th_id)

    def cmd_VT_INVALIDATE(self, gcmd):
        self._require_store(gcmd)
        self._require_idle(gcmd)
        ref = gcmd.get('TOOLHEAD')
        if ref.upper() == 'ALL':
            self.store.invalidate_all("manual_all")
        else:
            self.store.invalidate(self._get_th(gcmd), "manual")
        self._save()
        self.apply_to_slots(runtime=True)
        gcmd.respond_info("\n".join(self._summary_lines()))

    def cmd_VT_CHECK(self, gcmd):
        if self.store is None or self.print_guard == 'off':
            return
        bad = self.store.invalid_loaded()
        if not bad:
            return
        msg = "Toolhead(s) need calibration: " + ", ".join(
            "%s in slot %d (%s)" % (self.store.label(i), s,
                                    self.store.invalid_reason(i))
            for s, i in bad)
        if self.print_guard == 'block':
            raise gcmd.error(msg)
        gcmd.respond_info("WARNING: " + msg)

    # ---- debug trace ------------------------------------------------------
    # Wraps the G-code dispatcher so every command that runs (client commands
    # and the lines macros expand into) is recorded with a short call chain
    # naming the macro / module that issued it.  Off by default.
    def trace_note(self, text):
        if self._trace_orig is not None:
            self._trace_add("note", [text], "")

    def _trace_add(self, kind, lines, chain):
        now = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(now)) \
            + ".%03d" % (int(now * 1000) % 1000)
        self._trace_buf.append((ts, kind, list(lines), chain))
        logging.info("vt_trace %s %s %s | %s", ts, kind,
                     " ; ".join(lines)[:300], chain)

    @staticmethod
    def _trace_chain():
        """Innermost-first list of the macros / extras that led here."""
        names = []
        frame = sys._getframe(2)
        for _ in range(80):
            if frame is None:
                break
            owner = frame.f_locals.get('self')
            alias = getattr(owner, 'alias', None)
            path = frame.f_code.co_filename.replace("\\", "/")
            if isinstance(alias, str) and hasattr(owner, 'template'):
                name = "macro " + alias
            elif "/extras/" in path or "/kinematics/" in path:
                name = "%s:%s" % (os.path.basename(path)[:-3],
                                  frame.f_code.co_name)
            else:
                name = None
            if name and (not names or names[-1] != name):
                names.append(name)
            frame = frame.f_back
        return " <- ".join(names[:8]) or "top level"

    def _trace_install(self):
        if self._trace_orig is not None:
            return True
        orig = getattr(self.gcode, '_process_commands', None)
        if orig is None:
            return False
        vt = self

        def traced(commands, *args, **kwargs):
            if time.monotonic() > vt._trace_until:
                vt._trace_remove()
                return orig(commands, *args, **kwargs)
            try:
                if isinstance(commands, str):
                    commands = [commands]
                elif not isinstance(commands, (list, tuple)):
                    commands = list(commands)
                lines = [c.strip() for c in commands]
                lines = [c for c in lines
                         if c and c[0] not in "#;"
                         and c.split()[0].upper() not in TRACE_IGNORE]
                if lines:
                    vt._trace_add("run", lines, vt._trace_chain())
            except Exception:
                logging.exception("virtual_toolheads: trace failed")
            return orig(commands, *args, **kwargs)
        self.gcode._process_commands = traced
        self._trace_orig = orig
        return True

    def _trace_remove(self):
        if self._trace_orig is not None:
            self.gcode.__dict__.pop('_process_commands', None)
            self._trace_orig = None

    def cmd_VT_CARD(self, gcmd):
        """The dashboard card sends this when it sees the module ready, so
        that screens can open in the card instead of as Fluidd dialogs."""
        self._card_seen = time.monotonic()

    def cmd_VT_TRACE(self, gcmd):
        action = gcmd.get('ACTION', 'DUMP').upper()
        if action == "ON":
            minutes = gcmd.get_float('MINUTES', 15., minval=1., maxval=120.)
            self._trace_until = time.monotonic() + minutes * 60.
            if not self._trace_install():
                raise gcmd.error("This Klipper has no _process_commands "
                                 "dispatcher; tracing is unavailable")
            gcmd.respond_info("G-code trace ON for %d minutes. Reproduce the "
                              "problem, then run VT_TRACE ACTION=DUMP." % minutes)
        elif action == "OFF":
            self._trace_remove()
            gcmd.respond_info("G-code trace OFF (%d entries kept)"
                              % len(self._trace_buf))
        elif action == "CLEAR":
            self._trace_buf.clear()
            gcmd.respond_info("Trace cleared")
        elif action == "DUMP":
            count = gcmd.get_int('COUNT', 60, minval=1, maxval=TRACE_MAX_ENTRIES)
            rows = list(self._trace_buf)[-count:]
            if not rows:
                gcmd.respond_info("Trace is empty (run VT_TRACE ACTION=ON first)")
                return
            out = []
            for ts, kind, lines, chain in rows:
                body = " ; ".join(lines)
                if len(body) > 220:
                    body = body[:220] + "..."
                out.append("%s %-4s %s%s" % (ts, kind, body,
                                             ("   <- " + chain) if chain else ""))
            gcmd.respond_info("\n".join(out))
        else:
            raise gcmd.error("ACTION must be ON, OFF, DUMP or CLEAR")

    # ---- UI: action:prompt dialogs ---------------------------------------
    # Every button carries the full state of the dialog in its own command
    # line, so the pages are stateless and survive reconnects.
    def _show(self, prompt):
        self.trace_note("dialog: " + prompt.lines[0][len("prompt_begin "):])
        self._sticky = prompt if prompt.sticky else None
        self._last_prompt = prompt
        self._shown_at = time.monotonic()
        if prompt.status or prompt.restart:
            self._notice_seq += 1
            self._notice = {"seq": self._notice_seq, "title": prompt.title,
                            "lines": list(prompt.texts),
                            "restart": bool(prompt.restart)}
        else:
            self._notice = None
        for line in prompt.finish():
            self.gcode.respond_raw("// action:" + line)

    # Fluidd closes a dialog (click outside, Esc, the X) by running
    # RESPOND TYPE=command MSG=action:prompt_end.  While the wizard waits for
    # the user no G-code is running, so that command arrives at once and the
    # dialog can be shown again.  (During a long wizard step it is queued
    # behind the step, so only status text is used there: the card shows it.)
    def _dismissed(self):
        """Fluidd ran prompt_end.  Show a dialog again when
        - the wizard is waiting for the user (sticky dialog), or
        - the dismissal is stale: it was queued behind a long step (the user
          closed the progress screen), so it only ran after the step had
          shown its next dialog, which it would now close."""
        again = None
        if self._sticky is not None and self.calibrator._on:
            again, why = self._sticky, "dismissed"
        elif (self._last_prompt is not None
              and time.monotonic() - self._shown_at < STALE_DISMISS):
            again, why = self._last_prompt, "stale dismissal"
        if again is None:
            return
        self.trace_note("%s: dialog shown again" % why)
        for line in again.finish():
            self.gcode.respond_raw("// action:" + line)

    def _install_respond_hook(self):
        if self._respond_hooked:
            return
        old = self.gcode.register_command('RESPOND', None)
        if old is None:
            return
        helps = getattr(self.gcode, 'gcode_help', {})

        def hooked(gcmd):
            result = old(gcmd)
            try:
                if (gcmd.get('TYPE', 'echo').lower() == 'command'
                        and gcmd.get('MSG', '').strip().strip('"\'')
                        == 'action:prompt_end'):
                    self._dismissed()
            except Exception:
                logging.exception("virtual_toolheads: RESPOND hook failed")
            return result
        self.gcode.register_command('RESPOND', hooked,
                                    desc=helps.get('RESPOND'))
        self._respond_hooked = True

    _direct = False

    def cmd_VT_RESTART(self, gcmd):
        """Close the dialog, then FIRMWARE_RESTART.  Registered to work in
        the shutdown state, where only a few commands are accepted."""
        self._sticky = None
        self._notice = None
        self._last_prompt = None
        self.gcode.respond_raw("// action:prompt_end")
        self.gcode.run_script_from_command("FIRMWARE_RESTART")

    def cmd_VT_UI(self, gcmd):
        page = gcmd.get('PAGE', 'main').lower()
        if page == 'close':
            self._sticky = None
            self._notice = None
            self._last_prompt = None
            self.gcode.respond_raw("// action:prompt_end")
            return
        self._require_store(gcmd)
        handler = self._ui_pages.get(page)
        if handler is None:
            raise gcmd.error("Unknown VT_UI page '%s'" % page)
        # DIRECT=1: opened from a per-slot dashboard button, not from the
        # menu. Back/Cancel then closes the dialog instead of walking back
        # through the menu.
        self._direct = bool(gcmd.get_int('DIRECT', 0))
        # CARD=1: the dashboard card sent this; results are shown in the card
        # as a notice instead of a Fluidd dialog.
        self._card = bool(gcmd.get_int('CARD', 0))
        try:
            handler(gcmd)
        finally:
            self._direct = False
            self._card = False

    def _ui(self, page, **params):
        """ui_cmd for navigation buttons that honours DIRECT mode."""
        if self._direct:
            if page in ('main', 'close'):
                return ui_cmd('close')
            params['direct'] = 1
        return ui_cmd(page, **params)

    def _labels(self):
        return [self.store.label(i) for i in self.store.layout]

    def _where(self, th_id):
        slot = self.store.slot_of(th_id)
        return "T%d" % slot if slot is not None else "spare"

    def _card_notice(self, title, lines, restart=False):
        self.trace_note("card notice: " + title)
        self._sticky = None
        self._last_prompt = None
        self._notice_seq += 1
        self._notice = {"seq": self._notice_seq, "title": title,
                        "lines": list(lines), "restart": bool(restart)}

    def _ui_result(self, title, lines, restart=False):
        if self._card:
            self._card_notice(title, lines, restart)
            return
        p = Prompt(title)
        for line in lines:
            p.text(line)
        buttons = []
        if restart:
            # VT_RESTART works after Klipper has shut down because a toolhead
            # cable was pulled (it is registered for the not-ready state),
            # closes this dialog and runs FIRMWARE_RESTART.  On its own row,
            # above the others, so the label has room.
            p.buttons([("Restart firmware", "VT_RESTART", "warning")],
                      per_row=1)
            p.restart = True
        if self.store.invalid_loaded():
            buttons.append(("Calibrate now", "VT_CALIBRATE", "primary"))
        buttons += [("Menu", ui_cmd("main"), "secondary"),
                    ("Close", ui_cmd("close"), "secondary")]
        p.buttons(buttons)
        self._show(p)

    def _calibration_lines(self):
        bad = self.store.invalid_loaded()
        if not bad:
            return ["All loaded toolheads are calibrated."]
        return ["Needs calibration: " + ", ".join(
                    "%s (T%d)" % (self.store.label(i), s) for s, i in bad)]

    def _ui_main(self, gcmd):
        s = self.store
        p = Prompt("Virtual toolheads v%s" % VERSION)
        for slot, th_id in enumerate(s.layout):
            p.text("T%d: %s" % (slot, self._describe(th_id)))
        spare = s.spare_ids()
        if spare:
            p.text("Spare: " + ", ".join(
                "%s [%s]" % (s.label(i), SIDE_NAMES[s.side(i)])
                for i in spare))
        if s.invalid_loaded():
            for line in self._calibration_lines():
                p.text(line)
        p.buttons([("Change layout", self._ui("layout"), "primary")])
        p.buttons([("Change nozzle", self._ui("nozzle"), "primary"),
                   ("Change PID", self._ui("pid"), "primary")], per_row=2)
        p.buttons([("Boards", self._ui("boards"), "secondary"),
                   ("Backup", self._ui("backup"), "secondary")], per_row=2)
        # With readable boards a new toolhead is created when its board is
        # first seen; adding one by hand moves under Boards.
        if not self._boards_available():
            p.buttons([("Add toolhead", self._ui("add"), "secondary")])
        p.buttons([("Calibrate", "VT_CALIBRATE",
                    "warning" if s.invalid_loaded() else "secondary")])
        p.footer("Close", self._ui("close"))
        self._show(p)

    # -- layout --
    def _pending(self, gcmd):
        raw = gcmd.get('PENDING', None)
        if raw is None:
            return self._labels()
        refs = [r.strip() for r in raw.split(',')]
        if len(refs) != self.store.n_slots:
            raise gcmd.error("Layout needs exactly %d entries"
                             % self.store.n_slots)
        try:
            return [self.store.label(self.store.resolve(r)) for r in refs]
        except StoreError as e:
            raise gcmd.error(str(e))

    def _unload_param(self, gcmd):
        return gcmd.get_int('UNLOAD', 0, minval=0, maxval=1)

    # -- swapping toolheads when the boards identify themselves --
    def _swap_sel(self, gcmd):
        raw = gcmd.get('SEL', None)
        if raw is None:
            slot = gcmd.get_int('SLOT', None)
            return [slot] if slot is not None and \
                0 <= slot < self.store.n_slots else []
        try:
            sel = sorted(set(int(v) for v in raw.split(',') if v.strip()))
        except ValueError:
            raise gcmd.error("Bad SEL '%s'" % raw)
        if any(not 0 <= v < self.store.n_slots for v in sel):
            raise gcmd.error("Slot out of range")
        return sel

    def _ui_swap(self, gcmd):
        """Which toolheads are about to be swapped. The new ones are found by
        their boards after the restart, so nothing is asked about them."""
        s = self.store
        sel = self._swap_sel(gcmd)
        unload = gcmd.get_int('UNLOAD', 1)
        load = gcmd.get_int('LOAD', 0)
        csv = ",".join(str(v) for v in sel)
        p = Prompt("Swap toolheads")
        p.text("Pick the toolheads you are going to take out. The ones you "
               "put in are recognised automatically after the restart.")
        buttons = []
        for slot, th_id in enumerate(s.layout):
            nxt = [v for v in sel if v != slot] if slot in sel \
                else sorted(sel + [slot])
            buttons.append(("T%d: %s" % (slot, s.label(th_id)),
                            self._ui("swap", sel=",".join(
                                str(v) for v in nxt), unload=unload,
                                load=load),
                            "primary" if slot in sel else "secondary"))
        p.buttons(buttons, per_row=4)
        p.buttons([("Unload filament: %s" % ("on" if unload else "off"),
                    self._ui("swap", sel=csv, unload=1 - unload, load=load),
                    "primary" if unload else "secondary"),
                   ("Load filament after: %s" % ("on" if load else "off"),
                    self._ui("swap", sel=csv, unload=unload, load=1 - load),
                    "primary" if load else "secondary")])
        if unload:
            p.text("Filament loaded on the selected toolheads is unloaded.")
        if load:
            p.text("After the restart, filament is loaded in these slots "
                   "(after any calibration).")
        p.text("The toolhead that is picked up is docked.")
        if sel:
            p.footer("Continue", self._ui("swap_apply", sel=csv,
                                          unload=unload, load=load),
                     "primary")
        p.footer("Close" if self._direct else "Back", self._ui("main"))
        self._show(p)

    def _ui_swap_apply(self, gcmd):
        self._require_idle(gcmd)
        s = self.store
        sel = self._swap_sel(gcmd)
        if not sel:
            raise gcmd.error("Pick at least one toolhead")
        unload = gcmd.get_int('UNLOAD', 1)
        load = gcmd.get_int('LOAD', 0)
        lines = ["Swapping: " + ", ".join(
            "T%d (%s)" % (v, s.label(s.layout[v])) for v in sel)]
        if unload:
            lines += self._unload_slots(sel, dock=True, note=None)
        else:
            self._dock(lines)
        lines.extend(SWAP_NOTE)
        if load:
            lines.append("Filament is loaded again in %s after the restart."
                         % ", ".join("T%d" % v for v in sel))
        self._mark_swap_pending(load=sel if load else ())
        self._ui_result("Swap toolheads", lines, restart=True)

    def _ui_layout(self, gcmd):
        if self._boards_available():
            return self._ui_swap(gcmd)
        s = self.store
        pending = self._pending(gcmd)
        current = self._labels()
        csv = ",".join(pending)
        unload = self._unload_param(gcmd)
        p = Prompt("Toolhead layout")
        p.text("Pick a slot, then the toolhead that is physically in it.")
        buttons = [("T%d: %s" % (slot, label),
                    self._ui("layout_slot", slot=slot, pending=csv,
                             **_unl(unload)),
                    "primary" if label != current[slot] else "secondary")
                   for slot, label in enumerate(pending)]
        p.buttons(buttons, per_row=4)
        if pending != current:
            p.text("Not applied yet. Was: %s  Now: %s"
                   % (",".join(current), csv))
            p.buttons([("Unload filament: %s" % ("on" if unload else "off"),
                        self._ui("layout", pending=csv, **_unl(1 - unload)),
                        "primary" if unload else "secondary")])
            if unload:
                p.text("On Apply, filament loaded on the swapped toolheads "
                       "is unloaded.")
            p.footer("Apply", self._ui("layout_apply", pending=csv,
                                       **_unl(unload)), "primary")
        p.footer("Cancel" if pending != current else "Back", self._ui("main"))
        self._show(p)

    def _ui_layout_slot(self, gcmd):
        s = self.store
        slot = gcmd.get_int('SLOT', minval=0, maxval=s.n_slots - 1)
        if self._boards_available():
            return self._ui_swap(gcmd)
        pending = self._pending(gcmd)
        csv = ",".join(pending)
        unload = self._unload_param(gcmd)
        p = Prompt("Toolhead for slot T%d" % slot)
        p.text("Slot T%d (%s) currently: %s. Only %s toolheads fit here. A "
               "toolhead already in another slot swaps places with it."
               % (slot, SIDE_NAMES[slot_side(slot)], pending[slot],
                  SIDE_NAMES[slot_side(slot)]))
        buttons = []
        for th_id in s.ids_for_slot(slot):
            label = s.label(th_id)
            where = pending.index(label) if label in pending else None
            text = label if where is None or where == slot \
                else "%s (T%d)" % (label, where)
            buttons.append((text,
                            self._ui("layout", pending=",".join(
                                assign_slot(pending, slot, label)),
                                **_unl(unload)),
                            "primary" if label == pending[slot] else None))
        p.buttons(buttons, per_row=4)
        p.footer("Back", self._ui("close") if self._direct
                 else self._ui("layout", pending=csv, **_unl(unload)))
        self._show(p)

    def _ui_layout_apply(self, gcmd):
        self._require_idle(gcmd)
        refs = self._pending(gcmd)
        unload = self._unload_param(gcmd)
        changed = self._store_op(gcmd, lambda: self.store.set_layout(refs))
        self._save()
        self.apply_to_slots(runtime=True)
        lines = ["Layout: " + ", ".join("T%d=%s" % (i, l)
                                         for i, l in enumerate(self._labels()))]
        if not changed:
            lines.append("(no slot changed)")
        if unload:
            lines += self._unload_slots(changed)
            self._mark_swap_pending()
        self._ui_result("Layout applied", lines + self._calibration_lines(),
                        restart=bool(unload))

    # -- dock the active tool --
    def _ui_dock(self, gcmd):
        p = Prompt("Dock the active tool?")
        p.text("The active toolhead is put back in its dock (PARK_EXTRUDER).")
        p.buttons([("Dock", ui_cmd("dock_apply"), "primary"),
                   ("Cancel", ui_cmd("close"), "secondary")])
        self._show(p)

    def _ui_dock_apply(self, gcmd):
        self._require_idle(gcmd)
        self._status_screen("Docking", ["Docking the active toolhead..."])
        try:
            self.gcode.run_script_from_command("PARK_EXTRUDER")
        except Exception as e:
            logging.exception("virtual_toolheads: dock")
            self._ui_result("Docking failed", [str(e)])
            return
        self._sticky = None
        self._notice = None
        self._last_prompt = None
        self.gcode.respond_raw("// action:prompt_end")

    # -- PID tuning by temperature --
    def _ui_pid_toolheads(self, gcmd):
        s = self.store
        p = Prompt("Change PID: which toolhead?")
        p.buttons([("%s (%s)" % (s.label(i), self._where(i)),
                    self._ui("pid_pick", toolhead=s.label(i)), "primary"
                    if s.slot_of(i) is not None else None)
                   for i in s.ids()], per_row=4)
        p.footer("Back", self._ui("main"))
        self._show(p)

    def _ui_pid_pick(self, gcmd):
        """Stored tunings (click = use it) and the other temperatures (click =
        calibrate it right away with only PID on)."""
        th_id = self._get_th(gcmd)
        label = self.store.label(th_id)
        slot = self.store.slot_of(th_id)
        stored = dict((pid_key(t), (t, r))
                      for t, r in self.store.pid_profile_list(th_id))
        temps = sorted(set(PID_TEMP_PRESETS) |
                       set(t for t, _ in stored.values()) |
                       {float(self.store.pid_temp)})
        p = Prompt("PID tuning of %s (%s)" % (label, self._where(th_id)))
        p.text("Calibrated temperatures use the stored tuning when clicked."
               + (" Others start a PID calibration right away." if slot is not None
                  else " Put the toolhead in a slot to calibrate others."))
        for temp in temps:
            if pid_key(temp) in stored:
                prof = stored[pid_key(temp)][1]
                p.text("%.0f C: calibrated %s%s%s" % (
                    temp, prof["at"],
                    " (before the last nozzle change)" if prof.get("stale")
                    else "", " (in use)" if self.store.pid_profile_active(
                        th_id, pid_key(temp)) else ""))
        buttons = []
        for temp in temps:
            key = pid_key(temp)
            if key in stored:
                buttons.append(("%.0f \u00b0C" % temp,
                                ui_cmd("pid_apply", toolhead=label,
                                       temp=int(temp)),
                                "primary" if self.store.pid_profile_active(
                                    th_id, key) else "secondary"))
            elif slot is not None:
                buttons.append(("%.0f \u00b0C (new)" % temp,
                                cal_cmd("prep", sel=str(slot), ofs=0, pid=1,
                                        flow=0, pt=Calibrator._fmt_temp(temp),
                                        direct=1), None))
        if buttons:
            p.buttons(buttons, per_row=3)
        p.footer("Close" if self._direct else "Back",
                 self._ui("main") if self._direct else ui_cmd("pid"))
        self._show(p)

    def _ui_pid_apply(self, gcmd):
        """Switch to a stored tuning and just close the dialog."""
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        temp = gcmd.get_float('TEMP')
        text = self._store_op(gcmd, lambda: self._select_pid(th_id, temp))
        if self._card:
            self._card_notice("PID", [text])
            return
        self._sticky = None
        self._notice = None
        self._last_prompt = None
        self.gcode.respond_raw("// action:prompt_end")
        gcmd.respond_info(text)

    # -- nozzle --
    def _ui_nozzle_pick(self, gcmd):
        s = self.store
        p = Prompt("Change nozzle: which toolhead?")
        p.buttons([("%s (%s)" % (s.label(i), self._where(i)),
                    self._ui("nozzle_form", toolhead=s.label(i)), "primary"
                    if s.slot_of(i) is not None else None)
                   for i in s.ids()], per_row=4)
        p.footer("Back", self._ui("main"))
        self._show(p)

    def _form_values(self, gcmd, th_id=None):
        """Diameter/type/material, defaulting to the toolhead's current."""
        cur = self.store.get(th_id)["nozzle"] if th_id else \
            {"diameter": 0.4, "volume_type": "standard",
             "material": "stainless"}
        diameter = gcmd.get_float('DIAMETER', cur["diameter"])
        volume_type = gcmd.get('VOLUME_TYPE', cur["volume_type"])
        material = gcmd.get('MATERIAL', cur["material"])
        return cur, _canon_diameter(diameter), volume_type, material

    def _form_groups(self, p, page, base, diameter, volume_type, material):
        def pick(value, chosen):
            return "primary" if value == chosen else None
        p.text("Diameter (mm)")
        p.buttons([("%.1f" % d, self._ui(page, **dict(base, diameter=d)),
                    pick(d, diameter)) for d in VALID_DIAMETERS], per_row=4)
        p.text("Flow type")
        p.buttons([(v, self._ui(page, **dict(base, volume_type=v)),
                    pick(v, volume_type)) for v in VALID_VOLUME_TYPES])
        p.text("Material")
        p.buttons([(m, self._ui(page, **dict(base, material=m)),
                    pick(m, material)) for m in VALID_MATERIALS])

    def _ui_nozzle_form(self, gcmd, intro=None):
        th_id = self._get_th(gcmd)
        label = self.store.label(th_id)
        mode = gcmd.get('MODE', 'replace').lower()
        if mode not in ('replace', 'edit'):
            raise gcmd.error("MODE must be replace or edit")
        cur, diameter, vtype, material = self._form_values(gcmd, th_id)
        differs = not (_same_diameter(cur["diameter"], diameter)
                       and cur["volume_type"] == vtype
                       and cur["material"] == material)
        p = Prompt("Nozzle on toolhead %s (%s)" % (label, self._where(th_id)))
        for line in (intro or []):
            p.text(line)
        p.text("Now: %.1fmm %s/%s" % (cur["diameter"], cur["volume_type"],
                                      cur["material"]))
        p.buttons([("New nozzle fitted",
                    self._ui("nozzle_form", toolhead=label, mode="replace",
                           diameter=diameter, volume_type=vtype,
                           material=material),
                    "primary" if mode == "replace" else None),
                   ("Correct details only",
                    self._ui("nozzle_form", toolhead=label, mode="edit",
                           diameter=diameter, volume_type=vtype,
                           material=material),
                    "primary" if mode == "edit" else None)], per_row=2)
        base = {"toolhead": label, "mode": mode, "diameter": diameter,
                "volume_type": vtype, "material": material}
        self._form_groups(p, "nozzle_form", base, diameter, vtype, material)
        if mode == "replace":
            p.text("Calibration for %s will be invalidated." % label)
        elif differs:
            p.text("Details changed: calibration for %s will be invalidated."
                   % label)
        else:
            p.text("Nothing changed yet.")
        p.footer("Apply", self._ui("nozzle_apply", **base), "primary")
        p.footer("Cancel", self._ui("main"))
        self._show(p)

    def _ui_nozzle_apply(self, gcmd):
        self._require_idle(gcmd)
        th_id = self._get_th(gcmd)
        mode = gcmd.get('MODE', 'replace').lower()
        _, diameter, vtype, material = self._form_values(gcmd, th_id)
        if mode == 'replace':
            self._store_op(gcmd, lambda: self.store.replace_nozzle(
                th_id, diameter=diameter, volume_type=vtype,
                material=material))
        else:
            self._store_op(gcmd, lambda: self.store.set_nozzle(
                th_id, diameter=diameter, volume_type=vtype,
                material=material))
        self._save()
        if self.store.slot_of(th_id) is not None:
            self.apply_to_slots(runtime=True)
        lines = [self._describe(th_id)]
        lines += self._unknown_boards_lines()
        self._ui_result("Nozzle updated", lines + self._calibration_lines())

    # -- add --
    def _side_param(self, gcmd):
        try:
            return normalize_side(gcmd.get('SIDE', 'right'))
        except StoreError as e:
            raise gcmd.error(str(e))

    def _ui_add_form(self, gcmd):
        _, diameter, vtype, material = self._form_values(gcmd)
        side = self._side_param(gcmd)
        label = self.store.next_default_label(side)
        p = Prompt("Add toolhead")
        p.text("New spare toolhead, label %s (rename later with "
               "VT_RENAME TOOLHEAD=%s LABEL=name)." % (label, label))
        base = {"diameter": diameter, "volume_type": vtype,
                "material": material, "side": side}
        p.text("Fits (slot 0 is a different type of slot from slots 1-3)")
        p.buttons([("Left Slot (T0)",
                    self._ui("add_form", **dict(base, side="left")),
                    "primary" if side == "left" else None),
                   ("Right Slot (T1-T3)",
                    self._ui("add_form", **dict(base, side="right")),
                    "primary" if side == "right" else None)], per_row=2)
        self._form_groups(p, "add_form", base, diameter, vtype, material)
        p.footer("Add", self._ui("add_apply", **base), "primary")
        p.footer("Cancel", self._ui("main"))
        self._show(p)

    def _ui_add_apply(self, gcmd):
        self._require_idle(gcmd)
        _, diameter, vtype, material = self._form_values(gcmd)
        side = self._side_param(gcmd)
        th_id = self._store_op(gcmd, lambda: self.store.add_toolhead(
            diameter=diameter, volume_type=vtype, material=material,
            side=side))
        self._save()
        self._ui_result("Toolhead added", [self._describe(th_id)])

Plan = collections.namedtuple('Plan', 'ofs pid flow pt')


class Calibrator:
    """Selective XYZ-offset calibration built from the stock
    EXTRUDER_OFFSET_ACTION_* commands.

    Order follows the reference block in the stock _EXTRUDER_OFFSET_ACTION_
    PRESTART macro: per tool HEAT, AUTO_CLEAN, MANUAL_CLEAN, WAIT_COOL; then
    DETECT_PLATE; then PROBE_CALIBRATE per tool; then save and exit.  Only the
    selected tools take part (including the manual clean).  The stock
    commands run unchanged; this class only decides which ones to call and
    supplies the pauses (wipe the nozzle, remove the plate) as dialogs.

    Optionally the selected toolheads are PID-tuned first (stock
    PID_CALIBRATE, one heater at a time). That has to start from a cold
    nozzle, so it runs before the stock PRESTART homes and preheats; the
    result is recorded by the PID_CALIBRATE hook in VirtualToolheads.
    """
    COOL_TEMP = 140   # the stock preheat nozzle temperature
    PLATE_Z = 150     # bed position that makes the build plate easy to remove
    STOCK_STATE = "XYZ_OFFSET_CALIBRATE"
    STOCK_COMMANDS = ("PRESTART", "PREHOMING", "DETECT_PLATE", "PREHEAT",
                      "MANUAL_CLEAN", "HEAT", "AUTO_CLEAN", "WAIT_COOL",
                      "CHECK_TARGET_TEMP", "PROBE_CALIBRATE", "SAVE_RESULT",
                      "EXIT")
    PID_START_MAX = 50  # C: PID tuning starts from a cold (ambient) nozzle  # noqa
    START_GRACE = 180   # s: a wizard that never reached the stock state
    GUARD_TAIL = 20     # s: keep rejecting stragglers after the wizard ends

    STAGES = ('pid', 'flow', 'ofs')    # order the stages run in

    def __init__(self, vt):
        self.vt = vt
        # While the wizard runs, stock calibration commands sent by other
        # clients (the touchscreen app follows the stock calibration state
        # and continues its own flow) are rejected.
        self._on = False
        self._entered = False
        self._on_since = 0.
        self._tail_until = 0.
        self._internal = 0
        self.probe_runs = 1          # VT_CALIBRATION_LOOPS: probes per toolhead
        self.probe_dock = False      # ... docking the toolhead between them
        self.do_clean = True         # CLEAN=0: skip heating/cleaning steps
        self.add_ref = True          # VT_CALIBRATION_OPTIONS REF=0: no T0 reference
        self._loop = None            # (run, runs, docked) of the probe now
        self.repeat_notes = []
        self._hold = False          # keep the guard up (PID phase is long)
        self._pid_active = False
        self.rejected = []
        self.guard_info = "not installed"
        self.pages = {
            'start': self.page_start, 'prep': self.page_prep,
            'clean': self.page_clean, 'cool': self.page_cool,
            'detect': self.page_detect, 'probe': self.page_probe,
            'finish': self.page_finish, 'abort': self.page_abort,
            'pid': self.page_pid, 'begin': self.page_begin,
            'flow': self.page_flow,
        }

    # ---- helpers ---------------------------------------------------------
    @property
    def store(self):
        return self.vt.store

    def _run(self, script):
        self._internal += 1      # our own calls (and the stock code's nested
        try:                     # calls inside them) are always allowed
            self.vt.gcode.run_script_from_command(script)
        finally:
            self._internal -= 1

    def _main_state(self):
        msm = self.vt.printer.lookup_object('machine_state_manager', None)
        if msm is None:
            return None
        try:
            return str(msm.get_status()['main_state'])
        except Exception:
            return None

    def _disarm(self):
        """Wizard over. Commands the touchscreen queued while it ran execute
        right after our last command returns, so keep the guard up briefly."""
        self._on = False
        self._hold = False
        self._pid_active = False
        self._tail_until = time.monotonic() + self.GUARD_TAIL

    def guard_ok(self):
        return self.guard_info.startswith("active")

    def active(self):
        if not self._on:
            return time.monotonic() < self._tail_until
        if self._hold:
            return True
        state = self._main_state()
        if state == self.STOCK_STATE:
            self._entered = True
        elif state is not None:
            if self._entered or \
                    time.monotonic() - self._on_since > self.START_GRACE:
                self._on = False        # stock state ended (or never began)
                return False
        return True

    def install_guards(self):
        """Wrap the stock EXTRUDER_OFFSET_ACTION_* handlers."""
        gcode = self.vt.gcode
        helps = getattr(gcode, 'gcode_help', {})
        count = 0
        for short in self.STOCK_COMMANDS:
            name = "EXTRUDER_OFFSET_ACTION_" + short
            desc = helps.get(name)
            old = gcode.register_command(name, None)
            if old is None:
                continue
            gcode.register_command(name, self._make_guard(name, old),
                                   desc=desc)
            count += 1
        if count:
            self.guard_info = "active (%d stock commands)" % count
        else:
            self.guard_info = "not installed (stock calibration commands " \
                              "not found)"

    def _make_guard(self, name, old):
        def guarded(gcmd):
            if self._internal == 0 and self.active():
                if len(self.rejected) < 50:
                    self.rejected.append(name)
                logging.warning("virtual_toolheads: rejected %s %s while the "
                                "calibration wizard is running", name,
                                dict(getattr(gcmd, 'params', {})))
                raise gcmd.error(
                    "%s rejected: the virtual toolheads calibration wizard "
                    "is running (or has just finished). Finish or abort it "
                    "from its dialog (VT_CAL PAGE=abort)." % name)
            result = old(gcmd)
            if name.endswith("PROBE_CALIBRATE"):
                self._log_offset_run(gcmd)
            return result
        return guarded

    def _log_offset_run(self, gcmd):
        """Log the raw probe result of one offset run (wizard or touchscreen)."""
        try:
            tool = str(gcmd.get('TOOL_ID', '')).upper().lstrip('T')
            slot = int(tool)
            res = self._stock().last_xyz_result.get(self._name(slot))
            if res is None or len(res) < 3:
                return
            ctx = {}
            if self._loop is not None and self.active():
                run, total, dock = self._loop
                ctx = {"loop": run, "loops": total, "dock": bool(dock),
                       "cleaned": bool(self.do_clean)}
            if self.vt.last_home:
                ctx["home"] = dict(self.vt.last_home)
                ctx["session"] = self.vt.last_home.get("session")
            self.vt.log_calibration("offset", slot, x=float(res[0]),
                                    y=float(res[1]), z=float(res[2]), **ctx)
        except Exception:
            logging.exception("virtual_toolheads: offset run not logged")

    def _blocked_lines(self):
        if not self.rejected:
            return []
        names = sorted(set(self.rejected))
        return ["%d command(s) from another client (probably the "
                "touchscreen) were blocked while the wizard ran: %s"
                % (len(self.rejected), ", ".join(
                    n.replace("EXTRUDER_OFFSET_ACTION_", "") for n in names))]

    def _stock(self):
        return self.vt.printer.lookup_object('extruder_offset_calibration',
                                             None)

    def _name(self, slot):
        return self.vt._slots()[slot].name

    def _label(self, slot):
        return self.vt._slot_label(slot)

    def _sel(self, gcmd, allow_empty=False):
        raw = gcmd.get('SEL', '-')
        try:
            slots = sorted({int(x) for x in raw.split(',')
                            if x.strip() not in ('', '-')})
        except ValueError:
            raise gcmd.error("Bad SEL '%s'" % raw)
        for slot in slots:
            if not 0 <= slot < self.store.n_slots:
                raise gcmd.error("Slot %d does not exist" % slot)
        if not slots and not allow_empty:
            raise gcmd.error("No toolheads selected")
        return slots

    def _status(self, title, lines):
        p = Prompt(title, status=True)
        for line in lines:
            p.text(line)
        self.vt._show(p)

    def _error_prompt(self, title, message, retry):
        p = Prompt(title, sticky=True)
        p.text(message)
        p.buttons([("Retry", retry, "primary"),
                   ("Abort calibration", cal_cmd("abort"), "error")])
        self.vt._show(p)

    def _guard(self, title, retry, fn):
        """Run fn; on failure show Retry/Abort instead of a console error."""
        try:
            fn()
            return True
        except Exception as e:
            logging.exception("virtual_toolheads: calibration step failed")
            self._error_prompt(title, friendly_error(e), retry)
            return False

    # ---- entry points ----------------------------------------------------
    def cmd_VT_CALIBRATE(self, gcmd):
        self.vt._require_store(gcmd)
        self.vt._require_idle(gcmd)
        if self._stock() is None:
            raise gcmd.error("Stock calibration (extruder_offset_calibration) "
                             "is not available")
        raw = gcmd.get('TOOLHEADS', None)
        if raw is None:
            sel = self._needy()
        else:
            sel = set()
            for ref in raw.split(','):
                th_id = self.vt._store_op(
                    gcmd, lambda r=ref.strip(): self.store.resolve(r))
                slot = self.store.slot_of(th_id)
                if slot is None:
                    raise gcmd.error("Toolhead %s is not loaded in a slot"
                                     % self.store.label(th_id))
                sel.add(slot)
            sel = sorted(sel)
        # Naming toolheads asks for their offset calibration unless OFS=0.
        plan = self._plan_args(gcmd, sel, ofs_default=1 if raw else None)
        if gcmd.get_int('CONFIRM', 0):
            if not sel:
                raise gcmd.error("No toolheads to calibrate")
            self._run_wizard(sel, plan, gcmd)
        else:
            self._show_start(sel, bool(gcmd.get_int('DIRECT', 0)), plan)

    def cmd_VT_CALIBRATION_LOOPS(self, gcmd):
        count = None
        for name in ('N', 'COUNT', 'LOOPS'):
            count = gcmd.get_int(name, None, minval=1, maxval=20)
            if count is not None:
                break
        if count is not None:
            self.probe_runs = count
            self.probe_dock = False
        dock = gcmd.get_int('DOCK', None, minval=0, maxval=1)
        if dock is not None:
            self.probe_dock = bool(dock)
        moved = self._set_options(gcmd)
        gcmd.respond_info(
            "Offset probe loops per toolhead: %d%s%s" % (
                self.probe_runs,
                ", docking between probes" if self.probe_dock else "",
                "" if self.probe_runs == 1 else
                " (every run is logged, the last one is saved; reset with "
                "VT_CALIBRATION_LOOPS N=1)")
            + ("\nCLEAN and REF are options of the calibration, not of the "
               "loops: use VT_CALIBRATION_OPTIONS." if moved else ""))

    def _set_options(self, gcmd):
        """CLEAN= and REF=; returns whether either was given."""
        clean = gcmd.get_int('CLEAN', None, minval=0, maxval=1)
        if clean is not None:
            self.do_clean = bool(clean)
        ref = gcmd.get_int('REF', None, minval=0, maxval=1)
        if ref is not None:
            self.add_ref = bool(ref)
        return clean is not None or ref is not None

    def cmd_VT_CALIBRATION_OPTIONS(self, gcmd):
        self._set_options(gcmd)
        gcmd.respond_info("\n".join([
            "Offset calibration options (they reset on restart):",
            "  CLEAN=%d: %s" % (1 if self.do_clean else 0,
                                "heating and automatic cleaning run" if
                                self.do_clean else
                                "heating, cleaning and the wipe prompt are "
                                "skipped (for repeat runs)"),
            "  REF=%d: %s" % (1 if self.add_ref else 0,
                              "T0 is measured too, as the reference, so the "
                              "offsets are stored relative to it" if
                              self.add_ref else
                              "T0 is NOT added; the offsets of a toolhead "
                              "calibrated alone are not tied to T0's frame "
                              "and are marked as not aligned"),
            "Probe loops per toolhead: VT_CALIBRATION_LOOPS N=<count>"]))

    def cmd_VT_CAL(self, gcmd):
        page = gcmd.get('PAGE', 'start').lower()
        handler = self.pages.get(page)
        if handler is None:
            raise gcmd.error("Unknown VT_CAL page '%s'" % page)
        if page != 'abort':
            self.vt._require_store(gcmd)
            self.vt._require_idle(gcmd)
        handler(gcmd)

    # ---- pages -------------------------------------------------------------
    @staticmethod
    def _fmt_temp(temp):
        return "%g" % float(temp)

    def _needs(self, slot, lookup=True, st=None):
        """What a loaded toolhead is missing: offset, PID, flow K."""
        s = self.store
        th_id = s.toolhead_in_slot(slot)
        return {'ofs': not s.is_valid(th_id),
                'pid': s.pid(th_id) is None,
                'flow': self.vt.flow_state(slot, lookup, st) is False}

    def _auto_open_state(self):
        """What to do after a swap restart: None (nothing to open) or 'now'
        (open the calibrate screen).  A missing Dynamic Flow K alone never
        opens it; the Calibrate button in the card header covers that."""
        for slot in self._needy():
            n = self._needs(slot)
            if n['ofs'] or n['pid']:
                return 'now'
        return None

    def _needy(self):
        return [slot for slot in range(self.store.n_slots)
                if any(self._needs(slot).values())]

    def _default_on(self, kind, sel):
        return 1 if any(self._needs(slot)[kind] for slot in sel) else 0

    def _plan_args(self, gcmd, sel, ofs_default=None):
        """Which calibrations to run, from OFS / PID / FLOW (1 or 0) and the
        PID temperature (PID_TEMP or PT). Anything not given defaults to on
        when a selected toolhead lacks it."""
        flags = {}
        for kind, name in (('ofs', 'OFS'), ('pid', 'PID'), ('flow', 'FLOW')):
            value = gcmd.get_int(name, None)
            if value is None:
                value = ofs_default if kind == 'ofs' and \
                    ofs_default is not None else self._default_on(kind, sel)
            flags[kind] = 1 if value else 0
        raw = gcmd.get_float('PID_TEMP', None)
        if raw is None:
            raw = gcmd.get_float('PT', None)
        pt = self.store.pid_temp if raw is None else raw
        lo, hi = PID_TEMP_RANGE
        if flags['pid'] and not lo <= pt <= hi:
            raise gcmd.error("PID temperature must be between %d and %d C"
                             % (lo, hi))
        return Plan(flags['ofs'], flags['pid'], flags['flow'], pt)

    @staticmethod
    def _plan_cmd_args(plan):
        return dict(ofs=plan.ofs, pid=plan.pid, flow=plan.flow,
                    pt=Calibrator._fmt_temp(plan.pt))

    def _show_start(self, sel, direct=False, plan=None):
        s = self.store
        plan = plan or Plan(self._default_on('ofs', sel),
                            self._default_on('pid', sel),
                            self._default_on('flow', sel), s.pid_temp)
        dflag = 1 if direct else None

        def start_cmd(sel_, **change):
            p_ = plan._replace(**change)
            return cal_cmd("start", sel=csv_slots(sel_), direct=dflag,
                           **self._plan_cmd_args(p_))

        def toggle(label, kind):
            on = getattr(plan, kind)
            return ("%s: %s" % (label, "on" if on else "off"),
                    start_cmd(sel, **{kind: 0 if on else 1}),
                    "primary" if on else None)
        flow_possible = any(self.vt.flow_state(slot) is not None
                            for slot in range(s.n_slots))
        p = Prompt("Calibrate toolheads")
        p.text("Choose the toolheads to calibrate (those that need something "
               "are preselected), then which calibrations to run. Each is "
               "on when a selected toolhead lacks it. The others keep their "
               "calibration.")
        buttons = []
        for slot in range(s.n_slots):
            toggled = [x for x in sel if x != slot] if slot in sel \
                else sorted(sel + [slot])
            th_id = s.toolhead_in_slot(slot)
            text = "T%d: %s" % (slot, s.label(th_id))
            if any(self._needs(slot).values()):
                text += " (needs it)"
            elif s.frame_stale(th_id):
                text += " (not aligned to T0)"
            buttons.append((text, start_cmd(toggled),
                            "primary" if slot in sel else None))
        p.buttons(buttons, per_row=2)
        stale = [s.label(s.toolhead_in_slot(i)) for i in range(s.n_slots)
                 if s.frame_stale(s.toolhead_in_slot(i))]
        if stale:
            p.text("Not aligned to T0: %s. Their offset was measured in an "
                   "earlier session than T0's (or without T0), so it can be "
                   "off by the difference between the sessions (tens of um "
                   "in Z, more in Y). Select them to measure them against "
                   "T0 again; the others keep their calibration."
                   % ", ".join(stale))
        p.text("Offset calibration finds the position of each nozzle: heat, "
               "automatic clean (this extrudes filament, so load filament "
               "first), then you wipe the nozzle by hand and remove the "
               "build plate so the probe can run.")
        if self.add_ref:
            p.text("Offsets are stored relative to T0 (the left toolhead), "
                   "which is measured with every offset calibration. That "
                   "is what lets you recalibrate a single toolhead later "
                   "without redoing the others.")
        p.buttons([toggle("Offset calibration", 'ofs')])
        p.text("PID calibration tunes the heater of each selected toolhead. "
               "It starts from a cold nozzle and takes about 5 minutes each.")
        p.buttons([toggle("PID calibration", 'pid')])
        if plan.pid:
            temps = sorted(set(PID_TEMP_PRESETS) | {float(plan.pt)})
            p.buttons([("%s C" % self._fmt_temp(t), start_cmd(sel, pid=1, pt=t),
                        "primary" if abs(t - plan.pt) < 1e-9 else None)
                       for t in temps], per_row=3)
        if flow_possible:
            p.text("Dynamic Flow calibration finds the pressure advance (K) for the "
                   "filament in each selected toolhead and remembers it for "
                   "that filament. The nozzle is heated and filament is "
                   "extruded.")
            p.buttons([toggle("Dynamic Flow calibration", 'flow')])
        p.text("Order: PID, Dynamic Flow, then offset. While it runs, calibration "
               "commands from other clients (e.g. the touchscreen) are "
               "blocked.")
        if not self.guard_ok():
            p.text("WARNING: the calibration guard is not active (%s). The "
                   "touchscreen may interfere with the wizard."
                   % self.guard_info)
        if sel and (plan.ofs or plan.pid or plan.flow):
            p.footer("Start", cal_cmd("prep", sel=csv_slots(sel),
                                      **self._plan_cmd_args(plan)), "primary")
        elif sel:
            p.text("Turn on at least one calibration to start.")
        p.footer("Cancel", ui_cmd("close" if direct else "main"))
        self.vt._show(p)

    def page_start(self, gcmd):
        raw = gcmd.get('SEL', None)
        if raw is None:
            sel = self._needy()
        else:
            sel = self._sel(gcmd, allow_empty=True)
        self._show_start(sel, bool(gcmd.get_int('DIRECT', 0)),
                         self._plan_args(gcmd, sel))

    def page_prep(self, gcmd):
        sel = self._sel(gcmd)
        self._run_wizard(sel, self._plan_args(gcmd, sel), gcmd)

    def page_begin(self, gcmd):
        """Skip the rest of a tuning stage and carry on with the next one."""
        sel = self._sel(gcmd)
        plan = self._plan_args(gcmd, sel)
        self._next_stage(sel, plan, gcmd.get('STAGE', 'pid'))

    def page_pid(self, gcmd):
        sel = self._sel(gcmd)
        plan = self._plan_args(gcmd, sel)
        self._pid_phase(sel, gcmd.get_int('I', 0, minval=0,
                                          maxval=len(sel) - 1), plan)

    def page_flow(self, gcmd):
        sel = self._sel(gcmd)
        plan = self._plan_args(gcmd, sel)
        self._flow_phase(sel, gcmd.get_int('I', 0, minval=0,
                                           maxval=len(sel) - 1), plan)

    def _run_wizard(self, sel, plan, gcmd=None):
        def fail(msg):
            raise (gcmd.error(msg) if gcmd is not None else StoreError(msg))
        if not (plan.ofs or plan.pid or plan.flow):
            fail("Nothing to do: turn on at least one calibration")
        if plan.flow:
            bad = [slot for slot in sel if self.vt.flow_state(slot) is None]
            if bad:
                fail("Dynamic Flow calibration needs a filament set in %s (and the "
                     "flow calibrator)" % ", ".join(
                         "T%d (%s)" % (s_, self._label(s_)) for s_ in bad))
        if plan.pid:
            try:
                self.store.set_pid_temp(plan.pt)
                self.vt._save()
            except StoreError as e:
                fail(str(e))
        self._next_stage(sel, plan, None)

    def _next_stage(self, sel, plan, after):
        """Run the next enabled stage after 'after' (None = the first)."""
        start = 0 if after is None else self.STAGES.index(after) + 1
        for stage in self.STAGES[start:]:
            if getattr(plan, stage):
                if stage == 'pid':
                    return self._pid_phase(sel, 0, plan)
                if stage == 'flow':
                    return self._flow_phase(sel, 0, plan)
                self._hold = False
                self._pid_active = False
                return self._begin(sel)
        self._conclude(sel)

    # ---- PID phase -------------------------------------------------------
    def _heater(self, slot):
        try:
            return self.vt._slots()[slot].get_heater()
        except Exception:
            return None

    def _heater_off(self, slot):
        heater = self._heater(slot)
        pheaters = self.vt.printer.lookup_object('heaters', None)
        if heater is None or pheaters is None:
            return
        try:
            pheaters.set_temperature(heater, 0.)
        except Exception:
            logging.exception("virtual_toolheads: could not switch off the "
                              "heater of slot %d", slot)

    def _arm(self, reset):
        self._on = True
        self._entered = False
        self._on_since = time.monotonic()
        if reset:
            self.rejected = []

    def _hold_rejected(self):
        """True when the guard was already armed (PID phase): keep what it
        collected."""
        return self._on

    def _busy(self):
        """Another calibration (e.g. on the touchscreen) is running."""
        if not self._on and self._main_state() == self.STOCK_STATE:
            p = Prompt("Calibration already running")
            p.text("Another calibration is in progress (for example on the "
                   "touchscreen). Finish or cancel it there first, or cancel "
                   "it here.")
            p.buttons([("Cancel it", cal_cmd("abort"), "error"),
                       ("Menu", ui_cmd("main"), "secondary")])
            self.vt._show(p)
            return True
        return False

    def _pid_cool(self, slot, name):
        heater = self._heater(slot)
        temp = getattr(heater, 'smoothed_temp', None)
        if temp is None or temp <= self.PID_START_MAX:
            return
        self._status("PID calibration: toolhead T%d" % slot, [
            "T%d (%s) is at %.0f C. Waiting for it to cool to %d C so the "
            "tuning starts from a cold nozzle." % (
                slot, self._label(slot), temp, self.PID_START_MAX)])
        self._heater_off(slot)
        self._run("TEMPERATURE_WAIT SENSOR=%s MAXIMUM=%d"
                  % (name, self.PID_START_MAX))

    def _pid_phase(self, sel, k, plan):
        pt = plan.pt
        if self._busy():
            return
        self._arm(reset=(k == 0))
        self._hold = True
        self._pid_active = True
        csv = csv_slots(sel)
        for j in range(k, len(sel)):
            slot = sel[j]
            name = self._name(slot)
            label = self._label(slot)
            before = self.vt._pid_events.get(slot, 0)
            try:
                self._pid_cool(slot, name)
                self._status("PID calibration: toolhead T%d" % slot, [
                    "Tuning T%d (%s) at %s C, %d of %d. This takes several "
                    "minutes; the nozzle heats up and cools down repeatedly."
                    % (slot, label, self._fmt_temp(pt), j + 1, len(sel))])
                self._run("PID_CALIBRATE HEATER=%s TARGET=%s"
                          % (name, self._fmt_temp(pt)))
                if self.vt._pid_events.get(slot, 0) == before:
                    raise RuntimeError(
                        "PID calibration finished, but no new tuning was "
                        "found for T%d." % slot)
            except Exception as e:
                logging.exception("virtual_toolheads: PID calibration failed")
                self._heater_off(slot)
                p = Prompt("PID calibration of T%d failed" % slot, sticky=True)
                p.text(friendly_error(e))
                args = self._plan_cmd_args(plan)
                p.buttons([("Retry", cal_cmd("pid", sel=csv, i=j, **args),
                            "primary"),
                           ("Skip PID", cal_cmd("begin", sel=csv, stage="pid",
                                                **args), "secondary"),
                           ("Abort calibration", cal_cmd("abort"), "error")])
                self.vt._show(p)
                return
            self._heater_off(slot)
        self._next_stage(sel, plan, 'pid')

    # ---- flow phase ------------------------------------------------------
    def _ensure_xy_homed(self):
        """Picking up a toolhead needs the machine homed; stock
        FLOW_CALIBRATE only homes after the pick-up."""
        try:
            th = self.vt.printer.lookup_object('toolhead')
            homed = th.get_status(self.vt.printer.get_reactor().monotonic()
                                  ).get('homed_axes', '')
        except Exception:
            return
        if 'x' not in homed or 'y' not in homed:
            self._run("G28 X Y")

    def _ensure_filament_loaded(self, slot, label):
        """Dynamic Flow calibration extrudes, so the filament has to be loaded.
        Filament that is in the feeder but not loaded is loaded first (stock
        `AUTO_FEEDING EXTRUDER=n LOAD=1`); with no filament at all the
        calibration cannot go on."""
        state = self.vt.slot_filament(slot)
        if state == 'none':
            raise RuntimeError(
                "T%d (%s) has no filament. Insert filament, then retry."
                % (slot, label))
        if state in ('unloading', 'busy'):
            raise RuntimeError(
                "T%d (%s) is busy loading or unloading filament. Wait until "
                "it has finished, then retry." % (slot, label))
        if state != 'lane':
            return
        self._status("Loading filament: toolhead T%d" % slot, [
            "The filament of T%d (%s) is not loaded yet. Loading it before "
            "the flow calibration; the nozzle heats up, this can take a few "
            "minutes." % (slot, label)])
        self._run("AUTO_FEEDING EXTRUDER=%d LOAD=1" % slot)
        if self.vt.slot_filament(slot) not in ('loaded', 'unknown'):
            raise RuntimeError("The filament of T%d did not load." % slot)

    def _flow_phase(self, sel, k, plan):
        if self._busy():
            return
        self._arm(reset=(k == 0 and not plan.pid))
        self._hold = True
        self._pid_active = True       # the stock offset calibration has not begun
        csv = csv_slots(sel)
        for j in range(k, len(sel)):
            slot = sel[j]
            label = self._label(slot)
            self.vt._flow_outcome.pop(slot, None)
            try:
                self._status("Dynamic Flow calibration: toolhead T%d" % slot, [
                    "Calibrating T%d (%s), %d of %d. The toolhead is picked "
                    "up, the nozzle heats and filament is extruded; this "
                    "takes several minutes." % (slot, label, j + 1,
                                                len(sel))])
                self._ensure_filament_loaded(slot, label)
                self._ensure_xy_homed()
                self._run("T%d A0" % slot)
                self._run("FLOW_CALIBRATE")
                outcome = self.vt._flow_outcome.get(slot)
                if outcome == 'default':
                    raise RuntimeError(
                        "The measured value for T%d was out of range, so the "
                        "filament's default was used and nothing was "
                        "stored." % slot)
                if outcome not in ('recorded', 'same'):
                    raise RuntimeError(
                        "Dynamic Flow calibration finished, but no result was found "
                        "for T%d." % slot)
            except Exception as e:
                logging.exception("virtual_toolheads: flow calibration failed")
                self._heater_off(slot)
                p = Prompt("Dynamic Flow calibration of T%d failed" % slot, sticky=True)
                p.text(friendly_error(e))
                args = self._plan_cmd_args(plan)
                p.buttons([("Retry", cal_cmd("flow", sel=csv, i=j, **args),
                            "primary"),
                           ("Skip flow", cal_cmd("begin", sel=csv,
                                                 stage="flow", **args),
                            "secondary"),
                           ("Abort calibration", cal_cmd("abort"), "error")])
                self.vt._show(p)
                return
            self._heater_off(slot)
        self._next_stage(sel, plan, 'flow')

    def _conclude(self, sel):
        """The wizard ran without the offset stage: report and finish."""
        self._hold = False
        self._pid_active = False
        s = self.store
        lines = []
        for slot in sel:
            th_id = s.toolhead_in_slot(slot)
            text = "T%d: %s" % (slot, self.vt._describe(th_id))
            k = self.vt.flow_value(slot)
            if k is not None:
                text += "  Dynamic Flow K %.4f" % k
            lines.append(text)
        lines += self._blocked_lines()
        self._disarm()
        self.vt._ui_result("Calibration finished", lines)

    def _with_reference(self, sel):
        """T0 is the reference of all offsets: measuring it in the same
        session is what lets a single toolhead be recalibrated without
        redoing the others.  T0 on its own is no use (it only defines the
        frame), so a second, already calibrated toolhead goes with it.
        Returns the toolheads to measure and the lines that explain it."""
        sel = list(sel or [])
        notes = []
        st = self.vt.store
        if not sel:
            return sel, notes
        if 0 not in sel:
            if not self.add_ref:
                return sel, notes
            return [0] + sel, [
                "T0 (%s) is measured too, as the reference: every offset is "
                "stored relative to it, so you can recalibrate a single "
                "toolhead later without redoing the others "
                "(VT_CALIBRATION_OPTIONS REF=0 turns this off)."
                % self._label(0)]
        if len(sel) > 1:
            return sel, notes
        # T0 alone
        own_valid = st is not None and st.is_valid(st.toolhead_in_slot(0))
        others = [i for i in range(1, len(self.vt._slots()))
                  if st is not None and st.is_valid(st.toolhead_in_slot(i))]
        if self.add_ref and others:
            i = others[0]
            return [0, i], [
                "T%d (%s) is measured too: T0 alone only shows this "
                "session's frame. %s" % (i, self._label(i),
                    "It also shows how much T0 changed, so the other "
                    "toolheads stay valid." if not own_valid else
                    "Nothing would be stored for T0 otherwise.")]
        if not self.add_ref:
            why = "VT_CALIBRATION_OPTIONS REF=0 is set"
        else:
            why = "no other toolhead has a valid calibration yet"
        return sel, [
            "Warning: T0 is calibrated on its own because %s. %s" % (why,
                "The other toolheads cannot be corrected for a change of "
                "T0; recalibrate them too." if not own_valid else
                "Its measurement only describes this session and will not "
                "be stored.")]

    def _begin(self, sel):
        if self._busy():
            return
        sel, ref_lines = self._with_reference(sel)
        csv = csv_slots(sel)
        self._arm(reset=not self._hold_rejected())
        lines = ["Starting: homing and preheating. Calibrating: "
                 + ", ".join("T%d (%s)" % (i, self._label(i)) for i in sel)]
        lines += ref_lines
        self._status("Calibration", lines)

        def prep():
            self._run("EXTRUDER_OFFSET_ACTION_PRESTART")
            skipped = [i for i in range(self.store.n_slots) if i not in sel]
            if skipped:
                # The stock preheat warms every tool; switch the others off.
                self._run("EXTRUDER_OFFSET_ACTION_PREHEAT " + " ".join(
                    "T%d_TEMP=0" % i for i in skipped))
        if self._guard("Could not start calibration",
                       cal_cmd("prep", sel=csv, ofs=1, pid=0, flow=0), prep):
            self._entered = True
            self._clean(sel, 0)

    def page_clean(self, gcmd):
        sel = self._sel(gcmd)
        self._clean(sel, gcmd.get_int('I', 0, minval=0, maxval=len(sel) - 1))

    def _clean(self, sel, i):
        tool = sel[i]
        csv = csv_slots(sel)
        if not self.do_clean:
            # VT_CALIBRATION_OPTIONS CLEAN=0: no filament has run since the
            # last cleaning; go straight to the cool-down to probe temperature
            self._cool(sel, i)
            return
        self._status("Calibration: toolhead T%d" % tool, [
            "Heating T%d (%s) and cleaning the nozzle automatically "
            "(%d of %d). This takes a few minutes." % (
                tool, self._label(tool), i + 1, len(sel))])

        def steps():
            self._run("EXTRUDER_OFFSET_ACTION_HEAT TOOL_ID=T%d" % tool)
            self._run("EXTRUDER_OFFSET_ACTION_AUTO_CLEAN TOOL_ID=T%d" % tool)
            self._run("EXTRUDER_OFFSET_ACTION_MANUAL_CLEAN TOOL_ID=T%d" % tool)
        if not self._guard("Cleaning T%d failed" % tool,
                           cal_cmd("clean", sel=csv, i=i), steps):
            return
        p = Prompt("Wipe the nozzle of T%d (%s)" % (tool, self._label(tool)),
                   sticky=True)
        p.text("The toolhead is at the front and the nozzle is hot. "
               "Carefully remove any filament residue from the tip, "
               "then press Done.")
        p.buttons([("Done", cal_cmd("cool", sel=csv, i=i), "primary"),
                   ("Abort calibration", cal_cmd("abort"), "error")])
        self.vt._show(p)

    def page_cool(self, gcmd):
        sel = self._sel(gcmd)
        self._cool(sel, gcmd.get_int('I', 0, minval=0, maxval=len(sel) - 1))

    def _cool(self, sel, i):
        tool = sel[i]
        csv = csv_slots(sel)
        self._status("Calibration: toolhead T%d" % tool, [
            "Cooling T%d to %d C." % (tool, self.COOL_TEMP)])
        if not self._guard(
                "Cooling T%d failed" % tool, cal_cmd("cool", sel=csv, i=i),
                lambda: self._run("EXTRUDER_OFFSET_ACTION_WAIT_COOL TOOL_ID=T%d "
                                  "TEMP=%d" % (tool, self.COOL_TEMP))):
            return
        if i + 1 < len(sel):
            self._clean(sel, i + 1)
            return
        if not self.do_clean and not self._lower_bed(sel, i):
            return
        p = Prompt("Remove the build plate", sticky=True)
        p.text(("All nozzles are clean. " if self.do_clean else "")
               + "Remove the build plate from the bed "
               "so the probe can reach the calibration fixture, then press "
               "Continue.")
        p.buttons([("Continue", cal_cmd("detect", sel=csv), "primary"),
                   ("Abort calibration", cal_cmd("abort"), "error")])
        self.vt._show(p)

    def _lower_bed(self, sel, i):
        """The stock manual-clean step lowers the bed (Z150), which is also
        what makes the build plate easy to take off.  Without the cleaning
        steps (CLEAN=0) that never happens, so do it before asking."""
        def lower():
            self._run("SAVE_GCODE_STATE NAME=VT_PLATE")
            try:
                self._run("G90")
                self._run("G0 Z%d F12000" % self.PLATE_Z)
            finally:
                self._run("RESTORE_GCODE_STATE NAME=VT_PLATE")
        return self._guard("Lowering the bed failed",
                           cal_cmd("cool", sel=csv_slots(sel), i=i), lower)

    def page_detect(self, gcmd):
        sel = self._sel(gcmd)
        self._status("Calibration", ["Checking that the build plate is "
                                     "removed..."])
        if self._guard("Build plate check failed",
                       cal_cmd("detect", sel=csv_slots(sel)),
                       lambda: self._run("EXTRUDER_OFFSET_ACTION_DETECT_PLATE "
                                         "PRESENCE=0")):
            self._probe(sel, 0)

    def page_probe(self, gcmd):
        sel = self._sel(gcmd)
        self._probe(sel, gcmd.get_int('I', 0, minval=0, maxval=len(sel) - 1))

    def _probe(self, sel, start):
        csv = csv_slots(sel)
        stock = self._stock()
        for j in range(start, len(sel)):
            tool = sel[j]
            self._status("Calibration: toolhead T%d" % tool, [
                "Probing T%d (%s), %d of %d." % (
                    tool, self._label(tool), j + 1, len(sel))])
            retry = cal_cmd("probe", sel=csv, i=j)
            self._loop = (1, self.probe_runs, False)
            if not self._guard(
                    "Probing T%d failed" % tool, retry,
                    lambda: self._run("EXTRUDER_OFFSET_ACTION_PROBE_CALIBRATE "
                                      "TOOL_ID=T%d" % tool)):
                return
            # The stock command returns quietly if the probe did not finish.
            if stock.last_xyz_result.get(self._name(tool)) is None:
                self._error_prompt("Probing T%d failed" % tool,
                                   "The probe did not produce a result.", retry)
                return
            # VT_CALIBRATION_LOOPS: extra probes of the same toolhead, for studying
            # the run-to-run variation (each one is logged).  Best effort:
            # a failure only stops the repeats; the last result is saved.
            for run in range(1, self.probe_runs):
                self._status("Calibration: toolhead T%d" % tool, [
                    "Probing T%d (%s), %d of %d, run %d of %d." % (
                        tool, self._label(tool), j + 1, len(sel),
                        run + 1, self.probe_runs)])
                self._loop = (run + 1, self.probe_runs, self.probe_dock)
                try:
                    if self.probe_dock:
                        # dock it; the probe command picks it up again
                        self._run("PARK_EXTRUDER")
                    self._run("EXTRUDER_OFFSET_ACTION_PROBE_CALIBRATE "
                              "TOOL_ID=T%d" % tool)
                except Exception as e:
                    logging.exception("virtual_toolheads: repeated probe "
                                      "failed")
                    self.repeat_notes.append(
                        "T%d: repeat %d of %d stopped (%s)" % (
                            tool, run + 1, self.probe_runs,
                            friendly_error(e)))
                    break
            self._loop = None
        self._finish(sel)

    def page_finish(self, gcmd):
        self._finish(self._sel(gcmd))

    def _finish(self, sel):
        csv = csv_slots(sel)
        self._status("Calibration", ["Saving results..."])
        stock = self._stock()

        def save():
            # With all four tools calibrated the stock code has already saved.
            if getattr(stock, 'calibration_step', None) != 'save_result_completed':
                self._run("EXTRUDER_OFFSET_ACTION_SAVE_RESULT FORCE_SAVE=1")
        if not self._guard("Saving failed", cal_cmd("finish", sel=csv), save):
            return
        try:
            self._run("EXTRUDER_OFFSET_ACTION_EXIT")
        except Exception:
            logging.exception("virtual_toolheads: calibration exit failed")
        s = self.store
        lines = []
        for slot in sel:
            th_id = s.toolhead_in_slot(slot)
            ok = s.is_valid(th_id)
            lines.append("T%d: %s" % (slot, self.vt._describe(th_id))
                         + ("" if ok else "  (result NOT recorded)"))
        lines += self._blocked_lines()
        self._disarm()
        if self.probe_runs > 1:
            lines.append("Each toolhead was probed %d times%s (VT_CAL_LOG "
                         "shows every run); the last result was saved."
                         % (self.probe_runs, ", docked between probes"
                            if self.probe_dock else ""))
        if not self.do_clean:
            lines.append("The heating and cleaning steps were skipped "
                         "(VT_CALIBRATION_OPTIONS CLEAN=0).")
        lines += self.repeat_notes
        self.repeat_notes = []
        lines += self.vt.take_frame_notes()
        lines.append("Put the build plate back on the bed.")
        lines += self.vt._run_pending_load()
        self.vt._ui_result("Calibration finished", lines)

    def page_abort(self, gcmd):
        lines = ["Calibration cancelled. Heaters are off."]
        if self._pid_active:
            # Still in the PID phase: the stock calibration has not started.
            for slot in range(self.store.n_slots if self.store else 0):
                self._heater_off(slot)
        else:
            try:
                self._run("EXTRUDER_OFFSET_ACTION_EXIT")
            except Exception as e:
                lines = ["Nothing to cancel (%s)." % friendly_error(e)]
        lines += self._blocked_lines()
        self._disarm()
        lines.append("Put the build plate back if you removed it.")
        lines += self.vt._run_pending_load()
        if self.vt.store is not None:
            self.vt._ui_result("Calibration", lines)
        else:
            self._status("Calibration", lines)


def _make_wrapper(orig, kind):
    def wrapper(self, value, *args, **kwargs):
        result = orig(self, value, *args, **kwargs)
        inst = _ACTIVE
        if inst is not None and inst.printer is self.printer:
            try:
                inst.on_slot_nozzle_set(self, kind, value)
            except Exception:
                logging.exception("virtual_toolheads: nozzle hook failed")
        return result
    wrapper._vt_wrapped = True
    wrapper.__name__ = getattr(orig, '__name__', 'wrapper')
    return wrapper


def load_config(config):
    return VirtualToolheads(config)
