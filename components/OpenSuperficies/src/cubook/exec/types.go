// Package exec lowers vqa.action/v1 onto an Actuator, gates it with the pure
// pre-act safety function, and derives the verdict from frames only.
package exec

import (
	"crypto/sha256"
	"encoding/hex"

	"opensuperficies.local/srf/src/cubook/action"
)

// Frame is what an Observer returns: screen_id is the 16-hex layout identity,
// Digest the sha256 hex of the RGB24 pixels, RI the monotonic render_index.
type Frame struct {
	RI       uint32 `json:"ri"`
	ScreenID string `json:"screen_id"`
	Digest   string `json:"digest"`
	W        int    `json:"w"`
	H        int    `json:"h"`
}

// MkFrame builds a Frame whose digest is sha256(pixels) (test and fixture helper).
func MkFrame(ri uint32, screenID string, pixels string, w, h int) Frame {
	d := sha256.Sum256([]byte(pixels))
	return Frame{RI: ri, ScreenID: screenID, Digest: hex.EncodeToString(d[:]), W: w, H: h}
}

// Actuator is the only way events leave the executor. Real: XTestActuator.
type Actuator interface {
	Click(x, y, button, count int) error
	Type(text string) error
	Key(chord string) error
	Drag(x1, y1, x2, y2, button, steps int) error
	Scroll(x, y, dx, dy int) error
	ReleaseAll() error
}

// Observer returns the current frame (L1 capture in production).
type Observer interface{ Observe() (Frame, error) }

// Located is the resolved target plus the attributes the safety gate needs.
type Located struct {
	action.Located
	RI      uint32 // frame the target was resolved on
	Name    string
	Handler string
	AtRole  string
	Fill    string // "#RRGGBB"
}

// Locator resolves an action's target on the given pre-act frame.
type Locator interface {
	Locate(a *action.Action, f Frame) (Located, error)
}

// TextSource resolves args.text_ref to the literal, which never enters a log.
type TextSource interface {
	Text(ref string) (string, error)
}

// Logger receives one act-step line per Step (ajson.Writer).
type Logger interface{ Append(action.Step) error }

// Actuating reports whether op emits input events.
func Actuating(op string) bool {
	switch op {
	case "click", "type", "key", "drag", "scroll":
		return true
	}
	return false
}

// Verdict is the disjoint verdict from frames only; actuator rc never enters.
func Verdict(a *action.Action, before, after Frame, actuating bool) string {
	exps := a.Expect
	if a.Args.Until != nil {
		exps = append(append([]action.Expect{}, exps...), *a.Args.Until)
	}
	screenOK, haveScreen := true, false
	for _, e := range exps {
		if e.Kind == "screen_is" && e.ScreenID != "" {
			haveScreen = true
			if after.ScreenID != e.ScreenID {
				screenOK = false
			}
		}
	}
	if actuating {
		if before.Digest == after.Digest {
			return "NO_EFFECT"
		}
		if !screenOK {
			return "WRONG_EFFECT"
		}
		return "EFFECT_OK"
	}
	if !haveScreen {
		return "UNVERIFIED"
	}
	if screenOK {
		return "EFFECT_OK"
	}
	return "WRONG_EFFECT"
}
