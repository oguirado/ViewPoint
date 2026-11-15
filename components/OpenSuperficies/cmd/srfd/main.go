package main

import (
	"os"

	"opensuperficies.local/srf/src/surfacebook/manager"
)

// srfd is the single door every capsule class passes through. All of its behaviour, signal
// handling and teardown included, lives in manager.EjecutarDaemon, which returns only after
// teardown has completed; os.Exit here therefore cannot cut a teardown short.
func main() {
	os.Exit(manager.EjecutarDaemon(os.Args[1:], os.Stderr))
}
