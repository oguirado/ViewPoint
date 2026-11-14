package programsurface

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

func runArgs(container, profile, viewDir string, p Program, tiles []Tile, proxy string) []string {
	if p.Validate() != nil || (p.Network == NetProxy && proxy == "") || !validMountPath(profile) || !validMountPath(viewDir) {
		return nil
	}
	gid := os.Getenv("SRF_KVM_GID")
	if p.Runtime == RuntimeVM && (!validVMCapsule(container) || !validKVMGID(gid)) {
		return nil
	}
	type tile struct {
		ID string `json:"id"`
		X  int    `json:"x"`
		Y  int    `json:"y"`
		W  int    `json:"w"`
		H  int    `json:"h"`
	}
	wire := make([]tile, len(tiles))
	for i, t := range tiles {
		wire[i] = tile{t.ID, t.X, t.Y, t.W, t.H}
	}
	b, _ := json.Marshal(wire)
	network := "none"
	if p.Network == NetProxy {
		network = "srf-idnet"
	}
	args := []string{"docker", "run", "-d", "--name", container, "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--network", network,
		"--cpuset-cpus", "10-14", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256", "--shm-size", "2g",
		"--tmpfs", "/run/vqa/fb:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
		"--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=256m",
		"--tmpfs", "/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
		"--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
		"--tmpfs", "/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m",
		"--mount", "type=bind,src=" + profile + ",dst=/profile", "--mount", "type=bind,src=" + viewDir + ",dst=/srfview"}
	if p.Runtime == RuntimeVM {
		args = []string{"docker", "run", "-d", "--name", container, "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--network", "none",
			"--cpuset-cpus", "10-14", "--memory", strconv.Itoa(p.MemoryMiB) + "m", "--memory-swap", strconv.Itoa(p.MemoryMiB) + "m", "--pids-limit", "256", "--shm-size", "64m",
			"--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777,size=64m",
			"--tmpfs", "/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
			"--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
			"--tmpfs", "/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m",
			"--device", "/dev/kvm", "--group-add", gid,
			"--mount", "type=bind,src=" + profile + ",dst=/profile", "--mount", "type=bind,src=" + viewDir + ",dst=/srfview"}
	}
	if p.Network == NetProxy {
		args = append(args, "--env", "SRF_EGRESS_PROXY="+proxy)
	}
	args = append(args, "--env", "SRF_PROGRAM="+p.ID, "--env", "SRF_WINDOWS="+strconv.Itoa(len(tiles)), "--env", "SRF_TILES="+string(b))
	if p.Runtime == RuntimeVM {
		args = append(args, "--env", "SRF_GUEST_CPUS="+strconv.Itoa(p.GuestCPUs), "--env", "SRF_GUEST_MEM_MIB="+strconv.Itoa(p.GuestMemMiB), "--env", "SRF_PROFILE_DISK_MIB="+strconv.Itoa(p.ProfileDiskMiB))
	}
	if p.A11y {
		args = append(args, "--env", "NO_AT_BRIDGE=0")
	}
	return append(args, p.Image)
}

var kvmGIDRE = regexp.MustCompile(`^[1-9][0-9]*$`)

func validKVMGID(gid string) bool {
	if !kvmGIDRE.MatchString(gid) {
		return false
	}
	n, err := strconv.ParseUint(gid, 10, 32)
	return err == nil && n < 1<<32-1
}

func validMountPath(path string) bool {
	if !filepath.IsAbs(path) || !utf8.ValidString(path) || filepath.Clean(path) != path || strings.ContainsAny(path, ",\n\r\x00") {
		return false
	}
	for _, element := range strings.Split(path, string(filepath.Separator)) {
		if element == ".." {
			return false
		}
	}
	return true
}

var xidRE = regexp.MustCompile(`^0x[0-9a-f]{1,8}$`)

// exactObject rejects missing, unknown, repeated and null members, including
// repeated members that encoding/json would otherwise silently overwrite.
func exactObject(raw []byte, keys ...string) (map[string]json.RawMessage, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	tok, err := dec.Token()
	if err != nil || tok != json.Delim('{') {
		return nil, fmt.Errorf("expected object")
	}
	fields := make(map[string]json.RawMessage, len(keys))
	allowed := make(map[string]bool, len(keys))
	for _, key := range keys {
		allowed[key] = true
	}
	for dec.More() {
		tok, err = dec.Token()
		if err != nil {
			return nil, err
		}
		key, ok := tok.(string)
		if !ok || !allowed[key] || fields[key] != nil {
			return nil, fmt.Errorf("invalid or duplicate member")
		}
		var value json.RawMessage
		if err = dec.Decode(&value); err != nil {
			return nil, err
		}
		if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return nil, fmt.Errorf("null member")
		}
		fields[key] = value
	}
	if _, err = dec.Token(); err != nil {
		return nil, err
	}
	if len(fields) != len(keys) {
		return nil, fmt.Errorf("missing member")
	}
	if _, err = dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("trailing data")
	}
	return fields, nil
}

func parseReady(line string, tiles []Tile) ([]ReadyWindow, error) {
	bad := func() ([]ReadyWindow, error) { return nil, &Error{6, "invalid program readiness"} }
	if len(line) > 64*1024 || !strings.HasPrefix(line, ReadyToken+" ") {
		return bad()
	}
	fields, err := exactObject([]byte(line[len(ReadyToken)+1:]), "windows")
	if err != nil {
		return bad()
	}
	var raw []json.RawMessage
	if json.Unmarshal(fields["windows"], &raw) != nil || len(raw) != len(tiles) {
		return bad()
	}
	windows := make([]ReadyWindow, len(raw))
	taken := make(map[string]bool, len(raw))
	for i, value := range raw {
		if _, err := exactObject(value, "id", "x", "y", "w", "h", "xid"); err != nil {
			return bad()
		}
		if json.Unmarshal(value, &windows[i]) != nil {
			return bad()
		}
		w, t := windows[i], tiles[i]
		if w.ID != t.ID || w.X != t.X || w.Y != t.Y || w.W != t.W || w.H != t.H || !xidRE.MatchString(w.XID) {
			return bad()
		}
		// XIDs name numeric X resources: leading zeroes must not bypass uniqueness.
		xid, _ := strconv.ParseUint(w.XID[2:], 16, 32)
		canonical := strconv.FormatUint(xid, 16)
		if taken[canonical] {
			return bad()
		}
		taken[canonical] = true
	}
	return windows, nil
}
