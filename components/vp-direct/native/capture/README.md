# ViewPoint native X11 capture

The [capture ABI](vp_capture.h) exposes a resident XCB MIT-SHM worker for a private local Xvfb display. The caller supplies the display number and server PID. The worker verifies the socket peer and effective UID, converts BGRX to RGB24 and computes SHA-256. The caller supplies exclusive pixel and digest buffers until each synchronous call returns. The worker closes and joins before returning a loan on transport timeout.

The build uses system XCB, XCB SHM and OpenSSL. It builds only the private X11 transport and exports the capture control ABI. The frozen VP-TORO-1 contract is kept as a standalone candidate specification and its bulk provider remains on standby.

From this directory, run the offline ABI and invalid-display check:

```sh
env -u DISPLAY systemd-run --user --scope -p MemoryHigh=6G -p MemoryMax=8G -- sh tests/check_u.sh offline
```

For a private Xvfb fixture, use the same command with `private-x`. The fixture isolates X11 sockets and IPC, uses a private temporary filesystem and restricted device view, and disables the TCP listener.
