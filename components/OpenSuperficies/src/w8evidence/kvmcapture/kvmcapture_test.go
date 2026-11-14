package kvmcapture

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

const sampleTime = int64(1760000000)

func fixture() (Policy, Captures, time.Time) {
	a := strings.Repeat("a", 64)
	b := strings.Repeat("b", 64)
	rootDigest := strings.Repeat("c", 64)
	d := strings.Repeat("d", 64)
	rootOwner := uint64(0)
	p := Policy{Device: "/dev/kvm", User: "osvm", Group: "kvm", UID: 10001, GID: 993, PID: 700, OwnerUID: &rootOwner, Major: 10, Minor: 232, VMMSHA256: a, KernelSHA256: b, RootFSSHA256: rootDigest, ProbeSHA256: d, Scope: "lease-xyz", Acquisition: "capture-1", Provenance: "operator-approved", ValidFrom: sampleTime - 100, ValidUntil: sampleTime + 300, MaxAgeSeconds: 60}
	prefix := "kind=%s\nscope=lease-xyz\nacquisition=capture-1\nprovenance=operator-approved\ncaptured_at=1760000000\n"
	captures := Captures{
		Node:     sprintf(prefix, "node") + "device=/dev/kvm\ntype=char\nowner_uid=0\nowner_gid=993\nmode=0660\nmajor=10\nminor=232\ndevice_count=1\n",
		Account:  sprintf(prefix, "account") + "user=osvm\nuid=10001\ngroup=kvm\ngid=993\ndedicated=true\n",
		Process:  sprintf(prefix, "process") + "pid=700\nuser=osvm\nuid=10001\ngroup=kvm\ngid=993\nmapped=true\nprivileged=false\nvmm_sha256=" + a + "\nkernel_sha256=" + b + "\nrootfs_sha256=" + rootDigest + "\n",
		Behavior: sprintf(prefix, "behavior") + "pid=700\nuid=10001\ngid=993\ndevice=/dev/kvm\nvmm_sha256=" + a + "\nkernel_sha256=" + b + "\nrootfs_sha256=" + rootDigest + "\nprobe_sha256=" + d + "\nprobe_qualified=true\nprivileged=false\nemulation=false\ndevice_count=1\nopen=ok\nioctl=ok\ncold_boot=ok\n",
	}
	return p, captures, time.Unix(sampleTime+3, 0)
}
func sprintf(format, value string) string { return strings.Replace(format, "%s", value, 1) }
func TestMatchingLifecycleQualifiesOnlyCapturedScope(t *testing.T) {
	p, c, now := fixture()
	r, err := Assess(p, c, now)
	if err != nil || r.Decision != "QUALIFIED" || r.Inventory != "PASS" || r.Behavior != "PASS" || r.Reason != "CAPTURED_SCOPE_ONLY" || len(r.SourceSHA256) != 64 {
		t.Fatalf("positive fixture: %+v %v", r, err)
	}
	output, err := r.JSON()
	if err != nil {
		t.Fatal(err)
	}
	var decoded Record
	if err := json.Unmarshal(output, &decoded); err != nil || decoded != r {
		t.Fatalf("roundtrip: %+v %v", decoded, err)
	}
	same, _ := Assess(p, c, now)
	output2, _ := same.JSON()
	if string(output) != string(output2) {
		t.Fatalf("non-deterministic evidence")
	}
	// A passed fixture cannot be reused for a different policy scope.
	p.Scope = "different-lease"
	other, err := Assess(p, c, now)
	if err == nil || other.Decision == "QUALIFIED" {
		t.Fatalf("cross-scope succeeded: %+v %v", other, err)
	}
}
func TestIndependentNegativeMutations(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(*Policy, *Captures, *time.Time)
		want   string
	}{
		{"node present without proof", func(p *Policy, c *Captures, n *time.Time) { c.Behavior = "" }, "AUSENTE"},
		{"mismatched owner", func(p *Policy, c *Captures, n *time.Time) {
			c.Node = strings.Replace(c.Node, "owner_uid=0", "owner_uid=1", 1)
		}, "HOLD"},
		{"mismatched account", func(p *Policy, c *Captures, n *time.Time) {
			c.Account = strings.Replace(c.Account, "dedicated=true", "dedicated=false", 1)
		}, "HOLD"},
		{"mismatched process uid", func(p *Policy, c *Captures, n *time.Time) {
			c.Process = strings.Replace(c.Process, "uid=10001", "uid=0", 1)
		}, "HOLD"},
		{"VMM digest mismatch", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "vmm_sha256="+p.VMMSHA256, "vmm_sha256="+strings.Repeat("e", 64), 1)
		}, "HOLD"},
		{"missing ioctl", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "ioctl=ok\n", "", 1)
		}, "AUSENTE"},
		{"ioctl failure", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "ioctl=ok", "ioctl=fail", 1)
		}, "HOLD"},
		{"open failure", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "open=ok", "open=fail", 1)
		}, "HOLD"},
		{"cold-boot failure", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "cold_boot=ok", "cold_boot=fail", 1)
		}, "HOLD"},
		{"root policy", func(p *Policy, c *Captures, n *time.Time) { p.UID = 0 }, "HOLD"},
		{"root capture", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "uid=10001", "uid=0", 1)
		}, "HOLD"},
		{"privileged mode", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "privileged=false", "privileged=true", 1)
		}, "HOLD"},
		{"software emulation", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "emulation=false", "emulation=true", 1)
		}, "HOLD"},
		{"administrator only", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "probe_qualified=true", "probe_qualified=false", 1)
		}, "HOLD"},
		{"extra device", func(p *Policy, c *Captures, n *time.Time) {
			c.Node = strings.Replace(c.Node, "device_count=1", "device_count=2", 1)
		}, "HOLD"},
		{"extra behavior device", func(p *Policy, c *Captures, n *time.Time) {
			c.Behavior = strings.Replace(c.Behavior, "device_count=1", "device_count=2", 1)
		}, "HOLD"},
		{"world writable", func(p *Policy, c *Captures, n *time.Time) {
			c.Node = strings.Replace(c.Node, "mode=0660", "mode=0666", 1)
		}, "HOLD"},
		{"wrong probe", func(p *Policy, c *Captures, n *time.Time) { p.ProbeSHA256 = strings.Repeat("e", 64) }, "HOLD"},
		{"stale scope", func(p *Policy, c *Captures, n *time.Time) { *n = n.Add(90 * time.Second) }, "HOLD"},
		{"expired scope", func(p *Policy, c *Captures, n *time.Time) { *n = n.Add(400 * time.Second) }, "HOLD"},
		{"missing policy", func(p *Policy, c *Captures, n *time.Time) { p.Provenance = "" }, "AUSENTE"},
		{"missing kernel digest", func(p *Policy, c *Captures, n *time.Time) { p.KernelSHA256 = "" }, "AUSENTE"},
		{"missing owner policy", func(p *Policy, c *Captures, n *time.Time) { p.OwnerUID = nil }, "AUSENTE"},
		{"missing process mapping", func(p *Policy, c *Captures, n *time.Time) {
			c.Process = strings.Replace(c.Process, "mapped=true", "mapped=false", 1)
		}, "HOLD"},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			p, c, now := fixture()
			tt.mutate(&p, &c, &now)
			r, err := Assess(p, c, now)
			if err != nil {
				t.Fatal(err)
			}
			if r.Decision != "HOLD" || r.Inventory == "PASS" && r.Behavior == "PASS" {
				t.Fatalf("mutation escaped gate: %+v", r)
			}
			if tt.want == "AUSENTE" && !strings.Contains(r.Reason, "AUSENTE") {
				t.Fatalf("absence treated as data: %+v", r)
			}
		})
	}
}
func TestRejectedCapturesNeverLeaveQualifiedRecord(t *testing.T) {
	mutations := []struct {
		name   string
		change func(*Captures)
	}{
		{"duplicate field", func(c *Captures) { c.Node += "device=/dev/kvm\n" }},
		{"contradictory kind", func(c *Captures) { c.Node = strings.Replace(c.Node, "kind=node", "kind=behavior", 1) }},
		{"conflicting acquisition", func(c *Captures) {
			c.Behavior = strings.Replace(c.Behavior, "acquisition=capture-1", "acquisition=capture-2", 1)
		}},
		{"conflicting provenance", func(c *Captures) {
			c.Process = strings.Replace(c.Process, "provenance=operator-approved", "provenance=admin", 1)
		}},
		{"conflicting timestamps", func(c *Captures) {
			c.Account = strings.Replace(c.Account, "captured_at=1760000000", "captured_at=1760000001", 1)
		}},
		{"oversized", func(c *Captures) { c.Node = strings.Repeat("a", MaxCaptureBytes+1) }},
		{"unknown field", func(c *Captures) { c.Node += "shell=privileged\n" }},
		{"invalid decimal", func(c *Captures) { c.Process = strings.Replace(c.Process, "pid=700", "pid=0x700", 1) }},
		{"malformed line", func(c *Captures) { c.Node += "not-equals\n" }},
		{"control bytes", func(c *Captures) { c.Node += "\x00" }},
	}
	for _, tt := range mutations {
		t.Run(tt.name, func(t *testing.T) {
			p, c, now := fixture()
			tt.change(&c)
			r, err := Assess(p, c, now)
			if err == nil || r.Decision == "QUALIFIED" || r.Schema != "" {
				t.Fatalf("should reject with zero record: %+v %v", r, err)
			}
		})
	}
}
