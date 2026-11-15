package workerbridge

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

const OSSurface = "os-capsule"

var sharedIDs = []string{"task_id", "thread", "agent", "worker_id", "attempt_id", "lease_id", "key", "base_seq", "done_signal"}

// SharedIDs returns the nine PROTOCOL identifiers; role is copied separately.
// Surface is assigned only by the encoder and never copied as an identifier.
func SharedIDs() []string { return append([]string(nil), sharedIDs...) }

type projection struct {
	TaskID, AttemptID, LeaseID        any
	Repo                              Document
	Paths, WritePaths, Argv, Requests []any
}

func project(b Document) (projection, error) {
	var p projection
	if e := validate(b, "bridge_task_v1"); e != nil {
		return p, e
	}
	commit := str(b["observed_source_commit"])
	if len(commit) != 40 {
		return p, reject("MALFORMED", "unresolved or non-SHA1 bridge commit")
	}
	gates := []string{}
	for _, g := range array(b["acceptance_gates"]) {
		s := str(g)
		if strings.ContainsAny(s, "\r\n") {
			return p, reject("MALFORMED", "multi-line acceptance gate")
		}
		gates = append(gates, s)
	}
	p = projection{TaskID: b["task_id"], AttemptID: b["attempt_id"], LeaseID: b["lease_id"], Repo: Document{"url": "https://github.com/" + str(b["repo"]), "commit": commit}, Paths: append([]any{}, array(b["source_files"])...), WritePaths: append([]any{}, array(b["allowed_write_paths"])...), Argv: []any{"/bin/sh", "-ec", strings.Join(gates, "\n")}, Requests: []any{}}
	for i, raw := range array(b["evidence_requests"]) {
		text := str(raw)
		r := Document{"request_id": fmt.Sprintf("e%d", i)}
		switch {
		case strings.HasPrefix(text, "sha256:"):
			r["kind"] = "sha256"
			r["path"] = strings.TrimPrefix(text, "sha256:")
		case strings.HasPrefix(text, "line:"):
			body := strings.TrimPrefix(text, "line:")
			j := strings.LastIndexByte(body, ':')
			if j <= 0 {
				return p, reject("MALFORMED", "bad line request")
			}
			n, e := strconv.ParseInt(body[j+1:], 10, 64)
			if e != nil || n < 1 || strings.HasPrefix(body[j+1:], "+") {
				return p, reject("MALFORMED", "bad line request")
			}
			r["kind"] = "line"
			r["path"] = body[:j]
			r["line"] = json.Number(strconv.FormatInt(n, 10))
		case strings.HasPrefix(text, "marker:"):
			body := strings.TrimPrefix(text, "marker:")
			path, marker, ok := strings.Cut(body, ":")
			if !ok || marker == "" {
				return p, reject("MALFORMED", "bad marker request")
			}
			r["kind"] = "marker"
			r["path"] = path
			r["text"] = marker
		default:
			r["kind"] = "text"
			r["text"] = text
		}
		p.Requests = append(p.Requests, r)
		if path, ok := r["path"]; ok && !contains(p.Paths, path) {
			p.Paths = append(p.Paths, path)
		}
	}
	return p, nil
}

// BridgeToWorker projects a frozen bridge task without invoking its gates.
func BridgeToWorker(bridge []byte, objective string, timeoutMS int64) ([]byte, error) {
	b, e := Parse(bridge)
	if e != nil {
		return nil, e
	}
	p, e := project(b)
	if e != nil {
		return nil, e
	}
	sources := []any{}
	for _, path := range p.Paths {
		sources = append(sources, Document{"path": path, "sha256": nil})
	}
	w := Document{"schema": "os.cu.worker-task/v1", "task_id": p.TaskID, "attempt_id": p.AttemptID, "lease_id": p.LeaseID, "repo": p.Repo, "objective": objective, "sources": sources, "write_paths": p.WritePaths,
		"gate": Document{"argv": p.Argv, "cwd": ".", "timeout_ms": json.Number(strconv.FormatInt(timeoutMS, 10))}, "evidence": Document{"requests": p.Requests, "max_bytes": json.Number("102400")}, "consumer": Document{"name": "auto_ingest"}, "host_metric": Document{"name": "elapsed_ms", "unit": "ms", "target_max": nil, "required": false}, "bridge": b}
	if e = validateTask(w); e != nil {
		return nil, e
	}
	return Canonical(w)
}
func validateTask(w Document) error {
	if e := validate(w, "task"); e != nil {
		return e
	}
	seen := map[string]bool{}
	paths := []any{}
	for _, s := range array(w["sources"]) {
		p := str(object(s)["path"])
		if seen[p] {
			return reject("MALFORMED", "duplicate source")
		}
		seen[p] = true
		paths = append(paths, p)
	}
	requests := object(w["evidence"])["requests"]
	ids := map[string]bool{}
	for _, r := range array(requests) {
		o := object(r)
		id := str(o["request_id"])
		if ids[id] {
			return reject("MALFORMED", "duplicate evidence ID")
		}
		ids[id] = true
		if p, ok := o["path"]; ok && !seen[str(p)] {
			return reject("MALFORMED", "unbound evidence path")
		}
	}
	ttl := int64(900000)
	if n, ok := object(w["policy"])["ttl_ms"]; ok {
		ttl = num(n)
	}
	gate := object(w["gate"])
	if num(gate["timeout_ms"]) > ttl {
		return reject("MALFORMED", "gate exceeds task TTL")
	}
	if b, ok := w["bridge"]; ok {
		p, e := project(object(b))
		if e != nil {
			return e
		}
		actual := []any{w["task_id"], w["attempt_id"], w["lease_id"], w["repo"], paths, w["write_paths"], gate["argv"], gate["cwd"], requests}
		want := []any{p.TaskID, p.AttemptID, p.LeaseID, p.Repo, p.Paths, p.WritePaths, p.Argv, ".", p.Requests}
		if !equal(actual, want) {
			return reject("MALFORMED", "bridge projection drift")
		}
	}
	return nil
}

// WorkerToBridge validates the full projection before returning the original
// bridge task in canonical bytes. No dispatch identifier is repaired.
func WorkerToBridge(worker []byte) ([]byte, error) {
	w, e := Parse(worker)
	if e != nil {
		return nil, e
	}
	if e = validateTask(w); e != nil {
		return nil, e
	}
	b, ok := w["bridge"]
	if !ok {
		return nil, reject("MALFORMED", "worker has no bridge binding")
	}
	return BridgeCanonical(b)
}

// Envelope makes parity explicit. OutboxBytes refuses every parity envelope.
// This is transport data, never proof that a capsule or a gate actually ran.
type Envelope struct {
	Mode   string   `json:"mode"`
	Result Document `json:"result"`
}

// EncodeBridgeResult encodes a structurally valid receipt bound to its task.
// Runtime/grant verification belongs to a trusted supervisor in a later phase.
// Until the pinned canon admits os-capsule, every live request is unavailable.
func EncodeBridgeResult(worker, receipt []byte, promptID string, live bool) (Envelope, error) {
	var env Envelope
	w, e := Parse(worker)
	if e != nil {
		return env, e
	}
	b := object(w["bridge"])
	if live {
		if str(b["surface"]) != OSSurface {
			return env, unavailable("live surface is not os-capsule")
		}
		if e = validate(b, "bridge_task_v1"); e != nil {
			return env, unavailable("pinned canon does not admit os-capsule")
		}
	}
	if e = validateTask(w); e != nil {
		return env, e
	}
	if b == nil {
		return env, reject("MALFORMED", "missing bridge task")
	}
	r, e := Parse(receipt)
	if e != nil {
		return env, e
	}
	if e = validate(r, "receipt"); e != nil {
		return env, e
	}
	for _, key := range []string{"task_id", "attempt_id", "lease_id", "repo"} {
		if !equal(w[key], r[key]) {
			return env, reject("MALFORMED", "receipt task binding mismatch")
		}
	}
	if str(r["task_sha256"]) != digest(w) {
		return env, reject("MALFORMED", "receipt task digest mismatch")
	}
	binding := "os.cu.worker-receipt/v1 task_sha256=" + str(r["task_sha256"]) + " receipt_sha256=" + digest(r) + " rc=" + fmt.Sprint(r["exit_code"])
	answers, artifacts := []any{}, []any{}
	evidence := ""
	output := object(r["output"])
	if num(r["exit_code"]) == 0 {
		requested := array(b["evidence_requests"])
		seen := map[int64]bool{}
		for _, v := range array(object(r["evidence"])["answers"]) {
			a := object(v)
			id := str(a["request_id"])
			if !strings.HasPrefix(id, "e") {
				return env, reject("MALFORMED", "unmapped evidence ID")
			}
			i, err := strconv.ParseInt(id[1:], 10, 64)
			if err != nil || i < 0 || i >= int64(len(requested)) || seen[i] {
				return env, reject("MALFORMED", "unmapped evidence index")
			}
			seen[i] = true
			answers = append(answers, Document{"request_index": json.Number(strconv.FormatInt(i, 10)), "answer": a["answer"]})
		}
		if len(seen) != len(requested) {
			return env, reject("MALFORMED", "missing evidence answer")
		}
		for _, v := range array(output["files"]) {
			a := object(v)
			if !contains(w["write_paths"], a["path"]) {
				return env, reject("PATH_OUTSIDE_SURFACE", "output outside worker surface")
			}
			artifacts = append(artifacts, Document{"path": a["path"], "sha256": a["sha256"]})
		}
		evidence = promptID + "; " + binding
	} else {
		for i := range array(b["evidence_requests"]) {
			answers = append(answers, Document{"request_index": json.Number(strconv.Itoa(i)), "answer": "ABSENT"})
		}
		evidence = "BLOCKED: OS capsule attempt failed, " + binding + " reason=" + str(r["failure_reason"])
	}
	artifacts = append(artifacts, Document{"path": b["result_path"], "sha256": nil}, Document{"path": b["report_path"], "sha256": nil})
	surface := str(b["surface"])
	mode := "parity"
	if live {
		surface = OSSurface
		mode = "live"
	}
	result := Document{"schema_version": "bridge-result-v1", "surface": surface, "source_binding": Document{"requested_ref": b["requested_ref"], "observed_commit": object(r["repo"])["commit"], "mode": "vm_clone", "evidence": evidence}, "content": Document{"path": b["report_path"], "sha256": nil}, "artifacts": artifacts, "evidence_answers": answers}
	for _, key := range append(SharedIDs(), "role") {
		result[key] = b[key]
	}
	if output != nil && output["kind"] == "branch" {
		result["branch"] = output["branch"]
	}
	if e = validate(result, "bridge_result_v1"); e != nil {
		return env, e
	}
	allowed := append(append([]any{}, array(b["allowed_write_paths"])...), b["result_path"], b["report_path"])
	for _, v := range artifacts {
		if !contains(allowed, object(v)["path"]) {
			return env, reject("PATH_OUTSIDE_SURFACE", "artifact outside bridge surface")
		}
	}
	return Envelope{Mode: mode, Result: result}, nil
}

// OutboxBytes is a pure serialization boundary, not a filesystem writer.
func OutboxBytes(env Envelope) ([]byte, error) {
	if env.Mode != "live" || env.Result["surface"] != OSSurface {
		return nil, reject("MALFORMED", "parity envelope cannot enter outbox")
	}
	if e := validate(env.Result, "bridge_result_v1"); e != nil {
		return nil, unavailable("pinned canon does not admit os-capsule")
	}
	return BridgeCanonical(env.Result)
}
