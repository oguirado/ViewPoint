// Package switchboard is the host-only, fenced W8/OpenSwitchboard protocol adapter.
// It contains no guest connector, no network client, and no implicit file-bus fallback.
package switchboard

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"opensuperficies.local/srf/src/workerbridge"
)

const Schema = "os.cu.speech-act/v1"
const ReproNormalizer = "w8-repro/1"

// Refusal carries the W8 exit taxonomy; 1 is never emitted.
type Refusal struct {
	Code   int
	Reason string
}

func (e *Refusal) Error() string           { return fmt.Sprintf("switchboard rc=%d: %s", e.Code, e.Reason) }
func refuse(code int, reason string) error { return &Refusal{Code: code, Reason: reason} }
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var e *Refusal
	if errors.As(err, &e) && e.Code >= 2 && e.Code <= 7 {
		return e.Code
	}
	return 5
}

var identifierRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)
var shaRE = regexp.MustCompile(`^[0-9a-f]{64}$`)
var pathRE = regexp.MustCompile(`^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$`)
var refRE = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
var base64RunRE = regexp.MustCompile(`[A-Za-z0-9+/]{512}[A-Za-z0-9+/]{512}[A-Za-z0-9+/]*={0,2}`)

func validID(s string) bool { return len(s) >= 1 && len(s) <= 128 && identifierRE.MatchString(s) }
func validPath(s string) bool {
	if len(s) < 1 || len(s) > 1024 || !pathRE.MatchString(s) {
		return false
	}
	for _, p := range strings.Split(s, "/") {
		if p == "." || p == ".." || p == ".git" {
			return false
		}
	}
	return true
}
func digest(raw []byte) string    { d := sha256.Sum256(raw); return hex.EncodeToString(d[:]) }
func bodyRef(raw []byte) string   { return "sha256:" + digest(raw) }
func validateRef(ref string) bool { return len(ref) == 71 && refRE.MatchString(ref) }

// Grant is a snapshot of an already-authorized host lease, never user authority.
type Grant struct {
	WaveID, TaskID, AttemptID, LeaseID string
	TargetID, WorkerID                 string
	Epoch                              uint64
	SourceCommit                       string
	ExpiresMS                          int64
}

func (g Grant) validate() error {
	for _, s := range []string{g.WaveID, g.TaskID, g.AttemptID, g.LeaseID, g.TargetID, g.WorkerID} {
		if !validID(s) {
			return refuse(2, "invalid grant identifier")
		}
	}
	if len(g.SourceCommit) != 40 || !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(g.SourceCommit) || g.Epoch == 0 || g.ExpiresMS <= 0 {
		return refuse(4, "grant missing an authenticated commit, epoch or expiry")
	}
	return nil
}

// SpeechAct contains only the closed W8 fields; original bytes are private.
type SpeechAct struct {
	Schema      string   `json:"schema"`
	Act         string   `json:"act"`
	WaveID      string   `json:"wave_id"`
	TaskID      string   `json:"task_id"`
	AttemptID   string   `json:"attempt_id"`
	LeaseID     string   `json:"lease_id"`
	Seq         int      `json:"seq"`
	ReproSHA256 *string  `json:"repro_sha256"`
	Affected    []string `json:"affected"`
	Status      *string  `json:"status"`
	Body        string   `json:"body"`
	raw         []byte
}

func (s SpeechAct) Raw() []byte { return append([]byte(nil), s.raw...) }

// ParseSpeechAct uses the repository's strict duplicate-key, integer-only,
// Unicode-validating parser and rejects missing or extra fields.
func ParseSpeechAct(raw []byte, g Grant) (SpeechAct, error) {
	var s SpeechAct
	if e := g.validate(); e != nil {
		return s, e
	}
	obj, e := workerbridge.Parse(raw)
	if e != nil {
		return s, refuse(2, "malformed or ambiguous JSON: "+e.Error())
	}
	required := []string{"schema", "act", "wave_id", "task_id", "attempt_id", "lease_id", "seq", "repro_sha256", "affected", "status", "body"}
	if len(obj) != len(required) {
		return s, refuse(2, "speech-act field count not closed")
	}
	for _, k := range required {
		if _, ok := obj[k]; !ok {
			return s, refuse(2, "missing speech-act field "+k)
		}
	}
	read := func(k string) (string, error) {
		v, ok := obj[k].(string)
		if !ok {
			return "", refuse(2, "non-string "+k)
		}
		return v, nil
	}
	if s.Schema, e = read("schema"); e != nil {
		return SpeechAct{}, e
	}
	if s.Schema != Schema {
		return SpeechAct{}, refuse(2, "unknown speech-act schema")
	}
	if s.Act, e = read("act"); e != nil {
		return SpeechAct{}, e
	}
	switch s.Act {
	case "finding", "repro", "affected", "status":
	default:
		return SpeechAct{}, refuse(2, "unknown speech-act")
	}
	ids := []struct {
		key  string
		want string
		to   *string
	}{{"wave_id", g.WaveID, &s.WaveID}, {"task_id", g.TaskID, &s.TaskID}, {"attempt_id", g.AttemptID, &s.AttemptID}, {"lease_id", g.LeaseID, &s.LeaseID}}
	for _, id := range ids {
		v, er := read(id.key)
		if er != nil {
			return SpeechAct{}, er
		}
		if !validID(v) || v != id.want {
			return SpeechAct{}, refuse(5, "unbound "+id.key)
		}
		*id.to = v
	}
	n, ok := obj["seq"].(json.Number)
	if !ok {
		return SpeechAct{}, refuse(2, "seq must be an integer")
	}
	seq, er := strconv.ParseInt(string(n), 10, 64)
	if er != nil || seq < 0 || seq > 1000000 {
		return SpeechAct{}, refuse(2, "seq out of range")
	}
	s.Seq = int(seq)
	if obj["repro_sha256"] != nil {
		v, ok := obj["repro_sha256"].(string)
		if !ok || !shaRE.MatchString(v) {
			return SpeechAct{}, refuse(2, "invalid repro hash")
		}
		s.ReproSHA256 = &v
	}
	if (s.Act == "finding" || s.Act == "repro") && s.ReproSHA256 == nil {
		return SpeechAct{}, refuse(2, "finding/repro requires hash")
	}
	paths, ok := obj["affected"].([]any)
	if !ok || len(paths) > 64 || (s.Act == "affected" && len(paths) == 0) {
		return SpeechAct{}, refuse(2, "invalid affected list")
	}
	s.Affected = make([]string, 0, len(paths))
	seen := map[string]bool{}
	for _, x := range paths {
		p, ok := x.(string)
		if !ok || !validPath(p) || seen[p] {
			return SpeechAct{}, refuse(2, "unsafe or duplicate affected path")
		}
		seen[p] = true
		s.Affected = append(s.Affected, p)
	}
	if obj["status"] != nil {
		v, ok := obj["status"].(string)
		if !ok {
			return SpeechAct{}, refuse(2, "status must be string or null")
		}
		switch v {
		case "working", "blocked", "done", "stuck":
		default:
			return SpeechAct{}, refuse(2, "unknown status")
		}
		s.Status = &v
	}
	if (s.Act == "status") != (s.Status != nil) {
		return SpeechAct{}, refuse(2, "status presence does not match act")
	}
	if s.Body, e = read("body"); e != nil {
		return SpeechAct{}, e
	}
	if !utf8.ValidString(s.Body) || utf8.RuneCountInString(s.Body) > 2048 || base64RunRE.MatchString(s.Body) {
		return SpeechAct{}, refuse(5, "body unsafe or over limit")
	}
	s.raw = append([]byte(nil), raw...)
	return s, nil
}

// NativeAct maps a typed W8 act to the pinned native catalog without adding keys.
func NativeAct(s SpeechAct) (string, error) {
	switch s.Act {
	case "finding":
		return "claim", nil
	case "repro":
		return "evidence", nil
	case "affected":
		return "query", nil
	case "status":
		if s.Status == nil {
			return "", refuse(2, "status missing")
		}
		switch *s.Status {
		case "working", "done":
			return "commit", nil
		case "blocked", "stuck":
			return "yield", nil
		}
	}
	return "", refuse(2, "unknown act")
}

// Sidecar is host-created transport metadata, never part of SpeechAct.Raw().
type Sidecar struct {
	CodecVersion    string `json:"codec_version"`
	SenderExtension int    `json:"sender_extension"`
	NativeAct       string `json:"native_act"`
	TargetID        string `json:"target_id"`
	Epoch           uint64 `json:"epoch"`
	BusEpoch        uint64 `json:"bus_epoch"`
	RoomEpoch       uint64 `json:"room_epoch"`
	PayloadSHA256   string `json:"payload_sha256"`
	SourceBodyRef   string `json:"source_body_ref"`
	SourceBodyBytes int    `json:"source_body_bytes"`
	AttestationRef  string `json:"attestation_ref,omitempty"`
}

// CanonicalRepro uses C3's w8-repro/1 canonical JSON, without an 8-byte prefix.
// Callers must pass host-normalized typed fields, not untrusted free-form logs.
func CanonicalRepro(v map[string]any) ([]byte, string, error) {
	if s, ok := v["normalizer"].(string); !ok || s != ReproNormalizer {
		return nil, "", refuse(2, "unknown repro normalizer")
	}
	required := []string{"normalizer", "repo", "commit", "base_image_digest", "base_recipe_sha256", "cache_manifest_sha256", "guest_kernel_sha256", "guest_rootfs_sha256", "extract_policy_sha256", "gate_argv", "gate_cwd", "failure_kind", "normalized_exit", "affected", "assertion", "frames", "repro_steps"}
	if len(v) != len(required) {
		return nil, "", refuse(2, "repro field set is not closed")
	}
	for _, k := range required {
		if _, ok := v[k]; !ok {
			return nil, "", refuse(2, "missing repro field "+k)
		}
	}
	if e := validateReproFields(v); e != nil {
		return nil, "", e
	}
	raw, e := workerbridge.Canonical(v)
	if e != nil {
		return nil, "", refuse(2, "noncanonical repro: "+e.Error())
	}
	if !utf8.Valid(raw) {
		return nil, "", refuse(2, "repro UTF-8")
	}
	if _, e = workerbridge.Parse(raw); e != nil {
		return nil, "", refuse(2, "repro JSON: "+e.Error())
	}
	return raw, digest(raw), nil
}

// validateReproFields rejects untyped or invalid identity material before
// hashing it; volatile telemetry is deliberately not inferred from free text.
func validateReproFields(v map[string]any) error {
	text := func(name string, optional bool) bool {
		s, ok := v[name].(string)
		return ok && (optional || s != "") && utf8.ValidString(s)
	}
	for _, key := range []string{"repo", "gate_cwd", "failure_kind", "assertion"} {
		if !text(key, key == "assertion") {
			return refuse(2, "non-string repro field "+key)
		}
	}
	if v["gate_cwd"] != "." && !validPath(v["gate_cwd"].(string)) {
		return refuse(2, "unsafe repro cwd")
	}
	if s, ok := v["commit"].(string); !ok || len(s) != 40 || !regexp.MustCompile(`^[0-9a-f]{40}$`).MatchString(s) {
		return refuse(2, "invalid source commit")
	}
	for _, key := range []string{"base_image_digest", "base_recipe_sha256", "cache_manifest_sha256", "extract_policy_sha256"} {
		s, ok := v[key].(string)
		if !ok || !shaRE.MatchString(s) {
			return refuse(2, "invalid repro digest "+key)
		}
	}
	left, right := v["guest_kernel_sha256"], v["guest_rootfs_sha256"]
	if (left == nil) != (right == nil) {
		return refuse(2, "unpaired guest identity")
	}
	if left != nil {
		for _, val := range []any{left, right} {
			s, ok := val.(string)
			if !ok || !shaRE.MatchString(s) {
				return refuse(2, "invalid guest identity")
			}
		}
	}
	if !validExit(v["normalized_exit"]) {
		return refuse(2, "invalid normalized exit")
	}
	argv, ok := reproArray(v["gate_argv"])
	if !ok || len(argv) == 0 {
		return refuse(2, "invalid repro gate argv")
	}
	for _, val := range argv {
		s, ok := val.(string)
		if !ok || s == "" || !utf8.ValidString(s) {
			return refuse(2, "invalid gate token")
		}
	}
	steps, ok := reproArray(v["repro_steps"])
	if !ok {
		return refuse(2, "invalid repro steps")
	}
	for _, val := range steps {
		s, ok := val.(string)
		if !ok || !utf8.ValidString(s) {
			return refuse(2, "invalid repro step")
		}
	}
	paths, ok := reproArray(v["affected"])
	if !ok || len(paths) > 64 {
		return refuse(2, "invalid repro paths")
	}
	seen := map[string]bool{}
	for _, val := range paths {
		s, ok := val.(string)
		if !ok || !validPath(s) || seen[s] {
			return refuse(2, "invalid repro path")
		}
		seen[s] = true
	}
	frames, ok := reproArray(v["frames"])
	if !ok {
		return refuse(2, "invalid repro frames")
	}
	for _, f := range frames {
		o, ok := f.(map[string]any)
		if !ok || len(o) != 3 {
			return refuse(2, "invalid repro frame")
		}
		p, pOK := o["path"].(string)
		sym, sOK := o["symbol"].(string)
		if !pOK || !validPath(p) || !sOK || sym == "" || !utf8.ValidString(sym) || !validNonnegativeInteger(o["line"]) {
			return refuse(2, "invalid repro frame")
		}
	}
	return nil
}
func reproArray(x any) ([]any, bool) {
	if a, ok := x.([]any); ok {
		return a, true
	}
	return nil, false
}
func validNonnegativeInteger(x any) bool {
	switch n := x.(type) {
	case json.Number:
		if strings.ContainsAny(string(n), ".eE") {
			return false
		}
		val, e := strconv.ParseInt(string(n), 10, 64)
		return e == nil && val >= 0
	case int:
		return n >= 0
	case int64:
		return n >= 0
	}
	return false
}
func validExit(x any) bool {
	switch n := x.(type) {
	case json.Number:
		if strings.ContainsAny(string(n), ".eE") {
			return false
		}
		v, e := strconv.ParseInt(string(n), 10, 64)
		return e == nil && (v == 0 || (v >= 2 && v <= 7))
	case int:
		return n == 0 || (n >= 2 && n <= 7)
	case int64:
		return n == 0 || (n >= 2 && n <= 7)
	}
	return false
}
