# Virtual toolheads

Keeps each physical toolhead's calibration (`base_position`) and nozzle data
with the toolhead instead of with the slot it is plugged into. The four slots
(`extruder` .. `extruder3`) only point at whichever virtual toolhead is loaded
in them, so toolheads can be swapped between slots, or kept on the shelf, and
only the toolheads that actually need it are recalibrated.

Version 0.6 (history in `CHANGELOG.md`). User documentation: [`docs/virtual-toolheads.md`](../docs/virtual-toolheads.md).

## Contents

| Path | Purpose |
|---|---|
| `root/home/lava/klipper/klippy/extras/virtual_toolheads.py` | The Klipper extra: store, layout, nozzle tracking, dialogs, calibration wizard, guard, trace |
| `root/usr/local/share/firmware-config/tweaks/klipper/virtual_toolheads.cfg` | `[virtual_toolheads]`, the print-start hook and four menu macros; symlinked into `extended/klipper/` when enabled |
| `root/usr/local/share/firmware-config/functions/28_settings_toolheads_virtual_toolheads.yaml` | The on/off toggle in Firmware Config |
| `CHANGELOG.md` | Release history; `VERSION` in the module is the release version |
| `test/test_virtual_toolheads.py` | Unit tests (`python3 test/test_virtual_toolheads.py`, standard library only) |

Opt-in: disabled by default. While disabled the module is never loaded and the
firmware behaves like stock. Disabling it again leaves the stock files
(`extruder_base_position.json`, `extruder[x]_nozzle_config.json`) in stock
format, reflecting the toolheads currently loaded.

## Design

* **Observe, don't intercept.** No stock file is patched.
  * Calibration results are picked up from the stock
    `probe_inductance_coil: update_extruder_offset` event (sent after every
    `SAVE_RESULT` and `SET_EXTRUDER_BASE_POSITION`): each slot's
    `base_position` is compared with the store and a new value is recorded
    against the toolhead in that slot.
  * Nozzle changes made through the stock UI are mirrored into the toolhead in
    that slot by wrapping `PrinterExtruder._set_nozzle_diameter` and
    `_set_nozzle_volume_type` once, at class level (a re-entrancy flag stops
    our own changes from looping, and the wrapper looks up the live instance
    so a Klipper `RESTART` never calls into a stale one).
* **The store is the source of truth** (`persistent/virtual_toolheads.json`,
  atomic write, one `.bak` generation). At startup it is applied to the slots
  and the stock-format files are regenerated. If the store is missing it is
  created from the four current slots (toolheads A-D); if it is unreadable the
  feature disables itself, leaves the file untouched and Klipper starts
  normally.
* **Left Slot / Right Slot.** Slot 0 is different hardware from slots 1-3, so
  every toolhead records a `side` (`left` fits slot 0 only, `right` fits slots
  1-3). The store refuses to load, and `VT_SET_LAYOUT` refuses to apply, a
  layout that breaks this; the layout dialog only offers toolheads that fit the
  slot. Default labels are the side prefix plus a letter counted per side
  (`LA`, `LB`, `RA`, `RB`, ...).
* **Versions.** `VERSION` ("0.6") is the release of the mod as a whole and is
  stamped into the store as `mod_version`; `SCHEMA_VERSION` (6) is the file
  format. A schema 2 to 5 file is upgraded on first start: the original is kept
  once as `virtual_toolheads.json.v<old>`.
* **A toolhead that needs calibration is loaded with `base_position = None`**,
  the same state as a never-calibrated stock slot. A print-start hook
  (`_PRINT_START_VT_CHECK`, via the klipper-hooks overlay) warns or blocks
  according to `print_guard`.
* **Replacing a nozzle always invalidates; correcting nozzle data invalidates
  only if a value changes.** A machine-level `epoch` can invalidate everything
  at once (`VT_INVALIDATE TOOLHEAD=ALL`).
* **Selective calibration** (`VT_CALIBRATE`) runs the stock
  `EXTRUDER_OFFSET_ACTION_*` commands, in the order of the reference block in
  the stock `_EXTRUDER_OFFSET_ACTION_PRESTART` macro, for the selected
  toolheads only, with dialogs for the manual steps (wipe the nozzle, remove
  the plate). While it runs, stock calibration commands from *other* clients
  are rejected: the stock touchscreen app follows the calibration state and
  continues its own flow, and its queued commands would otherwise swap
  toolheads mid-wizard. The guard stays up 20 s after the wizard ends and can
  be disabled with `guard_calibration: False`.

## Stock internals this depends on

If the firmware changes any of these, the affected part logs the problem and
turns itself off instead of failing Klipper's startup (`VT_STATUS` reports it).
Written against the stock 2.0.0 sources (`kinematics/extruder.py`,
`extras/probe_inductance_coil.py`, `extras/extruder_config_bak.py`).

| Internal | Used for |
|---|---|
| printer object `extruder_list` | the four slots |
| `PrinterExtruder`: `name`, `base_position`, `gcode_offset`, `nozzle_diameter`, `nozzle_volume_type`, `nozzle_config_info`, `nozzle_config_path`, `update_extruder_gcode_offset()`, `active_gcode_offset()`, `_set_nozzle_diameter(d, save)`, `_set_nozzle_volume_type(v, save)` | applying the store to slots; the class-level wrappers |
| event `probe_inductance_coil: update_extruder_offset` | recording calibration results |
| `extruder_offset_calibration`: `last_xyz_result`, `calibration_step`; the commands `EXTRUDER_OFFSET_ACTION_{PRESTART,PREHEAT,HEAT,AUTO_CLEAN,MANUAL_CLEAN,WAIT_COOL,DETECT_PLATE,PROBE_CALIBRATE,SAVE_RESULT,EXIT}` and their parameters (`TOOL_ID`, `TEMP`, `PRESENCE`, `FORCE_SAVE`, `Tn_TEMP`) | the calibration wizard and its guard |
| `machine_state_manager.get_status()['main_state']` (`XYZ_OFFSET_CALIBRATE`) | detecting a running calibration |
| `printer.get_snapmaker_config_dir()`, `update_snapmaker_config_file()`, `get_start_args()['software_version']` | file locations and the nozzle config writer |
| `extruder_config_bak.base_position_config_path` (optional) | where the stock-format view is written |
| `print_stats.state` | refusing changes while printing |
| Klipper core: `gcode.register_command(name, None)` returning the old handler; `gcode.respond_raw`; `gcode._process_commands` (only `VT_TRACE`), the stock `RESPOND` command (wrapped to see a dismissed dialog) | wrapping stock commands; dialogs; the debug trace |

## Known limitations

* **Central assumption, not yet verified across machines:** a toolhead's
  `base_position` is a property of the toolhead (and its nozzle), not of the
  slot. Each result records the slot it was measured in (`calibration.slot` and
  `calibration.at` in the store; `calibrated_slot` / `calibrated_at` in the
  `printer.virtual_toolheads` status) so the assumption can be checked by
  calibrating one toolhead in two slots and comparing.
* The side of a spare toolhead is whatever the user says.
* PID tuning is per toolhead: the `default` profile of the slot's stock
  `extruder[x]_pid_parameters.json` is rewritten and `set_pid_profile` called
  when the layout changes (values are kept in config units and divided by
  `PID_PARAM_BASE` like stock does on load). `PID_CALIBRATE` is wrapped to
  record the result. Pressure advance etc. stay slot-bound in `printer.cfg`.
* The wizard is a sequence of up to three stages, PID, flow, offset, selected
  by a `Plan(ofs, pid, flow, pt)` that every dialog button carries (`OFS=`,
  `PID=`, `FLOW=`, `PT=`); `_next_stage` runs the next enabled one. The flow
  stage runs `T<n> A0` and `FLOW_CALIBRATE` and reads the outcome the
  `FLOW_CALIBRATE` hook stored for the slot. The guard is held (`_hold`) from
  the first PID/flow stage until the stock offset state machine starts or the
  wizard concludes.
* Dynamic flow: stock `flow_calibrator` keeps `_current_k[extruder name]` and
  saves it with `_save_config()` (`snapmaker/flow_calibrator.json`,
  `factor`). `FLOW_CALIBRATE` always works on the active extruder. We wrap
  `FLOW_CALIBRATE` (record the changed K), `FLOW_APPLY_CALIBRATE_K` (the print
  start macro calls it for every extruder; we put the toolhead's K into
  `_current_k` first) and `SET_PRINT_FILAMENT_CONFIG` (re-evaluate when a spool
  is assigned). The lane's filament comes from `print_task_config`
  (`filament_spool_id`, added by the spool-id patch, plus vendor / type /
  subtype); the Spoolman filament id is fetched in a thread from Moonraker's
  `/server/spoolman/proxy`. Relies on the private `_current_k`, `_save_config`
  and `_apply_calibrate_k` of the stock module.
* Dialogs use the standard `action:prompt` protocol and have no text input;
  rename a toolhead with `VT_RENAME`.
* Verified with Fluidd. Other UIs that implement the prompt protocol should
  work but have not been tested.
* The guard only covers `EXTRUDER_OFFSET_ACTION_*` commands.

## Tests

`python3 test/test_virtual_toolheads.py` runs 377 tests against fakes of the
printer, extruders, G-code dispatcher and the stock calibration object, which
are modelled on the stock 2.0.0 sources. They cover the store, layout and
nozzle rules, migration, PID tuning, event observation, the stock-UI hook, every dialog
(including a click-through of every reachable button), sides and labels, the
schema upgrade, the calibration wizard (including its PID phase)
(failures, retries, silent probe failures, abort), the guard, and the trace.
They do not replace testing on a printer.
