package programsurface

import (
	"errors"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
	"strings"
	"testing"
)

func requireCode(t *testing.T, err error, code int) {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) || e.Code != code {
		t.Fatalf("error = %v; want code %d", err, code)
	}
}

func TestCatalog(t *testing.T) {
	for id, entry := range catalog {
		if entry.ID != id {
			t.Errorf("catalog key %q differs from id %q", id, entry.ID)
		}
		if err := entry.Validate(); err != nil {
			t.Errorf("catalog %q: %v", id, err)
		}
	}
	mousepad := catalog["mousepad"]
	if mousepad.Enabled || mousepad.Image != "opensuperficies-program-office-arm64:0.1.0" {
		t.Errorf("mousepad stub: %+v", mousepad)
	}

	p, err := Lookup("notepad")
	if err != nil {
		t.Fatal(err)
	}
	want := Program{ID: "notepad", Image: "opensuperficies-program-gtk-arm64:0.1.0", Network: NetNone, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir, A11y: true, Enabled: true}
	if p != want {
		t.Fatalf("catalog = %+v", p)
	}
	for _, id := range []string{"", "unknown", "mousepad"} {
		_, err := Lookup(id)
		requireCode(t, err, 2)
	}
}

func TestValidate(t *testing.T) {
	p := Program{ID: "notepad", Image: "opensuperficies-program-gtk-arm64:0.1.0", Network: NetNone, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name   string
		change func(*Program)
	}{
		{"id", func(p *Program) { p.ID = "A" }}, {"short id", func(p *Program) { p.ID = "a" }}, {"long id", func(p *Program) { p.ID = strings.Repeat("a", 25) }},
		{"image", func(p *Program) { p.Image = "other:latest" }}, {"network", func(p *Program) { p.Network = "host" }},
		{"zero windows", func(p *Program) { p.MaxWindows = 0 }}, {"many windows", func(p *Program) { p.MaxWindows = 17 }},
		{"small width", func(p *Program) { p.MinWidth = 199 }}, {"small height", func(p *Program) { p.MinHeight = 199 }},
		{"wide", func(p *Program) { p.MinWidth = 1281 }}, {"tall", func(p *Program) { p.MinHeight = 901 }},
		{"relative", func(p *Program) { p.ExchangeDir = "profile/exchange" }}, {"root", func(p *Program) { p.ExchangeDir = "/profile" }},
		{"sibling", func(p *Program) { p.ExchangeDir = "/profiles/exchange" }}, {"traversal", func(p *Program) { p.ExchangeDir = "/profile/a/../exchange" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) { q := p; tc.change(&q); requireCode(t, q.Validate(), 3) })
	}
}

func TestValidCapsule(t *testing.T) {
	for _, s := range []string{"srf-pg-a", "srf-pg-0", "srf-pg-" + strings.Repeat("a", 40)} {
		if !ValidCapsule(s) {
			t.Errorf("refused %q", s)
		}
	}
	for _, s := range []string{"", "srf-pg-", "srf-id-a", "srf-pg-A", "srf-pg--a", "srf-pg-a_b", "srf-pg-" + strings.Repeat("a", 41), "srf-pg-a\n"} {
		if ValidCapsule(s) {
			t.Errorf("accepted %q", s)
		}
	}
}

func TestMetaviewerCatalog(t *testing.T) {
	p, err := Lookup("metaviewer-nested")
	if err != nil {
		t.Fatal(err)
	}
	want := Program{ID: "metaviewer-nested", Image: "opensuperficies-program-electron-arm64:0.1.0", Network: NetNone, SeccompIdentity: true, ClearSingleton: true, MinWidth: 640, MinHeight: 450, MaxWindows: 1, FixedWidth: 1280, FixedHeight: 720, ExchangeDir: ExchangeDir, A11y: true, Enabled: true}
	if p != want {
		t.Fatalf("catalog = %+v; want %+v", p, want)
	}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestFixedGeometryValidation(t *testing.T) {
	p, err := Lookup("notepad")
	if err != nil {
		t.Fatal(err)
	}
	p.MaxWindows = 1
	for _, size := range [][2]int{{0, 0}, {400, 300}, {1280, 900}, {1280, 720}} {
		q := p
		q.FixedWidth, q.FixedHeight = size[0], size[1]
		if err := q.Validate(); err != nil {
			t.Errorf("valid size %v: %v", size, err)
		}
	}
	for _, size := range [][2]int{{1280, 0}, {0, 720}, {-1, 720}, {1280, -1}, {199, 720}, {1280, 199}, {1281, 720}, {1280, 901}} {
		q := p
		q.FixedWidth, q.FixedHeight = size[0], size[1]
		t.Run(fmt.Sprint(size), func(t *testing.T) { requireCode(t, q.Validate(), 3) })
	}
	p.FixedWidth, p.FixedHeight, p.MaxWindows = 1280, 720, 2
	requireCode(t, p.Validate(), 3)
}

func TestFixedGeometryMeetsMinimum(t *testing.T) {
	for _, runtime := range []Runtime{RuntimeContainer, RuntimeVM} {
		for _, size := range [][2]int{{399, 300}, {400, 299}, {200, 200}} {
			t.Run(fmt.Sprintf("%s/%v", runtime, size), func(t *testing.T) {
				p := catalog["notepad"]
				if runtime == RuntimeVM {
					p = vmProgram()
				}
				p.MaxWindows = 1
				p.FixedWidth, p.FixedHeight = size[0], size[1]
				requireCode(t, p.Validate(), 3)
			})
		}
	}
}

func TestDisplayGeometryMatchesSurface(t *testing.T) {
	readConstants := func(filename string) map[string]int {
		t.Helper()
		file, err := parser.ParseFile(token.NewFileSet(), filename, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		values := make(map[string]int)
		for _, decl := range file.Decls {
			group, ok := decl.(*ast.GenDecl)
			if !ok || group.Tok != token.CONST {
				continue
			}
			for _, spec := range group.Specs {
				value := spec.(*ast.ValueSpec)
				for i, name := range value.Names {
					if i >= len(value.Values) {
						continue
					}
					literal, ok := value.Values[i].(*ast.BasicLit)
					if !ok || literal.Kind != token.INT {
						continue
					}
					n, err := strconv.Atoi(literal.Value)
					if err != nil {
						t.Fatal(err)
					}
					values[name.Name] = n
				}
			}
		}
		return values
	}
	catalogValues := readConstants("catalog.go")
	surfaceValues := readConstants("../browserid/surface/surface.go")
	for _, pair := range [][2]string{{"displayWidth", "Width"}, {"displayHeight", "Height"}} {
		got, catalogOK := catalogValues[pair[0]]
		want, surfaceOK := surfaceValues[pair[1]]
		if !catalogOK || !surfaceOK {
			t.Fatalf("missing integer constants: catalog %s present=%t; surface %s present=%t", pair[0], catalogOK, pair[1], surfaceOK)
		}
		if got != want {
			t.Errorf("catalog %s = %d; surface %s = %d", pair[0], got, pair[1], want)
		}
	}
}

func vmProgram() Program {
	return Program{ID: "notepad-vm", Image: "opensuperficies-vm-arm64:0.1.0", Network: NetNone, Runtime: RuntimeVM, MemoryMiB: 8192, KVM: true, GuestCPUs: 4, GuestMemMiB: 6144, ProfileDiskMiB: 1024, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir, A11y: true, Enabled: true}
}

func TestVMCatalog(t *testing.T) {
	p, err := Lookup("notepad-vm")
	if err != nil {
		t.Fatal(err)
	}
	if p != vmProgram() {
		t.Fatalf("VM catalog = %+v", p)
	}
	if ValidCapsule("srf-vm-test") {
		t.Fatal("VM accepted by program validator")
	}
}

func TestRuntimeValidation(t *testing.T) {
	base := vmProgram()
	if err := base.Validate(); err != nil {
		t.Fatal(err)
	}
	cases := map[string]func(*Program){
		"unknown runtime": func(p *Program) { p.Runtime = "other" },
		"no kvm":          func(p *Program) { p.KVM = false },
		"zero cpu":        func(p *Program) { p.GuestCPUs = 0 },
		"many cpu":        func(p *Program) { p.GuestCPUs = 6 },
		"small guest":     func(p *Program) { p.GuestMemMiB = 511 },
		"no overhead":     func(p *Program) { p.GuestMemMiB = 7169 },
		"large container": func(p *Program) { p.MemoryMiB = 8193 },
		"small container": func(p *Program) { p.MemoryMiB = 1024 },
		"small disk":      func(p *Program) { p.ProfileDiskMiB = 63 },
		"large disk":      func(p *Program) { p.ProfileDiskMiB = 4097 },
		"proxy":           func(p *Program) { p.Network = NetProxy },
		"container image": func(p *Program) { p.Image = "opensuperficies-program-gtk-arm64:0.1.0" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) { p := base; mutate(&p); requireCode(t, p.Validate(), 3) })
	}
	for _, cpus := range []int{1, 5} {
		for _, mem := range []int{512, 7168} {
			for _, disk := range []int{64, 4096} {
				p := base
				p.GuestCPUs, p.GuestMemMiB, p.ProfileDiskMiB = cpus, mem, disk
				if err := p.Validate(); err != nil {
					t.Fatal(err)
				}
			}
		}
	}
	container, _ := Lookup("notepad")
	for _, runtime := range []Runtime{"", RuntimeContainer} {
		container.Runtime = runtime
		if err := container.Validate(); err != nil {
			t.Fatal(err)
		}
		for name, mutate := range map[string]func(*Program){
			"kvm": func(p *Program) { p.KVM = true }, "memory": func(p *Program) { p.MemoryMiB = 8192 },
			"cpu": func(p *Program) { p.GuestCPUs = 1 }, "guest memory": func(p *Program) { p.GuestMemMiB = 512 },
			"disk": func(p *Program) { p.ProfileDiskMiB = 64 }, "vm image": func(p *Program) { p.Image = base.Image },
		} {
			t.Run(string(runtime)+name, func(t *testing.T) { p := container; mutate(&p); requireCode(t, p.Validate(), 3) })
		}
	}
}
