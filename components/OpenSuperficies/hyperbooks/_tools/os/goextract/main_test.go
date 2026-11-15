package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const src = `package demo

import "fmt"

// Limit caps things.
const Limit = 16

// Box holds a value.
type Box struct{ v int }

// Get returns the value.
func (b *Box) Get() int {
	// inner comment
	return b.v + len("lit")
}

func helper() string { return fmt.Sprint("x") }

func init() {}
func init() {}
`

func write(t *testing.T, dir, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(dir, "pkg"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "pkg", "demo.go"), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func extract(t *testing.T, dir string) *Bundle {
	t.Helper()
	b, err := Extract(dir, []string{"pkg"}, "HB-TEST-001")
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func anchor(b *Bundle, id string) *Anchor {
	for i := range b.Anchors {
		if b.Anchors[i].ID == id {
			return &b.Anchors[i]
		}
	}
	return nil
}

func TestIDsAndShape(t *testing.T) {
	d := t.TempDir()
	write(t, d, src)
	b := extract(t, d)
	for _, id := range []string{"pkg/demo.go::__module__", "pkg/demo.go::Limit", "pkg/demo.go::Box", "pkg/demo.go::Box.Get", "pkg/demo.go::helper", "pkg/demo.go::init", "pkg/demo.go::init_2"} {
		if anchor(b, id) == nil {
			t.Fatalf("missing symbol %s", id)
		}
	}
	if b.Schema != "hb.symbols.bundle/v1" || b.LangDefault != "go" {
		t.Fatalf("schema/lang %q %q", b.Schema, b.LangDefault)
	}
	g := anchor(b, "pkg/demo.go::Box.Get")
	if g.Lang != "go" || g.Span.StartLine != 12 || g.Span.EndLine != 15 {
		t.Fatalf("Get anchor %+v", g)
	}
	for _, s := range b.Symbols {
		if s.ID == "pkg/demo.go::Box.Get" && (s.Receiver != "Box" || !s.Exported || s.IsTest) {
			t.Fatalf("Get symbol %+v", s)
		}
	}
	if tx := b.Texts["pkg/demo.go::Box.Get"]; tx.Doc != "Get returns the value." || tx.Com != "inner comment" || tx.Lit != "lit" {
		t.Fatalf("texts %+v", tx)
	}
}

func TestDeterministic(t *testing.T) {
	d := t.TempDir()
	write(t, d, src)
	a, _ := Canon(extract(t, d))
	c, _ := Canon(extract(t, d))
	if !bytes.Equal(a, c) || len(a) < 200 {
		t.Fatal("two runs differ or empty")
	}
}

func TestShaASTSemantics(t *testing.T) {
	d := t.TempDir()
	write(t, d, src)
	base := anchor(extract(t, d), "pkg/demo.go::Box.Get")
	// comment-only edit: raw sha changes, sha_ast does not
	write(t, d, strings.Replace(src, "// inner comment", "// other comment", 1))
	c := anchor(extract(t, d), "pkg/demo.go::Box.Get")
	if c.Sha256 == base.Sha256 || c.Sha256AST != base.Sha256AST {
		t.Fatalf("comment edit: raw %v ast-equal %v", c.Sha256 != base.Sha256, c.Sha256AST == base.Sha256AST)
	}
	// body edit: both change
	write(t, d, strings.Replace(src, "b.v + len", "b.v - len", 1))
	e := anchor(extract(t, d), "pkg/demo.go::Box.Get")
	if e.Sha256AST == base.Sha256AST || e.Sha256 == base.Sha256 {
		t.Fatal("body edit not detected")
	}
	// moved line (blank line prepended inside the file): span moves, sha_ast equal
	write(t, d, strings.Replace(src, "// Box holds a value.", "\n// Box holds a value.", 1))
	m := anchor(extract(t, d), "pkg/demo.go::Box.Get")
	if m.Span.StartLine != base.Span.StartLine+1 || m.Sha256AST != base.Sha256AST {
		t.Fatalf("moved: %+v", m.Span)
	}
}

func TestUsage(t *testing.T) {
	null, _ := os.OpenFile(os.DevNull, os.O_WRONLY, 0)
	defer null.Close()
	if rc := run([]string{}, null, null); rc != 2 {
		t.Fatalf("usage rc %d", rc)
	}
	if rc := run([]string{"--dirs", "nope-missing"}, null, null); rc != 5 {
		t.Fatalf("missing dir rc %d", rc)
	}
}

const inlineSrc = `package demo

// Snap keeps a budget.
type Snap struct {
	a int // freeze budget, 50 ms
	// doc on b
	b int
}

const (
	K1 = 1 // one
	K2 = 2
)
`

// ADC-W5A-02(a): inline comments in a TypeSpec/ValueSpec do not move sig or sha256_ast; a real edit still does.
func TestSpecCommentsDoNotMoveShaAST(t *testing.T) {
	d := t.TempDir()
	write(t, d, inlineSrc)
	base := extract(t, d)
	s0, k0 := anchor(base, "pkg/demo.go::Snap"), anchor(base, "pkg/demo.go::K1")
	write(t, d, strings.NewReplacer("50 ms", "60 ms", "// doc on b", "// other", "// one", "// uno").Replace(inlineSrc))
	ed := extract(t, d)
	s1, k1 := anchor(ed, "pkg/demo.go::Snap"), anchor(ed, "pkg/demo.go::K1")
	if s1.Sha256AST != s0.Sha256AST || k1.Sha256AST != k0.Sha256AST {
		t.Fatalf("comment edit moved sha_ast: snap %v const %v", s1.Sha256AST != s0.Sha256AST, k1.Sha256AST != k0.Sha256AST)
	}
	for _, sy := range ed.Symbols {
		if sy.ID == "pkg/demo.go::Snap" && strings.Contains(sy.Signature, "//") {
			t.Fatalf("signature keeps a comment: %q", sy.Signature)
		}
	}
	write(t, d, strings.Replace(inlineSrc, "b int", "b int64", 1))
	if anchor(extract(t, d), "pkg/demo.go::Snap").Sha256AST == s0.Sha256AST {
		t.Fatal("type edit not detected")
	}
}
