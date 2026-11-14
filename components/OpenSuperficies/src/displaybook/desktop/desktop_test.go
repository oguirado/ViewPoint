package desktop

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/displaybook/puertos"
)

func TestDesktopConstants(t *testing.T) {
	if DefaultImageTag != "opensuperficies-desktop-arm64:1.0.0" {
		t.Fatalf("unexpected DefaultImageTag: got %s, want opensuperficies-desktop-arm64:1.0.0", DefaultImageTag)
	}
	if DefaultClass != "desktop" {
		t.Fatalf("unexpected DefaultClass: got %s, want desktop", DefaultClass)
	}
}

func TestDesktopQuarantineEnv(t *testing.T) {
	// Read the actual environment configuration built by the code
	cfg := BuildContainerConfig(2, 10022, "srf-net-2")
	if len(cfg.Env) == 0 {
		t.Fatalf("BuildContainerConfig returned empty Env")
	}

	envMap := make(map[string]string)
	for _, e := range cfg.Env {
		k, v, found := strings.Cut(e, "=")
		if found {
			envMap[k] = v
		}
	}

	// 1. Central property for desktop class: NO_AT_BRIDGE MUST be 0 (enables AT-SPI2 perception ladder)
	// If NO_AT_BRIDGE is 1 (like browser class), this test MUST fail!
	if val, ok := envMap["NO_AT_BRIDGE"]; !ok {
		t.Fatalf("NO_AT_BRIDGE is missing from cfg.Env")
	} else if val != "0" {
		t.Fatalf("NO_AT_BRIDGE must be 0 for desktop class, got %q", val)
	}

	// 2. Central property for desktop class: private D-Bus session bus MUST be unix:path=/run/newton-tenant/bus
	if val, ok := envMap["DBUS_SESSION_BUS_ADDRESS"]; !ok {
		t.Fatalf("DBUS_SESSION_BUS_ADDRESS is missing from cfg.Env")
	} else if val != "unix:path=/run/newton-tenant/bus" {
		t.Fatalf("DBUS_SESSION_BUS_ADDRESS must be 'unix:path=/run/newton-tenant/bus', got %q", val)
	}

	// 3. Central property: DBUS_SYSTEM_BUS_ADDRESS must be disabled:
	if val, ok := envMap["DBUS_SYSTEM_BUS_ADDRESS"]; !ok {
		t.Fatalf("DBUS_SYSTEM_BUS_ADDRESS is missing from cfg.Env")
	} else if val != "disabled:" {
		t.Fatalf("DBUS_SYSTEM_BUS_ADDRESS must be 'disabled:', got %q", val)
	}

	// 4. Verify all quarantine variables required by Puerta H
	requiredVars := map[string]string{
		"XDG_RUNTIME_DIR":       "/run/newton-tenant",
		"XDG_SESSION_TYPE":      "x11",
		"GTK_USE_PORTAL":        "0",
		"GDK_BACKEND":           "x11",
		"QT_QPA_PLATFORM":       "xcb",
		"GIO_USE_VFS":           "local",
		"DISPLAY":               ":100",
		"LIBGL_ALWAYS_SOFTWARE": "1",
		"GALLIUM_DRIVER":        "llvmpipe",
	}

	for k, expectedVal := range requiredVars {
		if actualVal, ok := envMap[k]; !ok {
			t.Errorf("missing required quarantine variable in cfg.Env: %s", k)
		} else if actualVal != expectedVal {
			t.Errorf("quarantine variable %s mismatch: got %q, want %q", k, actualVal, expectedVal)
		}
	}
}

func TestDesktopPortAllocation(t *testing.T) {
	plan := puertos.NuevoPlanPuertos()
	par, err := plan.Par(2)
	if err != nil {
		t.Fatalf("failed to calculate ports for slot 2: %v", err)
	}
	if par.Xpra != 10022 {
		t.Fatalf("slot 2 Xpra port expected 10022, got %d", par.Xpra)
	}
	if par.CDP != 9222 {
		t.Fatalf("slot 2 CDP port expected 9222, got %d", par.CDP)
	}
}

func TestCapsulaInfoSerialization(t *testing.T) {
	info := CapsulaInfo{
		Slot:       2,
		Clase:      "desktop",
		Contenedor: "srf-ng-2",
		PuertoXpra: 10022,
		Token:      "secret-token",
		Red:        "srf-net-2",
		Estado:     "listo",
	}

	data, err := json.Marshal(info)
	if err != nil {
		t.Fatalf("failed to marshal CapsulaInfo: %v", err)
	}

	var decoded CapsulaInfo
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatalf("failed to unmarshal CapsulaInfo: %v", err)
	}

	if decoded.Slot != 2 || decoded.Clase != "desktop" || decoded.Contenedor != "srf-ng-2" || decoded.PuertoXpra != 10022 {
		t.Fatalf("decoded info mismatch: %+v", decoded)
	}
}

func TestXTestSyntheticKeyAndButtonConstants(t *testing.T) {
	if ButtonLeft != 1 {
		t.Fatalf("ButtonLeft expected 1, got %d", ButtonLeft)
	}
	if ButtonMiddle != 2 {
		t.Fatalf("ButtonMiddle expected 2, got %d", ButtonMiddle)
	}
	if ButtonRight != 3 {
		t.Fatalf("ButtonRight expected 3, got %d", ButtonRight)
	}
	if ButtonWheelUp != 4 {
		t.Fatalf("ButtonWheelUp expected 4, got %d", ButtonWheelUp)
	}
	if ButtonWheelDown != 5 {
		t.Fatalf("ButtonWheelDown expected 5, got %d", ButtonWheelDown)
	}
}

func TestXTestClientNilSafety(t *testing.T) {
	var c *XTestClient

	if err := c.Close(); err != nil {
		t.Errorf("Close on nil client should not error, got %v", err)
	}
	if err := c.MovePointer(10, 20); err == nil {
		t.Errorf("MovePointer on nil client should error")
	}
	if err := c.ButtonDown(ButtonLeft); err == nil {
		t.Errorf("ButtonDown on nil client should error")
	}
	if err := c.ButtonUp(ButtonLeft); err == nil {
		t.Errorf("ButtonUp on nil client should error")
	}
	if err := c.Click(10, 20, ButtonLeft); err == nil {
		t.Errorf("Click on nil client should error")
	}
	if err := c.KeyDown(30); err == nil {
		t.Errorf("KeyDown on nil client should error")
	}
	if err := c.KeyUp(30); err == nil {
		t.Errorf("KeyUp on nil client should error")
	}
	if err := c.TypeKey(30); err == nil {
		t.Errorf("TypeKey on nil client should error")
	}
	if _, err := c.QueryWindows(); err == nil {
		t.Errorf("QueryWindows on nil client should error")
	}
	if _, err := c.GetWindowGeometry(0); err == nil {
		t.Errorf("GetWindowGeometry on nil client should error")
	}
	if _, err := c.GetWindowName(0); err == nil {
		t.Errorf("GetWindowName on nil client should error")
	}
	if _, err := c.GetWindowPID(0); err == nil {
		t.Errorf("GetWindowPID on nil client should error")
	}
}

func TestXTestWithLiveDisplay(t *testing.T) {
	disp := os.Getenv("DISPLAY")
	if disp == "" {
		disp = ":1"
	}

	client, err := NewXTestClient(disp)
	if err != nil {
		t.Skipf("skipping live display test; cannot connect to %s: %v", disp, err)
	}
	defer client.Close()

	w, h := client.ScreenDimensions()
	if w == 0 || h == 0 {
		t.Fatalf("expected positive screen dimensions, got %dx%d", w, h)
	}

	windows, err := client.QueryWindows()
	if err != nil {
		t.Fatalf("QueryWindows failed on %s: %v", disp, err)
	}
	t.Logf("found %d root windows on %s", len(windows), disp)

	x0, y0, err := client.QueryPointer()
	if err != nil {
		t.Fatalf("QueryPointer failed on %s: %v", disp, err)
	}
	t.Logf("initial pointer position: (%d, %d)", x0, y0)

	targetX, targetY := int16(120), int16(120)
	if err := client.MovePointer(targetX, targetY); err != nil {
		t.Fatalf("MovePointer failed on %s: %v", disp, err)
	}

	x1, y1, err := client.QueryPointer()
	if err != nil {
		t.Fatalf("QueryPointer after move failed on %s: %v", disp, err)
	}
	if x1 != targetX || y1 != targetY {
		t.Fatalf("pointer coordinates mismatch: got (%d, %d), want (%d, %d)", x1, y1, targetX, targetY)
	}
}

func TestR13PerceptionLadderNoAtBridge(t *testing.T) {
	// 1. Verify desktop environment configuration explicitly sets NO_AT_BRIDGE=0
	env := BuildDesktopEnv(10022)
	found := false
	for _, e := range env {
		if e == "NO_AT_BRIDGE=0" {
			found = true
		}
		if e == "NO_AT_BRIDGE=1" {
			t.Fatalf("hazard R-13: desktop class must not have NO_AT_BRIDGE=1 (destroys perception ladder)")
		}
	}
	if !found {
		t.Fatalf("hazard R-13: NO_AT_BRIDGE=0 missing from BuildDesktopEnv")
	}

	// 2. Verify perception ladder explicitly fails if NO_AT_BRIDGE=1 is set
	t.Setenv("NO_AT_BRIDGE", "1")
	_, err := ProbarPercepcionLocal(":100")
	if err == nil {
		t.Fatalf("hazard R-13: ProbarPercepcionLocal must fail when NO_AT_BRIDGE=1")
	}
	if !strings.Contains(err.Error(), "NO_AT_BRIDGE=1") {
		t.Fatalf("expected error mentioning NO_AT_BRIDGE=1, got: %v", err)
	}
}

func TestDetenerReportsError(t *testing.T) {
	if err := os.MkdirAll(DefaultRuntimeDir, 0755); err != nil {
		t.Fatalf("failed to create runtime dir: %v", err)
	}
	badStateDir := filepath.Join(DefaultRuntimeDir, "slot-9999.json")
	if err := os.MkdirAll(badStateDir, 0755); err != nil {
		t.Fatalf("failed to create bad state dir: %v", err)
	}
	nested := filepath.Join(badStateDir, "cannot-remove")
	if err := os.WriteFile(nested, []byte("x"), 0644); err != nil {
		t.Fatalf("failed to create nested file: %v", err)
	}
	defer os.RemoveAll(badStateDir)

	err := Detener(context.Background(), 9999)
	if err == nil {
		t.Fatalf("expected Detener to return an error when state file cannot be removed, got nil")
	}
	if !strings.Contains(err.Error(), "detener slot 9999 failures") {
		t.Fatalf("expected teardown failure message, got: %v", err)
	}
}

func TestListarReportsError(t *testing.T) {
	if err := os.MkdirAll(DefaultRuntimeDir, 0755); err != nil {
		t.Fatalf("failed to create runtime dir: %v", err)
	}
	corruptFile := filepath.Join(DefaultRuntimeDir, "slot-99999.json")
	if err := os.WriteFile(corruptFile, []byte("INVALID_JSON{{{"), 0644); err != nil {
		t.Fatalf("failed to write corrupt state file: %v", err)
	}
	defer os.Remove(corruptFile)

	list, err := Listar(context.Background())
	if err == nil {
		t.Fatalf("expected Listar to return error on corrupt state file, got list: %+v", list)
	}
	if !strings.Contains(err.Error(), "decoding state file") {
		t.Fatalf("expected decoding error, got: %v", err)
	}
}
