# vp-direct fixtures (F1)

Local fixture site with no real account, for S2, S3, S5, S7, U1b, U1c,
C5 router calibration and C6 forge validation only.

## Layout

- `site/index.html` - landing page listing all fixture pages.
- `site/login.html` - login-looking page that needs a human step.
- `site/chat.html` - chat-like page with a prompt box and a streamed answer.
- `site/form.html` - form whose submit is an enviar-class act.
- `site/timer.html` - DOM changes on a timer.
- `fixture_server.py` - tiny stdlib server, binds 127.0.0.1 only.
- `golden/router_golden.json` - router golden questions (request ->
  expected tool and args) covering every tool of STRATEGY_FABLE2 section 2
  plus the F3 v3 tools (perception, guest, data, factory) with >=90 entries.
- `replay/timelines/*.json` - recorded `rec.timeline/v1` samples.
- `replay/forge/*.json` - good and bad forge candidates (`vp.forge_tool/v1`) and
  the F3 2.1 forge receipts (`forge_receipt_valid.json`, `forge_receipt_invalid.json`).
- `manifests/capability_viewpoint_core.json` - synthetic ViewPoint core capability
  manifest with op_table example.
- `manifests/capability_asset_factory.json` - synthetic asset-factory manifest
  demonstrating the F3 2.4 provider/model facts (TRELLIS.2 runnable, UniRig
  unqualified, Hunyuan3D-2.1 excluded, MEASURED/TARGET/AUSENTE tags).
- `privacy/synthetic_secrets.json` - synthetic secrets (all FAKE, labelled
  `SYNTHETIC_FIXTURE_ONLY`) for the A2 privacy sentinel.
- `privacy/canaries.json` - synthetic canary tokens (labelled
  `SYNTHETIC_CANARY_ONLY`) for sentinel and scrub checks.
- `tests/test_fixture_smoke.py` - smoke test plus oracle, negative
  control and mutant checks, extended for B0P-F.

## Run the server

```bash
python3 -I fixture_server.py --host 127.0.0.1 --port 0
```

The server prints `fixture-server listening on 127.0.0.1:PORT` to stderr.
Stop it by its own PID only. It takes no other flags and reads no
profile, cookie, or credential files.

## Run the tests

```bash
cd components/vp-direct/fixtures
systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
  python3 -I tests/test_fixture_smoke.py -v
```

Note: `-m unittest tests.test_fixture_smoke` is not used because `-I`
isolated mode drops the current directory from `sys.path`.

## Rules

- Synthetic content only; no real account, no credentials.
- Loopback only; no network beyond 127.0.0.1.
- Frames in timelines are RAM-only descriptors; no frame bytes on disk.
