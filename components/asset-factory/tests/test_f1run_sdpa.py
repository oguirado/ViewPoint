"""Execute the pinned, patched sparse backend configuration, not a mock copy."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from providers.trellis2.build import build


class SparseBackendTests(unittest.TestCase):
    def test_exported_sparse_backend_honours_explicit_sdpa_without_flash_attn(self):
        lock=build.read_json(build.HERE/'build-inputs.json')
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp)
            build.export_sources(lock,Path(os.environ['AF_F1U1_REPOS']),stage)
            path=stage/'sources/trellis2/trellis2/modules/sparse/config.py'
            for backend in ('sdpa','naive'):
                with self.subTest(backend=backend),patch.dict(os.environ,{'SPARSE_ATTN_BACKEND':backend}),contextlib.redirect_stdout(io.StringIO()):
                    namespace={};exec(compile(path.read_bytes(),str(path),'exec'),namespace)
                    self.assertEqual(namespace['ATTN'],backend)


    def test_actual_sdpa_helpers_match_independent_attention_and_isolate_sequences(self):
        import ast
        import typing
        import torch
        import torch.nn.functional as functional
        lock=build.read_json(build.HERE/'build-inputs.json')
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp)
            build.export_sources(lock,Path(os.environ['AF_F1U1_REPOS']),stage)
            path=stage/'sources/trellis2/trellis2/modules/sparse/attention/full_attn.py'
            tree=ast.parse(path.read_bytes())
            selected=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in
                      ('_seqlen_segment_ids','_block_diag_attn_mask','_sdpa_varlen')]
            self.assertEqual(len(selected),3)
            namespace={'torch':torch,'F':functional,'Union':typing.Union,'List':typing.List}
            exec(compile(ast.Module(body=selected,type_ignores=[]),str(path),'exec'),namespace)
            q=torch.arange(40,dtype=torch.float64).reshape(5,2,4)/13
            k=q.flip(0)*0.3
            v=torch.arange(30,dtype=torch.float64).reshape(5,2,3)/17
            expected=[];qo=ko=0
            for ql,kl in zip((2,3),(3,2)):
                scores=torch.einsum('qhc,khc->hqk',q[qo:qo+ql],k[ko:ko+kl])/2
                expected.append(torch.einsum('hqk,khc->qhc',scores.softmax(-1),v[ko:ko+kl]))
                qo+=ql;ko+=kl
            expected=torch.cat(expected)
            # [TARGET] Float64 CPU regression tolerance, not a transferred geometry bound.
            for backend in ('sdpa','naive'):
                actual=namespace['_sdpa_varlen'](q,k,v,[2,3],[3,2],backend)
                self.assertTrue(torch.allclose(actual,expected,atol=1e-12,rtol=1e-12))
                changed=v.clone();changed[3:]+=100
                isolated=namespace['_sdpa_varlen'](q,k,changed,[2,3],[3,2],backend)
                self.assertTrue(torch.equal(actual[:2],isolated[:2]))
            # Negative control: treating packed sequences as one global sequence
            # must be distinguished from the independent mathematical oracle.
            broken=functional.scaled_dot_product_attention(q.transpose(0,1)[None],k.transpose(0,1)[None],v.transpose(0,1)[None])[0].transpose(0,1)
            self.assertFalse(torch.allclose(broken,expected,atol=1e-12,rtol=1e-12))


if __name__=='__main__':unittest.main()
