package w8muse

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"strings"
	"sync"
	"testing"

	"opensuperficies.local/srf/src/w8seat/patchpolicy"
)

type commandFunc func(context.Context, []string, io.Reader, io.Writer) error

func (f commandFunc) Run(c context.Context, a []string, i io.Reader, o io.Writer) error {
	return f(c, a, i, o)
}
func TestBundleStreamsWithoutDiskAndBindsRemoteTip(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	runner := commandFunc(func(_ context.Context, a []string, _ io.Reader, out io.Writer) error {
		s := strings.Join(a, " ")
		mu.Lock()
		calls = append(calls, s)
		mu.Unlock()
		switch {
		case strings.Contains(s, "remote get-url"):
			_, _ = io.WriteString(out, "https://github.com/team/project.git\n")
		case strings.Contains(s, "ls-remote"):
			_, _ = io.WriteString(out, strings.Repeat("b", 40)+"\trefs/heads/bridge/integration\n")
		case strings.Contains(s, "bundle create"):
			_, e := io.WriteString(out, "bundle bytes")
			return e
		case strings.Contains(s, "merge-base"):
			if !strings.HasSuffix(s, strings.Repeat("a", 40)+" "+strings.Repeat("b", 40)) && !strings.HasSuffix(s, strings.Repeat("a", 40)+" "+IntegrationRef) {
				t.Error(s)
			}
		default:
			t.Error(s)
		}
		return nil
	})
	repo := scratch(t)
	b, e := (GitRepository{Runner: runner}).Prepare(context.Background(), repo, strings.Repeat("a", 40), "team/project")
	if e != nil {
		t.Fatal(e)
	}
	contents, e := io.ReadAll(b.Reader)
	_ = b.Reader.Close()
	if e != nil || string(contents) != "bundle bytes" || b.Length != 12 || b.Tip != strings.Repeat("b", 40) {
		t.Fatalf("%+v %q %v", b, contents, e)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 6 || !strings.HasSuffix(calls[len(calls)-1], "bundle create - "+IntegrationRef) {
		t.Fatal(calls)
	}
	files, _ := os.ReadDir(repo)
	if len(files) != 0 {
		t.Fatalf("bundle persisted: %v", files)
	}
}
func TestPinNotProvenStopsBeforeBundle(t *testing.T) {
	calls := 0
	runner := commandFunc(func(_ context.Context, a []string, _ io.Reader, out io.Writer) error {
		calls++
		s := strings.Join(a, " ")
		switch {
		case strings.Contains(s, "remote get-url"):
			_, _ = io.WriteString(out, "https://github.com/team/project\n")
		case strings.Contains(s, "ls-remote"):
			_, _ = io.WriteString(out, strings.Repeat("b", 40)+"\trefs/heads/bridge/integration\n")
		case strings.Contains(s, "merge-base"):
			return refusal(3, "setup", "not ancestor")
		default:
			t.Fatal(s)
		}
		return nil
	})
	_, e := (GitRepository{Runner: runner}).Prepare(context.Background(), "mirror", strings.Repeat("a", 40), "team/project")
	if ExitCode(e) != 2 || calls != 3 {
		t.Fatalf("%d %v", calls, e)
	}
}
func TestPublisherRejectsParityBeforeAnyGit(t *testing.T) {
	calls := 0
	g := GitPublisher{Runner: commandFunc(func(context.Context, []string, io.Reader, io.Writer) error { calls++; return nil })}
	if e := g.Publish(context.Background(), Publication{}); ExitCode(e) != 4 || calls != 0 {
		t.Fatalf("%v calls=%d", e, calls)
	}
}
func TestSharedPatchPolicyCases(t *testing.T) {
	b, e := os.ReadFile("../../testdata/w8seat/patchpolicy_cases.json")
	if e != nil {
		t.Fatal(e)
	}
	var cases []struct {
		Name       string   `json:"name"`
		Diff       string   `json:"diff"`
		WritePaths []string `json:"write_paths"`
		OK         bool     `json:"accept"`
	}
	if e = json.Unmarshal(b, &cases); e != nil {
		t.Fatal(e)
	}
	if len(cases) == 0 {
		t.Fatal("shared fixtures absent")
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			_, e := patchpolicy.Check([]byte(c.Diff), c.WritePaths)
			if (e == nil) != c.OK {
				t.Fatalf("ok=%t err=%v", c.OK, e)
			}
		})
	}
}
func TestMirrorTipModeReadsTrackingRefWithoutNetwork(t *testing.T) {
	for _, tc := range []struct {
		name, tip string
		fail      bool
		want      int
	}{{"ok", strings.Repeat("b", 40), false, 0}, {"absent", "", true, 4}, {"shape", "not-a-sha", false, 2}} {
		runner := commandFunc(func(_ context.Context, a []string, _ io.Reader, out io.Writer) error {
			s := strings.Join(a, " ")
			switch {
			case strings.Contains(s, "ls-remote"):
				t.Fatalf("%s: mirror mode used the network: %s", tc.name, s)
			case strings.Contains(s, "remote get-url"):
				_, _ = io.WriteString(out, "https://github.com/team/project.git\n")
			case strings.Contains(s, "rev-parse --verify --end-of-options "+IntegrationRef+"^{commit}"):
				if tc.fail {
					return refusal(3, "setup", "no ref")
				}
				_, _ = io.WriteString(out, tc.tip+"\n")
			case strings.Contains(s, "merge-base"):
				if !strings.HasSuffix(s, strings.Repeat("a", 40)+" "+strings.Repeat("b", 40)) {
					t.Error(s)
				}
			case strings.Contains(s, "bundle create"):
				if !strings.HasSuffix(s, "bundle create - "+IntegrationRef) {
					t.Errorf("%s: bundle must name the mirror ref: %s", tc.name, s)
				}
				_, e := io.WriteString(out, "bundle bytes")
				return e
			default:
				t.Errorf("%s: %s", tc.name, s)
			}
			return nil
		})
		b, e := (GitRepository{Runner: runner, TipFromMirror: true}).Prepare(context.Background(), scratch(t), strings.Repeat("a", 40), "team/project")
		if got := ExitCode(e); got != tc.want {
			t.Fatalf("%s: rc=%d err=%v", tc.name, got, e)
		}
		if e == nil {
			if b.Tip != tc.tip {
				t.Fatalf("%s: tip %q", tc.name, b.Tip)
			}
			_ = b.Reader.Close()
		}
	}
}
