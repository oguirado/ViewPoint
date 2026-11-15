"""Tests for product/admission (SPEC.md).

Run: python3 -m unittest discover -s product/admission -p 'test_*.py' -v

Structure:
* ``NegativeControl*`` classes: one dedicated class per verdict/refusal code.
  Each test drives an invalid/adversarial input into the controller, asserts
  the exact typed code, and asserts the success path was NOT taken (no
  dispatch token, actuator free, epochs and head frame unchanged where the
  spec says so). Outcome codes also carry a contrast case: the neighbouring
  input that must NOT produce that code.
* ``MultiPeerConcurrency``: real ``threading`` peers contending over one
  shared controller, checked against a ground-truth actuation log kept by
  the test (not by the controller).
"""

from __future__ import annotations

import os
import random
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from admission import (  # noqa: E402
    GUI_TURN_CAP_MS,
    SCRIPT_TURN_CAP_MS,
    AdmissionController,
    Code,
    ExecutorOutcome,
    PostCapture,
    Proposal,
    inflate,
    intersects,
)

T0 = 1_000_000
TTL = 300_000
LEFT = (0, 0, 640, 1080)
MID = (640, 0, 640, 1080)
RIGHT = (1280, 0, 640, 1080)
FULL = (0, 0, 1920, 1080)


def gui_actor(c, actor="cam", region=FULL, now=T0, ttl=TTL):
    return c.issue_lease(actor, now, ttl_ms=ttl, allow_gui=True,
                         allowed_regions=[region])


def script_actor(c, actor="modeler", prefixes=("obj:CHAR_",), now=T0, ttl=TTL):
    return c.issue_lease(actor, now, ttl_ms=ttl, write_prefixes=prefixes)


_ids = iter(range(10**9))


def gui_prop(lease, frame, *, target=(100, 100, 50, 50), footprint=(90, 90, 70, 70),
             pid=None, action_class="input", **kw):
    return Proposal(
        proposal_id=pid or f"p-{next(_ids)}", actor_id=lease.actor_id, lane="gui",
        lease_id=lease.lease_id, lease_epoch=lease.lease_epoch,
        based_on_frame=frame.frame_seq, view_epoch=frame.view_epoch,
        scene_epoch=frame.scene_epoch, based_on_sha256=frame.sha256,
        target_region=target, footprint=footprint, action_class=action_class, **kw)


def script_prop(lease, frame, *, write_set=("obj:CHAR_body",), pid=None, **kw):
    return Proposal(
        proposal_id=pid or f"s-{next(_ids)}", actor_id=lease.actor_id, lane="script",
        lease_id=lease.lease_id, lease_epoch=lease.lease_epoch,
        based_on_frame=frame.frame_seq, view_epoch=frame.view_epoch,
        scene_epoch=frame.scene_epoch, write_set=tuple(write_set), **kw)


def ok_gui(regions=((100, 100, 20, 20),)):
    return PostCapture(changed_regions=tuple(regions)), ExecutorOutcome(rc=0, elapsed_ms=50)


class Base(unittest.TestCase):
    def setUp(self):
        self.c = AdmissionController(now_ms=T0)

    def assertRefused(self, decision, code):
        self.assertIs(decision.code, code, decision.reason)
        self.assertFalse(decision.admitted)
        self.assertIsNone(decision.dispatch_token)

    def snapshot(self):
        return (self.c.head_frame, self.c.epochs, self.c.actuator_busy)

    def admit(self, prop, now=T0):
        d = self.c.propose(prop, now)
        self.assertTrue(d.admitted, f"{d.code}: {d.reason}")
        self.assertIsNotNone(d.dispatch_token)
        return d


# ==========================================================================
# admission codes
# ==========================================================================

class NegativeControlADMITTED(Base):
    def test_fresh_proposal_admitted_and_takes_actuator(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f))
        self.assertIs(d.code, Code.ADMITTED)
        self.assertFalse(d.rebased)
        self.assertTrue(self.c.actuator_busy)

    def test_contrast_same_proposal_from_unleased_actor_not_admitted(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        forged = gui_prop(lease, f)
        forged = Proposal(**{**forged.__dict__, "actor_id": "intruder"})
        self.assertRefused(self.c.propose(forged, T0), Code.NO_LEASE)
        self.assertFalse(self.c.actuator_busy)


class NegativeControlADMITTED_REBASED(Base):
    def test_disjoint_foreign_change_rebases(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(1500, 800, 40, 40)], attributed_to="other")
        d = self.admit(gui_prop(lease, f))
        self.assertIs(d.code, Code.ADMITTED_REBASED)
        self.assertTrue(d.rebased)
        self.assertEqual(d.head_frame, f.frame_seq + 1)

    def test_contrast_change_inside_margin_is_not_rebased(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        # footprint ends at x=160; change starts at 164 (< 8 px margin)
        self.c.capture(T0 + 1, [(164, 100, 10, 10)], attributed_to="other")
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0), Code.STALE_REGION)


# ==========================================================================
# GUI outcomes
# ==========================================================================

class NegativeControlVERIFIED(Base):
    def test_rc0_change_inside_footprint_verified_view_change_bumps_epoch(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f, action_class="view_change"))
        r = self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 10)
        self.assertIs(r.verdict, Code.VERIFIED)
        self.assertEqual(r.view_epoch, f.view_epoch + 1)
        self.assertEqual(r.verify_frame_ref, f.frame_seq + 1)
        self.assertEqual(r.spill_regions, ())
        self.assertFalse(self.c.actuator_busy)

    def test_input_class_verified_without_view_epoch_bump(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f, action_class="input"))
        r = self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 10)
        self.assertIs(r.verdict, Code.VERIFIED)
        self.assertEqual(r.view_epoch, f.view_epoch)

    def test_contrast_rc0_with_zero_change_is_not_verified(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f))
        r = self.c.wait_verify(d.dispatch_token, PostCapture(changed_regions=()),
                               ExecutorOutcome(rc=0), T0 + 10)
        self.assertIs(r.verdict, Code.INPUT_UNCERTAIN)


class NegativeControlVERIFIED_WITH_SPILL(Base):
    def test_change_outside_footprint_reported_as_spill(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f))
        post, out = ok_gui([(100, 100, 20, 20), (900, 900, 30, 30)])
        r = self.c.wait_verify(d.dispatch_token, post, out, T0 + 10)
        self.assertIs(r.verdict, Code.VERIFIED_WITH_SPILL)
        self.assertNotEqual(r.verdict, Code.VERIFIED)
        self.assertEqual(r.own_regions, ((100, 100, 20, 20),))
        self.assertEqual(r.spill_regions, ((900, 900, 30, 30),))
        # spill is attributed in the published frame, so it stales overlapping proposals
        self.assertEqual(self.c.frame(r.verify_frame_ref).changed_regions,
                         ((100, 100, 20, 20), (900, 900, 30, 30)))


class NegativeControlNO_VISIBLE_CHANGE(Base):
    def test_rc3_and_zero_diff(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f, action_class="view_change"))
        r = self.c.wait_verify(d.dispatch_token, PostCapture(changed_regions=()),
                               ExecutorOutcome(rc=3), T0 + 10)
        self.assertIs(r.verdict, Code.NO_VISIBLE_CHANGE)
        self.assertEqual(r.view_epoch, f.view_epoch)  # no epoch bump
        self.assertFalse(self.c.in_reconcile)

    def test_contrast_rc3_contradicted_by_visible_change(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(lease, f))
        r = self.c.wait_verify(d.dispatch_token, PostCapture(changed_regions=((100, 100, 5, 5),)),
                               ExecutorOutcome(rc=3), T0 + 10)
        self.assertIs(r.verdict, Code.INPUT_UNCERTAIN)


class NegativeControlINPUT_UNCERTAIN(Base):
    def _dispatch(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        return lease, f, self.admit(gui_prop(lease, f))

    def test_ambiguous_rc_enters_reconcile_and_blocks_actuator(self):
        lease, f, d = self._dispatch()
        head = self.c.head_frame
        r = self.c.wait_verify(d.dispatch_token, PostCapture(((100, 100, 5, 5),)),
                               ExecutorOutcome(rc=2), T0 + 10)
        self.assertIs(r.verdict, Code.INPUT_UNCERTAIN)
        self.assertIsNone(r.verify_frame_ref)
        self.assertEqual(self.c.head_frame, head)  # nothing published
        self.assertTrue(self.c.in_reconcile)
        f2 = self.c.observe("cam", T0 + 11)
        self.assertRefused(self.c.propose(gui_prop(lease, f2), T0 + 11), Code.ACTUATOR_BUSY)
        g = self.c.resolve_reconcile(T0 + 12)
        self.assertIsNone(g.changed_regions)
        # the pre-reconcile frame can never be rebased across the reconcile
        self.assertRefused(self.c.propose(gui_prop(lease, f2), T0 + 13), Code.STALE_VIEW)
        self.admit(gui_prop(lease, self.c.observe("cam", T0 + 14)), T0 + 14)

    def test_missing_rc_or_missing_capture(self):
        for post, out in ((PostCapture(((100, 100, 5, 5),)), None),
                          (None, ExecutorOutcome(rc=0)),
                          (PostCapture(None), ExecutorOutcome(rc=0)),
                          (PostCapture(((5000, 0, 5, 5),)), ExecutorOutcome(rc=0))):
            with self.subTest(post=post, out=out):
                self.c = AdmissionController(now_ms=T0)
                _, _, d = self._dispatch()
                r = self.c.wait_verify(d.dispatch_token, post, out, T0 + 10)
                self.assertIs(r.verdict, Code.INPUT_UNCERTAIN)
                self.assertTrue(self.c.in_reconcile)


# ==========================================================================
# script outcomes
# ==========================================================================

class NegativeControlSTATE_VERIFIED(Base):
    def test_correlated_ok_receipt_bumps_scene_epoch(self):
        lease = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        p = script_prop(lease, f)
        d = self.admit(p)
        r = self.c.wait_verify(d.dispatch_token, PostCapture(((400, 300, 50, 50),)),
                               ExecutorOutcome(status="OK", event_id=p.proposal_id), T0 + 10)
        self.assertIs(r.verdict, Code.STATE_VERIFIED)
        self.assertEqual(r.scene_epoch, f.scene_epoch + 1)

    def test_contrast_ok_receipt_for_another_event_is_uncertain(self):
        lease = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        d = self.admit(script_prop(lease, f))
        r = self.c.wait_verify(d.dispatch_token, PostCapture(()),
                               ExecutorOutcome(status="OK", event_id="someone-else"), T0 + 10)
        self.assertIs(r.verdict, Code.OUTCOME_UNCERTAIN)
        self.assertEqual(r.scene_epoch, f.scene_epoch)

    def test_contrast_ok_with_error_text_is_not_verified(self):
        lease = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        p = script_prop(lease, f)
        d = self.admit(p)
        r = self.c.wait_verify(d.dispatch_token, PostCapture(()),
                               ExecutorOutcome(status="OK", event_id=p.proposal_id,
                                               error="Traceback"), T0 + 10)
        self.assertIs(r.verdict, Code.OUTCOME_UNCERTAIN)


class NegativeControlJOB_ERROR(Base):
    def test_error_receipt_no_scene_bump_and_frame_unattributed(self):
        g = gui_actor(self.c)
        lease = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        p = script_prop(lease, f)
        d = self.admit(p)
        r = self.c.wait_verify(d.dispatch_token, PostCapture(((1500, 900, 5, 5),)),
                               ExecutorOutcome(status="ERROR", event_id=p.proposal_id,
                                               error="NameError"), T0 + 10)
        self.assertIs(r.verdict, Code.JOB_ERROR)
        self.assertEqual(r.scene_epoch, f.scene_epoch)
        self.assertIsNone(self.c.frame(r.verify_frame_ref).changed_regions)
        # a failed job proves nothing about extent: GUI proposals on the old frame refuse
        self.assertRefused(self.c.propose(gui_prop(g, f), T0 + 11), Code.STALE_FRAME)


class NegativeControlOUTCOME_UNCERTAIN(Base):
    def test_missing_or_malformed_receipt(self):
        for out in (None, ExecutorOutcome(status="MAYBE", event_id=None)):
            with self.subTest(out=out):
                self.c = AdmissionController(now_ms=T0)
                lease = script_actor(self.c)
                f = self.c.observe("modeler", T0)
                p = script_prop(lease, f)
                if out is not None:
                    out = ExecutorOutcome(status="MAYBE", event_id=p.proposal_id)
                d = self.admit(p)
                r = self.c.wait_verify(d.dispatch_token, PostCapture(()), out, T0 + 10)
                self.assertIs(r.verdict, Code.OUTCOME_UNCERTAIN)
                self.assertTrue(self.c.in_reconcile)
                # never auto-repeats: the same id is consumed
                self.assertRefused(self.c.propose(p, T0 + 11), Code.DUPLICATE_EVENT)


# ==========================================================================
# freshness refusals
# ==========================================================================

class NegativeControlSTALE_FRAME(Base):
    def test_unattributed_foreign_change_refuses(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, None)  # unknown extent
        before = self.snapshot()
        d = self.c.propose(gui_prop(lease, f), T0 + 2)
        self.assertRefused(d, Code.STALE_FRAME)
        self.assertEqual(d.drops, 1)
        self.assertEqual(self.snapshot(), before)

    def test_no_region_safety_refuses(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(1500, 800, 4, 4)])
        self.assertRefused(self.c.propose(gui_prop(lease, f, target=None), T0), Code.STALE_FRAME)
        self.assertRefused(self.c.propose(gui_prop(lease, f, allow_rebase=False), T0),
                           Code.STALE_FRAME)

    def test_forged_binding_or_future_frame_refuses(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        forged = Proposal(**{**gui_prop(lease, f).__dict__, "based_on_sha256": "0" * 64})
        self.assertRefused(self.c.propose(forged, T0), Code.STALE_FRAME)
        future = Proposal(**{**gui_prop(lease, f).__dict__, "based_on_frame": f.frame_seq + 5})
        self.assertRefused(self.c.propose(future, T0), Code.STALE_FRAME)
        lying = Proposal(**{**gui_prop(lease, f).__dict__, "view_epoch": f.view_epoch + 1})
        self.assertRefused(self.c.propose(lying, T0), Code.STALE_FRAME)

    def test_three_drops_park_the_event(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, None)
        p = gui_prop(lease, f)
        for n in (1, 2, 3):
            d = self.c.propose(p, T0 + 2)
            self.assertRefused(d, Code.STALE_FRAME)
            self.assertEqual(d.drops, n)
        self.assertTrue(d.parked)
        self.assertRefused(self.c.propose(p, T0 + 3), Code.DUPLICATE_EVENT)


class NegativeControlSTALE_REGION(Base):
    def test_overlapping_change_refuses(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(1500, 800, 4, 4)])  # disjoint
        self.c.capture(T0 + 2, [(120, 120, 10, 10)])  # overlaps target
        before = self.snapshot()
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 3), Code.STALE_REGION)
        self.assertEqual(self.snapshot(), before)

    def test_change_touching_only_footprint_margin_refuses(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(90, 162, 10, 10)])  # footprint bottom 160, margin 8
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 2), Code.STALE_REGION)

    def test_contrast_change_beyond_margin_rebases(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(90, 168, 10, 10)])
        self.assertIs(self.c.propose(gui_prop(lease, f), T0 + 2).code, Code.ADMITTED_REBASED)


class NegativeControlSTALE_VIEW(Base):
    def test_view_epoch_change_refuses_even_if_region_disjoint(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(1500, 800, 4, 4)], view_changed=True)
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 2), Code.STALE_VIEW)

    def test_verified_view_change_stales_peer(self):
        a = gui_actor(self.c, "a", LEFT)
        b = gui_actor(self.c, "b", RIGHT)
        f = self.c.observe("a", T0)
        pb = gui_prop(b, f, target=(1400, 100, 50, 50), footprint=(1390, 90, 70, 70))
        d = self.admit(gui_prop(a, f, action_class="view_change"))
        self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 1)
        self.assertRefused(self.c.propose(pb, T0 + 2), Code.STALE_VIEW)


class NegativeControlSTALE_SCENE(Base):
    def test_scene_epoch_change_refuses(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.capture(T0 + 1, [(1500, 800, 4, 4)], scene_changed=True)
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 2), Code.STALE_SCENE)

    def test_verified_script_job_stales_gui_peer(self):
        g = gui_actor(self.c)
        s = script_actor(self.c)
        f = self.c.observe("cam", T0)
        p = script_prop(s, f)
        d = self.admit(p)
        self.c.wait_verify(d.dispatch_token, PostCapture(((1500, 900, 5, 5),)),
                           ExecutorOutcome(status="OK", event_id=p.proposal_id), T0 + 1)
        self.assertRefused(self.c.propose(gui_prop(g, f), T0 + 2), Code.STALE_SCENE)


class NegativeControlFRAME_EVICTED(Base):
    def test_evicted_base_frame_refuses(self):
        self.c = AdmissionController(now_ms=T0, ring_size=4)
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        for i in range(4):
            self.c.capture(T0 + i, [(1500, 800, 4, 4)])
        self.assertIsNone(self.c.frame(f.frame_seq))
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 9), Code.FRAME_EVICTED)

    def test_surface_restart_evicts_every_frame(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        self.c.surface_restart(T0 + 1)
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 2), Code.FRAME_EVICTED)


# ==========================================================================
# authority, validation and lock refusals
# ==========================================================================

class NegativeControlNO_LEASE(Base):
    def test_never_leased_revoked_and_unknown_lease(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        stranger = Proposal(**{**gui_prop(lease, f).__dict__, "actor_id": "ghost"})
        self.assertRefused(self.c.propose(stranger, T0), Code.NO_LEASE)
        unknown = Proposal(**{**gui_prop(lease, f).__dict__, "lease_id": "lease-forged"})
        self.assertRefused(self.c.propose(unknown, T0), Code.NO_LEASE)
        self.assertTrue(self.c.revoke_lease("cam"))
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0), Code.NO_LEASE)
        self.assertFalse(self.c.actuator_busy)

    def test_lease_of_another_actor_is_not_borrowable(self):
        a = gui_actor(self.c, "a")
        gui_actor(self.c, "b")
        f = self.c.observe("b", T0)
        borrowed = Proposal(**{**gui_prop(a, f).__dict__, "actor_id": "b"})
        self.assertRefused(self.c.propose(borrowed, T0), Code.NO_LEASE)


class NegativeControlLEASE_EXPIRED(Base):
    def test_ttl_elapsed(self):
        lease = gui_actor(self.c, ttl=1000)
        f = self.c.observe("cam", T0)
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 1000), Code.LEASE_EXPIRED)

    def test_expire_leases_sweep(self):
        lease = gui_actor(self.c, ttl=1000)
        f = self.c.observe("cam", T0)
        self.assertEqual(self.c.expire_leases(T0 + 999), ())
        self.assertEqual(self.c.expire_leases(T0 + 1000), (lease.lease_id,))
        self.assertRefused(self.c.propose(gui_prop(lease, f), T0 + 1), Code.LEASE_EXPIRED)

    def test_stale_epoch_and_superseded_lease_after_renewal(self):
        old = gui_actor(self.c)
        new = gui_actor(self.c)
        self.assertEqual(new.lease_epoch, old.lease_epoch + 1)
        f = self.c.observe("cam", T0)
        self.assertRefused(self.c.propose(gui_prop(old, f), T0), Code.LEASE_EXPIRED)
        stale_epoch = Proposal(**{**gui_prop(new, f).__dict__, "lease_epoch": old.lease_epoch})
        self.assertRefused(self.c.propose(stale_epoch, T0), Code.LEASE_EXPIRED)
        self.admit(gui_prop(new, f))


class NegativeControlACTUATOR_BUSY(Base):
    def test_second_actor_refused_while_dispatch_in_flight(self):
        a = gui_actor(self.c, "a", LEFT)
        b = gui_actor(self.c, "b", RIGHT)
        f = self.c.observe("a", T0)
        d = self.admit(gui_prop(a, f))
        pb = gui_prop(b, f, target=(1400, 100, 50, 50), footprint=(1390, 90, 70, 70))
        self.assertRefused(self.c.propose(pb, T0 + 1), Code.ACTUATOR_BUSY)
        # refused id is not consumed: it can be resubmitted after the window closes
        self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 2)
        self.assertIs(self.c.propose(pb, T0 + 3).code, Code.ADMITTED_REBASED)

    def test_script_lane_also_serialised(self):
        g = gui_actor(self.c)
        s = script_actor(self.c)
        f = self.c.observe("cam", T0)
        self.admit(gui_prop(g, f))
        self.assertRefused(self.c.propose(script_prop(s, f), T0), Code.ACTUATOR_BUSY)


class NegativeControlFOOTPRINT_MISSING(Base):
    def test_missing_or_invalid_footprint(self):
        lease = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        for fp in (None, (1, 2, 3), (10, 10, 0, 5), (1900, 0, 50, 50), (-1, 0, 5, 5),
                   ("a", 0, 5, 5), (True, 0, 5, 5)):
            with self.subTest(fp=fp):
                self.assertRefused(self.c.propose(gui_prop(lease, f, footprint=fp), T0),
                                   Code.FOOTPRINT_MISSING)
        self.assertFalse(self.c.actuator_busy)


class NegativeControlCLAIM_REFUSED(Base):
    def test_gui_without_allow_gui(self):
        s = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        p = Proposal(**{**gui_prop(s, f).__dict__})
        self.assertRefused(self.c.propose(p, T0), Code.CLAIM_REFUSED)

    def test_write_set_outside_prefixes_or_empty(self):
        s = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        for ws in ((), ("obj:PROP_chair",), ("obj:CHAR_ok", "mat:steal")):
            with self.subTest(ws=ws):
                self.assertRefused(self.c.propose(script_prop(s, f, write_set=ws), T0),
                                   Code.CLAIM_REFUSED)

    def test_disallowed_keys_and_bad_lane_or_class(self):
        g = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        for kw in ({"keys": ("KP_1", "ctrl+q")}, {"keys": ()}, {"keys": ("KP_1",) * 9},
                   {"action_class": "state_job"}):
            with self.subTest(kw=kw):
                self.assertRefused(self.c.propose(gui_prop(g, f, **kw), T0), Code.CLAIM_REFUSED)
        bad_lane = Proposal(**{**gui_prop(g, f).__dict__, "lane": "shell"})
        self.assertRefused(self.c.propose(bad_lane, T0), Code.CLAIM_REFUSED)

    def test_footprint_or_target_outside_allowed_regions(self):
        g = gui_actor(self.c, "a", LEFT)
        f = self.c.observe("a", T0)
        self.assertRefused(self.c.propose(
            gui_prop(g, f, target=(100, 100, 5, 5), footprint=(600, 90, 70, 70)), T0),
            Code.CLAIM_REFUSED)
        self.assertRefused(self.c.propose(
            gui_prop(g, f, target=(700, 100, 5, 5), footprint=(90, 90, 70, 70)), T0),
            Code.CLAIM_REFUSED)

    def test_forged_dispatch_token_cannot_close_a_dispatch(self):
        g = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(g, f))
        r = self.c.wait_verify("f" * 32, *ok_gui(), T0 + 1)
        self.assertIs(r.verdict, Code.CLAIM_REFUSED)
        self.assertTrue(self.c.actuator_busy)  # real dispatch still open
        self.assertIs(self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 2).verdict,
                      Code.VERIFIED)


class NegativeControlDUPLICATE_EVENT(Base):
    def test_replayed_proposal_id(self):
        g = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        p = gui_prop(g, f)
        d = self.admit(p)
        self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 1)
        f2 = self.c.observe("cam", T0 + 2)
        replay = Proposal(**{**p.__dict__, "based_on_frame": f2.frame_seq,
                             "based_on_sha256": f2.sha256})
        self.assertRefused(self.c.propose(replay, T0 + 2), Code.DUPLICATE_EVENT)

    def test_second_wait_verify_never_double_closes(self):
        g = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(g, f, action_class="view_change"))
        r1 = self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 1)
        epochs, head = self.c.epochs, self.c.head_frame
        r2 = self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + 2)
        self.assertIs(r1.verdict, Code.VERIFIED)
        self.assertIs(r2.verdict, Code.DUPLICATE_EVENT)
        self.assertEqual((self.c.epochs, self.c.head_frame), (epochs, head))

    def test_concurrent_same_id_admits_exactly_once(self):
        g = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        p = gui_prop(g, f)
        barrier = threading.Barrier(16)
        codes = []
        lock = threading.Lock()

        def peer():
            barrier.wait()
            d = self.c.propose(p, T0)
            with lock:
                codes.append(d.code)

        ts = [threading.Thread(target=peer) for _ in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(codes.count(Code.ADMITTED), 1)
        self.assertEqual(codes.count(Code.DUPLICATE_EVENT), 15)


class NegativeControlTURN_TIMEOUT(Base):
    def test_late_verify_times_out_and_enters_reconcile(self):
        g = gui_actor(self.c)
        f = self.c.observe("cam", T0)
        d = self.admit(gui_prop(g, f))
        head = self.c.head_frame
        r = self.c.wait_verify(d.dispatch_token, *ok_gui(), T0 + GUI_TURN_CAP_MS + 1)
        self.assertIs(r.verdict, Code.TURN_TIMEOUT)
        self.assertEqual(self.c.head_frame, head)
        self.assertTrue(self.c.in_reconcile)

    def test_executor_elapsed_over_cap(self):
        s = script_actor(self.c)
        f = self.c.observe("modeler", T0)
        p = script_prop(s, f)
        d = self.admit(p)
        r = self.c.wait_verify(d.dispatch_token, PostCapture(()),
                               ExecutorOutcome(status="OK", event_id=p.proposal_id,
                                               elapsed_ms=SCRIPT_TURN_CAP_MS + 1), T0 + 5)
        self.assertIs(r.verdict, Code.TURN_TIMEOUT)
        self.assertEqual(self.c.epochs[1], f.scene_epoch)  # no scene bump

    def test_two_consecutive_timeouts_park_the_actor(self):
        g = gui_actor(self.c)
        now = T0
        for _ in range(2):
            f = self.c.observe("cam", now)
            d = self.admit(gui_prop(g, f), now)
            now += GUI_TURN_CAP_MS + 1
            self.assertIs(self.c.wait_verify(d.dispatch_token, *ok_gui(), now).verdict,
                          Code.TURN_TIMEOUT)
            self.c.resolve_reconcile(now)
        self.assertIsNone(self.c.lease_of("cam"))
        f = self.c.observe("cam", now)
        self.assertRefused(self.c.propose(gui_prop(g, f), now), Code.NO_LEASE)


class CodeCoverage(unittest.TestCase):
    def test_every_code_has_a_dedicated_negative_control_class(self):
        names = {n for n in globals() if n.startswith("NegativeControl")}
        missing = [c.value for c in Code if f"NegativeControl{c.value}" not in names]
        self.assertEqual(missing, [])


# ==========================================================================
# multi-peer concurrency
# ==========================================================================

class MultiPeerConcurrency(unittest.TestCase):
    """Real threads contend over observe -> propose -> wait_verify.

    The test owns an executor that refuses to run two actuations at once
    (non-blocking lock) and a ground-truth log of what each actuation
    changed. After the run every admitted proposal is re-checked against
    that log: no change attributed to another dispatch may lie, with margin,
    over its target/footprint between its base frame and its admission head
    (zero silent overwrite), and no epoch-changing actuation may lie in that
    window (rebase only within an unchanged view/scene).
    """

    PEER_ITERS = 60

    def test_contending_peers_zero_double_actuation_zero_silent_overwrite(self):
        c = AdmissionController(now_ms=T0, ring_size=64)
        exec_lock = threading.Lock()
        log_lock = threading.Lock()
        violations = []
        history = {}       # frame_seq -> (regions, epoch_kind, actor)
        admitted = []      # (actor, based_on, head, guards, rebased)
        codes = []
        in_exec = [0]
        max_in_exec = [0]
        margin = 8

        peers = []
        cols = [(i * 320, 0, 320, 1080) for i in range(5)]
        for i, col in enumerate(cols):
            peers.append(("gui", f"panel-{i}", col, "input"))
        peers.append(("gui", "sweeper", FULL, "input"))        # overlaps everyone
        peers.append(("gui", "camera", FULL, "view_change"))   # bumps view_epoch
        peers.append(("script", "modeler", None, "state_job"))  # bumps scene_epoch
        leases = {}
        for lane, actor, region, _ in peers:
            if lane == "gui":
                leases[actor] = c.issue_lease(actor, T0, ttl_ms=10**9, allow_gui=True,
                                              allowed_regions=[region])
            else:
                leases[actor] = c.issue_lease(actor, T0, ttl_ms=10**9,
                                              write_prefixes=("obj:CHAR_",))
        start = threading.Barrier(len(peers))

        def run(lane, actor, region, klass, seed):
            rnd = random.Random(seed)
            lease = leases[actor]
            start.wait()
            for it in range(self.PEER_ITERS):
                f = c.observe(actor, T0)
                if rnd.random() < 0.5:
                    threading.Event().wait(rnd.random() * 0.001)  # think time
                if lane == "script":
                    if rnd.random() < 0.85:
                        continue  # scene jobs are rare
                    p = script_prop(lease, f, pid=f"{actor}-{it}")
                    fp = None
                else:
                    if actor == "camera" and rnd.random() < 0.85:
                        continue  # view changes are rare
                    x, y, w, h = region
                    if actor == "sweeper":
                        tgt, fp = (0, 0, 1920, 1080), (0, 0, 1920, 1080)
                    else:
                        tx = x + rnd.randrange(16, w - 80)
                        ty = y + rnd.randrange(16, h - 80)
                        tgt, fp = (tx, ty, 40, 40), (tx - 8, ty - 8, 56, 56)
                    p = gui_prop(lease, f, target=tgt, footprint=fp,
                                 pid=f"{actor}-{it}", action_class=klass)
                # hold the proposal on its original base frame and retry while the
                # actuator is busy: by the time it frees, the base is usually stale
                for _ in range(200):
                    d = c.propose(p, T0)
                    with log_lock:
                        codes.append(d.code)
                    if d.code is not Code.ACTUATOR_BUSY:
                        break
                    threading.Event().wait(0.0001)
                if not d.admitted:
                    continue
                # ---- actuate (test-owned exclusive executor) ----
                if not exec_lock.acquire(blocking=False):
                    with log_lock:
                        violations.append(f"double actuation by {actor}")
                    continue
                try:
                    with log_lock:
                        in_exec[0] += 1
                        max_in_exec[0] = max(max_in_exec[0], in_exec[0])
                    threading.Event().wait(0.0003)
                    if lane == "script":
                        changed = ((rnd.randrange(0, 1800), rnd.randrange(0, 1000), 20, 20),)
                    else:
                        fx, fy, fw, fh = fp
                        changed = ((fx + 4, fy + 4, min(fw - 8, 40), min(fh - 8, 40)),)
                    with log_lock:
                        in_exec[0] -= 1
                finally:
                    exec_lock.release()
                if lane == "script":
                    out = ExecutorOutcome(status="OK", event_id=p.proposal_id)
                else:
                    out = ExecutorOutcome(rc=0)
                r = c.wait_verify(d.dispatch_token, PostCapture(changed), out, T0)
                kind = {"input": None, "view_change": "view", "state_job": "scene"}[klass]
                with log_lock:
                    if r.verdict not in (Code.VERIFIED, Code.STATE_VERIFIED):
                        violations.append(f"{actor}: unexpected verdict {r.verdict}")
                    if r.verify_frame_ref in history:
                        violations.append(f"frame {r.verify_frame_ref} written twice")
                    history[r.verify_frame_ref] = (changed, kind, actor)
                    if lane == "gui":
                        guards = (inflate(p.target_region, margin), inflate(p.footprint, margin))
                        admitted.append((actor, p.based_on_frame, d.head_frame, guards, d.rebased))

        threads = [threading.Thread(target=run, args=(*peer, 1000 + i))
                   for i, peer in enumerate(peers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
            self.assertFalse(t.is_alive(), "peer hung")

        self.assertEqual(violations, [])
        self.assertEqual(max_in_exec[0], 1)
        # every frame after the initial one came from a logged actuation
        self.assertEqual(sorted(history), list(range(1, c.head_frame + 1)))
        # contention really happened and every class of outcome was exercised
        self.assertGreater(codes.count(Code.ACTUATOR_BUSY), 0, codes)
        self.assertGreater(codes.count(Code.ADMITTED_REBASED), 0)
        self.assertGreater(codes.count(Code.STALE_REGION), 0)
        self.assertGreater(codes.count(Code.ADMITTED) + codes.count(Code.ADMITTED_REBASED), 20)
        unexpected = set(codes) - {Code.ADMITTED, Code.ADMITTED_REBASED, Code.ACTUATOR_BUSY,
                                   Code.STALE_REGION, Code.STALE_VIEW, Code.STALE_SCENE,
                                   Code.FRAME_EVICTED}
        self.assertEqual(unexpected, set())
        # zero silent overwrite, re-derived from the test's own log
        for actor, base, head, guards, rebased in admitted:
            self.assertEqual(rebased, head > base)
            for seq in range(base + 1, head + 1):
                regions, kind, who = history[seq]
                self.assertIsNone(kind, f"{actor} rebased across {kind} change by {who}")
                for r in regions:
                    for g in guards:
                        self.assertFalse(intersects(g, r),
                                         f"{actor} admitted over {who}'s change {r} @ {seq}")

    def test_barrier_race_exactly_one_admission(self):
        for _ in range(20):
            c = AdmissionController(now_ms=T0)
            n = 8
            leases = [c.issue_lease(f"a{i}", T0, ttl_ms=TTL, allow_gui=True,
                                    allowed_regions=[(i * 240, 0, 240, 1080)]) for i in range(n)]
            f = c.observe("a0", T0)
            barrier = threading.Barrier(n)
            out = []
            lock = threading.Lock()

            def peer(i):
                x = i * 240 + 50
                p = gui_prop(leases[i], f, target=(x, 100, 40, 40), footprint=(x - 8, 92, 56, 56))
                barrier.wait()
                d = c.propose(p, T0)
                with lock:
                    out.append(d.code)

            ts = [threading.Thread(target=peer, args=(i,)) for i in range(n)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(out.count(Code.ADMITTED), 1, out)
            self.assertEqual(out.count(Code.ACTUATOR_BUSY), n - 1, out)


if __name__ == "__main__":
    unittest.main()
