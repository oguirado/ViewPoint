package w8seat

import (
	"context"
	"encoding/json"
	"io"
	"opensuperficies.local/srf/src/workerbridge"
	"os"
	"strings"
	"testing"
)

type countRunner struct{ calls int }

func (r *countRunner) Run(context.Context, []string, io.Reader, io.Writer) error {
	r.calls++
	return nil
}
func testPolicy() LaunchPolicy {
	return LaunchPolicy{CPUs: CPUConfig{Worker: []int{10, 11, 12, 13, 14}, Interactive: []int{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 15, 16, 17, 18, 19}}, ImageSHA: strings.Repeat("e", 64), SeccompFile: SeccompPath, SeccompSHA: strings.Repeat("d", 64), SeccompInline: `{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[]}`}
}
func TestArgvGolden(t *testing.T) {
	p := testPolicy()
	a, e := RenderArgv(DefaultLaunchSpec(p), "abcdef123456", "lease-a", p)
	if e != nil {
		t.Fatal(e)
	}
	joined := strings.Join(a, " ")
	for _, s := range []string{"--cpuset-cpus 10,11,12,13,14", "--security-opt seccomp=" + SeccompPath, "--memory 4g --memory-swap 4g", "--log-driver none --restart no", "os-worker-base@sha256:" + p.ImageSHA + " sleep infinity"} {
		if !strings.Contains(joined, s) {
			t.Fatalf("missing %s", s)
		}
	}
	if strings.Count(joined, "seccomp=") != 1 {
		t.Fatal(joined)
	}
}
func TestMustFailTrioZeroRuns(t *testing.T) {
	p := testPolicy()
	b, e := os.ReadFile("../../testdata/w8seat/launch_negatives.json")
	if e != nil {
		t.Fatal(e)
	}
	var fixtures []struct {
		Name  string         `json:"name"`
		Extra map[string]any `json:"extra"`
	}
	if e = json.Unmarshal(b, &fixtures); e != nil {
		t.Fatal(e)
	}
	for _, fixture := range fixtures {
		key := fixture.Name
		var m map[string]any
		_ = json.Unmarshal(DefaultLaunchSpec(p), &m)
		m[key] = fixture.Extra[key]
		r := &countRunner{}
		e := Create(context.Background(), r, canonical(m), "abcdef123456", "lease-a", 1000, p)
		if ExitCode(e) != 5 || r.calls != 0 {
			t.Fatal(key, e, r.calls)
		}
	}
	var m map[string]any
	_ = json.Unmarshal(DefaultLaunchSpec(p), &m)
	m["volumes"] = []any{map[string]any{"source": "/tmp/host", "target": "/opt/cache", "read_only": true}}
	r := &countRunner{}
	if e := Create(context.Background(), r, canonical(m), "abcdef123456", "lease-a", 1000, p); ExitCode(e) != 5 || r.calls != 0 {
		t.Fatal(e)
	}
}
func TestOnlyFiveVerbsCoversC2ToC5(t *testing.T) {
	for _, v := range append(append([]string{}, verbs...), "shell", "copy", "model") {
		a, e := ExecArgv("abcdef123456", v)
		allowed := false
		for _, x := range verbs {
			allowed = allowed || x == v
		}
		if (e == nil) != allowed {
			t.Fatal(v, e)
		}
		if e == nil && a[len(a)-1] != "/opt/w8/"+v {
			t.Fatal(a)
		}
	}
}

func TestRecipePATHIsRequiredAndExact(t *testing.T) {
	p := testPolicy()
	const imagePATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
	for _, tc := range []struct {
		name, path, extra string
		want              int
	}{
		{"pinned", imagePATH, "", 0}, {"missing", "", "", 5},
		{"altered", "/tmp:/usr/bin", "", 5}, {"unknown", imagePATH, "UNDECLARED=value", 5},
		{"duplicate", imagePATH, "PATH=" + imagePATH, 5}, {"emptyUnknown", imagePATH, "UNKNOWN=", 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := fakeInspect(p, "abcdef123456", "lease-a")
			m, _ = workerbridge.Parse(canonical(m))
			c := obj(m["Config"])
			env := []string{}
			for _, e := range strs(c["Env"]) {
				if !strings.HasPrefix(e, "PATH=") {
					env = append(env, e)
				}
			}
			if tc.path != "" {
				env = append(env, "PATH="+tc.path)
			}
			if tc.extra != "" {
				env = append(env, tc.extra)
			}
			c["Env"] = env
			_, _, err := inspectAdmission(canonical([]any{m}), p, "abcdef123456", "lease-a")
			if ExitCode(err) != tc.want {
				t.Fatalf("rc=%d want=%d: %v", ExitCode(err), tc.want, err)
			}
		})
	}
}
