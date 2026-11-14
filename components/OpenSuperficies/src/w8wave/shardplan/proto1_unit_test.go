package shardplan

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"opensuperficies.local/srf/src/w8wave/musesource"
)

// Goldens are generated with the standard encoder and SHA256, never by the
// shardplan SealManifest/VerifyManifest implementation under test.
func testHash(data []byte) string { s := sha256.Sum256(data); return hex.EncodeToString(s[:]) }
func testJSON(t *testing.T, v any) []byte {
	t.Helper()
	b, e := json.Marshal(v)
	if e != nil {
		t.Fatal(e)
	}
	return b
}
func testSeal(t *testing.T, root map[string]any) []byte {
	t.Helper()
	for _, v := range root["shards"].([]any) {
		shard := v.(map[string]any)
		seals := map[string]string{}
		for k, x := range shard {
			if k != "field_sha256" {
				seals[k] = testHash(testJSON(t, x))
			}
		}
		shard["field_sha256"] = seals
	}
	delete(root, "manifest_sha256")
	root["manifest_sha256"] = testHash(testJSON(t, root))
	return testJSON(t, root)
}
func testClone(t *testing.T, v map[string]any) map[string]any {
	t.Helper()
	var out map[string]any
	if e := json.Unmarshal(testJSON(t, v), &out); e != nil {
		t.Fatal(e)
	}
	return out
}
func putFixture(t *testing.T, file string, data []byte) {
	t.Helper()
	if e := os.MkdirAll(filepath.Dir(file), 0700); e != nil {
		t.Fatal(e)
	}
	if e := os.WriteFile(file, data, 0600); e != nil {
		t.Fatal(e)
	}
}

type trustedSpy struct {
	calls        atomic.Int64
	scratchOK    bool
	capabilityOK bool
	outputOK     bool
}

func (w *trustedSpy) HasCapability(cap string) bool {
	w.calls.Add(1)
	return w.capabilityOK && cap == "capsule-go"
}
func (w *trustedSpy) HasExclusiveResource(r string) bool { w.calls.Add(1); return r == "cpu" }
func (w *trustedSpy) ExecutableScratch(path string) bool {
	w.calls.Add(1)
	return w.scratchOK && path == "/workspace/.gotmp"
}
func (w *trustedSpy) SafeOutput(path string) bool {
	w.calls.Add(1)
	return w.outputOK && filepath.IsAbs(path)
}

// Real on-disk registration, expected and authenticated lease grant records
// exercise the actual musesource.Build checkRegistration path. Surface is
// chatgpt-mcp because bridge_task_v1 currently rejects os-capsule (reported as
// PRODUCTION_DEFECT); this positive cannot certify *live* w8muse admission.
type shardFixture struct {
	root         map[string]any
	cfg          Config
	ledgerPath   string
	registerPath []string
	spy          *trustedSpy
}

func newFixture(t *testing.T) shardFixture {
	t.Helper()
	rootDir := t.TempDir()
	commit := strings.Repeat("a", 40)
	gate := []string{"env", "GOTMPDIR=/workspace/.gotmp", "go", "test", "./src/a", "./src/b"}
	mkModule := func(name string, cases []string, output string) map[string]any {
		return map[string]any{
			"name": name, "resource_class": "go", "case_ids": cases,
			"required_capabilities": []string{"capsule-go"},
			"exclusive_resources":   []string{"cpu"},
			"gate_argv":             gate, "expected_outputs": []string{output},
		}
	}
	suite := testJSON(t, map[string]any{"modules": []any{
		mkModule("src/a", []string{"case-a", "case-b"}, "out/a.json"),
		mkModule("src/b", []string{"case-c"}, "out/b.json"),
	}})
	ledger := testJSON(t, map[string]any{"cases": []any{
		map[string]any{"case_id": "case-a", "duration_ns": int64(2)},
		map[string]any{"case_id": "case-b", "duration_ns": int64(3)},
		map[string]any{"case_id": "case-c", "duration_ns": int64(11)},
	}})
	shard := func(index int, mod string, cases []string, duration int64, output string) map[string]any {
		return map[string]any{
			"shard_id": fmt.Sprintf("shard-%d", index), "index": int64(index),
			"resource_class": "go", "modules": []string{mod}, "case_ids": cases,
			"estimated_duration_ns": duration, "required_capabilities": []string{"capsule-go"},
			"exclusive_resources": []string{"cpu"}, "gate_argv": gate,
			"expected_outputs": []string{output},
		}
	}
	manifest := map[string]any{
		"schema": Schema, "repo": "oguirado/OpenSuperficies", "source_commit": commit,
		"suite_sha256": testHash(suite), "duration_ledger_sha256": testHash(ledger),
		"planner_version": "planner-v1", "seed": int64(23), "requested_shards": int64(2),
		"selected_case_ids": []string{"case-a", "case-b", "case-c"},
		"capabilities":      []string{"capsule-go"},
		"shards":            []any{shard(0, "src/a", []string{"case-a", "case-b"}, 5, "out/a.json"), shard(1, "src/b", []string{"case-c"}, 11, "out/b.json")},
	}
	testSeal(t, manifest)
	root := filepath.Join(rootDir, "bridge")
	cfg := Config{
		SuiteJSON: suite, DurationLedgerJSON: ledger,
		Source:  musSource(root, commit),
		Witness: &trustedSpy{scratchOK: true, capabilityOK: true, outputOK: true},
	}
	spy := cfg.Witness.(*trustedSpy)
	var ledgerEvents []any
	registerPaths := make([]string, 0, 2)
	for i, output := range []string{"out/a.json", "out/b.json"} {
		taskID := fmt.Sprintf("job-%d", i)
		briefPath := filepath.Join(rootDir, fmt.Sprintf("brief-%d.md", i))
		brief := []byte(fmt.Sprintf("Frozen shard %d: audit only.\n", i))
		putFixture(t, briefPath, brief)
		reg := Registration{
			ShardID: fmt.Sprintf("shard-%d", i), TaskID: taskID, BriefPath: briefPath,
			BriefSHA256: testHash(brief), OutputPath: filepath.Join(rootDir, fmt.Sprintf("out-%d.json", i)),
			ExpectedOutputs: []string{output},
		}
		cfg.Registrations = append(cfg.Registrations, reg)
		task := map[string]any{
			"schema_version": "bridge-task-v1", "task_id": taskID,
			"thread": fmt.Sprintf("f-%s", taskID), "agent": "chatgpt",
			"worker_id": fmt.Sprintf("worker-%d", i), "attempt_id": "a1",
			"surface": "chatgpt-mcp", "role": "builder", "repo": "oguirado/OpenSuperficies",
			"requested_ref": "bridge/integration", "observed_source_commit": commit,
			"lease_id": fmt.Sprintf("lease-%s", taskID), "key": taskID + ".a1.chatgpt",
			"base_seq": int64(0), "expires_at_utc": "2099-01-01T00:00:00Z",
			"allowed_write_paths": []string{output}, "writer_branch": "bridge/" + taskID + "/a1",
			"integration_target": "bridge/integration",
			"result_path":        output, "report_path": output,
			"acceptance_gates": []string{"go test ./..."}, "evidence_requests": []string{"fixture"},
			"source_files": []string{"src/a"}, "done_signal": "fixture-ready",
		}
		taskPath := filepath.Join(root, "tasks", taskID, "a1", "task.json")
		putFixture(t, taskPath, testJSON(t, task))
		registerPaths = append(registerPaths, taskPath)
		putFixture(t, filepath.Join(root, "expected", taskID, "a1.json"), []byte(`{"nonce":"fixture"}`))
		event := map[string]any{
			"seq": int64(i + 1), "event": "lease_granted", "task_sha256": testHash(testJSON(t, task)),
		}
		for _, k := range []string{"task_id", "attempt_id", "lease_id", "thread", "agent", "worker_id", "key", "base_seq", "surface", "role", "allowed_write_paths", "expires_at_utc"} {
			event[k] = task[k]
		}
		ledgerEvents = append(ledgerEvents, event)
	}
	var ledgerLines bytes.Buffer
	for _, event := range ledgerEvents {
		ledgerLines.Write(testJSON(t, event))
		ledgerLines.WriteByte('\n')
	}
	ledgerPath := filepath.Join(root, "ledger.jsonl")
	putFixture(t, ledgerPath, ledgerLines.Bytes())
	return shardFixture{root: manifest, cfg: cfg, ledgerPath: ledgerPath, registerPath: registerPaths, spy: spy}
}

// Return the source config through a helper with the exact producer field names.
func musSource(root, commit string) musesource.Config {
	return musesource.Config{
		BridgeDir: root, Repository: "oguirado/OpenSuperficies", BaseCommit: commit, Attempt: "a1",
		GateArgv: [][]string{{"go", "test", "./..."}},
		ResourcePolicy: map[string]any{"inference": "host", "max_cpu_ms": int64(10000),
			"max_memory_bytes": int64(64 << 20), "max_wall_ms": int64(20000)},
		Obligations: []string{"frozen input", "exact registered output"},
		MaxInFlight: 2, InFlight: 0, WaveOpen: false, SeatReady: true, GuestInference: false,
	}
}

func TestProto1Shardplan(t *testing.T) {
	// Gate names are stable, anchored and nonempty. The green control runs
	// before every mutant. No mutant kill is claimed without a green control.
	t.Run("green-unequal-duration-independently-sealed", func(t *testing.T) {
		f := newFixture(t)
		manifest := testSeal(t, f.root)
		wantLedger, _ := os.ReadFile(f.ledgerPath)
		first, err := Compile(manifest, f.cfg)
		if err != nil {
			t.Fatalf("green registered source failure: %v (rc=%d)", err, ExitCode(err))
		}
		second, err := Compile(manifest, f.cfg)
		if err != nil {
			t.Fatalf("deterministic rebuild: %v", err)
		}
		if len(first.Items) != 2 || first.EmittedCount != 2 || first.RequestedShards != 2 || first.MaxConcurrentSeats != 2 || len(first.PendingShardIDs) != 0 {
			t.Fatalf("unexpected accounting: %+v", first)
		}
		if !bytes.Equal(first.Manifest, second.Manifest) {
			t.Fatal("nonidentical manifest bytes")
		}
		for i := range first.Items {
			if !bytes.Equal(first.Items[i].Queue, second.Items[i].Queue) || !bytes.Equal(first.Items[i].SourceItems, second.Items[i].SourceItems) || !bytes.Equal(first.Items[i].SourceManifest, second.Items[i].SourceManifest) {
				t.Fatalf("nonidentical shard %d", i)
			}
			var wire []struct {
				TaskID    string `json:"task_id"`
				Attempt   string `json:"attempt"`
				Objective string `json:"objective"`
				PatchPath string `json:"patch_path,omitempty"`
			}
			if e := json.Unmarshal(first.Items[i].SourceItems, &wire); e != nil || len(wire) != 1 || wire[0].TaskID != fmt.Sprintf("job-%d", i) || wire[0].Attempt != "a1" || wire[0].Objective == "" {
				t.Fatalf("Muse item %d malformed: %v / %+v", i, e, wire)
			}
			inv := first.Items[i].Invocation
			if !reflect.DeepEqual(inv.Prepare, [][]string{{"mkdir", "-p", "/workspace/.gotmp"}}) || !reflect.DeepEqual(inv.Env, []string{"GOTMPDIR=/workspace/.gotmp"}) || inv.NativeExitStatus != nil {
				t.Fatalf("capsule prerequisites %d: %+v", i, inv)
			}
		}
		var exported map[string]any
		if err := json.Unmarshal(first.Manifest, &exported); err != nil {
			t.Fatal(err)
		}
		if got := exported["requested_shards"]; got != float64(2) {
			t.Fatalf("requested count: %v", got)
		}
		after, _ := os.ReadFile(f.ledgerPath)
		if !bytes.Equal(wantLedger, after) {
			t.Fatal("source illegally changed lease ledger")
		}
		for _, reg := range f.cfg.Registrations {
			if _, e := os.Lstat(reg.OutputPath); !os.IsNotExist(e) {
				t.Fatalf("unexpected output written: %s: %v", reg.OutputPath, e)
			}
		}
		t.Log("green controls: 2 registered source items; source and duration SHA256 independent; exact union case-a,b,c=5+11 ns; byte-identical rebuild; no output/ledger mutation")
	})
	t.Run("credit-prefix-and-held-later-waves", func(t *testing.T) {
		f := newFixture(t)
		manifest := testSeal(t, f.root)
		f.cfg.Source.InFlight = 1
		result, e := Compile(manifest, f.cfg)
		if e != nil || len(result.Items) != 1 || !reflect.DeepEqual(result.PendingShardIDs, []string{"shard-1"}) || result.MaxConcurrentSeats != 2 {
			t.Fatalf("credit accounting %+v, %v", result, e)
		}
		f.cfg.Source.WaveOpen = true
		empty, e := Compile(manifest, f.cfg)
		if ExitCode(e) != 6 || !emptyResult(empty) {
			t.Fatalf("open-wave refusal %+v, %v", empty, e)
		}
		t.Log("one seat credit -> one emitted item, one held; open wave -> EN_DUDA")
	})
	mutants := []struct {
		name        string
		mutate      func(*testing.T, *shardFixture)
		wantCode    int
		wantDetail  string
		zeroWitness bool
	}{
		{"duplicate-case", func(t *testing.T, f *shardFixture) {
			f.root["shards"].([]any)[1].(map[string]any)["case_ids"] = []string{"case-a"}
		}, 2, "shard membership, duration, capability or fixed argv mismatch", true},
		{"empty-shard", func(t *testing.T, f *shardFixture) {
			s := f.root["shards"].([]any)[1].(map[string]any)
			s["modules"] = []string{}
		}, 2, "empty or malformed shard", true},
		{"absent-duration", func(t *testing.T, f *shardFixture) {
			f.cfg.DurationLedgerJSON = testJSON(t, map[string]any{"cases": []any{map[string]any{"case_id": "case-a", "duration_ns": int64(2)}, map[string]any{"case_id": "case-b", "duration_ns": int64(3)}}})
			f.root["duration_ledger_sha256"] = testHash(f.cfg.DurationLedgerJSON)
		}, 2, "missing measured duration: case-c", true},
		{"forged-source-seal", func(t *testing.T, f *shardFixture) { f.root["suite_sha256"] = strings.Repeat("f", 64) }, 5, "frozen source or duration seal mismatch", true},
		{"unknown-capability", func(t *testing.T, f *shardFixture) {
			f.root["shards"].([]any)[1].(map[string]any)["required_capabilities"] = []string{"unknown-cap"}
			f.root["capabilities"] = []string{"capsule-go", "unknown-cap"}
		}, 2, "shard membership, duration, capability or fixed argv mismatch", true},
		{"impossible-count", func(t *testing.T, f *shardFixture) { f.root["requested_shards"] = int64(3) }, 2, "manifest identity, pin or count", true},
		{"changed-registration", func(t *testing.T, f *shardFixture) {
			var task map[string]any
			b, _ := os.ReadFile(f.registerPath[1])
			if e := json.Unmarshal(b, &task); e != nil {
				t.Fatal(e)
			}
			task["allowed_write_paths"] = []string{"out/tampered.json"}
			putFixture(t, f.registerPath[1], testJSON(t, task))
		}, 4, "registered individual write paths differ", false},
		{"directory-output", func(t *testing.T, f *shardFixture) {
			if e := os.Mkdir(f.cfg.Registrations[1].OutputPath, 0700); e != nil {
				t.Fatal(e)
			}
		}, 2, "directory, symlink or invalid output target", true},
		{"nonexecutable-scratch-evidence", func(t *testing.T, f *shardFixture) { f.spy.scratchOK = false }, 4, "executable scratch proof unavailable: /workspace/.gotmp", false},
	}
	for _, mutant := range mutants {
		t.Run("must-FAIL-"+mutant.name, func(t *testing.T) {
			f := newFixture(t)
			mutant.mutate(t, &f)
			before, _ := os.ReadFile(f.ledgerPath)
			raw := testSeal(t, f.root)
			result, err := Compile(raw, f.cfg)
			if ExitCode(err) != mutant.wantCode || err == nil || !strings.Contains(err.Error(), mutant.wantDetail) || !emptyResult(result) {
				t.Fatalf("mutant survived: rc=%d err=%v output=%+v", ExitCode(err), err, result)
			}
			after, _ := os.ReadFile(f.ledgerPath)
			if !bytes.Equal(before, after) {
				t.Fatal("mutant mutated lease ledger")
			}
			if mutant.zeroWitness && f.spy.calls.Load() != 0 {
				t.Fatalf("mutant triggered witness before validation: %d", f.spy.calls.Load())
			}
			for _, reg := range f.cfg.Registrations {
				if b, e := os.ReadFile(reg.OutputPath); e == nil && len(b) > 0 {
					t.Fatalf("mutant produced bytes at %s", reg.OutputPath)
				}
			}
			t.Logf("killed %s: rc=%d, no compiled source items/queue, no send or lease mutation", mutant.name, mutant.wantCode)
		})
	}
	t.Run("strict-json-must-fail", func(t *testing.T) {
		f := newFixture(t)
		base := testSeal(t, f.root)
		cases := map[string][]byte{
			"duplicate-root-key": append(append([]byte{}, base[:len(base)-1]...), []byte(`,"schema":"proto1.suite-manifest/v1"}`)...),
			"unknown-field":      append(append([]byte{}, base[:len(base)-1]...), []byte(`,"arbitrary":1}`)...),
			"float-seed":         bytes.Replace(base, []byte(`"seed":23`), []byte(`"seed":23.0`), 1),
			"bool-seed":          bytes.Replace(base, []byte(`"seed":23`), []byte(`"seed":true`), 1),
			"path-traversal":     bytes.Replace(base, []byte(`out/a.json`), []byte(`../out/a.json`), 1),
		}
		for name, raw := range cases {
			if bytes.Equal(raw, base) {
				t.Fatalf("mutant %s did not change input", name)
			}
			result, err := Compile(raw, f.cfg)
			if ExitCode(err) != 2 || !emptyResult(result) {
				t.Errorf("%s accepted: %v %+v", name, err, result)
			}
		}
		if n := f.spy.calls.Load(); n != 0 {
			t.Fatalf("strict invalid input reached host witness: %d", n)
		}
		t.Logf("strict controls: %d of %d malformed/uncanonical records rejected", len(cases), len(cases))
	})
	t.Run("fixed-seed-barrier-concurrency", func(t *testing.T) {
		f := newFixture(t)
		manifest := testSeal(t, f.root)
		const workers = 8
		start := make(chan struct{})
		results := make([]Result, workers)
		errs := make([]error, workers)
		var wg sync.WaitGroup
		for i := 0; i < workers; i++ {
			wg.Add(1)
			go func(k int) { defer wg.Done(); <-start; results[k], errs[k] = Compile(manifest, f.cfg) }(i)
		}
		close(start)
		wg.Wait()
		for i := 0; i < workers; i++ {
			if errs[i] != nil || len(results[i].Items) != 2 {
				t.Fatalf("worker %d failed: %v", i, errs[i])
			}
			if !bytes.Equal(results[0].Manifest, results[i].Manifest) || !bytes.Equal(results[0].Items[0].SourceItems, results[i].Items[0].SourceItems) {
				t.Fatalf("worker %d non-deterministic", i)
			}
		}
		t.Logf("fixed seed=23; barrier synchronized %d builds; all identical; host test is not a live seat certification", workers)
	})
}

func emptyResult(r Result) bool {
	return len(r.Manifest) == 0 && len(r.Items) == 0 && len(r.PendingShardIDs) == 0 && r.EmittedCount == 0
}
