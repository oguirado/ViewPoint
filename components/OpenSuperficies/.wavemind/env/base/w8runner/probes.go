package main

import (
	"encoding/binary"
	"errors"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type probes struct {
	EtcWrite             string `json:"etc_write"`
	HomeAdmin            string `json:"home_admin"`
	EgressProbe          string `json:"egress_probe"`
	GatewayProbe         string `json:"gateway_probe"`
	EnvSecretNames       int    `json:"env_secret_names"`
	RuntimeSocketVisible bool   `json:"runtime_socket_visible"`
	InferenceProcesses   int    `json:"inference_processes"`
}

func blocked(address string) bool {
	c, e := net.DialTimeout("tcp", address, 200*time.Millisecond)
	if e == nil {
		c.Close()
		return false
	}
	return true
}
func hostProbes(_ string) (probes, error) {
	p := probes{EtcWrite: "unsafe", HomeAdmin: "unsafe", EgressProbe: "unsafe", GatewayProbe: "unsafe"}
	f, e := os.OpenFile("/etc/probe", os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e == nil {
		f.Close()
		os.Remove("/etc/probe")
		return p, errors.New("writable root")
	}
	if !errors.Is(e, syscall.EROFS) {
		return p, errors.New("unproven read-only root")
	}
	p.EtcWrite = "read_only"
	if _, e = os.Lstat("~"); !os.IsNotExist(e) {
		return p, errors.New("host home visible")
	}
	p.HomeAdmin = "absent"
	if !blocked("1.1.1.1:443") {
		return p, errors.New("off-list egress")
	}
	p.EgressProbe = "BLOCKED"
	routes, e := os.ReadFile("/proc/net/route")
	if e != nil {
		return p, e
	}
	found := false
	for _, line := range strings.Split(string(routes), "\n") {
		f := strings.Fields(line)
		if len(f) < 4 || f[1] != "00000000" {
			continue
		}
		v, e := strconv.ParseUint(f[2], 16, 32)
		if e != nil {
			return p, e
		}
		if v == 0 {
			continue
		}
		found = true
		ip := make(net.IP, 4)
		binary.LittleEndian.PutUint32(ip, uint32(v))
		if !blocked(net.JoinHostPort(ip.String(), "22")) {
			return p, errors.New("gateway reachable")
		}
	}
	// With network=none there is no default route: absence is explicitly measured,
	// rather than substituting an invented gateway address.
	if found {
		p.GatewayProbe = "BLOCKED"
	} else {
		p.GatewayProbe = "NO_ROUTE"
	}
	env, e := os.ReadFile("/proc/self/environ")
	if e != nil {
		return p, e
	}
	for _, entry := range strings.Split(string(env), "\x00") {
		name, _, _ := strings.Cut(entry, "=")
		name = strings.ToLower(name)
		for _, bad := range []string{"key", "token", "secret", "passwd", "anthropic", "gemini", "openai"} {
			if strings.Contains(name, bad) {
				p.EnvSecretNames++
				break
			}
		}
	}
	if p.EnvSecretNames != 0 {
		return p, errors.New("secret environment name")
	}
	if _, e = os.Lstat("/var/run/docker.sock"); e == nil {
		p.RuntimeSocketVisible = true
		return p, errors.New("runtime socket")
	}
	if !os.IsNotExist(e) {
		return p, e
	}
	// Check running executable names; inaccessible proc entries do not prove safe.
	procs, e := os.ReadDir("/proc")
	if e != nil {
		return p, e
	}
	for _, entry := range procs {
		if _, e := strconv.Atoi(entry.Name()); e != nil {
			continue
		}
		comm, e := os.ReadFile("/proc/" + entry.Name() + "/comm")
		if os.IsNotExist(e) {
			continue
		}
		if e != nil {
			return p, e
		}
		name := strings.ToLower(strings.TrimSpace(string(comm)))
		for _, bad := range []string{"codex", "claude", "gemini", "ollama", "llama-server", "vllm"} {
			if strings.Contains(name, bad) {
				p.InferenceProcesses++
				break
			}
		}
	}
	if p.InferenceProcesses != 0 {
		return p, errors.New("inference process")
	}
	return p, nil
}
