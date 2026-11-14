# Electron app input

`app.manifest` is intentionally empty: the packaged app is AUSENTE — no inventar.
`app.json` is TO CONFIRM with metaviewer-0; it does not prove an app exists.
Run `python3 -I -B scripts/program-electron/pin_app.py --app DIR` on an unpacked
operator-supplied build. This stages the input in `build/electron_app/`, writes
sorted SHA-256 file pins, and updates the recipe manifest hash. An existing
staged app is refused. No application or build is executed.
See `docs/spec/program_electron.md` for approval and build review.
