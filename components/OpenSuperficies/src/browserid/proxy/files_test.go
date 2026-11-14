package proxy

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPolicyAndRegistryFiles(t *testing.T) {
	d := t.TempDir()
	p := filepath.Join(d, "policy.json")
	doc := `{"schema":"browserid.proxy-policy/v1","approved":{"cap":["ok.example","127.0.0.1"]},"denied":["no.example"],"connect_ports":{"cap":{"ok.example":[8443]}}}`
	if e := os.WriteFile(p, []byte(doc), 0600); e != nil {
		t.Fatal(e)
	}
	pol := FilePolicy{p}
	if pol.Validate() != nil || pol.Decide("cap", "ok.example") != Approved || pol.Decide("cap", "no.example") != Denied || pol.Decide("other", "ok.example") != Pending || !pol.AllowPort("cap", "ok.example", 8443) || pol.AllowPort("cap", "ok.example", 80) {
		t.Fatal("policy mismatch")
	}
	os.WriteFile(p, []byte(`{}`), 0600)
	if pol.Decide("cap", "ok.example") != Denied {
		t.Fatal("malformed reload allowed")
	}
	r := filepath.Join(d, "registry.json")
	os.WriteFile(r, []byte(`{}`), 0600)
	if _, e := ReadRegistry(r); e != nil {
		t.Fatal("empty registry refused", e)
	}
	os.WriteFile(r, []byte(`{"cap":"192.0.2.2"}`), 0600)
	if _, e := ReadRegistry(r); e != nil {
		t.Fatal(e)
	}
	os.WriteFile(r, []byte(`{"cap":"192.0.2.2","other":"192.0.2.2"}`), 0600)
	if _, e := ReadRegistry(r); e == nil {
		t.Fatal("duplicate IP")
	}
	os.WriteFile(r, []byte(`{} {}`), 0600)
	if _, e := ReadRegistry(r); e == nil {
		t.Fatal("trailing JSON")
	}
	link := filepath.Join(d, "link")
	os.Symlink(p, link)
	if (FilePolicy{link}).Validate() == nil {
		t.Fatal("symlink followed")
	}
}
func TestAuditAppendPermissions(t *testing.T) {
	p := filepath.Join(t.TempDir(), "audit.jsonl")
	f, e := OpenAudit(p)
	if e != nil {
		t.Fatal(e)
	}
	f.WriteString("first\n")
	f.Close()
	f, e = OpenAudit(p)
	if e != nil {
		t.Fatal(e)
	}
	f.WriteString("second\n")
	f.Close()
	b, _ := os.ReadFile(p)
	if string(b) != "first\nsecond\n" {
		t.Fatal("not append-only")
	}
	os.Chmod(p, 0644)
	if f, e := OpenAudit(p); e == nil {
		f.Close()
		t.Fatal("readable audit accepted")
	}
}

func TestFilePrivateTargetsExactAndFailClosed(t *testing.T) {
	p := filepath.Join(t.TempDir(), "policy.json")
	for _, doc := range []struct {
		json          string
		valid, permit bool
	}{
		{`{"schema":"browserid.proxy-policy/v1","approved":{"cap":["private.example"]},"denied":[]}`, true, false},
		{`{"schema":"browserid.proxy-policy/v1","approved":{"cap":["private.example"]},"denied":[],"private_targets":{"cap":["private.example"]}}`, true, true},
		{`{"schema":"browserid.proxy-policy/v1","approved":{},"denied":[],"private_targets":{"cap":["PRIVATE.example"]}}`, false, false},
		{`{"schema":"browserid.proxy-policy/v1","approved":{},"denied":[],"private_targets":{"bad name":["private.example"]}}`, false, false},
		{`{}`, false, false},
	} {
		if e := os.WriteFile(p, []byte(doc.json), 0600); e != nil {
			t.Fatal(e)
		}
		pol := FilePolicy{p}
		if (pol.Validate() == nil) != doc.valid || pol.AllowPrivate("cap", "private.example") != doc.permit || pol.AllowPrivate("other", "private.example") || pol.AllowPrivate("cap", "sub.private.example") {
			t.Fatal("private file exception failed")
		}
	}
}
