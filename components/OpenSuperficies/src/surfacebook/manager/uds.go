package manager

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"opensuperficies.local/srf/src/kernelbook/flock"
	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// Exit codes carried by Response.RC and returned by the commands.
const (
	RCFallo         = 1  // the operation failed
	RCUso           = 2  // the request is malformed, names an unknown class, or names a slot the daemon does not know
	RCPeerRechazado = 9  // the kernel identity of the caller (SO_PEERCRED) is not admitted
	RCSlotOcupado   = 69 // the slot is already active or transitioning (same code as an exclusive-lock collision)
	// RCParada = 3 is declared in parada.go.
)

var (
	ErrSocketInvalido   = errors.New("uds: socket invalid or not a unix socket")
	ErrCredencialesPeer = errors.New("uds: cannot read SO_PEERCRED")
)

// rcDeError maps a manager error to the exit code the client receives.
func rcDeError(err error) int {
	switch {
	case errors.Is(err, ErrParadaActiva):
		return RCParada
	case errors.Is(err, ErrClaseInvalida), errors.Is(err, ErrClaseDistinta), errors.Is(err, ErrSlotAusente):
		return RCUso
	case errors.Is(err, ErrSlotOcupado):
		return RCSlotOcupado
	default:
		return RCFallo
	}
}

// PeerCredentials is the caller's kernel identity as reported by SO_PEERCRED.
type PeerCredentials struct {
	PID int32  `json:"pid"`
	UID uint32 `json:"uid"`
	GID uint32 `json:"gid"`
}

// PeerAdmitido is the whole admission decision of the daemon, as a pure function of the caller's
// kernel identity and the one uid the daemon serves: the uid must be equal. There is no second
// admitted uid, no root exemption and no group rule; a peer that is not admitted gets RCPeerRechazado
// and nothing is executed for it.
func PeerAdmitido(cred PeerCredentials, uidPermitido uint32) bool {
	return cred.UID == uidPermitido
}

// GetPeerCred extracts the SO_PEERCRED credentials of the peer of a unix-socket connection.
func GetPeerCred(conn net.Conn) (*PeerCredentials, error) {
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return nil, ErrSocketInvalido
	}
	rawConn, err := unixConn.SyscallConn()
	if err != nil {
		return nil, fmt.Errorf("%w: syscall conn: %v", ErrCredencialesPeer, err)
	}

	var ucred *unix.Ucred
	var credErr error
	err = rawConn.Control(func(fd uintptr) {
		ucred, credErr = unix.GetsockoptUcred(int(fd), unix.SOL_SOCKET, unix.SO_PEERCRED)
	})
	if err != nil {
		return nil, fmt.Errorf("%w: control: %v", ErrCredencialesPeer, err)
	}
	if credErr != nil {
		return nil, fmt.Errorf("%w: getsockopt: %v", ErrCredencialesPeer, credErr)
	}
	return &PeerCredentials{PID: ucred.Pid, UID: ucred.Uid, GID: ucred.Gid}, nil
}

// Request is an IPC command sent to srfd over the UDS.
type Request struct {
	Comando  string `json:"comando"` // lifecycle, queries, or one of the four servicios-* verbs below
	Clase    string `json:"clase,omitempty"`
	Slot     uint16 `json:"slot,omitempty"`
	Servicio string `json:"servicio,omitempty"`
	Capsula  string `json:"capsula,omitempty"`
	AIID     string `json:"ai_id,omitempty"`
	Motivo   string `json:"motivo,omitempty"`
}

// Response is the outcome returned by srfd.
type Response struct {
	OK    bool            `json:"ok"`
	RC    int             `json:"rc"`
	Error string          `json:"error,omitempty"`
	Datos json.RawMessage `json:"datos,omitempty"`
}

// RutaSocket resolves the daemon socket: the explicit value, $SRF_SOCKET, then
// $XDG_RUNTIME_DIR/opensuperficies/srfd.sock (the temporary directory when XDG_RUNTIME_DIR is unset).
func RutaSocket(explicita string) string {
	if explicita != "" {
		return explicita
	}
	if env := os.Getenv("SRF_SOCKET"); env != "" {
		return env
	}
	base := os.Getenv("XDG_RUNTIME_DIR")
	if base == "" {
		base = os.TempDir()
	}
	return filepath.Join(base, "opensuperficies", "srfd.sock")
}

// umaskMu serialises the process-wide umask change around bind.
var umaskMu sync.Mutex

// CrearSocket binds the daemon socket so that it is 0600 AT CREATION (the umask is narrowed around
// bind) and verifies the result: there is no chmod afterwards that could mask a broken umask.
// A stale socket is replaced; a regular file, or a socket a live process still serves, is never removed.
func CrearSocket(socketPath string) (net.Listener, error) {
	if socketPath == "" {
		return nil, errors.New("uds: socket path cannot be empty")
	}
	if err := os.MkdirAll(filepath.Dir(socketPath), 0o700); err != nil {
		return nil, fmt.Errorf("uds: mkdir %s: %w", filepath.Dir(socketPath), err)
	}
	if err := retirarSocketObsoleto(socketPath); err != nil {
		return nil, err
	}

	umaskMu.Lock()
	viejo := syscall.Umask(0o177)
	l, err := net.Listen("unix", socketPath)
	syscall.Umask(viejo)
	umaskMu.Unlock()
	if err != nil {
		return nil, fmt.Errorf("uds: listen %s: %w", socketPath, err)
	}

	fi, err := os.Lstat(socketPath)
	if err != nil {
		return nil, errors.Join(fmt.Errorf("uds: stat %s: %w", socketPath, err), l.Close())
	}
	if fi.Mode()&os.ModeSocket == 0 || fi.Mode().Perm() != 0o600 {
		// Close unlinks the socket it created; the explicit removal covers a listener that did not.
		return nil, errors.Join(fmt.Errorf("uds: %s was created as %v, expected a 0600 socket", socketPath, fi.Mode()),
			l.Close(), removerSiExiste(socketPath))
	}
	return l, nil
}

// retirarSocketObsoleto removes a leftover socket file so that bind can succeed.
func retirarSocketObsoleto(socketPath string) error {
	fi, err := os.Lstat(socketPath)
	if errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("uds: stat %s: %w", socketPath, err)
	}
	if fi.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("uds: %s exists and is not a socket: refusing to remove it", socketPath)
	}
	if c, derr := net.DialTimeout("unix", socketPath, 500*time.Millisecond); derr == nil {
		_ = c.Close()
		return fmt.Errorf("uds: %s is served by a live process: refusing to replace it", socketPath)
	}
	if err := os.Remove(socketPath); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("uds: remove stale socket %s: %w", socketPath, err)
	}
	return nil
}

// EnviarComando sends one request to srfd and reads one response.
func EnviarComando(socketPath string, req Request, timeout time.Duration) (*Response, error) {
	if timeout <= 0 {
		timeout = 30 * time.Second
	}
	conn, err := net.DialTimeout("unix", socketPath, timeout)
	if err != nil {
		return nil, fmt.Errorf("uds: dial %s: %w", socketPath, err)
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(timeout)); err != nil {
		return nil, fmt.Errorf("uds: set deadline: %w", err)
	}

	data, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("uds: marshal request: %w", err)
	}
	// srfd may answer and close before reading (a refused peer): the write then fails with EPIPE,
	// but the answer is already queued on the socket, so it is read before reporting the write error.
	_, werr := conn.Write(append(data, '\n'))
	linea, err := bufio.NewReader(io.LimitReader(conn, 4<<20)).ReadBytes('\n')
	if err != nil {
		if werr != nil {
			return nil, fmt.Errorf("uds: write request: %w", werr)
		}
		return nil, fmt.Errorf("uds: read response: %w", err)
	}
	var resp Response
	if err := json.Unmarshal(linea, &resp); err != nil {
		return nil, fmt.Errorf("uds: unmarshal response: %w", err)
	}
	return &resp, nil
}

// ConfigDaemon describes one srfd incarnation.
type ConfigDaemon struct {
	SocketPath      string
	ParadaPath      string                   // repository signal file; "" => $SRF_PARADA_REPO, then <repo>/PARADA.txt
	ParadaPlazaPath string                   // Plaza signal file; "" => $SRF_PARADA_PLAZA, then $SRF_SALA/PARADA.txt, then the campaign room
	UIDPermitido    *uint32                  // the only caller uid admitted; nil => this process's uid
	Telemetria      gobernador.SrfTelemetria // nil => live /proc telemetry
	Manager         OpcionesManager
	Gracia          time.Duration // how long in-flight requests may run once shutdown starts; 0 => 20 s
	TimeoutCierre   time.Duration // bound on stopping the live capsules at shutdown; 0 => 90 s
	PlazoLectura    time.Duration // how long a peer may take to send its request line; 0 => 5 s
	Log             io.Writer     // nil => discarded
}

const (
	// maxPeticion is the largest request line the daemon reads; a longer one is refused, never buffered.
	maxPeticion = 4096
	// plazoLecturaDefecto is the time a connected peer has to deliver its request line.
	plazoLecturaDefecto = 5 * time.Second
)

// EjecutarDaemon is the whole of srfd: flags, signals, Servir. It returns only after Servir has
// returned, that is after teardown finished, so main may os.Exit with its result.
func EjecutarDaemon(args []string, stderr io.Writer) int {
	flags := flag.NewFlagSet("srfd", flag.ContinueOnError)
	flags.SetOutput(stderr)
	socket := flags.String("socket", "", "path to the UDS socket (default $SRF_SOCKET, then $XDG_RUNTIME_DIR/opensuperficies/srfd.sock)")
	if err := flags.Parse(args); err != nil {
		return RCUso
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	defer stop()

	err := Servir(ctx, ConfigDaemon{SocketPath: RutaSocket(*socket), Log: stderr})
	if err != nil {
		fmt.Fprintf(stderr, "srfd: %v\n", err)
		if flock.IsLockError(err) {
			return flock.ExitCodeLockCollision
		}
		return rcDeError(err)
	}
	return 0
}

// demonio holds the resources of one incarnation, in the order they are acquired.
type demonio struct {
	cfg ConfigDaemon
	log io.Writer
	uid uint32

	dir        string
	dirCreado  bool
	walPath    string
	walExistia bool

	libro        *wal.LibroConsumo
	mgr          *SurfaceManager
	listener     net.Listener
	socketCreado bool
	servido      bool
	servicios    serviciosDaemon

	workCtx    context.Context
	cancelWork context.CancelFunc
	wg         sync.WaitGroup
}

// Servir runs one daemon incarnation until ctx is cancelled or serving fails, and returns only
// after teardown has COMPLETED on every path (normal, error, panic): in-flight requests are waited
// for, live capsules are stopped, the journal is fsynced and closed (its flock released), the socket is
// unlinked. The journal and its flock file are durable state and are NEVER unlinked by a run that
// served: the lessor rebuilds its fencing epochs from them at the next start. The only journal a run
// removes is one it created itself and never wrote to (a start that failed before serving).
func Servir(ctx context.Context, cfg ConfigDaemon) (err error) {
	if cfg.SocketPath == "" {
		return errors.New("empty socket path")
	}
	if cfg.Gracia <= 0 {
		cfg.Gracia = 20 * time.Second
	}
	if cfg.TimeoutCierre <= 0 {
		cfg.TimeoutCierre = 90 * time.Second
	}
	if cfg.PlazoLectura <= 0 {
		cfg.PlazoLectura = plazoLecturaDefecto
	}
	d := &demonio{cfg: cfg, log: cfg.Log, uid: uint32(os.Getuid())}
	if d.log == nil {
		d.log = io.Discard
	}
	if cfg.UIDPermitido != nil {
		d.uid = *cfg.UIDPermitido
	}
	d.workCtx, d.cancelWork = context.WithCancel(context.Background())
	defer d.cancelWork()

	cerrado := false
	defer func() {
		if !cerrado { // a panic is unwinding: teardown still runs, and says how it went
			if cerr := d.cerrar(); cerr != nil {
				fmt.Fprintf(d.log, "srfd: teardown during panic: %v\n", cerr)
			}
		}
	}()

	if err = d.abrir(); err == nil {
		err = d.servir(ctx)
	}
	cerrado = true
	return errors.Join(err, d.cerrar())
}

// asegurarDirectorio makes sure the socket directory exists and belongs to this uid.
func asegurarDirectorio(dir string) (creado bool, err error) {
	fi, err := os.Stat(dir)
	switch {
	case err == nil:
		if !fi.IsDir() {
			return false, fmt.Errorf("%s is not a directory", dir)
		}
		if st, ok := fi.Sys().(*syscall.Stat_t); ok && st.Uid != uint32(os.Getuid()) {
			return false, fmt.Errorf("socket directory %s belongs to uid %d, not to uid %d", dir, st.Uid, os.Getuid())
		}
		return false, nil
	case errors.Is(err, fs.ErrNotExist):
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return false, fmt.Errorf("mkdir %s: %w", dir, err)
		}
		return true, nil
	default:
		return false, fmt.Errorf("stat %s: %w", dir, err)
	}
}

// abrir acquires the resources in a fixed order; each one is recorded on d only after it was
// acquired, so cerrar releases exactly what this incarnation owns.
func (d *demonio) abrir() error {
	d.dir = filepath.Dir(d.cfg.SocketPath)
	var err error
	if d.dirCreado, err = asegurarDirectorio(d.dir); err != nil {
		return err
	}
	d.walPath = filepath.Join(d.dir, "srfd.wal")
	if _, serr := os.Lstat(d.walPath); serr == nil {
		d.walExistia = true
	}

	// The journal's exclusive flock is the single-instance guard: it is taken before the socket
	// is touched, so a second daemon fails here and never unlinks the first one's socket.
	flockExistia := false
	if _, serr := os.Lstat(d.walPath + ".flock"); serr == nil {
		flockExistia = true
	}
	d.libro, err = wal.AbrirLibro(d.walPath)
	if err != nil {
		if flock.IsLockError(err) {
			return fmt.Errorf("another srfd holds %s: %w", d.walPath, err)
		}
		// AbrirLibro may have created either file before failing: remove only what did not exist.
		var errs []error
		if !d.walExistia {
			errs = append(errs, removerSiExiste(d.walPath))
		}
		if !flockExistia {
			errs = append(errs, removerSiExiste(d.walPath+".flock"))
		}
		return errors.Join(fmt.Errorf("open journal %s: %w", d.walPath, err), errors.Join(errs...))
	}

	arr, err := arrendador.NuevoSrfArrendador(func(c string) string { return c }, d.libro)
	if err != nil {
		return fmt.Errorf("lessor: %w", err)
	}
	tel := d.cfg.Telemetria
	if tel == nil {
		tel = gobernador.NuevaTelemetriaProc(nil)
	}
	d.mgr, err = NuevoSurfaceManager(gobernador.NuevoGobernador(tel), arr, NuevoParadaManager(d.cfg.ParadaPath, d.cfg.ParadaPlazaPath), d.libro, d.cfg.Manager)
	if err != nil {
		return err
	}

	d.listener, err = CrearSocket(d.cfg.SocketPath)
	if err != nil {
		return err
	}
	d.socketCreado = true
	return nil
}

func removerSiExiste(ruta string) error {
	if err := os.Remove(ruta); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("remove %s: %w", ruta, err)
	}
	return nil
}

// servir accepts connections in the calling goroutine until ctx is cancelled. The only other
// goroutine closes the listener on cancellation and is waited for before servir returns.
func (d *demonio) servir(ctx context.Context) error {
	d.servido = true
	parar := make(chan struct{})
	terminado := make(chan struct{})
	go func() {
		defer close(terminado)
		select {
		case <-ctx.Done():
			if err := d.listener.Close(); err != nil {
				fmt.Fprintf(d.log, "srfd: close listener on shutdown: %v\n", err)
			}
		case <-parar:
		}
	}()
	defer func() {
		close(parar)
		<-terminado
	}()

	fmt.Fprintf(d.log, "srfd: ready socket=%s uid=%d\n", d.cfg.SocketPath, d.uid)
	for {
		conn, err := d.listener.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("accept: %w", err)
		}
		d.wg.Add(1)
		go d.atender(conn)
	}
}

// cerrar is teardown. It is safe to call after a partial abrir and releases only what was acquired.
func (d *demonio) cerrar() error {
	var errs []error

	// 1. No new connections.
	if d.listener != nil {
		if err := d.listener.Close(); err != nil && !errors.Is(err, net.ErrClosed) {
			errs = append(errs, fmt.Errorf("close listener: %w", err))
		}
	}

	// 2. In-flight requests: a bounded wait, then their dispatcher calls are cancelled and waited for.
	hecho := make(chan struct{})
	go func() {
		d.wg.Wait()
		close(hecho)
	}()
	plazo := time.NewTimer(d.cfg.Gracia)
	select {
	case <-hecho:
	case <-plazo.C:
		d.cancelWork()
		<-hecho
	}
	plazo.Stop()

	// 3. Live capsules are stopped through the guarded path; what cannot be stopped keeps the journal alive.
	var restantes []uint16
	if d.mgr != nil {
		ctx, cancel := context.WithTimeout(context.Background(), d.cfg.TimeoutCierre)
		var derr error
		restantes, derr = d.mgr.DetenerTodas(ctx)
		cancel()
		if len(restantes) > 0 {
			if derr == nil {
				derr = errors.New("no stop was attempted")
			}
			errs = append(errs, fmt.Errorf("%d capsule(s) still live at shutdown (slots %v), journal kept: %w", len(restantes), restantes, derr))
		}
	}

	// 4. The journal is durable state: fsynced and closed (its flock released), never unlinked. The one
	// exception is a journal THIS run created and never wrote to because it did not get to serve (the only
	// writers, the lessor's grants and revocations, run for requests): it holds nothing, so removing it
	// (while its flock is still held) resets no epoch and destroys no record.
	if d.libro != nil && !d.walExistia && !d.servido {
		errs = append(errs, removerSiExiste(d.walPath), removerSiExiste(d.walPath+".flock"))
	}
	switch {
	case d.mgr != nil:
		errs = append(errs, d.mgr.Cerrar())
	case d.libro != nil:
		errs = append(errs, cerrarLibro(d.libro))
	}

	// 5. The socket file: the listener normally unlinks it on Close; this does not rely on that.
	if d.socketCreado {
		errs = append(errs, removerSiExiste(d.cfg.SocketPath))
	}

	// 6. The directory, only if this run created it and it holds nothing now (a journal that was kept keeps it).
	if d.dirCreado {
		if err := os.Remove(d.dir); err != nil && !errors.Is(err, fs.ErrNotExist) {
			var pe *fs.PathError
			if !(errors.As(err, &pe) && errors.Is(pe.Err, syscall.ENOTEMPTY)) {
				errs = append(errs, fmt.Errorf("remove %s: %w", d.dir, err))
			}
		}
	}
	return errors.Join(errs...)
}

// atender serves one connection: the kernel identity of the peer first, then one request.
func (d *demonio) atender(conn net.Conn) {
	defer d.wg.Done()
	defer conn.Close()
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(d.log, "srfd: request handler panic: %v\n", r)
			d.responder(conn, Response{RC: RCFallo, Error: "internal error"})
		}
	}()

	cred, err := GetPeerCred(conn)
	if err != nil {
		d.responder(conn, Response{RC: RCPeerRechazado, Error: fmt.Sprintf("peer identity unavailable: %v", err)})
		return
	}
	if !PeerAdmitido(*cred, d.uid) {
		fmt.Fprintf(d.log, "srfd: refused peer pid=%d uid=%d (admitted uid %d)\n", cred.PID, cred.UID, d.uid)
		d.responder(conn, Response{RC: RCPeerRechazado, Error: fmt.Sprintf("peer uid %d is not admitted", cred.UID)})
		return
	}

	_ = conn.SetReadDeadline(time.Now().Add(d.cfg.PlazoLectura))
	linea, err := bufio.NewReaderSize(io.LimitReader(conn, maxPeticion), maxPeticion).ReadBytes('\n')
	if err != nil {
		d.responder(conn, Response{RC: RCUso, Error: fmt.Sprintf("no complete request line: %v", err)})
		return
	}
	var req Request
	if err := json.Unmarshal(linea, &req); err != nil {
		// A request whose verb was read but whose arguments do not fit their types (a slot of 70000, a
		// class that is a number) still names a mutation: the stop signal answers before the argument
		// error does. Only a line that is not json at all leaves the verb unknown.
		var tipo *json.UnmarshalTypeError
		if errors.As(err, &tipo) && (req.Comando == "iniciar" || req.Comando == "detener" || req.Comando == "servicios-solicitar") {
			if gerr := d.mgr.guardia.ExigirLibre(); gerr != nil {
				d.responder(conn, Response{RC: rcDeError(gerr), Error: gerr.Error()})
				return
			}
		}
		d.responder(conn, Response{RC: RCUso, Error: fmt.Sprintf("malformed json request: %v", err)})
		return
	}

	switch req.Comando {
	case "estado":
		est, err := d.mgr.Estado()
		if err != nil {
			d.responder(conn, Response{RC: RCFallo, Error: err.Error()})
			return
		}
		d.responderDatos(conn, est)

	case "iniciar":
		fmt.Fprintf(d.log, "srfd: iniciar clase=%q slot=%d peer_pid=%d\n", req.Clase, req.Slot, cred.PID)
		info, err := d.mgr.IniciarCapsula(d.workCtx, req.Clase, req.Slot)
		if err != nil {
			d.responder(conn, Response{RC: rcDeError(err), Error: err.Error()})
			return
		}
		d.responderDatos(conn, info)

	case "detener":
		fmt.Fprintf(d.log, "srfd: detener clase=%q slot=%d peer_pid=%d\n", req.Clase, req.Slot, cred.PID)
		if err := d.mgr.DetenerCapsula(d.workCtx, req.Slot, req.Clase); err != nil {
			d.responder(conn, Response{RC: rcDeError(err), Error: err.Error()})
			return
		}
		d.responder(conn, Response{OK: true})

	case "servicios-donde", "servicios-estado", "servicios-pendientes", "servicios-solicitar":
		datos, err := d.datosServicios(req)
		if err != nil {
			d.responder(conn, Response{RC: rcServicios(err), Error: err.Error()})
			return
		}
		d.responderDatos(conn, datos)

	case "mediacion":
		d.responderDatos(conn, d.mgr.CensoMediacion(d.workCtx))

	case "listar":
		d.responderDatos(conn, d.mgr.Listar())

	default:
		d.responder(conn, Response{RC: RCUso, Error: fmt.Sprintf("unknown command: %q", req.Comando)})
	}
}

func (d *demonio) responderDatos(conn net.Conn, datos any) {
	raw, err := json.Marshal(datos)
	if err != nil {
		d.responder(conn, Response{RC: RCFallo, Error: fmt.Sprintf("marshal answer: %v", err)})
		return
	}
	d.responder(conn, Response{OK: true, Datos: raw})
}

func (d *demonio) responder(conn net.Conn, resp Response) {
	data, err := json.Marshal(resp)
	if err != nil {
		fmt.Fprintf(d.log, "srfd: marshal response: %v\n", err)
		return
	}
	_ = conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
	if _, err := conn.Write(append(data, '\n')); err != nil {
		fmt.Fprintf(d.log, "srfd: write response (rc %d): %v\n", resp.RC, err)
	}
}

// clienteSrf is the command line of `srf`: every subcommand returns its exit code instead of exiting,
// so the code cmd/srf runs is the code the tests run.
type clienteSrf struct {
	stdout, stderr io.Writer
}

// EjecutarSrf is the whole of the `srf` client: it sends one request to srfd and returns the exit code
// srfd assigned to it (0 ok, 3 PARADA, 69 slot busy, ...), 1 when srfd cannot be reached, 2 for usage.
func EjecutarSrf(args []string, stdout, stderr io.Writer) int {
	c := clienteSrf{stdout: stdout, stderr: stderr}
	if len(args) < 1 {
		c.uso()
		return RCUso
	}
	switch sub := args[0]; sub {
	case "estado":
		return c.estado(args[1:])
	case "servicios-donde", "servicios-estado", "servicios-pendientes", "servicios-solicitar":
		return c.servicios(sub, args[1:])
	case "capsula":
		return c.capsula(args[1:])
	case "listar":
		return c.listar(args[1:])
	default:
		fmt.Fprintf(stderr, "unknown subcommand: %s\n", sub)
		c.uso()
		return RCUso
	}
}

func (c clienteSrf) uso() {
	fmt.Fprintf(c.stderr, "usage: srf <estado|capsula|listar|servicios-donde|servicios-estado|servicios-pendientes|servicios-solicitar> [options] [arguments]\n")
}

// parsear parses a subcommand's flags; ok is false when the command must return rc at once.
func (c clienteSrf) parsear(fl *flag.FlagSet, args []string) (rc int, ok bool) {
	fl.SetOutput(c.stderr)
	if err := fl.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0, false
		}
		return RCUso, false
	}
	return 0, true
}

// enviar sends one request to srfd; a transport failure is rc 1 with the socket named.
func (c clienteSrf) enviar(socketFlag string, req Request, timeout time.Duration) (*Response, int) {
	socketPath := RutaSocket(socketFlag)
	resp, err := EnviarComando(socketPath, req, timeout)
	if err != nil {
		fmt.Fprintf(c.stderr, "srf: cannot talk to srfd (%s): %v\n", socketPath, err)
		return nil, RCFallo
	}
	return resp, 0
}

// falla reports a refused or failed request and returns the code srfd assigned to it (never 0).
func (c clienteSrf) falla(resp *Response, jsonFlag bool, contexto string) int {
	if jsonFlag {
		out, _ := json.Marshal(map[string]any{"ok": false, "rc": resp.RC, "error": resp.Error})
		fmt.Fprintln(c.stdout, string(out))
	} else {
		fmt.Fprintf(c.stderr, "srf: %s: %s\n", contexto, resp.Error)
	}
	if resp.RC == 0 {
		return RCFallo // a failure never exits 0, whatever the answer carried
	}
	return resp.RC
}

func (c clienteSrf) estado(args []string) int {
	fl := flag.NewFlagSet("estado", flag.ContinueOnError)
	var (
		jsonFlag   bool
		socketFlag string
	)
	fl.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	fl.StringVar(&socketFlag, "socket", "", "path to srfd UDS socket")
	if rc, ok := c.parsear(fl, args); !ok {
		return rc
	}

	resp, rc := c.enviar(socketFlag, Request{Comando: "estado"}, 10*time.Second)
	if resp == nil {
		return rc
	}
	if !resp.OK {
		return c.falla(resp, false, "estado")
	}
	if jsonFlag {
		// The compact payload verbatim: it is the thing the 120-token ceiling applies to.
		fmt.Fprintln(c.stdout, string(resp.Datos))
		return 0
	}
	var est SrfEstado
	if err := json.Unmarshal(resp.Datos, &est); err != nil {
		fmt.Fprintf(c.stderr, "srf: unreadable state from srfd: %v\n", err)
		return RCFallo
	}
	fmt.Fprintf(c.stdout, "srfd: %s (parada: %s, vivas: %d, disp: %d MiB)\n", est.Estado, est.Parada, est.Vivas, est.DisponibleMiB)
	return 0
}

func (c clienteSrf) capsula(args []string) int {
	if len(args) < 1 {
		fmt.Fprintf(c.stderr, "usage: srf capsula <iniciar|detener|listar> [options]\n")
		return RCUso
	}
	switch action := args[0]; action {
	case "iniciar":
		return c.capsulaIniciar(args[1:])
	case "detener":
		return c.capsulaDetener(args[1:])
	case "listar":
		return c.listar(args[1:])
	default:
		fmt.Fprintf(c.stderr, "unknown capsula action: %s\n", action)
		return RCUso
	}
}

func (c clienteSrf) capsulaIniciar(args []string) int {
	fl := flag.NewFlagSet("iniciar", flag.ContinueOnError)
	var (
		claseFlag  string
		slotFlag   uint
		jsonFlag   bool
		socketFlag string
	)
	fl.StringVar(&claseFlag, "clase", ClaseDefecto, "capsule class")
	fl.UintVar(&slotFlag, "slot", 0, "slot number")
	fl.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	fl.StringVar(&socketFlag, "socket", "", "path to srfd UDS socket")
	if rc, ok := c.parsear(fl, args); !ok {
		return rc
	}

	// Nothing is validated here: the class and the slot are checked by srfd, AFTER its stop-signal guard,
	// so that an effective PARADA answers rc 3 whatever the arguments are.
	resp, rc := c.enviar(socketFlag, Request{Comando: "iniciar", Clase: claseFlag, Slot: uint16(slotFlag)}, 60*time.Second)
	if resp == nil {
		return rc
	}
	if !resp.OK {
		return c.falla(resp, jsonFlag, "start refused")
	}
	if jsonFlag {
		fmt.Fprintln(c.stdout, string(resp.Datos))
	} else {
		fmt.Fprintf(c.stdout, "slot %d (%s) started\n", slotFlag, claseFlag)
	}
	return 0
}

func (c clienteSrf) capsulaDetener(args []string) int {
	fl := flag.NewFlagSet("detener", flag.ContinueOnError)
	var (
		claseFlag  string
		slotFlag   uint
		jsonFlag   bool
		socketFlag string
	)
	fl.StringVar(&claseFlag, "clase", "", "capsule class (optional: srfd takes it from the mediated slot)")
	fl.UintVar(&slotFlag, "slot", 0, "slot number")
	fl.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	fl.StringVar(&socketFlag, "socket", "", "path to srfd UDS socket")
	if rc, ok := c.parsear(fl, args); !ok {
		return rc
	}

	resp, rc := c.enviar(socketFlag, Request{Comando: "detener", Clase: claseFlag, Slot: uint16(slotFlag)}, 30*time.Second)
	if resp == nil {
		return rc
	}
	if !resp.OK {
		return c.falla(resp, jsonFlag, "stop refused")
	}
	if jsonFlag {
		out, _ := json.Marshal(map[string]any{"ok": true, "slot": slotFlag, "estado": "detenido", "rc": 0})
		fmt.Fprintln(c.stdout, string(out))
	} else {
		fmt.Fprintf(c.stdout, "slot %d stopped\n", slotFlag)
	}
	return 0
}

func (c clienteSrf) listar(args []string) int {
	fl := flag.NewFlagSet("listar", flag.ContinueOnError)
	var (
		jsonFlag   bool
		socketFlag string
	)
	fl.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	fl.StringVar(&socketFlag, "socket", "", "path to srfd UDS socket")
	if rc, ok := c.parsear(fl, args); !ok {
		return rc
	}

	resp, rc := c.enviar(socketFlag, Request{Comando: "listar"}, 10*time.Second)
	if resp == nil {
		return rc
	}
	if !resp.OK {
		return c.falla(resp, jsonFlag, "list")
	}
	fmt.Fprintln(c.stdout, string(resp.Datos))
	return 0
}
