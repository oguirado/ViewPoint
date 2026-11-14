package exec

import (
	"fmt"
	"strings"
	"time"
)

// XTestAPI is the subset of displaybook/desktop.XTestClient the actuator uses.
type XTestAPI interface {
	MovePointer(x, y int16) error
	ButtonDown(button byte) error
	ButtonUp(button byte) error
	KeyDown(keycode byte) error
	KeyUp(keycode byte) error
}

var keycodes = map[string]byte{
	"q": 24, "w": 25, "e": 26, "r": 27, "t": 28, "y": 29, "u": 30, "i": 31, "o": 32, "p": 33,
	"a": 38, "s": 39, "d": 40, "f": 41, "g": 42, "h": 43, "j": 44, "k": 45, "l": 46,
	"z": 52, "x": 53, "c": 54, "v": 55, "b": 56, "n": 57, "m": 58,
	"1": 10, "2": 11, "3": 12, "4": 13, "5": 14, "6": 15, "7": 16, "8": 17, "9": 18, "0": 19,
	"enter": 36, "return": 36, "tab": 23, "space": 65, "esc": 9, "escape": 9, "backspace": 22,
	"delete": 119, "home": 110, "end": 115, "pageup": 112, "pagedown": 117,
	"left": 113, "right": 114, "up": 111, "down": 116,
	"f1": 67, "f2": 68, "f3": 69, "f4": 70, "f5": 71, "f6": 72, "f7": 73, "f8": 74, "f9": 75, "f10": 76, "f11": 95, "f12": 96,
	"ctrl": 37, "shift": 50, "alt": 64, "super": 133, "meta": 133,
	".": 60, ",": 59, "-": 20, "/": 61,
}

// XTestActuator lowers onto XTEST and tracks held buttons/keys for ReleaseAll.
type XTestActuator struct {
	X    XTestAPI
	Hold time.Duration
	btn  map[byte]bool
	key  map[byte]bool
}

func (a *XTestActuator) init() {
	if a.btn == nil {
		a.btn, a.key = map[byte]bool{}, map[byte]bool{}
	}
}

func (a *XTestActuator) down(b byte) error {
	a.init()
	a.btn[b] = true
	return a.X.ButtonDown(b)
}

func (a *XTestActuator) up(b byte) error {
	a.init()
	delete(a.btn, b)
	return a.X.ButtonUp(b)
}

func (a *XTestActuator) kdown(k byte) error {
	a.init()
	a.key[k] = true
	return a.X.KeyDown(k)
}

func (a *XTestActuator) kup(k byte) error {
	a.init()
	delete(a.key, k)
	return a.X.KeyUp(k)
}

func (a *XTestActuator) Click(x, y, button, count int) error {
	if err := a.X.MovePointer(int16(x), int16(y)); err != nil {
		return err
	}
	for i := 0; i < count; i++ {
		if err := a.down(byte(button)); err != nil {
			return err
		}
		if err := a.up(byte(button)); err != nil {
			return err
		}
	}
	return nil
}

func (a *XTestActuator) Drag(x1, y1, x2, y2, button, steps int) error {
	if err := a.X.MovePointer(int16(x1), int16(y1)); err != nil {
		return err
	}
	if err := a.down(byte(button)); err != nil {
		return err
	}
	for i := 1; i <= steps; i++ {
		if err := a.X.MovePointer(int16(x1+(x2-x1)*i/steps), int16(y1+(y2-y1)*i/steps)); err != nil {
			return err
		}
	}
	return a.up(byte(button))
}

// Scroll: dx/dy are wheel notches clamped to 64 (buttons 4/5 vertical, 6/7 horizontal).
func (a *XTestActuator) Scroll(x, y, dx, dy int) error {
	if err := a.X.MovePointer(int16(x), int16(y)); err != nil {
		return err
	}
	wheel := func(n int, neg, pos byte) error {
		b := pos
		if n < 0 {
			b, n = neg, -n
		}
		if n > 64 {
			n = 64
		}
		for i := 0; i < n; i++ {
			if err := a.down(b); err != nil {
				return err
			}
			if err := a.up(b); err != nil {
				return err
			}
		}
		return nil
	}
	if err := wheel(dy, 4, 5); err != nil {
		return err
	}
	return wheel(dx, 6, 7)
}

func (a *XTestActuator) Key(chord string) error {
	parts := strings.Split(strings.ToLower(chord), "+")
	var codes []byte
	for _, p := range parts {
		c, ok := keycodes[p]
		if !ok {
			return fmt.Errorf("unsupported key %q", p)
		}
		codes = append(codes, c)
	}
	for _, c := range codes {
		if err := a.kdown(c); err != nil {
			return err
		}
	}
	for i := len(codes) - 1; i >= 0; i-- {
		if err := a.kup(codes[i]); err != nil {
			return err
		}
	}
	return nil
}

func (a *XTestActuator) Type(text string) error {
	for _, r := range text {
		s := string(r)
		shift := r >= 'A' && r <= 'Z'
		c, ok := keycodes[strings.ToLower(s)]
		if r == ' ' {
			c, ok = keycodes["space"], true
		}
		if !ok {
			return fmt.Errorf("unsupported character %q", r)
		}
		if shift {
			if err := a.kdown(keycodes["shift"]); err != nil {
				return err
			}
		}
		if err := a.kdown(c); err != nil {
			return err
		}
		if err := a.kup(c); err != nil {
			return err
		}
		if shift {
			if err := a.kup(keycodes["shift"]); err != nil {
				return err
			}
		}
	}
	return nil
}

// ReleaseAll releases every button and key still held; first error wins.
func (a *XTestActuator) ReleaseAll() error {
	a.init()
	var first error
	for b := range a.btn {
		if err := a.X.ButtonUp(b); err != nil && first == nil {
			first = err
		}
	}
	for k := range a.key {
		if err := a.X.KeyUp(k); err != nil && first == nil {
			first = err
		}
	}
	a.btn, a.key = map[byte]bool{}, map[byte]bool{}
	return first
}
