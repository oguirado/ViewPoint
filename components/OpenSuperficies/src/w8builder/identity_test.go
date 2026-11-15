package w8builder

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
)

func fixturePins() []RepositoryPin {
	var pins []RepositoryPin
	for _, name := range []string{"opensuperficies", "vision-engine", "wender-agent"} {
		pins = append(pins, RepositoryPin{Repo: name, Path: "/fixture/" + name, Commit: strings.Repeat("a", 40), Workspace: "/tasks/" + name})
	}
	return pins
}
func identityFixture(t *testing.T) (RepoIdentity, []byte) {
	t.Helper()
	tree, e := os.ReadFile("../../testdata/w8builder/repo-identity.tree")
	if e != nil {
		t.Fatal(e)
	}
	data, e := os.ReadFile("../../testdata/w8builder/repo-identity-go.mod")
	if e != nil {
		t.Fatal(e)
	}
	set, e := IdentityFromGitTrees(tree)
	if e != nil {
		t.Fatal(e)
	}
	return set, data
}
func TestIdentityFromPinnedGitTreeFixture(t *testing.T) {
	set, data := identityFixture(t)
	h := sha1.Sum(append([]byte(fmt.Sprintf("blob %d\x00", len(data))), data...))
	if _, ok := set.blobs[hex.EncodeToString(h[:])]; !ok || set.Size() != 1 {
		t.Fatal("real tracked go.mod missing or shared blobs retained")
	}
	for _, bad := range []string{"malformed\x00", "100644 blob " + strings.Repeat("a", 40) + " -1\tx\x00", "100644 blob " + strings.Repeat("a", 40) + " 64\tx"} {
		if _, e := IdentityFromGitTrees([]byte(bad)); ExitCode(e) != 5 {
			t.Fatal("malformed tree accepted")
		}
	}
	// Dedupe and digest are independent of tree order and repeated target blobs.
	tree, _ := os.ReadFile("../../testdata/w8builder/repo-identity.tree")
	dup, e := IdentityFromGitTrees(tree, tree)
	if e != nil || dup.Digest() != set.Digest() || dup.Size() != 1 {
		t.Fatal("identity not stable")
	}
}
func TestLayerRejectsRenamedRealRepositoryBlob(t *testing.T) {
	set, data := identityFixture(t)
	for _, name := range []string{"usr/share/data/copy.txt", "usr/local/go/src/renamed.go", "opt/node/renamed.js"} {
		for _, nested := range []bool{false, true} {
			layer := testTar(t, map[string][]byte{name: data})
			if nested {
				layer = testTar(t, map[string][]byte{"layer/layer.tar": layer})
			}
			if ExitCode(ScanLayers(bytes.NewReader(layer), 1<<20, ScanPolicy{Identity: set})) != 5 {
				t.Fatal("copied real tracked file accepted", name, nested)
			}
		}
	}
}
func TestIdentityIgnoresTinyAndGenericDocuments(t *testing.T) {
	set, _ := identityFixture(t)
	for _, data := range [][]byte{nil, []byte("x"), bytes.Repeat([]byte("L"), 80), bytes.Repeat([]byte("R"), 80)} {
		if e := ScanLayers(bytes.NewReader(testTar(t, map[string][]byte{"usr/share/tool/shared": data})), 8192, ScanPolicy{Identity: set}); e != nil {
			t.Fatal(e)
		}
	}
}
func TestDeclaredWorkspaceAndGitDirectoryRefused(t *testing.T) {
	layer := testTar(t, map[string][]byte{"tasks/custom/copied": []byte("payload")})
	if ExitCode(ScanLayers(bytes.NewReader(layer), 8192, ScanPolicy{Workspaces: []string{"/tasks/custom"}})) != 5 {
		t.Fatal("workspace allowed")
	}
	var b bytes.Buffer
	w := tar.NewWriter(&b)
	_ = w.WriteHeader(&tar.Header{Name: "usr/share/repo/.git/", Typeflag: tar.TypeDir, Mode: 0755})
	_ = w.Close()
	if ExitCode(ScanLayers(&b, 8192)) != 5 {
		t.Fatal("git directory allowed")
	}
}
func TestBuildCollectsPinnedIdentitiesBeforeDockerAndRecordsEvidence(t *testing.T) {
	cfg, r := buildFixture(t)
	rec, e := Build(context.Background(), cfg)
	if e != nil {
		t.Fatal(e)
	}
	set, _ := identityFixture(t)
	evidence := rec["repo_identity"].(map[string]any)
	if evidence["size"] != set.Size() || evidence["sha256"] != set.Digest() || rec["repo_code_in_image"] != 0 {
		t.Fatal("identity record mismatch")
	}
	if r.calls[0][0] != "git" {
		t.Fatal("identity collection after docker")
	}
	for i, pin := range cfg.Repositories {
		got := strings.Join(r.calls[i], " ")
		if !strings.Contains(got, "-C "+pin.Path+" ls-tree -r -l -z --full-tree "+pin.Commit) {
			t.Fatal("unpinned git invocation", got)
		}
	}
	b, _ := json.Marshal(rec)
	if e := CheckRecord(b); e != nil {
		t.Fatal(e)
	}
	cfg, r = buildFixture(t)
	cfg.Repositories = nil
	if _, e = Build(context.Background(), cfg); ExitCode(e) != 4 || len(r.calls) != 0 {
		t.Fatal("missing pins reached docker", e)
	}
}
func TestRepositoryPinDataRejectsBranchesAndUnknownFields(t *testing.T) {
	pins := fixturePins()
	b, _ := json.Marshal(pins)
	if _, e := DecodeRepositoryPins(b); e != nil {
		t.Fatal(e)
	}
	pins[0].Commit = "main"
	b, _ = json.Marshal(pins)
	if _, e := DecodeRepositoryPins(b); ExitCode(e) != 2 {
		t.Fatal("branch accepted")
	}
	pins = fixturePins()
	pins[0].Repo = pins[1].Repo
	b, _ = json.Marshal(pins)
	if _, e := DecodeRepositoryPins(b); ExitCode(e) != 2 {
		t.Fatal("duplicate accepted")
	}
	pins = fixturePins()
	b, _ = json.Marshal(pins)
	b = append(b, []byte(" []")...)
	if _, e := DecodeRepositoryPins(b); ExitCode(e) != 2 {
		t.Fatal("trailing JSON accepted")
	}
}

func TestBuildRefusesCopiedRepositoryLayerBeforePublication(t *testing.T) {
	cfg, r := buildFixture(t)
	_, data := identityFixture(t)
	r.scanFiles = map[string][]byte{"usr/local/go/src/disguised.go": data}
	rec, e := Build(context.Background(), cfg)
	if rec != nil || ExitCode(e) != 5 {
		t.Fatal("copied repository published", rec, e)
	}
	for _, call := range r.calls {
		if strings.HasPrefix(strings.Join(call, " "), "docker image tag ") {
			t.Fatal("refused candidate tagged")
		}
	}
}
