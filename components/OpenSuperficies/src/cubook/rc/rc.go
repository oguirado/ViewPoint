// Package rc is the single exit-code table shared by every srf-cu-* binary
// (SPEC_INT R4: 0, 2..7; the value 1 is never produced).
package rc

import "errors"

const (
	OK     = 0
	Usage  = 2
	Anchor = 3
	Stale  = 4
	Gate   = 5
	Budget = 6
	Human  = 7
)

var table = map[string]int{
	"OK":              0,
	"E_USAGE":         2,
	"E_IO":            2,
	"E_SCHEMA":        2,
	"E_GRID":          2,
	"E_ANCHOR":        3,
	"STOP":            3,
	"INCOMPLETE":      3,
	"E_STALE":         4,
	"E_GATE":          5,
	"E_PIN":           5,
	"E_RAM_NOT_TMPFS": 5,
	"E_RAM_QUOTA":     5,
	"E_RAM_ESCAPE":    5,
	"E_RAM_LOCKED":    5,
	"E_ANALYZE":       5,
	"E_DIVERGENCE":    5,
	"E_BUDGET":        6,
	"RC_HUMANO":       7,
}

// Table returns a copy of the name -> rc table.
func Table() map[string]int {
	out := make(map[string]int, len(table))
	for k, v := range table {
		out[k] = v
	}
	return out
}

// Error carries a table name; its exit code is table[Name].
type Error struct {
	Name string
	Msg  string
}

func (e *Error) Error() string { return e.Name + ": " + e.Msg }

// New builds an *Error.
func New(name, msg string) *Error { return &Error{Name: name, Msg: msg} }

// Code maps a table name to its rc; unknown names map to Gate (5).
func Code(name string) int {
	if v, ok := table[name]; ok {
		return v
	}
	return Gate
}

// Of maps any error to an rc: nil -> 0, *Error -> table, anything else -> 2 (E_IO).
func Of(err error) int {
	if err == nil {
		return OK
	}
	var e *Error
	if errors.As(err, &e) {
		return Code(e.Name)
	}
	return Usage
}
