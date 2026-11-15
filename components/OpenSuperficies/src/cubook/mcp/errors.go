package mcp

import (
	"strings"

	"opensuperficies.local/srf/src/cubook/rc"
)

func gate(reason string) error   { return rc.New("E_GATE", reason) }
func schema(reason string) error { return rc.New("E_SCHEMA", reason) }
func budget(reason string) error { return rc.New("E_BUDGET", reason) }

// RcOf maps an error to the shared R4 table (never 1).
func RcOf(err error) int { return rc.Of(err) }

// StopOf returns the short stop reason of a table error.
func StopOf(err error) string {
	if e, ok := err.(*rc.Error); ok {
		return strings.SplitN(e.Msg, ":", 2)[0]
	}
	return "io"
}
