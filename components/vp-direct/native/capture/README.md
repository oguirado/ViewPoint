# ViewPoint native capture candidate

`bulk_capture.CaptureStage` opens the resident X11 capture worker after verifying the provider SHA-256 and negotiating the version and all structure sizes against the [frozen bulk ABI](../../contracts/vp-toro-1/v1/bulk_v1.h). The provider owns the XCB MIT-SHM connection and source mapping. It converts BGRX to tightly packed RGB24 and computes SHA-256. Python handles orchestration and independently verifies the digest while the exact native lease is held.

The shim accepts only a local private Xvfb display and verifies the socket peer against the supplied server PID and effective UID. `CaptureStage` writes the captured bytes into an unmapped memfd and seals it before publication. The provider validates its close-on-exec duplicate, all required seals, full readable extent and geometry before any read-only mapping. A staging-to-memfd copy is part of this implementation.

`CaptureStage` supplies a digest and geometry after verification and settlement. Staged bytes, fd values and memory addresses stay inside the trusted capture-stage process. Neither API includes remote fd transfer. The frozen [S5 recipient policy](../../contracts/vp-toro-1/v1/CONTRACT.md) governs any later cross-process arm. Close the stage before forking. A forked child cannot dispatch through an inherited provider or stage.

The native ABI accepts ordered batches and latest-frame delivery. Each lane may hold up to 32 [TARGET] queued or leased descriptors. It binds to a single session and consumer at a time. A different session can bind the lane after the old session is fenced and its custody has settled. With the expected-order flag set, the comparison uses the publication reference of the input or queue head. Successful borrows have a separate provider-issued lease reference. END is delivered as an epoch barrier after older loans settle. A break clears unpublished-to-consumer blocks and fences future borrows and views. Returned pointers and borrowed descriptors remain valid through the exact release. Session identities and per-series release history are kept for the provider lifetime. Use fresh session identities on restart.

The [prototype capture control ABI](vp_capture.h) remains available for transport fixtures. Its caller supplies exclusive writable storage through the synchronous call. The worker closes and joins before returning a loan on transport timeout.

## Build and verification

Run inside this directory. The offline suite uses capped scopes, Cargo with the existing system C dependencies, and AddressSanitizer on the Rust crate and C transport. The optional private Xvfb command runs functional parity and lifecycle checks. The fixture isolates its X11 sockets and IPC, uses a private `/tmp` and a restricted device view, and disables the TCP listener.

```sh
sh tests/check_u.sh offline
sh tests/check_u.sh private-x
```

Use the pinned [U arm](../../contracts/vp-toro-1/v1/arms/U.json) and [protocol](../../contracts/vp-toro-1/v1/protocol.json) for qualification. The historical X-U benchmark compares XWD and XShm with different copy paths. Wave U requires equal transport and copy paths, independently measured participating cgroup CPU usage, joined telemetry, matched factory controls and the registered protected-observation tail guardrail.

Implementation references [CITED] are `LC-004` for the FFI ownership boundary, `SW-0085` for exact lease identity under a lock, `SW-0101` for immutable sealed backing and stale identity checks, and `SW-0146` for batch crossing accounting. `SW-0056` and `SW-0062` describe queue designs for separate performance work.
