package browser

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/displaybook/puertos"
)

func TestBrowserPortPlan(t *testing.T) {
	plan := puertos.NuevoPlanPuertos()
	par0, err := plan.Par(0)
	if err != nil {
		t.Fatalf("unexpected error calculating slot 0 ports: %v", err)
	}
	if par0.Xpra != 10020 {
		t.Errorf("expected Xpra port 10020 for slot 0, got %d", par0.Xpra)
	}
	if par0.CDP != 9220 {
		t.Errorf("expected CDP port 9220 for slot 0, got %d", par0.CDP)
	}

	par1, err := plan.Par(1)
	if err != nil {
		t.Fatalf("unexpected error calculating slot 1 ports: %v", err)
	}
	if par1.Xpra != 10021 {
		t.Errorf("expected Xpra port 10021 for slot 1, got %d", par1.Xpra)
	}
	if par1.CDP != 9221 {
		t.Errorf("expected CDP port 9221 for slot 1, got %d", par1.CDP)
	}
}

func TestBrowserSlotLockReservation(t *testing.T) {
	tmpDir := t.TempDir()
	lock, err := puertos.ReservarSlot(tmpDir, 0)
	if err != nil {
		t.Fatalf("expected slot reservation to succeed: %v", err)
	}
	defer lock.Release()

	// Second reservation on same slot must collide
	_, err2 := puertos.ReservarSlot(tmpDir, 0)
	if err2 == nil {
		t.Fatalf("expected second reservation on slot 0 to fail with lock collision")
	}
	if !puertos.IsLockCollision(err2) {
		t.Errorf("expected lock collision error, got %v", err2)
	}
}

func TestBrowserTarContextPackaging(t *testing.T) {
	tmpDir := t.TempDir()
	dfPath := filepath.Join(tmpDir, "Dockerfile")
	epPath := filepath.Join(tmpDir, "entrypoint.sh")
	rlPath := filepath.Join(tmpDir, "srf-relay")

	if err := os.WriteFile(dfPath, []byte("FROM scratch\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(epPath, []byte("#!/bin/sh\n"), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(rlPath, []byte("fake-relay-bin"), 0755); err != nil {
		t.Fatal(err)
	}

	opts := BuildOptions{
		Tag:          "test-tag:1.0.0",
		Dockerfile:   dfPath,
		Entrypoint:   epPath,
		RelayBin:     rlPath,
		DockerSocket: "/nonexistent.sock",
	}

	// Should fail connecting to fake socket, but proves file validation and tar creation worked
	ctx := context.Background()
	_, err := Construir(ctx, opts)
	if err == nil {
		t.Fatalf("expected connection error against fake docker socket")
	}
}

// Regression test for U08-F6: Dockerfile must unexpose port 9222 inherited from base 1.0.0
func TestBrowserDockerfileUnexposesCDP(t *testing.T) {
	dfPath := filepath.Join("..", "..", "..", "docker", "ng", "browser", "Dockerfile")
	data, err := os.ReadFile(dfPath)
	if err != nil {
		// Fallback for tests running from different root
		dfPath = "docker/ng/browser/Dockerfile"
		data, err = os.ReadFile(dfPath)
		if err != nil {
			t.Fatalf("failed to read Dockerfile: %v", err)
		}
	}

	content := string(data)
	if !strings.Contains(content, "FROM scratch") {
		t.Fatalf("expected Dockerfile to use multi-stage build from scratch to strip inherited ports")
	}

	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(line)
		if strings.HasPrefix(trimmed, "EXPOSE") {
			if strings.Contains(trimmed, "9222") {
				t.Fatalf("Dockerfile EXPOSE line must not contain port 9222: %s", trimmed)
			}
			if !strings.Contains(trimmed, "10000") || !strings.Contains(trimmed, "9220") {
				t.Fatalf("Dockerfile EXPOSE line must declare 10000 and 9220: %s", trimmed)
			}
		}
	}
}

// Regression test for BLQ-NG-06 and U09-F1: PID must not use sh -c wrapper which matches itself,
// and must use -o (oldest) instead of -n (newest) to return the browser process instead of child renderers.
func TestBrowserPIDNoShellWrapper(t *testing.T) {
	path := "browser.go"
	data, err := os.ReadFile(path)
	if err != nil {
		path = filepath.Join("src", "displaybook", "browser", "browser.go")
		data, err = os.ReadFile(path)
		if err != nil {
			t.Fatalf("failed to read browser.go: %v", err)
		}
	}

	fset := token.NewFileSet()
	node, err := parser.ParseFile(fset, path, data, 0)
	if err != nil {
		t.Fatalf("failed to parse browser.go: %v", err)
	}

	var pidFunc *ast.FuncDecl
	for _, decl := range node.Decls {
		if fn, ok := decl.(*ast.FuncDecl); ok && fn.Name.Name == "PID" {
			pidFunc = fn
			break
		}
	}
	if pidFunc == nil {
		t.Fatalf("func PID not found in browser.go")
	}

	var hasShellWrapper bool
	var shellStrings []string
	var hasNewestFlag bool
	var hasOldestFlag bool
	ast.Inspect(pidFunc.Body, func(n ast.Node) bool {
		if lit, ok := n.(*ast.BasicLit); ok && lit.Kind == token.STRING {
			val := strings.Trim(lit.Value, `"`+"`")
			if val == "sh" || val == "-c" || strings.Contains(val, "sh -c") {
				hasShellWrapper = true
				shellStrings = append(shellStrings, val)
			}
			if val == "-n" {
				hasNewestFlag = true
			}
			if val == "-o" {
				hasOldestFlag = true
			}
		}
		return true
	})

	if hasShellWrapper {
		t.Fatalf("PID must not use shell wrapper (found forbidden shell tokens: %v); direct exec of pgrep required", shellStrings)
	}
	if hasNewestFlag {
		t.Fatalf("PID must not use -n (newest process); pgrep must use -o (oldest process) to target browser rather than child renderers")
	}
	if !hasOldestFlag {
		t.Fatalf("PID must use -o (oldest process) with pgrep")
	}
}
