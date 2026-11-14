// Package prodgate decides whether a CU binary may drive the real X server.
// Prod needs the capsule markers, DISPLAY ":100" and the root-window isolation
// id equal to VQA_ISO; fakes need OS_CU_TEST=1. Anything else is rc 5 with no
// event sent.
package prodgate

import (
	"regexp"

	"opensuperficies.local/srf/src/cubook/capture"
	"opensuperficies.local/srf/src/cubook/rc"
)

// Mode is the outcome of Check; the zero value means refused.
type Mode int

const (
	Prod Mode = iota + 1
	TestFake
)

// IsoReader reads _OPENNEWTON_ISOLATION_ID from the root window of display.
type IsoReader interface {
	Read(display string) (string, error)
}

var hex32 = regexp.MustCompile(`^[0-9a-f]{32}$`)

func refuse(reason string) error { return rc.New("E_GATE", reason) }

// Check returns TestFake only for OS_CU_TEST=1 (no other variable is read) and
// Prod only when every production condition holds, tested in the order
// capsule, display, iso so a host never reaches the X server.
func Check(env func(string) string, probe capture.CapsuleProbe, iso IsoReader) (Mode, error) {
	if env("OS_CU_TEST") == "1" {
		return TestFake, nil
	}
	if !capture.InCapsule(probe) {
		return 0, refuse("not_in_capsule")
	}
	if env("DISPLAY") != ":100" {
		return 0, refuse("host_display")
	}
	want := env("VQA_ISO")
	if !hex32.MatchString(want) || iso == nil {
		return 0, refuse("iso_mismatch")
	}
	got, err := iso.Read(":100")
	if err != nil || got != want {
		return 0, refuse("iso_mismatch")
	}
	return Prod, nil
}
