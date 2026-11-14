package w8muse

import (
	"bufio"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/workerbridge"
)

var taskPattern = regexp.MustCompile(`^[a-z0-9]+(?:-[a-z0-9]+)*$`)
var attemptPattern = regexp.MustCompile(`^a[0-9]+$`)
var shaPattern = regexp.MustCompile(`^[a-f0-9]{40}$`)
var writerSurfaces = map[string]bool{"codex-cloud": true, "jules": true, "perplexity-mcp": true, "gemini-spark-mcp": true, "chatgpt-mcp": true}

func strictJSON(b []byte, v any) error {
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e := d.Decode(v); e != nil {
		return refusal(2, "input", "JSON shape")
	}
	if e := d.Decode(new(any)); e != io.EOF {
		return refusal(2, "input", "trailing JSON")
	}
	return nil
}
func text(v any) string        { s, _ := v.(string); return s }
func obj(v any) map[string]any { o, _ := v.(map[string]any); return o }
func hash(b []byte) string     { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func canonical(v any) []byte   { b, _ := workerbridge.Canonical(v); return b }
func same(a, b any) bool       { return bytes.Equal(canonical(a), canonical(b)) }
func readBounded(path string, limit int64) ([]byte, error) {
	f, e := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Size() > limit {
		return nil, refusal(2, "input", "file type or cap")
	}
	b, e := io.ReadAll(io.LimitReader(f, limit+1))
	if int64(len(b)) > limit {
		return nil, refusal(2, "input", "file cap")
	}
	return b, e
}

// loadRegistration accepts only the original dispatched task and a current,
// digest-bound lease. It never repairs identifiers or manufactures dispatch.
func loadRegistration(c Config, item Item) ([]byte, workerbridge.Document, error) {
	if !taskPattern.MatchString(item.TaskID) || !attemptPattern.MatchString(item.Attempt) || strings.TrimSpace(item.Objective) == "" {
		return nil, nil, refusal(2, "input", "wave item")
	}
	root := c.BridgeDir
	b, e := readBounded(filepath.Join(root, "tasks", item.TaskID, item.Attempt, "task.json"), MaxHeader)
	if e != nil {
		return nil, nil, refusal(2, "input", "unregistered task")
	}
	task, e := workerbridge.Parse(b)
	if e != nil {
		return nil, nil, refusal(2, "input", "task JSON")
	}
	if e = workerbridge.ValidateDef(task, "bridge_task_v1"); e != nil {
		return nil, nil, refusal(2, "input", "task contract")
	}
	if task["task_id"] != item.TaskID || task["attempt_id"] != item.Attempt || task["role"] != "builder" {
		return nil, nil, refusal(2, "input", "task identity or role")
	}
	surface := text(task["surface"])
	if !c.Dry && surface != workerbridge.OSSurface {
		return nil, nil, refusal(4, "unavailable", "live requires os-capsule")
	}
	if c.Dry && !writerSurfaces[surface] && surface != workerbridge.OSSurface {
		return nil, nil, refusal(2, "input", "dry requires writer surface")
	}
	if task["writer_branch"] != "bridge/"+item.TaskID+"/"+item.Attempt {
		return nil, nil, refusal(2, "input", "auto_ingest branch shape")
	}
	expires, e := time.Parse(time.RFC3339, text(task["expires_at_utc"]))
	if e != nil || !expires.After(time.Now()) {
		return nil, nil, refusal(2, "input", "expired dispatch")
	}
	expected, e := readBounded(filepath.Join(root, "expected", item.TaskID, item.Attempt+".json"), MaxHeader)
	if e != nil {
		return nil, nil, refusal(2, "input", "expected record absent")
	}
	if _, e = workerbridge.Parse(expected); e != nil {
		return nil, nil, refusal(2, "input", "expected record JSON")
	}
	ledger, e := readBounded(filepath.Join(root, "ledger.jsonl"), 16<<20)
	if e != nil {
		return nil, nil, refusal(2, "input", "ledger unavailable")
	}
	if len(ledger) > 0 && ledger[len(ledger)-1] != '\n' {
		return nil, nil, refusal(2, "input", "torn ledger")
	}
	canonicalTask, e := workerbridge.BridgeCanonical(task)
	if e != nil {
		return nil, nil, e
	}
	digest := hash(canonicalTask)
	scan := bufio.NewScanner(bytes.NewReader(ledger))
	scan.Buffer(make([]byte, 4096), MaxHeader)
	active := false
	seq := 0
	for scan.Scan() {
		seq++
		ev, e := workerbridge.Parse(scan.Bytes())
		if e != nil || fmt.Sprint(ev["seq"]) != fmt.Sprint(seq) {
			return nil, nil, refusal(2, "input", "ledger sequence or JSON")
		}
		if ev["lease_id"] != task["lease_id"] {
			continue
		}
		switch ev["event"] {
		case "lease_granted":
			active = ev["task_sha256"] == digest
			for _, k := range []string{"task_id", "attempt_id", "lease_id", "thread", "agent", "worker_id", "key", "base_seq", "surface", "role", "allowed_write_paths", "expires_at_utc"} {
				active = active && same(ev[k], task[k])
			}
		case "lease_released", "result_admitted", "result_blocked":
			active = false
		}
	}
	if scan.Err() != nil || !active {
		return nil, nil, refusal(2, "input", "no current matching lease_granted")
	}
	return canonicalTask, task, nil
}

// promptBinding reads the canon's dispatch nonce; it is evidence provenance,
// never an authentication credential and never a capsule environment value.
func promptBinding(c Config, i Item) (string, error) {
	b, e := readBounded(filepath.Join(c.BridgeDir, "expected", i.TaskID, i.Attempt+".json"), MaxHeader)
	if e != nil {
		return "", refusal(2, "input", "expected record absent")
	}
	d, e := workerbridge.Parse(b)
	if e != nil {
		return "", refusal(2, "input", "expected record invalid")
	}
	nonce := text(d["nonce"])
	if nonce == "" {
		if c.Dry {
			return "", nil
		}
		return "", refusal(2, "input", "live prompt nonce absent")
	}
	if len(nonce) > 128 || strings.ContainsAny(nonce, " \r\n\t\x00") {
		return "", refusal(2, "input", "prompt nonce shape")
	}
	return "PROMPT-ID " + i.TaskID + " " + i.Attempt + " " + nonce, nil
}
