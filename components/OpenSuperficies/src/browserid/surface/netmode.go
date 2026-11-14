package surface

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"time"

	"golang.org/x/sys/unix"
)

// Browser network modes. Legacy is the shared srf-idnet network and srf-egress-proxy;
// open and proxied give every capsule its own network and never name the shared ones.
const (
	NetLegacy  = "legacy"
	NetOpen    = "open"
	NetProxied = "proxied"
)

// openImageReady is false while Image predates the entrypoint's SRF_NET_MODE=open support;
// the commit that pins the rebuilt identity image flips it.
const openImageReady = false

// OpenWitness is written by deploy/firewall/apply.sh once the open-egress rules are applied.
const OpenWitness = "/run/srf-open-egress/rules.sha256"

// openRules is a byte copy of deploy/firewall/srf-open-egress.rules (a test pins the equality).
//
//go:embed srf-open-egress.rules
var openRules []byte
var openRulesSHA = func() string { h := sha256.Sum256(openRules); return hex.EncodeToString(h[:]) }()

// Subnet pools: open uses /28 slots of 172.29.0.0/16, proxied /29 slots of 172.28.0.0/16.
type subnetPool struct {
	b1, b2 byte
	bits   int
	slots  int
}

var subnetPools = map[string]subnetPool{NetOpen: {172, 29, 28, 4096}, NetProxied: {172, 28, 29, 8192}}

// netPlan is the per-capsule network decision of one start.
type netPlan struct {
	Mode, Network, Subnet string
	ProxyIP, CapsuleIP    string // proxied only
	Env                   []string
}

func capsuleNetwork(c, mode string) string {
	if mode == NetOpen {
		return "srf-open-" + c
	}
	return "srf-idnet-" + c
}
func capsuleProxy(c string) string { return "srf-egress-" + c }

// openBridge is the 15-character Linux bridge name the firewall matches with srfo+.
func openBridge(c string) string {
	h := sha256.Sum256([]byte(c))
	return "srfo" + hex.EncodeToString(h[:])[:11]
}
func slotPrefix(mode string, i int) netip.Prefix {
	p := subnetPools[mode]
	off := i << (32 - p.bits)
	return netip.PrefixFrom(netip.AddrFrom4([4]byte{p.b1, p.b2, byte(off >> 8), byte(off)}), p.bits)
}
func hostAddr(p netip.Prefix, n byte) string {
	a := p.Addr().As4()
	a[3] += n
	return netip.AddrFrom4(a).String()
}

func validateNet(kind string, spec StartSpec) error {
	switch spec.Net {
	case "", NetLegacy, NetOpen, NetProxied:
	default:
		return fail(2, "unknown net mode")
	}
	if spec.Net != "" && kind != "browser" {
		return fail(2, "--net requires kind browser")
	}
	if spec.Net != NetProxied && (spec.Policy != "" || spec.Audit || spec.ProxyBin != "") {
		return fail(2, "--policy, --audit and --proxy-bin require --net proxied")
	}
	if spec.Net == NetProxied && (spec.Policy == "" || spec.ProxyBin == "") {
		return fail(2, "--net proxied requires --policy and --proxy-bin")
	}
	return nil
}

// netPreflight runs before any lease, network or docker call.
func (t Tool) netPreflight(spec StartSpec) error {
	switch spec.Net {
	case NetOpen:
		if !openImageReady && !t.openImage {
			return fail(3, "identity image without open net mode; rebuild required")
		}
		return t.openFirewall()
	case NetProxied:
		if !regularFile(spec.ProxyBin, true) || !regularFile(spec.Policy, false) || !mountPath(t.root()) {
			return fail(3, "absolute proxy executable and policy paths required")
		}
	}
	return nil
}

// openFirewall admits open mode only when the root-owned witness names the compiled rules.
func (t Tool) openFirewall() error {
	path, owner := t.witnessPath, t.witnessOwner
	if path == "" {
		path = OpenWitness
	}
	if owner == nil {
		owner = func(p string) (uint32, error) {
			var st unix.Stat_t
			if e := unix.Lstat(p, &st); e != nil {
				return 0, e
			}
			return st.Uid, nil
		}
	}
	refuse := fail(3, "open egress firewall not installed")
	dir := filepath.Dir(path)
	for _, p := range []string{dir, path} {
		fi, e := os.Lstat(p)
		if e != nil || fi.Mode().Perm()&0022 != 0 || (p == dir) != fi.IsDir() || p == path && !fi.Mode().IsRegular() {
			return refuse
		}
		if uid, e := owner(p); e != nil || uid != 0 {
			return refuse
		}
	}
	fd, e := unix.Open(path, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
	if e != nil {
		return refuse
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	b := make([]byte, 256)
	n, _ := f.Read(b)
	if strings.TrimSpace(string(b[:n])) != openRulesSHA {
		return refuse
	}
	return nil
}

// subnetRegistry is private (0600) beside the address registry; callers hold the Tool lock.
func (t Tool) subnetRegistry() string {
	return filepath.Join(filepath.Dir(t.Registry), "netmode-subnets.json")
}
func (t Tool) withSubnets(update func(map[string]map[string]string) error) error {
	if !filepath.IsAbs(t.Registry) {
		return fail(3, "absolute registry path required")
	}
	if e := privateDir(filepath.Dir(t.Registry)); e != nil {
		return e
	}
	// Like withRegistry: control roots with separate Tool locks share this file.
	fd, e := unix.Open(t.subnetRegistry()+".lock", unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if e != nil {
		return fail(4, "subnet registry lock")
	}
	defer unix.Close(fd)
	if unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB) != nil {
		return fail(4, "subnet registry busy")
	}
	defer unix.Flock(fd, unix.LOCK_UN)
	m := map[string]map[string]string{}
	b, e := readPrivate(t.subnetRegistry(), 1<<20)
	if e != nil && !errors.Is(e, os.ErrNotExist) {
		return fail(4, "subnet registry unavailable")
	}
	if e == nil && json.Unmarshal(b, &m) != nil || m == nil {
		return fail(6, "subnet registry corrupt")
	}
	held, used := map[string]bool{}, map[string]bool{}
	for mode, caps := range m {
		pool, ok := subnetPools[mode]
		if !ok || caps == nil {
			return fail(6, "invalid subnet registry")
		}
		for c, s := range caps {
			p, err := netip.ParsePrefix(s)
			if err != nil || !p.Addr().Is4() || !browserCapsuleRE.MatchString(c) || held[c] || used[s] || p.Bits() != pool.bits || p.Masked() != p || p.String() != s {
				return fail(6, "invalid subnet registry")
			}
			if a := p.Addr().As4(); a[0] != pool.b1 || a[1] != pool.b2 {
				return fail(6, "invalid subnet registry")
			}
			held[c], used[s] = true, true
		}
	}
	if e = update(m); e != nil {
		return e
	}
	return atomicJSON(t.subnetRegistry(), m)
}

// reserveSubnet probes deterministically from the sha256(capsule) slot; held subnets are never reused.
func (t Tool) reserveSubnet(c, mode string) (netip.Prefix, error) {
	var chosen netip.Prefix
	e := t.withSubnets(func(m map[string]map[string]string) error {
		for _, caps := range m {
			if _, ok := caps[c]; ok {
				return fail(4, "capsule subnet already reserved")
			}
		}
		used := map[string]bool{}
		for _, s := range m[mode] {
			used[s] = true
		}
		n := subnetPools[mode].slots
		h := sha256.Sum256([]byte(c))
		first := int(binary.BigEndian.Uint32(h[:4]) % uint32(n))
		for k := 0; k < n; k++ {
			p := slotPrefix(mode, (first+k)%n)
			if !used[p.String()] {
				if m[mode] == nil {
					m[mode] = map[string]string{}
				}
				m[mode][c] = p.String()
				chosen = p
				return nil
			}
		}
		return fail(4, "net mode subnet pool exhausted")
	})
	return chosen, e
}
func (t Tool) releaseSubnet(c string) error {
	return t.withSubnets(func(m map[string]map[string]string) error {
		for mode, caps := range m {
			delete(caps, c)
			if len(caps) == 0 {
				delete(m, mode)
			}
		}
		return nil
	})
}

// netModeUp creates the capsule's network (and proxy); on failure it removes what it created.
func (t Tool) netModeUp(ctx context.Context, c string, spec StartSpec) (netPlan, error) {
	plan := netPlan{Mode: spec.Net, Network: capsuleNetwork(c, spec.Net)}
	prefix, e := t.reserveSubnet(c, spec.Net)
	if e != nil {
		return plan, e
	}
	plan.Subnet = prefix.String()
	create := []string{"docker", "network", "create", "--internal", "--subnet", plan.Subnet, "--label", "srf.netmode=proxied", plan.Network}
	if spec.Net == NetOpen {
		create = []string{"docker", "network", "create", "--driver", "bridge", "--subnet", plan.Subnet, "-o", "com.docker.network.bridge.name=" + openBridge(c), "-o", "com.docker.network.bridge.enable_icc=false", "--label", "srf.netmode=open", plan.Network}
		plan.Env = []string{"SRF_NET_MODE=open"}
	} else {
		plan.ProxyIP, plan.CapsuleIP = hostAddr(prefix, 2), hostAddr(prefix, 3)
		plan.Env = []string{"SRF_EGRESS_PROXY=http://" + plan.ProxyIP + ":3128", "SRF_NET_MODE=proxied"}
	}
	if _, e = t.R.Run(ctx, create, nil); e != nil {
		e = fail(6, "capsule network creation failed")
	} else if e = t.verifyCapsuleNetwork(ctx, c, plan); e == nil && spec.Net == NetProxied {
		e = t.capsuleProxyUp(ctx, c, plan, spec)
	}
	if e != nil {
		cleanup, done := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
		defer done()
		_ = t.netModeDown(cleanup, c, spec.Net)
		return plan, e
	}
	return plan, nil
}
func (t Tool) verifyCapsuleNetwork(ctx context.Context, c string, plan netPlan) error {
	b, e := t.R.Run(ctx, []string{"docker", "network", "inspect", "--format", "{{json .}}", plan.Network}, nil)
	var n struct {
		Name     string
		Internal bool
		Options  map[string]string
		IPAM     struct{ Config []struct{ Subnet string } }
	}
	if e != nil || json.Unmarshal(b, &n) != nil || n.Name != plan.Network || n.Internal != (plan.Mode == NetProxied) || len(n.IPAM.Config) != 1 || n.IPAM.Config[0].Subnet != plan.Subnet {
		return fail(6, "capsule network does not match its reservation")
	}
	if plan.Mode == NetOpen && (n.Options["com.docker.network.bridge.name"] != openBridge(c) || n.Options["com.docker.network.bridge.enable_icc"] != "false") {
		return fail(6, "open network bridge options not applied")
	}
	return nil
}
func (t Tool) netModeDir(c string) string { return filepath.Join(t.root(), "netmode", c) }

// capsuleProxyUp hardens the per-capsule proxy exactly like the shared proxy in NetUp.
func (t Tool) capsuleProxyUp(ctx context.Context, c string, plan netPlan, spec StartSpec) error {
	name := capsuleProxy(c)
	regDir := filepath.Join(t.netModeDir(c), "registry")
	if e := privateDir(regDir); e != nil {
		return e
	}
	if e := atomicJSON(filepath.Join(regDir, "registry.json"), map[string]string{c: plan.CapsuleIP}); e != nil {
		return e
	}
	args := []string{"docker", "create", "--name", name, "--label", "srf.netmode=proxied", "--network", "bridge", "--user", proxyUser(), "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--memory", "256m", "--memory-swap", "256m", "--pids-limit", "256", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m", "--mount", "type=bind,src=" + spec.ProxyBin + ",dst=/srf-egress-proxy,readonly", "--mount", "type=bind,src=" + regDir + ",dst=/registry,readonly", "--mount", "type=bind,src=" + filepath.Dir(spec.Policy) + ",dst=/policy,readonly"}
	audit := []string{"--no-audit"}
	if spec.Audit {
		auditDir := filepath.Join(t.netModeDir(c), "audit")
		if e := privateDir(auditDir); e != nil {
			return e
		}
		args = append(args, "--mount", "type=bind,src="+auditDir+",dst=/audit")
		audit = []string{"--audit", "/audit/egress.jsonl"}
	}
	args = append(args, "--entrypoint", "/srf-egress-proxy", "opensuperficies-desktop-arm64:1.1.0", "--listen", plan.ProxyIP+":3128", "--registry", "/registry/registry.json", "--policy", "/policy/"+filepath.Base(spec.Policy))
	if _, e := t.R.Run(ctx, append(args, audit...), nil); e != nil {
		return fail(6, "capsule proxy creation failed")
	}
	if _, e := t.R.Run(ctx, []string{"docker", "network", "connect", "--ip", plan.ProxyIP, plan.Network, name}, nil); e != nil {
		return fail(6, "capsule proxy network connection failed")
	}
	if _, e := t.R.Run(ctx, []string{"docker", "start", name}, nil); e != nil {
		return fail(6, "capsule proxy start failed")
	}
	b, e := t.R.Run(ctx, []string{"docker", "inspect", "--format", `{{len .NetworkSettings.Networks}} {{(index .NetworkSettings.Networks "` + plan.Network + `").IPAddress}} {{if index .NetworkSettings.Networks "bridge"}}bridge{{end}} {{len .HostConfig.PortBindings}}`, name}, nil)
	if e != nil || strings.TrimSpace(string(b)) != "2 "+plan.ProxyIP+" bridge 0" {
		return fail(3, "capsule proxy network attachments or published ports invalid")
	}
	for attempt := 0; attempt < 100; attempt++ {
		logs, err := t.R.Run(ctx, []string{"docker", "logs", "--tail", "30", name}, nil)
		if err != nil {
			return fail(6, "capsule proxy readiness unavailable")
		}
		for _, line := range strings.Split(string(logs), "\n") {
			var report struct{ Status string }
			if json.Unmarshal([]byte(line), &report) == nil && report.Status == "EGRESS_PROXY_READY" {
				b, err := t.R.Run(ctx, []string{"docker", "inspect", "--format", "{{.State.Running}}", name}, nil)
				if err != nil || strings.TrimSpace(string(b)) != "true" {
					return fail(3, "capsule proxy is not running")
				}
				return nil
			}
		}
		if err := t.waitReadiness(ctx); err != nil {
			return fail(6, "capsule proxy readiness cancelled")
		}
	}
	return fail(4, "capsule proxy readiness timeout")
}

// verifyCapsuleNet checks that the launched capsule is attached to its own network only.
func (t Tool) verifyCapsuleNet(ctx context.Context, c string, plan netPlan) error {
	want := "1 attached"
	format := `{{len .NetworkSettings.Networks}} {{if index .NetworkSettings.Networks "` + plan.Network + `"}}attached{{end}}`
	if plan.Mode == NetProxied {
		want = "1 " + plan.CapsuleIP
		format = `{{len .NetworkSettings.Networks}} {{(index .NetworkSettings.Networks "` + plan.Network + `").IPAddress}}`
	}
	b, e := t.R.Run(ctx, []string{"docker", "inspect", "--format", format, c}, nil)
	if e != nil || strings.TrimSpace(string(b)) != want {
		return fail(6, "capsule network attachment mismatch; lease retained")
	}
	return nil
}

// exists reports whether a docker inventory query names exactly one object.
func (t Tool) exists(ctx context.Context, query []string, name string) (bool, error) {
	b, e := t.R.Run(ctx, query, nil)
	if e != nil {
		return false, fail(6, "capsule network inventory unavailable")
	}
	switch strings.TrimSpace(string(b)) {
	case "":
		return false, nil
	case name:
		return true, nil
	}
	return false, fail(6, "ambiguous capsule network inventory")
}

// netModeDown removes the per-capsule proxy, the network and the subnet reservation, verifying each.
func (t Tool) netModeDown(ctx context.Context, c, mode string) error {
	remove := func(query, rm []string, name, what string) error {
		found, e := t.exists(ctx, query, name)
		if e != nil {
			return e
		}
		if found {
			if _, e = t.R.Run(ctx, rm, nil); e != nil {
				return fail(6, what+" removal failed; lease retained")
			}
		}
		if found, e = t.exists(ctx, query, name); e != nil || found {
			return fail(6, what+" removal unverified; lease retained")
		}
		return nil
	}
	if mode == NetProxied {
		p := capsuleProxy(c)
		if e := remove([]string{"docker", "ps", "-a", "--filter", "name=^/" + p + "$", "--format", "{{.Names}}"}, []string{"docker", "rm", "-f", p}, p, "capsule proxy"); e != nil {
			return e
		}
	}
	n := capsuleNetwork(c, mode)
	if e := remove([]string{"docker", "network", "ls", "--filter", "name=^" + n + "$", "--format", "{{.Name}}"}, []string{"docker", "network", "rm", n}, n, "capsule network"); e != nil {
		return e
	}
	if e := t.releaseSubnet(c); e != nil {
		return e
	}
	if os.RemoveAll(filepath.Join(t.netModeDir(c), "registry")) != nil {
		return fail(6, "capsule proxy registry cleanup failed; lease retained")
	}
	return nil
}
