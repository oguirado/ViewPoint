package surface

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func sleepShort() { time.Sleep(10 * time.Millisecond) }

type fakeStreamer struct{ lines []string }

func (f fakeStreamer) Stream(ctx context.Context, argv []string, line func([]byte)) error {
	for _, l := range f.lines {
		line([]byte(l))
	}
	<-ctx.Done()
	return ctx.Err()
}

func TestRecordDemoRowsNeverCarryPrintableKeys(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	r.pair = pairFixture(1280, 900)
	tool.S = fakeStreamer{lines: []string{
		`{"k":"ready"}`,
		`{"k":"click","b":1,"x":300,"y":200,"m":[]}`,
		`{"k":"char"}`, `{"k":"char"}`, `{"k":"char"}`,
		`{"k":"key","keysym":"Return","m":[]}`,
		`{"k":"drag","b":1,"x":10,"y":20,"x2":110,"y2":60,"m":["shift"]}`,
		`{"k":"scroll","x":5,"y":6,"dir":"down","m":[]}`,
		`not json`,
	}}
	dir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error)
	go func() { done <- tool.record(ctx, c.Container, dir, c.Windows) }()
	for i := 0; i < 400; i++ {
		b, _ := os.ReadFile(filepath.Join(dir, "events.jsonl"))
		if strings.Count(string(b), "\n") >= 5 {
			break
		}
		sleepShort()
	}
	cancel()
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	f, e := os.Open(filepath.Join(dir, "events.jsonl"))
	if e != nil {
		t.Fatal(e)
	}
	defer f.Close()
	var kinds []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var ev DemoEvent
		if json.Unmarshal(sc.Bytes(), &ev) != nil {
			t.Fatal("row is not JSON")
		}
		kinds = append(kinds, ev.Kind)
		if ev.Kind == "text" && (ev.TextLen != 3 || ev.Keysym != "") {
			t.Fatal("text row must carry only its length", ev)
		}
		if ev.Kind == "click" && (ev.Window != "w1" || *ev.X != 300 || *ev.Y != 200) {
			t.Fatal("click not mapped to window coordinates", ev)
		}
		if ev.Kind == "stop" && ev.Reason != "operator" {
			t.Fatal(ev)
		}
	}
	if strings.Join(kinds, ",") != "click,text,key,drag,scroll,stop" {
		t.Fatal(kinds)
	}
	if _, e := os.Stat(filepath.Join(dir, "README.md")); e != nil {
		t.Fatal("README missing")
	}
}

func TestRecordNeedsLoginAndValidID(t *testing.T) {
	tool, _, _, _ := setup(t)
	c := start(t, tool)
	if e := tool.ViewRecord(context.Background(), c.Container, 0, "x", func(ViewInfo) error { return nil }); Code(e) != 3 {
		t.Fatal("recording without a streamer must refuse", e)
	}
	tool.S = fakeStreamer{}
	if e := tool.ViewRecord(context.Background(), c.Container, 0, "Bad ID/../x", func(ViewInfo) error { return nil }); Code(e) != 2 {
		t.Fatal(e)
	}
}

func TestViewStopRefusesWithoutAViewAndNeverSignalsOtherProcesses(t *testing.T) {
	tool, _, _, _ := setup(t)
	if _, e := tool.ViewStop(context.Background(), "srf-id-test"); Code(e) != 4 {
		t.Fatal("no registration must refuse", e)
	}
	// A registration naming a process that is not this capsule's view (here: the test binary,
	// its parent) is stale: refused and removed, and nothing is signalled.
	for _, pid := range []int{os.Getppid()} {
		if e := privateDir(filepath.Dir(tool.viewPidPath("srf-id-test"))); e != nil {
			t.Fatal(e)
		}
		if e := os.WriteFile(tool.viewPidPath("srf-id-test"), []byte(strconv.Itoa(pid)+"\n"), 0600); e != nil {
			t.Fatal(e)
		}
		if _, e := tool.ViewStop(context.Background(), "srf-id-test"); Code(e) != 4 {
			t.Fatal("foreign pid must be refused", e)
		}
		if _, e := os.Stat(tool.viewPidPath("srf-id-test")); !os.IsNotExist(e) {
			t.Fatal("stale registration must be removed")
		}
	}
	if _, e := tool.ViewStop(context.Background(), "../x"); Code(e) != 2 {
		t.Fatal(e)
	}
}

func TestOperatorPasteNeedsTheOperatorViewAndBoundedUTF8(t *testing.T) {
	tool, _, _, _ := setup(t)
	if _, e := tool.OperatorPaste(context.Background(), "srf-id-test", []byte("hola")); Code(e) != 4 {
		t.Fatal("without an operator view paste-text must refuse", e)
	}
	if _, e := tool.OperatorPaste(context.Background(), "srf-id-test", []byte{0xff, 0xfe}); Code(e) != 2 {
		t.Fatal(e)
	}
	if _, e := tool.OperatorPaste(context.Background(), "srf-id-test", make([]byte, OperatorPasteMax+1)); Code(e) != 2 {
		t.Fatal(e)
	}
}
