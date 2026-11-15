package pbx

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func arrancarDaemonPrueba(t *testing.T) (func(), string) {
	t.Helper()
	if _, err := exec.LookPath(DefaultSbdPath); err != nil {
		t.Skipf("OpenSwitchboard sbd binary not available (set SRF_SBD_BIN): %v", err)
	}
	tempDir := t.TempDir()
	baseDir := filepath.Join(tempDir, "openswitchboard")
	udsPath := filepath.Join(tempDir, "sbd.sock")
	walPath := filepath.Join(baseDir, "wal", "aries.wal")
	roomsDir := filepath.Join(baseDir, "rooms")
	if err := os.MkdirAll(filepath.Dir(walPath), 0700); err != nil {
		t.Fatalf("mkdir wal: %v", err)
	}
	if err := os.MkdirAll(roomsDir, 0700); err != nil {
		t.Fatalf("mkdir rooms: %v", err)
	}

	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen dynamic port: %v", err)
	}
	httpAddr := l.Addr().String()
	_ = l.Close()

	pointerFile := filepath.Join(tempDir, "sbd-active.json")
	cfg := ActiveSbdConfig{
		Root:    tempDir,
		UDSPath: udsPath,
		HTTP:    httpAddr,
		PID:     0,
	}
	b, err := json.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal active config: %v", err)
	}
	if err := os.WriteFile(pointerFile, b, 0600); err != nil {
		t.Fatalf("write active pointer: %v", err)
	}
	t.Setenv("SRF_SBD_POINTER_FILE", pointerFile)

	cmd := exec.Command(DefaultSbdPath, "daemon",
		"--salas", roomsDir,
		"--wal", walPath,
		"--uds", udsPath,
		"--http", httpAddr,
	)
	cmd.Env = append(os.Environ(), "XDG_RUNTIME_DIR="+tempDir)
	if err := cmd.Start(); err != nil {
		_ = os.Remove(pointerFile)
		t.Fatalf("start daemon: %v", err)
	}

	ready := false
	for i := 0; i < 40; i++ {
		conn, err := net.DialTimeout("unix", udsPath, 50*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			ready = true
			break
		}
		time.Sleep(25 * time.Millisecond)
	}
	if !ready {
		_ = cmd.Process.Kill()
		_ = os.Remove(pointerFile)
		t.Fatalf("arrancarDaemonPrueba: timeout waiting for UDS socket %s", udsPath)
	}

	cleanup := func() {
		_ = cmd.Process.Kill()
		_ = cmd.Wait()
		_ = os.Remove(pointerFile)
	}
	return cleanup, udsPath
}

// TestAntiGhostCheckFalsification verifies that starttime discrepancies and dead PIDs are rejected.
func TestAntiGhostCheckFalsification(t *testing.T) {
	livePID := os.Getpid()
	liveStart, err := ObtenerStartTimeProcfs(livePID)
	if err != nil {
		t.Fatalf("cannot get live starttime for current process: %v", err)
	}

	// Case 1: Matching live PID and starttime passes
	identOk := ProcessIdentity{
		PID:       livePID,
		StartTime: liveStart,
	}
	if err := AntiGhostCheck(identOk); err != nil {
		t.Fatalf("expected live identity to pass, got: %v", err)
	}

	// Case 2: Mismatched starttime MUST be rejected with ErrStartTimeDiscrepante
	identMutant := ProcessIdentity{
		PID:       livePID,
		StartTime: liveStart + 99999, // deliberate mutant perturbation
	}
	errMutant := AntiGhostCheck(identMutant)
	if errMutant == nil {
		t.Fatalf("anti-ghost failed to reject mutant starttime")
	}
	if !errors.Is(errMutant, ErrStartTimeDiscrepante) {
		t.Fatalf("expected ErrStartTimeDiscrepante, got: %v", errMutant)
	}

	// Case 3: Dead/non-existent PID MUST be rejected with ErrProcesoNoEncontrado
	identDead := ProcessIdentity{
		PID:       9999999,
		StartTime: 12345,
	}
	errDead := AntiGhostCheck(identDead)
	if errDead == nil {
		t.Fatalf("anti-ghost failed to reject non-existent PID")
	}
	if !errors.Is(errDead, ErrProcesoNoEncontrado) {
		t.Fatalf("expected ErrProcesoNoEncontrado, got: %v", errDead)
	}
}

// TestBargeSimuladoEpochBumpAndStepLadder verifies the 5-step ladder, monotonic epoch increment,
// and active lease revocation against a hermetic daemon.
func TestBargeSimuladoEpochBumpAndStepLadder(t *testing.T) {
	cleanup, _ := arrancarDaemonPrueba(t)
	defer cleanup()

	ctx := context.Background()
	res, err := EjecutarBargeSimulado(ctx)
	if err != nil {
		t.Fatalf("EjecutarBargeSimulado failed: %v", err)
	}

	if res.Pasos != 5 {
		t.Fatalf("expected exactly 5 steps, got %d (detalles: %v)", res.Pasos, res.DetallesPasos)
	}

	if res.EpocaNueva <= res.EpocaPrevia {
		t.Fatalf("expected EpocaNueva > EpocaPrevia (%d > %d)", res.EpocaNueva, res.EpocaPrevia)
	}

	if res.EpocaLeidaDeArrendador != res.EpocaNueva {
		t.Fatalf("expected EpocaLeidaDeArrendador == EpocaNueva (%d == %d)",
			res.EpocaLeidaDeArrendador, res.EpocaNueva)
	}

	if res.ArriendosRevocados <= 0 {
		t.Fatalf("expected ArriendosRevocados > 0, got %d", res.ArriendosRevocados)
	}
}

// TestRegistrarRejectsUnknownRole verifies that unknown roles such as OPENSUPERFICIES
// are rejected fail-closed, rather than being silently laundered to SURFACE.
func TestRegistrarRejectsUnknownRole(t *testing.T) {
	cleanup, _ := arrancarDaemonPrueba(t)
	defer cleanup()

	mgr := NuevoPhonebookManager("")
	_, err := mgr.Registrar("unknown-role-capsule", RoleOpenSuperficies, "capsule", os.Getpid(), nil)
	if err == nil || !strings.Contains(err.Error(), "unknown or unsupported role") {
		t.Fatalf("expected Registrar to reject unknown role %q with validation error, got: %v", RoleOpenSuperficies, err)
	}
}

// TestPhonebookManagerRegistrationAndBaja tests registration within range 300-899 and deletion.
func TestPhonebookManagerRegistrationAndBaja(t *testing.T) {
	cleanup, _ := arrancarDaemonPrueba(t)
	defer cleanup()

	mgr := NuevoPhonebookManager("")
	pid := os.Getpid()
	slug := "test-capsule-alpha"

	ext, err := mgr.Registrar(slug, RoleSurface, "browser-capsule", pid, nil)
	if err != nil {
		t.Fatalf("Registrar failed: %v", err)
	}

	if ext.Numero < MinCapsuleExtension || ext.Numero > 899 {
		t.Fatalf("assigned extension %d outside [%d, 899]", ext.Numero, MinCapsuleExtension)
	}

	// Verify extension appears in phonebook listing
	lista, err := mgr.Listar()
	if err != nil {
		t.Fatalf("Listar failed: %v", err)
	}
	found := false
	for _, item := range lista {
		if item.Slug == slug && item.Numero == ext.Numero {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("registered extension not found in Listar()")
	}

	// Deregister extension via Baja
	if err := mgr.Baja(slug); err != nil {
		t.Fatalf("Baja failed: %v", err)
	}
}

// TestSrfdSideHoldSnoopWhisper exercises hold toggling, snoop monitoring, and whisper injection.
func TestSrfdSideHoldSnoopWhisper(t *testing.T) {
	cleanup, _ := arrancarDaemonPrueba(t)
	defer cleanup()

	mgr := NuevoPhonebookManager("")

	ext, err := mgr.Registrar("control-capsule", RoleSurface, "capsule", os.Getpid(), nil)
	if err != nil {
		t.Fatalf("register failed: %v", err)
	}

	// Test Hold on
	extHold, err := mgr.Hold("control-capsule", false)
	if err != nil {
		t.Fatalf("hold on failed: %v", err)
	}
	if extHold.Estado != "ON_HOLD" {
		t.Fatalf("expected ON_HOLD, got %s", extHold.Estado)
	}

	// Test Hold off
	extResume, err := mgr.Hold("control-capsule", true)
	if err != nil {
		t.Fatalf("hold off failed: %v", err)
	}
	if extResume.Estado != "CONNECTED" {
		t.Fatalf("expected CONNECTED, got %s", extResume.Estado)
	}

	// Test Snoop
	extSnoop, err := mgr.Snoop("control-capsule")
	if err != nil {
		t.Fatalf("snoop failed: %v", err)
	}
	if extSnoop.Numero != ext.Numero {
		t.Fatalf("expected snoop ext %d, got %d", ext.Numero, extSnoop.Numero)
	}

	// Test Whisper
	if err := mgr.Whisper("control-capsule", "steering hint"); err != nil {
		t.Fatalf("whisper failed: %v", err)
	}
}

// TestSbdCLIBindingLive verifies the live sbd binary discovery, version check,
// and that invalid subcommands are rejected fail-closed via SubcomandoValido.
func TestSbdCLIBindingLive(t *testing.T) {
	if _, err := exec.LookPath(DefaultSbdPath); err != nil {
		t.Skipf("OpenSwitchboard sbd binary not available at %s: %v", DefaultSbdPath, err)
	}
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())
	ctx := context.Background()
	out, err := EjecutarSBD(ctx, "version")
	if err != nil {
		t.Fatalf("EjecutarSBD version failed: %v (out: %s)", err, string(out))
	}
	if !SubcomandoValido("register") || !SubcomandoValido("phonebook") || !SubcomandoValido("barge") {
		t.Fatalf("expected subcommands not in catalogue")
	}
	if _, err := EjecutarSBD(ctx, "invalid-cmd-xyz"); err == nil || !strings.Contains(err.Error(), "invalid or unknown sbd subcommand") {
		t.Fatalf("expected EjecutarSBD to reject invalid subcommand via SubcomandoValido, got: %v", err)
	}
}

// TestSbdErrorFailClosedWhenDaemonDown verifies that sbd errors are never discarded:
// when the daemon is not running, hold, snoop, whisper, barge, phonebook, and baja all fail closed.
func TestSbdErrorFailClosedWhenDaemonDown(t *testing.T) {
	// Ensure active pointer is not present and hermetic fallback is used
	_ = os.Remove(ActiveSbdPointerFile)
	t.Setenv("SRF_SBD_POINTER_FILE", filepath.Join(t.TempDir(), "nonexistent.json"))
	t.Setenv("XDG_RUNTIME_DIR", t.TempDir())

	mgr := NuevoPhonebookManager(filepath.Join(t.TempDir(), "srf-nonexistent-agenda.json"))

	// 1. Phonebook Listar MUST fail closed when daemon is unreachable
	if _, err := mgr.Listar(); err == nil {
		t.Fatalf("expected mgr.Listar() to fail when daemon is down, got nil error")
	}

	// 2. Registrar MUST fail closed when daemon is unreachable
	if _, err := mgr.Registrar("ghost-agent", RoleSurface, "capsule", os.Getpid(), nil); err == nil {
		t.Fatalf("expected mgr.Registrar() to fail when daemon is down, got nil error")
	}

	// 3. Barge MUST fail closed when daemon is unreachable
	if _, err := EjecutarBargeSimulado(context.Background()); err == nil {
		t.Fatalf("expected EjecutarBargeSimulado to fail when daemon is down, got nil error")
	}

	// Setup fake extension in manager agenda
	agenda := map[int]*Extension{
		300: {
			Numero:     300,
			AgenteSlug: "ghost-agent",
			Slug:       "ghost-agent",
			Estado:     "CONNECTED",
		},
	}
	_ = mgr.GuardarAgenda(agenda)

	// 3. Hold MUST fail closed when daemon is down
	if _, err := mgr.Hold("ghost-agent", false); err == nil {
		t.Fatalf("expected Hold to fail when daemon is down, got nil error")
	}

	// 4. Snoop MUST fail closed when daemon is down
	if _, err := mgr.Snoop("ghost-agent"); err == nil {
		t.Fatalf("expected Snoop to fail when daemon is down, got nil error")
	}

	// 5. Whisper MUST fail closed when daemon is down
	if err := mgr.Whisper("ghost-agent", "hello"); err == nil {
		t.Fatalf("expected Whisper to fail when daemon is down, got nil error")
	}

	// 6. Baja MUST fail closed when daemon is down
	if err := mgr.Baja("ghost-agent"); err == nil {
		t.Fatalf("expected Baja to fail when daemon is down, got nil error")
	}
}
