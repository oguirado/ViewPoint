package exec

import (
	"bytes"
	"encoding/json"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/rc"
)

var gateVerdicts = map[string]bool{"NO_ACT": true, "STOPPED": true, "CANNOT_SEE": true, "UNVERIFIED": true}

// halted reports whether Run stops after this step (a shorter log is legitimate only then).
func halted(s action.Step) bool {
	if s.RC != 0 {
		return true
	}
	switch s.Verdict {
	case "NO_EFFECT", "WRONG_EFFECT", "SIDE_EFFECT", "CANNOT_SEE", "MCP_ERROR":
		return true
	}
	return false
}

func diverge(msg string) error { return rc.New("E_DIVERGENCE", msg) }

// ParseFrames reads a frames.jsonl (one Frame per line) into a map by RI.
func ParseFrames(data []byte) (map[uint32]Frame, error) {
	out := map[uint32]Frame{}
	for _, ln := range bytes.Split(bytes.TrimRight(data, "\n"), []byte("\n")) {
		if len(ln) == 0 {
			continue
		}
		var f Frame
		if err := json.Unmarshal(ln, &f); err != nil {
			return nil, rc.New("E_SCHEMA", err.Error())
		}
		out[f.RI] = f
	}
	return out, nil
}

// Replay re-derives every recorded step from (plan, frames) with no actuator.
// On success the output equals the recorded act_steps.jsonl byte for byte;
// any mismatch (swapped screen, wrong action, wrong verdict) is rc 5 E_DIVERGENCE.
func Replay(p *action.Plan, steps []action.Step, frames map[uint32]Frame) ([]byte, error) {
	byStep := map[uint32]*action.Action{}
	for i := range p.Actions {
		byStep[p.Actions[i].Step] = &p.Actions[i]
	}
	if len(steps) > len(p.Actions) {
		return nil, diverge("more recorded steps than plan actions")
	}
	if len(steps) < len(p.Actions) && (len(steps) == 0 || !halted(steps[len(steps)-1])) {
		return nil, diverge("recorded steps are a truncated, non-halted prefix of the plan")
	}
	for i := range steps {
		if steps[i].Step != p.Actions[i].Step {
			return nil, diverge("step number differs from plan order")
		}
	}
	var out bytes.Buffer
	for _, s := range steps {
		a := byStep[s.Step]
		if a == nil {
			return out.Bytes(), diverge("step not in plan")
		}
		sha, err := a.SHA()
		if err != nil || sha != s.ActionSHA || a.Op != s.Op {
			return out.Bytes(), diverge("action_sha256 differs from plan")
		}
		fb, ok := frames[s.RIBefore]
		if !ok || fb.ScreenID != s.ScreenBefore {
			return out.Bytes(), diverge("screen_before differs from recorded frame")
		}
		if s.InputSHA != inputSHA(sha, fb.Digest, s.Located) {
			return out.Bytes(), diverge("input_sha256 differs")
		}
		fa := fb
		if s.RIAfter != s.RIBefore {
			if fa, ok = frames[s.RIAfter]; !ok {
				return out.Bytes(), diverge("ri_after frame missing")
			}
			if s.Settle != s.RIAfter-s.RIBefore {
				return out.Bytes(), diverge("settle_frames != ri_after-ri_before")
			}
		}
		if fa.ScreenID != s.ScreenAfter {
			return out.Bytes(), diverge("screen_after differs from recorded frame")
		}
		if s.RIAfter != s.RIBefore || s.Stop == nil {
			want := Verdict(a, fb, fa, Actuating(a.Op))
			if s.Unsettled {
				want = "CANNOT_SEE"
			}
			if want != s.Verdict {
				return out.Bytes(), diverge("verdict " + s.Verdict + " != derived " + want)
			}
		} else if !gateVerdicts[s.Verdict] {
			return out.Bytes(), diverge("gated step with verdict " + s.Verdict)
		}
		b, err := json.Marshal(s)
		if err != nil {
			return out.Bytes(), rc.New("E_IO", err.Error())
		}
		out.Write(b)
		out.WriteByte('\n')
	}
	return out.Bytes(), nil
}
