# QD timer smoke receipt

**PASS [MEASURED]**. Exit code `0` [MEASURED]. The completed smoke used Xvfb, headed Chrome, and the repository timer fixture. The process IDs below belong to the private PID namespace.

```sh
env -u DISPLAY systemd-run --user --scope -q -p MemoryHigh=6G -p MemoryMax=8G -- \
    taskset -c 16-19 python3 -IB components/vp-direct/qa/live_driver/driver.py smoke
```

| Evidence | Value [MEASURED] |
|---|---|
| Fixture | `fixtures/site/timer.html` |
| Fixture SHA-256 | `c01163025008d13c0baf866e1daca9a12d1937a2d7f180cef4c796fbce7cde33` |
| Effective CPU affinity | `16,17,18,19` |
| Chrome DEB | `157.0.8103.50-1` |
| Display and Xvfb PID | `:90`, `4` |
| Chrome PID and XID | `18`, `0x200002` |
| Window witness | `865950f6502e87ef17e2b2b812893a8c4c19906085a200282d4662ffce5e53` |
| CDP listener | `127.0.0.1:42005` |
| Viewport | `1016×677` pixels |
| Earlier decoded-pixel SHA-256 | `e79de8d3a01be9f4b2b0068a5df6d55a7e0eaa4a2476a5dda965723263cc9928` |
| Later decoded-pixel SHA-256 | `4bbb9fbc8091371f74952b300d6deb85fea7f2e170c84e9a3b9595481dcb6495` |
| Earlier AX SHA-256 | `76f5df84ad5787890ab5f150009c108bf26b57ab2ca7f74ac27132b091de9092` |
| Later AX SHA-256 | `85e3442821d50aa86289eee002a12e7fed4f2a9ba2dcb7c3b750f8e8c0070973` |
| Capture monotonic timestamps, ns | `1431878435239421`, `1431878819260199` |
| Browser profile and runtime root removed | `true`, `true` |
| X11 lock and socket removed | `true`, `true` |
| Scoped processes remaining | `0` |

Startup attempts before this receipt exposed an Xvfb GLX crash and a CDP document-readiness race. The passing invocation disabled GLX and pinned the document only after the expected fixture URL finished loading. Later driver changes added scope partitioning, provider adapters and geometry latching, with fake-based verification. This receipt covers only the timer smoke.
