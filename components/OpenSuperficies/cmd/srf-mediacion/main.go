package main

import (
	"os"

	"opensuperficies.local/srf/src/surfacebook/manager"
)

// srf-mediacion prints the mediation census and exits with its verdict: 0 only when both legs
// (Docker, procfs) ran to completion and nothing foreign was observed; 1 when a foreign surface
// was observed and also when a leg did not run (estado "censo_parcial"). All of the behaviour lives
// in manager.EjecutarMediacion, which returns the exit code.
func main() {
	os.Exit(manager.EjecutarMediacion(os.Args[1:], os.Stdout, os.Stderr, manager.CensoOpciones{}))
}
