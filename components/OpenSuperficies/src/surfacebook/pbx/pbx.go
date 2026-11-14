package pbx

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

// DefaultSbdPath is the absolute path of the OpenSwitchboard daemon v1.0.0 binary. It must
// match the KernelBook launcher allowlist (src/kernelbook/lanzador), which rejects bare "sbd".
// Install OpenSwitchboard there (placeholder home layout) or adjust both constants together.
const DefaultSbdPath = "/home/user/Documentos/OpenSwitchboard/bin/sbd"

// ActiveSbdPointerFile points to the active hermetic sbd daemon configuration.
const ActiveSbdPointerFile = "/tmp/srf-sbd-u17.active"

// ActiveSbdConfig models the runtime configuration emitted by sbd-prueba.sh.
type ActiveSbdConfig struct {
	Root    string `json:"root"`
	UDSPath string `json:"uds"`
	HTTP    string `json:"http"`
	PID     int    `json:"pid"`
}

// SubcomandosSBD lists the complete set of subcommands discovered from sbd v1.0.0 usage.
var SubcomandosSBD = []string{
	"version",
	"daemon",
	"bridge",
	"send",
	"read",
	"register",
	"phonebook",
	"dial",
	"barge",
	"deck",
	"hold",
	"rooms",
	"snoop",
	"spawn",
	"transfer",
	"unbarge",
	"wal-resolve",
	"whisper",
}

// SubcomandoValido checks if a given subcommand string is part of sbd's known command catalogue.
func SubcomandoValido(cmd string) bool {
	for _, s := range SubcomandosSBD {
		if s == cmd {
			return true
		}
	}
	return false
}

// HermeticDefaultSocket is the canonical hermetic fallback socket path for tests when no daemon is active.
const HermeticDefaultSocket = "/tmp/srf-sbd-hermetic/sbd.sock"

// LeerConfiguracionSbdActiva reads the hermetic daemon configuration if present.
func LeerConfiguracionSbdActiva() *ActiveSbdConfig {
	pointerFile := os.Getenv("SRF_SBD_POINTER_FILE")
	if pointerFile == "" {
		pointerFile = ActiveSbdPointerFile
	}
	data, err := os.ReadFile(pointerFile)
	if err != nil {
		return nil
	}
	var cfg ActiveSbdConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		lines := strings.Split(string(data), "\n")
		root := strings.TrimSpace(lines[0])
		if root != "" {
			return &ActiveSbdConfig{
				Root:    root,
				UDSPath: filepath.Join(root, "sbd.sock"),
			}
		}
		return nil
	}
	return &cfg
}

// RutaAgendaPorDefecto computes the canonical path for switchboard phonebook.json.
func RutaAgendaPorDefecto() string {
	if cfg := LeerConfiguracionSbdActiva(); cfg != nil && cfg.Root != "" {
		return filepath.Join(cfg.Root, "openswitchboard", "wal", "phonebook.json")
	}
	baseDir := os.Getenv("XDG_RUNTIME_DIR")
	if baseDir == "" {
		baseDir = filepath.Join(os.TempDir(), "openswitchboard")
	} else {
		baseDir = filepath.Join(baseDir, "openswitchboard")
	}
	return filepath.Join(baseDir, "wal", "phonebook.json")
}

// RutaUDSSocketActiva returns the active UDS socket path for sbd.
// When no active hermetic configuration is present, it returns HermeticDefaultSocket
// to ensure daemon-down checks probe the hermetic socket rather than the operator's socket.
func RutaUDSSocketActiva() string {
	if cfg := LeerConfiguracionSbdActiva(); cfg != nil && cfg.UDSPath != "" {
		return cfg.UDSPath
	}
	return HermeticDefaultSocket
}

// EjecutarSBD invokes the real sbd CLI binary under KernelBook launcher allowlist rules,
// propagating active hermetic XDG_RUNTIME_DIR isolation when present.
func EjecutarSBD(ctx context.Context, args ...string) ([]byte, error) {
	if len(args) == 0 || (!SubcomandoValido(args[0]) && args[0] != "version") {
		cmdName := "<empty>"
		if len(args) > 0 {
			cmdName = args[0]
		}
		return nil, fmt.Errorf("pbx: invalid or unknown sbd subcommand %q", cmdName)
	}

	cmd, err := lanzador.CommandContext(ctx, DefaultSbdPath, args...)
	if err != nil {
		return nil, fmt.Errorf("pbx: validate sbd command: %w", err)
	}

	if cfg := LeerConfiguracionSbdActiva(); cfg != nil && cfg.Root != "" {
		env := os.Environ()
		hasXDG := false
		for i, e := range env {
			if strings.HasPrefix(e, "XDG_RUNTIME_DIR=") {
				env[i] = "XDG_RUNTIME_DIR=" + cfg.Root
				hasXDG = true
				break
			}
		}
		if !hasXDG {
			env = append(env, "XDG_RUNTIME_DIR="+cfg.Root)
		}
		cmd.Env = env
	}

	out, err := cmd.CombinedOutput()
	if err != nil {
		return out, fmt.Errorf("pbx: sbd execution failed (%w): %s", err, string(out))
	}
	return out, nil
}

// PhonebookManager handles phonebook state, process-bound registration, and anti-ghost sweeping.
type PhonebookManager struct {
	mu         sync.Mutex
	rutaAgenda string
}

// NuevoPhonebookManager creates a manager pointing to the specified or default phonebook path.
func NuevoPhonebookManager(rutaAgenda string) *PhonebookManager {
	if rutaAgenda == "" {
		rutaAgenda = RutaAgendaPorDefecto()
	}
	return &PhonebookManager{
		rutaAgenda: rutaAgenda,
	}
}

// DemonioVivo probes whether the switchboard daemon UDS socket is active and accepting connections.
func (m *PhonebookManager) DemonioVivo() bool {
	sockPath := RutaUDSSocketActiva()
	conn, err := net.DialTimeout("unix", sockPath, 250*time.Millisecond)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// CargarAgenda reads and deserializes the phonebook JSON map.
func (m *PhonebookManager) CargarAgenda() (map[int]*Extension, error) {
	data, err := os.ReadFile(m.rutaAgenda)
	if err != nil {
		if os.IsNotExist(err) {
			return make(map[int]*Extension), nil
		}
		return nil, fmt.Errorf("read phonebook: %w", err)
	}

	if len(bytesTrim(data)) == 0 {
		return make(map[int]*Extension), nil
	}

	var rawMap map[string]json.RawMessage
	if err := json.Unmarshal(data, &rawMap); err != nil {
		return nil, fmt.Errorf("unmarshal phonebook: %w", err)
	}

	result := make(map[int]*Extension, len(rawMap))
	for k, v := range rawMap {
		var ext Extension
		if err := json.Unmarshal(v, &ext); err != nil {
			return nil, fmt.Errorf("corrupt phonebook entry %q: %w", k, err)
		}
		num := ext.Numero
		if num == 0 {
			if parsed, err := fmt.Sscanf(k, "%d", &num); err == nil && parsed == 1 {
				ext.Numero = num
			}
		}
		ext.Extension = ext.Numero
		if ext.Slug == "" {
			ext.Slug = ext.AgenteSlug
		}
		if ext.AgenteSlug == "" {
			ext.AgenteSlug = ext.Slug
		}
		if ext.StartTime == 0 {
			ext.StartTime = ext.StartTimeTick
		}
		if ext.StartTimeTick == 0 {
			ext.StartTimeTick = ext.StartTime
		}
		result[ext.Numero] = &ext
	}

	return result, nil
}

func bytesTrim(b []byte) []byte {
	for len(b) > 0 && (b[0] == ' ' || b[0] == '\t' || b[0] == '\n' || b[0] == '\r') {
		b = b[1:]
	}
	for len(b) > 0 && (b[len(b)-1] == ' ' || b[len(b)-1] == '\t' || b[len(b)-1] == '\n' || b[len(b)-1] == '\r') {
		b = b[:len(b)-1]
	}
	return b
}

// GuardarAgenda writes the phonebook map atomically to disk under an explicit file lock,
// preserving the canonical OpenSwitchboard SbdExtension schema and sala_actual field.
func (m *PhonebookManager) GuardarAgenda(agenda map[int]*Extension) error {
	dir := filepath.Dir(m.rutaAgenda)
	if err := os.MkdirAll(dir, 0700); err != nil {
		return fmt.Errorf("create phonebook dir: %w", err)
	}

	lockFile := m.rutaAgenda + ".lock"
	lf, err := os.OpenFile(lockFile, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return fmt.Errorf("open phonebook lock: %w", err)
	}
	defer lf.Close()

	if err := syscall.Flock(int(lf.Fd()), syscall.LOCK_EX); err != nil {
		return fmt.Errorf("acquire phonebook lock: %w", err)
	}
	defer syscall.Flock(int(lf.Fd()), syscall.LOCK_UN)

	serializable := make(map[string]map[string]any, len(agenda))
	for k, v := range agenda {
		key := fmt.Sprintf("%d", k)
		v.Numero = k
		slug := v.AgenteSlug
		if slug == "" {
			slug = v.Slug
		}
		st := v.StartTimeTick
		if st == 0 {
			st = v.StartTime
		}
		entry := map[string]any{
			"numero":          v.Numero,
			"agente_slug":     slug,
			"modelo":          v.Modelo,
			"rol":             v.Rol,
			"pid":             v.PID,
			"start_time_tick": st,
			"registrado_en":   v.RegistradoEn,
			"estado":          v.Estado,
		}
		if v.SalaActual != "" {
			entry["sala_actual"] = v.SalaActual
		}
		serializable[key] = entry
	}

	data, err := json.MarshalIndent(serializable, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal phonebook: %w", err)
	}

	tmpFile := fmt.Sprintf("%s.tmp.%d", m.rutaAgenda, time.Now().UnixNano())
	if err := os.WriteFile(tmpFile, data, 0600); err != nil {
		return fmt.Errorf("write temp phonebook: %w", err)
	}

	if err := os.Rename(tmpFile, m.rutaAgenda); err != nil {
		_ = os.Remove(tmpFile)
		return fmt.Errorf("atomic replace phonebook: %w", err)
	}

	return nil
}

// Registrar performs process-witnessed registration into the phonebook within [300, 899].
// If using the default switchboard environment, it drives registration through the real sbd CLI binary.
func (m *PhonebookManager) Registrar(slug, rol, modelo string, pid int, ident *ProcessIdentity) (*Extension, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if !m.DemonioVivo() {
		return nil, fmt.Errorf("pbx: sbd daemon is not running (socket %s unreachable)", RutaUDSSocketActiva())
	}

	if slug == "" {
		return nil, errors.New("pbx: slug cannot be empty")
	}
	if rol == "" {
		rol = RoleSurface
	} else if rol != RoleSurface {
		return nil, fmt.Errorf("pbx: unknown or unsupported role %q for surface capsule (must be %s)", rol, RoleSurface)
	}
	if modelo == "" {
		modelo = "capsule"
	}

	var startTick uint64

	// Anti-ghost verification
	if ident != nil {
		if err := AntiGhostCheck(*ident); err != nil {
			return nil, err
		}
		pid = ident.PID
		startTick = ident.EffectiveStartTime()
	} else {
		if pid <= 0 {
			pid = os.Getpid()
		}
		liveTick, err := ObtenerStartTimeProcfs(pid)
		if err != nil {
			return nil, fmt.Errorf("pbx: live starttime lookup for pid %d: %w", pid, err)
		}
		startTick = liveTick

		// Cross-check procfs timestamp independently against boot time & inode mtime
		if err := CrossCheckProcfsTime(pid, liveTick); err != nil {
			return nil, err
		}
	}

	// Route through the real sbd CLI when switchboard environment is active
	out, err := EjecutarSBD(context.Background(), "register",
		"--agent", slug,
		"--model", modelo,
		"--role", rol,
		"--pid", strconv.Itoa(pid),
	)
	if err != nil {
		return nil, fmt.Errorf("pbx: sbd register CLI failed: %w", err)
	}

	// Parse assigned extension number from sbd output: "ASIGNADA EXTENSION %d a ..."
	re := regexp.MustCompile(`ASIGNADA EXTENSION (\d+) a`)
	matches := re.FindSubmatch(out)
	if len(matches) < 2 {
		return nil, fmt.Errorf("pbx: unable to parse assigned extension from sbd output: %s", string(out))
	}

	extNum, err := strconv.Atoi(string(matches[1]))
	if err != nil {
		return nil, fmt.Errorf("pbx: parse extension number %q: %w", string(matches[1]), err)
	}

	if extNum < MinCapsuleExtension || extNum > 899 {
		return nil, fmt.Errorf("pbx: assigned extension %d out of capsule bounds [%d, 899]",
			extNum, MinCapsuleExtension)
	}

	ext := &Extension{
		Numero:        extNum,
		Extension:     extNum,
		AgenteSlug:    slug,
		Slug:          slug,
		Modelo:        modelo,
		Rol:           rol,
		PID:           pid,
		StartTimeTick: startTick,
		StartTime:     startTick,
		RegistradoEn:  time.Now(),
		Estado:        "CONNECTED",
	}

	return ext, nil
}

// Baja deregisters an extension by slug under an explicit file lock.
func (m *PhonebookManager) Baja(slug string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	if !m.DemonioVivo() {
		return fmt.Errorf("pbx: sbd daemon is not running (socket %s unreachable)", RutaUDSSocketActiva())
	}

	agenda, err := m.CargarAgenda()
	if err != nil {
		return err
	}

	encontrado := false
	for num, ext := range agenda {
		if ext.AgenteSlug == slug || ext.Slug == slug {
			delete(agenda, num)
			encontrado = true
		}
	}

	if encontrado {
		return m.GuardarAgenda(agenda)
	}

	return nil
}

// Listar returns active extensions. It strictly verifies that the switchboard daemon is alive,
// failing closed if the daemon is stopped or unreachable.
func (m *PhonebookManager) Listar() ([]*Extension, error) {
	if !m.DemonioVivo() {
		return nil, fmt.Errorf("pbx: sbd daemon is not running (socket %s unreachable)", RutaUDSSocketActiva())
	}

	out, err := EjecutarSBD(context.Background(), "phonebook")
	if err != nil {
		return nil, fmt.Errorf("pbx: sbd phonebook CLI failed: %w", err)
	}

	agendaMap, _ := m.CargarAgenda()
	var lista []*Extension

	// Parse table lines from sbd phonebook output
	scanner := bufio.NewScanner(bytes.NewReader(out))
	headerPassed := false
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if strings.HasPrefix(line, "EXT") && strings.Contains(line, "AGENTE") {
			headerPassed = true
			continue
		}
		if strings.HasPrefix(line, "---") || strings.HasPrefix(line, "OPENSWITCHBOARD") || line == "" {
			continue
		}
		if !headerPassed {
			continue
		}

		parts := strings.Split(line, "|")
		if len(parts) < 5 {
			continue
		}

		extNum, err := strconv.Atoi(strings.TrimSpace(parts[0]))
		if err != nil {
			continue
		}
		agentSlug := strings.TrimSpace(parts[1])
		model := strings.TrimSpace(parts[2])
		role := strings.TrimSpace(parts[3])
		status := strings.TrimSpace(parts[4])

		ext := &Extension{
			Numero:     extNum,
			Extension:  extNum,
			AgenteSlug: agentSlug,
			Slug:       agentSlug,
			Modelo:     model,
			Rol:        role,
			Estado:     status,
		}

		if cached, ok := agendaMap[extNum]; ok {
			ext.PID = cached.PID
			ext.StartTime = cached.StartTime
			ext.StartTimeTick = cached.StartTimeTick
			ext.RegistradoEn = cached.RegistradoEn
		}

		lista = append(lista, ext)
	}

	sort.Slice(lista, func(i, j int) bool {
		return lista[i].Numero < lista[j].Numero
	})
	return lista, nil
}

// Hold toggles hold state on the srfd side while invoking sbd hold CLI with error check.
func (m *PhonebookManager) Hold(slug string, off bool) (*Extension, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	agenda, err := m.CargarAgenda()
	if err != nil {
		return nil, err
	}

	for _, ext := range agenda {
		if ext.AgenteSlug == slug || ext.Slug == slug {
			holdArgs := []string{"hold", "--ext", strconv.Itoa(ext.Numero)}
			if sock := RutaUDSSocketActiva(); sock != "" {
				holdArgs = append(holdArgs, "--socket", sock)
			}
			if off {
				holdArgs = append(holdArgs, "--off")
			}
			if _, err := EjecutarSBD(context.Background(), holdArgs...); err != nil {
				return nil, fmt.Errorf("pbx: sbd hold CLI failed: %w", err)
			}

			if off {
				ext.Estado = "CONNECTED"
			} else {
				ext.Estado = "ON_HOLD"
			}
			return ext, nil
		}
	}

	return nil, fmt.Errorf("pbx: extension for slug %q not found", slug)
}

// Snoop performs observation check for a capsule extension, invoking sbd snoop CLI with error check.
func (m *PhonebookManager) Snoop(slug string) (*Extension, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	agenda, err := m.CargarAgenda()
	if err != nil {
		return nil, err
	}

	for _, ext := range agenda {
		if ext.AgenteSlug == slug || ext.Slug == slug {
			snoopArgs := []string{"snoop", "--ext", strconv.Itoa(ext.Numero)}
			if sock := RutaUDSSocketActiva(); sock != "" {
				snoopArgs = append(snoopArgs, "--socket", sock)
			}
			if _, err := EjecutarSBD(context.Background(), snoopArgs...); err != nil {
				return nil, fmt.Errorf("pbx: sbd snoop CLI failed: %w", err)
			}
			return ext, nil
		}
	}

	return nil, fmt.Errorf("pbx: extension for slug %q not found", slug)
}

// Whisper injects an advisory directive, invoking sbd whisper CLI with error check.
func (m *PhonebookManager) Whisper(toSlug, msg string) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	agenda, err := m.CargarAgenda()
	if err != nil {
		return err
	}

	for _, ext := range agenda {
		if ext.AgenteSlug == toSlug || ext.Slug == toSlug {
			if ext.Estado == "DISCONNECTED" {
				return fmt.Errorf("pbx: target extension %d is disconnected", ext.Numero)
			}
			whisperArgs := []string{"whisper", "--to", strconv.Itoa(ext.Numero), "--msg", msg}
			if sock := RutaUDSSocketActiva(); sock != "" {
				whisperArgs = append(whisperArgs, "--socket", sock)
			}
			if _, err := EjecutarSBD(context.Background(), whisperArgs...); err != nil {
				return fmt.Errorf("pbx: sbd whisper CLI failed: %w", err)
			}
			return nil
		}
	}

	return fmt.Errorf("pbx: target slug %q not found", toSlug)
}
