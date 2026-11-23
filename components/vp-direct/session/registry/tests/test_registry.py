"""S1 contract and mutation checks with synthetic profiles and launch handles."""

import builtins
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from schemas.validation import SCHEMA_DIR, validate_contract
from session import registry
from session.registry import LaunchNotRunning, OwnedInstance, SessionRefused, SessionRegistry, SiteProfile


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent))
        self.workspace = Path(self.temp)
        self.checkout = self.workspace / "checkout"
        self.checkout.mkdir()
        self.home = self.workspace / "data"
        self.enterContext(patch.object(registry, "CHECKOUT", self.checkout))
        self.args = {
            "site_group": "fixture_chat", "browser": "google-chrome", "kind": "browser",
            "pace": {"min_interval_ms": 500}, "risk_ceiling": "leer",
        }
        self.site = SiteProfile("fixture-profile", Path("/opt/google/chrome/chrome"), ":0", "cdp", True)
        self.calls = []
        self.launch = Mock(side_effect=self.new_instance)
        self.fixture = json.loads(
            (SCHEMA_DIR / "tests/examples/vp.session_v1.valid.json").read_bytes()
        )

    def new_instance(self, site, profile):
        seq = self.launch.call_count
        return OwnedInstance(
            window={"pid": 100 + seq, "xid": hex(100 + seq), "witness": f"witness-{seq}"},
            capsule_id=f"capsule-{seq}", incarnation=f"incarnation-{seq}",
            detach=lambda: self.calls.append(("detach", seq)),
            stop=lambda: self.calls.append(("stop", seq)),
            cdp_port=49000 + seq, cdp_target_id=f"target-{seq}",
        )

    def store(self, *, site=None, **kwargs):
        return SessionRegistry({("fixture_chat", "google-chrome"): site or self.site},
                               self.launch, data_home=self.home, **kwargs)

    def assert_refused(self, code, action, *args):
        with self.assertRaises(SessionRefused) as raised:
            action(*args)
        self.assertEqual(raised.exception.code, code)

    def test_open_list_close_and_reopen_preserve_profile(self):
        store = self.store()
        opened = store.vp_session_open(self.args)
        validate_contract("vp.session/v1", opened)
        sid = opened["session_id"]
        self.assertEqual(opened["state"], "LOGIN_PENDING")
        self.assertIsNone(opened["lease"]["holder"])
        self.assertEqual(store.get_session(sid), opened)
        self.assertEqual(store.vp_session_list(), [{"session_id": sid, "state": "LOGIN_PENDING"}])
        profile = store.profile_root / self.site.profile_id
        self.assertEqual(stat.S_IMODE(profile.lstat().st_mode), 0o700)
        original_inode = profile.lstat().st_ino
        self.assertEqual(store.vp_session_close(sid), {"session_id": sid, "state": "CLOSED"})
        self.assertEqual(store.vp_session_close(sid), {"session_id": sid, "state": "CLOSED"})
        self.assertIsNone(store.get_session(sid))
        self.assertEqual(store.vp_session_list(), [])
        self.assertEqual(self.calls, [("detach", 1), ("stop", 1)])
        closed = self.read_wire(store, sid)
        validate_contract("vp.session/v1", closed)
        self.assertEqual(closed["lease"], {"holder": None, "authority_epoch": 1})
        reopened = store.vp_session_open(self.args)
        self.assertNotEqual(reopened["incarnation"], opened["incarnation"])
        self.assertEqual(profile.lstat().st_ino, original_inode)

    def read_wire(self, store, sid):
        with store._db() as db:
            wire, = db.execute("SELECT wire FROM sessions WHERE session_id = ?", (sid,)).fetchone()
        return json.loads(wire)

    def test_pending_launch_reserves_the_profile_for_other_registry_clients(self):
        first, second = self.store(), self.store()
        started, finish = Event(), Event()

        def held_launch(site, profile):
            started.set()
            if not finish.wait(5):
                raise AssertionError("test launch was never released")
            return self.new_instance(site, profile)

        self.launch.side_effect = held_launch
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.vp_session_open, self.args)
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(second.vp_session_list()[0]["state"], "LAUNCHING")
                self.assert_refused("PROFILE_IN_USE", second.vp_session_open, self.args)
            finally:
                finish.set()
            opened = pending.result(timeout=5)
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(second.get_session(opened["session_id"]), opened)
        self.assert_refused("OWNER", second.vp_session_close, opened["session_id"])
        self.assertEqual(self.calls, [])
        with patch.object(registry.os, "getpid", return_value=os.getpid() + 1):
            self.assert_refused("OWNER", first.vp_session_close, opened["session_id"])
        first.vp_session_close(opened["session_id"])

    def profile_oracle(self, first, second):
        first.vp_session_open(self.args)
        self.assert_refused("PROFILE_IN_USE", second.vp_session_open, self.args)
        self.assertEqual(self.launch.call_count, 1)

    def test_profile_in_use(self):
        self.profile_oracle(self.store(), self.store())

    def test_mutant_two_leases_for_one_profile(self):
        first, second = self.store(), self.store()
        with second._db(write=True) as db:
            db.execute("DROP INDEX live_profile")

        def unguarded_reserve(db, record):
            db.execute("INSERT INTO sessions (session_id, profile_id, attached) VALUES (?, ?, 0)",
                       (record["session_id"], record["profile_id"]))

        with patch.object(second, "_reserve", unguarded_reserve), self.assertRaises(AssertionError):
            self.profile_oracle(first, second)
        self.assertEqual(self.launch.call_count, 2)

    def test_default_profile_refused(self):
        for profile_id in ("Default", "guest profile", "Profile-1", ".."):
            with self.subTest(profile_id=profile_id):
                store = self.store(site=replace(self.site, profile_id=profile_id))
                self.assert_refused("DEFAULT_PROFILE_REFUSED", store.vp_session_open, self.args)
                self.assertFalse(store.profile_root.exists())
        self.launch.assert_not_called()

    def test_snap_chromium_cdp_unavailable(self):
        store = self.store(site=replace(self.site, executable=Path("/snap/bin/chromium")))
        self.assert_refused("SNAP_CDP_UNAVAILABLE", store.vp_session_open, self.args)
        self.launch.assert_not_called()

    def test_firefox_cdp_deferred_and_approved_atspi_can_open(self):
        site = replace(self.site, executable=Path("/snap/bin/firefox"))
        args = dict(self.args, browser="firefox")
        store = SessionRegistry({("fixture_chat", "firefox"): site}, self.launch, data_home=self.home)
        self.assert_refused("BIDI_DEFERRED", store.vp_session_open, args)
        self.launch.assert_not_called()
        atspi = replace(site, backend="atspi", cdp_allowed=False)
        store = SessionRegistry({("fixture_chat", "firefox"): atspi}, self.launch, data_home=self.home)
        opened = store.vp_session_open(args)
        self.assertEqual(opened["backend"], "atspi")
        self.assertNotIn("cdp_target_id", opened)

    def test_missing_pace_or_risk_ceiling_and_vm_viewer_refused(self):
        store = self.store()
        for key in ("pace", "risk_ceiling"):
            with self.subTest(missing=key):
                args = {k: value for k, value in self.args.items() if k != key}
                self.assert_refused("-32602", store.vp_session_open, args)
        self.assert_refused("-32602", store.vp_session_open, dict(self.args, kind="vm_viewer"))
        self.launch.assert_not_called()

    def test_existing_profile_symlink_refused_before_launch(self):
        store = self.store()
        store.profile_root.mkdir(mode=0o700)
        other = self.workspace / "other"
        other.mkdir(mode=0o700)
        (store.profile_root / self.site.profile_id).symlink_to(other)
        with self.assertRaises(ValueError):
            store.vp_session_open(self.args)
        self.assertEqual(store.vp_session_list(), [])
        self.launch.assert_not_called()

    def assert_profile_opaque(self, store):
        profile = store.profile_root / self.site.profile_id
        events = []
        path_open = Path.open
        os_open = os.open
        os_scandir = os.scandir
        os_listdir = os.listdir

        def audit(call):
            def audited(path, *args, **kwargs):
                if not isinstance(path, int) and Path(path).is_relative_to(profile):
                    events.append(path)
                return call(path, *args, **kwargs)
            return audited

        with (
            patch.object(builtins, "open", audit(builtins.open)),
            patch.object(Path, "open", audit(path_open)),
            patch.object(os, "open", audit(os_open)),
            patch.object(os, "scandir", audit(os_scandir)),
            patch.object(os, "listdir", audit(os_listdir)),
        ):
            opened = store.vp_session_open(self.args)
            store.vp_session_close(opened["session_id"])
        self.assertEqual(events, [])

    def test_profile_is_never_opened_or_enumerated(self):
        self.assert_profile_opaque(self.store())

    def test_mutant_hash_profile_contents(self):
        store = self.store()
        store.profile_root.mkdir(mode=0o700)
        profile = store.profile_root / self.site.profile_id
        profile.mkdir(mode=0o700)
        canary = profile / "synthetic-fixture.txt"
        canary.write_text("fixture-only")
        open_session = store.vp_session_open

        def digesting_open(args):
            hashlib.sha256(canary.read_bytes()).hexdigest()
            return open_session(args)

        with patch.object(store, "vp_session_open", digesting_open), self.assertRaises(AssertionError):
            self.assert_profile_opaque(store)
        self.assertTrue(canary.is_file())

    def outside_checkout_oracle(self, path):
        with self.assertRaises(ValueError):
            SessionRegistry({}, self.launch, data_home=path)
        self.assertFalse(path.exists())

    def test_registry_path_outside_checkout_and_no_symlink_alias(self):
        store = self.store()
        self.assertFalse(store.path.is_relative_to(self.checkout))
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        self.outside_checkout_oracle(self.checkout / "data")
        alias = self.workspace / "alias"
        alias.symlink_to(self.checkout)
        self.outside_checkout_oracle(alias / "data")

    def test_mutant_registry_inside_checkout(self):
        with patch.object(registry, "_data_home", side_effect=Path), self.assertRaises(AssertionError):
            self.outside_checkout_oracle(self.checkout / "data")

    def xid_only_oracle(self, store):
        instance = self.new_instance(self.site, None)
        self.launch.side_effect = None
        self.launch.return_value = replace(instance, window={"xid": "0x123"})
        self.assert_refused("-32602", store.vp_session_open, self.args)
        self.assertEqual(store.vp_session_list(), [])
        self.assertEqual(self.calls, [("stop", 0)])

    def test_xid_only_not_a_window_binding(self):
        self.xid_only_oracle(self.store())

    def test_mutant_xid_only_binding(self):
        with (
            patch.object(registry, "_validate_record"),
            patch.object(registry, "_window_key", return_value=(":0", 0x123)),
            self.assertRaises(AssertionError),
        ):
            self.xid_only_oracle(self.store())

    def test_duplicate_canonical_xid_refused_across_profiles(self):
        store = self.store()
        store.register_attached(self.fixture)
        duplicate = dict(
            self.fixture, session_id="s-other", profile_id="other-profile", incarnation="i-other",
            window={"xid": str(0x123), "pid": 456, "witness": "other"},
        )
        self.assert_refused("identity_mismatch", store.register_attached, duplicate)

    def test_attached_close_fences_broker_and_never_stops_foreign_process(self):
        custody = Mock()
        writer = self.store()
        reader = self.store(revoke=custody.revoke, detach_attached=custody.detach)
        child_cmd = [sys.executable, "-I", "-B", "-c", "import sys; sys.stdin.buffer.read()"]
        with subprocess.Popen(child_cmd, stdin=subprocess.PIPE) as child:
            foreign = dict(self.fixture, window=dict(self.fixture["window"], pid=child.pid))
            writer.register_attached(foreign)
            sid = foreign["session_id"]
            self.assert_refused("NO_LEASE", writer.vp_session_close, sid)
            self.assertEqual(reader.vp_session_close(sid), {"session_id": sid, "state": "CLOSED"})
            self.assertIsNone(child.poll())
        self.assertEqual([call[0] for call in custody.mock_calls], ["revoke", "detach"])
        custody.revoke.assert_called_once_with(sid, foreign["lease"])
        custody.detach.assert_called_once_with(sid)
        self.assertIsNone(reader.get_session(sid))
        self.assertFalse(reader.profile_root.exists())
        self.assertEqual(reader.vp_session_list(), [])
        self.launch.assert_not_called()
        self.assertEqual(self.calls, [])
        closed = self.read_wire(reader, sid)
        self.assertNotIn("origin", closed)
        self.assertEqual(closed["lease"], {"holder": None, "authority_epoch": 2})

    def test_uncertain_launch_and_close_stay_reserved(self):
        store, peer = self.store(), self.store()
        self.launch.side_effect = LaunchNotRunning("PORT_BUSY", "no child")
        self.assert_refused("PORT_BUSY", store.vp_session_open, self.args)
        self.assertEqual(peer.vp_session_list(), [])
        self.launch.side_effect = OSError("may have started")
        self.assert_refused("UNCERTAIN_OUTCOME", store.vp_session_open, self.args)
        self.assert_refused("PROFILE_IN_USE", peer.vp_session_open, self.args)
        self.assertEqual(peer.vp_session_list()[0]["state"], "LAUNCHING")

        other = self.store(site=replace(self.site, profile_id="retry-profile"))
        stop = Mock(side_effect=[OSError("could not confirm exit"), None])
        self.launch.side_effect = None
        self.launch.return_value = replace(self.new_instance(self.site, None), stop=stop)
        opened = other.vp_session_open(self.args)
        sid = opened["session_id"]
        self.assert_refused("UNCERTAIN_OUTCOME", other.vp_session_close, sid)
        self.assertIsNone(other.get_session(sid))
        self.assert_refused("PROFILE_IN_USE", other.vp_session_open, self.args)
        self.assertEqual(other.vp_session_close(sid)["state"], "CLOSED")
        self.assertEqual(stop.call_count, 2)

    def test_incompatible_provisioning_refused_before_launch(self):
        for site in (replace(self.site, cdp_allowed=False), replace(self.site, display=":1")):
            with self.subTest(site=site):
                store = self.store(site=site)
                self.assert_refused("-32602", store.vp_session_open, self.args)
                self.assertEqual(store.vp_session_list(), [])
                self.assertFalse(store.profile_root.exists())
        self.launch.assert_not_called()

    def test_live_script_compiles_without_running(self):
        path = Path(registry.__file__).with_name("LIVE_session.py")
        compile(path.read_bytes(), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
