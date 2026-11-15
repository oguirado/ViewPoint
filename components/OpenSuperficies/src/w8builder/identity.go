package w8builder

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"io"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// RepositoryPin is host-side data. A full commit is mandatory; branches and
// working-tree contents are never used. Names must match the warm manifests.
type RepositoryPin struct {
	Repo      string `json:"repo"`
	Path      string `json:"path"`
	Commit    string `json:"commit"`
	Workspace string `json:"workspace,omitempty"`
}

type RepoIdentity struct{ blobs map[string]struct{} }

func (s RepoIdentity) Size() int { return len(s.blobs) }

// Digest is SHA-256 of sorted lowercase blob IDs, each terminated by LF.
func (s RepoIdentity) Digest() string {
	ids := make([]string, 0, len(s.blobs))
	for id := range s.blobs {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	if len(ids) == 0 {
		return digest(nil)
	}
	return digest([]byte(strings.Join(ids, "\n") + "\n"))
}
func gitObjectID(s string) bool {
	b, e := hex.DecodeString(s)
	return e == nil && len(b) == 20 && s == strings.ToLower(s)
}
func genericRepoDocument(name string) bool {
	b := strings.ToLower(path.Base(name))
	ext := path.Ext(b)
	if ext != "" && ext != ".md" && ext != ".txt" && ext != ".rst" {
		return false
	}
	stem := strings.TrimSuffix(b, ext)
	return stem == "readme" || stem == "license" || stem == "licence" || stem == "copying" || stem == "copyright" || strings.HasPrefix(stem, "license-")
}

// IdentityFromGitTrees consumes git ls-tree -r -l -z --full-tree output.
// Sizes come from the pinned Git objects, never the mutable checkout. No blobs
// are opened, so sensitive paths are not read to construct this set.
func IdentityFromGitTrees(trees ...[]byte) (RepoIdentity, error) {
	s := RepoIdentity{blobs: map[string]struct{}{}}
	for _, tree := range trees {
		if len(tree) > 16<<20 || (len(tree) > 0 && tree[len(tree)-1] != 0) {
			return s, refusal(5, "verification", "git tree cap/framing")
		}
		for _, row := range bytes.Split(tree, []byte{0}) {
			if len(row) == 0 {
				continue
			}
			meta, name, ok := bytes.Cut(row, []byte{'\t'})
			f := strings.Fields(string(meta))
			if !ok || len(f) != 4 || !gitObjectID(f[2]) || dangerousPath(string(name)) {
				return s, refusal(5, "verification", "invalid git tree row")
			}
			if f[1] == "commit" && f[0] == "160000" {
				continue // gitlinks are commit references, not tracked file blobs.
			}
			if f[1] != "blob" || (f[0] != "100644" && f[0] != "100755" && f[0] != "120000") {
				return s, refusal(5, "verification", "invalid git blob mode")
			}
			n, e := strconv.ParseInt(f[3], 10, 64)
			if e != nil || n < 0 {
				return s, refusal(5, "verification", "invalid git blob size")
			}
			if n < 64 || genericRepoDocument(string(name)) {
				continue
			}
			s.blobs[f[2]] = struct{}{}
			if len(s.blobs) > 100000 {
				return s, refusal(5, "verification", "repo identity cap")
			}
		}
	}
	return s, nil
}

func DecodeRepositoryPins(data []byte) ([]RepositoryPin, error) {
	var pins []RepositoryPin
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if len(data) > 64<<10 || d.Decode(&pins) != nil || len(pins) != 3 {
		return nil, refusal(2, "pin", "three repository pins required")
	}
	// Reject trailing JSON, including a second valid value.
	if d.Decode(new(any)) != io.EOF {
		return nil, refusal(2, "pin", "repository pin trailing data")
	}
	if e := validateRepositoryPins(pins); e != nil {
		return nil, e
	}
	return pins, nil
}
func validateRepositoryPins(pins []RepositoryPin) error {
	required := map[string]bool{"opensuperficies": false, "vision-engine": false, "wender-agent": false}
	if len(pins) != len(required) {
		return refusal(4, "unavailable", "target repository pins absent")
	}
	for _, p := range pins {
		seen, ok := required[p.Repo]
		if !ok || seen || !filepath.IsAbs(p.Path) || filepath.Clean(p.Path) != p.Path || !gitObjectID(p.Commit) {
			return refusal(2, "pin", "invalid repository pin")
		}
		required[p.Repo] = true
		if p.Workspace != "" && (!strings.HasPrefix(p.Workspace, "/") || path.Clean(p.Workspace) != p.Workspace || p.Workspace == "/") {
			return refusal(2, "pin", "invalid task workspace")
		}
	}
	return nil
}
func collectRepoIdentity(ctx context.Context, r Runner, pins []RepositoryPin, files recipe) (RepoIdentity, error) {
	if e := validateRepositoryPins(pins); e != nil {
		return RepoIdentity{}, e
	}
	combined := RepoIdentity{blobs: map[string]struct{}{}}
	for _, p := range pins {
		m, e := parseObject(files["warm/"+p.Repo+".json"])
		if e != nil || m["repo"] != p.Repo {
			return combined, refusal(2, "pin", "warm repository mismatch")
		}
		out := &limitedOutput{cap: 16 << 20}
		argv := []string{"git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-C", p.Path, "ls-tree", "-r", "-l", "-z", "--full-tree", p.Commit + "^{commit}"}
		if e = r.Run(ctx, argv, nil, out); e != nil || out.overflow {
			return combined, refusal(4, "unavailable", "pinned git tree unavailable")
		}
		set, e := IdentityFromGitTrees(out.data)
		if e != nil {
			return combined, e
		}
		for id := range set.blobs {
			combined.blobs[id] = struct{}{}
		}
		if combined.Size() > 100000 {
			return combined, refusal(5, "verification", "repo identity cap")
		}
	}
	if combined.Size() == 0 {
		return combined, refusal(4, "unavailable", "empty repository identity evidence")
	}
	return combined, nil
}
