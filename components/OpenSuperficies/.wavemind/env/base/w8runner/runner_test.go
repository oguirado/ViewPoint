package main

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestTextExtractorDispatch(t *testing.T) {
	for _, tc := range []struct {
		name, text string
		ok         bool
	}{
		{"report.md", "healthy report\n", true}, {"hash.txt", strings.Repeat("a", 64), true},
		{"frame.txt", strings.Repeat("ab", 2048), false}, {"frame.json", `{"frame":"` + strings.Repeat("a", 129) + `"}`, false},
		{"frame.txt", strings.Repeat("YWFh", 40), false}, {"bad.json", `{"a":1} {"b":2}`, false},
		{"delta.patch", "diff --git a/src/a.go b/src/a.go\n--- a/src/a.go\n+++ b/src/a.go\n@@ -1 +1 @@\n-old\n+new\n", true},
		{"bad.patch", "GIT binary patch\n", false}, {"report.txt", "bad\x00", false},
	} {
		if got := validText(tc.name, []byte(tc.text), []string{"src/a.go"}) == nil; got != tc.ok {
			t.Errorf("%s accepted=%v", tc.name, got)
		}
	}
}
func TestExtractUSTARAndNoSymlink(t *testing.T) {
	dir := t.TempDir()
	os.WriteFile(filepath.Join(dir, "report.md"), []byte("ok\n"), 0644)
	var b bytes.Buffer
	if e := extract(dir, &b, []string{"src/a.go"}); e != nil {
		t.Fatal(e)
	}
	tr := tar.NewReader(&b)
	h, e := tr.Next()
	if e != nil || h.Format != tar.FormatUSTAR || h.Name != "report.md" || h.Mode != 0644 {
		t.Fatalf("header=%+v err=%v", h, e)
	}
	os.Symlink("report.md", filepath.Join(dir, "link.txt"))
	b.Reset()
	if extract(dir, &b, nil) == nil {
		t.Fatal("symlink accepted")
	}
}
func TestGateRingRawStatusAndTimeout(t *testing.T) {
	root := t.TempDir()
	fake := &fakeCommands{output: strings.Repeat("x", 70000), raw: 1}
	r := runner{root: root, commands: fake}
	var b bytes.Buffer
	if rc := r.gate(strings.NewReader(`{"argv":["tool","arg"],"timeout_ms":100}`), &b); rc != 5 {
		t.Fatalf("rc=%d", rc)
	}
	var metrics map[string]any
	json.Unmarshal(b.Bytes(), &metrics)
	if metrics["stdout_total_bytes"] != float64(70000) || metrics["stdout_retained_bytes"] != float64(65536) || metrics["stdout_truncated"] != true {
		t.Fatalf("missing ring measurements: %s", b.String())
	}
	var s progress
	json.Unmarshal(b.Bytes(), &s)
	if s.RawStatus != 1 || s.RC != 5 {
		t.Fatalf("%+v", s)
	}
	log, _ := os.ReadFile(filepath.Join(root, "out/gate.log.txt"))
	if len(log) != 65536 {
		t.Fatalf("ring=%d", len(log))
	}
	fake.timeout = true
	b.Reset()
	if r.gate(strings.NewReader(`{"argv":["tool"],"timeout_ms":1}`), &b) != 5 {
		t.Fatal("timeout")
	}
	json.Unmarshal(b.Bytes(), &s)
	if !s.TimedOut || s.RawStatus != 124 {
		t.Fatalf("%+v", s)
	}
}
func TestIngestUnsafeBeforeBundleAndHeadMismatch(t *testing.T) {
	fake := &fakeCommands{head: strings.Repeat("b", 40)}
	r := runner{root: t.TempDir(), commands: fake, probe: func(string) (probes, error) { return probes{}, io.ErrUnexpectedEOF }}
	var b bytes.Buffer
	if rc := r.ingest(strings.NewReader("unread bundle"), &b); rc != 5 || fake.calls != 0 {
		t.Fatalf("rc=%d calls=%d", rc, fake.calls)
	}
	r.probe = func(string) (probes, error) { return safeProbes(), nil }
	header, _ := json.Marshal(ingestRequest{Commit: strings.Repeat("a", 40), BundleLen: 3, WritePaths: []string{"src/a.go"}})
	var in bytes.Buffer
	writeHeader(&in, header)
	in.WriteString("git")
	b.Reset()
	if rc := r.ingest(&in, &b); rc != 3 {
		t.Fatalf("head mismatch rc=%d: %s", rc, b.String())
	}
}
func TestPurgeHiddenFilesAndNoRc1(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, ".w8"), 0700)
	os.WriteFile(filepath.Join(root, ".w8/status.json"), []byte("{}"), 0600)
	r := runner{root: root}
	var b bytes.Buffer
	if r.purge(&b) != 0 {
		t.Fatal(b.String())
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 0 {
		t.Fatal("hidden files remain")
	}
	for raw := -2; raw < 140; raw++ {
		if rc := normalize(raw); rc == 1 || rc < 0 || rc > 7 {
			t.Fatalf("raw=%d rc=%d", raw, rc)
		}
	}
}

type fakeCommands struct {
	output, head string
	raw, calls   int
	timeout      bool
}

func (f *fakeCommands) run(_ context.Context, _ string, argv []string, _ io.Reader, out io.Writer) (int, string, bool) {
	f.calls++
	if slices.Contains(argv, "rev-parse") {
		io.WriteString(out, f.head)
		return 0, "", false
	}
	if slices.Contains(argv, "clone") || slices.Contains(argv, "checkout") {
		return 0, "", false
	}
	io.WriteString(out, f.output)
	return f.raw, "", f.timeout
}
func safeProbes() probes {
	return probes{EtcWrite: "read_only", HomeAdmin: "absent", EgressProbe: "BLOCKED", GatewayProbe: "NO_ROUTE"}
}

func TestSharedPatchPolicyCases(t *testing.T) {
	b, e := os.ReadFile("../../../../testdata/w8seat/patchpolicy_cases.json")
	if e != nil {
		t.Fatal(e)
	}
	var cases []struct {
		Name       string   `json:"name"`
		Diff       string   `json:"diff"`
		WritePaths []string `json:"write_paths"`
		Accept     bool     `json:"accept"`
	}
	if e = json.Unmarshal(b, &cases); e != nil {
		t.Fatal(e)
	}
	if len(cases) == 0 {
		t.Fatal("empty shared cases")
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			if accepted := checkPatch([]byte(c.Diff), c.WritePaths) == nil; accepted != c.Accept {
				t.Fatalf("accepted=%v want=%v", accepted, c.Accept)
			}
		})
	}
}
func TestRunnerExtractCapsAndStrictText(t *testing.T) {
	for _, tc := range []struct{ name, text string }{
		{"bad.json", `{"a":1,"a":2}`}, {"bad.json", `[1,2]`}, {"bad.txt", strings.Repeat("a", 129)},
		{"bad.txt", strings.Repeat("A", 100)}, {"bad.txt", "\x1b[31mred"},
	} {
		if validText(tc.name, []byte(tc.text), nil) == nil {
			t.Errorf("accepted %s %q", tc.name, tc.text)
		}
	}
	dir := t.TempDir()
	var b bytes.Buffer
	for i := 0; i < 65; i++ {
		os.WriteFile(filepath.Join(dir, fmt.Sprintf("%02d.txt", i)), []byte("x"), 0644)
	}
	if extract(dir, &b, nil) == nil || b.Len() != 0 {
		t.Fatal("entry limit must reject before writing")
	}
	os.Remove(filepath.Join(dir, "64.txt"))
	b.Reset()
	if e := extract(dir, &b, nil); e != nil {
		t.Fatal(e)
	}
	if b.Len() > archiveCap {
		t.Fatal("tar cap")
	}
}
func TestRunnerOnlyFiveVerbsAndNoRc1(t *testing.T) {
	r := runner{root: t.TempDir()}
	for _, v := range []string{"shell", "cp", "exec", "model", ""} {
		var out bytes.Buffer
		if rc := r.run(v, strings.NewReader(""), &out); rc != 2 {
			t.Errorf("%s rc=%d", v, rc)
		}
	}
}

func TestJSONEscapesCannotHideEncodedBinary(t *testing.T) {
	for _, content := range []string{`{"frame":"` + strings.Repeat(`\u0061`, 129) + `"}`, `{"` + strings.Repeat(`\u0061`, 129) + `":"ok"}`, `{"frame":"` + strings.Repeat(`\u0059\u0057\u0046\u0068`, 30) + `"}`} {
		if validText("report.json", []byte(content), nil) == nil {
			t.Fatal("escaped encoded data accepted")
		}
	}
}

func TestIngestCapsAndGateShapeNeverExecute(t *testing.T) {
	for _, req := range []ingestRequest{
		{Commit: strings.Repeat("a", 40), BundleLen: bundleCap + 1, WritePaths: []string{"src/a.go"}},
		{Commit: strings.Repeat("a", 40), BundleLen: 1, PatchLen: patchCap + 1, WritePaths: []string{"src/a.go"}},
		{Commit: strings.Repeat("a", 64), BundleLen: 1, WritePaths: []string{"src/a.go"}},
		{Commit: strings.Repeat("a", 40), BundleLen: 1, WritePaths: []string{"src/a.go"}, Gateway: "127.0.0.1"},
	} {
		fake := &fakeCommands{}
		r := runner{root: t.TempDir(), commands: fake, probe: func(string) (probes, error) { return safeProbes(), nil }}
		h, _ := json.Marshal(req)
		var in, out bytes.Buffer
		writeHeader(&in, h)
		if rc := r.ingest(&in, &out); rc != 2 || fake.calls != 0 {
			t.Fatalf("rc=%d calls=%d", rc, fake.calls)
		}
	}
	for _, body := range []string{`{"argv":[],"timeout_ms":1}`, `{"argv":["tool"],"timeout_ms":900001}`, `{"argv":["tool"],"timeout_ms":1,"env_file":"x"}`, `{"argv":[""],"timeout_ms":1}`} {
		fake := &fakeCommands{}
		r := runner{root: t.TempDir(), commands: fake}
		var out bytes.Buffer
		if rc := r.gate(strings.NewReader(body), &out); rc != 2 || fake.calls != 0 {
			t.Fatalf("shape rc=%d calls=%d", rc, fake.calls)
		}
	}
}
func TestMetadataDoesNotFollowSymlinkOrReadOversize(t *testing.T) {
	root := t.TempDir()
	os.Mkdir(filepath.Join(root, ".w8"), 0700)
	outside := filepath.Join(root, "outside.json")
	os.WriteFile(outside, []byte(`{"rc":0}`), 0600)
	os.Symlink(outside, filepath.Join(root, ".w8/status.json"))
	r := runner{root: root}
	var out bytes.Buffer
	if rc := r.run("status", nil, &out); rc != 4 {
		t.Fatalf("symlink status rc=%d", rc)
	}
	os.Remove(filepath.Join(root, ".w8/status.json"))
	os.WriteFile(filepath.Join(root, ".w8/status.json"), []byte(strings.Repeat(" ", 1<<20+1)), 0600)
	out.Reset()
	if r.run("status", nil, &out) != 4 {
		t.Fatal("oversized status")
	}
}

func TestReceiptDataMeasuresFilesAndAnswers(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, "repo/src"), 0700)
	os.MkdirAll(filepath.Join(root, "out"), 0700)
	os.MkdirAll(filepath.Join(root, ".w8"), 0700)
	os.WriteFile(filepath.Join(root, "repo/src/a.go"), []byte("new\n"), 0755)
	patch := []byte("diff --git a/src/a.go b/src/a.go\nold mode 100644\nnew mode 100755\n--- a/src/a.go\n+++ b/src/a.go\n@@ -1 +1 @@\n-old\n+new\n")
	req := ingestRequest{WritePaths: []string{"src/a.go"}, EvidenceRequests: []evidenceRequest{{RequestID: "sha", Kind: "sha256", Path: "src/a.go"}, {RequestID: "line", Kind: "line", Path: "src/a.go", Line: 1}}}
	r := runner{root: root}
	if e := r.receiptData(req, patch, nil); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(filepath.Join(root, "out/receipt-data.json"))
	if e != nil {
		t.Fatal(e)
	}
	var record receiptData
	if e = json.Unmarshal(b, &record); e != nil {
		t.Fatal(e)
	}
	if len(record.Files) != 1 || record.Files[0].Mode != "100755" || record.Files[0].Bytes != 4 || len(record.Answers) != 2 || record.Answers[1].Answer != "new" {
		t.Fatalf("%+v", record)
	}
	req.EvidenceRequests = []evidenceRequest{{RequestID: "text", Kind: "text", Text: "what happened"}}
	if r.receiptData(req, patch, nil) == nil {
		t.Fatal("missing model answer invented")
	}
}

func TestGatePostProcessingKeepsRawStatus(t *testing.T) {
	root := t.TempDir()
	os.MkdirAll(filepath.Join(root, ".w8"), 0700)
	os.WriteFile(filepath.Join(root, ".w8/request.json"), []byte("{"), 0600)
	r := runner{root: root, commands: &fakeCommands{output: "gate output\n"}}
	var out bytes.Buffer
	if rc := r.gate(strings.NewReader(`{"argv":["tool"],"timeout_ms":1}`), &out); rc != 5 {
		t.Fatalf("rc=%d", rc)
	}
	var s progress
	if json.Unmarshal(out.Bytes(), &s) != nil || s.RawStatus != 0 || s.RC != 5 || s.StdoutTotalBytes != 12 {
		t.Fatalf("raw evidence lost: %s", out.String())
	}
}

func TestWorkspacePeakPersistsAndNeverBecomesZeroOnError(t *testing.T) {
	root := t.TempDir()
	values := []int64{4096, 16384, 8192, 32768}
	n := 0
	r := runner{root: root, workspaceUsed: func(string) (int64, error) { v := values[n]; n++; return v, nil }}
	for _, want := range []int64{4096, 16384, 16384, 32768} {
		got, err := r.sampleWorkspace()
		if err != nil || got != want {
			t.Fatalf("peak=%d want=%d err=%v", got, want, err)
		}
	}
	// Each capsule verb is a fresh process: the maximum must survive a new runner.
	r = runner{root: root, workspaceUsed: func(string) (int64, error) { return 4096, nil }}
	if got, err := r.sampleWorkspace(); err != nil || got != 32768 {
		t.Fatalf("restarted peak=%d %v", got, err)
	}
	r.workspaceUsed = func(string) (int64, error) { return 0, io.ErrUnexpectedEOF }
	if _, err := r.sampleWorkspace(); err == nil {
		t.Fatal("missing measurement passed")
	}
}
func TestGateReportsMeasuredWorkspacePeak(t *testing.T) {
	r := runner{root: t.TempDir(), commands: &fakeCommands{}, workspaceUsed: func(string) (int64, error) { return 12288, nil }}
	var out bytes.Buffer
	if rc := r.gate(strings.NewReader(`{"argv":["tool"],"timeout_ms":100}`), &out); rc != 0 {
		t.Fatalf("gate rc=%d %s", rc, out.String())
	}
	var m map[string]any
	if json.Unmarshal(out.Bytes(), &m) != nil || m["workspace_peak_bytes"] != float64(12288) {
		t.Fatalf("missing peak: %s", out.String())
	}
}

// Real git must accept the delta argv: a rejected flag fails every gate with rc 5.
func TestDeltaArgvAcceptedByRealGit(t *testing.T) {
	if _, e := exec.LookPath("git"); e != nil {
		t.Skip("git absent")
	}
	dir := t.TempDir()
	g := func(args ...string) []byte {
		c := exec.Command("git", append([]string{"-C", dir}, args...)...)
		c.Env = append(os.Environ(), "GIT_CONFIG_GLOBAL=/dev/null", "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@t", "GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@t")
		out, e := c.CombinedOutput()
		if e != nil {
			t.Fatalf("git %v: %v %s", args, e, out)
		}
		return out
	}
	g("init", "-q")
	g("commit", "-q", "--allow-empty", "-m", "pin")
	pin := strings.TrimSpace(string(g("rev-parse", "HEAD")))
	if e := os.MkdirAll(filepath.Join(dir, "src"), 0o755); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(filepath.Join(dir, "src", "a.go"), []byte("package a\n"), 0o644); e != nil {
		t.Fatal(e)
	}
	g("add", "--intent-to-add", "--", ".")
	argv := deltaArgv(pin)
	out := g(argv[1:]...)
	if e := checkPatch(out, []string{"src/a.go"}); e != nil {
		t.Fatalf("delta %q: %v", out, e)
	}
}
