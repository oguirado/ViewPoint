package exec

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/ajson"
	"opensuperficies.local/srf/src/cubook/rc"
)

const denyPath = "../../../testdata/cu/plans/act_deny.tsv"

// world is a fake screen: Observe returns the current pixels with a +1 render_index.
type world struct {
	*FakeActuator
	ri     uint32
	pix    string
	screen string
	effect bool // click/type/key change the pixels
	w, h   int
	script []string // optional screen ids returned per observe (cycle test)
	n      int
}

func newWorld(effect bool) *world {
	wd := &world{FakeActuator: &FakeActuator{}, pix: "p0", screen: "aaaaaaaaaaaaaaaa", effect: effect, w: 1280, h: 1064}
	wd.OnEvent = func(string) {
		if wd.effect {
			wd.pix += "x"
		}
	}
	return wd
}

func (w *world) Observe() (Frame, error) {
	w.ri++
	s := w.screen
	if w.script != nil {
		s = w.script[w.n%len(w.script)]
		w.n++
	}
	return MkFrame(w.ri, s, w.pix, w.w, w.h), nil
}

type fixedLoc struct {
	l Located
}

func (f fixedLoc) Locate(a *action.Action, fr Frame) (Located, error) {
	l := f.l
	l.RI = fr.RI
	return l, nil
}

func loc(level string) Located {
	return Located{Located: action.Located{By: "a11y", RID: "h:a11y:button.OK.0", BBox: [4]int{10, 10, 20, 20}, MatchLevel: level}, Name: "OK", AtRole: "button"}
}

func act(op, risk string, extra string) *action.Action {
	js := fmt.Sprintf(`{"schema":"vqa.action/v1","step":%d,"op":"%s","target":{"by":"xy","xy":[10,10]},"args":{%s},"pre":{"match_min":"L1"},"expect":[],"risk":"%s","allow":null,"fallback":"none","on_fail":"stop","policy":"script","reason_sha8":"00000000"}`, 1, op, extra, risk)
	a, err := action.DecodeAction([]byte(js))
	if err != nil {
		panic(err)
	}
	return a
}

func newExec(w *world, l Located, cfg Config) *Executor {
	if cfg.Deny == nil {
		d, err := LoadDenyList(denyPath)
		if err != nil {
			panic(err)
		}
		cfg.Deny = d
	}
	return New(cfg, w, w, fixedLoc{l}, nil)
}

func TestWriteNeedsL0L1NoEvents(t *testing.T) {
	for _, lv := range []string{"L2", "L3", "L4", "NONE"} {
		w := newWorld(true)
		st, _ := newExec(w, loc(lv), Config{}).Step(act("click", "write", `"button":1`))
		if st.Verdict != "NO_ACT" || st.RC != 5 || w.Events != 0 {
			t.Fatalf("%s: verdict %s rc %d events %d", lv, st.Verdict, st.RC, w.Events)
		}
	}
	w := newWorld(true)
	st, _ := newExec(w, loc("L1"), Config{}).Step(act("click", "write", `"button":1`))
	if st.Verdict != "EFFECT_OK" || w.Events != 1 {
		t.Fatalf("L1 write must act: %s %d", st.Verdict, w.Events)
	}
}

func TestIrrevRc7NoEvents(t *testing.T) {
	w := newWorld(true)
	st, _ := newExec(w, loc("L0"), Config{}).Step(act("click", "irrev", `"button":1`))
	if st.RC != 7 || w.Events != 0 {
		t.Fatalf("rc %d events %d", st.RC, w.Events)
	}
}

func TestDenyListRc5(t *testing.T) {
	for _, nm := range []string{"Emergency abort", "Enviar", "Kill process", "Cuarentena now"} {
		w := newWorld(true)
		l := loc("L0")
		l.Name = nm
		st, _ := newExec(w, l, Config{}).Step(act("click", "read", `"button":1`))
		if st.RC != 5 || *st.Stop != "deny_list" || w.Events != 0 {
			t.Fatalf("%q: rc %d stop %v events %d", nm, st.RC, st.Stop, w.Events)
		}
	}
	w := newWorld(true)
	l := loc("L0")
	l.Handler = "TripGate"
	if st, _ := newExec(w, l, Config{}).Step(act("click", "read", `"button":1`)); st.RC != 5 {
		t.Fatal("handler not denied")
	}
	l = loc("L0")
	l.Fill = "#9A1B1B"
	if st, _ := newExec(w, l, Config{}).Step(act("click", "read", `"button":1`)); st.RC != 5 {
		t.Fatal("fill not denied")
	}
	// allow:<rid> works in mock mode only
	l = loc("L0")
	l.Name = "Enviar"
	a := act("click", "read", `"button":1`)
	rid := l.RID
	a.Allow = &rid
	if st, _ := newExec(newWorld(true), l, Config{}).Step(a); st.RC != 5 {
		t.Fatal("allow honoured outside mock")
	}
	if st, _ := newExec(newWorld(true), l, Config{Mock: true}).Step(a); st.RC != 0 {
		t.Fatalf("allow not honoured in mock: rc %d", st.RC)
	}
}

func TestKeyDenied(t *testing.T) {
	for _, c := range []string{"alt+f4", "Ctrl+Q", "ctrl+w", "w+control"} {
		w := newWorld(true)
		st, _ := newExec(w, loc("L0"), Config{}).Step(act("key", "read", `"chord":"`+c+`"`))
		if st.RC != 5 || *st.Stop != "key_denied" || w.Events != 0 {
			t.Fatalf("%s: %v %d", c, st.Stop, w.Events)
		}
	}
	w := newWorld(true)
	if st, _ := newExec(w, loc("L0"), Config{}).Step(act("key", "read", `"chord":"ctrl+s"`)); st.RC != 0 {
		t.Fatal("ctrl+s must pass")
	}
}

type texts map[string]string

func (m texts) Text(r string) (string, error) { return m[r], nil }

func typeArgs(s string) string {
	h := sha256.Sum256([]byte(s))
	return fmt.Sprintf(`"text_ref":"t","text_sha256":"%s","len":%d`, hex.EncodeToString(h[:]), len(s))
}

func TestTypedIntoShell(t *testing.T) {
	w := newWorld(true)
	l := loc("L0")
	l.AtRole = "terminal"
	st, _ := newExec(w, l, Config{Texts: texts{"t": "rm -rf"}}).Step(act("type", "read", typeArgs("rm -rf")))
	if st.RC != 5 || *st.Stop != "typed_into_shell" || w.Events != 0 {
		t.Fatalf("%v %d", st.Stop, w.Events)
	}
	// non-terminal types fine and the literal never reaches the log
	var buf bytes.Buffer
	lg := &memLog{buf: &buf}
	w = newWorld(true)
	e := newExec(w, loc("L0"), Config{Texts: texts{"t": "hunter2"}})
	e.Log = lg
	st, _ = e.Step(act("type", "read", typeArgs("hunter2")))
	if st.Verdict != "EFFECT_OK" || strings.Contains(buf.String(), "hunter2") {
		t.Fatalf("verdict %s log %s", st.Verdict, buf.String())
	}
	// wrong text -> refused, zero events
	w = newWorld(true)
	st, _ = newExec(w, loc("L0"), Config{Texts: texts{"t": "other"}}).Step(act("type", "read", typeArgs("hunter2")))
	if st.RC != 5 || w.Events != 0 {
		t.Fatal("text sha mismatch must be refused")
	}
}

type memLog struct{ buf *bytes.Buffer }

func (m *memLog) Append(s action.Step) error {
	b, _ := jsonMarshal(s)
	m.buf.Write(b)
	return nil
}

func TestReleaseAllOnError(t *testing.T) {
	w := newWorld(true)
	w.FailOp = "click"
	st, _ := newExec(w, loc("L0"), Config{}).Step(act("click", "read", `"button":1`))
	if w.Releases != 1 || st.RC == 0 || st.Verdict == "EFFECT_OK" {
		t.Fatalf("releases %d rc %d verdict %s", w.Releases, st.RC, st.Verdict)
	}
	w = newWorld(true)
	newExec(w, loc("L0"), Config{}).Step(act("click", "read", `"button":1`))
	if w.Releases != 1 {
		t.Fatalf("ReleaseAll after OK batch: %d", w.Releases)
	}
}

func TestActuatorOkButSameFramesIsNoEffect(t *testing.T) {
	w := newWorld(false) // actuator returns nil, screen never changes
	st, _ := newExec(w, loc("L0"), Config{}).Step(act("click", "read", `"button":1`))
	if st.Verdict != "NO_EFFECT" || st.Verdict == "EFFECT_OK" {
		t.Fatalf("verdict %s", st.Verdict)
	}
	if st.Settle != 3 || st.Unsettled {
		t.Fatalf("settle %d unsettled %v", st.Settle, st.Unsettled)
	}
}

func TestStuckThreeTimesRc5(t *testing.T) {
	w := newWorld(false)
	e := newExec(w, loc("L0"), Config{})
	a := act("click", "read", `"button":1`)
	var rcs []int
	for i := 0; i < 4; i++ {
		st, _ := e.Step(a)
		rcs = append(rcs, st.RC)
	}
	if rcs[0] != 0 || rcs[1] != 0 || rcs[2] != 5 || rcs[3] != 5 {
		t.Fatalf("rcs %v", rcs)
	}
	if w.Events != 3 {
		t.Fatalf("4th attempt must emit no events, got %d", w.Events)
	}
}

func TestCycleFourSeenThreeTimesRc5(t *testing.T) {
	if !CycleHit([]string{"a", "b", "c", "d", "a", "b", "c", "d", "a", "b", "c", "d"}) {
		t.Fatal("cycle of 4 x3 not detected")
	}
	if CycleHit([]string{"a", "a", "a", "a", "a", "a"}) || CycleHit([]string{"a", "b", "c", "d", "a", "b", "c", "d"}) {
		t.Fatal("false cycle")
	}
	w := newWorld(true)
	w.script = []string{"1111111111111111", "2222222222222222", "3333333333333333", "4444444444444444"}
	e := newExec(w, loc("L0"), Config{SettleK: 1, SettleMax: 4})
	var last action.Step
	for i := 0; i < 12 && last.RC == 0; i++ {
		last, _ = e.Step(act("click", "read", `"button":1`))
	}
	if last.RC != 5 || last.Stop == nil {
		t.Fatalf("cycle not tripped: %+v", last.Stop)
	}
}

func TestStaleFrameRc4(t *testing.T) {
	w := newWorld(true)
	e := newExec(w, loc("L0"), Config{})
	e.Loc = staleLoc{}
	st, _ := e.Step(act("click", "read", `"button":1`))
	if st.RC != 4 || w.Events != 0 {
		t.Fatalf("rc %d events %d", st.RC, w.Events)
	}
	w = newWorld(true)
	e = newExec(w, loc("L0"), Config{})
	if st, _ = e.Step(act("click", "read", `"button":1`)); st.RC != 0 {
		t.Fatal(st.RC)
	}
	w.w = 1264 // geometry change
	if st, _ = e.Step(act("click", "read", `"button":1`)); st.RC != 4 {
		t.Fatalf("geometry change rc %d", st.RC)
	}
}

type staleLoc struct{}

func (staleLoc) Locate(a *action.Action, f Frame) (Located, error) {
	l := loc("L0")
	l.RI = f.RI + 7
	return l, nil
}

func TestStopFileRc3(t *testing.T) {
	stop := filepath.Join(t.TempDir(), "STOP")
	w := newWorld(true)
	e := newExec(w, loc("L0"), Config{StopFile: stop})
	if st, _ := e.Step(act("click", "read", `"button":1`)); st.RC != 0 {
		t.Fatal("no STOP file yet")
	}
	os.WriteFile(stop, nil, 0o600)
	ev := w.Events
	st, _ := e.Step(act("click", "read", `"button":1`))
	if st.RC != 3 || st.Verdict != "STOPPED" || w.Events != ev || w.Releases < 2 {
		t.Fatalf("rc %d verdict %s events %d releases %d", st.RC, st.Verdict, w.Events-ev, w.Releases)
	}
}

func TestNoDisplayEnvUsed(t *testing.T) {
	t.Setenv("DISPLAY", ":0")
	files, _ := filepath.Glob("*.go")
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, _ := os.ReadFile(f)
		if bytes.Contains(b, []byte("DISPLAY")) || bytes.Contains(b, []byte("os.Getenv")) || bytes.Contains(b, []byte("os.Environ")) {
			t.Errorf("%s reads the environment", f)
		}
	}
	w := newWorld(true)
	if st, _ := newExec(w, loc("L0"), Config{}).Step(act("click", "read", `"button":1`)); st.RC != 0 {
		t.Fatal("step failed")
	}
}

func buildRun(t *testing.T) (*action.Plan, []byte, []byte) {
	dir := t.TempDir()
	steps := filepath.Join(dir, "act_steps.jsonl")
	lw, err := ajson.Open(steps)
	if err != nil {
		t.Fatal(err)
	}
	w := newWorld(true)
	e := newExec(w, loc("L0"), Config{})
	e.Log = lw
	var frames bytes.Buffer
	w2 := &recObs{world: w, buf: &frames}
	e.Obs = w2
	p := &action.Plan{Schema: action.SchemaPlan, Actions: []action.Action{*act("click", "read", `"button":1`), *act("key", "read", `"chord":"ctrl+s"`)}}
	p.Actions[1].Step = 2
	if r, _, err := e.Run(p); r != 0 || err != nil {
		t.Fatalf("run rc %d err %v", r, err)
	}
	lw.Close()
	b, _ := os.ReadFile(steps)
	return p, b, frames.Bytes()
}

type recObs struct {
	*world
	buf *bytes.Buffer
}

func (r *recObs) Observe() (Frame, error) {
	f, err := r.world.Observe()
	b, _ := jsonMarshal(f)
	r.buf.Write(b)
	return f, err
}

func replayOnce(t *testing.T, p *action.Plan, stepsB, framesB []byte) ([]byte, error) {
	var steps []action.Step
	for _, ln := range bytes.Split(bytes.TrimRight(stepsB, "\n"), []byte("\n")) {
		s, err := action.DecodeStep(ln)
		if err != nil {
			t.Fatal(err)
		}
		steps = append(steps, *s)
	}
	fr, err := ParseFrames(framesB)
	if err != nil {
		t.Fatal(err)
	}
	return Replay(p, steps, fr)
}

func TestReplayByteIdentical3x(t *testing.T) {
	p, stepsB, framesB := buildRun(t)
	var outs [][]byte
	for i := 0; i < 3; i++ {
		o, err := replayOnce(t, p, stepsB, framesB)
		if err != nil {
			t.Fatal(err)
		}
		outs = append(outs, o)
	}
	if !bytes.Equal(outs[0], outs[1]) || !bytes.Equal(outs[1], outs[2]) || !bytes.Equal(outs[0], stepsB) {
		t.Fatal("replay not byte identical to the recorded log")
	}
}

func TestReplaySwappedScreenDiverges(t *testing.T) {
	p, stepsB, framesB := buildRun(t)
	bad := bytes.Replace(framesB, []byte("aaaaaaaaaaaaaaaa"), []byte("bbbbbbbbbbbbbbbb"), 3)
	_, err := replayOnce(t, p, stepsB, bad)
	if rc.Of(err) != 5 {
		t.Fatalf("swapped screen must diverge rc 5, got %v", err)
	}
	// swapped pixels (verdict no longer derivable)
	bad = bytes.ReplaceAll(framesB, []byte(`"digest":"`), []byte(`"digest":"0`))
	if _, err := replayOnce(t, p, stepsB, bad); rc.Of(err) != 5 {
		t.Fatalf("changed digest must diverge: %v", err)
	}
}

func jsonMarshal(v any) ([]byte, error) {
	b, err := json.Marshal(v)
	return append(b, '\n'), err
}
