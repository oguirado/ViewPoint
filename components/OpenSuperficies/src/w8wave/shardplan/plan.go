// Package shardplan validates a pinned, finite suite partition before asking the
// registered Muse source to compile any task-source items. It grants no leases,
// executes no commands and has no publication transport.
package shardplan

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"opensuperficies.local/srf/src/w8wave/musesource"
	"opensuperficies.local/srf/src/workerbridge"
)

const (
	Schema            = "proto1.suite-manifest/v1"
	MaxManifestBytes  = 256 << 10
	MaxLedgerBytes    = 256 << 10
	MaxShards         = 64
	MaxSeatExecutions = 2
)

// The JSON tags specify the public emission order. Hashing uses canonical
// sorted-key JSON, not the order of this wire representation.
type Manifest struct {
	Schema               string   `json:"schema"`
	Repo                 string   `json:"repo"`
	SourceCommit         string   `json:"source_commit"`
	SuiteSHA256          string   `json:"suite_sha256"`
	DurationLedgerSHA256 string   `json:"duration_ledger_sha256"`
	PlannerVersion       string   `json:"planner_version"`
	Seed                 int64    `json:"seed"`
	RequestedShards      int64    `json:"requested_shards"`
	SelectedCaseIDs      []string `json:"selected_case_ids"`
	Capabilities         []string `json:"capabilities"`
	Shards               []Shard  `json:"shards"`
	ManifestSHA256       string   `json:"manifest_sha256"`
}

type Shard struct {
	ShardID              string            `json:"shard_id"`
	Index                int64             `json:"index"`
	ResourceClass        string            `json:"resource_class"`
	Modules              []string          `json:"modules"`
	CaseIDs              []string          `json:"case_ids"`
	EstimatedDurationNS  int64             `json:"estimated_duration_ns"`
	RequiredCapabilities []string          `json:"required_capabilities"`
	ExclusiveResources   []string          `json:"exclusive_resources"`
	GateArgv             []string          `json:"gate_argv"`
	ExpectedOutputs      []string          `json:"expected_outputs"`
	FieldSHA256          map[string]string `json:"field_sha256"`
}

// Registration is a host-owned binding to a preexisting dispatch. The compiler
// does not create the brief, output, expected record, task or lease ledger.
type Registration struct {
	ShardID         string
	TaskID          string
	BriefPath       string
	BriefSHA256     string
	OutputPath      string
	ExpectedOutputs []string
}

// HostWitness is supplied by the trusted host, never from guest JSON. The
// scratch answer means directory + executable traversal and creation have been
// verified in the target execution namespace, not merely on the compiler VM.
// Missing proof refuses compilation; this interface does not bless a runner.
type HostWitness interface {
	HasCapability(string) bool
	HasExclusiveResource(string) bool
	ExecutableScratch(string) bool
	SafeOutput(string) bool
}

// Config pins two *raw* frozen inventories, independently of the manifest.
// SuiteJSON is an inventory of complete modules and ordered test cases; ledger
// JSON is a measured case-duration inventory, with no inferred zero durations.
type Config struct {
	Source             musesource.Config
	SuiteJSON          []byte
	DurationLedgerJSON []byte
	Registrations      []Registration
	Witness            HostWitness
}

// Invocation is descriptive, not a shell script or a runnable authority. The
// executor must run Prepare first and preserve Env and Argv as literal vectors.
type Invocation struct {
	Prepare          [][]string `json:"prepare"`
	Env              []string   `json:"env"`
	Argv             []string   `json:"argv"`
	NativeExitStatus *int       `json:"native_exit_status"`
}

type Item struct {
	ShardID        string
	TaskID         string
	Queue          []byte
	SourceItems    []byte
	SourceManifest []byte
	Invocation     Invocation
}

type Result struct {
	Manifest           []byte
	Items              []Item
	PendingShardIDs    []string
	RequestedShards    int
	EmittedCount       int
	MaxConcurrentSeats int
}

var (
	idRX           = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)
	repoRX         = regexp.MustCompile(`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`)
	versionRX      = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	pythonModuleRX = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$`)
	capabilityRX   = regexp.MustCompile(`^[a-z][a-z0-9-]{0,63}$`)
)

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

// ExitCode preserves the W8 0,2..7 vocabulary; native child status must be
// recorded separately and must not be conflated with this result.
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var reject *workerbridge.Reject
	if errors.As(err, &reject) && reject.Code >= 2 && reject.Code <= 7 {
		return reject.Code
	}
	return 6
}

func hash(b []byte) string { sum := sha256.Sum256(b); return hex.EncodeToString(sum[:]) }
func hexPin(s string, n int) bool {
	if len(s) != n {
		return false
	}
	for i := range s {
		if !((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f')) {
			return false
		}
	}
	return true
}
func closed(o map[string]any, names ...string) bool {
	if len(o) != len(names) {
		return false
	}
	for _, name := range names {
		if _, ok := o[name]; !ok {
			return false
		}
	}
	return true
}
func object(v any) (map[string]any, bool) { m, ok := v.(map[string]any); return m, ok }
func array(v any) ([]any, bool)           { a, ok := v.([]any); return a, ok }
func stringField(v any) (string, bool)    { s, ok := v.(string); return s, ok }
func integer(v any) (int64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	i, err := strconv.ParseInt(string(n), 10, 64)
	return i, err == nil && i >= 0
}
func stringsField(v any, limit int, valid func(string) bool) ([]string, bool) {
	a, ok := array(v)
	if !ok || len(a) > limit {
		return nil, false
	}
	out := make([]string, 0, len(a))
	for _, x := range a {
		s, ok := stringField(x)
		if !ok || !valid(s) {
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}
func unique(v []string) bool {
	set := make(map[string]bool, len(v))
	for _, x := range v {
		if set[x] {
			return false
		}
		set[x] = true
	}
	return true
}
func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
func sortedUnique(a []string) bool {
	if len(a) == 0 || !unique(a) {
		return false
	}
	return sort.StringsAreSorted(a)
}
func validRel(s string) bool {
	if s == "" || len(s) > 512 || !utf8.ValidString(s) || path.IsAbs(s) || path.Clean(s) != s || strings.ContainsAny(s, "\\\x00\r\n\t<>:&;|`$ \"") {
		return false
	}
	for _, v := range strings.Split(s, "/") {
		if v == "" || v == "." || v == ".." || v == ".git" {
			return false
		}
		for _, r := range v {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-' || r == '.') {
				return false
			}
		}
	}
	return true
}
func validModule(class, name string) bool {
	if class == "python" {
		return pythonModuleRX.MatchString(name)
	}
	return validRel(name)
}
func validAbs(s string) bool {
	if !filepath.IsAbs(s) || filepath.Clean(s) != s || len(s) > 4096 || strings.ContainsAny(s, "\\\x00\r\n\t") {
		return false
	}
	for _, p := range strings.Split(s, "/") {
		if p == "." || p == ".." {
			return false
		}
	}
	return true
}
func validArg(s string) bool {
	return s != "" && len(s) <= 2048 && utf8.ValidString(s) && !strings.ContainsAny(s, "\x00\r\n\t")
}
func sortedDigest(v any) (string, error) {
	b, e := workerbridge.Canonical(v)
	if e != nil {
		return "", e
	}
	return hash(b), nil
}
func parseClosed(raw []byte, max int, names ...string) (map[string]any, error) {
	if len(raw) == 0 || len(raw) > max {
		return nil, refusal(2, "JSON byte cap or empty input")
	}
	m, e := workerbridge.Parse(raw)
	if e != nil {
		return nil, refusal(2, "ambiguous, duplicate-key or non-integer JSON")
	}
	if !closed(m, names...) {
		return nil, refusal(2, "missing or unknown JSON fields")
	}
	return m, nil
}

const suiteModuleFields = "name resource_class case_ids required_capabilities exclusive_resources gate_argv expected_outputs"

type suiteModule struct {
	name, class                                   string
	cases, capabilities, resources, gate, outputs []string
}

func decodeSuite(raw []byte) (map[string]suiteModule, error) {
	root, e := parseClosed(raw, MaxLedgerBytes, "modules")
	if e != nil {
		return nil, e
	}
	entries, ok := array(root["modules"])
	if !ok || len(entries) == 0 || len(entries) > 256 {
		return nil, refusal(2, "invalid suite module count")
	}
	out := make(map[string]suiteModule, len(entries))
	allCases := make(map[string]bool)
	for _, v := range entries {
		m, ok := object(v)
		if !ok || !closed(m, strings.Fields(suiteModuleFields)...) {
			return nil, refusal(2, "suite module shape")
		}
		name, _ := stringField(m["name"])
		class, _ := stringField(m["resource_class"])
		if class != "go" && class != "python" && class != "host" {
			return nil, refusal(2, "suite resource class")
		}
		if !validModule(class, name) || out[name].name != "" {
			return nil, refusal(2, "duplicate or invalid suite module")
		}
		cases, ok1 := stringsField(m["case_ids"], 4096, func(s string) bool { return idRX.MatchString(s) })
		caps, ok2 := stringsField(m["required_capabilities"], 64, func(s string) bool { return capabilityRX.MatchString(s) })
		resources, ok3 := stringsField(m["exclusive_resources"], 64, func(s string) bool { return capabilityRX.MatchString(s) })
		gate, ok4 := stringsField(m["gate_argv"], 64, validArg)
		outputs, ok5 := stringsField(m["expected_outputs"], 64, validRel)
		if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || len(cases) == 0 || len(caps) == 0 || len(gate) == 0 || len(outputs) == 0 || !unique(cases) || !sortedUnique(caps) || !unique(resources) || !unique(outputs) {
			return nil, refusal(2, "invalid suite module inventory")
		}
		for _, id := range cases {
			if allCases[id] {
				return nil, refusal(2, "suite case id reused across modules")
			}
			allCases[id] = true
		}
		out[name] = suiteModule{name, class, cases, caps, resources, gate, outputs}
	}
	return out, nil
}

func decodeDurations(raw []byte) (map[string]int64, error) {
	root, e := parseClosed(raw, MaxLedgerBytes, "cases")
	if e != nil {
		return nil, e
	}
	a, ok := array(root["cases"])
	if !ok || len(a) == 0 || len(a) > 16384 {
		return nil, refusal(2, "duration case count")
	}
	times := make(map[string]int64, len(a))
	for _, v := range a {
		rec, ok := object(v)
		if !ok || !closed(rec, "case_id", "duration_ns") {
			return nil, refusal(2, "duration ledger record shape")
		}
		id, ok := stringField(rec["case_id"])
		n, valid := integer(rec["duration_ns"])
		if !ok || !idRX.MatchString(id) || !valid {
			return nil, refusal(2, "measured duration invalid")
		}
		if _, seen := times[id]; seen {
			return nil, refusal(2, "duplicate measured duration")
		}
		times[id] = n
	}
	return times, nil
}

const shardFields = "shard_id index resource_class modules case_ids estimated_duration_ns required_capabilities exclusive_resources gate_argv expected_outputs field_sha256"
const topFields = "schema repo source_commit suite_sha256 duration_ledger_sha256 planner_version seed requested_shards selected_case_ids capabilities shards manifest_sha256"

func decodeManifest(raw []byte) (Manifest, error) {
	root, e := parseClosed(raw, MaxManifestBytes, strings.Fields(topFields)...)
	if e != nil {
		return Manifest{}, e
	}
	m := Manifest{}
	m.Schema, _ = stringField(root["schema"])
	m.Repo, _ = stringField(root["repo"])
	m.SourceCommit, _ = stringField(root["source_commit"])
	m.SuiteSHA256, _ = stringField(root["suite_sha256"])
	m.DurationLedgerSHA256, _ = stringField(root["duration_ledger_sha256"])
	m.PlannerVersion, _ = stringField(root["planner_version"])
	m.ManifestSHA256, _ = stringField(root["manifest_sha256"])
	seed, okSeed := integer(root["seed"])
	count, okCount := integer(root["requested_shards"])
	m.Seed, m.RequestedShards = seed, count
	var okIDs, okCaps bool
	m.SelectedCaseIDs, okIDs = stringsField(root["selected_case_ids"], 16384, func(s string) bool { return idRX.MatchString(s) })
	m.Capabilities, okCaps = stringsField(root["capabilities"], 64, func(s string) bool { return capabilityRX.MatchString(s) })
	shardRaw, okShards := array(root["shards"])
	if m.Schema != Schema || !repoRX.MatchString(m.Repo) || !hexPin(m.SourceCommit, 40) || !hexPin(m.SuiteSHA256, 64) || !hexPin(m.DurationLedgerSHA256, 64) || !versionRX.MatchString(m.PlannerVersion) || !hexPin(m.ManifestSHA256, 64) || !okSeed || !okCount || count < 1 || count > MaxShards || !okIDs || !okCaps || !sortedUnique(m.SelectedCaseIDs) || !sortedUnique(m.Capabilities) || !okShards || len(shardRaw) != int(count) {
		return Manifest{}, refusal(2, "manifest identity, pin or count")
	}
	m.Shards = make([]Shard, 0, len(shardRaw))
	for _, rawShard := range shardRaw {
		v, ok := object(rawShard)
		if !ok || !closed(v, strings.Fields(shardFields)...) {
			return Manifest{}, refusal(2, "shard missing or unknown field")
		}
		s := Shard{}
		s.ShardID, _ = stringField(v["shard_id"])
		s.Index, _ = integer(v["index"])
		var validIndex bool
		_, validIndex = integer(v["index"])
		s.ResourceClass, _ = stringField(v["resource_class"])
		s.EstimatedDurationNS, _ = integer(v["estimated_duration_ns"])
		var validDuration bool
		_, validDuration = integer(v["estimated_duration_ns"])
		var o1, o2, o3, o4, o5, o6 bool
		s.Modules, o1 = stringsField(v["modules"], 256, func(x string) bool { return validModule(s.ResourceClass, x) })
		s.CaseIDs, o2 = stringsField(v["case_ids"], 16384, func(x string) bool { return idRX.MatchString(x) })
		s.RequiredCapabilities, o3 = stringsField(v["required_capabilities"], 64, func(x string) bool { return capabilityRX.MatchString(x) })
		s.ExclusiveResources, o4 = stringsField(v["exclusive_resources"], 64, func(x string) bool { return capabilityRX.MatchString(x) })
		s.GateArgv, o5 = stringsField(v["gate_argv"], 64, validArg)
		s.ExpectedOutputs, o6 = stringsField(v["expected_outputs"], 64, validRel)
		fieldMap, ok := object(v["field_sha256"])
		if !idRX.MatchString(s.ShardID) || !validIndex || !validDuration || !(s.ResourceClass == "go" || s.ResourceClass == "python" || s.ResourceClass == "host") || !o1 || !o2 || !o3 || !o4 || !o5 || !o6 || len(s.Modules) == 0 || len(s.CaseIDs) == 0 || len(s.RequiredCapabilities) == 0 || len(s.GateArgv) == 0 || len(s.ExpectedOutputs) == 0 || !unique(s.Modules) || !unique(s.CaseIDs) || !sortedUnique(s.RequiredCapabilities) || !unique(s.ExclusiveResources) || !unique(s.ExpectedOutputs) || !ok || len(fieldMap) != 10 {
			return Manifest{}, refusal(2, "empty or malformed shard")
		}
		s.FieldSHA256 = make(map[string]string, len(fieldMap))
		for _, key := range strings.Fields(shardFields)[:10] {
			got, ok := stringField(fieldMap[key])
			if !ok || !hexPin(got, 64) {
				return Manifest{}, refusal(2, "missing shard field seal")
			}
			actual, err := sortedDigest(v[key])
			if err != nil || actual != got {
				return Manifest{}, refusal(5, "shard field seal mismatch: "+key)
			}
			s.FieldSHA256[key] = got
		}
		m.Shards = append(m.Shards, s)
	}
	core := make(map[string]any, len(root)-1)
	for k, v := range root {
		if k != "manifest_sha256" {
			core[k] = v
		}
	}
	observed, e := sortedDigest(core)
	if e != nil || observed != m.ManifestSHA256 {
		return Manifest{}, refusal(5, "manifest SHA256 mismatch")
	}
	return m, nil
}

func checkGate(class string, modules []string, argv []string) bool {
	switch class {
	case "go":
		// The source argv is a fixed, literal exec vector. A separate mkdir
		// prerequisite and GOTMPDIR binding are not shell interpolation.
		return len(argv) >= 5 && argv[0] == "env" && argv[1] == "GOTMPDIR=/workspace/.gotmp" && argv[2] == "go" && argv[3] == "test" && !hasShellMeta(argv[4:])
	case "python":
		return len(argv) == 3+len(modules) && argv[0] == "python3" && argv[1] == "-m" && argv[2] == "unittest" && sameStrings(argv[3:], modules)
	case "host":
		return len(argv) >= 6 && argv[0] == "taskset" && argv[1] == "-c" && argv[2] == "10-14" && argv[3] == "go" && argv[4] == "test" && !hasShellMeta(argv[5:])
	}
	return false
}
func hasShellMeta(a []string) bool {
	for _, arg := range a {
		if !validArg(arg) || strings.ContainsAny(arg, ";&|`$><") {
			return true
		}
	}
	return false
}
func orderedUnion(groups ...[]string) []string {
	seen := map[string]bool{}
	var out []string
	for _, g := range groups {
		for _, x := range g {
			if !seen[x] {
				seen[x] = true
				out = append(out, x)
			}
		}
	}
	return out
}
func expectedContents(path string) ([]byte, error) {
	if !validAbs(path) {
		return nil, refusal(2, "registration noncanonical absolute path")
	}
	before, e := os.Lstat(path)
	if e != nil || !before.Mode().IsRegular() || before.Size() > musesource.MaxBriefBytes {
		return nil, refusal(2, "brief absent, nonregular, or oversized")
	}
	content, e := os.ReadFile(path)
	if e != nil || len(content) == 0 || len(content) > musesource.MaxBriefBytes || !utf8.Valid(content) || bytes.IndexByte(content, 0) >= 0 {
		return nil, refusal(2, "invalid brief bytes")
	}
	after, e := os.Lstat(path)
	if e != nil || !os.SameFile(before, after) {
		return nil, refusal(2, "brief changed during compilation")
	}
	return content, nil
}
func safeOutputFile(s string) bool {
	if !validAbs(s) {
		return false
	}
	st, e := os.Lstat(s)
	if e == nil {
		return st.Mode().IsRegular()
	}
	return os.IsNotExist(e)
}
func checkTaskPaths(root, attempt string, reg Registration, outputs []string) error {
	location := filepath.Join(root, "tasks", reg.TaskID, attempt, "task.json")
	stat, e := os.Lstat(location)
	if e != nil || !stat.Mode().IsRegular() || stat.Size() > 64<<10 {
		return refusal(4, "registered task file unavailable")
	}
	raw, e := os.ReadFile(location)
	if e != nil || len(raw) > 64<<10 {
		return refusal(4, "registered task read failed")
	}
	obj, e := workerbridge.Parse(raw)
	if e != nil {
		return refusal(4, "registered task JSON invalid")
	}
	listed, ok := stringsField(obj["allowed_write_paths"], 64, validRel)
	if !ok || !sameStrings(listed, outputs) {
		return refusal(4, "registered individual write paths differ")
	}
	return nil
}

// Compile checks the complete frozen inventory, all resource witnesses and all
// registered file bindings before returning a single item. It compiles a prefix
// with at most two available seat credits; remaining waves remain held.
func Compile(raw []byte, c Config) (Result, error) {
	zero := Result{}
	m, e := decodeManifest(raw)
	if e != nil {
		return zero, e
	}
	src := c.Source
	if c.Witness == nil || src.Repository != m.Repo || src.BaseCommit != m.SourceCommit || !validAbs(src.BridgeDir) || len(src.Attempt) < 2 || src.Attempt[0] != 'a' || !validDecimal(src.Attempt[1:]) || src.MaxInFlight < 1 || src.MaxInFlight > MaxSeatExecutions || src.InFlight < 0 || src.InFlight > src.MaxInFlight || src.GuestInference {
		return zero, refusal(2, "source configuration, pins, or two-seat cap")
	}
	if len(c.SuiteJSON) == 0 || len(c.DurationLedgerJSON) == 0 || hash(c.SuiteJSON) != m.SuiteSHA256 || hash(c.DurationLedgerJSON) != m.DurationLedgerSHA256 {
		return zero, refusal(5, "frozen source or duration seal mismatch")
	}
	modules, e := decodeSuite(c.SuiteJSON)
	if e != nil {
		return zero, e
	}
	durations, e := decodeDurations(c.DurationLedgerJSON)
	if e != nil {
		return zero, e
	}
	if len(c.Registrations) != len(m.Shards) {
		return zero, refusal(2, "one registration required per shard")
	}
	seenCases := map[string]bool{}
	seenModules := map[string]bool{}
	seenShards := map[string]bool{}
	seenTasks := map[string]bool{}
	usedOutputs := map[string]bool{}
	allCaps := map[string]bool{}
	for i, s := range m.Shards {
		if s.Index != int64(i) || seenShards[s.ShardID] {
			return zero, refusal(2, "noncontiguous or duplicate shard index")
		}
		seenShards[s.ShardID] = true
		reg := c.Registrations[i]
		if reg.ShardID != s.ShardID || !idRX.MatchString(reg.TaskID) || seenTasks[reg.TaskID] || !validAbs(reg.BriefPath) || !validAbs(reg.OutputPath) || !hexPin(reg.BriefSHA256, 64) || !sameStrings(reg.ExpectedOutputs, s.ExpectedOutputs) {
			return zero, refusal(2, "registration identity or exact output binding")
		}
		seenTasks[reg.TaskID] = true
		if !safeOutputFile(reg.OutputPath) {
			return zero, refusal(2, "directory, symlink or invalid output target")
		}
		var caseParts, capParts, resourceParts, outputParts [][]string
		var duration int64
		for _, name := range s.Modules {
			witness, ok := modules[name]
			if !ok || seenModules[name] || witness.class != s.ResourceClass {
				return zero, refusal(2, "module missing, repeated or mixed resource class")
			}
			seenModules[name] = true
			if len(caseParts) > 0 && (!sameStrings(capParts[0], witness.capabilities) || !sameStrings(resourceParts[0], witness.resources) || !sameStrings(s.GateArgv, witness.gate)) {
				return zero, refusal(2, "mixed capability, exclusive resource or gate in shard")
			}
			if !sameStrings(s.GateArgv, witness.gate) {
				return zero, refusal(2, "gate argv differs from frozen source")
			}
			caseParts = append(caseParts, witness.cases)
			capParts = append(capParts, witness.capabilities)
			resourceParts = append(resourceParts, witness.resources)
			outputParts = append(outputParts, witness.outputs)
			for _, caseID := range witness.cases {
				ns, ok := durations[caseID]
				if !ok {
					return zero, refusal(2, "missing measured duration: "+caseID)
				}
				if math.MaxInt64-duration < ns {
					return zero, refusal(2, "duration sum overflow")
				}
				duration += ns
				if seenCases[caseID] {
					return zero, refusal(2, "duplicate case across shards")
				}
				seenCases[caseID] = true
			}
		}
		var exactCases, exactOutputs []string
		for _, part := range caseParts {
			exactCases = append(exactCases, part...)
		}
		for _, part := range outputParts {
			exactOutputs = append(exactOutputs, part...)
		}
		if !sameStrings(s.CaseIDs, exactCases) || !sameStrings(s.RequiredCapabilities, capParts[0]) || !sameStrings(s.ExclusiveResources, resourceParts[0]) || !sameStrings(s.ExpectedOutputs, exactOutputs) || s.EstimatedDurationNS != duration || !checkGate(s.ResourceClass, s.Modules, s.GateArgv) {
			return zero, refusal(2, "shard membership, duration, capability or fixed argv mismatch")
		}
		for _, p := range s.ExpectedOutputs {
			if usedOutputs[p] {
				return zero, refusal(2, "duplicate expected output")
			}
			usedOutputs[p] = true
		}
		for _, cap := range s.RequiredCapabilities {
			allCaps[cap] = true
		}
	}
	union := make([]string, 0, len(seenCases))
	for id := range seenCases {
		union = append(union, id)
	}
	sort.Strings(union)
	if !sameStrings(union, m.SelectedCaseIDs) || len(allCaps) != len(m.Capabilities) {
		return zero, refusal(2, "selected-case union or capability declaration mismatch")
	}
	for _, cap := range m.Capabilities {
		if !allCaps[cap] {
			return zero, refusal(2, "unknown declared capability")
		}
	}
	if len(seenModules) < int(m.RequestedShards) {
		return zero, refusal(2, "infeasible shard count")
	}
	// All validation above precedes any registration lookup or Muse call.
	for i, s := range m.Shards {
		r := c.Registrations[i]
		brief, e := expectedContents(r.BriefPath)
		if e != nil {
			return zero, e
		}
		if hash(brief) != r.BriefSHA256 {
			return zero, refusal(5, "brief SHA256 mismatch")
		}
		if !c.Witness.SafeOutput(r.OutputPath) {
			return zero, refusal(4, "host output witness missing")
		}
		for _, cap := range s.RequiredCapabilities {
			if !c.Witness.HasCapability(cap) {
				return zero, refusal(4, "required capability unavailable: "+cap)
			}
		}
		for _, resource := range s.ExclusiveResources {
			if !c.Witness.HasExclusiveResource(resource) {
				return zero, refusal(4, "exclusive resource unavailable: "+resource)
			}
		}
		scratch := ""
		if s.ResourceClass == "go" {
			scratch = "/workspace/.gotmp"
		}
		if s.ResourceClass == "python" {
			scratch = "/workspace/.pytmp"
		}
		if scratch != "" && !c.Witness.ExecutableScratch(scratch) {
			return zero, refusal(4, "executable scratch proof unavailable: "+scratch)
		}
	}
	for i, s := range m.Shards {
		if e := checkTaskPaths(src.BridgeDir, src.Attempt, c.Registrations[i], s.ExpectedOutputs); e != nil {
			return zero, e
		}
	}
	if src.WaveOpen || !src.SeatReady || src.InFlight == src.MaxInFlight {
		return zero, refusal(6, "EN_DUDA: wave open, seat unavailable/refused, or no credits")
	}
	free := src.MaxInFlight - src.InFlight
	if free > MaxSeatExecutions-src.InFlight {
		free = MaxSeatExecutions - src.InFlight
	}
	result := Result{RequestedShards: len(m.Shards), MaxConcurrentSeats: MaxSeatExecutions}
	result.Manifest, e = json.Marshal(m)
	if e != nil {
		return zero, refusal(2, "manifest encoding")
	}
	for i, s := range m.Shards {
		if i >= free {
			result.PendingShardIDs = append(result.PendingShardIDs, s.ShardID)
			continue
		}
		r := c.Registrations[i]
		queue := []byte(r.TaskID + "\t" + r.BriefPath + "\t" + r.OutputPath + "\n")
		one := src
		one.GateArgv = [][]string{append([]string(nil), s.GateArgv...)}
		wave, frozen, err := musesource.Build(queue, one)
		if err != nil {
			return zero, err
		}
		if err = musesource.ValidateWave(wave); err != nil {
			return zero, refusal(5, "source item failed strict validation")
		}
		inv := Invocation{Argv: append([]string(nil), s.GateArgv...)}
		if s.ResourceClass == "go" {
			inv.Prepare = [][]string{{"mkdir", "-p", "/workspace/.gotmp"}}
			inv.Env = []string{"GOTMPDIR=/workspace/.gotmp"}
		}
		if s.ResourceClass == "python" {
			inv.Prepare = [][]string{{"mkdir", "-p", "/workspace/.pytmp"}}
			inv.Env = []string{"TMPDIR=/workspace/.pytmp"}
		}
		result.Items = append(result.Items, Item{ShardID: s.ShardID, TaskID: r.TaskID, Queue: queue, SourceItems: wave, SourceManifest: frozen, Invocation: inv})
	}
	result.EmittedCount = len(result.Items)
	return result, nil
}
func validDecimal(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

// VerifyManifest performs the strict schema, field-seal and manifest-seal checks
// without I/O or trusted host access. It is suitable for an A-manifest boundary.
func VerifyManifest(raw []byte) (Manifest, error) { return decodeManifest(raw) }

// SealManifest is a producer helper for already validated typed inventories.
// It does not bless source bytes, durations, registrations or capabilities;
// Compile always independently checks them again from raw pins.
func SealManifest(m Manifest) ([]byte, error) {
	if len(m.Shards) == 0 {
		return nil, refusal(2, "empty shard inventory")
	}
	for i := range m.Shards {
		s := &m.Shards[i]
		b, e := json.Marshal(s)
		if e != nil {
			return nil, e
		}
		v, e := workerbridge.Parse(b)
		if e != nil {
			return nil, e
		}
		delete(v, "field_sha256")
		seals := make(map[string]string, len(v))
		for key, value := range v {
			digest, err := sortedDigest(value)
			if err != nil {
				return nil, err
			}
			seals[key] = digest
		}
		s.FieldSHA256 = seals
	}
	m.ManifestSHA256 = ""
	b, e := json.Marshal(m)
	if e != nil {
		return nil, e
	}
	v, e := workerbridge.Parse(b)
	if e != nil {
		return nil, e
	}
	delete(v, "manifest_sha256")
	m.ManifestSHA256, e = sortedDigest(v)
	if e != nil {
		return nil, e
	}
	result, e := json.Marshal(m)
	if e != nil {
		return nil, e
	}
	if _, e = VerifyManifest(result); e != nil {
		return nil, fmt.Errorf("sealed manifest invalid: %w", e)
	}
	return result, nil
}
