package helpers

import (
	"crypto/sha256"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"strconv"
	"testing"
)

func TestSurfaceHashesCurrent(t *testing.T) {
	found := map[string]string{}
	files, e := filepath.Glob("../../browserid/surface/*.go")
	if e != nil {
		t.Fatal(e)
	}
	for _, p := range files {
		f, e := parser.ParseFile(token.NewFileSet(), p, nil, 0)
		if e != nil {
			t.Fatal(e)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			v, ok := n.(*ast.ValueSpec)
			if !ok {
				return true
			}
			for i, name := range v.Names {
				for _, entry := range Entries {
					if name.Name == entry.Name && i < len(v.Values) {
						lit, ok := v.Values[i].(*ast.BasicLit)
						if !ok {
							t.Fatal("helper no longer literal")
						}
						s, e := strconv.Unquote(lit.Value)
						if e != nil {
							t.Fatal(e)
						}
						found[name.Name] = fmt.Sprintf("%x", sha256.Sum256([]byte(s)))
					}
				}
			}
			return true
		})
	}
	for _, e := range Entries {
		if found[e.Name] != e.SHA256 {
			t.Errorf("%s stale; run go generate", e.Name)
		}
	}
	if len(found) != len(Entries) {
		t.Fatal("helper coverage")
	}
}

func TestFullHelperPolicy(t *testing.T) {
	want := [...]Entry{
		{"pastePython", "74fd82e120e49baae93a35d0b6132d0388d8732df5eaca78028a7337b95db456", 10000, 16384, false},
		{"axPython", "787719608997663805358935c26ce03a6c53b98ba77d703c9d19bdcd9dd55b4e", 3000, 32769, false},
		{"downloadsPython", "14efbda583462aac99af82fe0c33c25dcf05836c6b2cdaf874961affe87858c6", 18000, 0, false},
		{"recordPython", "712d6ed3e3bcaae3831d872c0690652f3daf5c08d634d12d11bd5db03fdfa83c", 0, -1, true},
	}
	if Entries != want {
		t.Errorf("helper policy drift: got %#v want %#v", Entries, want)
	}
}
