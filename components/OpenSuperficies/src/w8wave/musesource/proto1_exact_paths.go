package musesource

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"opensuperficies.local/srf/src/w8muse"
	"opensuperficies.local/srf/src/workerbridge"
)

// BuildExact is a read-only, fail-closed preflight for registered file grants.
// The sidecar records only exact, already-registered grants; it is not a grant.
const exactSourceSchema = "proto1.source-exact/v1"
const exactSeatLimit = 2

var exactFileComponent = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)

type exactSnapshot struct {
	path string
	info os.FileInfo
	hash [sha256.Size]byte
	cap  int64
}

type exactRegistration struct {
	id        string
	grants    []string
	snapshots []exactSnapshot
	output    string
}

type exactSidecar struct {
	Schema          string              `json:"schema"`
	SourceManifest  json.RawMessage     `json:"source_manifest"`
	ExactWritePaths map[string][]string `json:"exact_write_paths"`
	SHA256          string              `json:"sha256"`
}

func exactRefusal(detail string) error {
	return refusal(2, "E_WRITE_PATH: "+detail)
}

// exactRepoRoot resolves the repository anchor without extending Config. A
// regular bridge at <root>/bridge or <root>/.wavemind/bridge is supported.
// An unknown layout is refused rather than assuming an arbitrary worktree.
func exactRepoRoot(bridge string) (string, error) {
	if !validAbs(bridge) || filepath.Base(bridge) != "bridge" {
		return "", exactRefusal("bridge root is not canonical")
	}
	root := filepath.Dir(bridge)
	if filepath.Base(root) == ".wavemind" {
		root = filepath.Dir(root)
	}
	real, err := filepath.EvalSymlinks(root)
	if err != nil || real != root {
		return "", exactRefusal("repository root is absent or symbolic")
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return "", exactRefusal("repository root is not a directory")
	}
	return root, nil
}

// exactRepoFile accepts canonical repository *file* names, including a
// not-yet-created leaf, but never a directory, symbolic ancestor or escape.
func exactRepoFile(root, name string) error {
	if name == "" || len(name) > 512 || filepath.IsAbs(name) ||
		strings.ContainsAny(name, "\\\x00\r\n") || filepath.ToSlash(filepath.Clean(name)) != name {
		return exactRefusal("noncanonical file name")
	}
	parts := strings.Split(name, "/")
	for _, part := range parts {
		if part == "" || part == "." || part == ".." || part == ".git" || !exactFileComponent.MatchString(part) {
			return exactRefusal("directory, wildcard, traversal or noncanonical file name")
		}
	}
	full := filepath.Join(root, filepath.FromSlash(name))
	if !strings.HasPrefix(full, root+string(filepath.Separator)) {
		return exactRefusal("repository escape")
	}
	current := root
	missing := false
	for i, part := range parts {
		current = filepath.Join(current, part)
		if missing {
			continue
		}
		info, err := os.Lstat(current)
		if errors.Is(err, os.ErrNotExist) {
			missing = true
			continue
		}
		if err != nil || info.Mode()&os.ModeSymlink != 0 {
			return exactRefusal("symlink or unavailable file component")
		}
		if i == len(parts)-1 {
			if !info.Mode().IsRegular() {
				return exactRefusal("registered file resolves to a directory or nonregular node")
			}
		} else if !info.IsDir() {
			return exactRefusal("non-directory ancestor")
		}
	}
	return nil
}

// Queue output is a local absolute file target, not a repository write grant.
// Prevent aliases through any existing symlink and accidental directory output.
func exactOutputFile(path string) error {
	if !validAbs(path) {
		return exactRefusal("unclean queue output path")
	}
	parts := strings.Split(strings.TrimPrefix(path, string(filepath.Separator)), string(filepath.Separator))
	current := string(filepath.Separator)
	for i, part := range parts {
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if errors.Is(err, os.ErrNotExist) {
			return nil // A future output file or parent is not an authority grant.
		}
		if err != nil || info.Mode()&os.ModeSymlink != 0 {
			return exactRefusal("queue output symbolic or unavailable")
		}
		if i == len(parts)-1 {
			if !info.Mode().IsRegular() {
				return exactRefusal("queue output is not a file")
			}
		} else if !info.IsDir() {
			return exactRefusal("queue output has non-directory ancestor")
		}
	}
	return nil
}

func exactReadSnapshot(path string, limit int64) ([]byte, exactSnapshot, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() {
		return nil, exactSnapshot{}, refusal(4, "registered file unavailable")
	}
	b, err := readBounded(path, limit)
	if err != nil {
		return nil, exactSnapshot{}, refusal(4, "registered file invalid or changed")
	}
	return b, exactSnapshot{path: path, info: info, hash: sha256.Sum256(b), cap: limit}, nil
}

func exactSnapshotMatches(s exactSnapshot) bool {
	b, next, err := exactReadSnapshot(s.path, s.cap)
	return err == nil && bytes.Equal(s.hash[:], next.hash[:]) && os.SameFile(s.info, next.info) && b != nil
}

func exactGrants(task workerbridge.Document, root string) ([]string, error) {
	raw, ok := task["allowed_write_paths"].([]any)
	if !ok || len(raw) > 256 {
		return nil, exactRefusal("missing or oversized registered write paths")
	}
	out := make([]string, 0, len(raw))
	seen := map[string]bool{}
	for _, value := range raw {
		p, ok := value.(string)
		if !ok || seen[p] {
			return nil, exactRefusal("duplicate or non-string registered grant")
		}
		if err := exactRepoFile(root, p); err != nil {
			return nil, err
		}
		seen[p] = true
		out = append(out, p)
	}
	for _, key := range []string{"result_path", "report_path"} {
		p, ok := task[key].(string)
		if !ok {
			return nil, exactRefusal("missing registered output file")
		}
		if err := exactRepoFile(root, p); err != nil {
			return nil, err
		}
		if seen[p] {
			return nil, exactRefusal("output aliases a write grant or another output")
		}
		seen[p] = true
	}
	return out, nil
}

func exactRegistrations(entries []entry, cfg Config, root string) ([]exactRegistration, error) {
	regs := make([]exactRegistration, 0, len(entries))
	// One ledger snapshot binds every lease to the same observed generation.
	ledgerPath := filepath.Join(cfg.BridgeDir, "ledger.jsonl")
	ledger, ledgerSnap, err := exactReadSnapshot(ledgerPath, maxLedgerBytes)
	if err != nil || (len(ledger) > 0 && ledger[len(ledger)-1] != '\n') {
		return nil, refusal(4, "lease ledger unavailable or torn")
	}
	globallyGranted := make(map[string]string)
	for _, item := range entries {
		taskPath := filepath.Join(cfg.BridgeDir, "tasks", item.id, cfg.Attempt, "task.json")
		taskRaw, taskSnap, err := exactReadSnapshot(taskPath, w8muse.MaxHeader)
		if err != nil {
			return nil, err
		}
		task, err := workerbridge.Parse(taskRaw)
		if err != nil {
			return nil, refusal(4, "registered task invalid")
		}
		paths, err := exactGrants(task, root)
		if err != nil {
			return nil, err
		}
		for _, p := range paths {
			if prior, exists := globallyGranted[p]; exists {
				return nil, exactRefusal("duplicate grant across jobs " + prior + " and " + item.id)
			}
			globallyGranted[p] = item.id
		}
		if err := exactOutputFile(item.outputPath); err != nil {
			return nil, err
		}
		expectedPath := filepath.Join(cfg.BridgeDir, "expected", item.id, cfg.Attempt+".json")
		_, expectedSnap, err := exactReadSnapshot(expectedPath, w8muse.MaxHeader)
		if err != nil {
			return nil, err
		}
		// Real task/schema/expected/expiry/identity/ledger validator; no stub.
		if err := checkRegistration(cfg, item.id); err != nil {
			return nil, err
		}
		regs = append(regs, exactRegistration{
			id: item.id, grants: paths, output: item.outputPath,
			snapshots: []exactSnapshot{taskSnap, expectedSnap, ledgerSnap},
		})
	}
	return regs, nil
}

func exactStillRegistered(regs []exactRegistration, cfg Config, root string) error {
	for _, reg := range regs {
		for _, s := range reg.snapshots {
			if !exactSnapshotMatches(s) {
				return refusal(4, "registration or live lease ledger changed")
			}
		}
		if err := exactOutputFile(reg.output); err != nil {
			return err
		}
		for _, p := range reg.grants {
			if err := exactRepoFile(root, p); err != nil {
				return err
			}
		}
		if err := checkRegistration(cfg, reg.id); err != nil {
			return err
		}
	}
	return nil
}

// BuildExact validates every queued registration and file before delegating to
// legacy Build. It never creates, reserves, submits, releases or publishes.
// All refusal paths return (nil, nil, error), including late integrity failures.
func BuildExact(queue []byte, cfg Config) ([]byte, []byte, error) {
	if err := validateConfig(cfg); err != nil {
		return nil, nil, err
	}
	entries, err := parseQueue(queue)
	if err != nil {
		return nil, nil, err
	}
	root, err := exactRepoRoot(cfg.BridgeDir)
	if err != nil {
		return nil, nil, err
	}
	regs, err := exactRegistrations(entries, cfg, root)
	if err != nil {
		return nil, nil, err
	}
	if cfg.InFlight >= exactSeatLimit && cfg.MaxInFlight > cfg.InFlight {
		return nil, nil, refusal(6, "EN_DUDA: wave open, seat unavailable/refused, or no credits")
	}
	// The existing builder remains responsible for queue, brief, seat,
	// source-manifest and final wire validation, and rechecks registrations.
	wire, sourceManifest, err := Build(queue, cfg)
	if err != nil {
		return nil, nil, err
	}
	free := cfg.MaxInFlight - cfg.InFlight
	if remaining := exactSeatLimit - cfg.InFlight; free > remaining {
		free = remaining
	}
	if free <= 0 {
		return nil, nil, refusal(6, "EN_DUDA: wave open, seat unavailable/refused, or no credits")
	}
	var items []w8muse.Item
	if err := json.Unmarshal(wire, &items); err != nil || len(items) == 0 {
		return nil, nil, refusal(2, "invalid exact wave")
	}
	count := len(items)
	if count > free {
		count = free
	}
	// Use the sealed w8muse.Item field order. Legacy Build's map-based
	// encoder is unchanged, but its alphabetical key order is not propagated.
	wire, err = json.Marshal(items[:count])
	if err != nil || ValidateWave(wire) != nil {
		return nil, nil, refusal(2, "invalid exact Muse wire")
	}
	frozen, err := workerbridge.Parse(sourceManifest)
	if err != nil {
		return nil, nil, refusal(2, "invalid frozen source manifest")
	}
	if len(items) > count {
		all, ok := frozen["frozen"].([]any)
		if !ok || len(all) < count {
			return nil, nil, refusal(2, "frozen source manifest mismatch")
		}
		frozen["frozen"] = all[:count]
		frozen["emitted_count"] = count
		frozen["pending_count"] = len(entries) - count
	}
	frozen["wave_sha256"] = digest(wire)
	sourceManifest, err = json.Marshal(frozen)
	if err != nil || len(sourceManifest) > w8muse.MaxHeader {
		return nil, nil, refusal(2, "exact source manifest byte cap")
	}
	var source workerbridge.Document
	source, err = workerbridge.Parse(sourceManifest)
	if err != nil {
		return nil, nil, refusal(2, "invalid source manifest")
	}
	exactPaths := make(map[string][]string, count)
	for i := 0; i < count; i++ {
		exactPaths[regs[i].id] = regs[i].grants
	}
	unhashed := map[string]any{
		"schema":            exactSourceSchema,
		"source_manifest":   source,
		"exact_write_paths": exactPaths,
	}
	canonical, err := workerbridge.Canonical(unhashed)
	if err != nil {
		return nil, nil, refusal(2, "uncanonical exact manifest")
	}
	sidecar, err := json.Marshal(exactSidecar{
		Schema:          exactSourceSchema,
		SourceManifest:  json.RawMessage(sourceManifest),
		ExactWritePaths: exactPaths,
		SHA256:          digest(canonical),
	})
	if err != nil || len(sidecar) > w8muse.MaxHeader {
		return nil, nil, refusal(2, "exact manifest byte cap")
	}
	if err := exactStillRegistered(regs, cfg, root); err != nil {
		return nil, nil, err
	}
	return wire, sidecar, nil
}
