package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"strconv"
	"strings"
	"testing"
	"time"
)

// Read the actual Go builders without importing the surface or running Docker.
func TestShimDeadlineInsideSurfaceWrappers(t *testing.T) {
	sources := map[string]*ast.File{}
	for _, name := range []string{"surface", "io", "ax", "downloads", "viewstop", "record", "login", "view"} {
		f, err := parser.ParseFile(token.NewFileSet(), "../../src/browserid/surface/"+name+".go", nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		sources[name] = f
	}
	helperSource := map[string]string{}
	for _, f := range sources {
		ast.Inspect(f, func(n ast.Node) bool {
			v, ok := n.(*ast.ValueSpec)
			if !ok || len(v.Names) != 1 || len(v.Values) != 1 {
				return true
			}
			lit, ok := v.Values[0].(*ast.BasicLit)
			if ok && lit.Kind == token.STRING {
				helperSource[v.Names[0].Name], _ = strconv.Unquote(lit.Value)
			}
			return true
		})
	}
	tests := []struct {
		name            string
		argv            []string
		policy, wrapper time.Duration
	}{
		{"xdotool", []string{"xdotool", "getwindowfocus"}, 3 * time.Second, 5 * time.Second},
		{"fbread", []string{"srf-fbread", "pair", ":100", "0"}, 3 * time.Second, 5 * time.Second},
		{"ax", []string{"python3", "-I", "-c", helperSource["axPython"]}, 3 * time.Second, 5 * time.Second},
		{"paste", []string{"python3", "-I", "-c", helperSource["pastePython"]}, 10 * time.Second, 12 * time.Second},
		{"type", []string{"xdotool", "type", "--clearmodifiers", "--delay", "0", "--file", "-"}, 28 * time.Second, 30 * time.Second},
		{"downloads", []string{"python3", "-I", "-c", helperSource["downloadsPython"], "/profile/exchange"}, 18 * time.Second, 20 * time.Second},
		{"xpra-start", []string{"identity-entrypoint.sh", "xpra-start"}, 110 * time.Second, 120 * time.Second},
		{"xpra-stop", []string{"identity-entrypoint.sh", "xpra-stop"}, 13 * time.Second, 15 * time.Second},
		{"record", []string{"python3", "-I", "-u", "-c", helperSource["recordPython"]}, 0, 0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			o := proto.Open{Argv: tt.argv}
			if tt.name == "record" {
				o.StdinCap = -1
			}
			ms, err := proto.Allowed(o, helpers.Hashes())
			if err != nil {
				t.Fatal(err)
			}
			policy := time.Duration(ms) * time.Millisecond
			if policy != tt.policy {
				t.Errorf("policy=%v want %v", policy, tt.policy)
			}
			deadline := shimDeadline(ms)
			wantDeadline := tt.policy
			if tt.policy > 0 {
				wantDeadline += time.Second
			}
			if deadline != wantDeadline {
				t.Errorf("shim deadline %v want %v", deadline, wantDeadline)
			}
			if tt.wrapper > 0 && deadline >= tt.wrapper {
				t.Errorf("shim %v must be strictly below wrapper %v", deadline, tt.wrapper)
			}
			if tt.name == "record" && deadline != 0 {
				t.Fatal("record must be unbounded and cancellable")
			}
		})
	}
	// Check every direct timeout builder, including both paste paths, plus the
	// common t.exec builder used by frame/input probes. Fail closed if missing.
	counts := map[string]int{}
	for name, f := range sources {
		ast.Inspect(f, func(n ast.Node) bool {
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
			wrapper := time.Duration(0)
			for i, s := range a {
				if s == "timeout" && i+3 < len(a) {
					var err error
					wrapper, err = time.ParseDuration(a[i+3])
					if err != nil {
						t.Fatal(err)
					}
				}
			}
			key := ""
			for _, s := range a {
				switch s {
				case "pastePython":
					key = "paste"
				case "downloadsPython":
					key = "downloads"
				case "recordPython":
					key = "record"
				case "axPython":
					key = "ax"
				case "xdotool":
					key = "type"
				case "xpra-start", "xpra-stop":
					key = s
				}
			}
			if key == "" && name == "surface" && wrapper > 0 {
				key = "xdotool"
			}
			if key == "" {
				return true
			}
			counts[key]++
			for _, tt := range tests {
				if tt.name != key {
					continue
				}
				if key == "ax" {
					if wrapper != 0 {
						t.Fatal("AX wrapper changed; inspect context deadline")
					}
					return true
				}
				if wrapper != tt.wrapper {
					t.Errorf("%s actual wrapper %v != binding %v", key, wrapper, tt.wrapper)
				}
			}
			return true
		})
	}
	for key, want := range map[string]int{"xdotool": 1, "paste": 2, "type": 1, "downloads": 1, "xpra-start": 1, "xpra-stop": 1, "record": 1, "ax": 1} {
		if counts[key] != want {
			t.Errorf("%s builder count %d want %d", key, counts[key], want)
		}
	}
	// AX's real direct builder is bounded by its enclosing context, not timeout argv.
	axDeadline := false
	ast.Inspect(sources["ax"], func(n ast.Node) bool {
		c, ok := n.(*ast.CallExpr)
		if !ok || len(c.Args) != 2 {
			return true
		}
		s, ok := c.Fun.(*ast.SelectorExpr)
		if !ok || s.Sel.Name != "WithTimeout" {
			return true
		}
		b, ok := c.Args[1].(*ast.BinaryExpr)
		if !ok {
			return true
		}
		v, ok := b.X.(*ast.BasicLit)
		if !ok {
			return true
		}
		unit, ok := b.Y.(*ast.SelectorExpr)
		if !ok || unit.Sel.Name != "Second" {
			return true
		}
		seconds, err := strconv.Atoi(v.Value)
		if err != nil {
			t.Fatal(err)
		}
		if time.Duration(seconds)*time.Second <= 4*time.Second {
			t.Fatal("AX context must exceed shim deadline")
		}
		axDeadline = true
		return true
	})
	if !axDeadline {
		t.Fatal("AX context deadline AUSENTE")
	}
	// Ensure source extraction cannot silently turn the record policy into a fake.
	if !strings.Contains(helperSource["recordPython"], "import") {
		t.Fatal("record source AUSENTE")
	}
}
