package workerbridge

import (
	"encoding/json"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// CheckInput is an explicit, immutable snapshot for offline canon parity.
// Caller-supplied leases, changes and answers are NOT trusted runtime evidence.
// This model never grants a lease, persists a receipt, or admits a live result.
// Replay/prompt/blob inputs are optional only for legacy unbound fixtures.
type CheckInput struct {
	Task           json.RawMessage   `json:"task"`
	Result         json.RawMessage   `json:"result"`
	ResultWire     string            `json:"result_wire,omitempty"`
	Report         string            `json:"report"`
	Lease          json.RawMessage   `json:"lease"`
	Head           *int64            `json:"head"`
	Now            string            `json:"now"`
	Changes        []Change          `json:"changes"`
	Expected       []Expected        `json:"expected"`
	PRBranch       *string           `json:"pr_branch,omitempty"`
	Blobs          map[string]string `json:"blobs,omitempty"`
	ExpectedDoc    *PromptBinding    `json:"expected_doc,omitempty"`
	PromptAtCommit *string           `json:"prompt_at_commit,omitempty"`
	LocalPrompt    *string           `json:"local_prompt,omitempty"`
	Stored         *StoredKey        `json:"stored,omitempty"`
}
type Change struct {
	Status string   `json:"status"`
	Mode   string   `json:"mode"`
	Paths  []string `json:"paths"`
}
type Expected struct {
	Index int     `json:"request_index"`
	Kind  string  `json:"kind"`
	Value *string `json:"expected"`
}
type PromptBinding struct {
	Nonce  string          `json:"nonce"`
	Prompt json.RawMessage `json:"prompt"`
}
type StoredKey struct {
	BodySHA256 string `json:"body_sha256"`
	Verdict    string `json:"verdict"`
}
type Verdict struct {
	Status string `json:"status"`
	Class  string `json:"reject_class,omitempty"`
}

func rejectVerdict(class string) Verdict { return Verdict{Status: "REJECT", Class: class} }

var writers = map[string]bool{"codex-cloud": true, "jules": true, "perplexity-mcp": true, "gemini-spark-mcp": true, "chatgpt-mcp": true}
var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b`),
	regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{22,255}\b`),
	regexp.MustCompile(`\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`),
	regexp.MustCompile(`-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----`),
	regexp.MustCompile(`\bxox[abposr]-[A-Za-z0-9-]{10,}`),
	regexp.MustCompile(`\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}`),
	regexp.MustCompile(`\bAIza[0-9A-Za-z_-]{35}\b`),
	regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`),
	regexp.MustCompile(`(?i)\bauthorization\s*:\s*(?:bearer|token)\s+[A-Za-z0-9_\-.=+/]{16,}`),
	regexp.MustCompile(`(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret|password|passwd)["']?\s*[:=]\s*["']?[A-Za-z0-9_\-./+=]{16,}`),
}

func secret(data string) bool {
	for _, rx := range secretPatterns {
		if rx.MatchString(data) {
			return true
		}
	}
	return false
}

var blockerRX = regexp.MustCompile(`(?i)\b(?:blocked|blocker|cannot|can not|could not|couldn't|unable|unavailable|inaccessible|no access|not (?:available|accessible|found|visible|exposed|imported|readable|resolvable)|does not exist|doesn't exist|404|denied|refused|failed)\b`)
var clauseRX = regexp.MustCompile(`[.;:!?\n]`)
var wordRX = regexp.MustCompile(`[a-z']+`)

func statedBlocker(s string) bool {
	for _, loc := range blockerRX.FindAllStringIndex(s, -1) {
		start := max(0, loc[0]-60)
		clauses := clauseRX.Split(s[start:loc[0]], -1)
		words := wordRX.FindAllString(strings.ToLower(clauses[len(clauses)-1]), -1)
		words = words[max(0, len(words)-3):]
		negated := false
		for _, w := range words {
			switch w {
			case "no", "not", "never", "without", "nothing", "none", "neither", "nor":
				negated = true
			}
			if strings.HasSuffix(w, "n't") {
				negated = true
			}
		}
		if !negated {
			return true
		}
	}
	return false
}
func pathAllowed(p string, allowed []any) bool {
	for _, raw := range allowed {
		s := str(raw)
		if p == s || strings.HasSuffix(s, "/") && strings.HasPrefix(p, s) {
			return true
		}
	}
	return false
}
func ownPath(p string, task Document) bool {
	return p == task["result_path"] || p == task["report_path"]
}
func branchName(s string) string {
	for _, p := range []string{"refs/remotes/origin/", "refs/heads/", "origin/"} {
		if strings.HasPrefix(s, p) {
			return strings.TrimPrefix(s, p)
		}
	}
	return s
}

// CheckResult compares status and Reject class with bridge_core.check on an
// offline snapshot. It is deliberately separate from encoding and outbox access.
func CheckResult(in CheckInput) Verdict {
	resultWire := []byte(in.Result)
	if in.ResultWire != "" {
		resultWire = []byte(in.ResultWire)
	}
	r, e := Parse(resultWire)
	if e != nil {
		return rejectVerdict(Class(e))
	}
	if e = validate(r, "bridge_result_v1"); e != nil {
		return rejectVerdict(Class(e))
	}
	task, e := Parse(in.Task)
	if e != nil {
		return rejectVerdict("MALFORMED")
	}
	if secret(string(resultWire)) || secret(in.Report) {
		return rejectVerdict("SECRET_DETECTED")
	}
	if in.Blobs != nil {
		for _, c := range in.Changes {
			if c.Status != "D" && len(c.Paths) > 0 && secret(in.Blobs[c.Paths[len(c.Paths)-1]]) {
				return rejectVerdict("SECRET_DETECTED")
			}
		}
	}
	lease, e := Parse(in.Lease)
	if e != nil || !equal(task["lease_id"], r["lease_id"]) {
		return rejectVerdict("REJECT_NO_LEASE")
	}
	for _, key := range []string{"thread", "agent", "worker_id", "task_id", "attempt_id", "key"} {
		if !equal(lease[key], r[key]) || !equal(task[key], r[key]) {
			return rejectVerdict("REJECT_NO_LEASE")
		}
	}
	taskBytes, e := BridgeCanonical(task)
	if e != nil || lease["task_sha256"] != hash(taskBytes) {
		return rejectVerdict("REJECT_NO_LEASE")
	}
	resultBytes, _ := BridgeCanonical(r)
	if in.Stored != nil {
		if in.Stored.BodySHA256 != hash(resultBytes) {
			return rejectVerdict("REJECT_KEY_REUSE")
		}
		class := in.Stored.Verdict
		if class == "ACCEPT" {
			class = ""
		}
		return Verdict{Status: "REPLAY", Class: class}
	}
	now, e := time.Parse(time.RFC3339, in.Now)
	if e != nil {
		return rejectVerdict("MALFORMED")
	}
	expiry, e := time.Parse(time.RFC3339, str(lease["expires_at_utc"]))
	if e != nil {
		return rejectVerdict("MALFORMED")
	}
	if lease["status"] != "granted" || !now.Before(expiry) {
		return rejectVerdict("EXPIRED_LEASE")
	}
	if !equal(r["base_seq"], lease["base_seq"]) || (in.Head == nil || num(r["base_seq"]) != *in.Head) {
		return rejectVerdict("STALE")
	}
	sb, content := object(r["source_binding"]), object(r["content"])
	for _, key := range []string{"surface", "role", "done_signal"} {
		if !equal(r[key], task[key]) {
			return rejectVerdict("MALFORMED")
		}
	}
	if !equal(sb["requested_ref"], task["requested_ref"]) || !equal(content["path"], task["report_path"]) {
		return rejectVerdict("MALFORMED")
	}
	writer := task["role"] == "builder" && writers[str(task["surface"])]
	artifacts := array(r["artifacts"])
	if !writer {
		_, pr := r["pr_url"]
		_, branch := r["branch"]
		if len(artifacts) > 0 || len(in.Changes) > 0 || pr || branch || in.PRBranch != nil {
			return rejectVerdict("WRITE_FROM_READ_ONLY_SURFACE")
		}
	} else {
		if in.PRBranch != nil {
			name := branchName(*in.PRBranch)
			want := str(task["writer_branch"])
			suffix := strings.TrimPrefix(name, want+"-")
			rx := regexp.MustCompile(`^[0-9]+$`)
			if name != want && (!strings.HasPrefix(name, want+"-") || !rx.MatchString(suffix)) {
				return rejectVerdict("MALFORMED")
			}
		}
		for _, c := range in.Changes {
			for _, p := range c.Paths {
				if strings.HasPrefix(p, ".wavemind/bridge/inbox/") || p == ".wavemind/bridge/PROTOCOL.md" || p == "PROTOCOL.md" {
					return rejectVerdict("INBOX_TOUCHED")
				}
			}
		}
		allowed := append(append([]any{}, array(task["allowed_write_paths"])...), task["result_path"], task["report_path"])
		for _, c := range in.Changes {
			for _, p := range c.Paths {
				if !cleanPath(p) || !pathAllowed(p, allowed) || (strings.HasPrefix(p, ".git/") || strings.HasPrefix(p, ".wavemind/")) && !ownPath(p, task) {
					return rejectVerdict("PATH_OUTSIDE_SURFACE")
				}
			}
			if c.Mode == "120000" || c.Mode == "160000" {
				return rejectVerdict("PATH_OUTSIDE_SURFACE")
			}
		}
		for _, a := range artifacts {
			p := str(object(a)["path"])
			if !cleanPath(p) || !pathAllowed(p, allowed) {
				return rejectVerdict("PATH_OUTSIDE_SURFACE")
			}
		}
	}
	if strings.TrimSpace(in.Report) == "" {
		return rejectVerdict("HOLLOW_RESULT")
	}
	answers := map[int]string{}
	for _, raw := range array(r["evidence_answers"]) {
		a := object(raw)
		index := int(num(a["request_index"]))
		if _, ok := answers[index]; ok || index >= len(array(task["evidence_requests"])) {
			return rejectVerdict("MALFORMED")
		}
		answers[index] = str(a["answer"])
	}
	if len(answers) != len(array(task["evidence_requests"])) {
		return rejectVerdict("HOLLOW_RESULT")
	}
	blocked := len(answers) > 0 && statedBlocker(str(sb["evidence"]))
	for _, a := range answers {
		if a != "ABSENT" {
			blocked = false
		}
	}
	if writer && !blocked {
		substantive := false
		for _, c := range in.Changes {
			for _, p := range c.Paths {
				if !ownPath(p, task) {
					substantive = true
				}
			}
		}
		if !substantive {
			return rejectVerdict("HOLLOW_RESULT")
		}
	}
	if in.ExpectedDoc != nil && len(in.ExpectedDoc.Prompt) > 0 && string(in.ExpectedDoc.Prompt) != "null" && !blocked {
		if !strings.Contains(str(sb["evidence"]), in.ExpectedDoc.Nonce) || in.LocalPrompt == nil || in.PromptAtCommit == nil || *in.LocalPrompt != *in.PromptAtCommit {
			return rejectVerdict("PROMPT_MISMATCH")
		}
	}
	if content["sha256"] != nil && content["sha256"] != hash([]byte(in.Report)) {
		return rejectVerdict("EVIDENCE_MISMATCH")
	}
	if in.Blobs != nil {
		for _, raw := range artifacts {
			a := object(raw)
			blob, ok := in.Blobs[str(a["path"])]
			if !ok {
				return rejectVerdict("HOLLOW_RESULT")
			}
			if a["sha256"] != nil && a["sha256"] != hash([]byte(blob)) {
				return rejectVerdict("EVIDENCE_MISMATCH")
			}
		}
	}
	if in.Expected == nil {
		return rejectVerdict("MALFORMED")
	}
	if blocked {
		return Verdict{Status: "BLOCKED", Class: "BLOCKED_RECEIPT"}
	}
	for _, expected := range in.Expected {
		if expected.Value == nil {
			continue
		}
		answer := answers[expected.Index]
		if expected.Kind == "sha256" {
			answer = strings.ToLower(strings.TrimSpace(answer))
		} else {
			answer = strings.TrimSuffix(strings.TrimSuffix(answer, "\n"), "\r")
		}
		if answer != *expected.Value {
			return rejectVerdict("EVIDENCE_MISMATCH")
		}
	}
	if !utf8.ValidString(in.Report) {
		return rejectVerdict("MALFORMED")
	}
	return Verdict{Status: "ACCEPT"}
}
