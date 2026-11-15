# Browser identity window tiling (unit contract)

`opensuperficies.local/srf/src/browserid/tiling` is a Go-stdlib-only, pure
planner. It does **not** launch Chromium, mount browser profiles, read cookies
or credentials, take screenshots, inject XTEST input, or manage a browser
capsule. An identity has one profile and at most one live browser capsule;
multiple windows within that capsule share the identity's browser process.
Keyboard input serialisation and enforcing the profile lock belong to the
capsule/controller, not this geometry library.

## Public contract

Call `tiling.Plan(tiling.Spec{ProfileID, DisplayWidth, DisplayHeight, Windows,
MinWidth, MinHeight})` to obtain a `tiling.Layout` with display dimensions,
`Columns`, `Rows`, and row-major `Windows`. `MinWidth` and `MinHeight` default
independently to **1264** and **900** when set to zero. The display is at least
1x1 and no larger than **7680x4320**; the window count is **1..16**. Profile
IDs begin with an ASCII letter or digit and thereafter permit letters,
digits, `.`, `_`, and `-` (no slash or whitespace). Surface IDs are exactly
`<profile_id>/w<one-based-index>` (e.g., `perplexity-1/w2`). There are no
profile paths or credential-related fields.

The deterministic grid is **widest-first**: `columns = min(N,
floor(displayWidth/minWidth))`, `rows = ceil(N/columns)`; failure is returned
if there cannot be `rows` cells with the required minimum height. Cells use
integer floor division on both axes; all extra horizontal pixels go to the
**last column**, and all extra vertical pixels to the **last row**. Windows
occupy cells in row-major order. Any remaining tail cells are **empty**, not
surfaces. Rectangles are half-open `[X,X+Width) × [Y,Y+Height)` and never
overlap or extend beyond the display. Actual Chromium/window-manager
placement should be checked by the caller; this module only plans rectangles.

Each `Window.ChromiumArgs()` returns two strings,
`--window-position=x,y` and `--window-size=w,h`, with integer coordinates
and sizes (not a process command line). `Layout.DisplayToWindow(Point)` maps a
display pixel to `(surfaceID, windowLocalPoint)`;
`Layout.WindowToDisplay(surfaceID, Point)` performs the inverse. Both are
pure mappings. Origin `(0,0)` is the top-left of the corresponding space.

## Refusals

No partial layout is returned on failure. An `*tiling.Error` carries an
inspectable `ErrorKind`: `invalid_input` for invalid profile, display,
window count or negative minima; `insufficient_space` if minimum width or
height cannot be met; `outside_window` for pixels outside all planned
windows, including unused cells or an out-of-bounds window-local point; and
`unknown_surface` for an unallocated surface ID. The module exposes no CLI;
process exit-code handling is outside its scope.

## Tests and boundaries

Public-package `TestBidTiling*` cases cover exact windows for N=1,2,3,4,6 on
2560x1800 and 3840x2160 (2560x1800/N=6 must refuse); remainder allocation on
2561x1801; coordinate round trips and refusal at rectangle bounds; Chromium
argv; invalid specs and typed minimum-size refusal; the maximum display and
N=16; and exhaustive per-pixel overlap and coverage checks on displays up
to 9x8 using overridden 2x2 minima. Tests need no browser, display server,
network, process invocation or filesystem access. The acceptance command is
run by the bridge-gate CI; a PR alone does not certify its result.
