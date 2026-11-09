import json, os, unittest
from playwright.sync_api import sync_playwright

JS = open(os.path.join(os.path.dirname(__file__), '..', 'root', 'home', 'lava', 'fluidd', 'virtual-toolheads-card.js')).read()

def vt_status(**kw):
    s = {'ready': True, 'error': '', 'version': '0.2', 'epoch': 1,
         'layout': ['LA', 'RA', 'RB', 'RC'],
         'toolheads': {
           'LA': {'slot': 0, 'side': 'left', 'valid': True, 'pid': {'calibrated': True, 'target': 210.0, 'at': '2026-10-01 10:00:00', 'source': 'calibrated'}, 'flow': {'count': 1, 'filament': 'fil:11', 'k': 0.0412}, 'nozzle': {'diameter': 0.4, 'volume_type': 'standard', 'material': 'stainless'}},
           'RA': {'slot': 1, 'side': 'right', 'valid': False, 'invalid_reason': 'nozzle changed', 'pid': {'calibrated': False, 'target': None, 'at': None, 'source': None}, 'flow': {'count': 0, 'filament': 'fil:12', 'k': None}, 'nozzle': {'diameter': 0.6, 'volume_type': 'high_flow', 'material': 'hardened'}},
           'RB': {'slot': 2, 'side': 'right', 'valid': True, 'nozzle': {'diameter': 0.4, 'volume_type': 'standard', 'material': 'stainless'}},
           'RC': {'slot': 3, 'side': 'right', 'valid': True, 'nozzle': {'diameter': 0.4, 'volume_type': 'standard', 'material': 'stainless'}}},
         'invalid_loaded': ['RA']}
    s.update(kw); return s

def page_html(cards=('Spoolman',)):
    body = ''.join(
        '<div class="mb-2 v-card v-sheet theme--dark collapsable-card" data-v-aaa>'
        '<div class="v-card__title collapsable-card-title card-heading" data-v-aaa>%s</div>'
        '<button class="v-btn v-btn--is-elevated btncolor" data-v-aaa>x</button></div>' % c for c in cards)
    return '<html><body><div id="app">%s</div></body></html>' % body

EDIT_JS = """(on) => {
  document.querySelectorAll('#app > .v-card').forEach(c => {
    if (c.id === 'vt-card') return;
    c.classList.toggle('collapsed', on);
    c.querySelector('.v-card__title').classList.toggle('draggable', on);
  });
}"""

class Mock:
    def __init__(self, objects=True, status=None, legacy=False, states=None):
        self.sent = []; self.objects = objects; self.status = status or vt_status()
        self.fail_gcode = False; self.conns = []; self.history = []; self.legacy = legacy; self.states = states or {'extruder': 'PARKED', 'extruder1': 'ACTIVATE', 'extruder2': 'PARKED', 'extruder3': 'PARKED'}
    def handler(self, ws):
        self.conns.append(ws)
        def on_msg(raw):
            m = json.loads(raw); meth = m.get('method'); i = m['id']
            if meth == 'printer.objects.list':
                res = {'objects': (['toolhead', 'virtual_toolheads'] + ([] if self.legacy else ['extruder', 'extruder1', 'extruder2', 'extruder3'])) if self.objects else ['toolhead']}
            elif meth == 'printer.objects.subscribe':
                st = {'virtual_toolheads': self.status, 'print_stats': {'state': 'standby'}, 'toolhead': {'extruder': 'extruder1'}}
                if not self.legacy: st.update({k: {'state': v} for k, v in self.states.items()})
                res = {'status': st}
            elif meth == 'printer.gcode.script':
                self.sent.append(m['params']['script'])
                if self.fail_gcode:
                    ws.send(json.dumps({'jsonrpc': '2.0', 'id': i, 'error': {'message': 'boom'}})); return
                res = 'ok'
            elif meth == 'server.gcode_store':
                res = {'gcode_store': [{'message': l, 'type': 'response'} for l in self.history]}
            else: res = {}
            ws.send(json.dumps({'jsonrpc': '2.0', 'id': i, 'result': res}))
        ws.on_message(on_msg)
    def console(self, line):
        for c in self.conns:
            c.send(json.dumps({'method': 'notify_gcode_response', 'params': [line]}))
    def push(self, status):
        for c in self.conns:
            c.send(json.dumps({'method': 'notify_status_update', 'params': [status, 0]}))

class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start(); cls.br = cls.pw.chromium.launch()
    @classmethod
    def tearDownClass(cls):
        cls.br.close(); cls.pw.stop()
    def open(self, mock, cards=('Spoolman',), width=1280):
        ctx = self.br.new_context(viewport={'width': width, 'height': 900})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.route('http://fluidd.test/', lambda r: r.fulfill(body=page_html(cards), content_type='text/html'))
        pg.route_web_socket('ws://fluidd.test/websocket', mock.handler)
        pg.goto('http://fluidd.test/')
        pg.add_script_tag(content=JS)
        return pg

class PlacementTests(Base):
    def test_inserted_after_spoolman(self):
        pg = self.open(Mock()); pg.wait_for_selector('#vt-card')
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card').previousElementSibling.textContent.includes('Spoolman')"))
    def test_absent_without_object(self):
        pg = self.open(Mock(objects=False)); pg.wait_for_timeout(500)
        self.assertEqual(pg.locator('#vt-card').count(), 0)
    def test_anchor_preference_order_and_last_resort(self):
        pg = self.open(Mock(), cards=('Cameras', 'Tool', 'AFC')); pg.wait_for_selector('#vt-card')
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card').previousElementSibling.textContent.includes('AFC')"))
        pg = self.open(Mock(), cards=('Cameras', 'Tool')); pg.wait_for_selector('#vt-card')
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card').previousElementSibling.textContent.includes('Tool')"))
        pg = self.open(Mock(), cards=('Cameras',)); pg.wait_for_timeout(500)
        self.assertEqual(pg.locator('#vt-card').count(), 0)
    def test_reinserted_after_rerender(self):
        pg = self.open(Mock()); pg.wait_for_selector('#vt-card')
        pg.evaluate("document.querySelector('#vt-card').remove()")
        pg.wait_for_selector('#vt-card', timeout=3000)
    def test_no_duplicates_and_double_load(self):
        pg = self.open(Mock()); pg.wait_for_selector('#vt-card')
        pg.add_script_tag(content=JS); pg.wait_for_timeout(300)
        self.assertEqual(pg.locator('#vt-card').count(), 1)
    def test_header_mirrors_fluidd_title(self):
        pg = self.open(Mock()); pg.wait_for_selector('#vt-card')
        self.assertEqual(pg.locator('#vt-card .v-card__title .v-icon--left svg').count(), 1)
        self.assertEqual(pg.locator('#vt-card .v-card__title .font-weight-light').inner_text(), 'Toolheads')
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card .font-weight-light').hasAttribute('data-v-aaa')"))
        self.assertGreaterEqual(pg.locator('#vt-card .col-auto').count(), 2)

    def test_scope_attr_and_classes_copied(self):
        pg = self.open(Mock()); pg.wait_for_selector('#vt-card')
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card').hasAttribute('data-v-aaa')"))
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card').classList.contains('collapsable-card')"))

class RenderingTests(Base):
    def test_lanes_content(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-lane').count(), 4)
        t = pg.locator('.vt-lane[data-slot="1"]').inner_text()
        for s in ('T1', 'RA', '0.60 mm', 'Hardened steel', 'High flow', 'Needs calibration'):
            self.assertIn(s, t)
        self.assertNotIn('slot', t.lower().replace('t1', ''))
        self.assertNotIn('Left slot', pg.locator('#vt-card').inner_text())
    def test_pid_line(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertIn('PID 210 \u00b0C', pg.locator('.vt-lane[data-slot="0"]').inner_text())
        self.assertIn('PID not tuned', pg.locator('.vt-lane[data-slot="1"]').inner_text())
        self.assertEqual(pg.locator('.vt-lane[data-slot="1"] .vt-pid.vt-dim').count(), 1)
        self.assertEqual(pg.locator('.vt-lane[data-slot="0"] .vt-pid.vt-ok').count(), 1)
        # an older module without PID data shows no PID line
        self.assertEqual(pg.locator('.vt-lane[data-slot="2"] .vt-pid').count(), 0)
        st = vt_status(); st['toolheads']['RB']['pid'] = {'calibrated': True, 'target': None, 'at': None, 'source': 'adopted'}
        pg2 = self.open(Mock(status=st)); pg2.wait_for_selector('.vt-lane')
        self.assertIn('PID tuned', pg2.locator('.vt-lane[data-slot="2"]').inner_text())
    def test_flow_line(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertIn('Dynamic Flow K 0.041', pg.locator('.vt-lane[data-slot="0"]').inner_text())
        self.assertEqual(pg.locator('.vt-lane[data-slot="0"] .vt-flow.vt-ok').count(), 1)
        self.assertIn('Dynamic Flow not calibrated', pg.locator('.vt-lane[data-slot="1"]').inner_text())
        self.assertEqual(pg.locator('.vt-lane[data-slot="1"] .vt-flow.vt-dim').count(), 1)
        # older module (no flow data): no flow line
        self.assertEqual(pg.locator('.vt-lane[data-slot="2"] .vt-flow').count(), 0)
        st = vt_status(); st['toolheads']['RB']['flow'] = {'count': 0, 'filament': None, 'k': None}
        pg2 = self.open(Mock(status=st)); pg2.wait_for_selector('.vt-lane')
        self.assertEqual(pg2.locator('.vt-lane[data-slot="2"] .vt-flow').count(), 1)
        self.assertIn('No Filament Loaded', pg2.locator('.vt-lane[data-slot="2"]').inner_text())
    def test_status_notice(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-notice').count(), 0)
        n1 = {'seq': 1, 'title': 'Dynamic Flow calibration: toolhead T1', 'lines': ['Calibrating T1 (B), 1 of 2.', 'This takes several minutes.']}
        m.push({'virtual_toolheads': {'notice': n1}})
        pg.wait_for_selector('.vt-notice')
        t = pg.locator('.vt-notice').inner_text()
        self.assertIn('Dynamic Flow calibration: toolhead T1', t)
        self.assertIn('This takes several minutes.', t)
        # below the toolhead layout
        self.assertTrue(pg.evaluate("document.querySelector('.vt-lanes').compareDocumentPosition(document.querySelector('.vt-notice')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        # the next notice replaces it
        m.push({'virtual_toolheads': {'notice': {'seq': 2, 'title': 'Calibration', 'lines': ['Probing T1.']}}})
        pg.wait_for_function("document.querySelector('.vt-notice').innerText.indexOf('Probing T1.')>=0")
        self.assertEqual(pg.locator('.vt-notice').count(), 1)
        # hide this one: it stays hidden, the next one shows again
        pg.locator('.vt-notice-x').click()
        self.assertEqual(pg.locator('.vt-notice').count(), 0)
        m.push({'virtual_toolheads': {'invalid_loaded': []}})
        pg.wait_for_timeout(200)
        self.assertEqual(pg.locator('.vt-notice').count(), 0)
        m.push({'virtual_toolheads': {'notice': {'seq': 3, 'title': 'Calibration', 'lines': ['Cooling.']}}})
        pg.wait_for_selector('.vt-notice')
        # a dialog (or a close) clears it
        m.push({'virtual_toolheads': {'notice': None}})
        pg.wait_for_function("!document.querySelector('.vt-notice')")
    def test_notice_without_module_support_is_absent(self):
        st = vt_status(); st.pop('notice', None)
        pg = self.open(Mock(status=st)); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-notice').count(), 0)
    def test_four_lanes_fit_one_row_on_a_tablet(self):
        pg = self.open(Mock(), width=640); pg.wait_for_selector('.vt-lane')
        tops = pg.evaluate("[...document.querySelectorAll('.vt-lane')].map(l=>Math.round(l.getBoundingClientRect().top))")
        self.assertEqual(len(set(tops)), 1, tops)
        # two buttons side by side, labelled in full, inside their lane; no nozzle button
        self.assertEqual(pg.locator('.vt-lane[data-slot="0"] .vt-change').inner_text(), 'Swap Tool')
        self.assertEqual(pg.locator('.vt-pidbtn').count(), 0)
        self.assertEqual(pg.locator('.vt-nozzle').count(), 0)
        over = pg.evaluate("[...document.querySelectorAll('.vt-lane')].some(l=>[...l.querySelectorAll('button')].some(b=>b.getBoundingClientRect().right>l.getBoundingClientRect().right+1||b.scrollWidth>b.clientWidth+1))")
        self.assertFalse(over)
    def test_lanes_wrap_on_a_phone(self):
        pg = self.open(Mock(), width=360); pg.wait_for_selector('.vt-lane')
        tops = pg.evaluate("[...document.querySelectorAll('.vt-lane')].map(l=>Math.round(l.getBoundingClientRect().top))")
        self.assertGreater(len(set(tops)), 1)
    def test_stale_pid_is_grey(self):
        st = vt_status()
        st['toolheads']['LA']['pid'] = {'calibrated': True, 'target': 210.0, 'at': 'x', 'source': 'calibrated', 'stale': True, 'profiles': []}
        pg = self.open(Mock(status=st)); pg.wait_for_selector('.vt-lane')
        self.assertIn('PID 210 \u00b0C (old nozzle)', pg.locator('.vt-lane[data-slot="0"]').inner_text())
        self.assertEqual(pg.locator('.vt-lane[data-slot="0"] .vt-pid.vt-dim').count(), 1)
        self.assertEqual(pg.locator('.vt-lane[data-slot="0"] .vt-pid.vt-ok').count(), 0)
    def boards_status(self):
        return vt_status(boards={'available': True, 'slots': ['a', 'b', 'c', 'd'], 'unknown': []})
    def test_swap_tool_button_toggles_and_opens_a_local_panel(self):
        m = Mock(status=self.boards_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-swap').count(), 0)
        pg.locator('.vt-change').nth(2).click()
        pg.wait_for_selector('.vt-swap')
        pg.wait_for_timeout(200)
        self.assertEqual(m.sent, [])                         # nothing sent yet
        self.assertEqual(pg.locator('.vt-change.vt-on').count(), 1)
        self.assertEqual(pg.locator('.vt-change').nth(2).get_attribute('aria-pressed'), 'true')
        self.assertEqual(pg.locator('.vt-swap-slot').count(), 0)   # no second set of buttons
        # below the toolhead layout
        self.assertTrue(pg.evaluate("document.querySelector('.vt-lanes').compareDocumentPosition(document.querySelector('.vt-swap')) & Node.DOCUMENT_POSITION_FOLLOWING"))
    def test_swap_buttons_toggle_locally_and_continue_sends_one_command(self):
        m = Mock(status=self.boards_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-change').nth(2).click(); pg.wait_for_selector('.vt-swap')
        pg.locator('.vt-change').nth(1).click()              # add T1
        pg.locator('.vt-change').nth(2).click()              # remove T2
        pg.locator('.vt-change').nth(3).click()              # add T3
        self.assertEqual(pg.locator('.vt-change.vt-on').count(), 2)
        self.assertIn('on', pg.locator('.vt-swap-unload').inner_text())
        pg.locator('.vt-swap-unload').click()
        self.assertIn('off', pg.locator('.vt-swap-unload').inner_text())
        pg.wait_for_timeout(200)
        self.assertEqual(m.sent, [])
        pg.locator('.vt-swap-go').click()
        for _ in range(20):
            if m.sent: break
            pg.wait_for_timeout(100)
        self.assertEqual(m.sent, ['VT_UI PAGE=swap_apply SEL=1,3 UNLOAD=0 LOAD=0'])
        self.assertEqual(pg.locator('.vt-swap').count(), 0)
        self.assertEqual(pg.locator('.vt-change.vt-on').count(), 0)
    def test_load_filament_after_toggle_is_sent_and_remembered(self):
        m = Mock(status=self.boards_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-change').nth(2).click(); pg.wait_for_selector('.vt-swap')
        self.assertIn('off', pg.locator('.vt-swap-load').inner_text())
        pg.locator('.vt-swap-load').click()
        self.assertIn('on', pg.locator('.vt-swap-load').inner_text())
        pg.locator('.vt-swap-go').click()
        for _ in range(20):
            if m.sent: break
            pg.wait_for_timeout(100)
        self.assertEqual(m.sent, ['VT_UI PAGE=swap_apply SEL=2 UNLOAD=1 LOAD=1'])
        # the choice is remembered for the next swap
        pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-change').nth(1).click(); pg.wait_for_selector('.vt-swap')
        self.assertIn('on', pg.locator('.vt-swap-load').inner_text())
    def test_untoggling_the_last_button_closes_the_panel_and_cancel_clears_all(self):
        m = Mock(status=self.boards_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-change').nth(0).click(); pg.wait_for_selector('.vt-swap')
        pg.locator('.vt-change').nth(0).click()
        self.assertEqual(pg.locator('.vt-swap').count(), 0)
        pg.locator('.vt-change').nth(0).click(); pg.locator('.vt-change').nth(1).click()
        self.assertEqual(pg.locator('.vt-change.vt-on').count(), 2)
        pg.locator('.vt-swap-cancel').click()
        self.assertEqual(pg.locator('.vt-swap').count(), 0)
        self.assertEqual(pg.locator('.vt-change.vt-on').count(), 0)
        self.assertEqual(m.sent, [])
    def test_swap_panel_closes_when_printing_starts(self):
        m = Mock(status=self.boards_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-change').nth(1).click(); pg.wait_for_selector('.vt-swap')
        m.push({'print_stats': {'state': 'printing'}})
        pg.wait_for_function("!document.querySelector('.vt-swap')")
    def test_notice_can_offer_restart_firmware(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.push({'virtual_toolheads': {'notice': {'seq': 5, 'title': 'Swap toolheads', 'lines': ['Now power off and swap toolheads.'], 'restart': True}}})
        pg.wait_for_selector('.vt-notice-restart')
        pg.locator('.vt-notice-restart').click()
        for _ in range(20):
            if m.sent: break
            pg.wait_for_timeout(100)
        self.assertEqual(m.sent, ['VT_RESTART'])
    def cal_status(self, flow=True):
        st = vt_status(calibrate={'flow_possible': flow, 'pid_presets': [190, 200, 210, 220, 230, 250], 'pid_range': [150, 300]}, pid_temp=210.0)
        for lbl, n in (('LA', {}), ('RA', {'ofs': True, 'pid': True, 'flow': False}), ('RB', {'flow': True}), ('RC', {})):
            st['toolheads'][lbl]['needs'] = dict({'ofs': False, 'pid': False, 'flow': False}, **n)
        return st
    def test_calibrate_mode_relabels_the_buttons_and_preselects_what_needs_it(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-change').first.inner_text(), 'Swap Tool')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        pg.wait_for_timeout(200)
        self.assertEqual(m.sent, [])                                   # nothing sent
        self.assertEqual(pg.locator('.vt-change').first.inner_text(), 'Calibrate')
        on = pg.evaluate("[...document.querySelectorAll('.vt-change')].map(b=>b.classList.contains('vt-on'))")
        self.assertEqual(on, [False, True, True, False])               # RA and RB need something
        # defaults: offset, PID and flow on because a picked toolhead lacks each
        for k in ('ofs', 'pid', 'flow'):
            self.assertIn('on', pg.locator('.vt-cal-' + k).inner_text())
        self.assertEqual(pg.locator('.vt-cal-pt.vt-on').inner_text(), '210')
    def test_calibrate_buttons_toggle_and_defaults_follow_the_selection(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        pg.locator('.vt-change').nth(1).click()                         # drop RA: only RB (flow) is left
        self.assertIn('off', pg.locator('.vt-cal-ofs').inner_text())
        self.assertIn('off', pg.locator('.vt-cal-pid').inner_text())
        self.assertIn('on', pg.locator('.vt-cal-flow').inner_text())
        self.assertEqual(pg.locator('.vt-cal-pt').count(), 0)           # no temperature without PID
        pg.locator('.vt-cal-pid').click()                               # by hand: sticks
        pg.locator('.vt-change').nth(0).click()                         # add LA: defaults recomputed, PID stays as set
        self.assertIn('on', pg.locator('.vt-cal-pid').inner_text())
    def test_calibrate_start_sends_one_command(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        pg.locator('.vt-cal-flow').click()                              # flow off
        pg.locator('.vt-cal-pt').nth(3).click()                         # 220
        pg.wait_for_timeout(200)
        self.assertEqual(m.sent, [])
        pg.locator('.vt-cal-go').click()
        for _ in range(20):
            if m.sent: break
            pg.wait_for_timeout(100)
        self.assertEqual(m.sent, ['VT_CAL PAGE=prep SEL=1,2 OFS=1 PID=1 FLOW=0 PT=220'])
        self.assertEqual(pg.locator('.vt-calpanel').count(), 0)
        self.assertEqual(pg.locator('.vt-change').first.inner_text(), 'Swap Tool')   # labels return
    def test_calibrate_panel_explains_each_stage(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        txt = pg.locator('.vt-calpanel').inner_text()
        for s in ('Offset calibration finds the position', 'PID calibration tunes the heater',
                  'Dynamic Flow calibration finds the pressure advance', 'Order: PID, Dynamic Flow, then offset'):
            self.assertIn(s, txt)
        pg.locator('.vt-change').nth(1).click(); pg.locator('.vt-change').nth(2).click()
        self.assertNotIn('Turn on at least one', pg.locator('.vt-calpanel').inner_text())
        pg.locator('.vt-change').nth(0).click()
        self.assertIn('Turn on at least one', pg.locator('.vt-calpanel').inner_text())

    def menu_status(self, boards=True):
        st = self.cal_status()
        st['options'] = {'diameters': [0.2, 0.4, 0.6, 0.8], 'volume_types': ['standard', 'high_flow'], 'materials': ['stainless', 'hardened']}
        st['toolheads']['LA']['pid'] = {'calibrated': True, 'target': 210.0, 'at': '2026-10-01 10:00:00', 'source': 'calibrated', 'stale': False,
            'profiles': [{'temp': 210.0, 'at': '2026-10-01 10:00:00', 'stale': False, 'active': True}, {'temp': 230.0, 'at': '2026-10-02 10:00:00', 'stale': True, 'active': False}]}
        st['toolheads']['SP'] = {'slot': None, 'side': 'right', 'valid': False, 'nozzle': {'diameter': 0.4, 'volume_type': 'standard', 'material': 'stainless'}}
        st['backup_list'] = [{'name': 'vt-20261030-101010.json', 'when': '2026-10-30 10:10:10', 'summary': 'layout LA, RA, RB, RC'}]
        st['boards'] = {'available': boards, 'slots': ['AAAA', 'BBBB', None, None], 'unknown': [2], 'lines': ['T0: LA (board AAAA)', 'T2: unrecognised board CCCC']}
        return st
    def open_menu(self, st=None):
        m = Mock(status=st or self.menu_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-menu').click(); pg.wait_for_selector('.vt-menupanel')
        return m, pg
    def sent_after(self, m, pg):
        for _ in range(20):
            if m.sent: break
            pg.wait_for_timeout(100)
        return m.sent
    def test_menu_stays_in_the_card_and_closes(self):
        m, pg = self.open_menu()
        pg.wait_for_timeout(200)
        self.assertEqual(m.sent, [])
        self.assertEqual(pg.locator('.vt-mn-nozzle').count(), 1)
        self.assertEqual(pg.locator('.vt-mn-add').count(), 0)       # boards readable: no manual add
        self.assertEqual(pg.locator('.vt-mn-layout').count(), 0)
        pg.locator('.vt-menu').click()
        self.assertEqual(pg.locator('.vt-menupanel').count(), 0)
    def test_menu_calibrate_starts_the_calibrate_mode(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        self.assertEqual(pg.locator('.vt-menupanel').count(), 0)
        self.assertEqual(pg.locator('.vt-change').first.inner_text(), 'Calibrate')
        self.assertEqual(m.sent, [])
    def test_header_calibrate_only_when_something_needs_it(self):
        st = self.menu_status()
        for t in st['toolheads'].values(): t['needs'] = {'ofs': False, 'pid': False, 'flow': False}
        st['invalid_loaded'] = []
        m = Mock(status=st); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-calibrate').count(), 0)
        pg.locator('.vt-menu').click(); pg.locator('.vt-mn-calibrate').click()      # still reachable from the menu
        pg.wait_for_selector('.vt-calpanel')
        self.assertEqual(pg.locator('.vt-calibrate').count(), 1)                    # shown while the mode is on
        pg.locator('.vt-cal-cancel').click()
        self.assertEqual(pg.locator('.vt-calibrate').count(), 0)
        m.push({'virtual_toolheads': dict(st, toolheads=dict(st['toolheads'], RB=dict(st['toolheads']['RB'], needs={'ofs': False, 'pid': True, 'flow': False})))})
        pg.wait_for_selector('.vt-calibrate')
    def test_menu_without_boards_offers_add_and_layout(self):
        m, pg = self.open_menu(self.menu_status(boards=False))
        self.assertEqual(pg.locator('.vt-mn-add').count(), 1)
        pg.locator('.vt-mn-layout').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=layout'])
    def test_menu_without_module_support_keeps_the_dialog(self):
        st = self.menu_status(); del st['options']
        m = Mock(status=st); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-menu').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI'])
    def test_menu_excludes_swap_and_calibrate(self):
        m, pg = self.open_menu()
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        self.assertEqual(pg.locator('.vt-menupanel').count(), 0)
        pg.locator('.vt-menu').click(); pg.wait_for_selector('.vt-menupanel')
        self.assertEqual(pg.locator('.vt-calpanel').count(), 0)
        pg.locator('.vt-change').nth(0).click(); pg.wait_for_selector('.vt-swap')
        self.assertEqual(pg.locator('.vt-menupanel').count(), 0)
    def test_menu_nozzle_form_sends_one_command(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-nozzle').click()
        pg.locator('.vt-mn-th', has_text='RA').click()
        self.assertEqual(pg.locator('.vt-mn-d.vt-on').inner_text(), '0.6')
        self.assertEqual(pg.locator('.vt-mn-mode.vt-on').inner_text(), 'New nozzle fitted')
        pg.locator('.vt-mn-d', has_text='0.8').click()
        pg.locator('.vt-mn-m', has_text='stainless').click()
        pg.wait_for_timeout(200)
        self.assertEqual(m.sent, [])
        pg.locator('.vt-mn-apply').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=nozzle_apply CARD=1 TOOLHEAD=RA MODE=replace DIAMETER=0.8 VOLUME_TYPE=high_flow MATERIAL=stainless'])
        self.assertEqual(pg.locator('.vt-menupanel').count(), 0)
    BOX = "(s) => { const r = document.querySelector(s).getBoundingClientRect(); return [r.left, r.width]; }"
    def test_menu_toolhead_buttons_line_up_with_the_lane_buttons(self):
        m, pg = self.open_menu()
        pg.add_style_tag(content='.v-card__text{padding:16px}')       # as Vuetify has it
        pg.locator('.vt-mn-nozzle').click(); pg.wait_for_selector('.vt-mn-th')
        lanes = pg.evaluate("[...document.querySelectorAll('.vt-change')].map(b => { const r = b.getBoundingClientRect(); return [r.left, r.width]; })")
        picks = pg.evaluate("[...document.querySelectorAll('.vt-mn-th')].map(b => { const r = b.getBoundingClientRect(); return [r.left, r.width, r.top]; })")
        self.assertEqual(len(lanes), 4); self.assertEqual(len(picks), 5)
        for l, p in zip(lanes, picks[:4]):
            self.assertAlmostEqual(l[0], p[0], delta=1.5); self.assertAlmostEqual(l[1], p[1], delta=1.5)
        sp = picks[4]                                                    # the spare: new line, first column, lane width
        self.assertGreater(sp[2], picks[0][2] + 20)
        self.assertAlmostEqual(sp[0], lanes[0][0], delta=1.5); self.assertAlmostEqual(sp[1], lanes[0][1], delta=1.5)
    def test_main_menu_grid_has_two_fixed_columns_and_a_wide_calibrate(self):
        m, pg = self.open_menu()
        g = lambda sel: pg.evaluate("(s) => { const r = document.querySelector(s).getBoundingClientRect(); return [r.left, r.right, r.top, r.width]; }", sel)
        nz, pid, bd, bk, cal = g('.vt-mn-nozzle'), g('.vt-mn-pid'), g('.vt-mn-boards'), g('.vt-mn-backup'), g('.vt-mn-calibrate')
        self.assertAlmostEqual(nz[2], pid[2], delta=1); self.assertAlmostEqual(bd[2], bk[2], delta=1)
        self.assertLess(nz[2], bd[2]); self.assertLess(bd[2], cal[2])
        self.assertAlmostEqual(nz[0], bd[0], delta=1); self.assertAlmostEqual(pid[0], bk[0], delta=1)
        self.assertAlmostEqual(nz[3], bd[3], delta=1)
        self.assertAlmostEqual(cal[0], nz[0], delta=1); self.assertAlmostEqual(cal[1], pid[1], delta=1)
    def test_menu_nozzle_edit_needs_a_change(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-nozzle').click(); pg.locator('.vt-mn-th', has_text='LA').click()
        pg.locator('.vt-mn-mode', has_text='Correct').click()
        self.assertTrue(pg.locator('.vt-mn-apply').is_disabled())
        pg.locator('.vt-mn-d', has_text='0.6').click()
        self.assertFalse(pg.locator('.vt-mn-apply').is_disabled())
    def test_menu_pid_stored_and_new_temperatures(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-pid').click(); pg.locator('.vt-mn-th', has_text='LA').click()
        txt = pg.locator('.vt-menupanel').inner_text()
        self.assertIn('210 \u00b0C: calibrated', txt); self.assertIn('before the last nozzle change', txt)
        self.assertEqual(pg.locator('.vt-mn-pt.vt-on').inner_text(), '210 \u00b0C')
        pg.locator('.vt-mn-pt', has_text='230').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=pid_apply CARD=1 TOOLHEAD=LA TEMP=230'])
        m.sent.clear()
        pg.locator('.vt-menu').click(); pg.locator('.vt-mn-pid').click(); pg.locator('.vt-mn-th', has_text='LA').click()
        pg.locator('.vt-mn-pt-new', has_text='250').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_CAL PAGE=prep SEL=0 OFS=0 PID=1 FLOW=0 PT=250 DIRECT=1'])
    def test_menu_pid_spare_cannot_start_new(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-pid').click(); pg.locator('.vt-mn-th', has_text='SP (spare)').click()
        self.assertEqual(pg.locator('.vt-mn-pt-new').count(), 0)
    def test_menu_backup_actions_confirm_first(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-backup').click()
        pg.locator('.vt-mn-restore').click()
        self.assertIn('replaces all current toolhead data', pg.locator('.vt-menupanel').inner_text())
        pg.wait_for_timeout(200); self.assertEqual(m.sent, [])
        pg.locator('.vt-mn-cancel').click()
        pg.locator('.vt-mn-restore').click(); pg.locator('.vt-mn-confirm').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=restore_apply CARD=1 NAME=vt-20261030-101010.json'])
        m.sent.clear()
        pg.locator('.vt-menu').click(); pg.locator('.vt-mn-backup').click()
        pg.locator('.vt-mn-delete').click(); pg.locator('.vt-mn-confirm').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=backup_delete_apply CARD=1 NAME=vt-20261030-101010.json'])
        m.sent.clear()
        pg.locator('.vt-menu').click(); pg.locator('.vt-mn-backup').click()
        pg.locator('.vt-mn-backup-now').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=backup_apply CARD=1'])
    def test_menu_boards(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-boards').click()
        self.assertIn('T2: unrecognised board CCCC', pg.locator('.vt-menupanel').inner_text())
        pg.locator('.vt-mn-learn').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=serial_learn_apply CARD=1'])
        m.sent.clear()
        pg.locator('.vt-menu').click(); pg.locator('.vt-mn-boards').click(); pg.locator('.vt-mn-identify').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=serial'])     # the server's dialog
        m.sent.clear()
        pg.locator('.vt-menu').click(); pg.locator('.vt-mn-boards').click(); pg.locator('.vt-mn-forget').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=serial_forget_apply CARD=1'])
    def test_menu_add_spare(self):
        m, pg = self.open_menu()
        pg.locator('.vt-mn-boards').click(); pg.locator('.vt-mn-addspare').click()
        pg.locator('.vt-mn-side', has_text='Left').click(); pg.locator('.vt-mn-d', has_text='0.6').click()
        pg.locator('.vt-mn-add').click()
        self.assertEqual(self.sent_after(m, pg), ['VT_UI PAGE=add_apply CARD=1 SIDE=left DIAMETER=0.6 VOLUME_TYPE=standard MATERIAL=stainless'])
    def test_menu_closes_when_printing(self):
        m, pg = self.open_menu()
        m.push({'print_stats': {'state': 'printing'}})
        pg.wait_for_function("!document.querySelector('.vt-menupanel')")

    def test_card_announces_itself_and_opens_the_requested_panel(self):
        st = self.cal_status(); st['auto_calibrate'] = 111
        m = Mock(status=st); pg = self.open(m)
        pg.wait_for_selector('.vt-calpanel')
        self.assertIn('VT_CARD', self.sent_after(m, pg))
        self.assertEqual(m.sent.count('VT_CARD'), 1)
        pg.locator('.vt-cal-cancel').click()
        m.push({'virtual_toolheads': dict(st)})                       # same request again
        pg.wait_for_timeout(300)
        self.assertEqual(pg.locator('.vt-calpanel').count(), 0)      # handled once
        m.push({'virtual_toolheads': dict(st, auto_calibrate=222)})  # a new request
        pg.wait_for_selector('.vt-calpanel')
    def test_requested_panel_waits_while_printing(self):
        st = self.cal_status(); st['auto_calibrate'] = 333
        m = Mock(status=st); pg = self.open(m); pg.wait_for_selector('.vt-calpanel')
        pg.locator('.vt-cal-cancel').click()
        m.push({'print_stats': {'state': 'printing'}})
        m.push({'virtual_toolheads': dict(st, auto_calibrate=444)})
        pg.wait_for_timeout(300)
        self.assertEqual(pg.locator('.vt-calpanel').count(), 0)
    def test_no_announcement_from_an_older_module(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.wait_for_timeout(300)
        self.assertNotIn('VT_CARD', m.sent)
    def test_calibrate_cancel_restores_the_buttons(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        pg.locator('.vt-cal-cancel').click()
        self.assertEqual(pg.locator('.vt-calpanel').count(), 0)
        self.assertEqual(pg.locator('.vt-change').first.inner_text(), 'Swap Tool')
        self.assertEqual(pg.locator('.vt-change.vt-on').count(), 0)
        # the header button toggles the mode too
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        pg.locator('.vt-calibrate').click()
        self.assertEqual(pg.locator('.vt-calpanel').count(), 0)
        self.assertEqual(m.sent, [])
    def test_start_needs_a_toolhead_and_a_stage(self):
        m = Mock(status=self.cal_status()); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        pg.locator('.vt-change').nth(1).click(); pg.locator('.vt-change').nth(2).click()   # nothing picked
        self.assertTrue(pg.locator('.vt-cal-go').is_disabled())
        pg.locator('.vt-change').nth(0).click()
        pg.locator('.vt-cal-ofs').click()                               # LA needs nothing: all stages off by default
        self.assertFalse(pg.locator('.vt-cal-go').is_disabled())
    def test_flow_button_is_hidden_when_flow_cannot_run(self):
        m = Mock(status=self.cal_status(flow=False)); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        self.assertEqual(pg.locator('.vt-cal-flow').count(), 0)
    def test_calibrate_mode_replaces_the_swap_panel_and_closes_when_printing(self):
        st = self.cal_status(); st['boards'] = {'available': True, 'slots': [], 'unknown': []}
        m = Mock(status=st); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-change').nth(0).click(); pg.wait_for_selector('.vt-swap')
        pg.locator('.vt-calibrate').click(); pg.wait_for_selector('.vt-calpanel')
        self.assertEqual(pg.locator('.vt-swap').count(), 0)
        m.push({'print_stats': {'state': 'printing'}})
        pg.wait_for_function("!document.querySelector('.vt-calpanel')")
    def test_active_tag_opens_the_dock_prompt(self):
        st = vt_status()
        m = Mock(status=st); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-dock').count(), 1)
        pg.locator('.vt-dock').click()
        for _ in range(20):
            if any('PAGE=dock' in s for s in m.sent): break
            pg.wait_for_timeout(100)
        self.assertIn('VT_UI PAGE=dock DIRECT=1', m.sent)
        m.push({'print_stats': {'state': 'printing'}})
        pg.wait_for_function("document.querySelector('.vt-dock').disabled")
        self.assertEqual(pg.locator('.vt-notice.vt-printing').inner_text(), 'Controls are disabled while printing.')
    def test_material_names(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertIn('Stainless steel', pg.locator('.vt-lane[data-slot="0"]').inner_text())
        self.assertIn('Hardened steel', pg.locator('.vt-lane[data-slot="1"]').inner_text())
        st = vt_status(); st['toolheads']['RB']['nozzle']['material'] = 'tungsten'
        pg2 = self.open(Mock(status=st)); pg2.wait_for_selector('.vt-lane')
        self.assertIn('tungsten', pg2.locator('.vt-lane[data-slot="2"]').inner_text())

    def test_active_highlight(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-active').count(), 1)
        self.assertEqual(pg.locator('.vt-active').get_attribute('data-slot'), '1')
        self.assertEqual(pg.locator('.vt-activetag').count(), 1)
    def test_calibrate_button_only_when_invalid(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-calibrate').count(), 1)
        m = Mock(status=vt_status(invalid_loaded=[])); pg2 = self.open(m); pg2.wait_for_selector('.vt-lane')
        self.assertEqual(pg2.locator('.vt-calibrate').count(), 0)
    def test_live_update(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.push({'virtual_toolheads': {'invalid_loaded': []}, 'extruder1': {'state': 'PARKED'}, 'extruder2': {'state': 'ACTIVATE'}})
        pg.wait_for_function("document.querySelector('.vt-active').dataset.slot==='2'")
        self.assertEqual(pg.locator('.vt-calibrate').count(), 0)
    def test_printing_disables(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.push({'print_stats': {'state': 'printing'}})
        pg.wait_for_selector('.vt-printing')
        self.assertTrue(pg.locator('.vt-menu').is_disabled())
        self.assertTrue(pg.locator('.vt-change').first.is_disabled())
    def test_not_ready_notice(self):
        pg = self.open(Mock(status=vt_status(ready=False, error='store broken'))); pg.wait_for_selector('.vt-unavail')
        self.assertIn('store broken', pg.locator('.vt-unavail').inner_text())
        self.assertEqual(pg.locator('.vt-lane').count(), 0)
    def test_collapse_persists(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-collapse svg').count(), 1)
        self.assertEqual(pg.locator('.vt-collapse').inner_text().strip(), '')
        pg.click('.vt-collapse'); self.assertEqual(pg.locator('.vt-lane').count(), 0)
        self.assertEqual(pg.evaluate("localStorage.getItem('vt-card-collapsed')"), '1')
    def test_narrow_no_overflow(self):
        pg = self.open(Mock(), width=360); pg.wait_for_selector('.vt-lane')
        self.assertTrue(pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
    def test_screenshots(self):
        os.makedirs('/tmp/vt_shots', exist_ok=True)
        for name, w in (('wide', 1280), ('narrow', 400)):
            pg = self.open(Mock(), width=w); pg.wait_for_selector('.vt-lane')
            pg.screenshot(path='/tmp/vt_shots/card_%s.png' % name)

class EditModeTests(Base):
    def test_edit_mode_shows_checkbox_not_lanes(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        pg.evaluate(EDIT_JS, True)
        pg.wait_for_selector('#vt-card[data-vt-mode=edit]')
        self.assertEqual(pg.locator('.vt-lane').count(), 0)
        self.assertEqual(pg.locator('#vt-card .v-input--checkbox').count(), 1)
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card').classList.contains('collapsed')"))
        self.assertTrue(pg.evaluate("document.querySelector('#vt-card .v-card__title').classList.contains('draggable')"))
        self.assertEqual(pg.locator('#vt-card .font-weight-light').inner_text(), 'Toolheads')
        self.assertEqual(pg.locator('#vt-card .vt-menu').count(), 0)

    def test_leaving_edit_mode_restores_card(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        pg.evaluate(EDIT_JS, True); pg.wait_for_selector('#vt-card[data-vt-mode=edit]')
        pg.evaluate(EDIT_JS, False); pg.wait_for_selector('.vt-lane')
        self.assertFalse(pg.evaluate("document.querySelector('#vt-card').classList.contains('collapsed')"))

    def test_checkbox_hides_and_restores(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        pg.evaluate(EDIT_JS, True); pg.wait_for_selector('#vt-card[data-vt-mode=edit]')
        pg.click('#vt-card .v-input--checkbox')
        self.assertEqual(pg.get_attribute('#vt-card .v-input--checkbox', 'aria-checked'), 'false')
        pg.evaluate(EDIT_JS, False); pg.wait_for_timeout(400)
        self.assertEqual(pg.locator('#vt-card').count(), 0)
        pg.evaluate(EDIT_JS, True); pg.wait_for_selector('#vt-card[data-vt-mode=edit]')
        pg.click('#vt-card .v-input--checkbox')
        pg.evaluate(EDIT_JS, False); pg.wait_for_selector('.vt-lane')

    def test_misplaced_card_returns_after_anchor(self):
        pg = self.open(Mock(), cards=('Spoolman', 'Printer Status', 'Fans')); pg.wait_for_selector('.vt-lane')
        pg.evaluate("document.getElementById('app').appendChild(document.getElementById('vt-card'))")
        pg.wait_for_function("document.getElementById('vt-card').previousElementSibling.textContent.includes('Spoolman')", timeout=3000)

class ParkTests(Base):
    """The extruder's own state decides which lane is active."""
    def active(self, pg):
        return pg.locator('.vt-lane.vt-active').count()
    def test_active_follows_extruder_state(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertEqual(self.active(pg), 1)
        pg.wait_for_selector('.vt-lane[data-slot="1"].vt-active')
    def test_fresh_boot_with_everything_parked_shows_none(self):
        # Klipper still names toolhead.extruder = extruder1 here
        st = {'extruder': 'PARKED', 'extruder1': 'PARKED', 'extruder2': 'PARKED', 'extruder3': 'PARKED'}
        pg = self.open(Mock(states=st)); pg.wait_for_selector('.vt-lane'); pg.wait_for_timeout(200)
        self.assertEqual(self.active(pg), 0)
        self.assertEqual(pg.locator('.vt-activetag').count(), 0)
    def test_parking_and_picking_update_live(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.push({'extruder1': {'state': 'PARKED'}})
        pg.wait_for_function("() => !document.querySelector('.vt-lane.vt-active')")
        m.push({'extruder3': {'state': 'ACTIVATE'}})
        pg.wait_for_selector('.vt-lane[data-slot="3"].vt-active')
        self.assertEqual(self.active(pg), 1)
    def test_toolhead_extruder_alone_does_not_activate(self):
        st = {'extruder': 'PARKED', 'extruder1': 'PARKED', 'extruder2': 'PARKED', 'extruder3': 'PARKED'}
        m = Mock(states=st); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.push({'toolhead': {'extruder': 'extruder2'}}); pg.wait_for_timeout(200)
        self.assertEqual(self.active(pg), 0)
    # ---- firmware without extruder states: console fallback
    def test_legacy_parking_removes_the_highlight(self):
        m = Mock(legacy=True); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertEqual(self.active(pg), 1)
        m.console('// park extruder1 !!!')
        pg.wait_for_function("() => !document.querySelector('.vt-lane.vt-active')")
    def test_legacy_picking_another_toolhead_activates_it(self):
        m = Mock(legacy=True); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.console('// park extruder1 !!!')
        pg.wait_for_function("() => !document.querySelector('.vt-lane.vt-active')")
        m.push({'toolhead': {'extruder': 'extruder2'}})
        pg.wait_for_selector('.vt-lane[data-slot="2"].vt-active')
    def test_legacy_reload_replays_the_console(self):
        m = Mock(legacy=True); m.history = ['ok', '// park extruder1 !!!']
        pg = self.open(m); pg.wait_for_selector('.vt-lane'); pg.wait_for_timeout(200)
        self.assertEqual(self.active(pg), 0)

class RenameTests(Base):
    def edit(self, m=None, slot=1, text=None, key='Enter'):
        m = m or Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-lane[data-slot="%d"] .vt-name' % slot).click()
        pg.wait_for_selector('.vt-name-input')
        if text is not None:
            pg.fill('.vt-name-input', text)
        if key: pg.press('.vt-name-input', key)
        pg.wait_for_timeout(200)
        return m, pg

    def test_name_is_a_button_and_no_side_tags(self):
        pg = self.open(Mock()); pg.wait_for_selector('.vt-lane')
        self.assertEqual(pg.locator('.vt-name').count(), 4)
        self.assertEqual(pg.locator('.vt-name').nth(1).inner_text(), 'RA')
        self.assertNotIn('slot', pg.locator('#vt-card').inner_text().lower())

    def test_click_shows_prefilled_focused_input(self):
        m, pg = self.edit(key=None)
        self.assertEqual(pg.input_value('.vt-name-input'), 'RA')
        self.assertTrue(pg.evaluate("document.activeElement.classList.contains('vt-name-input')"))

    def test_enter_sends_rename(self):
        m, pg = self.edit(text='Prusa-1')
        self.assertEqual(m.sent, ['VT_RENAME TOOLHEAD=RA LABEL=Prusa-1'])
        self.assertEqual(pg.locator('.vt-name-input').count(), 0)

    def test_unchanged_or_escape_sends_nothing(self):
        m, pg = self.edit(text='RA'); self.assertEqual(m.sent, [])
        m, pg = self.edit(text='zzz', key='Escape'); self.assertEqual(m.sent, [])
        self.assertEqual(pg.locator('.vt-name-input').count(), 0)
        self.assertEqual(pg.locator('.vt-name').nth(1).inner_text(), 'RA')

    def test_invalid_names_not_sent(self):
        for bad in ('has space', 'a;M112', '', 'bad!'):
            m, pg = self.edit(text=bad)
            self.assertEqual(m.sent, [], bad)
            self.assertEqual(pg.locator('.vt-note.vt-err').count(), 1, bad)
            self.assertEqual(pg.locator('.vt-name-input').count(), 1, bad)

    def test_duplicate_name_rejected_case_insensitively(self):
        m, pg = self.edit(text='rb')
        self.assertEqual(m.sent, [])
        self.assertIn('already used', pg.locator('.vt-note').inner_text())

    def test_edit_survives_status_push(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-lane[data-slot="2"] .vt-name').click()
        pg.fill('.vt-name-input', 'Half')
        m.push({'extruder1': {'state': 'PARKED'}, 'extruder3': {'state': 'ACTIVATE'}})
        pg.wait_for_function("document.querySelector('.vt-active').dataset.slot==='3'")
        self.assertEqual(pg.input_value('.vt-name-input'), 'Half')

    def test_rename_disabled_while_printing(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        m.push({'print_stats': {'state': 'printing'}}); pg.wait_for_selector('.vt-printing')
        self.assertTrue(pg.locator('.vt-name').first.is_disabled())

    def test_new_name_appears_after_status_update(self):
        m, pg = self.edit(text='Fresh')
        st = vt_status(); st['layout'][1] = 'Fresh'
        st['toolheads']['Fresh'] = st['toolheads'].pop('RA')
        m.push({'virtual_toolheads': st})
        pg.wait_for_function("document.querySelectorAll('.vt-name')[1].textContent==='Fresh'")

class ActionTests(Base):
    def run_btn(self, sel, nth=0):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator(sel).nth(nth).click(); pg.wait_for_timeout(200); return m, pg
    def test_menu(self):
        m, _ = self.run_btn('.vt-menu'); self.assertEqual(m.sent, ['VT_UI'])
    def test_calibrate(self):
        m, _ = self.run_btn('.vt-calibrate'); self.assertEqual(m.sent, ['VT_CALIBRATE DIRECT=1'])
    def test_change(self):
        m, _ = self.run_btn('.vt-change', 2)
        self.assertEqual(m.sent, ['VT_UI PAGE=layout_slot SLOT=2 PENDING=LA,RA,RB,RC DIRECT=1'])
    def test_bad_label_never_sent(self):
        st = vt_status(); st['layout'] = ['LA', 'R A;M112', 'RB', 'RC']
        st['toolheads']['R A;M112'] = st['toolheads']['RA']
        m = Mock(status=st); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        self.assertTrue(pg.locator('.vt-change').nth(1).is_disabled())
    def test_command_errors_are_shown(self):
        m = Mock(); m.fail_gcode = True; pg = self.open(m); pg.wait_for_selector('.vt-lane')
        pg.locator('.vt-menu').click(); pg.wait_for_selector('.vt-note.vt-err')
        self.assertIn('boom', pg.locator('.vt-note').inner_text())
        self.assertEqual(pg.locator('.vt-lane').count(), 4)

class ConnectionTests(Base):
    def test_klipper_shutdown_then_ready(self):
        m = Mock(); pg = self.open(m); pg.wait_for_selector('.vt-lane')
        for c in m.conns: c.send(json.dumps({'method': 'notify_klippy_shutdown', 'params': []}))
        pg.wait_for_selector('.vt-unavail')
        for c in m.conns: c.send(json.dumps({'method': 'notify_klippy_ready', 'params': []}))
        pg.wait_for_selector('.vt-lane')

if __name__ == '__main__':
    unittest.main()
