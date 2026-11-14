"""Router C6 safety design: production pins and a LOCAL PROPOSAL model.

Production test classes call the bound actor, AX matcher, and CDP executor. The
expected failure names the missing-fresh-snapshot gate defect; it is not a fix.
The separate observed-behavior pin intentionally records the unsafe current
behavior and must change when that defect is fixed.

Classes prefixed ``_Proposal`` are defined only in this test module. Their
registry, tokens, phases, receipts, and adapter are NOT repository interfaces.
They specify deterministic ownership and failure transitions with callbacks,
not thread/process synchronization, browser behavior, or host integration.
Future host integration evidence is required before declaring any finding fixed.
No document-string assertions, network, browser, desktop, sleeps, or frame files
are used. Missing-fingerprint and bare-context repros belong to the Jules work;
this module deliberately does not duplicate them.
"""

import unittest
from unittest.mock import Mock, call, patch

from wender.act import actor, cdp_executor
from wender.perceive import ax


def _recorded_node():
    node = {
        "role": "button",
        "name": "Delete saved draft",
        "attrs": (("disabled", "false"), ("pressed", "false")),
        "depth": 1,
        "ancestors": ("main",),
        "ordinal": 0,
        "cardinality": 1,
        "backend_id": 101,
        "box": [10, 20, 80, 30],
    }
    node["id"] = ax.stable_id(node)
    node["fp_exact"], node["fp_stable"] = ax.fingerprints(node)
    return node


def _fake_transport(events=None, runtime_error=None):
    """Inject an offline CDP transport; a runtime error occurs after send starts."""
    transport = Mock(spec=["send"])

    def send(method, params=None, session_id=None):
        if events is not None:
            events.append(method)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "object-%s" % params["backendNodeId"]}}
        if method == "Runtime.callFunctionOn" and runtime_error is not None:
            raise runtime_error
        return {}

    transport.send.side_effect = send
    return transport


class TestProductionActorPins(unittest.TestCase):
    """Observed repository behavior, including gaps; not proposed guarantees."""

    def test_path_preference_ignores_input_order_duplicates_and_unknown_labels(self):
        self.assertEqual(actor.PATHS, ("api", "ax", "coordinates"))
        for mask in range(8):
            selected = [p for i, p in enumerate(actor.PATHS) if mask & (1 << i)]
            available = list(reversed(selected)) + ["unknown"] + selected
            with self.subTest(mask=mask):
                self.assertEqual(
                    actor.choose_path(iter(available)), selected[0] if selected else None
                )

    def test_current_single_string_is_silently_consumed_as_characters(self):
        for path in actor.PATHS:
            with self.subTest(path=path):
                with self.assertRaises((TypeError, ValueError)):
                    actor.choose_path(path)
                self.assertEqual(actor.choose_path([path]), path)

    def test_execute_is_still_an_unwired_stub_for_every_path(self):
        with patch.object(actor, "ActorLock") as lock_factory:
            with patch.object(cdp_executor, "run_ax_action") as executor:
                for path in actor.PATHS:
                    with self.subTest(path=path):
                        with self.assertRaises(NotImplementedError):
                            actor.execute(path, {"op": "click", "target": "unused"})
                lock_factory.assert_not_called()
                executor.assert_not_called()

    def test_separate_production_instances_can_be_owned_at_the_same_time(self):
        first, second = actor.ActorLock(), actor.ActorLock()
        with first.acquire("actor-a"):
            with second.acquire("actor-b"):
                self.assertEqual((first.holder, second.holder), ("actor-a", "actor-b"))
        self.assertIsNone(first.holder)
        self.assertIsNone(second.holder)

    def test_current_empty_holder_does_not_imply_the_primitive_is_unlocked(self):
        lock = actor.ActorLock()
        with self.assertRaises(ValueError):
            lock.acquire(None)
        self.assertIsNone(lock.holder)

    def test_metadata_gaps_at_primitive_boundaries_are_deterministic(self):
        """F-ACT-04: inject primitive callbacks, without scheduling threads."""
        primitive = Mock(spec=["acquire", "release"])
        state = {"held": False}
        observations = []
        with patch.object(actor.threading, "Lock", return_value=primitive):
            lock = actor.ActorLock()

        def observe(phase):
            with self.assertRaises(actor.ActorBusy) as busy:
                lock.acquire("contender")
            observations.append((phase, lock.holder, str(busy.exception)))

        def acquire(blocking):
            self.assertIs(blocking, False)
            if state["held"]:
                return False
            state["held"] = True
            observe("primitive acquired, holder not yet published")
            return True

        def release():
            self.assertTrue(state["held"])
            observe("holder cleared, primitive still held")
            state["held"] = False

        primitive.acquire.side_effect = acquire
        primitive.release.side_effect = release
        lock.acquire("actor-a")
        self.assertEqual(lock.holder, "actor-a")
        lock.release()
        self.assertEqual(len(observations), 2)
        for phase, holder, diagnostic in observations:
            with self.subTest(phase=phase):
                self.assertEqual(holder, "actor-a")
                self.assertEqual(diagnostic, "surface already driven by 'actor-a'")
        self.assertFalse(state["held"])


class TestProductionSnapshotGates(unittest.TestCase):
    """Real matching/dispatch tests with recorded exact fingerprints preserved."""

    def setUp(self):
        self.node = _recorded_node()
        self.nodes = [self.node]
        self.describe = ax.describe(self.node)
        self.action = {
            "op": "click",
            "target": self.node["id"],
            "irreversible": True,
            "describe": dict(self.describe),
        }
        # setUp assertions are outside expectedFailure's test-body suppression.
        self.assertEqual(self.describe["fp_exact"], ax.fingerprints(self.node)[0])
        self.assertEqual(self.describe["fp_stable"], ax.fingerprints(self.node)[1])
        self.assertEqual(len(self.describe["fp_exact"]), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in self.describe["fp_exact"]))
        match = ax.match(self.nodes, self.describe)
        self.assertEqual(match["level"], "L0_EXACT")
        self.assertFalse(match["tie"])
        self.assertFalse(match.get("established", False))
        self.assertIs(match["node"], self.node)

    def _run(self, transport, read_nodes, nodes=None, action=None, **kwargs):
        return cdp_executor.run_ax_action(
            transport, "session-a", self.nodes if nodes is None else nodes,
            self.action if action is None else action, read_nodes,
            retries=1, wait_ms=0, sleep=Mock(), **kwargs
        )

    def test_valid_exact_fingerprints_with_two_observations_authorize_one_dispatch(self):
        transport = _fake_transport()
        read_nodes = Mock(side_effect=[self.nodes, self.nodes])
        receipt = self._run(transport, read_nodes)
        self.assertEqual((receipt["ok"], receipt["verified"], receipt["attempts"]), (1, 1, 1))
        self.assertEqual(read_nodes.call_count, 2)
        self.assertEqual(
            [c.args[0] for c in transport.send.call_args_list],
            ["DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn"],
        )

    def test_real_l0_and_l1_ties_are_refused_at_each_snapshot_gate(self):
        for level in ("L0_EXACT", "L1_STABLE"):
            for stage in ("initial", "fresh"):
                with self.subTest(level=level, stage=stage):
                    candidate = dict(self.node)
                    if level == "L1_STABLE":
                        candidate["attrs"] = (("disabled", "false"), ("pressed", "true"))
                    tied = [candidate, dict(candidate, id="duplicate-id", backend_id=202)]
                    diagnostic = ax.match(tied, self.describe)
                    self.assertEqual(diagnostic["level"], level)
                    self.assertTrue(diagnostic["tie"])
                    transport = _fake_transport()
                    read_nodes = Mock(return_value=tied)
                    with self.assertRaises(cdp_executor.ActionError):
                        self._run(
                            transport, read_nodes,
                            nodes=tied if stage == "initial" else self.nodes,
                            allow_coordinates=True,
                        )
                    transport.send.assert_not_called()
                    self.assertEqual(read_nodes.call_count, 0 if stage == "initial" else 1)

    def test_fresh_gate_dispatches_using_the_new_backend_identity(self):
        events = []
        fresh = [dict(self.node, backend_id=202)]
        transport = _fake_transport(events)
        reads = []

        def read_nodes():
            reads.append(None)
            events.append("fresh-gate" if len(reads) == 1 else "post-state")
            return fresh

        receipt = self._run(transport, read_nodes)
        self.assertEqual(receipt["ok"], 1)
        self.assertEqual(
            transport.send.call_args_list[:2],
            [
                call("DOM.scrollIntoViewIfNeeded", {"backendNodeId": 202}, session_id="session-a"),
                call("DOM.resolveNode", {"backendNodeId": 202}, session_id="session-a"),
            ],
        )
        self.assertEqual(transport.send.call_args_list[2].args[1]["objectId"], "object-202")
        self.assertEqual(
            events,
            ["fresh-gate", "DOM.scrollIntoViewIfNeeded", "DOM.resolveNode",
             "Runtime.callFunctionOn", "post-state"],
        )

    def test_fresh_identity_downgrade_to_l2_is_refused_before_transport(self):
        changed = dict(self.node, ancestors=("dialog",), backend_id=202)
        changed["id"] = ax.stable_id(changed)
        self.assertEqual(ax.match([changed], self.describe)["level"], "L2_NAME")
        transport = _fake_transport()
        read_nodes = Mock(return_value=[changed])
        with self.assertRaises(cdp_executor.ActionError):
            self._run(transport, read_nodes)
        read_nodes.assert_called_once_with()
        transport.send.assert_not_called()

    def test_observed_empty_fresh_snapshot_is_refused(self):
        transport = _fake_transport()
        with self.assertRaises(cdp_executor.ActionError):
            self._run(transport, Mock(return_value=[]))
        transport.send.assert_not_called()

    def test_irreversible_runtime_failure_does_not_fall_back_to_coordinates(self):
        transport = _fake_transport(runtime_error=RuntimeError("reply lost after send"))
        receipt = self._run(transport, Mock(return_value=self.nodes), allow_coordinates=True)
        self.assertEqual((receipt["ok"], receipt["path"], receipt["attempts"]), (0, "ax", 1))
        self.assertEqual(
            [c.args[0] for c in transport.send.call_args_list],
            ["DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn"],
        )

    def test_current_reversible_executor_can_fallback_after_a_lost_runtime_reply(self):
        """Observed gap: a possibly delivered mutation can lead to pointer fallback."""
        transport = _fake_transport(runtime_error=RuntimeError("reply lost after send"))
        action = dict(self.action, irreversible=False)
        receipt = self._run(
            transport, Mock(return_value=self.nodes), action=action, allow_coordinates=True
        )
        self.assertEqual((receipt["ok"], receipt["path"], receipt["attempts"]), (1, "coordinates", 2))
        self.assertEqual(
            [c.args[0] for c in transport.send.call_args_list],
            ["DOM.scrollIntoViewIfNeeded", "DOM.resolveNode", "Runtime.callFunctionOn",
             "Input.dispatchMouseEvent", "Input.dispatchMouseEvent", "Input.dispatchMouseEvent"],
        )

    def _missing_fresh_snapshot_trial(self):
        events = []
        transport = _fake_transport(events)
        read_count = []

        def read_nodes():
            read_count.append(None)
            if len(read_count) == 1:
                events.append("fresh-gate:StopIteration")
                raise StopIteration("fresh snapshot unavailable before dispatch")
            events.append("post-state")
            return self.nodes

        outcome = {"error": None, "receipt": None}
        try:
            outcome["receipt"] = self._run(transport, read_nodes)
        except Exception as exc:
            outcome["error"] = exc
        return transport, events, outcome

    def test_current_missing_fresh_snapshot_gate_defect_dispatch_trace(self):
        transport, events, outcome = self._missing_fresh_snapshot_trial()
        self.assertIsInstance(outcome["error"], cdp_executor.ActionError)
        self.assertIsNone(outcome["receipt"])
        self.assertEqual(transport.send.call_count, 0)
        self.assertEqual(events, ["fresh-gate:StopIteration"])

    def test_missing_fresh_snapshot_gate_defect_must_fail_closed_before_any_transport(self):
        """Desired contract; fails today with intact fingerprints and an irreversible click."""
        transport, events, outcome = self._missing_fresh_snapshot_trial()
        # Evaluate both safety predicates together so the failure records the
        # missing ActionError AND the transport calls after the failed gate.
        self.assertEqual(
            (isinstance(outcome["error"], cdp_executor.ActionError), transport.send.call_count),
            (True, 0),
            "missing-fresh-snapshot gate defect: unobserved freshness cannot authorize "
            "mutation; error=%r; events=%r" % (outcome["error"], events),
        )


# LOCAL PROPOSAL reference model only. These are not new production interfaces.


class _ProposalRejected(ValueError):
    pass


class _ProposalOwnershipError(RuntimeError):
    pass


class _ProposalBusy(RuntimeError):
    def __init__(self, metadata):
        self.metadata = dict(metadata)
        super().__init__("surface already owned by %s" % metadata["actor_id"])


class _ProposalOwnerToken:
    def __init__(self, actor_id, surface):
        self.actor_id = actor_id
        self.surface = surface


def _proposal_key(value):
    if not isinstance(value, str) or not value.strip():
        raise _ProposalRejected("ownership and surface keys must be nonempty strings")
    return value


def _proposal_choose_path(available):
    if isinstance(available, (str, bytes)):
        raise _ProposalRejected("available paths must be a collection, not one string")
    if not isinstance(available, (list, tuple, set, frozenset)):
        raise _ProposalRejected("available paths must be a finite collection")
    if not all(isinstance(path, str) for path in available):
        raise _ProposalRejected("every available path label must be a string")
    return actor.choose_path(available)


class _ProposalRegistry:
    """Logical shared ownership only; the host must supply canonical surface keys."""
    def __init__(self):
        self._surfaces = {}

    def handle(self, surface):
        surface = _proposal_key(surface)
        slot = self._surfaces.setdefault(surface, {"state": None})
        return _ProposalHandle(surface, slot)


class _ProposalHandle:
    def __init__(self, surface, slot):
        self.surface = surface
        self._slot = slot

    def inspect(self):
        state = self._slot["state"]
        if state is None:
            return None
        token, actor_id, phase = state
        return {"surface": self.surface, "actor_id": actor_id,
                "token": token, "phase": phase}

    def acquire(self, actor_id):
        actor_id = _proposal_key(actor_id)
        if self.inspect() is not None:
            raise _ProposalBusy(self.inspect())
        token = _ProposalOwnerToken(actor_id, self.surface)
        # One logical transition publishes ownership and metadata together.
        # This is NOT a claim of atomicity across real threads or processes.
        # Registry authority is independent of mutable token diagnostics.
        self._slot["state"] = (token, actor_id, "acquired")
        return token

    def _require_owner(self, actor_id, token):
        current = self.inspect()
        if (current is None or current["token"] is not token
                or current["actor_id"] != actor_id):
            raise _ProposalOwnershipError("release or phase change by a different owner")

    def phase(self, actor_id, token, phase):
        self._require_owner(actor_id, token)
        self._slot["state"] = (token, actor_id, _proposal_key(phase))

    def release(self, actor_id, token):
        self._require_owner(actor_id, token)
        self._slot["state"] = None


class _ProposalAdapter:
    """Single-attempt, injected transaction sketch; not actor.execute wiring.

    Every selected path uses the same supplied surface handle. Fresh validation,
    dispatch, verification, and cleanup all retain the token. Any dispatch error
    stays UNKNOWN even if the subsequent read succeeds: conservative reference
    policy, without a real semantic reconciliation or retry protocol. Failed
    cleanup/release quarantines the still-owned surface pending host recovery.
    """
    def __init__(self, handle, dispatchers):
        self.handle = handle
        self.dispatchers = dispatchers

    def execute(self, actor_id, available, read_fresh, validate, verify, cleanup,
                irreversible=False):
        path = _proposal_choose_path(available)
        if not isinstance(irreversible, bool):
            raise _ProposalRejected("irreversible must be a bool")
        if irreversible and path == "coordinates":
            raise _ProposalRejected("coordinates cannot authorize irreversible actions")
        if path is None or path not in self.dispatchers:
            raise _ProposalRejected("selected dispatcher is unavailable")
        token = self.handle.acquire(actor_id)
        receipt = {"path": path, "status": "REJECTED", "primary_status": None, "validation_error": None,
                   "dispatch_error": None, "verification_error": None, "cleanup_error": None}
        try:
            self.handle.phase(actor_id, token, "fresh_validation")
            try:
                fresh = read_fresh()
                if not isinstance(fresh, (list, tuple)):
                    raise _ProposalRejected("fresh snapshot was not observed")
                target = validate(fresh)
                if target is None:
                    raise _ProposalRejected("fresh target validation refused")
            except AssertionError:
                # Assertions inside deterministic test callbacks must remain
                # test failures, not become modeled action-error receipts.
                raise
            except Exception as exc:
                receipt["validation_error"] = str(exc) or type(exc).__name__
            else:
                self.handle.phase(actor_id, token, "dispatch")
                try:
                    self.dispatchers[path](target)
                except AssertionError:
                    raise
                except Exception as exc:
                    receipt["dispatch_error"] = str(exc) or type(exc).__name__
                self.handle.phase(actor_id, token, "verification")
                verified = False
                try:
                    verified = verify(target) is True
                except AssertionError:
                    raise
                except Exception as exc:
                    receipt["verification_error"] = str(exc) or type(exc).__name__
                receipt["status"] = (
                    "VERIFIED" if verified and receipt["dispatch_error"] is None else "UNKNOWN"
                )
        finally:
            receipt["primary_status"] = receipt["status"]
            self.handle.phase(actor_id, token, "cleanup")
            try:
                cleanup()
                self.handle.release(actor_id, token)
            except AssertionError:
                raise
            except Exception as exc:
                receipt["cleanup_error"] = str(exc) or type(exc).__name__
                receipt["status"] = "QUARANTINED"
                self.handle.phase(actor_id, token, "quarantined")
        return receipt


class TestLocalProposalOwnershipModel(unittest.TestCase):
    """PROPOSAL conformance only; no production ownership repair is asserted."""

    def setUp(self):
        self.registry = _ProposalRegistry()
        self.first = self.registry.handle("surface-a")
        self.second = self.registry.handle("surface-a")

    def test_wrong_actor_release_preserves_token_metadata_and_exclusion(self):
        token = self.first.acquire("actor-a")
        before = self.second.inspect()
        token.actor_id = "actor-b"
        token.surface = "surface-b"
        with self.assertRaises(_ProposalOwnershipError):
            self.second.release("actor-b", token)
        self.assertEqual(self.first.inspect(), before)
        with self.assertRaises(_ProposalBusy) as busy:
            self.second.acquire("actor-b")
        self.assertEqual(busy.exception.metadata, before)
        self.first.release("actor-a", token)
        self.assertIsNone(self.second.inspect())

    def test_stale_same_name_token_cannot_release_a_new_acquisition(self):
        old = self.first.acquire("actor-a")
        self.first.release("actor-a", old)
        current = self.second.acquire("actor-a")
        self.assertIsNot(current, old)
        before = self.second.inspect()
        with self.assertRaises(_ProposalOwnershipError):
            self.first.release("actor-a", old)
        self.assertEqual(self.first.inspect(), before)
        self.second.release("actor-a", current)

    def test_same_actor_cannot_reenter_a_surface_with_a_second_handle(self):
        token = self.first.acquire("actor-a")
        with self.assertRaises(_ProposalBusy):
            self.second.acquire("actor-a")
        self.assertIs(self.second.inspect()["token"], token)
        self.first.release("actor-a", token)

    def test_nonempty_owner_and_surface_keys_are_required_before_acquisition(self):
        for invalid in (None, "", " ", 0, False):
            with self.subTest(value=invalid):
                with self.assertRaises(_ProposalRejected):
                    self.first.acquire(invalid)
                self.assertIsNone(self.first.inspect())
                with self.assertRaises(_ProposalRejected):
                    self.registry.handle(invalid)

    def test_single_path_strings_are_rejected_by_the_proposed_boundary(self):
        for path in actor.PATHS:
            with self.subTest(path=path):
                with self.assertRaises(_ProposalRejected):
                    _proposal_choose_path(path)
                self.assertEqual(_proposal_choose_path([path]), path)

    def test_proposed_selector_preserves_no_match_and_rejects_malformed_collections(self):
        for available in ([], (), set(), frozenset(), ["unknown"]):
            with self.subTest(available=available):
                self.assertIsNone(_proposal_choose_path(available))
                with patch.object(self.first, "acquire") as acquire:
                    with self.assertRaises(_ProposalRejected):
                        _ProposalAdapter(self.first, {"api": Mock()}).execute(
                            "actor-a", available, Mock(), Mock(), Mock(), Mock()
                        )
                    acquire.assert_not_called()
        for available in (None, 7, b"api", iter(["api"]), ["api", None], [False], [["api"]]):
            with self.subTest(available=available):
                with self.assertRaises(_ProposalRejected):
                    _proposal_choose_path(available)
        for available in (["ax"], ("ax",), {"ax"}, frozenset({"ax"})):
            with self.subTest(available=available):
                self.assertEqual(_proposal_choose_path(available), "ax")


class TestLocalProposalAdapterModel(unittest.TestCase):
    """Callbacks specify lock scope and no-fallback; real host evidence is AUSENTE."""

    def _dependencies(self):
        return {
            "read_fresh": Mock(return_value=[{"id": "target"}]),
            "validate": Mock(side_effect=lambda nodes: nodes[0]),
            "verify": Mock(return_value=True),
            "cleanup": Mock(),
        }

    def test_two_handles_share_ownership_across_all_paths_and_lifecycle_callbacks(self):
        for selected in actor.PATHS:
            with self.subTest(selected=selected):
                registry = _ProposalRegistry()
                owned = registry.handle("surface-a")
                contender = registry.handle("surface-a")
                self.assertIsNot(owned, contender)
                self.assertIs(owned._slot, contender._slot)
                observed = []

                def checkpoint(label, phase):
                    metadata = owned.inspect()
                    self.assertEqual(metadata["actor_id"], "actor-a")
                    self.assertEqual(metadata["surface"], "surface-a")
                    self.assertEqual(metadata["phase"], phase)
                    self.assertIsNotNone(metadata["token"])
                    observed.append(label)
                    for competing_path in actor.PATHS:
                        dependencies = self._dependencies()
                        dispatchers = {p: Mock() for p in actor.PATHS}
                        attempt = _ProposalAdapter(contender, dispatchers)
                        with self.assertRaises(_ProposalBusy) as busy:
                            attempt.execute("actor-b", [competing_path], **dependencies)
                        self.assertEqual(busy.exception.metadata, metadata)
                        self.assertEqual(owned.inspect(), metadata)
                        for dependency in dependencies.values():
                            dependency.assert_not_called()
                        for dispatcher in dispatchers.values():
                            dispatcher.assert_not_called()

                def read_fresh():
                    checkpoint("fresh_read", "fresh_validation")
                    return [{"id": "target"}]

                def validate(nodes):
                    checkpoint("fresh_validate", "fresh_validation")
                    return nodes[0]

                def dispatch(target):
                    checkpoint("dispatch", "dispatch")

                def verify(target):
                    checkpoint("verification", "verification")
                    return True

                def cleanup():
                    checkpoint("cleanup", "cleanup")

                adapter = _ProposalAdapter(owned, {selected: dispatch})
                receipt = adapter.execute(
                    "actor-a", [selected], read_fresh, validate, verify, cleanup
                )
                self.assertEqual(receipt["status"], "VERIFIED")
                self.assertEqual(observed, ["fresh_read", "fresh_validate", "dispatch", "verification", "cleanup"])
                self.assertIsNone(owned.inspect())
                token = contender.acquire("actor-b")
                contender.release("actor-b", token)

    def test_distinct_surfaces_progress_independently_inside_an_owned_callback(self):
        registry = _ProposalRegistry()
        first = registry.handle("surface-a")
        second = registry.handle("surface-b")
        self.assertIsNot(first._slot, second._slot)
        nested = []

        def first_dispatch(target):
            before = first.inspect()

            def second_dispatch(other_target):
                self.assertEqual(first.inspect(), before)
                self.assertEqual(second.inspect()["actor_id"], "actor-b")
                self.assertIsNot(second.inspect()["token"], before["token"])

            other = _ProposalAdapter(second, {"coordinates": second_dispatch})
            nested.append(other.execute("actor-b", ["coordinates"], **self._dependencies()))
            self.assertEqual(first.inspect(), before)
            self.assertIsNone(second.inspect())

        adapter = _ProposalAdapter(first, {"api": first_dispatch})
        receipt = adapter.execute("actor-a", ["api"], **self._dependencies())
        self.assertEqual(receipt["status"], "VERIFIED")
        self.assertEqual(nested[0]["status"], "VERIFIED")
        self.assertIsNone(first.inspect())

    def test_ambiguous_dispatch_has_one_attempt_no_weaker_fallback_and_owned_verification(self):
        for selected, available in (("api", ["api", "ax", "coordinates"]),
                                    ("ax", ["ax", "coordinates"])):
            with self.subTest(selected=selected):
                handle = _ProposalRegistry().handle("surface-a")
                dispatchers = {p: Mock() for p in actor.PATHS}
                dispatchers[selected].side_effect = RuntimeError("mutation sent; reply lost")
                dependencies = self._dependencies()

                def verify(target):
                    self.assertEqual(handle.inspect()["phase"], "verification")
                    self.assertEqual(handle.inspect()["actor_id"], "actor-a")
                    return False

                dependencies["verify"].side_effect = verify
                receipt = _ProposalAdapter(handle, dispatchers).execute(
                    "actor-a", available, **dependencies
                )
                self.assertEqual(receipt["status"], "UNKNOWN")
                self.assertEqual(receipt["dispatch_error"], "mutation sent; reply lost")
                dispatchers[selected].assert_called_once()
                for path in actor.PATHS:
                    if path != selected:
                        dispatchers[path].assert_not_called()
                dependencies["verify"].assert_called_once()
                dependencies["cleanup"].assert_called_once_with()
                self.assertIsNone(handle.inspect())

    def test_failed_or_unreadable_verification_never_retries_or_weakens_the_path(self):
        for failure in (False, RuntimeError("post-state unreadable")):
            with self.subTest(failure=failure):
                handle = _ProposalRegistry().handle("surface-a")
                dispatchers = {p: Mock() for p in actor.PATHS}
                dependencies = self._dependencies()
                if isinstance(failure, Exception):
                    dependencies["verify"].side_effect = failure
                else:
                    dependencies["verify"].return_value = failure
                receipt = _ProposalAdapter(handle, dispatchers).execute(
                    "actor-a", actor.PATHS, **dependencies
                )
                self.assertEqual(receipt["status"], "UNKNOWN")
                dispatchers["api"].assert_called_once()
                dispatchers["ax"].assert_not_called()
                dispatchers["coordinates"].assert_not_called()
                self.assertIsNone(handle.inspect())

    def test_missing_fresh_observation_rejects_without_dispatch_and_cleans_up_while_owned(self):
        for missing in (None, {}, StopIteration("fresh snapshot unavailable")):
            with self.subTest(missing=missing):
                handle = _ProposalRegistry().handle("surface-a")
                dispatchers = {p: Mock() for p in actor.PATHS}
                dependencies = self._dependencies()
                if isinstance(missing, Exception):
                    dependencies["read_fresh"].side_effect = missing
                else:
                    dependencies["read_fresh"].return_value = missing

                def cleanup():
                    self.assertEqual(handle.inspect()["phase"], "cleanup")
                    self.assertEqual(handle.inspect()["actor_id"], "actor-a")

                dependencies["cleanup"].side_effect = cleanup
                receipt = _ProposalAdapter(handle, dispatchers).execute(
                    "actor-a", ["ax"], **dependencies
                )
                self.assertEqual(receipt["status"], "REJECTED")
                self.assertTrue(receipt["validation_error"])
                for dispatcher in dispatchers.values():
                    dispatcher.assert_not_called()
                dependencies["validate"].assert_not_called()
                dependencies["verify"].assert_not_called()
                dependencies["cleanup"].assert_called_once_with()
                self.assertIsNone(handle.inspect())

    def test_coordinates_are_forbidden_for_irreversible_requests_before_acquisition(self):
        handle = _ProposalRegistry().handle("surface-a")
        dispatchers = {p: Mock() for p in actor.PATHS}
        dependencies = self._dependencies()
        with self.assertRaises(_ProposalRejected):
            _ProposalAdapter(handle, dispatchers).execute(
                "actor-a", ["coordinates"], irreversible=True, **dependencies
            )
        for dependency in dependencies.values():
            dependency.assert_not_called()
        for dispatcher in dispatchers.values():
            dispatcher.assert_not_called()
        self.assertIsNone(handle.inspect())

    def test_cleanup_error_preserves_ambiguous_outcome_and_quarantines_ownership(self):
        handle = _ProposalRegistry().handle("surface-a")
        dispatchers = {"ax": Mock(side_effect=RuntimeError("mutation outcome unknown"))}
        dependencies = self._dependencies()

        def cleanup():
            metadata = handle.inspect()
            self.assertEqual(metadata["phase"], "cleanup")
            with self.assertRaises(_ProposalOwnershipError):
                handle.release("actor-b", metadata["token"])
            self.assertEqual(handle.inspect(), metadata)
            raise RuntimeError("cleanup failed")

        dependencies["cleanup"].side_effect = cleanup
        receipt = _ProposalAdapter(handle, dispatchers).execute(
            "actor-a", ["ax"], **dependencies
        )
        self.assertEqual(receipt["status"], "QUARANTINED")
        self.assertEqual(receipt["primary_status"], "UNKNOWN")
        self.assertEqual(receipt["dispatch_error"], "mutation outcome unknown")
        self.assertEqual(receipt["cleanup_error"], "cleanup failed")
        self.assertEqual(handle.inspect()["actor_id"], "actor-a")
        self.assertEqual(handle.inspect()["phase"], "quarantined")
        with self.assertRaises(_ProposalBusy) as busy:
            handle.acquire("actor-b")
        self.assertEqual(busy.exception.metadata, handle.inspect())

    def test_release_failure_retains_owner_metadata_and_blocks_surface_reuse(self):
        handle = _ProposalRegistry().handle("surface-a")
        dependencies = self._dependencies()
        adapter = _ProposalAdapter(handle, {"ax": Mock()})
        with patch.object(handle, "release", side_effect=RuntimeError("release failed")):
            receipt = adapter.execute("actor-a", ["ax"], **dependencies)
        self.assertEqual(receipt["status"], "QUARANTINED")
        self.assertEqual(receipt["primary_status"], "VERIFIED")
        self.assertEqual(receipt["cleanup_error"], "release failed")
        self.assertEqual(handle.inspect()["actor_id"], "actor-a")
        self.assertEqual(handle.inspect()["phase"], "quarantined")
        with self.assertRaises(_ProposalBusy):
            handle.acquire("actor-b")


if __name__ == "__main__":
    unittest.main()
