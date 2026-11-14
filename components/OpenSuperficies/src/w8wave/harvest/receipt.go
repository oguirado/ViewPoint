package harvest

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"io"
	"path"
	"strconv"
	"strings"
	"unicode/utf8"

	"opensuperficies.local/srf/src/workerbridge"
)

// validateCandidate checks the host-owned submission envelope before the only
// irreversible send. Task text remains data for the existing Muse seat adapter.
func validateCandidate(f FrozenWave, c Candidate) (Attempt, error) {
	a := c.Attempt
	bad := func(detail string) (Attempt, error) { return Attempt{}, refusal(5, detail) }
	if c.Route != MuseSeatRoute || c.GuestInference || len(c.BindMounts) != 0 || c.EnvFile != "" || c.RuntimeSocket {
		return bad("submission violates the host-only Muse seat boundary")
	}
	if !present(c.ID) || !present(a.TaskID) || !present(a.AttemptID) || !present(a.LeaseID) || !validHex(a.Nonce, 64) {
		return bad("candidate or attempt identity is absent")
	}
	if !validHex(f.SHA256(), 64) || !validHex(a.InputSHA256, 64) || a.InputSHA256 != f.wave.InputSHA256 || a.ResourcePolicySHA256 != f.wave.ResourcePolicySHA256 {
		return bad("candidate input or resource policy differs from the frozen wave")
	}
	found := false
	for _, obligation := range f.wave.Obligations {
		if obligation.ID == a.ObligationID { found = true; break }
	}
	if !found { return bad("candidate refers to an unknown obligation") }
	if a.Backend != ProcessBackend && a.Backend != KVMBackend || a.RequiresKVM && a.Backend != KVMBackend {
		return bad("required execution backend is unavailable")
	}
	for _, frame := range a.FrameOrder {
		if !present(frame) { return bad("missing ordered frame identity") }
	}
	if len(a.WorkerTask) == 0 || len(a.WorkerTask) > 1<<20 { return bad("worker task is absent or over its bound") }
	w, err := workerbridge.Parse(a.WorkerTask)
	if err != nil { return bad("worker task is not strict JSON") }
	if err = workerbridge.ValidateDef(w, "task"); err != nil { return bad("worker task violates the frozen schema") }
	if err = validateWorkerBinding(w, a.WorkerTask); err != nil { return bad(err.Error()) }
	repo := receiptObject(w["repo"])
	if w["task_id"] != a.TaskID || w["attempt_id"] != a.AttemptID || w["lease_id"] != a.LeaseID || repo["url"] != waveRepositoryURL(f.wave.Repo) || repo["commit"] != f.wave.BaseCommit || w["objective"] != f.wave.Objective || !receiptEqual(receiptObject(w["gate"])["argv"], f.wave.GateArgv) {
		return bad("worker task differs from the frozen dispatch")
	}
	if bridge := receiptObject(w["bridge"]); bridge != nil && bridge["surface"] == "jules" {
		return bad("host Muse cannot carry jules provenance")
	}
	// This is the existing Muse handle tuple, not a new lifecycle identity. The
	// wave binding is recorded separately so a later wave cannot resend it.
	keyBytes, err := json.Marshal([]string{a.TaskID, a.AttemptID, a.LeaseID})
	if err != nil { return bad("attempt identity cannot be encoded") }
	key := digest(keyBytes)
	if a.Key != "" && a.Key != key { return bad("supplied attempt key differs from the Muse identity") }
	a.Key = key
	a.WorkerTask = bytes.Clone(a.WorkerTask)
	a.FrameOrder = append([]string(nil), a.FrameOrder...)
	return a, nil
}

// validateExtraction verifies the unchanged host receipt against the admitted
// worker task, the trusted seat grant, and the actual in-memory text extraction.
// A nonzero receipt outcome is returned with nil error only when its evidence
// and cleanup are complete; uncertain cleanup always remains quarantined.
func validateExtraction(f FrozenWave, a Attempt, execution Execution, x Extraction) (int, error) {
	bad := func(detail string) (int, error) { return 5, refusal(5, detail) }
	uncertain := func(detail string) (int, error) { return 6, refusal(6, detail) }
	if !x.HostValidated || !x.A4Validated || x.AttemptKey != a.Key || x.Nonce != a.Nonce || x.Cleanup.AttemptKey != a.Key || x.Cleanup.LeaseID != a.LeaseID || x.Cleanup.Nonce != a.Nonce {
		return bad("extraction lacks matching trusted host provenance")
	}
	if !x.Cleanup.Known || !x.Cleanup.Verified { return uncertain("purge is failed or unknown; retain admission capacity") }
	if len(x.Receipt) == 0 || len(x.Receipt) > 1<<20 || len(x.Grant) == 0 || len(x.Grant) > 1<<20 {
		return bad("host receipt or grant is absent or over its bound")
	}
	r, err := workerbridge.Parse(x.Receipt)
	if err != nil { return bad("receipt is not strict JSON") }
	if r["task_id"] != a.TaskID || r["attempt_id"] != a.AttemptID || r["lease_id"] != a.LeaseID || r["lease_nonce"] != a.Nonce {
		return bad("receipt belongs to a different task, attempt, lease or nonce")
	}
	cleanup := receiptObject(r["cleanup"])
	if cleanup["status"] == "failed" || cleanup["status"] == "not_observed" || cleanup["status"] == "not_applicable" {
		return uncertain("receipt does not prove a verified local purge")
	}
	if err = workerbridge.ValidateDef(cleanup, "cleanup_success"); err != nil { return uncertain("receipt cleanup cannot release admission capacity") }
	if err = workerbridge.ValidateDef(r, "receipt"); err != nil { return bad("receipt violates the frozen schema") }
	w, err := workerbridge.Parse(a.WorkerTask)
	if err != nil { return bad("admitted worker task is malformed") }
	if err = workerbridge.ValidateDef(w, "task"); err != nil { return bad("admitted worker task no longer validates") }
	g, err := workerbridge.Parse(x.Grant)
	if err != nil { return bad("seat grant is not strict JSON") }
	if err = workerbridge.ValidateDef(g, "seat_grant"); err != nil { return bad("seat grant violates the frozen schema") }
	if len(execution.Grant) != 0 {
		original, e := workerbridge.Parse(execution.Grant)
		if e != nil || !receiptEqual(original, g) { return bad("collection changed the original seat grant") }
	}
	wire, err := workerbridge.Canonical(w)
	if err != nil { return bad("worker task cannot be canonicalized") }
	taskHash := digest(wire)
	wire, err = workerbridge.Canonical(g)
	if err != nil { return bad("seat grant cannot be canonicalized") }
	if r["task_sha256"] != taskHash || g["task_sha256"] != taskHash || r["seat_grant_sha256"] != digest(wire) || g["task_id"] != a.TaskID || g["attempt_id"] != a.AttemptID || g["lease_id"] != a.LeaseID || g["lease_nonce"] != a.Nonce || r["seat_id"] != g["seat_id"] {
		return bad("receipt, grant and task digest binding differs")
	}
	repo := receiptObject(r["repo"])
	runtime := receiptObject(r["runtime"])
	if !receiptEqual(r["repo"], w["repo"]) || repo["url"] != waveRepositoryURL(f.wave.Repo) || repo["commit"] != f.wave.BaseCommit || g["repo_commit"] != f.wave.BaseCommit || runtime["commit"] != f.wave.BaseCommit || runtime["backend"] != string(a.Backend) || runtime["inference"] != "host" || !receiptEqual(r["runtime"], g["runtime"]) {
		return bad("receipt runtime or repository differs from the frozen admission")
	}
	if a.RequiresKVM && runtime["backend"] != string(KVMBackend) { return bad("KVM obligation was executed on a process-only backend") }
	issued, expiry, until := receiptNumber(g["issued_at_ms"]), receiptNumber(g["expires_at_ms"]), receiptNumber(g["collect_until_ms"])
	started, finished := receiptNumber(r["started_at_ms"]), receiptNumber(r["finished_at_ms"])
	if issued >= expiry || expiry > until || started < issued || started >= expiry || finished < started || finished > until {
		return bad("receipt timing falls outside its seat grant")
	}
	// CITED defaults and collection grace match the frozen contract model.
	policy := receiptObject(w["policy"])
	ttl := receiptDefaultNumber(policy, "ttl_ms", 900000)
	limits := receiptObject(g["limits"])
	if until-expiry > 60000 || expiry-issued > ttl || expiry-issued > receiptNumber(limits["ttl_ms"]) || receiptNumber(limits["ttl_ms"]) > ttl || receiptNumber(receiptObject(w["gate"])["timeout_ms"]) > receiptNumber(limits["ttl_ms"]) || receiptNumber(r["exit_code"]) == 0 && finished > expiry {
		return bad("grant or successful receipt exceeds the immutable task TTL")
	}
	if !receiptNetworkSubset(receiptObject(g["network"]), receiptObject(policy["network"])) { return bad("network grant exceeds the immutable request") }
	gate := receiptObject(r["gate"])
	if gate != nil {
		expected := receiptObject(w["gate"])
		for _, key := range []string{"argv", "cwd", "timeout_ms"} {
			if !receiptEqual(gate[key], expected[key]) { return bad("receipt gate differs from the admitted ordered gate") }
		}
		if receiptNumber(gate["duration_ms"]) > finished-started { return bad("gate duration exceeds its receipt interval") }
		if receiptNumber(gate["exit_code"]) != normalizedReceiptGate(gate) { return bad("gate exit classification differs from the frozen contract") }
		for _, name := range []string{"stdout", "stderr"} {
			stream := receiptObject(gate[name])
			retained, total := receiptNumber(stream["retained_bytes"]), receiptNumber(stream["total_bytes"])
			if retained > total || stream["truncated"] != (retained < total) || retained > 0 && stream["sha256"] == nil { return bad("retained gate stream is not bound") }
		}
	}
	if err = validateGrantObservations(g, r); err != nil { return bad(err.Error()) }
	rc := int(receiptNumber(r["exit_code"]))
	if rc == 0 {
		if err = validateReceiptEvidence(w, r); err != nil { return bad(err.Error()) }
	}
	metric, metricRequest := receiptObject(r["host_metric"]), receiptObject(w["host_metric"])
	if metric["name"] != metricRequest["name"] || metric["unit"] != metricRequest["unit"] || rc == 0 && metricRequest["required"] == true && metric["value"] == nil || rc == 0 && metricRequest["target_max"] != nil && metric["value"] != nil && receiptNumber(metric["value"]) > receiptNumber(metricRequest["target_max"]) {
		return bad("host metric does not match its immutable request")
	}
	if err = validateTextExtraction(w, r, x); err != nil { return bad(err.Error()) }
	return rc, nil
}

func validateGrantObservations(grant, receipt workerbridge.Document) error {
	bad := func(detail string) error { return refusal(5, detail) }
	isolation := receiptObject(grant["isolation"])
	limits := receiptObject(grant["limits"])
	hardening := receiptObject(isolation["hardening"])
	runtime := receiptObject(grant["runtime"])
	if isolation == nil || limits == nil { return bad("local seat grant lacks isolation or limits") }
	if isolation["workspace_root"] == isolation["frames_root"] { return bad("frame and checkout roots are not distinct") }
	if !receiptEqual(receiptObject(runtime["workspace"])["size_bytes"], limits["workspace_bytes"]) { return bad("workspace size differs from reservation") }
	if receiptNumber(limits["workspace_bytes"]) > receiptNumber(limits["memory_bytes"]) { return bad("workspace reservation exceeds granted memory") }
	interactive := map[int64]bool{}
	for _, cpu := range receiptArray(hardening["interactive_cpus"]) { interactive[receiptNumber(cpu)] = true }
	lastCPU := int64(-1)
	for _, cpu := range receiptArray(limits["cpuset"]) {
		if interactive[receiptNumber(cpu)] { return bad("grant uses an interactive CPU") }
		if receiptNumber(cpu) <= lastCPU { return bad("grant cpuset is not in canonical order") }
		lastCPU = receiptNumber(cpu)
	}
	networkMode := receiptObject(grant["network"])["mode"]
	egressMode := receiptObject(hardening["egress"])["mode"]
	if (networkMode == "none") != (egressMode == "none") { return bad("grant network and egress policy differ") }
	observed := receiptObject(receipt["observations"])
	if observed == nil {
		if receipt["gate"] != nil || receiptNumber(receipt["exit_code"]) == 0 { return bad("executed work lacks A4 observations") }
		return nil
	}
	for _, key := range []string{"uid", "rootfs_read_only", "cap_drop", "no_new_privileges", "no_host_mounts", "no_runtime_socket", "seccomp_profile_sha256", "workspace_root", "frames_root", "swap_enabled", "core_dumps_enabled"} {
		if !receiptEqual(observed[key], isolation[key]) { return bad("A4 observation differs from granted " + key) }
	}
	for observedKey, limitKey := range map[string]string{"cpuset": "cpuset", "memory_limit_bytes": "memory_bytes", "pids_limit": "pids", "workspace_limit_bytes": "workspace_bytes"} {
		if !receiptEqual(observed[observedKey], limits[limitKey]) { return bad("observed resource limit differs from its grant") }
	}
	if !receiptEqual(observed["network"], grant["network"]) { return bad("observed network differs from its grant") }
	for peak, limit := range map[string]string{"peak_memory_bytes":"memory_bytes", "peak_pids":"pids", "workspace_peak_bytes":"workspace_bytes"} {
		if observed[peak] != nil && receiptNumber(observed[peak]) > receiptNumber(limits[limit]) { return bad("observed resource peak exceeds its grant") }
	}
	probes := receiptObject(observed["probes"])
	for _, key := range []string{"devices", "nofile", "oom_score_adj", "daemon", "memswap_equals_memory"} {
		if !receiptEqual(probes[key], hardening[key]) { return bad("observed probe differs from granted " + key) }
	}
	if receiptNumber(probes["orphans_after_task"]) != 0 || receiptNumber(observed["public_listeners"]) != 0 || receiptNumber(observed["x11_listeners"]) != 0 { return bad("cleanup leaves a process or listener") }
	if runtime["profile"] == "worker" && observed["xpra"] != "off" { return bad("headless work unexpectedly has a UI relay") }
	if runtime["profile"] == "worker_ui" && observed["xpra"] != "uds" { return bad("UI work lacks a private relay") }
	if runtime["backend"] == string(KVMBackend) && !receiptEqual(receiptObject(runtime["nesting"])["outer_image_sha256"], receiptObject(runtime["base_image"])["digest"]) { return bad("nested runtime image differs from its grant") }
	return nil
}

func validateReceiptEvidence(worker, receipt workerbridge.Document) error {
	requested := map[string]workerbridge.Document{}
	for _, raw := range receiptArray(receiptObject(worker["evidence"])["requests"]) {
		request := receiptObject(raw)
		id := receiptString(request["request_id"])
		if _, exists := requested[id]; exists { return refusal(5, "duplicate requested evidence identity") }
		requested[id] = request
	}
	answered := map[string]string{}
	for _, raw := range receiptArray(receiptObject(receipt["evidence"])["answers"]) {
		answer := receiptObject(raw)
		id := receiptString(answer["request_id"])
		_, bound := requested[id]
		_, duplicate := answered[id]
		if !bound || duplicate { return refusal(5, "unbound or duplicate evidence answer") }
		answered[id] = receiptString(answer["answer"])
	}
	if len(answered) != len(requested) { return refusal(5, "missing requested evidence answer") }
	sources := map[string]any{}
	for _, raw := range receiptArray(worker["sources"]) {
		source := receiptObject(raw)
		name := receiptString(source["path"])
		if _, exists := sources[name]; exists { return refusal(5, "duplicate worker source") }
		sources[name] = source["sha256"]
	}
	seen := map[string]bool{}
	observedDigests := map[string]string{}
	for _, raw := range receiptArray(receipt["sources_observed"]) {
		source := receiptObject(raw)
		name := receiptString(source["path"])
		want, exists := sources[name]
		if !exists || seen[name] || want != nil && want != source["sha256"] { return refusal(5, "unbound, changed or duplicate observed source") }
		seen[name] = true
		observedDigests[name] = receiptString(source["sha256"])
	}
	if len(seen) != len(sources) { return refusal(5, "source evidence is incomplete") }
	for id, request := range requested {
		if request["kind"] == "sha256" {
			answer := answered[id]
			if !validHex(answer, 64) || answer != observedDigests[receiptString(request["path"])] { return refusal(5, "SHA256 evidence answer differs from its observed source") }
		}
	}
	return nil
}

func validateTextExtraction(worker, receipt workerbridge.Document, x Extraction) error {
	bad := func(detail string) error { return refusal(5, detail) }
	canonicalReceipt, err := workerbridge.Canonical(receipt)
	if err != nil { return bad("receipt cannot be canonicalized for its evidence budget") }
	total := int64(len(canonicalReceipt))
	for _, name := range []string{"stdout", "stderr"} { total += receiptNumber(receiptObject(receiptObject(receipt["gate"])[name])["retained_bytes"]) }
	maxBytes := receiptNumber(receiptObject(worker["evidence"])["max_bytes"])
	// The frozen model counts canonical receipt bytes and retained diagnostics
	// even when a failed attempt has no artifact extraction at all.
	if total > maxBytes { return bad("diagnostic receipt exceeds the evidence budget") }
	extract := receiptObject(receipt["extract"])
	if extract == nil {
		if len(x.Payload) != 0 || len(x.Files) != 0 || len(x.Repros) != 0 || receiptNumber(receipt["exit_code"]) == 0 { return bad("text extraction lacks a receipt binding") }
		return nil
	}
	if len(x.Payload) == 0 || len(x.Payload) > 16<<20 || len(x.Files) == 0 || len(x.Files) > 256 || receiptNumber(extract["bytes"]) != int64(len(x.Payload)) || receiptNumber(extract["entries"]) != int64(len(x.Files)) || extract["sha256"] != digest(x.Payload) {
		return bad("extract bytes, entry count or digest differs from the host payload")
	}
	reader := bytes.NewReader(x.Payload)
	archive := tar.NewReader(reader)
	seen := map[string]bool{}
	for {
		header, err := archive.Next()
		if err == io.EOF { break }
		if err != nil { return bad("extract is not a complete text archive") }
		name := header.Name
		data, exists := x.Files[name]
		if (header.Typeflag != tar.TypeReg && header.Typeflag != tar.TypeRegA) || header.Mode != 0644 || !safeExtractPath(name) || seen[name] || !exists || header.Size <= 0 || header.Size > maxBytes || int64(len(data)) != header.Size {
			return bad("extract contains a link, duplicate, unsafe path, empty or unbound artifact")
		}
		body, err := io.ReadAll(io.LimitReader(archive, maxBytes+1))
		if err != nil || !bytes.Equal(body, data) || !utf8.Valid(body) || bytes.IndexByte(body, 0) >= 0 { return bad("extract artifact is not matching UTF-8 text") }
		total += int64(len(body))
		if total > maxBytes { return bad("receipt and extracted text exceed the evidence budget") }
		seen[name] = true
	}
	if len(seen) != len(x.Files) { return bad("host file map has entries absent from the archive") }
	// Only zero tar padding may follow its terminator. No appended second archive
	// or unbound payload is accepted under the first archive's entry inventory.
	for reader.Len() > 0 {
		b, _ := reader.ReadByte()
		if b != 0 { return bad("extract has trailing unbound payload") }
	}
	output := receiptObject(receipt["output"])
	if output == nil { return nil }
	if output["base_commit"] != receiptObject(worker["repo"])["commit"] { return bad("output changed the frozen base commit") }
	if output["kind"] == "branch" && output["repo_url"] != receiptObject(worker["repo"])["url"] { return bad("output repository differs from its task") }
	if output["kind"] == "branch" && len(receiptArray(output["files"])) > 0 && output["head_commit"] == output["base_commit"] { return bad("changed output has an unchanged branch commit") }
	var outputBytes int64
	if patch := receiptObject(output["patch"]); patch != nil {
		body, exists := x.Files[receiptString(patch["path"])]
		if !exists || patch["sha256"] != digest(body) || receiptNumber(patch["bytes"]) != int64(len(body)) { return bad("output patch differs from extracted bytes") }
		outputBytes += receiptNumber(patch["bytes"])
	}
	files := map[string]bool{}
	for _, raw := range receiptArray(output["files"]) {
		file := receiptObject(raw)
		name := receiptString(file["path"])
		allowed := false
		for _, value := range receiptArray(worker["write_paths"]) {
			prefix := receiptString(value)
			if name == prefix || strings.HasPrefix(name, strings.TrimSuffix(prefix, "/")+"/") { allowed = true; break }
		}
		if !allowed || files[name] { return bad("output manifest exceeds or duplicates its write surface") }
		files[name] = true
		outputBytes += receiptNumber(file["bytes"])
		if body, exists := x.Files[name]; exists {
			if file["operation"] == "delete" || file["sha256"] != digest(body) || receiptNumber(file["bytes"]) != int64(len(body)) { return bad("output file differs from extracted bytes") }
		}
		// A branch manifest describes repository files, while the text transport
		// may carry their patch. The trusted Extractor validates that manifest;
		// harvest must not reinterpret repository paths as archive entry names.
	}
	if outputBytes > receiptDefaultNumber(receiptObject(worker["policy"]), "output_max_bytes", 1048576) { return bad("output exceeds the immutable export budget") }
	return nil
}

func validateWorkerBinding(worker workerbridge.Document, wire []byte) error {
	seen := map[string]bool{}
	for _, raw := range receiptArray(worker["sources"]) {
		name := receiptString(receiptObject(raw)["path"])
		if seen[name] { return refusal(5, "duplicate worker source path") }
		seen[name] = true
	}
	requests := map[string]bool{}
	for _, raw := range receiptArray(receiptObject(worker["evidence"])["requests"]) {
		request := receiptObject(raw)
		id := receiptString(request["request_id"])
		if requests[id] { return refusal(5, "duplicate evidence request identity") }
		requests[id] = true
		if source, hasPath := request["path"]; hasPath && !seen[receiptString(source)] { return refusal(5, "evidence request has no source binding") }
	}
	if worker["bridge"] != nil {
		if _, err := workerbridge.WorkerToBridge(wire); err != nil { return refusal(5, "worker bridge projection differs") }
	}
	if receiptNumber(receiptObject(worker["gate"])["timeout_ms"]) > receiptDefaultNumber(receiptObject(worker["policy"]), "ttl_ms", 900000) { return refusal(5, "worker gate exceeds its immutable TTL") }
	return nil
}

func receiptDefaultNumber(object workerbridge.Document, key string, fallback int64) int64 {
	if value, ok := object[key]; ok { return receiptNumber(value) }
	return fallback
}
func normalizedReceiptGate(gate workerbridge.Document) int64 {
	switch gate["terminated_by"] {
	case "unknown": return 6
	case "timeout", "cancelled", "oom", "signal": return 5
	}
	if gate["raw_exit_code"] == nil { return 3 }
	if receiptNumber(gate["raw_exit_code"]) == 0 { return 0 }
	return 5
}
func receiptNetworkSubset(granted, requested workerbridge.Document) bool {
	if granted["mode"] == "none" { return true }
	if requested == nil || requested["mode"] == "none" { return false }
	allowed := map[string]bool{}
	for _, entry := range receiptArray(requested["allowlist"]) {
		b, err := workerbridge.Canonical(entry)
		if err != nil { return false }
		allowed[string(b)] = true
	}
	for _, entry := range receiptArray(granted["allowlist"]) {
		b, err := workerbridge.Canonical(entry)
		if err != nil || !allowed[string(b)] { return false }
	}
	return true
}

func safeExtractPath(name string) bool {
	if !present(name) || path.IsAbs(name) || path.Clean(name) != name || strings.ContainsAny(name, "/\\") { return false }
	for _, part := range strings.Split(name, "/") { if part == "" || part == "." || part == ".." || part == ".git" { return false } }
	switch path.Ext(name) { case ".md", ".json", ".txt", ".patch", ".diff": return true }
	return false
}

func waveRepositoryURL(repo string) string {
	if strings.HasPrefix(repo, "https://") { return repo }
	return "https://github.com/" + repo
}
func receiptObject(v any) workerbridge.Document { d, _ := v.(map[string]any); return d }
func receiptArray(v any) []any { a, _ := v.([]any); return a }
func receiptString(v any) string { s, _ := v.(string); return s }
func receiptNumber(v any) int64 { n, _ := strconv.ParseInt(receiptStringNumber(v), 10, 64); return n }
func receiptStringNumber(v any) string { n, _ := v.(json.Number); return string(n) }
func receiptEqual(a, b any) bool {
	x, e := workerbridge.Canonical(a)
	y, f := workerbridge.Canonical(b)
	return e == nil && f == nil && bytes.Equal(x, y)
}
