"""W3 citation repros and adversarial controls from JCODE / amended TIME §5."""
import copy
import unittest
from visioncore import cite, ids, layout, pack
from tests.test_cite import make_vision, make_pack, fail, FROZEN_T, FLICK_T, CHG_T, MOVE_T, M


class CitationSealTests(unittest.TestCase):
    def setUp(self):
        self.v, self.regions, self.kid = make_vision()
        self.pk = make_pack()
        self.pk['gops'] = ['g0']

    def verify(self, cid, pointer, reason, quote=None, pointers=None):
        return cite.verify_citations(self.v, {'results': [fail(cid, pointers or [pointer], reason, quote)]}, self.pk)

    def test_jc1_original_static_content_bypass_not_reproduced(self):
        for cid in (*cite.VISION_CHECKS, 'actor.teleport'):
            with self.subTest(cid=cid):
                self.assertEqual(self.verify(cid, 'f0:t0000', 'static_content')['rc'], 3)

    def test_jc1_reason_cannot_relabel_vision_check(self):
        positives = [('render.flicker', FLICK_T, 'flicker'), ('render.frozen_region', FROZEN_T, 'frozen'),
                     ('gui.scroll_stuck', FROZEN_T, 'frozen'), ('ui.overlap', CHG_T, 'overlap'),
                     ('actor.teleport', MOVE_T, 'teleport')]
        for cid, tile, reason in positives:
            with self.subTest(cid=cid):
                self.assertEqual(self.verify(cid, ids.field_id(1, M(*tile)), reason)['rc'], 0)
                bad_tile, bad_reason = (CHG_T, 'missing') if reason != 'overlap' else (FROZEN_T, 'frozen')
                self.assertEqual(self.verify(cid, ids.field_id(1, M(*bad_tile)), bad_reason)['rc'], 3)
        drift = copy.deepcopy(self.v)
        drift['regions']['r1:0000'] = {'id': 'r1:0000', 'frame': 1, 'cls': 'DRIFT', 'spans': [['0000','0000']]}
        self.v = drift
        self.assertEqual(self.verify('ui.drift', 'r1:0000', 'drift')['rc'], 0)
        self.assertEqual(self.verify('ui.drift', ids.field_id(1, M(*CHG_T)), 'missing')['rc'], 3)

    def test_jc3_wire_g0_not_reproduced(self):
        self.pk['frame_map'] = [[0, 0]]
        self.assertEqual(self.verify('render.flicker', 'g0', 'flicker')['rc'], 0)
        self.assertEqual(self.verify('render.flicker', self.kid, 'flicker')['rc'], 0)
        self.assertEqual(self.verify('legacy', 'g1', 'black')['rc'], 3)
        self.assertEqual(self.verify('render.flicker', ids.field_id(1, M(*FLICK_T)), 'flicker')['rc'], 3)

    def test_jc2_pack_quote_roundtrip(self):
        tr = self.v['gops'][0]['tracks'][0]
        tr.update(n_tiles_max=1, cls_mask=20, mv_mode=[2, 0])
        rid = tr['r_rep']
        self.v['regions'][rid].update(cls='CHANGED', bbox_px=[0,0,16,16])
        pr = pack.pack({'W': self.v['W'], 'H': self.v['H'], 'params_sha256': self.v['params_sha256'],
                        'gops': [self.v['gops'][0]], 'regions': self.v['regions']})
        self.pk.update(pr.pack)
        item = pr.pack['v3']['g'][0]['i'][0]
        self.assertEqual(item[8].split()[0], item[0])
        self.assertEqual(self.verify('render.flicker', item[0], 'flicker', item[8])['rc'], 0)
        q = cite.render_quote(rid, self.v, *ids.parse_any(rid))
        self.assertIn(' CHANGED', q)
        self.assertEqual(self.verify('legacy', rid, 'missing', q)['rc'], 0)
        multi = self.verify('render.flicker', item[0], 'flicker', item[8], [item[0], rid])
        self.assertEqual(multi['rc'], 0)
        self.assertEqual(multi['results'][0]['accepted_pointers'], [item[0], rid])
        self.assertEqual(self.verify('render.flicker', item[0], 'flicker', item[8] + ' forged', [item[0], rid])['rc'], 3)
        self.assertEqual(self.verify('render.flicker', item[0], 'flicker', q)['rc'], 3)

    def test_jc4_window_uses_exact_frames(self):
        self.v['gops'][0]['frame_ids'] = [0, 5, 10]
        win = cite.window_frames(self.v, self.pk)
        self.assertEqual(win.batch, {0,5,10})
        self.assertFalse(cite._in_window(self.v, 'track', ids.parse_any(self.kid)[1], win))


    def test_jc2_global_quote_roundtrip(self):
        pr = pack.pack({'W':self.v['W'],'H':self.v['H'],'params_sha256':self.v['params_sha256'],
                        'gops':[self.v['gops'][1]],'regions':[]})
        self.pk.update(pr.pack)
        item = pr.pack['v3']['g'][0]['i'][0]
        self.assertEqual(self.verify('legacy.black',item[0],'black',item[2])['rc'],0)
