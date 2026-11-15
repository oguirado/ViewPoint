# FX2-F7: bounded ring observations and workspace node release

Task: `fx2-f7-rtring-ramws-retry-and-release`, attempt `a1`.

This change repairs two Python behaviors in the exclusive production files and
promotes the two named stale-sequence pins and the FIFO release pin only after
their substantive assertions pass. The replacement gate passes in this VM.
Collection of this change is not acceptance by the orchestrator.

## Source binding and coordinates

All bound files were fetched through the GitHub connector with the explicit ref
`bridge/integration`. That ref resolved to
`f4cf3965493a052afbb7b704f94e531c98b84d30` before the task/source reads and still
resolved to that commit after the reads. The task branch was created from this
exact commit, and its initial ref was read back to confirm the same commit.

The inbox task card and metadata record the older commit
`0982a56086f33a9b1e36fa5c7baef5bac49f5ef3`. The user instruction takes precedence:
use the resolved current `bridge/integration` commit. This discrepancy is
reported rather than treating the older metadata value as observed source.

Production files were fetched as base64 and decoded to exact bytes. Every bound
file's bytes were checked against its returned Git blob SHA before use. Evidence
hashes and quotes refer to the unchanged source, not this candidate:

| Evidence | Exact source value |
| --- | --- |
| SHA-256 of `vadapt/rtring.py` | `3c9104ee16c412b631ba2276a825ed54de65681f0686df7aaf7211820b7d0ac8` |
| SHA-256 of `vadapt/ramws.py` | `46d8e3060b0414f31aad06ce39d922c64f3f7f0f19d9a4b651b2d8fe5db59a17` |

Source `vadapt/rtring.py:241`, including its four leading spaces:

```text
    def _try(self, r, retries):
```

Source `vadapt/ramws.py:182`, including its four leading spaces:

```text
    def release(self, p):
```

The following coordinates distinguish source and candidate line numbers:

| Behavior | Source at the observed commit | Candidate on the task branch |
| --- | --- | --- |
| Ring retry loop and mismatched-index verdict | `vadapt/rtring.py:241-255` | `vadapt/rtring.py:241-260`; new confirmation at `255-259` |
| Requested-frame view and CRC predicate | `vadapt/rtring.py:256-263` | `vadapt/rtring.py:261-268`; unchanged logic |
| Reader accounting and polling | `vadapt/rtring.py:265-318` | `vadapt/rtring.py:270-323`; unchanged logic |
| Workspace containment checks | `vadapt/ramws.py:182-192` | `vadapt/ramws.py:182-192`; unchanged |
| Workspace deletion and credit | `vadapt/ramws.py:193-200` | `vadapt/ramws.py:193-205` |
| Named ring pins | `tests/test_adv_rtring_stale_seq.py:88-142` | `tests/test_adv_rtring_stale_seq.py:90-113` |
| Named FIFO pin | `tests/test_jq1_a3_ramws_special_nodes.py:105-121` | `tests/test_jq1_a3_ramws_special_nodes.py:113-143` |

The bound `RAM_RING_AND_WORKSPACE_SPEC.md` describes the previous mismatch
classification in section 2.8.3 and previous release dispatch in section 3.7.
This owned note records their changes; that independently owned specification
is unchanged. Its Python/ARM limitation remains applicable.

## Ring: confirm a mismatched header before a terminal verdict

Previously `_try` read an even `s1`, then `ri`, then a second scalar sequence.
When `ri != r`, equality of the two scalar sequence values immediately returned
`lapped` or `absent`. The scripted observation model can return two stale even
sequence values while exposing a different generation's render index. That
incoherent observation must consume an attempt instead of becoming a final
classification.

The change retains the existing second scalar sequence comparison and then
reads the adjacent `(seq, render_index)` fields with the already-defined `_QQ`
struct. Classification is allowed only when this confirmation equals `(s1, ri)`.
An odd/changed sequence or changed render index in the confirmation differs
from that tuple and causes `continue` in the existing `range(retries)` loop.
No additional retry loop, sleep, or payload copy is introduced.

This check is restricted to the mismatched-index branch. Coherent `ri > r`
still returns `lapped`; coherent `ri < r` still returns `absent`, including with
a budget of one. An exhausted budget returns `torn` exactly as before.

### Ring invariants and controls

| Invariant | Candidate evidence |
| --- | --- |
| Odd or inconsistent observations use the existing attempt budget, including zero budget | `test_odd_and_zero_budget_never_extend_attempts`, `test_stale_paired_confirmation_consumes_existing_attempt_budget` |
| Coherent lapped, future-absent, untouched-slot and skipped-publication controls keep their classification | `test_coherent_lapped_and_absent_keep_classification_and_counters`, `test_coherent_empty_and_skipped_publication_are_absent` |
| Transient changed-index or changed-sequence observations can recover within the budget | `test_changed_index_observation_retries_and_recovers_frame`, `test_matching_index_sequence_change_retries_then_succeeds` |
| CRC mismatch refuses a frame when verification is enabled; the existing explicit opt-out remains | `test_guarantee_crc_mismatch_never_returned_as_frame` |
| Views remain read-only and backed by the original mmap; validity changes when overwrite begins | `test_guarantee_frame_valid_turns_false_after_later_commit` |
| `_try` does not mutate counters; `acquire` distinguishes overrun/torn; `poll` preserves ordering and absent gaps | Both named pins and `test_poll_order_lapped_and_absent_accounting` |
| A live writer view preserves its file and quota reservation until close can unmap it | `test_live_writer_view_keeps_reservation_until_released` |

The fixtures at `tests/test_adv_rtring_stale_seq.py:17-61` script struct reads
while leaving the actual mmap buffer intact. The source absent pin requested
`r=1` while the in-flight header also contained `ri=1`; it therefore entered
NumPy's buffer path with a `StaleSeqView` proxy and raised `TypeError`. The
corrected absent pin requests `r=3` in the same slot containing `ri=1`, so it
exercises a real `ri < r` case. The lapped pin uses the actual published even
sequence (`2`) before the writer changes it to odd (`3`).

Both repaired pins were run against the original production method and failed
on their intended status assertions (`lapped`/`absent` instead of `torn`). The
same assertions passed after the production change, before their two named
`expectedFailure` decorators were removed.

The owned fixture uses verified `/dev/shm` storage, real `RamWorkspace` objects,
8-by-8 RGB frames and two or three slots: ring files are 4,480 or 4,672 bytes.
Its workspace quota is 1 MiB. Only the statvfs free-space threshold is mocked;
quota accounting, allocation, mapping, view lifetime and deletion are real.

## Workspace: classify the terminal node without following it

The previous release dispatch unlinked only files or symlinks and recursively
removed directories. A FIFO matched none of these branches, so release could
return normally with the FIFO still present. The same omission applied to
other nondirectory special modes.

After the existing live-workspace, NUL, root, traversal and parent-realpath
checks, release now uses `os.lstat(tgt)` to classify the final node. A missing
target returns zero immediately. A directory uses the existing `_tree_bytes`
and `shutil.rmtree` path. Every nondirectory uses its own `st_blocks * 512` and
`os.unlink(tgt)`, without opening it or resolving its final symlink target.

`used` and `_released` are updated only after the selected deletion operation
returns successfully. Nonmissing errors remain visible; they do not grant a
release credit. The `max(0, used - freed)` accounting rule is unchanged.

### Workspace invariants and controls

| Invariant | Evidence |
| --- | --- |
| FIFO release completes without opening a pipe and removes the node | Promoted `test_release_fifo_special_node_removal_defect`; child completion token and no-open assertions |
| A terminal symlink is unlinked without inspecting or opening its target, including a broken target | `test_release_terminal_symlinks_never_follows_targets`; external sentinel contents and mtime preserved |
| Positive allocated block bytes are credited exactly once for ordinary metadata and subdirectories | `test_release_regular_metadata_and_directory_accounting` |
| Releasing a missing/already-released target returns zero without double credit | FIFO, symlink and metadata controls |
| A failed unlink preserves the node, used/released counters and peak | `test_release_unlink_failure_keeps_accounting_and_node` |
| Root protection, traversal/parent escape refusal and closed-workspace behavior remain | Unchanged source guards plus the replacement gate's escape, helper and quota suites |

The owned child helper now requires a bounded queue result and checks the child
exit status. An empty queue can no longer silently pass. Its FIFO test also
requires an explicit success token, and the helper closes process/queue
resources. The outside sentinel is outside the workspace but within verified
RAM storage. Filesystem tests use inert text/metadata; there are no persistent
frame copies or required services.

## Observed VM tests

Environment: Python 3.12.14, NumPy 2.3.5, Linux x86_64 VM. Durations below are
unittest durations observed in this VM, not host benchmarks or speed claims.

| Run | Observed outcome | Duration |
| --- | --- | --- |
| Exact replacement gate on original sources | 57 tests; OK with 3 expected failures; exit 0 | VM 0.104 s |
| Original two ring pins with expectation markers disabled in memory | 1 assertion failure, 1 `TypeError`; original wrapper shell ended with `cat` and reported 0 despite failed tests | VM 0.002 s |
| Repaired ring pins against original reader | 2 intended assertion failures; Python exit 1 | VM 0.006 s |
| Same repaired ring pins after production fix, before promotion | 2 passed; exit 0 | VM 0.006 s |
| Original FIFO pin with expectation marker disabled in memory, original release | 1 assertion failure: FIFO remained | VM 0.024 s |
| Same original FIFO assertion after release fix, before promotion | 1 passed | VM 0.021 s |
| Final owned ring module | 12 passed; exit 0 | VM 0.024 s |
| Final owned special-node module | 6 passed; exit 0 | VM 0.038 s |
| Exact replacement gate on combined candidate | 68 passed; exit 0; no skips or expected failures | VM 0.109 s |

The replacement gate was run verbatim:

```sh
python3 -m unittest tests.test_adv_rtring_stale_seq tests.test_jq1_a3_ramws_special_nodes tests.test_rtring_props tests.test_ramws_helpers_unit tests.test_adv_ramws_escape tests.test_jq1_a3_ramws_janitor_quota_edges tests.test_jq1_a3_ramws_share_and_disk_guards
```

The complete observed outputs, including baseline failures and the final gate's
intentional torn-drop and live-view warnings, are retained in the task's
`report.md`. The gate grew by eleven tests: eight ring controls and three
workspace controls. Only the two named stale-sequence pins and named FIFO pin
were promoted; no other expected-failure status was changed.

## Limits and separate host checks

This is Python-model evidence only. `_QQ.unpack_from` is neither a hardware
atomic snapshot nor an acquire/release fence. Internally coherent stale values
that persist across all observations cannot be distinguished by this model.
No native seqlock, ARM ordering guarantee, or hardware stress result is claimed.
The requested-frame/CRC path is unchanged, as are ring format and public APIs.

The workspace change retains the existing path-based parent containment guard.
It introduces no descriptor-anchored protection against arbitrary concurrent
parent-directory replacement. Actual filesystem tests cover FIFOs, symlinks,
ordinary metadata and directories; socket/device behavior follows the generic
nondirectory branch by inspection, with no socket service or device fixture.

The full `tests.test_rt1_ring` benchmark and `tests.test_ramws` keep-frames
battery were read as context but were not run. They remain separate host checks
because of resource and persistent-copy requirements. `tests.test_ramws_share`
was read but was not executed; the named replacement share/disk-guard suite was
executed. No later independently owned Jules ring/workspace files were changed.

Orchestrator acceptance, native/hardware behavior and host performance are
ABSENT from this evidence. The pull request is for collection only.
