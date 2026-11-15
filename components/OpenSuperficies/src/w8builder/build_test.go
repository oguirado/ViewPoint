package w8builder

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeRunner struct {
	mu                                         sync.Mutex
	calls                                      [][]string
	journal                                    []string
	buildCount                                 int
	different, journalFail, buildFail, rootful bool
	containerdIDs                              bool
	entered, release                           chan struct{}
	images                                     []string
	scanFiles                                  map[string][]byte
}

func fixtureOCI(t *testing.T, marker int) ([]byte, string) {
	t.Helper()
	config, _ := json.Marshal(map[string]any{"architecture": "arm64", "os": "linux", "config": map[string]any{"User": "10001:10001", "Entrypoint": []string{"/usr/bin/tini", "-s", "--"}}, "fixture_marker": marker})
	configSHA := digest(config)
	layer := []byte("test fixture layer")
	layerSHA := digest(layer)
	manifest, _ := json.Marshal(map[string]any{"schemaVersion": 2, "config": map[string]any{"digest": "sha256:" + configSHA, "size": len(config)}, "layers": []any{map[string]any{"digest": "sha256:" + layerSHA, "size": len(layer)}}})
	manifestSHA := digest(manifest)
	index, _ := json.Marshal(map[string]any{"schemaVersion": 2, "manifests": []any{map[string]any{"digest": "sha256:" + manifestSHA, "size": len(manifest), "platform": map[string]any{"architecture": "arm64", "os": "linux"}}}})
	return testTar(t, map[string][]byte{"oci-layout": []byte(`{"imageLayoutVersion":"1.0.0"}`), "index.json": index, "blobs/sha256/" + configSHA: config, "blobs/sha256/" + manifestSHA: manifest, "blobs/sha256/" + layerSHA: layer}), "sha256:" + configSHA
}

type testRunner struct {
	*fakeRunner
	t *testing.T
}

func (f *testRunner) Run(ctx context.Context, args []string, in io.Reader, out io.Writer) error {
	f.mu.Lock()
	f.calls = append(f.calls, append([]string{}, args...))
	f.mu.Unlock()
	s := strings.Join(args, " ")
	switch {
	case args[0] == "git":
		b, e := os.ReadFile("../../testdata/w8builder/repo-identity.tree")
		if e != nil {
			return e
		}
		_, e = out.Write(b)
		return e
	case args[0] == "systemd-cat":
		b, e := io.ReadAll(in)
		if e != nil {
			return e
		}
		if f.journalFail {
			return errors.New("journal unavailable")
		}
		f.mu.Lock()
		f.journal = append(f.journal, string(b))
		f.mu.Unlock()
		return nil
	case strings.HasPrefix(s, "docker info"):
		rootless := `["name=rootless"]`
		if f.rootful {
			rootless = `[]`
		}
		_, e := io.WriteString(out, `{"SecurityOptions":`+rootless+`,"DriverStatus":[["driver-type","io.containerd.snapshotter.v1"]],"CgroupVersion":"2"}`)
		return e
	case s == "docker buildx version":
		_, e := io.WriteString(out, "buildx test fixture")
		return e
	case strings.HasPrefix(s, "docker buildx build"):
		f.mu.Lock()
		f.buildCount++
		n := f.buildCount
		f.mu.Unlock()
		if f.entered != nil && n == 1 {
			close(f.entered)
			select {
			case <-f.release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		if f.buildFail {
			return errors.New("fixture build failure")
		}
		_, e := io.Copy(io.Discard, in)
		if e != nil {
			return e
		}
		marker := 0
		if f.different && n == 2 {
			marker = 1
		}
		b, id := fixtureOCI(f.t, marker)
		if f.containerdIDs {
			if m, me := hashOCI(bytes.NewReader(b)); me == nil {
				id = "sha256:" + m.Manifest
			}
		}
		if n == 1 {
			f.mu.Lock()
			f.images = []string{id}
			f.mu.Unlock()
		}
		_, e = out.Write(b)
		return e
	case s == "docker load":
		_, e := io.Copy(io.Discard, in)
		return e
	case strings.HasPrefix(s, "docker image inspect os-worker-base:current"):
		return errors.New("fixture has no previous image")
	case strings.HasPrefix(s, "docker image inspect "):
		f.mu.Lock()
		configRef := strings.HasPrefix(args[3], "sha256:") && !containsString(f.images, args[3])
		f.mu.Unlock()
		if f.containerdIDs && configRef {
			return errors.New("containerd store: no image with that config ID")
		}
		_, e := io.WriteString(out, `{"User":"10001:10001","Entrypoint":["/usr/bin/tini","-s","--"]}`)
		return e
	case strings.HasPrefix(s, "docker run "):
		_, e := io.WriteString(out, "uid=10001(worker) gid=10001(worker) groups=10001(worker)\n")
		return e
	case strings.HasPrefix(s, "docker save "):
		files := f.scanFiles
		if files == nil {
			files = map[string][]byte{"usr/bin/tool": []byte("fixture safe")}
		}
		layer := testTar(f.t, files)
		_, e := out.Write(testTar(f.t, map[string][]byte{"layer/layer.tar": layer}))
		return e
	case strings.HasPrefix(s, "docker image tag "):
		return nil
	case strings.HasPrefix(s, "docker image ls "):
		f.mu.Lock()
		defer f.mu.Unlock()
		_, e := io.WriteString(out, strings.Join(f.images, "\n"))
		return e
	case strings.HasPrefix(s, "docker image rm --force "):
		id := args[len(args)-1]
		f.mu.Lock()
		defer f.mu.Unlock()
		var keep []string
		for _, v := range f.images {
			if v != id {
				keep = append(keep, v)
			}
		}
		f.images = keep
		return nil
	default:
		return errors.New("unexpected fake argv: " + s)
	}
}
func buildFixture(t *testing.T) (BuildConfig, *testRunner) {
	t.Helper()
	files, e := loadDir("../../.wavemind/env")
	if e != nil {
		t.Fatal(e)
	}
	if e = lintRecipe(files); e != nil {
		t.Fatal(e)
	}
	sha := digest(files["base/recipe.manifest"])
	b, _ := json.Marshal(approval{Schema: "os.cu.builder-approval/v1", Mode: "per_build", Image: "os-worker-base", Arch: "aarch64", RecipeSHA256: sha, ApprovalID: "b2-0001", OperatorNote: "unit fixture"})
	r := &testRunner{fakeRunner: &fakeRunner{}, t: t}
	return BuildConfig{Repositories: fixturePins(), StateDir: filepath.Join(t.TempDir(), "state"), Context: bytes.NewReader(tarContext(files)), Runner: r, approvalRead: func(string) ([]byte, error) { return b, nil }}, r
}
func refreshContext(t *testing.T, cfg *BuildConfig) {
	t.Helper()
	files, e := loadDir("../../.wavemind/env")
	if e != nil {
		t.Fatal(e)
	}
	cfg.Context = bytes.NewReader(tarContext(files))
}
func TestBuildApprovalSingleUseAndFlock(t *testing.T) {
	cfg, r := buildFixture(t)
	r.entered = make(chan struct{})
	r.release = make(chan struct{})
	finished := make(chan error, 1)
	go func() { _, e := Build(context.Background(), cfg); finished <- e }()
	select {
	case <-r.entered:
	case <-time.After(5 * time.Second):
		t.Fatal("build never entered")
	}
	other := cfg
	refreshContext(t, &other)
	if _, e := Build(context.Background(), other); ExitCode(e) != 4 {
		t.Errorf("parallel rc=%d %v", ExitCode(e), e)
	}
	close(r.release)
	if e := <-finished; e != nil {
		t.Fatal(e)
	}
	refreshContext(t, &cfg)
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 7 {
		t.Fatalf("replay rc=%d", ExitCode(e))
	}
	if r.buildCount != 2 {
		t.Fatalf("one authorized pair expected, builds=%d", r.buildCount)
	}
	target := filepath.Join(t.TempDir(), "approval")
	if e := os.WriteFile(target, []byte("{}"), 0644); e != nil {
		t.Fatal(e)
	}
	link := target + ".link"
	if e := os.Symlink(target, link); e != nil {
		t.Fatal(e)
	}
	if _, e := openApproval(link); ExitCode(e) != 7 {
		t.Fatal("symlink approval accepted")
	}
}
func TestBuildReproducibilityRejectsDifferingOCIDigest(t *testing.T) {
	cfg, r := buildFixture(t)
	r.different = true
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 5 {
		t.Fatalf("mismatch rc=%d %v", ExitCode(e), e)
	}
	for _, a := range r.calls {
		if len(a) > 2 && a[0] == "docker" && a[1] == "run" {
			t.Fatal("probe before reproducibility")
		}
	}
	refreshContext(t, &cfg)
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 7 {
		t.Fatal("failed build did not burn approval")
	}
}
func TestOneJournalLinePerCreate(t *testing.T) {
	cfg, r := buildFixture(t)
	if _, e := Build(context.Background(), cfg); e != nil {
		t.Fatal(e)
	}
	count := 0
	for _, a := range r.calls {
		if a[0] == "docker" && a[1] == "run" {
			count++
		}
	}
	if count != 1 || len(r.journal) != count || strings.Count(r.journal[0], "\n") != 1 || !strings.HasPrefix(r.journal[0], "W8_CAPSULE_CREATED kind=probe lease=- image=") {
		t.Fatalf("creates=%d journal=%q", count, r.journal)
	}
	cfg, r = buildFixture(t)
	r.journalFail = true
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 4 {
		t.Fatalf("journal failure rc=%d %v", ExitCode(e), e)
	}
	for _, a := range r.calls {
		if a[0] == "docker" && a[1] == "run" {
			t.Fatal("container created after journal failure")
		}
	}
}
func TestBuilderProbeArgvAndOptW8Pinning(t *testing.T) {
	image := "sha256:" + digest([]byte("fixture image"))
	name := "0123456789ab"
	argv := ProbeArgv(image, name)
	if e := ValidateProbeArgv(argv, image, name); e != nil {
		t.Fatal(e)
	}
	for i := range argv {
		mutant := append([]string{}, argv[:i]...)
		mutant = append(mutant, argv[i+1:]...)
		if ExitCode(ValidateProbeArgv(mutant, image, name)) != 5 {
			t.Fatalf("missing arg accepted: %s", argv[i])
		}
	}
	if ExitCode(ScanLayers(bytes.NewReader(testTar(t, map[string][]byte{"opt/w8/ingest": []byte("binary")})), 8192)) != 5 {
		t.Fatal("unpinned opt/w8 runner accepted")
	}
}
func TestRootfulDaemonNeverBuilds(t *testing.T) {
	cfg, r := buildFixture(t)
	r.rootful = true
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 5 {
		t.Fatalf("rootful rc%d %v", ExitCode(e), e)
	}
	if r.buildCount != 0 {
		t.Fatal("rootful build reached")
	}
}
func TestRetentionPrunesByID(t *testing.T) {
	r := &testRunner{fakeRunner: &fakeRunner{}, t: t}
	ids := []string{"sha256:" + digest([]byte("current fixture")), "sha256:" + digest([]byte("previous fixture")), "sha256:" + digest([]byte("old fixture"))}
	r.images = append([]string{}, ids...)
	if e := retainImages(context.Background(), r, ids[0], ids[1]); e != nil {
		t.Fatal(e)
	}
	if len(r.images) != 2 {
		t.Fatal(r.images)
	}
	found := false
	for _, a := range r.calls {
		if strings.Join(a, " ") == "docker image rm --force "+ids[2] {
			found = true
		}
	}
	if !found {
		t.Fatal("older image not removed by full ID")
	}
}

func TestUnapprovedNeverReachesRunner(t *testing.T) {
	cfg, r := buildFixture(t)
	cfg.approvalRead = func(string) ([]byte, error) {
		return []byte(`{"schema":"os.cu.builder-approval/v1","mode":"per_build","image":"os-worker-base","arch":"aarch64","recipe_sha256":"` + digest([]byte("unapproved fixture")) + `","approval_id":"b2-0001","operator_note":"fixture"}`), nil
	}
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 7 {
		t.Fatalf("unapproved rc%d %v", ExitCode(e), e)
	}
	if len(r.calls) != 0 {
		t.Fatal("unapproved reached runner")
	}
}
func TestFailedBuildBurnsApproval(t *testing.T) {
	cfg, r := buildFixture(t)
	r.buildFail = true
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 3 {
		t.Fatalf("failed build rc%d %v", ExitCode(e), e)
	}
	refreshContext(t, &cfg)
	if _, e := Build(context.Background(), cfg); ExitCode(e) != 7 {
		t.Fatalf("failed approval reused rc%d", ExitCode(e))
	}
}
func TestProductionRunnerRefusesNonOsworker(t *testing.T) {
	for _, r := range []Runner{ExecRunner{}, &ExecRunner{}} {
		_, e := Build(context.Background(), BuildConfig{Runner: r})
		if ExitCode(e) != 5 {
			t.Fatalf("production caller rc%d %v", ExitCode(e), e)
		}
	}
}

func TestBuildContainerdStoreAddressesCandidateByManifest(t *testing.T) {
	cfg, r := buildFixture(t)
	r.containerdIDs = true
	res, e := Build(context.Background(), cfg)
	if e != nil {
		t.Fatalf("containerd-store build rc=%d %v", ExitCode(e), e)
	}
	manifest, _ := res["image_digest"].(string)
	if !strings.HasPrefix(manifest, "sha256:") {
		manifest = "sha256:" + manifest
	}
	tagged := false
	for _, c := range r.calls {
		if strings.Join(c, " ") == "docker image tag "+manifest+" os-worker-base:current" {
			tagged = true
		}
	}
	if !tagged {
		t.Fatalf("candidate not addressed by manifest digest %s", manifest)
	}
}

func containsString(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}
