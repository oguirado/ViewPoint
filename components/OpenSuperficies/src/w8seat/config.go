package w8seat

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

func workerReason(e error) string {
	if e == nil {
		return ""
	}
	if r, ok := e.(*workerbridge.Reject); ok {
		return r.Class
	}
	return "indeterminate"
}

// LoadConfig consumes operator CPU/seccomp files, the existing green builder
// ledger, and an explicitly supplied warm-cache manifest. It never warms/builds.
// The cache manifest is a host warm-job receipt {volume,manifest_sha256,read_only}.
func LoadConfig(ctx context.Context, stateDir, cacheManifest string, r w8builder.Runner) (Config, error) {
	c := Config{StateDir: stateDir, Runner: r, Diag: os.Stderr}
	if _, e := secureRead(CPUPath, 4096, true); e != nil {
		return c, e
	}
	affinity, e := capture(ctx, r, nil, 4096, "systemctl", "--user", "show", "docker.service", "--property=CPUAffinity", "--value")
	if e != nil {
		return c, refusal(4, "unavailable", "daemon affinity unavailable")
	}
	cpus, e := LoadCPUConfig(CPUPath, string(bytes.TrimSpace(affinity)))
	if e != nil {
		return c, e
	}
	b, e := secureRead(SeccompPath, 1<<20, true)
	if e != nil || hash(b) != ProfileSHA() {
		return c, refusal(4, "unavailable", "installed seccomp differs from seat binary")
	}
	records, e := secureRead(filepath.Join(stateDir, "base_image_records.jsonl"), 4<<20, false)
	if e != nil {
		return c, refusal(4, "unavailable", "green base image ledger unavailable")
	}
	lines := strings.Split(strings.TrimSpace(string(records)), "\n")
	if len(lines) == 0 {
		return c, refusal(4, "unavailable", "base image ledger empty")
	}
	last := []byte(lines[len(lines)-1])
	if e = w8builder.CheckRecord(last); e != nil {
		return c, e
	}
	record, e := workerbridge.Parse(last)
	if e != nil {
		return c, e
	}
	approval, e := secureRead(w8builder.DefaultApprovalPath, 4096, true)
	if e != nil {
		return c, e
	}
	a, e := workerbridge.Parse(approval)
	if e != nil || a["mode"] != "per_build" || a["recipe_sha256"] != record["recipe_sha256"] || hash(approval) != obj(record["approval"])["approval_file_sha256"] {
		return c, refusal(4, "unavailable", "base approval does not match measured record")
	}
	cache, e := secureRead(cacheManifest, 4096, false)
	if e != nil {
		return c, refusal(4, "unavailable", "warm cache manifest absent")
	}
	m, e := workerbridge.Parse(cache)
	if e != nil || len(m) != 3 || m["volume"] != "os_worker_cache" || m["read_only"] != true || !hex64.MatchString(str(m["manifest_sha256"])) {
		return c, refusal(4, "unavailable", "warm cache manifest shape")
	}
	c.BaseImage = map[string]any{"name": "os-worker-base", "digest": record["image_digest"], "recipe_sha256": record["recipe_sha256"], "approval_sha256": obj(record["approval"])["approval_file_sha256"]}
	c.CacheSHA = str(m["manifest_sha256"])
	var inline bytes.Buffer
	if e = json.Compact(&inline, b); e != nil {
		return c, refusal(4, "unavailable", "seccomp profile JSON")
	}
	c.Policy = LaunchPolicy{CPUs: cpus, ImageSHA: str(record["image_digest"]), SeccompFile: SeccompPath, SeccompSHA: hash(b), SeccompInline: inline.String()}
	return c, nil
}
