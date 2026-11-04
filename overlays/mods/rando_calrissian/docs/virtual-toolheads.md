# Virtual Toolheads

Version 0.6 (history in `overlays/mods/rando_calrissian/01-virtual-toolheads/CHANGELOG.md`).

Keeps each toolhead's calibration and nozzle data with the toolhead itself, not
with the slot it sits in. With a few spare toolheads you can swap them in and
out of the four slots and recalibrate only the toolhead that needs it, instead
of recalibrating the whole machine.

> **Warning**: Experimental. This changes where calibration data is stored and
> adds a calibration wizard that moves toolheads and heats nozzles. Read the
> risks below and watch the first runs.

## What it does

- Gives every toolhead a name and stores which kind of slot it fits (Left Slot
  or Right Slot, see below), its nozzle diameter, flow type (standard or high
  flow) and material (stainless or hardened). Default names are the side
  followed by a letter counted per side: `LA`, `LB`, ... for Left Slot
  toolheads and `RA`, `RB`, ... for Right Slot toolheads. Rename with
  `VT_RENAME` or by clicking on the name of the toolhead in the card in Fluidd.
- Stores each toolhead's calibration with the toolhead. The four slots point at
  whichever toolhead is loaded in them.
- Keeps the stock calibration and nozzle files up to date, so the rest of the
  firmware keeps working as before.
- Marks a toolhead as needing calibration when its nozzle is replaced, or when
  its nozzle diameter, flow type or material is changed.
- Lets you calibrate only the toolheads that need it, including the manual
  nozzle-wiping step for those toolheads only.

On first start the four toolheads you have become `LA` (slot 0) and `RA`, `RB`,
`RC` (slots 1-3), with their existing calibration.

## Enabling

1. Open the Firmware Configuration web interface.
2. Enable **Virtual Toolheads** and confirm.
3. Klipper restarts. Run `VT_STATUS` in the console.

Disabling it again restarts Klipper and returns to stock behaviour with the
toolheads that are currently loaded.

## Using it

Four buttons appear in the Macros panel of Fluidd and Mainsail:

| Macro | What it opens |
|---|---|
| `VT_MENU` | Overview of the four slots and spares, with links to everything below, and *Add toolhead* |
| `VT_LAYOUT_MENU` | Pick a slot, then the toolhead that is physically in it. A toolhead already in another slot swaps places. Nothing changes until you press *Apply*. |
| `VT_NOZZLE_MENU` | Pick a toolhead, then *New nozzle fitted* (always needs recalibration) or *Correct details only* (needs it only if something changes), choose diameter, flow type and material, *Apply* |
| `VT_CALIBRATE_MENU` | Calibrate the loaded toolheads that need it |

**Swapping toolheads.** On a printer whose toolhead boards can be identified
(see *Toolhead boards*), nothing has to be declared: *Change layout* opens
**Swap toolheads** straight away. Pick the toolheads you are going to take out
(the ones you put in are recognised after the restart, so they are not asked
about), keep *Unload filament* on (the default) and click *Continue*. The
filament of the picked slots is unloaded, the toolhead that is picked up is
docked, and a *Restart firmware* button waits. The note says to power off and swap
the toolheads (swapping without powering down is possible, at your own risk);
then power on or click *Restart firmware*. After the restart the layout follows the hardware and the
*Calibrate* screen opens by itself if anything needs it (otherwise a dialog
lists what was detected). With *Unload filament* off it only docks. In the Fluidd card, a slot's
**Swap Tool** button is a toggle: it marks that toolhead for taking out and
opens a small panel inside the card (below the lanes) with *Unload filament*,
*Continue* and *Cancel*. Press Swap Tool on other lanes to add them (or again
to remove one; removing the last closes the panel). *Cancel* clears every
toggle. It all changes instantly in the browser; only *Continue* sends a command. When the swap has run, the card
shows the result and a *Restart firmware* button. The panel closes by itself if
a print starts. Mainsail and printers without readable boards use the dialogs.
On a printer whose boards cannot be read, the older screens below (pick a slot,
pick the toolhead in it, *Apply*) are used and the layout must be updated by
hand after swapping.

**Unload filament (older layout screens).** The *Change layout* screen shows an *Unload filament:
off/on* button once the pending layout differs from the current one. With it
on, *Apply* also unloads the filament of every slot whose toolhead changed and
that has filament loaded in the toolhead (stock `AUTO_FEEDING EXTRUDER=n UNLOAD=1`, one slot
after another). While it works a status screen shows which slot is unloading;
once filament has been unloaded the toolhead is docked (`PARK_EXTRUDER`) as
the final step, and then the *Layout applied* screen lists the outcome per
slot, with a note and a *Restart firmware* button (`VT_RESTART`: closes the dialog, then
`FIRMWARE_RESTART`; it works after Klipper has shut down). If you
swap the toolheads' USB connections, do it while this screen is up (Klipper
stops when a cable is pulled), then click *Restart firmware*. No power cycle is
needed. After the restart the *Calibrate* screen opens by itself if any toolhead
in the new layout is missing an offset, PID tuning or flow K; if everything is
calibrated nothing opens. (A marker file next to the store, valid for 4
hours, records that the restart is part of a swap; a plain restart never
opens the screen.) Whether filament is loaded is read from the feeder's
channel state (`load_finish`), not from a sensor, because a sensor also sees
filament that is merely waiting in the lane or on the spool after an unload.
Slots whose filament is already unloaded are left alone, and slots that are
unloading or loading right now are skipped with a note. The flow stage uses the
same state and refuses a toolhead that is busy loading or unloading. *Cancel*
unloads nothing. A failed unload is reported and the layout
stays applied; if every unload failed the toolhead is not docked. Console: `VT_SET_LAYOUT LAYOUT=... UNLOAD=1`.

Console commands: `VT_STATUS`, `VT_ADD [SIDE=left|right]`, `VT_SET_SIDE`,
`VT_RENAME`, `VT_DELETE`, `VT_SET_LAYOUT LAYOUT=LA,RA,RD,RE` (one toolhead per
slot, in slot order), `VT_REPLACE_NOZZLE`, `VT_SET_NOZZLE`, `VT_INVALIDATE`,
`VT_CALIBRATE`, `VT_SET_PID_TEMP`, `VT_FLOW`, `VT_FLOW_FORGET`. All changes are refused while printing.

### Left Slot and Right Slot toolheads

Slot 0 (the first toolhead slot) is different hardware from slots 1-3, so a
toolhead made for one kind of slot does not fit the other. Every toolhead is
therefore either a **Left Slot** toolhead (fits slot 0 only) or a **Right Slot**
toolhead (fits slots 1-3).

- Layouts that break this are refused, and the layout dialog only offers the
  toolheads that fit the slot you are changing.
- The dialog for adding a toolhead asks which kind it is; Right Slot is
  preselected. Console: `VT_ADD SIDE=left` or `SIDE=right`.
- The side of a *spare* toolhead can be corrected with
  `VT_SET_SIDE TOOLHEAD=<name> SIDE=left|right`. A loaded toolhead's side is
  decided by its slot and cannot be changed while it is loaded.

### Calibrating

`VT_CALIBRATE_MENU` first asks which toolheads to calibrate (those that need
something are preselected), then which calibrations to run, each with its own
on/off toggle:

| Toggle | What it does | On by default when a selected toolhead... |
|---|---|---|
| Offset calibration | Heats, cleans the nozzle automatically (this extrudes filament, so load filament first) and parks the head at the front so you can wipe the nozzle; then asks you to remove the build plate and probes the toolhead | needs calibration |
| PID calibration | Tunes the heater from a cold nozzle (temperature selectable, default 210 C) | has no PID tuning |
| Flow calibration | Runs the stock dynamic flow calibration (K) for the filament in the lane | has no K for the filament it holds now |

Any combination can run. The stages always run in the order **PID, flow,
offset**: PID has to start from a cold nozzle, and the offset stage ends with
the nozzle clean and the build plate off. For flow calibration the wizard picks
up each toolhead (`T<n> A0`) and runs `FLOW_CALIBRATE`, which heats the nozzle
and extrudes filament; it needs filament in the lane. If the filament is detected in the
feeder but not loaded to the toolhead, the wizard loads it first (stock
`AUTO_FEEDING EXTRUDER=n LOAD=1`, with a status screen); if there is no
filament at all, or the load fails, you get *Retry* / *Skip flow* / *Abort*. If only PID and/or
flow are selected the wizard does not touch the build plate. Toolheads you did
not select are not heated, cleaned or probed and keep their calibration.
*Abort calibration* is available in every dialog.

Console: `VT_CALIBRATE [TOOLHEADS=A,B] [OFS=0|1] [PID=0|1] [PID_TEMP=210]
[FLOW=0|1] [CONFIRM=1]`. Without a flag the default above applies; naming
`TOOLHEADS` also turns the offset stage on unless `OFS=0`.

While the wizard runs, stock calibration commands from other clients, such as
the touchscreen app, are rejected, because they would swap toolheads in the
middle of the procedure. The stock touchscreen may show a warning. The
protection ends about 20 seconds after the wizard finishes.

**Dialogs during a calibration.** Fluidd closes a dialog when you click outside
it or press Esc. The wizard therefore treats its screens in two ways:

- *Waiting for you* (wipe the nozzle, remove the build plate, and the
  Retry / Skip / Abort dialogs after a failure): if one is dismissed it is shown
  again at once. Use its buttons, in particular *Abort calibration*, to leave.
  This relies on Klipper being idle while the wizard waits for you, which it is.
- *Working* (heating, cooling, PID, flow, probing, unloading): these screens have
  no buttons. Klipper is busy with a long step then, so a dismissed dialog cannot
  be shown again until the step ends. Instead the dashboard card shows the same
  text below the toolheads until the next notice or dialog. If you dismiss a progress screen, the close
  request waits behind the running step and would otherwise close the next
  dialog the moment it appears; a close that arrives within 1.5 s of a dialog
  being shown is therefore ignored and the dialog is shown again. Other dialogs (menus,
  forms, results) are closed normally when dismissed.

### PID tuning

PID tuning is stored per toolhead, like the nozzle and the base position, and
follows the toolhead when you change the layout: the tuning is written into
the heater of the slot it is now in, and into that slot's stock
`extruder[x]_pid_parameters.json` (`default` profile), so the stock UI and a
restart see the same values.

- The first time the feature starts, each loaded toolhead adopts the tuning its
  slot is using. Later, a toolhead that has no stored tuning (for example a new
  one) leaves the heater it is plugged into unchanged.
- Every stock `PID_CALIBRATE`, whether from the console, the touchscreen or the
  wizard, is recorded for the toolhead in that heater's slot.
- The calibration wizard offers *PID calibration* for the selected toolheads
  only. It is on by default when a selected toolhead has no tuning yet. It
  runs before anything else, because tuning has to start from a cold nozzle: a
  hot nozzle is switched off and the wizard waits until it is below 50 C. The
  temperature defaults to 210 C (standard PLA) and can be changed in the wizard
  (`VT_CALIBRATE PID=1 PID_TEMP=230`) or stored with `VT_SET_PID_TEMP TEMP=`.
  Allowed range 150-300 C. Each toolhead takes several minutes.
- If tuning fails the wizard offers *Retry*, *Skip PID* (carry on with the
  next stage) and *Abort calibration*, and the heater is switched off.
- **Several tunings per toolhead, by temperature.** Every calibrated run
  (console, touchscreen or wizard) at a temperature the toolhead has not been
  tuned at is added to its list; a re-run at the same temperature replaces that
  entry. The latest run is the one in use. Tunings the feature only adopted or
  observed (no target temperature) are in use but not listed. `VT_PID
  [TOOLHEAD=]` lists them; `VT_PID_SELECT TOOLHEAD= TEMP=` switches (idle
  only). In the menu, **Change PID** (pick a toolhead) opens a dialog with the
  preset temperatures and any stored ones: a calibrated temperature switches to
  that tuning and closes the dialog; a "(new)" one starts a PID-only calibration at
  that temperature immediately (heating begins, no further screen).
- Fitting or changing a nozzle does not discard the tunings, but they were
  measured with the old nozzle: they stay stored and the card shows the PID
  line grey ("old nozzle") instead of green until you calibrate again at that
  temperature.

### Dynamic flow calibration (K) per filament

The stock dynamic flow calibration (`FLOW_CALIBRATE`, the K / pressure advance
value) keeps one value per slot, so changing the filament in a toolhead means
calibrating again. With this feature each toolhead remembers one value per
filament, and swapping back and forth between filaments restores the right one.

- The filament is identified by its **Spoolman filament id**, so another spool
  of the same filament shares the value. The module asks Spoolman for it once
  per spool, through Moonraker's Spoolman proxy, and remembers the answer. Until
  that is known the spool id is used (and the values move to the filament id
  afterwards). A lane without a Spoolman spool uses vendor / type / subtype of
  its tag.
- Every `FLOW_CALIBRATE` (console, touchscreen, slicer-driven) is recorded for
  the toolhead in the active slot and the filament in it. A result that is just
  the filament's default (stock falls back to it when the value is out of
  range), an aborted run, or an unchanged value is not recorded.
- When a toolhead gets a filament it has a value for - after a layout change,
  when a spool is assigned to the lane, and at print start - that value is put
  back into stock's table (`flow_calibrator.json`) before stock applies it. If
  it has no value for the new filament, nothing changes: the lane keeps the
  last value, as stock does.
- A value is only used while the nozzle diameter and flow type are the ones it
  was measured with.
- On the first start, the values already in `flow_calibrator.json` are adopted
  for the filament currently in each lane, unless they equal the filament's
  default.
- The wizard's flow stage records the result like any other `FLOW_CALIBRATE`;
  if stock falls back to the filament's default (value out of range) or gives
  no result, the wizard reports a failure with *Retry*, *Skip flow* and
  *Abort calibration*.
- `VT_FLOW [TOOLHEAD=]` lists the stored values and the filament each loaded
  toolhead has. `VT_FLOW_FORGET TOOLHEAD= [KEY=]` removes them (not while
  printing).
- Options in `[virtual_toolheads]`: `flow_filament_lookup` (`True`; `False`
  keeps spool ids and never contacts Spoolman) and `moonraker_url`
  (`http://127.0.0.1:7125`).

### Toolhead boards (automatic swap detection)

Each toolhead board has a unique USB serial number that stays with the board
when its cable moves to another port. Klipper's `[mcu eN]` is tied to a port, so
the serial seen on each port says which toolhead Klipper really has in each
slot. The module reads these serials from sysfs, so no toolhead has to be
reachable, and it works after a cable was pulled.

- **First use:** the boards of the toolheads in the current layout are recorded
  and one dialog asks you to confirm (*Undo* forgets them).
- **After a restart:** if every connected board is known, the layout is updated
  to match the hardware and a dialog lists what changed (with *Calibrate now*
  if something needs it). Swapping toolheads and USB cables therefore needs no
  declaration: just swap and click *Restart firmware*.
- **A mismatch that cannot be right** (a Left Slot toolhead on a Right Slot
  port, a board appearing twice) is reported and the layout is left alone.
- **An unrecognised board** asks what it is: *New toolhead* (it takes the slot,
  the toolhead it displaces becomes a spare, and the nozzle details screen
  opens straight away, pre-filled with the displaced toolhead's nozzle), *This is X* (a toolhead
  that has no board yet) or *Replaces X* (a toolhead whose own board is not
  connected). A replacement keeps nozzle, PID and flow K, but its offset must be
  calibrated again.
- The menu has **Boards** (what is connected, *Identify new board* when an
  unrecognised one is connected, *Add a spare (not connected)* to register a
  toolhead before it is plugged in, *Record the connected boards*,
  *Forget all boards*). Console: `VT_SERIALS`, `VT_SERIAL_LEARN`,
  `VT_SERIAL_FORGET [TOOLHEAD=]`.
- On a printer whose boards cannot be read, nothing changes and the layout
  stays whatever was declared.

### Backup and restore

Menu **Backup**. *Back up now* saves a full copy of the toolhead data
(nozzles, offsets, PID, flow K, boards, layout) in
`persistent/virtual_toolheads.json.backups/`, named by date and time. The newest
20 are kept. *Restore...* lists them (newest first); pick one to see its layout,
then *Restore* (or *Delete this backup*). A restore saves the current data
first (`...-beforerestore.json`), re-reads the connected boards and applies the
restored values to the slots straight away. It is refused while printing.
Console: `VT_BACKUP [TAG=]`, `VT_BACKUPS`, `VT_RESTORE NAME=`,
`VT_BACKUP_DELETE NAME=`.

### Print-start check

If a loaded toolhead needs calibration, starting a print shows a warning. Set
`print_guard: block` in the `[virtual_toolheads]` section to make the check
raise an error in the start macro instead, or `off` to disable it. (The check
is called through the klipper-hooks overlay.)

## Dashboard card (Fluidd)

An optional overlay (`02-virtual-toolheads-card`) adds a **Toolheads** card to the Fluidd status page, placed after the Spoolman card
(or AFC / Tool card if there is no Spoolman). It shows one lane per slot:
`T1 > RA`, left/right slot, nozzle diameter, material, flow type and whether
the toolhead is calibrated. Buttons: **Menu** (`VT_UI`), **Calibrate** (only
when a loaded toolhead is invalid), and per slot **Swap Tool** (layout; nozzle and PID changes are in the menu). The blue **Active** tag on the running toolhead is a button: it asks whether to dock the active tool (`VT_UI PAGE=dock`, runs `PARK_EXTRUDER`). The **Menu** button opens a panel in the card with Change nozzle, Change PID, Boards and Backup (plus Add toolhead and Change layout when the boards cannot be read). Choices are made locally and one `VT_UI ... CARD=1` command is sent on Apply; its result appears as a card notice. `VT_UI` remains for other interfaces. The header **Calibrate** button (shown only while an installed toolhead needs something; Calibrate is always in the Menu panel) toggles a calibrate mode: lane buttons become **Calibrate** toggles (toolheads that need something are preselected), with local Offset / PID / Flow K toggles and PID temperature chips, and *Start* sends one `VT_CAL PAGE=prep`. With the Fluidd card the calibrate panel opens in the card (the card announces itself with `VT_CARD`; other interfaces get the dialog). After a swap restart the calibrate screen opens only when an offset or PID tuning is missing; a missing Dynamic Flow K alone just leaves the card's Calibrate button showing. The result of board detection (which toolheads changed place) is a console status message, not a dialog; a dialog appears only for something unexpected (an unknown board or a conflict). If a toolhead MCU is lost outside the Swap flow (for example a nozzle hot-swapped while powered), a dialog asks whether you did that and offers Restart firmware. The Swap flow skips docking when no tool is picked up. Controls are disabled while printing, shown in the same blue notice style as the status mirror. The card opens its own
Moonraker websocket and shows nothing if the feature is disabled.

It loads through a small `index.html` patch and must be applied after
`62-app-fluidd`. Mainsail and HelixScreen are not covered; use the Macros
buttons there.

## Risks

- Uses a different place to store calibration data than stock. The stock files
  are regenerated from it, but editing them by hand has no lasting effect while
  the feature is enabled.
- Assumes a toolhead's calibration does not depend on the slot it is in. The
  slot each result was measured in is recorded, so you can check this on your
  machine by calibrating one toolhead in two slots and comparing.
- If you swap toolheads and forget to update the layout, the wrong offsets are
  used. Always update the layout after swapping.
- The firmware cannot tell which kind of slot a spare toolhead fits; it only
  knows what you told it. A wrong side on a spare either blocks a layout that
  would fit or allows one that physically cannot.
- The calibration wizard moves toolheads and heats nozzles, using the stock
  calibration commands. PID calibration runs the stock `PID_CALIBRATE`, which
  heats each nozzle repeatedly around the chosen temperature.
- Stock calibration commands from other clients are rejected while the wizard
  runs, including cancelling from the touchscreen; use *Abort calibration* in
  the dialog (or `VT_CAL PAGE=abort`).

## Recommendation

- Stay at the printer for the first calibrations after enabling.
- After swapping a toolhead into a different slot, check alignment with a small
  multi-tool test print before relying on it.
- Keep `print_guard` at `warn` or `block`.

## Technical Details

- Data: `persistent/virtual_toolheads.json` (with one `.bak` copy). If it is
  missing it is rebuilt from the current slots; if it cannot be read the
  feature switches itself off and leaves the file alone.
- Enabling adds `klipper/virtual_toolheads.cfg` to
  `/oem/printer_data/config/extended/`, like the other tweaks.
- Options in `[virtual_toolheads]`: `print_guard` (`off`, `warn`, `block`),
  `guard_calibration` (`True` by default), `store_path`,
  `flow_filament_lookup` (`True`), `moonraker_url`.
- `VT_STATUS` shows the release version (also in the main dialog and in the
  `printer.virtual_toolheads` status object) and whether the calibration
  protection is installed. If
  something misbehaves, `VT_TRACE ACTION=ON`, reproduce, then
  `VT_TRACE ACTION=DUMP` lists every G-code command that ran and what issued it.
- Design notes and the stock internals relied on are in the overlay README.

**Configuration:** Enable or disable in the Firmware Configuration web
interface. Manual editing of `virtual_toolheads.cfg` is not recommended.
