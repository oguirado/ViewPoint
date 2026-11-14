package surface

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const ProxyContainer = "srf-egress-proxy"
const ProxyURL = "http://172.30.80.2:3128"
const networkName = "srf-idnet"
const subnet = "172.30.80.0/24"

// DockerLauncher uses the host Runner; only the cmd adapter executes processes.
// Availability is a configuration check. Start checks the live proxy under the
// Tool lock before acquiring an identity lease.
type DockerLauncher struct{ R Runner }

func (d DockerLauncher) Available() error {
	if d.R == nil {
		return fail(3, "Docker runner required")
	}
	return nil
}
func (d DockerLauncher) Start(ctx context.Context, p Launch) error {
	if !capsuleRE.MatchString(p.Container) || len(p.Argv) < 5 || p.Argv[0] != "docker" || p.Argv[1] != "run" {
		return fail(2, "invalid launch plan")
	}
	_, e := d.R.Run(ctx, p.Argv, nil)
	return e
}
func (d DockerLauncher) Remove(ctx context.Context, c string) error {
	if !capsuleRE.MatchString(c) {
		return fail(2, "invalid identity capsule")
	}
	b, e := d.R.Run(ctx, []string{"docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=^/" + c + "$"}, nil)
	if e != nil {
		return fail(6, "capsule inventory unavailable")
	}
	if strings.TrimSpace(string(b)) == "" {
		return nil
	}
	if strings.TrimSpace(string(b)) != c {
		return fail(6, "ambiguous capsule inventory")
	}
	_, e = d.R.Run(ctx, []string{"docker", "rm", "-f", c}, nil)
	return e
}
func (t Tool) proxyRunning(ctx context.Context) error {
	b, e := t.R.Run(ctx, []string{"docker", "inspect", "--format", "{{.State.Running}}", ProxyContainer}, nil)
	if e != nil || strings.TrimSpace(string(b)) != "true" {
		return fail(3, "egress proxy is not running; use net up")
	}
	return nil
}

type NetSpec struct{ ProxyBin, Policy, AuditDir string }

func mountPath(p string) bool { return filepath.IsAbs(p) && !strings.ContainsAny(p, ",\n\r\x00") }
func regularFile(p string, executable bool) bool {
	if !mountPath(p) {
		return false
	}
	resolved, e := filepath.EvalSymlinks(p)
	if e != nil || resolved != filepath.Clean(p) {
		return false
	}
	fi, e := os.Lstat(p)
	return e == nil && fi.Mode().IsRegular() && (!executable || fi.Mode().Perm()&0111 != 0)
}
func (t Tool) networkExists(ctx context.Context) (bool, error) {
	b, e := t.R.Run(ctx, []string{"docker", "network", "ls", "--filter", "name=^srf-idnet$", "--format", "{{.Name}}"}, nil)
	if e != nil {
		return false, fail(6, "network inventory unavailable")
	}
	s := strings.TrimSpace(string(b))
	if s == "" {
		return false, nil
	}
	if s != networkName {
		return false, fail(6, "ambiguous identity network")
	}
	return true, nil
}
func (t Tool) verifyNetwork(ctx context.Context) error {
	b, e := t.R.Run(ctx, []string{"docker", "network", "inspect", "--format", "{{json .}}", networkName}, nil)
	var n struct {
		Name     string
		Internal bool
		IPAM     struct{ Config []struct{ Subnet string } }
	}
	if e != nil || json.Unmarshal(b, &n) != nil || n.Name != networkName || !n.Internal || len(n.IPAM.Config) != 1 || n.IPAM.Config[0].Subnet != subnet {
		return fail(3, "identity network must be internal with the reserved subnet")
	}
	return nil
}
func (t Tool) proxyExists(ctx context.Context) (bool, error) {
	b, e := t.R.Run(ctx, []string{"docker", "ps", "-a", "--filter", "name=^/srf-egress-proxy$", "--format", "{{.Names}}"}, nil)
	if e != nil {
		return false, fail(6, "proxy inventory unavailable")
	}
	s := strings.TrimSpace(string(b))
	if s == "" {
		return false, nil
	}
	if s != ProxyContainer {
		return false, fail(6, "ambiguous proxy container")
	}
	return true, nil
}
func (t Tool) NetUp(ctx context.Context, cfg NetSpec) error {
	if t.R == nil || !regularFile(cfg.ProxyBin, true) || !regularFile(cfg.Policy, false) || !mountPath(cfg.AuditDir) || !mountPath(t.Registry) {
		return fail(3, "absolute proxy executable, policy, registry and audit paths required")
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return e
	}
	defer unlock()
	if e = privateDir(cfg.AuditDir); e != nil {
		return e
	}
	// Initialize without replacing active allocations.
	if e = t.registry("", "", false); e != nil {
		return e
	}
	exists, e := t.networkExists(ctx)
	if e != nil {
		return e
	}
	if !exists {
		if _, e = t.R.Run(ctx, []string{"docker", "network", "create", "--internal", "--subnet", subnet, networkName}, nil); e != nil {
			return fail(6, "identity network creation failed")
		}
	}
	if e = t.verifyNetwork(ctx); e != nil {
		return e
	}
	args := []string{"docker", "create", "--name", ProxyContainer, "--network", "bridge", "--user", proxyUser(), "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "256", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m", "--mount", "type=bind,src=" + cfg.ProxyBin + ",dst=/srf-egress-proxy,readonly", "--mount", "type=bind,src=" + filepath.Dir(t.Registry) + ",dst=/registry,readonly", "--mount", "type=bind,src=" + filepath.Dir(cfg.Policy) + ",dst=/policy,readonly", "--mount", "type=bind,src=" + cfg.AuditDir + ",dst=/audit", "--entrypoint", "/srf-egress-proxy", "opensuperficies-desktop-arm64:1.1.0", "--listen", "172.30.80.2:3128", "--registry", "/registry/" + filepath.Base(t.Registry), "--policy", "/policy/" + filepath.Base(cfg.Policy), "--audit", "/audit/egress.jsonl"}
	raw, _ := json.Marshal(args)
	hash := sha256.Sum256(raw)
	label := hex.EncodeToString(hash[:])
	exists, e = t.proxyExists(ctx)
	if e != nil {
		return e
	}
	if exists {
		b, err := t.R.Run(ctx, []string{"docker", "inspect", "--format", `{{index .Config.Labels "srf.identity.proxy.recipe"}}`, ProxyContainer}, nil)
		if err != nil || strings.TrimSpace(string(b)) != label {
			return fail(3, "existing proxy configuration differs; net down before replacing it")
		}
	} else {
		args = append(args[:4], append([]string{"--label", "srf.identity.proxy.recipe=" + label}, args[4:]...)...)
		if _, e = t.R.Run(ctx, args, nil); e != nil {
			return fail(6, "proxy creation failed; net down to recover")
		}
		if _, e = t.R.Run(ctx, []string{"docker", "network", "connect", "--ip", "172.30.80.2", networkName, ProxyContainer}, nil); e != nil {
			return fail(6, "proxy network connection failed; net down to recover")
		}
	}
	if _, e = t.R.Run(ctx, []string{"docker", "start", ProxyContainer}, nil); e != nil {
		return fail(6, "proxy start failed")
	}
	// Docker assigns runtime addresses at start. Verify both attachments and
	// the absence of published ports before admitting identity launches.
	b, e := t.R.Run(ctx, []string{"docker", "inspect", "--format", `{{len .NetworkSettings.Networks}} {{(index .NetworkSettings.Networks "srf-idnet").IPAddress}} {{if index .NetworkSettings.Networks "bridge"}}bridge{{end}} {{len .HostConfig.PortBindings}}`, ProxyContainer}, nil)
	if e != nil || strings.TrimSpace(string(b)) != "2 172.30.80.2 bridge 0" {
		return fail(3, "proxy network attachments or published ports invalid; net down to recover")
	}
	for attempt := 0; attempt < 100; attempt++ {
		logs, err := t.R.Run(ctx, []string{"docker", "logs", "--tail", "30", ProxyContainer}, nil)
		if err != nil {
			return fail(6, "proxy readiness unavailable")
		}
		for _, line := range strings.Split(string(logs), "\n") {
			var report struct{ Status string }
			if json.Unmarshal([]byte(line), &report) == nil && report.Status == "EGRESS_PROXY_READY" {
				return t.proxyRunning(ctx)
			}
		}
		timer := time.NewTimer(100 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return fail(6, "proxy readiness cancelled")
		case <-timer.C:
		}
	}
	return fail(4, "proxy readiness timeout")
}
func (t Tool) NetDown(ctx context.Context) error {
	if t.R == nil {
		return fail(3, "Docker runner required")
	}
	unlock, e := t.lock(ctx)
	if e != nil {
		return e
	}
	defer unlock()
	// Refuse before removing anything while capsules still use the shared network: a partial
	// teardown (proxy gone, network kept by attached capsules) is never left behind.
	b, e := t.R.Run(ctx, []string{"docker", "ps", "-a", "--filter", "network=" + networkName, "--format", "{{.Names}}"}, nil)
	if e != nil {
		return fail(6, "network inventory unavailable")
	}
	for _, n := range strings.Fields(string(b)) {
		if n != ProxyContainer {
			return fail(4, "identity capsules still attached to the shared network; stop them first")
		}
	}
	if t.Registry != "" {
		if e = t.withRegistry(func(m map[string]string) error {
			if len(m) != 0 {
				return fail(4, "identity capsules still hold shared addresses; stop them first")
			}
			return nil
		}); e != nil {
			return e
		}
	}
	exists, e := t.proxyExists(ctx)
	if e != nil {
		return e
	}
	if exists {
		if _, e = t.R.Run(ctx, []string{"docker", "rm", "-f", ProxyContainer}, nil); e != nil {
			return fail(6, "proxy removal failed")
		}
	}
	if exists, e = t.proxyExists(ctx); e != nil {
		return e
	} else if exists {
		return fail(6, "proxy removal unverified")
	}
	exists, e = t.networkExists(ctx)
	if e != nil {
		return e
	}
	if exists {
		if _, e = t.R.Run(ctx, []string{"docker", "network", "rm", networkName}, nil); e != nil {
			return fail(6, "network removal failed; stop identity capsules first")
		}
	}
	if exists, e = t.networkExists(ctx); e != nil {
		return e
	} else if exists {
		return fail(6, "network removal unverified")
	}
	return nil
}
func (t Tool) reserveIP(c string) (string, error) {
	var chosen string
	e := t.withRegistry(func(m map[string]string) error {
		if _, ok := m[c]; ok {
			return fail(4, "capsule address already reserved")
		}
		used := map[string]bool{}
		for capsule, addr := range m {
			parsed, err := netip.ParseAddr(addr)
			if err != nil || !parsed.Is4() || !capsuleRE.MatchString(capsule) {
				return fail(6, "invalid address registry")
			}
			octets := parsed.As4()
			if octets[0] != 172 || octets[1] != 30 || octets[2] != 80 || octets[3] < 10 || octets[3] > 250 || used[addr] {
				return fail(6, "registry address outside capsule pool or duplicated")
			}
			used[addr] = true
		}
		for n := 10; n <= 250; n++ {
			ip := fmt.Sprintf("172.30.80.%d", n)
			if !used[ip] {
				m[c] = ip
				chosen = ip
				return nil
			}
		}
		return fail(4, "identity address pool exhausted")
	})
	return chosen, e
}

// proxyUser is the CLI principal's uid:gid. The proxy only reads the registry and policy that
// this principal wrote 0600 and appends the audit; running it as that principal (inside the
// hardened, capability-free, read-only container) lets the private files stay private. The
// browser capsules keep their own fixed uid 10001.
func proxyUser() string { return strconv.Itoa(os.Getuid()) + ":" + strconv.Itoa(os.Getgid()) }
