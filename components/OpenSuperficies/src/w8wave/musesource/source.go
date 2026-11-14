// Package musesource freezes registered operator jobs into bounded Muse waves.
// It does not launch workers, execute gates, infer, or create registrations.
package musesource

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"opensuperficies.local/srf/src/w8muse"
	"opensuperficies.local/srf/src/workerbridge"
)

const (
	MaxQueueBytes = 256 << 10
	MaxQueueLines = 64
	MaxBriefBytes = 64 << 10
	maxLedgerBytes = 16 << 20
)

var (
	taskPattern = regexp.MustCompile("^[a-z0-9]+(?:-[a-z0-9]+)*$")
	attemptPattern = regexp.MustCompile("^a[0-9]+$")
	commitPattern = regexp.MustCompile("^[a-f0-9]{40}$")
	repoPattern = regexp.MustCompile("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
)

type Config struct {
	BridgeDir      string
	Repository     string
	BaseCommit     string
	Attempt        string
	GateArgv       [][]string
	ResourcePolicy map[string]any
	Obligations    []string
	MaxInFlight    int
	InFlight       int
	WaveOpen       bool
	SeatReady      bool
	GuestInference bool
}

type entry struct {
	id, briefPath, outputPath string
}

type registrationCheck func(Config, string) error

func refusal(code int, detail string) error {
	class := "input"
	switch code {
	case 3:
		class = "execution"
	case 4:
		class = "unavailable"
	case 5:
		class = "verification"
	case 6:
		class = "EN_DUDA"
	case 7:
		class = "operator"
	}
	return &workerbridge.Reject{Code: code, Class: class, Detail: detail}
}

// ExitCode is the normalized W8 outcome: 0 or 2..7, never 1.
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var r *workerbridge.Reject
	if errors.As(err, &r) && r.Code >= 2 && r.Code <= 7 {
		return r.Code
	}
	return 6
}

func digest(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func validAbs(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || strings.ContainsAny(path, "\x00\r\n\\") {
		return false
	}
	for _, component := range strings.Split(path, string(filepath.Separator)) {
		if component == "." || component == ".." {
			return false
		}
	}
	return true
}

func readBounded(path string, limit int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > limit {
		return nil, refusal(2, "file absent, symlink, nonregular, or too large")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, refusal(2, "file unavailable")
	}
	defer f.Close()
	after, err := f.Stat()
	if err != nil || !after.Mode().IsRegular() || !os.SameFile(info, after) {
		return nil, refusal(2, "file changed during read")
	}
	b, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil || int64(len(b)) > limit {
		return nil, refusal(2, "file exceeds cap or read failed")
	}
	return b, nil
}

func parseQueue(raw []byte) ([]entry, error) {
	if len(raw) == 0 || len(raw) > MaxQueueBytes || !utf8.Valid(raw) || bytes.IndexByte(raw, 0) >= 0 {
		return nil, refusal(2, "queue byte cap or encoding")
	}
	lines := bytes.Split(bytes.TrimSuffix(raw, []byte{'\n'}), []byte{'\n'})
	if len(lines) == 0 || len(lines) > MaxQueueLines {
		return nil, refusal(2, "queue line cap")
	}
	entries := make([]entry, 0, len(lines))
	seen := make(map[string]bool)
	for _, line := range lines {
		fields := bytes.Split(line, []byte{'\t'})
		if len(fields) != 3 {
			return nil, refusal(2, "queue requires exactly three TAB fields")
		}
		id, brief, output := string(fields[0]), string(fields[1]), string(fields[2])
		if !taskPattern.MatchString(id) || seen[id] || !validAbs(brief) || !validAbs(output) {
			return nil, refusal(2, "task id, duplicate, or unclean absolute path")
		}
		seen[id] = true
		entries = append(entries, entry{id: id, briefPath: brief, outputPath: output})
	}
	return entries, nil
}

func validateConfig(c Config) error {
	if !validAbs(c.BridgeDir) || !repoPattern.MatchString(c.Repository) || !commitPattern.MatchString(c.BaseCommit) ||
		!attemptPattern.MatchString(c.Attempt) || c.MaxInFlight < 1 || c.MaxInFlight > 64 ||
		c.InFlight < 0 || c.InFlight > c.MaxInFlight || c.GuestInference {
		return refusal(2, "invalid source configuration or guest inference")
	}
	if len(c.GateArgv) == 0 || len(c.GateArgv) > 32 || len(c.Obligations) == 0 || len(c.Obligations) > 32 {
		return refusal(2, "gates or obligations absent/out of bounds")
	}
	for _, gate := range c.GateArgv {
		if len(gate) == 0 || len(gate) > 64 {
			return refusal(2, "gate argv bounds")
		}
		for _, arg := range gate {
			if arg == "" || len(arg) > 2048 || !utf8.ValidString(arg) || strings.ContainsRune(arg, 0) {
				return refusal(2, "invalid gate argv field")
			}
		}
	}
	for _, o := range c.Obligations {
		if strings.TrimSpace(o) == "" || len(o) > 512 || !utf8.ValidString(o) || strings.ContainsRune(o, 0) {
			return refusal(2, "invalid obligation")
		}
	}
	if len(c.ResourcePolicy) != 4 || c.ResourcePolicy["inference"] != "host" {
		return refusal(2, "resource policy must be bounded and host inference only")
	}
	for _, key := range []string{"max_cpu_ms", "max_memory_bytes", "max_wall_ms"} {
		switch c.ResourcePolicy[key].(type) {
		case int, int64, json.Number:
		default:
			return refusal(2, "resource bound must be an integer")
		}
		n, err := strconv.ParseInt(fmt.Sprint(c.ResourcePolicy[key]), 10, 64)
		if err != nil || n <= 0 {
			return refusal(2, "missing positive resource bound: "+key)
		}
	}
	gates, err := json.Marshal(c.GateArgv)
	if err != nil || len(gates) > 16384 {
		return refusal(2, "gate argv byte cap")
	}
	return nil
}

func same(a, b any) bool {
	left, e1 := workerbridge.Canonical(a)
	right, e2 := workerbridge.Canonical(b)
	return e1 == nil && e2 == nil && bytes.Equal(left, right)
}

// checkRegistration mirrors the Muse read-only task, expected and lease checks.
// No task, expected record or ledger is ever created by this source.
func checkRegistration(c Config, id string) error {
	root := c.BridgeDir
	raw, err := readBounded(filepath.Join(root, "tasks", id, c.Attempt, "task.json"), w8muse.MaxHeader)
	if err != nil {
		return refusal(4, "registered builder task unavailable")
	}
	task, err := workerbridge.Parse(raw)
	if err != nil || workerbridge.ValidateDef(task, "bridge_task_v1") != nil {
		return refusal(4, "registration contract unavailable")
	}
	if task["task_id"] != id || task["attempt_id"] != c.Attempt || task["role"] != "builder" ||
		task["writer_branch"] != "bridge/"+id+"/"+c.Attempt ||
		task["repo"] != c.Repository || task["observed_source_commit"] != c.BaseCommit {
		return refusal(4, "registration identity, repository, role or source pin mismatch")
	}
	allowedSurfaces := map[string]bool{"codex-cloud": true, "jules": true, "perplexity-mcp": true, "gemini-spark-mcp": true, "chatgpt-mcp": true, "os-capsule": true}
	surface, ok := task["surface"].(string)
	if !ok || !allowedSurfaces[surface] {
		return refusal(4, "registered builder surface unavailable")
	}
	expires, expErr := time.Parse(time.RFC3339, fmt.Sprint(task["expires_at_utc"]))
	if expErr != nil || !expires.After(time.Now()) {
		return refusal(4, "registration expired or invalid")
	}
	expected, err := readBounded(filepath.Join(root, "expected", id, c.Attempt+".json"), w8muse.MaxHeader)
	if err != nil {
		return refusal(4, "expected registration unavailable")
	}
	if _, err := workerbridge.Parse(expected); err != nil {
		return refusal(4, "expected record invalid")
	}
	canonical, err := workerbridge.BridgeCanonical(task)
	if err != nil {
		return refusal(4, "canonical registration unavailable")
	}
	taskHash := digest(canonical)
	ledger, err := readBounded(filepath.Join(root, "ledger.jsonl"), maxLedgerBytes)
	if err != nil || (len(ledger) > 0 && ledger[len(ledger)-1] != '\n') {
		return refusal(4, "lease ledger unavailable or torn")
	}
	scan := bufio.NewScanner(bytes.NewReader(ledger))
	scan.Buffer(make([]byte, 4096), w8muse.MaxHeader)
	active := false
	seq := 0
	for scan.Scan() {
		seq++
		ev, parseErr := workerbridge.Parse(scan.Bytes())
		if parseErr != nil || fmt.Sprint(ev["seq"]) != strconv.Itoa(seq) {
			return refusal(4, "lease ledger invalid")
		}
		if ev["lease_id"] != task["lease_id"] {
			continue
		}
		switch ev["event"] {
		case "lease_granted":
			active = ev["task_sha256"] == taskHash
			for _, key := range []string{"task_id", "attempt_id", "lease_id", "thread", "agent", "worker_id", "key", "base_seq", "surface", "role", "allowed_write_paths", "expires_at_utc"} {
				active = active && same(ev[key], task[key])
			}
		case "lease_released", "result_admitted", "result_blocked":
			active = false
		}
	}
	if scan.Err() != nil || !active {
		return refusal(4, "no active matching builder lease")
	}
	return nil
}

// ValidateWave enforces the driver's exact wire shape before publication.
func ValidateWave(raw []byte) error {
	if len(raw) == 0 || len(raw) > w8muse.MaxHeader {
		return refusal(2, "wave header byte cap")
	}
	if _, err := workerbridge.Decode(raw); err != nil {
		return refusal(2, "ambiguous wave JSON")
	}
	var objects []map[string]json.RawMessage
	dec := json.NewDecoder(bytes.NewReader(raw))
	if err := dec.Decode(&objects); err != nil || len(objects) < 1 || len(objects) > 64 {
		return refusal(2, "wave count or JSON shape")
	}
	if _, err := dec.Token(); err != io.EOF {
		return refusal(2, "trailing wave data")
	}
	for _, o := range objects {
		if len(o) < 3 || len(o) > 4 {
			return refusal(2, "wave object field count")
		}
		for name := range o {
			if name != "task_id" && name != "attempt" && name != "objective" && name != "patch_path" {
				return refusal(2, "unknown wave field")
			}
		}
		var id, attempt, objective string
		for name, target := range map[string]*string{"task_id": &id, "attempt": &attempt, "objective": &objective} {
			if err := json.Unmarshal(o[name], target); err != nil {
				return refusal(2, "wave field not a string")
			}
		}
		if !taskPattern.MatchString(id) || !attemptPattern.MatchString(attempt) || strings.TrimSpace(objective) == "" {
			return refusal(2, "wave item invalid")
		}
		if patch, ok := o["patch_path"]; ok {
			var path string
			if json.Unmarshal(patch, &path) != nil || !validAbs(path) {
				return refusal(2, "invalid patch path")
			}
		}
	}
	return nil
}

func fieldHashes(frozen map[string]any) (map[string]string, error) {
	hashes := make(map[string]string, len(frozen))
	for field, value := range frozen {
		var raw []byte
		switch v := value.(type) {
		case string:
			raw = []byte(v)
		default:
			var err error
			raw, err = json.Marshal(value)
			if err != nil {
				return nil, refusal(2, "unhashable frozen field")
			}
		}
		hashes[field] = digest(raw)
	}
	return hashes, nil
}

// Build validates the entire queue before returning even one output byte.
// Admission uses externally verified seat readiness; it never probes or runs a seat.
func Build(queue []byte, cfg Config) ([]byte, []byte, error) {
	return build(queue, cfg, checkRegistration)
}

func build(queue []byte, cfg Config, registration registrationCheck) ([]byte, []byte, error) {
	if err := validateConfig(cfg); err != nil {
		return nil, nil, err
	}
	entries, err := parseQueue(queue)
	if err != nil {
		return nil, nil, err
	}
	// Validate the entire queue before admission. An invalid pending job is
	// not silently accepted merely because this wave has fewer free credits.
	briefs := make([][]byte, len(entries))
	for i, item := range entries {
		brief, err := readBounded(item.briefPath, MaxBriefBytes)
		if err != nil || len(brief) == 0 || !utf8.Valid(brief) || bytes.IndexByte(brief, 0) >= 0 || strings.TrimSpace(string(brief)) == "" {
			return nil, nil, refusal(2, "invalid, missing, or oversized brief")
		}
		briefs[i] = brief
	}
	if cfg.WaveOpen || !cfg.SeatReady || cfg.InFlight == cfg.MaxInFlight {
		return nil, nil, refusal(6, "EN_DUDA: wave open, seat unavailable/refused, or no credits")
	}
	for _, item := range entries {
		if err := registration(cfg, item.id); err != nil {
			return nil, nil, err
		}
	}
	free := cfg.MaxInFlight - cfg.InFlight
	wave := make([]map[string]any, 0, len(entries))
	frozen := make([]map[string]any, 0, len(entries))
	for i, item := range entries {
		if i >= free {
			break
		}
		brief := briefs[i]
		objective := string(brief)
		wave = append(wave, map[string]any{"task_id": item.id, "attempt": cfg.Attempt, "objective": objective})
		fields := map[string]any{
			"task_id": item.id, "attempt": cfg.Attempt,
			"brief_path": item.briefPath, "output_path": item.outputPath,
			"repository": cfg.Repository, "base_commit": cfg.BaseCommit,
			"objective": objective, "input_sha256": digest(brief),
			"gate_argv": cfg.GateArgv, "resource_policy": cfg.ResourcePolicy,
			"obligations": cfg.Obligations,
		}
		hashes, err := fieldHashes(fields)
		if err != nil {
			return nil, nil, err
		}
		fields["field_sha256"] = hashes
		frozen = append(frozen, fields)
	}
	wire, err := json.Marshal(wave)
	if err != nil || ValidateWave(wire) != nil {
		return nil, nil, refusal(2, "invalid generated wave")
	}
	manifest, err := json.Marshal(map[string]any{
		"schema_version": "w8-muse-source-frozen-v1",
		"queue_sha256": digest(queue),
		"queued_count": len(entries),
		"emitted_count": len(wave),
		"max_in_flight": cfg.MaxInFlight,
		"prior_in_flight": cfg.InFlight,
		"pending_count": len(entries) - len(wave),
		"frozen": frozen,
		"wave_sha256": digest(wire),
	})
	if err != nil || len(manifest) > w8muse.MaxHeader {
		return nil, nil, refusal(2, "manifest byte cap")
	}
	return wire, manifest, nil
}
