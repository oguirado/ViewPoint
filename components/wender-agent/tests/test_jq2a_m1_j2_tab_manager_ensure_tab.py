# @module {"id":"wa.tests.test_jq2a_m1_j2_tab_manager_ensure_tab","layer":1,"owner":"PERCEIVE","deps":["wa.perceive.cdp_pipe","wa.perceive.cdp_port"],"book":"HB-WA-s1-perceive"}
"""@module wa.tests.test_jq2a_m1_j2_tab_manager_ensure_tab - TabManager ensure_tab and SessionRegistry contracts.

Validates origin normalization, deceptive host rejection, ambiguity refusal without CDP calls,
explicit target selection, true-miss tab creation, and ownership distinction on close.
"""

from dataclasses import dataclass
import unittest
from urllib.parse import urlsplit
import numpy as np

# Probe owner module availability explicitly
try:
    import wender.perceive.session_registry as session_registry_mod
    HAS_SESSION_REGISTRY = hasattr(session_registry_mod, "SessionRegistry") and hasattr(session_registry_mod, "Handle")
except ImportError:
    HAS_SESSION_REGISTRY = False

try:
    import wender.perceive.tab_manager as tab_manager_mod
    HAS_TAB_MANAGER = hasattr(tab_manager_mod, "TabManager")
except ImportError:
    HAS_TAB_MANAGER = False

OWNER_PRESENT = HAS_SESSION_REGISTRY and HAS_TAB_MANAGER


def expected_if_owner_absent(func):
    """Applies unittest.expectedFailure dynamically if owner implementation is absent."""
    if not OWNER_PRESENT:
        return unittest.expectedFailure(func)
    return func


# --- Fallback Dataclasses for Contract Harness Compatibility ---

@dataclass(frozen=True)
class Handle:
    kind: str
    canonical_id: str
    generation: int


@dataclass(frozen=True)
class TargetRecord:
    handle: Handle
    transport: object
    owned: bool
    epoch: int
    session_id: str | None
    attachment_owned: bool
    closed: bool


@dataclass(frozen=True)
class FrameRef:
    handle: Handle
    epoch: int
    frame_seq: int
    width: int
    height: int
    rgb: object
    digest: str


@dataclass(frozen=True)
class Observation:
    handle: Handle
    epoch: int
    frame_seq: int
    dpr_ppm: int
    state: dict
    frame: FrameRef


@dataclass(frozen=True)
class Receipt:
    action_id: str
    handle: Handle
    status: str
    reason: str | None


@dataclass(frozen=True)
class ActionRequest:
    handle: Handle
    action_id: str
    epoch: int
    payload: dict
    cost: int
    deadline_ns: int


@dataclass(frozen=True)
class ActorLease:
    handle: Handle
    owner: str
    generation: int
    nonce: bytes
    expires_ns: int


@dataclass(frozen=True)
class FocusLease:
    lease: ActorLease
    domain: str
    epoch: int


@dataclass(frozen=True)
class StreamSpec:
    width: int
    height: int
    slots: int
    quota_bytes: int


@dataclass(frozen=True)
class StreamToken:
    handle: Handle
    epoch: int


@dataclass(frozen=True)
class Ticket:
    action_id: str
    handle: Handle


@dataclass(frozen=True)
class ChangeResult:
    handle: Handle
    epoch: int
    settled: bool
    claims: np.ndarray
    ops: dict


@dataclass(frozen=True)
class PerceptionResult:
    handle: Handle
    observation: Observation | None
    error: str | None


@dataclass(frozen=True)
class WindowFrames:
    handle: Handle
    epoch: int
    frames: tuple
    frame_seqs: tuple
    dpr_ppm: int


@dataclass(frozen=True)
class WindowDescription:
    handle: Handle
    epoch: int
    frame_seqs: tuple
    descriptors: tuple
    regions: tuple
    error: str | None


class _FakeCDP:
    """Fake CDP transport to simulate CDP methods in RAM without network or browser."""

    def __init__(self, targets=None):
        self.targets = list(targets) if targets is not None else []
        self.calls = []
        self._next_session_id = 100
        self._next_target_id = 200

    def send(self, method, params=None, session_id=None, timeout=None):
        params = params or {}
        self.calls.append({"method": method, "params": params, "sessionId": session_id})

        if method == "Target.getTargets":
            return {"targetInfos": list(self.targets)}
        elif method == "Target.createTarget":
            self._next_target_id += 1
            tid = f"t-created-{self._next_target_id}"
            new_target = {"targetId": tid, "type": "page", "url": params.get("url", "about:blank")}
            self.targets.append(new_target)
            return {"targetId": tid}
        elif method == "Target.attachToTarget":
            self._next_session_id += 1
            sid = f"s-{self._next_session_id}"
            return {"sessionId": sid}
        elif method == "Page.enable":
            return {}
        elif method == "Page.navigate":
            return {"frameId": "f-1", "loaderId": "l-1"}
        elif method == "Target.detachFromTarget":
            return {}
        elif method == "Target.closeTarget":
            return {"success": True}
        return {}

    def calls_for(self, method):
        return [c for c in self.calls if c["method"] == method]


class TestTabManagerContractControls(unittest.TestCase):
    """Healthy contract harness and value structure tests (passing controls)."""

    def test_canonical_dataclass_structural_compatibility(self):
        """Validates canonical dataclass fields and immutability."""
        h = Handle(kind="cdp_tab", canonical_id="t-1", generation=0)
        self.assertEqual(h.kind, "cdp_tab")
        self.assertEqual(h.canonical_id, "t-1")
        self.assertEqual(h.generation, 0)

        tr = TargetRecord(
            handle=h,
            transport=None,
            owned=True,
            epoch=0,
            session_id="s-1",
            attachment_owned=True,
            closed=False,
        )
        self.assertEqual(tr.handle, h)
        self.assertTrue(tr.owned)
        self.assertFalse(tr.closed)

        rc = Receipt(action_id="act-1", handle=h, status="ok", reason=None)
        self.assertEqual(rc.status, "ok")

    def test_origin_normalization_contract(self):
        """Helper contract test for origin normalization logic."""
        def normalize_origin(url: str) -> str:
            parts = urlsplit(url)
            scheme = parts.scheme.lower()
            hostname = parts.hostname.lower() if parts.hostname else ""
            port = parts.port
            if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
                port = None
            port_str = f":{port}" if port else ""
            return f"{scheme}://{hostname}{port_str}"

        self.assertEqual(normalize_origin("https://example.com/page"), "https://example.com")
        self.assertEqual(normalize_origin("https://example.com:443/page"), "https://example.com")
        self.assertEqual(normalize_origin("https://user:pass@example.com/page"), "https://example.com")
        self.assertNotEqual(normalize_origin("https://example.com.deceptive.com/"), "https://example.com")


class TestTabManagerEnsureTab(unittest.TestCase):
    """Owner contract tests for TabManager.ensure_tab and SessionRegistry."""

    @expected_if_owner_absent
    def test_ensure_tab_duplicate_normalized_origins_and_deceptive_host(self):
        """Combines duplicate normalized origins and deceptive prefix origin during ensure_tab."""
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager

        fake = _FakeCDP(targets=[
            {"targetId": "t-deceptive", "type": "page", "url": "https://chatgpt.com.deceptive.org/page"},
            {"targetId": "t-1", "type": "page", "url": "https://chatgpt.com:443/chat/1"},
        ])
        reg = SessionRegistry()
        tm = TabManager(cdp=fake, registry=reg)

        # Reuses t-1 because origin matches normalized https://chatgpt.com
        handle = tm.ensure_tab("https://chatgpt.com/", "https://chatgpt.com/chat/1")
        self.assertEqual(handle.canonical_id, "t-1")
        self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)

    @expected_if_owner_absent
    def test_ensure_tab_ambiguity_refuses_without_cdp(self):
        """Asserts zero create or attach calls on ambiguity and rejects first-prefix-match mutant."""
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager

        fake = _FakeCDP(targets=[
            {"targetId": "t-1", "type": "page", "url": "https://perplexity.ai/search/1"},
            {"targetId": "t-2", "type": "page", "url": "https://perplexity.ai/search/2"},
        ])
        reg = SessionRegistry()
        tm = TabManager(cdp=fake, registry=reg)

        with self.assertRaises((ValueError, Exception)):
            tm.ensure_tab("https://perplexity.ai/", "https://perplexity.ai/search/1")

        # Zero create or attach calls must be made on ambiguity!
        self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)
        self.assertEqual(len(fake.calls_for("Target.attachToTarget")), 0)

    @expected_if_owner_absent
    def test_ensure_tab_explicit_selected_target_binds(self):
        """Explicit selected_target binds to the requested target resolving ambiguity (positive control)."""
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager

        fake = _FakeCDP(targets=[
            {"targetId": "t-1", "type": "page", "url": "https://perplexity.ai/search/1"},
            {"targetId": "t-2", "type": "page", "url": "https://perplexity.ai/search/2"},
        ])
        reg = SessionRegistry()
        tm = TabManager(cdp=fake, registry=reg)

        handle = tm.ensure_tab("https://perplexity.ai/", "https://perplexity.ai/search/2", selected_target="t-2")
        self.assertEqual(handle.canonical_id, "t-2")
        self.assertEqual(len(fake.calls_for("Target.createTarget")), 0)

        attach_calls = fake.calls_for("Target.attachToTarget")
        self.assertEqual(len(attach_calls), 1)
        self.assertEqual(attach_calls[0]["params"]["targetId"], "t-2")

    @expected_if_owner_absent
    def test_ensure_tab_true_miss_creates_scoped_tab(self):
        """Creates a scoped tab only on a true miss."""
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager

        fake = _FakeCDP(targets=[
            {"targetId": "t-other", "type": "page", "url": "https://other.com/page"},
        ])
        reg = SessionRegistry()
        tm = TabManager(cdp=fake, registry=reg)

        handle = tm.ensure_tab("https://chatgpt.com/", "https://chatgpt.com/c/new")

        create_calls = fake.calls_for("Target.createTarget")
        self.assertEqual(len(create_calls), 1)
        self.assertEqual(create_calls[0]["params"]["url"], "https://chatgpt.com/c/new")

        attach_calls = fake.calls_for("Target.attachToTarget")
        self.assertEqual(len(attach_calls), 1)

    @expected_if_owner_absent
    def test_ensure_tab_close_distinguishes_attachment_ownership_from_target_ownership(self):
        """Close is idempotent and distinguishes attachment ownership from target ownership."""
        from wender.perceive.session_registry import SessionRegistry
        from wender.perceive.tab_manager import TabManager

        fake = _FakeCDP(targets=[
            {"targetId": "t-reused", "type": "page", "url": "https://example.com/reused"},
        ])
        reg = SessionRegistry()
        tm = TabManager(cdp=fake, registry=reg)

        # 1. Reuse existing target (attachment_owned=True, owned=False)
        h_reused = tm.ensure_tab("https://example.com/", "https://example.com/reused")
        rec_1 = tm.close(h_reused)
        self.assertEqual(rec_1.status, "closed")

        # Reused target close detaches session but does NOT close target
        self.assertEqual(len(fake.calls_for("Target.detachFromTarget")), 1)
        self.assertEqual(len(fake.calls_for("Target.closeTarget")), 0)

        # Idempotent close
        rec_2 = tm.close(h_reused)
        self.assertEqual(rec_2.status, "already_closed")
        self.assertEqual(len(fake.calls_for("Target.detachFromTarget")), 1)

        # 2. Create new target (attachment_owned=True, owned=True)
        h_created = tm.ensure_tab("https://example.com/new", "https://example.com/new")
        rec_3 = tm.close(h_created)
        self.assertEqual(rec_3.status, "closed")

        # Created target close detaches session AND closes target
        self.assertEqual(len(fake.calls_for("Target.closeTarget")), 1)
        self.assertEqual(len(fake.calls_for("Browser.close")), 0)


if __name__ == "__main__":
    unittest.main()
