/* Virtual Toolheads dashboard card for Fluidd (v0.3).
 * Opens its own Moonraker websocket, subscribes to the `virtual_toolheads`
 * object and renders one lane per tool slot next to the Spoolman/AFC cards.
 * Shows nothing when the object does not exist. Safe to load twice. */
(function () {
  'use strict';
  if (window.__vtCard) return;
  window.__vtCard = true;

  var CARD_ID = 'vt-card';
  var ANCHORS = ['Spoolman', 'AFC', 'Tool', 'Toolhead', 'Extruder', 'Printer Status'];
  var LABEL_RE = /^[A-Za-z0-9_-]{1,16}$/;
  var LS_KEY = 'vt-card-collapsed';
  var MATERIALS = { stainless: 'Stainless steel', hardened: 'Hardened steel' };
  var NOZZLE_ICON = 'M8 2h8a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zM12 4.2a4.8 4.8 0 1 0 0 9.6 4.8 4.8 0 0 0 0-9.6zM12 5.6a3.4 3.4 0 1 1 0 6.8 3.4 3.4 0 0 1 0-6.8zM12 8a1 1 0 1 0 0 2 1 1 0 0 0 0-2zM8.5 15.2h7v1h-7zM8.5 17.2h7v1h-7zM10.5 20h3L12 23.2z';

  var editing = null;   // { slot, value } while a name is being edited
  var state = { vt: null, printing: false, active: null, parked: null, exState: {}, hideSeq: null, swap: null, cal: null, menu: null, ready: false, note: '', noteErr: false };
  var ws = null, nextId = 1, pending = {}, backoff = 1000, subscribed = false;

  function lsGet() { try { return localStorage.getItem(LS_KEY) === '1'; } catch (e) { return false; } }
  function autoGet() { try { return localStorage.getItem('vtAutoCal') || ''; } catch (e) { return ''; } }
  function loadGet() { try { return localStorage.getItem('vtSwapLoad') === '1' ? 1 : 0; } catch (e) { return 0; } }
  function loadSet(v) { try { localStorage.setItem('vtSwapLoad', v ? '1' : '0'); } catch (e) {} }
  function autoSet(v) { try { localStorage.setItem('vtAutoCal', String(v)); } catch (e) {} }
  function lsSet(v) { try { localStorage.setItem(LS_KEY, v ? '1' : '0'); } catch (e) {} }
  var HIDE_KEY = 'vt-card-hidden';
  function hidGet() { try { return localStorage.getItem(HIDE_KEY) === '1'; } catch (e) { return false; } }
  function hidSet(v) { try { localStorage.setItem(HIDE_KEY, v ? '1' : '0'); } catch (e) {} }
  var shown = !hidGet();
  var collapsed = lsGet();
  var CHECK_ON = 'M10,17L5,12L6.41,10.58L10,14.17L17.59,6.58L19,8M19,3H5C3.89,3 3,3.89 3,5V19C3,20.1 3.9,21 5,21H19C20.1,21 21,20.1 21,19V5C21,3.89 20.1,3 19,3Z';
  var CHECK_OFF = 'M19,3H5C3.89,3 3,3.89 3,5V19A2,2 0 0,0 5,21H19A2,2 0 0,0 21,19V5C21,3.89 20.1,3 19,3M19,5V19H5V5H19Z';

  /* ---------- websocket ---------- */
  function rpc(method, params) {
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) return reject(new Error('Not connected'));
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      ws.send(JSON.stringify({ jsonrpc: '2.0', method: method, params: params || {}, id: id }));
    });
  }
  function connect() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    try { ws = new WebSocket(proto + location.host + '/websocket'); } catch (e) { return retry(); }
    ws.onopen = function () { backoff = 1000; subscribe(); };
    ws.onmessage = function (ev) {
      var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.id != null && pending[m.id]) {
        var p = pending[m.id]; delete pending[m.id];
        if (m.error) p.reject(new Error(m.error.message || 'error')); else p.resolve(m.result);
      } else if (m.method === 'notify_status_update') {
        merge(m.params && m.params[0]); render();
      } else if (m.method === 'notify_gcode_response') {
        if (consoleLine(m.params && m.params[0])) render();
      } else if (m.method === 'notify_klippy_ready') {
        subscribe();
      } else if (m.method === 'notify_klippy_disconnected' || m.method === 'notify_klippy_shutdown') {
        state.ready = false; render();
      }
    };
    ws.onclose = function () {
      ws = null; subscribed = false; pending = {};
      state.ready = false; state.exState = {}; render(); retry();
    };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }
  function retry() { setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 15000); }

  function subscribe() {
    rpc('printer.objects.list').then(function (r) {
      var has = r && r.objects && r.objects.indexOf('virtual_toolheads') >= 0;
      if (!has) { state.vt = null; render(); return; }
      var objs = { virtual_toolheads: null, print_stats: ['state'], toolhead: ['extruder'] };
      /* Each extruder reports ACTIVATE (picked up) or PARKED (on its dock). */
      r.objects.forEach(function (o) { if (/^extruder\d*$/.test(o)) objs[o] = ['state']; });
      return rpc('printer.objects.subscribe', { objects: objs }).then(function (res) { merge(res.status); return replayConsole(); })
        .then(function () { render(); });
    }).catch(function () { state.vt = null; render(); });
  }
  /* The toolhead stays "active" in Klipper's status after it is parked, so the
   * console is the only place that says it was put back on its dock. */
  var PARK_RE = /^\W*park\s+(extruder\d*)\b/i;
  var PICK_RE = /\b(?:pick(?:ed)?[ -]?up|picked|unpark(?:ed)?|activat(?:e|ed|ing)|select(?:ed)?)\b.*?\b(extruder\d*)\b/i;
  function consoleEvent(line) {
    if (typeof line !== 'string') return null;
    var m = PARK_RE.exec(line.replace(/^\/\/\s*/, ''));
    if (m) return { kind: 'park', name: m[1].toLowerCase() };
    m = PICK_RE.exec(line);
    if (m) return { kind: 'pick', name: m[1].toLowerCase() };
    return null;
  }
  function consoleLine(line) {
    var ev = consoleEvent(line); if (!ev) return false;
    var before = state.parked;
    if (ev.kind === 'park') state.parked = ev.name;
    else if (state.parked === ev.name) state.parked = null;
    return state.parked !== before;
  }
  function replayConsole() {
    return rpc('server.gcode_store', { count: 200 }).then(function (r) {
      var lines = (r && r.gcode_store) || [];
      state.parked = null;
      for (var i = 0; i < lines.length; i++) consoleLine(lines[i].message);
      if (state.parked !== state.active) state.parked = null;
    }).catch(function () {});
  }
  function hasExState() { return Object.keys(state.exState).length > 0; }
  function merge(st) {
    if (!st) return;
    Object.keys(st).forEach(function (k) {
      if (/^extruder\d*$/.test(k) && st[k] && st[k].state != null) state.exState[k] = String(st[k].state);
    });
    if (st.virtual_toolheads) state.vt = Object.assign({}, state.vt || {}, st.virtual_toolheads);
    if (st.print_stats && st.print_stats.state != null)
      state.printing = ['printing', 'paused'].indexOf(st.print_stats.state) >= 0;
    if (st.toolhead && st.toolhead.extruder != null) {
      if (st.toolhead.extruder !== state.active) state.parked = null;
      state.active = st.toolhead.extruder;
    }
    state.ready = !!(state.vt && state.vt.ready);
    announce(); autoCalibrate();
  }

  /* Tell the module that this card exists, so screens open here and not as
   * Fluidd dialogs; once per time the module becomes ready. */
  var announced = false;
  function announce() {
    if (!state.ready) { announced = false; return; }
    if (announced || !state.vt || !('auto_calibrate' in state.vt)) return;
    announced = true;
    rpc('printer.gcode.script', { script: 'VT_CARD' }).catch(function () { announced = false; });
  }
  /* After a swap restart the module asks for the calibrate panel. */
  var autoHandled = autoGet();
  function autoCalibrate() {
    var id = state.vt && state.vt.auto_calibrate;
    if (!id || String(id) === autoHandled || !state.ready || state.printing || !calSupported()) return;
    autoHandled = String(id); autoSet(id);
    calStart();
  }

  /* ---------- commands ---------- */
  function send(cmd) {
    state.note = ''; state.noteErr = false;
    rpc('printer.gcode.script', { script: cmd }).catch(function (e) {
      state.note = 'Command failed: ' + e.message; state.noteErr = true; render();
    });
  }
  function labelOk(l) { return typeof l === 'string' && LABEL_RE.test(l); }

  /* ---------- DOM ---------- */
  var CSS = [
    '#vt-card .vt-lanes{display:flex;flex-wrap:wrap;gap:8px;padding:0 0 4px}',
    '#vt-card .vt-lane{flex:1 1 132px;min-width:132px;border:1px solid rgba(128,128,128,.35);border-radius:8px;padding:8px;overflow-wrap:anywhere}',
    '#vt-card .vt-pick{display:flex;flex-wrap:wrap;gap:8px;margin:8px -12px 0 -15px}',
    '#vt-card .vt-pickslot{flex:1 1 132px;min-width:132px;border:1px solid transparent;border-radius:8px;padding:8px}',
    '#vt-card .vt-pickslot.vt-fill{height:0;padding-top:0;padding-bottom:0;visibility:hidden}',
    '#vt-card .vt-pickslot .v-btn{width:100%;min-width:0;padding:0 4px;letter-spacing:0}',
    '#vt-card .vt-pickbreak{flex:0 0 100%;height:0;margin:-8px 0 0}',
    '#vt-card .vt-menugrid{display:grid;grid-template-columns:repeat(2,minmax(0,240px));gap:8px;margin-top:8px}',
    '#vt-card .vt-menugrid .v-btn{width:100%;min-width:0;letter-spacing:0}',
    '#vt-card .vt-menugrid .vt-wide{grid-column:1 / -1}',
    '#vt-card .vt-lane.vt-active{border-color:#2196f3;box-shadow:0 0 0 1px #2196f3 inset}',
    '#vt-card .vt-lane.vt-bad{border-color:#fb8c00}',
    '#vt-card .vt-head{display:flex;justify-content:space-between;font-weight:600;margin-bottom:4px}',
    '#vt-card .vt-row{display:flex;gap:8px;align-items:center}',
    '#vt-card .vt-tag{font-size:11px;opacity:.75;border:1px solid rgba(128,128,128,.5);border-radius:4px;padding:0 4px}',
    '#vt-card .vt-kv{font-size:13px;line-height:1.5}',
    '#vt-card .vt-dim{opacity:.6}',
    '#vt-card .vt-name{background:none;border:0;padding:0 2px;color:inherit;font:inherit;font-weight:600;cursor:pointer;border-bottom:1px dotted currentColor}',
    '#vt-card .vt-name:disabled{cursor:default;border-bottom-color:transparent}',
    '#vt-card .vt-name-input{background:transparent;color:inherit;font:inherit;font-weight:600;width:9em;border:1px solid #2196f3;border-radius:4px;padding:0 4px}',
    '#vt-card .vt-activetag{color:#2196f3;border-color:#2196f3}',
    '#vt-card button.vt-dock{background:none;cursor:pointer;font:inherit;font-size:11px}',
    '#vt-card button.vt-dock:disabled{cursor:default;opacity:.5}',
    '#vt-card .vt-ok{color:#4caf50}#vt-card .vt-warn{color:#fb8c00}',
    '#vt-card .vt-btns{display:flex;flex-direction:row;gap:6px;margin-top:6px}',
    '#vt-card .vt-btns .v-btn{flex:1 1 0;min-width:0;padding:0 4px;letter-spacing:0}',
    '#vt-card .vt-note,#vt-card .vt-unavail{padding:0 0 12px;font-size:13px;opacity:.85}',
    '#vt-card .vt-note.vt-err{color:#ff5252;opacity:1}',
    '#vt-card .vt-notice{margin:0 16px 12px;padding:8px 12px;border-left:3px solid #2196f3;border-radius:4px;background:rgba(33,150,243,.1);font-size:13px;line-height:1.5}',
    '#vt-card .vt-notice-head{display:flex;justify-content:space-between;align-items:flex-start;font-weight:600}',
    '#vt-card .vt-notice-x{background:none;border:0;color:inherit;cursor:pointer;opacity:.6;font-size:16px;line-height:1;padding:0 2px}',
    '#vt-card .vt-notice-x:hover{opacity:1}',
    '#vt-card .vt-swap-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}',
    '#vt-card .vt-swap-row .v-btn{min-width:0;letter-spacing:0}',
    '#vt-card .vt-swap-row .v-btn.vt-on,#vt-card .vt-btns .v-btn.vt-on,#vt-card .vt-calibrate.vt-on,#vt-card .vt-menu.vt-on{background:#2196f3 !important;color:#fff !important}'
  ].join('');

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function scopeAttrs(from, to) {
    if (!from) return;
    for (var i = 0; i < from.attributes.length; i++) {
      var n = from.attributes[i].name;
      if (n.indexOf('data-v-') === 0) to.setAttribute(n, '');
    }
  }
  function findAnchor() {
    var titles = document.querySelectorAll('.v-card__title');
    for (var a = 0; a < ANCHORS.length; a++) {
      for (var i = 0; i < titles.length; i++) {
        var t = titles[i];
        if (t.closest('#' + CARD_ID)) continue;
        if (t.textContent.indexOf(ANCHORS[a]) >= 0) {
          var c = t.closest('.v-card');
          if (c) return c;
        }
      }
    }
    return null;
  }
  function btn(proto, text, disabled, onclick) {
    var b = el('button', proto || 'v-btn v-btn--is-elevated v-btn--has-bg theme--dark v-size--small', null);
    b.type = 'button';
    var s = el('span', 'v-btn__content', text);
    b.appendChild(s);
    if (disabled) { b.disabled = true; b.classList.add('v-btn--disabled'); }
    b.addEventListener('click', onclick);
    return b;
  }

  var tpl = { btn: '', title: '', card: '', scope: null, edit: false };
  function learn(anchor) {
    var t = anchor.querySelector('.v-card__title');
    var b = anchor.querySelector('button.v-btn');
    tpl.edit = !!anchor.querySelector('.v-card__title.draggable');
    tpl.card = tpl.edit ? anchor.className
      : anchor.className.replace(/\bcollapsed\b/g, '').trim();
    tpl.title = t ? t.className : 'v-card__title card-heading';
    tpl.btn = b ? b.className.replace(/\bv-btn--disabled\b/g, '').trim() : '';
    tpl.scope = t;
  }

  function titleHead(row) {
    var c1 = el('div', 'text-no-wrap col align-self-center');
    var ico = el('span', 'v-icon notranslate v-icon--left theme--dark');
    ico.setAttribute('aria-hidden', 'true');
    ico.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" role="img" '
      + 'aria-hidden="true" class="v-icon__svg"><path fill-rule="evenodd" d="' + NOZZLE_ICON + '"></path></svg>';
    c1.appendChild(ico);
    c1.appendChild(el('span', 'font-weight-light', 'Toolheads'));
    row.appendChild(c1);
  }

  /* Layout-edit mode: Fluidd shows every card collapsed with a visibility
   * checkbox. We mirror that; the card cannot be dragged, it simply follows
   * the card it is attached to. */
  function buildEditCard() {
    var card = el('div', tpl.card);
    card.id = CARD_ID; card.dataset.vtMode = 'edit';
    card.setAttribute('narrow', 'true');
    scopeAttrs(tpl.scope, card);
    var title = el('div', tpl.title);
    var row = el('div', 'row flex-nowrap no-gutters');
    titleHead(row);
    var c2 = el('div', 'col col-auto align-self-center');
    var box = el('div', 'v-input mt-0 pt-0 v-input--hide-details v-input--is-label-active '
      + (shown ? 'v-input--is-dirty ' : '') + 'theme--dark v-input--selection-controls '
      + 'v-input--checkbox' + (shown ? ' primary--text' : ''));
    box.style.cursor = 'pointer';
    box.setAttribute('role', 'checkbox'); box.setAttribute('aria-checked', shown ? 'true' : 'false');
    box.setAttribute('aria-label', 'Show Toolheads'); box.tabIndex = 0;
    box.innerHTML = '<div class="v-input__control"><div class="v-input__slot"><div class="v-input--selection-controls__input">'
      + '<span aria-hidden="true" class="v-icon notranslate theme--dark' + (shown ? ' primary--text' : '')
      + '"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" role="img" aria-hidden="true" '
      + 'class="v-icon__svg"><path d="' + (shown ? CHECK_ON : CHECK_OFF) + '"></path></svg></span></div></div></div>';
    function toggle() { shown = !shown; hidSet(!shown); render(); }
    box.addEventListener('click', toggle);
    box.addEventListener('keydown', function (e) {
      if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(); }
    });
    var lay = el('div', 'layout'); lay.appendChild(box);
    var wrap = el('div'); wrap.appendChild(lay); c2.appendChild(wrap); row.appendChild(c2);
    title.appendChild(row);
    var kids = title.querySelectorAll('*');
    for (var k = 0; k < kids.length; k++) scopeAttrs(tpl.scope, kids[k]);
    card.appendChild(title);
    var hid = el('div'); hid.style.display = 'none'; card.appendChild(hid);
    return card;
  }

  function buildCard() {
    var vt = state.vt;
    var card = el('div', tpl.card || 'mb-2 mb-md-4 v-card v-sheet theme--dark rounded-md');
    card.id = CARD_ID; card.dataset.vtMode = 'normal';
    scopeAttrs(tpl.scope, card);
    var style = el('style', null, CSS); card.appendChild(style);

    var canAct = state.ready && !state.printing;
    var title = el('div', tpl.title);
    scopeAttrs(tpl.scope, title);
    var row = el('div', 'row flex-nowrap no-gutters');
    titleHead(row);
    var invalid = (vt && vt.invalid_loaded) || [];
    function colAuto(child) {
      var c = el('div', 'col col-auto align-self-center'); c.appendChild(child); row.appendChild(c);
    }
    if (calSupported() && (state.cal || anyNeeds())) {
      /* Calibration is chosen in the card (no server round trip per click):
       * this toggles the mode, the lanes' buttons become Calibrate toggles. */
      var cal = btn(tpl.btn, 'Calibrate', !canAct, function () {
        if (state.cal) state.cal = null; else calStart();
        render();
      });
      cal.classList.add('vt-calibrate'); if (state.cal) { cal.classList.add('vt-on'); cal.setAttribute('aria-pressed', 'true'); }
      colAuto(cal);
    } else if (!calSupported() && invalid.length) {
      var cal2 = btn(tpl.btn, 'Calibrate', !canAct, function () { send('VT_CALIBRATE DIRECT=1'); });
      cal2.classList.add('vt-calibrate'); colAuto(cal2);
    }
    var menu = btn(tpl.btn, 'Menu', !canAct, function () {
      if (!menuSupported()) { send('VT_UI'); return; }
      /* the menu lives in the card; VT_UI stays for other interfaces */
      if (state.menu) state.menu = null; else { state.menu = { view: 'main' }; state.swap = null; state.cal = null; }
      render();
    });
    menu.classList.add('vt-menu'); if (state.menu) { menu.classList.add('vt-on'); menu.setAttribute('aria-pressed', 'true'); }
    colAuto(menu);
    var col = el('button', 'v-btn v-btn--icon v-btn--round theme--dark v-size--default vt-collapse');
    col.type = 'button';
    col.title = collapsed ? 'Show' : 'Hide';
    col.setAttribute('aria-label', col.title);
    col.innerHTML = '<span class="v-btn__content"><span aria-hidden="true" class="v-icon notranslate '
      + 'v-icon--dense theme--dark"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
      + 'role="img" aria-hidden="true" class="v-icon__svg" style="transition:transform .2s;'
      + 'transform:rotate(' + (collapsed ? '180' : '0') + 'deg)"><path d="'
      + 'M7.41,15.41L12,10.83L16.59,15.41L18,14L12,8L6,14L7.41,15.41Z"></path></svg></span></span>';
    col.addEventListener('click', function () { collapsed = !collapsed; lsSet(collapsed); render(); });
    var wrap = el('div'); wrap.appendChild(col); colAuto(wrap);
    title.appendChild(row);
    var kids = title.querySelectorAll('*');
    for (var k = 0; k < kids.length; k++) scopeAttrs(tpl.scope, kids[k]);
    card.appendChild(title);

    if (collapsed) return card;

    var body = el('div', 'overflow-hidden vt-body'); body.id = 'vt-card-content';
    var lanes = el('div', 'vt-lanes');
    if (!state.ready) {
      lanes.appendChild(el('div', 'vt-unavail', vt && vt.error ? 'Virtual toolheads: ' + vt.error : 'Klipper is not ready.'));
    } else {
      var layout = vt.layout || [];
      for (var s = 0; s < layout.length; s++) lanes.appendChild(lane(s, layout[s], canAct));
    }
    var txt = el('div', 'v-card__text'); txt.appendChild(lanes);
    body.appendChild(txt);
    if (state.swap && (!state.ready || state.printing || !boardsReadable())) state.swap = null;
    if (state.cal && (!state.ready || state.printing || !calSupported())) state.cal = null;
    if (state.swap) body.appendChild(swapPanel(canAct));
    if (state.menu && (!state.ready || state.printing || !menuSupported())) state.menu = null;
    if (state.cal) body.appendChild(calPanel(canAct));
    if (state.menu) body.appendChild(menuPanel(canAct));
    var nt = state.ready && vt && vt.notice;
    if (nt && nt.seq !== state.hideSeq) body.appendChild(noticeBox(nt));
    if (state.printing) {
      var pn = el('div', 'vt-notice vt-printing', null);
      pn.setAttribute('role', 'status');
      pn.appendChild(el('div', 'vt-notice-line', 'Controls are disabled while printing.'));
      body.appendChild(pn);
    }
    if (state.note) body.appendChild(el('div', 'vt-note' + (state.noteErr ? ' vt-err' : ''), state.note));
    card.appendChild(body);
    return card;
  }

  /* The status text of the running calibration / unload (the dialog Fluidd
   * may have been dismissed). Replaced by the next notice or dialog; the x
   * hides this one only. */
  function calSupported() { return !!(state.vt && state.vt.calibrate); }

  function slotNeeds(slot) {
    var vt = state.vt, label = (vt.layout || [])[slot];
    var th = (vt.toolheads || {})[label] || {};
    return th.needs || {};
  }

  /* True when an installed toolhead lacks an offset, PID tuning or flow K. */
  function anyNeeds() {
    var vt = state.vt, n = false;
    (vt.layout || []).forEach(function (l, slot) {
      var s = slotNeeds(slot);
      if (s.ofs || s.pid || s.flow) n = true;
    });
    return n || !!((vt.invalid_loaded || []).length);
  }

  function calStart() {
    var vt = state.vt, sel = [];
    state.swap = null; state.menu = null;
    (vt.layout || []).forEach(function (l, slot) {
      var n = slotNeeds(slot);
      if (n.ofs || n.pid || n.flow) sel.push(slot);
    });
    state.cal = { sel: sel, ofs: 0, pid: 0, flow: 0, manual: {}, pt: (vt.pid_temp || 210) };
    calDefaults();
  }

  /* Each stage is on by default when a picked toolhead lacks it, until the
   * user switches it by hand. */
  function calDefaults() {
    var c = state.cal, vt = state.vt;
    ['ofs', 'pid', 'flow'].forEach(function (k) {
      if (c.manual[k]) return;
      var on = 0;
      c.sel.forEach(function (slot) { if (slotNeeds(slot)[k]) on = 1; });
      if (k === 'flow' && !(vt.calibrate && vt.calibrate.flow_possible)) on = 0;
      c[k] = on;
    });
  }

  function calPanel(canAct) {
    var c = state.cal, vt = state.vt, cfg = vt.calibrate || {};
    var box = el('div', 'vt-notice vt-calpanel');
    var head = el('div', 'vt-notice-head');
    head.appendChild(el('span', 'vt-notice-title', 'Calibrate toolheads'));
    var x = el('button', 'vt-notice-x', '\u00d7');
    x.type = 'button'; x.title = 'Cancel'; x.setAttribute('aria-label', 'Cancel');
    x.addEventListener('click', function () { state.cal = null; render(); });
    head.appendChild(x);
    box.appendChild(head);
    box.appendChild(el('div', 'vt-notice-line', 'Press Calibrate on each toolhead to calibrate (uncalibrated will be preselected), then choose what to run.'));
    var HELP = {
      ofs: 'Offset calibration finds the position of each nozzle: heat, automatic clean, then you wipe the nozzle by hand and remove the build plate so the probe can run.',
      pid: 'PID calibration tunes the heater of each selected toolhead. It must start from a cold nozzle and takes about 5 minutes each.',
      flow: 'Dynamic Flow calibration finds the pressure advance (K) for the filament in each selected toolhead and remembers it for that filament.'
    };
    [['ofs', 'Offset calibration'], ['pid', 'PID calibration'], ['flow', 'Dynamic Flow calibration']].forEach(function (k) {
      if (k[0] === 'flow' && !cfg.flow_possible) return;
      var row = el('div', 'vt-swap-row');
      var b = btn(tpl.btn, k[1] + ': ' + (c[k[0]] ? 'on' : 'off'), !canAct, function () {
        c[k[0]] = c[k[0]] ? 0 : 1; c.manual[k[0]] = true; render();
      });
      b.classList.add('vt-cal-' + k[0]); if (c[k[0]]) b.classList.add('vt-on');
      b.setAttribute('aria-pressed', c[k[0]] ? 'true' : 'false');
      row.appendChild(b);
      box.appendChild(row);
      box.appendChild(el('div', 'vt-notice-line vt-cal-help', HELP[k[0]]));
      if (k[0] === 'pid' && c.pid) {
        box.appendChild(el('div', 'vt-notice-line', 'PID temperature (\u00b0C)'));
        var temps = (cfg.pid_presets || []).slice();
        if (temps.indexOf(c.pt) < 0) { temps.push(c.pt); temps.sort(function (a, b) { return a - b; }); }
        var pr = el('div', 'vt-swap-row');
        temps.forEach(function (t) {
          var tb = btn(tpl.btn, String(t), !canAct, function () { c.pt = t; render(); });
          tb.classList.add('vt-cal-pt'); if (t === c.pt) tb.classList.add('vt-on');
          tb.setAttribute('aria-pressed', t === c.pt ? 'true' : 'false');
          pr.appendChild(tb);
        });
        box.appendChild(pr);
      }
    });
    box.appendChild(el('div', 'vt-notice-line', 'Order: PID, Dynamic Flow, then offset. While it runs, calibration commands from other clients (e.g. the touchscreen) are blocked.'));
    var none = !c.sel.length || !(c.ofs || c.pid || c.flow);
    if (c.sel.length && !(c.ofs || c.pid || c.flow)) box.appendChild(el('div', 'vt-notice-line', 'Turn on at least one calibration to start.'));
    var acts = el('div', 'vt-swap-row');
    var go = btn(tpl.btn, 'Start', !canAct || none, function () {
      var cmd = 'VT_CAL PAGE=prep SEL=' + c.sel.join(',') + ' OFS=' + c.ofs + ' PID=' + c.pid + ' FLOW=' + c.flow + ' PT=' + c.pt;
      state.cal = null; render(); send(cmd);
    });
    go.classList.add('vt-cal-go');
    var cancel = btn(tpl.btn, 'Cancel', false, function () { state.cal = null; render(); });
    cancel.classList.add('vt-cal-cancel');
    acts.appendChild(go); acts.appendChild(cancel); box.appendChild(acts);
    return box;
  }


  /* ---------- Menu (in the card; VT_UI stays for other interfaces) ---------- */
  function menuSupported() { return !!(state.vt && state.vt.options); }

  function thList() {
    var ths = (state.vt && state.vt.toolheads) || {};
    return Object.keys(ths).sort(function (a, b) {
      var sa = ths[a].slot, sb = ths[b].slot;
      if (sa == null && sb == null) return a < b ? -1 : 1;
      if (sa == null) return 1;
      if (sb == null) return -1;
      return sa - sb;
    });
  }
  function whereTxt(th) { return th.slot == null ? 'spare' : 'T' + th.slot; }

  function chips(items, isOn, onPick, cls, canAct) {
    var row = el('div', 'vt-swap-row');
    items.forEach(function (it) {
      var b = btn(tpl.btn, it.text, !canAct, function () { onPick(it.value); });
      b.classList.add(cls); if (isOn && isOn(it.value)) { b.classList.add('vt-on'); b.setAttribute('aria-pressed', 'true'); }
      row.appendChild(b);
    });
    return row;
  }
  function actBtn(text, cls, disabled, fn) {
    var b = btn(tpl.btn, text, disabled, fn); b.classList.add(cls); return b;
  }
  function menuLine(box, text) { box.appendChild(el('div', 'vt-notice-line', text)); }
  function menuGo(cmd) { state.menu = null; render(); send(cmd); }
  function fmtD(d) { return Number(d).toFixed(1); }

  function nozzleForm(box, f, canAct, opts) {
    var o = state.vt.options;
    menuLine(box, 'Diameter (mm)');
    box.appendChild(chips(o.diameters.map(function (d) { return { text: fmtD(d), value: d }; }),
      function (v) { return Math.abs(v - f.d) < 1e-9; }, function (v) { f.d = v; render(); }, 'vt-mn-d', canAct));
    menuLine(box, 'Flow type');
    box.appendChild(chips(o.volume_types.map(function (d) { return { text: d, value: d }; }),
      function (v) { return v === f.v; }, function (v) { f.v = v; render(); }, 'vt-mn-v', canAct));
    menuLine(box, 'Material');
    box.appendChild(chips(o.materials.map(function (d) { return { text: d, value: d }; }),
      function (v) { return v === f.m; }, function (v) { f.m = v; render(); }, 'vt-mn-m', canAct));
  }

  /* The toolheads as buttons laid out like the lanes (same widths and gaps,
   * so they line up with the Swap Tool buttons); spare toolheads follow on
   * new lines in the same pattern. */
  function toolheadPicker(box, canAct, onPick) {
    var ths = state.vt.toolheads || {}, n = (state.vt.layout || []).length || 4;
    var names = thList(), loaded = names.filter(function (l) { return ths[l].slot != null; });
    var spare = names.filter(function (l) { return ths[l].slot == null; });
    var wrap = el('div', 'vt-pick');
    function item(l) {
      var slot = el('div', 'vt-pickslot');
      var b = btn(tpl.btn, l + (ths[l].slot == null ? ' (spare)' : ''), !canAct, function () { onPick(l); });
      b.classList.add('vt-mn-th'); slot.appendChild(b); wrap.appendChild(slot);
    }
    function fill(count) {
      for (var i = (n - (count % n)) % n; i > 0; i--) wrap.appendChild(el('div', 'vt-pickslot vt-fill'));
    }
    loaded.forEach(item); fill(loaded.length);
    if (spare.length) { wrap.appendChild(el('div', 'vt-pickbreak')); spare.forEach(item); fill(spare.length); }
    box.appendChild(wrap);
  }

  function menuPanel(canAct) {
    var m = state.menu, vt = state.vt, ths = vt.toolheads || {};
    var titles = { main: 'Menu', nozzle: 'Change nozzle', pid: 'Change PID', backup: 'Backup', boards: 'Boards', add: 'Add toolhead' };
    var box = el('div', 'vt-notice vt-menupanel');
    var head = el('div', 'vt-notice-head');
    head.appendChild(el('span', 'vt-notice-title', titles[m.view] || 'Menu'));
    var x = el('button', 'vt-notice-x', '×');
    x.type = 'button'; x.title = 'Close'; x.setAttribute('aria-label', 'Close');
    x.addEventListener('click', function () { state.menu = null; render(); });
    head.appendChild(x); box.appendChild(head);
    var back = function () { state.menu = { view: 'main' }; render(); };
    var footer = function (extra) {
      var acts = el('div', 'vt-swap-row');
      (extra || []).forEach(function (b) { acts.appendChild(b); });
      acts.appendChild(actBtn(m.view === 'main' ? 'Close' : 'Back', 'vt-mn-back', false, m.view === 'main' ? function () { state.menu = null; render(); } : back));
      box.appendChild(acts);
    };
    var readable = boardsReadable();

    if (m.view === 'main') {
      var grid = el('div', 'vt-menugrid');
      function mi(view, text, wide, fn) {
        var b = actBtn(text, 'vt-mn-' + view, !canAct, fn || function () { state.menu = { view: view }; render(); });
        if (wide) b.classList.add('vt-wide');
        grid.appendChild(b);
      }
      mi('nozzle', 'Change nozzle'); mi('pid', 'Change PID');
      mi('boards', 'Boards'); mi('backup', 'Backup');
      if (!readable) {
        mi('add', 'Add toolhead');
        mi('layout', 'Change layout', false, function () { menuGo('VT_UI PAGE=layout'); });
      }
      if (calSupported()) mi('calibrate', 'Calibrate', true, function () { calStart(); render(); });
      box.appendChild(grid);
      menuLine(box, readable ? 'To swap toolheads press Swap Tool on a lane.' : 'Change layout opens the dialog.');
      footer();
    } else if (m.view === 'nozzle') {
      if (!m.th) {
        menuLine(box, 'Which toolhead?');
        toolheadPicker(box, canAct, function (l) {
          var n = ths[l].nozzle || {};
          m.th = l; m.f = { mode: 'replace', d: n.diameter, v: n.volume_type, m: n.material }; render();
        });
        footer();
      } else {
        var th = ths[m.th] || {}, n = th.nozzle || {}, f = m.f;
        menuLine(box, m.th + ' (' + whereTxt(th) + '), now: ' + fmtD(n.diameter) + 'mm ' + n.volume_type + '/' + n.material);
        box.appendChild(chips([{ text: 'New nozzle fitted', value: 'replace' }, { text: 'Correct details only', value: 'edit' }],
          function (v) { return v === f.mode; }, function (v) { f.mode = v; render(); }, 'vt-mn-mode', canAct));
        nozzleForm(box, f, canAct);
        var same = Math.abs(f.d - n.diameter) < 1e-9 && f.v === n.volume_type && f.m === n.material;
        menuLine(box, f.mode === 'replace' ? 'Calibration for ' + m.th + ' will be invalidated.'
          : (same ? 'Nothing changed yet.' : 'Details changed: calibration for ' + m.th + ' will be invalidated.'));
        footer([actBtn('Apply', 'vt-mn-apply', !canAct || (f.mode === 'edit' && same), function () {
          menuGo('VT_UI PAGE=nozzle_apply CARD=1 TOOLHEAD=' + m.th + ' MODE=' + f.mode + ' DIAMETER=' + f.d + ' VOLUME_TYPE=' + f.v + ' MATERIAL=' + f.m);
        })]);
      }
    } else if (m.view === 'pid') {
      if (!m.th) {
        menuLine(box, 'Which toolhead?');
        toolheadPicker(box, canAct, function (l) { m.th = l; render(); });
        footer();
      } else {
        var t2 = ths[m.th] || {}, prof = ((t2.pid || {}).profiles) || [];
        var by = {}; prof.forEach(function (pp) { by[pp.temp] = pp; });
        var temps = ((vt.calibrate || {}).pid_presets || []).slice();
        prof.forEach(function (pp) { if (temps.indexOf(pp.temp) < 0) temps.push(pp.temp); });
        if (vt.pid_temp != null && temps.indexOf(vt.pid_temp) < 0) temps.push(vt.pid_temp);
        temps.sort(function (a, b) { return a - b; });
        menuLine(box, 'PID tuning of ' + m.th + ' (' + whereTxt(t2) + '). Calibrated temperatures use the stored tuning when clicked.' + (t2.slot != null ? ' Others start a PID calibration right away.' : ' Put the toolhead in a slot to calibrate others.'));
        prof.forEach(function (pp) {
          menuLine(box, Math.round(pp.temp) + ' °C: calibrated ' + pp.at + (pp.stale ? ' (before the last nozzle change)' : '') + (pp.active ? ' (in use)' : ''));
        });
        var row2 = el('div', 'vt-swap-row');
        temps.forEach(function (t) {
          var pp = by[t];
          if (pp) {
            var b = actBtn(Math.round(t) + ' °C', 'vt-mn-pt', !canAct, function () { menuGo('VT_UI PAGE=pid_apply CARD=1 TOOLHEAD=' + m.th + ' TEMP=' + Math.round(t)); });
            if (pp.active) { b.classList.add('vt-on'); b.setAttribute('aria-pressed', 'true'); }
            row2.appendChild(b);
          } else if (t2.slot != null) {
            row2.appendChild(actBtn(Math.round(t) + ' °C (new)', 'vt-mn-pt-new', !canAct, function () {
              menuGo('VT_CAL PAGE=prep SEL=' + t2.slot + ' OFS=0 PID=1 FLOW=0 PT=' + t + ' DIRECT=1');
            }));
          }
        });
        box.appendChild(row2);
        footer();
      }
    } else if (m.view === 'backup') {
      var list = vt.backup_list || [];
      if (m.confirm) {
        var cf = m.confirm, entry = list.filter(function (b) { return b.name === cf.name; })[0] || { when: cf.name, summary: '' };
        menuLine(box, (cf.kind === 'restore' ? 'Restore ' : 'Delete ') + entry.when + '? ' + entry.summary);
        if (cf.kind === 'restore') menuLine(box, 'This replaces all current toolhead data. A backup of the current data is made first.');
        var go = actBtn(cf.kind === 'restore' ? 'Restore' : 'Delete', 'vt-mn-confirm', !canAct, function () {
          menuGo('VT_UI PAGE=' + (cf.kind === 'restore' ? 'restore_apply' : 'backup_delete_apply') + ' CARD=1 NAME=' + cf.name);
        });
        var no = actBtn('Cancel', 'vt-mn-cancel', false, function () { m.confirm = null; render(); });
        var ar = el('div', 'vt-swap-row'); ar.appendChild(go); ar.appendChild(no); box.appendChild(ar);
      } else {
        var nb = el('div', 'vt-swap-row');
        nb.appendChild(actBtn('Back up now', 'vt-mn-backup-now', !canAct, function () { menuGo('VT_UI PAGE=backup_apply CARD=1'); }));
        box.appendChild(nb);
        if (!list.length) menuLine(box, 'There are no backups yet.');
        list.forEach(function (b) {
          var r = el('div', 'vt-mn-backup');
          r.appendChild(el('div', 'vt-notice-line', b.when + ' — ' + b.summary));
          var br = el('div', 'vt-swap-row');
          br.appendChild(actBtn('Restore', 'vt-mn-restore', !canAct, function () { m.confirm = { kind: 'restore', name: b.name }; render(); }));
          br.appendChild(actBtn('Delete', 'vt-mn-delete', !canAct, function () { m.confirm = { kind: 'delete', name: b.name }; render(); }));
          r.appendChild(br); box.appendChild(r);
        });
        footer();
      }
    } else if (m.view === 'boards') {
      var bd = vt.boards || {};
      if (!bd.available) menuLine(box, 'The toolhead boards cannot be identified on this printer.');
      else {
        (bd.lines || []).forEach(function (l) { menuLine(box, l); });
        menuLine(box, 'Boards are recognised on every restart, so swapping toolheads and USB cables needs no extra steps.');
      }
      var bb = el('div', 'vt-swap-row');
      if (bd.available && (bd.unknown || []).length)
        bb.appendChild(actBtn('Identify new board', 'vt-mn-identify', !canAct, function () { menuGo('VT_UI PAGE=serial'); }));
      bb.appendChild(actBtn('Add a spare (not connected)', 'vt-mn-addspare', !canAct, function () { state.menu = { view: 'add' }; render(); }));
      if (bd.available) {
        bb.appendChild(actBtn('Record the connected boards', 'vt-mn-learn', !canAct, function () { menuGo('VT_UI PAGE=serial_learn_apply CARD=1'); }));
        bb.appendChild(actBtn('Forget all boards', 'vt-mn-forget', !canAct, function () { menuGo('VT_UI PAGE=serial_forget_apply CARD=1'); }));
      }
      box.appendChild(bb);
      footer();
    } else if (m.view === 'add') {
      if (!m.f) m.f = { side: 'right', d: 0.4, v: 'standard', m: 'stainless' };
      var af = m.f;
      menuLine(box, 'Side');
      box.appendChild(chips([{ text: 'Left', value: 'left' }, { text: 'Right', value: 'right' }],
        function (v) { return v === af.side; }, function (v) { af.side = v; render(); }, 'vt-mn-side', canAct));
      nozzleForm(box, af, canAct);
      footer([actBtn('Add', 'vt-mn-add', !canAct, function () {
        menuGo('VT_UI PAGE=add_apply CARD=1 SIDE=' + af.side + ' DIAMETER=' + af.d + ' VOLUME_TYPE=' + af.v + ' MATERIAL=' + af.m);
      })]);
    }
    return box;
  }

  function boardsReadable() {
    return !!(state.vt && state.vt.boards && state.vt.boards.available);
  }

  /* Swap toolheads: choose the slots to take out and whether to unload their
   * filament. Local state only; Continue sends one command. */
  function swapPanel(canAct) {
    var sw = state.swap, vt = state.vt;
    var box = el('div', 'vt-notice vt-swap');
    var head = el('div', 'vt-notice-head');
    head.appendChild(el('span', 'vt-notice-title', 'Swap toolheads'));
    var x = el('button', 'vt-notice-x', '\u00d7');
    x.type = 'button'; x.title = 'Cancel'; x.setAttribute('aria-label', 'Cancel');
    x.addEventListener('click', function () { state.swap = null; render(); });
    head.appendChild(x);
    box.appendChild(head);
    box.appendChild(el('div', 'vt-notice-line', 'Press Swap Tool on each toolhead you are going to take out. The ones you put in are recognised automatically after the restart.'));
    var un = btn(tpl.btn, 'Unload filament: ' + (sw.unload ? 'on' : 'off'), !canAct, function () {
      sw.unload = sw.unload ? 0 : 1; render();
    });
    un.classList.add('vt-swap-unload'); if (sw.unload) un.classList.add('vt-on');
    un.setAttribute('aria-pressed', sw.unload ? 'true' : 'false');
    var ld = btn(tpl.btn, 'Load filament after: ' + (sw.load ? 'on' : 'off'), !canAct, function () {
      sw.load = sw.load ? 0 : 1; loadSet(sw.load); render();
    });
    ld.classList.add('vt-swap-load'); if (sw.load) ld.classList.add('vt-on');
    ld.setAttribute('aria-pressed', sw.load ? 'true' : 'false');
    var row2 = el('div', 'vt-swap-row'); row2.appendChild(un); row2.appendChild(ld); box.appendChild(row2);
    box.appendChild(el('div', 'vt-notice-line', (sw.unload ? 'Filament loaded on the picked toolheads is unloaded. ' : '') + (sw.load ? 'After the restart (and any calibration) filament is loaded in these slots. ' : '') + 'The toolhead that is picked up is docked.'));
    var acts = el('div', 'vt-swap-row');
    var go = btn(tpl.btn, 'Continue', !canAct || !sw.sel.length, function () {
      var cmd = 'VT_UI PAGE=swap_apply SEL=' + sw.sel.join(',') + ' UNLOAD=' + sw.unload + ' LOAD=' + (sw.load ? 1 : 0);
      state.swap = null; render(); send(cmd);
    });
    go.classList.add('vt-swap-go');
    var cancel = btn(tpl.btn, 'Cancel', false, function () { state.swap = null; render(); });  /* un-toggles every Swap Tool button */
    cancel.classList.add('vt-swap-cancel');
    acts.appendChild(go); acts.appendChild(cancel); box.appendChild(acts);
    return box;
  }

  function noticeBox(nt) {
    var box = el('div', 'vt-notice');
    box.setAttribute('role', 'status');
    var head = el('div', 'vt-notice-head');
    head.appendChild(el('span', 'vt-notice-title', nt.title || ''));
    var x = el('button', 'vt-notice-x', '\u00d7');
    x.type = 'button'; x.title = 'Hide'; x.setAttribute('aria-label', 'Hide');
    x.addEventListener('click', function () { state.hideSeq = nt.seq; render(); });
    head.appendChild(x);
    box.appendChild(head);
    (nt.lines || []).forEach(function (l) { box.appendChild(el('div', 'vt-notice-line', l)); });
    if (nt.restart) {
      var rb = btn(tpl.btn, 'Restart firmware', false, function () { send('VT_RESTART'); });
      rb.classList.add('vt-notice-restart');
      var rr = el('div', 'vt-swap-row'); rr.appendChild(rb); box.appendChild(rr);
    }
    return box;
  }

  function nameInput(slot, label) {
    var inp = el('input', 'vt-name-input');
    inp.type = 'text'; inp.maxLength = 16; inp.value = editing.value;
    inp.setAttribute('aria-label', 'New name for ' + label);
    inp.addEventListener('input', function () { editing.value = inp.value; });
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); commitRename(label); }
      else if (e.key === 'Escape') { e.preventDefault(); editing = null; render(); }
    });
    inp.addEventListener('blur', function () {
      /* leaving the field without Enter cancels, unless a re-render swapped it */
      setTimeout(function () {
        if (editing && editing.slot === slot && document.activeElement !== document.querySelector('#vt-card .vt-name-input')) {
          editing = null; render();
        }
      }, 150);
    });
    return inp;
  }

  function commitRename(old) {
    var v = (editing.value || '').trim();
    if (v === old) { editing = null; render(); return; }
    var err = '';
    if (!LABEL_RE.test(v)) err = 'Name must be 1-16 characters: letters, digits, _ or -.';
    else {
      var names = Object.keys((state.vt && state.vt.toolheads) || {});
      for (var i = 0; i < names.length; i++)
        if (names[i] !== old && names[i].toLowerCase() === v.toLowerCase()) err = "'" + names[i] + "' is already used.";
    }
    if (err) { state.note = err; state.noteErr = true; render(); focusInput(); return; }
    editing = null;
    send('VT_RENAME TOOLHEAD=' + old + ' LABEL=' + v);
    render();
  }
  function focusInput() {
    var inp = document.querySelector('#vt-card .vt-name-input'); if (inp) { inp.focus(); }
  }

  function lane(slot, label, canAct) {
    var th = (state.vt.toolheads || {})[label] || {};
    var n = th.nozzle || {};
    var exName = slot === 0 ? 'extruder' : 'extruder' + slot;
    /* The extruder's own state is the truth. Klipper's toolhead.extruder still
     * names a parked one (and one on a fresh boot), so it is only a fallback
     * for firmware that does not report the state. */
    var isActive = hasExState()
      ? state.exState[exName] === 'ACTIVATE'
      : state.active === exName && state.parked !== exName;
    var ln = el('div', 'vt-lane' + (isActive ? ' vt-active' : '') + (th.valid === false ? ' vt-bad' : ''));
    ln.setAttribute('data-slot', String(slot));
    var h = el('div', 'vt-head');
    var nm = el('span', 'vt-row');
    nm.appendChild(el('span', null, 'T' + slot + ' \u203a'));
    var okLabel = labelOk(label);
    if (editing && editing.slot === slot) {
      nm.appendChild(nameInput(slot, label));
    } else {
      var nb = el('button', 'vt-name', label);
      nb.type = 'button';
      nb.title = 'Rename ' + label;
      if (!canAct || !okLabel) nb.disabled = true;
      nb.addEventListener('click', function () {
        editing = { slot: slot, value: label }; state.note = ''; state.noteErr = false; render();
        var inp = document.querySelector('#vt-card .vt-name-input'); if (inp) { inp.focus(); inp.select(); }
      });
      nm.appendChild(nb);
    }
    h.appendChild(nm);
    var tags = el('span', 'vt-row');
    if (isActive) {
      /* Clicking the tag asks whether to dock the active tool. */
      var at = el('button', 'vt-tag vt-activetag vt-dock', 'Active');
      at.type = 'button'; at.title = 'Dock the active tool';
      if (!canAct) at.disabled = true;
      at.addEventListener('click', function () { send('VT_UI PAGE=dock DIRECT=1'); });
      tags.appendChild(at);
    }
    h.appendChild(tags);
    ln.appendChild(h);
    var kv = el('div', 'vt-kv');
    kv.appendChild(el('div', null, (n.diameter != null ? Number(n.diameter).toFixed(2) : '?') + ' mm'));
    kv.appendChild(el('div', null, MATERIALS[n.material] || n.material || '?'));
    kv.appendChild(el('div', null, n.volume_type === 'high_flow' ? 'High flow' : 'Standard flow'));
    var cs = el('div', th.valid === false ? 'vt-warn vt-cal' : 'vt-ok vt-cal',
      th.valid === false ? 'Needs calibration' : 'Calibrated');
    cs.title = th.valid === false ? (th.invalid_reason || '') : '';
    kv.appendChild(cs);
    if (th.pid) {
      /* Green: tuned. Grey: not tuned, or tuned before the last nozzle change. */
      var pidOk = th.pid.calibrated && !th.pid.stale;
      var pt = th.pid.calibrated
        ? (th.pid.target ? 'PID ' + Math.round(th.pid.target) + ' \u00b0C' : 'PID tuned')
          + (th.pid.stale ? ' (old nozzle)' : '')
        : 'PID not tuned';
      var pe = el('div', 'vt-pid ' + (pidOk ? 'vt-ok' : 'vt-dim'), pt);
      if (th.pid.calibrated && th.pid.at) pe.title = 'Tuned ' + th.pid.at + (th.pid.source ? ' (' + th.pid.source + ')' : '');
      kv.appendChild(pe);
    }
    /* Dynamic flow (K) of the filament in the lane; "No Filament Loaded"
     * for a toolhead without filament. */
    if (th.flow && th.flow.filament) {
      var fk = th.flow.k != null;
      var fe = el('div', 'vt-flow ' + (fk ? 'vt-ok' : 'vt-dim'),
        fk ? 'Dynamic Flow K ' + Number(th.flow.k).toFixed(3) : 'Dynamic Flow not calibrated');
      fe.title = 'Filament ' + th.flow.filament;
      kv.appendChild(fe);
    } else if (th.flow) {
      kv.appendChild(el('div', 'vt-flow vt-dim', 'No Filament Loaded'));
    }
    ln.appendChild(kv);
    var bs = el('div', 'vt-btns');
    var ok = labelOk(label);
    var change = btn(tpl.btn, state.cal ? 'Calibrate' : 'Swap Tool', !canAct || !ok, function () {
      state.note = ''; state.noteErr = false;
      if (state.cal) {
        /* calibration mode: the button toggles this toolhead for calibration */
        var ci = state.cal.sel.indexOf(slot);
        if (ci >= 0) state.cal.sel.splice(ci, 1); else { state.cal.sel.push(slot); state.cal.sel.sort(); }
        calDefaults(); render();
      } else if (boardsReadable()) {
        /* The toolhead boards identify themselves: the button toggles this
         * toolhead for taking out; nothing is sent until Continue. */
        state.menu = null;
        if (!state.swap) state.swap = { sel: [slot], unload: 1, load: loadGet() };
        else {
          var i = state.swap.sel.indexOf(slot);
          if (i >= 0) state.swap.sel.splice(i, 1); else { state.swap.sel.push(slot); state.swap.sel.sort(); }
          if (!state.swap.sel.length) state.swap = null;
        }
        render();
      } else {
        send('VT_UI PAGE=layout_slot SLOT=' + slot + ' PENDING=' + (state.vt.layout || []).join(',') + ' DIRECT=1');
      }
    });
    change.classList.add('vt-change');
    var picked = state.cal ? state.cal.sel : (state.swap ? state.swap.sel : []);
    if (picked.indexOf(slot) >= 0) {
      change.classList.add('vt-on'); change.setAttribute('aria-pressed', 'true');
    }
    /* Nozzle and PID changes are in the menu (VT_MENU). */
    bs.appendChild(change);
    ln.appendChild(bs);
    return ln;
  }

  function render() {
    var existing = document.getElementById(CARD_ID);
    if (!state.vt) { if (existing) existing.remove(); return; }
    var anchor = findAnchor();
    if (!anchor) { if (existing) existing.remove(); return; }
    learn(anchor);
    if (!tpl.edit && !shown) { if (existing) existing.remove(); return; }
    var fresh = tpl.edit ? buildEditCard() : buildCard();
    if (existing) existing.remove();
    anchor.parentNode.insertBefore(fresh, anchor.nextSibling);
    if (editing) focusInput();
  }

  /* Vue re-renders the dashboard without knowing about our node: re-insert
   * after the anchor whenever the card is missing, misplaced, or Fluidd
   * switched between normal and layout-edit mode. */
  var scheduled = false;
  function needsRender() {
    if (!state.vt) return !!document.getElementById(CARD_ID);
    var anchor = findAnchor(), c = document.getElementById(CARD_ID);
    if (!anchor) return !!c;
    var edit = !!anchor.querySelector('.v-card__title.draggable');
    if (!edit && !shown) return !!c;
    return !c || c.previousElementSibling !== anchor
      || c.dataset.vtMode !== (edit ? 'edit' : 'normal');
  }
  var mo = new MutationObserver(function () {
    if (scheduled) return;
    scheduled = true;
    setTimeout(function () { scheduled = false; if (needsRender()) render(); }, 100);
  });

  function start() {
    mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
    connect();
  }
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
})();
