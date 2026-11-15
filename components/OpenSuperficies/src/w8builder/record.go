package w8builder

import (
	"context"
	"encoding/json"
	"fmt"
	"opensuperficies.local/srf/src/workerbridge"
	"path/filepath"
	"strings"
)

func CheckRecord(data []byte) error {
	v, e := workerbridge.Decode(data)
	if e != nil {
		return refusal(5, "verification", "invalid base image record JSON")
	}
	// Local, explicitly validated evidence extension; the frozen v1 schema is
	// applied to the base projection and continues to reject all unknown keys.
	m, ok := v.(map[string]any)
	if !ok {
		return refusal(5, "verification", "base image record object required")
	}
	if evidence, exists := m["repo_identity"]; exists {
		obj, ok := evidence.(map[string]any)
		if !ok || len(obj) != 2 {
			return refusal(5, "verification", "invalid repository identity evidence")
		}
		n, ok := obj["size"].(json.Number)
		size, err := n.Int64()
		sha, shaOK := obj["sha256"].(string)
		if !ok || err != nil || size <= 0 || size > 100000 || !shaOK || !validDigest(sha) {
			return refusal(5, "verification", "invalid repository identity evidence")
		}
		delete(m, "repo_identity")
	}
	if e = workerbridge.ValidateDef(v, "base_image_record"); e != nil {
		return refusal(5, "verification", "invalid base image record contract")
	}
	a := m["approval"].(map[string]any)
	if a["recipe_sha256"] != m["recipe_sha256"] {
		return refusal(5, "verification", "record recipe/approval mismatch")
	}
	return nil
}
func newRecord(recipeSHA, imageSHA, approvalSHA string) map[string]any {
	return map[string]any{
		"schema": "os.cu.base-image/v1", "name": "os-worker-base", "arch": "aarch64", "recipe_sha256": recipeSHA, "image_digest": imageSHA, "build_context": "recipe-dir", "fetch_pins_verified": true, "user_uid": 10001, "entrypoint": []string{"/usr/bin/tini", "-s", "--"}, "secret_paths_in_context": 0, "frame_bytes_in_image": 0, "repo_code_in_image": 0, "approval": map[string]any{"mode": "per_build", "approval_file_sha256": approvalSHA, "recipe_sha256": recipeSHA}, "retained_rank": 1,
	}
}
func imageID(s string) bool {
	return strings.HasPrefix(s, "sha256:") && validDigest(strings.TrimPrefix(s, "sha256:"))
}
func retainImages(ctx context.Context, r Runner, current, previous string) error {
	if !imageID(current) || (previous != "" && !imageID(previous)) {
		return refusal(5, "verification", "invalid retention ID")
	}
	ids, e := capture(ctx, r, "docker", "image", "ls", "os-worker-base", "--no-trunc", "--quiet")
	if e != nil {
		return refusal(6, "indeterminate", "retention list")
	}
	seen := map[string]bool{}
	for _, id := range strings.Fields(string(ids)) {
		if !imageID(id) {
			return refusal(5, "verification", "retention must use image IDs")
		}
		if seen[id] {
			continue
		}
		seen[id] = true
		if id == current || id == previous {
			continue
		}
		if _, e = capture(ctx, r, "docker", "image", "rm", "--force", id); e != nil {
			return refusal(6, "indeterminate", "retention prune failed")
		}
	}
	after, e := capture(ctx, r, "docker", "image", "ls", "os-worker-base", "--no-trunc", "--quiet")
	if e != nil {
		return refusal(6, "indeterminate", "retention verification")
	}
	unique := map[string]bool{}
	for _, id := range strings.Fields(string(after)) {
		if !imageID(id) {
			return refusal(5, "verification", "retention ID")
		}
		unique[id] = true
	}
	if len(unique) > 2 {
		return refusal(5, "verification", "more than two base image digests")
	}
	if !unique[current] {
		return refusal(5, "verification", "current image missing after retention")
	}
	return nil
}
func (l *buildLock) record(rec map[string]any) error {
	b, e := json.Marshal(rec)
	if e != nil {
		return refusal(6, "indeterminate", "record encoding")
	}
	if e = CheckRecord(b); e != nil {
		return e
	}
	f, e := stateFile(filepath.Join(l.dir, "base_image_records.jsonl"))
	if e != nil {
		return e
	}
	defer f.Close()
	// Keep append-only history, but explicitly demote the previous rank-one record.
	old, e := boundedRead(f, 4<<20)
	if e != nil {
		return refusal(6, "indeterminate", "record ledger cap/read")
	}
	if len(old) > 0 && old[len(old)-1] != '\n' {
		return refusal(6, "indeterminate", "partial record ledger")
	}
	lines := strings.Split(strings.TrimSpace(string(old)), "\n")
	if len(lines) > 0 && lines[len(lines)-1] != "" {
		prev, e := parseObject([]byte(lines[len(lines)-1]))
		if e != nil {
			return refusal(6, "indeterminate", "invalid record ledger")
		}
		if e = CheckRecord([]byte(lines[len(lines)-1])); e != nil {
			return refusal(6, "indeterminate", "invalid prior record")
		}
		if prev["image_digest"] != rec["image_digest"] {
			prev["retained_rank"] = 2
			pb, _ := json.Marshal(prev)
			if e = appendSynced(f, pb, l.dir); e != nil {
				return e
			}
		}
	}
	return appendSynced(f, b, l.dir)
}
func ProbeArgv(image, container string) []string {
	return []string{"docker", "run", "--rm", "--name", container, "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "64", "--memory", "4g", "--memory-swap", "4g", "--cpuset-cpus", "10-14", "--ulimit", "nofile=8192:8192", "--ulimit", "core=0", "--oom-score-adj", "800", "--user", "10001:10001", image, "id"}
}
func ValidateProbeArgv(argv []string, image, container string) error {
	expected := ProbeArgv(image, container)
	if fmt.Sprint(argv) != fmt.Sprint(expected) || !imageID(image) || !containerToken.MatchString(container) {
		return refusal(5, "containment", "unhardened probe argv")
	}
	return nil
}
