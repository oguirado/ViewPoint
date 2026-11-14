package w8warm

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/workerbridge"
)

func recipe(t *testing.T) Recipe {
	t.Helper()
	b, e := os.ReadFile("../../testdata/w8warm/recipe.json")
	if e != nil {
		t.Fatal(e)
	}
	r, e := ParseRecipe(b)
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func good(t *testing.T) JobPlan {
	t.Helper()
	p, e := Plan(recipe(t), BaseImage{Digest: strings.Repeat("a", 64), RecipeSHA256: strings.Repeat("b", 64), Approved: true}, RulesRecord{RulesSHA256: strings.Repeat("c", 64), VerifySHA256: strings.Repeat("d", 64)})
	if e != nil {
		t.Fatal(e)
	}
	return p
}

type fake struct {
	calls   [][]string
	outputs [][]byte
	errAt   int
	cancel  context.CancelFunc
}

func (f *fake) Run(_ context.Context, argv []string, _ io.Reader, out io.Writer) error {
	f.calls = append(f.calls, append([]string{}, argv...))
	n := len(f.calls)
	if n == 1 && f.cancel != nil {
		f.cancel()
	}
	if n == f.errAt {
		return context.Canceled
	}
	if n > 1 && n-2 < len(f.outputs) {
		_, _ = out.Write(f.outputs[n-2])
	}
	return nil
}
func TestRecipeStrict(t *testing.T) {
	b, _ := os.ReadFile("../../testdata/w8warm/recipe.json")
	for _, tc := range []struct {
		name string
		body []byte
	}{
		{"unknown", bytes.Replace(b, []byte(`"repo":"opensuperficies"`), []byte(`"repo":"opensuperficies","surprise":1`), 1)},
		{"float", bytes.Replace(b, []byte(`"cache_miss_rc":4`), []byte(`"cache_miss_rc":4.0`), 1)},
		{"bom", append([]byte{0xef, 0xbb, 0xbf}, b...)},
		{"unknown-lockfile", bytes.Replace(b, []byte(`"path":"go.sum"`), []byte(`"path":"go.sum","mode":"rw"`), 1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, e := ParseRecipe(tc.body)
			if ExitCode(e) != 2 {
				t.Fatalf("got %d: %v", ExitCode(e), e)
			}
		})
	}
}
func TestOperatorAndRulesGates(t *testing.T) {
	r := recipe(t)
	base := BaseImage{Digest: strings.Repeat("a", 64), RecipeSHA256: strings.Repeat("b", 64), Approved: true}
	if p, e := Plan(r, base, RulesRecord{}); ExitCode(e) != 4 || len(p.Fill) != 0 {
		t.Fatalf("missing rules: %v %#v", e, p.Fill)
	}
	base.Approved = false
	if _, e := Plan(r, base, RulesRecord{RulesSHA256: strings.Repeat("c", 64), VerifySHA256: strings.Repeat("d", 64)}); ExitCode(e) != 7 {
		t.Fatalf("unapproved: %v", e)
	}
	if _, e := ParseRules([]byte(`{"rules_sha256":"` + strings.Repeat("A", 64) + `","verify_sha256":"` + strings.Repeat("d", 64) + `"}`)); ExitCode(e) != 5 {
		t.Fatalf("bad pin: %v", e)
	}
}
func TestFixedArgvAndContainment(t *testing.T) {
	p := good(t)
	has := func(a []string, s string) bool {
		for _, v := range a {
			if v == s {
				return true
			}
		}
		return false
	}
	if !has(p.Measure, ReadonlyMount) || !has(p.Fill, writableMount) || has(p.Fill, ReadonlyMount) || !has(p.Measure, "none") || !has(p.Fill, "br-osw") {
		t.Fatal("volume or network mismatch")
	}
	for _, argv := range [][]string{p.Fill, p.Measure} {
		for _, v := range argv {
			if v == "--privileged" || v == "--env-file" || v == "--volume" || strings.Contains(v, "docker.sock") || strings.Contains(v, "type=bind,") {
				t.Fatalf("forbidden %s", v)
			}
		}
		for _, needed := range []string{"--init", "--read-only", "--cap-drop", "--memory", "--memory-swap", "--tmpfs"} {
			if !has(argv, needed) {
				t.Fatalf("missing %s", needed)
			}
		}
	}
	for _, s := range []string{"--privileged", "--env-file", "type=bind,src=/tmp,dst=/opt/cache", "--volume", "/var/run/docker.sock"} {
		bad := p
		bad.Fill = append(append([]string{}, p.Fill...), s)
		if e := validatePlan(bad); ExitCode(e) != 5 {
			t.Fatalf("%s rc %d", s, ExitCode(e))
		}
	}
}
func listing(t *testing.T) []byte {
	t.Helper()
	b, e := os.ReadFile("../../testdata/w8warm/listing.golden.txt")
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func TestDeterministicMeasurementAndManifest(t *testing.T) {
	p := good(t)
	l := listing(t)
	f := &fake{outputs: [][]byte{l, l}}
	got, sha, e := Run(context.Background(), f, p)
	if e != nil {
		t.Fatal(e)
	}
	if !bytes.Equal(got, l) || sha != hash(l) || len(f.calls) != 3 {
		t.Fatalf("unexpected result: %v %s calls=%d", e, sha, len(f.calls))
	}
	again, againSHA, e := Run(context.Background(), &fake{outputs: [][]byte{l, l}}, p)
	if e != nil || againSHA != sha || !bytes.Equal(again, got) {
		t.Fatal("nondeterministic digest")
	}
	name := filepath.Join(t.TempDir(), "manifest.json")
	if e := WriteSeatManifest(name, sha); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(name)
	if e != nil {
		t.Fatal(e)
	}
	golden, e := os.ReadFile("../../testdata/w8warm/seat-manifest.golden.json")
	if e != nil {
		t.Fatal(e)
	}
	if !bytes.Equal(b, golden) {
		t.Fatalf("noncanonical receipt:\n%s\n%s", b, golden)
	}
	st, _ := os.Stat(name)
	if st.Mode().Perm() != 0644 {
		t.Fatalf("mode %v", st.Mode())
	}
	// Test-local copy of the exact config.go:72 predicate.
	m, e := workerbridge.Parse(b)
	str := func(v any) string { s, _ := v.(string); return s }
	hex64 := regexp.MustCompile(`^[a-f0-9]{64}$`)
	if e != nil || len(m) != 3 || m["volume"] != "os_worker_cache" || m["read_only"] != true || !hex64.MatchString(str(m["manifest_sha256"])) {
		t.Fatal("seat predicate refused manifest")
	}
}
func TestUnsafeManifests(t *testing.T) {
	sha := strings.Repeat("a", 64)
	for _, v := range []string{strings.Repeat("A", 64), strings.Repeat("a", 63), ""} {
		if e := WriteSeatManifest(filepath.Join(t.TempDir(), "new"), v); ExitCode(e) != 2 {
			t.Fatalf("invalid sha %q: %v", v, e)
		}
	}
	dir := t.TempDir()
	p := filepath.Join(dir, "out")
	if e := os.Symlink("absent", p); e != nil {
		t.Fatal(e)
	}
	if e := WriteSeatManifest(p, sha); ExitCode(e) != 5 {
		t.Fatalf("symlink: %v", e)
	}
	_ = os.Remove(p)
	if e := os.WriteFile(p, []byte("old"), 0666); e != nil {
		t.Fatal(e)
	}
	if e := os.Chmod(p, 0664); e != nil {
		t.Fatal(e)
	}
	if e := WriteSeatManifest(p, sha); ExitCode(e) != 5 {
		t.Fatalf("group writable: %v", e)
	}
	// Parse and model the exact seat predicate on adversarial raw JSON documents.
	for _, raw := range []string{
		`{"volume":"os_worker_cache","read_only":true,"manifest_sha256":"` + sha + `","other":1}`,
		`{"volume":"os_worker_cache","read_only":false,"manifest_sha256":"` + sha + `"}`,
		`{"volume":"os_worker_cache","read_only":"true","manifest_sha256":"` + sha + `"}`,
		`{"volume":"os_worker_cache","read_only":true,"manifest_sha256":"` + strings.Repeat("A", 64) + `"}`,
		`{"volume":"os_worker_cache","read_only":true,"manifest_sha256":"` + strings.Repeat("a", 63) + `"}`,
	} {
		m, e := workerbridge.Parse([]byte(raw))
		hex64 := regexp.MustCompile(`^[a-f0-9]{64}$`)
		s, _ := m["manifest_sha256"].(string)
		if e == nil && len(m) == 3 && m["volume"] == "os_worker_cache" && m["read_only"] == true && hex64.MatchString(s) {
			t.Fatalf("seat accepted invalid %s", raw)
		}
	}
}
func TestRunFailuresNeverPublish(t *testing.T) {
	p := good(t)
	l := listing(t)
	for _, tc := range []struct {
		name string
		f    *fake
		rc   int
	}{
		{"empty", &fake{outputs: [][]byte{nil, nil}}, 4},
		{"unstable", &fake{outputs: [][]byte{l, []byte(strings.Replace(string(l), "\t3\t", "\t4\t", 1))}}, 6},
		{"fill interruption", &fake{errAt: 1}, 3},
		{"measure interruption", &fake{errAt: 2}, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b, sha, e := Run(context.Background(), tc.f, p)
			if ExitCode(e) != tc.rc || b != nil || sha != "" {
				t.Fatalf("result rc=%d sha=%q listing=%q", ExitCode(e), sha, b)
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	f := &fake{cancel: cancel}
	defer cancel()
	if _, _, e := Run(ctx, f, p); ExitCode(e) != 6 {
		t.Fatalf("interrupted rc=%d", ExitCode(e))
	}
}
func TestListingInvalid(t *testing.T) {
	r := recipe(t)
	line := string(listing(t))
	for _, bad := range []string{"cargo/a\t0\t4755\t" + strings.Repeat("a", 64) + "\n", strings.Replace(line, "gomod/", "../", 1), strings.TrimSuffix(line, "\n"), strings.Replace(line, "gomod/", "other/", 1), line + line} {
		if e := ValidateListing([]byte(bad), r.CachePaths); e == nil {
			t.Fatalf("accepted %q", bad)
		}
	}
}
func TestHash(t *testing.T) {
	s := sha256.Sum256([]byte("abc"))
	if hash([]byte("abc")) != hex.EncodeToString(s[:]) {
		t.Fatal("sha mismatch")
	}
}
func TestPlanCopies(t *testing.T) {
	p := good(t)
	before := append([]string{}, p.Measure...)
	other := append([]string{}, p.Fill...)
	if reflect.DeepEqual(before, other) {
		t.Fatal("fill and measure identical")
	}
}
