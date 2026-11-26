# FC-BOOT-L Tier 1 owner

Run `daemon-adapter/tools/fcbootl --config "$MAIN_CONFIG" --socket "$TOOLS_SOCKET" --live-lines "$LIVE_LINES"` from a Python environment with the dependencies in `fc_boot_requirements.txt`. The supplied socket must match the private MAIN configuration. The command runs the single cell (`n1`) followed by the existing paired reservation (`a1`, `a2`). Each attempt must return the expected SUMSQ result and a verified release receipt before the run can succeed. The owner appends `LIVE_START` and `LIVE_END` to the existing operator-owned output file.

Use the existing MAIN owner configuration for the schema root, canonical private journal, Tools and MAIN signer pins, PSI policy, grant TTLs, single job and paired reservation. Add `socket_path` and `tier1_policy`, which contains `path` to a private owner policy file and its JCS `sha256`. The native command resolves the runtime from this policy. A caller-supplied `adapter` or `tier` returns `PROFILE_UNSUPPORTED`.

The policy needs these entries before provisioning the command.

| Entry | Required value |
|---|---|
| `profiles` | A map keyed by canonical `vp.capsule_profile/v1` SHA-256. An approved entry carries integer `tier`, the full profile `image` object, and an `artifacts` map of canonical path strings under `firecracker`, `kernel`, `init` and `config`. |
| `guardian` | `path`, `file_sha256` and the JCS `sha256` pinned by the profile. Supply the qualified guardian's canonical `cgroup` and observed `caps`. |
| `sandbox` | Canonical `path` and SHA-256 of the provisioned unprivileged Bubblewrap executable. |
| `cgroup_root`, `runtime_root`, `evidence_root` | The canonical roots registered with Tools. Main uses direct cgroup and RAM runtime leaves for each attempt. Tools verifies closure evidence against these roots and the host kernel. |
| `leaf_caps` | String values for `memory.high`, `memory.max`, `memory.swap.max`, `cpu.max`, `pids.max` and `cpuset.cpus`. Supply those fields also in the guardian's caps. |

The fixed R7 cell uses explicit network mode `none`. The execution identity must have KVM access and the approved local primary and KVM groups. The owner checks the VMM's credentials, mounts, API/vsock peers and Firecracker seccomp readback before guest work. The VMM sees a private root with read-only held artifacts, the KVM device and the per-attempt RAM directory.

If the CLI exits with `OUTCOME_UNKNOWN`, replay with `daemon-adapter/tools/fcbootl --config "$MAIN_CONFIG" --socket "$TOOLS_SOCKET" --replay-pending`. This reuses the persisted Tools transaction and fences a claim whose launch has uncertain custody. A later attempt requires its own fresh identifiers and authorization.
