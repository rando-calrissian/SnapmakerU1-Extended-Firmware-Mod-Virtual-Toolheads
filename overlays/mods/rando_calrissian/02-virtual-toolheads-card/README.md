# Virtual toolheads: Fluidd dashboard card

Adds `/virtual-toolheads-card.js` to Fluidd and loads it from `index.html`
(patch applies after `62-app-fluidd`). Requires the `01-virtual-toolheads`
overlay; without the `virtual_toolheads` Klipper object the card stays hidden.
Tests: `python3 test/test_card.py` (needs Playwright + Chromium).

The card also shows the status text of a running calibration or unload (the
module publishes it as `virtual_toolheads.notice`) below the toolhead layout,
until the next notice or dialog replaces it. An x hides the current one.
