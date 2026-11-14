package w8builder

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

type BuildConfig struct {
	RecipeDir, ApprovalPath, StateDir string
	Context                           io.Reader
	Runner                            Runner
	Repositories                      []RepositoryPin
	// Tests in this package may inject verified bytes; production callers cannot.
	approvalRead func(string) ([]byte, error)
}

func tarContext(files recipe) []byte {
	var b bytes.Buffer
	tw := tar.NewWriter(&b)
	names := make([]string, 0, len(files))
	for n := range files {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		_ = tw.WriteHeader(&tar.Header{Name: n, Mode: 0644, Size: int64(len(files[n]))})
		_, _ = tw.Write(files[n])
	}
	_ = tw.Close()
	return b.Bytes()
}

// Build consumes approval before invoking any build, hashes two streamed OCI
// exports, probes and scans in RAM, then publishes only a validated record.
func Build(ctx context.Context, cfg BuildConfig) (rec map[string]any, err error) {
	defer func() {
		if recover() != nil {
			rec = nil
			err = refusal(6, "indeterminate", "builder panic; quarantine state")
		}
	}()
	ctx, cancel := context.WithTimeout(ctx, 60*time.Minute)
	defer cancel()
	r := cfg.Runner
	if r == nil {
		r = ExecRunner{}
	}
	// Production state and approval locations are fixed by the account database.
	// In particular HOME or --state-dir cannot create a fresh replay ledger.
	switch r.(type) {
	case ExecRunner, *ExecRunner:
		u, e := user.LookupId(strconv.Itoa(os.Geteuid()))
		if e != nil || u.Username != "osworker" || os.Geteuid() == 0 {
			return nil, refusal(5, "containment", "builder must run as dedicated osworker")
		}
		state := filepath.Join(u.HomeDir, ".local/state/w8")
		if filepath.Clean(cfg.StateDir) != state {
			return nil, refusal(5, "containment", "canonical osworker state directory required")
		}
		if cfg.ApprovalPath != "" && cfg.ApprovalPath != DefaultApprovalPath {
			return nil, refusal(7, "operator_recipe", "canonical operator approval path required")
		}
		expected := "unix:///run/user/" + strconv.Itoa(os.Geteuid()) + "/docker.sock"
		if os.Getenv("DOCKER_HOST") != expected {
			return nil, refusal(5, "containment", "dedicated rootless socket required")
		}
	}

	var files recipe
	if cfg.Context != nil {
		files, err = loadTar(cfg.Context)
	} else if cfg.RecipeDir != "" {
		files, err = loadDir(cfg.RecipeDir)
	} else {
		return nil, refusal(2, "usage", "recipe context required")
	}
	if err != nil {
		return nil, err
	}
	if err = lintRecipe(files); err != nil {
		return nil, err
	}
	if e := validateRepositoryPins(cfg.Repositories); e != nil {
		return nil, e
	}
	recipeSHA := digest(files["base/recipe.manifest"])
	lock, e := acquireLock(cfg.StateDir)
	if e != nil {
		return nil, e
	}
	defer lock.close()
	approvalPath := cfg.ApprovalPath
	if approvalPath == "" {
		approvalPath = DefaultApprovalPath
	}
	readApproval := cfg.approvalRead
	if readApproval == nil {
		readApproval = openApproval
	}
	ab, e := readApproval(approvalPath)
	if e != nil {
		return nil, e
	}
	_, approvalSHA, e := lock.consume(ab, recipeSHA)
	if e != nil {
		return nil, e
	}
	identities, e := collectRepoIdentity(ctx, r, cfg.Repositories, files)
	if e != nil {
		return nil, e
	}
	policy := ScanPolicy{Identity: identities}
	for _, pin := range cfg.Repositories {
		if pin.Workspace != "" {
			policy.Workspaces = append(policy.Workspaces, pin.Workspace)
		}
	}
	info, e := capture(ctx, r, "docker", "info", "--format", "{{json .}}")
	if e != nil {
		return nil, refusal(4, "unavailable", "daemon unreachable")
	}
	var daemon struct {
		SecurityOptions []string   `json:"SecurityOptions"`
		DriverStatus    [][]string `json:"DriverStatus"`
		CgroupVersion   string     `json:"CgroupVersion"`
	}
	if e = json.Unmarshal(info, &daemon); e != nil {
		return nil, refusal(5, "containment", "daemon info invalid")
	}
	rootless := false
	for _, s := range daemon.SecurityOptions {
		if s == "name=rootless" || s == "rootless" {
			rootless = true
		}
	}
	containerd := false
	for _, pair := range daemon.DriverStatus {
		if len(pair) == 2 && pair[0] == "driver-type" && strings.Contains(pair[1], "containerd") {
			containerd = true
		}
	}
	if !rootless || daemon.CgroupVersion != "2" || !containerd {
		return nil, refusal(5, "containment", "rootless containerd cgroup-v2 daemon required")
	}
	if _, e = capture(ctx, r, "docker", "buildx", "version"); e != nil {
		return nil, refusal(4, "unavailable", "buildx missing")
	}
	previous := ""
	old, e := capture(ctx, r, "docker", "image", "ls", "os-worker-base:current", "--no-trunc", "--quiet")
	if e != nil {
		return nil, refusal(4, "unavailable", "current image inventory unavailable")
	}
	previous = strings.TrimSpace(string(old))
	if previous != "" && !imageID(previous) {
		return nil, refusal(5, "verification", "previous image ID")
	}
	existingRaw, e := capture(ctx, r, "docker", "image", "ls", "os-worker-base", "--no-trunc", "--quiet")
	if e != nil {
		return nil, refusal(4, "unavailable", "base image inventory unavailable")
	}
	existing := map[string]bool{}
	for _, id := range strings.Fields(string(existingRaw)) {
		if !imageID(id) {
			return nil, refusal(5, "verification", "base image inventory ID")
		}
		existing[id] = true
	}
	if previous != "" && !existing[previous] {
		return nil, refusal(6, "indeterminate", "inconsistent image inventory")
	}

	pins, _ := parseObject(files["pins.json"])
	epoch := string(pins["source_date_epoch"].(json.Number))
	buildArgs := []string{"docker", "buildx", "build", "--no-cache", "--platform", "linux/arm64", "--provenance=false", "--sbom=false", "--build-arg", "SOURCE_DATE_EPOCH=" + epoch, "--file", "base/Dockerfile", "--output", "type=oci,rewrite-timestamp=true,dest=-", "-"}
	contextBytes := tarContext(files)
	first, e := streamBuild(ctx, r, buildArgs, contextBytes, true)
	if e != nil {
		if first.Config != "" && !existing["sha256:"+first.Config] {
			cleanupCtx, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
			_, ce := capture(cleanupCtx, r, "docker", "image", "rm", "--force", "sha256:"+first.Config)
			stop()
			if ce != nil {
				return nil, refusal(6, "indeterminate", "partial import cleanup failed; quarantine")
			}
		}
		return nil, e
	}
	candidate := "sha256:" + first.Config
	// With the containerd image store (required above) a loaded image is addressed by its
	// manifest digest, not its config digest; fall back to the manifest when the config ID is unknown.
	if _, ie := capture(ctx, r, "docker", "image", "inspect", candidate, "--format", "{{.Id}}"); ie != nil && first.Manifest != "" {
		if _, me := capture(ctx, r, "docker", "image", "inspect", "sha256:"+first.Manifest, "--format", "{{.Id}}"); me == nil {
			candidate = "sha256:" + first.Manifest
		}
	}
	published := false
	tagged := false
	defer func() {
		if !published {
			if tagged && previous != "" && previous != candidate {
				restoreCtx, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
				_, restoreErr := capture(restoreCtx, r, "docker", "image", "tag", previous, "os-worker-base:current")
				stop()
				if restoreErr != nil {
					rec = nil
					err = refusal(6, "indeterminate", "publication rollback failed; quarantine")
					return
				}
			}
			if existing[candidate] {
				return
			}
			cleanupCtx, stop := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
			defer stop()
			if _, ce := capture(cleanupCtx, r, "docker", "image", "rm", "--force", candidate); ce != nil {
				rec = nil
				err = refusal(6, "indeterminate", "candidate cleanup failed; quarantine")
			}
		}
	}()
	second, e := streamBuild(ctx, r, buildArgs, contextBytes, false)
	if e != nil {
		return nil, e
	}
	if first != second {
		return nil, refusal(5, "verification", "OCI manifest/config differ between no-cache builds")
	}
	conf, e := capture(ctx, r, "docker", "image", "inspect", candidate, "--format", "{{json .Config}}")
	if e != nil {
		return nil, refusal(3, "execution", "candidate inspect failed")
	}
	var config struct {
		User       string   `json:"User"`
		Entrypoint []string `json:"Entrypoint"`
	}
	if e = json.Unmarshal(conf, &config); e != nil || config.User != "10001:10001" || strings.Join(config.Entrypoint, "\x00") != "/usr/bin/tini\x00-s\x00--" {
		return nil, refusal(5, "verification", "candidate runtime configuration")
	}
	var nonce [6]byte
	if _, e = rand.Read(nonce[:]); e != nil {
		return nil, refusal(6, "indeterminate", "container name entropy")
	}
	container := hex.EncodeToString(nonce[:])
	argv := ProbeArgv(candidate, container)
	if e = ValidateProbeArgv(argv, candidate, container); e != nil {
		return nil, e
	}
	output := &limitedOutput{cap: 4096}
	if e = JournalCreate(ctx, r, CapsuleJournal{Kind: "probe", Lease: "-", Image: first.Manifest, Container: container, CallerUID: os.Geteuid()}, argv, nil, output); e != nil {
		if ExitCode(e) == 4 {
			return nil, e
		}
		return nil, refusal(3, "execution", "identity/assertion probe failed")
	}
	if !regexp.MustCompile(`(?:^|\s)uid=10001(?:\(|\s)`).Match(output.data) || !regexp.MustCompile(`(?:^|\s)gid=10001(?:\(|\s)`).Match(output.data) {
		return nil, refusal(5, "verification", "probe uid/gid mismatch")
	}
	if e = streamScan(ctx, r, candidate, policy); e != nil {
		return nil, e
	}
	rec = newRecord(recipeSHA, first.Manifest, approvalSHA)
	rec["repo_identity"] = map[string]any{"size": identities.Size(), "sha256": identities.Digest()}
	encoded, _ := json.Marshal(rec)
	if e = CheckRecord(encoded); e != nil {
		return nil, e
	}
	// Keep the rollback image tagged: on the containerd store an untagged image drops out of
	// 'docker image ls os-worker-base' and would never be pruned (one leaked image per publish).
	oldPrevRaw, e := capture(ctx, r, "docker", "image", "ls", "os-worker-base:previous", "--no-trunc", "--quiet")
	if e != nil {
		return nil, refusal(4, "unavailable", "previous image inventory unavailable")
	}
	oldPrev := strings.TrimSpace(string(oldPrevRaw))
	if previous != "" && previous != candidate {
		if _, e = capture(ctx, r, "docker", "image", "tag", previous, "os-worker-base:previous"); e != nil {
			return nil, refusal(6, "indeterminate", "previous tag failed")
		}
	}
	if _, e = capture(ctx, r, "docker", "image", "tag", candidate, "os-worker-base:current"); e != nil {
		return nil, refusal(6, "indeterminate", "tag publish failed")
	}
	if oldPrev != "" && imageID(oldPrev) && oldPrev != candidate && oldPrev != previous {
		if _, e = capture(ctx, r, "docker", "image", "rm", "--force", oldPrev); e != nil {
			return nil, refusal(6, "indeterminate", "superseded previous prune failed")
		}
	}
	tagged = true
	if e = retainImages(ctx, r, candidate, previous); e != nil {
		return nil, e
	}
	if e = lock.record(rec); e != nil {
		return nil, e
	}
	published = true
	return rec, nil
}
func streamBuild(ctx context.Context, r Runner, argv []string, contextBytes []byte, load bool) (ociResult, error) {
	// The loader keeps the caller's context: an OCI verification failure cancels only the build, and the import is
	// allowed to finish so its printed image ID can be removed (a killed load leaves an unidentifiable partial import).
	loadCtx := ctx
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	rd, wr := io.Pipe()
	defer rd.Close()
	done := make(chan error, 1)
	go func() {
		e := asyncRun(ctx, r, argv, bytes.NewReader(contextBytes), wr)
		_ = wr.CloseWithError(e)
		done <- e
	}()
	sourceCount := countReader{r: rd}
	var reader io.Reader = &sourceCount
	var teeCount countWriter
	var loadWr *io.PipeWriter
	var loadDone chan error
	loadOutput := &limitedOutput{cap: 64 << 10}
	if load {
		loadRd, lw := io.Pipe()
		loadWr = lw
		loadDone = make(chan error, 1)
		go func() {
			e := asyncRun(loadCtx, r, []string{"docker", "load"}, loadRd, loadOutput)
			_ = loadRd.CloseWithError(e)
			loadDone <- e
		}()
		teeCount.w = lw
		reader = io.TeeReader(&sourceCount, &teeCount)
	}
	result, scanErr := hashOCI(reader)
	if scanErr != nil {
		cancel()
		_ = rd.CloseWithError(scanErr)
	}
	if loadWr != nil {
		// A complete stream imports cleanly; a truncated one makes docker load fail without an image.
		_ = loadWr.Close()
	}
	var buildErr error
	select {
	case buildErr = <-done:
	case <-ctx.Done():
		buildErr = ctx.Err()
	}
	var loadErr error
	if loadDone != nil {
		select {
		case loadErr = <-loadDone:
		case <-loadCtx.Done():
			loadErr = loadCtx.Err()
		}
	}
	if indeterminate(buildErr) {
		return result, buildErr
	}
	if indeterminate(loadErr) {
		return result, loadErr
	}
	if scanErr != nil {
		// The loader can have imported a candidate before an OCI verification failure.
		// Only a full loaded ID is eligible for cleanup; never guess from a tag.
		loaded := regexp.MustCompile(`(?m)^Loaded image ID: sha256:([a-f0-9]{64})$`).FindSubmatch(loadOutput.data)
		if len(loaded) == 2 {
			result.Config = string(loaded[1])
		} else if load && teeCount.n > 0 {
			return result, refusal(6, "indeterminate", "partial OCI import has no verified image ID; quarantine")
		}
		if buildErr != nil && sourceCount.n == 0 {
			return result, refusal(3, "execution", "build failed")
		}
		return result, scanErr
	}
	if buildErr != nil || loadErr != nil {
		return result, refusal(3, "execution", "build/load failed")
	}
	return result, nil
}
func streamScan(ctx context.Context, r Runner, candidate string, policy ScanPolicy) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	rd, wr := io.Pipe()
	defer rd.Close()
	done := make(chan error, 1)
	go func() {
		e := asyncRun(ctx, r, []string{"docker", "save", candidate}, nil, wr)
		_ = wr.CloseWithError(e)
		done <- e
	}()
	e := ScanLayers(rd, ScanByteCap, policy)
	if e != nil {
		cancel()
		_ = rd.CloseWithError(e)
	}
	select {
	case runErr := <-done:
		if indeterminate(runErr) {
			return runErr
		}
		if e != nil {
			return e
		}
		if runErr != nil {
			return refusal(5, "verification", "image scan stream failed")
		}
		return nil
	case <-ctx.Done():
		if e != nil {
			return e
		}
		return refusal(6, "indeterminate", "scan cancellation")
	}
}

// countWriter tracks bytes forwarded to the loader without buffering them.
type countWriter struct {
	w io.Writer
	n int64
}

func (w *countWriter) Write(p []byte) (int, error) {
	n, e := w.w.Write(p)
	w.n += int64(n)
	return n, e
}

type countReader struct {
	r io.Reader
	n int64
}

func (r *countReader) Read(p []byte) (int, error) { n, e := r.r.Read(p); r.n += int64(n); return n, e }
