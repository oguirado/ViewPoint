package workerbridge

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
)

func fixture(t *testing.T, name string, out any) {
	t.Helper()
	b, err := os.ReadFile("../../testdata/workerbridge/" + name)
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(b, out); err != nil {
		t.Fatal(err)
	}
}
func wire(t *testing.T, v any) []byte {
	t.Helper()
	b, e := Canonical(v)
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func doc(t *testing.T, b []byte) Document {
	t.Helper()
	d, e := Parse(b)
	if e != nil {
		t.Fatal(e)
	}
	return d
}
func rejects(t *testing.T, e error, code int) {
	t.Helper()
	if e == nil || ExitCode(e) != code {
		t.Fatalf("want rc %d, got %v (%d)", code, e, ExitCode(e))
	}
}
func golden(t *testing.T) map[string]json.RawMessage {
	t.Helper()
	var g map[string]json.RawMessage
	fixture(t, "codec.json", &g)
	return g
}

func TestCanonicalIndependentVectors(t *testing.T) {
	var vectors []struct {
		Value          json.RawMessage
		Worker, Bridge string
	}
	fixture(t, "canonical.json", &vectors)
	for _, v := range vectors {
		x, e := Decode(v.Value)
		if e != nil {
			t.Fatal(e)
		}
		w, e := Canonical(x)
		if e != nil || string(w) != v.Worker {
			t.Fatalf("worker %q != %q (%v)", w, v.Worker, e)
		}
		b, e := BridgeCanonical(x)
		if e != nil || string(b) != v.Bridge {
			t.Fatalf("bridge %q != %q (%v)", b, v.Bridge, e)
		}
	}
}
func TestStrictJSON(t *testing.T) {
	for _, s := range []string{`{"x":1,"x":2}`, `{"x":1e0}`, `{"x":1.0}`, `{"x":-0}`, `{"x":"\ud800"}`, `{} {}`, "\xef\xbb\xbf{}", string([]byte{'{', '"', 'x', '"', ':', '"', 255, '"', '}'})} {
		if _, e := Decode([]byte(s)); e == nil {
			t.Fatalf("accepted %q", s)
		}
	}
}
func TestProjectionAndByteRoundTrip(t *testing.T) {
	g := golden(t)
	w, e := BridgeToWorker(g["bridge"], "Bridge PROMPT.md for os-w8-fixture a1 (synthetic).", 10000)
	if e != nil {
		t.Fatal(e)
	}
	var want string
	_ = json.Unmarshal(g["worker_bytes"], &want)
	if string(w) != want {
		t.Fatalf("worker differs from model\n%s", w)
	}
	b, e := WorkerToBridge(w)
	if e != nil {
		t.Fatal(e)
	}
	_ = json.Unmarshal(g["bridge_bytes"], &want)
	if string(b) != want {
		t.Fatal("bridge round trip changed bytes")
	}
	// The nine shared IDs must either refuse drift or be rebound in result encoding.
	for _, key := range SharedIDs() {
		t.Run(key, func(t *testing.T) {
			d := doc(t, w)
			bridge := d["bridge"].(map[string]any)
			if key == "base_seq" {
				bridge[key] = json.Number("2")
			} else {
				bridge[key] = "other-value"
			}
			mut := wire(t, d)
			env, err := EncodeBridgeResult(mut, g["receipt"], "PROMPT-ID synthetic", false)
			if err == nil {
				if !equal(env.Result[key], bridge[key]) {
					t.Fatal("identifier neither rejected nor rebound")
				}
			}
		})
	}
}
func TestGoldenReceiptEncodingAndOutboxRefusal(t *testing.T) {
	g := golden(t)
	env, e := EncodeBridgeResult(g["worker"], g["receipt"], "PROMPT-ID synthetic", false)
	if e != nil {
		t.Fatal(e)
	}
	var want string
	_ = json.Unmarshal(g["envelope_bytes"], &want)
	if string(wire(t, env)) != want {
		t.Fatalf("result differs from Python model: %s", wire(t, env))
	}
	_, e = OutboxBytes(env)
	rejects(t, e, 5)
	_, e = EncodeBridgeResult(g["worker"], g["receipt"], "PROMPT-ID synthetic", true)
	rejects(t, e, 4)
	// The frozen schema does not yet admit os-capsule. No surface relabelling is allowed.
	d := doc(t, g["worker"])
	d["bridge"].(map[string]any)["surface"] = "os-capsule"
	_, e = EncodeBridgeResult(wire(t, d), g["receipt"], "PROMPT-ID synthetic", true)
	rejects(t, e, 4)
	_, e = OutboxBytes(Envelope{Mode: "live", Result: Document{"surface": "os-capsule"}})
	rejects(t, e, 4)
}
func TestFailedReceiptIsBlocked(t *testing.T) {
	g := golden(t)
	r := doc(t, g["receipt"])
	r["exit_code"] = json.Number("5")
	r["failure_reason"] = "gate"
	r["output"] = nil
	gate := r["gate"].(map[string]any)
	gate["raw_exit_code"] = json.Number("1")
	gate["exit_code"] = json.Number("5")
	env, e := EncodeBridgeResult(g["worker"], wire(t, r), "PROMPT-ID synthetic", false)
	if e != nil {
		t.Fatal(e)
	}
	if !strings.HasPrefix(env.Result["source_binding"].(map[string]any)["evidence"].(string), "BLOCKED:") {
		t.Fatal("missing blocker")
	}
	for _, a := range env.Result["evidence_answers"].([]any) {
		if a.(map[string]any)["answer"] != "ABSENT" {
			t.Fatal("non-ABSENT failure")
		}
	}
	if _, ok := env.Result["branch"]; ok {
		t.Fatal("failed result carries branch")
	}
}
func TestCodecMustFail(t *testing.T) {
	g := golden(t)
	for _, mutation := range []struct {
		key   string
		value any
	}{{"acceptance_gates", []any{"go test\necho unsafe"}}, {"observed_source_commit", nil}, {"observed_source_commit", strings.Repeat("a", 64)}, {"unknown", true}} {
		b := doc(t, g["bridge"])
		b[mutation.key] = mutation.value
		_, e := BridgeToWorker(wire(t, b), "objective", 10000)
		rejects(t, e, 5)
	}
	for _, key := range []string{"task_id", "attempt_id", "lease_id"} {
		w := doc(t, g["worker"])
		w[key] = "other"
		_, e := WorkerToBridge(wire(t, w))
		rejects(t, e, 5)
	}
	r := doc(t, g["receipt"])
	r["lease_id"] = "stale"
	_, e := EncodeBridgeResult(g["worker"], wire(t, r), "PROMPT-ID synthetic", false)
	rejects(t, e, 5)
	r = doc(t, g["receipt"])
	files := r["output"].(map[string]any)["files"].([]any)
	files[0].(map[string]any)["path"] = "outside.go"
	_, e = EncodeBridgeResult(g["worker"], wire(t, r), "PROMPT-ID synthetic", false)
	rejects(t, e, 5)
}
func TestCanonPairedVerdicts(t *testing.T) {
	var cases []struct {
		CheckInput
		Name, Status string
		RejectClass  *string `json:"reject_class"`
	}
	fixture(t, "canon_cases.json", &cases)
	if len(cases) < 20 {
		t.Fatal("paired corpus too small")
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			v := CheckResult(c.CheckInput)
			want := ""
			if c.RejectClass != nil {
				want = *c.RejectClass
			}
			if v.Status != c.Status || v.Class != want {
				t.Fatalf("Go %s/%s != canon %s/%s", v.Status, v.Class, c.Status, want)
			}
		})
	}
}
func TestLaunchSpecModelParity(t *testing.T) {
	var cases []struct {
		Name, Backend string
		Interactive   []int `json:"interactive_cpus"`
		Spec          json.RawMessage
		RC            int
	}
	fixture(t, "launch_cases.json", &cases)
	if len(cases) < 20 {
		t.Fatal("missing launch negatives")
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			before := bytes.Clone(c.Spec)
			e := ValidateLaunchSpec(c.Spec, c.Backend, c.Interactive)
			if ExitCode(e) != c.RC {
				t.Fatalf("rc %d != model %d: %v", ExitCode(e), c.RC, e)
			}
			if !bytes.Equal(before, c.Spec) {
				t.Fatal("validator mutated input")
			}
		})
	}
}
func TestEmbeddedSchemaPin(t *testing.T) {
	b, e := os.ReadFile("../../contracts/worker/os.cu.worker-task.v1.schema.json")
	if e != nil {
		t.Fatal(e)
	}
	if !bytes.Equal(b, schemaBytes) {
		t.Fatal("embedded contract drift")
	}
}

func TestFullCosignedSchemaModelCorpus(t *testing.T) {
	var cases []struct {
		Definition string
		Instance   json.RawMessage
		Valid      bool
	}
	fixture(t, "schema_cases.json", &cases)
	if len(cases) < 100 {
		t.Fatal("co-signed model capture unexpectedly small")
	}
	for i, c := range cases {
		v, e := Decode(c.Instance)
		if e != nil {
			t.Fatal(e)
		}
		e = validate(v, c.Definition)
		if (e == nil) != c.Valid {
			t.Errorf("case %d definition %s Go err=%v model valid=%v", i, c.Definition, e, c.Valid)
		}
	}
}
func TestCanonicalRejectsFloatValues(t *testing.T) {
	for _, v := range []any{1.0, Document{"x": 1.0}, []any{1.0}} {
		if _, e := Canonical(v); e == nil {
			t.Fatal("float accepted")
		}
	}
}

func TestNilJSONContainersRemainNull(t *testing.T) {
	for _, v := range []any{Document(nil), []any(nil)} {
		b, e := Canonical(v)
		if e != nil || string(b) != "null" {
			t.Fatalf("nil container became %s (%v)", b, e)
		}
	}
	for _, v := range []any{Document{}, []any{}} {
		b, e := Canonical(v)
		if e != nil || string(b) == "null" {
			t.Fatalf("explicit empty container became null: %v", e)
		}
	}
}

func TestExitCodesFailClosed(t *testing.T) {
	var absent *Reject
	for _, tc := range []struct {
		err  error
		want int
	}{
		{nil, 0}, {errors.New("generic"), 5}, {&Reject{}, 5}, {&Reject{Code: 1}, 5},
		{&Reject{Code: 4}, 4}, {&Reject{Code: 6}, 6}, {&Reject{Code: 99}, 5}, {absent, 5},
	} {
		if got := ExitCode(tc.err); got != tc.want {
			t.Fatalf("normalized rc %d != %d", got, tc.want)
		}
	}
}
