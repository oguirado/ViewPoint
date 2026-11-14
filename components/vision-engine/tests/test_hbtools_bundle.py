"""HBT2: neutral producer boundary, byte roundtrips, foreign-language texts, rejection controls."""
import base64
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hbtools_v2'))
import hb_lib_bundle as SB
import hb_extract2 as X
import hb_build as B
import hb_index as I
import hb_scaffold2 as SC
import hblib2 as L
import mini_hb
import schemalib


class Bundle(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / 'build' / 'room_P'
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'TMPDIR': str(scratch)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def python_bundle(self):
        subject = ROOT / 'tests/fixtures/hbv2/graph'
        return SB.emit(X.extract(str(subject)), str(subject), SB.LEGACY_FIXTURE_ID)

    def test_all_sealed_books_default_and_python_bundle_match_prechange_bytes(self):
        # Measured from the pre-HBT2 tools, including manifest + router inputs.
        # CARD_D01F: only HB-VC-a0-arch is re-anchored for EQUIV_RECEIPT.json.
        # CARD_D02F: S2 re-anchored for eval/results/rt0d02/EQUIV_RECEIPT.json (RT0D02).
        expected = {'HB-VC-a0-arch': 'ae354d0a3aa7ddab3cfcc77096eff2e57c0f3bbf03d77ada2b43937e41e234d8', 'HB-VC-s0-grid': '5d994c71e4cc974607a5fffa74b4946a83dd55e852d50ea215a27ae42dd21f28', 'HB-VC-s1-desc': 'a004fdbef17f221a1e88c49fb83e9b37aa45a10ebc9cce99a7aa0d6f79ed63fc', 'HB-VC-s2-change': '57afecb09699b67e4578f3428c14915897df7a23b79691436afde01e57ead496', 'HB-VC-s3-pyramid': 'ad0a375fa3315207ab245674179392138fb8e7eb1c4c8a0a62599dbb4b04d9cb', 'HB-VC-s4-regions': '1bbcb4e83e0a1eec519cc112ea23d2677e4ec744901a4c20bcd68e3b043c639a', 'HB-VC-s5-gop': '785250b1e2091bd3fd0288831aff71dcc0443a2d0f34fcec63f81b93e7eecf93', 'HB-VC-s6-pack-cite': '6f2195cb17464574a9489ba61bd7707bbf241a548d9f1969047f7967d15d3d0f', 'HB-VC-s7-vmem': '622bc0a22654de6b38bd02fcc267e1cb2eeac5e485b6721b5ab65caeabb0f785', 'HB-VN-s0s1': '906bcd8c3b697e1620da6f2738921340db37b26d9adfec35f3fa58adf1f40b7f'}
        files = ['out/symbols_raw.jsonl','manifest.json','book.md','symbols.jsonl','nodes.jsonl','anchors.jsonl','graph.json',
                 'index/cols.json','index/post.json','index/vocab.json','index/ID']
        for name, want in expected.items():
            with self.subTest(book=name):
                src=ROOT/'hyperbooks'/name; dst=self.root/name
                shutil.copytree(src,dst,ignore=shutil.ignore_patterns('__pycache__'))
                cfg=L.jload(str(src/'seed/slots.json'))
                scope=cfg.get('env',{}).get('HBV2_SRC_DIRS') or ','.join(cfg['subject'])
                env={k:v for k,v in os.environ.items() if not k.startswith('HBV2_')}
                env.update(HBV2_ROOT=str(dst),HBV2_BOOK_SUB='.',HBV2_SUBJECT=str(ROOT),HBV2_SRC_DIRS=scope,
                           PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(ROOT/'build/room_P'))
                for flag in (False,True):
                    cmds=[['hb_extract2.py','--out',str(dst/'out')],['hb_build.py','--render-derived'],['hb_index.py']]
                    for cmd in cmds:
                        if flag:cmd+=['--symbols-bundle',str(dst/'out/symbols_bundle.json')]
                        proc=subprocess.run([sys.executable,str(ROOT/'hbtools_v2'/cmd[0])]+cmd[1:],env=env,capture_output=True,text=True,timeout=180)
                        self.assertEqual(proc.returncode,0,cmd[0]+proc.stderr[-500:])
                    hashes={f:L.sha_file(str(dst/f)) for f in files}
                    self.assertEqual(L.sha256b(L.canon(hashes)),want,('bundle' if flag else 'default')+' '+name)

    def test_python_bundle_preserves_raw_anchors_edges_and_texts(self):
        b = self.python_bundle()
        x = SB.decode(b)
        original = X.extract(b['root'])
        self.assertEqual(L.canon(x['symbols']), L.canon(original['symbols']))
        self.assertEqual(L.canon(x['anchors']), L.canon(original['anchors']))
        self.assertEqual(x['edges'], original['edges'])
        self.assertEqual(x['texts'], X.symbol_texts(original['repo']))

    def test_bundle_is_only_source_and_roundtrips_build_index(self):
        dst = self.root / 'mini'
        mini_hb.make_mini(str(ROOT / 'tests/fixtures/hbv2/graph'), str(dst))
        b = SB.emit(X.extract(str(dst/'subject')), str(dst/'subject'), SB.LEGACY_FIXTURE_ID)
        path = self.root/'bundle.json'
        L.write_json(str(path), b)
        files = ['book.md','symbols.jsonl','nodes.jsonl','anchors.jsonl','graph.json','claims.jsonl','manifest.json',
                 'index/cols.json','index/post.json','index/vocab.json','index/ID']
        before = {f:(dst/'book'/f).read_bytes() for f in files}
        shutil.rmtree(dst/'subject')
        with mock.patch.object(X, 'extract', side_effect=AssertionError('parser used')), mock.patch.object(X, 'Repo', side_effect=AssertionError('parser used')):
            B.build_all(str(dst), symbols_bundle=str(path))
            I.build_index(str(dst), symbols_bundle=str(path))
        self.assertEqual(before, {f:(dst/'book'/f).read_bytes() for f in files})

    def go_bundle(self):
        ids = ['tools/tiny.go::'+s for s in ('Alpha','Beta','Gamma')]
        return dict(schema=SB.SCHEMA,hb_id='HB-GO-001',root='fixture',lang_default='go',
                    files=[dict(path='tools/tiny.go',lang='go',sha256=L.sha256b(b'package tiny\n'))],
                    symbols=[dict(id=s,path='tools/tiny.go',name=s.split('::')[1],kind='function',span=[i+1,i+1],
                                  lang='go',signature='func '+s.split('::')[1]+'() string',signature_sha256=L.sha256s(s)) for i,s in enumerate(ids)],
                    anchors=[],edges=[dict(src=ids[0],dst=ids[1],kind='CALLS'),dict(src=ids[1],dst=ids[2],kind='CALLS')],
                    texts={s:dict(doc='documentation nebula',com='comment quasar',lit='literal pulsar') for s in ids})

    def test_go_book_symbols_edges_lang_and_all_text_columns(self):
        b = self.go_bundle()
        p = self.root/'go.json'
        L.write_json(str(p), b)
        book = self.root/'book'
        book.mkdir()
        slots = { 'hb_id': b['hb_id'], 'sections':{ 'hb-s%d'%n:dict(title='Go',purpose='Go symbols',prose='Go documentation',anchors=[s['id'] for s in b['symbols']] if n==0 else []) for n in range(8)},'modules':{},'claims':[] }
        (book/'book.md').write_text(SC.scaffold(slots))
        with mock.patch.object(X,'Repo',side_effect=AssertionError('Go parsed as Python')):
            _,d = B.build_all(str(self.root),symbols_bundle=str(p))
            I.build_index(str(self.root),symbols_bundle=str(p))
        self.assertFalse(d['issues'])
        self.assertEqual(len(d['symbols']),3)
        self.assertEqual({a['lang'] for a in d['anchors']},{'go'})
        self.assertEqual(L.jload(str(book/'manifest.json'))['hb_id'],b['hb_id'])
        cols = L.jload(str(book/'index/cols.json'))
        for field in ('doc','com','lit'):
            self.assertTrue(all(n>0 for n in cols['len'][field]),field)
        g = X.Graph(d['graph'])
        self.assertEqual(sum(1 for k in d['graph']['fwd']['etype'] if g.etypes[k]=='CALLS'),2)

    def test_go_bundle_anchors_validate_against_standard_schema(self):
        b = self.go_bundle()
        source = b'package tiny\n'
        # Explicit producer evidence for this fixture; no Python AST or absent
        # evidence is substituted for a Go producer's hashes/byte spans.
        for s in b['symbols']:
            name = s['name']
            fragment = ('func %s() string { return "%s" }\n' % (name, name)).encode()
            line = source.count(b'\n') + 1
            s['span'] = [line, line]
            s['signature_sha256'] = L.sha256s(s['signature'])
            producer_ast = dict(kind='function', name=name, result='string',
                                body=[dict(kind='return', string=name)])
            b['anchors'].append(dict(
                schema='hb.anchor.v2', id=s['id'], path=s['path'], symbol=name,
                role='symbol', kind=s['kind'], lang=s['lang'],
                span=dict(start_line=line, end_line=line),
                start_byte=len(source), end_byte=len(source) + len(fragment),
                sha256=L.sha256b(fragment), sha256_ast=L.sha256b(L.canon(producer_ast)),
                doc_sha256=L.sha256s(b['texts'][s['id']]['doc']),
                signature_sha256=s['signature_sha256'], status='active', redirect_to=None))
            source += fragment
        b['files'][0].update(source_b64=base64.b64encode(source).decode(), sha256=L.sha256b(source))
        path = self.root / 'go.json'
        L.write_json(str(path), b)
        book = self.root / 'book'
        book.mkdir()
        (book / 'book.md').write_text(SC.scaffold(dict(
            hb_id=b['hb_id'], sections={'hb-s%d' % n: dict(
                title='Go', purpose='Go', prose='Go',
                anchors=[s['id'] for s in b['symbols']] if n == 0 else [])
                for n in range(8)}, modules={}, claims=[])))
        with mock.patch.object(X, 'Repo', side_effect=AssertionError('Go parsed as Python')):
            _, built = B.build_all(str(self.root), symbols_bundle=str(path))
        anchors = [json.loads(line) for line in (book / 'anchors.jsonl').read_text().splitlines()]
        self.assertEqual(len(anchors), len(b['symbols']))
        self.assertEqual(anchors, built['anchors'])
        schema_dir = str(ROOT / 'standards/schemas')
        for anchor in anchors:
            self.assertEqual(anchor['lang'], 'go')
            self.assertEqual(schemalib.validate(anchor, 'hb.anchor.v2', schema_dir), [])
        # All previous languages remain valid; the newly supported languages
        # share the same standard anchor contract. Unknown languages fail.
        for lang in ('python', 'gdscript', 'markdown', 'json', 'other', 'go',
                     'rust', 'typescript', 'javascript', 'c', 'cpp', 'wasm'):
            with self.subTest(lang=lang):
                self.assertEqual(schemalib.validate(dict(anchors[0], lang=lang), 'hb.anchor.v2', schema_dir), [])
        self.assertTrue(schemalib.validate(dict(anchors[0], lang='unknown'), 'hb.anchor.v2', schema_dir))

    def test_foreign_source_never_runs_python_subanchor_parser(self):
        b=self.go_bundle()
        data=b'package tiny\n'+b'//comment\n'*300
        b['files'][0].update(source_b64=base64.b64encode(data).decode(),sha256=L.sha256b(data))
        first=b['symbols'][0];first['span']=[1,301]
        b['anchors']=[dict(schema='hb.anchor.v2',id=first['id'],path=first['path'],symbol=first['name'],role='symbol',
                           kind='function',lang='go',span=dict(start_line=1,end_line=301),start_byte=0,end_byte=len(data),
                           sha256=L.sha256b(data),signature_sha256=first['signature_sha256'])]
        p=self.root/'go.json';L.write_json(str(p),b)
        book=self.root/'book';book.mkdir()
        (book/'book.md').write_text(SC.scaffold(dict(hb_id=b['hb_id'],sections={'hb-s%d'%n:dict(title='Go',purpose='Go',prose='Go',anchors=[s['id'] for s in b['symbols']] if n==0 else []) for n in range(8)},modules={},claims=[])))
        with mock.patch.object(B.P,'subanchor',side_effect=AssertionError('foreign source parsed')):
            _,d=B.build_all(str(self.root),symbols_bundle=str(p))
        self.assertEqual(len(d['symbols']),3)

    def test_invalid_bundles_rejected(self):
        b = self.go_bundle()
        changes = [lambda x:x.update(schema='wrong'),lambda x:x['symbols'].append(copy.deepcopy(x['symbols'][0])),
                   lambda x:x['edges'][0].update(dst='missing'),lambda x:x['symbols'][0].update(lang='python'),
                   lambda x:x['symbols'][0].update(span=[3,1]),lambda x:x['texts'].pop(next(iter(x['texts']))),
                   lambda x:x['files'][0].update(source_b64='Zm9v'),lambda x:x['symbols'][0].update(kind='unknown'),lambda x:x.update(lang_default='BadLanguage')]
        for change in changes:
            mutated=copy.deepcopy(b);change(mutated)
            with self.subTest(change=changes.index(change)),self.assertRaises(ValueError): SB.decode(mutated)

    def test_corrupt_python_evidence_rejected(self):
        b=self.python_bundle()
        for change in (lambda x:x['symbols'][0]['legacy'].update(sha256='0'*64),
                       lambda x:x['symbols'][0].update(legacy={}),
                       lambda x:x['extraction']['stats'].update(calls=-1),
                       lambda x:x['anchors'][0].update(signature_sha256='0'*64)):
            bad=copy.deepcopy(b);change(bad)
            with self.assertRaises(ValueError):SB.decode(bad)

    def test_config_and_flag_identity_and_env_bundle(self):
        self.assertIn('HB-CUSTOM',SC.scaffold(dict(hb_id='HB-CUSTOM',sections={'hb-s%d'%n:dict(title='x',purpose='x',prose='x') for n in range(8)},modules={},claims=[])))
        p=self.root/'go.json';L.write_json(str(p),self.go_bundle())
        with mock.patch.dict(os.environ,{'HBV2_SYMBOLS_BUNDLE':str(p)}):
            self.assertEqual(len(SB.source('missing')['symbols']),3)
        self.assertEqual(SB.book_id(config={'book_id':'HB-CONFIG'}),'HB-CONFIG')
        self.assertEqual(SB.book_id(explicit='HB-FLAG',config={'book_id':'HB-CONFIG'}),'HB-FLAG')

    def test_build_identity_comes_from_new_book_config_and_flag(self):
        dst=self.root/'mini'
        mini_hb.make_mini(str(ROOT/'tests/fixtures/hbv2/graph'),str(dst))
        (dst/'book/manifest.json').unlink()
        L.write_json(str(dst/'book/seed/slots.json'),dict(book_id='HB-CONFIG-NEW'))
        B.build_all(str(dst))
        self.assertEqual(L.jload(str(dst/'book/manifest.json'))['hb_id'],'HB-CONFIG-NEW')
        B.build_all(str(dst),hb_id='HB-EXPLICIT')
        self.assertEqual(L.jload(str(dst/'book/manifest.json'))['hb_id'],'HB-EXPLICIT')

    def test_cached_bundle_and_raw_are_consumed_without_producer(self):
        out = self.root/'out'; out.mkdir()
        b = self.python_bundle()
        L.write_json(str(out/'symbols_bundle.json'), b)
        L.write_jsonl(str(out/'symbols_raw.jsonl'), SB.decode(b)['symbols'])
        with mock.patch.object(SB.subprocess, 'run', side_effect=AssertionError('producer rerun')):
            self.assertEqual(SB.source('missing', str(out))['symbols'], SB.decode(b)['symbols'])
            L.write_jsonl(str(out/'symbols_raw.jsonl'), [])
            with self.assertRaises(ValueError):
                SB.source('missing', str(out))

    def test_all_clis_accept_foreign_bundle_environment(self):
        p=self.root/'go.json';L.write_json(str(p), self.go_bundle())
        book=self.root/'book';book.mkdir()
        slots=dict(hb_id='HB-GO-001',sections={'hb-s%d'%n:dict(title='Go',purpose='Go',prose='Go',anchors=[s['id'] for s in self.go_bundle()['symbols']] if n==0 else []) for n in range(8)},modules={},claims=[])
        (book/'book.md').write_text(SC.scaffold(slots))
        env={k:v for k,v in os.environ.items() if not k.startswith('HBV2_')}
        env.update(HBV2_SYMBOLS_BUNDLE=str(p),PYTHONDONTWRITEBYTECODE='1')
        commands=[['hb_extract2.py','--subject','missing','--out',str(self.root/'out')],
                  ['hb_build.py','--root',str(self.root),'--render-derived'],
                  ['hb_index.py','--root',str(self.root)]]
        for cmd in commands:
            proc=subprocess.run([sys.executable,str(ROOT/'hbtools_v2'/cmd[0])]+cmd[1:],env=env,capture_output=True,text=True,timeout=60)
            self.assertEqual(proc.returncode,0,cmd[0]+proc.stderr)
        self.assertEqual(L.jload(str(book/'index/cols.json'))['N'],3)

    def test_extract_cli_bundle_ignores_missing_subject_and_preserves_bytes(self):
        p=self.root/'bundle.json';L.write_json(str(p),self.python_bundle())
        out=self.root/'out'
        proc=subprocess.run([sys.executable,str(ROOT/'hbtools_v2/hb_extract2.py'),'--subject','missing','--out',str(out),'--symbols-bundle',str(p)],capture_output=True,text=True,timeout=60)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(L.read_jsonl(str(out/'symbols_raw.jsonl')),SB.load(str(p))['symbols'])


if __name__=='__main__':
    unittest.main()
