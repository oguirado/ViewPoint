package uidmapcapture

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

func id(n uint32) *uint32 { return &n }
func validFixture() (Capture, Policy) {
	c := Capture{
		Owner: "seat-owner-44", ExpectedOwner: "seat-owner-44",
		ProcessStart: "pidstart-11", ExpectedProcessStart: "pidstart-11",
		UIDMap: "0 30001 1\n1 200000 65536\n",
		GIDMap: "0 40001 1\n1 300000 65536\n",
		Passwd: osworkerNologin,
		Group:  "osworker:x:40001:\nvideo:x:44:osworker\n",
		SubUID: "osworker:200000:65536\n",
		SubGID: "osworker:300000:65536\n",
	}
	p := Policy{
		ExpectedAccount: "osworker", ApprovedUIDRanges: []HostRange{{30001, 1}, {200000, 65536}},
		ApprovedGIDRanges: []HostRange{{40001, 1}, {300000, 65536}},
		ContainerUID:      id(0), ContainerGID: id(0),
		SupplementalGroupsSupplied: true, ObservedSupplementalGIDs: []uint32{44},
		AllowedSupplementalGIDs: []uint32{44}, DeviceGroupMembershipSupplied: true,
		ObservedDeviceGroupGIDs: []uint32{44}, ApprovedDeviceGroupGIDs: []uint32{44},
	}
	return c, p
}
func TestValidMultiEntryAndDeterministicEvidence(t *testing.T) {
	c, p := validFixture()
	got := Evaluate(c, p)
	if got.Decision != "PASS" || len(got.Missing) != 0 || len(got.Failures) != 0 {
		t.Fatalf("valid fixture: %+v", got)
	}
	if len(got.UIDMap) != 2 || len(got.GIDMap) != 2 {
		t.Fatalf("lost multi-entry maps: %+v", got)
	}
	if got.HostUID == nil || *got.HostUID != 30001 || got.HostGID == nil || *got.HostGID != 40001 {
		t.Fatalf("wrong translated identities: %+v", got)
	}
	if got.RawSHA256.UIDMap != hashText(c.UIDMap) || got.RawSHA256.GIDMap != hashText(c.GIDMap) {
		t.Fatal("bad raw digest")
	}
	a, _ := json.Marshal(got)
	b, _ := json.Marshal(Evaluate(c, p))
	if string(a) != string(b) {
		t.Fatal("non-deterministic JSON")
	}
	if got.RejectUnknown() != nil {
		t.Fatal("PASS rejected")
	}
	if host, ok := Translate(got.UIDMap, 2); !ok || host != 200001 {
		t.Fatal("second observed range not translated")
	}
}
func TestMustFailFixtures(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*Capture, *Policy)
		reason string
	}{
		{"different process start", func(c *Capture, p *Policy) { c.ProcessStart = "another-start" }, "process_start_mismatch"},
		{"different capture owner", func(c *Capture, p *Policy) { c.Owner = "another-owner" }, "capture_owner_mismatch"},
		{"missing gid_map", func(c *Capture, p *Policy) { c.GIDMap = "" }, "gid_map"},
		{"root execution forbidden", func(c *Capture, p *Policy) { p.ForbidContainerRoot = true }, "container_root_forbidden"},
		{"inside overlap", func(c *Capture, p *Policy) { c.UIDMap = "0 30001 2\n1 200000 4\n" }, "uid_map_invalid"},
		{"outside overlap", func(c *Capture, p *Policy) { c.UIDMap = "0 30001 1\n1 30001 1\n" }, "uid_map_invalid"},
		{"overflow", func(c *Capture, p *Policy) { c.UIDMap = "4294967295 20 2\n" }, "uid_map_invalid"},
		{"unexpected group", func(c *Capture, p *Policy) { p.ObservedSupplementalGIDs = []uint32{55} }, "unexpected_supplemental_group"},
		{"missing subuid range", func(c *Capture, p *Policy) { c.SubUID = "osworker:300000:65536\n" }, "uid_subid_range_missing"},
		{"missing subgid range", func(c *Capture, p *Policy) { c.SubGID = "another:300000:65536\n" }, "gid_subid_range_missing"},
		{"different coverage", func(c *Capture, p *Policy) { c.GIDMap = "0 40001 1\n1 300000 65535\n" }, "uid_gid_container_coverage_mismatch"},
		{"unknown container ID", func(c *Capture, p *Policy) { p.ContainerUID = id(80000) }, "uid_unmapped"},
		{"login account", func(c *Capture, p *Policy) { c.Passwd = osworkerLoginShell }, "dedicated_account_has_login_shell"},
		{"missing group authorization", func(c *Capture, p *Policy) { p.SupplementalGroupsSupplied = false }, "supplemental_group_evidence"},
		{"missing device evidence", func(c *Capture, p *Policy) { p.DeviceGroupMembershipSupplied = false }, "device_group_evidence"},
		{"unexpected device membership", func(c *Capture, p *Policy) { p.ObservedDeviceGroupGIDs = []uint32{55} }, "unexpected_device_group"},
		{"missing approved ranges", func(c *Capture, p *Policy) { p.ApprovedUIDRanges = nil }, "approved_uid_ranges"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c, p := validFixture()
			tt.mutate(&c, &p)
			got := Evaluate(c, p)
			if got.Decision != "HOLD" || got.RejectUnknown() == nil {
				t.Fatalf("not held: %+v", got)
			}
			found := false
			for _, s := range append(append([]string{}, got.Failures...), got.Missing...) {
				if s == tt.reason {
					found = true
				}
			}
			if !found {
				t.Fatalf("reason %s not found: %+v", tt.reason, got)
			}
		})
	}
}
func TestParsersBoundaryAndSecrets(t *testing.T) {
	cases := []struct {
		raw    string
		wantOK bool
	}{
		{"0 4294967295 1\n", true},
		{"4294967295 0 1\n", true},
		{"0 1 0\n", false},
		{"1 1 -2\n", false},
		{"1 +1 1\n", false},
		{"4294967295 0 2\n", false},
		{"0 4294967295 2\n", false},
		{"bad\n", false},
		{strings.Repeat("1 2 1\n", MaxMapEntries+1), false},
		{strings.Repeat("x", MaxLineBytes+1), false},
		{"1 2 1\x00", false},
	}
	for _, tc := range cases {
		_, err := ParseMap(tc.raw)
		if (err == nil) != tc.wantOK {
			t.Errorf("ParseMap(%q)=%v, want ok %v", tc.raw[:min(len(tc.raw), 30)], err, tc.wantOK)
		}
	}
	if _, err := ParseAccounts("worker:actualhash:1:2:x:/home:/bin/false\n"); err == nil {
		t.Fatal("real password field accepted")
	}
	if _, err := ParseGroups("video:actualhash:44:worker\n"); err == nil {
		t.Fatal("real group password accepted")
	}
	if _, err := ParseSubIDs("worker:4294967295:2\n"); err == nil {
		t.Fatal("subid overflow accepted")
	}
	if _, err := ParseSubIDs("worker:10:5\nworker:14:1\n"); err == nil {
		t.Fatal("overlapping subid accepted")
	}
	if _, err := ParseMap(""); err != ErrAbsent {
		t.Fatal("missing map not marked AUSENTE")
	}
	if got, ok := Translate([]IDRange{{0, 5, 1}}, math.MaxUint32); ok || got != 0 {
		t.Fatal("unobserved ID translated")
	}
}
func TestUnknownCannotBeConfusedWithHostRoot(t *testing.T) {
	c, p := validFixture()
	p.ContainerUID = nil
	p.ContainerGID = nil
	got := Evaluate(c, p)
	if got.Decision != "HOLD" || got.HostUID != nil || got.HostGID != nil {
		t.Fatalf("unknown identities must stay nil: %+v", got)
	}
	c, p = validFixture()
	c.UIDMap = "0 0 1\n1 200000 65536\n"
	got = Evaluate(c, p)
	if got.Decision != "HOLD" {
		t.Fatalf("host root allowed: %+v", got)
	}
}

// Synthetic /etc/passwd entries (fixtures, not credentials).
const (
	osworkerNologin    = "osworker:x:30001:40001:worker:/home/osworker:/usr/sbin/nologin\n"
	osworkerLoginShell = "osworker:x:30001:40001:worker:/home/osworker:/bin/bash\n"
)
