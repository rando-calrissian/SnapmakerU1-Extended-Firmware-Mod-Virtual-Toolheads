#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware
# SPDX-FileCopyrightText: Copyright (c) 2026 @YOUR-GITHUB-USERNAME

# Run with: python3 test/test_virtual_toolheads.py
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRAS = os.path.join(HERE, "..", "root", "home", "lava", "klipper",
                      "klippy", "extras")
sys.path.insert(0, os.path.abspath(EXTRAS))
import virtual_toolheads as vt  # noqa: E402

BP = [[12.96, 236.92, -2.06], [12.65, 236.87, -2.13],
      [12.79, 236.73, -2.10], [12.50, 236.90, -2.01]]


def slots4(with_bp=True):
    return [{"diameter": 0.4, "volume_type": "standard",
             "base_position": BP[i] if with_bp else None} for i in range(4)]


class StoreTests(unittest.TestCase):
    """Fresh store: slot 0 holds a Left Slot toolhead (LA), slots 1-3 hold
    Right Slot toolheads (RA, RB, RC)."""
    def setUp(self):
        self.s = vt.VirtualToolheadStore.from_slots(slots4())

    def labels(self):
        return [self.s.label(i) for i in self.s.layout]

    def test_default_label_letters(self):
        self.assertEqual([vt.default_label(i) for i in (0, 1, 25, 26, 27, 51, 52)],
                         ["A", "B", "Z", "AA", "AB", "AZ", "BA"])

    def test_side_labels_and_slot_sides(self):
        self.assertEqual([vt.side_label("left", 0), vt.side_label("left", 1),
                          vt.side_label("right", 0), vt.side_label("right", 2),
                          vt.side_label("right", 26)],
                         ["LA", "LB", "RA", "RC", "RAA"])
        self.assertEqual([vt.slot_side(i) for i in range(4)],
                         ["left", "right", "right", "right"])

    def test_normalize_side(self):
        for text, want in (("left", "left"), ("LEFT", "left"), ("l", "left"),
                           (" Right ", "right"), ("R", "right")):
            self.assertEqual(vt.normalize_side(text), want)
        for bad in ("middle", "", "lefty"):
            with self.assertRaises(vt.StoreError):
                vt.normalize_side(bad)

    def test_fresh_store(self):
        s = self.s
        self.assertEqual(self.labels(), ["LA", "RA", "RB", "RC"])
        self.assertEqual([s.side(i) for i in s.layout],
                         ["left", "right", "right", "right"])
        self.assertTrue(all(s.is_valid(i) for i in s.layout))
        self.assertEqual(s.effective_base_position("th2"), BP[1])
        self.assertEqual(s.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(s.data["mod_version"], vt.VERSION)

    def test_migration_uncalibrated_slot(self):
        sl = slots4()
        sl[2]["base_position"] = None
        s = vt.VirtualToolheadStore.from_slots(sl)
        self.assertFalse(s.is_valid("th3"))
        self.assertIsNone(s.effective_base_position("th3"))
        self.assertEqual(s.invalid_reason("th3"), "never_calibrated")

    def test_add_default_labels_per_side(self):
        s = self.s
        rd = s.add_toolhead()                    # right is the default
        re_ = s.add_toolhead(side="right")
        lb = s.add_toolhead(side="left")
        lc = s.add_toolhead(side="left")
        self.assertEqual([s.label(i) for i in (rd, re_, lb, lc)],
                         ["RD", "RE", "LB", "LC"])
        self.assertEqual([s.side(i) for i in (rd, re_, lb, lc)],
                         ["right", "right", "left", "left"])
        s.rename(rd, "spare")                    # frees RD again
        self.assertEqual(s.label(s.add_toolhead()), "RD")
        self.assertFalse(s.is_valid(re_))

    def test_default_label_skips_custom_labels_in_the_way(self):
        s = self.s
        s.add_toolhead(label="RD")               # a custom name equal to the next default
        self.assertEqual(s.label(s.add_toolhead()), "RE")
        s.rename("th2", "mine")                  # freeing RA makes it the next default
        self.assertEqual(s.label(s.add_toolhead()), "RA")

    def test_label_rules(self):
        s = self.s
        with self.assertRaises(vt.StoreError):
            s.add_toolhead(label="la")           # case-insensitive duplicate
        with self.assertRaises(vt.StoreError):
            s.add_toolhead(label="bad label")
        with self.assertRaises(vt.StoreError):
            s.add_toolhead(label="x" * 17)
        with self.assertRaises(vt.StoreError):
            s.add_toolhead(side="middle")

    def test_layout_swap_and_rules(self):
        s = self.s
        s.add_toolhead()  # RD
        s.add_toolhead()  # RE
        self.assertEqual(s.set_layout(["LA", "RA", "RD", "RE"]), [2, 3])
        self.assertEqual(self.labels(), ["LA", "RA", "RD", "RE"])
        self.assertEqual(s.slot_of(s.resolve("RB")), None)
        with self.assertRaises(vt.StoreError):
            s.set_layout(["LA", "RA", "RA", "RE"])
        with self.assertRaises(vt.StoreError):
            s.set_layout(["LA", "RA", "RD"])
        with self.assertRaises(vt.StoreError):
            s.set_layout(["LA", "RA", "RD", "nope"])
        # failed attempts must not have changed anything
        self.assertEqual(self.labels(), ["LA", "RA", "RD", "RE"])

    def test_layout_side_rules(self):
        s = self.s
        lb = s.add_toolhead(side="left")
        s.add_toolhead()                         # RD
        # a Left Slot toolhead only fits slot 0 ...
        with self.assertRaises(vt.StoreError) as cm:
            s.set_layout(["LA", "LB", "RB", "RC"])
        self.assertIn("LB is a Left Slot toolhead", str(cm.exception))
        self.assertIn("slot 1", str(cm.exception))
        # ... and a Right Slot toolhead never fits slot 0
        with self.assertRaises(vt.StoreError) as cm:
            s.set_layout(["RA", "LA", "RB", "RC"])
        self.assertIn("RA is a Right Slot toolhead", str(cm.exception))
        self.assertIn("slot 0", str(cm.exception))
        # a different Left toolhead may take slot 0, rights may shuffle freely
        self.assertEqual(s.set_layout(["LB", "RC", "RB", "RA"]), [0, 1, 3])
        self.assertEqual(self.labels(), ["LB", "RC", "RB", "RA"])
        self.assertEqual(s.ids_for_slot(0), ["th1", lb])
        self.assertEqual(s.ids_for_slot(2), ["th2", "th3", "th4", "th6"])
        # nothing changed by the rejected attempts
        with self.assertRaises(vt.StoreError):
            s.set_layout(["RD", "RC", "RB", "RA"])
        self.assertEqual(self.labels(), ["LB", "RC", "RB", "RA"])

    def test_calibration_travels_with_toolhead(self):
        s = self.s
        rb = s.resolve("RB")
        s.add_toolhead()  # RD
        s.set_layout(["LA", "RA", "RD", "RC"])
        s.set_layout(["LA", "RA", "RB", "RC"])
        self.assertEqual(s.effective_base_position(rb), BP[2])

    def test_set_side_rules(self):
        s = self.s
        with self.assertRaises(vt.StoreError):   # loaded toolheads are fixed
            s.set_side(s.resolve("LA"), "right")
        rd = s.add_toolhead()
        self.assertFalse(s.set_side(rd, "right"))             # confirmed, unchanged
        self.assertTrue(s.set_side(rd, "left"))
        self.assertEqual(s.side(rd), "left")
        self.assertEqual(s.label(rd), "RD")                   # label is not renamed
        with self.assertRaises(vt.StoreError):
            s.set_side(rd, "sideways")

    def test_set_side_invalidates_when_calibrated_in_the_other_kind_of_slot(self):
        s = self.s
        rd = s.add_toolhead()
        s.record_calibration(rd, [1, 2, 3], 2)               # measured in slot 2
        self.assertTrue(s.is_valid(rd))
        s.set_side(rd, "left")                                # impossible hardware
        self.assertFalse(s.is_valid(rd))
        self.assertEqual(s.invalid_reason(rd), "side_changed")
        ld = s.add_toolhead(side="left")
        s.record_calibration(ld, [1, 2, 3], 0)
        s.set_side(ld, "left")                                # no change
        self.assertTrue(s.is_valid(ld))

    def test_delete_loaded_forbidden(self):
        with self.assertRaises(vt.StoreError):
            self.s.delete(self.s.resolve("LA"))
        e = self.s.add_toolhead()
        self.s.delete(e)
        self.assertNotIn(e, self.s.ids())

    def test_replace_always_invalidates(self):
        s = self.s
        a = s.resolve("LA")
        s.replace_nozzle(a)   # same size, new nozzle
        self.assertFalse(s.is_valid(a))
        self.assertEqual(s.invalid_reason(a), "nozzle_replaced")
        # old position kept as data but not effective
        self.assertIsNotNone(s.get(a)["calibration"]["base_position"])
        self.assertIsNone(s.effective_base_position(a))

    def test_set_nozzle_invalidates_only_on_change(self):
        s = self.s
        a = s.resolve("LA")
        self.assertFalse(s.set_nozzle(a, diameter=0.4, volume_type="standard"))
        self.assertTrue(s.is_valid(a))
        self.assertTrue(s.set_nozzle(a, diameter=0.6))
        self.assertFalse(s.is_valid(a))
        s.record_calibration(a, [1, 2, 3], 0)
        self.assertTrue(s.set_nozzle(a, material="hardened"))
        self.assertFalse(s.is_valid(a))
        s.record_calibration(a, [1, 2, 4], 0)
        self.assertTrue(s.set_nozzle(a, volume_type="high_flow"))

    def test_nozzle_value_validation(self):
        a = self.s.resolve("LA")
        for kw in ({"diameter": 0.5}, {"volume_type": "huge"},
                   {"material": "gold"}):
            with self.assertRaises(vt.StoreError):
                self.s.set_nozzle(a, **kw)
            with self.assertRaises(vt.StoreError):
                self.s.replace_nozzle(a, **kw)

    def test_record_calibration(self):
        s = self.s
        a = s.resolve("LA")
        self.assertFalse(s.record_calibration(a, BP[0], 0))        # same value
        self.assertFalse(s.record_calibration(a, [v + 1e-8 for v in BP[0]], 0))
        s.invalidate(a, "nozzle_replaced")
        # identical stale value must NOT re-validate
        self.assertFalse(s.record_calibration(a, BP[0], 0))
        self.assertFalse(s.is_valid(a))
        self.assertTrue(s.record_calibration(a, [13.0, 236.9, -2.0], 0, "T"))
        self.assertTrue(s.is_valid(a))
        self.assertEqual(s.get(a)["calibration"]["at"], "T")

    def test_epoch_invalidates_everything(self):
        s = self.s
        s.invalidate_all("machine_recal")
        self.assertEqual(len(s.invalid_loaded()), 4)
        self.assertEqual(s.invalid_reason("th1"), "epoch")
        s.record_calibration("th1", [9, 9, 9], 0)
        self.assertTrue(s.is_valid("th1"))
        self.assertFalse(s.is_valid("th2"))

    def test_roundtrip_and_validation(self):
        d = self.s.to_dict()
        s2 = vt.VirtualToolheadStore.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(s2.to_dict(), d)
        self.assertFalse(s2.migrated)
        bad = self.s.to_dict()
        bad["layout"][1] = bad["layout"][0]
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(bad)
        bad = self.s.to_dict()
        bad["layout"][0] = "th99"
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(bad)
        bad = self.s.to_dict()
        bad["schema_version"] = 99
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(bad)
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict({"schema_version": 1})
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict({"schema_version": 2})

    def test_loading_rejects_bad_sides(self):
        bad = self.s.to_dict()
        bad["toolheads"]["th1"]["side"] = "middle"
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(bad)
        bad = self.s.to_dict()                     # hand-edited: right toolhead in slot 0
        bad["layout"][0], bad["layout"][1] = bad["layout"][1], bad["layout"][0]
        with self.assertRaises(vt.StoreError) as cm:
            vt.VirtualToolheadStore.from_dict(bad)
        self.assertIn("loaded in slot", str(cm.exception))
        bad = self.s.to_dict()
        del bad["toolheads"]["th1"]["side"]
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(bad)


def v2_store(spares=()):
    """A store as 0.2/0.3 wrote it (schema 2): no PID fields, and the old
    `side_assumed` flag. spares: (label, side) pairs."""
    s = vt.VirtualToolheadStore.from_slots(slots4())
    for label, side in spares:
        s.add_toolhead(label=label, side=side)
    d = s.to_dict()
    d["schema_version"] = 2
    d["mod_version"] = "0.3"
    d.pop("pid_temp")
    d.pop("pid_adopted")
    for th in d["toolheads"].values():
        th.pop("pid")
        th["side_assumed"] = False
    return d


class MigrationTests(unittest.TestCase):
    def test_schema_2_gains_pid_fields_and_keeps_everything_else(self):
        d = v2_store(spares=(("Fast", "right"),))
        s = vt.VirtualToolheadStore.from_dict(d)
        self.assertTrue(s.migrated)
        self.assertEqual(s.migrated_from, 2)
        self.assertEqual(s.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(s.pid_temp, vt.DEFAULT_PID_TEMP)
        self.assertFalse(s.pid_adopted)
        self.assertTrue(all(s.pid(i) is None for i in s.ids()))
        self.assertTrue(all("side_assumed" not in s.get(i) for i in s.ids()))
        self.assertEqual([s.label(i) for i in s.layout], ["LA", "RA", "RB", "RC"])
        self.assertEqual(s.effective_base_position("th3"), BP[2])
        self.assertIn("Fast", [s.label(i) for i in s.ids()])

    def test_migrated_store_roundtrips_as_schema_3(self):
        s = vt.VirtualToolheadStore.from_dict(v2_store())
        again = vt.VirtualToolheadStore.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertFalse(again.migrated)
        self.assertEqual(again.to_dict(), s.to_dict())

    def test_migration_does_not_modify_its_input(self):
        d = v2_store()
        before = json.dumps(d, sort_keys=True)
        vt.VirtualToolheadStore.from_dict(d)
        self.assertEqual(json.dumps(d, sort_keys=True), before)

    def test_schema_2_with_missing_keys_is_rejected(self):
        d = v2_store()
        del d["layout"]
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)

    def test_schema_1_is_no_longer_supported(self):
        d = v2_store()
        d["schema_version"] = 1
        with self.assertRaises(vt.StoreError) as cm:
            vt.VirtualToolheadStore.from_dict(d)
        self.assertIn("schema_version", str(cm.exception))


###########################################################################
# Fakes modeled on the real kinematics/extruder.py attributes
###########################################################################

class GCodeError(Exception):
    pass


class FakeGcmd:
    error = GCodeError

    def __init__(self, **params):
        self.params = {k.upper(): str(v) for k, v in params.items()}
        self.out = []

    _REQUIRED = object()

    def get(self, name, default=_REQUIRED):
        if name in self.params:
            return self.params[name]
        if default is FakeGcmd._REQUIRED:
            raise GCodeError("Error on 'CMD': missing %s" % name)
        return default

    def get_float(self, name, default=None, minval=None, maxval=None):
        if name in self.params:
            v = float(self.params[name])
            if (minval is not None and v < minval) or \
                    (maxval is not None and v > maxval):
                raise GCodeError("%s out of range" % name)
            return v
        return default

    def get_int(self, name, default=_REQUIRED, minval=None, maxval=None):
        if name in self.params:
            v = int(self.params[name])
            if (minval is not None and v < minval) or \
                    (maxval is not None and v > maxval):
                raise GCodeError("%s out of range" % name)
            return v
        if default is FakeGcmd._REQUIRED:
            raise GCodeError("Error on 'CMD': missing %s" % name)
        return default

    def respond_info(self, msg, log=True):
        self.out.append(msg)


class FakeGcode:
    def __init__(self):
        self.cmds = {}
        self.not_ready_ok = set()
        self.raw = []
        self.output_callbacks = []
        # stock RESPOND: prints "// <msg>"
        self.cmds["RESPOND"] = lambda g: self.raw.append("// " + g.get('MSG', ''))

    def respond_raw(self, msg):
        self.raw.append(msg)
        for cb in list(self.output_callbacks):
            cb(msg)

    def respond_info(self, msg, log=True):
        self.respond_raw("// " + "\n// ".join(msg.split("\n")))

    def _process_commands(self, commands, need_ack=True):
        for line in commands:
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            parts = line.split()
            params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            g = FakeGcmd(**params)
            handler = self.cmds.get(parts[0])
            if handler is None:
                raise GCodeError("Unknown command: %s" % parts[0])
            handler(g)

    def run_script_from_command(self, script):
        self._process_commands(script.strip().split("\n"), need_ack=False)

    def run_line(self, line):
        self._process_commands([line])

    def take_prompt(self):
        lines, self.raw = self.raw, []
        return lines

    def register_command(self, name, func, when_not_ready=False, desc=None):
        if func is None:
            return self.cmds.pop(name, None)
        self.cmds[name] = func
        if when_not_ready:
            self.not_ready_ok.add(name)

    def run(self, name, **params):
        g = FakeGcmd(**params)
        self.cmds[name](g)
        return g


class FakeExtruder:
    """Mirrors the PrinterExtruder bits virtual_toolheads touches."""
    # class-level so wrapping affects all instances, like the real class
    def __init__(self, printer, name, index, diameter=0.4, vtype="standard",
                 base_position=None, tmpdir=None):
        self.printer = printer
        self.name = name
        self.extruder_index = index
        self.nozzle_diameter = diameter
        self.nozzle_volume_type = vtype
        self.nozzle_config_info = {"diameter": diameter,
                                   "diameter_v160": diameter,
                                   "volume_type": vtype}
        self.nozzle_config_path = os.path.join(tmpdir, name + "_nozzle_config.json")
        self.base_position = base_position
        self.gcode_offset = [0, 0, 0] if name == "extruder" else None
        self.active_offset_calls = 0

    def get_name(self):
        return self.name

    def get_heater(self):
        return getattr(self, "heater", None)

    def _set_nozzle_diameter(self, diameter, save=True):
        self.nozzle_diameter = diameter
        self.nozzle_config_info["diameter"] = diameter
        self.nozzle_config_info["diameter_v160"] = diameter

    def _set_nozzle_volume_type(self, volume_type, save=True):
        self.nozzle_volume_type = volume_type
        self.nozzle_config_info["volume_type"] = volume_type

    def get_center_base_position(self):
        pos = [e.base_position for e in self.printer.lookup_object("extruder_list")
               if e.base_position is not None]
        if len(pos) < 2:
            return None
        xs, ys = [p[0] for p in pos], [p[1] for p in pos]
        return [(min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, pos[0][2]]

    def update_extruder_gcode_offset(self):
        c = self.get_center_base_position()
        if c is not None and self.base_position is not None:
            self.gcode_offset = [self.base_position[i] - c[i] for i in range(3)]

    def active_gcode_offset(self):
        self.active_offset_calls += 1


class FakeToolhead:
    def __init__(self, ex):
        self.ex = ex

    def get_extruder(self):
        return self.ex


class FakePrintStats:
    state = "standby"


class FakeConfig:
    def __init__(self, printer, **opts):
        self.printer = printer
        self.opts = opts

    def get_printer(self):
        return self.printer

    def get(self, name, default=None):
        return self.opts.get(name, default)

    def getchoice(self, name, choices, default):
        return choices[self.opts.get(name, default)]

    def getboolean(self, name, default=None):
        return self.opts.get(name, default)


class FakePrinter:
    def __init__(self, tmp):
        self.tmp = tmp
        self.handlers = {}
        self.objects = {}
        self.sent = []
        self.gcode = FakeGcode()
        self.print_stats = FakePrintStats()
        self.saved_nozzle_files = []
        self.objects["gcode"] = self.gcode
        self.objects["print_stats"] = self.print_stats

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    def send_event(self, event, *params):
        self.sent.append(event)
        for cb in list(self.handlers.get(event, [])):
            cb(*params)

    def get_snapmaker_config_dir(self, name="snapmaker"):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def get_start_args(self):
        return {"software_version": "test"}

    def update_snapmaker_config_file(self, path, info):
        self.saved_nozzle_files.append((path, dict(info)))
        return True


def make_printer(tmp, bps=None, diameters=None):
    p = FakePrinter(tmp)
    names = ["extruder", "extruder1", "extruder2", "extruder3"]
    exts = []
    for i, n in enumerate(names):
        bp = None if bps is None else bps[i]
        d = 0.4 if diameters is None else diameters[i]
        exts.append(FakeExtruder(p, n, i, d, "standard", bp, tmp))
    p.objects["extruder_list"] = exts
    p.objects["toolhead"] = FakeToolhead(exts[0])
    return p, exts


LEGACY_LABELS = {"LA": "A", "RA": "B", "RB": "C", "RC": "D"}


def start(p, legacy_labels=True, add_ref=False, **opts):
    """Boot the module. Most behaviour tests are not about naming, so by
    default the four migrated toolheads are renamed to A-D; tests of the real
    default labels pass legacy_labels=False."""
    mod = vt.VirtualToolheads(FakeConfig(p, **opts))
    p.send_event("klippy:connect")
    # most tests are about the flow of a calibration, not about T0 being
    # added as its reference (that has its own tests)
    mod.calibrator.add_ref = add_ref
    if legacy_labels and mod.store is not None:
        for th_id in mod.store.ids():
            if mod.store.label(th_id) in LEGACY_LABELS:
                mod.store.rename(th_id, LEGACY_LABELS[mod.store.label(th_id)])
    return mod


class GlueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_first_boot_migrates_and_writes_store(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        self.assertIsNotNone(mod.store, mod.error)
        path = os.path.join(self.tmp, "persistent", "virtual_toolheads.json")
        self.assertTrue(os.path.exists(path))
        # slots untouched on migration
        self.assertEqual([e.base_position for e in exts], BP)
        # stock-format view written
        view = json.load(open(os.path.join(self.tmp, "snapmaker",
                                           "extruder_base_position.json")))
        self.assertEqual(view["extruder2"]["base_position"], BP[2])

    def test_layout_swap_applies_nozzle_and_base_position(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        g = p.gcode.run("VT_ADD", LABEL="E", DIAMETER=0.6, VOLUME_TYPE="high_flow")
        self.assertIn("E", g.out[0])
        # E was never calibrated -> slot 3 gets None until calibrated
        g = p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,B,C,E")
        self.assertIn("Needs calibration: E (slot 3)", g.out[0])
        e3 = exts[3]
        self.assertIsNone(e3.base_position)
        self.assertEqual(e3.gcode_offset, None)
        self.assertEqual(e3.nozzle_diameter, 0.6)
        self.assertEqual(e3.nozzle_volume_type, "high_flow")
        # nozzle config file persisted through the stock helper
        self.assertTrue(any(path.endswith("extruder3_nozzle_config.json")
                            and info["diameter"] == 0.6 and
                            info["diameter_v160"] == 0.6
                            for path, info in p.saved_nozzle_files))
        # view reflects the null
        view = json.load(open(os.path.join(self.tmp, "snapmaker",
                                           "extruder_base_position.json")))
        self.assertIsNone(view["extruder3"]["base_position"])
        # swap D back in: its calibration returns with it
        p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,B,C,D")
        self.assertEqual(exts[3].base_position, BP[3])
        self.assertEqual(exts[3].nozzle_diameter, 0.4)
        # offsets are recomputed against the new layout
        self.assertIsNotNone(exts[3].gcode_offset)

    def test_calibration_event_is_recorded_against_loaded_toolhead(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_ADD", LABEL="E")
        p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,B,C,E")
        # stock SAVE_RESULT: sets slot attribute then sends the event
        exts[3].base_position = [13.1, 236.8, -2.2]
        p.send_event(vt.OFFSET_EVENT)
        s = mod.store
        e = s.resolve("E")
        self.assertTrue(s.is_valid(e))
        self.assertEqual(s.effective_base_position(e), [13.1, 236.8, -2.2])
        self.assertEqual(s.get(e)["calibration"]["slot"], 3)
        # D keeps its own data
        self.assertEqual(s.effective_base_position(s.resolve("D")), BP[3])
        # persisted
        data = json.load(open(os.path.join(self.tmp, "persistent",
                                           "virtual_toolheads.json")))
        self.assertTrue(data["toolheads"][e]["calibration"]["valid"])

    def test_replace_nozzle_on_loaded_toolhead_clears_slot(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        g = p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD="B", DIAMETER=0.2)
        self.assertIn("NEEDS CALIBRATION (nozzle_replaced)", g.out[0])
        self.assertIsNone(exts[1].base_position)
        self.assertEqual(exts[1].nozzle_diameter, 0.2)
        # center now computed from the three remaining valid slots
        self.assertIsNotNone(exts[0].gcode_offset)

    def test_replace_nozzle_on_spare_does_not_touch_slots(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_ADD", LABEL="E")
        before = [(e.base_position, e.nozzle_diameter) for e in exts]
        sent = len(p.sent)
        p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD="E", DIAMETER=0.8)
        self.assertEqual([(e.base_position, e.nozzle_diameter) for e in exts],
                         before)
        self.assertEqual(len(p.sent), sent)

    def test_stock_ui_nozzle_change_is_mirrored_and_invalidates(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        # what SET_NOZZLE_PROPERTIES / the webhook end up calling
        exts[2]._set_nozzle_diameter(0.6, False)
        exts[2]._set_nozzle_volume_type("high_flow", False)
        s = mod.store
        c = s.resolve("C")
        self.assertEqual(s.get(c)["nozzle"]["diameter"], 0.6)
        self.assertEqual(s.get(c)["nozzle"]["volume_type"], "high_flow")
        self.assertFalse(s.is_valid(c))
        # live slot untouched (stock keeps old offset until recalibrated)
        self.assertEqual(exts[2].base_position, BP[2])
        # stale identical value must not re-validate on an unrelated event
        p.send_event(vt.OFFSET_EVENT)
        self.assertFalse(s.is_valid(c))
        # new calibration does
        exts[2].base_position = [12.9, 236.5, -2.0]
        p.send_event(vt.OFFSET_EVENT)
        self.assertTrue(s.is_valid(c))

    def test_stock_nozzle_change_same_value_does_not_invalidate(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        exts[0]._set_nozzle_diameter(0.4, False)
        self.assertTrue(mod.store.is_valid(mod.store.resolve("A")))

    def test_our_own_setter_calls_do_not_loop_or_invalidate(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_SET_NOZZLE", TOOLHEAD="A", DIAMETER=0.6)
        s = mod.store
        a = s.resolve("A")
        self.assertEqual(s.get(a)["nozzle"]["diameter"], 0.6)
        self.assertFalse(s.is_valid(a))
        # only A changed
        self.assertTrue(all(s.is_valid(s.resolve(x)) for x in "BCD"))

    def test_set_nozzle_no_change_is_noop(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        g = p.gcode.run("VT_SET_NOZZLE", TOOLHEAD="A", DIAMETER=0.4)
        self.assertTrue(g.out[0].startswith("No change"))
        self.assertTrue(mod.store.is_valid(mod.store.resolve("A")))

    def test_second_boot_loads_store_and_applies_it(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_ADD", LABEL="E", DIAMETER=0.8)
        p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,B,E,D")
        # reboot: stock files say whatever; store must win
        p2, exts2 = make_printer(self.tmp, BP)
        mod2 = start(p2)
        self.assertEqual(mod2.store.layout, mod.store.layout)
        self.assertEqual(exts2[2].nozzle_diameter, 0.8)
        self.assertIsNone(exts2[2].base_position)   # E uncalibrated
        self.assertEqual(exts2[3].base_position, BP[3])

    def test_restart_does_not_double_wrap_or_use_stale_instance(self):
        p, exts = make_printer(self.tmp, BP)
        mod1 = start(p)
        p2, exts2 = make_printer(self.tmp, BP)
        mod2 = start(p2)
        # the class wrapper must be applied exactly once
        w = FakeExtruder._set_nozzle_diameter
        self.assertTrue(getattr(w, "_vt_wrapped", False))
        self.assertFalse(getattr(w.__wrapped__ if hasattr(w, "__wrapped__") else
                                 object(), "_vt_wrapped", False))
        exts2[0]._set_nozzle_diameter(0.6, False)
        self.assertFalse(mod2.store.is_valid(mod2.store.resolve("A")))
        # and the old instance (other printer) is not touched
        self.assertTrue(mod1.store.is_valid(mod1.store.resolve("A")))

    def test_corrupt_store_disables_feature_without_touching_it(self):
        d = os.path.join(self.tmp, "persistent")
        os.makedirs(d)
        path = os.path.join(d, "virtual_toolheads.json")
        open(path, "w").write("{not json")
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        self.assertIsNone(mod.store)
        self.assertEqual(open(path).read(), "{not json")
        self.assertEqual([e.base_position for e in exts], BP)
        with self.assertRaises(GCodeError):
            p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,B,C,D")
        g = p.gcode.run("VT_STATUS")
        self.assertIn("unavailable", g.out[0])
        # VT_CHECK never blocks when the feature is down
        p.gcode.run("VT_CHECK")

    def test_blocked_while_printing(self):
        p, exts = make_printer(self.tmp, BP)
        start(p)
        p.print_stats.state = "printing"
        for cmd, kw in (("VT_ADD", {}), ("VT_SET_LAYOUT", {"LAYOUT": "A,B,C,D"}),
                        ("VT_REPLACE_NOZZLE", {"TOOLHEAD": "A"})):
            with self.assertRaises(GCodeError):
                p.gcode.run(cmd, **kw)

    def test_print_guard_modes(self):
        for mode in ("off", "warn", "block"):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            p, exts = make_printer(tmp, BP)
            start(p, print_guard=mode)
            p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD="C")
            if mode == "block":
                with self.assertRaises(GCodeError):
                    p.gcode.run("VT_CHECK")
            else:
                g = p.gcode.run("VT_CHECK")
                self.assertEqual(bool(g.out), mode == "warn")

    def test_user_errors_become_gcode_errors(self):
        p, exts = make_printer(self.tmp, BP)
        start(p)
        for cmd, kw in (("VT_SET_LAYOUT", {"LAYOUT": "A,B,C"}),
                        ("VT_SET_LAYOUT", {"LAYOUT": "A,A,C,D"}),
                        ("VT_DELETE", {"TOOLHEAD": "A"}),
                        ("VT_REPLACE_NOZZLE", {"TOOLHEAD": "Q"}),
                        ("VT_SET_NOZZLE", {"TOOLHEAD": "A", "DIAMETER": 0.5}),
                        ("VT_RENAME", {"TOOLHEAD": "A", "LABEL": "B"})):
            with self.assertRaises(GCodeError, msg=(cmd, kw)):
                p.gcode.run(cmd, **kw)

    def test_invalidate_all_bumps_epoch(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_INVALIDATE", TOOLHEAD="ALL")
        self.assertEqual(mod.store.epoch, 2)
        self.assertTrue(all(e.base_position is None for e in exts))

    def test_reactivates_offset_of_active_extruder(self):
        p, exts = make_printer(self.tmp, BP)
        start(p)
        before = exts[0].active_offset_calls
        p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,B,C,D")
        self.assertGreater(exts[0].active_offset_calls, before)

    def test_status_object(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD="D")
        st = mod.get_status()
        json.dumps(st)  # must be JSON-serialisable for moonraker
        self.assertEqual(st["layout"], list("ABCD"))
        self.assertEqual(st["invalid_loaded"], ["D"])
        self.assertEqual(st["toolheads"]["D"]["invalid_reason"], "nozzle_replaced")
        self.assertEqual(st["toolheads"]["A"]["slot"], 0)

    def test_status_offers_calibration_data_to_the_card(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        st = mod.get_status()
        json.dumps(st)
        cal = st["calibrate"]
        self.assertEqual(cal["pid_presets"], list(vt.PID_TEMP_PRESETS))
        self.assertEqual(cal["pid_range"], list(vt.PID_TEMP_RANGE))
        self.assertIn("flow_possible", cal)
        needs = st["toolheads"]["A"]["needs"]
        self.assertEqual(set(needs), {"ofs", "pid", "flow"})


class CardModeTests(unittest.TestCase):
    """VT_UI ... CARD=1 (sent by the dashboard card): the result is a notice
    in the status, not a Fluidd dialog."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_status_lists_options_and_backups(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        st = mod.get_status()
        json.dumps(st)
        self.assertEqual(st["options"]["diameters"], list(vt.VALID_DIAMETERS))
        self.assertEqual(st["options"]["materials"], list(vt.VALID_MATERIALS))
        self.assertEqual(st["backup_list"], [])
        p.gcode.run("VT_BACKUP")
        bl = mod.get_status()["backup_list"]
        self.assertEqual(len(bl), 1)
        self.assertEqual(set(bl[0]), {"name", "when", "summary"})
        self.assertTrue(bl[0]["summary"].startswith("layout "))
        self.assertIn("lines", mod.get_status()["boards"])

    def test_status_does_little_work_per_call(self):
        """The status is read about four times a second: it lists the backup
        folder only when it changed, reads the print task once, and never
        starts a Spoolman lookup."""
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run("VT_BACKUP")
        mod.get_status()                                  # fills the cache
        calls = {"task": 0, "listdir": 0, "lookup": 0}
        orig_task, orig_listdir = mod._task_status, vt.os.listdir
        def task():
            calls["task"] += 1
            return orig_task()
        def listdir(path):
            calls["listdir"] += 1
            return orig_listdir(path)
        mod._task_status = task
        mod._request_filament_lookup = lambda spool: calls.__setitem__("lookup", calls["lookup"] + 1)
        vt.os.listdir = listdir
        try:
            for _ in range(5):
                mod.get_status()
        finally:
            vt.os.listdir = orig_listdir
        self.assertEqual(calls["listdir"], 0)
        self.assertEqual(calls["lookup"], 0)
        self.assertLessEqual(calls["task"], 5)            # once per call
        # the cache follows new, deleted and restored backups
        p.gcode.run("VT_BACKUP")
        self.assertEqual(len(mod.get_status()["backup_list"]), 2)
        name = mod.get_status()["backup_list"][0]["name"]
        p.gcode.run("VT_BACKUP_DELETE", NAME=name)
        self.assertEqual(len(mod.get_status()["backup_list"]), 1)

    def test_nozzle_apply_shows_a_notice_only(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI PAGE=nozzle_apply CARD=1 TOOLHEAD=A MODE=replace DIAMETER=0.6")
        self.assertEqual(p.gcode.take_prompt(), [])
        nt = mod.get_status()["notice"]
        self.assertEqual(nt["title"], "Nozzle updated")
        self.assertEqual(mod.store.get(mod.store.resolve("A"))["nozzle"]["diameter"], 0.6)

    def test_without_card_a_dialog_opens(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI PAGE=nozzle_apply TOOLHEAD=A MODE=replace DIAMETER=0.6")
        self.assertTrue(p.gcode.take_prompt())

    def test_backup_restore_and_delete_from_the_card(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run_line("VT_UI PAGE=backup_apply CARD=1")
        name = mod.get_status()["backup_list"][0]["name"]
        self.assertEqual(mod.get_status()["notice"]["title"], "Backup")
        p.gcode.run_line("VT_UI PAGE=restore_apply CARD=1 NAME=" + name)
        self.assertEqual(mod.get_status()["notice"]["title"], "Backup restored")
        p.gcode.run_line("VT_UI PAGE=backup_delete_apply CARD=1 NAME=" + name)
        self.assertNotIn(name, [b["name"] for b in mod.get_status()["backup_list"]])

    def test_card_flag_does_not_stick(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p)
        p.gcode.run_line("VT_UI PAGE=backup_apply CARD=1")
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI PAGE=backup_apply")
        self.assertTrue(p.gcode.take_prompt())


def prompt_buttons(lines):
    """-> list of (label, gcode, color) from captured '// action:' lines."""
    out = []
    for l in lines:
        for key in ("prompt_button ", "prompt_footer_button "):
            if l.startswith("// action:" + key):
                label, gcode, color = l[len("// action:" + key):].split("|")
                out.append((label, gcode, color))
    return out


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def boot(self, tmp=None, spares=2, legacy=True):
        p, exts = make_printer(tmp or self.tmp, BP)
        FakeStockCalibration(p, exts)
        mod = start(p, legacy_labels=legacy)
        for label in ("E", "F")[:spares]:
            p.gcode.run("VT_ADD", LABEL=label)
        p.gcode.take_prompt()
        return p, exts, mod

    def test_assign_slot_swaps(self):
        L = list("ABCD")
        self.assertEqual(vt.assign_slot(L, 2, "E"), list("ABED"))
        self.assertEqual(vt.assign_slot(L, 0, "D"), list("DBCA"))
        self.assertEqual(vt.assign_slot(L, 1, "B"), L)
        self.assertEqual(L, list("ABCD"))   # input untouched

    def test_prompt_structure(self):
        p = vt.Prompt("T")
        p.text("hi")
        p.buttons([("a", "X", "primary"), ("b", "Y", None)], per_row=1)
        p.footer("Close", "Z")
        self.assertEqual(p.finish(), [
            "prompt_begin T", "prompt_text hi",
            "prompt_button_group_start", "prompt_button a|X|primary",
            "prompt_button_group_end",
            "prompt_button_group_start", "prompt_button b|Y|secondary",
            "prompt_button_group_end",
            "prompt_footer_button Close|Z|info", "prompt_show"])

    def test_main_menu(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_UI")
        lines = p.gcode.take_prompt()
        self.assertEqual(lines[0], "// action:prompt_begin Virtual toolheads v%s"
                         % vt.VERSION)
        self.assertEqual(lines[-1], "// action:prompt_show")
        text = "\n".join(lines)
        self.assertIn("T0: A 0.4mm standard/stainless  calibrated", text)
        self.assertIn("T1: B 0.4mm standard/stainless  calibrated", text)
        self.assertNotIn("[Left Slot]", text.split("Spare:")[0])   # not on slot lines
        self.assertIn("Spare: E [Right Slot], F [Right Slot]", text)
        gcodes = [g for _, g, _ in prompt_buttons(lines)]
        self.assertIn("VT_UI PAGE=layout", gcodes)
        self.assertIn("VT_UI PAGE=nozzle", gcodes)
        self.assertIn("VT_UI PAGE=add", gcodes)
        self.assertIn("VT_CALIBRATE", gcodes)

    # ---- Unload filament option on the layout screen ----
    class Sensor:
        def __init__(self, detected):
            self.detected = detected

        def get_status(self, eventtime):
            return {"filament_detected": self.detected, "enabled": True}

    def unload_setup(self, loaded):
        p, exts, mod = self.boot()
        self.unloads = []
        self.unload_fail = set()

        def auto_feeding(g):
            n = g.get_int('EXTRUDER')
            if n in self.unload_fail:
                raise GCodeError("unload failed")
            self.unloads.append((n, g.get_int('UNLOAD')))
            self.order.append("unload%d" % n)
        self.order = []
        self.dock_fail = False

        def park(g):
            if self.dock_fail:
                raise GCodeError("park failed")
            self.order.append("dock")
        p.gcode.cmds["AUTO_FEEDING"] = auto_feeding
        p.gcode.cmds["PARK_EXTRUDER"] = park
        for i in range(4):
            p.objects["filament_motion_sensor e%d_filament" % i] = \
                self.Sensor(i in loaded)
        return p, exts, mod

    # ---- calibrate screen after the restart ----
    def swap_marker(self, mod):
        return mod._store_path() + ".swap"

    def test_apply_with_unload_leaves_a_swap_marker(self):
        p, exts, mod = self.unload_setup({2})
        self.assertFalse(os.path.exists(self.swap_marker(mod)))
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        self.assertTrue(os.path.exists(self.swap_marker(mod)))
        p.gcode.take_prompt()

    def test_apply_without_unload_leaves_no_marker(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D")
        self.assertFalse(os.path.exists(self.swap_marker(mod)))

    def restart(self, p, mod_old, needy=None):
        """A firmware restart: a new module instance connects, then ready."""
        mod = start(p, legacy_labels=False)
        p.gcode.take_prompt()
        if needy is not None:
            # the fake printer keeps the old instance's handlers registered
            for m in (mod, mod_old):
                m.calibrator._needy = lambda: needy
        p.send_event("klippy:ready")
        return mod

    def test_restart_after_swap_opens_calibrate_when_needed(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        mod2 = self.restart(p, mod)
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("Calibrat", text)
        self.assertIn("VT_CAL PAGE=prep", text)
        self.assertFalse(os.path.exists(self.swap_marker(mod2)))

    def test_restart_after_swap_skips_when_everything_is_calibrated(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,C,D UNLOAD=1")
        p.gcode.take_prompt()
        mod2 = self.restart(p, mod, needy=[])
        self.assertEqual(p.gcode.take_prompt(), [])

    class TimerReactor:
        NEVER = float("inf")
        def __init__(self):
            self.now = 0.0
            self.timers = []
        def monotonic(self):
            return self.now
        def register_timer(self, cb, when):
            self.timers.append((when, cb))
        def run(self, seconds):
            """Advance the clock, firing the timers that come due."""
            end = self.now + seconds
            while True:
                due = sorted(t for t in self.timers if t[0] <= end)
                if not due:
                    break
                when, cb = due[0]
                self.timers.remove(due[0])
                self.now = max(self.now, when)
                cb(self.now)
            self.now = end

    def flow_only_restart(self, identified_after=None):
        """Restart after a swap where the only gap is the flow K of a filament
        that is not identified yet. Returns (printer, reactor)."""
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        rx = self.TimerReactor()
        p.get_reactor = lambda: rx
        mod2 = start(p, legacy_labels=False)
        p.gcode.take_prompt()
        state = {"ident": False}
        self.mods = (mod, mod2)
        for m in (mod, mod2):
            m.calibrator._needy = lambda: [2]
            m.calibrator._needs = lambda slot: {"ofs": False, "pid": False, "flow": True}
            m.flow_identified = lambda slot: state["ident"]
        p.send_event("klippy:ready")
        rx.run(vt.SWAP_RESUME_DELAY + 0.1)
        return p, rx, state

    def test_unidentified_filament_waits_then_leaves_the_screen_closed(self):
        p, rx, state = self.flow_only_restart()
        p.gcode.take_prompt()
        rx.run(vt.FILAMENT_ID_WAIT + 2)
        self.assertNotIn("VT_CAL PAGE=prep", "\n".join(p.gcode.take_prompt()))

    def test_flow_gap_alone_never_opens_the_screen(self):
        p, rx, state = self.flow_only_restart()
        p.gcode.take_prompt()
        state["ident"] = True            # identified, and its K is still missing
        rx.run(vt.FILAMENT_ID_POLL + 0.1)
        self.assertNotIn("VT_CAL PAGE=prep", "\n".join(p.gcode.take_prompt()))

    def test_filament_identified_with_the_k_found_opens_nothing(self):
        p, rx, state = self.flow_only_restart()
        p.gcode.take_prompt()
        state["ident"] = True
        # the K turned up once the filament was known
        for m in self.mods:
            m.calibrator._needy = lambda: []
        rx.run(vt.FILAMENT_ID_POLL + 0.1)
        self.assertNotIn("VT_CAL PAGE=prep", "\n".join(p.gcode.take_prompt()))

    def card_restart(self, announced):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        mod2 = start(p, legacy_labels=False)
        p.gcode.take_prompt()
        if announced:
            # the fake printer keeps the old instance's handlers registered
            for m in (mod, mod2):
                m.cmd_VT_CARD(None)
        p.send_event("klippy:ready")
        # whichever instance handled the ready event holds the request
        return p, next((m for m in (mod, mod2) if m._auto_cal is not None), mod2)

    def test_with_the_card_the_calibrate_request_goes_to_the_card(self):
        p, mod2 = self.card_restart(True)
        self.assertEqual(p.gcode.take_prompt(), [])          # no dialog
        rid = mod2.get_status()["auto_calibrate"]
        self.assertTrue(rid)

    def test_without_the_card_the_dialog_opens(self):
        p, mod2 = self.card_restart(False)
        self.assertIn("VT_CAL PAGE=prep", "\n".join(p.gcode.take_prompt()))
        self.assertIsNone(mod2.get_status()["auto_calibrate"])

    def test_calibrate_request_ends_with_the_need_or_the_time(self):
        p, mod2 = self.card_restart(True)
        self.assertTrue(mod2.get_status()["auto_calibrate"])
        mod2._auto_cal_at -= vt.AUTO_CAL_TTL + 1
        self.assertIsNone(mod2.get_status()["auto_calibrate"])
        mod2._auto_cal_at += vt.AUTO_CAL_TTL + 1
        mod2.calibrator._needy = lambda: []
        self.assertIsNone(mod2.get_status()["auto_calibrate"])

    def test_a_plain_restart_opens_nothing(self):
        p, exts, mod = self.unload_setup(set())
        mod2 = self.restart(p, mod)
        self.assertEqual(p.gcode.take_prompt(), [])

    def test_a_stale_marker_is_ignored_and_removed(self):
        p, exts, mod = self.unload_setup({2})
        with open(self.swap_marker(mod), "w") as f:
            json.dump({"at": time.time() - vt.SWAP_MARKER_TTL - 60}, f)
        mod2 = self.restart(p, mod)
        self.assertEqual(p.gcode.take_prompt(), [])
        self.assertFalse(os.path.exists(self.swap_marker(mod2)))

    class Feeder:
        def __init__(self, chans):
            self.chans = chans

        def get_status(self, eventtime=None):
            return {"extruder%d" % i: {"filament_detected": d,
                                       "channel_state": s}
                    for i, (d, s) in self.chans.items()}

    def feeder(self, p, states, sensors=None):
        """states: slot -> (filament detected in the lane, channel state).
        sensors: slots whose motion sensor sees filament (default: all that
        are detected, as a sensor also sees an unloaded spool)."""
        for i in range(4):
            seen = (i in sensors) if sensors is not None \
                else states.get(i, (False, ""))[0]
            p.objects["filament_motion_sensor e%d_filament" % i] = \
                self.Sensor(seen)
        full = {i: states.get(i, (False, "unload_finish")) for i in range(4)}
        p.objects["filament_feed left"] = self.Feeder(
            {0: full[0], 1: full[1]})
        p.objects["filament_feed right"] = self.Feeder(
            {2: full[2], 3: full[3]})

    def test_unload_only_what_is_loaded_in_the_toolhead(self):
        p, exts, mod = self.unload_setup(set())
        # T2 loaded; T3's filament is only on the spool (sensor still sees it)
        self.feeder(p, {2: (True, "load_finish"), 3: (True, "unload_finish")})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertEqual(self.unloads, [(2, 1)])
        self.assertIn("T2: filament unloaded", text)
        self.assertNotIn("T3: filament unloaded", text)
        self.assertNotIn("T3: skipped", text)

    def test_unload_skips_a_toolhead_that_is_already_unloading(self):
        p, exts, mod = self.unload_setup(set())
        self.feeder(p, {2: (True, "unload_doing"), 3: (True, "load_finish")})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertEqual(self.unloads, [(3, 1)])
        self.assertIn("T2: skipped, the filament is already being unloaded",
                      text)

    def test_unload_skips_a_toolhead_that_is_loading(self):
        p, exts, mod = self.unload_setup(set())
        self.feeder(p, {2: (True, "load_feeding")})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertEqual(self.unloads, [])
        self.assertIn("T2: skipped", text)
        self.assertNotIn("Unloading filament", text)    # no status screen

    def test_everything_already_unloaded_unloads_and_docks_nothing(self):
        p, exts, mod = self.unload_setup(set())
        self.feeder(p, {2: (True, "unload_finish"), 3: (True, "preload_finish"), 0: (False, "wait_insert")})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertEqual(self.unloads, [])
        self.assertEqual(self.order, [])               # nothing docked
        self.assertIn("no filament loaded (feeder state:", text)
        self.assertNotIn("preload_finish", text)
        self.assertNotIn("wait_insert", text)
        self.assertIn("Filament Preloaded", text)

    def test_a_failed_unload_state_falls_back_to_the_sensor(self):
        p, exts, mod = self.unload_setup(set())
        self.feeder(p, {2: (True, "unload_fail")}, sensors={2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        self.assertEqual(self.unloads, [(2, 1)])

    def test_unload_toggle_defaults_off_and_is_carried(self):
        p, exts, mod = self.unload_setup({2, 3})
        p.gcode.run_line("VT_UI PAGE=layout PENDING=A,B,E,F")
        lines = p.gcode.take_prompt()
        btn = {l: g for l, g, _ in prompt_buttons(lines)}
        self.assertIn("Unload filament: off", btn)
        self.assertEqual(btn["Unload filament: off"],
                         "VT_UI PAGE=layout PENDING=A,B,E,F UNLOAD=1")
        self.assertEqual(btn["Apply"],
                         "VT_UI PAGE=layout_apply PENDING=A,B,E,F")
        p.gcode.run_line("VT_UI PAGE=layout PENDING=A,B,E,F UNLOAD=1")
        lines = p.gcode.take_prompt()
        btn = {l: g for l, g, _ in prompt_buttons(lines)}
        self.assertIn("Unload filament: on", btn)
        self.assertEqual(btn["Apply"],
                         "VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        self.assertIn("VT_UI PAGE=layout_slot SLOT=2 PENDING=A,B,E,F UNLOAD=1",
                      btn.values())
        # the toggle survives picking another toolhead for a slot
        p.gcode.run_line("VT_UI PAGE=layout_slot SLOT=2 PENDING=A,B,E,F "
                         "UNLOAD=1")
        btn = {l: g for l, g, _ in prompt_buttons(p.gcode.take_prompt())}
        self.assertTrue(all("UNLOAD=1" in g for l, g in btn.items()
                            if g.startswith("VT_UI PAGE=layout ")))

    def test_restart_button_is_on_its_own_row_above_the_others(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        lines = p.gcode.take_prompt()
        groups, cur = [], None
        for line in lines:
            if line.endswith("prompt_button_group_start"):
                cur = []
            elif line.endswith("prompt_button_group_end"):
                groups.append(cur)
            elif "prompt_button " in line and cur is not None:
                cur.append(line.split("prompt_button ")[1].split("|")[0])
        self.assertEqual(groups[0], ["Restart firmware"])
        self.assertIn("Menu", groups[1])
        self.assertIn("Close", groups[1])

    def test_a_dismissal_queued_behind_the_unload_does_not_close_the_result(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        # Fluidd's close, queued during the unload, runs right afterwards
        p.gcode.run_line("RESPOND TYPE=command MSG=action:prompt_end")
        again = p.gcode.take_prompt()
        self.assertIn("// action:prompt_begin Layout applied", again)
        self.assertEqual(again[-1], "// action:prompt_show")

    def test_a_later_dismissal_of_a_normal_dialog_is_honoured(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        mod._shown_at -= vt.STALE_DISMISS + 1
        p.gcode.run_line("RESPOND TYPE=command MSG=action:prompt_end")
        self.assertEqual(p.gcode.take_prompt(), ["// action:prompt_end"])

    def test_restart_button_closes_the_dialog_and_restarts(self):
        p, exts, mod = self.unload_setup({2})
        restarts = []
        p.gcode.cmds["FIRMWARE_RESTART"] = lambda g: restarts.append(1)
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        p.gcode.take_prompt()
        self.assertIn("VT_RESTART", p.gcode.not_ready_ok)   # works after a shutdown
        p.gcode.run_line("VT_RESTART")
        self.assertEqual(p.gcode.take_prompt(), ["// action:prompt_end"])
        self.assertEqual(restarts, [1])
        # and nothing re-shows the dialog afterwards
        mod._shown_at -= vt.STALE_DISMISS + 1
        p.gcode.run_line("RESPOND TYPE=command MSG=action:prompt_end")
        self.assertEqual(p.gcode.take_prompt(), ["// action:prompt_end"])

    def test_no_toggle_when_nothing_changes(self):
        p, exts, mod = self.unload_setup({0})
        p.gcode.run_line("VT_UI PAGE=layout")
        texts = [l for l, _, _ in prompt_buttons(p.gcode.take_prompt())]
        self.assertFalse(any(t.startswith("Unload") for t in texts))

    def test_apply_with_unload_unloads_only_swapped_loaded_slots(self):
        p, exts, mod = self.unload_setup({1, 2, 3})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertEqual(self.unloads, [(2, 1), (3, 1)])   # slot 1 unchanged
        self.assertIn("T2: filament unloaded", text)
        self.assertIn("T3: filament unloaded", text)
        self.assertIn("no power cycle is needed", text)
        self.assertIn("click Restart firmware", text)
        self.assertIn("Calibrate screen opens after the restart", text)
        self.assertIn("Restart firmware|VT_RESTART|warning", text)
        self.assertEqual(mod._labels(), list("ABEF"))

    def test_unload_shows_status_screens_then_docks_then_result(self):
        p, exts, mod = self.unload_setup({2, 3})
        orig = p.gcode.cmds["AUTO_FEEDING"]
        seen = []

        def spy(g):
            seen.append("\n".join(p.gcode.raw))   # screen up while unloading
            orig(g)
        p.gcode.cmds["AUTO_FEEDING"] = spy
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        self.assertIn("Unloading T2: E (1 of 2)", seen[0])
        self.assertIn("Unloading T3: F (2 of 2)", seen[1])
        self.assertEqual(self.order, ["unload2", "unload3", "dock"])
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("Docking the toolhead", text)
        self.assertLess(text.index("Docking the toolhead"),
                        text.index("Layout applied"))
        self.assertIn("Toolhead docked", text)

    def test_no_dock_when_every_unload_failed(self):
        p, exts, mod = self.unload_setup({2})
        self.unload_fail = {2}
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        self.assertEqual(self.order, [])
        text = "\n".join(p.gcode.take_prompt())
        self.assertNotIn("docked", text)

    def test_dock_failure_is_reported(self):
        p, exts, mod = self.unload_setup({2})
        self.dock_fail = True
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,D UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("Docking failed", text)
        self.assertEqual(mod._labels(), list("ABED"))

    def test_apply_without_unload_leaves_filament(self):
        p, exts, mod = self.unload_setup({2, 3})
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F")
        self.assertEqual(self.unloads, [])
        self.assertNotIn("VT_RESTART",
                         "\n".join(p.gcode.take_prompt()))
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,C,D UNLOAD=1")
        self.assertIn("VT_RESTART", "\n".join(p.gcode.take_prompt()))
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F")
        self.assertNotIn("no power cycle is needed",
                         "\n".join(p.gcode.take_prompt()))

    def test_unload_skips_empty_slots_and_says_so(self):
        p, exts, mod = self.unload_setup(set())
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertEqual(self.unloads, [])
        self.assertIn("no filament loaded (feeder state:", text)
        self.assertIn("no power cycle is needed", text)

    def test_failed_unload_is_reported_and_the_rest_still_run(self):
        p, exts, mod = self.unload_setup({2, 3})
        self.unload_fail = {2}
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F UNLOAD=1")
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("T2: unload failed", text)
        self.assertEqual(self.unloads, [(3, 1)])
        self.assertEqual(mod._labels(), list("ABEF"))   # layout still applied

    def test_cancel_unloads_nothing(self):
        p, exts, mod = self.unload_setup({2, 3})
        p.gcode.run_line("VT_UI PAGE=layout PENDING=A,B,E,F UNLOAD=1")
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI PAGE=main")
        self.assertEqual(self.unloads, [])
        self.assertEqual(mod._labels(), list("ABCD"))

    def test_console_set_layout_unload(self):
        p, exts, mod = self.unload_setup({2})
        p.gcode.run_line("VT_SET_LAYOUT LAYOUT=A,B,E,D UNLOAD=1")
        self.assertEqual(self.unloads, [(2, 1)])

    def test_close_emits_prompt_end(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_UI", PAGE="close")
        self.assertEqual(p.gcode.take_prompt(), ["// action:prompt_end"])

    def test_layout_flow_stages_then_applies_once(self):
        p, exts, mod = self.boot()
        sent = len(p.sent)
        # open layout, pick slot T2, pick toolhead E, pick slot T3, pick F
        p.gcode.run_line("VT_UI PAGE=layout")
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI PAGE=layout_slot SLOT=2 PENDING=A,B,C,D")
        btn = {l: g for l, g, _ in prompt_buttons(p.gcode.take_prompt())}
        self.assertEqual(btn["E"], "VT_UI PAGE=layout PENDING=A,B,E,D")
        self.assertEqual(btn["B (T1)"], "VT_UI PAGE=layout PENDING=A,C,B,D")
        self.assertNotIn("A (T0)", btn)      # A is a Left Slot toolhead: no fit
        # nothing applied while staging
        self.assertEqual(mod.store.layout, ["th1", "th2", "th3", "th4"])
        self.assertEqual(len(p.sent), sent)
        p.gcode.run_line("VT_UI PAGE=layout PENDING=A,B,E,F")
        lines = p.gcode.take_prompt()
        gcodes = [g for _, g, _ in prompt_buttons(lines)]
        self.assertIn("VT_UI PAGE=layout_apply PENDING=A,B,E,F", gcodes)
        p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=A,B,E,F")
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("T2=E, T3=F", text)
        self.assertIn("Needs calibration: E (T2), F (T3)", text)
        self.assertEqual(mod._labels(), list("ABEF"))
        self.assertIsNone(exts[2].base_position)

    def test_layout_page_has_no_apply_when_unchanged(self):
        p, exts, mod = self.boot()
        p.gcode.run_line("VT_UI PAGE=layout")
        gcodes = [g for _, g, _ in prompt_buttons(p.gcode.take_prompt())]
        self.assertFalse(any("layout_apply" in g for g in gcodes))

    def test_nozzle_flow_replace(self):
        p, exts, mod = self.boot()
        p.gcode.run_line("VT_UI PAGE=nozzle")
        btn = {l: g for l, g, _ in prompt_buttons(p.gcode.take_prompt())}
        self.assertEqual(btn["B (T1)"], "VT_UI PAGE=nozzle_form TOOLHEAD=B")
        self.assertEqual(btn["E (spare)"], "VT_UI PAGE=nozzle_form TOOLHEAD=E")
        p.gcode.run_line("VT_UI PAGE=nozzle_form TOOLHEAD=B")
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("Now: 0.4mm standard/stainless", text)
        self.assertIn("Calibration for B will be invalidated.", text)
        p.gcode.run_line("VT_UI PAGE=nozzle_apply TOOLHEAD=B MODE=replace "
                         "DIAMETER=0.6 VOLUME_TYPE=high_flow MATERIAL=hardened")
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("0.6mm high_flow/hardened", text)
        self.assertIn("Needs calibration: B (T1)", text)
        self.assertEqual(exts[1].nozzle_diameter, 0.6)
        self.assertIsNone(exts[1].base_position)

    def test_nozzle_edit_mode_only_invalidates_on_change(self):
        p, exts, mod = self.boot()
        p.gcode.run_line("VT_UI PAGE=nozzle_form TOOLHEAD=A MODE=edit")
        self.assertIn("Nothing changed yet.", "\n".join(p.gcode.take_prompt()))
        p.gcode.run_line("VT_UI PAGE=nozzle_form TOOLHEAD=A MODE=edit DIAMETER=0.8")
        self.assertIn("Details changed", "\n".join(p.gcode.take_prompt()))
        p.gcode.run_line("VT_UI PAGE=nozzle_apply TOOLHEAD=A MODE=edit DIAMETER=0.4")
        p.gcode.take_prompt()
        self.assertTrue(mod.store.is_valid(mod.store.resolve("A")))

    def test_nozzle_form_marks_selection(self):
        p, exts, mod = self.boot()
        p.gcode.run_line("VT_UI PAGE=nozzle_form TOOLHEAD=A DIAMETER=0.6")
        colors = {l: c for l, g, c in prompt_buttons(p.gcode.take_prompt())}
        self.assertEqual(colors["0.6"], "primary")
        self.assertEqual(colors["0.4"], "secondary")

    def test_add_flow_defaults_to_right_slot(self):
        p, exts, mod = self.boot(spares=0, legacy=False)
        p.gcode.run_line("VT_UI PAGE=add")
        lines = p.gcode.take_prompt()
        text = "\n".join(lines)
        self.assertIn("label RD", text)                       # right is the default
        colors = {l: c for l, _, c in prompt_buttons(lines)}
        self.assertEqual(colors["Right Slot (T1-T3)"], "primary")
        self.assertEqual(colors["Left Slot (T0)"], "secondary")
        p.gcode.run_line("VT_UI PAGE=add_apply DIAMETER=0.8 VOLUME_TYPE=standard "
                         "MATERIAL=hardened")
        self.assertIn("RD 0.8mm standard/hardened",
                      "\n".join(p.gcode.take_prompt()))
        th = mod.store.spare_ids()[0]
        self.assertEqual((mod.store.label(th), mod.store.side(th)), ("RD", "right"))

    def test_add_flow_left_slot(self):
        p, exts, mod = self.boot(spares=0, legacy=False)
        lines = self.click_ui(p, "VT_UI PAGE=add SIDE=left")
        self.assertIn("label LB", "\n".join(lines))          # LA is already taken
        colors = {l: c for l, _, c in prompt_buttons(lines)}
        self.assertEqual(colors["Left Slot (T0)"], "primary")
        # the choice is carried by every button of the form
        for label, gcode, _ in prompt_buttons(lines):
            if label in ("0.2", "high_flow", "hardened", "Add"):
                self.assertIn("SIDE=left", gcode, label)
        p.gcode.run_line("VT_UI PAGE=add_apply SIDE=left")
        th = mod.store.spare_ids()[0]
        self.assertEqual((mod.store.label(th), mod.store.side(th)), ("LB", "left"))
        # a left spare is offered for slot 0 only
        lines = self.click_ui(p, "VT_UI PAGE=layout_slot SLOT=0 PENDING=LA,RA,RB,RC")
        self.assertIn("LB", [l for l, _, _ in prompt_buttons(lines)])
        lines = self.click_ui(p, "VT_UI PAGE=layout_slot SLOT=2 PENDING=LA,RA,RB,RC")
        self.assertNotIn("LB", [l for l, _, _ in prompt_buttons(lines)])

    def test_add_bad_side_rejected(self):
        p, exts, mod = self.boot(spares=0)
        for line in ("VT_UI PAGE=add SIDE=middle", "VT_UI PAGE=add_apply SIDE=up"):
            with self.assertRaises(GCodeError, msg=line):
                p.gcode.run_line(line)

    def click_ui(self, p, line):
        p.gcode.run_line(line)
        return p.gcode.take_prompt()

    def test_mutating_pages_refused_while_printing(self):
        p, exts, mod = self.boot()
        p.print_stats.state = "printing"
        for line in ("VT_UI PAGE=layout_apply PENDING=A,C,B,D",
                     "VT_UI PAGE=nozzle_apply TOOLHEAD=A",
                     "VT_UI PAGE=add_apply"):
            with self.assertRaises(GCodeError, msg=line):
                p.gcode.run_line(line)
        # read-only pages still work
        p.gcode.run_line("VT_UI")

    def test_bad_input(self):
        p, exts, mod = self.boot()
        for line in ("VT_UI PAGE=nope", "VT_UI PAGE=layout PENDING=A,B,C",
                     "VT_UI PAGE=layout PENDING=A,B,C,Q",
                     "VT_UI PAGE=layout_slot SLOT=9 PENDING=A,B,C,D",
                     "VT_UI PAGE=nozzle_form TOOLHEAD=Q",
                     "VT_UI PAGE=nozzle_form TOOLHEAD=A MODE=weird",
                     "VT_UI PAGE=layout_apply PENDING=A,A,C,D"):
            with self.assertRaises(GCodeError, msg=line):
                p.gcode.run_line(line)

    def test_ui_unavailable_when_store_down(self):
        d = os.path.join(self.tmp, "persistent")
        os.makedirs(d)
        open(os.path.join(d, "virtual_toolheads.json"), "w").write("{bad")
        p, exts = make_printer(self.tmp, BP)
        start(p)
        with self.assertRaises(GCodeError):
            p.gcode.run_line("VT_UI")
        p.gcode.run_line("VT_UI PAGE=close")   # closing always works

    def test_every_reachable_dialog_is_well_formed_and_every_apply_runs(self):
        """Click through the whole UI: every button must lead somewhere valid."""
        p, exts, mod = self.boot()
        queue = ["VT_UI"]
        seen, applies = set(), set()
        while queue:
            line = queue.pop()
            if line in seen:
                continue
            seen.add(line)
            self.assertLess(len(seen), 5000, "UI graph exploded")
            if "_apply" in line:
                applies.add(line)
                continue
            if line.startswith("VT_CAL PAGE=") and "PAGE=start" not in line:
                continue            # wizard action pages: see CalibrationTests
            p.gcode.run_line(line)
            lines = p.gcode.take_prompt()
            if "PAGE=close" in line:
                self.assertEqual(lines, ["// action:prompt_end"])
                continue
            self.assertTrue(lines[0].startswith("// action:prompt_begin "), line)
            self.assertEqual(lines[-1], "// action:prompt_show", line)
            starts = lines.count("// action:prompt_button_group_start")
            self.assertEqual(starts,
                             lines.count("// action:prompt_button_group_end"))
            for label, gcode, color in prompt_buttons(lines):
                self.assertTrue(gcode.startswith(("VT_UI PAGE=", "VT_CAL PAGE=",
                                                  "VT_CALIBRATE")), gcode)
                self.assertIn(color, ("primary", "secondary", "info",
                                      "warning", "error"))
                self.assertNotIn("|", label)
                queue.append(gcode)
        self.assertGreater(len(seen), 50)
        self.assertGreater(len(applies), 10)
        # the layouts the dialogs can build are always physically possible
        sides = {"A": "left"}
        for line in applies:
            if "PAGE=layout_apply" in line:
                pending = line.split("PENDING=")[1].split()[0].split(",")
                for slot, label in enumerate(pending):
                    self.assertEqual(sides.get(label, "right"), vt.slot_side(slot),
                                     line)
        # every Apply button reachable from the UI must execute cleanly
        for i, line in enumerate(sorted(applies)):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            p2, ex2, mod2 = self.boot(tmp)
            try:
                p2.gcode.run_line(line)
            except GCodeError as e:
                self.fail("%s -> %s" % (line, e))
            self.assertTrue(p2.gcode.take_prompt()[0].startswith(
                "// action:prompt_begin"), line)


###########################################################################
# Calibration wizard, against a fake of the stock EXTRUDER_OFFSET_ACTION_*
###########################################################################

class DirectModeTests(UiTests):
    """Dialogs opened from a dashboard slot button (DIRECT=1) close on
    Back/Cancel; the same dialogs opened from the menu go back to it."""

    def footers(self, p, cmd, **kw):
        p.gcode.run(cmd, **kw)
        return [b for b in prompt_buttons(p.gcode.take_prompt())]

    def back_of(self, buttons):
        return [g for l, g, c in buttons if l in ("Back", "Cancel")]

    def test_layout_slot_direct_back_closes(self):
        p, _, _ = self.boot()
        b = self.footers(p, "VT_UI", PAGE="layout_slot", SLOT="1",
                         PENDING="A,B,C,D", DIRECT="1")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=close"])
        # picking a toolhead keeps direct mode on the next page
        pick = [g for l, g, c in b if l == "E"][0]
        self.assertIn("DIRECT=1", pick)

    def test_layout_slot_menu_back_returns_to_layout(self):
        p, _, _ = self.boot()
        b = self.footers(p, "VT_UI", PAGE="layout_slot", SLOT="1",
                         PENDING="A,B,C,D")
        self.assertEqual(self.back_of(b)[0].split()[1], "PAGE=layout")
        self.assertTrue(all("DIRECT" not in g for l, g, c in b))

    def test_layout_page_direct_cancel_closes(self):
        p, _, _ = self.boot()
        b = self.footers(p, "VT_UI", PAGE="layout", PENDING="A,B,C,E", DIRECT="1")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=close"])
        slots = [g for l, g, c in b if l.startswith("T")]
        self.assertTrue(all("DIRECT=1" in g for g in slots))

    def test_nozzle_form_direct_cancel_closes_and_propagates(self):
        p, _, _ = self.boot()
        b = self.footers(p, "VT_UI", PAGE="nozzle_form", TOOLHEAD="B", DIRECT="1")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=close"])
        for l, g, c in b:
            if g.startswith("VT_UI PAGE=nozzle_"):
                self.assertIn("DIRECT=1", g, l)

    def test_nozzle_form_menu_cancel_returns_to_menu(self):
        p, _, _ = self.boot()
        b = self.footers(p, "VT_UI", PAGE="nozzle_form", TOOLHEAD="B")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=main"])

    def test_direct_does_not_leak_into_next_command(self):
        p, _, _ = self.boot()
        p.gcode.run("VT_UI", PAGE="nozzle_form", TOOLHEAD="B", DIRECT="1")
        p.gcode.take_prompt()
        b = self.footers(p, "VT_UI", PAGE="nozzle_form", TOOLHEAD="B")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=main"])

    def test_calibrate_direct_cancel_closes(self):
        p, _, _ = self.boot()
        b = self.footers(p, "VT_CALIBRATE", DIRECT="1")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=close"])
        toggles = [g for l, g, c in b if l.startswith("T")]
        self.assertTrue(toggles and all("DIRECT=1" in g for g in toggles))
        b = self.footers(p, "VT_CALIBRATE")
        self.assertEqual(self.back_of(b), ["VT_UI PAGE=main"])


class FakeStockCalibration:
    """Behaves like probe_inductance_coil.ExtruderOffsetCalibration."""
    TOOLS = {"T0": "extruder", "T1": "extruder1", "T2": "extruder2",
             "T3": "extruder3"}

    def __init__(self, printer, exts):
        self.printer = printer
        self.exts = exts
        self.calls = []
        self.calibration_step = "idle"
        self.main_state = "IDLE"
        self.last_xyz_result = {e.name: None for e in exts}
        self.fail = {}          # command -> remaining failures (raise)
        self.silent_fail = {}   # tool -> remaining silent probe failures
        self.n_probe = 0
        self.fail_after_state = 0   # PRESTART enters the state, then raises
        printer.objects["extruder_offset_calibration"] = self
        printer.objects["machine_state_manager"] = self
        for name in ("PRESTART", "PREHOMING", "DETECT_PLATE", "PREHEAT",
                     "MANUAL_CLEAN", "HEAT", "AUTO_CLEAN", "WAIT_COOL",
                     "CHECK_TARGET_TEMP", "PROBE_CALIBRATE", "SAVE_RESULT",
                     "EXIT"):
            full = "EXTRUDER_OFFSET_ACTION_" + name
            printer.gcode.cmds[full] = self._make(name, full)

    def _make(self, name, full):
        def handler(gcmd):
            self.calls.append((name, dict(gcmd.params)))
            if name == "PRESTART" and self.fail_after_state > 0:
                self.fail_after_state -= 1
                self.main_state = "XYZ_OFFSET_CALIBRATE"
                raise GCodeError("homing failed")
            if self.fail.get(name, 0) > 0:
                self.fail[name] -= 1
                raise GCodeError('{"coded": "0003-0530-0000-0011", '
                                 '"msg": "The plate has not been removed"}')
            if name != "PRESTART" and name != "PREHEAT" and \
                    name != "SAVE_RESULT" and self.main_state != "XYZ_OFFSET_CALIBRATE":
                raise GCodeError("Operation requires XYZ_OFFSET_CALIBRATE state")
            getattr(self, "do_" + name.lower(), lambda g: None)(gcmd)
        return handler

    def do_prestart(self, gcmd):
        self.main_state = "XYZ_OFFSET_CALIBRATE"
        self.calibration_step = "ready"
        self.last_xyz_result = {e.name: None for e in self.exts}

    def do_probe_calibrate(self, gcmd):
        tool = gcmd.params["TOOL_ID"]
        name = self.TOOLS[tool]
        if self.silent_fail.get(tool, 0) > 0:
            self.silent_fail[tool] -= 1
            return
        self.n_probe += 1
        self.last_xyz_result[name] = [13.0 + 0.01 * self.n_probe, 236.9,
                                      -2.0 - 0.01 * self.n_probe]
        if all(v is not None for v in self.last_xyz_result.values()):
            self.calibration_step = "calibration_completed"
            self.printer.gcode.run_line(
                "EXTRUDER_OFFSET_ACTION_SAVE_RESULT FORCE_SAVE=0")

    def do_save_result(self, gcmd):
        force = int(gcmd.params.get("FORCE_SAVE", 0))
        if not force and any(v is None for v in self.last_xyz_result.values()):
            raise GCodeError("calibration detection not completed")
        for e in self.exts:
            if self.last_xyz_result[e.name] is not None:
                e.base_position = list(self.last_xyz_result[e.name])
        self.printer.send_event(vt.OFFSET_EVENT)
        self.calibration_step = "save_result_completed"

    def do_exit(self, gcmd):
        self.main_state = "IDLE"
        self.calibration_step = "idle"
        self.last_xyz_result = {e.name: None for e in self.exts}

    def get_status(self, eventtime=None):
        return {"main_state": self.main_state}

    def names(self):
        return [c[0] for c in self.calls]

    def tools_for(self, name):
        return [c[1]["TOOL_ID"] for c in self.calls if c[0] == name]


PID_A = {"Kp": 31.68, "Ki": 10.56, "Kd": 23.76}


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def boot(self, invalid=("B", "D"), **opts):
        p, exts = make_printer(self.tmp, BP)
        stock = FakeStockCalibration(p, exts)
        mod = start(p, **opts)
        # these tests are about the probing flow; PID has its own tests, and
        # is only preselected for toolheads without a tuning
        for label in invalid:
            p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=label)
        for th_id in mod.store.ids():       # (a new nozzle forgets the PID)
            mod.store.record_pid(th_id, dict(PID_A), 210, "calibrated")
        p.gcode.take_prompt()
        return p, exts, mod, stock

    @staticmethod
    def button(lines, label):
        for l, g, _ in prompt_buttons(lines):
            if l == label:
                return g
        raise AssertionError("no button %r in %r" % (label, lines))

    def click(self, p, line):
        p.gcode.run_line(line)
        return p.gcode.take_prompt()

    def test_start_page_preselects_invalid_toolheads(self):
        p, exts, mod, stock = self.boot()
        lines = self.click(p, "VT_CALIBRATE")
        text = "\n".join(lines)
        self.assertIn("Calibrate toolheads", text)
        self.assertEqual(self.button(lines, "Start"),
                         "VT_CAL PAGE=prep SEL=1,3 OFS=1 PID=0 FLOW=0 PT=210")
        self.assertEqual(self.button(lines, "T0: A"),
                         "VT_CAL PAGE=start SEL=0,1,3 OFS=1 PID=0 FLOW=0 PT=210")
        self.assertEqual(self.button(lines, "T1: B (needs it)"),
                         "VT_CAL PAGE=start SEL=3 OFS=1 PID=0 FLOW=0 PT=210")
        colors = {l: c for l, _, c in prompt_buttons(lines)}
        self.assertEqual(colors["T1: B (needs it)"], "primary")
        self.assertEqual(colors["T0: A"], "secondary")
        self.assertEqual(stock.calls, [])    # nothing runs until Start

    def test_nothing_selected_has_no_start_button(self):
        p, exts, mod, stock = self.boot(invalid=())
        lines = self.click(p, "VT_CALIBRATE")
        self.assertFalse(any(l == "Start" for l, _, _ in prompt_buttons(lines)))
        lines = self.click(p, "VT_CAL PAGE=start SEL=-")
        self.assertFalse(any(l == "Start" for l, _, _ in prompt_buttons(lines)))
        with self.assertRaises(GCodeError):
            p.gcode.run_line("VT_CALIBRATE CONFIRM=1")

    DISMISS = 'RESPOND TYPE=command MSG=action:prompt_end'

    def test_wipe_and_plate_dialogs_come_back_when_dismissed(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertIn("Wipe the nozzle of T1 (B)", "\n".join(lines))
        again = self.click(p, self.DISMISS)
        self.assertEqual(again[0], "// action:prompt_end")      # Fluidd's own
        self.assertIn("// action:prompt_begin Wipe the nozzle of T1 (B)", again)
        self.assertEqual(again[-1], "// action:prompt_show")
        again = self.click(p, self.DISMISS)                # as often as needed
        self.assertIn("// action:prompt_begin Wipe the nozzle of T1 (B)", again)
        lines = self.click(p, self.button(lines, "Done"))
        self.assertIn("Remove the build plate", "\n".join(lines))
        again = self.click(p, self.DISMISS)
        self.assertIn("// action:prompt_begin Remove the build plate", again)

    def test_abort_ends_the_re_showing(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, self.button(lines, "Abort calibration"))
        mod._shown_at -= vt.STALE_DISMISS + 1       # not a stale dismissal
        self.assertEqual(self.click(p, self.DISMISS),
                         ["// action:prompt_end"])

    def test_other_dialogs_stay_dismissed(self):
        p, exts, mod, stock = self.boot()
        self.click(p, "VT_UI")
        mod._shown_at -= vt.STALE_DISMISS + 1
        self.assertEqual(self.click(p, self.DISMISS),
                         ["// action:prompt_end"])
        self.click(p, "VT_CALIBRATE")                 # the start screen
        mod._shown_at -= vt.STALE_DISMISS + 1
        self.assertEqual(self.click(p, self.DISMISS),
                         ["// action:prompt_end"])

    def test_other_respond_calls_are_left_alone(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertEqual(self.click(p, "RESPOND TYPE=command MSG=hello"),
                         ["// hello"])

    def test_no_re_showing_when_the_wizard_is_not_running(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        mod.calibrator._on = False                # e.g. the wizard died
        mod._shown_at -= vt.STALE_DISMISS + 1
        self.assertEqual(self.click(p, self.DISMISS),
                         ["// action:prompt_end"])

    def test_status_screens_are_published_for_the_card(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        seen = []
        orig = p.gcode.cmds["EXTRUDER_OFFSET_ACTION_HEAT"]

        def spy(g):
            seen.append(mod.get_status()["notice"])
            return orig(g)
        p.gcode.cmds["EXTRUDER_OFFSET_ACTION_HEAT"] = spy
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertTrue(seen[0])
        self.assertTrue(seen[0]["title"])
        self.assertTrue(seen[0]["lines"])
        # the wipe dialog needs the user: it replaces the notice
        self.assertIsNone(mod.get_status()["notice"])
        self.click(p, self.button(lines, "Abort calibration"))
        self.assertIsNone(mod.get_status()["notice"])

    def test_notice_sequence_changes_with_each_status(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        mod._status_screen("One", ["a"])
        s1 = mod.get_status()["notice"]
        mod._status_screen("Two", ["b"])
        s2 = mod.get_status()["notice"]
        self.assertEqual((s1["title"], s2["title"]), ("One", "Two"))
        self.assertGreater(s2["seq"], s1["seq"])
        p.gcode.run_line("VT_UI PAGE=close")
        self.assertIsNone(mod.get_status()["notice"])

    def test_full_selective_flow(self):
        p, exts, mod, stock = self.boot(invalid=("B", "D"))
        old_a, old_c = list(exts[0].base_position), list(exts[2].base_position)

        lines = self.click(p, "VT_CAL PAGE=prep SEL=1,3 OFS=1")
        self.assertEqual(stock.names()[:2], ["PRESTART", "PREHEAT"])
        self.assertEqual(stock.calls[1][1], {"T0_TEMP": "0", "T2_TEMP": "0"})
        # first tool: heat, auto clean, manual clean, then the wipe dialog
        self.assertEqual(stock.names()[2:], ["HEAT", "AUTO_CLEAN", "MANUAL_CLEAN"])
        self.assertEqual(stock.tools_for("HEAT"), ["T1"])
        self.assertIn("Wipe the nozzle of T1 (B)", "\n".join(lines))
        done = self.button(lines, "Done")
        self.assertEqual(done, "VT_CAL PAGE=cool SEL=1,3 I=0")

        lines = self.click(p, done)
        self.assertEqual(stock.tools_for("WAIT_COOL"), ["T1"])
        # straight on to the second tool
        self.assertEqual(stock.tools_for("HEAT"), ["T1", "T3"])
        self.assertIn("Wipe the nozzle of T3 (D)", "\n".join(lines))
        lines = self.click(p, self.button(lines, "Done"))
        self.assertEqual(stock.tools_for("WAIT_COOL"), ["T1", "T3"])
        self.assertIn("Remove the build plate", "\n".join(lines))

        lines = self.click(p, self.button(lines, "Continue"))
        self.assertEqual(stock.tools_for("PROBE_CALIBRATE"), ["T1", "T3"])
        self.assertEqual(stock.calls[[c[0] for c in stock.calls].index("DETECT_PLATE")][1],
                         {"PRESENCE": "0"})
        # saved once, only the probed tools, then exit
        self.assertEqual(stock.names().count("SAVE_RESULT"), 1)
        save = [c for c in stock.calls if c[0] == "SAVE_RESULT"][0]
        self.assertEqual(save[1], {"FORCE_SAVE": "1"})
        self.assertEqual(stock.names()[-1], "EXIT")
        text = "\n".join(lines)
        self.assertIn("Calibration finished", text)
        self.assertIn("Put the build plate back", text)

        # untouched tools were never heated, cleaned, probed
        for name in ("HEAT", "AUTO_CLEAN", "MANUAL_CLEAN", "WAIT_COOL",
                     "PROBE_CALIBRATE"):
            self.assertNotIn("T0", stock.tools_for(name))
            self.assertNotIn("T2", stock.tools_for(name))
        # and the store reflects it
        s = mod.store
        self.assertEqual(s.invalid_loaded(), [])
        self.assertEqual(exts[0].base_position, old_a)
        self.assertEqual(exts[2].base_position, old_c)
        self.assertEqual(s.effective_base_position(s.resolve("B")),
                         exts[1].base_position)
        self.assertEqual(s.get(s.resolve("D"))["calibration"]["slot"], 3)

    def test_wait_cool_target_is_140(self):
        p, exts, mod, stock = self.boot(invalid=("A",))
        self.click(p, "VT_CAL PAGE=prep SEL=0 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=0 I=0")
        wc = [c for c in stock.calls if c[0] == "WAIT_COOL"][0]
        self.assertEqual(wc[1], {"TOOL_ID": "T0", "TEMP": "140"})

    def test_all_four_selected_does_not_save_twice(self):
        p, exts, mod, stock = self.boot(invalid=("A", "B", "C", "D"))
        self.click(p, "VT_CAL PAGE=prep SEL=0,1,2,3 OFS=1")
        self.assertNotIn("PREHEAT", stock.names()[1:2])   # nobody to switch off
        self.click(p, "VT_CAL PAGE=probe SEL=0,1,2,3 I=0")
        # stock auto-saved after the last probe (FORCE_SAVE=0); we must not repeat
        saves = [c[1] for c in stock.calls if c[0] == "SAVE_RESULT"]
        self.assertEqual(saves, [{"FORCE_SAVE": "0"}])
        self.assertEqual(mod.store.invalid_loaded(), [])
        self.assertEqual(stock.names()[-1], "EXIT")

    def test_plate_still_on_offers_retry(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.main_state = "XYZ_OFFSET_CALIBRATE"   # as after PRESTART
        stock.fail["DETECT_PLATE"] = 1
        lines = self.click(p, "VT_CAL PAGE=detect SEL=1")
        text = "\n".join(lines)
        self.assertIn("Build plate check failed", text)
        self.assertIn("The plate has not been removed", text)   # decoded JSON
        self.assertNotIn('"coded"', text)
        self.assertEqual(stock.tools_for("PROBE_CALIBRATE"), [])
        retry = self.button(lines, "Retry")
        self.assertEqual(self.button(lines, "Abort calibration"),
                         "VT_CAL PAGE=abort")
        lines = self.click(p, retry)
        self.assertEqual(stock.tools_for("PROBE_CALIBRATE"), ["T1"])
        self.assertIn("Calibration finished", "\n".join(lines))

    def test_silent_probe_failure_is_detected(self):
        p, exts, mod, stock = self.boot(invalid=("B", "D"))
        stock.main_state = "XYZ_OFFSET_CALIBRATE"   # as after PRESTART
        stock.silent_fail["T3"] = 1
        lines = self.click(p, "VT_CAL PAGE=probe SEL=1,3 I=0")
        self.assertEqual(stock.tools_for("PROBE_CALIBRATE"), ["T1", "T3"])
        self.assertNotIn("SAVE_RESULT", stock.names())
        self.assertIn("did not produce a result", "\n".join(lines))
        retry = self.button(lines, "Retry")
        self.assertEqual(retry, "VT_CAL PAGE=probe SEL=1,3 I=1")   # resumes at T3
        lines = self.click(p, retry)
        # T1 is not probed again
        self.assertEqual(stock.tools_for("PROBE_CALIBRATE"), ["T1", "T3", "T3"])
        self.assertIn("Calibration finished", "\n".join(lines))
        self.assertEqual(mod.store.invalid_loaded(), [])

    def test_step_failure_shows_retry_and_abort(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.fail["AUTO_CLEAN"] = 1
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertIn("Cleaning T1 failed", "\n".join(lines))
        self.assertNotIn("MANUAL_CLEAN", stock.names())    # stopped at the failure
        lines = self.click(p, self.button(lines, "Retry"))
        self.assertEqual(stock.tools_for("MANUAL_CLEAN"), ["T1"])
        self.assertIn("Wipe the nozzle", "\n".join(lines))

    def test_start_failure_offers_retry(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.fail["PRESTART"] = 1
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertIn("Could not start calibration", "\n".join(lines))
        self.assertEqual(self.button(lines, "Retry"),
                         "VT_CAL PAGE=prep SEL=1 OFS=1 PID=0 FLOW=0")

    def test_abort_exits_and_is_safe_when_idle(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        lines = self.click(p, "VT_CAL PAGE=abort")
        self.assertEqual(stock.names()[-1], "EXIT")
        self.assertEqual(stock.main_state, "IDLE")
        self.assertIn("Calibration cancelled", "\n".join(lines))
        # aborting again (nothing running) must not blow up
        lines = self.click(p, "VT_CAL PAGE=abort")
        self.assertIn("Nothing to cancel", "\n".join(lines))

    def test_console_entry_with_toolheads_and_confirm(self):
        p, exts, mod, stock = self.boot(invalid=())
        p.gcode.run_line("VT_CALIBRATE TOOLHEADS=C CONFIRM=1")
        self.assertEqual(stock.tools_for("HEAT"), ["T2"])
        self.assertEqual(stock.calls[1][1], {"T0_TEMP": "0", "T1_TEMP": "0",
                                             "T3_TEMP": "0"})
        # a valid toolhead can be recalibrated on request
        p.gcode.take_prompt()
        with self.assertRaises(GCodeError):
            p.gcode.run_line("VT_CALIBRATE TOOLHEADS=Q")
        p.gcode.run("VT_ADD", LABEL="E")
        with self.assertRaises(GCodeError):
            p.gcode.run_line("VT_CALIBRATE TOOLHEADS=E")   # spare, not loaded

    def test_refused_while_printing_or_without_stock_object(self):
        p, exts, mod, stock = self.boot()
        p.print_stats.state = "printing"
        for line in ("VT_CALIBRATE", "VT_CAL PAGE=prep SEL=1 OFS=1",
                     "VT_CAL PAGE=probe SEL=1"):
            with self.assertRaises(GCodeError, msg=line):
                p.gcode.run_line(line)
        p.print_stats.state = "standby"
        del p.objects["extruder_offset_calibration"]
        with self.assertRaises(GCodeError):
            p.gcode.run_line("VT_CALIBRATE")

    def test_bad_selection(self):
        p, exts, mod, stock = self.boot()
        for line in ("VT_CAL PAGE=prep SEL=-", "VT_CAL PAGE=prep SEL=9 OFS=1",
                     "VT_CAL PAGE=prep SEL=x", "VT_CAL PAGE=nope",
                     "VT_CAL PAGE=clean SEL=1 I=5"):
            with self.assertRaises(GCodeError, msg=line):
                p.gcode.run_line(line)
        self.assertEqual(stock.calls, [])

    def test_result_and_menu_offer_calibrate_when_needed(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        lines = self.click(p, "VT_UI")
        colors = {l: c for l, _, c in prompt_buttons(lines)}
        self.assertEqual(colors["Calibrate"], "warning")
        lines = self.click(p, "VT_UI PAGE=layout_apply PENDING=A,B,C,D")
        self.assertEqual(self.button(lines, "Calibrate now"), "VT_CALIBRATE")
        mod.store.record_calibration(mod.store.resolve("B"), [1, 2, 3], 1)
        lines = self.click(p, "VT_UI PAGE=layout_apply PENDING=A,B,C,D")
        self.assertFalse(any(l == "Calibrate now" for l, _, _ in prompt_buttons(lines)))


    # ---- the incident: the touchscreen continues its own flow -------------
    def screen_heat_t0(self, p):
        """What the touchscreen app sent right after homing finished."""
        p.gcode.run_line("EXTRUDER_OFFSET_ACTION_HEAT TOOL_ID=T0")

    def test_foreign_commands_are_rejected_while_wizard_runs(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")       # wizard active, at wipe dialog
        calls_before = list(stock.calls)
        for line in ("EXTRUDER_OFFSET_ACTION_HEAT TOOL_ID=T0",
                     "EXTRUDER_OFFSET_ACTION_AUTO_CLEAN TOOL_ID=T0",
                     "EXTRUDER_OFFSET_ACTION_PREHEAT",
                     "EXTRUDER_OFFSET_ACTION_PROBE_CALIBRATE TOOL_ID=T0",
                     "EXTRUDER_OFFSET_ACTION_SAVE_RESULT FORCE_SAVE=1",
                     "EXTRUDER_OFFSET_ACTION_EXIT"):
            with self.assertRaises(GCodeError, msg=line) as cm:
                p.gcode.run_line(line)
            self.assertIn("calibration wizard is running", str(cm.exception))
        # nothing reached the stock handlers, and the wizard is unharmed
        self.assertEqual(stock.calls, calls_before)
        self.assertEqual(stock.main_state, "XYZ_OFFSET_CALIBRATE")
        lines = self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.assertIn("Remove the build plate", "\n".join(lines))

    def test_wizard_result_reports_blocked_commands(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        for _ in range(2):
            with self.assertRaises(GCodeError):
                self.screen_heat_t0(p)
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        lines = self.click(p, "VT_CAL PAGE=detect SEL=1")
        text = "\n".join(lines)
        self.assertIn("2 command(s) from another client", text)
        self.assertIn("HEAT", text)

    def test_foreign_commands_pass_when_wizard_is_not_running(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.main_state = "XYZ_OFFSET_CALIBRATE"
        self.screen_heat_t0(p)                        # the touchscreen's own flow
        self.assertEqual(stock.tools_for("HEAT"), ["T0"])

    def test_guard_released_after_finish_and_abort(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.click(p, "VT_CAL PAGE=detect SEL=1")      # probes, saves, exits
        mod.calibrator._tail_until = 0                 # grace window over
        stock.main_state = "XYZ_OFFSET_CALIBRATE"      # a later screen flow
        self.screen_heat_t0(p)
        self.assertEqual(stock.tools_for("HEAT")[-1], "T0")
        # abort path
        stock.calls.clear()
        stock.main_state = "IDLE"
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        with self.assertRaises(GCodeError):
            self.screen_heat_t0(p)
        self.click(p, "VT_CAL PAGE=abort")
        mod.calibrator._tail_until = 0
        stock.main_state = "XYZ_OFFSET_CALIBRATE"
        self.screen_heat_t0(p)

    def test_guard_expires_when_stock_state_ends(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        with self.assertRaises(GCodeError):
            self.screen_heat_t0(p)
        stock.main_state = "IDLE"                      # exited some other way
        self.assertFalse(mod.calibrator.active())
        stock.main_state = "XYZ_OFFSET_CALIBRATE"      # a new, foreign calibration
        self.screen_heat_t0(p)                         # must not be blocked
        self.assertEqual(stock.tools_for("HEAT")[-1], "T0")

    def test_guard_expires_if_start_never_reached_stock_state(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.fail["PRESTART"] = 1                     # fails before any state change
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertTrue(mod.calibrator.active())       # armed, within the grace period
        mod.calibrator._on_since -= 1000               # long ago, never started
        self.assertFalse(mod.calibrator.active())

    def test_retry_after_failed_start_is_not_treated_as_foreign(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.fail_after_state = 1      # PRESTART sets the state, then fails
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertIn("Could not start calibration", "\n".join(lines))
        self.assertEqual(stock.main_state, "XYZ_OFFSET_CALIBRATE")
        lines = self.click(p, self.button(lines, "Retry"))
        self.assertNotIn("already running", "\n".join(lines))
        self.assertIn("Wipe the nozzle", "\n".join(lines))

    def test_refuses_to_start_over_a_running_stock_calibration(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        stock.main_state = "XYZ_OFFSET_CALIBRATE"      # e.g. started on the screen
        lines = self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertIn("Calibration already running", "\n".join(lines))
        self.assertEqual(stock.calls, [])
        self.assertEqual(self.button(lines, "Cancel it"), "VT_CAL PAGE=abort")
        self.click(p, "VT_CAL PAGE=abort")
        self.assertEqual(stock.names(), ["EXIT"])

    def test_nested_stock_save_is_allowed_inside_probe(self):
        # all four selected: the stock PROBE_CALIBRATE itself calls SAVE_RESULT
        p, exts, mod, stock = self.boot(invalid=("A", "B", "C", "D"))
        self.click(p, "VT_CAL PAGE=prep SEL=0,1,2,3 OFS=1")
        lines = self.click(p, "VT_CAL PAGE=probe SEL=0,1,2,3 I=0")
        self.assertIn("Calibration finished", "\n".join(lines))
        self.assertEqual([c[1] for c in stock.calls if c[0] == "SAVE_RESULT"],
                         [{"FORCE_SAVE": "0"}])

    def test_guard_can_be_disabled(self):
        p, exts, mod, stock = self.boot(invalid=("B",), guard_calibration=False)
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.screen_heat_t0(p)                         # passes straight through
        self.assertEqual(stock.tools_for("HEAT"), ["T1", "T0"])

    def test_no_guard_when_store_is_down(self):
        d = os.path.join(self.tmp, "persistent")
        os.makedirs(d)
        with open(os.path.join(d, "virtual_toolheads.json"), "w") as f:
            f.write("{bad")
        p, exts = make_printer(self.tmp, BP)
        stock = FakeStockCalibration(p, exts)
        start(p)
        stock.main_state = "XYZ_OFFSET_CALIBRATE"
        self.screen_heat_t0(p)                         # stock behaviour untouched
        self.assertEqual(stock.tools_for("HEAT"), ["T0"])

    def test_stragglers_queued_during_the_wizard_are_rejected_after_it_ends(self):
        # The screen sent these while the wizard's long command was running;
        # they execute the moment it returns, i.e. after our EXIT.
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.click(p, "VT_CAL PAGE=detect SEL=1")      # probes, saves, exits
        self.assertEqual(stock.main_state, "IDLE")
        calls = list(stock.calls)
        for line in ("EXTRUDER_OFFSET_ACTION_PROBE_CALIBRATE TOOL_ID=T2",
                     "EXTRUDER_OFFSET_ACTION_DETECT_PLATE PRESENCE=0",  # no state check
                     "EXTRUDER_OFFSET_ACTION_PREHEAT",                  # no state check
                     "EXTRUDER_OFFSET_ACTION_SAVE_RESULT"):             # no state check
            with self.assertRaises(GCodeError, msg=line):
                p.gcode.run_line(line)
        self.assertEqual(stock.calls, calls)
        # once the grace window is over the stock commands work again
        mod.calibrator._tail_until = 0
        p.gcode.run_line("EXTRUDER_OFFSET_ACTION_PREHEAT")
        self.assertEqual(stock.calls[-1][0], "PREHEAT")

    def test_calibration_loops_probes_each_toolhead_several_times(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        p.gcode.run_line("VT_CALIBRATION_LOOPS N=3")
        self.assertEqual(mod.calibrator.probe_runs, 3)
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        lines = self.click(p, "VT_CAL PAGE=detect SEL=1")
        probes = [c for c in stock.calls if c[0] == "PROBE_CALIBRATE"]
        self.assertEqual(len(probes), 3)
        rows = [r for r in mod._read_cal_log() if r["kind"] == "offset"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["x"] for r in rows}), 3)     # three results
        self.assertIn("probed 3 times", "\n".join(lines))
        # the last one is what was saved
        self.assertAlmostEqual(exts[1].base_position[0], rows[-1]["x"])

    def test_calibration_loops_can_dock_between_probes(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        parks = []
        p.gcode.cmds["PARK_EXTRUDER"] = lambda g: parks.append(1)
        p.gcode.run_line("VT_CALIBRATION_LOOPS N=3 DOCK=1")
        self.assertTrue(mod.calibrator.probe_dock)
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        lines = "\n".join(self.click(p, "VT_CAL PAGE=detect SEL=1"))
        self.assertEqual(len(parks), 2)          # before repeats 2 and 3
        self.assertEqual(len([c for c in stock.calls
                              if c[0] == "PROBE_CALIBRATE"]), 3)
        self.assertIn("docked between probes", lines)

    def test_dock_is_off_by_default_and_reset_by_a_new_count(self):
        p, exts, mod, stock = self.boot()
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(FakeGcmd(N=3, DOCK=1))
        self.assertTrue(mod.calibrator.probe_dock)
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(FakeGcmd(DOCK=0))
        self.assertFalse(mod.calibrator.probe_dock)
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(FakeGcmd(N=2, DOCK=1))
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(FakeGcmd(N=2))
        self.assertFalse(mod.calibrator.probe_dock)

    def test_loop_count_and_docking_are_logged_with_each_probe(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        p.gcode.cmds["PARK_EXTRUDER"] = lambda g: None
        p.gcode.run_line("VT_CALIBRATION_LOOPS N=3 DOCK=1")
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.click(p, "VT_CAL PAGE=detect SEL=1")
        rows = [r for r in mod._read_cal_log() if r["kind"] == "offset"]
        self.assertEqual([(r["loop"], r["loops"], r["dock"]) for r in rows],
                         [(1, 3, False), (2, 3, True), (3, 3, True)])
        self.assertEqual({r["slot"] for r in rows}, {1})
        text = []

        class G:
            def get(s_, n, d=None, **kw): return {"ACTION": "SHOW"}.get(n, d)
            def get_int(s_, n, d=None, **kw): return d
            def respond_info(s_, m): text.append(m)
        mod.cmd_VT_CAL_LOG(G())
        self.assertTrue(any("loop 2/3, docked" in t for t in text))

    def test_slot_report_compares_the_shift_between_slots_with_the_noise(self):
        mod = self.boot()[2]
        rows = []
        t = 1000000000
        for slot, base, sess in ((1, 10.000, 0), (1, 10.002, 1),
                                 (3, 10.050, 0), (3, 10.048, 1)):
            for n, jitter in enumerate((0., 0.001, -0.001, 0.0005)):
                rows.append({"kind": "offset", "toolhead": "RA", "slot": slot,
                             "t": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                time.gmtime(t)),
                             "loop": n + 1, "loops": 4, "dock": False,
                             "x": base + jitter, "y": 0., "z": 0.})
                t += 140
            t += 20000
        text = "\n".join(mod.cal_log_slot_report(rows))
        self.assertIn("RA slot T1: 8 runs in 2 sessions (loops: 4)", text)
        self.assertIn("RA slot T3: 8 runs in 2 sessions", text)
        self.assertRegex(text, r"RA slot shift T3 vs T1: x \+0\.0480 = \d+\.\dx scatter")

    def test_clean_0_skips_the_heating_and_cleaning_steps(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        ran = []
        for name in ("SAVE_GCODE_STATE", "G90", "G0", "RESTORE_GCODE_STATE"):
            p.gcode.cmds[name] = lambda g: None
        orig = p.gcode._process_commands
        p.gcode._process_commands = lambda cmds, need_ack=True: (
            ran.extend(cmds), orig(cmds, need_ack))[1]
        p.gcode.run_line("VT_CALIBRATION_LOOPS N=2 CLEAN=0")
        self.assertFalse(mod.calibrator.do_clean)
        text = "\n".join(self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1"))
        # the stock cleaning step lowers the bed; without it we do, so the
        # build plate can be taken off
        low = [c for c in ran if c.startswith(("SAVE_GCODE_STATE", "G90",
                                               "G0 ", "RESTORE_GCODE_STATE"))]
        self.assertEqual(low, ["SAVE_GCODE_STATE NAME=VT_PLATE", "G90",
                               "G0 Z150 F12000",
                               "RESTORE_GCODE_STATE NAME=VT_PLATE"])
        names = [c[0] for c in stock.calls]
        self.assertNotIn("HEAT", names)
        self.assertNotIn("AUTO_CLEAN", names)
        self.assertNotIn("MANUAL_CLEAN", names)
        self.assertIn("WAIT_COOL", names)           # straight to the cool-down
        self.assertIn("Remove the build plate", text)
        self.assertNotIn("All nozzles are clean", text)
        lines = "\n".join(self.click(p, "VT_CAL PAGE=detect SEL=1"))
        self.assertIn("heating and cleaning steps were skipped", lines)
        rows = [r for r in mod._read_cal_log() if r["kind"] == "offset"]
        self.assertEqual([r["cleaned"] for r in rows], [False, False])
        report = "\n".join(mod.cal_log_slot_report(rows))
        self.assertIn("not cleaned", report)

    def test_with_cleaning_the_bed_is_left_to_the_stock_clean_step(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        ran = []
        orig = p.gcode._process_commands
        p.gcode._process_commands = lambda cmds, need_ack=True: (
            ran.extend(cmds), orig(cmds, need_ack))[1]
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertFalse([c for c in ran if "VT_PLATE" in c])

    def test_loops_and_options_are_separate_commands(self):
        p, exts, mod, stock = self.boot()
        cal = mod.calibrator
        out = p.gcode.run("VT_CALIBRATION_OPTIONS", CLEAN="0", REF="0").out
        self.assertFalse(cal.do_clean)
        self.assertFalse(cal.add_ref)
        self.assertIn("CLEAN=0", "\n".join(out))
        self.assertIn("REF=0", "\n".join(out))
        cal.cmd_VT_CALIBRATION_LOOPS(FakeGcmd(N=3))     # does not touch them
        self.assertEqual(cal.probe_runs, 3)
        self.assertFalse(cal.do_clean)
        self.assertFalse(cal.add_ref)
        p.gcode.run("VT_CALIBRATION_OPTIONS", CLEAN="1", REF="1")
        self.assertTrue(cal.do_clean)
        self.assertTrue(cal.add_ref)

    def test_clean_and_ref_on_the_loops_command_still_work_with_a_hint(self):
        p, exts, mod, stock = self.boot()
        g = FakeGcmd(CLEAN=0)
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(g)
        self.assertFalse(mod.calibrator.do_clean)
        self.assertIn("VT_CALIBRATION_OPTIONS", "\n".join(g.out))

    def test_a_failing_repeat_stops_the_repeats_but_still_saves(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        p.gcode.run_line("VT_CALIBRATION_LOOPS N=4")
        real = p.gcode.cmds["EXTRUDER_OFFSET_ACTION_PROBE_CALIBRATE"]
        n = []

        def flaky(g):
            n.append(1)
            if len(n) == 2:
                raise GCodeError("probe lost")
            return real(g)
        p.gcode.cmds["EXTRUDER_OFFSET_ACTION_PROBE_CALIBRATE"] = flaky
        mod.calibrator.install_guards()
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        lines = "\n".join(self.click(p, "VT_CAL PAGE=detect SEL=1"))
        self.assertEqual(len(n), 2)
        self.assertIn("Calibration finished", lines)
        self.assertIn("repeat 2 of 4 stopped", lines)

    def test_calibration_loops_defaults_to_one_and_reports(self):
        p, exts, mod, stock = self.boot()
        self.assertEqual(mod.calibrator.probe_runs, 1)
        g = FakeGcmd(N=2)
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(g)
        self.assertEqual(mod.calibrator.probe_runs, 2)
        self.assertIn("loops per toolhead: 2", g.out[0])
        g = FakeGcmd()
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(g)         # just reports
        self.assertEqual(mod.calibrator.probe_runs, 2)
        mod.calibrator.cmd_VT_CALIBRATION_LOOPS(FakeGcmd(N=1))
        self.assertEqual(mod.calibrator.probe_runs, 1)

    def test_every_offset_run_is_logged(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.click(p, "VT_CAL PAGE=detect SEL=1")
        rows = mod._read_cal_log()
        self.assertEqual([r["kind"] for r in rows], ["offset", "store"])
        self.assertEqual(rows[0]["slot"], 1)
        self.assertTrue(rows[1]["session"])
        self.assertTrue(rows[1]["frame"])
        self.assertEqual(rows[1]["how"], "no_t0")
        self.assertEqual(rows[1]["ver"], vt.VERSION)
        self.assertEqual(rows[0]["toolhead"], mod.store.label(
            mod.store.toolhead_in_slot(1)))
        for axis in ("x", "y", "z"):
            self.assertIsInstance(rows[0][axis], float)

    def hook_g28(self, p, mod, fail=False):
        """A stand-in for the stock G28 that prints what the real Z homing
        prints, under our hook."""
        def g28(gcmd):
            if gcmd.get("XYONLY", None) is None:
                p.gcode.respond_info("z offset: -0.5")
                p.gcode.respond_info("z_overshoot_comp: 0.0123")
                p.gcode.respond_info("z_mesh_complete: -0.31")
            if fail:
                raise GCodeError("homing failed")
        p.gcode.cmds["G28"] = g28
        mod._home_hooked = False
        mod._install_home_hook()

    def test_z_homing_values_are_logged_with_each_offset_run(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.hook_g28(p, mod)
        p.gcode.run_line("G28")
        h = mod.last_home
        self.assertEqual((h["z_offset"], h["z_overshoot_comp"],
                          h["z_mesh_complete"]), (-0.5, 0.0123, -0.31))
        self.assertEqual(h["tool"], "extruder")
        self.assertEqual(h["tool_z_offset"], 0.0)
        self.assertEqual(p.gcode.output_callbacks, [])      # listener removed
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.click(p, "VT_CAL PAGE=detect SEL=1")
        row = [r for r in mod._read_cal_log() if r["kind"] == "offset"][0]
        self.assertEqual(row["home"]["z_overshoot_comp"], 0.0123)
        self.assertEqual(row["home"]["t"], h["t"])
        # a second homing is a new frame
        p.gcode.run_line("G28")
        self.assertEqual(mod.last_home["n"], h["n"] + 1)

    def test_homing_without_z_or_a_failed_homing_keeps_the_last_values(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.hook_g28(p, mod)
        p.gcode.run_line("G28")
        before = dict(mod.last_home)
        p.gcode.run_line("G28 XYONLY=1")
        self.assertEqual(mod.last_home, before)
        self.hook_g28(p, mod, fail=True)
        with self.assertRaises(GCodeError):
            p.gcode.run_line("G28")
        self.assertEqual(mod.last_home, before)
        self.assertEqual(p.gcode.output_callbacks, [])

    def test_offset_runs_without_any_homing_seen_log_no_home(self):
        p, exts, mod, stock = self.boot(invalid=("B",))
        self.click(p, "VT_CAL PAGE=prep SEL=1 OFS=1")
        self.click(p, "VT_CAL PAGE=cool SEL=1 I=0")
        self.click(p, "VT_CAL PAGE=detect SEL=1")
        row = [r for r in mod._read_cal_log() if r["kind"] == "offset"][0]
        self.assertNotIn("home", row)

    def test_guard_status_is_visible(self):
        p, exts, mod, stock = self.boot()
        g = p.gcode.run("VT_STATUS")
        self.assertIn("Calibration guard: active (12 stock commands)", g.out[0])
        self.assertEqual(mod.get_status()["calibration_guard"],
                         "active (12 stock commands)")
        json.dumps(mod.get_status())
        lines = self.click(p, "VT_CALIBRATE")
        self.assertNotIn("WARNING", "\n".join(lines))

    def test_guard_status_when_disabled_or_stock_missing(self):
        p, exts, mod, stock = self.boot(guard_calibration=False)
        self.assertEqual(mod.calibrator.guard_info, "disabled in config")
        lines = self.click(p, "VT_CALIBRATE")
        self.assertIn("WARNING: the calibration guard is not active "
                      "(disabled in config)", "\n".join(lines))
        # stock commands absent -> reported, not silently "active"
        p2, exts2 = make_printer(tempfile.mkdtemp(), BP)
        mod2 = start(p2)
        self.assertTrue(mod2.calibrator.guard_info.startswith("not installed"))
        self.assertFalse(mod2.calibrator.guard_ok())

    def test_guard_install_failure_is_reported(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p, exts = make_printer(tmp, BP)
        FakeStockCalibration(p, exts)
        mod = vt.VirtualToolheads(FakeConfig(p))
        real = p.gcode.register_command
        def flaky(name, func, desc=None):
            if func is None:                    # the "unregister" step fails
                raise RuntimeError("no register for you")
            return real(name, func, desc)
        p.gcode.register_command = flaky
        p.send_event("klippy:connect")
        # connect swallowed the error and recorded it; the store still works
        self.assertIsNotNone(mod.store)
        self.assertFalse(mod.calibrator.guard_ok())
        self.assertTrue(mod.calibrator.guard_info.startswith("FAILED"))
        lines = self.click(p, "VT_CALIBRATE")
        self.assertIn("WARNING: the calibration guard is not active",
                      "\n".join(lines))


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts = make_printer(self.tmp, BP)
        self.stock = FakeStockCalibration(self.p, self.exts)
        self.mod = start(self.p)
        self.p.gcode.run_line("VT_REPLACE_NOZZLE TOOLHEAD=B")
        for th_id in self.mod.store.ids():       # (a new nozzle forgets PID)
            self.mod.store.record_pid(th_id, dict(PID_A), 210, "calibrated")
        self.p.gcode.take_prompt()

    def dump(self, count=60):
        g = self.p.gcode.run("VT_TRACE", ACTION="DUMP", COUNT=count)
        return g.out[0]

    def test_off_by_default_and_does_not_touch_dispatcher(self):
        self.assertNotIn("_process_commands", self.p.gcode.__dict__)
        self.assertIn("Trace is empty", self.dump())

    def test_records_commands_notes_and_chain(self):
        g = self.p.gcode
        g.run("VT_TRACE", ACTION="ON")
        g.run_line("VT_CAL PAGE=prep SEL=1 OFS=1")
        text = self.dump(100)
        self.assertIn("run  VT_CAL PAGE=prep SEL=1 OFS=1", text)
        # nested stock commands are visible, attributed to our module
        self.assertIn("EXTRUDER_OFFSET_ACTION_HEAT TOOL_ID=T1", text)
        self.assertIn("virtual_toolheads:", text)
        self.assertIn("note dialog: Wipe the nozzle of T1 (B)", text)
        # order is preserved: the dialog note comes after the clean steps
        self.assertLess(text.index("MANUAL_CLEAN TOOL_ID=T1"),
                        text.index("dialog: Wipe the nozzle"))
        # a later command shows up after the dialog -- this is the evidence
        # needed to see who issues a stray T0
        with self.assertRaises(GCodeError):            # the guard rejects it...
            g.run_line("EXTRUDER_OFFSET_ACTION_PREHEAT")
        last = self.dump(1)                            # ...but the trace saw it
        self.assertIn("EXTRUDER_OFFSET_ACTION_PREHEAT", last)
        self.assertIn("run ", last)

    def test_polling_noise_is_ignored(self):
        g = self.p.gcode
        g.cmds["M105"] = lambda gcmd: None
        g.run("VT_TRACE", ACTION="ON")
        g.run_line("M105")
        g.run_line("# comment")
        self.assertIn("Trace is empty", self.dump())

    def test_off_restores_dispatcher_and_behaviour_is_unchanged(self):
        g = self.p.gcode
        g.run("VT_TRACE", ACTION="ON")
        self.assertIn("_process_commands", g.__dict__)
        g.run("VT_TRACE", ACTION="OFF")
        self.assertNotIn("_process_commands", g.__dict__)
        n = len(list(self.mod._trace_buf))
        g.run_line("VT_STATUS")
        self.assertEqual(len(list(self.mod._trace_buf)), n)
        # tracing must not change what the wizard does
        g.run("VT_TRACE", ACTION="ON")
        g.run_line("VT_CAL PAGE=prep SEL=1 OFS=1")
        self.assertEqual(self.stock.names()[:2], ["PRESTART", "PREHEAT"])

    def test_auto_expires(self):
        g = self.p.gcode
        g.run("VT_TRACE", ACTION="ON", MINUTES=1)
        self.mod._trace_until = 0                  # time is up
        g.run_line("VT_STATUS")                    # first command removes it
        self.assertNotIn("_process_commands", g.__dict__)
        self.assertEqual(len(list(self.mod._trace_buf)), 0)

    def test_clear_and_count(self):
        g = self.p.gcode
        g.run("VT_TRACE", ACTION="ON")
        for _ in range(5):
            g.run_line("VT_STATUS")
        self.assertEqual(len(self.dump(3).split("\n")), 3)
        g.run("VT_TRACE", ACTION="CLEAR")
        self.assertIn("Trace is empty", self.dump())

    def test_unsupported_dispatcher_and_bad_action(self):
        g = self.p.gcode
        saved = type(g)._process_commands
        try:
            del type(g)._process_commands
            with self.assertRaises(GCodeError):
                g.run("VT_TRACE", ACTION="ON")
        finally:
            type(g)._process_commands = saved
        with self.assertRaises(GCodeError):
            g.run("VT_TRACE", ACTION="WAT")

    def test_traced_dispatch_still_propagates_errors(self):
        g = self.p.gcode
        g.run("VT_TRACE", ACTION="ON")
        with self.assertRaises(GCodeError):
            g.run_line("VT_CAL PAGE=nope")
        self.assertIn("VT_CAL PAGE=nope", self.dump())


class SidesAndVersionTests(unittest.TestCase):
    """Left Slot / Right Slot toolheads, the side/index labels, the schema 2 -> 3
    store upgrade and the release version. These use the real default labels."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store_path = os.path.join(self.tmp, "persistent",
                                       "virtual_toolheads.json")

    def boot(self, **opts):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p, legacy_labels=False, **opts)
        return p, exts, mod

    def write_v2(self, spares=()):
        d = v2_store(spares)
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        with open(self.store_path, "w") as f:
            json.dump(d, f)
        return d

    # ---- fresh install --------------------------------------------------
    def test_fresh_install_labels_sides_and_version(self):
        p, exts, mod = self.boot()
        s = mod.store
        self.assertEqual([s.label(i) for i in s.layout], ["LA", "RA", "RB", "RC"])
        data = json.load(open(self.store_path))
        self.assertEqual(data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(data["mod_version"], vt.VERSION)
        self.assertEqual(vt.VERSION, "0.7")
        out = p.gcode.run("VT_STATUS").out[0].split("\n")
        self.assertEqual(out[0], "Virtual toolheads v0.7")
        self.assertEqual(out[1], "Slot 0 (Left Slot): LA 0.4mm "
                                 "standard/stainless  calibrated  no PID tuning")
        self.assertTrue(out[2].startswith("Slot 1 (Right Slot): RA 0.4mm"))
        st = mod.get_status()
        json.dumps(st)
        self.assertEqual(st["version"], "0.7")
        self.assertEqual(st["toolheads"]["LA"]["side"], "left")
        self.assertEqual(st["toolheads"]["RC"]["side"], "right")

    def test_version_reported_even_when_the_store_is_down(self):
        os.makedirs(os.path.dirname(self.store_path))
        with open(self.store_path, "w") as f:
            f.write("{bad")
        p, exts, mod = self.boot()
        self.assertIn("v0.7 unavailable", p.gcode.run("VT_STATUS").out[0])
        self.assertEqual(mod.get_status()["version"], "0.7")

    def test_version_matches_the_changelog(self):
        changelog = os.path.join(HERE, "..", "CHANGELOG.md")
        with open(changelog) as f:
            first = next(l for l in f if l.startswith("## "))
        self.assertIn("[%s]" % vt.VERSION, first)

    # ---- VT_ADD / VT_SET_SIDE --------------------------------------------
    def test_vt_add_sides_and_default_labels(self):
        p, exts, mod = self.boot()
        s = mod.store
        g = p.gcode.run("VT_ADD")
        self.assertIn("Added Right Slot toolhead RD ", g.out[0])
        g = p.gcode.run("VT_ADD", SIDE="left")
        self.assertIn("Added Left Slot toolhead LB ", g.out[0])
        g = p.gcode.run("VT_ADD", SIDE="L")
        self.assertIn("Added Left Slot toolhead LC ", g.out[0])
        g = p.gcode.run("VT_ADD", SIDE="right", LABEL="Fast")
        self.assertIn("Added Right Slot toolhead Fast ", g.out[0])
        with self.assertRaises(GCodeError):
            p.gcode.run("VT_ADD", SIDE="middle")
        self.assertEqual(len(s.spare_ids()), 4)       # the bad one added nothing
        self.assertEqual(json.load(open(self.store_path))["toolheads"]
                         [s.resolve("LB")]["side"], "left")

    def test_vt_set_side(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_ADD")                          # RD
        g = p.gcode.run("VT_SET_SIDE", TOOLHEAD="RD", SIDE="left")
        self.assertTrue(g.out[0].startswith(
            "Side set: RD is a Left Slot toolhead."), g.out[0])
        g = p.gcode.run("VT_SET_SIDE", TOOLHEAD="RD", SIDE="left")
        self.assertTrue(g.out[0].startswith("Side confirmed:"))
        with self.assertRaises(GCodeError):            # loaded: its slot decides
            p.gcode.run("VT_SET_SIDE", TOOLHEAD="LA", SIDE="right")
        with self.assertRaises(GCodeError):
            p.gcode.run("VT_SET_SIDE", TOOLHEAD="RD", SIDE="up")
        with self.assertRaises(GCodeError):
            p.gcode.run("VT_SET_SIDE", TOOLHEAD="RD")
        p.print_stats.state = "printing"
        with self.assertRaises(GCodeError):
            p.gcode.run("VT_SET_SIDE", TOOLHEAD="RD", SIDE="right")

    def test_set_side_that_contradicts_the_calibration_slot_invalidates(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_ADD")
        rd = mod.store.resolve("RD")
        mod.store.record_calibration(rd, [1, 2, 3], 2)
        g = p.gcode.run("VT_SET_SIDE", TOOLHEAD="RD", SIDE="left")
        self.assertIn("needs calibrating again", g.out[0])
        self.assertFalse(mod.store.is_valid(rd))

    # ---- layout rules through the commands and dialogs ---------------------
    def test_layout_command_refuses_the_wrong_side_and_changes_nothing(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_ADD", SIDE="left")             # LB
        p.gcode.run("VT_ADD")                          # RD
        before = [(e.base_position, e.nozzle_diameter) for e in exts]
        sent = len(p.sent)
        for layout, who, slot in (("LA,LB,RB,RC", "LB", "slot 1"),
                                  ("RA,LA,RB,RC", "RA", "slot 0"),
                                  ("LA,RA,RB,LB", "LB", "slot 3"),
                                  ("RD,RA,RB,RC", "RD", "slot 0")):
            with self.assertRaises(GCodeError, msg=layout) as cm:
                p.gcode.run("VT_SET_LAYOUT", LAYOUT=layout)
            self.assertIn(who, str(cm.exception))
            self.assertIn(slot, str(cm.exception))
        self.assertEqual(mod._labels(), ["LA", "RA", "RB", "RC"])
        self.assertEqual([(e.base_position, e.nozzle_diameter) for e in exts],
                         before)
        self.assertEqual(len(p.sent), sent)
        # the valid moves still work: a left spare into slot 0, rights shuffled
        g = p.gcode.run("VT_SET_LAYOUT", LAYOUT="LB,RC,RD,RA")
        self.assertIn("Layout: LB, RC, RD, RA", g.out[0])
        self.assertIsNone(exts[0].base_position)       # LB was never calibrated

    def test_layout_dialog_offers_only_toolheads_that_fit(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_ADD", SIDE="left")             # LB
        p.gcode.run("VT_ADD")                          # RD
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI PAGE=layout_slot SLOT=0 PENDING=LA,RA,RB,RC")
        lines = p.gcode.take_prompt()
        self.assertEqual(sorted(l for l, _, _ in prompt_buttons(lines)
                                if l not in ("Back",)), ["LA", "LB"])
        self.assertIn("Only Left Slot toolheads fit here", "\n".join(lines))
        p.gcode.run_line("VT_UI PAGE=layout_slot SLOT=3 PENDING=LA,RA,RB,RC")
        lines = p.gcode.take_prompt()
        self.assertEqual(sorted(l for l, _, _ in prompt_buttons(lines)
                                if l not in ("Back",)),
                         ["RA (T1)", "RB (T2)", "RC", "RD"])
        self.assertIn("Only Right Slot toolheads fit here", "\n".join(lines))

    def test_layout_apply_by_hand_with_a_wrong_side_is_refused(self):
        p, exts, mod = self.boot()
        before = mod._labels()
        with self.assertRaises(GCodeError):
            p.gcode.run_line("VT_UI PAGE=layout_apply PENDING=RA,LA,RB,RC")
        self.assertEqual(mod._labels(), before)

    def test_main_menu_shows_sides(self):
        p, exts, mod = self.boot()
        p.gcode.run("VT_ADD", SIDE="left")
        p.gcode.take_prompt()
        p.gcode.run_line("VT_UI")
        text = "\n".join(p.gcode.take_prompt())
        self.assertIn("Virtual toolheads v0.7", text)
        self.assertIn("T0: LA 0.4mm standard/stainless", text)
        self.assertIn("Spare: LB [Left Slot]", text)   # spares still show their side

    # ---- upgrading a schema 2 store ---------------------------------------------
    def test_upgrade_from_schema_2_keeps_calibration_and_the_original_file(self):
        original = self.write_v2(spares=(("Fast", "right"), ("Wide", "left")))
        p, exts, mod = self.boot()
        s = mod.store
        self.assertIsNotNone(s, mod.error)
        self.assertEqual([s.label(i) for i in s.layout], ["LA", "RA", "RB", "RC"])
        self.assertEqual([e.base_position for e in exts], BP)   # data carried over
        data = json.load(open(self.store_path))
        self.assertEqual(data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(data["mod_version"], vt.VERSION)
        self.assertEqual(json.load(open(self.store_path + ".v2")), original)
        by = {s.label(i): i for i in s.ids()}
        self.assertEqual((s.side(by["Wide"]), s.side(by["Fast"])), ("left", "right"))
        # a second boot must not overwrite the preserved original
        before = open(self.store_path + ".v2").read()
        p2, exts2 = make_printer(self.tmp, BP)
        mod2 = start(p2, legacy_labels=False)
        self.assertEqual(open(self.store_path + ".v2").read(), before)
        self.assertFalse(mod2.store.migrated)

    def test_a_store_with_an_impossible_layout_disables_the_feature(self):
        # e.g. hand-edited: a Right Slot toolhead in slot 0
        d = vt.VirtualToolheadStore.from_slots(slots4()).to_dict()
        d["toolheads"]["th1"]["side"] = "right"
        os.makedirs(os.path.dirname(self.store_path))
        with open(self.store_path, "w") as f:
            json.dump(d, f)
        raw = open(self.store_path).read()
        p, exts, mod = self.boot()
        self.assertIsNone(mod.store)
        self.assertIn("loaded in slot 0", mod.error)
        self.assertEqual(open(self.store_path).read(), raw)     # left untouched
        self.assertEqual([e.base_position for e in exts], BP)   # stock behaviour

    def test_upgraded_store_applies_like_any_other(self):
        self.write_v2(spares=(("RD", "right"),))
        p, exts, mod = self.boot()
        g = p.gcode.run("VT_SET_LAYOUT", LAYOUT="LA,RA,RB,RD")
        self.assertIn("Needs calibration: RD (slot 3)", g.out[0])
        self.assertIsNone(exts[3].base_position)


class MaterialAndLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_materials_are_stainless_and_hardened(self):
        self.assertEqual(vt.VALID_MATERIALS, ["stainless", "hardened"])
        s = vt.VirtualToolheadStore.from_slots(slots4())
        self.assertTrue(all(s.get(i)["nozzle"]["material"] == "stainless"
                            for i in s.ids()))
        self.assertEqual(s.get(s.add_toolhead())["nozzle"]["material"], "stainless")

    def test_the_old_material_name_is_rejected_everywhere(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        with self.assertRaises(vt.StoreError):
            s.add_toolhead(material="standard")
        with self.assertRaises(vt.StoreError):
            s.set_nozzle("th1", material="standard")
        with self.assertRaises(vt.StoreError):
            s.replace_nozzle("th1", material="standard")
        d = s.to_dict()
        d["toolheads"]["th1"]["nozzle"]["material"] = "standard"
        with self.assertRaises(vt.StoreError) as cm:
            vt.VirtualToolheadStore.from_dict(d)       # a file nobody renamed
        self.assertIn("material", str(cm.exception))
        # flow type is a different field and keeps its stock name
        self.assertEqual(vt.VALID_VOLUME_TYPES, ["standard", "high_flow"])

    def test_console_and_dialog_defaults_use_stainless(self):
        p, exts = make_printer(self.tmp, BP)
        mod = start(p, legacy_labels=False)
        g = p.gcode.run("VT_ADD")
        self.assertIn("standard/stainless", g.out[0])
        p.gcode.run_line("VT_UI PAGE=add")
        lines = p.gcode.take_prompt()
        colors = {l: c for l, _, c in prompt_buttons(lines)}
        self.assertEqual(colors["stainless"], "primary")      # preselected
        self.assertEqual(colors["hardened"], "secondary")
        self.assertFalse(any("MATERIAL=standard" in g
                             for _, g, _ in prompt_buttons(lines)))
        p.gcode.run_line("VT_UI PAGE=add_apply")
        th = mod.store.spare_ids()[-1]
        self.assertEqual(mod.store.get(th)["nozzle"]["material"], "stainless")

    def test_main_menu_button_layout(self):
        p, exts = make_printer(self.tmp, BP)
        FakeStockCalibration(p, exts)
        start(p)
        p.gcode.run_line("VT_UI")
        lines = p.gcode.take_prompt()
        rows, row = [], None
        for l in lines:
            if l == "// action:prompt_button_group_start":
                row = []
            elif l == "// action:prompt_button_group_end":
                rows.append(row)
                row = None
            elif row is not None and l.startswith("// action:prompt_button "):
                row.append(l[len("// action:prompt_button "):].split("|")[0])
        self.assertEqual(rows, [["Change layout"], ["Change nozzle", "Change PID"],
                                ["Boards", "Backup"], ["Add toolhead"],
                                ["Calibrate"]])


# ---------------------------------------------------------------------------
# PID tuning per toolhead
# ---------------------------------------------------------------------------
class FakePidControl:
    """The bits of heaters.ControlPID that virtual_toolheads touches. Profile
    values are held divided by 255, files hold config units, like stock."""
    BASE = 255.

    def __init__(self, tmp, name, kp, ki, kd):
        self.pid_profiles = {"default": {
            "Kp": kp / self.BASE, "Ki": ki / self.BASE, "Kd": kd / self.BASE,
            "full_power_threshold": None, "zero_power_threshold": None,
            "settle_delta": 2.0, "settle_slope": 0.5}}
        self.current_profile = "default"
        self.json_filename = os.path.join(tmp, "snapmaker",
                                          name + "_pid_parameters.json")
        self.ignore_pid_json = False
        self.set_calls = 0

    def set_pid_profile(self, profile):
        self.current_profile = profile
        self.set_calls += 1

    def _save_heater_pid_profiles_to_json(self, path, profiles):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(profiles, f)

    def config_units(self):
        p = self.pid_profiles["default"]
        return {k: p[k] * self.BASE for k in ("Kp", "Ki", "Kd")}


class FakeHeater:
    def __init__(self, control):
        self.control = control
        self.smoothed_temp = 25.


class FakeHeaters:
    def __init__(self):
        self.set_calls = []

    def set_temperature(self, heater, temp, wait=False):
        self.set_calls.append((heater, temp))
        if temp == 0:
            pass


class FakePidStock:
    """Stock PID_CALIBRATE / TEMPERATURE_WAIT. A calibration replaces the
    heater's tuning with new values (as the real one does, via the controller
    and, if write_file, its JSON file)."""
    def __init__(self, printer, exts, tmp):
        self.printer = printer
        self.exts = {e.name: e for e in exts}
        self.calls = []
        self.waits = []
        self.fail = {}
        self.write_file = True
        self.n = 0
        self.heaters = FakeHeaters()
        printer.objects["heaters"] = self.heaters
        printer.gcode.cmds["PID_CALIBRATE"] = self.pid_calibrate
        printer.gcode.cmds["TEMPERATURE_WAIT"] = self.temperature_wait

    def pid_calibrate(self, gcmd):
        name = gcmd.get("HEATER")
        self.calls.append((name, gcmd.get("TARGET", None)))
        if self.fail.get(name, 0) > 0:
            self.fail[name] -= 1
            raise GCodeError("PID autotune failed: heater timed out")
        self.n += 1
        control = self.exts[name].heater.control
        base = control.BASE
        new = {"Kp": 40. + self.n, "Ki": 12. + self.n, "Kd": 25. + self.n}
        prof = control.pid_profiles["default"]
        for k, v in new.items():
            prof[k] = v / base
        if self.write_file:
            control._save_heater_pid_profiles_to_json(
                control.json_filename,
                {"default": dict(new, full_power_threshold=None,
                                 zero_power_threshold=None,
                                 settle_delta=2.0, settle_slope=0.5)})

    def temperature_wait(self, gcmd):
        sensor = gcmd.get("SENSOR")
        self.waits.append(sensor)
        self.exts[sensor].heater.smoothed_temp = 25.


def pid_printer(tmp):
    p, exts = make_printer(tmp, BP)
    for i, e in enumerate(exts):
        e.heater = FakeHeater(FakePidControl(tmp, e.name, 30. + i, 10. + i,
                                             20. + i))
    stock = FakePidStock(p, exts, tmp)
    return p, exts, stock


class PidStoreTests(unittest.TestCase):
    def test_new_store_has_empty_pid_state(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        self.assertEqual(s.pid_temp, vt.DEFAULT_PID_TEMP)
        self.assertFalse(s.pid_adopted)
        self.assertTrue(all(s.pid(i) is None for i in s.ids()))

    def test_record_pid_reports_change_and_roundtrips(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        th = s.layout[0]
        self.assertTrue(s.record_pid(th, dict(PID_A), 210, "calibrated"))
        self.assertTrue(s.record_pid(th, dict(PID_A), 220, "calibrated"))
        self.assertFalse(s.record_pid(th, dict(PID_A), 220, "calibrated"))
        self.assertEqual(s.pid(th)["target"], 220.)
        self.assertTrue(s.record_pid(th, {"Kp": 1., "Ki": 2., "Kd": 3.}))
        again = vt.VirtualToolheadStore.from_dict(
            json.loads(json.dumps(s.to_dict())))
        self.assertEqual(again.pid_values(th)["Kp"], 1.)

    def test_unusable_values_are_rejected(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        for bad in ({}, {"Kp": 1., "Ki": 2.}, {"Kp": "x", "Ki": 1., "Kd": 1.},
                    {"Kp": float("nan"), "Ki": 1., "Kd": 1.}):
            with self.assertRaises(vt.StoreError):
                s.record_pid(s.layout[0], bad)

    def test_pid_temp_range(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        s.set_pid_temp(230)
        self.assertEqual(s.pid_temp, 230.)
        for bad in (100, 400, "hot"):
            with self.assertRaises(vt.StoreError):
                s.set_pid_temp(bad)

    def test_corrupt_pid_record_is_rejected_on_load(self):
        d = vt.VirtualToolheadStore.from_slots(slots4()).to_dict()
        th = next(iter(d["toolheads"].values()))
        th["pid"] = {"values": {"Kp": 1.}, "target": None, "at": "x",
                     "source": "calibrated"}
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)


class PidModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts, self.stock = pid_printer(self.tmp)

    def boot(self):
        return start(self.p)

    def test_first_boot_adopts_what_each_slot_uses(self):
        mod = self.boot()
        s = mod.store
        for slot, e in enumerate(self.exts):
            rec = s.pid(s.toolhead_in_slot(slot))
            self.assertEqual(rec["source"], "adopted")
            self.assertAlmostEqual(rec["values"]["Kp"], 30. + slot, places=6)
        self.assertTrue(s.pid_adopted)

    def test_adoption_happens_only_once(self):
        mod = self.boot()
        mod.store.add_toolhead(label="E")
        mod._save()
        p2, exts2, _ = pid_printer(self.tmp)
        mod2 = start(p2)
        e = [i for i in mod2.store.ids() if mod2.store.label(i) == "E"][0]
        self.assertIsNone(mod2.store.pid(e))

    def test_retune_outside_the_module_is_observed(self):
        self.boot()
        control = self.exts[1].heater.control
        control.pid_profiles["default"]["Kp"] = 99. / 255.
        p2, exts2, _ = pid_printer(self.tmp)
        exts2[1].heater.control.pid_profiles["default"]["Kp"] = 99. / 255.
        mod2 = start(p2)
        rec = mod2.store.pid(mod2.store.toolhead_in_slot(1))
        self.assertEqual(rec["source"], "observed")
        self.assertAlmostEqual(rec["values"]["Kp"], 99., places=6)

    def test_missing_heater_is_tolerated(self):
        for e in self.exts:
            e.heater = None
        mod = self.boot()
        self.assertIsNotNone(mod.store)
        self.assertFalse(mod.store.pid_adopted)

    def test_layout_swap_applies_each_toolheads_tuning(self):
        mod = self.boot()
        s = mod.store
        labels = [s.label(i) for i in s.layout]    # A, B, C, D
        self.p.gcode.run("VT_SET_LAYOUT", LAYOUT=",".join(
            [labels[0], labels[2], labels[1], labels[3]]))
        # slot 1 now holds C (adopted from slot 2: Kp 32), slot 2 holds B
        c1 = self.exts[1].heater.control
        c2 = self.exts[2].heater.control
        self.assertAlmostEqual(c1.config_units()["Kp"], 32., places=6)
        self.assertAlmostEqual(c2.config_units()["Kp"], 31., places=6)
        self.assertEqual(c1.set_calls, 1)
        # and the file holds config units, like stock's
        with open(c1.json_filename) as f:
            self.assertAlmostEqual(json.load(f)["default"]["Kp"], 32., places=6)
        # untouched slots were not rewritten
        self.assertEqual(self.exts[0].heater.control.set_calls, 0)

    def test_toolhead_without_tuning_leaves_the_heater_alone(self):
        mod = self.boot()
        s = mod.store
        s.add_toolhead(label="E")
        self.p.gcode.run("VT_SET_LAYOUT", LAYOUT=",".join(
            [s.label(i) for i in s.layout[:3]] + ["E"]))
        self.assertAlmostEqual(self.exts[3].heater.control.config_units()["Kp"],
                               33., places=6)

    def test_pid_calibrate_is_recorded_for_the_toolhead_in_that_slot(self):
        mod = self.boot()
        self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder2 TARGET=230")
        s = mod.store
        rec = s.pid(s.toolhead_in_slot(2))
        self.assertEqual(rec["source"], "calibrated")
        self.assertEqual(rec["target"], 230.)
        self.assertAlmostEqual(rec["values"]["Kp"], 41., places=6)
        self.assertEqual(s.pid(s.toolhead_in_slot(1))["source"], "adopted")
        with open(mod._store_path()) as f:
            saved = json.load(f)
        self.assertTrue(any(th["pid"] and th["pid"]["source"] == "calibrated"
                            for th in saved["toolheads"].values()))

    def test_every_pid_run_is_logged_even_when_the_tuning_is_unchanged(self):
        mod = self.boot()
        for _ in range(2):
            self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder2 TARGET=230")
        rows = [r for r in mod._read_cal_log() if r["kind"] == "pid"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["target"], 230.)
        self.assertAlmostEqual(rows[0]["kp"], 41., places=6)

    def test_pid_result_is_read_from_the_controller_without_a_file(self):
        mod = self.boot()
        self.stock.write_file = False
        self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder TARGET=210")
        rec = mod.store.pid(mod.store.toolhead_in_slot(0))
        self.assertEqual(rec["source"], "calibrated")
        self.assertAlmostEqual(rec["values"]["Kp"], 41., places=6)

    def test_pid_calibrate_failure_records_nothing(self):
        mod = self.boot()
        self.stock.fail["extruder"] = 1
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder TARGET=210")
        rec = mod.store.pid(mod.store.toolhead_in_slot(0))
        self.assertEqual(rec["source"], "adopted")

    def test_calibration_follows_the_toolhead_through_a_swap(self):
        mod = self.boot()
        s = mod.store
        self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder1 TARGET=210")
        tuned = s.label(s.toolhead_in_slot(1))
        labels = [s.label(i) for i in s.layout]
        self.p.gcode.run("VT_SET_LAYOUT", LAYOUT=",".join(
            [labels[0], labels[2], labels[1], labels[3]]))
        self.assertAlmostEqual(self.exts[2].heater.control.config_units()["Kp"],
                               41., places=6)
        self.assertEqual(s.label(s.toolhead_in_slot(2)), tuned)

    def test_set_pid_temp(self):
        mod = self.boot()
        g = self.p.gcode.run("VT_SET_PID_TEMP", TEMP=230)
        self.assertEqual(mod.store.pid_temp, 230.)
        self.assertIn("230", g.out[0])
        with self.assertRaises(GCodeError):
            self.p.gcode.run("VT_SET_PID_TEMP", TEMP=1000)
        self.assertEqual(mod.store.pid_temp, 230.)

    def test_status_reports_pid(self):
        mod = self.boot()
        self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder TARGET=215")
        st = mod.get_status()
        self.assertEqual(st["pid_temp"], vt.DEFAULT_PID_TEMP)
        entry = st["toolheads"][mod.store.label(mod.store.toolhead_in_slot(0))]
        self.assertTrue(entry["pid"]["calibrated"])
        self.assertEqual(entry["pid"]["target"], 215.)
        self.assertEqual(entry["pid"]["source"], "calibrated")

    def test_describe_mentions_pid(self):
        mod = self.boot()
        th = mod.store.toolhead_in_slot(0)
        self.assertIn("PID stored", mod._describe(th))
        self.p.gcode.run_line("PID_CALIBRATE HEATER=extruder TARGET=215")
        self.assertIn("PID 215C", mod._describe(th))

    def test_hook_is_installed_once(self):
        self.boot()
        hooked = self.p.gcode.cmds["PID_CALIBRATE"]
        self.p.send_event("klippy:connect")
        self.assertIs(self.p.gcode.cmds["PID_CALIBRATE"], hooked)


PID_B = {"Kp": 41., "Ki": 11., "Kd": 21.}


class PidListStoreTests(unittest.TestCase):
    def setUp(self):
        self.s = vt.VirtualToolheadStore.from_slots(slots4())
        self.th = self.s.layout[1]

    def list(self):
        return [(t, r["values"]["Kp"]) for t, r in self.s.pid_profile_list(self.th)]

    def test_every_calibrated_temperature_is_added(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.record_pid(self.th, dict(PID_B), 250, "calibrated")
        self.assertEqual(self.list(), [(210., PID_A["Kp"]), (250., PID_B["Kp"])])
        self.assertEqual(self.s.pid(self.th)["target"], 250.)   # latest in use

    def test_nozzle_change_keeps_tunings_but_marks_them_stale(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.replace_nozzle(self.th)
        self.assertTrue(self.s.pid(self.th)["stale"])
        self.assertTrue(self.s.pid_profile_list(self.th)[0][1]["stale"])
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.assertFalse(self.s.pid(self.th).get("stale"))
        self.assertFalse(self.s.pid_profile_list(self.th)[0][1]["stale"])

    def test_a_rerun_at_the_same_temperature_replaces_that_entry(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.record_pid(self.th, dict(PID_B), 210, "calibrated")
        self.assertEqual(self.list(), [(210., PID_B["Kp"])])

    def test_adopted_and_observed_tunings_are_not_listed(self):
        self.s.record_pid(self.th, dict(PID_A), None, "adopted")
        self.assertEqual(self.list(), [])
        self.assertIsNotNone(self.s.pid(self.th))

    def test_select_makes_a_stored_tuning_active(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.record_pid(self.th, dict(PID_B), 250, "calibrated")
        self.assertTrue(self.s.select_pid(self.th, 210))
        self.assertEqual(self.s.pid(self.th)["values"]["Kp"], PID_A["Kp"])
        self.assertEqual(self.s.pid(self.th)["target"], 210.)
        self.assertFalse(self.s.select_pid(self.th, 210))        # no change
        self.assertTrue(self.s.pid_profile_active(self.th, "210"))
        self.assertFalse(self.s.pid_profile_active(self.th, "250"))
        self.assertEqual(len(self.list()), 2)                    # list kept

    def test_select_of_an_unknown_temperature_is_an_error(self):
        with self.assertRaises(vt.StoreError):
            self.s.select_pid(self.th, 230)

    def test_a_nozzle_change_keeps_the_list_and_the_tuning(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.record_pid(self.th, dict(PID_B), 250, "calibrated")
        self.s.replace_nozzle(self.th, diameter=0.4)
        self.s.set_nozzle(self.th, diameter=0.6)
        self.assertEqual(len(self.list()), 2)
        self.assertEqual(self.s.pid(self.th)["target"], 250.)

    def test_clear_pid_forgets_the_list_too(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.clear_pid(self.th)
        self.assertEqual(self.list(), [])
        self.assertIsNone(self.s.pid(self.th))

    def test_schema_4_gains_the_list_from_a_calibrated_tuning(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        self.s.record_pid(self.s.layout[2], dict(PID_A), None, "adopted")
        d = self.s.to_dict()
        d["schema_version"] = 4
        for th in d["toolheads"].values():
            th.pop("pid_profiles")
        s = vt.VirtualToolheadStore.from_dict(d)
        self.assertEqual(s.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(s.migrated_from, 4)
        self.assertEqual([t for t, _ in s.pid_profile_list(self.th)], [210.])
        self.assertEqual(s.pid_profile_list(self.s.layout[2]), [])   # adopted
        self.assertEqual(s.pid(self.th)["target"], 210.)

    def test_roundtrip_and_corruption(self):
        self.s.record_pid(self.th, dict(PID_A), 210, "calibrated")
        again = vt.VirtualToolheadStore.from_dict(
            json.loads(json.dumps(self.s.to_dict())))
        self.assertEqual(again.to_dict(), self.s.to_dict())
        d = self.s.to_dict()
        d["toolheads"][self.th]["pid_profiles"]["210"]["values"] = {"Kp": 1}
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)
        d = self.s.to_dict()
        d["toolheads"][self.th]["pid_profiles"]["hot"] = {}
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)


class PidListModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts, self.stock = pid_printer(self.tmp)
        self.mod = start(self.p)
        self.p.gcode.take_prompt()

    def kp(self, slot):
        return self.exts[slot].heater.control.config_units()["Kp"]

    def calibrate(self, heater="extruder2", temp=230):
        self.p.gcode.run_line("PID_CALIBRATE HEATER=%s TARGET=%d" % (heater, temp))

    def th(self, slot=2):
        return self.mod.store.toolhead_in_slot(slot)

    def test_each_temperature_run_is_added(self):
        self.calibrate(temp=210)
        self.calibrate(temp=250)
        self.calibrate(temp=210)                  # again: replaces, no new entry
        self.assertEqual([t for t, _ in self.mod.store.pid_profile_list(self.th())],
                         [210., 250.])

    def test_vt_pid_select_switches_the_heater_and_survives_a_restart(self):
        self.calibrate(temp=210)
        kp210 = self.kp(2)
        self.calibrate(temp=250)
        self.assertNotAlmostEqual(self.kp(2), kp210)
        g = self.p.gcode.run("VT_PID_SELECT", TOOLHEAD="C", TEMP=210)
        self.assertIn("now in use", g.out[0])
        self.assertAlmostEqual(self.kp(2), kp210, places=6)
        self.assertEqual(self.mod.store.pid(self.th())["target"], 210.)
        self.mod._save()
        with open(self.mod._store_path()) as f:
            saved = json.load(f)
        self.assertEqual(saved["toolheads"][self.th()]["pid"]["target"], 210.)

    def test_select_of_a_toolhead_in_no_slot_only_changes_the_store(self):
        s = self.mod.store
        th = s.add_toolhead(label="E")
        s.record_pid(th, dict(PID_A), 210, "calibrated")
        s.record_pid(th, dict(PID_B), 250, "calibrated")
        before = [self.kp(i) for i in range(4)]
        self.p.gcode.run("VT_PID_SELECT", TOOLHEAD="E", TEMP=210)
        self.assertEqual([self.kp(i) for i in range(4)], before)
        self.assertEqual(s.pid(th)["target"], 210.)

    def test_select_errors(self):
        for params in ({"TOOLHEAD": "C", "TEMP": 230}, {"TOOLHEAD": "Z", "TEMP": 210}):
            with self.assertRaises(GCodeError):
                self.p.gcode.run("VT_PID_SELECT", **params)

    def test_select_is_refused_while_printing(self):
        self.calibrate(temp=210)
        self.p.print_stats.state = "printing"
        with self.assertRaises(GCodeError):
            self.p.gcode.run("VT_PID_SELECT", TOOLHEAD="C", TEMP=210)

    def test_vt_pid_lists_the_tunings(self):
        self.calibrate(temp=210)
        self.calibrate(temp=250)
        self.p.gcode.run("VT_PID_SELECT", TOOLHEAD="C", TEMP=210)
        out = self.p.gcode.run("VT_PID", TOOLHEAD="C").out[0]
        self.assertIn("210 C", out)
        self.assertIn("250 C", out)
        self.assertEqual(out.count("<- in use"), 1)
        self.assertIn("210 C", out.split("<- in use")[0].splitlines()[-1])

    def test_status_publishes_the_list_for_the_card(self):
        self.calibrate(temp=210)
        self.calibrate(temp=250)
        pid = self.mod.get_status()["toolheads"]["C"]["pid"]
        self.assertEqual([(q["temp"], q["active"]) for q in pid["profiles"]],
                         [(210., False), (250., True)])
        self.p.gcode.run("VT_PID_SELECT", TOOLHEAD="C", TEMP=210)
        pid = self.mod.get_status()["toolheads"]["C"]["pid"]
        self.assertEqual([q["active"] for q in pid["profiles"]], [True, False])
        none = self.mod.get_status()["toolheads"]["A"]["pid"]
        self.assertEqual(none["profiles"], [])

    def test_dialog_lists_temperatures_and_applies_a_pick(self):
        self.calibrate(temp=210)
        kp210 = self.kp(2)
        self.calibrate(temp=250)
        self.p.gcode.run_line("VT_UI PAGE=pid_pick TOOLHEAD=C DIRECT=1")
        lines = self.p.gcode.take_prompt()
        text = "\n".join(lines)
        self.assertIn("PID tuning of C (T2)", text)
        self.assertNotIn("calibrated separately", text)
        btn = {l: g for l, g, _ in prompt_buttons(lines)}
        self.assertEqual(btn["210 \u00b0C"],
                         "VT_UI PAGE=pid_apply TOOLHEAD=C TEMP=210")
        self.assertIn("(in use)", text.split("250 C")[1])
        # preset temperatures that were never calibrated start a PID-only run
        new = btn["220 \u00b0C (new)"]
        self.assertIn("VT_CAL PAGE=prep", new)
        for part in ("SEL=2", "PID=1", "OFS=0", "FLOW=0", "PT=220"):
            self.assertIn(part, new)
        self.assertNotIn("250 \u00b0C (new)", btn)
        # picking a stored one switches and closes: no new dialog
        self.p.gcode.run_line(btn["210 \u00b0C"])
        after = self.p.gcode.take_prompt()
        self.assertEqual([l for l in after if l != '// action:prompt_end'], [])
        self.assertAlmostEqual(self.kp(2), kp210, places=6)

    def test_menu_leads_to_the_pid_dialog_with_a_back_button(self):
        self.p.gcode.run_line("VT_UI")
        btn = {l: g for l, g, _ in prompt_buttons(self.p.gcode.take_prompt())}
        self.assertEqual(btn["Change PID"], "VT_UI PAGE=pid")
        self.p.gcode.run_line(btn["Change PID"])
        lines = self.p.gcode.take_prompt()
        btn = {l: g for l, g, _ in prompt_buttons(lines)}
        self.assertIn("Change PID: which toolhead?", "\n".join(lines))
        self.p.gcode.run_line(btn["C (T2)"])
        lines = self.p.gcode.take_prompt()
        btn = {l: g for l, g, _ in prompt_buttons(lines)}
        self.assertEqual(btn["Back"], "VT_UI PAGE=pid")

    def test_dock_dialog_asks_then_parks(self):
        self.p.gcode.run_line("VT_UI PAGE=dock DIRECT=1")
        lines = self.p.gcode.take_prompt()
        self.assertIn("Dock the active tool?", "\n".join(lines))
        btn = {l: g for l, g, _ in prompt_buttons(lines)}
        self.assertEqual(btn["Dock"], "VT_UI PAGE=dock_apply")
        parked = []
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: parked.append(1)
        self.p.gcode.run_line(btn["Dock"])
        self.assertEqual(parked, [1])
        after = self.p.gcode.take_prompt()
        self.assertEqual(after[-1], "// action:prompt_end")

    def test_dialog_without_tunings_offers_calibration(self):
        self.p.gcode.run_line("VT_UI PAGE=pid_pick TOOLHEAD=A")
        btn = {l: g for l, g, _ in prompt_buttons(self.p.gcode.take_prompt())}
        self.assertIn("210 \u00b0C (new)", btn)

    def test_replacing_the_nozzle_keeps_the_list(self):
        self.calibrate(temp=210)
        self.p.gcode.run_line("VT_REPLACE_NOZZLE TOOLHEAD=C")
        pid = self.mod.get_status()["toolheads"]["C"]["pid"]
        self.assertEqual([q["temp"] for q in pid["profiles"]], [210.])
        self.assertTrue(pid["calibrated"])


class PidWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts, self.pstock = pid_printer(self.tmp)
        self.stock = FakeStockCalibration(self.p, self.exts)
        self.mod = start(self.p)
        self.p.gcode.take_prompt()
        self.order = []
        g = self.p.gcode
        for name in ("PID_CALIBRATE", "TEMPERATURE_WAIT",
                     "EXTRUDER_OFFSET_ACTION_PRESTART"):
            g.cmds[name] = self._tap(name, g.cmds[name])

    def _tap(self, name, fn):
        def tapped(gcmd):
            self.order.append((name, gcmd.params.get("HEATER")
                               or gcmd.params.get("SENSOR")))
            return fn(gcmd)
        return tapped

    def click(self, line):
        self.p.gcode.run_line(line)
        return self.p.gcode.take_prompt()

    @staticmethod
    def labels(lines):
        return [l for l, _, _ in prompt_buttons(lines)]

    def button(self, lines, label):
        return CalibrationTests.button(lines, label)

    def need_calibration(self, *slots):
        for slot in slots:
            th = self.mod.store.toolhead_in_slot(slot)
            keep = self.mod.store.pid(th)        # a new nozzle forgets PID
            self.click("VT_REPLACE_NOZZLE TOOLHEAD=%s"
                       % self.mod.store.label(th))
            if keep:
                self.mod.store.record_pid(th, keep["values"], keep["target"],
                                          keep["source"])

    def test_pid_is_off_by_default_when_every_selected_toolhead_has_it(self):
        self.need_calibration(1)
        lines = self.click("VT_CALIBRATE")
        self.assertIn("PID calibration: off", self.labels(lines))
        start = self.button(lines, "Start")
        self.assertIn("PID=0", start)

    def test_pid_is_preselected_for_a_toolhead_without_tuning(self):
        th = self.mod.store.toolhead_in_slot(1)
        self.mod.store.clear_pid(th)
        self.click("VT_REPLACE_NOZZLE TOOLHEAD=%s" % self.mod.store.label(th))
        lines = self.click("VT_CALIBRATE")
        self.assertIn("PID calibration: on", self.labels(lines))
        self.assertIn("PID=1", self.button(lines, "Start"))
        # temperature choices are offered, the default is marked
        self.assertIn("210 C", self.labels(lines))
        colors = {l: c for l, _, c in prompt_buttons(lines)}
        self.assertEqual(colors["210 C"], "primary")
        self.assertNotEqual(colors["230 C"], "primary")

    def test_toggling_pid_and_picking_a_temperature(self):
        self.need_calibration(1)
        lines = self.click("VT_CALIBRATE")
        lines = self.click(self.button(lines, "PID calibration: off"))
        self.assertIn("PID calibration: on", self.labels(lines))
        lines = self.click(self.button(lines, "230 C"))
        self.assertIn("PT=230", self.button(lines, "Start"))
        lines = self.click(self.button(lines, "PID calibration: on"))
        self.assertNotIn("230 C", self.labels(lines))

    def test_pid_runs_first_for_the_selected_toolheads_only(self):
        s = self.mod.store
        self.click("VT_REPLACE_NOZZLE TOOLHEAD=%s" % s.label(s.toolhead_in_slot(1)))
        self.click("VT_REPLACE_NOZZLE TOOLHEAD=%s" % s.label(s.toolhead_in_slot(3)))
        self.click("VT_CAL PAGE=prep SEL=1,3 OFS=1 PID=1 PT=225")
        names = [n for n, _ in self.order]
        self.assertEqual(self.pstock.calls,
                         [("extruder1", "225"), ("extruder3", "225")])
        self.assertLess(names.index("PID_CALIBRATE"),
                        names.index("EXTRUDER_OFFSET_ACTION_PRESTART"))
        self.assertEqual(names.count("PID_CALIBRATE"), 2)
        self.assertEqual(s.pid_temp, 225.)         # remembered
        # untouched toolheads keep their tuning
        self.assertEqual(s.pid(s.toolhead_in_slot(0))["source"], "adopted")
        self.assertEqual(s.pid(s.toolhead_in_slot(1))["source"], "calibrated")
        # the probing flow went on to the first wipe dialog
        self.assertEqual(self.stock.tools_for("HEAT"), ["T1"])
        # the heaters were switched off after tuning
        self.assertEqual([t for _, t in self.pstock.heaters.set_calls], [0., 0.])

    def test_hot_nozzle_is_cooled_before_tuning(self):
        self.exts[1].heater.smoothed_temp = 180.
        self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=210")
        self.assertEqual(self.pstock.waits, ["extruder1"])
        names = [n for n, _ in self.order]
        self.assertLess(names.index("TEMPERATURE_WAIT"),
                        names.index("PID_CALIBRATE"))
        self.assertEqual(self.pstock.heaters.set_calls[0][1], 0.)

    def test_cold_nozzle_is_not_waited_for(self):
        self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=210")
        self.assertEqual(self.pstock.waits, [])

    def test_no_pid_goes_straight_to_probing(self):
        self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=0")
        self.assertEqual(self.pstock.calls, [])
        self.assertEqual(self.stock.tools_for("HEAT"), ["T1"])

    def test_failure_offers_retry_skip_and_abort_and_switches_the_heater_off(self):
        self.pstock.fail["extruder1"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1,2 OFS=1 PID=1 PT=210")
        text = "\n".join(lines)
        self.assertIn("PID calibration of T1 failed", text)
        self.assertEqual(self.labels(lines)[:3],
                         ["Retry", "Skip PID", "Abort calibration"])
        self.assertEqual(self.stock.calls, [])          # probing has not begun
        self.assertEqual(self.pstock.heaters.set_calls[-1][1], 0.)
        lines = self.click(self.button(lines, "Retry"))
        self.assertEqual(self.pstock.calls,
                         [("extruder1", "210"), ("extruder1", "210"),
                          ("extruder2", "210")])
        self.assertEqual(self.stock.tools_for("HEAT"), ["T1"])

    def test_retry_resumes_at_the_failed_toolhead(self):
        self.pstock.fail["extruder2"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1,2 OFS=1 PID=1 PT=210")
        self.assertEqual(self.pstock.calls,
                         [("extruder1", "210"), ("extruder2", "210")])
        self.click(self.button(lines, "Retry"))
        self.assertEqual(self.pstock.calls[2:], [("extruder2", "210")])

    def test_skip_pid_continues_with_the_probing_flow(self):
        self.pstock.fail["extruder1"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=210")
        lines = self.click(self.button(lines, "Skip PID"))
        self.assertEqual(self.stock.tools_for("HEAT"), ["T1"])
        self.assertIn("Wipe the nozzle of T1", "\n".join(lines))

    def test_abort_after_failure_leaves_the_guard_released(self):
        self.pstock.fail["extruder1"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=210")
        self.click(self.button(lines, "Abort calibration"))
        self.assertFalse(self.mod.calibrator._on)
        self.assertEqual(self.pstock.heaters.set_calls[-1][1], 0.)
        # the touchscreen's own commands work again once the grace is over
        self.mod.calibrator._tail_until = 0
        self.p.gcode.run_line("EXTRUDER_OFFSET_ACTION_PRESTART")

    def test_a_calibration_that_produces_no_new_tuning_is_a_failure(self):
        self.pstock.pid_calibrate_orig = self.p.gcode.cmds["PID_CALIBRATE"]
        # a PID_CALIBRATE that succeeds but changes nothing
        self.p.gcode.cmds["PID_CALIBRATE"] = lambda g: None
        self.mod._pid_hooked = False
        self.mod._install_pid_hook()
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=210")
        self.assertIn("no new tuning", "\n".join(lines))

    def test_foreign_commands_are_rejected_while_the_pid_dialog_is_up(self):
        self.pstock.fail["extruder1"] = 1
        self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=210")     # failure dialog
        with self.assertRaises(GCodeError) as cm:
            self.p.gcode.run_line("EXTRUDER_OFFSET_ACTION_PRESTART")
        self.assertIn("rejected", str(cm.exception))
        self.assertEqual(self.stock.calls, [])

    def test_console_entry_with_pid_options(self):
        self.p.gcode.run_line("VT_CALIBRATE TOOLHEADS=%s PID=1 PID_TEMP=220 "
                              "CONFIRM=1" % self.mod.store.label(
                                  self.mod.store.toolhead_in_slot(1)))
        self.assertEqual(self.pstock.calls, [("extruder1", "220")])

    def test_bad_pid_temperature_is_refused(self):
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 PT=900")
        self.assertEqual(self.pstock.calls, [])


# ---------------------------------------------------------------------------
# Dynamic flow (K) per toolhead and filament
# ---------------------------------------------------------------------------
class FakeFlowCalibrator:
    """The bits of flow_calibrator.FlowCalibrator that virtual_toolheads
    touches: a K per extruder name, saved on request, applied on request."""
    def __init__(self, printer, exts, k=0.02):
        self.printer = printer
        self.names = [e.name for e in exts]
        self._current_k = {n: k for n in self.names}
        self.saved = 0
        self.applied = []          # _apply_calibrate_k(idx) -> K used
        self.stock_applied = []    # FLOW_APPLY_CALIBRATE_K ran with this K
        self.result = {}           # extruder name -> K the next run measures
        self.fail = {}             # extruder name -> failures left
        self.calls = 0
        printer.objects["flow_calibrator"] = self
        g = printer.gcode.cmds
        g["FLOW_CALIBRATE"] = self.cmd_calibrate
        g["FLOW_APPLY_CALIBRATE_K"] = self.cmd_apply

    def _save_config(self):
        self.saved += 1

    def _apply_calibrate_k(self, idx):
        self.applied.append((idx, self._current_k[self.names[idx]]))

    def cmd_calibrate(self, gcmd):
        self.calls += 1
        ex = self.printer.objects["toolhead"].get_extruder()
        if self.fail.get(ex.name, 0) > 0:
            self.fail[ex.name] -= 1
            raise GCodeError("e1_filament runout")
        k = self.result.get(ex.name)
        if k is not None:
            self._current_k[ex.name] = k

    def cmd_apply(self, gcmd):
        idx = gcmd.get_int("EXTRUDER")
        self.stock_applied.append((idx, self._current_k[self.names[idx]]))


class FakeTaskConfig:
    def __init__(self, printer, spools):
        self.status = {
            "filament_spool_id": list(spools),
            "filament_vendor": ["Snapmaker"] * 4,
            "filament_type": ["PLA"] * 4,
            "filament_sub_type": ["SnapSpeed"] * 4,
        }
        printer.objects["print_task_config"] = self
        printer.gcode.cmds["SET_PRINT_FILAMENT_CONFIG"] = self.cmd_set

    def get_status(self):
        return {k: list(v) for k, v in self.status.items()}

    def cmd_set(self, gcmd):
        i = gcmd.get_int("CONFIG_EXTRUDER")
        if gcmd.get("FILAMENT_SPOOL_ID", None) is not None:
            self.status["filament_spool_id"][i] = gcmd.get_int("FILAMENT_SPOOL_ID")
        if gcmd.get("FILAMENT_TYPE", None) is not None:
            self.status["filament_type"][i] = gcmd.get("FILAMENT_TYPE")


class FakeFilamentParameters:
    def __init__(self, printer, default=0.02):
        self.default = default
        printer.objects["filament_parameters"] = self

    def get_flow_k(self, vendor, main, sub, diameter, volume_type):
        return self.default


def sync_spawn(self, work, done):
    try:
        result = work()
    except Exception:
        result = None
    done(result)


class FlowStoreTests(unittest.TestCase):
    def store(self):
        return vt.VirtualToolheadStore.from_slots(slots4())

    def test_new_store_has_empty_flow(self):
        s = self.store()
        self.assertEqual(s.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(s.spool_filament, {})
        self.assertFalse(s.flow_adopted)
        self.assertTrue(all(s.flow_entries(i) == {} for i in s.ids()))

    def test_record_and_lookup_respects_the_nozzle(self):
        s = self.store()
        th = s.layout[1]
        self.assertTrue(s.record_flow(th, "fil:11", 0.04, 0.4, "standard"))
        self.assertFalse(s.record_flow(th, "fil:11", 0.04, 0.4, "standard"))
        self.assertEqual(s.flow_entry(th, "fil:11", 0.4, "standard")["k"], 0.04)
        self.assertIsNone(s.flow_entry(th, "fil:11", 0.6, "standard"))
        self.assertIsNone(s.flow_entry(th, "fil:11", 0.4, "high_flow"))
        self.assertIsNone(s.flow_entry(th, "fil:12", 0.4, "standard"))
        self.assertTrue(s.record_flow(th, "fil:11", 0.05, 0.4, "standard"))

    def test_bad_keys_and_values_are_refused(self):
        s = self.store()
        th = s.layout[0]
        for key in ("", "x:1", "fil:", "fil:abc", "tag:"):
            with self.assertRaises(vt.StoreError):
                s.record_flow(th, key, 0.03, 0.4, "standard")
        for k in (0, -1, 2, "x"):
            with self.assertRaises(vt.StoreError):
                s.record_flow(th, "fil:1", k, 0.4, "standard")

    def test_forget(self):
        s = self.store()
        th = s.layout[0]
        s.record_flow(th, "fil:1", 0.03, 0.4, "standard")
        s.record_flow(th, "fil:2", 0.03, 0.4, "standard")
        self.assertEqual(s.forget_flow(th, "fil:1"), 1)
        self.assertEqual(s.forget_flow(th, "fil:1"), 0)
        self.assertEqual(s.forget_flow(th), 1)

    def test_learning_a_filament_id_rekeys_spool_values(self):
        s = self.store()
        a, b = s.layout[1], s.layout[2]
        s.record_flow(a, "spool:35", 0.04, 0.4, "standard")
        s.record_flow(b, "spool:35", 0.05, 0.4, "standard")
        s.record_flow(b, "fil:11", 0.06, 0.4, "standard")
        self.assertTrue(s.set_spool_filament(35, 11))
        self.assertEqual(list(s.flow_entries(a)), ["fil:11"])
        self.assertEqual(s.flow_entries(b)["fil:11"]["k"], 0.06)   # existing wins
        self.assertNotIn("spool:35", s.flow_entries(b))
        self.assertFalse(s.set_spool_filament(35, 11))

    def test_roundtrip_and_corruption(self):
        s = self.store()
        s.record_flow(s.layout[0], "tag:Snapmaker|PLA|SnapSpeed", 0.03, 0.4,
                      "standard")
        s.set_spool_filament(5, 7)
        again = vt.VirtualToolheadStore.from_dict(json.loads(json.dumps(s.to_dict())))
        self.assertEqual(again.to_dict(), s.to_dict())
        d = s.to_dict()
        d["toolheads"][s.layout[0]]["flow"]["fil:1"] = {
            "k": 5, "diameter": 0.4, "volume_type": "standard"}
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)
        d = s.to_dict()
        d["spool_filament"] = {"x": 1}
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)

    def test_schema_3_gains_flow_fields(self):
        d = self.store().to_dict()
        d["schema_version"] = 3
        d.pop("spool_filament")
        d.pop("flow_adopted")
        for th in d["toolheads"].values():
            th.pop("flow")
        s = vt.VirtualToolheadStore.from_dict(d)
        self.assertTrue(s.migrated)
        self.assertEqual(s.migrated_from, 3)
        self.assertEqual(s.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertTrue(all(s.flow_entries(i) == {} for i in s.ids()))

    def test_schema_2_goes_all_the_way_up(self):
        s = vt.VirtualToolheadStore.from_dict(v2_store())
        self.assertEqual(s.migrated_from, 2)
        self.assertEqual(s.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertEqual(s.spool_filament, {})


class FlowModuleTests(unittest.TestCase):
    FILAMENTS = {35: 10, 34: 11, 37: 12, 36: 13, 40: 20, 41: 11}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fetched = []
        self.FILAMENTS = dict(FlowModuleTests.FILAMENTS)
        outer = self
        orig_spawn = vt.VirtualToolheads._spawn
        orig_fetch = vt.VirtualToolheads._fetch_filament_id

        def fetch(self_, spool_id):
            outer.fetched.append(spool_id)
            if spool_id not in outer.FILAMENTS:
                raise OSError("unreachable")
            return outer.FILAMENTS[spool_id]
        vt.VirtualToolheads._spawn = sync_spawn
        vt.VirtualToolheads._fetch_filament_id = fetch
        self.addCleanup(setattr, vt.VirtualToolheads, "_spawn", orig_spawn)
        self.addCleanup(setattr, vt.VirtualToolheads, "_fetch_filament_id",
                        orig_fetch)
        self.p, self.exts = make_printer(self.tmp, BP)
        self.fc = FakeFlowCalibrator(self.p, self.exts)
        self.ptc = FakeTaskConfig(self.p, [35, 34, 37, 36])
        self.fp = FakeFilamentParameters(self.p)

    def boot(self):
        self.mod = start(self.p)
        return self.mod

    def th(self, slot):
        return self.mod.store.toolhead_in_slot(slot)

    def calibrate(self, slot, k):
        self.p.objects["toolhead"].ex = self.exts[slot]
        self.fc.result = {self.exts[slot].name: k}
        self.p.gcode.run_line("FLOW_CALIBRATE")

    def set_spool(self, slot, spool):
        self.p.gcode.run_line("SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=%d "
                              "FILAMENT_SPOOL_ID=%d" % (slot, spool))

    def k(self, slot):
        return self.fc._current_k[self.exts[slot].name]

    def entry(self, slot, key):
        e = self.exts[slot]
        return self.mod.store.flow_entry(self.th(slot), key, e.nozzle_diameter,
                                         e.nozzle_volume_type)

    # ---- startup ---------------------------------------------------------
    def test_a_calibrated_k_is_adopted_once_for_the_loaded_filament(self):
        self.fc._current_k["extruder1"] = 0.035
        self.boot()
        s = self.mod.store
        self.assertEqual(self.entry(1, "fil:11")["k"], 0.035)
        self.assertEqual(self.entry(1, "fil:11")["source"], "adopted")
        for slot in (0, 2, 3):          # still the default: nothing to keep
            self.assertEqual(s.flow_entries(self.th(slot)), {})
        self.assertTrue(s.flow_adopted)
        self.assertEqual(s.spool_filament["34"], 11)
        # not adopted a second time
        self.p2, self.exts2 = make_printer(self.tmp, BP)
        fc2 = FakeFlowCalibrator(self.p2, self.exts2)
        FakeTaskConfig(self.p2, [35, 34, 37, 36])
        FakeFilamentParameters(self.p2)
        fc2._current_k["extruder2"] = 0.05
        mod2 = start(self.p2)
        self.assertEqual(mod2.store.flow_entries(mod2.store.toolhead_in_slot(2)), {})

    def test_without_flow_calibrator_everything_is_inert(self):
        del self.p.objects["flow_calibrator"]
        mod = self.boot()
        self.assertIsNotNone(mod.store)
        self.assertEqual(self.fetched, [])
        st = mod.get_status()["toolheads"]["B"]["flow"]
        self.assertEqual(st["count"], 0)

    # ---- recording -------------------------------------------------------
    def test_flow_calibrate_is_recorded_for_the_toolhead_and_filament(self):
        self.boot()
        self.calibrate(1, 0.041)
        e = self.entry(1, "fil:11")
        self.assertEqual((e["k"], e["source"]), (0.041, "calibrated"))
        self.assertEqual(self.fc.calls, 1)
        with open(self.mod._store_path()) as f:
            saved = json.load(f)
        self.assertEqual(saved["toolheads"][self.th(1)]["flow"]["fil:11"]["k"], 0.041)

    def test_every_flow_run_is_logged_and_summarised(self):
        self.boot()
        self.set_spool(0, 2)
        for k in (0.040, 0.042, 0.041):
            self.calibrate(0, k)
        rows = [r for r in self.mod._read_cal_log() if r["kind"] == "flow"]
        self.assertEqual([r["k"] for r in rows], [0.040, 0.042, 0.041])
        self.assertEqual(rows[0]["outcome"], "recorded")
        out = []
        class G:
            def get(s_, n, d=None, **kw): return {"KIND": None}.get(n, d)
            def get_int(s_, n, d=None, **kw): return d
            def respond_info(s_, m): out.append(m)
        self.mod.cmd_VT_CAL_LOG(G())
        text = "\n".join(out)
        self.assertIn("(slot T0)", text)
        self.assertIn("3 runs", text)
        self.assertIn("k mean 0.0410 sd 0.0010 range 0.0020", text)

    def test_the_log_survives_a_restart_and_can_be_cleared(self):
        self.boot()
        self.set_spool(0, 2)
        self.calibrate(0, 0.040)
        self.assertEqual(len(self.mod._read_cal_log()), 1)
        self.p.handlers.clear()
        self.boot()
        self.assertEqual(len(self.mod._read_cal_log()), 1)
        class G:
            def get(s_, n, d=None, **kw): return {"ACTION": "CLEAR"}.get(n, d)
            def respond_info(s_, m): pass
        self.mod.cmd_VT_CAL_LOG(G())
        self.assertEqual(self.mod._read_cal_log(), [])

    def test_failed_unchanged_or_default_results_are_not_recorded(self):
        self.boot()
        self.calibrate(1, None)                  # aborted: K untouched
        self.calibrate(1, 0.02)                  # out of range -> the default
        self.assertEqual(self.mod.store.flow_entries(self.th(1)), {})
        self.calibrate(1, 0.045)
        self.calibrate(1, 0.045)                 # same again: no change
        self.assertEqual(len(self.mod.store.flow_entries(self.th(1))), 1)

    def test_a_failing_calibration_still_raises_and_clears_busy(self):
        self.boot()

        def boom(gcmd):
            raise GCodeError("filament runout")
        # put a failing stock handler under our wrapper by re-hooking
        self.mod._flow_hooked = False
        self.p.gcode.cmds["FLOW_CALIBRATE"] = boom
        self.mod._install_flow_hooks()
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("FLOW_CALIBRATE")
        self.assertFalse(self.mod._flow_busy)
        self.assertEqual(self.mod.store.flow_entries(self.th(0)), {})

    def test_a_lane_without_a_spool_uses_the_tag(self):
        self.ptc.status["filament_spool_id"][1] = 0
        self.boot()
        self.calibrate(1, 0.05)
        self.assertEqual(self.entry(1, "tag:Snapmaker|PLA|SnapSpeed")["k"], 0.05)

    def test_a_lane_without_filament_records_nothing(self):
        self.ptc.status["filament_spool_id"][1] = 0
        self.ptc.status["filament_type"][1] = "NONE"
        self.boot()
        self.calibrate(1, 0.05)
        self.assertEqual(self.mod.store.flow_entries(self.th(1)), {})

    # ---- swapping filaments in one toolhead ------------------------------
    def test_swapping_filaments_back_and_forth_restores_each_k(self):
        self.boot()
        self.calibrate(1, 0.041)                       # filament 11
        self.set_spool(1, 40)                          # filament 20, no value
        self.assertEqual(self.k(1), 0.041)             # stock behaviour: kept
        self.calibrate(1, 0.031)
        self.assertEqual(self.entry(1, "fil:20")["k"], 0.031)
        self.fc.applied.clear()
        self.set_spool(1, 34)                          # back to filament 11
        self.assertEqual(self.k(1), 0.041)
        self.assertEqual(self.fc.applied[-1], (1, 0.041))
        self.set_spool(1, 40)
        self.assertEqual(self.k(1), 0.031)

    def test_another_spool_of_the_same_filament_shares_the_value(self):
        self.boot()
        self.calibrate(1, 0.041)                       # spool 34 = filament 11
        self.set_spool(1, 40)
        self.set_spool(1, 41)                          # spool 41 = filament 11
        self.assertEqual(self.k(1), 0.041)

    def test_a_value_measured_with_another_nozzle_is_not_used(self):
        self.boot()
        self.calibrate(1, 0.041)
        self.set_spool(1, 40)
        self.p.gcode.run("VT_SET_NOZZLE", TOOLHEAD="B", DIAMETER=0.6)
        self.set_spool(1, 34)
        self.assertIsNone(self.entry(1, "fil:11"))
        self.fc._current_k["extruder1"] = 0.07
        self.set_spool(1, 40)
        self.set_spool(1, 34)
        self.assertEqual(self.k(1), 0.07)

    def test_values_follow_the_toolhead_to_another_slot(self):
        self.ptc.status["filament_spool_id"][2] = 34       # same filament
        self.boot()
        self.calibrate(1, 0.041)                           # toolhead B
        self.p.gcode.run("VT_SET_LAYOUT", LAYOUT="A,C,B,D")
        self.assertEqual(self.k(2), 0.041)                 # B is in slot 2 now
        self.assertEqual(self.fc.applied[-1], (2, 0.041))

    def test_print_start_applies_the_value_even_while_printing(self):
        self.boot()
        self.calibrate(1, 0.041)
        self.fc._current_k["extruder1"] = 0.099            # something else
        self.p.print_stats.state = "printing"
        self.p.gcode.run_line("FLOW_APPLY_CALIBRATE_K EXTRUDER=1")
        self.assertEqual(self.fc.stock_applied[-1], (1, 0.041))

    def test_a_filament_change_during_a_print_changes_nothing(self):
        self.boot()
        self.calibrate(1, 0.041)
        self.set_spool(1, 40)
        self.calibrate(1, 0.031)
        self.p.print_stats.state = "printing"
        self.set_spool(1, 34)
        self.assertEqual(self.k(1), 0.031)

    # ---- Spoolman lookup -------------------------------------------------
    def test_spool_key_is_used_until_the_filament_id_is_known(self):
        del self.FILAMENTS[34]
        self.boot()
        self.calibrate(1, 0.041)
        self.assertEqual(self.entry(1, "spool:34")["k"], 0.041)
        self.FILAMENTS[34] = 11
        self.mod._lookup_failed.clear()
        self.mod._refresh_flow()
        self.assertEqual(self.entry(1, "fil:11")["k"], 0.041)
        self.assertIsNone(self.entry(1, "spool:34"))

    def test_a_failed_lookup_is_not_repeated_straight_away(self):
        del self.FILAMENTS[34]
        self.boot()
        n = self.fetched.count(34)
        self.mod._refresh_flow()
        self.mod._refresh_flow()
        self.assertEqual(self.fetched.count(34), n)
        self.mod._lookup_failed[34] -= vt.FLOW_LOOKUP_RETRY + 1
        self.mod._refresh_flow()
        self.assertEqual(self.fetched.count(34), n + 1)

    def test_lookup_can_be_switched_off(self):
        self.mod = start(self.p, flow_filament_lookup=False)
        self.assertEqual(self.fetched, [])
        self.mod.store.record_flow(self.th(1), "spool:34", 0.04, 0.4, "standard")
        self.assertEqual(self.mod._filament_key(1), "spool:34")

    # ---- commands and status ---------------------------------------------
    def test_vt_flow_lists_values_and_the_current_filament(self):
        self.boot()
        self.calibrate(1, 0.041)
        out = self.p.gcode.run("VT_FLOW").out[0]
        self.assertIn("B  (slot 1, filament fil:11)", out)
        self.assertIn("fil:11  K 0.0410  0.4mm standard", out)
        self.assertIn("no stored flow values", out)
        out = self.p.gcode.run("VT_FLOW", TOOLHEAD="B").out[0]
        self.assertNotIn("A  (slot 0", out)

    def test_vt_flow_forget(self):
        self.boot()
        self.calibrate(1, 0.041)
        g = self.p.gcode.run("VT_FLOW_FORGET", TOOLHEAD="B", KEY="fil:11")
        self.assertIn("Forgot 1", g.out[0])
        self.assertEqual(self.mod.store.flow_entries(self.th(1)), {})
        self.calibrate(1, 0.041)
        self.p.print_stats.state = "printing"
        with self.assertRaises(GCodeError):
            self.p.gcode.run("VT_FLOW_FORGET", TOOLHEAD="B")

    def test_status_reports_flow(self):
        self.boot()
        self.calibrate(1, 0.041)
        st = self.mod.get_status()["toolheads"]
        self.assertEqual(st["B"]["flow"], {"count": 1, "filament": "fil:11",
                                           "k": 0.041})
        self.assertEqual(st["A"]["flow"]["k"], None)
        self.mod.store.add_toolhead(label="E")
        self.assertEqual(self.mod.get_status()["toolheads"]["E"]["flow"]["filament"],
                         None)

    def test_upgrade_from_schema_3_keeps_a_backup(self):
        self.boot()
        d = self.mod.store.to_dict()
        d["schema_version"] = 3
        d.pop("spool_filament")
        d.pop("flow_adopted")
        for th in d["toolheads"].values():
            th.pop("flow")
        with open(self.mod._store_path(), "w") as f:
            json.dump(d, f)
        p2, exts2 = make_printer(self.tmp, BP)
        FakeFlowCalibrator(p2, exts2)
        FakeTaskConfig(p2, [35, 34, 37, 36])
        FakeFilamentParameters(p2)
        mod2 = start(p2, legacy_labels=False)
        self.assertEqual(mod2.store.data["schema_version"], vt.SCHEMA_VERSION)
        self.assertTrue(os.path.exists(self.mod._store_path() + ".v3"))


class RelativeToT0Tests(unittest.TestCase):
    """Offsets are stored relative to T0 so sessions can be mixed."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts = make_printer(self.tmp, BP)
        self.stock = FakeStockCalibration(self.p, self.exts)
        self.mod = start(self.p)
        self.st = self.mod.store
        self.p.gcode.take_prompt()

    def stored(self, slot):
        return self.st.stored_base(self.st.toolhead_in_slot(slot))

    def session(self, **raws):
        """raws: slot number -> raw measurement, as the stock save does."""
        res = {e.name: None for e in self.exts}
        for slot, pos in raws.items():
            res[self.exts[int(slot[1:])].name] = list(pos)
        self.stock.last_xyz_result = res
        for slot, pos in raws.items():
            self.exts[int(slot[1:])].base_position = list(pos)
        self.p.send_event(vt.OFFSET_EVENT)
        return self.mod.calibrator.vt.take_frame_notes()

    def shifted(self, slot, dz, dy=0.0):
        b = BP[slot]
        return [b[0], b[1] + dy, b[2] + dz]

    def test_other_toolhead_is_corrected_by_the_frame_of_t0(self):
        notes = self.session(T0=self.shifted(0, 0.05), T1=self.shifted(1, 0.05))
        self.assertAlmostEqual(self.stored(1)[2], BP[1][2], places=6)
        self.assertAlmostEqual(self.stored(0)[2], BP[0][2], places=6)
        self.assertIn("relative to T0", " ".join(notes))
        # the printer sees the corrected values
        self.assertAlmostEqual(self.exts[1].base_position[2], BP[1][2], places=6)

    def test_single_toolhead_with_t0_keeps_the_others_valid(self):
        self.session(T0=self.shifted(0, -0.03), T2=self.shifted(2, -0.03 + 0.02))
        self.assertAlmostEqual(self.stored(2)[2], BP[2][2] + 0.02, places=6)
        for slot in (0, 1, 3):
            self.assertTrue(self.st.is_valid(self.st.toolhead_in_slot(slot)))
        self.assertAlmostEqual(self.stored(1)[2], BP[1][2], places=6)

    def test_without_t0_the_value_is_taken_as_is_with_a_warning(self):
        notes = self.session(T1=self.shifted(1, 0.04))
        self.assertAlmostEqual(self.stored(1)[2], BP[1][2] + 0.04, places=6)
        self.assertIn("T0 was not measured", " ".join(notes))

    def test_t0_alone_does_not_change_anything_stored(self):
        notes = self.session(T0=self.shifted(0, 0.05))
        self.assertAlmostEqual(self.stored(0)[2], BP[0][2], places=6)
        self.assertIn("not stored", " ".join(notes))
        self.assertTrue(self.st.is_valid(self.st.toolhead_in_slot(1)))

    def test_new_t0_nozzle_moves_the_others_by_the_change_of_t0(self):
        p = self.p
        p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=self.st.label(
            self.st.toolhead_in_slot(0)))
        self.assertFalse(self.st.is_valid(self.st.toolhead_in_slot(0)))
        # new T0 tip sits 0.10 mm lower than the old one; T1 was measured
        # with it in the same session (frame +0.02)
        self.session(T0=self.shifted(0, 0.02 + 0.10),
                     T1=self.shifted(1, 0.02))
        # T1 relative to the new T0 is -0.10 compared with before
        self.assertAlmostEqual(self.stored(1)[2] - self.stored(0)[2],
                               BP[1][2] - BP[0][2] - 0.10, places=6)
        # T2/T3 were not measured, they followed
        self.assertAlmostEqual(self.stored(2)[2] - self.stored(0)[2],
                               BP[2][2] - BP[0][2] - 0.10, places=6)
        self.assertTrue(self.st.is_valid(self.st.toolhead_in_slot(2)))

    def test_new_t0_without_a_bridge_says_so(self):
        self.p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=self.st.label(
            self.st.toolhead_in_slot(0)))
        notes = self.session(T0=self.shifted(0, 0.1))
        self.assertIn("cannot be tied", " ".join(notes))

    def test_ref_option_and_reference_is_added_to_a_single_toolhead(self):
        cal = self.mod.calibrator
        cal.add_ref = True
        sel, notes = cal._with_reference([2])
        self.assertEqual(sel, [0, 2])
        self.assertIn("measured too", notes[0])
        self.assertEqual(cal._with_reference([0, 2])[0], [0, 2])
        self.p.gcode.run("VT_CALIBRATION_LOOPS", REF="0")
        self.assertFalse(cal.add_ref)
        self.assertEqual(cal._with_reference([2]), ([2], []))

    def test_t0_alone_gets_a_valid_toolhead_added(self):
        cal = self.mod.calibrator
        cal.add_ref = True
        sel, notes = cal._with_reference([0])
        self.assertEqual(sel, [0, 1])
        self.assertIn("T0 alone", notes[0])

    def test_t0_alone_after_nozzle_change_says_others_stay_valid(self):
        self.p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=self.st.label(
            self.st.toolhead_in_slot(0)))
        cal = self.mod.calibrator
        cal.add_ref = True
        sel, notes = cal._with_reference([0])
        self.assertEqual(sel, [0, 1])
        self.assertIn("stay valid", notes[0])

    def test_t0_alone_warns_when_no_other_toolhead_is_valid(self):
        for slot in (1, 2, 3):
            self.p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=self.st.label(
                self.st.toolhead_in_slot(slot)))
        cal = self.mod.calibrator
        cal.add_ref = True
        sel, notes = cal._with_reference([0])
        self.assertEqual(sel, [0])
        self.assertIn("no other toolhead", notes[0])

    def test_t0_alone_with_ref_off_warns(self):
        cal = self.mod.calibrator
        cal.add_ref = False
        sel, notes = cal._with_reference([0])
        self.assertEqual(sel, [0])
        self.assertIn("REF=0", notes[0])

    # ---- not aligned to T0 -----------------------------------------------
    def _frames(self, **frames):
        """slot name -> frame id for the stored calibrations (T0=, T1=, ...)"""
        for key, frame in frames.items():
            c = self.st.get(self.st.toolhead_in_slot(int(key[1:])))["calibration"]
            c["frame"] = frame

    def test_fixtures_start_in_one_frame(self):
        for slot in range(4):
            self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(slot)))

    def test_a_toolhead_in_another_frame_is_not_aligned_to_t0(self):
        self._frames(T1="other")
        self.assertTrue(self.st.frame_stale(self.st.toolhead_in_slot(1)))
        self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(0)))
        self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(2)))

    def test_when_t0_is_in_another_frame_everything_else_is_flagged(self):
        self._frames(T0="other")
        self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(0)))
        for slot in (1, 2, 3):
            self.assertTrue(self.st.frame_stale(self.st.toolhead_in_slot(slot)))

    def test_measured_without_t0_gets_a_frame_of_its_own(self):
        self.session(T1=self.shifted(1, 0.04))
        self.assertTrue(self.st.frame_stale(self.st.toolhead_in_slot(1)))
        self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(2)))

    def test_measured_with_t0_shares_its_frame(self):
        self.session(T0=self.shifted(0, 0.05), T1=self.shifted(1, 0.05))
        self.assertEqual(self.st.frame_of(self.st.toolhead_in_slot(1)),
                         self.st.frame_of(self.st.toolhead_in_slot(0)))
        self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(1)))

    def test_new_t0_toolhead_in_another_frame_is_tied_by_a_bridge(self):
        self.p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=self.st.label(
            self.st.toolhead_in_slot(0)))
        self._frames(T0="lb-old-frame")
        self.session(T0=self.shifted(0, 0.05), T1=self.shifted(1, 0.05))
        self.assertEqual(self.st.frame_of(self.st.toolhead_in_slot(0)),
                         self.st.frame_of(self.st.toolhead_in_slot(1)))
        for slot in range(4):
            self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(slot)))

    def test_a_save_is_logged_with_session_frame_and_shift(self):
        self.session(T0=self.shifted(0, 0.05), T1=self.shifted(1, 0.05))
        rows = [r for r in self.mod._read_cal_log() if r["kind"] == "store"]
        self.assertEqual([r["slot"] for r in rows], [1])
        r = rows[0]
        self.assertEqual(r["how"], "relative")
        self.assertEqual(r["frame"], self.st.frame_of(self.st.toolhead_in_slot(0)))
        self.assertAlmostEqual(r["shift"][2], -0.05, places=5)
        self.assertEqual(r["ver"], vt.VERSION)
        self.assertTrue(r["session"])

    def test_invalid_toolheads_are_not_reported(self):
        self._frames(T1="other")
        self.p.gcode.run("VT_REPLACE_NOZZLE", TOOLHEAD=self.st.label(
            self.st.toolhead_in_slot(1)))
        self.assertFalse(self.st.frame_stale(self.st.toolhead_in_slot(1)))

    def test_status_summary_and_prompt_show_it(self):
        self._frames(T1="other")
        label = self.st.label(self.st.toolhead_in_slot(1))
        self.assertTrue(self.mod.get_status()["toolheads"][label]["frame_stale"])
        self.assertIn("NOT ALIGNED TO T0",
                      "\n".join(self.mod._summary_lines()))
        self.mod.calibrator._needs = lambda *a, **k: {}   # nothing else needed
        self.mod.calibrator._show_start([])
        text = "\n".join(str(l) for l in self.p.gcode.take_prompt())
        self.assertIn("Not aligned to T0: %s" % label, text)
        self.assertIn("(not aligned to T0)", text)


class WizardStagesTests(unittest.TestCase):
    """Offset / PID / flow can each be switched on or off in the wizard."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts, self.pstock = pid_printer(self.tmp)
        self.fc = FakeFlowCalibrator(self.p, self.exts)
        self.ptc = FakeTaskConfig(self.p, [35, 34, 37, 36])
        self.fp = FakeFilamentParameters(self.p)
        self.stock = FakeStockCalibration(self.p, self.exts)
        self.order = []
        g = self.p.gcode
        for i in range(4):
            g.cmds["T%d" % i] = self._pick(i)
        g.cmds["G28"] = lambda gcmd: None
        outer = self
        orig_spawn = vt.VirtualToolheads._spawn
        orig_fetch = vt.VirtualToolheads._fetch_filament_id
        vt.VirtualToolheads._spawn = sync_spawn
        vt.VirtualToolheads._fetch_filament_id = \
            lambda self_, sid: {35: 10, 34: 11, 37: 12, 36: 13}[sid]
        self.addCleanup(setattr, vt.VirtualToolheads, "_spawn", orig_spawn)
        self.addCleanup(setattr, vt.VirtualToolheads, "_fetch_filament_id",
                        orig_fetch)
        self.mod = start(self.p)
        self.p.gcode.take_prompt()
        for name in ("PID_CALIBRATE", "FLOW_CALIBRATE",
                     "EXTRUDER_OFFSET_ACTION_PRESTART"):
            g.cmds[name] = self._tap(name, g.cmds[name])

    def _pick(self, i):
        def pick(gcmd):
            self.order.append("T%d" % i)
            self.p.objects["toolhead"].ex = self.exts[i]
        return pick

    def _tap(self, name, fn):
        def tapped(gcmd):
            self.order.append(name.replace("EXTRUDER_OFFSET_ACTION_", ""))
            return fn(gcmd)
        return tapped

    def click(self, line):
        self.p.gcode.run_line(line)
        return self.p.gcode.take_prompt()

    @staticmethod
    def labels(lines):
        return [l for l, _, _ in prompt_buttons(lines)]

    def button(self, lines, label):
        return CalibrationTests.button(lines, label)

    def th(self, slot):
        return self.mod.store.toolhead_in_slot(slot)

    def tune(self, *slots):
        for slot in slots:
            self.mod.store.record_pid(self.th(slot), dict(PID_A), 210, "calibrated")

    def give_k(self, slot, k=0.041):
        e = self.exts[slot]
        key = self.mod._filament_key(slot)
        self.mod.store.record_flow(self.th(slot), key, k, e.nozzle_diameter,
                                   e.nozzle_volume_type)

    def need_offset(self, *slots):
        for slot in slots:
            th = self.th(slot)
            keep = self.mod.store.pid(th)      # a new nozzle forgets the PID
            self.click("VT_REPLACE_NOZZLE TOOLHEAD=%s"
                       % self.mod.store.label(th))
            if keep:
                self.mod.store.record_pid(th, keep["values"],
                                          keep["target"], keep["source"])

    # ---- defaults --------------------------------------------------------
    def test_everything_defaults_on_when_it_is_missing(self):
        self.mod.store.clear_pid(self.th(1))
        self.need_offset(1)
        lines = self.click("VT_CALIBRATE")
        self.assertIn("Offset calibration: on", self.labels(lines))
        self.assertIn("PID calibration: on", self.labels(lines))
        self.assertIn("Dynamic Flow calibration: on", self.labels(lines))
        self.assertIn("OFS=1 PID=1 FLOW=1", self.button(lines, "Start"))

    def test_calibrations_default_off_when_already_done(self):
        self.tune(0, 1, 2, 3)
        for slot in range(4):
            self.give_k(slot)
        lines = self.click("VT_CALIBRATE")
        self.assertEqual(self.labels(lines)[:4],
                         ["T0: A", "T1: B", "T2: C", "T3: D"])      # none needy
        self.assertIn("Offset calibration: off", self.labels(lines))
        self.assertIn("PID calibration: off", self.labels(lines))
        self.assertIn("Dynamic Flow calibration: off", self.labels(lines))
        self.assertNotIn("Start", self.labels(lines))

    def test_only_what_a_selected_toolhead_lacks_is_on(self):
        self.tune(0, 1, 2, 3)
        self.give_k(0); self.give_k(2); self.give_k(3)       # B lacks flow only
        lines = self.click("VT_CALIBRATE")
        self.assertEqual([l for l in self.labels(lines) if "needs it" in l],
                         ["T1: B (needs it)"])
        self.assertIn("OFS=0 PID=0 FLOW=1", self.button(lines, "Start"))

    def test_toggles_change_one_calibration_at_a_time(self):
        self.tune(0, 1, 2, 3)
        self.need_offset(1)
        lines = self.click("VT_CALIBRATE")
        lines = self.click(self.button(lines, "PID calibration: off"))
        self.assertIn("OFS=1 PID=1 FLOW=1", self.button(lines, "Start"))
        lines = self.click(self.button(lines, "Dynamic Flow calibration: on"))
        self.assertIn("OFS=1 PID=1 FLOW=0", self.button(lines, "Start"))
        lines = self.click(self.button(lines, "Offset calibration: on"))
        self.assertIn("OFS=0 PID=1 FLOW=0", self.button(lines, "Start"))
        self.assertIn("210 C", self.labels(lines))            # PID temps only
        lines = self.click(self.button(lines, "PID calibration: on"))
        self.assertNotIn("Start", self.labels(lines))
        self.assertIn("Turn on at least one calibration", "\n".join(lines))
        self.assertNotIn("210 C", self.labels(lines))

    def test_the_flow_toggle_is_absent_without_the_flow_calibrator(self):
        del self.p.objects["flow_calibrator"]
        lines = self.click("VT_CALIBRATE")
        self.assertFalse([l for l in self.labels(lines) if l.startswith("Dynamic Flow")])

    # ---- running -----------------------------------------------------------
    # ---- filament must be loaded for the flow stage ----
    class Sensor:
        def __init__(self, detected):
            self.detected = detected

        def get_status(self, eventtime):
            return {"filament_detected": self.detected, "enabled": True}

    class Feeder:
        def __init__(self, chans):
            self.chans = chans

        def get_status(self, eventtime=None):
            return {"extruder%d" % i: {"filament_detected": d,
                                       "channel_state": s}
                    for i, (d, s) in self.chans.items()}

    def feeder_setup(self, loaded, lane, fail_load=()):
        """loaded: slots with filament at the toolhead; lane: slots with
        filament in the feeder only."""
        self.loads = []
        for i in range(4):
            self.p.objects["filament_motion_sensor e%d_filament" % i] = \
                self.Sensor(i in loaded)
        chans = {i: (i in loaded or i in lane,
                     "load_finish" if i in loaded else "none")
                 for i in range(4)}
        self.p.objects["filament_feed left"] = self.Feeder(
            {0: chans[0], 1: chans[1]})
        self.p.objects["filament_feed right"] = self.Feeder(
            {2: chans[2], 3: chans[3]})

        def auto_feeding(g):
            n = g.get_int('EXTRUDER')
            self.order.append("LOAD%d" % n)
            if n in fail_load:
                raise GCodeError("load failed")
            self.loads.append(n)
            self.p.objects["filament_motion_sensor e%d_filament" % n] = \
                self.Sensor(True)
        self.p.gcode.cmds["AUTO_FEEDING"] = auto_feeding

    def test_flow_loads_filament_that_is_detected_but_not_loaded(self):
        self.feeder_setup(loaded={2}, lane={1})
        self.fc.result = {"extruder1": 0.041, "extruder2": 0.05}
        lines = self.click("VT_CAL PAGE=prep SEL=1,2 OFS=0 PID=0 FLOW=1")
        self.assertEqual(self.order, ["LOAD1", "T1", "FLOW_CALIBRATE", "T2",
                                      "FLOW_CALIBRATE"])
        self.assertEqual(self.loads, [1])               # T2 was loaded already
        self.assertIn("Calibration finished", "\n".join(lines))

    def test_flow_loads_a_toolhead_whose_filament_was_unloaded(self):
        # unloaded from the toolhead, still detected in the lane and by the
        # sensor: not loaded, so it is loaded first
        self.feeder_setup(loaded=set(), lane={1})
        self.p.objects["filament_feed left"] = self.Feeder(
            {0: (False, "unload_finish"), 1: (True, "unload_finish")})
        self.p.objects["filament_motion_sensor e1_filament"] = self.Sensor(True)
        self.fc.result = {"extruder1": 0.041}
        orig = self.p.gcode.cmds["AUTO_FEEDING"]

        def load(g):
            orig(g)
            self.p.objects["filament_feed left"] = self.Feeder(
                {0: (False, "unload_finish"), 1: (True, "load_finish")})
        self.p.gcode.cmds["AUTO_FEEDING"] = load
        self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertEqual(self.order, ["LOAD1", "T1", "FLOW_CALIBRATE"])

    def test_flow_refuses_a_toolhead_that_is_unloading(self):
        self.feeder_setup(loaded=set(), lane={1})
        self.p.objects["filament_feed left"] = self.Feeder(
            {0: (False, "unload_finish"), 1: (True, "unload_doing")})
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        text = "\n".join(lines)
        self.assertIn("busy loading or unloading", text)
        self.assertEqual(self.loads, [])
        self.assertNotIn("FLOW_CALIBRATE", self.order)

    def test_flow_shows_a_loading_screen(self):
        self.feeder_setup(loaded=set(), lane={1})
        self.fc.result = {"extruder1": 0.041}
        seen = []
        orig = self.p.gcode.cmds["AUTO_FEEDING"]

        def spy(g):
            seen.append("\n".join(self.p.gcode.raw))
            orig(g)
        self.p.gcode.cmds["AUTO_FEEDING"] = spy
        self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("Loading filament: toolhead T1", seen[0])

    def test_flow_with_no_filament_fails_with_retry(self):
        self.feeder_setup(loaded=set(), lane=set())
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        text = "\n".join(lines)
        self.assertIn("Dynamic Flow calibration of T1 failed", text)
        self.assertIn("has no filament", text)
        self.assertEqual(self.loads, [])
        self.assertNotIn("FLOW_CALIBRATE", self.order)
        self.assertIn("Retry", self.labels(lines))

    def test_flow_load_failure_offers_retry_skip_abort(self):
        self.feeder_setup(loaded=set(), lane={1}, fail_load={1})
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("Dynamic Flow calibration of T1 failed", "\n".join(lines))
        self.assertEqual(self.labels(lines)[-3:],
                         ["Retry", "Skip flow", "Abort calibration"])
        self.assertNotIn("FLOW_CALIBRATE", self.order)

    def test_flow_load_that_does_not_take_is_a_failure(self):
        self.feeder_setup(loaded=set(), lane={1})
        self.p.gcode.cmds["AUTO_FEEDING"] = lambda g: None   # sensor stays off
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("did not load", "\n".join(lines))

    def test_flow_without_feeder_info_just_runs(self):
        self.fc.result = {"extruder1": 0.041}
        self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertEqual(self.order, ["T1", "FLOW_CALIBRATE"])

    def test_flow_alone_picks_up_and_calibrates_each_toolhead(self):
        self.fc.result = {"extruder1": 0.041, "extruder2": 0.05}
        lines = self.click("VT_CAL PAGE=prep SEL=1,2 OFS=0 PID=0 FLOW=1")
        self.assertEqual(self.order, ["T1", "FLOW_CALIBRATE", "T2", "FLOW_CALIBRATE"])
        self.assertEqual(self.stock.calls, [])              # no offset stage
        self.assertEqual(self.mod.store.flow_entry(self.th(1), "fil:11", 0.4,
                                                   "standard")["k"], 0.041)
        text = "\n".join(lines)
        self.assertIn("Calibration finished", text)
        self.assertIn("Dynamic Flow K 0.0410", text)
        self.assertIn("Dynamic Flow K 0.0500", text)
        self.assertNotIn("build plate", text)
        self.assertFalse(self.mod.calibrator._on)

    def test_pid_alone_finishes_without_the_offset_stage(self):
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=1 FLOW=0 PT=215")
        self.assertEqual(self.order, ["PID_CALIBRATE"])
        self.assertEqual(self.stock.calls, [])
        self.assertIn("Calibration finished", "\n".join(lines))
        self.assertIn("PID 215C", "\n".join(lines))

    def test_stages_run_pid_then_flow_then_offset(self):
        self.fc.result = {"extruder1": 0.041}
        self.need_offset(1)
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=1 FLOW=1 PT=210")
        self.assertEqual(self.order, ["PID_CALIBRATE", "T1", "FLOW_CALIBRATE",
                                      "PRESTART"])
        self.assertEqual(self.stock.tools_for("HEAT"), ["T1"])
        self.assertIn("Wipe the nozzle of T1", "\n".join(lines))
        self.assertEqual(self.pstock.calls, [("extruder1", "210")])

    def test_offset_alone_still_works_as_before(self):
        self.need_offset(1)
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=0 FLOW=0")
        self.assertEqual(self.order, ["PRESTART"])
        self.assertEqual(self.pstock.calls, [])
        self.assertEqual(self.fc.calls, 0)

    def test_nothing_on_is_refused(self):
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=0")
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("VT_CALIBRATE TOOLHEADS=B OFS=0 PID=0 "
                                  "FLOW=0 CONFIRM=1")

    def test_flow_needs_a_filament(self):
        self.ptc.status["filament_spool_id"][1] = 0
        self.ptc.status["filament_type"][1] = "NONE"
        with self.assertRaises(GCodeError) as cm:
            self.p.gcode.run_line("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("T1", str(cm.exception))
        self.assertEqual(self.order, [])

    def test_console_toolheads_default_to_the_offset_stage(self):
        self.tune(0, 1, 2, 3)
        self.give_k(1)
        self.p.gcode.run_line("VT_CALIBRATE TOOLHEADS=B CONFIRM=1")
        self.assertEqual(self.order, ["PRESTART"])
        self.order.clear()
        self.mod.calibrator._tail_until = 0
        self.stock.main_state = "IDLE"
        self.p.gcode.run_line("VT_CAL PAGE=abort")
        self.p.gcode.run_line("VT_CALIBRATE TOOLHEADS=B OFS=0 FLOW=1 CONFIRM=1")
        self.assertEqual(self.order, ["T1", "FLOW_CALIBRATE"])

    # ---- failures ----------------------------------------------------------
    def test_failure_dialogs_come_back_when_dismissed(self):
        self.fc.fail["extruder1"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        again = self.click("RESPOND TYPE=command MSG=action:prompt_end")
        self.assertIn("// action:prompt_begin Dynamic Flow calibration of T1 failed",
                      again)
        self.click(self.button(lines, "Abort calibration"))
        self.mod._shown_at -= vt.STALE_DISMISS + 1
        self.assertEqual(
            self.click("RESPOND TYPE=command MSG=action:prompt_end"),
            ["// action:prompt_end"])

    def test_a_failed_flow_calibration_offers_retry_skip_and_abort(self):
        self.fc.fail["extruder1"] = 1
        self.fc.result = {"extruder1": 0.041, "extruder2": 0.05}
        lines = self.click("VT_CAL PAGE=prep SEL=1,2 OFS=0 PID=0 FLOW=1")
        self.assertIn("Dynamic Flow calibration of T1 failed", "\n".join(lines))
        self.assertEqual(self.labels(lines)[:3],
                         ["Retry", "Skip flow", "Abort calibration"])
        self.assertEqual(self.pstock.heaters.set_calls[-1][1], 0.)
        lines = self.click(self.button(lines, "Retry"))
        self.assertEqual(self.order, ["T1", "FLOW_CALIBRATE", "T1",
                                      "FLOW_CALIBRATE", "T2", "FLOW_CALIBRATE"])
        self.assertIn("Calibration finished", "\n".join(lines))

    def test_retry_resumes_at_the_failed_toolhead(self):
        self.fc.fail["extruder2"] = 1
        self.fc.result = {"extruder1": 0.041, "extruder2": 0.05}
        lines = self.click("VT_CAL PAGE=prep SEL=1,2 OFS=0 PID=0 FLOW=1")
        self.click(self.button(lines, "Retry"))
        self.assertEqual(self.order.count("FLOW_CALIBRATE"), 3)
        self.assertEqual(self.order[-2:], ["T2", "FLOW_CALIBRATE"])

    def test_skip_flow_goes_on_to_the_offset_stage(self):
        self.need_offset(1)
        self.fc.fail["extruder1"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=1 PID=0 FLOW=1")
        lines = self.click(self.button(lines, "Skip flow"))
        self.assertEqual(self.stock.tools_for("HEAT"), ["T1"])
        self.assertIn("Wipe the nozzle of T1", "\n".join(lines))

    def test_skip_pid_goes_on_to_the_flow_stage(self):
        self.pstock.fail["extruder1"] = 1
        self.fc.result = {"extruder1": 0.041}
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=1 FLOW=1")
        self.assertIn("PID calibration of T1 failed", "\n".join(lines))
        lines = self.click(self.button(lines, "Skip PID"))
        self.assertEqual(self.order, ["PID_CALIBRATE", "T1", "FLOW_CALIBRATE"])
        self.assertIn("Calibration finished", "\n".join(lines))

    def test_an_out_of_range_result_is_a_failure(self):
        self.fc._current_k["extruder1"] = 0.05        # something calibrated before
        self.fc.result = {"extruder1": 0.02}          # = the default
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("out of range", "\n".join(lines))
        self.assertEqual(self.mod.store.flow_entries(self.th(1)), {})

    def test_a_run_with_no_result_is_a_failure(self):
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("no result", "\n".join(lines))

    def test_a_value_equal_to_the_stored_one_counts_as_done(self):
        self.give_k(1, 0.041)
        self.fc._current_k["extruder1"] = 0.041
        self.fc.result = {"extruder1": 0.041}
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.assertIn("Calibration finished", "\n".join(lines))

    def test_abort_after_a_flow_failure_releases_the_guard(self):
        self.fc.fail["extruder1"] = 1
        lines = self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")
        self.click(self.button(lines, "Abort calibration"))
        self.assertFalse(self.mod.calibrator._on)
        self.assertEqual(self.pstock.heaters.set_calls[-1][1], 0.)
        self.assertEqual(self.stock.calls, [])               # nothing to exit

    def test_foreign_commands_are_rejected_during_the_flow_stage(self):
        self.fc.fail["extruder1"] = 1
        self.click("VT_CAL PAGE=prep SEL=1 OFS=0 PID=0 FLOW=1")   # dialog up
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("EXTRUDER_OFFSET_ACTION_PRESTART")


class SpoolmanLookupTests(unittest.TestCase):
    """The real lookup path against a fake Moonraker."""
    def setUp(self):
        import http.server
        import threading
        outer = self
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                payload = json.dumps(outer.reply).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass
        self.reply = {"result": {"id": 34, "filament": {"id": 11}}}
        self.status = 200
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts = make_printer(self.tmp, BP)
        self.mod = vt.VirtualToolheads(FakeConfig(
            self.p, moonraker_url="http://127.0.0.1:%d" % self.server.server_port))

    def test_asks_moonraker_for_the_spool_and_reads_the_filament_id(self):
        self.assertEqual(self.mod._fetch_filament_id(34), 11)
        path, body = self.requests[0]
        self.assertEqual(path, "/server/spoolman/proxy")
        self.assertEqual(body, {"request_method": "GET", "path": "/v1/spool/34"})

    def test_unknown_or_unreachable_gives_none_or_an_error(self):
        self.reply = {"result": {"id": 9}}
        self.assertIsNone(self.mod._fetch_filament_id(9))
        self.status = 404
        with self.assertRaises(Exception):
            self.mod._fetch_filament_id(9)

    def test_spawn_runs_off_thread_and_calls_back_on_the_reactor(self):
        import threading
        callbacks = []
        got = threading.Event()

        class Reactor:
            def register_async_callback(self_, cb):
                callbacks.append(cb)
                got.set()
        self.p.get_reactor = lambda: Reactor()
        result = []
        main = threading.current_thread()
        seen = []
        self.mod._spawn(lambda: seen.append(threading.current_thread()) or 7,
                        result.append)
        self.assertTrue(got.wait(5))
        self.assertIsNot(seen[0], main)
        self.assertEqual(result, [])            # not yet: only on the reactor
        callbacks[0](0.)
        self.assertEqual(result, [7])

    def test_spawn_survives_a_failing_lookup(self):
        import threading
        got = threading.Event()
        callbacks = []

        class Reactor:
            def register_async_callback(self_, cb):
                callbacks.append(cb)
                got.set()
        self.p.get_reactor = lambda: Reactor()
        result = []

        def boom():
            raise OSError("down")
        self.mod._spawn(boom, result.append)
        self.assertTrue(got.wait(5))
        callbacks[0](0.)
        self.assertEqual(result, [None])


###########################################################################
# Schema 7: coordinate frames
###########################################################################
class FrameMigrationTests(unittest.TestCase):
    def v6(self, spec):
        """spec: slot -> (at, anchored) for the four toolheads of slots4()"""
        s = vt.VirtualToolheadStore.from_slots(slots4())
        d = s.to_dict()
        d["schema_version"] = 6
        for slot, (at, anchored) in spec.items():
            c = d["toolheads"][d["layout"][slot]]["calibration"]
            c["at"] = at
            if anchored is not None:
                c["anchored"] = anchored
        for th in d["toolheads"].values():
            for key in ("frame", "session", "format", "mod_version"):
                th["calibration"].pop(key, None)
        return d

    def frames(self, s):
        return [s.frame_of(s.toolhead_in_slot(i)) for i in range(4)]

    def test_values_saved_together_share_a_frame_and_others_do_not(self):
        s = vt.VirtualToolheadStore.from_dict(self.v6({
            0: ("2026-08-22T07:29:51Z", None),
            1: ("2026-08-22T07:29:51Z", None),
            2: ("2026-08-11T08:39:56Z", None),
            3: ("2026-08-22T07:29:52Z", None)}))
        self.assertEqual(s.migrated_from, 6)
        f = self.frames(s)
        self.assertEqual(f[0], f[1])
        self.assertEqual(f[0], f[3])
        self.assertNotEqual(f[0], f[2])
        self.assertTrue(s.frame_stale(s.toolhead_in_slot(2)))
        self.assertFalse(s.frame_stale(s.toolhead_in_slot(1)))

    def test_a_value_stored_relative_to_t0_joins_the_frame_of_t0(self):
        s = vt.VirtualToolheadStore.from_dict(self.v6({
            0: ("2026-08-22T07:29:51Z", None),
            1: ("2026-08-22T17:22:41Z", True),
            2: ("2026-08-22T07:29:51Z", None),
            3: ("2026-08-22T07:29:51Z", None)}))
        f = self.frames(s)
        self.assertEqual(len(set(f)), 1)
        c = s.get(s.toolhead_in_slot(1))["calibration"]
        self.assertNotIn("anchored", c)
        self.assertEqual(c["format"], 2)

    def test_measured_without_t0_stays_in_its_own_frame(self):
        s = vt.VirtualToolheadStore.from_dict(self.v6({
            0: ("2026-08-22T07:29:51Z", None),
            1: ("2026-08-22T07:29:51Z", False),
            2: ("2026-08-22T07:29:51Z", None),
            3: ("2026-08-22T07:29:51Z", None)}))
        f = self.frames(s)
        self.assertNotEqual(f[1], f[0])
        self.assertEqual(f[0], f[2])

    def test_a_clock_that_jumped_never_hides_a_difference(self):
        # nothing is ever grouped across more than the session window
        s = vt.VirtualToolheadStore.from_dict(self.v6({
            0: ("2026-08-22T07:29:51Z", None),
            1: ("2026-08-22T07:40:00Z", None),
            2: ("2026-08-22T07:50:00Z", None),
            3: ("2026-08-22T08:00:00Z", None)}))
        self.assertEqual(len(set(self.frames(s))), 4)

    def test_toolheads_without_a_calibration_have_no_frame(self):
        d = self.v6({0: ("2026-08-22T07:29:51Z", None)})
        d["toolheads"][d["layout"][3]]["calibration"].update(
            base_position=None, valid=False, reason="never_calibrated")
        s = vt.VirtualToolheadStore.from_dict(d)
        self.assertIsNone(s.frame_of(s.toolhead_in_slot(3)))

    def test_a_fresh_store_starts_in_one_frame(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        self.assertEqual(len(set(self.frames(s))), 1)
        self.assertIsNotNone(self.frames(s)[0])

    def test_recording_in_a_frame_keeps_or_changes_it(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        a = s.layout[1]
        frame = s.frame_of(a)
        self.assertFalse(s.record_calibration(a, BP[1], 1))     # unchanged
        self.assertEqual(s.frame_of(a), frame)
        self.assertTrue(s.record_calibration(a, BP[1], 1, frame="new"))
        self.assertEqual(s.frame_of(a), "new")
        self.assertTrue(s.record_calibration(a, [1, 2, 3], 1))
        self.assertNotIn(s.frame_of(a), (frame, "new"))



###########################################################################
# Toolhead boards (USB serial numbers)
###########################################################################
class BoardStoreTests(unittest.TestCase):
    def test_schema_5_is_migrated_with_no_serials(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        d = s.to_dict()
        d["schema_version"] = 5
        for th in d["toolheads"].values():
            del th["serial"]
        s2 = vt.VirtualToolheadStore.from_dict(d)
        self.assertTrue(s2.migrated)
        self.assertEqual(s2.migrated_from, 5)
        self.assertTrue(all(s2.serial(i) is None for i in s2.ids()))

    def test_a_serial_belongs_to_one_toolhead(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        a, b = s.layout[0], s.layout[1]
        s.set_serial(a, "AAA")
        self.assertEqual(s.by_serial("AAA"), a)
        with self.assertRaises(vt.StoreError):
            s.set_serial(b, "AAA")
        s.set_serial(a, None)
        self.assertIsNone(s.by_serial("AAA"))

    def test_duplicate_serials_in_a_file_are_refused(self):
        s = vt.VirtualToolheadStore.from_slots(slots4())
        d = s.to_dict()
        for th in d["toolheads"].values():
            th["serial"] = "SAME"
        with self.assertRaises(vt.StoreError):
            vt.VirtualToolheadStore.from_dict(d)

    def test_read_usb_serial_walks_up_to_the_usb_device(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        dev = os.path.join(tmp, "devices", "usb1", "1-1.2")
        iface = os.path.join(dev, "1-1.2:1.0")
        os.makedirs(iface)
        with open(os.path.join(dev, "serial"), "w") as f:
            f.write("ABC123\n")
        tty = os.path.join(tmp, "class", "tty", "ttyACM0")
        os.makedirs(tty)
        os.symlink(iface, os.path.join(tty, "device"))
        node = os.path.join(tmp, "ttyACM0")
        open(node, "w").close()
        link = os.path.join(tmp, "by-path-x")
        os.symlink(node, link)
        self.assertEqual(vt.read_usb_serial(
            link, os.path.join(tmp, "class", "tty")), "ABC123")
        self.assertIsNone(vt.read_usb_serial(
            "/nonexistent", os.path.join(tmp, "class", "tty")))
        self.assertIsNone(vt.read_usb_serial(None))


class BoardFixture(unittest.TestCase):
    class Mcu:
        def __init__(self, port):
            self._serialport = port

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts = make_printer(self.tmp, BP)
        self.serials = {"/dev/port%d" % i: "BOARD00000%d" % i for i in range(4)}
        for i in range(4):
            self.p.objects["mcu e%d" % i] = self.Mcu("/dev/port%d" % i)
        orig = vt.read_usb_serial
        vt.read_usb_serial = lambda port, *a: self.serials.get(port)
        self.addCleanup(setattr, vt, "read_usb_serial", orig)
        self.mod = None

    def boot(self):
        """(Re)start: a new module instance connects, then Klipper is ready."""
        self.p.handlers.clear()      # the old instance is gone
        self.mod = start(self.p, legacy_labels=False)
        self.p.gcode.take_prompt()
        self.p.send_event("klippy:ready")
        return self.mod

    def prompt(self):
        return "\n".join(self.p.gcode.take_prompt())

    def swap_cables(self, a, b):
        pa, pb = "/dev/port%d" % a, "/dev/port%d" % b
        self.serials[pa], self.serials[pb] = self.serials[pb], self.serials[pa]

    def settled(self):
        """Boot once so the serials of the layout are recorded, then clear."""
        mod = self.boot()
        self.p.gcode.take_prompt()
        return mod


class BoardModuleTests(BoardFixture):
    def test_first_start_records_the_boards_and_asks_to_confirm(self):
        mod = self.boot()
        s = mod.store
        self.assertEqual([s.serial(i) for i in s.layout],
                         ["BOARD000000", "BOARD000001", "BOARD000002", "BOARD000003"])
        text = self.prompt()
        self.assertIn("Toolhead boards recorded", text)
        self.assertIn("T1: RA (board ...000001)", text)

    def test_undo_forgets_the_boards(self):
        self.boot()
        text = self.prompt()
        self.assertIn("VT_UI PAGE=serial_forget_apply", text)
        self.p.gcode.run_line("VT_UI PAGE=serial_forget_apply")
        self.assertTrue(all(self.mod.store.serial(i) is None
                            for i in self.mod.store.ids()))

    def lose_mcu(self, name):
        self.p.get_state_message = lambda: (
            "Lost communication with MCU '%s'" % name, "")
        self.p.send_event("klippy:shutdown")

    def test_lost_toolhead_mcu_offers_a_restart(self):
        self.settled()
        self.boot()
        self.p.gcode.take_prompt()
        self.lose_mcu("e2")
        text = self.prompt()
        self.assertIn("Lost communication with MCU 'e2'", text)
        self.assertIn("hot-swap", text)
        self.assertIn("Restart firmware|VT_RESTART", text)

    def test_lost_mcu_during_a_started_swap_adds_no_dialog(self):
        self.settled()
        self.boot()
        self.p.gcode.take_prompt()
        self.mod._mark_swap_pending()
        self.lose_mcu("e2")
        self.assertEqual(self.p.gcode.take_prompt(), [])

    def test_other_shutdowns_add_no_dialog(self):
        self.settled()
        self.boot()
        self.p.gcode.take_prompt()
        self.p.get_state_message = lambda: ("Heater extruder not heating", "")
        self.p.send_event("klippy:shutdown")
        self.assertEqual(self.p.gcode.take_prompt(), [])

    def test_a_restart_with_nothing_changed_shows_nothing(self):
        self.settled()
        self.boot()
        self.assertEqual(self.p.gcode.take_prompt(), [])

    def test_swapped_cables_update_the_layout(self):
        self.settled()
        self.swap_cables(1, 2)
        mod = self.boot()
        self.assertEqual([mod.store.label(i) for i in mod.store.layout],
                         ["LA", "RB", "RA", "RC"])
        text = self.prompt()
        # a status message in the console, not a dialog
        self.assertNotIn("prompt_begin", text)
        self.assertIn("// T1: RA -> RB", text)
        self.assertIn("// T2: RB -> RA", text)
        # the offsets follow the toolheads: slot 1 now carries RB's
        self.assertEqual(list(self.exts[1].base_position), BP[2])

    def test_no_boards_identified_changes_nothing(self):
        self.serials.clear()
        mod = self.boot()
        self.assertEqual(self.p.gcode.take_prompt(), [])
        self.assertFalse(mod.get_status()["boards"]["available"])
        self.assertTrue(all(mod.store.serial(i) is None
                            for i in mod.store.ids()))

    def test_a_left_toolhead_on_a_right_port_is_reported_not_applied(self):
        self.settled()
        self.swap_cables(0, 1)
        mod = self.boot()
        self.assertEqual([mod.store.label(i) for i in mod.store.layout],
                         ["LA", "RA", "RB", "RC"])
        self.assertIn("Check the toolhead cables", self.prompt())

    def test_an_unknown_board_asks_what_it_is(self):
        self.settled()
        self.serials["/dev/port2"] = "NEWBOARD0001"
        mod = self.boot()
        text = self.prompt()
        self.assertIn("New toolhead board in T2", text)
        self.assertIn("New toolhead|VT_UI PAGE=serial_new SLOT=2", text)
        self.assertIn("Replaces RB|VT_UI PAGE=serial_is SLOT=2 TOOLHEAD=RB",
                      text)
        # the layout was left alone
        self.assertEqual(mod.store.label(mod.store.layout[2]), "RB")

    def test_a_new_board_can_become_a_new_toolhead(self):
        self.settled()
        self.serials["/dev/port2"] = "NEWBOARD0001"
        mod = self.boot()
        self.p.gcode.take_prompt()
        self.p.gcode.run_line("VT_UI PAGE=serial_new SLOT=2")
        s = mod.store
        new = s.layout[2]
        self.assertEqual(s.serial(new), "NEWBOARD0001")
        self.assertEqual(s.side(new), "right")
        self.assertIn(s.by_serial("BOARD000002"), s.spare_ids())
        self.assertFalse(s.is_valid(new))
        self.assertEqual(s.get(new)["nozzle"]["diameter"], 0.4)
        # straight to the nozzle details of the new toolhead
        text = self.prompt()
        self.assertIn("Nozzle on toolhead", text)
        self.assertIn("Which nozzle does it have?", text)
        label = s.label(new)
        self.p.gcode.run_line(
            "VT_UI PAGE=nozzle_apply TOOLHEAD=%s MODE=edit DIAMETER=0.6 "
            "VOLUME_TYPE=standard MATERIAL=hardened" % label)
        self.assertEqual(s.get(new)["nozzle"]["diameter"], 0.6)
        self.assertEqual(s.get(new)["nozzle"]["material"], "hardened")

    def test_a_replacement_board_keeps_the_toolhead_but_needs_calibration(self):
        self.settled()
        self.serials["/dev/port2"] = "NEWBOARD0001"
        mod = self.boot()
        self.p.gcode.take_prompt()
        s = mod.store
        rb = s.resolve("RB")
        s.get(rb)["nozzle"]["diameter"] = 0.6
        self.p.gcode.run_line("VT_UI PAGE=serial_is SLOT=2 TOOLHEAD=RB")
        self.assertEqual(s.serial(rb), "NEWBOARD0001")
        self.assertEqual(s.layout[2], rb)
        self.assertFalse(s.is_valid(rb))
        self.assertEqual(s.invalid_reason(rb), "board_replaced")
        self.assertEqual(s.get(rb)["nozzle"]["diameter"], 0.6)
        self.assertIn("RB is now T2", self.prompt())

    def test_a_board_can_be_assigned_to_a_toolhead_that_has_none(self):
        self.settled()
        mod = self.mod
        spare = mod.store.add_toolhead(label="Sp", side="right")
        mod._save()
        self.serials["/dev/port3"] = "SPAREBOARD01"
        mod = self.boot()
        text = self.prompt()
        self.assertIn("This is Sp|VT_UI PAGE=serial_is SLOT=3 TOOLHEAD=Sp",
                      text)
        self.p.gcode.run_line("VT_UI PAGE=serial_is SLOT=3 TOOLHEAD=Sp")
        self.assertEqual(mod.store.layout[3], spare)
        self.assertEqual(mod.store.serial(spare), "SPAREBOARD01")

    def test_add_toolhead_moves_under_boards_when_they_are_readable(self):
        self.settled()
        self.p.gcode.run_line("VT_UI")
        self.assertNotIn("Add toolhead", self.prompt())
        self.p.gcode.run_line("VT_UI PAGE=boards")
        text = self.prompt()
        self.assertIn("Add a spare (not connected)|VT_UI PAGE=add", text)
        self.assertNotIn("Identify new board", text)

    def test_boards_offers_to_identify_an_unknown_board(self):
        self.settled()
        self.serials["/dev/port2"] = "NEWBOARD0001"
        self.boot()
        self.p.gcode.take_prompt()
        self.p.gcode.run_line("VT_UI PAGE=boards")
        self.assertIn("Identify new board|VT_UI PAGE=serial", self.prompt())

    def test_add_toolhead_stays_in_the_menu_without_readable_boards(self):
        self.serials.clear()
        self.boot()
        self.p.gcode.take_prompt()
        self.p.gcode.run_line("VT_UI")
        self.assertIn("Add toolhead", self.prompt())

    def test_status_reports_the_boards(self):
        mod = self.settled()
        st = mod.get_status()
        self.assertTrue(st["boards"]["available"])
        self.assertEqual(st["boards"]["slots"], ["BOARD000000", "BOARD000001", "BOARD000002", "BOARD000003"])
        self.assertEqual(st["toolheads"]["RA"]["serial"], "BOARD000001")

    def test_commands_list_learn_and_forget(self):
        mod = self.settled()
        out = self.p.gcode.run("VT_SERIALS").out[0]
        self.assertIn("T1: RA (board ...000001)", out)
        self.p.gcode.run("VT_SERIAL_FORGET", TOOLHEAD="RA")
        self.assertIsNone(mod.store.serial(mod.store.resolve("RA")))
        self.p.gcode.run("VT_SERIAL_LEARN")
        self.assertEqual(mod.store.serial(mod.store.resolve("RA")), "BOARD000001")
        self.p.gcode.run("VT_SERIAL_FORGET")
        self.assertTrue(all(mod.store.serial(i) is None
                            for i in mod.store.ids()))

    def test_menu_has_a_boards_page(self):
        mod = self.settled()
        self.p.gcode.run_line("VT_UI PAGE=boards")
        text = self.prompt()
        self.assertIn("Toolhead boards", text)
        self.assertIn("T2: RB (board ...000002)", text)



class SwapFlowTests(BoardFixture):
    """The Change layout screens when the boards identify themselves."""
    def setUp(self):
        super().setUp()
        self.settled()

    def test_change_layout_goes_straight_to_the_toolhead_list(self):
        self.p.gcode.run_line("VT_UI PAGE=layout")
        text = self.prompt()
        self.assertIn("Swap toolheads", text)
        self.assertIn("T1: RA|VT_UI PAGE=swap SEL=1 UNLOAD=1 LOAD=0", text)
        self.assertIn("Unload filament: on", text)        # on by default
        self.assertNotIn("Continue", text)               # nothing picked yet

    def test_selecting_toggles_and_offers_continue(self):
        self.p.gcode.run_line("VT_UI PAGE=swap SEL=1,2 UNLOAD=1")
        text = self.prompt()
        self.assertIn("T1: RA|VT_UI PAGE=swap SEL=2 UNLOAD=1 LOAD=0|primary", text)
        self.assertIn("T3: RC|VT_UI PAGE=swap SEL=1,2,3 UNLOAD=1 LOAD=0|secondary",
                      text)
        self.assertIn("Unload filament: on|VT_UI PAGE=swap SEL=1,2 UNLOAD=0 LOAD=0",
                      text)
        self.assertIn("Continue|VT_UI PAGE=swap_apply SEL=1,2 UNLOAD=1 LOAD=0", text)

    def test_the_card_button_preselects_its_slot(self):
        self.p.gcode.run_line(
            "VT_UI PAGE=layout_slot SLOT=2 PENDING=LA,RA,RB,RC DIRECT=1")
        text = self.prompt()
        self.assertIn("Swap toolheads", text)
        self.assertIn("T2: RB|VT_UI PAGE=swap SEL= UNLOAD=1 LOAD=0 DIRECT=1|primary",
                      text)
        self.assertIn("Continue|VT_UI PAGE=swap_apply SEL=2 UNLOAD=1 LOAD=0 DIRECT=1",
                      text)

    def test_continue_unloads_docks_and_offers_the_restart(self):
        order = []
        self.p.gcode.cmds["AUTO_FEEDING"] = lambda g: order.append(
            "unload%s" % g.get("EXTRUDER"))
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: order.append("dock")
        for i in (1, 2):
            self.p.objects["filament_feed %s" % ("left" if i < 2 else "right")] \
                = None
        class Feed:
            def get_status(self, t=None):
                return {"extruder%d" % i: {"filament_detected": True,
                                           "channel_state": "load_finish"}
                        for i in (1, 2)}
        self.p.objects["filament_feed left"] = Feed()
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=1,2 UNLOAD=1")
        text = self.prompt()
        self.assertEqual(order, ["unload1", "unload2", "dock"])
        self.assertIn("Restart firmware|VT_RESTART", text)
        self.assertIn("recognized automatically", text)
        self.assertIn("power off and swap toolheads", text)
        self.assertTrue(os.path.exists(self.mod._swap_marker()))

    def test_the_restart_result_is_mirrored_in_the_card_with_its_button(self):
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: None
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=2 UNLOAD=0")
        nt = self.mod.get_status()["notice"]
        self.assertTrue(nt["restart"])
        self.assertEqual(nt["title"], "Swap toolheads")
        self.assertIn("power off and swap toolheads", "\n".join(nt["lines"]))

    def set_states(self, active=None):
        for i, ex in enumerate(self.exts):
            st = "ACTIVATE" if i == active else "PARKED"
            ex.get_status = lambda t, st=st: {"state": st}

    def test_nothing_is_docked_when_no_tool_is_picked_up(self):
        order = []
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: order.append("dock")
        self.set_states(active=None)
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=2 UNLOAD=0")
        text = self.prompt()
        self.assertEqual(order, [])
        self.assertNotIn("Docking", text)
        self.assertIn("Restart firmware|VT_RESTART", text)

    def test_a_picked_up_tool_is_docked(self):
        order = []
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: order.append("dock")
        self.set_states(active=1)
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=2 UNLOAD=0")
        self.assertEqual(order, ["dock"])

    def test_unknown_state_still_docks(self):
        order = []
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: order.append("dock")
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=2 UNLOAD=0")
        self.assertEqual(order, ["dock"])

    def test_unload_off_still_docks(self):
        order = []
        self.p.gcode.cmds["AUTO_FEEDING"] = lambda g: order.append("unload")
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: order.append("dock")
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=2 UNLOAD=0")
        self.assertEqual(order, ["dock"])

    def test_empty_selection_is_refused(self):
        with self.assertRaises(GCodeError):
            self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL= UNLOAD=1")

    def test_after_the_restart_it_goes_straight_to_calibrate_if_needed(self):
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: None
        self.p.gcode.run_line("VT_UI PAGE=swap_apply SEL=1,2 UNLOAD=0")
        self.p.gcode.take_prompt()
        self.swap_cables(1, 2)
        mod = self.boot_needy([1])
        text = self.prompt()
        self.assertEqual([mod.store.label(i) for i in mod.store.layout],
                         ["LA", "RB", "RA", "RC"])
        self.assertIn("VT_CAL PAGE=prep", text)

    def swap_with_load(self, load=1, sel="2"):
        self.p.gcode.cmds["PARK_EXTRUDER"] = lambda g: None
        self.p.gcode.run_line(
            "VT_UI PAGE=swap_apply SEL=%s UNLOAD=0 LOAD=%d" % (sel, load))
        self.p.gcode.take_prompt()

    def no_calibration(self):
        """Nothing is offered for calibration, so the load is not held back."""
        orig = vt.Calibrator._auto_open_state
        vt.Calibrator._auto_open_state = lambda self: None
        self.addCleanup(setattr, vt.Calibrator, "_auto_open_state", orig)

    def feed_calls(self):
        calls = []
        self.p.gcode.cmds["AUTO_FEEDING"] = lambda g: calls.append(
            (g.get("EXTRUDER"), g.get("LOAD"), g.get("UNLOAD", None)))
        return calls

    def test_the_swap_screen_has_an_independent_load_option(self):
        self.p.gcode.run_line("VT_UI PAGE=swap SEL=1,2 UNLOAD=1")
        text = self.prompt()
        self.assertIn("Load filament after: off|VT_UI PAGE=swap SEL=1,2 "
                      "UNLOAD=1 LOAD=1", text)
        self.assertIn("Unload filament: on|VT_UI PAGE=swap SEL=1,2 UNLOAD=0 "
                      "LOAD=0", text)
        self.p.gcode.run_line("VT_UI PAGE=swap SEL=1,2 UNLOAD=0 LOAD=1")
        text = self.prompt()
        self.assertIn("Load filament after: on|VT_UI PAGE=swap SEL=1,2 "
                      "UNLOAD=0 LOAD=0", text)
        self.assertIn("Continue|VT_UI PAGE=swap_apply SEL=1,2 UNLOAD=0 "
                      "LOAD=1", text)

    def test_load_is_off_by_default_and_nothing_is_loaded(self):
        calls = self.feed_calls()
        self.swap_with_load(load=0)
        self.boot()
        self.assertEqual(calls, [])

    def test_load_after_the_restart_loads_the_swapped_slots(self):
        self.no_calibration()
        calls = self.feed_calls()
        self.swap_with_load(load=1, sel="1,2")
        self.assertIn("load", open(self.mod._swap_marker()).read())
        self.boot()
        self.assertEqual(calls, [("1", "1", None), ("2", "1", None)])
        text = self.prompt()
        self.assertIn("// T1: filament loaded", text)
        self.assertIn("// T2: filament loaded", text)
        self.assertNotIn("prompt_begin Toolhead", text)
        # one-shot: a later restart loads nothing
        self.boot()
        self.assertEqual(len(calls), 2)

    def test_slots_without_filament_or_already_loaded_are_skipped(self):
        self.no_calibration()
        calls = self.feed_calls()
        state = {1: ("wait_insert", False), 2: ("load_finish", True)}

        class Feed:
            def get_status(self, t=None):
                return {"extruder%d" % i: {"filament_detected": d,
                                           "channel_state": st}
                        for i, (st, d) in state.items()}
        self.p.objects["filament_feed left"] = Feed()
        self.swap_with_load(load=1, sel="1,2")
        self.boot()
        self.assertEqual(calls, [])
        text = self.prompt()
        self.assertIn("T1: no filament to load", text)
        self.assertIn("T2: filament already loaded", text)

    def test_a_failed_load_is_reported_and_the_next_slot_is_tried(self):
        self.no_calibration()
        calls = []

        def feeding(g):
            calls.append(g.get("EXTRUDER"))
            if g.get("EXTRUDER") == "1":
                raise GCodeError("jam")
        self.p.gcode.cmds["AUTO_FEEDING"] = feeding
        self.swap_with_load(load=1, sel="1,2")
        self.boot()
        self.assertEqual(calls, ["1", "2"])
        text = self.prompt()
        self.assertIn("T1: load failed", text)
        self.assertIn("T2: filament loaded", text)

    def test_load_waits_for_the_calibration_screen(self):
        calls = self.feed_calls()
        self.swap_with_load(load=1, sel="1,2")
        self.swap_cables(1, 2)
        mod = self.boot_needy([1])
        text = self.prompt()
        self.assertIn("VT_CAL PAGE=prep", text)
        self.assertIn("waits until the calibration", text)
        self.assertEqual(calls, [])
        lines = mod._run_pending_load()          # what finishing / cancelling does
        self.assertEqual(calls, [("1", "1", None), ("2", "1", None)])
        self.assertIn("T1: filament loaded", lines)

    def test_a_dialog_that_needs_attention_drops_the_load(self):
        calls = self.feed_calls()
        self.swap_with_load(load=1)
        self.serials["/dev/port2"] = "BRANDNEW"       # unknown board
        self.boot()
        self.assertEqual(calls, [])
        text = self.prompt()
        self.assertIn("New toolhead board", text)
        self.assertIn("Filament load skipped for T2", text)

    def test_no_load_during_a_print(self):
        calls = self.feed_calls()
        self.no_calibration()
        self.swap_with_load(load=1)
        self.p.print_stats.state = "printing"
        self.boot()
        self.assertEqual(calls, [])
        self.assertIn("a print is running", self.prompt())

    def boot_needy(self, needy):
        self.p.handlers.clear()
        FakeStockCalibration(self.p, self.exts)
        mod = start(self.p, legacy_labels=False)
        self.p.gcode.take_prompt()
        mod.calibrator._needy = lambda: needy
        self.p.send_event("klippy:ready")
        return mod

    def test_without_readable_boards_the_old_layout_screens_remain(self):
        self.serials.clear()
        self.boot()
        self.p.gcode.take_prompt()
        self.p.gcode.run_line("VT_UI PAGE=layout")
        self.assertIn("Toolhead layout", self.prompt())


###########################################################################
# Backup / restore
###########################################################################
class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.p, self.exts = make_printer(self.tmp, BP)
        self.mod = start(self.p)
        self.s = self.mod.store

    def names(self):
        return self.mod._backup_names()

    def test_backup_now_saves_a_copy_of_the_store(self):
        self.p.gcode.run_line("VT_UI PAGE=backup_apply")
        self.assertIn("Saved", "\n".join(self.p.gcode.take_prompt()))
        names = self.names()
        self.assertEqual(len(names), 1)
        with open(os.path.join(self.mod._backup_dir(), names[0])) as f:
            self.assertEqual(json.load(f)["layout"], self.s.layout)

    def test_names_are_unique_and_ordered_within_one_second(self):
        for _ in range(3):
            self.p.gcode.run("VT_BACKUP", TAG="Test 1!")
        names = self.names()
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(names, sorted(names, reverse=True))
        self.assertTrue(all(n.endswith("-test1.json") for n in names))

    def test_only_the_newest_backups_are_kept(self):
        for _ in range(vt.MAX_BACKUPS + 3):
            self.mod._make_backup()
        self.assertEqual(len(self.names()), vt.MAX_BACKUPS)

    def test_restore_brings_back_the_old_data_and_backs_up_the_current(self):
        self.p.gcode.run("VT_BACKUP")
        name = self.names()[0]
        self.s.rename(self.s.resolve("A"), "Renamed")
        self.s.replace_nozzle(self.s.resolve("B"), diameter=0.6)
        self.p.gcode.run("VT_RESTORE", NAME=name)
        s = self.mod.store
        self.assertNotIn("Renamed", [s.label(i) for i in s.ids()])
        self.assertEqual(s.get(s.layout[1])["nozzle"]["diameter"], 0.4)
        self.assertEqual(self.exts[1].nozzle_diameter, 0.4)
        self.assertTrue(any(n.endswith("-beforerestore.json")
                            for n in self.names()))
        # and it is what was saved to disk
        with open(self.mod._store_path()) as f:
            self.assertEqual(json.load(f)["layout"], s.layout)

    def test_restore_dialogs_lead_from_the_list_to_the_apply(self):
        self.p.gcode.run("VT_BACKUP")
        name = self.names()[0]
        self.p.gcode.run_line("VT_UI PAGE=backup")
        text = "\n".join(self.p.gcode.take_prompt())
        self.assertIn("VT_UI PAGE=restore_pick", text)
        self.p.gcode.run_line("VT_UI PAGE=restore_pick")
        text = "\n".join(self.p.gcode.take_prompt())
        self.assertIn("VT_UI PAGE=restore_confirm NAME=" + name, text)
        self.p.gcode.run_line("VT_UI PAGE=restore_confirm NAME=" + name)
        text = "\n".join(self.p.gcode.take_prompt())
        self.assertIn("replaces all current toolhead data", text)
        self.assertIn("VT_UI PAGE=restore_apply NAME=" + name, text)
        self.p.gcode.run_line("VT_UI PAGE=restore_apply NAME=" + name)
        self.assertIn("Restored", "\n".join(self.p.gcode.take_prompt()))

    def test_no_restore_button_without_backups(self):
        self.p.gcode.run_line("VT_UI PAGE=backup")
        self.assertNotIn("restore_pick", "\n".join(self.p.gcode.take_prompt()))

    def test_delete_removes_a_backup(self):
        self.p.gcode.run("VT_BACKUP")
        name = self.names()[0]
        self.p.gcode.run_line("VT_UI PAGE=backup_delete_apply NAME=" + name)
        self.assertEqual(self.names(), [])

    def test_bad_or_foreign_names_are_refused(self):
        for bad in ("../x.json", "vt-1.json", "virtual_toolheads.json"):
            with self.assertRaises(GCodeError):
                self.p.gcode.run("VT_RESTORE", NAME=bad)
        with self.assertRaises(GCodeError):
            self.p.gcode.run("VT_RESTORE", NAME="vt-20200101-000000.json")

    def test_a_backup_with_another_slot_count_is_refused(self):
        self.mod._make_backup()
        name = self.names()[0]
        path = os.path.join(self.mod._backup_dir(), name)
        with open(path) as f:
            d = json.load(f)
        d["layout"] = d["layout"][:3]
        with open(path, "w") as f:
            json.dump(d, f)
        with self.assertRaises(GCodeError):
            self.p.gcode.run("VT_RESTORE", NAME=name)

    def test_restore_is_refused_while_printing(self):
        self.p.gcode.run("VT_BACKUP")
        self.p.print_stats.state = "printing"
        with self.assertRaises(GCodeError):
            self.p.gcode.run("VT_RESTORE", NAME=self.names()[0])

    def test_status_counts_the_backups(self):
        self.assertEqual(self.mod.get_status()["backups"], 0)
        self.p.gcode.run("VT_BACKUP")
        self.assertEqual(self.mod.get_status()["backups"], 1)

    def test_vt_backups_lists_them(self):
        self.p.gcode.run("VT_BACKUP")
        out = self.p.gcode.run("VT_BACKUPS").out[0]
        self.assertIn(self.names()[0], out)
        self.assertIn("layout", out)



if __name__ == "__main__":
    unittest.main(verbosity=1)
