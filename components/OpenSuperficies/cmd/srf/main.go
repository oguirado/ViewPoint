package main

import (
	"os"

	"opensuperficies.local/srf/src/surfacebook/manager"
)

// srf is the client of srfd. All of its behaviour lives in manager.EjecutarSrf, which returns the
// exit code srfd assigned to the request (3 PARADA, 69 slot busy, ...).
func main() {
	os.Exit(manager.EjecutarSrf(os.Args[1:], os.Stdout, os.Stderr))
}
