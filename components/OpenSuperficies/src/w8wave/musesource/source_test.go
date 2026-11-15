package musesource

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type caseFixture struct {
	Name  string `json:"name"`
	Queue string `json:"queue"`
	Wire  string `json:"wire"`
}
type fixtureSet struct {
	QueueCases []caseFixture `json:"queue_cases"`
	WaveCases  []caseFixture `json:"wave_cases"`
}
type strictItem struct {
	TaskID    string `json:"task_id"`
	Attempt   string `json:"attempt"`
	Objective string `json:"objective"`
	PatchPath string `json:"patch_path,omitempty"`
}

func testConfig(root string) Config {
	return Config{
		BridgeDir: filepath.Join(root, "bridge"),
		Repository: "oguirado/OpenSuperficies",
		BaseCommit: strings.Repeat("a", 40),
		Attempt: "a1",
		GateArgv: [][]string{{"go", "test", "./..."}},
		ResourcePolicy: map[string]any{
			"inference": "host", "max_cpu_ms": 1000,
			"max_memory_bytes": 64 << 20, "max_wall_ms": 1200,
		},
		Obligations: []string{"respect the dispatched write paths", "preserve gate evidence"},
		MaxInFlight: 6, SeatReady: true,
	}
}

func queueFor(briefs []string, dir string) []byte {
	var b strings.Builder
	for i, brief := range briefs {
		fmt.Fprintf(&b, "job-%d\t%s\t%s\n", i, brief, filepath.Join(dir, fmt.Sprintf("out-%d.json", i)))
	}
	return []byte(b.String())
}

func allowed(_ Config, _ string) error { return nil }

func mustWrite(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestPositiveThreeLineQueueStrictDeterministic(t *testing.T) {
	dir := t.TempDir()
	var paths []string
	for i := 0; i < 3; i++ {
		path := filepath.Join(dir, fmt.Sprintf("brief-%d.md", i))
		mustWrite(t, path, []byte(fmt.Sprintf("Objective %d: inspect passive source only.\n", i)))
		paths = append(paths, path)
	}
	queue := queueFor(paths, dir)
	cfg := testConfig(dir)
	a, manifestA, err := build(queue, cfg, allowed)
	if err != nil {
		t.Fatalf("positive wave: %v", err)
	}
	b, manifestB, err := build(queue, cfg, allowed)
	if err != nil || !bytes.Equal(a, b) || !bytes.Equal(manifestA, manifestB) {
		t.Fatalf("frozen bytes nondeterministic: %v", err)
	}
	if err := ValidateWave(a); err != nil {
		t.Fatalf("strict wave validation: %v", err)
	}
	decoder := json.NewDecoder(bytes.NewReader(a))
	decoder.DisallowUnknownFields()
	var items []strictItem
	if err := decoder.Decode(&items); err != nil || len(items) != 3 {
		t.Fatalf("strict decoder: %v, items=%d", err, len(items))
	}
	var manifest struct {
		Frozen []struct {
			InputSHA256 string            `json:"input_sha256"`
			FieldSHA256 map[string]string `json:"field_sha256"`
		} `json:"frozen"`
		PendingCount int `json:"pending_count"`
	}
	if err := json.Unmarshal(manifestA, &manifest); err != nil || len(manifest.Frozen) != 3 || manifest.PendingCount != 0 {
		t.Fatalf("frozen manifest: %v", err)
	}
	for i, item := range manifest.Frozen {
		brief, err := os.ReadFile(paths[i])
		if err != nil {
			t.Fatal(err)
		}
		hash := sha256.Sum256(brief)
		want := hex.EncodeToString(hash[:])
		if item.InputSHA256 != want || item.FieldSHA256["objective"] != want || len(item.FieldSHA256) != 11 {
			t.Errorf("entry %d lacks frozen-field hashes", i)
		}
	}
}

func TestCreditCapNoSecondWave(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "brief.md")
	mustWrite(t, path, []byte("Frozen task objective\n"))
	queue := queueFor([]string{path, path, path}, dir)
	cfg := testConfig(dir)
	cfg.MaxInFlight, cfg.InFlight = 2, 1
	wave, manifest, err := build(queue, cfg, allowed)
	if err != nil {
		t.Fatal(err)
	}
	var items []strictItem
	if err := json.Unmarshal(wave, &items); err != nil || len(items) != 1 {
		t.Fatalf("expected only one free credit: %v", err)
	}
	var record map[string]any
	if err := json.Unmarshal(manifest, &record); err != nil || record["pending_count"] != float64(2) {
		t.Fatalf("pending count: %v", err)
	}
	cfg.WaveOpen = true
	a, b, err := build(queue, cfg, allowed)
	if ExitCode(err) != 6 || a != nil || b != nil {
		t.Fatalf("open wave should be EN_DUDA without output: %v", err)
	}
	cfg.WaveOpen = false
	cfg.SeatReady = false
	a, b, err = build(queue, cfg, allowed)
	if ExitCode(err) != 6 || a != nil || b != nil {
		t.Fatalf("unavailable seat should be EN_DUDA without fallback: %v", err)
	}
}

func TestMustFailFixtures(t *testing.T) {
	dir := t.TempDir()
	brief := filepath.Join(dir, "brief.md")
	output := filepath.Join(dir, "out.json")
	mustWrite(t, brief, []byte("passive brief data\n"))
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "testdata", "w8wave", "musesource", "cases.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixtures fixtureSet
	if err := json.Unmarshal(data, &fixtures); err != nil {
		t.Fatal(err)
	}
	cfg := testConfig(dir)
	for _, fixture := range fixtures.QueueCases {
		t.Run(fixture.Name, func(t *testing.T) {
			queue := strings.ReplaceAll(strings.ReplaceAll(fixture.Queue, "{BRIEF}", brief), "{OUTPUT}", output)
			a, b, err := build([]byte(queue), cfg, allowed)
			if ExitCode(err) != 2 || a != nil || b != nil {
				t.Fatalf("queue should refuse code 2 and return no bytes: %v", err)
			}
		})
	}
	for _, fixture := range fixtures.WaveCases {
		t.Run(fixture.Name, func(t *testing.T) {
			if got := ExitCode(ValidateWave([]byte(fixture.Wire))); got != 2 {
				t.Fatalf("bad output field should give 2, got %d", got)
			}
		})
	}
}

func TestAdditionalMustFailCases(t *testing.T) {
	dir := t.TempDir()
	brief := filepath.Join(dir, "brief.md")
	mustWrite(t, brief, []byte("valid brief\n"))
	cfg := testConfig(dir)
	queue := queueFor([]string{brief}, dir)
	t.Run("unregistered", func(t *testing.T) {
		a, b, err := Build(queue, cfg)
		if ExitCode(err) != 4 || a != nil || b != nil {
			t.Fatalf("missing read-only registration should yield 4: %v", err)
		}
	})
	t.Run("oversized-brief", func(t *testing.T) {
		mustWrite(t, brief, bytes.Repeat([]byte("x"), MaxBriefBytes+1))
		a, b, err := build(queue, cfg, allowed)
		if ExitCode(err) != 2 || a != nil || b != nil {
			t.Fatalf("oversized brief must fail without output: %v", err)
		}
		mustWrite(t, brief, []byte("valid brief\n"))
	})
	t.Run("guest-inference", func(t *testing.T) {
		c := cfg
		c.GuestInference = true
		a, b, err := build(queue, c, allowed)
		if ExitCode(err) != 2 || a != nil || b != nil {
			t.Fatalf("guest inference must fail: %v", err)
		}
	})
	t.Run("guest-policy", func(t *testing.T) {
		c := cfg
		c.ResourcePolicy = map[string]any{
			"inference": "guest", "max_cpu_ms": 1000,
			"max_memory_bytes": 64 << 20, "max_wall_ms": 1200,
		}
		_, _, err := build(queue, c, allowed)
		if ExitCode(err) != 2 {
			t.Fatalf("guest policy must fail: %v", err)
		}
	})
	t.Run("65-items", func(t *testing.T) {
		briefs := make([]string, 65)
		for i := range briefs {
			briefs[i] = brief
		}
		a, b, err := build(queueFor(briefs, dir), cfg, allowed)
		if ExitCode(err) != 2 || a != nil || b != nil {
			t.Fatalf("65 items must fail: %v", err)
		}
	})
	t.Run("output-65", func(t *testing.T) {
		wave := make([]map[string]string, 65)
		for i := range wave {
			wave[i] = map[string]string{
				"task_id": fmt.Sprintf("job-%d", i), "attempt": "a1", "objective": "data",
			}
		}
		raw, _ := json.Marshal(wave)
		if ExitCode(ValidateWave(raw)) != 2 {
			t.Fatal("65 wave items accepted")
		}
	})
}

func TestExitCodeNormalization(t *testing.T) {
	if ExitCode(nil) != 0 || ExitCode(fmt.Errorf("unknown")) != 6 {
		t.Fatal("normalized exit codes must not use 1")
	}
}
