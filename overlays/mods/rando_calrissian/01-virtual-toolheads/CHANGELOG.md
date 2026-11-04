# Changelog

Release versions of the virtual toolheads mod (`VERSION` in
`virtual_toolheads.py`). The store file has its own `schema_version`, which
only changes when the file format does.

## [0.6]

- After a swap restart the calibrate screen no longer opens for a missing
  Dynamic Flow K alone (the Calibrate button in the card header shows
  instead). The result of board detection is a console status message rather
  than a dialog; the dialog stays for unknown boards and conflicts. If a
  toolhead MCU is lost outside the Swap flow ("Lost communication with MCU
  'e2'"), a dialog asks whether a toolhead or nozzle was hot-swapped and
  offers Restart firmware.

- The status (read by Klipper's subscription about four times a second) does
  less work: the backup folder is listed again only when it changed, the print
  task is read once per call, and reading the status never starts a Spoolman
  lookup.

- Card menu: the toolhead pickers (Change nozzle, Change PID) use the lanes'
  button widths and spacing so they line up with the Swap Tool buttons, with
  spare toolheads on new lines in the same pattern. The main menu is a grid of
  two fixed-width columns (Change nozzle / Change PID, Boards / Backup) with a
  full-width Calibrate below.

- After a swap restart the calibrate panel opens in the dashboard card
  instead of the Fluidd dialog. The card announces itself with `VT_CARD`; the
  module then publishes `auto_calibrate` (a request id, valid while something
  still needs calibrating, at most 10 minutes) and the card opens its panel
  once per id. Without a card the dialog opens as before.

- Card: Calibrate is also in the Menu panel; the header Calibrate button only
  shows while an installed toolhead lacks an offset, PID tuning or flow K (or
  the calibrate mode is open).
- Card: **Menu** opens a panel inside the card (Change nozzle, Change PID,
  Boards, Backup; Add toolhead and Change layout only when the boards are not
  readable). Choices are local; one `VT_UI` command is sent on Apply. `VT_UI`
  stays as the fallback for other interfaces and for older modules.
- `VT_UI ... CARD=1` shows the result as a card notice instead of a dialog.
  Status gains `options` (nozzle choices), `backup_list` and `boards.lines`.

- Swap: docking is skipped when no tool is picked up (feeder reports it not
  active); if the state is unknown it still docks.
- Card: the calibrate panel carries the same help text as the dialog (per-stage descriptions, order note, "turn on at least one" hint).
- Card: the header **Calibrate** button toggles a calibrate mode. The lane
  buttons relabel to **Calibrate** and toggle toolheads (those that need
  something are preselected); Offset / PID / Flow K toggles and PID
  temperature chips are local, and *Start* sends one `VT_CAL PAGE=prep`.
  Cancel or Start restores the Swap Tool labels. The status gains `calibrate`
  and per-toolhead `needs`.

- Card: the Swap Tool buttons are the toggles for the swap (no second set of slot buttons in the panel); Cancel un-toggles them all.

- Card: **Swap Tool** opens a Swap toolheads panel inside the card when the
  boards are readable (instant local toggles for the slots and *Unload
  filament*; only *Continue* sends `VT_UI PAGE=swap_apply`). Result dialogs with a
  Restart firmware button are mirrored in the card's notice, with the button
  (`notice.restart`).

- Swap screen note: power off and swap (or restart at your own risk), then power on or click Restart Firmware. The swap marker now stays valid for 4 hours so a power-off swap still opens Calibrate.

- With readable boards the main menu has no *Add toolhead* (it moves to
  **Boards** as *Add a spare (not connected)*; it stays in the menu without
  readable boards). *New toolhead* for an unrecognised board now opens the
  nozzle details screen straight away. **Boards** shows *Identify new board*
  when an unrecognised board is connected.

- **Swap toolheads screen.** Where the boards can be read, *Change layout*
  (and the card's Swap Tool button) skip the old "pick a slot, pick its
  toolhead" screens: pick the toolheads to take out, *Unload filament* is on by
  default, *Continue* unloads, docks, and offers *Restart firmware*; the new
  toolheads are detected after the restart, and the Calibrate screen opens
  straight away if something needs it. Without readable boards the old
  screens remain. The unload report now says why a slot was skipped
  ("no filament loaded (feeder state: ...)").

- **Backup and restore.** Menu **Backup**: *Back up now*, and *Restore...*
  (pick a backup, see its layout, *Restore* or *Delete this backup*). Backups are
  full copies of the store kept in `virtual_toolheads.json.backups/`
  (`vt-YYYYMMDD-HHMMSS[-tag].json`, newest 20 kept). A restore first backs up
  the current data (`-beforerestore`), then follows the connected boards and
  applies everything live. Refused while printing and for a backup with a
  different slot count. Console: `VT_BACKUP [TAG=]`, `VT_BACKUPS`,
  `VT_RESTORE NAME=`, `VT_BACKUP_DELETE NAME=`.

- **Toolhead boards (schema 6).** Every toolhead board has its own USB serial
  number (read from sysfs, no communication with the board needed) that stays
  with the board when its cable moves. The module records it per toolhead and
  on every start follows the toolheads that are really connected:
  - first use: the boards of the toolheads in the current layout are recorded,
    with one confirmation dialog (Undo forgets them);
  - cables swapped: when every board is known the layout is updated to match
    the hardware, with a dialog listing the changes (and *Calibrate now* if
    something needs it); a Left Slot toolhead on a Right Slot port, or a board
    appearing twice, is reported and nothing is changed;
  - an unrecognised board asks: *New toolhead* (nozzle copied from the toolhead
    it displaces, which becomes a spare), or *This is X* / *Replaces X* for a
    toolhead whose own board is not connected (a replacement keeps nozzle, PID
    and flow K but needs its offset calibrated again);
  - printers that cannot read the serials (or the toolhead simulator bypass)
    are left as before.
  Menu: **Boards**. Console: `VT_SERIALS`, `VT_SERIAL_LEARN`,
  `VT_SERIAL_FORGET [TOOLHEAD=]`. `VT_STATUS` shows `serial` per toolhead and a
  `boards` block.

## [0.5]

- Card: new toolhead icon (tall body, round motor drum, heatsink fins, nozzle tip).

- Card: the blue *Active* tag is a button that opens a "Dock the active tool?" prompt (`VT_UI PAGE=dock`). The "Controls are disabled while printing" message uses the notice style of the status mirror.

- Schema 5: several PID tunings per toolhead, keyed by temperature (like flow K
  by filament). Calibrated runs add an entry, same temperature replaces.
  `VT_PID`, `VT_PID_SELECT`, `pid_pick` dialog, main-menu **Change PID** (toolhead list, then the dialog; lists preset and stored temperatures, new ones start a PID-only calibration immediately (no extra screen); picking a stored one closes the dialog). The card has one button per lane, *Swap Tool*; Change Nozzle and Change PID are menu-only.
  Nozzle changes keep the tunings but mark them stale (grey on the card) until
  recalibrated.

- Unload / flow: "loaded" now means the feeder's channel state is `load_finish`
  (loaded in the toolhead), not that a sensor sees filament. Toolheads whose
  filament is already unloaded are not unloaded again; toolheads that are
  unloading or loading are skipped (unload) or refused with Retry (flow stage).

- Fix: closing a progress screen (for example during the filament unload) no
  longer swallows the dialog that follows it. The close request is queued
  behind the running step and used to arrive just after the next dialog was
  shown; a close within 1.5 s of a dialog is now treated as stale.
- The *Restart firmware* button is on its own row above the other buttons and
  closes the dialog while the firmware restarts (`VT_RESTART`, registered for
  the shutdown state, then `FIRMWARE_RESTART`).
- Card: lanes can be as narrow as 132 px (four fit one row from about 550 px);
  the buttons are stacked and labelled *Change Tool* / *Change Nozzle*.

- Dialogs during a calibration: screens that wait for the user (wipe the nozzle,
  remove the build plate, failure dialogs) are shown again if Fluidd dismisses
  them (the module wraps `RESPOND` to see `action:prompt_end`); progress screens
  are published as `notice` in the module status and shown by the dashboard card
  below the toolheads until the next notice or dialog.

- Flow calibration stage: filament that is detected in the feeder but not
  loaded is loaded first (`AUTO_FEEDING LOAD=1`); a lane with no filament
  fails the stage with Retry / Skip flow / Abort.

- Change layout: an *Unload filament* option; on Apply it unloads the loaded
  spools of the swapped slots (`VT_SET_LAYOUT UNLOAD=1`), with a status screen
  while unloading and the toolhead docked (`PARK_EXTRUDER`) as the last step. The result screen has a *Restart firmware* button
  (`FIRMWARE_RESTART`) and a note: swap the toolheads' USB connections there,
  restart the firmware; no power cycle. After that restart the Calibrate screen
  opens by itself if anything in the new layout is uncalibrated.

- Calibration wizard: separate on/off toggles for offset, PID and flow
  calibration, so any combination can run for the selected toolheads. Each is
  on by default when a selected toolhead lacks it (offset invalid, no PID
  tuning, no K for the filament it holds). Stages run PID, flow, offset. The
  flow stage picks up each toolhead and runs `FLOW_CALIBRATE`; with offset off
  the wizard never touches the build plate. Console: `VT_CALIBRATE OFS= PID=
  FLOW=`; naming `TOOLHEADS=` turns the offset stage on unless `OFS=0`. The
  preselection now includes toolheads that only lack PID or flow.
- Card: a "Flow K" line per lane (green with the value, dimmed when the
  filament has none).

- Dynamic flow calibration (stock `flow_calibrator`, the K / pressure advance
  value) is stored per toolhead and per filament (schema 4; a schema 3 or 2
  store is upgraded, the original kept as `virtual_toolheads.json.v3` / `.v2`).
  The filament is identified by its Spoolman filament id (looked up once per
  spool through Moonraker's Spoolman proxy and remembered), by the spool id
  until that is known, or by vendor / type / subtype for a lane without a
  spool. A stored value is only used when the nozzle diameter and flow type
  match the ones it was measured with.
- Every stock `FLOW_CALIBRATE` result is recorded for the toolhead in the
  active slot. When a toolhead gets a filament it has a value for (layout
  change, new spool assigned, print start) the value is put back into stock's
  table before stock applies it. Values already in `flow_calibrator.json` are
  adopted once for the filament currently loaded, unless they equal the
  filament's default.
- `VT_FLOW [TOOLHEAD=]` lists stored values, `VT_FLOW_FORGET TOOLHEAD= [KEY=]`
  removes them; `printer.virtual_toolheads` shows the count, current filament
  key and K per toolhead.

## [0.4]

- Card: a lane is "Active" only while its extruder reports `state: ACTIVATE`
  (the extruders report `PARKED` on their dock, including all four on a fresh
  boot). Klipper's `toolhead.extruder` keeps naming a parked extruder, so it is
  used only on firmware that has no extruder state, together with the
  `// park extruderN` console line. The PID line is green when tuned and dimmed
  when not.
- PID tuning is stored per virtual toolhead (schema 3), like the nozzle and
  the base position, and restored into the slot's heater when the layout
  changes. It uses the stock heater code: the tuning lives in stock's
  `extruder[x]_pid_parameters.json` (`default` profile) and is applied with
  `set_pid_profile`. On the first start each loaded toolhead adopts the tuning
  its slot already uses. Toolheads without a stored tuning leave the heater
  unchanged.
- Store schema 3. A schema 2 store (0.2/0.3) is upgraded on first start; the
  original is kept once as `virtual_toolheads.json.v2`. Earlier versions were
  never released, so there is no upgrade from schema 1.
- Every stock `PID_CALIBRATE` (console, touchscreen or the wizard) is recorded
  for the toolhead in that heater's slot.
- The calibration wizard can run PID calibration first, on cold nozzles, for
  just the selected toolheads. Temperature setting (default 210 C, the
  standard PLA temperature) in the wizard and with `VT_SET_PID_TEMP`.

## [0.3]

- Dialogs opened from a per-slot dashboard button (`DIRECT=1`) now close on
  Back/Cancel instead of walking back through the layout/menu pages. Opened
  from the menu, they still return to it.
- Fluidd dashboard card (separate overlay): header now mirrors the other cards
  (icon, light-weight title font), arrow collapse button, "Active"
  tag on the loaded extruder. Slot-side tags removed from the lanes; the
  toolhead name is a button that renames it in place (`VT_RENAME`). Card
  title is now "Toolheads"; materials read "Stainless steel" / "Hardened steel". In Fluidd's dashboard layout editor the card now
  shows the standard collapsed header with a visibility checkbox (it cannot be
  dragged; it stays after its neighbour card), and is re-placed correctly after
  Fluidd re-renders the dashboard.

## [0.2]

- Left Slot / Right Slot: every virtual toolhead records which kind of slot it
  fits (slot 0 is different hardware from slots 1-3). Layouts that put a Left
  Slot toolhead in slots 1-3, or a Right Slot toolhead in slot 0, are refused,
  and the layout dialog only offers toolheads that fit the slot.
- Adding a toolhead asks for its side (console `VT_ADD SIDE=left|right`,
  default right).
- New default labels: side prefix plus a letter counted per side (`LA`, `LB`,
  ... for left, `RA`, `RB`, ... for right). Names the user chose are kept.
- `VT_SET_SIDE` fixes the side of a spare toolhead.
- Store schema 2 (sides and labels).
- Nozzle material is `stainless` or `hardened` (was `standard` or `hardened`;
  the flow type keeps the stock names `standard` / `high_flow`). Files still
  saying `standard` as a material are not loaded and must be edited by hand;
  there is no automatic rename.
- Dialog and message wording: the slot lines no longer repeat the side (the
  `L`/`R` label prefix and the slot heading carry it), the main dialog buttons
  are *Change layout*, *Change nozzle*, *Add toolhead*, *Calibrate*, and
  `VT_ADD` / `VT_SET_SIDE` confirmations state the side explicitly.
- The release version is shown in `VT_STATUS`, the main dialog, the
  `printer.virtual_toolheads` status object and the log.

## [0.1]

- Per-toolhead calibration and nozzle data, layouts, spare toolheads.
- Fluidd / Mainsail dialogs for layout, nozzles and adding toolheads.
- Selective calibration wizard with the manual wipe step for selected
  toolheads only, and a guard against the stock touchscreen continuing its own
  calibration while the wizard runs.
- Print-start check, `VT_TRACE` debug trace.
