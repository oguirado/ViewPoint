"""W4 V2 regression oracles; all stores and experiments use lane scratch."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from visioncore import canon, ids
from visioncore.errors import VqaError
from vmem import compact, ingest, receipts, router, units
from vmem.store import Store
from tests.test_vmem_units import SC, TK, anchor, mk, tile


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / 'build' / 'room_V2'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name


class TestLook(unittest.TestCase):
    def test_declared_tile_count_is_not_inferred_from_truncated_blob(self):
        a = {'grid': SC['grid'], 'n': 20, 'ah64': '1'*16, 'desc_blob': 'a'*64}
        b = dict(a, ah64='2'*16, desc_blob='b'*64)
        self.assertEqual(units.look_eq(a, b, lambda sha: tile(3, 9)), 'DIFFERENT')

    def test_partial_tile_is_not_silently_discarded(self):
        a = {'grid': SC['grid'], 'n': 1, 'ah64': '1'*16, 'desc_blob': 'a'*64}
        b = dict(a, ah64='2'*16, desc_blob='b'*64)
        self.assertEqual(units.look_eq(a, b, lambda sha: tile(3, 9) + b'x'), 'DIFFERENT')

    def test_exact_hash_does_not_override_different_block_sizes(self):
        a = {'grid': SC['grid'], 'n': 1, 'ah64': '1'*16, 'desc_blob': 'a'*64}
        self.assertEqual(units.look_eq(a, dict(a, n=2)), 'DIFFERENT')


class TestAnswers(Scratch):
    def seed(self, summary='baseline counter recorded correctly', render_index=None):
        a = dict(anchor(), render_index=render_index)
        u = mk(anchors=[a], pin='GOLDEN', verdict='PASS', summary=summary)
        st = Store(self.root)
        with st.lock():
            ingest.write_gen(st, [u], [])
        return u

    def test_answer_without_alignment_is_keyframe(self):
        self.seed()
        self.assertEqual(router.answer(self.root, q='counter')['granularity'], 'keyframe')

    def test_answer_with_alignment_is_frame(self):
        self.seed(render_index=84)
        self.assertEqual(router.answer(self.root, q='counter')['granularity'], 'frame')

    def test_composed_short_quote_abstains(self):
        self.seed(summary='counter ok')
        with self.assertRaises(VqaError) as err:
            router.answer(self.root, q='counter')
        self.assertEqual((err.exception.rc, err.exception.detail['letter']), (3, 'g'))

    def test_composed_unresolvable_citation_abstains(self):
        u = mk(pin='GOLDEN', verdict='PASS', anchors=[], summary='counter baseline recorded')
        st = Store(self.root)
        with st.lock():
            ingest.write_gen(st, [u], [])
        with self.assertRaises(VqaError) as err:
            router.answer(self.root, q='counter')
        self.assertEqual((err.exception.code, err.exception.detail.get('letter')),
                         ('NOT_IN_VISUAL_MEMORY', 'g'))

    def test_five_unrecorded_queries_abstain(self):
        self.seed()
        for q in ('unrecorded', 'volcano', 'spaceship', 'underwater', 'missinglabel'):
            with self.subTest(q=q), self.assertRaises(VqaError) as err:
                router.answer(self.root, q=q)
            self.assertEqual((err.exception.code, err.exception.rc), ('NOT_IN_VISUAL_MEMORY', 3))

    def test_fg_sha_stable_three_persisted_compactions(self):
        self.seed()
        from vmem import gates
        before = gates.verify(self.root)['fg_ids_sha256']
        for _ in range(3):
            st = Store(self.root)
            with st.lock():
                active = st.gen_units(st.current())
                plan = compact.compact(active)
                ingest.write_gen(st, plan['active'], plan['archive'])
            after = gates.verify(self.root)
            self.assertEqual(after['fg_ids_sha256'], before)
            self.assertEqual(gates.failed(after), [])


class TestJournal(Scratch):
    def test_short_append_is_complete_before_ack(self):
        j = receipts.Journal(self.root)
        write = os.write
        with mock.patch.object(receipts.os, 'write', side_effect=lambda fd, data: write(fd, data[:7])):
            rec = j.append('ingest_begin', {'marker': 'durable'})
        self.assertEqual(j.records(), [rec])
        self.assertTrue(j.verify_chain())

    def test_short_checkpoint_is_complete_before_unlink(self):
        j = receipts.Journal(self.root)
        j.append('flip', {'gen': 1})
        write = os.write
        with mock.patch.object(receipts.os, 'write', side_effect=lambda fd, data: write(fd, data[:7])):
            base = j.checkpoint(1)
        self.assertEqual(j.base(), base)
        self.assertTrue(j.verify_chain())

    def test_zero_write_refuses_ack(self):
        j = receipts.Journal(self.root)
        with mock.patch.object(receipts.os, 'write', return_value=0):
            with self.assertRaises(VqaError) as err:
                j.append('flip', {'gen': 1})
        self.assertEqual(err.exception.rc, 2)

    def test_cached_tail_avoids_historical_record_reads(self):
        j = receipts.Journal(self.root)
        j.append('flip', {'gen': 1})
        with mock.patch.object(j, 'records', side_effect=AssertionError('historical scan')):
            rec = j.append('flip', {'gen': 2})
        self.assertEqual(rec['seq'], 2)
        self.assertTrue(j.verify_chain())

    def test_other_writer_invalidates_cached_tail(self):
        j = receipts.Journal(self.root)
        j.append('flip', {'gen': 1})
        receipts.Journal(self.root).append('flip', {'gen': 2})
        self.assertEqual(j.append('flip', {'gen': 3})['seq'], 3)
        self.assertTrue(j.verify_chain())

    def test_recover_torn_invalidates_cached_tail(self):
        j = receipts.Journal(self.root)
        j.append('flip', {'gen': 1})
        with open(j.path(1), 'ab') as f:
            f.write(b'{broken')
        self.assertEqual(j.recover_torn(), len(b'{broken'))
        self.assertEqual(j.append('flip', {'gen': 2})['seq'], 2)
        self.assertTrue(j.verify_chain())


class TestTrackRegistry(unittest.TestCase):
    def test_registered_roles_roundtrip_at_supported_resolutions(self):
        for w, h in ((1280,720), (1920,1080), (3840,2160), (1024,1024)):
            for role in ('tile','ui','actor','a11y','island'):
                tk = units.track_key('demo', 'scene', w, h, role, 0, 63)
                self.assertEqual(ids.parse_any(tk)[0], 'track_key')

    def test_id_negative_return_codes(self):
        for value in ('f84:t0a4', 'f084:t0a40', 'f84:t0A40'):
            with self.subTest(value=value), self.assertRaises(VqaError) as err:
                ids.parse_any(value)
            self.assertEqual(err.exception.rc, 3)
        with self.assertRaises(VqaError) as err:
            ids.field_id(84, 65535, 1280, 720)
        self.assertEqual(err.exception.rc, 3)
        with self.assertRaises(VqaError) as err:
            ids.grid_ident(1, 70000)
        self.assertEqual(err.exception.rc, 2)

    def test_track_key_negative_inputs(self):
        for app, role, lo, hi in (('Bad','ui',0,63), ('demo','bogus',0,63), ('demo','ui',64,63)):
            with self.subTest(role=role, lo=lo), self.assertRaises(VqaError):
                units.track_key(app, 'scene', 1280, 720, role, lo, hi)


class TestMutationOracles(Scratch):
    def test_band_three_is_required(self):
        sig = (1 << 0) | (1 << 16) | (1 << 32)
        u = mk(kind='defect', pin='FAIL', verdict='FAIL', anchors=[dict(anchor(), sig64='%016x' % sig)])
        files = router.build_sig_index([u])
        view = mock.Mock()
        view.snap.read.side_effect = files.__getitem__
        self.assertEqual(router.sig_lookup(view, 0), [(3, u['id'])])

    def test_links_do_not_change_ranking(self):
        a = mk(1, summary='counter baseline', links=[])
        b = mk(2, summary='counter baseline', links=[{'rel':'same_defect','to':'V-DEF-000003'}])
        scores = router.bm25f([a, b], ['counter'])
        self.assertEqual(scores[a['id']], scores[b['id']])
        self.assertEqual(router.bm25f([b], ['defect']), {})

    def test_fg_hash_contract(self):
        import hashlib
        u = mk(pin='GOLDEN')
        expected = hashlib.sha256((u['id'] + '\n').encode()).hexdigest()
        self.assertEqual(compact.fg_ids_sha256([u]), expected)

    def test_rotation_does_not_wrap_u64(self):
        self.assertTrue(receipts.rotates((1 << 64)-1, 2, 10))

    def test_quote_eleven_is_rejected(self):
        u = mk(pin='GOLDEN', summary='counter ok!')
        st = Store(self.root)
        with st.lock():
            ingest.write_gen(st, [u], [])
        with self.assertRaises(VqaError):
            router.check_answer(self.root, {'citations':[{'unit_id':u['id'], 'cite':u['anchors'][0]['cite'], 'quote':u['summary']}]})

    def test_actual_directory_flush_order(self):
        import stat
        events = []
        real_fsync, real_rename, real_replace = os.fsync, os.rename, os.replace
        def sync(fd):
            events.append(('dir' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file',
                           os.readlink('/proc/self/fd/%d' % fd)))
            return real_fsync(fd)
        def rename(a,b):
            events.append(('rename', str(b)))
            return real_rename(a,b)
        def replace(a,b):
            events.append(('replace', str(b)))
            return real_replace(a,b)
        st = Store(self.root)
        with mock.patch('os.fsync', side_effect=sync), mock.patch('os.rename', side_effect=rename), mock.patch('os.replace', side_effect=replace):
            with st.lock():
                st.commit({'units/active.jsonl': b''})
        rename_i = next(i for i,e in enumerate(events) if e[0]=='rename')
        replace_i = next(i for i,e in enumerate(events) if e[0]=='replace')
        self.assertTrue(any(k=='file' for k,p in events[:rename_i]))
        self.assertTrue(any(k=='dir' and p.endswith('.tmp') for k,p in events[:rename_i]))
        self.assertTrue(any(k=='dir' and p==os.path.join(self.root,'gen') for k,p in events[rename_i:replace_i]))
        self.assertTrue(any(k=='dir' and p==self.root for k,p in events[replace_i:]))
        self.assertTrue(any(k=='file' and '/journal/' in p for k,p in events[replace_i:]))


class TestRecordOracles(Scratch):
    def test_fail_gop_is_compaction_root(self):
        a = dict(anchor(), gop_blob='a'*64)
        u = mk(kind='episode', verdict='FAIL', pin='FAIL', anchors=[a])
        self.assertIn(a['gop_blob'], units.blob_refs(u))
        self.assertIn(a['gop_blob'], compact.compact([u])['live_blobs'])

    def test_schema_gate_checks_complete_record(self):
        from vmem import gates
        u = mk()
        del u['value_den']
        st = Store(self.root)
        with st.lock():
            st.commit({'units/active.jsonl': canon.dumps(u).encode()}, strict=False)
        self.assertEqual(gates.verify(self.root)['gates']['g_schema'], 5)

    def test_pin_persists_catalogued(self):
        u = mk()
        st = Store(self.root)
        with st.lock():
            ingest.write_gen(st, [u], [])
        receipt = Path(self.root) / 'receipt.txt'
        receipt.write_text('operator catalogue admission')
        ingest.pin_run(self.root, u['id'], str(receipt))
        self.assertEqual(Store(self.root).gen_units(Store(self.root).current())[0]['pin'], 'CATALOGUED')
