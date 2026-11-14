#!/usr/bin/env python3
"""Sparse-file disk accounting: caches excluded, evidence and other data capped."""
import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
loader=importlib.machinery.SourceFileLoader('vis_budget',str(ROOT/'vis'))
spec=importlib.util.spec_from_loader(loader.name,loader)
core=importlib.util.module_from_spec(spec);loader.exec_module(core)

class Budget(unittest.TestCase):
    def test_p4_cache_exclusion_keeps_frame_budget(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(core,'jobpath',return_value=Path(tmp)):
            root=Path(tmp); job=dict(job='budget')
            for directory in ('home/.cache/download','cache/download'):
                path=root/directory/'large'; path.parent.mkdir(parents=True)
                with path.open('wb') as stream: stream.truncate(2*1024**3+1)
                core.budget(job)
            for directory in ('frames','home/documents','data/cache'):
                path=root/directory/'large'; path.parent.mkdir(parents=True)
                with path.open('wb') as stream: stream.truncate(2*1024**3+1)
                with self.subTest(directory=directory), self.assertRaises(core.Failure) as error: core.budget(job)
                self.assertEqual(error.exception.rc,6); path.unlink()
            core.budget(job)

if __name__=='__main__': unittest.main()
