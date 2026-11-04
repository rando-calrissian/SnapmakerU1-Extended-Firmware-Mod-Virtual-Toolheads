# Add virtual toolheads (calibration follows the toolhead, not the slot)

Version 0.6 of the mod (see `CHANGELOG.md`).

## Summary

Adds an opt-in feature that stores each toolhead's calibration (`base_position`)
and nozzle data with the *toolhead* instead of with the slot it is plugged
into. With spare toolheads you can swap them between the four slots and only
recalibrate the one that needs it, instead of recalibrating all four.

* Named virtual toolheads, each with the kind of slot it fits (Left Slot =
  slot 0, Right Slot = slots 1-3: different hardware, so a toolhead only fits
  its own kind), nozzle diameter, flow type (standard / high flow) and
  material (stainless / hardened). Default names are the side plus a letter per
  side: `LA`, `LB`, ... / `RA`, `RB`, ...
* A layout (which toolhead is in which slot) that is applied to the stock
  files, so the rest of the firmware is unaffected. Layouts that put a toolhead
  in the wrong kind of slot are refused.
* Replacing a nozzle, or changing diameter / flow type / material, marks that
  toolhead as needing calibration.
* Fluidd / Mainsail dialogs for layout, nozzle changes, adding toolheads and
  calibration (standard `action:prompt` protocol).
* Selective calibration: the stock `EXTRUDER_OFFSET_ACTION_*` commands run for
  the selected toolheads only, including the manual nozzle wipe for those
  toolheads only.
* PID tuning stored per toolhead and restored into the slot's heater (and its
  stock `extruder[x]_pid_parameters.json`) when the layout changes. The
  calibration wizard can run `PID_CALIBRATE` first, from a cold nozzle, for the
  selected toolheads only (default 210 C, adjustable).
* Dynamic flow calibration (K) stored per toolhead and filament (Spoolman
  filament id), restored when the filament or layout changes.
* Toolhead boards are recognised by their USB serial number: swapping toolheads
  and USB cables is detected on restart (layout follows the hardware; unknown
  boards ask whether they are new or replace a toolhead).
* With board detection, *Change layout* becomes a one-screen "Swap toolheads"
  (unload on by default, dock, restart); the new toolheads are detected.
* The card's Swap Tool opens an in-card panel (instant toggles) where boards are readable.
* Backup and restore of all toolhead data from the menu (and `VT_BACKUP`,
  `VT_RESTORE`); a restore backs up the current data first.
* The calibration wizard has separate toggles for offset, PID and flow
  calibration (each on by default when a selected toolhead lacks it); stages
  run PID, flow, offset.

Disabled by default; behind a toggle in Firmware Config.

## Where it lives

`overlays/firmware-extended/42-feature-virtual-toolheads/` (number and name are
suggestions) plus `docs/virtual-toolheads.md`. It can equally be carried as a
personal mod (`overlays/mods/<username>/01-virtual-toolheads/`); the overlay is
self-contained, so only the path changes (and the `help_url` in the YAML points
at the docs page).

## No stock files are patched

* Calibration results are picked up from the stock
  `probe_inductance_coil: update_extruder_offset` event.
* Stock-UI nozzle changes are mirrored by wrapping two `PrinterExtruder`
  methods at runtime.
* The stock calibration commands are wrapped (same mechanism as
  `rename_existing`) so that, while the wizard runs, calibration commands from
  other clients are rejected. The stock touchscreen app follows the calibration
  state and sends its own `HEAT TOOL_ID=T0` etc., which would otherwise swap
  toolheads in the middle of the wizard.

The full list of stock internals relied on is in the overlay README. If one is
missing, the affected part logs and turns itself off; a store that cannot be
read disables the feature and leaves the file alone.

## Testing

**Automated:** `python3 test/test_virtual_toolheads.py` (377 tests, standard
library only). Fakes of the printer, extruders, G-code dispatcher and stock
calibration object are modelled on the 2.0.0 sources. This covers the store and
its rules, schema migration, PID tuning, event observation, the stock-UI hook, every dialog
(including a click-through of every reachable button and every reachable Apply),
the calibration wizard (failures, retries, silent probe failure, abort, PID phase), the
guard, and the trace.

**On hardware** (author to complete; fake-based tests do not replace this):

- [ ] Enable via Firmware Config: Klipper restarts, `VT_STATUS` lists A-D with
      the existing calibration and `Calibration guard: active (12 stock commands)`
- [ ] Layout dialog: swap a spare toolhead in; slot nozzle size/type and offsets
      update; a spare that was never calibrated shows as needing calibration
- [ ] Layout dialog only offers toolheads that fit the slot (Left in slot 0,
      Right in slots 1-3); a Left Slot spare added with *Add toolhead* can be
      loaded into slot 0 only
- [ ] Nozzle dialog: *New nozzle fitted* invalidates; *Correct details only*
      invalidates only when a value changes
- [ ] Selective calibration of one toolhead end to end (wipe step, plate removal,
      result recorded, other toolheads untouched)
- [ ] PID: `PID_CALIBRATE` run through the wizard on an unpicked, unhomed
      toolhead completes; the result appears in `VT_STATUS` and in the slot's
      `extruder[x]_pid_parameters.json`; recorded values match what the stock
      UI shows: ______
- [ ] PID: run PID at two temperatures, menu > **Change PID** switches
      between them (heater values change live); after a nozzle change the PID
      line is grey until recalibrated
- [ ] Boards: first start records the four serials (confirmation dialog);
      swap two USB cables, restart: the layout follows, dialog lists it;
      a replacement board offers New / This is / Replaces
- [ ] Backup: *Back up now*, change something, *Restore...* it; the card and
      the slots show the restored values without a restart
- [ ] PID: swap two toolheads, check each slot's heater uses the swapped
      tuning (file and live) and the console is not flooded by `set_pid_profile`
- [ ] PID: `TEMPERATURE_WAIT SENSOR=extruderN` cool-down works (hot nozzle
      before the wizard); two heaters at once is not attempted
- [ ] Flow: `FLOW_CALIBRATE` on a toolhead is recorded (`VT_FLOW`); changing
      the spool of that lane to another filament and back restores the first K
      (check `snapmaker/flow_calibrator.json` and `printer.extruderN.pressure_advance`)
- [ ] Wizard, flow stage: `T<n> A0` picks the toolhead up from a parked,
      possibly unhomed state and `FLOW_CALIBRATE` runs to the end for each
      selected toolhead; with only PID / flow selected the build plate can stay
      on; then PID + flow + offset together in one run
- [ ] Change layout with *Unload filament* on: spools on the swapped slots
      unload one after another with a status screen; the toolhead docks at the end
      (`PARK_EXTRUDER`); empty slots are skipped; Cancel unloads nothing
- [ ] Click outside / press Esc on the wipe-nozzle and remove-plate dialogs: they
      come straight back; during heating/flow/unload the card shows the status text
- [ ] After an unload, swap a toolhead's USB cable while the result screen is up;
      *Restart firmware* brings the new toolhead up without a power cycle, and the
      Calibrate screen opens by itself when something needs calibrating
- [ ] Flow stage on a lane with filament in the feeder but not loaded: it loads
      first (status screen), then calibrates; an empty lane gives the Retry dialog
- [ ] Flow: the Spoolman filament id lookup works (`VT_FLOW` shows `fil:<id>`,
      not `spool:<id>`); with Spoolman unreachable it falls back quietly
- [ ] Stock touchscreen during the wizard: warning shown, no tool swap; works
      normally about 20 s after the wizard ends
- [ ] Disable the toggle: stock behaviour with the current layout
- [ ] **The central assumption:** one toolhead calibrated in two different slots;
      difference between the two `base_position` values: ______ mm
- [ ] Other UIs (Mainsail / HelixScreen), if tested: ______

## Known limitations

* The firmware cannot detect which kind of slot a toolhead fits; the side is
  set when the toolhead is added (`VT_ADD SIDE=`, default right) or with
  `VT_SET_SIDE`.
* Assumes calibration is a property of the toolhead, not the slot (see the
  checklist item above); the slot of each result is recorded so it can be
  checked.
* PID tuning is per toolhead, but pressure advance etc. stay slot-bound.
* The guard covers `EXTRUDER_OFFSET_ACTION_*` commands only.
* Dialogs have no text input; rename with `VT_RENAME`.

## Questions for maintainers

* Promote to `firmware-extended`, or keep as a mod for now?
* New Firmware Config settings group `toolheads`: fine, or should it live in an
  existing group?
* Is `docs/virtual-toolheads.md` the right place, and does it need linking from
  an index/nav?
* Overlay number (`42`) is a placeholder.

## Checklist

- [ ] `YOUR-GITHUB-USERNAME` replaced in the SPDX headers
      (`grep -rn YOUR-GITHUB-USERNAME .`)
- [ ] Files are mode 644 (git only records 644 vs 755; a local build copies
      modes from the working tree, so run `chmod -R u=rwX,go=rX` on it first)
- [ ] Built with the overlay and flashed once
