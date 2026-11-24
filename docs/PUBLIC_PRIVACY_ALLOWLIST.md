# Public privacy scanner exceptions

Use the exact `--allow RULE:PATH:LINE` flags evaluated for the release
candidate. Recheck each line and reason before reusing a flag on another
revision. Run without the flags first to see what each one suppresses.

| Scanner location | Review |
| --- | --- |
| `PATH:components/blender-visual-qa/bpy_side/canon.py:26` | Default Linux shared-memory workspace. The code verifies that the selected root resides on a RAM filesystem. |
| `PRIVATE:components/vp-direct/native/capture/build.rs:1` | Cargo build source. The finding comes from the extension rule. The compiler input is relative to the crate. |
| `PRIVATE:components/vp-direct/native/capture/src/lib.rs:1` | Native capture source. The finding comes from the extension rule. |
| `PERSONAL:components/asset-factory/providers/trellis2/build/THIRD_PARTY_NOTICES.txt:27` | Upstream licence attribution. |
| `PERSONAL:components/asset-factory/providers/trellis2/build/THIRD_PARTY_NOTICES.txt:51` | Upstream licence attribution. |
| `PERSONAL:components/asset-factory/tests/test_n18_qtbridge_gates.py:38` | Synthetic systemd cgroup coordinate matched by the email pattern. |
| `PERSONAL:components/vp-direct/accept/t1/qt1-evidence/admitted/manifest.json:1` | Retained systemd cgroup coordinate matched by the email pattern. |
| `PERSONAL:components/vp-direct/accept/t1/qt1-evidence/capacity-run4/manifest.json:1` | Retained systemd cgroup coordinate matched by the email pattern. |
| `PERSONAL:components/vp-direct/accept/t2/qt2-evidence/sampler-relocation.json:4` | Retained systemd cgroup coordinate before relocation. |
| `PERSONAL:components/vp-direct/accept/t2/qt2-evidence/sampler-relocation.json:5` | Retained systemd cgroup coordinate after relocation. |
| `PERSONAL:components/vp-direct/privacy/tests/perf-observation-evidence/profile.json:1` | Retained systemd cgroup coordinates matched by the email pattern. |
| `PERSONAL:components/asset-factory/NOTICE:3` | Public project identity in the copyright notice. |
| `PERSONAL:components/asset-factory/tools/gates.py:112` | Exact copy of the copyright notice used by the component's licensing gate. |
| `PERSONAL:components/vp-direct/vendor/NOTICE.md:8` | Public project identity in the copyright notice. |
| `PERSONAL:NOTICE-EVALUATION.md:63` | Public source link for the pinned Vision Engine wheel. |
| `PERSONAL:PROVENANCE.md:12` | Public project identity in the component provenance. |
| `PERSONAL:PROVENANCE.md:17` | Public project site link. |
| `PERSONAL:PROVENANCE.md:118` | Public source links for the wheel and router provenance. |
| `PERSONAL:RELEASE_NOTES_v0.3.2.md:40` | Link to the public run that documents the scheduled Volume failure. |
| `PERSONAL:docs/VISUAL_QA_3D.md:113` | Public commit link for the vendored router tooling. |
| `PERSONAL:docs/VISUAL_QA_3D.md:115` | Public source link for the pinned Vision Engine wheel. |

The [release privacy reconciliation](release/v0.3.2_PRIVACY_RECONCILIATION.md)
records the exact history coordinates and their dispositions.
