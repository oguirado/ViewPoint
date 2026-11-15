package musesource

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"opensuperficies.local/srf/src/workerbridge"
)

type protoFixture struct {
	root, bridge string
	cfg          Config
	queue        []byte
	jobs         []string
	task         map[string]map[string]any
	briefs       map[string][]byte
}

func protoWrite(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
}

func protoJSON(t *testing.T, value any) []byte {
	t.Helper()
	b, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func protoMakeFixture(t *testing.T, size int) *protoFixture {
	t.Helper()
	f := &protoFixture{root: t.TempDir(), task: map[string]map[string]any{}, briefs: map[string][]byte{}}
	f.bridge = filepath.Join(f.root, ".wavemind", "bridge")
	f.cfg = Config{
		BridgeDir:  f.bridge,
		Repository: "oguirado/OpenSuperficies",
		BaseCommit: strings.Repeat("a", 40),
		Attempt:    "a1",
		GateArgv:   [][]string{{"go", "test", "./..."}},
		ResourcePolicy: map[string]any{
			"inference": "host", "max_cpu_ms": int64(1000),
			"max_memory_bytes": int64(67108864), "max_wall_ms": int64(1200),
		},
		Obligations: []string{"registered-grants-only"},
		MaxInFlight: 4, SeatReady: true,
	}
	if err := os.MkdirAll(filepath.Join(f.root, "src", "work"), 0700); err != nil {
		t.Fatal(err)
	}
	// Fixed-seed permutation makes registered order deliberate, not map order.
	rng := rand.New(rand.NewSource(71531))
	var queue strings.Builder
	ledger := make([]byte, 0)
	for i := 0; i < size; i++ {
		id := fmt.Sprintf("job-%d", i)
		f.jobs = append(f.jobs, id)
		objective := []byte(fmt.Sprintf("Inspect café and Ω in fixture %d.\n", i))
		brief := filepath.Join(f.root, "brief", id+".md")
		protoWrite(t, brief, objective)
		f.briefs[id] = objective
		output := filepath.Join(f.root, "output", id+".json")
		fmt.Fprintf(&queue, "%s\t%s\t%s\n", id, brief, output)
		grants := []string{fmt.Sprintf("src/work/%s.go", id), fmt.Sprintf("docs/work/%s.md", id)}
		if rng.Intn(2) == 0 {
			grants[0], grants[1] = grants[1], grants[0]
		}
		expiry := time.Now().UTC().Add(24 * time.Hour).Truncate(time.Second).Format(time.RFC3339)
		task := map[string]any{
			"schema_version": "bridge-task-v1", "task_id": id,
			"thread": "thread-" + id, "agent": "chatgpt", "worker_id": "chatgpt-f", "attempt_id": "a1",
			"surface": "chatgpt-mcp", "role": "builder", "repo": f.cfg.Repository,
			"requested_ref": "bridge/integration", "observed_source_commit": f.cfg.BaseCommit,
			"lease_id": "lease-" + id, "key": id + ".a1.chatgpt-f", "base_seq": 0,
			"expires_at_utc": expiry, "allowed_write_paths": grants, "writer_branch": "bridge/" + id + "/a1",
			"integration_target": "bridge/integration",
			"result_path":        ".wavemind/bridge/outbox/chatgpt/" + id + "/a1/result.json",
			"report_path":        ".wavemind/bridge/outbox/chatgpt/" + id + "/a1/report.md",
			"acceptance_gates":   []string{"go test ./..."},
			"evidence_requests":  []string{"sha256:src/file.go"},
			"source_files":       []string{"src/workerbridge/json.go"},
			"done_signal":        "READY_FOR_COLLECTION " + id + " a1",
		}
		f.task[id] = task
		protoWrite(t, filepath.Join(f.bridge, "tasks", id, "a1", "task.json"), protoJSON(t, task))
		protoWrite(t, filepath.Join(f.bridge, "expected", id, "a1.json"), []byte("{}"))
		// Registry fixture uses the real bridge canonical codec, not an
		// allow-all registration function or fake ledger identity.
		canonical, err := workerbridge.BridgeCanonical(task)
		if err != nil {
			t.Fatal(err)
		}
		taskHash := sha256.Sum256(canonical)
		event := map[string]any{
			"seq": i + 1, "event": "lease_granted",
			"lease_id": task["lease_id"], "task_sha256": hex.EncodeToString(taskHash[:]),
		}
		for _, key := range []string{"task_id", "attempt_id", "lease_id", "thread", "agent", "worker_id", "key", "base_seq", "surface", "role", "allowed_write_paths", "expires_at_utc"} {
			event[key] = task[key]
		}
		ledger = append(ledger, protoJSON(t, event)...)
		ledger = append(ledger, '\n')
	}
	f.queue = []byte(queue.String())
	protoWrite(t, filepath.Join(f.bridge, "ledger.jsonl"), ledger)
	return f
}

func (f *protoFixture) mutateTask(t *testing.T, id string, key string, value any) {
	t.Helper()
	f.task[id][key] = value
	protoWrite(t, filepath.Join(f.bridge, "tasks", id, "a1", "task.json"), protoJSON(t, f.task[id]))
}

// A read-only sink spy watches all fixture paths, including output directories.
// Production BuildExact has no publisher/sink injection surface (AUSENTE).
type protoSinkSpy struct{ content map[string][sha256.Size]byte }

func protoWatch(t *testing.T, root string) protoSinkSpy {
	t.Helper()
	s := protoSinkSpy{content: map[string][sha256.Size]byte{}}
	err := filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.Mode().IsRegular() {
			b, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			s.content[p] = sha256.Sum256(b)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return s
}
func (s protoSinkSpy) assertNoPersistence(t *testing.T, root string) {
	t.Helper()
	now := protoWatch(t, root)
	if !reflect.DeepEqual(s.content, now.content) {
		t.Fatalf("read-only source changed fixture files: prior=%d after=%d", len(s.content), len(now.content))
	}
}

func protoMustReject(t *testing.T, f *protoFixture, code int, prefix string) {
	t.Helper()
	spy := protoWatch(t, f.root)
	a, b, err := BuildExact(f.queue, f.cfg)
	var rejected *workerbridge.Reject
	if !errors.As(err, &rejected) || rejected.Code != code || ExitCode(err) != code || a != nil || b != nil {
		t.Fatalf("expected refusal %d with nil outputs; wire=%q sidecar=%q err=%v", code, a, b, err)
	}
	if code == 2 && rejected.Class != "input" {
		t.Fatalf("bad class %q", rejected.Class)
	}
	if !strings.HasPrefix(rejected.Detail, prefix) {
		t.Fatalf("expected detail prefix %q, got %q", prefix, rejected.Detail)
	}
	spy.assertNoPersistence(t, f.root)
	t.Logf("REFUSAL expected=%d actual=%d detail=%q output_bytes=0 source_mutations=0", code, rejected.Code, rejected.Detail)
}

// Independent JSON canonicalizer: sorts keys, keeps UTF-8 and integer tokens,
// never consults BuildExact's hash/encoder. The fixture has no C0 controls.
func protoOracleCanonical(t *testing.T, obj any) []byte {
	t.Helper()
	var out bytes.Buffer
	var encode func(any)
	encode = func(v any) {
		switch x := v.(type) {
		case map[string]any:
			out.WriteByte('{')
			keys := make([]string, 0, len(x))
			for k := range x {
				keys = append(keys, k)
			}
			sort.Strings(keys)
			for i, k := range keys {
				if i > 0 {
					out.WriteByte(',')
				}
				encode(k)
				out.WriteByte(':')
				encode(x[k])
			}
			out.WriteByte('}')
		case []any:
			out.WriteByte('[')
			for i, z := range x {
				if i > 0 {
					out.WriteByte(',')
				}
				encode(z)
			}
			out.WriteByte(']')
		case string:
			var q bytes.Buffer
			encoder := json.NewEncoder(&q)
			encoder.SetEscapeHTML(false)
			if err := encoder.Encode(x); err != nil {
				t.Fatal(err)
			}
			out.Write(bytes.TrimSuffix(q.Bytes(), []byte{'\n'}))
		case json.Number:
			out.WriteString(string(x))
		case int:
			out.WriteString(strconv.Itoa(x))
		case nil:
			out.WriteString("null")
		case bool:
			if x {
				out.WriteString("true")
			} else {
				out.WriteString("false")
			}
		default:
			raw := protoJSON(t, x)
			var typed any
			dec := json.NewDecoder(bytes.NewReader(raw))
			dec.UseNumber()
			if err := dec.Decode(&typed); err != nil {
				t.Fatal(err)
			}
			encode(typed)
		}
	}
	encode(obj)
	return out.Bytes()
}

func TestProto1Exactpaths(t *testing.T) {
	// Green controls run before every attack; they bind real lease records.
	t.Run("positive-real-registration-canonical-sidecar-and-strict-wire", func(t *testing.T) {
		f := protoMakeFixture(t, 3)
		f.cfg.InFlight = 1 // exactly one seat, two still-pending jobs
		spy := protoWatch(t, f.root)
		legacyWire, legacyManifest, err := Build(f.queue, f.cfg)
		if err != nil {
			t.Fatalf("real registration prerequisite: %v", err)
		}
		if ValidateWave(legacyWire) != nil {
			t.Fatal("legacy wave prerequisite failed")
		}
		a, sidecar, err := BuildExact(f.queue, f.cfg)
		if err != nil {
			t.Fatalf("exact source: %v", err)
		}
		again, againSidecar, err := BuildExact(f.queue, f.cfg)
		if err != nil || !bytes.Equal(a, again) || !bytes.Equal(sidecar, againSidecar) {
			t.Fatalf("nondeterministic exact bytes: %v", err)
		}
		spy.assertNoPersistence(t, f.root)
		if err := ValidateWave(a); err != nil {
			t.Fatal(err)
		}
		// w8muse.Item's documented order, independent literal wire fixture.
		expectWire := "[{\"task_id\":\"job-0\",\"attempt\":\"a1\",\"objective\":\"Inspect café and Ω in fixture 0.\\n\"}]"
		if string(a) != expectWire {
			t.Fatalf("wire changed: got %q want %q", a, expectWire)
		}
		dec := json.NewDecoder(bytes.NewReader(a))
		dec.DisallowUnknownFields()
		var strict []struct {
			TaskID    string `json:"task_id"`
			Attempt   string `json:"attempt"`
			Objective string `json:"objective"`
			PatchPath string `json:"patch_path,omitempty"`
		}
		if err := dec.Decode(&strict); err != nil || len(strict) != 1 {
			t.Fatalf("strict decoder: %v", err)
		}
		var side struct {
			Schema          string              `json:"schema"`
			SourceManifest  json.RawMessage     `json:"source_manifest"`
			ExactWritePaths map[string][]string `json:"exact_write_paths"`
			SHA256          string              `json:"sha256"`
		}
		if err := json.Unmarshal(sidecar, &side); err != nil {
			t.Fatal(err)
		}
		if side.Schema != "proto1.source-exact/v1" || len(side.ExactWritePaths) != 1 || !reflect.DeepEqual(side.ExactWritePaths["job-0"], f.task["job-0"]["allowed_write_paths"]) {
			t.Fatalf("sidecar grants not identical to registered grants: %#v", side.ExactWritePaths)
		}
		if !bytes.HasPrefix(sidecar, []byte(`{"schema":"proto1.source-exact/v1","source_manifest":`)) ||
			bytes.Index(sidecar, []byte(`"exact_write_paths":`)) < 0 ||
			bytes.Index(sidecar, []byte(`"sha256":`)) < 0 {
			t.Fatalf("bad outer field emission order: %q", sidecar)
		}
		var frozen map[string]any
		decoder := json.NewDecoder(bytes.NewReader(side.SourceManifest))
		decoder.UseNumber()
		if err := decoder.Decode(&frozen); err != nil {
			t.Fatal(err)
		}
		if fmt.Sprint(frozen["emitted_count"]) != "1" || fmt.Sprint(frozen["pending_count"]) != "2" {
			t.Fatalf("incorrect cap manifest: %v", frozen)
		}
		fw, ok := frozen["frozen"].([]any)
		if !ok || len(fw) != 1 {
			t.Fatalf("expected 1 frozen row, got %v", frozen["frozen"])
		}
		entry := fw[0].(map[string]any)
		briefHash := sha256.Sum256(f.briefs["job-0"])
		if entry["input_sha256"] != hex.EncodeToString(briefHash[:]) ||
			entry["field_sha256"].(map[string]any)["objective"] != hex.EncodeToString(briefHash[:]) {
			t.Fatal("input or objective field hash does not match independent SHA256")
		}
		wireHash := sha256.Sum256(a)
		if frozen["wave_sha256"] != hex.EncodeToString(wireHash[:]) {
			t.Fatal("wrong output wire hash")
		}
		var legacy map[string]any
		d := json.NewDecoder(bytes.NewReader(legacyManifest))
		d.UseNumber()
		if err := d.Decode(&legacy); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(fw[0], legacy["frozen"].([]any)[0]) || frozen["queue_sha256"] != legacy["queue_sha256"] {
			t.Fatal("legacy frozen fields were altered")
		}
		unhashed := map[string]any{"schema": side.Schema, "source_manifest": frozen, "exact_write_paths": side.ExactWritePaths}
		oracleHash := sha256.Sum256(protoOracleCanonical(t, unhashed))
		if side.SHA256 != hex.EncodeToString(oracleHash[:]) {
			t.Fatal("outer canonical sha256 differs from independent oracle")
		}
		t.Logf("GREEN real_registry=3 checked_pending=2 emitted=1 stable=2 strict_decoder=1 field_hashes=2 outer_sha256=%s no_persistence=1", side.SHA256)
	})
	t.Run("positive-two-seat-cap", func(t *testing.T) {
		f := protoMakeFixture(t, 3)
		f.cfg.MaxInFlight = 6
		a, b, err := BuildExact(f.queue, f.cfg)
		if err != nil {
			t.Fatal(err)
		}
		var items []map[string]any
		if err := json.Unmarshal(a, &items); err != nil || len(items) != 2 {
			t.Fatalf("seat cap: %v got=%d", err, len(items))
		}
		if !bytes.Contains(b, []byte(`"pending_count":1`)) {
			t.Fatalf("no pending count: %s", b)
		}
		t.Log("GREEN admitted=2 pending=1 configured_max_in_flight=6")
	})
	t.Run("positive-concurrent-barrier", func(t *testing.T) {
		f := protoMakeFixture(t, 2)
		start := make(chan struct{})
		var wg sync.WaitGroup
		type output struct {
			wire, manifest []byte
			err            error
		}
		results := make([]output, 2)
		for i := 0; i < 2; i++ {
			wg.Add(1)
			go func(j int) {
				defer wg.Done()
				<-start
				results[j].wire, results[j].manifest, results[j].err = BuildExact(f.queue, f.cfg)
			}(i)
		}
		close(start)
		wg.Wait()
		if results[0].err != nil || results[1].err != nil || !bytes.Equal(results[0].wire, results[1].wire) || !bytes.Equal(results[0].manifest, results[1].manifest) {
			t.Fatalf("concurrent unequal: %+v", results)
		}
		t.Log("GREEN barrier_workers=2 deterministic=1 runtime_writes=0")
	})
	cases := []struct {
		name   string
		size   int
		job    int
		grant  []string
		code   int
		prefix string
		setup  func(*testing.T, *protoFixture)
	}{
		{name: "must-fail-trailing-slash", size: 1, job: 0, grant: []string{"src/work/"}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-wildcard", size: 1, job: 0, grant: []string{"src/work/*.go"}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-parent-traversal", size: 1, job: 0, grant: []string{"src/../escape.go"}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-duplicate-file", size: 1, job: 0, grant: []string{"src/work/job-0.go", "src/work/job-0.go"}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-symlink-escape", size: 1, job: 0, grant: []string{"escape/out.go"}, code: 2, prefix: "E_WRITE_PATH:", setup: func(t *testing.T, f *protoFixture) {
			if err := os.Symlink(t.TempDir(), filepath.Join(f.root, "escape")); err != nil {
				t.Fatal(err)
			}
		}},
		{name: "must-fail-pending-invalid-beyond-admission", size: 3, job: 2, grant: []string{"src/work/*.go"}, code: 2, prefix: "E_WRITE_PATH:", setup: func(t *testing.T, f *protoFixture) { f.cfg.InFlight = 1 }},
		{name: "must-fail-directory-broadening", size: 1, job: 0, grant: []string{"src/work"}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-backslash", size: 1, job: 0, grant: []string{`src\work\bad.go`}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-absolute-name", size: 1, job: 0, grant: []string{"/tmp/abs.go"}, code: 2, prefix: "E_WRITE_PATH:"},
		{name: "must-fail-noncanonical-name", size: 1, job: 0, grant: []string{"docs//file.go"}, code: 2, prefix: "E_WRITE_PATH:"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := protoMakeFixture(t, tc.size)
			if tc.setup != nil {
				tc.setup(t, f)
			}
			f.mutateTask(t, f.jobs[tc.job], "allowed_write_paths", tc.grant)
			protoMustReject(t, f, tc.code, tc.prefix)
		})
	}
	t.Run("must-fail-mutated-live-lease-ledger", func(t *testing.T) {
		f := protoMakeFixture(t, 1)
		ledgerPath := filepath.Join(f.bridge, "ledger.jsonl")
		raw, err := os.ReadFile(ledgerPath)
		if err != nil {
			t.Fatal(err)
		}
		var ev map[string]any
		if err := json.Unmarshal(bytes.TrimSpace(raw), &ev); err != nil {
			t.Fatal(err)
		}
		ev["task_sha256"] = strings.Repeat("0", 64)
		protoWrite(t, ledgerPath, append(protoJSON(t, ev), '\n'))
		protoMustReject(t, f, 4, "no active matching builder lease")
	})
	t.Run("must-fail-registration-pin-mutated", func(t *testing.T) {
		f := protoMakeFixture(t, 1)
		f.mutateTask(t, "job-0", "observed_source_commit", strings.Repeat("b", 40))
		protoMustReject(t, f, 4, "registration identity")
	})
	t.Run("must-fail-registered-output-directory", func(t *testing.T) {
		f := protoMakeFixture(t, 1)
		f.mutateTask(t, "job-0", "result_path", "src/work/")
		protoMustReject(t, f, 2, "E_WRITE_PATH:")
	})
	t.Run("must-fail-wave-open-and-full", func(t *testing.T) {
		f := protoMakeFixture(t, 1)
		f.cfg.WaveOpen = true
		protoMustReject(t, f, 6, "EN_DUDA:")
		f.cfg.WaveOpen = false
		f.cfg.InFlight = 4
		protoMustReject(t, f, 6, "EN_DUDA:")
		f.cfg.InFlight = 2
		f.cfg.MaxInFlight = 5
		protoMustReject(t, f, 6, "EN_DUDA:")
	})
}
