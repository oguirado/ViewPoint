package proto

import (
	"crypto/sha256"
	"fmt"
	"math"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"regexp"
	"strconv"
	"strings"
)

var decimal = regexp.MustCompile(`^[0-9]+$`)
var signed = regexp.MustCompile(`^-?[0-9]+$`)
var xidRE = regexp.MustCompile(`^(0x[0-9a-fA-F]+|[0-9]+)$`)
var keyRE = regexp.MustCompile(`^[A-Za-z0-9_+]{1,32}$`)

func number(s string, lo, hi int64) bool {
	if !decimal.MatchString(s) {
		return false
	}
	n, e := strconv.ParseInt(s, 10, 64)
	return e == nil && n >= lo && n <= hi
}
func coordinate(s string) bool {
	if !signed.MatchString(s) {
		return false
	}
	n, e := strconv.ParseInt(s, 10, 32)
	return e == nil && n >= -65536 && n <= 65536
}
func refused() error { return &Error{2, "AUSENTE — VM command or request bounds not allowed"} }
func allowed(o Open, hashes map[string]bool) (int64, error) {
	timeout, _, err := allowedBounds(o, hashes)
	return timeout, err
}

// StdinLimit returns the effective stdin bound from the same exact policy match as Allowed.
// A zero requested cap selects the matched entry; a positive cap may only reduce it.
func StdinLimit(o Open, hashes map[string]bool) (int64, error) {
	_, cap, err := allowedBounds(o, hashes)
	return cap, err
}

func allowedBounds(o Open, hashes map[string]bool) (int64, int64, error) {
	if len(o.Argv) == 0 || len(o.Argv) > 256 || o.StdinCap < -1 || o.TimeoutMS < 0 {
		return 0, 0, refused()
	}
	for _, a := range o.Argv {
		if strings.IndexByte(a, 0) >= 0 {
			return 0, 0, refused()
		}
	}
	var timeout, cap int64
	a := o.Argv[1:]
	switch o.Argv[0] {
	case "xdotool", "/usr/bin/xdotool":
		var ok bool
		timeout, cap, ok = xdotool(a)
		if !ok {
			return 0, 0, refused()
		}
	case "srf-fbread", "/usr/local/libexec/srf-fbread":
		if strings.Join(a, "\x00") != "pair\x00:100\x000" {
			return 0, 0, refused()
		}
		timeout = 3000
	case "identity-entrypoint.sh", "/usr/local/bin/identity-entrypoint.sh":
		if len(a) != 1 {
			return 0, 0, refused()
		}
		switch a[0] {
		case "xpra-start":
			timeout = 110000
		case "xpra-stop":
			timeout = 13000
		default:
			return 0, 0, refused()
		}
	case "python3", "/usr/bin/python3":
		stream := false
		idx := 2
		if len(a) >= 2 && a[0] == "-I" && a[1] == "-u" {
			stream = true
			idx = 3
		}
		if len(a) <= idx || a[0] != "-I" || a[idx-1] != "-c" {
			return 0, 0, refused()
		}
		h := fmt.Sprintf("%x", sha256.Sum256([]byte(a[idx])))
		if !hashes[h] {
			return 0, 0, refused()
		}
		found := false
		for _, entry := range helpers.Entries {
			if entry.SHA256 != h {
				continue
			}
			found = true
			timeout = entry.TimeoutMS
			cap = entry.StdinCap
			if stream != entry.Stream {
				return 0, 0, refused()
			}
			tail := a[idx+1:]
			if entry.Name == "downloadsPython" {
				if !downloadArgs(tail) {
					return 0, 0, refused()
				}
			} else if len(tail) != 0 {
				return 0, 0, refused()
			}
		}
		if !found {
			return 0, 0, refused()
		}
	default:
		return 0, 0, refused()
	}
	if cap == -1 {
		if o.StdinCap != -1 || o.TimeoutMS != 0 {
			return 0, 0, refused()
		}
	} else if o.StdinCap < 0 || o.StdinCap > cap {
		return 0, 0, refused()
	}
	if timeout > 0 && o.TimeoutMS > timeout {
		return 0, 0, refused()
	}
	if o.TimeoutMS > 0 {
		timeout = o.TimeoutMS
	}
	if o.StdinCap > 0 {
		cap = o.StdinCap
	}
	return timeout, cap, nil
}
func downloadArgs(a []string) bool {
	if len(a) != 1 && len(a) != 3 {
		return false
	}
	if a[0] != "/profile/exchange" && a[0] != "/profile/Downloads" {
		return false
	}
	if len(a) == 1 {
		return true
	}
	n := a[1]
	return len(n) > 0 && len(n) <= 200 && !strings.ContainsAny(n, "/\x00") && n != "." && n != ".." && !strings.HasSuffix(n, ".crdownload") && (a[2] == "delete" || a[2] == "15728640")
}

// rules describe the finite xdotool operand shapes; chained invocations are parsed operand by operand.
var commands = map[string]bool{"getwindowgeometry": true, "windowactivate": true, "getwindowfocus": true, "key": true, "mousemove": true, "click": true, "mousedown": true, "mouseup": true, "sleep": true, "type": true, "getwindowname": true, "search": true, "windowmove": true, "windowsize": true}

func xdotool(a []string) (int64, int64, bool) {
	if len(a) == 0 {
		return 0, 0, false
	}
	timeout := int64(3000)
	cap := int64(0)
	held := ""
	for len(a) > 0 {
		cmd := a[0]
		a = a[1:]
		take := func(s string) bool {
			if len(a) > 0 && a[0] == s {
				a = a[1:]
				return true
			}
			return false
		}
		operand := func(check func(string) bool) bool {
			if len(a) == 0 || !check(a[0]) {
				return false
			}
			a = a[1:]
			return true
		}
		xid := func(s string) bool {
			if !xidRE.MatchString(s) {
				return false
			}
			base := 10
			if strings.HasPrefix(s, "0x") {
				base = 16
				s = s[2:]
			}
			n, e := strconv.ParseUint(s, base, 32)
			return e == nil && n > 0
		}
		integer := func(s string) bool { return number(s, 0, 60000) }
		button := func(s string) bool { return number(s, 1, 9) }
		switch cmd {
		case "keydown":
			if held != "" || len(a) == 0 || (a[0] != "ctrl" && a[0] != "shift" && a[0] != "alt" && a[0] != "super") {
				return 0, 0, false
			}
			held = a[0]
			a = a[1:]
		case "keyup":
			if held == "" || len(a) != 1 || a[0] != held {
				return 0, 0, false
			}
			held = ""
			a = a[1:]
		case "getwindowgeometry":
			take("--shell")
			if !operand(xid) {
				return 0, 0, false
			}
		case "windowactivate":
			if !take("--sync") || !operand(xid) {
				return 0, 0, false
			}
		case "getwindowfocus":
			take("-f")
		case "getwindowname":
			if !operand(xid) {
				return 0, 0, false
			}
		case "key":
			take("--clearmodifiers")
			if !operand(func(s string) bool { return keyRE.MatchString(s) && !xdotoolCommandToken(s) }) {
				return 0, 0, false
			}
		case "mousemove":
			take("--sync")
			if take("--window") && !operand(xid) {
				return 0, 0, false
			}
			if !operand(coordinate) || !operand(coordinate) {
				return 0, 0, false
			}
		case "click":
			take("--clearmodifiers")
			if take("--repeat") {
				if !operand(func(s string) bool { return number(s, 1, 1000) }) {
					return 0, 0, false
				}
				if take("--delay") && !take("40") {
					return 0, 0, false
				}
			}
			if !operand(button) {
				return 0, 0, false
			}
		case "mousedown", "mouseup":
			if !operand(button) {
				return 0, 0, false
			}
		case "sleep":
			if !operand(func(s string) bool {
				n, e := strconv.ParseFloat(s, 64)
				return e == nil && !math.IsNaN(n) && !math.IsInf(n, 0) && n >= 0 && n <= 1
			}) {
				return 0, 0, false
			}
		case "type":
			if len(a) == 0 {
				return 0, 0, false
			}
			take("--clearmodifiers")
			if take("--delay") && !operand(integer) {
				return 0, 0, false
			}
			if !take("--file") || !take("-") || len(a) > 0 {
				return 0, 0, false
			}
			timeout = 28000
			cap = 8000
		case "search":
			take("--onlyvisible")
			if !take("--class") || !operand(func(s string) bool { return len(s) > 0 && len(s) <= 200 && !strings.HasPrefix(s, "-") }) {
				return 0, 0, false
			}
		case "windowmove", "windowsize":
			if !operand(xid) || !operand(coordinate) || !operand(coordinate) {
				return 0, 0, false
			}
		default:
			return 0, 0, false
		}
	}
	return timeout, cap, held == ""
}

// xdotool recognizes command words even in the variadic key operand position.
// Refuse these as key operands so no parser-level command escape can occur.
func xdotoolCommandToken(s string) bool {
	if commands[s] {
		return true
	}
	switch s {
	case "keydown", "keyup", "exec", "behave", "behave_screen_edge", "selectwindow", "getactivewindow", "getwindowpid", "getdisplaygeometry", "get_num_desktops", "get_desktop", "get_desktop_viewport", "get_desktop_for_window", "set_num_desktops", "set_desktop", "set_desktop_viewport", "set_desktop_for_window", "windowfocus", "windowraise", "windowreparent", "windowmap", "windowunmap", "windowminimize", "windowkill", "windowclose", "set_window", "mousemove_relative", "getmouselocation":
		return true
	}
	return false
}
