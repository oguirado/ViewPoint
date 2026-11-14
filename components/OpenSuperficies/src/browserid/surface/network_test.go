package surface

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type networkRunner struct {
	calls          [][]string
	network, proxy bool
	attached       string // docker ps --filter network= output
	badNetwork     bool
	label          string
}

func (r *networkRunner) Run(_ context.Context, a []string, _ []byte) ([]byte, error) {
	r.calls = append(r.calls, append([]string(nil), a...))
	s := strings.Join(a, " ")
	switch {
	case strings.Contains(s, "--filter network="):
		return []byte(r.attached), nil
	case strings.Contains(s, "logs"):
		return []byte(`{"status":"EGRESS_PROXY_READY"}`), nil
	case strings.Contains(s, "network ls"):
		if r.network {
			return []byte("srf-idnet"), nil
		}
		return nil, nil
	case strings.Contains(s, "network inspect"):
		if !r.network {
			return nil, errors.New("absent")
		}
		if r.badNetwork {
			return []byte(`{"Name":"srf-idnet","Internal":false,"IPAM":{"Config":[{"Subnet":"172.30.80.0/24"}]}}`), nil
		}
		return []byte(`{"Name":"srf-idnet","Internal":true,"IPAM":{"Config":[{"Subnet":"172.30.80.0/24"}]}}`), nil
	case strings.Contains(s, "network create"):
		r.network = true
	case strings.Contains(s, "network rm"):
		r.network = false
	case strings.Contains(s, "ps -a"):
		if r.proxy {
			return []byte("srf-egress-proxy\n"), nil
		}
	case strings.Contains(s, "Config.Labels"):
		return []byte(r.label), nil
	case strings.Contains(s, "NetworkSettings.Networks"):
		return []byte("2 172.30.80.2 bridge 0"), nil
	case strings.Contains(s, "inspect"):
		if !r.proxy {
			return nil, errors.New("absent")
		}
		return []byte("true\n"), nil
	case strings.Contains(s, "create --name"):
		r.proxy = true
		for _, v := range a {
			if strings.HasPrefix(v, "srf.identity.proxy.recipe=") {
				r.label = strings.TrimPrefix(v, "srf.identity.proxy.recipe=")
			}
		}
	case strings.Contains(s, "rm -f"):
		r.proxy = false
	}
	return nil, nil
}
func TestNetworkLifecycle(t *testing.T) {
	root := t.TempDir()
	_ = os.Chmod(root, 0700)
	bin := filepath.Join(root, "proxy")
	_ = os.WriteFile(bin, []byte("fake executable"), 0700)
	policy := filepath.Join(root, "policy.json")
	_ = os.WriteFile(policy, []byte(`{}`), 0600)
	r := &networkRunner{}
	tool := Tool{R: r, Root: filepath.Join(root, "state"), Registry: filepath.Join(root, "registry.json")}
	cfg := NetSpec{ProxyBin: bin, Policy: policy, AuditDir: filepath.Join(root, "audit")}
	if e := tool.NetUp(context.Background(), cfg); e != nil {
		t.Fatal(e)
	}
	joined := ""
	for _, a := range r.calls {
		joined += strings.Join(a, " ") + "\n"
	}
	for _, want := range []string{"network create --internal --subnet 172.30.80.0/24 srf-idnet", "--network bridge", "network connect --ip 172.30.80.2 srf-idnet srf-egress-proxy", "--cap-drop ALL", "--read-only", "--user " + proxyUser(), "--memory 256m", "--entrypoint /srf-egress-proxy", "--listen 172.30.80.2:3128", "dst=/registry,readonly", "dst=/policy,readonly", "dst=/audit"} {
		if !strings.Contains(joined, want) {
			t.Fatal(want, joined)
		}
	}
	before := len(r.calls)
	if e := tool.NetUp(context.Background(), cfg); e != nil {
		t.Fatal(e)
	}
	for _, a := range r.calls[before:] {
		if strings.Contains(strings.Join(a, " "), " create ") {
			t.Fatal("non-idempotent", a)
		}
	}
	if e := tool.NetDown(context.Background()); e != nil {
		t.Fatal(e)
	}
	if e := tool.NetDown(context.Background()); e != nil {
		t.Fatal("idempotent down", e)
	}
	// A capsule still on srf-idnet (even exited) or a held address refuses down before any removal.
	if e := tool.NetUp(context.Background(), cfg); e != nil {
		t.Fatal(e)
	}
	r.attached = "srf-egress-proxy\nsrf-id-cuentas-1\n"
	before = len(r.calls)
	if Code(tool.NetDown(context.Background())) != 4 || !r.proxy || !r.network {
		t.Fatal("down with an attached capsule must refuse and keep the proxy")
	}
	for _, a := range r.calls[before:] {
		if strings.Contains(strings.Join(a, " "), " rm ") {
			t.Fatal("removal before refusal", a)
		}
	}
	r.attached = "srf-egress-proxy\n"
	if e := tool.registry("srf-id-held-1", "172.30.80.10", false); e != nil {
		t.Fatal(e)
	}
	if Code(tool.NetDown(context.Background())) != 4 || !r.proxy {
		t.Fatal("down with a held address must refuse and keep the proxy")
	}
	if e := tool.registry("srf-id-held-1", "", true); e != nil {
		t.Fatal(e)
	}
	if e := tool.NetDown(context.Background()); e != nil {
		t.Fatal("down after stop", e)
	}
	r.network = true
	r.badNetwork = true
	if Code(tool.NetUp(context.Background(), cfg)) != 3 {
		t.Fatal("accepted non-internal network")
	}
}
func TestDirectLauncherAndIP(t *testing.T) {
	tool, r, _, _ := setup(t)
	tool.Launcher = DockerLauncher{R: r}
	r.proxy = false
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Windows: 1}); Code(e) != 3 {
		t.Fatal("missing proxy", e)
	}
	r.proxy = true
	c := start(t, tool)
	joined := ""
	for _, a := range r.calls {
		joined += strings.Join(a, " ") + "\n"
	}
	for _, want := range []string{"docker run -d", "--ip 172.30.80.10", "SRF_EGRESS_PROXY=http://172.30.80.2:3128"} {
		if !strings.Contains(joined, want) {
			t.Fatal(want, joined)
		}
	}
	if e := tool.Stop(context.Background(), c.Container); e != nil {
		t.Fatal(e)
	}
	b, e := os.ReadFile(tool.Registry)
	if e != nil || string(b) != "{}" {
		t.Fatal(string(b), e)
	}
	ip, e := tool.reserveIP("srf-id-new")
	if e != nil || ip != "172.30.80.10" {
		t.Fatal(ip, e)
	}
	ip, e = tool.reserveIP("srf-id-next")
	if e != nil || ip != "172.30.80.11" {
		t.Fatal(ip, e)
	}
}

func TestAddressPoolExhaustionAndCorruption(t *testing.T) {
	tool, _, _, _ := setup(t)
	m := map[string]string{}
	for n := 10; n <= 250; n++ {
		m[fmt.Sprintf("srf-id-p%d", n)] = fmt.Sprintf("172.30.80.%d", n)
	}
	if e := atomicJSON(tool.Registry, m); e != nil {
		t.Fatal(e)
	}
	if _, e := tool.reserveIP("srf-id-full"); Code(e) != 4 {
		t.Fatal("exhaustion", e)
	}
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Capsule: "srf-id-full", Windows: 1}); Code(e) != 4 {
		t.Fatal(e)
	}
	// A pre-launch allocation failure must release the identity and leave no session.
	lease, e := tool.Store.Acquire("identity-a", "other")
	if e != nil {
		t.Fatal("lease leaked", e)
	}
	_ = tool.Store.Release(lease)
	if _, e = os.Lstat(tool.statePath("srf-id-full")); !os.IsNotExist(e) {
		t.Fatal("reserved unusable session", e)
	}
	m = map[string]string{"srf-id-a": "172.30.80.2"}
	if e = atomicJSON(tool.Registry, m); e != nil {
		t.Fatal(e)
	}
	if _, e = tool.reserveIP("srf-id-new"); Code(e) != 6 {
		t.Fatal("accepted proxy IP as capsule", e)
	}
}

func TestProfileMetadataRefusalDoesNotLaunch(t *testing.T) {
	tool, r, l, _ := setup(t)
	tool.ProfileCheck = nil
	p := filepath.Join(tool.StoreRoot, "profiles", "identity-a")
	if e := os.MkdirAll(p, 0755); e != nil {
		t.Fatal(e)
	}
	if e := os.Chmod(p, 0755); e != nil {
		t.Fatal(e)
	}
	if _, e := tool.Start(context.Background(), StartSpec{Identity: "identity-a", Windows: 1}); Code(e) != 4 {
		t.Fatal(e)
	}
	if len(l.plans) != 0 {
		t.Fatal("unsafe profile launched")
	}
	for _, a := range r.calls {
		if strings.Contains(strings.Join(a, " "), "docker run") {
			t.Fatal(a)
		}
	}
}
