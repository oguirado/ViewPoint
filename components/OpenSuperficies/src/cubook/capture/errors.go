// Package capture is the OS-side raw-RGB capture path of the CU capsule (os-cu-0 L1).
// Frames go only into ramws paths handed in by the caller; evidence JSON is path-free.
package capture

import "fmt"

// Error classes follow DESIGN_CU rc table: E_GATE=5, E_STALE=4, E_IO/E_SCHEMA/E_USAGE=2.
const (
	ClassGate   = "E_GATE"
	ClassStale  = "E_STALE"
	ClassIO     = "E_IO"
	ClassSchema = "E_SCHEMA"
	ClassUsage  = "E_USAGE"
)

// Error is the single error type of the package; cmd maps Class through the shared rc table.
type Error struct {
	Class  string
	Reason string
	Err    error
}

func (e *Error) Error() string {
	if e.Err != nil {
		return fmt.Sprintf("%s %s: %v", e.Class, e.Reason, e.Err)
	}
	return fmt.Sprintf("%s %s", e.Class, e.Reason)
}

func (e *Error) Unwrap() error { return e.Err }

func gate(reason string) *Error { return &Error{Class: ClassGate, Reason: reason} }
func ioErr(reason string, err error) *Error {
	return &Error{Class: ClassIO, Reason: reason, Err: err}
}
func schemaErr(reason string, err error) *Error {
	return &Error{Class: ClassSchema, Reason: reason, Err: err}
}
