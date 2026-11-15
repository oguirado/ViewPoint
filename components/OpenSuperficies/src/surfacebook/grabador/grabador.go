// Package grabador implements the SurfaceRecordingManager core of U-16: a nested Xvfb test
// display, a recording session with four artifacts on disk at t0, and a safety observation of
// the operator's display that is read from the kernel socket table, not from the caller's
// own arguments.
package grabador

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"
	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

func init() {
	xgb.Logger.SetOutput(io.Discard)
}

const (
	FicheroVideo      = "video-000.mp4"
	FicheroEventos    = "eventos.jsonl"
	FicheroFlujo      = "FLUJO.md"
	FicheroManifiesto = "manifiesto.json"
	FicheroPID        = "supervisor.pid"

	MarcaBloqueFlujo = "<!-- DATOS_SESION_AUTOMATICO -->"

	// TestigoAlcance is the campaign's own external instrument (PLAN DEC-20). A session runs it
	// during the recording and reports the number of lines it printed.
	TestigoAlcance = "scripts/ng/display-alcance.sh"

	dirSocketsX = "/tmp/.X11-unix"
)

var CuatroArtefactos = []string{FicheroVideo, FicheroEventos, FicheroFlujo, FicheroManifiesto}

// Seams that only tests replace. They let a test prove that a guard refuses BEFORE anything is
// dialled or spawned, without ever touching the operator's display when the guard is removed.
var (
	conectarX    = func(display string) (*xgb.Conn, error) { return xgb.NewConnDisplay(display) }
	crearComando = lanzador.CommandContext

	// elegirObjetivo resolves the recording target from the nested server's own window tree. A test
	// replaces it to produce a disagreement with the id the client allocated (the fail-closed branch
	// of the dual XID oracle).
	elegirObjetivo = elegirObjetivoDelServidor

	// sondaTestigo is the liveness canary of the campaign's scope witness (see sondearDisplay).
	sondaTestigo = sondearDisplay

	// lecturaX is one reading of the kernel socket table for a process snapshot (see observar); a
	// test wraps it to count the readings a session makes by a source other than its own counter.
	lecturaX = observar

	// trasLaInstantanea runs between the process-table snapshot of an observation and the reading of
	// the descriptors of its members. Only tests replace it, to make a pid change identity in between.
	trasLaInstantanea = func() {}
)

// ---------------------------------------------------------------------------------------------
// Display spelling: normalisation and the host-display guard
// ---------------------------------------------------------------------------------------------

// ErrDisplayProhibido means the spelling resolves to the operator's display (:0, :1, or the
// ambient $DISPLAY). ErrDisplayInvalido means it cannot be proven to be a local nested display.
var (
	ErrDisplayProhibido = errors.New("grabador_display_prohibido")
	ErrDisplayInvalido  = errors.New("grabador_display_invalido")
)

var (
	reSocketX = regexp.MustCompile(`\.X11-unix/X([0-9]+)`)
	reNumero  = regexp.MustCompile(`^([+-]?)([0-9]+)(?:\.([0-9]+))?$`)
)

// DisplayX is a parsed X display spelling.
type DisplayX struct {
	Original  string
	Protocolo string
	Host      string
	Ruta      string // set when the spelling names a socket path instead of host:number
	Numero    int
	Pantalla  int
}

// Canonico returns the only spelling the recorder ever passes to a child: ":N".
func (d DisplayX) Canonico() string { return ":" + strconv.Itoa(d.Numero) }

// Local reports whether the spelling reaches the server through the local unix socket.
func (d DisplayX) Local() bool {
	if d.Ruta != "" {
		return false
	}
	if d.Protocolo != "" && d.Protocolo != "unix" && d.Protocolo != "local" {
		return false
	}
	h := strings.ToLower(d.Host)
	return h == "" || h == "unix"
}

// NormalizarDisplay parses every X display spelling the clients in this stack understand
// ([proto/]host:N[.S], unix:N, [::1]:N, socket paths, N with leading zeros or a sign) into one
// structure. Anything it cannot parse strictly is an error: an unparseable display is never
// assumed to be safe.
func NormalizarDisplay(spec string) (DisplayX, error) {
	d := DisplayX{Original: spec}
	if spec == "" {
		return d, fmt.Errorf("%w: empty display (the ambient DISPLAY is never adopted)", ErrDisplayInvalido)
	}
	for _, r := range spec {
		ok := r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || strings.ContainsRune(":./_-+[]%", r)
		if !ok {
			return d, fmt.Errorf("%w: character %q is not allowed in %q", ErrDisplayInvalido, r, spec)
		}
	}
	if m := reSocketX.FindStringSubmatch(spec); m != nil {
		n, err := strconv.ParseUint(m[1], 10, 32)
		if err != nil {
			return d, fmt.Errorf("%w: socket number out of range in %q", ErrDisplayInvalido, spec)
		}
		d.Ruta, d.Numero = spec, int(n)
		return d, nil
	}
	colon := strings.LastIndex(spec, ":")
	if colon < 0 {
		return d, fmt.Errorf("%w: %q does not have the host:number form", ErrDisplayInvalido, spec)
	}
	hostPart, dispPart := spec[:colon], spec[colon+1:]
	if i := strings.LastIndex(hostPart, "/"); i >= 0 {
		d.Protocolo = strings.ToLower(hostPart[:i])
		hostPart = hostPart[i+1:]
		switch d.Protocolo {
		case "unix", "local", "tcp", "inet", "inet6":
		default:
			return d, fmt.Errorf("%w: unknown protocol %q in %q", ErrDisplayInvalido, d.Protocolo, spec)
		}
	}
	if strings.Contains(dispPart, "/") {
		return d, fmt.Errorf("%w: path after the colon in %q", ErrDisplayInvalido, spec)
	}
	d.Host = strings.TrimSuffix(strings.TrimPrefix(hostPart, "["), "]")
	m := reNumero.FindStringSubmatch(dispPart)
	if m == nil {
		return d, fmt.Errorf("%w: unreadable display number in %q", ErrDisplayInvalido, spec)
	}
	n, err := strconv.ParseUint(m[2], 10, 16)
	if err != nil || (m[1] == "-" && n != 0) {
		return d, fmt.Errorf("%w: display number out of range in %q", ErrDisplayInvalido, spec)
	}
	d.Numero = int(n)
	if m[3] != "" {
		p, err := strconv.ParseUint(m[3], 10, 16)
		if err != nil {
			return d, fmt.Errorf("%w: screen number out of range in %q", ErrDisplayInvalido, spec)
		}
		d.Pantalla = int(p)
	}
	return d, nil
}

// numerosDisplayHost lists the display numbers that belong to the operator: :0, :1 and whatever
// the ambient $DISPLAY resolves to.
func numerosDisplayHost() map[int]bool {
	m := map[int]bool{0: true, 1: true}
	if amb := os.Getenv("DISPLAY"); amb != "" {
		if d, err := NormalizarDisplay(amb); err == nil {
			m[d.Numero] = true
		}
	}
	return m
}

// ValidarDisplayAnidado is the host-display guard. It normalises the spelling first and only
// then compares, so ":1.0", "unix:1", "localhost:1.0", "[::1]:01", "/tmp/.X11-unix/X1" and every
// other spelling of :0 or :1 are refused. Only a local ":N" spelling of a non-operator display
// passes; the returned DisplayX carries the canonical form.
func ValidarDisplayAnidado(spec string) (DisplayX, error) {
	d, err := NormalizarDisplay(spec)
	if err != nil {
		return d, err
	}
	if numerosDisplayHost()[d.Numero] {
		return d, fmt.Errorf("%w: %q resolves to the operator's display :%d", ErrDisplayProhibido, spec, d.Numero)
	}
	if !d.Local() {
		return d, fmt.Errorf("%w: %q is not a local display (nested recording only uses the unix socket)", ErrDisplayInvalido, spec)
	}
	return d, nil
}

// ---------------------------------------------------------------------------------------------
// Child process supervision
// ---------------------------------------------------------------------------------------------

// cola keeps the last bytes written to it; it is the stderr sink of a child.
type cola struct {
	mu  sync.Mutex
	b   []byte
	cap int
}

func (c *cola) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.b = append(c.b, p...)
	if len(c.b) > c.cap {
		c.b = c.b[len(c.b)-c.cap:]
	}
	return len(p), nil
}

func (c *cola) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return strings.TrimSpace(string(c.b))
}

// contadorLineas counts newline bytes (exactly what `wc -l` counts) and keeps a short head of
// the output for diagnostics.
type contadorLineas struct {
	mu     sync.Mutex
	lineas int
	cabeza []byte
}

func (c *contadorLineas) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.lineas += bytes.Count(p, []byte{'\n'})
	if room := 1024 - len(c.cabeza); room > 0 {
		if len(p) < room {
			room = len(p)
		}
		c.cabeza = append(c.cabeza, p[:room]...)
	}
	return len(p), nil
}

func (c *contadorLineas) Lineas() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.lineas
}

func (c *contadorLineas) Cabeza() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return strings.TrimSpace(string(c.cabeza))
}

// proceso is a started child whose Wait is owned by exactly one goroutine.
type proceso struct {
	cmd    *exec.Cmd
	pid    int
	done   chan struct{}
	err    error
	stderr *cola
}

// hijosConocidos holds the pids of the children this package started and whose Wait is still
// pending. The process is a child subreaper (see activarSubreaper): everything else that shows up as
// a direct child is an orphan it adopted, and only those are reaped by recogerAdoptados.
var hijosConocidos sync.Map

var (
	subreaperUna sync.Once
	subreaperErr error
)

// activarSubreaper makes the calling process a child subreaper (prctl PR_SET_CHILD_SUBREAPER): a
// descendant whose parent exits is re-parented to this process instead of to pid 1, so a parent-pid
// walk from this process still reaches a daemonised or double-forked descendant of the recorder.
// The attribute belongs to the process, is not inherited, survives execve and only helps for forks
// made after it is set, so iniciar calls it before every spawn.
func activarSubreaper() error {
	subreaperUna.Do(func() {
		subreaperErr = unix.Prctl(unix.PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
	})
	return subreaperErr
}

// recogerAdoptados reaps the adopted orphans that already exited (a subreaper must reap what it
// adopts, or they linger as zombies). It never waits on a child this package started, whose own
// Wait must see the exit status, and never blocks. It returns how many it reaped.
func recogerAdoptados() int {
	snap, err := instantanea()
	if err != nil {
		return 0
	}
	return recogerDe(snap)
}

func recogerDe(snap map[int]infoProc) int {
	yo, n := os.Getpid(), 0
	for pid, inf := range snap {
		if inf.PPid != yo || inf.Estado != 'Z' {
			continue
		}
		if _, propio := hijosConocidos.Load(pid); propio {
			continue
		}
		var ws syscall.WaitStatus
		if w, err := syscall.Wait4(pid, &ws, syscall.WNOHANG, nil); err == nil && w == pid {
			n++
		}
	}
	return n
}

// iniciar starts cmd. On context cancellation the child gets sig (not SIGKILL) and is only
// killed after gracia, so a cancelled context can never strand a lock file or truncate a video.
func iniciar(cmd *exec.Cmd, sig syscall.Signal, gracia time.Duration, stdout io.Writer) (*proceso, error) {
	return iniciarConCola(cmd, sig, gracia, stdout, 4096)
}

func iniciarConCola(cmd *exec.Cmd, sig syscall.Signal, gracia time.Duration, stdout io.Writer, capStderr int) (*proceso, error) {
	_ = activarSubreaper() // before the first fork; a failure is reported by the session, not here
	p := &proceso{cmd: cmd, done: make(chan struct{}), stderr: &cola{cap: capStderr}}
	cmd.Cancel = func() error { return cmd.Process.Signal(sig) }
	cmd.WaitDelay = gracia
	cmd.Stdout = stdout
	cmd.Stderr = p.stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	p.pid = cmd.Process.Pid
	hijosConocidos.Store(p.pid, struct{}{})
	go func() {
		p.err = cmd.Wait()
		hijosConocidos.Delete(p.pid)
		close(p.done)
	}()
	return p, nil
}

// ejecutar runs a short command to completion through the launcher and returns what it printed.
// The child gets exactly the environment env; the deadline is real.
func ejecutar(ctx context.Context, plazo time.Duration, env []string, nombre string, args ...string) (stdout, stderr string, err error) {
	ctx, cancel := context.WithTimeout(ctx, plazo)
	defer cancel()
	cmd, err := crearComando(ctx, nombre, args...)
	if err != nil {
		return "", "", fmt.Errorf("launcher rejected %s: %w", nombre, err)
	}
	cmd.Env = env
	var out bytes.Buffer
	p, err := iniciarConCola(cmd, syscall.SIGTERM, 2*time.Second, &out, 32<<10)
	if err != nil {
		return "", "", fmt.Errorf("error starting %s: %w", nombre, err)
	}
	<-p.done
	if p.err != nil {
		if ctx.Err() != nil {
			return out.String(), p.stderr.String(), fmt.Errorf("%s did not finish within %v: %w", nombre, plazo, ctx.Err())
		}
		return out.String(), p.stderr.String(), fmt.Errorf("%s failed: %v: %s", nombre, p.err, ultimos(p.stderr.String(), 300))
	}
	return out.String(), p.stderr.String(), nil
}

// ultimos keeps the last n bytes of a diagnostic: what a tool complains about comes last.
func ultimos(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return "..." + s[len(s)-n:]
}

func (p *proceso) vivo() bool {
	select {
	case <-p.done:
		return false
	default:
		return true
	}
}

// detener asks the child to stop with sig, escalates to SIGKILL after gracia, and always waits
// for the child to be reaped. forzado reports whether the escalation was needed.
func (p *proceso) detener(sig syscall.Signal, gracia time.Duration) (forzado bool, err error) {
	if !p.vivo() {
		return false, nil
	}
	if e := p.cmd.Process.Signal(sig); e != nil && !errors.Is(e, os.ErrProcessDone) {
		err = fmt.Errorf("signal to pid %d: %w", p.pid, e)
	}
	select {
	case <-p.done:
		return false, err
	case <-time.After(gracia):
	}
	forzado = true
	if e := p.cmd.Process.Kill(); e != nil && !errors.Is(e, os.ErrProcessDone) {
		err = errors.Join(err, fmt.Errorf("kill of pid %d: %w", p.pid, e))
	}
	select {
	case <-p.done:
	case <-time.After(5 * time.Second):
		err = errors.Join(err, fmt.Errorf("pid %d did not exit after SIGKILL", p.pid))
	}
	return forzado, err
}

// ---------------------------------------------------------------------------------------------
// Nested Xvfb
// ---------------------------------------------------------------------------------------------

// XvfbServer represents a nested Xvfb X11 server instance.
type XvfbServer struct {
	Display    string
	SocketPath string
	LockPath   string
	PID        int

	proc *proceso
	once sync.Once
	err  error
}

// ArrancarXvfb starts a dedicated Xvfb on the given display through the launcher. The spelling
// goes through the host-display guard before anything is stat'ed, dialled or spawned. It
// refuses a display already served by a live X server and never removes a socket it did not
// create: a dead leftover is reclaimed by Xvfb itself.
func ArrancarXvfb(ctx context.Context, display string) (*XvfbServer, error) {
	d, err := ValidarDisplayAnidado(display)
	if err != nil {
		return nil, err
	}
	canon := d.Canonico()
	socketPath := filepath.Join(dirSocketsX, "X"+strconv.Itoa(d.Numero))
	lockPath := "/tmp/.X" + strconv.Itoa(d.Numero) + "-lock"

	if _, errS := os.Stat(socketPath); errS == nil {
		if c, errC := conectarX(canon); errC == nil {
			c.Close()
			return nil, fmt.Errorf("display %s is already served by a live X server", canon)
		}
	}

	// -noreset: without it the server resets when the readiness probe (or any client) disconnects,
	// and a client that connects during the reset gets ECONNRESET.
	cmd, err := crearComando(ctx, "Xvfb", canon, "-screen", "0", "1024x768x24", "-nolisten", "tcp", "-noreset")
	if err != nil {
		return nil, fmt.Errorf("launcher rejected Xvfb: %w", err)
	}
	// Strictly isolated environment: the ambient DISPLAY is never inherited.
	cmd.Env = []string{"PATH=" + os.Getenv("PATH")}
	p, err := iniciar(cmd, syscall.SIGTERM, 3*time.Second, nil)
	if err != nil {
		return nil, fmt.Errorf("error starting Xvfb on %s: %w", canon, err)
	}
	server := &XvfbServer{Display: canon, SocketPath: socketPath, LockPath: lockPath, PID: p.pid, proc: p}

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if !p.vivo() {
			errDet := server.Detener()
			return nil, errors.Join(fmt.Errorf("Xvfb exited before it was ready on %s: %v %s", canon, p.err, p.stderr.String()), errDet)
		}
		if _, errS := os.Stat(socketPath); errS == nil {
			if c, errC := conectarX(canon); errC == nil {
				c.Close()
				return server, nil
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	errDet := server.Detener()
	return nil, errors.Join(fmt.Errorf("Xvfb on %s did not answer within 3 seconds", canon), errDet)
}

// Detener stops Xvfb with SIGTERM first (a killed Xvfb leaves /tmp/.X<N>-lock behind), waits for
// it to be reaped, escalates only after a grace period, and then verifies that neither the lock
// file nor the socket survived. A leftover it can prove is its own is removed and reported.
func (s *XvfbServer) Detener() error {
	if s == nil {
		return nil
	}
	s.once.Do(func() { s.err = s.detener() })
	return s.err
}

func (s *XvfbServer) detener() error {
	var errs []error
	if s.proc != nil {
		forzado, err := s.proc.detener(syscall.SIGTERM, 3*time.Second)
		if err != nil {
			errs = append(errs, err)
		}
		if forzado {
			errs = append(errs, fmt.Errorf("Xvfb pid %d did not exit on SIGTERM and was forced", s.PID))
		}
	}
	if s.LockPath != "" {
		if b, err := os.ReadFile(s.LockPath); err == nil {
			if pid, _ := strconv.Atoi(strings.TrimSpace(string(b))); pid == s.PID {
				if err := os.Remove(s.LockPath); err != nil {
					errs = append(errs, fmt.Errorf("could not remove %s: %w", s.LockPath, err))
				} else {
					errs = append(errs, fmt.Errorf("Xvfb left %s behind and it was removed", s.LockPath))
				}
			} else {
				errs = append(errs, fmt.Errorf("%s belongs to pid %d, not to Xvfb %d", s.LockPath, pid, s.PID))
			}
		} else if !errors.Is(err, fs.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	if s.SocketPath != "" {
		if _, err := os.Lstat(s.SocketPath); err == nil {
			if c, errC := conectarX(s.Display); errC == nil {
				c.Close()
				errs = append(errs, fmt.Errorf("%s is still served by another X server", s.SocketPath))
			} else if err := os.Remove(s.SocketPath); err != nil {
				errs = append(errs, fmt.Errorf("could not remove %s: %w", s.SocketPath, err))
			} else {
				errs = append(errs, fmt.Errorf("Xvfb left %s behind and it was removed", s.SocketPath))
			}
		}
	}
	return errors.Join(errs...)
}

// ---------------------------------------------------------------------------------------------
// Nested test window
// ---------------------------------------------------------------------------------------------

// VentanaPrueba represents a mapped X11 test window on a nested display.
type VentanaPrueba struct {
	Conn    *xgb.Conn
	WinID   xproto.Window
	XID     uint64
	Display string
}

func internarAtomo(c *xgb.Conn, nombre string) (xproto.Atom, error) {
	r, err := xproto.InternAtom(c, false, uint16(len(nombre)), nombre).Reply()
	if err != nil {
		return 0, err
	}
	return r.Atom, nil
}

// CrearVentanaPrueba connects to the nested display in pure Go, creates a test window, sets
// WM_NAME, WM_CLASS and _NET_WM_PID, maps it, and confirms with the server that it is viewable.
func CrearVentanaPrueba(display, titulo string, width, height int) (*VentanaPrueba, error) {
	d, err := ValidarDisplayAnidado(display)
	if err != nil {
		return nil, err
	}
	c, err := conectarX(d.Canonico())
	if err != nil {
		return nil, fmt.Errorf("error connecting to display %s: %w", d.Canonico(), err)
	}
	fallo := func(format string, a ...any) (*VentanaPrueba, error) {
		c.Close()
		return nil, fmt.Errorf(format, a...)
	}

	setup := xproto.Setup(c)
	if setup == nil || len(setup.Roots) == 0 {
		return fallo("empty X11 setup")
	}
	screen := setup.DefaultScreen(c)

	winID, err := xproto.NewWindowId(c)
	if err != nil {
		return fallo("error allocating the window id: %w", err)
	}
	if width <= 0 {
		width = 800
	}
	if height <= 0 {
		height = 600
	}
	if titulo == "" {
		titulo = "OpenSuperficies test window"
	}

	mask := uint32(xproto.CwBackPixel | xproto.CwEventMask)
	values := []uint32{screen.WhitePixel, xproto.EventMaskExposure | xproto.EventMaskStructureNotify}
	if err := xproto.CreateWindowChecked(c, screen.RootDepth, winID, screen.Root,
		50, 50, uint16(width), uint16(height), 0,
		xproto.WindowClassInputOutput, screen.RootVisual, mask, values).Check(); err != nil {
		return fallo("error creating the X11 window: %w", err)
	}

	if err := xproto.ChangePropertyChecked(c, xproto.PropModeReplace, winID,
		xproto.AtomWmName, xproto.AtomString, 8, uint32(len(titulo)), []byte(titulo)).Check(); err != nil {
		return fallo("error setting WM_NAME: %w", err)
	}
	wmClass := []byte("srf-ventana-prueba\x00srf-ventana-prueba\x00")
	if err := xproto.ChangePropertyChecked(c, xproto.PropModeReplace, winID,
		xproto.AtomWmClass, xproto.AtomString, 8, uint32(len(wmClass)), wmClass).Check(); err != nil {
		return fallo("error setting WM_CLASS: %w", err)
	}
	atomPID, err := internarAtomo(c, "_NET_WM_PID")
	if err != nil {
		return fallo("error interning _NET_WM_PID: %w", err)
	}
	pidBytes := make([]byte, 4)
	binary.LittleEndian.PutUint32(pidBytes, uint32(os.Getpid()))
	if err := xproto.ChangePropertyChecked(c, xproto.PropModeReplace, winID,
		atomPID, xproto.AtomCardinal, 32, 1, pidBytes).Check(); err != nil {
		return fallo("error setting _NET_WM_PID: %w", err)
	}

	if err := xproto.MapWindowChecked(c, winID).Check(); err != nil {
		return fallo("error mapping the X11 window: %w", err)
	}
	attrs, err := xproto.GetWindowAttributes(c, winID).Reply()
	if err != nil {
		return fallo("error reading the window attributes: %w", err)
	}
	if attrs.MapState != xproto.MapStateViewable {
		return fallo("window %#x is not viewable (map_state=%d)", uint32(winID), attrs.MapState)
	}

	return &VentanaPrueba{Conn: c, WinID: winID, XID: uint64(winID), Display: d.Canonico()}, nil
}

// Cerrar unmaps and destroys the window and terminates the X11 connection. It is idempotent.
func (v *VentanaPrueba) Cerrar() error {
	if v == nil || v.Conn == nil {
		return nil
	}
	var errs []error
	if err := xproto.UnmapWindowChecked(v.Conn, v.WinID).Check(); err != nil {
		errs = append(errs, fmt.Errorf("unmap: %w", err))
	}
	if err := xproto.DestroyWindowChecked(v.Conn, v.WinID).Check(); err != nil {
		errs = append(errs, fmt.Errorf("destroy: %w", err))
	}
	v.Conn.Close()
	v.Conn = nil
	return errors.Join(errs...)
}

// ---------------------------------------------------------------------------------------------
// Manifest and artifacts
// ---------------------------------------------------------------------------------------------

// Manifiesto defines the session manifest written to manifiesto.json.
type Manifiesto struct {
	SchemaVersion   string       `json:"schema_version"`
	SessionID       string       `json:"session_id"`
	Creado          string       `json:"creado"`
	DuracionMS      int64        `json:"duracion_ms"`
	Estado          string       `json:"estado"`
	RazonIncompleta string       `json:"razon_incompleta,omitempty"`
	Display         string       `json:"display"`
	Objetivo        ObjetivoInfo `json:"objetivo"`
	Artefactos      []string     `json:"artefactos"`
	Eventos         EventosInfo  `json:"eventos"`
	Video           VideoInfo    `json:"video"`
}

type ObjetivoInfo struct {
	XID    string `json:"xid"`
	Titulo string `json:"titulo"`
}

type EventosInfo struct {
	Fichero string `json:"fichero"`
	NLineas int    `json:"n_lineas"`
	SHA256  string `json:"sha256"`
}

type VideoInfo struct {
	Fichero    string  `json:"fichero"`
	Bytes      int64   `json:"bytes"`
	Fotogramas int     `json:"fotogramas"`
	DuracionS  float64 `json:"duracion_s"`
}

// PlantillaFlujo is the initial markdown template for FLUJO.md.
const PlantillaFlujo = `# Surface Recording Flow

## Session: %s
- Start time: %s
- Assigned display: %s

### Operating Objectives
1. Initialize the artifacts at t0 (video-000.mp4, eventos.jsonl, FLUJO.md, manifiesto.json).
2. Capture the video stream and the events without touching the operator's host display.
3. Consolidate the manifest as complete after a clean stop.
`

// ContarArtefactosEnDisco scans the session directory with os.Stat to count how many of the 4
// declared artifacts exist on disk at the moment of invocation.
func ContarArtefactosEnDisco(sessionDir string) (int, []string) {
	count := 0
	var encontrados []string
	for _, art := range CuatroArtefactos {
		p := filepath.Join(sessionDir, art)
		if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
			count++
			encontrados = append(encontrados, art)
		}
	}
	return count, encontrados
}

// EscribirManifiestoAtomico writes the manifest JSON atomically via a temporary file and rename.
// A failed write never leaves the temporary file behind.
func EscribirManifiestoAtomico(path string, m *Manifiesto) error {
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".escribiendo"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}

// LeerManifiesto reads and unmarshals manifiesto.json from disk.
func LeerManifiesto(path string) (*Manifiesto, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var m Manifiesto
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// ---------------------------------------------------------------------------------------------
// Safety observation: which X servers do the recorder and its children hold connections to?
// The answer is read from the kernel (netlink sock_diag for unix sockets, /proc/net/tcp for
// TCP), so it does not depend on how any display argument was spelled.
// ---------------------------------------------------------------------------------------------

// ConexionX is one X11 client connection held by a process of the observed tree.
type ConexionX struct {
	PID     int    `json:"pid"`
	Familia string `json:"familia"` // "unix" or "tcp"
	Display int    `json:"display"`
}

// ObservacionX is one instantaneous reading of the kernel socket table for a process tree.
type ObservacionX struct {
	Raiz        int
	PIDs        []int
	SocketsUnix int
	Conexiones  []ConexionX

	padres  map[int]int    // parent pid of every member, from the snapshot the reading used
	inicios map[int]uint64 // start time of every member, from the same snapshot
}

// vivo reports whether the process identified by id was a live member of the observed tree.
func (o ObservacionX) vivo(id procID) bool {
	i, ok := o.inicios[id.PID]
	return ok && i == id.Inicio
}

// bajo reports whether pid is id itself or descends from it in the observed tree.
func (o ObservacionX) bajo(pid int, id procID) bool {
	for n := 0; n <= len(o.padres); n++ {
		if pid == id.PID {
			return o.vivo(id)
		}
		pp, ok := o.padres[pid]
		if !ok {
			return false
		}
		pid = pp
	}
	return false
}

// sockUnix is what sock_diag reports for one AF_UNIX socket.
type sockUnix struct {
	Nombre string
	Peer   uint32
	Estado uint8
}

const (
	udiagShowName = 0x01
	udiagShowPeer = 0x04
	unixDiagName  = 0
	unixDiagPeer  = 2
	tcpBaseX      = 6000
	tcpMaxX       = 6063
)

// displayDeNombre extracts the display number from a socket name such as
// "@/tmp/.X11-unix/X150" (abstract) or "/tmp/.X11-unix/X1" (pathname).
func displayDeNombre(nombre string) (int, bool) {
	m := reSocketX.FindStringSubmatch(nombre)
	if m == nil {
		return 0, false
	}
	n, err := strconv.ParseUint(m[1], 10, 31)
	if err != nil {
		return 0, false
	}
	return int(n), true
}

func peticionDiagUnix() []byte {
	req := make([]byte, 16+24)
	binary.NativeEndian.PutUint32(req[0:], uint32(len(req)))
	binary.NativeEndian.PutUint16(req[4:], unix.SOCK_DIAG_BY_FAMILY)
	binary.NativeEndian.PutUint16(req[6:], unix.NLM_F_REQUEST|unix.NLM_F_DUMP)
	binary.NativeEndian.PutUint32(req[8:], 1)
	req[16] = unix.AF_UNIX
	binary.NativeEndian.PutUint32(req[20:], 0xffffffff)
	binary.NativeEndian.PutUint32(req[28:], udiagShowName|udiagShowPeer)
	binary.NativeEndian.PutUint32(req[32:], 0xffffffff)
	binary.NativeEndian.PutUint32(req[36:], 0xffffffff)
	return req
}

// parsearDiagUnix folds one netlink datagram of sock_diag replies into dst. It reports whether
// the terminating NLMSG_DONE was seen.
func parsearDiagUnix(b []byte, dst map[uint32]sockUnix) (fin bool, err error) {
	for len(b) >= 16 {
		l := int(binary.NativeEndian.Uint32(b[0:]))
		t := binary.NativeEndian.Uint16(b[4:])
		if l < 16 || l > len(b) {
			return false, fmt.Errorf("malformed netlink message (len=%d, available=%d)", l, len(b))
		}
		payload := b[16:l]
		switch t {
		case unix.NLMSG_DONE:
			return true, nil
		case unix.NLMSG_ERROR:
			if len(payload) >= 4 {
				return false, fmt.Errorf("sock_diag rejected the request: %w", syscall.Errno(-int32(binary.NativeEndian.Uint32(payload))))
			}
			return false, errors.New("sock_diag returned an error without a code")
		default:
			if len(payload) < 16 {
				return false, fmt.Errorf("truncated unix_diag reply (%d bytes)", len(payload))
			}
			s := sockUnix{Estado: payload[2]}
			ino := binary.NativeEndian.Uint32(payload[4:])
			attrs := payload[16:]
			for len(attrs) >= 4 {
				alen := int(binary.NativeEndian.Uint16(attrs[0:]))
				atype := binary.NativeEndian.Uint16(attrs[2:])
				if alen < 4 || alen > len(attrs) {
					break
				}
				data := attrs[4:alen]
				switch atype {
				case unixDiagName:
					if len(data) > 0 && data[0] == 0 {
						s.Nombre = "@" + strings.TrimRight(string(data[1:]), "\x00")
					} else {
						s.Nombre = strings.TrimRight(string(data), "\x00")
					}
				case unixDiagPeer:
					if len(data) >= 4 {
						s.Peer = binary.NativeEndian.Uint32(data)
					}
				}
				adv := (alen + 3) &^ 3
				if adv > len(attrs) {
					break
				}
				attrs = attrs[adv:]
			}
			dst[ino] = s
		}
		adv := (l + 3) &^ 3
		if adv > len(b) {
			break
		}
		b = b[adv:]
	}
	return false, nil
}

// volcarSocketsUnix dumps every AF_UNIX socket of the caller's network namespace with its name
// and the inode of its peer. It is the same interface `ss -x` uses.
func volcarSocketsUnix() (map[uint32]sockUnix, error) {
	fd, err := unix.Socket(unix.AF_NETLINK, unix.SOCK_RAW|unix.SOCK_CLOEXEC, unix.NETLINK_SOCK_DIAG)
	if err != nil {
		return nil, fmt.Errorf("netlink sock_diag is not available: %w", err)
	}
	defer unix.Close(fd)
	tv := unix.Timeval{Sec: 3}
	if err := unix.SetsockoptTimeval(fd, unix.SOL_SOCKET, unix.SO_RCVTIMEO, &tv); err != nil {
		return nil, fmt.Errorf("SO_RCVTIMEO: %w", err)
	}
	if err := unix.Sendto(fd, peticionDiagUnix(), 0, &unix.SockaddrNetlink{Family: unix.AF_NETLINK}); err != nil {
		return nil, fmt.Errorf("sock_diag request: %w", err)
	}
	res := make(map[uint32]sockUnix, 4096)
	buf := make([]byte, 1<<17)
	for {
		n, _, err := unix.Recvfrom(fd, buf, 0)
		if err != nil {
			if errors.Is(err, unix.EINTR) {
				continue
			}
			return nil, fmt.Errorf("sock_diag read: %w", err)
		}
		fin, err := parsearDiagUnix(buf[:n], res)
		if err != nil {
			return nil, err
		}
		if fin {
			return res, nil
		}
	}
}

// entradaTCP is one non-listening TCP socket of /proc/net/tcp{,6}.
type entradaTCP struct {
	PuertoRemoto int
	Estado       string
}

// parsearTCP reads a /proc/net/tcp or /proc/net/tcp6 table, keyed by socket inode.
func parsearTCP(r io.Reader) (map[uint64]entradaTCP, error) {
	data, err := io.ReadAll(r)
	if err != nil {
		return nil, err
	}
	res := map[uint64]entradaTCP{}
	for i, linea := range strings.Split(string(data), "\n") {
		if i == 0 || strings.TrimSpace(linea) == "" {
			continue
		}
		f := strings.Fields(linea)
		if len(f) < 10 {
			continue
		}
		rem := f[2]
		j := strings.LastIndex(rem, ":")
		if j < 0 {
			continue
		}
		puerto, err := strconv.ParseUint(rem[j+1:], 16, 32)
		if err != nil {
			continue
		}
		ino, err := strconv.ParseUint(f[9], 10, 64)
		if err != nil || ino == 0 {
			continue
		}
		res[ino] = entradaTCP{PuertoRemoto: int(puerto), Estado: f[3]}
	}
	return res, nil
}

func leerTablasTCP() (map[uint64]entradaTCP, error) {
	res := map[uint64]entradaTCP{}
	for _, p := range []string{filepath.Join(procDir, "net", "tcp"), filepath.Join(procDir, "net", "tcp6")} {
		f, err := os.Open(p)
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				continue
			}
			return nil, err
		}
		t, err := parsearTCP(f)
		f.Close()
		if err != nil {
			return nil, err
		}
		for k, v := range t {
			res[k] = v
		}
	}
	return res, nil
}

// infoProc is what /proc/<pid>/stat says about a process. Inicio (field 22, clock ticks since boot)
// pairs with the pid: a pid the kernel handed to another process later has another Inicio.
type infoProc struct {
	PPid   int
	Estado byte
	Pgrp   int
	Inicio uint64
}

// procID identifies one process for as long as it lives: pid plus start time.
type procID struct {
	PID    int
	Inicio uint64
}

// leerInfo reads the stat of a process; ok is false when it vanished.
func leerInfo(pid int) (infoProc, bool) {
	b, err := os.ReadFile(filepath.Join(procDir, strconv.Itoa(pid), "stat"))
	if err != nil {
		return infoProc{}, false
	}
	s := string(b)
	i := strings.LastIndex(s, ")")
	if i < 0 || i+2 >= len(s) {
		return infoProc{}, false
	}
	f := strings.Fields(s[i+1:]) // f[0] is field 3 (state); starttime, field 22, is f[19]
	if len(f) < 2 || len(f[0]) == 0 {
		return infoProc{}, false
	}
	pp, err := strconv.Atoi(f[1])
	if err != nil {
		return infoProc{}, false
	}
	inf := infoProc{PPid: pp, Estado: f[0][0]}
	if len(f) > 2 {
		inf.Pgrp, _ = strconv.Atoi(f[2])
	}
	if len(f) > 19 {
		inf.Inicio, _ = strconv.ParseUint(f[19], 10, 64)
	}
	return inf, true
}

// instantanea reads the stat of every process once.
func instantanea() (map[int]infoProc, error) {
	ents, err := os.ReadDir(procDir)
	if err != nil {
		return nil, err
	}
	snap := make(map[int]infoProc, len(ents))
	for _, e := range ents {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		if inf, ok := leerInfo(pid); ok {
			snap[pid] = inf
		}
	}
	return snap, nil
}

// miembros returns raiz and everything below it in the snapshot, leaving out the excluded
// processes (the campaign's own read-only witness is not a recorder) with their subtrees and their
// process groups. The walk is by parent pid; membership survives re-parenting because the observing
// process is a child subreaper (activarSubreaper), so an orphaned descendant is adopted by it and
// stays reachable. An exclusion whose pid now carries another start time is ignored: the pid was
// reused by an unrelated process. A dead group leader still excludes its group: the kernel does not
// reuse a pid while it names a live process group.
func miembros(snap map[int]infoProc, raiz int, excluir []procID) []int {
	fuera := map[int]bool{}
	grupos := map[int]bool{}
	for _, x := range excluir {
		if inf, vive := snap[x.PID]; vive && x.Inicio != 0 && inf.Inicio != x.Inicio {
			continue
		}
		fuera[x.PID] = true
		grupos[x.PID] = true
	}
	hijos := map[int][]int{}
	for pid, inf := range snap {
		hijos[inf.PPid] = append(hijos[inf.PPid], pid)
		if grupos[inf.Pgrp] {
			fuera[pid] = true
		}
	}
	visto := map[int]bool{raiz: true}
	cola := []int{raiz}
	var orden []int
	for len(cola) > 0 {
		p := cola[0]
		cola = cola[1:]
		if inf, ok := snap[p]; ok && inf.Estado != 'Z' && inf.Estado != 'X' {
			orden = append(orden, p)
		}
		for _, h := range hijos[p] {
			if !visto[h] && !fuera[h] {
				visto[h] = true
				cola = append(cola, h)
			}
		}
	}
	sort.Ints(orden)
	return orden
}

// arbolDeProcesos returns raiz and all its live descendants, leaving out the subtrees rooted at
// the excluded pids.
func arbolDeProcesos(raiz int, excluir []int) ([]int, error) {
	snap, err := instantanea()
	if err != nil {
		return nil, err
	}
	return miembros(snap, raiz, identidades(snap, excluir)), nil
}

// identidades pairs pids with the start time they have now.
func identidades(snap map[int]infoProc, pids []int) []procID {
	ids := make([]procID, len(pids))
	for i, p := range pids {
		ids[i] = procID{PID: p, Inicio: snap[p].Inicio}
	}
	return ids
}

// procDir is where the process table lives; only tests point it elsewhere.
var procDir = "/proc"

// pausaReintento is the pause between two reads of a process that is briefly unreadable; tests
// replace it.
var pausaReintento = func() { time.Sleep(3 * time.Millisecond) }

// inodosDeSockets lists the socket inodes a process holds. A process caught between fork and exec is
// briefly unreadable, so a permission error retries the whole scan; a process that ended is not
// counted; a process that still exists and stays unreadable is an error, because the tree could then
// hide a connection and the observer must not answer "none" for it.
func inodosDeSockets(pid int) ([]uint64, error) {
	var err error
	for intento := 0; intento < 5; intento++ {
		var inos []uint64
		inos, err = leerSockets(pid)
		if err == nil {
			return inos, nil
		}
		if errors.Is(err, fs.ErrNotExist) || errors.Is(err, syscall.ESRCH) {
			return nil, nil // the process ended
		}
		if !errors.Is(err, fs.ErrPermission) {
			return nil, err
		}
		pausaReintento()
	}
	if _, ok := leerInfo(pid); !ok {
		return nil, nil // it ended while we were retrying
	}
	return nil, err
}

// leerSockets makes one pass over the descriptors of a process.
func leerSockets(pid int) ([]uint64, error) {
	dir := filepath.Join(procDir, strconv.Itoa(pid), "fd")
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var out []uint64
	for _, e := range ents {
		l, err := os.Readlink(filepath.Join(dir, e.Name()))
		if err != nil {
			if errors.Is(err, fs.ErrNotExist) {
				continue // the descriptor was closed meanwhile
			}
			return nil, err
		}
		if strings.HasPrefix(l, "socket:[") && strings.HasSuffix(l, "]") {
			if n, err := strconv.ParseUint(l[len("socket:["):len(l)-1], 10, 64); err == nil {
				out = append(out, n)
			}
		}
	}
	return out, nil
}

// ObservarConexionesX reads the kernel socket table and returns every X11 client connection held
// right now by raiz and its descendants (minus the excluded subtrees). A unix socket counts when
// its PEER socket carries an X11 name, so the servers' own accepted sockets are never mistaken
// for clients; a TCP socket counts when it is connected to a port in 6000..6063 and is not
// listening. A descendant that was re-parented is still a member as long as raiz is a child
// subreaper (see activarSubreaper); a pid is only attributed to the process it was when the
// process table was read (start time), never to a later process that reused the number.
func ObservarConexionesX(raiz int, excluir ...int) (ObservacionX, error) {
	snap, err := instantanea()
	if err != nil {
		return ObservacionX{Raiz: raiz}, fmt.Errorf("process tree: %w", err)
	}
	return observar(snap, raiz, identidades(snap, excluir))
}

func observar(snap map[int]infoProc, raiz int, excluir []procID) (ObservacionX, error) {
	obs := ObservacionX{Raiz: raiz, padres: map[int]int{}, inicios: map[int]uint64{}}
	pids := miembros(snap, raiz, excluir)
	obs.PIDs = pids
	for _, pid := range pids {
		obs.padres[pid] = snap[pid].PPid
		obs.inicios[pid] = snap[pid].Inicio
	}
	trasLaInstantanea()
	dueno := map[uint64]int{}
	for _, pid := range pids {
		inos, err := inodosDeSockets(pid)
		if err != nil {
			return obs, fmt.Errorf("descriptors of pid %d: %w", pid, err)
		}
		if ahora, ok := leerInfo(pid); !ok || ahora.Inicio != snap[pid].Inicio {
			continue // it ended, or the number now belongs to another process: not a member
		}
		for _, ino := range inos {
			if _, ya := dueno[ino]; !ya {
				dueno[ino] = pid
			}
		}
	}
	diag, err := volcarSocketsUnix()
	if err != nil {
		return obs, err
	}
	tcp, err := leerTablasTCP()
	if err != nil {
		return obs, fmt.Errorf("tcp tables: %w", err)
	}
	for ino, pid := range dueno {
		if s, ok := diag[uint32(ino)]; ok {
			obs.SocketsUnix++
			if peer, ok := diag[s.Peer]; ok && s.Peer != 0 {
				if n, ok := displayDeNombre(peer.Nombre); ok {
					obs.Conexiones = append(obs.Conexiones, ConexionX{PID: pid, Familia: "unix", Display: n})
				}
			}
			continue
		}
		if t, ok := tcp[ino]; ok && t.Estado != "0A" && t.PuertoRemoto >= tcpBaseX && t.PuertoRemoto <= tcpMaxX {
			obs.Conexiones = append(obs.Conexiones, ConexionX{PID: pid, Familia: "tcp", Display: t.PuertoRemoto - tcpBaseX})
		}
	}
	sort.Slice(obs.Conexiones, func(i, j int) bool {
		a, b := obs.Conexiones[i], obs.Conexiones[j]
		if a.PID != b.PID {
			return a.PID < b.PID
		}
		if a.Familia != b.Familia {
			return a.Familia < b.Familia
		}
		return a.Display < b.Display
	})
	return obs, nil
}

// minMuestrasGrabador is how many samples must have seen the recorder itself alive before the
// safety field may be false.
const minMuestrasGrabador = 3

// muestreador accumulates kernel observations over a session. It is used from one goroutine.
//
// A connection is foreign when it goes to a display other than the declared nested one, AND
// whenever it goes to a display of the operator (:0, :1, the ambient $DISPLAY) even if that is the
// declared one: the observer must not depend on the guard having refused the spelling.
type muestreador struct {
	objetivo int
	host     map[int]bool
	excluir  []procID
	grabador procID // the recorder (pid and start time); zero until it starts

	sinSubreaper error // set when the process could not become a child subreaper

	muestras          int
	errores           int
	ultimoError       error
	grabadorVivo      int // samples in which the recorder itself was a live member of the tree
	grabadorConectado int // samples in which the recorder's own subtree held a connection to the nested display
	maxConexiones     int
	displays          map[int]bool
	foraneas          map[ConexionX]bool
}

func nuevoMuestreador(objetivo int) *muestreador {
	return &muestreador{objetivo: objetivo, host: numerosDisplayHost(), displays: map[int]bool{}, foraneas: map[ConexionX]bool{}}
}

// excluirPID leaves the subtree and the process group of pid out of every later observation. The
// exclusion is tied to the start time pid has now.
func (m *muestreador) excluirPID(pid int) {
	inf, _ := leerInfo(pid)
	m.excluir = append(m.excluir, procID{PID: pid, Inicio: inf.Inicio})
}

// registrarGrabador declares the recorder process the observer must find alive and connected.
func (m *muestreador) registrarGrabador(pid int) {
	if inf, ok := leerInfo(pid); ok {
		m.grabador = procID{PID: pid, Inicio: inf.Inicio}
	}
}

// muestrear reads the kernel and folds the reading in. Orphans the process adopted that already
// exited are reaped after the reading.
func (m *muestreador) muestrear() {
	snap, err := instantanea()
	var obs ObservacionX
	if err == nil {
		obs, err = lecturaX(snap, os.Getpid(), m.excluir)
	}
	if err == nil {
		recogerDe(snap)
	}
	if err != nil {
		m.errores++
		m.ultimoError = err
		return
	}
	m.registrar(obs)
}

// registrar folds one observation into the accumulated state.
func (m *muestreador) registrar(obs ObservacionX) {
	m.muestras++
	if m.grabador.PID != 0 && obs.vivo(m.grabador) {
		m.grabadorVivo++
	}
	propia := false
	for _, c := range obs.Conexiones {
		m.displays[c.Display] = true
		if c.Display == m.objetivo && !m.host[c.Display] {
			// Only the recorder's own connection proves the observer can see the recorder; the
			// supervisor's connection (its test window) is in the tree as well and proves nothing.
			if m.grabador.PID != 0 && obs.bajo(c.PID, m.grabador) {
				propia = true
			}
		} else {
			m.foraneas[c] = true
		}
	}
	if propia {
		m.grabadorConectado++
	}
	if len(obs.Conexiones) > m.maxConexiones {
		m.maxConexiones = len(obs.Conexiones)
	}
}

// decidir turns the accumulated observations into the value of display_host_tocado.
//
//	true  - some sample saw a process of the session connected to an X server other than the
//	        nested display (the operator's display included, under any spelling);
//	false - samples were taken, none failed, none saw a foreign connection, the recorder itself was
//	        observed alive in at least minMuestrasGrabador samples, and some sample saw the
//	        recorder's own connection to the nested display (the observer can see the recorder);
//	nil   - unknown: the observer could not measure, or it never watched the recorder long enough.
//	        A connection shorter than the sampling period can be missed, so only a recorder that was
//	        seen alive and connected turns "none seen" into false.
func (m *muestreador) decidir() (*bool, string) {
	t, f := true, false
	if len(m.foraneas) > 0 {
		return &t, ""
	}
	if m.errores > 0 {
		return nil, fmt.Sprintf("observador_no_disponible: %d failed samples (%v)", m.errores, m.ultimoError)
	}
	if m.muestras == 0 {
		return nil, "observador_sin_muestras"
	}
	if m.sinSubreaper != nil {
		return nil, fmt.Sprintf("membresia_no_garantizada: the process is not a child subreaper (%v)", m.sinSubreaper)
	}
	if m.grabadorVivo < minMuestrasGrabador {
		return nil, fmt.Sprintf("grabador_no_observado: the recorder was alive in %d of %d samples (at least %d needed)", m.grabadorVivo, m.muestras, minMuestrasGrabador)
	}
	if m.grabadorConectado == 0 {
		return nil, "observador_ciego: no sample saw the recorder's own X connection to the nested display"
	}
	return &f, ""
}

func (m *muestreador) listaDisplays() []int {
	out := make([]int, 0, len(m.displays))
	for d := range m.displays {
		out = append(out, d)
	}
	sort.Ints(out)
	return out
}

func (m *muestreador) listaForaneas() []ConexionX {
	out := make([]ConexionX, 0, len(m.foraneas))
	for c := range m.foraneas {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Display != out[j].Display {
			return out[i].Display < out[j].Display
		}
		return out[i].PID < out[j].PID
	})
	return out
}

// ---------------------------------------------------------------------------------------------
// Recording session
// ---------------------------------------------------------------------------------------------

// SesionOpciones configures a surface recording session.
type SesionOpciones struct {
	Display        string
	Raiz           string
	Titulo         string
	Segundos       int
	XID            uint64
	Tamano         string // recorded area, default 1024x768
	TestigoAlcance string // external witness script, default TestigoAlcance
	// Parar, when non-nil, ends the recording cleanly when it is closed (the operator's stop from the cockpit,
	// cockpit/grabacion.go): the session is NOT cancelled, ffmpeg gets its SIGINT and the manifest can reach
	// "completa". With Parar set, Segundos <= 0 means "until Parar closes". A nil channel never fires.
	Parar <-chan struct{}
	// AlAbrir, when set, is called once at t0, as soon as the session directory exists and before ffmpeg starts,
	// so a supervisor learns the session id without waiting for the session to end.
	AlAbrir func(sessionID, dir string)

	// Seams for tests. Production code never sets them.
	entradaFFmpeg  string   // replaces the validated display as ffmpeg's input
	entornoTestigo []string // extra environment entries for the witness
}

// SesionResultado captures the observed measurements of a completed or failed recording session.
// It is produced on every path, failing ones included.
type SesionResultado struct {
	OK                  bool     `json:"ok"`
	SessionID           string   `json:"session_id"`
	Dir                 string   `json:"dir"`
	ArtefactosEnT0      int      `json:"artefactos_en_t0"`
	ArtefactosEnT0Lista []string `json:"artefactos_en_t0_lista"`
	ManifiestoEstado    string   `json:"manifiesto_estado"`
	// DisplayHostTocado is the safety field, a tri-state read from the kernel socket table (see
	// muestreador.decidir): true when a process of the session held a connection to an X server other
	// than the nested display or to any display of the operator; false ONLY when, in addition, the
	// recorder itself was observed alive in at least three samples and its own connection to the
	// nested display was seen; null (unknown) otherwise.
	DisplayHostTocado   *bool       `json:"display_host_tocado"`
	Display             string      `json:"display"`
	DisplaySolicitado   string      `json:"display_solicitado"`
	DuracionMS          int64       `json:"duracion_ms"`
	VideoBytes          int64       `json:"video_bytes"`
	EventosLineas       int         `json:"eventos_lineas"`
	XID                 string      `json:"xid,omitempty"`
	XIDAsignado         string      `json:"xid_asignado,omitempty"`
	XIDCoincide         *bool       `json:"xid_coincide,omitempty"`
	MuestrasX           int         `json:"muestras_x"`
	ConexionesXMax      int         `json:"conexiones_x_max"`
	DisplaysXObservados []int       `json:"displays_x_observados"`
	ConexionesForaneas  []ConexionX `json:"conexiones_x_foraneas"`
	// MuestrasGrabadorVivo and MuestrasGrabadorConectado are the samples in which the recorder itself
	// was seen alive, and seen holding its own connection to the nested display. display_host_tocado
	// is only false when the first is at least minMuestrasGrabador and the second is at least 1.
	MuestrasGrabadorVivo      int `json:"muestras_grabador_vivo"`
	MuestrasGrabadorConectado int `json:"muestras_grabador_conectado"`
	// Video verification: ffmpeg decodes the recorded file (see verificarVideo).
	VideoFotogramas int     `json:"video_fotogramas"`
	VideoDuracionS  float64 `json:"video_duracion_s"`
	VideoValidacion string  `json:"video_validacion"`
	// DiffAlcanceLineas is the number of lines the scope witness printed; null when the witness did
	// not run, failed, or was blind (TestigoCiego). TestigoAlcance is set only when the witness was
	// started. TestigoDisplay is the display the witness reads and the canary probed.
	DiffAlcanceLineas  *int   `json:"diff_alcance_lineas"`
	TestigoAlcance     string `json:"testigo_alcance"`
	TestigoDisplay     string `json:"testigo_display"`
	TestigoCiego       bool   `json:"testigo_ciego"`
	TestigoCiegoMotivo string `json:"testigo_ciego_motivo,omitempty"`
	Error              string `json:"error,omitempty"`
}

func (r *SesionResultado) addError(msg string) {
	if msg == "" {
		return
	}
	if r.Error != "" {
		r.Error += "; "
	}
	r.Error += msg
}

// eventoLinea is one line of eventos.jsonl.
type eventoLinea struct {
	SchemaVersion string         `json:"schema_version"`
	SessionID     string         `json:"session_id"`
	Sequence      int            `json:"sequence"`
	EventID       string         `json:"event_id"`
	EventType     string         `json:"event_type"`
	Source        string         `json:"source"`
	MonotonicNS   int64          `json:"monotonic_ns"`
	WallTime      string         `json:"wall_time"`
	Payload       map[string]any `json:"payload"`
}

func monotonicNS() int64 {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts); err != nil {
		return time.Now().UnixNano()
	}
	return ts.Nano()
}

var reNombreSeguro = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// sesion holds the state of one EjecutarSesion call so that a single finalizer can run on every path.
type sesion struct {
	opts        SesionOpciones
	disp        DisplayX
	res         *SesionResultado
	mu          *muestreador
	tInicio     time.Time
	dirCreada   bool
	dir         string
	pidPath     string
	eventos     string
	flujo       string
	manifPath   string
	video       string
	fEventos    *os.File
	seq         int
	manifiesto  *Manifiesto
	ffmpeg      *proceso
	testigo     *proceso
	cuenta      *contadorLineas
	testigoRC   error
	ctx         context.Context
	cancelada   bool
	ffmpegMurio bool
	forzado     bool
	errEventos  error
}

func (s *sesion) evento(tipo string, payload map[string]any) {
	if s.fEventos == nil {
		return
	}
	now := time.Now()
	l := eventoLinea{
		SchemaVersion: "1.0.0", SessionID: s.res.SessionID, Sequence: s.seq,
		EventID: fmt.Sprintf("ev-%04d", s.seq), EventType: tipo, Source: "grabador",
		MonotonicNS: monotonicNS(), WallTime: now.UTC().Format(time.RFC3339), Payload: payload,
	}
	b, err := json.Marshal(l)
	if err == nil {
		_, err = s.fEventos.Write(append(b, '\n'))
	}
	if err == nil {
		err = s.fEventos.Sync()
	}
	if err != nil && s.errEventos == nil {
		s.errEventos = err
	}
	s.seq++
}

// EjecutarSesion executes a surface recording session on the given (nested) display. It writes
// the four artifacts at t0, records with ffmpeg through the launcher, samples the kernel socket
// table for foreign X connections, runs the campaign's external witness during the recording, and
// transitions the manifest from "incompleta" to "completa" only when the artifacts are verified on
// disk. The returned result is never nil; it is complete on failing paths as well, and the error
// is non-nil exactly when the session is not OK.
func EjecutarSesion(ctx context.Context, opts SesionOpciones) (*SesionResultado, error) {
	if opts.Raiz == "" {
		opts.Raiz = "videos/srf-ng"
	}
	if opts.Segundos <= 0 && opts.Parar == nil {
		opts.Segundos = 3
	}
	if opts.Titulo == "" {
		opts.Titulo = "surface-recording-session"
	}
	if opts.Tamano == "" {
		opts.Tamano = "1024x768"
	}
	if opts.TestigoAlcance == "" {
		opts.TestigoAlcance = TestigoAlcance
	}
	s := &sesion{
		opts: opts,
		res: &SesionResultado{
			DisplaySolicitado: opts.Display, ManifiestoEstado: "ausente",
			ArtefactosEnT0Lista: []string{}, DisplaysXObservados: []int{}, ConexionesForaneas: []ConexionX{},
		},
		seq: 1,
		ctx: ctx,
	}
	errCorrer := s.correr(ctx)
	return s.cerrar(errCorrer)
}

func (s *sesion) correr(ctx context.Context) error {
	disp, err := ValidarDisplayAnidado(s.opts.Display)
	if err != nil {
		return err
	}
	s.disp = disp
	s.res.Display = disp.Canonico()
	s.mu = nuevoMuestreador(disp.Numero)
	s.mu.sinSubreaper = activarSubreaper() // before the first spawn: the recorder's orphans must come to us
	if s.opts.XID != 0 {
		s.res.XID = Hex(s.opts.XID)
	}

	s.tInicio = time.Now()
	base := s.tInicio.Format("20060102-150405") + "-" + strings.Trim(reNombreSeguro.ReplaceAllString(s.opts.Titulo, "-"), ".-")
	if err := os.MkdirAll(s.opts.Raiz, 0755); err != nil {
		return fmt.Errorf("error creating the sessions root: %w", err)
	}
	for i := 0; ; i++ {
		id := base
		if i > 0 {
			id = fmt.Sprintf("%s-%d", base, i+1)
		}
		dir := filepath.Join(s.opts.Raiz, id)
		err := os.Mkdir(dir, 0755)
		if err == nil {
			s.res.SessionID, s.dir, s.dirCreada = id, dir, true
			break
		}
		if !errors.Is(err, fs.ErrExist) || i > 99 {
			return fmt.Errorf("error creating the session directory: %w", err)
		}
	}
	s.res.Dir = s.dir
	if s.opts.AlAbrir != nil {
		s.opts.AlAbrir(s.res.SessionID, s.dir)
	}

	// supervisor.pid names a live process only while the session runs; cerrar removes it.
	s.pidPath = filepath.Join(s.dir, FicheroPID)
	if err := os.WriteFile(s.pidPath, []byte(fmt.Sprintf("%d\n", os.Getpid())), 0644); err != nil {
		return fmt.Errorf("failed writing supervisor.pid: %w", err)
	}

	s.eventos = filepath.Join(s.dir, FicheroEventos)
	s.fEventos, err = os.OpenFile(s.eventos, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0644)
	if err != nil {
		return fmt.Errorf("failed initializing eventos.jsonl: %w", err)
	}
	s.evento("session.open", map[string]any{"display": s.res.Display, "xid": Hex(s.opts.XID), "titulo": s.opts.Titulo})
	if s.errEventos != nil {
		return fmt.Errorf("failed writing eventos.jsonl: %w", s.errEventos)
	}

	s.flujo = filepath.Join(s.dir, FicheroFlujo)
	flujo := fmt.Sprintf(PlantillaFlujo, s.res.SessionID, s.tInicio.UTC().Format(time.RFC3339), s.res.Display)
	if err := os.WriteFile(s.flujo, []byte(flujo), 0644); err != nil {
		return fmt.Errorf("failed initializing FLUJO.md: %w", err)
	}

	s.manifPath = filepath.Join(s.dir, FicheroManifiesto)
	s.manifiesto = &Manifiesto{
		SchemaVersion: "1.0.0", SessionID: s.res.SessionID, Creado: s.tInicio.UTC().Format(time.RFC3339),
		Estado: "incompleta", RazonIncompleta: "session IN PROGRESS: this manifest is completed on stop",
		Display:    s.res.Display,
		Objetivo:   ObjetivoInfo{XID: Hex(s.opts.XID), Titulo: s.opts.Titulo},
		Artefactos: CuatroArtefactos,
		Eventos:    EventosInfo{Fichero: FicheroEventos, NLineas: 1},
		Video:      VideoInfo{Fichero: FicheroVideo},
	}
	if err := EscribirManifiestoAtomico(s.manifPath, s.manifiesto); err != nil {
		return fmt.Errorf("failed writing the provisional manifest: %w", err)
	}

	// ffmpeg records the canonical nested display, never the caller's spelling.
	entrada := s.res.Display
	if s.opts.entradaFFmpeg != "" {
		entrada = s.opts.entradaFFmpeg
	}
	s.video = filepath.Join(s.dir, FicheroVideo)
	cmd, err := crearComando(ctx, "ffmpeg",
		"-hide_banner", "-nostdin", "-loglevel", "warning",
		"-f", "x11grab", "-draw_mouse", "0", "-framerate", "15", "-video_size", s.opts.Tamano,
		"-i", entrada,
		"-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
		"-y", s.video)
	if err != nil {
		return fmt.Errorf("launcher rejected ffmpeg: %w", err)
	}
	cmd.Env = []string{"DISPLAY=" + entrada, "PATH=" + os.Getenv("PATH")}
	s.ffmpeg, err = iniciar(cmd, syscall.SIGINT, 3*time.Second, nil)
	if err != nil {
		return fmt.Errorf("error starting ffmpeg: %w", err)
	}
	s.mu.registrarGrabador(s.ffmpeg.pid)
	s.mu.muestrear()

	// Wait for the video file to appear; sample the socket table on the way so that a child
	// that touches a foreign display and dies at once is still seen.
	esperaFin := time.Now().Add(2 * time.Second)
	for i := 0; time.Now().Before(esperaFin) && s.ffmpeg.vivo(); i++ {
		if fi, err := os.Stat(s.video); err == nil && !fi.IsDir() {
			break
		}
		if i%2 == 0 {
			s.mu.muestrear()
		}
		time.Sleep(50 * time.Millisecond)
	}

	// --- AT T0: the artifacts that exist on disk, counted by os.Stat ---
	s.res.ArtefactosEnT0, s.res.ArtefactosEnT0Lista = ContarArtefactosEnDisco(s.dir)
	if s.res.ArtefactosEnT0Lista == nil {
		s.res.ArtefactosEnT0Lista = []string{}
	}
	s.mu.muestrear()

	// The external witness runs while ffmpeg records. Its subtree is excluded from the sampling:
	// it reads the operator's display on purpose and is not a recorder.
	s.iniciarTestigo(ctx)

	// Segundos <= 0 (only with Parar set): no timer, the recording lasts until the operator's stop.
	var finC <-chan time.Time
	if s.opts.Segundos > 0 {
		fin := time.NewTimer(time.Duration(s.opts.Segundos) * time.Second)
		defer fin.Stop()
		finC = fin.C
	}
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	muestra := time.NewTicker(100 * time.Millisecond) // the safety observation is sampled every 100 ms
	defer muestra.Stop()
grabando:
	for {
		select {
		case <-ctx.Done():
			s.cancelada = true
			break grabando
		case <-s.ffmpeg.done:
			// A cancelled context stops ffmpeg through its own Cancel; that is a cancellation,
			// not a crash.
			if ctx.Err() != nil {
				s.cancelada = true
			} else {
				s.ffmpegMurio = true
			}
			break grabando
		case <-finC:
			break grabando
		case <-s.opts.Parar:
			// the operator's clean stop: same path as the timer, the manifest may still be "completa"
			s.evento("recording.stop", map[string]any{"origen": "operador", "duracion_ms": time.Since(s.tInicio).Milliseconds()})
			break grabando
		case <-muestra.C:
			s.mu.muestrear()
		case <-tick.C:
			s.evento("recording.tick", map[string]any{"duracion_ms": time.Since(s.tInicio).Milliseconds()})
		}
	}
	s.mu.muestrear()
	return nil
}

// entornoTestigo is the environment the witness runs in (and the canary probes with): the caller's,
// without SRF_DISPLAY, plus the test seam. The witness reads $SRF_DISPLAY, ":1" when unset.
func (s *sesion) entornoTestigo() []string {
	env := make([]string, 0, len(os.Environ())+len(s.opts.entornoTestigo))
	for _, e := range os.Environ() {
		if !strings.HasPrefix(e, "SRF_DISPLAY=") {
			env = append(env, e)
		}
	}
	return append(env, s.opts.entornoTestigo...)
}

// displayDelTestigo is the display the witness script reads: the last SRF_DISPLAY entry of env, or
// ":1" like the script's own default.
func displayDelTestigo(env []string) string {
	d := ""
	for _, e := range env {
		if v, ok := strings.CutPrefix(e, "SRF_DISPLAY="); ok {
			d = v
		}
	}
	if d == "" {
		d = ":1"
	}
	return d
}

// reSinDisplay is what an X client says when it cannot reach its display.
var reSinDisplay = regexp.MustCompile(`(?i)(unable to|cannot|can.t|could not|couldn.t) open display`)

// sondearDisplay is the liveness canary of the witness: the same read-only root query the witness
// makes (xwininfo -root -tree on the display it watches). Nothing is mapped, moved or drawn. The
// witness script swallows a failure of that query and prints nothing, which is indistinguishable
// from a clean display; this probe tells the two apart.
func sondearDisplay(ctx context.Context, display string, env []string) error {
	out, _, err := ejecutar(ctx, 15*time.Second, env, "xwininfo", "-display", display, "-root", "-tree")
	if err != nil {
		return err
	}
	if !strings.Contains(out, "Root window id:") {
		return fmt.Errorf("xwininfo on %s did not describe a root window", display)
	}
	return nil
}

// sondearTestigo runs the canary with the witness's own environment. momento says when.
func (s *sesion) sondearTestigo(momento string) error {
	env := s.entornoTestigo()
	display := displayDelTestigo(env)
	s.res.TestigoDisplay = display
	ctx := context.WithoutCancel(s.ctx) // a cancelled session must not be blamed on the witness
	if err := sondaTestigo(ctx, display, env); err != nil {
		return fmt.Errorf("%s: %w", momento, err)
	}
	return nil
}

// ciegoTestigo records that the witness could not have seen anything: its count is unknown.
func (s *sesion) ciegoTestigo(err error) {
	s.res.TestigoCiego = true
	s.res.TestigoCiegoMotivo = fmt.Sprintf("the witness display %s was not readable (%v)", s.res.TestigoDisplay, err)
	s.testigoRC = errors.New("witness blind: " + s.res.TestigoCiegoMotivo)
	s.res.DiffAlcanceLineas = nil
}

func (s *sesion) iniciarTestigo(ctx context.Context) {
	ruta := s.opts.TestigoAlcance
	if _, err := os.Stat(ruta); err != nil {
		s.testigoRC = fmt.Errorf("witness missing: %w", err)
		return
	}
	// Canary, before: a witness that cannot read its display prints nothing and exits 0.
	if err := s.sondearTestigo("before the session"); err != nil {
		s.ciegoTestigo(err)
		return
	}
	cmd, err := crearComando(ctx, "bash", ruta, "diff")
	if err != nil {
		s.testigoRC = fmt.Errorf("launcher rejected the witness: %w", err)
		return
	}
	cmd.Env = s.entornoTestigo()
	// Its own process group: whatever the witness leaves behind after it exits (an orphan the
	// subreaper adopts) stays excluded from the recorder observation.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	s.cuenta = &contadorLineas{}
	p, err := iniciar(cmd, syscall.SIGTERM, 2*time.Second, s.cuenta)
	if err != nil {
		s.testigoRC = fmt.Errorf("error starting the witness: %w", err)
		return
	}
	s.testigo = p
	s.res.TestigoAlcance = ruta // set only now that the witness has started
	s.mu.excluirPID(p.pid)
}

// cerrar runs on every path: it stops the children, closes the artifacts, re-reads the manifest
// from disk, collects the witness and decides every reported field.
func (s *sesion) cerrar(errCorrer error) (*SesionResultado, error) {
	res := s.res
	if errCorrer != nil {
		res.addError(errCorrer.Error())
	}

	// 1. Recorder: one last observation, then SIGINT so ffmpeg finalizes the mp4.
	if s.ffmpeg != nil {
		if s.ffmpeg.vivo() && s.mu != nil {
			s.mu.muestrear()
		}
		forzado, err := s.ffmpeg.detener(syscall.SIGINT, 3*time.Second)
		s.forzado = forzado
		if err != nil {
			res.addError(err.Error())
		}
		if forzado {
			res.addError("ffmpeg did not exit on SIGINT and was forced")
		}
		if s.ffmpegMurio {
			res.addError(fmt.Sprintf("ffmpeg exited early: %v %s", s.ffmpeg.err, s.ffmpeg.stderr.String()))
		}
	}
	if s.cancelada {
		res.addError("session cancelled by the context")
	}

	// 2. Witness: wait for it, then take its line count.
	if s.testigo != nil {
		select {
		case <-s.testigo.done:
		case <-time.After(30 * time.Second):
			_, _ = s.testigo.detener(syscall.SIGTERM, time.Second)
		}
		switch {
		case s.testigo.vivo():
			s.testigoRC = errors.New("the witness did not exit")
		case s.testigo.err != nil:
			s.testigoRC = fmt.Errorf("the witness exited with an error: %v %s", s.testigo.err, s.testigo.stderr.String())
		default:
			n := s.cuenta.Lineas()
			res.DiffAlcanceLineas = &n
			// Canary, after: the witness display must still answer, or its silence proves nothing.
			// Second source: the witness's own stderr. The probe asks the display the witness is
			// expected to read; the witness says which one it really failed to open.
			if err := s.sondearTestigo("after the session"); err != nil {
				s.ciegoTestigo(err)
			} else if m := reSinDisplay.FindString(s.testigo.stderr.String()); m != "" {
				s.ciegoTestigo(fmt.Errorf("the witness reported %q", m))
			}
		}
	}
	if s.testigoRC != nil {
		res.addError(s.testigoRC.Error())
	}
	if res.DiffAlcanceLineas != nil && *res.DiffAlcanceLineas != 0 {
		res.addError(fmt.Sprintf("the scope witness printed %d lines: %s", *res.DiffAlcanceLineas, s.cuenta.Cabeza()))
	}

	// 3. Artifacts on disk.
	if s.dirCreada {
		s.cerrarArtefactos()
	}

	// 4. The safety field, from the kernel observations.
	if s.mu == nil { // refused before any session state existed: observe the bare process
		s.mu = nuevoMuestreador(-1)
		s.mu.muestrear()
	}
	if s.mu.muestras == 0 && s.mu.errores == 0 {
		s.mu.muestrear()
	}
	tocado, motivo := s.mu.decidir()
	res.DisplayHostTocado = tocado
	res.addError(motivo)
	if tocado != nil && *tocado {
		res.addError(fmt.Sprintf("the session tree held X connections to foreign displays: %v", s.mu.listaForaneas()))
	}
	res.MuestrasX = s.mu.muestras
	res.MuestrasGrabadorVivo = s.mu.grabadorVivo
	res.MuestrasGrabadorConectado = s.mu.grabadorConectado
	res.ConexionesXMax = s.mu.maxConexiones
	res.DisplaysXObservados = s.mu.listaDisplays()
	res.ConexionesForaneas = s.mu.listaForaneas()

	recogerAdoptados()
	res.OK = res.ManifiestoEstado == "completa" && tocado != nil && !*tocado &&
		res.DiffAlcanceLineas != nil && *res.DiffAlcanceLineas == 0 && res.Error == ""
	if !res.OK {
		if res.Error == "" {
			res.Error = "invalid session"
		}
		return res, errors.New(res.Error)
	}
	return res, nil
}

// ValidacionVideo is what decoding the recorded file showed.
type ValidacionVideo struct {
	Fotogramas int
	DuracionS  float64
}

var (
	reDuracion  = regexp.MustCompile(`Duration: ([0-9]+):([0-9]+):([0-9]+(?:\.[0-9]+)?)`)
	reFotograma = regexp.MustCompile(`(?m)^frame=([0-9]+)$`)
)

// verificarVideo decides whether the recording is playable: ffmpeg opens the file (a truncated mp4
// has no moov atom and is refused), maps its first video stream, decodes every frame, and the
// container declares a positive duration. The check goes through the launcher's ffmpeg: ffprobe,
// which ships with it, is not on the launcher's allowlist. Decoding is stricter than reading the
// header: a file whose index is intact but whose frames are not still fails.
func verificarVideo(ctx context.Context, ruta string) (ValidacionVideo, error) {
	var v ValidacionVideo
	env := []string{"PATH=" + os.Getenv("PATH")}
	out, errOut, err := ejecutar(ctx, 60*time.Second, env, "ffmpeg",
		"-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-progress", "pipe:1",
		"-i", ruta, "-map", "0:v:0", "-f", "null", "-")
	if err != nil {
		return v, fmt.Errorf("ffmpeg cannot decode a video stream from %s: %w", filepath.Base(ruta), err)
	}
	if m := reFotograma.FindAllStringSubmatch(out, -1); len(m) > 0 {
		v.Fotogramas, _ = strconv.Atoi(m[len(m)-1][1])
	}
	if m := reDuracion.FindStringSubmatch(errOut); m != nil {
		h, _ := strconv.Atoi(m[1])
		min, _ := strconv.Atoi(m[2])
		seg, _ := strconv.ParseFloat(m[3], 64)
		v.DuracionS = float64(h*3600+min*60) + seg
	}
	if v.Fotogramas < 1 {
		return v, fmt.Errorf("the video stream of %s decoded %d frames", filepath.Base(ruta), v.Fotogramas)
	}
	if v.DuracionS <= 0 {
		return v, fmt.Errorf("%s declares no positive duration (%v s)", filepath.Base(ruta), v.DuracionS)
	}
	return v, nil
}

// cerrarArtefactos finishes eventos.jsonl and FLUJO.md, decides and writes the final manifest,
// re-reads it from disk, and removes supervisor.pid.
func (s *sesion) cerrarArtefactos() {
	res := s.res
	duracion := time.Since(s.tInicio).Milliseconds()
	res.DuracionMS = duracion

	s.evento("session.close", map[string]any{"duracion_ms": duracion})
	if s.fEventos != nil {
		if err := s.fEventos.Close(); err != nil && s.errEventos == nil {
			s.errEventos = err
		}
	}
	if s.errEventos != nil {
		res.addError(fmt.Sprintf("failed writing eventos.jsonl: %v", s.errEventos))
	}

	eventosData, err := os.ReadFile(s.eventos)
	if err != nil {
		res.addError(fmt.Sprintf("failed re-reading eventos.jsonl: %v", err))
	}
	res.EventosLineas = bytes.Count(eventosData, []byte{'\n'})
	sum := sha256.Sum256(eventosData)

	var videoBytes int64
	if fi, err := os.Stat(s.video); err == nil {
		videoBytes = fi.Size()
	}
	res.VideoBytes = videoBytes
	videoOK := false
	switch {
	case videoBytes == 0:
		res.VideoValidacion = "no video bytes"
	default:
		v, err := verificarVideo(context.WithoutCancel(s.ctx), s.video)
		res.VideoFotogramas, res.VideoDuracionS = v.Fotogramas, v.DuracionS
		if err != nil {
			res.VideoValidacion = err.Error()
			res.addError("video is not playable: " + err.Error())
		} else {
			res.VideoValidacion, videoOK = "ok", true
		}
	}

	bloque := fmt.Sprintf("\n\n%s\n## Session data (automatic)\n- Session ID: %s\n- Duration: %d ms\n- Display: %s\n- XID: %s\n- Video bytes: %d\n- Events: %d\n",
		MarcaBloqueFlujo, res.SessionID, duracion, res.Display, Hex(s.opts.XID), videoBytes, res.EventosLineas)
	if f, err := os.OpenFile(s.flujo, os.O_WRONLY|os.O_APPEND, 0644); err != nil {
		res.addError(fmt.Sprintf("failed opening FLUJO.md: %v", err))
	} else {
		if _, err := f.WriteString(bloque); err != nil {
			res.addError(fmt.Sprintf("failed appending to FLUJO.md: %v", err))
		}
		if err := f.Close(); err != nil {
			res.addError(fmt.Sprintf("failed closing FLUJO.md: %v", err))
		}
	}

	var faltantes []string
	for _, art := range CuatroArtefactos {
		fi, err := os.Stat(filepath.Join(s.dir, art))
		if err != nil || fi.IsDir() {
			faltantes = append(faltantes, art+" (ausente)")
		} else if art == FicheroVideo && fi.Size() == 0 {
			faltantes = append(faltantes, art+" (vacio)")
		}
	}
	s.manifiesto.DuracionMS = duracion
	s.manifiesto.Eventos.NLineas = res.EventosLineas
	s.manifiesto.Eventos.SHA256 = hex.EncodeToString(sum[:])
	s.manifiesto.Video.Bytes = videoBytes
	s.manifiesto.Video.Fotogramas = res.VideoFotogramas
	s.manifiesto.Video.DuracionS = res.VideoDuracionS
	s.manifiesto.Objetivo.XID = Hex(s.opts.XID)

	switch {
	case len(faltantes) == 0 && res.ArtefactosEnT0 == 4 && videoOK && !s.cancelada && !s.ffmpegMurio && !s.forzado && s.errEventos == nil:
		s.manifiesto.Estado = "completa"
		s.manifiesto.RazonIncompleta = ""
	default:
		s.manifiesto.Estado = "incompleta"
		razon := []string{}
		razon = append(razon, faltantes...)
		if res.ArtefactosEnT0 != 4 {
			razon = append(razon, fmt.Sprintf("artefactos_en_t0=%d", res.ArtefactosEnT0))
		}
		if s.cancelada {
			razon = append(razon, "cancelled")
		}
		if s.ffmpegMurio {
			razon = append(razon, "ffmpeg died")
		}
		if s.forzado {
			razon = append(razon, "ffmpeg forced")
		}
		if !videoOK {
			razon = append(razon, "video not playable: "+res.VideoValidacion)
		}
		s.manifiesto.RazonIncompleta = "invalid session: " + strings.Join(razon, ", ")
	}
	if err := EscribirManifiestoAtomico(s.manifPath, s.manifiesto); err != nil {
		res.addError(fmt.Sprintf("failed updating the manifest: %v", err))
	}
	// The reported state is the one on disk, read back after the write.
	if leido, err := LeerManifiesto(s.manifPath); err != nil {
		res.addError(fmt.Sprintf("failed reading the final manifest: %v", err))
		res.ManifiestoEstado = "ilegible"
	} else {
		res.ManifiestoEstado = leido.Estado
	}

	if err := os.Remove(s.pidPath); err != nil && !errors.Is(err, fs.ErrNotExist) {
		res.addError(fmt.Sprintf("could not remove supervisor.pid: %v", err))
	}
}

// ---------------------------------------------------------------------------------------------
// Nested recording test
// ---------------------------------------------------------------------------------------------

// PruebaAnidada orchestrates a full nested recording test on the given display (e.g. :150): it
// starts an isolated Xvfb, creates a test window, resolves its XID from the server's own window
// tree, runs the recording session, and tears everything down on every path. A teardown residue
// (a live Xvfb, /tmp/.X<N>-lock, the socket) turns the result into a failure. The result is never
// nil.
//
// The display is never defaulted here: an empty spelling (an unset shell variable) is refused by
// the guard like any other spelling that is not provably a nested display, before anything is
// spawned. The default ":150" belongs to the command line's flag, not to this function.
func PruebaAnidada(ctx context.Context, display string, segundos int, raiz string) (*SesionResultado, error) {
	return pruebaAnidada(ctx, SesionOpciones{Display: display, Raiz: raiz, Segundos: segundos, Titulo: "prueba-anidada"})
}

func pruebaAnidada(ctx context.Context, opts SesionOpciones) (res *SesionResultado, err error) {
	d, errV := ValidarDisplayAnidado(opts.Display)
	if errV != nil {
		return EjecutarSesion(ctx, opts) // refuses, and reports the failure with the full field set
	}
	opts.Display = d.Canonico()

	fallo := func(e error) (*SesionResultado, error) { return reporteSinSesion(opts, d, e) }

	xvfb, err := ArrancarXvfb(ctx, d.Canonico())
	if err != nil {
		return fallo(fmt.Errorf("failed starting Xvfb on %s: %w", d.Canonico(), err))
	}
	defer func() {
		if e := xvfb.Detener(); e != nil {
			if res == nil {
				res = &SesionResultado{}
			}
			res.OK = false
			res.addError("Xvfb teardown: " + e.Error())
			err = errors.Join(err, e)
		}
	}()

	win, err := CrearVentanaPrueba(d.Canonico(), "U-16 OpenSuperficies test window", 800, 600)
	if err != nil {
		return fallo(fmt.Errorf("failed creating the test window on %s: %w", d.Canonico(), err))
	}
	defer func() {
		if e := win.Cerrar(); e != nil {
			if res == nil {
				res = &SesionResultado{}
			}
			res.OK = false
			res.addError("closing the test window: " + e.Error())
			err = errors.Join(err, e)
		}
	}()

	// Resolve the XID from the server's own tree, then compare it with the id the client allocated.
	elegido, errSel := elegirObjetivo(d.Canonico())
	if errSel != nil {
		return fallo(fmt.Errorf("failed resolving the target on %s: %w", d.Canonico(), errSel))
	}
	opts.XID = elegido.XID

	res, err = EjecutarSesion(ctx, opts)
	if res != nil {
		coincide := elegido.XID == win.XID
		res.XIDAsignado = Hex(win.XID)
		res.XIDCoincide = &coincide
		if !coincide {
			res.OK = false
			res.addError(fmt.Sprintf("the server XID %s does not match the allocated one %s", Hex(elegido.XID), Hex(win.XID)))
			err = errors.Join(err, errors.New("xid_no_coincide"))
		}
	}
	return res, err
}

// elegirObjetivoDelServidor asks the nested X server, on a connection of its own, which window is
// the recording target. The window may take a moment to be listed, so an unresolved answer is
// retried; an ambiguous one is final.
func elegirObjetivoDelServidor(display string) (Candidato, error) {
	var elegido Candidato
	var err error
	for i := 0; i < 20; i++ {
		var cands []Candidato
		cands, err = CandidatosDeDisplay(display, os.Getpid())
		if err == nil {
			elegido, err = ElegirHost(cands)
		}
		if err == nil || errors.Is(err, ErrObjetivoAmbiguo) {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	return elegido, err
}

// reporteSinSesion builds the failure report of a run that could not start a recording session
// at all (Xvfb or the window failed): the full field set, the kernel observation of the bare
// process tree, no artifacts.
func reporteSinSesion(opts SesionOpciones, d DisplayX, causa error) (*SesionResultado, error) {
	s := &sesion{
		opts: opts,
		disp: d,
		mu:   nuevoMuestreador(d.Numero),
		res: &SesionResultado{
			DisplaySolicitado: opts.Display, Display: d.Canonico(), ManifiestoEstado: "ausente",
			ArtefactosEnT0Lista: []string{}, DisplaysXObservados: []int{}, ConexionesForaneas: []ConexionX{},
		},
	}
	return s.cerrar(causa)
}
