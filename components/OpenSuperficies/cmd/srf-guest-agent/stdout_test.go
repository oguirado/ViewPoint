package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"go/ast"
	"go/parser"
	"go/token"
	"io"
	"path/filepath"
	"strconv"
	"testing"
	"time"

	"opensuperficies.local/srf/src/vmsurface/helpers"
	"opensuperficies.local/srf/src/vmsurface/proto"
)

type zeroStdout struct{}

func (zeroStdout) Read(b []byte) (int, error) {
	clear(b)
	return len(b), nil
}

type stdoutReadProbe struct {
	io.Reader
	max int
}

func (r *stdoutReadProbe) Read(b []byte) (int, error) {
	if len(b) > r.max {
		r.max = len(b)
	}
	return r.Reader.Read(b)
}

type stdoutWriteProbe struct{ max, total int }

func (w *stdoutWriteProbe) Write(b []byte) (int, error) {
	if len(b) > w.max {
		w.max = len(b)
	}
	w.total += len(b)
	return len(b), nil
}

func TestPairBudgetBoundaries(t *testing.T) {
	for _, tc := range []struct {
		name string
		a, b uint64
		bad  bool
	}{
		{"empty-legs", 0, 0, false},
		{"first-leg-at-limit", maxPairStdout - 16, 0, false},
		{"second-leg-at-limit", 0, maxPairStdout - 16, false},
		{"first-leg-over-limit", maxPairStdout - 15, 0, true},
		{"sum-over-limit", maxPairStdout - 16, 1, true},
		{"uint64-overflow", ^uint64(0), 0, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var a, b [8]byte
			binary.BigEndian.PutUint64(a[:], tc.a)
			binary.BigEndian.PutUint64(b[:], tc.b)
			// Oversized declarations need no body to be rejected. Keep the
			// synthetic source bounded even for the uint64-overflow case.
			body := tc.a
			if body > maxPairStdout {
				body = 0
			}
			r := &stdoutReadProbe{Reader: io.MultiReader(bytes.NewReader(a[:]), io.LimitReader(zeroStdout{}, int64(body)), bytes.NewReader(b[:]), io.LimitReader(zeroStdout{}, int64(tc.b)))}
			w := &stdoutWriteProbe{}
			err := relayStdout(r, w, true, false)
			if tc.bad {
				var pe *proto.Error
				if !errors.As(err, &pe) || pe.Code != 5 {
					t.Fatalf("want rc 5, got %v", err)
				}
			} else if err != nil || uint64(w.total) != tc.a+tc.b+16 {
				t.Fatalf("err=%v bytes=%d", err, w.total)
			}
			if r.max > proto.MaxPayload || w.max > proto.MaxPayload || w.total > maxPairStdout {
				t.Fatalf("unbounded read=%d write=%d total=%d", r.max, w.max, w.total)
			}
		})
	}
}

func TestPairBudgetFragmentedHeaders(t *testing.T) {
	var p pairBudget
	var wire [21]byte
	binary.BigEndian.PutUint64(wire[:8], 3)
	binary.BigEndian.PutUint64(wire[11:19], 2)
	for i := range wire {
		if err := p.accept(wire[i : i+1]); err != nil {
			t.Fatal(err)
		}
	}
	if err := p.finish(); err != nil {
		t.Fatal(err)
	}
	if err := p.accept([]byte{0}); err == nil {
		t.Fatal("accepted trailing byte")
	}
}

func TestFiniteAndRecordStdoutBudgets(t *testing.T) {
	for _, record := range []bool{false, true} {
		for _, size := range []int{0, proto.MaxPayload, proto.MaxPayload + 1, 3 * proto.MaxPayload} {
			r := &stdoutReadProbe{Reader: io.LimitReader(zeroStdout{}, int64(size))}
			w := &stdoutWriteProbe{}
			err := relayStdout(r, w, false, record)
			bad := !record && size > proto.MaxPayload
			if (err != nil) != bad {
				t.Fatalf("record=%v size=%d err=%v", record, size, err)
			}
			if !bad && w.total != size {
				t.Fatalf("bytes=%d want=%d", w.total, size)
			}
			if r.max > proto.MaxPayload || w.max > proto.MaxPayload {
				t.Fatal("buffer exceeds one frame")
			}
		}
	}
}

func TestStdoutCaps(t *testing.T) {
	for _, tc := range []struct {
		mode string
		argv []string
		cap  int
	}{
		{"pair-forever", []string{"srf-fbread", "pair", ":100", "0"}, pairSize},
		{"oversize-header", []string{"srf-fbread", "pair", ":100", "0"}, 0},
		{"oversize-second-header", []string{"/usr/local/libexec/srf-fbread", "pair", ":100", "0"}, 0},
		{"partial-header", []string{"srf-fbread", "pair", ":100", "0"}, 0},
		{"pair-truncated", []string{"srf-fbread", "pair", ":100", "0"}, 0},
		{"pair-trailing", []string{"srf-fbread", "pair", ":100", "0"}, 16},
		{"flood", []string{"xdotool", "getwindowfocus"}, proto.MaxPayload},
		{"flood", []string{"identity-entrypoint.sh", "xpra-stop"}, proto.MaxPayload},
	} {
		t.Run(tc.mode+tc.argv[0], func(t *testing.T) {
			c, _ := start(t, tc.mode)
			b, _ := json.Marshal(proto.Open{Argv: tc.argv, TimeoutMS: 500})
			send(t, c, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: b})
			rc, out, _ := exit(t, c, 1)
			if rc != 5 || len(out) > tc.cap {
				t.Fatalf("rc=%d stdout=%d; want rc=5 and stdout<=%d", rc, len(out), tc.cap)
			}
		})
	}
}

func TestPairDeadline(t *testing.T) {
	c, _ := start(t, "wait")
	b, _ := json.Marshal(proto.Open{Argv: []string{"srf-fbread", "pair", ":100", "0"}, TimeoutMS: 30})
	send(t, c, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: b})
	rc, _, _ := exit(t, c, 1)
	if rc != 6 {
		t.Fatalf("deadline rc=%d", rc)
	}
}

func TestHelperStdoutCaps(t *testing.T) {
	sources := helperSources(t)
	for _, entry := range helpers.Entries {
		t.Run(entry.Name, func(t *testing.T) {
			c, _ := start(t, "flood")
			c.SetDeadline(time.Now().Add(5 * time.Second))
			argv := []string{"python3", "-I"}
			o := proto.Open{}
			if entry.Stream {
				argv = append(argv, "-u")
				o.StdinCap = -1
			}
			argv = append(argv, "-c", sources[entry.Name])
			if entry.Name == "downloadsPython" {
				argv = append(argv, "/profile/exchange")
			}
			o.Argv = argv
			b, _ := json.Marshal(o)
			send(t, c, proto.Frame{Type: proto.TOpen, Stream: 1, Payload: b})
			total := 0
			// For record, prove output continues beyond the finite-command cap,
			// then explicitly cancel. Discard every frame rather than collecting it.
			for i := 0; i < 1024; i++ {
				f, err := proto.ReadFrame(c)
				if err != nil {
					t.Fatal(err)
				}
				if f.Type == proto.TStdout {
					if len(f.Payload) > proto.MaxPayload {
						t.Fatal("oversize frame")
					}
					total += len(f.Payload)
					if entry.Stream && total > proto.MaxPayload {
						send(t, c, proto.Frame{Type: proto.TCancel, Stream: 1})
						// Cancel once, continue draining until EXIT.
						break
					}
				}
				if f.Type == proto.TExit {
					var x proto.Exit
					if err := json.Unmarshal(f.Payload, &x); err != nil {
						t.Fatal(err)
					}
					if entry.Stream || x.RC != 5 || total > proto.MaxPayload {
						t.Fatalf("stream=%v rc=%d bytes=%d", entry.Stream, x.RC, total)
					}
					return
				}
			}
			if !entry.Stream {
				t.Fatal("missing bounded stdout EXIT")
			}
			rc, _, _ := exit(t, c, 1)
			if rc != 6 {
				t.Fatalf("record cancel rc=%d", rc)
			}
		})
	}
}

func helperSources(t *testing.T) map[string]string {
	t.Helper()
	files, err := filepath.Glob("../../src/browserid/surface/*.go")
	if err != nil {
		t.Fatal(err)
	}
	sources := map[string]string{}
	for _, path := range files {
		f, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			v, ok := n.(*ast.ValueSpec)
			if !ok {
				return true
			}
			for i, name := range v.Names {
				for _, entry := range helpers.Entries {
					if name.Name == entry.Name && i < len(v.Values) {
						lit, ok := v.Values[i].(*ast.BasicLit)
						if !ok {
							t.Fatal("helper source is not a literal")
						}
						s, err := strconv.Unquote(lit.Value)
						if err != nil {
							t.Fatal(err)
						}
						sources[name.Name] = s
					}
				}
			}
			return true
		})
	}
	for _, entry := range helpers.Entries {
		if sources[entry.Name] == "" {
			t.Fatalf("missing helper source %s", entry.Name)
		}
	}
	return sources
}
