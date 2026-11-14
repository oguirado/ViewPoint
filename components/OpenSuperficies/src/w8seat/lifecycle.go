package w8seat

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"strings"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

func (s *Seat) exec(ctx context.Context, container, verb string, in io.Reader, limit int) ([]byte, error) {
	a, e := ExecArgv(container, verb)
	if e != nil {
		return nil, e
	}
	return capture(ctx, s.cfg.Runner, in, limit, a...)
}
func (s *Seat) execute(j *pending, d gobernador.SrfDecisionAdmision) {
	defer s.release(j)
	code, reason := 0, ""
	step, cleanupWhy, why := "lease", "", ""
	started := false
	var gate, obs, ext map[string]any
	var files map[string][]byte
	defer func() {
		if recover() != nil {
			code = 6
			reason = "indeterminate"
		}
		firstCode, firstReason := code, reason
		clean := map[string]any{"status": "not_applicable", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}
		if started {
			var e error
			files, clean, e = s.cleanup(j.attempt.Container, str(j.task["lease_id"]), strs(j.task["write_paths"]), files)
			if e != nil {
				cleanupWhy = e.Error()
				code = 6
				reason = "indeterminate"
			} else if obs != nil {
				obj(obs["probes"])["orphans_after_task"] = 0
			}
			if len(files) > 0 {
				ext = ExtractRecord(files)
			}
		}
		s.markFinalizing(j.attempt.Handle)
		if e := s.lessor.RevocarCapsula(str(j.task["lease_id"])); e != nil {
			code = 6
			reason = "indeterminate"
			clean = map[string]any{"status": "not_observed", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}
		}
		if started {
			s.governor.Liberar(d.Reserva, d.CPUs, "terminal")
		} else {
			s.governor.Devolver(d, "terminal")
		}
		if code == 6 {
			clean = map[string]any{"status": "not_observed", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}
		}
		if code != 0 && s.cfg.Diag != nil {
			fmt.Fprintf(s.cfg.Diag, "w8-seat attempt=%.12s step=%s code=%d reason=%s first_code=%d first_reason=%s why=%q cleanup=%q\n", j.attempt.Handle, step, code, reason, firstCode, firstReason, why, cleanupWhy)
		}
		if s.ctx.Err() == nil {
			s.finish(j, code, reason, gate, obs, ext, clean, files)
		}
	}()
	fail := func(e error, fallback int, r string) {
		code = ExitCode(e)
		if code == 0 || code == 6 {
			code = fallback
		}
		reason = r
		if code == 2 {
			reason = "contract"
		}
		if code == 4 {
			reason = "unavailable"
		}
		if code == 6 {
			reason = "indeterminate"
		}
		if code == 7 {
			reason = "operator_capability"
		}
	}
	if e := s.lessor.Validar(j.lease); e != nil {
		code = 5
		reason = s.cancelReason(j)
		return
	}
	step = "daemon"
	if e := daemonProbe(j.ctx, s.cfg.Runner); e != nil {
		code = ExitCode(e)
		reason = "unavailable"
		if code == 5 {
			reason = "containment"
		}
		return
	}
	step = "render"
	a := j.attempt
	spec := DefaultLaunchSpec(s.cfg.Policy)
	argv, e := RenderArgv(spec, a.Container, str(j.task["lease_id"]), s.cfg.Policy)
	if e != nil {
		fail(e, 5, "containment")
		return
	}
	step = "profile"
	if e = s.cfg.CheckProfile(s.cfg.Policy); e != nil {
		fail(e, 4, "unavailable")
		return
	}
	_ = s.store.Update(a.Handle, func(x *Attempt) { x.State = "STARTING" })
	// JournalCreate emits first. Track an ambiguous run failure as possibly created.
	step = "create"
	tracked := &createTracker{runner: s.cfg.Runner, onRun: func() { started = true }}
	e = w8builder.JournalCreate(j.ctx, tracked, w8builder.CapsuleJournal{Kind: "task", Lease: str(j.task["lease_id"]), Image: s.cfg.Policy.ImageSHA, Container: a.Container, CallerUID: int(a.UID)}, argv, nil, io.Discard)
	if e != nil {
		fail(e, 3, "setup")
		return
	}
	step = "inspect"
	inspected, e := capture(j.ctx, s.cfg.Runner, nil, MaxHeader, "docker", "inspect", a.Container)
	if e != nil {
		code = 6
		reason = "indeterminate"
		return
	}
	host, pid, e := inspectAdmission(inspected, s.cfg.Policy, a.Container, str(j.task["lease_id"]))
	if e != nil {
		code = 5
		reason = "containment"
		why = e.Error()
		return
	}
	step = "cgroup"
	cg, e := s.cfg.CgroupPath(pid)
	if e != nil {
		code = 4
		reason = "unavailable"
		return
	}
	s.mu.Lock()
	if live := s.active[a.Handle]; live != nil {
		live.cgroup = cg
	}
	s.mu.Unlock()
	var input bytes.Buffer
	h := map[string]any{"commit": obj(j.task["repo"])["commit"], "bundle_len": len(j.bundle), "patch_len": len(j.patch), "write_paths": j.task["write_paths"], "gateway": "", "sources": j.task["sources"], "evidence_requests": obj(j.task["evidence"])["requests"]}
	if e = WriteFrame(&input, h); e != nil {
		code = 2
		reason = "contract"
		return
	}
	stream := io.MultiReader(&input, bytes.NewReader(j.bundle), bytes.NewReader(j.patch))
	step = "ingest"
	ingested, runErr := s.exec(j.ctx, a.Container, "ingest", stream, MaxHeader)
	result, e := decodeObject(ingested)
	if e != nil {
		code = 5
		reason = "containment"
		return
	}
	step = "probes"
	if e = completeProbes(host, obj(result["probes"])); e != nil {
		code = 5
		reason = "containment"
		why = e.Error()
		return
	}
	obs = observations(a.Container, s.cfg.Policy, host)
	step = "head"
	if number(result["rc"]) == 3 || str(result["head"]) != obj(j.task["repo"])["commit"] {
		code = 3
		reason = "setup"
		return
	}
	if runErr != nil || number(result["rc"]) != 0 {
		code = 5
		reason = "containment"
		return
	}
	step = "gate"
	_ = s.store.Update(a.Handle, func(x *Attempt) { x.State = "GATING" })
	gateStart := time.Now()
	taskGate := obj(j.task["gate"])
	type done struct {
		data []byte
		err  error
	}
	ch := make(chan done, 1)
	go func() {
		b, e := s.exec(j.ctx, a.Container, "gate", bytes.NewReader(canonical(map[string]any{"argv": taskGate["argv"], "timeout_ms": taskGate["timeout_ms"]})), MaxHeader)
		ch <- done{b, e}
	}()
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	var finished done
	waiting := true
	for waiting {
		select {
		case finished = <-ch:
			waiting = false
		case <-tick.C:
			statusCtx, cancel := context.WithTimeout(j.ctx, 400*time.Millisecond)
			status, e := s.exec(statusCtx, a.Container, "status", nil, 65536)
			cancel()
			if e == nil {
				if _, e := decodeObject(status); e != nil {
					j.cancel()
				}
			}
		case <-j.ctx.Done():
			finished = <-ch
			waiting = false
		}
	}
	duration := time.Since(gateStart).Milliseconds()
	memoryPeak, pidPeak, peakErr := s.cfg.ReadPeaks(cg)
	if peakErr == nil {
		obs["peak_memory_bytes"] = memoryPeak
		obs["peak_pids"] = pidPeak
	}

	if j.ctx.Err() != nil {
		code = 5
		reason = s.cancelReason(j)
		return
	}
	step = "gate-result"
	state, e := decodeObject(finished.data)
	if e != nil {
		code = 5
		reason = "verification"
		return
	}
	workspacePeak, wsOK := state["workspace_peak_bytes"]
	if wsOK {
		obs["workspace_peak_bytes"] = workspacePeak
	}
	raw, ok := state["raw_status"]
	if !ok {
		code = 5
		reason = "verification"
		return
	}
	explicit, exists := state["rc"]
	if !exists || number(explicit) < 0 || number(explicit) > 7 || number(explicit) == 1 {
		code = 5
		reason = "verification"
		return
	}
	code = int(number(explicit))
	reason = "gate"
	switch code {
	case 0:
		reason = ""
	case 2:
		reason = "contract"
	case 3:
		reason = "execution"
	case 4:
		reason = "unavailable"
	case 5:
		if number(raw) == 0 {
			reason = "verification"
		}
	case 6:
		reason = "indeterminate"
	case 7:
		reason = "operator_capability"
	}
	if state["timed_out"] == true {
		code = 5
		reason = "timeout"
	} else if str(state["signal"]) != "" {
		code = 5
		reason = "gate"
	}
	if finished.err != nil && code == 0 {
		code = 3
		reason = "execution"
	}
	var terminated any
	if state["timed_out"] == true {
		terminated = "timeout"
	} else if str(state["signal"]) != "" {
		terminated = "signal"
	}
	// The runner reports the combined child stdout/stderr ring, preserving discarded-byte counts.
	total, totalOK := state["stdout_total_bytes"]
	retained, retainedOK := state["stdout_retained_bytes"]
	sha, shaOK := state["stdout_sha256"]
	truncated, truncatedOK := state["stdout_truncated"]
	if !totalOK || !retainedOK || !shaOK || !truncatedOK || number(total) < number(retained) || number(retained) < 0 || number(retained) > 65536 || !hex64.MatchString(str(sha)) {
		code = 5
		reason = "verification"
		return
	}
	if _, ok := truncated.(bool); !ok {
		code = 5
		reason = "verification"
		return
	}
	gate = map[string]any{"argv": taskGate["argv"], "cwd": taskGate["cwd"], "timeout_ms": taskGate["timeout_ms"], "raw_exit_code": raw, "exit_code": code, "terminated_by": terminated, "duration_ms": duration, "stdout": map[string]any{"sha256": sha, "retained_bytes": retained, "total_bytes": total, "truncated": truncated}, "stderr": map[string]any{"sha256": nil, "retained_bytes": 0, "total_bytes": 0, "truncated": false}}

	_ = s.store.Update(a.Handle, func(x *Attempt) { x.State = "COLLECTING" })
	out, e := s.exec(j.ctx, a.Container, "extract", nil, MaxArchive+1)
	if e != nil {
		code = 5
		reason = "verification"
		return
	}
	finalStatus, statusErr := s.exec(j.ctx, a.Container, "status", nil, 1<<20)
	finalState, decodeErr := decodeObject(finalStatus)
	finalPeak, present := finalState["workspace_peak_bytes"]
	peakValue, validPeak := measuredWorkspacePeak(finalPeak)
	wsOK = statusErr == nil && decodeErr == nil && present && validPeak && peakValue >= number(workspacePeak)
	if wsOK {
		obs["workspace_peak_bytes"] = finalPeak
	} else {
		obs["workspace_peak_bytes"] = nil
	}
	files, e = AdmitExtract(bytes.NewReader(out), strs(j.task["write_paths"]))
	if e != nil {
		code = 5
		reason = "verification"
		return
	}
	if code == 0 && number(retained) > 0 {
		log, ok := files["gate.log.txt"]
		if !ok || int64(len(log)) != number(retained) || hash(log) != str(sha) {
			code = 5
			reason = "verification"
		}
	}
	if (number(total) > number(retained)) != (truncated == true) {
		code = 5
		reason = "verification"
	}
	if code == 0 {
		if peakErr != nil || !wsOK {
			code = 4
			reason = "unavailable"
		} else {
			reason = ""
		}
	}
}

type createTracker struct {
	runner w8builder.Runner
	onRun  func()
}

func (t *createTracker) Run(ctx context.Context, a []string, in io.Reader, out io.Writer) error {
	if len(a) > 1 && a[0] == "docker" && a[1] == "run" {
		t.onRun()
	}
	return t.runner.Run(ctx, a, in, out)
}
func (s *Seat) cleanup(container, lease string, paths []string, files map[string][]byte) (map[string][]byte, map[string]any, error) {
	failed := map[string]any{"status": "failed", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": false}
	op := func(verb string, cap int) ([]byte, error) {
		ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
		defer cancel()
		return s.exec(ctx, container, verb, nil, cap)
	}
	if files == nil {
		if b, e := op("extract", MaxArchive+1); e == nil {
			files, _ = AdmitExtract(bytes.NewReader(b), paths)
		}
	}
	purged, purgeErr := op("purge", 65536)
	p, e := decodeObject(purged)
	workspaceOK := e == nil && p["remaining_workspace_bytes"] != nil && number(p["remaining_workspace_bytes"]) == 0
	ctx, cancel := context.WithTimeout(context.Background(), 400*time.Millisecond)
	top, topErr := capture(ctx, s.cfg.Runner, nil, 65536, "docker", "top", container, "-eo", "pid,stat,comm")
	cancel()
	noOrphans := topErr == nil && safeTop(top)
	ctx, cancel = context.WithTimeout(context.Background(), 500*time.Millisecond)
	_, rmErr := capture(ctx, s.cfg.Runner, nil, 65536, "docker", "rm", "-f", container)
	cancel()
	ctx, cancel = context.WithTimeout(context.Background(), 300*time.Millisecond)
	remaining, psErr := capture(ctx, s.cfg.Runner, nil, 65536, "docker", "ps", "-a", "--filter", "label=w8.lease="+lease, "--quiet")
	cancel()
	if purgeErr != nil || !workspaceOK || !noOrphans || rmErr != nil || psErr != nil || len(bytes.TrimSpace(remaining)) != 0 {
		why := fmt.Sprintf("cleanup witness incomplete: purge=%t workspace=%t top=%t orphans=%t rm=%t ps=%t remaining=%t", purgeErr == nil, workspaceOK, topErr == nil, noOrphans, rmErr == nil, psErr == nil, len(bytes.TrimSpace(remaining)) == 0)
		return files, failed, refusal(6, "indeterminate", why)
	}
	return files, map[string]any{"status": "verified", "remaining_pids": 0, "remaining_workspace_bytes": 0, "resources_released": true}, nil
}

// safeTop accepts exactly the idle body (tini + sleep). dockerd refuses `top`
// without a PID column ("Couldn't find PID field in ps output"), so the PID is
// requested and only checked for shape.
func safeTop(b []byte) bool {
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) != 3 || strings.Join(strings.Fields(lines[0]), " ") != "PID STAT COMMAND" {
		return false
	}
	seen := false
	names := map[string]bool{}
	for _, line := range lines[1:] {
		f := strings.Fields(line)
		if len(f) != 3 || !isPID(f[0]) || strings.Contains(f[1], "Z") || (f[2] != "tini" && f[2] != "sleep") {
			return false
		}
		if names[f[2]] {
			return false
		}
		names[f[2]] = true
		seen = true
	}
	return seen && names["tini"] && names["sleep"]
}
func isPID(s string) bool {
	if s == "" || len(s) > 10 || s[0] == '0' {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

// Janitor is the sole writer of restart rc6 receipts. A driver seat_lost record
// is local transport evidence and is never ingested as a worker receipt.
func (s *Seat) Janitor(ctx context.Context, restart bool) error {
	names, e := capture(ctx, s.cfg.Runner, nil, 65536, "docker", "ps", "-a", "--filter", "label=w8.seat=1", "--format", "{{.Names}}")
	if e != nil {
		return refusal(4, "unavailable", "janitor daemon unavailable")
	}
	containers := strings.Fields(string(names))
	if len(containers) > 512 {
		return refusal(6, "indeterminate", "janitor container cap")
	}
	rows := s.store.All()
	liveContainers := map[string]bool{}
	s.mu.Lock()
	for _, a := range rows {
		if _, ok := s.active[a.Handle]; ok {
			liveContainers[a.Container] = true
		}
	}
	s.mu.Unlock()
	for _, container := range containers {
		if !id12.MatchString(container) {
			return refusal(6, "indeterminate", "unknown labelled container")
		}
		if liveContainers[container] {
			continue
		}
		if _, e = capture(ctx, s.cfg.Runner, nil, 65536, "docker", "rm", "-f", container); e != nil {
			return refusal(6, "indeterminate", "janitor remove failed")
		}
		why := "no_lease"
		if restart {
			why = "restart"
		}
		if e = s.journal(ctx, "W8_CAPSULE_REAPED container="+container+" reason="+why+"\n"); e != nil {
			return e
		}
	}
	for _, a := range rows {
		s.mu.Lock()
		_, live := s.active[a.Handle]
		s.mu.Unlock()
		if live || len(a.Response.Receipt) > 0 || a.State == "REFUSED" {
			continue
		}
		task, e := workerbridge.Parse(a.Task)
		if e != nil {
			return refusal(6, "indeterminate", "restart task corrupt")
		}
		if e = s.lessor.RevocarCapsula(str(task["lease_id"])); e != nil {
			return e
		}
		job := &pending{attempt: a, task: task}
		s.finish(job, 6, "indeterminate", nil, nil, nil, map[string]any{"status": "not_observed", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}, nil)
	}
	return s.store.Reap(time.Now(), func(line string) error { return s.journal(ctx, line) })
}
