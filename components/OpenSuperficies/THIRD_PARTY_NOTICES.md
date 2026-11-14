# Third-party notices — OpenSuperficies

OpenSuperficies is licensed under AGPL-3.0-only (see `LICENSE`).
Copyright (C) 2026 oguirado <oguirado@users.noreply.github.com>.

Dependencies are fetched by the Go and npm toolchains; they are not vendored
in this tree. Upstream licences as declared by each project:

| Component | Where | Licence |
|---|---|---|
| chromedp/cdproto, chromedp/sysutil | `go.mod` | MIT |
| coder/websocket | `go.mod`, `cockpit/go.mod` | ISC |
| jezek/xgb | `go.mod` | BSD-3-Clause |
| golang.org/x/sys | `go.mod` | BSD-3-Clause |
| go-json-experiment/json | `go.mod` (indirect) | BSD-3-Clause |
| wailsapp/wails/v2 and its deps (labstack/echo, gorilla/websocket, godbus/dbus, google/uuid, go-ole, leaanthony/*, go-toast, …) | `cockpit/go.mod` | MIT / BSD-2-Clause / BSD-3-Clause |
| Wails generated runtime | `cockpit/frontend/wailsjs/` (shipped) | MIT, © Lea Anthony |
| svelte, @sveltejs/vite-plugin-svelte, vite, vitest, @xterm/xterm | `cockpit/frontend/package.json` (dev) | MIT |
| typescript, @playwright/test, playwright-core | `cockpit/frontend/package.json` (dev) | Apache-2.0 |
| DejaVu Sans Mono font | `bench/wl/scenes/v{2,3}/fonts/` (shipped, with its `LICENSE`) | Bitstream Vera / DejaVu font licence (permissive; the font may not be sold by itself) |
| Docker base images (`debian:12-slim`, `debian:bookworm-slim`, `ubuntu:24.04`) | `docker/` | referenced, not redistributed |

## Guest kernel

The microVM guest uses Linux 6.18.52 (GPL-2.0-only). Only `guest/kernel.config`
and the pinned tarball hash (`guest/PROCEDENCIA.json`) are shipped; the kernel
binary is built locally (`guest/README.md`). Anyone redistributing a built
kernel must also provide the corresponding source (GPL-2.0 §3).
