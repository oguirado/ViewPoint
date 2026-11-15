package exec

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"time"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/rc"
)

// Config holds executor policy; no environment variable is ever read.
type Config struct {
	Mock      bool
	StopFile  string
	Deny      *DenyList
	Texts     TextSource
	Sleep     func(time.Duration) // nil = no sleep
	Now       func() time.Time    // nil = wall_ms 0
	SettleK   int                 // default 3
	SettleMax int                 // default 30
}

// Executor runs steps; it is single-goroutine.
type Executor struct {
	Cfg Config
	Act Actuator
	Obs Observer
	Loc Locator
	Log Logger

	seq      uint32
	steps    uint32
	baseW    int
	baseH    int
	noEffect map[string]int
	hist     []string
}

func New(cfg Config, act Actuator, obs Observer, loc Locator, log Logger) *Executor {
	if cfg.SettleK == 0 {
		cfg.SettleK = 3
	}
	if cfg.SettleMax == 0 {
		cfg.SettleMax = 30
	}
	return &Executor{Cfg: cfg, Act: act, Obs: obs, Loc: loc, Log: log, noEffect: map[string]int{}}
}

func str(s string) *string { return &s }

func (e *Executor) sleep(d time.Duration) {
	if e.Cfg.Sleep != nil {
		e.Cfg.Sleep(d)
	}
}

func stepCap(policy string) uint32 {
	if policy == "script" {
		return 200
	}
	return 60
}

func inputSHA(actionSHA, digest string, loc action.Located) string {
	c, _ := action.Canonical(loc)
	h := sha256.Sum256([]byte(actionSHA + "\n" + digest + "\n" + string(c)))
	return hex.EncodeToString(h[:])
}

const zeroScreen = "0000000000000000"

// Step executes one action. The error is non-nil only for log/IO failures;
// gate stops are reported in the returned Step (rc, stop, verdict).
func (e *Executor) Step(a *action.Action) (action.Step, error) {
	start := time.Time{}
	if e.Cfg.Now != nil {
		start = e.Cfg.Now()
	}
	e.steps++
	sha, err := a.SHA()
	if err != nil {
		return action.Step{}, rc.New("E_SCHEMA", err.Error())
	}
	st := action.Step{
		Schema: action.SchemaStep, Step: a.Step, ActionSHA: sha, Op: a.Op,
		Located:      action.Located{By: "xy", MatchLevel: "NONE"},
		ScreenBefore: zeroScreen, ScreenAfter: zeroScreen,
		Evidence: action.Evidence{Regions: []string{}, Frames: []string{},
			A11yDelta: action.A11yDelta{Added: []string{}, Removed: []string{}, Moved: []string{}}},
	}
	finish := func(verdict string, stop string, r int) (action.Step, error) {
		st.Verdict, st.RC = verdict, r
		if stop != "" {
			st.Stop = str(stop)
		}
		st.Budget.Steps = e.steps
		if e.Cfg.Now != nil {
			st.Budget.WallMS = uint32(e.Cfg.Now().Sub(start).Milliseconds())
		}
		if e.Log != nil {
			if err := e.Log.Append(st); err != nil {
				return st, err
			}
		}
		return st, nil
	}

	before, err := e.Obs.Observe()
	if err != nil {
		return finish("CANNOT_SEE", "observe_failed", rc.Usage)
	}
	st.RIBefore, st.RIAfter = before.RI, before.RI
	st.ScreenBefore, st.ScreenAfter = before.ScreenID, before.ScreenID
	if e.baseW == 0 {
		e.baseW, e.baseH = before.W, before.H
	}
	st.InputSHA = inputSHA(sha, before.Digest, st.Located)

	if e.Cfg.StopFile != "" {
		if _, err := os.Stat(e.Cfg.StopFile); err == nil {
			e.Act.ReleaseAll()
			return finish("STOPPED", "STOP", rc.Anchor)
		}
	}
	if e.steps > stepCap(a.Policy) {
		return finish("NO_ACT", "step_cap", rc.Gate)
	}
	if a.Op == "mcp_call" {
		return finish("NO_ACT", "op_unsupported", rc.Usage)
	}

	var loc Located
	if Actuating(a.Op) || a.Target != nil {
		loc, err = e.Loc.Locate(a, before)
		if err != nil {
			return finish("CANNOT_SEE", "not_found", rc.Anchor)
		}
		st.Located = loc.Located
		st.InputSHA = inputSHA(sha, before.Digest, st.Located)
	}
	var dest [2]int
	if a.Op == "drag" {
		if a.Args.To == nil {
			return finish("NO_ACT", "drag_target_missing", rc.Gate)
		}
		if a.Args.To.By == "xy" && a.Args.To.XY != nil {
			dest = [2]int{a.Args.To.XY[0], a.Args.To.XY[1]}
		} else {
			shadow := *a
			shadow.Target = a.Args.To
			tl, err := e.Loc.Locate(&shadow, before)
			if err != nil {
				return finish("CANNOT_SEE", "not_found", rc.Anchor)
			}
			dest[0], dest[1] = center(tl.BBox)
		}
	}
	d := Check(SafetyIn{Action: a, Loc: loc, Before: before, BaseW: e.baseW, BaseH: e.baseH,
		Deny: e.Cfg.Deny, Mock: e.Cfg.Mock, NoEffect: e.noEffect, ActionSHA: sha})
	if d.Verdict != "" {
		return finish(d.Verdict, d.Stop, d.RC)
	}

	var text string
	if a.Op == "type" {
		if e.Cfg.Texts == nil {
			return finish("NO_ACT", "text_unavailable", rc.Gate)
		}
		text, err = e.Cfg.Texts.Text(a.Args.TextRef)
		h := sha256.Sum256([]byte(text))
		if err != nil || hex.EncodeToString(h[:]) != a.Args.TextSHA256 || uint32(len(text)) != *a.Args.Len {
			return finish("NO_ACT", "text_mismatch", rc.Gate)
		}
	}

	if Actuating(a.Op) {
		e.seq++
		st.ActuatorSeq = e.seq
		aerr := e.lower(a, loc, text, dest)
		rerr := e.Act.ReleaseAll() // mandatory after every batch and on error
		if aerr != nil || rerr != nil {
			return finish("UNVERIFIED", "actuator_error", rc.Usage)
		}
	} else if a.Op == "wait" {
		ms := uint32(100)
		if a.Args.TimeoutMS != nil {
			ms = *a.Args.TimeoutMS
		}
		e.sleep(time.Duration(ms) * time.Millisecond)
	} else {
		// observe/locate/assert: no events, verdict from the current frame
		v := Verdict(a, before, before, false)
		return finish(v, "", rc.OK)
	}

	after, n, unsettled, err := e.settle(before)
	if err != nil {
		return finish("CANNOT_SEE", "observe_failed", rc.Usage)
	}
	st.RIAfter, st.Settle, st.Unsettled, st.ScreenAfter = after.RI, uint32(n), unsettled, after.ScreenID
	st.Evidence.Frames = []string{"f" + utoa(before.RI), "f" + utoa(after.RI)}
	verdict := Verdict(a, before, after, Actuating(a.Op))
	if unsettled {
		verdict = "CANNOT_SEE"
	}
	e.hist = append(e.hist, after.ScreenID)
	stop, r := "", rc.OK
	if verdict == "NO_EFFECT" {
		k := StuckKey(before.ScreenID, sha)
		e.noEffect[k]++
		if e.noEffect[k] >= 3 {
			stop, r = "stuck", rc.Gate
		}
	}
	if r == rc.OK && CycleHit(e.hist) {
		stop, r = "cycle", rc.Gate
	}
	return finish(verdict, stop, r)
}

func utoa(u uint32) string {
	b, _ := json.Marshal(u)
	return string(b)
}

func (e *Executor) settle(before Frame) (Frame, int, bool, error) {
	prev, run := before, 0
	for n := 1; n <= e.Cfg.SettleMax; n++ {
		e.sleep(100 * time.Millisecond)
		f, err := e.Obs.Observe()
		if err != nil {
			return f, n, false, err
		}
		if f.Digest == prev.Digest {
			run++
		} else {
			run = 0
		}
		prev = f
		if run >= e.Cfg.SettleK {
			return f, n, false, nil
		}
	}
	return prev, e.Cfg.SettleMax, true, nil
}

func center(b [4]int) (int, int) { return b[0] + b[2]/2, b[1] + b[3]/2 }

func (e *Executor) lower(a *action.Action, loc Located, text string, dest [2]int) error {
	x, y := center(loc.BBox)
	btn, cnt := 1, 1
	if a.Args.Button != nil {
		btn = *a.Args.Button
	}
	if a.Args.Count != nil {
		cnt = *a.Args.Count
	}
	switch a.Op {
	case "click":
		return e.Act.Click(x, y, btn, cnt)
	case "type":
		return e.Act.Type(text)
	case "key":
		return e.Act.Key(NormChord(a.Args.Chord, a.Args.Mods))
	case "drag":
		return e.Act.Drag(x, y, dest[0], dest[1], btn, *a.Args.Steps)
	case "scroll":
		dx, dy := 0, 0
		if a.Args.DX != nil {
			dx = *a.Args.DX
		}
		if a.Args.DY != nil {
			dy = *a.Args.DY
		}
		return e.Act.Scroll(x, y, dx, dy)
	}
	return errf("E_USAGE", "op %s not actuating", a.Op)
}

// Run executes a plan until the first halting result and returns the final rc.
func (e *Executor) Run(p *action.Plan) (int, []action.Step, error) {
	var out []action.Step
	for i := range p.Actions {
		a := &p.Actions[i]
		// One log line per plan action (replay needs len/step to match): a
		// retry_once first attempt is held back and dropped if it is retried.
		real := e.Log
		var held *heldLog
		if a.OnFail == "retry_once" && real != nil {
			held = &heldLog{}
			e.Log = held
		}
		st, err := e.Step(a)
		retried := err == nil && st.Verdict == "NO_EFFECT" && st.RC == 0 && a.OnFail == "retry_once"
		if held != nil {
			e.Log = real
			if !retried {
				if lerr := held.flush(real); lerr != nil && err == nil {
					err = lerr
				}
			}
		}
		if retried {
			e.sleep(500 * time.Millisecond)
			out = append(out, st)
			st, err = e.Step(a)
		}
		out = append(out, st)
		if err != nil {
			return rc.Of(err), out, err
		}
		if st.RC != 0 {
			return st.RC, out, nil
		}
		switch st.Verdict {
		case "NO_EFFECT", "WRONG_EFFECT", "SIDE_EFFECT", "CANNOT_SEE", "MCP_ERROR":
			if a.OnFail == "ask" {
				return rc.Human, out, nil
			}
			return rc.Gate, out, nil
		}
	}
	return rc.OK, out, nil
}

// heldLog buffers the steps of one attempt until Run decides to keep them.
type heldLog struct{ steps []action.Step }

func (h *heldLog) Append(s action.Step) error { h.steps = append(h.steps, s); return nil }

func (h *heldLog) flush(l Logger) error {
	for _, s := range h.steps {
		if err := l.Append(s); err != nil {
			return err
		}
	}
	return nil
}
