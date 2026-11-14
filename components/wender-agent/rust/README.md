# rust/ - low-level layer (placeholder, empty in v0)

Stack ruling: Rust for the low level. `wender_native` is an empty crate; nothing is built or
imported by the Python package in v0. Intended contents (TARGET, none implemented):
- RAM-only frame capture and shared-memory transport to vision-engine (STORAGE LAW: no frames on disk);
- input injection for the coordinates path (rung 5 / act path 3);
- a fast AT-SPI / D-Bus reader if the `gdbus` subprocess path proves too slow (no measurement yet).
Priority among these: AUSENTE (open question Q5 in HB-WA-a0-arch).
