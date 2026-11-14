package surface

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

// netFake models per-capsule networks and proxies; everything else goes to fakeRunner.
type netFake struct {
	*fakeRunner
	all      [][]string
	nets     map[string][]string // name -> create argv
	proxies  map[string][]string // name -> create argv
	failOn   string              // joined-argv substring that fails
	stuck    string              // joined-argv substring of an rm that leaves the object in place
	attached string              // capsule attachment report override
}

func newNetFake(r *fakeRunner) *netFake {
	return &netFake{fakeRunner: r, nets: map[string][]string{}, proxies: map[string][]string{}}
}
func after(a []string, flag string) string {
	if i := slices.Index(a, flag); i >= 0 && i+1 < len(a) {
		return a[i+1]
	}
	return ""
}
func (r *netFake) Run(ctx context.Context, a []string, in []byte) ([]byte, error) {
	r.all = append(r.all, slices.Clone(a))
	s := strings.Join(a, " ")
	last := a[len(a)-1]
	if r.failOn != "" && strings.Contains(s, r.failOn) {
		return nil, errors.New("injected failure")
	}
	switch {
	case strings.HasPrefix(s, "docker network create"):
		r.nets[last] = slices.Clone(a)
	case strings.HasPrefix(s, "docker network inspect"):
		c, ok := r.nets[last]
		if !ok {
			return nil, errors.New("absent")
		}
		opts := map[string]string{}
		for i, v := range c {
			if v == "-o" {
				k, val, _ := strings.Cut(c[i+1], "=")
				opts[k] = val
			}
		}
		b, _ := json.Marshal(map[string]any{"Name": last, "Internal": slices.Contains(c, "--internal"), "Options": opts, "IPAM": map[string]any{"Config": []map[string]string{{"Subnet": after(c, "--subnet")}}}})
		return b, nil
	case strings.HasPrefix(s, "docker network ls"):
		name := strings.TrimSuffix(strings.TrimPrefix(after(a, "--filter"), "name=^"), "$")
		if _, ok := r.nets[name]; ok {
			return []byte(name + "\n"), nil
		}
		return nil, nil
	case strings.HasPrefix(s, "docker network rm"):
		if r.stuck == "" || !strings.Contains(s, r.stuck) {
			delete(r.nets, last)
		}
	case strings.HasPrefix(s, "docker network connect"):
	case strings.HasPrefix(s, "docker create --name srf-egress-"):
		r.proxies[a[3]] = slices.Clone(a)
	case strings.HasPrefix(s, "docker ps -a --filter name=^/srf-egress-"):
		name := strings.TrimSuffix(strings.TrimPrefix(after(a, "--filter"), "name=^/"), "$")
		if _, ok := r.proxies[name]; ok {
			return []byte(name + "\n"), nil
		}
		return nil, nil
	case strings.HasPrefix(s, "docker rm -f srf-egress-"):
		if r.stuck == "" || !strings.Contains(s, r.stuck) {
			delete(r.proxies, last)
		}
	case strings.HasPrefix(s, "docker start srf-egress-"):
	case strings.HasPrefix(s, "docker inspect") && strings.HasPrefix(last, "srf-egress-"):
		if strings.Contains(s, "PortBindings") {
			ip, _, _ := strings.Cut(after(r.proxies[last], "--listen"), ":")
			return []byte("2 " + ip + " bridge 0\n"), nil
		}
		return []byte("true\n"), nil
	case strings.HasPrefix(s, "docker logs") && strings.HasPrefix(last, "srf-egress-"):
		return []byte(`{"status":"EGRESS_PROXY_READY"}` + "\n"), nil
	case strings.HasPrefix(s, "docker inspect --format {{len .NetworkSettings.Networks}}"):
		if r.attached != "" {
			return []byte(r.attached), nil
		}
		if strings.Contains(s, "IPAddress") {
			return []byte("1 " + r.capsuleIP() + "\n"), nil
		}
		return []byte("1 attached\n"), nil
	default:
		return r.fakeRunner.Run(ctx, a, in)
	}
	return nil, nil
}

// capsuleIP is .3 of the proxied network's subnet, as docker would assign for --ip.
func (r *netFake) capsuleIP() string {
	for _, c := range r.nets {
		if p, e := netip.ParsePrefix(after(c, "--subnet")); e == nil && slices.Contains(c, "--internal") {
			a := p.Addr().As4()
			a[3] += 3
			return netip.AddrFrom4(a).String()
		}
	}
	return ""
}
func (r *netFake) joined() string {
	out := ""
	for _, a := range r.all {
		out += strings.Join(a, " ") + "\n"
	}
	return out
}
func (r *netFake) index(prefix string) int {
	return slices.IndexFunc(r.all, func(a []string) bool { return strings.HasPrefix(strings.Join(a, " "), prefix) })
}

func rulesSHA(t *testing.T) string {
	b, e := os.ReadFile(filepath.Join("..", "..", "..", "deploy", "firewall", "srf-open-egress.rules"))
	if e != nil {
		t.Fatal(e)
	}
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

// netTool is setup() with the per-capsule network fake, an installed witness and proxied inputs.
func netTool(t *testing.T) (*Tool, *netFake, *fakeLaunch, StartSpec) {
	t.Helper()
	tool, r, l, _ := setup(t)
	nf := newNetFake(r)
	tool.R = nf
	dir := filepath.Join(filepath.Dir(tool.Root), "run-witness")
	if e := os.Mkdir(dir, 0755); e != nil {
		t.Fatal(e)
	}
	tool.witnessPath = filepath.Join(dir, "rules.sha256")
	if e := os.WriteFile(tool.witnessPath, []byte(rulesSHA(t)+"\n"), 0644); e != nil {
		t.Fatal(e)
	}
	tool.witnessOwner = func(string) (uint32, error) { return 0, nil }
	tool.openImage = true
	bin := filepath.Join(filepath.Dir(tool.Root), "proxy-bin")
	policy := filepath.Join(filepath.Dir(tool.Root), "policy", "policy.json")
	if os.WriteFile(bin, []byte("fake"), 0700) != nil || os.Mkdir(filepath.Dir(policy), 0700) != nil || os.WriteFile(policy, []byte(`{}`), 0600) != nil {
		t.Fatal("fixture")
	}
	return tool, nf, l, StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetProxied, Policy: policy, ProxyBin: bin}
}
func expectedSlot(c string, slots uint32) uint32 {
	h := sha256.Sum256([]byte(c))
	return binary.BigEndian.Uint32(h[:4]) % slots
}

// sharedNamed reports an argv that names the shared proxy or network as an object.
func sharedNamed(a []string) bool {
	for _, v := range a {
		for _, n := range []string{"srf-egress-proxy", "srf-idnet"} {
			if v == n || strings.Contains(v, `"`+n+`"`) || strings.Contains(v, "^"+n+"$") || strings.Contains(v, "^/"+n+"$") {
				return true
			}
		}
	}
	return false
}

func TestNetModeRefusalsBeforeDocker(t *testing.T) {
	tool, nf, l, proxied := netTool(t)
	ctx := context.Background()
	program := StartSpec{Identity: "identity-a", Windows: 1, Kind: "program", Program: "notepad"}
	vm := StartSpec{Identity: "identity-a", Windows: 1, Kind: "vm", Program: "notepad-vm"}
	browser := StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1}
	with := func(s StartSpec, f func(*StartSpec)) StartSpec { f(&s); return s }
	for name, spec := range map[string]StartSpec{
		"program legacy":       with(program, func(s *StartSpec) { s.Net = NetLegacy }),
		"program open":         with(program, func(s *StartSpec) { s.Net = NetOpen }),
		"vm proxied":           with(vm, func(s *StartSpec) { s.Net = NetProxied; s.Policy = proxied.Policy; s.ProxyBin = proxied.ProxyBin }),
		"unknown":              with(browser, func(s *StartSpec) { s.Net = "bridge" }),
		"policy legacy":        with(browser, func(s *StartSpec) { s.Policy = proxied.Policy }),
		"policy open":          with(browser, func(s *StartSpec) { s.Net = NetOpen; s.Policy = proxied.Policy }),
		"audit legacy":         with(browser, func(s *StartSpec) { s.Audit = true }),
		"audit open":           with(browser, func(s *StartSpec) { s.Net = NetOpen; s.Audit = true }),
		"proxy-bin legacy":     with(browser, func(s *StartSpec) { s.ProxyBin = proxied.ProxyBin }),
		"proxy-bin explicit":   with(browser, func(s *StartSpec) { s.Net = NetLegacy; s.ProxyBin = proxied.ProxyBin }),
		"proxied no policy":    with(proxied, func(s *StartSpec) { s.Policy = "" }),
		"proxied no proxy-bin": with(proxied, func(s *StartSpec) { s.ProxyBin = "" }),
	} {
		t.Run(name, func(t *testing.T) {
			if e := ValidateStartSpec(spec); Code(e) != 2 {
				t.Fatal("validate", e)
			}
			if _, e := tool.Start(ctx, spec); Code(e) != 2 {
				t.Fatal("start", e)
			}
		})
	}
	// Proxy executable validated like NetUp: executable, absolute, no symlink.
	link := filepath.Join(filepath.Dir(proxied.ProxyBin), "proxy-link")
	plain := filepath.Join(filepath.Dir(proxied.ProxyBin), "proxy-plain")
	if os.Symlink(proxied.ProxyBin, link) != nil || os.WriteFile(plain, []byte("x"), 0600) != nil {
		t.Fatal("fixture")
	}
	for _, bin := range []string{link, plain, "relative/proxy", filepath.Join(filepath.Dir(link), "absent")} {
		spec := proxied
		spec.ProxyBin = bin
		if _, e := tool.Start(ctx, spec); Code(e) != 3 {
			t.Fatal(bin, e)
		}
	}
	spec := proxied
	spec.Policy = filepath.Dir(proxied.Policy)
	if _, e := tool.Start(ctx, spec); Code(e) != 3 {
		t.Fatal("directory policy", e)
	}
	if len(nf.all) != 0 || len(l.plans) != 0 {
		t.Fatal("refusal reached docker", nf.all)
	}
	if lease, e := tool.Store.Acquire("identity-a", "other"); e != nil {
		t.Fatal("refusal held the lease", e)
	} else {
		_ = tool.Store.Release(lease)
	}
}

func TestOpenRefusedUntilImageRebuilt(t *testing.T) {
	tool, _, _, _ := netTool(t)
	tool.openImage = false
	if e := tool.netPreflight(StartSpec{Net: NetOpen}); Code(e) != 3 || !strings.Contains(e.Error(), "rebuild") {
		t.Fatal("open must fail closed on the pre-open identity image", e)
	}
}

func TestOpenWitness(t *testing.T) {
	tool, nf, l, _ := netTool(t)
	if e := tool.openFirewall(); e != nil {
		t.Fatal("installed witness refused", e)
	}
	if e := (Tool{}).openFirewall(); Code(e) != 3 && e != nil {
		t.Fatal(e) // production path: absent on this host or installed; never another code
	}
	sum := rulesSHA(t)
	dir := filepath.Dir(tool.witnessPath)
	for name, mutate := range map[string]func() func(){
		"missing": func() func() {
			_ = os.Remove(tool.witnessPath)
			return func() { _ = os.WriteFile(tool.witnessPath, []byte(sum+"\n"), 0644) }
		},
		"file owner": func() func() {
			tool.witnessOwner = func(p string) (uint32, error) {
				if p == tool.witnessPath {
					return 1000, nil
				}
				return 0, nil
			}
			return func() { tool.witnessOwner = func(string) (uint32, error) { return 0, nil } }
		},
		"dir owner": func() func() {
			tool.witnessOwner = func(p string) (uint32, error) {
				if p == dir {
					return 1000, nil
				}
				return 0, nil
			}
			return func() { tool.witnessOwner = func(string) (uint32, error) { return 0, nil } }
		},
		"owner error": func() func() {
			tool.witnessOwner = func(string) (uint32, error) { return 0, errors.New("stat") }
			return func() { tool.witnessOwner = func(string) (uint32, error) { return 0, nil } }
		},
		"wrong hash": func() func() {
			_ = os.WriteFile(tool.witnessPath, []byte(strings.Repeat("0", 64)+"\n"), 0644)
			return func() { _ = os.WriteFile(tool.witnessPath, []byte(sum+"\n"), 0644) }
		},
		"empty": func() func() {
			_ = os.WriteFile(tool.witnessPath, nil, 0644)
			return func() { _ = os.WriteFile(tool.witnessPath, []byte(sum+"\n"), 0644) }
		},
		"group writable file": func() func() {
			_ = os.Chmod(tool.witnessPath, 0664)
			return func() { _ = os.Chmod(tool.witnessPath, 0644) }
		},
		"other writable dir": func() func() {
			_ = os.Chmod(dir, 0757)
			return func() { _ = os.Chmod(dir, 0755) }
		},
		"symlink": func() func() {
			real := tool.witnessPath + ".real"
			_ = os.Rename(tool.witnessPath, real)
			_ = os.Symlink(real, tool.witnessPath)
			return func() { _ = os.Remove(tool.witnessPath); _ = os.Rename(real, tool.witnessPath) }
		},
		"directory": func() func() {
			_ = os.Remove(tool.witnessPath)
			_ = os.Mkdir(tool.witnessPath, 0755)
			return func() { _ = os.Remove(tool.witnessPath); _ = os.WriteFile(tool.witnessPath, []byte(sum+"\n"), 0644) }
		},
	} {
		t.Run(name, func(t *testing.T) {
			restore := mutate()
			defer restore()
			if e := tool.openFirewall(); Code(e) != 3 || e.Error() != "open egress firewall not installed" {
				t.Fatal(e)
			}
			if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}); Code(e) != 3 {
				t.Fatal(e)
			}
		})
	}
	if len(nf.all) != 0 || len(l.plans) != 0 {
		t.Fatal("witness refusal reached docker", nf.all)
	}
	if _, e := os.Lstat(tool.subnetRegistry()); !os.IsNotExist(e) {
		t.Fatal("witness refusal reserved a subnet")
	}
	if e := tool.openFirewall(); e != nil {
		t.Fatal("restored witness refused", e)
	}
}

func TestOpenModeArgvAndStop(t *testing.T) {
	tool, nf, l, _ := netTool(t)
	c, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen})
	if e != nil {
		t.Fatal(e)
	}
	slot := expectedSlot("srf-id-test", 4096)
	subnet := fmt.Sprintf("172.29.%d.%d/28", slot*16/256, slot*16%256)
	h := sha256.Sum256([]byte("srf-id-test"))
	bridge := "srfo" + hex.EncodeToString(h[:])[:11]
	create := []string{"docker", "network", "create", "--driver", "bridge", "--subnet", subnet, "-o", "com.docker.network.bridge.name=" + bridge, "-o", "com.docker.network.bridge.enable_icc=false", "--label", "srf.netmode=open", "srf-open-srf-id-test"}
	if len(bridge) != 15 || nf.index("docker network create") < 0 || !slices.Equal(nf.all[nf.index("docker network create")], create) {
		t.Fatal(nf.joined())
	}
	argv := l.plans[0].Argv
	if after(argv, "--network") != "srf-open-srf-id-test" || slices.Contains(argv, "--ip") || !slices.Contains(argv, "SRF_NET_MODE=open") {
		t.Fatal(argv)
	}
	for _, v := range argv {
		if strings.Contains(v, "SRF_EGRESS_PROXY") || strings.Contains(v, "172.30.80.") {
			t.Fatal("open capsule got proxy or shared address", v)
		}
	}
	for _, a := range nf.all {
		if sharedNamed(a) {
			t.Fatal("open mode named a shared object", a)
		}
	}
	if b, _ := os.ReadFile(tool.Registry); len(b) != 0 && string(b) != "{}" {
		t.Fatal("open mode reserved a shared registry address", string(b))
	}
	b, _ := json.Marshal(c)
	if c.NetMode != NetOpen || !strings.Contains(string(b), `"net_mode":"open"`) {
		t.Fatal(string(b))
	}
	s, e := tool.load("srf-id-test")
	if e != nil || s.NetMode != NetOpen {
		t.Fatal(s.NetMode, e)
	}
	if reg, e := readPrivate(tool.subnetRegistry(), 1<<20); e != nil || string(reg) != `{"open":{"srf-id-test":"`+subnet+`"}}` {
		t.Fatal(string(reg), e)
	}
	before := len(nf.all)
	if e = tool.Stop(context.Background(), "srf-id-test"); e != nil {
		t.Fatal(e)
	}
	verify := slices.IndexFunc(nf.all[before:], func(a []string) bool {
		return slices.Equal(a, []string{"docker", "ps", "-a", "--format", "{{.Names}}", "--filter", "name=^/srf-id-test$"})
	})
	rm := slices.IndexFunc(nf.all[before:], func(a []string) bool {
		return slices.Equal(a, []string{"docker", "network", "rm", "srf-open-srf-id-test"})
	})
	if verify < 0 || rm < verify || len(nf.nets) != 0 {
		t.Fatal("stop order or network left", nf.joined())
	}
	if reg, e := readPrivate(tool.subnetRegistry(), 1<<20); e != nil || string(reg) != `{}` {
		t.Fatal("subnet reservation kept", string(reg), e)
	}
	if _, e = os.Lstat(tool.statePath("srf-id-test")); !os.IsNotExist(e) {
		t.Fatal("state kept")
	}
}

func TestProxiedModeArgvAuditAndStop(t *testing.T) {
	for _, audit := range []bool{false, true} {
		t.Run(fmt.Sprint("audit=", audit), func(t *testing.T) {
			tool, nf, l, spec := netTool(t)
			spec.Audit = audit
			c, e := tool.Start(context.Background(), spec)
			if e != nil {
				t.Fatal(e)
			}
			slot := expectedSlot("srf-id-test", 8192)
			base := fmt.Sprintf("172.28.%d.%d", slot*8/256, slot*8%256)
			proxyIP := fmt.Sprintf("172.28.%d.%d", slot*8/256, slot*8%256+2)
			capIP := fmt.Sprintf("172.28.%d.%d", slot*8/256, slot*8%256+3)
			if !slices.Equal(nf.all[nf.index("docker network create")], []string{"docker", "network", "create", "--internal", "--subnet", base + "/29", "--label", "srf.netmode=proxied", "srf-idnet-srf-id-test"}) {
				t.Fatal(nf.joined())
			}
			argv := l.plans[0].Argv
			if after(argv, "--network") != "srf-idnet-srf-id-test" || after(argv, "--ip") != capIP || !slices.Contains(argv, "SRF_EGRESS_PROXY=http://"+proxyIP+":3128") || !slices.Contains(argv, "SRF_NET_MODE=proxied") {
				t.Fatal(argv)
			}
			for _, a := range nf.all {
				if sharedNamed(a) {
					t.Fatal("proxied mode named a shared object", a)
				}
			}
			p := nf.proxies["srf-egress-srf-id-test"]
			joined := strings.Join(p, " ")
			for _, want := range []string{"--network bridge", "--cap-drop ALL", "--read-only", "--user " + proxyUser(), "--listen " + proxyIP + ":3128", "--registry /registry/registry.json", "--policy /policy/policy.json", "src=" + filepath.Dir(spec.Policy) + ",dst=/policy,readonly"} {
				if !strings.Contains(joined, want) {
					t.Fatal(want, joined)
				}
			}
			if audit != strings.Contains(joined, "--audit /audit/egress.jsonl") || audit == slices.Contains(p, "--no-audit") || audit != strings.Contains(joined, ",dst=/audit") {
				t.Fatal("audit flags", joined)
			}
			for _, want := range []string{"docker network connect --ip " + proxyIP + " srf-idnet-srf-id-test srf-egress-srf-id-test", "docker start srf-egress-srf-id-test"} {
				if !strings.Contains(nf.joined(), want) {
					t.Fatal(want, nf.joined())
				}
			}
			reg, e := readPrivate(filepath.Join(tool.netModeDir("srf-id-test"), "registry", "registry.json"), 1<<20)
			if e != nil || string(reg) != `{"srf-id-test":"`+capIP+`"}` {
				t.Fatal(string(reg), e)
			}
			if c.NetMode != NetProxied {
				t.Fatal(c)
			}
			auditDir := filepath.Join(tool.netModeDir("srf-id-test"), "audit")
			if _, e = os.Lstat(auditDir); audit == os.IsNotExist(e) {
				t.Fatal("audit dir presence", e)
			}
			before := len(nf.all)
			if e = tool.Stop(context.Background(), "srf-id-test"); e != nil {
				t.Fatal(e)
			}
			tail := nf.all[before:]
			pr := slices.IndexFunc(tail, func(a []string) bool {
				return slices.Equal(a, []string{"docker", "rm", "-f", "srf-egress-srf-id-test"})
			})
			nr := slices.IndexFunc(tail, func(a []string) bool {
				return slices.Equal(a, []string{"docker", "network", "rm", "srf-idnet-srf-id-test"})
			})
			if pr < 0 || nr < pr || len(nf.nets) != 0 || len(nf.proxies) != 0 {
				t.Fatal("stop order or leftovers", nf.joined())
			}
			if b, e := readPrivate(tool.subnetRegistry(), 1<<20); e != nil || string(b) != "{}" {
				t.Fatal(string(b), e)
			}
			if _, e = os.Lstat(filepath.Join(tool.netModeDir("srf-id-test"), "registry")); !os.IsNotExist(e) {
				t.Fatal("per-capsule registry kept")
			}
		})
	}
}

// The per-capsule proxy keeps the shared proxy's hardening flags, image and entrypoint.
func TestCapsuleProxyHardeningMatchesNetUp(t *testing.T) {
	tool, nf, _, spec := netTool(t)
	if _, e := tool.Start(context.Background(), spec); e != nil {
		t.Fatal(e)
	}
	per := nf.proxies["srf-egress-srf-id-test"]
	root := t.TempDir()
	_ = os.Chmod(root, 0700)
	r := &networkRunner{}
	shared := Tool{R: r, Root: filepath.Join(root, "state"), Registry: filepath.Join(root, "registry.json")}
	if e := shared.NetUp(context.Background(), NetSpec{ProxyBin: spec.ProxyBin, Policy: spec.Policy, AuditDir: filepath.Join(root, "audit")}); e != nil {
		t.Fatal(e)
	}
	var up []string
	for _, a := range r.calls {
		if slices.Contains(a, "create") && slices.Contains(a, "--name") {
			up = a
		}
	}
	hardening := func(a []string) []string {
		return a[slices.Index(a, "--network"):slices.Index(a, "--mount")]
	}
	tail := func(a []string) []string { i := slices.Index(a, "--entrypoint"); return a[i : i+3] }
	if up == nil || !slices.Equal(hardening(per), hardening(up)) || !slices.Equal(tail(per), tail(up)) {
		t.Fatal(per, up)
	}
}

func TestSubnetProbingCollisionAndExhaustion(t *testing.T) {
	tool, nf, l, _ := netTool(t)
	slot := expectedSlot("srf-id-test", 4096)
	taken := fmt.Sprintf("172.29.%d.%d/28", slot*16/256, slot*16%256)
	next := (slot + 1) % 4096
	if e := atomicJSON(tool.subnetRegistry(), map[string]map[string]string{"open": {"srf-id-other": taken}}); e != nil {
		t.Fatal(e)
	}
	p, e := tool.reserveSubnet("srf-id-test", NetOpen)
	if e != nil || p.String() != fmt.Sprintf("172.29.%d.%d/28", next*16/256, next*16%256) {
		t.Fatal(p, e)
	}
	if _, e = tool.reserveSubnet("srf-id-test", NetProxied); Code(e) != 4 {
		t.Fatal("one capsule held two subnets", e)
	}
	if e = tool.releaseSubnet("srf-id-test"); e != nil {
		t.Fatal(e)
	}
	// Wrap-around: the last slot is taken, the walk continues at slot 0.
	full := map[string]string{}
	for i := 0; i < 4096; i++ {
		full[fmt.Sprintf("srf-id-p%d", i)] = fmt.Sprintf("172.29.%d.%d/28", i*16/256, i*16%256)
	}
	if e = atomicJSON(tool.subnetRegistry(), map[string]map[string]string{"open": full}); e != nil {
		t.Fatal(e)
	}
	if _, e = tool.reserveSubnet("srf-id-test", NetOpen); Code(e) != 4 {
		t.Fatal("exhaustion", e)
	}
	if _, e = tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}); Code(e) != 4 || len(l.plans) != 0 || nf.index("docker network create") >= 0 {
		t.Fatal("exhausted start", e, nf.joined())
	}
	if lease, e := tool.Store.Acquire("identity-a", "other"); e != nil {
		t.Fatal("exhausted start kept the lease", e)
	} else {
		_ = tool.Store.Release(lease)
	}
	// Proxied has its own pool; a full open pool does not block it.
	if p, e = tool.reserveSubnet("srf-id-test", NetProxied); e != nil || p.Bits() != 29 || p.Addr().As4()[1] != 28 {
		t.Fatal(p, e)
	}
	for _, bad := range []string{`{"open":{"srf-id-a":"172.29.0.8/28"}}`, `{"open":{"srf-id-a":"172.28.0.0/28"}}`, `{"open":{"srf-id-a":"172.29.0.0/29"}}`, `{"proxied":{"srf-id-a":"172.28.0.0/29","srf-id-b":"172.28.0.0/29"}}`, `{"open":{"srf-id-a":"172.29.0.0/28"},"proxied":{"srf-id-a":"172.28.0.0/29"}}`, `{"bridge":{}}`, `{"open":null}`, `{"open":{"srf-pg-a":"172.29.0.0/28"}}`, `{"open":{"srf-id-a":"fd00::/28"}}`, `null`, `[`} {
		if e = os.WriteFile(tool.subnetRegistry(), []byte(bad), 0600); e != nil {
			t.Fatal(e)
		}
		if _, e = tool.reserveSubnet("srf-id-new", NetOpen); Code(e) != 6 {
			t.Fatal(bad, e)
		}
	}
	// Another control root holding the registry lock is busy, never a lost update.
	_ = os.Remove(tool.subnetRegistry())
	fd, e := unix.Open(tool.subnetRegistry()+".lock", unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC, 0600)
	if e != nil || unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB) != nil {
		t.Fatal("lock fixture", e)
	}
	if _, e = tool.reserveSubnet("srf-id-new", NetOpen); Code(e) != 4 {
		t.Fatal("held lock", e)
	}
	_ = unix.Close(fd)
	if _, e = tool.reserveSubnet("srf-id-new", NetOpen); e != nil {
		t.Fatal(e)
	}
}

func TestNetModeStopFailureRetainsLease(t *testing.T) {
	for _, tc := range []struct{ mode, failOn, stuck string }{
		{NetOpen, "docker network rm", ""},
		{NetOpen, "", "docker network rm"},
		{NetOpen, "docker network ls", ""},
		{NetProxied, "docker rm -f srf-egress-", ""},
		{NetProxied, "", "docker rm -f srf-egress-"},
		{NetProxied, "docker network rm", ""},
	} {
		t.Run(tc.mode+tc.failOn+tc.stuck, func(t *testing.T) {
			tool, nf, _, spec := netTool(t)
			if tc.mode == NetOpen {
				spec = StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}
			}
			if _, e := tool.Start(context.Background(), spec); e != nil {
				t.Fatal(e)
			}
			nf.failOn, nf.stuck = tc.failOn, tc.stuck
			if e := tool.Stop(context.Background(), "srf-id-test"); Code(e) != 6 {
				t.Fatal(e)
			}
			if _, e := tool.Store.Acquire("identity-a", "other"); e == nil {
				t.Fatal("failed stop released the lease")
			}
			if _, e := tool.load("srf-id-test"); e != nil {
				t.Fatal("failed stop dropped state", e)
			}
			if b, _ := readPrivate(tool.subnetRegistry(), 1<<20); !strings.Contains(string(b), "srf-id-test") {
				t.Fatal("failed stop released the subnet", string(b))
			}
			nf.failOn, nf.stuck = "", ""
			if e := tool.Stop(context.Background(), "srf-id-test"); e != nil {
				t.Fatal("retry", e)
			}
			if len(nf.nets) != 0 || len(nf.proxies) != 0 {
				t.Fatal("retry left resources")
			}
		})
	}
}

func TestNetModeFailedStartCleansUp(t *testing.T) {
	for _, tc := range []struct{ mode, failOn, attached string }{
		{NetOpen, "docker network create", ""},
		{NetOpen, "docker network inspect", ""},
		{NetProxied, "docker create --name srf-egress-", ""},
		{NetProxied, "docker network connect", ""},
		{NetProxied, "docker start srf-egress-", ""},
		{NetProxied, "PortBindings", ""},
		{NetProxied, "docker logs --tail 30 srf-egress-", ""},
		{NetProxied, "docker run --rm", ""}, // stale browser lock cleanup, after the session is saved
	} {
		t.Run(tc.mode+" "+tc.failOn, func(t *testing.T) {
			tool, nf, l, spec := netTool(t)
			if tc.mode == NetOpen {
				spec = StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}
			}
			nf.failOn = tc.failOn
			if _, e := tool.Start(context.Background(), spec); e == nil {
				t.Fatal("start succeeded")
			}
			if len(l.plans) != 0 || len(nf.nets) != 0 || len(nf.proxies) != 0 {
				t.Fatal("failed start left resources", nf.joined())
			}
			if b, e := readPrivate(tool.subnetRegistry(), 1<<20); e != nil || string(b) != "{}" {
				t.Fatal("failed start kept the subnet", string(b), e)
			}
			if lease, e := tool.Store.Acquire("identity-a", "other"); e != nil {
				t.Fatal("failed start kept the lease", e)
			} else {
				_ = tool.Store.Release(lease)
			}
		})
	}
	// A wrong post-launch attachment is a lease-retaining failure, recovered by stop.
	tool, nf, _, _ := netTool(t)
	nf.attached = "2 attached\n"
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}); Code(e) != 6 {
		t.Fatal(e)
	}
	if e := tool.Stop(context.Background(), "srf-id-test"); e != nil || len(nf.nets) != 0 {
		t.Fatal(e)
	}
	// A network without the bridge options is refused before launch.
	tool, nf, l, _ := netTool(t)
	tool.R = &optionDropper{nf}
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}); Code(e) != 6 || len(l.plans) != 0 || len(nf.nets) != 0 {
		t.Fatal(e)
	}
}

// optionDropper reports the open network without the requested bridge options.
type optionDropper struct{ *netFake }

func (o *optionDropper) Run(ctx context.Context, a []string, in []byte) ([]byte, error) {
	b, e := o.netFake.Run(ctx, a, in)
	if e == nil && strings.HasPrefix(strings.Join(a, " "), "docker network inspect") {
		b = []byte(strings.Replace(string(b), `"com.docker.network.bridge.enable_icc":"false"`, `"com.docker.network.bridge.enable_icc":"true"`, 1))
	}
	return b, e
}

func TestNetModeStateTamper(t *testing.T) {
	tool, _, _, _ := netTool(t)
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: NetOpen}); e != nil {
		t.Fatal(e)
	}
	s, e := tool.load("srf-id-test")
	if e != nil {
		t.Fatal(e)
	}
	for _, mutate := range []func(*session){
		func(s *session) { s.NetMode = "" },
		func(s *session) { s.Capsule.NetMode = NetProxied },
		func(s *session) { s.NetMode, s.Capsule.NetMode = NetLegacy, NetLegacy },
		func(s *session) { s.NetMode, s.Capsule.NetMode = "bridge", "bridge" },
	} {
		bad := s
		mutate(&bad)
		if e = atomicJSON(tool.statePath("srf-id-test"), bad); e != nil {
			t.Fatal(e)
		}
		if _, e = tool.load("srf-id-test"); Code(e) != 6 {
			t.Fatal(bad.NetMode, bad.Capsule.NetMode, e)
		}
	}
}

// Legacy (absent --net, or explicit legacy) keeps today's argv byte for byte.
func TestLegacyStartGolden(t *testing.T) {
	const want = `PLAN ["docker","run","-d","--name","srf-id-test","--ip","172.30.80.10","--security-opt","seccomp=<ROOT>/state/identity-seccomp.json","--user","10001:10001","--read-only","--cap-drop","ALL","--network","srf-idnet","--cpuset-cpus","10-14","--memory","4g","--memory-swap","4g","--pids-limit","256","--shm-size","2g","--tmpfs","/run/vqa/fb:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m","--tmpfs","/tmp:rw,nosuid,nodev,mode=1777,size=256m","--tmpfs","/run/user/10001:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m","--tmpfs","/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m","--tmpfs","/home/worker:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=64m","--mount","type=bind,src=<ROOT>/store/profiles/identity-a,dst=/profile","--mount","type=bind,src=<ROOT>/state/view/srf-id-test/s,dst=/srfview","--env","SRF_EGRESS_PROXY=http://172.30.80.2:3128","--env","SRF_WINDOWS=1","--env","SRF_TILES=[{\"id\":\"w1\",\"x\":0,\"y\":0,\"w\":1280,\"h\":900}]","opensuperficies-identity-browser-arm64:0.1.0"]
CALL ["docker","inspect","--format","{{.State.Running}}","srf-egress-proxy"]
CALL ["docker","network","inspect","--format","{{json .}}","srf-idnet"]
CALL ["docker","run","--rm","--user","10001:10001","--network","none","--read-only","--cap-drop","ALL","--mount","type=bind,src=<ROOT>/store/profiles/identity-a,dst=/profile","--entrypoint","/bin/rm","opensuperficies-identity-browser-arm64:0.1.0","-f","/profile/SingletonLock","/profile/SingletonCookie","/profile/SingletonSocket"]
CALL ["docker","inspect","--format","{{(index .NetworkSettings.Networks \"srf-idnet\").IPAddress}}","srf-id-test"]
CALL ["docker","logs","--tail","30","srf-id-test"]
CALL ["docker","exec","-u","10001:10001","-e","DISPLAY=:100","srf-id-test","timeout","--signal=TERM","--kill-after=1s","5s","xdotool","getwindowgeometry","--shell","291"]
`
	for _, net := range []string{"", NetLegacy} {
		tool, r, l, _ := setup(t)
		c, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-test", Windows: 1, Net: net})
		if e != nil {
			t.Fatal(e)
		}
		b, _ := json.Marshal(l.plans[0].Argv)
		got := "PLAN " + string(b) + "\n"
		for _, a := range r.calls {
			b, _ := json.Marshal(a)
			got += "CALL " + string(b) + "\n"
		}
		got = strings.ReplaceAll(got, filepath.Dir(tool.Root), "<ROOT>")
		if got != want {
			t.Fatalf("net %q:\n%s", net, got)
		}
		if b, _ = json.Marshal(c); strings.Contains(string(b), "net_mode") {
			t.Fatal("legacy wire changed", string(b))
		}
		if s, e := tool.load("srf-id-test"); e != nil || s.NetMode != "" {
			t.Fatal(s.NetMode, e)
		}
		if _, e = os.Lstat(tool.subnetRegistry()); !os.IsNotExist(e) {
			t.Fatal("legacy touched the subnet registry")
		}
	}
}

// The embedded rules are byte-identical to deploy/firewall; apply.sh never flushes foreign chains.
func TestOpenRulesDraft(t *testing.T) {
	deploy := filepath.Join("..", "..", "..", "deploy", "firewall")
	b, e := os.ReadFile(filepath.Join(deploy, "srf-open-egress.rules"))
	if e != nil || string(b) != string(openRules) || rulesSHA(t) != openRulesSHA {
		t.Fatal("embedded rules drifted from deploy/firewall", e)
	}
	var rules []string
	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, "-") {
			rules = append(rules, line)
		}
	}
	want := []string{}
	for _, r := range []string{"100.90.1.1", "100.100.1.1"} {
		want = append(want, "-A SRF-OPEN-EGRESS -d "+r+"/32 -p udp -m udp --dport 53 -j ACCEPT", "-A SRF-OPEN-EGRESS -d "+r+"/32 -p tcp -m tcp --dport 53 -j ACCEPT")
	}
	for _, n := range []string{"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4"} {
		want = append(want, "-A SRF-OPEN-EGRESS -d "+n+" -j DROP")
	}
	want = append(want, "-A SRF-OPEN-EGRESS -j RETURN", "-A SRF-OPEN-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT", "-A SRF-OPEN-INPUT -j DROP", "-I DOCKER-USER 1 -s 172.29.0.0/16 -j SRF-OPEN-EGRESS", "-I INPUT 1 -i srfo+ -j SRF-OPEN-INPUT")
	if !slices.Equal(rules, want) {
		t.Fatal(rules)
	}
	sh, e := os.ReadFile(filepath.Join(deploy, "apply.sh"))
	if e != nil {
		t.Fatal(e)
	}
	for _, bad := range []string{"-F DOCKER-USER", "-F INPUT", "-F FORWARD", "-F\n", "--flush", "exit 1", "die 1"} {
		if strings.Contains(string(sh), bad) {
			t.Fatal("apply.sh", bad)
		}
	}
	// The only flush is of its own chains.
	for _, line := range strings.Split(string(sh), "\n") {
		if strings.Contains(line, " -F") && !strings.Contains(line, `iptables -w -F "$chain"`) || strings.Contains(line, "for chain in") && !strings.HasSuffix(line, "for chain in SRF-OPEN-EGRESS SRF-OPEN-INPUT; do") {
			t.Fatal("apply.sh flushes a foreign chain:", line)
		}
	}
	for _, need := range []string{"set -eu", "--noflush", "/run/srf-open-egress", "remove", `[ "$(id -u)" = 0 ]`} {
		if !strings.Contains(string(sh), need) {
			t.Fatal("apply.sh missing", need)
		}
	}
	if fi, e := os.Stat(filepath.Join(deploy, "apply.sh")); e != nil || fi.Mode().Perm()&0100 == 0 {
		t.Fatal("apply.sh not executable", e)
	}
}
