package w8muse

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"opensuperficies.local/srf/src/w8seat/patchpolicy"
	"opensuperficies.local/srf/src/workerbridge"
)

type Item struct {
	TaskID    string `json:"task_id"`
	Attempt   string `json:"attempt"`
	Objective string `json:"objective"`
	PatchPath string `json:"patch_path,omitempty"`
}
type Bundle struct {
	Tip    string
	Length int64
	Reader io.ReadCloser
}
type Repository interface {
	Prepare(context.Context, string, string, string) (Bundle, error)
}
type Publication struct {
	RepoDir, ScratchDir, Commit string
	Task                        workerbridge.Document
	Envelope                    workerbridge.Envelope
	Artifacts                   map[string]string
	Report                      []byte
}
type Publisher interface {
	Publish(context.Context, Publication) error
}
type Config struct {
	Dry                             bool
	CampaignDir, BridgeDir, RepoDir string
	Timeout                         time.Duration
	GateTimeoutMS                   int64
	PollInterval                    time.Duration
	Seat                            Seat
	Repository                      Repository
	Publisher                       Publisher
}

// Result is driver status, never a fabricated worker receipt. A local seat_lost
// is superseded only by an authoritative receipt fetched from the restarted seat.
type Result struct {
	TaskID                 string                 `json:"task_id"`
	Attempt                string                 `json:"attempt"`
	Handle                 string                 `json:"handle,omitempty"`
	ExitCode               int                    `json:"exit_code"`
	Reason                 string                 `json:"reason,omitempty"`
	BridgeResult           *workerbridge.Envelope `json:"bridge_result"`
	ObservedIntegrationTip string                 `json:"observed_integration_tip,omitempty"`
	Receipt                json.RawMessage        `json:"receipt,omitempty"`
	Source                 string                 `json:"source"`
}
type marker struct {
	PublicationAttempted bool            `json:"publication_attempted"`
	Published            bool            `json:"published"`
	PromptID             string          `json:"prompt_id"`
	Worker               json.RawMessage `json:"worker_task"`
	Handle               string          `json:"handle"`
	Tip                  string          `json:"observed_integration_tip"`
	Dry                  bool            `json:"dry"`
}

func validateConfig(c Config) error {
	if c.CampaignDir == "" || c.BridgeDir == "" || c.RepoDir == "" || c.Seat == nil || c.Repository == nil {
		return refusal(2, "input", "driver dependencies absent")
	}
	if c.Timeout <= 0 || c.Timeout > 17*time.Minute || c.GateTimeoutMS <= 0 || c.GateTimeoutMS > 900000 {
		return refusal(2, "input", "deadline or gate timeout")
	}
	if !c.Dry && c.Publisher == nil {
		return refusal(4, "unavailable", "publisher absent")
	}
	return nil
}
func attemptPaths(c Config, i Item) (string, string) {
	base := filepath.Join(c.CampaignDir, "w8-muse")
	return filepath.Join(base, "attempts", i.TaskID+"."+i.Attempt+".json"), filepath.Join(base, "local", i.TaskID+"."+i.Attempt+".json")
}
func persist(path string, v any, exclusive bool) error {
	if e := os.MkdirAll(filepath.Dir(path), 0700); e != nil {
		return e
	}
	b, e := json.MarshalIndent(v, "", "  ")
	if e != nil {
		return e
	}
	b = append(b, '\n')
	if exclusive {
		f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
		if e != nil {
			return e
		}
		_, e = f.Write(b)
		if e == nil {
			e = f.Sync()
		}
		ce := f.Close()
		if e == nil {
			e = ce
		}
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".status-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer os.Remove(name)
	if e = f.Chmod(0644); e == nil {
		_, e = f.Write(b)
	}
	if e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e == nil {
		e = ce
	}
	if e == nil {
		e = os.Rename(name, path)
	}
	return e
}
func markFailure(c Config, i Item, r Result, e error) (Result, error) {
	r.ExitCode = ExitCode(e)
	if r.Reason == "" {
		r.Reason = workerbridge.Class(e)
	}
	_, local := attemptPaths(c, i)
	if pe := persist(local, r, false); pe != nil {
		return r, refusal(6, "indeterminate", "local status write failed")
	}
	return r, e
}
func lost(c Config, i Item, r Result, step string, cause error) (Result, error) {
	r.Reason = "seat_lost"
	r.Source = "local_status"
	detail := "at " + step
	if cause != nil {
		detail += ": " + cause.Error()
	}
	return markFailure(c, i, r, refusal(6, "seat_lost", detail+"; fetch authoritative restart receipt; never resubmit"))
}
func boundContext(ctx context.Context, c Config, task workerbridge.Document) (context.Context, context.CancelFunc) {
	deadline := time.Now().Add(c.Timeout)
	if exp, e := time.Parse(time.RFC3339, text(task["expires_at_utc"])); e == nil && exp.Add(2*time.Minute).Before(deadline) {
		deadline = exp.Add(2 * time.Minute)
	}
	return context.WithDeadline(ctx, deadline)
}
func Run(ctx context.Context, c Config, i Item) (Result, error) {
	result := Result{TaskID: i.TaskID, Attempt: i.Attempt, Source: "driver"}
	if e := validateConfig(c); e != nil {
		result.ExitCode = ExitCode(e)
		return result, e
	}
	bridge, task, e := loadRegistration(c, i)
	if e != nil {
		result.ExitCode = ExitCode(e)
		return result, e
	}
	worker, e := workerbridge.BridgeToWorker(bridge, i.Objective, c.GateTimeoutMS)
	if e != nil {
		result.ExitCode = 2
		return result, refusal(2, "input", "unmappable dispatched task: "+e.Error())
	}
	promptID, e := promptBinding(c, i)
	if e != nil {
		result.ExitCode = ExitCode(e)
		return result, e
	}
	ctx, cancel := boundContext(ctx, c, task)
	defer cancel()
	patch := []byte(nil)
	if i.PatchPath != "" {
		patch, e = readBounded(i.PatchPath, MaxPatch)
		if e != nil {
			return markFailure(c, i, result, refusal(2, "input", "patch unavailable or exceeds cap"))
		}
		if _, e = patchpolicy.Check(patch, writePaths(task)); e != nil {
			return markFailure(c, i, result, refusal(2, "input", "patch policy"))
		}
	}
	markerPath, _ := attemptPaths(c, i)
	result.Handle = hash(canonical([]any{task["task_id"], task["attempt_id"], task["lease_id"]}))
	state := marker{Worker: worker, Dry: c.Dry, Handle: result.Handle, PromptID: promptID}
	// Reserve before any bundle or submit. Even a lost submit reply cannot cause
	// another execution on a later driver invocation.
	if e = persist(markerPath, state, true); e != nil {
		e = withCode(&result, 2, "attempt already reserved; reconcile, never resubmit")
		return result, e
	}
	bundle, e := c.Repository.Prepare(ctx, c.RepoDir, text(task["observed_source_commit"]), text(task["repo"]))
	if e != nil {
		return markFailure(c, i, result, e)
	}
	if bundle.Reader == nil {
		return markFailure(c, i, result, refusal(2, "input", "bundle stream absent"))
	}
	defer bundle.Reader.Close()
	if !shaPattern.MatchString(bundle.Tip) || bundle.Length <= 0 || bundle.Length > MaxBundle {
		return markFailure(c, i, result, refusal(2, "input", "bundle length or integration tip"))
	}
	result.ObservedIntegrationTip = bundle.Tip
	state.Tip = bundle.Tip
	if e = persist(markerPath, state, false); e != nil {
		return markFailure(c, i, result, refusal(6, "indeterminate", "attempt write failed"))
	}
	response, e := c.Seat.Exchange(ctx, Request{Verb: "submit", WorkerTask: worker, BundleLen: bundle.Length, PatchLen: int64(len(patch)), ObservedIntegrationTip: bundle.Tip}, bundle.Reader, patch)
	if e != nil {
		return lost(c, i, result, "submit", e)
	}
	if response.ExitCode != 0 && len(response.Receipt) == 0 {
		return markFailure(c, i, result, seatError(response))
	}
	if response.Handle != result.Handle {
		return lost(c, i, result, "submit handle", fmt.Errorf("seat handle %.12s", response.Handle))
	}
	result.Handle = response.Handle
	state.Handle = response.Handle
	if e = persist(markerPath, state, false); e != nil {
		return lost(c, i, result, "marker", e)
	}
	for {
		response, e = retryRead(ctx, c.Seat, Request{Verb: "wait", Handle: result.Handle})
		if e != nil {
			if ctx.Err() != nil {
				return cancelExpired(c, i, result)
			}
			return lost(c, i, result, "wait", e)
		}
		if response.ExitCode != 0 && len(response.Receipt) == 0 {
			return markFailure(c, i, result, seatError(response))
		}
		if response.Ready || len(response.Receipt) > 0 {
			break
		}
		interval := c.PollInterval
		if interval <= 0 {
			interval = time.Second
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return cancelExpired(c, i, result)
		case <-timer.C:
		}
	}
	return finish(ctx, c, i, task, worker, promptID, result)
}
func withCode(r *Result, code int, detail string) error {
	r.ExitCode = code
	r.Reason = "input"
	return refusal(code, "input", detail)
}
func seatError(r Response) error {
	code := r.ExitCode
	if code < 2 || code > 7 {
		code = 5
	}
	return refusal(code, r.Reason, "seat refusal")
}
func retryRead(ctx context.Context, s Seat, r Request) (Response, error) {
	var response Response
	var e error
	for n := 0; n < 3; n++ {
		if ctx.Err() != nil {
			return response, ctx.Err()
		}
		response, e = s.Exchange(ctx, r, nil, nil)
		if e == nil {
			return response, nil
		}
	}
	return response, e
}
func cancelExpired(c Config, i Item, r Result) (Result, error) {
	// The attempt context has expired; cancellation gets its own short bound.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	response, e := c.Seat.Exchange(ctx, Request{Verb: "cancel", Handle: r.Handle}, nil, nil)
	if e != nil {
		return lost(c, i, r, "cancel", e)
	}
	if response.ExitCode != 0 && response.ExitCode != 5 {
		return markFailure(c, i, r, seatError(response))
	}
	return markFailure(c, i, r, refusal(5, "timeout", "driver deadline; cancel sent; reconcile receipt"))
}

// Reconcile reads an existing attempt after seat restart. The original worker
// bytes and handle are reused, and neither Muse nor submit runs again.
func Reconcile(ctx context.Context, c Config, i Item) (Result, error) {
	r := Result{TaskID: i.TaskID, Attempt: i.Attempt, Source: "driver"}
	if e := validateConfig(c); e != nil {
		return r, e
	}
	if !taskPattern.MatchString(i.TaskID) || !attemptPattern.MatchString(i.Attempt) {
		return r, refusal(2, "input", "attempt identity")
	}
	markerPath, _ := attemptPaths(c, i)
	b, e := readBounded(markerPath, MaxHeader)
	if e != nil {
		return r, refusal(2, "input", "attempt marker absent")
	}
	var m marker
	if e = strictJSON(b, &m); e != nil {
		return r, e
	}
	if m.Dry != c.Dry {
		return r, refusal(2, "input", "attempt mode cannot change")
	}
	w, e := workerbridge.Parse(m.Worker)
	if e != nil {
		return r, e
	}
	task := obj(w["bridge"])
	if w["task_id"] != i.TaskID || w["attempt_id"] != i.Attempt {
		return r, refusal(2, "input", "attempt marker mismatch")
	}
	if m.Handle == "" {
		return lost(c, i, r, "reconcile marker", nil)
	} // unknown submit outcome needs seat operator reconciliation, no replay
	r.Handle = m.Handle
	r.ObservedIntegrationTip = m.Tip
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	return finish(ctx, c, i, task, m.Worker, m.PromptID, r)
}
func finish(ctx context.Context, c Config, i Item, task workerbridge.Document, worker []byte, promptID string, r Result) (Result, error) {
	response, e := retryRead(ctx, c.Seat, Request{Verb: "receipt", Handle: r.Handle})
	if e != nil {
		if ctx.Err() != nil {
			return cancelExpired(c, i, r)
		}
		return lost(c, i, r, "receipt", e)
	}
	if response.ExitCode != 0 && len(response.Receipt) == 0 {
		return markFailure(c, i, r, seatError(response))
	}
	if len(response.Receipt) == 0 {
		return markFailure(c, i, r, refusal(4, "unavailable", "receipt not ready"))
	}
	// Parity encoding validates receipt schema, digest, identifiers and output
	// paths, but parity bytes never enter an outbox or a live publisher.
	_, e = workerbridge.EncodeBridgeResult(worker, response.Receipt, promptID, false)
	if e != nil {
		return markFailure(c, i, r, refusal(5, "verification", "receipt binding or schema"))
	}
	receipt, _ := workerbridge.Parse(response.Receipt)
	r.Receipt = response.Receipt
	r.Source = "seat_receipt"
	r.BridgeResult = nil
	var env workerbridge.Envelope
	if !c.Dry {
		env, e = workerbridge.EncodeBridgeResult(worker, response.Receipt, promptID, true)
		if e == nil && env.Mode == "live" {
			r.BridgeResult = &env
		} else if e != nil && ExitCode(e) != 4 {
			return markFailure(c, i, r, e)
		}
	}
	rc := 0
	_, _ = fmt.Sscan(fmt.Sprint(receipt["exit_code"]), &rc)
	r.ExitCode = rc
	if rc != 0 {
		r.Reason = text(receipt["failure_reason"])
	}
	if r.BridgeResult == nil {
		if rc < 4 {
			r.ExitCode = 4
		}
		r.Reason = "unavailable"
	}
	if e = collect(c, i, r, response.Artifacts); e != nil {
		return markFailure(c, i, r, e)
	}
	// Acknowledgement never precedes the durable, atomic local collection.
	ack, e := c.Seat.Exchange(ctx, Request{Verb: "ack", Handle: r.Handle}, nil, nil)
	if e != nil {
		return lost(c, i, r, "ack", e)
	}
	if ack.ExitCode != 0 && !(len(ack.Receipt) > 0 && sameJSON(ack.Receipt, r.Receipt)) {
		return markFailure(c, i, r, seatError(ack))
	}
	if r.BridgeResult != nil {
		markerPath, _ := attemptPaths(c, i)
		b, pe := readBounded(markerPath, MaxHeader)
		var state marker
		if pe != nil {
			return markFailure(c, i, r, refusal(6, "indeterminate", "publication marker absent"))
		}
		if pe = strictJSON(b, &state); pe != nil {
			return markFailure(c, i, r, pe)
		}
		if state.PublicationAttempted && !state.Published {
			return markFailure(c, i, r, refusal(6, "indeterminate", "prior publication outcome unknown; never retry push"))
		}
		if !state.Published {
			state.PublicationAttempted = true
			if pe = persist(markerPath, state, false); pe != nil {
				return markFailure(c, i, r, refusal(6, "indeterminate", "publication reservation failed"))
			}
			if pe = c.Publisher.Publish(ctx, Publication{RepoDir: c.RepoDir, ScratchDir: filepath.Join(c.CampaignDir, "w8-muse", "publish", i.TaskID, i.Attempt), Commit: text(task["observed_source_commit"]), Task: task, Envelope: env, Artifacts: response.Artifacts, Report: publicationReport(r, response.Artifacts)}); pe != nil {
				return markFailure(c, i, r, pe)
			}
			state.Published = true
			if pe = persist(markerPath, state, false); pe != nil {
				return markFailure(c, i, r, refusal(6, "indeterminate", "publication completion write failed"))
			}
		}
	}
	_, local := attemptPaths(c, i)
	if e = persist(local, r, false); e != nil {
		return r, refusal(6, "indeterminate", "authoritative local status write failed")
	}
	return r, nil
}
func writePaths(task workerbridge.Document) []string {
	out := []string{}
	for _, v := range task["allowed_write_paths"].([]any) {
		out = append(out, text(v))
	}
	return out
}
func report(r Result) []byte {
	return []byte(fmt.Sprintf("# W8 attempt %s %s\n\nDriver rc: %d. Reason: %s. Source: %s.\nIntegration tip: %s.\n\nAuthoritative worker receipt is receipt.json. Host gates were not run by this driver.\n", r.TaskID, r.Attempt, r.ExitCode, r.Reason, r.Source, r.ObservedIntegrationTip))
}
func collect(c Config, i Item, r Result, artifacts map[string]string) error {
	if len(artifacts) > 64 {
		return refusal(5, "verification", "artifact count")
	}
	total := 0
	for name, body := range artifacts {
		total += len(body)
		if name != filepath.Base(name) || strings.ContainsAny(name, "/\\\x00") || len(name) > 100 || len(body) == 0 || !utf8.ValidString(body) || strings.ContainsRune(body, 0) || total > 102400 {
			return refusal(5, "verification", "artifact name/text/cap")
		}
		switch filepath.Ext(name) {
		case ".md", ".json", ".txt", ".patch", ".diff":
		default:
			return refusal(5, "verification", "artifact suffix")
		}
		switch name {
		case "receipt.json", "driver.json":
			return refusal(5, "verification", "reserved artifact name")
		}
	}
	parent := filepath.Join(c.CampaignDir, "w8-muse", "receipts", i.TaskID)
	if e := os.MkdirAll(parent, 0700); e != nil {
		return e
	}
	stage, e := os.MkdirTemp(parent, ".collect-")
	if e != nil {
		return e
	}
	defer os.RemoveAll(stage)
	write := func(name string, b []byte) error {
		f, e := os.OpenFile(filepath.Join(stage, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0644)
		if e != nil {
			return e
		}
		_, e = f.Write(b)
		if e == nil {
			e = f.Sync()
		}
		ce := f.Close()
		if e == nil {
			e = ce
		}
		return e
	}
	for name, body := range artifacts {
		if e = write(name, []byte(body)); e != nil {
			return e
		}
	}
	if _, ok := artifacts["report.md"]; !ok {
		if e = write("report.md", report(r)); e != nil {
			return e
		}
	}
	if e = write("receipt.json", r.Receipt); e != nil {
		return e
	}
	b, _ := json.MarshalIndent(r, "", "  ")
	if e = write("driver.json", append(b, '\n')); e != nil {
		return e
	}
	target := filepath.Join(parent, i.Attempt)
	if _, e = os.Lstat(target); e == nil {
		// Recovery must not replace prior evidence. Identical authoritative receipt
		// is already durable; a changed authoritative receipt is quarantined.
		prior, pe := readBounded(filepath.Join(target, "receipt.json"), MaxHeader)
		if pe != nil || !sameJSON(prior, r.Receipt) {
			return refusal(6, "indeterminate", "collected receipt changed")
		}
		return nil
	} else if !os.IsNotExist(e) {
		return e
	}
	if e = os.Rename(stage, target); e != nil {
		return e
	}
	d, e := os.Open(parent)
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}
func sameJSON(a, b []byte) bool {
	x, e := workerbridge.Decode(a)
	y, f := workerbridge.Decode(b)
	return e == nil && f == nil && same(x, y)
}

func publicationReport(r Result, artifacts map[string]string) []byte {
	if body, ok := artifacts["report.md"]; ok {
		return []byte(body)
	}
	return report(r)
}
