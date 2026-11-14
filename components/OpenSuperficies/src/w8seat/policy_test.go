package w8seat

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCpusetFromOperatorFile(t *testing.T) {
	p := filepath.Join(t.TempDir(), "cpus.json")
	if _, e := LoadCPUConfig(p, "10-14"); ExitCode(e) != 4 {
		t.Fatal(e)
	}
	for _, v := range []string{`{"worker":"10-14","interactive":"0-9,15-19"}`, `{"worker":"10-14","interactive":"0-10"}`, `{"worker":"9-14","interactive":"0-8"}`, `{"worker":"10-14","interactive":""}`} {
		if e := os.WriteFile(p, []byte(v), 0600); e != nil {
			t.Fatal(e)
		}
		c, e := LoadCPUConfig(p, "10 11 12 13 14")
		if strings.Contains(v, "0-9,15-19") {
			if e != nil || len(c.Worker) != 5 {
				t.Fatal(c, e)
			}
		} else if ExitCode(e) != 4 {
			t.Fatal(c, e)
		}
	}
}
func TestPeerCredSupplementaryAdminAndRoot(t *testing.T) {
	lookup := func(uid uint32) ([]string, error) { return []string{"12", "42"}, nil }
	for _, c := range []struct {
		u, g uint32
		want int
	}{{0, 42, 7}, {1001, 12, 0}, {1002, 42, 0}} {
		if e := AuthorizePeer(c.u, c.g, "42", lookup); ExitCode(e) != c.want {
			t.Fatalf("%+v: %v", c, e)
		}
	}
	if e := AuthorizePeer(123, 12, "99", lookup); ExitCode(e) != 7 {
		t.Fatal(e)
	}
}
func tarBytes(t *testing.T, name string, body []byte, typ byte, format tar.Format) []byte {
	t.Helper()
	var b bytes.Buffer
	w := tar.NewWriter(&b)
	h := &tar.Header{Name: name, Size: int64(len(body)), Mode: 0644, Typeflag: typ, Format: format}
	if typ != tar.TypeReg {
		h.Size = 0
		body = nil
	}
	if e := w.WriteHeader(h); e != nil {
		t.Fatal(e)
	}
	if _, e := w.Write(body); e != nil {
		t.Fatal(e)
	}
	if e := w.Close(); e != nil {
		t.Fatal(e)
	}
	return b.Bytes()
}
func TestExtractAdmissionTarBombAndSymlink(t *testing.T) {
	for _, typ := range []byte{tar.TypeSymlink, tar.TypeFifo, tar.TypeLink} {
		if _, e := AdmitExtract(bytes.NewReader(tarBytes(t, "report.txt", nil, typ, tar.FormatUSTAR)), nil); ExitCode(e) != 5 {
			t.Fatal(e)
		}
	}
	b := tarBytes(t, "report.txt", []byte("ok"), tar.TypeReg, tar.FormatUSTAR)
	if _, e := AdmitExtract(bytes.NewReader(append(b, make([]byte, 196609)...)), nil); ExitCode(e) != 5 {
		t.Fatal(e)
	}
	b = tarBytes(t, "report.txt", []byte("ok"), tar.TypeReg, tar.FormatPAX)
	// FormatPAX can be emitted as USTAR for ordinary headers; force metadata below.
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	_ = w.WriteHeader(&tar.Header{Name: "report.txt", Size: 2, Mode: 0644, Format: tar.FormatPAX, PAXRecords: map[string]string{"comment": "x"}})
	_, _ = w.Write([]byte("ok"))
	_ = w.Close()
	if _, e := AdmitExtract(&buf, nil); ExitCode(e) != 5 {
		t.Fatal(e)
	}
	_ = b
}
func TestTextExtractorDispatch(t *testing.T) {
	for _, c := range []struct {
		name, body string
		want       int
	}{
		{"report.txt", "gate passed\n", 0}, {"sha.txt", strings.Repeat("a", 64), 0}, {"frame.txt", strings.Repeat("AbcD+/", 30), 5}, {"frame.txt", strings.Repeat("ab", 2048), 5}, {"frame.json", `{"frame":"` + strings.Repeat("ab", 2048) + `"}`, 5}, {"bad.json", "[]", 5},
		{"delta.patch", "diff --git a/src/a.go b/src/a.go\n--- a/src/a.go\n+++ b/src/a.go\n@@ -1 +1 @@\n-old\n+new\n", 0},
	} {
		_, e := AdmitExtract(bytes.NewReader(tarBytes(t, c.name, []byte(c.body), tar.TypeReg, tar.FormatUSTAR)), []string{"src/a.go"})
		if ExitCode(e) != c.want {
			t.Fatalf("%s: %v", c.name, e)
		}
	}
}
func TestNoRc1(t *testing.T) {
	for i := -2; i < 260; i++ {
		if Normalize(i) == 1 {
			t.Fatal(i)
		}
	}
}
func TestProtocolStrictFrames(t *testing.T) {
	var b bytes.Buffer
	r := Request{Verb: "health"}
	if e := WriteFrame(&b, r); e != nil {
		t.Fatal(e)
	}
	var got Request
	if e := ReadFrame(&b, &got); e != nil || got.Verb != "health" {
		t.Fatal(got, e)
	}
	b.Write([]byte{0, 32, 0, 0})
	if e := ReadFrame(&b, &got); ExitCode(e) != 2 {
		t.Fatal(e)
	}
	_ = json.Valid
}

func TestExtractUSTARCountDuplicateAndHugeHeader(t *testing.T) {
	var b bytes.Buffer
	w := tar.NewWriter(&b)
	_ = w.WriteHeader(&tar.Header{Name: "report.txt", Mode: 0644, Size: 1 << 30, Format: tar.FormatUSTAR})
	if _, e := AdmitExtract(&b, nil); ExitCode(e) != 5 {
		t.Fatal(e)
	}
	for _, count := range []int{64, 65} {
		b.Reset()
		w = tar.NewWriter(&b)
		for i := 0; i < count; i++ {
			name := fmt.Sprintf("r%d.txt", i)
			_ = w.WriteHeader(&tar.Header{Name: name, Mode: 0644, Size: 1, Format: tar.FormatUSTAR})
			_, _ = w.Write([]byte("x"))
		}
		_ = w.Close()
		_, e := AdmitExtract(&b, nil)
		if (e == nil) != (count == 64) {
			t.Fatal(count, e)
		}
	}
	b.Reset()
	w = tar.NewWriter(&b)
	for i := 0; i < 2; i++ {
		_ = w.WriteHeader(&tar.Header{Name: "duplicate.txt", Mode: 0644, Size: 1, Format: tar.FormatUSTAR})
		_, _ = w.Write([]byte("x"))
	}
	_ = w.Close()
	if _, e := AdmitExtract(&b, nil); ExitCode(e) != 5 {
		t.Fatal(e)
	}
}
func TestForbiddenRequestFieldsAreContainment(t *testing.T) {
	for _, key := range []string{"binds", "env_file", "mounts"} {
		var b bytes.Buffer
		if e := WriteFrame(&b, map[string]any{"verb": "submit", key: "forbidden"}); e != nil {
			t.Fatal(e)
		}
		var r Request
		if e := ReadFrame(&b, &r); ExitCode(e) != 5 {
			t.Fatal(key, e)
		}
	}
}
func TestSeccompRequiredExactlyOnce(t *testing.T) {
	p := testPolicy()
	a, e := RenderArgv(DefaultLaunchSpec(p), "abcdef123456", "lease-a", p)
	if e != nil {
		t.Fatal(e)
	}
	for i, v := range a {
		if v == "seccomp="+SeccompPath {
			a[i] = "seccomp=unconfined"
		}
	}
	if e = AssertArgv(a, "abcdef123456", "lease-a", p); ExitCode(e) != 5 {
		t.Fatal(e)
	}
}
