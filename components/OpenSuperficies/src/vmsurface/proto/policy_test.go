package proto

import (
	"go/ast"
	"go/parser"
	"go/token"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"strconv"
	"testing"
	"time"
)

// Parse the real builders without importing the surface or executing Docker.
func TestPolicyTimeoutsInsideWrappers(t *testing.T) {
	sources := map[string]string{}
	wrappers := map[string][]int64{}
	axContext := int64(0)
	for _, file := range []string{"surface", "io", "ax", "downloads", "record", "viewstop", "login", "view"} {
		f, err := parser.ParseFile(token.NewFileSet(), "../../browserid/surface/"+file+".go", nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		ast.Inspect(f, func(n ast.Node) bool {
			if v, ok := n.(*ast.ValueSpec); ok && len(v.Names) == 1 && len(v.Values) == 1 {
				if lit, ok := v.Values[0].(*ast.BasicLit); ok && lit.Kind == token.STRING {
					sources[v.Names[0].Name], _ = strconv.Unquote(lit.Value)
				}
			}
			if call, ok := n.(*ast.CallExpr); ok && file == "ax" && len(call.Args) == 2 {
				if sel, ok := call.Fun.(*ast.SelectorExpr); ok && sel.Sel.Name == "WithTimeout" {
					if b, ok := call.Args[1].(*ast.BinaryExpr); ok && b.Op == token.MUL {
						lit, lOK := b.X.(*ast.BasicLit)
						unit, uOK := b.Y.(*ast.SelectorExpr)
						if lOK && uOK && unit.Sel.Name == "Second" {
							axContext, _ = strconv.ParseInt(lit.Value, 10, 64)
							axContext *= 1000
						}
					}
				}
			}
			c, ok := n.(*ast.CompositeLit)
			if !ok {
				return true
			}
			a := make([]string, len(c.Elts))
			for i, e := range c.Elts {
				switch e := e.(type) {
				case *ast.BasicLit:
					a[i], _ = strconv.Unquote(e.Value)
				case *ast.Ident:
					a[i] = e.Name
				}
			}
			if len(a) < 2 || a[0] != "docker" || a[1] != "exec" {
				return true
			}
			key := ""
			for _, s := range a {
				switch s {
				case "pastePython", "axPython", "downloadsPython", "recordPython", "xpra-start", "xpra-stop":
					key = s
				case "xdotool":
					key = "type"
				}
			}
			if key == "" && file == "surface" {
				key = "common"
			}
			if key == "" {
				return true
			}
			ms := int64(0)
			for i, s := range a {
				if s == "timeout" && i+3 < len(a) {
					d, e := time.ParseDuration(a[i+3])
					if e != nil {
						t.Fatal(e)
					}
					ms = d.Milliseconds()
				}
			}
			wrappers[key] = append(wrappers[key], ms)
			return true
		})
	}
	for _, tc := range []struct {
		name, builder   string
		argv            []string
		policy, wrapper int64
		count           int
	}{
		{"xdotool", "common", []string{"xdotool", "getwindowfocus"}, 3000, 5000, 1},
		{"fbread", "common", []string{"srf-fbread", "pair", ":100", "0"}, 3000, 5000, 1},
		{"paste", "pastePython", []string{"python3", "-I", "-c", sources["pastePython"]}, 10000, 12000, 2},
		{"ax", "axPython", []string{"python3", "-I", "-c", sources["axPython"]}, 3000, 5000, 1},
		{"type", "type", []string{"xdotool", "type", "--file", "-"}, 28000, 30000, 1},
		{"downloads", "downloadsPython", []string{"python3", "-I", "-c", sources["downloadsPython"], "/profile/exchange"}, 18000, 20000, 1},
		{"record", "recordPython", []string{"python3", "-I", "-u", "-c", sources["recordPython"]}, 0, 0, 1},
		{"xpra-start", "xpra-start", []string{"identity-entrypoint.sh", "xpra-start"}, 110000, 120000, 1},
		{"xpra-stop", "xpra-stop", []string{"identity-entrypoint.sh", "xpra-stop"}, 13000, 15000, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			o := Open{Argv: tc.argv}
			if tc.name == "record" {
				o.StdinCap = -1
			}
			ms, err := Allowed(o, helpers.Hashes())
			if err != nil {
				t.Fatal(err)
			}
			if ms != tc.policy {
				t.Errorf("policy %d want %d", ms, tc.policy)
			}
			if ms > 0 && ms+1000 >= tc.wrapper {
				t.Error("shim deadline must be strictly inside binding wrapper")
			}
			actual := wrappers[tc.builder]
			if len(actual) != tc.count {
				t.Fatalf("builder coverage %d want %d", len(actual), tc.count)
			}
			for _, w := range actual {
				if tc.name == "ax" {
					// AX uses an enclosing context, rather than a timeout executable.
					if w != 0 || axContext != 10000 || axContext <= ms+1000 {
						t.Errorf("AX context drift: direct=%d context=%d", w, axContext)
					}
				} else if w != tc.wrapper || (ms > 0 && w <= ms+1000) {
					t.Errorf("real wrapper %d must match %d and exceed shim %d", w, tc.wrapper, ms+1000)
				}
			}
		})
	}
}

func TestMatchedStdinPolicy(t *testing.T) {
	for _, tc := range []struct {
		argv []string
		want int64
	}{
		{[]string{"xdotool", "search", "--class", "type"}, 0},
		{[]string{"xdotool", "type", "--file", "-"}, 8000},
		{[]string{"srf-fbread", "pair", ":100", "0"}, 0},
	} {
		o := Open{Argv: tc.argv, StdinCap: tc.want}
		if _, err := Allowed(o, nil); err != nil {
			t.Fatal(err)
		}
		o.StdinCap++
		if _, err := Allowed(o, nil); err == nil {
			t.Errorf("%q accepted excess stdin", tc.argv)
		}
	}
	// io.go Type passes the validated text unchanged; inject.validRequest limits
	// it to 2000 UTF-8 runes, requiring at most 8000 bytes (not 32 KiB).
	f, err := parser.ParseFile(token.NewFileSet(), "../../browserid/inject/inject.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	ast.Inspect(f, func(n ast.Node) bool {
		b, ok := n.(*ast.BinaryExpr)
		if !ok || b.Op != token.GTR {
			return true
		}
		c, ok := b.X.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := c.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "RuneCountInString" {
			return true
		}
		lit, ok := b.Y.(*ast.BasicLit)
		if ok && lit.Value == "2000" {
			found = true
		}
		return true
	})
	if !found {
		t.Fatal("surface typed-text bound changed; review policy byte cap")
	}
}
