package w8seat

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"opensuperficies.local/srf/src/w8seat/patchpolicy"
	"opensuperficies.local/srf/src/workerbridge"
)

func (s *Seat) runtime(task map[string]any) map[string]any {
	return map[string]any{"backend": "docker", "profile": "worker", "base_image": s.cfg.BaseImage, "commit": obj(task["repo"])["commit"], "workspace": map[string]any{"kind": "tmpfs", "root": "/workspace", "size_bytes": int64(2 << 30), "options": "rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001", "purge": "task_end", "ingest": "git-bundle-over-exec", "cache": map[string]any{"volume": "os_worker_cache", "target": "/opt/cache", "read_only": true, "manifest_sha256": s.cfg.CacheSHA}}, "extract": "tar-exec", "inference": "host", "vmm": nil}
}
func (s *Seat) grant(a *Attempt, task map[string]any) ([]byte, error) {
	nonce := make([]byte, 32)
	if _, e := rand.Read(nonce); e != nil {
		return nil, e
	}
	p := s.cfg.Policy
	g := map[string]any{"schema": "os.cu.worker-seat/v1", "task_id": task["task_id"], "attempt_id": task["attempt_id"], "lease_id": task["lease_id"], "task_sha256": hash(canonical(task)), "repo_commit": obj(task["repo"])["commit"], "seat_id": "w8-seat", "lease_nonce": hex.EncodeToString(nonce), "issued_at_ms": a.Created, "expires_at_ms": a.Expires, "collect_until_ms": a.Expires + 120000, "runtime": s.runtime(task), "network": map[string]any{"mode": "none", "allowlist": []any{}}, "limits": map[string]any{"ttl_ms": a.Expires - a.Created, "cpuset": p.CPUs.Worker, "memory_bytes": int64(4 << 30), "pids": 256, "workspace_bytes": int64(2 << 30)}, "isolation": map[string]any{"uid": 10001, "rootfs_read_only": true, "cap_drop": []string{"ALL"}, "no_new_privileges": true, "no_host_mounts": true, "no_runtime_socket": true, "seccomp_profile_sha256": p.SeccompSHA, "workspace_root": "/workspace/repo", "frames_root": "/dev/shm/w8/frames", "swap_enabled": false, "core_dumps_enabled": false, "hardening": map[string]any{"daemon": "rootless", "init": "tini", "read_only": true, "memswap_equals_memory": true, "nofile": 8192, "oom_score_adj": 800, "interactive_cpus": p.CPUs.Interactive, "devices": []any{}, "bind_mounts": []any{}, "env_file": nil, "runtime_socket_mounted": false, "env_names": envOrder, "egress": map[string]any{"mode": "none"}, "exec_allowlist_sha256": ExecAllowlistSHA()}}}
	if e := contract(g, "seat_grant"); e != nil {
		return nil, e
	}
	return canonical(g), nil
}
func (s *Seat) finish(j *pending, code int, reason string, gate, obs, extract map[string]any, cleanup map[string]any, files map[string][]byte) {
	a := j.attempt
	g, _ := workerbridge.Parse(a.Grant)
	if g == nil {
		var e error
		a.Grant, e = s.grant(a, j.task)
		if e != nil {
			return
		}
		g, _ = workerbridge.Parse(a.Grant)
	}
	if cleanup["status"] == "not_applicable" {
		gate = nil
		obs = nil
	}
	answers := []any{}
	sources := []any{}
	var output any
	if code == 0 {
		var err error
		output, sources, answers, err = admitReceiptData(j.task, files)
		if err != nil {
			code = 5
			reason = "verification"
			output = nil
		}
	}
	var fail any = reason
	if code == 0 {
		fail = nil
	}
	notes := fmt.Sprintf("observed_integration_tip=%s target_id=%s-%d bridge_result=AUSENTE (canon unavailable).", a.Tip, a.Container, a.Epoch)
	if len(j.patch) > 0 {
		notes += " input_patch_sha256=" + hash(j.patch)
	}
	metric := map[string]any{"name": "elapsed_ms", "unit": "ms", "value": nil, "provenance": "unavailable", "sample_count": 0}
	if !j.started.IsZero() {
		metric["value"] = time.Since(j.started).Milliseconds()
		metric["provenance"] = "host"
		metric["sample_count"] = 1
	}

	receipt := map[string]any{"schema": "os.cu.worker-receipt/v1", "task_id": j.task["task_id"], "attempt_id": j.task["attempt_id"], "lease_id": j.task["lease_id"], "task_sha256": hash(canonical(j.task)), "repo": j.task["repo"], "seat_id": "w8-seat", "lease_nonce": g["lease_nonce"], "seat_grant_sha256": hash(a.Grant), "started_at_ms": a.Created, "finished_at_ms": time.Now().UnixMilli(), "exit_code": code, "failure_reason": fail, "runtime": g["runtime"], "gate": gate, "sources_observed": sources, "output": output, "evidence": map[string]any{"answers": answers, "notes": notes}, "host_metric": metric, "observations": obs, "cleanup": cleanup, "extract": extract}
	b := canonical(receipt)
	if e := validateReceipt(b); e != nil {
		// Never publish an invalid receipt or turn an absent witness into success.
		diag := receiptDiagnosis(receipt)
		code = 6
		receipt["exit_code"] = 6
		receipt["failure_reason"] = "indeterminate"
		receipt["output"] = nil
		receipt["gate"] = nil
		receipt["observations"] = nil
		receipt["extract"] = nil
		receipt["cleanup"] = map[string]any{"status": "not_observed", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}
		receipt["evidence"] = map[string]any{"answers": []any{}, "notes": notes + " Receipt precondition absent; quarantined: " + e.Error() + diag}
		b = canonical(receipt)
		if validateReceipt(b) != nil {
			return
		}
	}
	artifacts := map[string]string{}
	for k, v := range files {
		artifacts[k] = string(v)
	}
	response := Response{ExitCode: code, Reason: "unavailable", Handle: a.Handle, Ready: true, Receipt: json.RawMessage(b), Artifacts: artifacts, BridgeResult: nil}
	if e := s.store.WriteBody(a, response); e != nil {
		response.ExitCode = 6
		receipt["exit_code"] = 6
		receipt["failure_reason"] = "indeterminate"
		receipt["output"] = nil
		receipt["observations"] = nil
		receipt["gate"] = nil
		receipt["cleanup"] = map[string]any{"status": "not_observed", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}
		response.Receipt = canonical(receipt)
	}
	_ = s.store.Update(a.Handle, func(x *Attempt) {
		x.Response = response
		if response.ExitCode == 0 {
			x.State = "SUCCEEDED"
		} else if response.ExitCode == 6 {
			x.State = "INDETERMINATE"
		} else {
			x.State = "FAILED"
		}
	})
}
func admitReceiptData(task map[string]any, files map[string][]byte) (any, []any, []any, error) {
	bad := func() (any, []any, []any, error) {
		return nil, nil, nil, refusal(5, "verification", "receipt evidence absent or invalid")
	}
	data := files["receipt-data.json"]
	var meta map[string]any
	if len(data) > 0 {
		var e error
		meta, e = workerbridge.Parse(data)
		if e != nil {
			return bad()
		}
	}
	paths := strs(task["write_paths"])
	changed := []any{}
	delta := files["delta.patch"]
	if len(delta) > 0 {
		p, e := patchpolicy.Check(delta, paths)
		if e != nil {
			return bad()
		}
		observed := arr(meta["files"])
		if len(observed) != len(p) {
			return bad()
		}
		seen := map[string]bool{}
		for _, x := range observed {
			v := obj(x)
			if e := contract(v, "changed_file"); e != nil {
				return bad()
			}
			found := false
			for _, c := range p {
				found = found || (c.Path == v["path"] && c.Mode == v["mode"] && c.Operation == v["operation"])
			}
			if !found || seen[str(v["path"])] {
				return bad()
			}
			seen[str(v["path"])] = true
			changed = append(changed, v)
		}
	} else if len(arr(meta["files"])) != 0 {
		return bad()
	}
	sources := []any{}
	sourceSeen := map[string]bool{}
	for _, v := range arr(meta["sources_observed"]) {
		if contract(v, "source_observed") != nil {
			return bad()
		}
		m := obj(v)
		p := str(m["path"])
		if sourceSeen[p] {
			return bad()
		}
		sourceSeen[p] = true
		found := false
		for _, requested := range arr(task["sources"]) {
			q := obj(requested)
			if q["path"] == p {
				found = true
				if q["sha256"] != nil && q["sha256"] != m["sha256"] {
					return bad()
				}
			}
		}
		if !found {
			return bad()
		}
		sources = append(sources, m)
	}
	if len(sources) != len(arr(task["sources"])) {
		return bad()
	}
	requests := arr(obj(task["evidence"])["requests"])
	answers := arr(meta["answers"])
	if answers == nil {
		answers = []any{}
	}
	if len(answers) != len(requests) || contract(answers, "answers") != nil {
		return bad()
	}
	seen := map[string]bool{}
	for _, raw := range answers {
		v := obj(raw)
		id := str(v["request_id"])
		if seen[id] {
			return bad()
		}
		seen[id] = true
		exists := false
		for _, r := range requests {
			exists = exists || obj(r)["request_id"] == id
		}
		if !exists {
			return bad()
		}
	}
	pin := obj(task["repo"])["commit"]
	branch := obj(task["bridge"])["writer_branch"]
	out := map[string]any{"kind": "branch", "repo_url": obj(task["repo"])["url"], "branch": branch, "base_commit": pin, "head_commit": pin, "files": changed}
	if contract(out, "output") != nil {
		return bad()
	}
	return out, sources, answers, nil
}

func receiptDiagnosis(r map[string]any) string {
	msg := ""
	for k, def := range map[string]string{"gate": "gate_observed", "observations": "observations", "extract": "extract_record", "output": "output", "runtime": "runtime", "cleanup": "cleanup", "host_metric": "metric_observed", "evidence": "receipt_evidence"} {
		if r[k] != nil {
			if e := contract(r[k], def); e != nil {
				msg += " " + k + ":" + e.Error()
			}
		}
	}
	return msg
}
