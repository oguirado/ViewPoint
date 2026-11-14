package proto

import (
	"go/ast"
	"go/parser"
	"go/token"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"path/filepath"
	"strconv"
	"testing"
)

func TestGeneratedHelpersAllowed(t *testing.T) {
	files, _ := filepath.Glob("../../browserid/surface/*.go")
	sources := map[string]string{}
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
				for _, entry := range helpers.Entries {
					if entry.Name == name.Name && i < len(v.Values) {
						s, e := strconv.Unquote(v.Values[i].(*ast.BasicLit).Value)
						if e != nil {
							t.Fatal(e)
						}
						sources[name.Name] = s
					}
				}
			}
			return true
		})
	}
	for _, e := range helpers.Entries {
		a := []string{"python3", "-I"}
		if e.Stream {
			a = append(a, "-u")
		}
		a = append(a, "-c", sources[e.Name])
		if e.Name == "downloadsPython" {
			a = append(a, "/profile/exchange")
		}
		o := Open{Argv: a, StdinCap: e.StdinCap}
		timeout, err := Allowed(o, helpers.Hashes())
		if err != nil || timeout != e.TimeoutMS {
			t.Errorf("%s: %d %v", e.Name, timeout, err)
		}

		cap, capErr := StdinLimit(o, helpers.Hashes())
		if capErr != nil || cap != e.StdinCap {
			t.Errorf("%s matched stdin: %d %v", e.Name, cap, capErr)
		}
		if e.StdinCap > 0 {
			narrow := o
			narrow.StdinCap = 1
			if cap, err := StdinLimit(narrow, helpers.Hashes()); err != nil || cap != 1 {
				t.Errorf("%s narrowed stdin: %d %v", e.Name, cap, err)
			}
		}
		if e.Name == "downloadsPython" {
			for _, tail := range [][]string{{"file.txt", "delete"}, {"file.txt", "15728640"}} {
				o.Argv = append(append([]string{}, a...), tail...)
				if _, err := Allowed(o, helpers.Hashes()); err != nil {
					t.Fatal(err)
				}
			}
		}
		if e.StdinCap >= 0 {
			o.StdinCap = e.StdinCap + 1
			_, err = Allowed(o, helpers.Hashes())
			code(t, err, 2)
		}
		o.StdinCap = e.StdinCap
		o.TimeoutMS = e.TimeoutMS + 1
		_, err = Allowed(o, helpers.Hashes())
		code(t, err, 2)
		o.TimeoutMS = 0
		o.Argv = append(append([]string{}, a...), "unexpected")
		_, err = Allowed(o, helpers.Hashes())
		code(t, err, 2)
	}
	for _, k := range []string{"exec", "windowkill", "behave", "key", "getmouselocation"} {
		_, e := Allowed(Open{Argv: []string{"xdotool", "key", k}}, nil)
		code(t, e, 2)
	}
}
