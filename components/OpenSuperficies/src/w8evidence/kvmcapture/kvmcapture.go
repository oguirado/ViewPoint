// Package kvmcapture qualifies passive, caller-provided KVM evidence. It never
// examines a device, process, host, or VM. Qualification is limited to one
// explicit acquisition and the caller's policy; it is not a live KVM probe.
package kvmcapture

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"hash"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// MaxCaptureBytes is a TARGET safety bound per captured text document.
const MaxCaptureBytes = 4096

// Policy is supplied by a trusted caller, never inferred from captured text.
// A zero/missing policy field holds qualification rather than proving absence.
type Policy struct {
	Device, User, Group                                string
	UID, GID, PID, Major, Minor                        uint64
	OwnerUID                                           *uint64
	VMMSHA256, KernelSHA256, RootFSSHA256, ProbeSHA256 string
	Scope, Acquisition, Provenance                     string
	ValidFrom, ValidUntil, MaxAgeSeconds               int64
}

// Captures are passive KEY=VALUE UTF-8 records, each with its own kind.
// They must all describe the same acquisition and non-root mapped process.
type Captures struct{ Node, Account, Process, Behavior string }

// Record separates inventory from actual behavior. AUSENTE is deliberately
// distinct from FAIL; neither can produce a QUALIFIED decision.
type Record struct {
	Schema       string `json:"schema"`
	Decision     string `json:"decision"`
	Inventory    string `json:"inventory"`
	Behavior     string `json:"behavior"`
	Reason       string `json:"reason"`
	SourceSHA256 string `json:"source_sha256"`
	Scope        string `json:"scope,omitempty"`
	Acquisition  string `json:"acquisition,omitempty"`
}

var common = []string{"kind", "scope", "acquisition", "provenance", "captured_at"}
var allowed = map[string][]string{
	"node":     {"device", "type", "owner_uid", "owner_gid", "mode", "major", "minor", "device_count"},
	"account":  {"user", "uid", "group", "gid", "dedicated"},
	"process":  {"pid", "user", "uid", "group", "gid", "mapped", "privileged", "vmm_sha256", "kernel_sha256", "rootfs_sha256"},
	"behavior": {"pid", "uid", "gid", "device", "vmm_sha256", "kernel_sha256", "rootfs_sha256", "probe_sha256", "probe_qualified", "privileged", "emulation", "device_count", "open", "ioctl", "cold_boot"},
}

func parse(kind, input string) (map[string]string, error) {
	if len(input) == 0 {
		return nil, nil
	}
	if len(input) > MaxCaptureBytes || !utf8.ValidString(input) || strings.ContainsAny(input, "\x00\r") {
		return nil, errors.New("invalid capture encoding or size")
	}
	permitted := make(map[string]bool)
	for _, k := range append(append([]string{}, common...), allowed[kind]...) {
		permitted[k] = true
	}
	lines := strings.Split(strings.TrimSuffix(input, "\n"), "\n")
	fields := make(map[string]string)
	for _, line := range lines {
		if len(line) == 0 || len(line) > 256 || strings.Count(line, "=") != 1 {
			return nil, errors.New("malformed capture line")
		}
		pair := strings.SplitN(line, "=", 2)
		key, value := pair[0], pair[1]
		if !permitted[key] || value == "" || len(value) > 128 {
			return nil, errors.New("unknown or invalid capture field")
		}
		if _, exists := fields[key]; exists {
			return nil, errors.New("duplicate capture field")
		}
		for _, r := range value {
			if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune("_./:-", r)) {
				return nil, errors.New("invalid capture value")
			}
		}
		switch key {
		case "owner_uid", "owner_gid", "uid", "gid", "pid", "major", "minor", "device_count", "captured_at":
			if _, ok := decimal(value); !ok {
				return nil, errors.New("invalid decimal field")
			}
		case "vmm_sha256", "kernel_sha256", "rootfs_sha256", "probe_sha256":
			if !digestOK(value) {
				return nil, errors.New("invalid digest")
			}
		case "dedicated", "mapped", "privileged", "emulation", "probe_qualified":
			if value != "true" && value != "false" {
				return nil, errors.New("invalid boolean")
			}
		case "mode":
			if len(value) != 4 || value[0] != '0' {
				return nil, errors.New("invalid node mode")
			}
			if _, err := strconv.ParseUint(value, 8, 16); err != nil {
				return nil, errors.New("invalid node mode")
			}
		case "open", "ioctl", "cold_boot":
			if value != "ok" && value != "fail" && value != "unavailable" {
				return nil, errors.New("invalid behavior status")
			}
		}
		fields[key] = value
	}
	if fields["kind"] != kind {
		return nil, errors.New("conflicting capture kind")
	}
	return fields, nil
}

func digestOK(s string) bool {
	if len(s) != 64 {
		return false
	}
	for _, r := range s {
		if !(r >= '0' && r <= '9' || r >= 'a' && r <= 'f') {
			return false
		}
	}
	return true
}
func decimal(s string) (uint64, bool) {
	n, err := strconv.ParseUint(s, 10, 64)
	return n, err == nil && strconv.FormatUint(n, 10) == s
}
func equalNumber(s string, n uint64) bool { actual, ok := decimal(s); return ok && actual == n }
func sourceHash(p Policy, c Captures) string {
	h := sha256.New()
	b, _ := json.Marshal(p) // A fixed struct: stable order, no maps.
	for _, piece := range []string{string(b), c.Node, c.Account, c.Process, c.Behavior} {
		frame(h, []byte(piece))
	}
	return hex.EncodeToString(h.Sum(nil))
}
func frame(h hash.Hash, b []byte) {
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(b)))
	_, _ = h.Write(length[:])
	_, _ = h.Write(b)
}

// Assess is deterministic and side-effect free. now must be provided by the
// caller (tests can use a fake clock); this function never inspects the host.
// Malformed, duplicate or cross-capture conflicting text returns a zero Record
// and an error: no qualified evidence survives such a failure.
func Assess(p Policy, c Captures, now time.Time) (Record, error) {
	rec := Record{Schema: "kvm-capture-v1", Decision: "HOLD", Inventory: "AUSENTE", Behavior: "AUSENTE", Reason: "AUSENTE policy", SourceSHA256: sourceHash(p, c), Scope: p.Scope, Acquisition: p.Acquisition}
	raw := []struct{ kind, data string }{{"node", c.Node}, {"account", c.Account}, {"process", c.Process}, {"behavior", c.Behavior}}
	parsed := make(map[string]map[string]string, len(raw))
	for _, part := range raw {
		fields, err := parse(part.kind, part.data)
		if err != nil {
			return Record{}, fmt.Errorf("%s capture rejected: %w", part.kind, err)
		}
		parsed[part.kind] = fields
	}
	if p.Device != "/dev/kvm" || p.User == "" || p.Group == "" || p.UID == 0 || p.GID == 0 || p.PID == 0 || p.OwnerUID == nil || p.Major == 0 || p.Scope == "" || p.Acquisition == "" || p.Provenance == "" || p.ValidFrom <= 0 || p.ValidUntil < p.ValidFrom || p.MaxAgeSeconds <= 0 || !digestOK(p.VMMSHA256) || !digestOK(p.KernelSHA256) || !digestOK(p.RootFSSHA256) || !digestOK(p.ProbeSHA256) {
		return rec, nil
	}
	// Evaluate provenance, acquisition and time for EVERY supplied capture.
	// A missing complete document is a HOLD; no document can borrow fields.
	for _, part := range raw {
		m := parsed[part.kind]
		if m == nil {
			rec.Reason = "AUSENTE " + part.kind + " capture"
			return rec, nil
		}
		for _, key := range append(append([]string{}, common...), allowed[part.kind]...) {
			if m[key] == "" {
				rec.Reason = "AUSENTE " + part.kind + "." + key
				return rec, nil
			}
		}
	}
	var acquired string
	for _, part := range raw {
		m := parsed[part.kind]
		if m["scope"] != p.Scope || m["acquisition"] != p.Acquisition || m["provenance"] != p.Provenance {
			return Record{}, errors.New("conflicting acquisition or provenance")
		}
		if acquired == "" {
			acquired = m["captured_at"]
		}
		if m["captured_at"] != acquired {
			return Record{}, errors.New("conflicting capture time")
		}
	}
	acquiredSeconds, ok := decimal(acquired)
	if !ok || acquiredSeconds > uint64(^uint64(0)>>1) {
		return Record{}, errors.New("invalid capture time")
	}
	instant := int64(acquiredSeconds)
	if now.IsZero() || instant < p.ValidFrom || instant > p.ValidUntil || now.Unix() < instant || now.Unix() > p.ValidUntil || now.Unix()-instant > p.MaxAgeSeconds {
		rec.Reason = "STALE_SCOPE"
		return rec, nil
	}
	node, acct, proc, behavior := parsed["node"], parsed["account"], parsed["process"], parsed["behavior"]
	mode, modeErr := strconv.ParseUint(node["mode"], 8, 16)
	if modeErr != nil || len(node["mode"]) != 4 || node["mode"][0] != '0' {
		return Record{}, errors.New("invalid node mode")
	}
	if node["device"] != p.Device || node["type"] != "char" || !equalNumber(node["owner_uid"], *p.OwnerUID) || !equalNumber(node["owner_gid"], p.GID) || !equalNumber(node["major"], p.Major) || !equalNumber(node["minor"], p.Minor) || !equalNumber(node["device_count"], 1) || mode&0660 != 0660 || mode&07007 != 0 {
		rec.Inventory = "FAIL"
		rec.Reason = "NODE_POLICY_MISMATCH"
		return rec, nil
	}
	if acct["user"] != p.User || acct["group"] != p.Group || !equalNumber(acct["uid"], p.UID) || !equalNumber(acct["gid"], p.GID) || acct["dedicated"] != "true" {
		rec.Inventory = "FAIL"
		rec.Reason = "DEDICATED_ACCOUNT_MISMATCH"
		return rec, nil
	}
	if !equalNumber(proc["pid"], p.PID) || !equalNumber(proc["uid"], p.UID) || !equalNumber(proc["gid"], p.GID) || proc["user"] != p.User || proc["group"] != p.Group || proc["mapped"] != "true" || proc["privileged"] != "false" || proc["vmm_sha256"] != p.VMMSHA256 || proc["kernel_sha256"] != p.KernelSHA256 || proc["rootfs_sha256"] != p.RootFSSHA256 {
		rec.Inventory = "FAIL"
		rec.Reason = "MAPPED_PROCESS_MISMATCH"
		return rec, nil
	}
	rec.Inventory = "PASS"
	if !equalNumber(behavior["pid"], p.PID) || !equalNumber(behavior["uid"], p.UID) || !equalNumber(behavior["gid"], p.GID) || behavior["device"] != p.Device || behavior["vmm_sha256"] != p.VMMSHA256 || behavior["kernel_sha256"] != p.KernelSHA256 || behavior["rootfs_sha256"] != p.RootFSSHA256 || behavior["probe_sha256"] != p.ProbeSHA256 || behavior["probe_qualified"] != "true" || behavior["privileged"] != "false" || behavior["emulation"] != "false" || !equalNumber(behavior["device_count"], 1) {
		rec.Behavior = "FAIL"
		rec.Reason = "BEHAVIOR_SCOPE_MISMATCH"
		return rec, nil
	}
	if behavior["open"] != "ok" || behavior["ioctl"] != "ok" || behavior["cold_boot"] != "ok" {
		rec.Behavior = "FAIL"
		rec.Reason = "BEHAVIOR_NOT_PROVEN"
		return rec, nil
	}
	rec.Decision, rec.Behavior, rec.Reason = "QUALIFIED", "PASS", "CAPTURED_SCOPE_ONLY"
	return rec, nil
}

// JSON emits stable integer-safe evidence text (no floats or maps).
func (r Record) JSON() ([]byte, error) { return json.Marshal(r) }
