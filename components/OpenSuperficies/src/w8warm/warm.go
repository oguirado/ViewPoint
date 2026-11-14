// Package w8warm plans an operator-gated dependency cache warm and independently
// measures its contents. It never publishes an unmeasured cache receipt.
package w8warm

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

const Volume = "os_worker_cache"
const ReadonlyMount = "type=volume,src=os_worker_cache,dst=/opt/cache,readonly"
const writableMount = "type=volume,src=os_worker_cache,dst=/opt/cache"
const seccomp = "/usr/local/share/opensuperficies/w8-seccomp.json"
const maxListing = 4 << 20

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

func reject(code int, class, detail string) error {
	return &workerbridge.Reject{Code: code, Class: class, Detail: detail}
}
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
func hash(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

// Recipe is the closed os.cu.warm-manifest/v1 document. The fill instruction
// is a fixed policy marker, not arbitrary shell code.
type Recipe struct {
	Schema              string            `json:"schema"`
	Repo                string            `json:"repo"`
	Volume              string            `json:"volume"`
	Target              string            `json:"target"`
	TaskReadOnly        bool              `json:"task_read_only"`
	Fill                string            `json:"fill"`
	Network             string            `json:"network"`
	OfflineEnv          map[string]string `json:"offline_env"`
	Toolchains          []string          `json:"toolchains"`
	CachePaths          []string          `json:"cache_paths"`
	CacheMissRC         int               `json:"cache_miss_rc"`
	ToolchainPinsSHA256 string            `json:"toolchain_pins_sha256"`
	Lockfiles           []Lockfile        `json:"lockfiles"`
}
type Lockfile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

// BaseImage must originate in a checked final base_image_records.jsonl entry.
// Approved additionally records the current operator's explicit warm-job approval.
type BaseImage struct {
	Digest       string
	RecipeSHA256 string
	Approved     bool
	WorkerCPUs   string
}

// RulesRecord represents an operator-applied and verified br-osw ruleset.
type RulesRecord struct {
	RulesSHA256  string `json:"rules_sha256"`
	VerifySHA256 string `json:"verify_sha256"`
}

func ParseRecipe(b []byte) (Recipe, error) {
	var r Recipe
	if _, e := workerbridge.Parse(b); e != nil {
		return r, reject(2, "input", "warm recipe JSON")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e := d.Decode(&r); e != nil {
		return r, reject(2, "input", "warm recipe fields")
	}
	if e := r.validate(); e != nil {
		return r, e
	}
	return r, nil
}
func ParseRules(b []byte) (RulesRecord, error) {
	var r RulesRecord
	if _, e := workerbridge.Parse(b); e != nil {
		return r, reject(5, "verification", "rules JSON")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e := d.Decode(&r); e != nil {
		return r, reject(5, "verification", "rules shape")
	}
	if !digestPattern.MatchString(r.RulesSHA256) || !digestPattern.MatchString(r.VerifySHA256) {
		return r, reject(5, "verification", "operator rules pins")
	}
	return r, nil
}

// BaseFromLedger reads only the final checked line, never an earlier or
// unverified image. The operator approval is not inferred from the ledger.
func BaseFromLedger(b []byte) (BaseImage, error) {
	var base BaseImage
	if len(b) == 0 || len(b) > 4<<20 || b[len(b)-1] != '\n' {
		return base, reject(4, "unavailable", "base ledger incomplete")
	}
	lines := bytes.Split(bytes.TrimSuffix(b, []byte{'\n'}), []byte{'\n'})
	last := lines[len(lines)-1]
	if e := w8builder.CheckRecord(last); e != nil {
		return base, reject(5, "verification", "last base record invalid")
	}
	m, e := workerbridge.Parse(last)
	if e != nil {
		return base, reject(5, "verification", "base record JSON")
	}
	digest, _ := m["image_digest"].(string)
	recipe, _ := m["recipe_sha256"].(string)
	if !digestPattern.MatchString(digest) || !digestPattern.MatchString(recipe) {
		return base, reject(5, "verification", "base digest format")
	}
	base.Digest = digest
	base.RecipeSHA256 = recipe
	return base, nil
}

var allowedEnv = map[string]string{
	"LC_ALL": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "TINI_SUBREAPER": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true", "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "GOMODCACHE": "/opt/cache/gomod", "GOCACHE": "/workspace/.cache/go-build", "CARGO_HOME": "/opt/cache/cargo", "CARGO_NET_OFFLINE": "true", "PIP_NO_INDEX": "1", "HOME": "/home/worker", "TMPDIR": "/tmp",
}

func validRelative(s string) bool {
	if s == "" || strings.HasPrefix(s, "/") || strings.ContainsAny(s, "\\\x00\t\n\r") || !utf8.ValidString(s) || path.Clean(s) != s {
		return false
	}
	for _, p := range strings.Split(s, "/") {
		if p == "." || p == ".." || p == "" {
			return false
		}
	}
	return true
}
func (r Recipe) validate() error {
	if r.Schema != "os.cu.warm-manifest/v1" || r.Repo != "opensuperficies" || r.Volume != Volume || r.Target != "/opt/cache" || !r.TaskReadOnly || r.Fill != "operator-approved-warm-job" || r.Network != "operator-host-rules-only" || r.CacheMissRC != 4 || !digestPattern.MatchString(r.ToolchainPinsSHA256) {
		return reject(2, "input", "warm recipe contract")
	}
	if len(r.OfflineEnv) != len(allowedEnv) {
		return reject(5, "containment", "offline env allowlist")
	}
	for k, v := range r.OfflineEnv {
		if a, ok := allowedEnv[k]; !ok || a != v {
			return reject(5, "containment", "offline env value")
		}
	}
	if len(r.Toolchains) == 0 || len(r.Toolchains) > 64 || len(r.CachePaths) == 0 || len(r.CachePaths) > 64 || len(r.Lockfiles) == 0 || len(r.Lockfiles) > 64 {
		return reject(2, "input", "warm recipe collections")
	}
	seen := map[string]bool{}
	for _, t := range r.Toolchains {
		if t == "" || len(t) > 80 || strings.ContainsAny(t, " \t\r\n\x00") || seen[t] {
			return reject(2, "input", "toolchain token")
		}
		seen[t] = true
	}
	seen = map[string]bool{}
	for _, p := range r.CachePaths {
		if !strings.HasPrefix(p, "/opt/cache/") || path.Clean(p) != p || !validRelative(strings.TrimPrefix(p, "/opt/cache/")) || seen[p] {
			return reject(5, "containment", "cache path")
		}
		seen[p] = true
	}
	for _, a := range r.CachePaths {
		for _, b := range r.CachePaths {
			if a != b && strings.HasPrefix(a, b+"/") {
				return reject(5, "containment", "overlapping cache paths")
			}
		}
	}
	seen = map[string]bool{}
	for _, l := range r.Lockfiles {
		if !validRelative(l.Path) || !digestPattern.MatchString(l.SHA256) || seen[l.Path] {
			return reject(2, "input", "lockfile pin")
		}
		seen[l.Path] = true
	}
	return nil
}

// JobPlan is the concrete immutable-by-verification output of Plan.
// Go's identifier namespace does not permit both a type and function named Plan.
type JobPlan struct {
	Fill       []string
	Measure    []string
	CachePaths []string
	recipe     Recipe
	base       BaseImage
	rules      RulesRecord
}

// Plan builds fixed argv vectors; it never executes a process. A missing rules
// record cannot produce even a fill argv, and no unapproved base can run.
func Plan(r Recipe, base BaseImage, rules RulesRecord) (JobPlan, error) {
	var p JobPlan
	if e := r.validate(); e != nil {
		return p, e
	}
	if !base.Approved {
		return p, reject(7, "operator_capability", "explicit warm-job approval missing")
	}
	if base.Digest == "" {
		return p, reject(4, "unavailable", "base image missing")
	}
	if !digestPattern.MatchString(base.Digest) || !digestPattern.MatchString(base.RecipeSHA256) {
		return p, reject(5, "verification", "approved base digest")
	}
	if rules.RulesSHA256 == "" || rules.VerifySHA256 == "" {
		return p, reject(4, "unavailable", "operator host rules unavailable")
	}
	if !digestPattern.MatchString(rules.RulesSHA256) || !digestPattern.MatchString(rules.VerifySHA256) {
		return p, reject(5, "verification", "operator host rules invalid")
	}
	cpus := base.WorkerCPUs
	if cpus == "" {
		cpus = "10-14"
	}
	if !regexp.MustCompile(`^[0-9]+(?:[-,][0-9]+)*$`).MatchString(cpus) {
		return p, reject(5, "containment", "worker CPU set")
	}
	common := []string{"docker", "run", "--rm", "--init", "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--security-opt", "seccomp=" + seccomp,
		"--tmpfs", "/workspace:rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=2g", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=256m", "--tmpfs", "/home/worker:rw,noexec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=64m", "--tmpfs", "/dev/shm:rw,noexec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=64m",
		"--cpuset-cpus", cpus, "--cpus", "2", "--cpu-shares", "128", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256", "--ulimit", "nofile=8192:8192", "--ulimit", "core=0", "--oom-score-adj", "800", "--log-driver", "none", "--restart", "no"}
	names := make([]string, 0, len(r.OfflineEnv))
	for k := range r.OfflineEnv {
		names = append(names, k)
	}
	sort.Strings(names)
	for _, k := range names {
		common = append(common, "--env", k+"="+r.OfflineEnv[k])
	}
	img := "os-worker-base@sha256:" + base.Digest
	fill := append(append([]string{}, common...), "--mount", writableMount, "--network", "br-osw", img, "/opt/w8/warm-fill")
	measure := append(append([]string{}, common...), "--mount", ReadonlyMount, "--network", "none", img, "python3", "-B", "-c", measureScript)
	measure = append(measure, r.CachePaths...)
	p = JobPlan{Fill: fill, Measure: measure, CachePaths: append([]string{}, r.CachePaths...), recipe: r, base: base, rules: rules}
	return p, nil
}

// measureScript is constant, not recipe input. It uses lstat, emits only
// regular files, and rejects unexpected filesystem objects at all depths.
const measureScript = `import hashlib,os,stat,sys
roots=sys.argv[1:]
rows=[]
for root in roots:
    if not root.startswith('/opt/cache/') or os.path.normpath(root)!=root: sys.exit(5)
    if not os.path.isdir(root) or os.path.islink(root): sys.exit(4)
    for here,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            full=os.path.join(here,name)
            st=os.lstat(full)
            if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)) or st.st_mode & 0o6000: sys.exit(5)
        for name in files:
            full=os.path.join(here,name)
            st=os.lstat(full)
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o6000: sys.exit(5)
            rel=os.path.relpath(full,'/opt/cache')
            if any(c in rel for c in '\t\n\r'): sys.exit(5)
            h=hashlib.sha256()
            with open(full,'rb') as f:
                while True:
                    data=f.read(65536)
                    if not data: break
                    h.update(data)
            rows.append((rel,st.st_size,st.st_mode & 0o777,h.hexdigest()))
for rel,size,mode,digest in sorted(rows,key=lambda a:a[0].encode('utf-8')):
    sys.stdout.write('%s\t%d\t%04o\t%s\n'%(rel,size,mode,digest))
`

// ValidateListing accepts exactly the deterministic measured text protocol.
func ValidateListing(b []byte, cachePaths []string) error {
	if len(b) == 0 {
		return reject(4, "unavailable", "empty cache listing")
	}
	if len(b) > maxListing || !utf8.Valid(b) || b[len(b)-1] != '\n' {
		return reject(5, "verification", "invalid listing bytes")
	}
	roots := map[string]bool{}
	for _, p := range cachePaths {
		roots[strings.TrimPrefix(p, "/opt/cache/")] = true
	}
	prev := ""
	for _, line := range strings.Split(strings.TrimSuffix(string(b), "\n"), "\n") {
		fields := strings.Split(line, "\t")
		if len(fields) != 4 || !validRelative(fields[0]) || (prev != "" && fields[0] <= prev) {
			return reject(5, "verification", "listing order or row")
		}
		inRoot := false
		for p := range roots {
			if fields[0] == p || strings.HasPrefix(fields[0], p+"/") {
				inRoot = true
			}
		}
		if !inRoot {
			return reject(5, "verification", "listing path outside recipe")
		}
		n, e := strconv.ParseUint(fields[1], 10, 63)
		if e != nil || strconv.FormatUint(n, 10) != fields[1] {
			return reject(5, "verification", "listing size")
		}
		mode, e := strconv.ParseUint(fields[2], 8, 16)
		if e != nil || len(fields[2]) != 4 || mode > 0777 || fmt.Sprintf("%04o", mode) != fields[2] {
			return reject(5, "verification", "listing mode")
		}
		if !digestPattern.MatchString(fields[3]) {
			return reject(5, "verification", "listing digest")
		}
		prev = fields[0]
	}
	return nil
}

type boundedWriter struct {
	bytes.Buffer
	tooLarge bool
}

func (w *boundedWriter) Write(b []byte) (int, error) {
	if w.Len()+len(b) > maxListing {
		w.tooLarge = true
		return 0, reject(5, "verification", "listing size cap")
	}
	return w.Buffer.Write(b)
}
func runOne(ctx context.Context, runner w8builder.Runner, argv []string, out io.Writer) (err error) {
	defer func() {
		if recover() != nil {
			err = reject(6, "indeterminate", "runner panic")
		}
	}()
	return runner.Run(ctx, argv, nil, out)
}
func validatePlan(p JobPlan) error {
	check, e := Plan(p.recipe, p.base, p.rules)
	if e != nil {
		return e
	}
	if !reflect.DeepEqual(check.Fill, p.Fill) || !reflect.DeepEqual(check.Measure, p.Measure) || !reflect.DeepEqual(check.CachePaths, p.CachePaths) {
		return reject(5, "containment", "argv not the approved plan")
	}
	for _, argv := range [][]string{p.Fill, p.Measure} {
		for _, a := range argv {
			if a == "--privileged" || a == "--env-file" || a == "--volume" || a == "-v" || strings.Contains(a, "docker.sock") || strings.HasPrefix(a, "type=bind,") {
				return reject(5, "containment", "forbidden launch argument")
			}
		}
	}
	return nil
}

// Run fills once, then measures twice with network none. It never writes a
// manifest; callers may publish only after a nil error and stable digest.
func Run(ctx context.Context, runner w8builder.Runner, plan JobPlan) ([]byte, string, error) {
	if e := validatePlan(plan); e != nil {
		return nil, "", e
	}
	if runner == nil {
		return nil, "", reject(2, "input", "missing runner")
	}
	if e := ctx.Err(); e != nil {
		return nil, "", reject(6, "indeterminate", "context cancelled")
	}
	if e := runOne(ctx, runner, plan.Fill, io.Discard); e != nil {
		if ctx.Err() != nil {
			return nil, "", reject(6, "indeterminate", "interrupted fill")
		}
		return nil, "", reject(3, "execution", "fill failed")
	}
	if ctx.Err() != nil {
		return nil, "", reject(6, "indeterminate", "interrupted fill")
	}
	var result []byte
	var previous string
	for i := 0; i < 2; i++ {
		w := &boundedWriter{}
		if e := runOne(ctx, runner, plan.Measure, w); e != nil {
			if ctx.Err() != nil {
				return nil, "", reject(6, "indeterminate", "interrupted measure")
			}
			if w.tooLarge {
				return nil, "", reject(5, "verification", "listing cap")
			}
			return nil, "", reject(3, "execution", "measure failed")
		}
		if ctx.Err() != nil {
			return nil, "", reject(6, "indeterminate", "interrupted measure")
		}
		b := w.Bytes()
		if e := ValidateListing(b, plan.CachePaths); e != nil {
			return nil, "", e
		}
		h := hash(b)
		if i > 0 && h != previous {
			return nil, "", reject(6, "indeterminate", "unstable second measurement")
		}
		previous = h
		result = append([]byte(nil), b...)
	}
	return result, previous, nil
}

// CheckOutputPath refuses existing unsafe targets before any warm execution.
func CheckOutputPath(name string) error {
	if name == "" {
		return reject(2, "input", "manifest path required")
	}
	st, e := os.Lstat(name)
	if errors.Is(e, os.ErrNotExist) {
		return nil
	}
	if e != nil {
		return reject(5, "verification", "manifest target lstat")
	}
	if !st.Mode().IsRegular() || st.Mode().Perm()&0022 != 0 {
		return reject(5, "verification", "unsafe manifest target")
	}
	return nil
}

// WriteSeatManifest publishes exactly the canonical three-key seat record.
// Staging, sync and chmod precede rename; a failed stage leaves no partial file.
func WriteSeatManifest(name, sha string) error {
	if !digestPattern.MatchString(sha) {
		return reject(2, "input", "manifest digest must be lowercase hex64")
	}
	if e := CheckOutputPath(name); e != nil {
		return e
	}
	data, e := workerbridge.Canonical(map[string]any{"volume": Volume, "read_only": true, "manifest_sha256": sha})
	if e != nil || len(data) > 4096 {
		return reject(5, "verification", "canonical manifest")
	}
	dir := filepath.Dir(name)
	st, e := os.Lstat(dir)
	if e != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 {
		return reject(5, "containment", "unsafe manifest directory")
	}
	f, e := os.CreateTemp(dir, ".w8-warm-")
	if e != nil {
		return reject(3, "execution", "manifest temp create")
	}
	published := false
	defer func() {
		_ = f.Close()
		if !published {
			_ = os.Remove(f.Name())
		}
	}()
	if e = f.Chmod(0644); e != nil {
		return reject(3, "execution", "manifest chmod")
	}
	if n, err := f.Write(data); err != nil || n != len(data) {
		return reject(3, "execution", "manifest write")
	}
	if e = f.Sync(); e != nil {
		return reject(3, "execution", "manifest sync")
	}
	if e = f.Close(); e != nil {
		return reject(3, "execution", "manifest close")
	}
	if e = CheckOutputPath(name); e != nil {
		return e
	}
	if e = os.Rename(f.Name(), name); e != nil {
		return reject(3, "execution", "manifest rename")
	}
	published = true
	d, e := os.Open(dir)
	if e != nil {
		return reject(6, "indeterminate", "manifest directory open after rename")
	}
	defer d.Close()
	if e = d.Sync(); e != nil {
		return reject(6, "indeterminate", "manifest directory sync after rename")
	}
	return nil
}
