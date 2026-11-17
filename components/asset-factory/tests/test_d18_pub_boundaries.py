"""Focused publisher boundary checks using synthetic local inputs."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.publish import publisher
from tools.publish.static_gate import static_violations


class BoundaryTests(unittest.TestCase):
    def test_named_backends_are_disabled_at_configuration(self):
        for destination in ("fixture:work/", "fixture_remote:review/"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(publisher.PublishError, "remote_disabled"):
                    publisher.configuration({
                        "schema": "af.publish-config/v1", "destination_root": destination,
                    })

    def test_static_helper_launch_arguments(self):
        valid = (
            "from asset_factory import proc\nproc.popen(['true'])",
            "process_helper().popen(['true'], start_new_session=True)",
        )
        for snippet in valid:
            self.assertEqual(static_violations(snippet), [])
        invalid = (
            "from asset_factory import proc\nproc.popen(['true'], shell=True)",
            "process_helper().popen(['true'], shell=True)",
            "from asset_factory.proc import popen as launch\nlaunch(['true'], **options)",
            "helper = process_helper\nhelper().popen(*arguments)",
        )
        for snippet in invalid:
            with self.subTest(snippet=snippet):
                self.assertIn("unguarded_process", static_violations(snippet))

    def test_reviewer_static_snippets(self):
        # Parse only. These snippets are never imported or executed.
        proc_import = "from asset_factory import proc\n"
        invalid = {
            "kwargs_dict": proc_import + "kw={'shell':True}\nproc.popen(['x'], **kw)",
            "kwargs_param": proc_import + "def f(**k):\n    return proc.popen(['x'], **k)",
            "getattr": proc_import + "f=getattr(proc,'popen')\nf('x', shell=True)",
            "attr_alias": proc_import + "f=proc.popen\nf('kill -9 -1', shell=True)",
            "import_as": "import asset_factory.proc as q\nq.popen('x', shell=True)",
            "from_as": "from asset_factory.proc import popen as run\nrun('x', shell=True)",
            "string_cmd_static": proc_import + "proc.popen('kill -9 -1', start_new_session=True)",
            "param_alias": proc_import + "def launch(h, c):\n    return h.popen(c, shell=True)\nlaunch(proc, 'x')",
            "for_alias": proc_import + "for h in (proc,):\n    h.popen('x', shell=True)",
            "lambda_alias": proc_import + "(lambda h: h.popen('x', shell=True))(proc)",
            "class_attr": proc_import + "class K:\n    h = proc\nK.h.popen('x', shell=True)",
            "default_arg": proc_import + "def f(h=proc):\n    h.popen('x', shell=True)",
            "comprehension": proc_import + "[h.popen('x', shell=True) for h in [proc]]",
            "with_alias": proc_import + "import contextlib\nwith contextlib.nullcontext(proc) as h:\n    h.popen('x', shell=True)",
            "walrus": proc_import + "(h := proc).popen('x', shell=True)",
            "signal_param": proc_import + "def f(h):\n    h.signal_group(-1, 9)\nf(proc)",
            "variable_argv": proc_import + "args = ['true']\nproc.popen(args)",
            "returned_module": "def f():\n    return process_helper()",
            "returned_member": proc_import + "def f():\n    return proc.popen",
        }
        for key, snippet in invalid.items():
            with self.subTest(key=key):
                self.assertIn("unguarded_process", static_violations(snippet))
        for snippet in (proc_import + "proc.popen(['true'], start_new_session=True)",
                        "import asset_factory.proc as q\nq.popen(('true',))"):
            self.assertEqual(static_violations(snippet), [])

    def test_cleanup_requires_directory_descriptor(self):
        scoped = "import shutil\nshutil.rmtree(name, dir_fd=directory)"
        unscoped = "import shutil\nshutil.rmtree(name)"
        self.assertEqual(static_violations(scoped), [])
        self.assertIn("unguarded_process", static_violations(unscoped))

    def test_credential_components_refuse_before_file_access(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in (".netrc", ".npmrc", ".NETRC", ".NPMRC"):
                with self.subTest(name=name):
                    candidate = Path(temp) / name
                    with self.assertRaisesRegex(publisher.PublishError, "credential_source_forbidden"):
                        publisher.safe_source(candidate, temp)

    def test_destination_root_rejects_control_characters(self):
        with tempfile.TemporaryDirectory() as temp:
            for suffix in ("\n", "\t", "\x00", "\x7f", "\x85"):
                with self.subTest(suffix=suffix):
                    with self.assertRaisesRegex(publisher.PublishError, "invalid_local_destination"):
                        publisher.root_path(":local:" + temp + "/outside" + suffix)
                    with self.assertRaisesRegex(publisher.PublishError, "invalid_local_store"):
                        publisher.local_store(temp + "/outside" + suffix)
            self.assertEqual(publisher.root_path(":local:" + temp + "/safe"), ":local:" + temp + "/safe/")
            self.assertEqual(publisher.local_store(temp + "/safe"), Path(temp) / "safe")

    def test_artifact_type_rejects_empty_path_parts(self):
        with self.assertRaisesRegex(publisher.PublishError, "unsupported_artifact_path"):
            publisher.artifact_path(".")

    def test_lock_capacity_preserves_existing_lock_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            roots = [":local:" + str(runtime / suffix) for suffix in ("a", "b", "c")]
            with patch.object(publisher, "lock_user_root", return_value=runtime), \
                    patch.object(publisher, "MAX_LOCK_FILES", 2, create=True):
                with publisher.destination_lock(roots[0], 1):
                    with self.assertRaisesRegex(publisher.PublishError, "transfer_in_progress"):
                        with publisher.destination_lock(roots[0], 1):
                            self.fail("same destination acquired two locks")
                    with publisher.destination_lock(roots[1], 1):
                        pass
                lock_dir = runtime / "af-publish-locks"
                before = {entry.name: entry.stat().st_ino for entry in lock_dir.glob("*.lock")}
                self.assertEqual(len(before), 2)
                with self.assertRaisesRegex(publisher.PublishError, "lock_capacity_exceeded"):
                    with publisher.destination_lock(roots[2], 1):
                        self.fail("created a lock beyond capacity")
                with publisher.destination_lock(roots[0], 1):
                    self.assertEqual({entry.name: entry.stat().st_ino for entry in lock_dir.glob("*.lock")}, before)
