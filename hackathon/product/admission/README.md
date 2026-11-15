# product/admission

This is the reference admission controller for several actors that share one ViewPoint surface. It is stdlib-only
Python 3. The full specification is in [SPEC.md](SPEC.md).

| File | Purpose |
|---|---|
| `SPEC.md` | Lease model, Invariant R (region-scoped rebase), verification table, global invariants |
| `admission.py` | `AdmissionController`: `issue_lease`, `revoke_lease`, `expire_leases`, `observe`, `propose`, `wait_verify`, `capture`, `resolve_reconcile`, `surface_restart` |
| `test_admission.py` | One `NegativeControl<CODE>` class per code, plus threaded multi-peer contention tests |

## Usage

```python
from admission import AdmissionController, Proposal, PostCapture, ExecutorOutcome

c = AdmissionController(now_ms=0)
lease = c.issue_lease("camera-agent", 0, ttl_ms=300_000, allow_gui=True,
                      allowed_regions=[(0, 0, 1920, 1080)])
f = c.observe("camera-agent", 0)
d = c.propose(Proposal(
        proposal_id="camera-agent-1", actor_id="camera-agent", lane="gui",
        lease_id=lease.lease_id, lease_epoch=lease.lease_epoch,
        based_on_frame=f.frame_seq, view_epoch=f.view_epoch, scene_epoch=f.scene_epoch,
        based_on_sha256=f.sha256, target_region=(600, 300, 80, 80),
        footprint=(0, 80, 1580, 900), keys=("KP_1",), action_class="view_change"), 0)
if d.admitted:
    # The caller's executor runs the act here, then reports what the capture saw.
    r = c.wait_verify(d.dispatch_token, PostCapture(((600, 300, 80, 80),)),
                      ExecutorOutcome(rc=0, elapsed_ms=120), 150)
    print(r.verdict)  # Code.VERIFIED
```

## Tests

```bash
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -p MemorySwapMax=0 \
  python3 -m unittest discover -s product/admission -p 'test_*.py' -v
```

`CodeCoverage` fails if a code in `Code` has no matching `NegativeControl<CODE>` class.
