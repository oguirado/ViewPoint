package manager

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// TestMain lets the test binary act as srfd: the signal tests start it as a child process and
// signal it, so they exercise the very function main runs (EjecutarDaemon), not a look-alike.
func TestMain(m *testing.M) {
	if os.Getenv("SRF_TEST_HELPER_DAEMON") == "1" {
		os.Exit(EjecutarDaemon(os.Args[1:], os.Stderr))
	}
	code := m.Run()
	if dirBinarios != "" {
		_ = os.RemoveAll(dirBinarios) // the binaries the command-line tests built
	}
	os.Exit(code)
}

// ---------------------------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------------------------

// capsulaFalsa stands in for bin/srf-capsula. It records its argv and answers like the dispatcher.
// Arguments: $1=verb $2=--clase $3=class $4=--slot $5=slot $6=--json.
const capsulaFalsa = `#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SRF_TEST_LOG"
case "$1" in
iniciar)
  [ -n "$SRF_TEST_ARRANQUE" ] && : > "$SRF_TEST_ARRANQUE"
  [ -n "$SRF_TEST_ESPERA" ] && sleep "$SRF_TEST_ESPERA"
  if [ "$SRF_TEST_MODO" = fallo ]; then echo "simulated start failure" >&2; exit 1; fi
  if [ "$SRF_TEST_MODO" = basura ]; then echo "this is not json"; exit 0; fi
  printf '{"slot":%s,"clase":"%s","contenedor":"srf-ng-%s","id":"fake","puerto_xpra":%d,"puerto_cdp":%d,"estado":"listo"}\n' "$5" "$3" "$5" $((10020+$5)) $((9220+$5))
  ;;
detener)
  [ -n "$SRF_TEST_ARRANQUE_DETENER" ] && : > "$SRF_TEST_ARRANQUE_DETENER"
  [ -n "$SRF_TEST_ESPERA_DETENER" ] && sleep "$SRF_TEST_ESPERA_DETENER"
  if [ "$SRF_TEST_MODO_DETENER" = fallo ]; then echo "simulated stop failure" >&2; exit 1; fi
  printf '{"slot":%s,"clase":"%s","estado":"detenido","rc":0}\n' "$5" "$3"
  ;;
esac
`

// corto returns a short-lived directory with a short path (unix socket paths are limited to 108 bytes).
func corto(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "srf")
	if err != nil {
		t.Fatalf("mkdir temp: %v", err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

func escribir(t *testing.T, ruta, contenido string) {
	t.Helper()
	if err := os.WriteFile(ruta, []byte(contenido), 0o644); err != nil {
		t.Fatalf("write %s: %v", ruta, err)
	}
}

const paradaLibre = "LIBRE 2026-09-18T10:00:00+02:00\n"
const paradaActiva = "PARADA 2026-09-18T10:00:00+02:00\n"

// escribirCapsulaFalsa installs the fake dispatcher and its log, returning (binary, log).
func escribirCapsulaFalsa(t *testing.T, dir string) (string, string) {
	t.Helper()
	bin := filepath.Join(dir, "srf-capsula-falsa")
	if err := os.WriteFile(bin, []byte(capsulaFalsa), 0o755); err != nil {
		t.Fatalf("write fake dispatcher: %v", err)
	}
	log := filepath.Join(dir, "dispatcher.log")
	t.Setenv("SRF_TEST_LOG", log)
	return bin, log
}

// lineas returns the non-empty lines of a file (nil if it does not exist).
// esperarArchivo waits (bounded) until the fake dispatcher has created the marker file.
func esperarArchivo(t *testing.T, ruta string) {
	t.Helper()
	limite := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(ruta); err == nil {
			return
		}
		if time.Now().After(limite) {
			t.Fatalf("the dispatcher never created %s", ruta)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func lineas(t *testing.T, ruta string) []string {
	t.Helper()
	data, err := os.ReadFile(ruta)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		t.Fatalf("read %s: %v", ruta, err)
	}
	var out []string
	for _, l := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	return out
}

type entorno struct {
	dir     string
	log     string
	bin     string
	parada  string // repository signal file
	plaza   string // Plaza signal file
	walPath string
	libro   *wal.LibroConsumo
	mgr     *SurfaceManager
}

type opcionesEntorno struct {
	parada  string  // content of the repository PARADA file; "" means LIBRE, "-" means the file does not exist
	plaza   string  // content of the Plaza PARADA file, same convention
	guardia Guardia // overrides the file-based guard
	censo   CensoOpciones
}

// escribirSenal writes a signal file: "" is LIBRE, "-" leaves the file absent, anything else is its content.
func escribirSenal(t *testing.T, ruta, contenido string) {
	t.Helper()
	switch contenido {
	case "":
		escribir(t, ruta, paradaLibre)
	case "-":
		_ = os.Remove(ruta)
	default:
		escribir(t, ruta, contenido)
	}
}

func nuevoEntorno(t *testing.T, o opcionesEntorno) *entorno {
	t.Helper()
	dir := t.TempDir()
	bin, log := escribirCapsulaFalsa(t, dir)

	e := &entorno{dir: dir, log: log, bin: bin, parada: filepath.Join(dir, "PARADA.txt"), plaza: filepath.Join(dir, "PARADA-PLAZA.txt"), walPath: filepath.Join(dir, "test.wal")}
	escribirSenal(t, e.parada, o.parada)
	escribirSenal(t, e.plaza, o.plaza)

	libro, err := wal.AbrirLibro(e.walPath)
	if err != nil {
		t.Fatalf("open journal: %v", err)
	}
	t.Cleanup(func() { _ = libro.Cerrar() })
	e.libro = libro

	arr, err := arrendador.NuevoSrfArrendador(func(c string) string { return c }, libro)
	if err != nil {
		t.Fatalf("lessor: %v", err)
	}
	gov := gobernador.NuevoGobernador(&gobernador.TelemetriaSintetica{DisponibleBytes: 64 << 30})

	var g Guardia = NuevoParadaManager(e.parada, e.plaza)
	if o.guardia != nil {
		g = o.guardia
	}
	if o.censo.RepoDir == "" {
		o.censo.RepoDir = dir
	}
	mgr, err := NuevoSurfaceManager(gov, arr, g, libro, OpcionesManager{CapsulaBin: bin, RepoDir: dir, Timeout: 20 * time.Second, Censo: o.censo})
	if err != nil {
		t.Fatalf("manager: %v", err)
	}
	e.mgr = mgr
	return e
}

// registrosWAL re-reads the journal from disk: a second source, independent of the manager's memory.
func (e *entorno) registrosWAL(t *testing.T) []wal.Registro {
	t.Helper()
	regs, err := wal.LeerRegistros(e.walPath)
	if err != nil {
		t.Fatalf("read journal: %v", err)
	}
	return regs
}

func (e *entorno) revocaciones(t *testing.T) int {
	n := 0
	for _, r := range e.registrosWAL(t) {
		if strings.Contains(string(r.Detalles), `"accion":"revocar"`) {
			n++
		}
	}
	return n
}

func (e *entorno) vivas(t *testing.T) int {
	t.Helper()
	est, err := e.mgr.Estado()
	if err != nil {
		t.Fatalf("Estado: %v", err)
	}
	return est.Vivas
}

// guardiaQueSeVuelvePARADA reads LIBRE for the first n calls to ExigirLibre and PARADA afterwards.
type guardiaQueSeVuelvePARADA struct {
	mu     sync.Mutex
	libres int
}

func (g *guardiaQueSeVuelvePARADA) Estado() (string, bool) { return "LIBRE", true }
func (g *guardiaQueSeVuelvePARADA) ExigirLibre() error {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.libres > 0 {
		g.libres--
		return nil
	}
	return ErrParadaActiva
}

// registro is an io.Writer that signals when the daemon printed its readiness line.
type registro struct {
	mu    sync.Mutex
	buf   bytes.Buffer
	listo chan struct{}
	once  sync.Once
}

func nuevoRegistro() *registro { return &registro{listo: make(chan struct{})} }

func (r *registro) Write(p []byte) (int, error) {
	r.mu.Lock()
	r.buf.Write(p)
	visto := strings.Contains(r.buf.String(), "srfd: ready")
	r.mu.Unlock()
	if visto {
		r.once.Do(func() { close(r.listo) })
	}
	return len(p), nil
}

func (r *registro) String() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.buf.String()
}

// arrancar runs Servir in this process and returns a function that cancels it and returns its error.
func arrancar(t *testing.T, cfg ConfigDaemon) (func() error, *registro) {
	t.Helper()
	log := nuevoRegistro()
	cfg.Log = log
	ctx, cancel := context.WithCancel(context.Background())
	res := make(chan error, 1)
	go func() { res <- Servir(ctx, cfg) }()

	select {
	case <-log.listo:
	case err := <-res:
		cancel()
		t.Fatalf("Servir returned before it was ready: %v\nlog: %s", err, log.String())
	case <-time.After(10 * time.Second):
		cancel()
		<-res
		t.Fatalf("timeout waiting for srfd readiness\nlog: %s", log.String())
	}

	var once sync.Once
	var resultado error
	parar := func() error {
		once.Do(func() {
			cancel()
			resultado = <-res
		})
		return resultado
	}
	t.Cleanup(func() { _ = parar() })
	return parar, log
}

func nombres(t *testing.T, dir string) []string {
	t.Helper()
	entradas, err := os.ReadDir(dir)
	if err != nil {
		t.Fatalf("read dir %s: %v", dir, err)
	}
	var out []string
	for _, e := range entradas {
		out = append(out, e.Name())
	}
	sort.Strings(out)
	return out
}

func configDePrueba(t *testing.T, sockDir string, e *entorno) ConfigDaemon {
	t.Helper()
	return ConfigDaemon{
		SocketPath:      filepath.Join(sockDir, "srfd.sock"),
		ParadaPath:      e.parada,
		ParadaPlazaPath: e.plaza,
		Telemetria:      &gobernador.TelemetriaSintetica{DisponibleBytes: 64 << 30},
		Manager:         OpcionesManager{CapsulaBin: e.bin, RepoDir: e.dir, Timeout: 20 * time.Second},
		Gracia:          5 * time.Second,
	}
}

// servidorDockerFalso serves GET /containers/json on a unix socket, like the Docker Engine would.
func servidorDockerFalso(t *testing.T, estado int, cuerpo string) string {
	t.Helper()
	dir := corto(t)
	sock := filepath.Join(dir, "d.sock")
	l, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatalf("listen fake docker: %v", err)
	}
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/containers/json" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(estado)
		_, _ = io.WriteString(w, cuerpo)
	})}
	go func() { _ = srv.Serve(l) }()
	t.Cleanup(func() { _ = srv.Close() })
	return sock
}

const cabeceraTCP = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

func filaTCP(sl, puerto int, estado string) string {
	return fmt.Sprintf("%4d: 0100007F:%04X 00000000:0000 %s 00000000:00000000 00:00000000 00000000  1000        0 %d 1 0000000000000000 100 0 0 10 0\n", sl, puerto, estado, 1000+sl)
}

func filaTCP6(sl, puerto int, estado string) string {
	return fmt.Sprintf("%4d: 00000000000000000000000001000000:%04X 00000000000000000000000000000000:0000 %s 00000000:00000000 00:00000000 00000000  1000        0 %d 1 0000000000000000 100 0 0 10 0\n", sl, puerto, estado, 2000+sl)
}

// procFalso builds a procfs tree: one directory per pid holding comm, and net/tcp, net/tcp6.
func procFalso(t *testing.T, comms map[int]string, tcp, tcp6 string) string {
	t.Helper()
	dir := t.TempDir()
	for pid, comm := range comms {
		p := filepath.Join(dir, strconv.Itoa(pid))
		if err := os.MkdirAll(p, 0o755); err != nil {
			t.Fatal(err)
		}
		escribir(t, filepath.Join(p, "comm"), comm+"\n")
	}
	if err := os.MkdirAll(filepath.Join(dir, "net"), 0o755); err != nil {
		t.Fatal(err)
	}
	escribir(t, filepath.Join(dir, "net", "tcp"), tcp)
	escribir(t, filepath.Join(dir, "net", "tcp6"), tcp6)
	return dir
}

const yo = 4242 // the pid the fake procfs treats as the census process itself

// ---------------------------------------------------------------------------------------------
// Socket and kernel identity
// ---------------------------------------------------------------------------------------------

// TestSocketNaceConModo0600 guards CrearSocket: the socket must exist as 0600 the moment bind returns.
func TestSocketNaceConModo0600(t *testing.T) {
	p := filepath.Join(corto(t), "s.sock")
	l, err := CrearSocket(p)
	if err != nil {
		t.Fatalf("CrearSocket: %v", err)
	}
	defer l.Close()

	fi, err := os.Lstat(p)
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode()&os.ModeSocket == 0 || fi.Mode().Perm() != 0o600 {
		t.Fatalf("socket mode = %v, want a 0600 socket", fi.Mode())
	}
}

// TestSocketNoPisaUnFicheroRegular guards retirarSocketObsoleto: a regular file at the socket path is never removed.
func TestSocketNoPisaUnFicheroRegular(t *testing.T) {
	p := filepath.Join(corto(t), "s.sock")
	escribir(t, p, "precious\n")

	l, err := CrearSocket(p)
	if err == nil {
		l.Close()
		t.Fatal("CrearSocket succeeded over a regular file")
	}
	if data, rerr := os.ReadFile(p); rerr != nil || string(data) != "precious\n" {
		t.Fatalf("the regular file was disturbed: %q, %v", data, rerr)
	}
}

// TestSocketNoPisaUnDaemonVivo guards retirarSocketObsoleto: a socket a live process serves is never replaced.
func TestSocketNoPisaUnDaemonVivo(t *testing.T) {
	p := filepath.Join(corto(t), "s.sock")
	vivo, err := net.Listen("unix", p)
	if err != nil {
		t.Fatal(err)
	}
	defer vivo.Close()
	go func() {
		for {
			c, aerr := vivo.Accept()
			if aerr != nil {
				return
			}
			c.Close()
		}
	}()

	l, err := CrearSocket(p)
	if err == nil {
		l.Close()
		t.Fatal("CrearSocket replaced a socket served by a live process")
	}
	c, derr := net.Dial("unix", p)
	if derr != nil {
		t.Fatalf("the live socket stopped answering: %v", derr)
	}
	c.Close()
}

// TestRutaSocketPrecedencia guards RutaSocket: the explicit value wins over $SRF_SOCKET, which wins over the runtime directory.
func TestRutaSocketPrecedencia(t *testing.T) {
	t.Setenv("SRF_SOCKET", "/a/env.sock")
	t.Setenv("XDG_RUNTIME_DIR", "/run/x")
	if got := RutaSocket("/a/flag.sock"); got != "/a/flag.sock" {
		t.Fatalf("explicit value lost to the environment: %q", got)
	}
	if got := RutaSocket(""); got != "/a/env.sock" {
		t.Fatalf("$SRF_SOCKET ignored: %q", got)
	}
	t.Setenv("SRF_SOCKET", "")
	if got := RutaSocket(""); got != "/run/x/opensuperficies/srfd.sock" {
		t.Fatalf("runtime directory default = %q", got)
	}
}

// TestDirectorioAjenoSeRechaza guards asegurarDirectorio: a socket directory that belongs to another
// uid is never used (the socket and the journal would live where someone else can replace them).
func TestDirectorioAjenoSeRechaza(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("running as root: no directory belongs to a different uid")
	}
	if creado, err := asegurarDirectorio("/usr"); err == nil || creado {
		t.Fatalf("asegurarDirectorio(/usr) = %v, %v; want a refusal (the directory belongs to root)", creado, err)
	}
	propio := corto(t)
	if creado, err := asegurarDirectorio(propio); err != nil || creado {
		t.Fatalf("a directory of this uid was not accepted as pre-existing: %v, %v", creado, err)
	}
}

// TestPeerCredLeeLaIdentidadDelKernel guards GetPeerCred: uid, gid and pid come from the kernel and
// are compared with what this process knows about itself (the client and the server are one process).
func TestPeerCredLeeLaIdentidadDelKernel(t *testing.T) {
	p := filepath.Join(corto(t), "c.sock")
	l, err := CrearSocket(p)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()

	resultado := make(chan *PeerCredentials, 1)
	fallo := make(chan error, 1)
	go func() {
		conn, aerr := l.Accept()
		if aerr != nil {
			fallo <- aerr
			return
		}
		defer conn.Close()
		cred, cerr := GetPeerCred(conn)
		if cerr != nil {
			fallo <- cerr
			return
		}
		resultado <- cred
	}()

	cliente, err := net.Dial("unix", p)
	if err != nil {
		t.Fatal(err)
	}
	defer cliente.Close()

	select {
	case err := <-fallo:
		t.Fatalf("server side: %v", err)
	case cred := <-resultado:
		if cred.UID != uint32(os.Getuid()) || cred.GID != uint32(os.Getgid()) || cred.PID != int32(os.Getpid()) {
			t.Fatalf("SO_PEERCRED = %+v, this process is uid=%d gid=%d pid=%d", *cred, os.Getuid(), os.Getgid(), os.Getpid())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timeout reading SO_PEERCRED")
	}
}

// TestServidorAdmiteSoloAlUIDConfigurado guards the admission check in atender: a caller whose kernel
// uid is not the admitted one is refused with RCPeerRechazado and nothing is executed for it.
func TestServidorAdmiteSoloAlUIDConfigurado(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	propio := uint32(os.Getuid())
	otro := propio + 1

	// A daemon that admits some other uid must refuse this process.
	cfg := configDePrueba(t, corto(t), e)
	cfg.UIDPermitido = &otro
	parar, log := arrancar(t, cfg)
	resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "estado"}, 5*time.Second)
	if err != nil {
		t.Fatalf("EnviarComando: %v", err)
	}
	if resp.OK || resp.RC != RCPeerRechazado {
		t.Fatalf("a foreign uid was not refused: %+v", *resp)
	}
	if !strings.Contains(log.String(), "refused peer") {
		t.Fatalf("the refusal was not logged: %q", log.String())
	}
	if err := parar(); err != nil {
		t.Fatalf("teardown: %v", err)
	}

	// The same request against a daemon that admits this uid succeeds.
	cfg2 := configDePrueba(t, corto(t), e)
	cfg2.UIDPermitido = &propio
	arrancar(t, cfg2)
	resp, err = EnviarComando(cfg2.SocketPath, Request{Comando: "estado"}, 5*time.Second)
	if err != nil || !resp.OK {
		t.Fatalf("the admitted uid was refused: %v %+v", err, resp)
	}
}

// ---------------------------------------------------------------------------------------------
// Stop signal
// ---------------------------------------------------------------------------------------------

// casosSenal are the contents a single signal file can have, with what scripts/ng/campana.sh
// (leer_parada) prints for each: the reading rules of the stop signal in one table.
var casosSenal = []struct {
	nombre    string
	contenido string // "-" absent, "DIR" a directory, "0000" a regular file nobody may read
	lectura   string
}{
	{"LIBRE with a timestamp", paradaLibre, SenalLibre},
	{"LIBRE without a newline", "LIBRE", SenalLibre},
	{"LIBRE after leading blanks", "  \tLIBRE later\n", SenalLibre},
	{"LIBRE and a second line", "LIBRE\nPARADA\n", SenalLibre},
	{"PARADA", paradaActiva, SenalParada},
	{"empty file", "", SenalParada},
	{"blank first line", "\nLIBRE\n", SenalParada},
	{"LIBRE only on the second line", "PARADA\nLIBRE\n", SenalParada},
	{"LIBREX", "LIBREX ya\n", SenalParada},
	{"lowercase", "libre\n", SenalParada},
	{"carriage return after the word", "LIBRE\r\n", SenalParada},
	{"missing file", "-", SenalAusente},
	{"directory instead of file", "DIR", SenalAusente},
	{"unreadable file", "0000", SenalParada},
}

// prepararSenal creates the file (or the directory, or the absence) a case describes and returns its path.
func prepararSenal(t *testing.T, dir, nombre, contenido string) string {
	t.Helper()
	ruta := filepath.Join(dir, nombre)
	switch contenido {
	case "-":
	case "DIR":
		if err := os.Mkdir(ruta, 0o755); err != nil {
			t.Fatal(err)
		}
	case "0000":
		escribir(t, ruta, paradaLibre)
		if err := os.Chmod(ruta, 0); err != nil {
			t.Fatal(err)
		}
		if f, err := os.Open(ruta); err == nil { // running as root: mode 0 does not stop the read
			f.Close()
			t.Fatalf("this test needs a file the current user cannot read (euid %d)", os.Geteuid())
		}
	default:
		escribir(t, ruta, contenido)
	}
	return ruta
}

// TestSenalSeLeeComoCampanaSh guards LeerSenal: each file content reads as the table says, AND the reading
// equals what scripts/ng/campana.sh prints for the same file (a second, independent implementation of the rules).
func TestSenalSeLeeComoCampanaSh(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Fatal("the unreadable-file case cannot be built as root")
	}
	script := filepath.Join(findRepoRoot(), "scripts", "ng", "campana.sh")
	if _, err := os.Stat(script); err != nil {
		t.Fatalf("the campaign instrument is required to cross-check the reading: %v", err)
	}
	for _, c := range casosSenal {
		t.Run(c.nombre, func(t *testing.T) {
			dir := t.TempDir()
			ruta := prepararSenal(t, dir, "senal.txt", c.contenido)
			if got := LeerSenal(ruta); got != c.lectura {
				t.Fatalf("LeerSenal = %s, want %s", got, c.lectura)
			}

			// The same content as the repository file, then as the Plaza file, read by campana.sh.
			for _, lado := range []string{"repo", "plaza"} {
				repo, sala := t.TempDir(), t.TempDir()
				objetivo := repo
				if lado == "plaza" {
					objetivo = sala
				}
				prepararSenal(t, objetivo, "PARADA.txt", c.contenido)
				escribir(t, filepath.Join(map[string]string{"repo": sala, "plaza": repo}[lado], "PARADA.txt"), paradaLibre)

				cmd := exec.Command("bash", script, "parada")
				cmd.Env = append(os.Environ(), "SRF_REPO="+repo, "SRF_SALA="+sala)
				salida, _ := cmd.Output() // rc 3 is the expected answer under PARADA
				var vista struct{ Repo, Plaza, Efectivo string }
				if err := json.Unmarshal(salida, &vista); err != nil {
					t.Fatalf("campana.sh parada printed %q: %v", salida, err)
				}

				t.Setenv("SRF_REPO", repo)
				t.Setenv("SRF_SALA", sala)
				t.Setenv("SRF_PARADA_REPO", "")
				t.Setenv("SRF_PARADA_PLAZA", "")
				gr, gp, ge := NuevoParadaManager("", "").Lecturas()
				if gr != vista.Repo || gp != vista.Plaza || ge != vista.Efectivo {
					t.Fatalf("%s side: daemon reads %s/%s/%s, campana.sh reads %s/%s/%s", lado, gr, gp, ge, vista.Repo, vista.Plaza, vista.Efectivo)
				}
			}
		})
	}
}

// TestParadaEfectivaEsElParDeFicheros guards ParadaManager and Efectiva: the effective signal is LIBRE only
// when BOTH the repository file and the Plaza file read LIBRE. The four states of the pair, then the absent ones.
func TestParadaEfectivaEsElParDeFicheros(t *testing.T) {
	casos := []struct {
		nombre, repo, plaza, efectiva string
	}{
		{"LIBRE / LIBRE", paradaLibre, paradaLibre, SenalLibre},
		{"LIBRE / PARADA", paradaLibre, paradaActiva, SenalParada},
		{"PARADA / LIBRE", paradaActiva, paradaLibre, SenalParada},
		{"PARADA / PARADA", paradaActiva, paradaActiva, SenalParada},
		{"LIBRE / plaza missing", paradaLibre, "-", SenalParada},
		{"repo missing / LIBRE", "-", paradaLibre, SenalParada},
		{"both missing", "-", "-", SenalParada},
		{"LIBRE / plaza empty", paradaLibre, "", SenalParada},
	}
	for _, c := range casos {
		t.Run(c.nombre, func(t *testing.T) {
			dir := t.TempDir()
			repo := filepath.Join(dir, "repo.txt")
			plaza := filepath.Join(dir, "plaza.txt")
			if c.repo != "-" {
				escribir(t, repo, c.repo)
			}
			if c.plaza != "-" {
				escribir(t, plaza, c.plaza)
			}
			pm := NuevoParadaManager(repo, plaza)
			if pm.Ruta() != repo || pm.RutaPlaza() != plaza {
				t.Fatalf("the reader consults %s and %s, not the files it was given", pm.Ruta(), pm.RutaPlaza())
			}
			estado, libre := pm.Estado()
			if estado != c.efectiva || libre != (c.efectiva == SenalLibre) {
				t.Fatalf("Estado() = %s/%v, want %s", estado, libre, c.efectiva)
			}
			if err := pm.ExigirLibre(); errors.Is(err, ErrParadaActiva) != (c.efectiva != SenalLibre) {
				t.Fatalf("ExigirLibre() = %v under an effective %s", err, c.efectiva)
			}
		})
	}
}

// TestParadaLeeAmbosFicherosCadaVez guards the fact that nothing is cached: flipping either file while the
// reader lives changes its very next answer, in both directions.
func TestParadaLeeAmbosFicherosCadaVez(t *testing.T) {
	dir := t.TempDir()
	repo, plaza := filepath.Join(dir, "r"), filepath.Join(dir, "p")
	escribir(t, repo, paradaLibre)
	escribir(t, plaza, paradaLibre)
	pm := NuevoParadaManager(repo, plaza)
	if err := pm.ExigirLibre(); err != nil {
		t.Fatalf("both LIBRE: %v", err)
	}
	for nombre, fichero := range map[string]string{"repository": repo, "Plaza": plaza} {
		escribir(t, fichero, paradaActiva)
		if err := pm.ExigirLibre(); !errors.Is(err, ErrParadaActiva) {
			t.Fatalf("a PARADA written into the %s file was not seen: %v", nombre, err)
		}
		escribir(t, fichero, paradaLibre)
		if err := pm.ExigirLibre(); err != nil {
			t.Fatalf("the %s file went back to LIBRE but the reader still refuses: %v", nombre, err)
		}
	}
}

// TestRutasDeLaSenal guards the resolution of both paths: explicit value, then the specific variable,
// then (Plaza only) $SRF_SALA, then the campaign defaults.
func TestRutasDeLaSenal(t *testing.T) {
	t.Setenv("SRF_REPO", "/r")
	t.Setenv("SRF_PARADA_REPO", "")
	t.Setenv("SRF_PARADA_PLAZA", "")
	t.Setenv("SRF_SALA", "")
	if got := RutaParadaRepo(""); got != "/r/PARADA.txt" {
		t.Fatalf("repository default = %q", got)
	}
	if got := RutaParadaPlaza(""); got != PlazaSalaPorDefecto+"/PARADA.txt" {
		t.Fatalf("Plaza default = %q", got)
	}
	t.Setenv("SRF_SALA", "/sala")
	if got := RutaParadaPlaza(""); got != "/sala/PARADA.txt" {
		t.Fatalf("$SRF_SALA ignored: %q", got)
	}
	t.Setenv("SRF_PARADA_PLAZA", "/env/plaza.txt")
	t.Setenv("SRF_PARADA_REPO", "/env/repo.txt")
	if RutaParadaPlaza("") != "/env/plaza.txt" || RutaParadaRepo("") != "/env/repo.txt" {
		t.Fatalf("the specific variables lost to a default: %q %q", RutaParadaPlaza(""), RutaParadaRepo(""))
	}
	if RutaParadaPlaza("/x/p") != "/x/p" || RutaParadaRepo("/x/r") != "/x/r" {
		t.Fatal("an explicit path lost to the environment")
	}
}

// pares are the effective-PARADA states of the (repository, Plaza) pair used by the mutation tests.
var pares = []struct{ nombre, repo, plaza string }{
	{"repository PARADA", paradaActiva, ""},
	{"Plaza PARADA", "", paradaActiva},
	{"both PARADA", paradaActiva, paradaActiva},
	{"repository missing", "-", ""},
	{"Plaza missing", "", "-"},
	{"both missing", "-", "-"},
	{"Plaza empty", "", "\n"},
}

// TestParadaEfectivaRechazaTodaMutacionSinTocarEstado guards the guard at the top of both mutating
// verbs, for every state of the pair in which the effective signal is PARADA. A refused request must leave
// no trace: no dispatcher call, no journal record, no reservation.
func TestParadaEfectivaRechazaTodaMutacionSinTocarEstado(t *testing.T) {
	for _, par := range pares {
		t.Run(par.nombre, func(t *testing.T) {
			e := nuevoEntorno(t, opcionesEntorno{parada: par.repo, plaza: par.plaza})
			ctx := context.Background()

			if _, err := e.mgr.IniciarCapsula(ctx, "browser", 5); !errors.Is(err, ErrParadaActiva) {
				t.Fatalf("IniciarCapsula under PARADA = %v, want ErrParadaActiva", err)
			}
			if err := e.mgr.DetenerCapsula(ctx, 5, "browser"); !errors.Is(err, ErrParadaActiva) {
				t.Fatalf("DetenerCapsula under PARADA = %v, want ErrParadaActiva", err)
			}

			if l := lineas(t, e.log); len(l) != 0 {
				t.Fatalf("the dispatcher ran under PARADA: %q", l)
			}
			if regs := e.registrosWAL(t); len(regs) != 0 {
				t.Fatalf("the journal was written under PARADA: %d records", len(regs))
			}
			if n := e.vivas(t); n != 0 {
				t.Fatalf("a reservation was taken under PARADA: vivas=%d", n)
			}

			// Read-only verbs keep answering under PARADA.
			est, err := e.mgr.Estado()
			if err != nil || est.Parada != "PARADA" {
				t.Fatalf("Estado under PARADA = %+v, %v", est, err)
			}
			if len(e.mgr.Listar()) != 0 {
				t.Fatal("Listar returned surfaces that were never started")
			}
		})
	}
}

// TestParadaVaAntesQueLaValidacion guards the ORDER of the guard: with an effective PARADA a mutating verb
// answers ErrParadaActiva (rc 3) whatever its arguments are, even an invalid class; the same arguments
// answer ErrClaseInvalida (rc 2) when the signal is LIBRE, so the first answer is not a constant.
func TestParadaVaAntesQueLaValidacion(t *testing.T) {
	ctx := context.Background()
	activa := nuevoEntorno(t, opcionesEntorno{plaza: paradaActiva})
	if _, err := activa.mgr.IniciarCapsula(ctx, "no-such-class; rm -rf x", 5); !errors.Is(err, ErrParadaActiva) {
		t.Fatalf("IniciarCapsula(invalid class) under PARADA = %v, want ErrParadaActiva", err)
	}
	if err := activa.mgr.DetenerCapsula(ctx, 5, "../../x"); !errors.Is(err, ErrParadaActiva) {
		t.Fatalf("DetenerCapsula(invalid class) under PARADA = %v, want ErrParadaActiva", err)
	}
	if err := activa.mgr.DetenerCapsula(ctx, 9, ""); !errors.Is(err, ErrParadaActiva) {
		t.Fatalf("DetenerCapsula(unknown slot, no class) under PARADA = %v, want ErrParadaActiva", err)
	}

	libre := nuevoEntorno(t, opcionesEntorno{})
	if _, err := libre.mgr.IniciarCapsula(ctx, "no-such-class; rm -rf x", 5); !errors.Is(err, ErrClaseInvalida) {
		t.Fatalf("IniciarCapsula(invalid class) under LIBRE = %v, want ErrClaseInvalida", err)
	}
	if err := libre.mgr.DetenerCapsula(ctx, 5, "../../x"); !errors.Is(err, ErrClaseInvalida) {
		t.Fatalf("DetenerCapsula(invalid class) under LIBRE = %v, want ErrClaseInvalida", err)
	}
}

// TestParadaVaAntesQueElDecodificadorDeArgumentos guards the same order on the wire: a mutating request whose
// arguments do not even decode (slot 70000 overflows uint16, a class that is a number) answers rc 3 under an
// effective PARADA and rc 2 under LIBRE; a line that is not json at all has no verb and is rc 2 in both.
func TestParadaVaAntesQueElDecodificadorDeArgumentos(t *testing.T) {
	malas := [][]byte{
		[]byte(`{"comando":"iniciar","clase":"browser","slot":70000}` + "\n"),
		[]byte(`{"comando":"iniciar","clase":7,"slot":5}` + "\n"),
		[]byte(`{"comando":"detener","slot":"cinco"}` + "\n"),
	}
	for nombre, parada := range map[string]string{"PARADA": paradaActiva, "LIBRE": ""} {
		e := nuevoEntorno(t, opcionesEntorno{plaza: parada})
		cfg := configDePrueba(t, corto(t), e)
		arrancar(t, cfg)
		esperado := RCUso
		if parada != "" {
			esperado = RCParada
		}
		for _, linea := range malas {
			resp, err := intercambioCrudo(t, cfg.SocketPath, linea, 5*time.Second)
			if err != nil || resp.OK || resp.RC != esperado {
				t.Fatalf("%s: %q = %+v, %v; want rc %d", nombre, linea, resp, err, esperado)
			}
		}
		resp, err := intercambioCrudo(t, cfg.SocketPath, []byte("this is not json\n"), 5*time.Second)
		if err != nil || resp.OK || resp.RC != RCUso {
			t.Fatalf("%s: a line that is not json = %+v, %v; want rc %d", nombre, resp, err, RCUso)
		}
		if l := lineas(t, e.log); len(l) != 0 {
			t.Fatalf("%s: the dispatcher ran: %q", nombre, l)
		}
	}
}

// guardiaQueEscribeUnaSenal reads the real pair of files, and just before the n-th read it writes PARADA
// into one of them: it proves WHICH read (and which file) stops a request that had already been admitted.
type guardiaQueEscribeUnaSenal struct {
	t       *testing.T
	pm      *ParadaManager
	fichero string
	libres  int // reads that see the untouched files before the file is changed
}

func (g *guardiaQueEscribeUnaSenal) Estado() (string, bool) { return g.pm.Estado() }
func (g *guardiaQueEscribeUnaSenal) ExigirLibre() error {
	if g.libres == 0 {
		escribir(g.t, g.fichero, paradaActiva)
	} else {
		g.libres--
	}
	return g.pm.ExigirLibre()
}

// TestParadaSeReleeAntesDelDespacho guards the second read of the guard, immediately before the
// dispatch: a PARADA that appears after admission and lease, in EITHER file, must still stop the start,
// and the admission and the lease already taken must be given back.
func TestParadaSeReleeAntesDelDespacho(t *testing.T) {
	casos := map[string]func(e *entorno) Guardia{
		"fake guard": func(e *entorno) Guardia { return &guardiaQueSeVuelvePARADA{libres: 1} },
		"repository file written between the reads": func(e *entorno) Guardia {
			return &guardiaQueEscribeUnaSenal{t: t, pm: NuevoParadaManager(e.parada, e.plaza), fichero: e.parada, libres: 1}
		},
		"Plaza file written between the reads": func(e *entorno) Guardia {
			return &guardiaQueEscribeUnaSenal{t: t, pm: NuevoParadaManager(e.parada, e.plaza), fichero: e.plaza, libres: 1}
		},
	}
	for nombre, guardia := range casos {
		t.Run(nombre, func(t *testing.T) {
			e := nuevoEntorno(t, opcionesEntorno{})
			e.mgr.guardia = guardia(e) // the entorno exists first: the guard needs its files

			_, err := e.mgr.IniciarCapsula(context.Background(), "browser", 5)
			if !errors.Is(err, ErrParadaActiva) {
				t.Fatalf("IniciarCapsula = %v, want ErrParadaActiva from the second read", err)
			}
			if l := lineas(t, e.log); len(l) != 0 {
				t.Fatalf("the dispatcher ran although PARADA appeared before the dispatch: %q", l)
			}
			if n := e.vivas(t); n != 0 {
				t.Fatalf("admission not returned: vivas=%d", n)
			}
			if len(e.mgr.Listar()) != 0 {
				t.Fatal("the slot stayed reserved")
			}
			if got := e.revocaciones(t); got != 1 {
				t.Fatalf("the lease was not revoked in the journal (revocations=%d)", got)
			}
		})
	}
}

// TestParadaSeReleeAntesDelDespachoDeDetener is the same guarantee for a stop: the capsule is running,
// a PARADA appears after the first read of the stop, and the dispatcher must not be called; the slot
// stays tracked as listo with its reservation and lease, so a later stop under LIBRE can still release it.
func TestParadaSeReleeAntesDelDespachoDeDetener(t *testing.T) {
	for _, fichero := range []string{"repository", "Plaza"} {
		t.Run(fichero, func(t *testing.T) {
			e := nuevoEntorno(t, opcionesEntorno{})
			ctx := context.Background()
			if _, err := e.mgr.IniciarCapsula(ctx, "browser", 5); err != nil {
				t.Fatalf("IniciarCapsula: %v", err)
			}
			objetivo := e.parada
			if fichero == "Plaza" {
				objetivo = e.plaza
			}
			// One read for the top of the stop sees LIBRE; the read before the dispatch sees the PARADA.
			e.mgr.guardia = &guardiaQueEscribeUnaSenal{t: t, pm: NuevoParadaManager(e.parada, e.plaza), fichero: objetivo, libres: 1}

			if err := e.mgr.DetenerCapsula(ctx, 5, ""); !errors.Is(err, ErrParadaActiva) {
				t.Fatalf("DetenerCapsula = %v, want ErrParadaActiva from the second read", err)
			}
			for _, l := range lineas(t, e.log) {
				if strings.HasPrefix(l, "detener") {
					t.Fatalf("the dispatcher stopped a capsule although PARADA appeared before the dispatch: %q", l)
				}
			}
			if listado := e.mgr.Listar(); len(listado) != 1 || listado[0].Estado != EstadoListo {
				t.Fatalf("the refused stop must leave the slot tracked as %q: %+v", EstadoListo, listado)
			}
			if n := e.vivas(t); n != 1 {
				t.Fatalf("the reservation must be kept: vivas=%d", n)
			}
			if got := e.revocaciones(t); got != 0 {
				t.Fatalf("the lease was revoked although the capsule was not stopped (%d revocations)", got)
			}

			escribir(t, objetivo, paradaLibre)
			e.mgr.guardia = NuevoParadaManager(e.parada, e.plaza)
			if err := e.mgr.DetenerCapsula(ctx, 5, ""); err != nil {
				t.Fatalf("the stop after the PARADA was lifted: %v", err)
			}
		})
	}
}

// ---------------------------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------------------------

// TestClaseInvalidaNoLlegaAlLanzador guards the closed set of classes: nothing but a known class is
// ever passed to the dispatcher, whatever text a caller sends.
func TestClaseInvalidaNoLlegaAlLanzador(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	ctx := context.Background()

	if _, err := e.mgr.IniciarCapsula(ctx, "browser; touch pwned", 5); !errors.Is(err, ErrClaseInvalida) {
		t.Fatalf("IniciarCapsula with a hostile class = %v, want ErrClaseInvalida", err)
	}
	if err := e.mgr.DetenerCapsula(ctx, 5, "../../x"); !errors.Is(err, ErrClaseInvalida) {
		t.Fatalf("DetenerCapsula with a hostile class = %v, want ErrClaseInvalida", err)
	}
	if l := lineas(t, e.log); len(l) != 0 {
		t.Fatalf("the dispatcher ran for an invalid class: %q", l)
	}
	if _, err := os.Stat(filepath.Join(e.dir, "pwned")); err == nil {
		t.Fatal("a shell command embedded in the class was executed")
	}
}

// TestDetenerNombraSiempreLaClase guards the rule that cost this campaign two attempts: every stop
// passes --clase explicitly, taken from the mediated slot; a request that lets the dispatcher assume
// "browser" is refused.
func TestDetenerNombraSiempreLaClase(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	ctx := context.Background()

	if _, err := e.mgr.IniciarCapsula(ctx, "desktop", 5); err != nil {
		t.Fatalf("IniciarCapsula desktop: %v", err)
	}
	if err := e.mgr.DetenerCapsula(ctx, 5, "browser"); !errors.Is(err, ErrClaseDistinta) {
		t.Fatalf("stopping a desktop slot as browser = %v, want ErrClaseDistinta", err)
	}
	if err := e.mgr.DetenerCapsula(ctx, 9, ""); !errors.Is(err, ErrSlotAusente) {
		t.Fatalf("stopping an unknown slot without a class = %v, want ErrSlotAusente", err)
	}
	if l := lineas(t, e.log); len(l) != 1 {
		t.Fatalf("a refused stop reached the dispatcher: %q", l)
	}

	if err := e.mgr.DetenerCapsula(ctx, 5, ""); err != nil {
		t.Fatalf("DetenerCapsula (class inferred from the slot): %v", err)
	}
	if err := e.mgr.DetenerCapsula(ctx, 9, "microvm"); err != nil {
		t.Fatalf("DetenerCapsula (unknown slot, class named): %v", err)
	}
	esperado := []string{
		"iniciar --clase desktop --slot 5 --json",
		"detener --clase desktop --slot 5 --json",
		"detener --clase microvm --slot 9 --json",
	}
	if got := lineas(t, e.log); strings.Join(got, "|") != strings.Join(esperado, "|") {
		t.Fatalf("dispatcher calls:\n got %q\nwant %q", got, esperado)
	}
	if n := e.vivas(t); n != 0 {
		t.Fatalf("the reservation of the stopped slot was not released: vivas=%d", n)
	}
}

// TestIniciarFallidoDevuelveLoQueTomo guards the rollback of a start whose dispatcher fails: the
// reservation is returned, the lease is revoked in the journal (re-read from disk) and the slot is free.
func TestIniciarFallidoDevuelveLoQueTomo(t *testing.T) {
	t.Setenv("SRF_TEST_MODO", "fallo")
	e := nuevoEntorno(t, opcionesEntorno{})

	_, err := e.mgr.IniciarCapsula(context.Background(), "browser", 5)
	if err == nil || !strings.Contains(err.Error(), "simulated start failure") {
		t.Fatalf("IniciarCapsula = %v, want the dispatcher's own stderr in the error", err)
	}
	if n := e.vivas(t); n != 0 {
		t.Fatalf("the admission was not returned: vivas=%d", n)
	}
	if len(e.mgr.Listar()) != 0 {
		t.Fatal("the slot stayed reserved after a failed start")
	}
	if got := e.revocaciones(t); got != 1 {
		t.Fatalf("the journal shows %d lease revocations, want 1", got)
	}
}

// TestIniciarDeshaceUnaRespuestaIlegible guards the teardown of a capsule that started but whose
// answer cannot be used: it exists, so it must be stopped, and the stop must name the class.
func TestIniciarDeshaceUnaRespuestaIlegible(t *testing.T) {
	t.Setenv("SRF_TEST_MODO", "basura")
	e := nuevoEntorno(t, opcionesEntorno{})

	if _, err := e.mgr.IniciarCapsula(context.Background(), "browser", 5); err == nil {
		t.Fatal("IniciarCapsula accepted an unreadable answer")
	}
	esperado := "iniciar --clase browser --slot 5 --json|detener --clase browser --slot 5 --json"
	if got := strings.Join(lineas(t, e.log), "|"); got != esperado {
		t.Fatalf("dispatcher calls = %q, want %q (the started capsule must be stopped)", got, esperado)
	}
	if n := e.vivas(t); n != 0 || len(e.mgr.Listar()) != 0 {
		t.Fatalf("state left behind: vivas=%d listar=%d", n, len(e.mgr.Listar()))
	}
}

// TestDetenerNoOcultaUnaRevocacionSinDiarizar guards the success path of a stop: when the lease
// revocation cannot be journalled the stop still frees the slot but REPORTS the failure.
func TestDetenerNoOcultaUnaRevocacionSinDiarizar(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	ctx := context.Background()
	if _, err := e.mgr.IniciarCapsula(ctx, "browser", 5); err != nil {
		t.Fatalf("IniciarCapsula: %v", err)
	}
	if err := e.libro.Cerrar(); err != nil { // the journal becomes unwritable
		t.Fatalf("closing the journal: %v", err)
	}

	err := e.mgr.DetenerCapsula(ctx, 5, "")
	if err == nil || !strings.Contains(err.Error(), "revocation") {
		t.Fatalf("DetenerCapsula = %v, want an error naming the lease revocation", err)
	}
	if n := e.vivas(t); n != 0 || len(e.mgr.Listar()) != 0 {
		t.Fatalf("the capsule was stopped but its slot was not freed: vivas=%d listar=%d", n, len(e.mgr.Listar()))
	}
}

// TestSlotSeReservaAntesDeLanzar guards the slot reservation: while a start is still running, a
// second start of the same slot is refused at once and the dispatcher runs exactly once.
func TestSlotSeReservaAntesDeLanzar(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	arranque := filepath.Join(e.dir, "arranque")
	t.Setenv("SRF_TEST_ARRANQUE", arranque)
	t.Setenv("SRF_TEST_ESPERA", "1")

	primero := make(chan error, 1)
	go func() {
		_, err := e.mgr.IniciarCapsula(context.Background(), "browser", 5)
		primero <- err
	}()

	esperarArchivo(t, arranque)

	if _, err := e.mgr.IniciarCapsula(context.Background(), "browser", 5); !errors.Is(err, ErrSlotOcupado) {
		t.Fatalf("second start of a slot that is starting = %v, want ErrSlotOcupado", err)
	}
	if err := <-primero; err != nil {
		t.Fatalf("first start: %v", err)
	}
	if got := len(lineas(t, e.log)); got != 1 {
		t.Fatalf("the dispatcher ran %d times for one slot", got)
	}
}

// TestDetenerFallidoConservaLaCapsula guards the failure path of a stop: a capsule whose stop failed
// may still exist, so it stays tracked as listo with its reservation, and a retry can still release it.
func TestDetenerFallidoConservaLaCapsula(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	ctx := context.Background()
	if _, err := e.mgr.IniciarCapsula(ctx, "browser", 5); err != nil {
		t.Fatalf("IniciarCapsula: %v", err)
	}

	t.Setenv("SRF_TEST_MODO_DETENER", "fallo")
	err := e.mgr.DetenerCapsula(ctx, 5, "")
	if err == nil || !strings.Contains(err.Error(), "simulated stop failure") {
		t.Fatalf("DetenerCapsula = %v, want the dispatcher's own stderr in the error", err)
	}
	if listado := e.mgr.Listar(); len(listado) != 1 || listado[0].Estado != EstadoListo {
		t.Fatalf("a capsule whose stop failed must stay tracked as %q: %+v", EstadoListo, listado)
	}
	if n := e.vivas(t); n != 1 {
		t.Fatalf("its reservation must be kept: vivas=%d", n)
	}
	if got := e.revocaciones(t); got != 0 {
		t.Fatalf("its lease was revoked although it may still be running (%d revocations)", got)
	}

	t.Setenv("SRF_TEST_MODO_DETENER", "")
	if err := e.mgr.DetenerCapsula(ctx, 5, ""); err != nil {
		t.Fatalf("the retry: %v", err)
	}
	if n := e.vivas(t); n != 0 || len(e.mgr.Listar()) != 0 {
		t.Fatalf("the retry did not release the slot: vivas=%d listar=%d", n, len(e.mgr.Listar()))
	}
}

// TestIniciarConTeardownFallidoDejaLaCapsulaHuerfana guards the double failure: the start answer is
// unusable AND the teardown fails. The capsule may exist, so the slot stays tracked as huerfana with its
// reservation and lease (not silently forgotten), the error names both failures, and a later stop releases it.
func TestIniciarConTeardownFallidoDejaLaCapsulaHuerfana(t *testing.T) {
	t.Setenv("SRF_TEST_MODO", "basura")
	t.Setenv("SRF_TEST_MODO_DETENER", "fallo")
	e := nuevoEntorno(t, opcionesEntorno{})
	ctx := context.Background()

	_, err := e.mgr.IniciarCapsula(ctx, "browser", 5)
	if err == nil || !strings.Contains(err.Error(), EstadoHuerfana) || !strings.Contains(err.Error(), "simulated stop failure") {
		t.Fatalf("IniciarCapsula = %v, want an error naming the orphan slot and the failed teardown", err)
	}
	if listado := e.mgr.Listar(); len(listado) != 1 || listado[0].Estado != EstadoHuerfana {
		t.Fatalf("the slot must stay tracked as %q: %+v", EstadoHuerfana, listado)
	}
	if n := e.vivas(t); n != 1 {
		t.Fatalf("the reservation must be kept for a capsule that may exist: vivas=%d", n)
	}
	if got := e.revocaciones(t); got != 0 {
		t.Fatalf("the lease was revoked although the capsule may exist (%d revocations)", got)
	}

	t.Setenv("SRF_TEST_MODO_DETENER", "")
	if err := e.mgr.DetenerCapsula(ctx, 5, ""); err != nil {
		t.Fatalf("stopping the orphan: %v", err)
	}
	if n := e.vivas(t); n != 0 || len(e.mgr.Listar()) != 0 {
		t.Fatalf("stopping the orphan did not release it: vivas=%d listar=%d", n, len(e.mgr.Listar()))
	}
	if got := e.revocaciones(t); got != 1 {
		t.Fatalf("the orphan's lease was not revoked when it was stopped (%d revocations)", got)
	}
}

// TestDetenerConcurrenteNoLiberaDosVeces guards the deteniendo state: while a stop is running, a second
// stop of the same slot is refused, so the reservation and the lease are released exactly once.
func TestDetenerConcurrenteNoLiberaDosVeces(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	ctx := context.Background()
	if _, err := e.mgr.IniciarCapsula(ctx, "browser", 5); err != nil {
		t.Fatalf("IniciarCapsula: %v", err)
	}
	marcador := filepath.Join(e.dir, "detener-arranque")
	t.Setenv("SRF_TEST_ARRANQUE_DETENER", marcador)
	t.Setenv("SRF_TEST_ESPERA_DETENER", "1")

	primero := make(chan error, 1)
	go func() { primero <- e.mgr.DetenerCapsula(ctx, 5, "") }()
	esperarArchivo(t, marcador)

	if err := e.mgr.DetenerCapsula(ctx, 5, ""); !errors.Is(err, ErrSlotOcupado) {
		t.Fatalf("second stop of a slot that is stopping = %v, want ErrSlotOcupado", err)
	}
	if err := <-primero; err != nil {
		t.Fatalf("first stop: %v", err)
	}
	stops := 0
	for _, l := range lineas(t, e.log) {
		if strings.HasPrefix(l, "detener") {
			stops++
		}
	}
	if stops != 1 {
		t.Fatalf("the dispatcher stopped the slot %d times", stops)
	}
	if got := e.revocaciones(t); got != 1 {
		t.Fatalf("the lease was revoked %d times, want once", got)
	}
}

// ---------------------------------------------------------------------------------------------
// Micro-JSON budget and wire behaviour
// ---------------------------------------------------------------------------------------------

// TestEstadoPresupuestoDeTokens measures the payload srfd really sends for `estado` with the external
// tokenizer, and refuses an empty payload (a zero-byte answer measures 0 tokens and would pass a ceiling).
func TestEstadoPresupuestoDeTokens(t *testing.T) {
	tok, err := exec.LookPath("newton-verbo")
	if err != nil {
		t.Fatalf("newton-verbo is not on PATH, so the 120-token ceiling cannot be measured (this test never skips): %v", err)
	}
	e := nuevoEntorno(t, opcionesEntorno{})
	cfg := configDePrueba(t, corto(t), e)
	arrancar(t, cfg)

	resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "estado"}, 5*time.Second)
	if err != nil || !resp.OK {
		t.Fatalf("estado: %v %+v", err, resp)
	}
	if len(resp.Datos) == 0 {
		t.Fatal("estado answered with an empty payload")
	}
	var campos map[string]json.RawMessage
	if err := json.Unmarshal(resp.Datos, &campos); err != nil {
		t.Fatalf("estado payload is not a JSON object: %v", err)
	}
	for _, k := range []string{"estado", "parada", "vivas", "reservado_mib", "disponible_mib", "soberana_mib"} {
		if _, ok := campos[k]; !ok {
			t.Fatalf("estado payload lacks %q: %s", k, resp.Datos)
		}
	}
	if len(campos) != 6 {
		t.Fatalf("estado payload has %d fields, the contract fixes 6: %s", len(campos), resp.Datos)
	}

	cmd := exec.Command(tok, "tok")
	cmd.Stdin = bytes.NewReader(append(append([]byte{}, resp.Datos...), '\n'))
	out, err := cmd.Output()
	if err != nil {
		t.Fatalf("newton-verbo tok: %v", err)
	}
	n, err := strconv.Atoi(strings.TrimSpace(string(out)))
	if err != nil {
		t.Fatalf("newton-verbo tok printed %q", out)
	}
	if n < 1 || n > 120 {
		t.Fatalf("estado costs %d tokens, want 1..120: %s", n, resp.Datos)
	}
}

// TestRPCParadaDevuelveRC3 guards the mapping from ErrParadaActiva to the exit code on the wire, for every
// state of the (repository, Plaza) pair that is an effective PARADA.
func TestRPCParadaDevuelveRC3(t *testing.T) {
	for _, par := range pares {
		t.Run(par.nombre, func(t *testing.T) {
			e := nuevoEntorno(t, opcionesEntorno{parada: par.repo, plaza: par.plaza})
			cfg := configDePrueba(t, corto(t), e)
			arrancar(t, cfg)

			// The second request has an invalid class and slot 0: PARADA still answers first.
			for _, req := range []Request{{Comando: "iniciar", Clase: "browser", Slot: 5}, {Comando: "detener", Clase: "browser", Slot: 5}, {Comando: "iniciar", Clase: "???", Slot: 0}} {
				resp, err := EnviarComando(cfg.SocketPath, req, 5*time.Second)
				if err != nil {
					t.Fatalf("%s: %v", req.Comando, err)
				}
				if resp.OK || resp.RC != RCParada {
					t.Fatalf("%s %+v under PARADA = %+v, want rc %d", req.Comando, req, *resp, RCParada)
				}
			}
			for _, comando := range []string{"estado", "listar", "mediacion"} {
				resp, err := EnviarComando(cfg.SocketPath, Request{Comando: comando}, 10*time.Second)
				if err != nil || !resp.OK {
					t.Fatalf("%s under PARADA must still answer: %v %+v", comando, err, resp)
				}
			}
			if l := lineas(t, e.log); len(l) != 0 {
				t.Fatalf("the dispatcher ran under PARADA: %q", l)
			}
		})
	}
}

// ---------------------------------------------------------------------------------------------
// Mediation census
// ---------------------------------------------------------------------------------------------

// TestCensoDockerMuertoFallaCerrado: a Docker leg that cannot list containers must say so. Every
// case below has ajenos == 0, which is exactly what used to be indistinguishable from "did not look".
func TestCensoDockerMuertoFallaCerrado(t *testing.T) {
	proc := procFalso(t, map[int]string{yo: "test"}, cabeceraTCP, cabeceraTCP)
	casos := map[string]string{
		"dead socket":        filepath.Join(corto(t), "no-hay-docker.sock"),
		"server error":       servidorDockerFalso(t, http.StatusInternalServerError, `{"message":"boom"}`),
		"body is null":       servidorDockerFalso(t, http.StatusOK, `null`),
		"body is not a list": servidorDockerFalso(t, http.StatusOK, `{"containers":[]}`),
		"body is garbage":    servidorDockerFalso(t, http.StatusOK, `<html>not docker</html>`),
	}
	for nombre, socket := range casos {
		t.Run(nombre, func(t *testing.T) {
			rep := censar(context.Background(), CensoOpciones{DockerSocket: socket, ProcDir: proc, SelfPID: yo, RepoDir: t.TempDir()}, nuevoConjuntoMediado())
			if rep.Ajenos != 0 {
				t.Fatalf("the fixture is meant to have nothing foreign: %+v", rep)
			}
			if rep.CensoDockerOK {
				t.Fatalf("censo_docker_ok is true although the Docker leg did not run: %+v", rep)
			}
			if !rep.CensoProcfsOK {
				t.Fatalf("the procfs leg was healthy but censo_procfs_ok is false: %+v", rep)
			}
			if rep.Estado != CensoParcial || rep.RC() == 0 {
				t.Fatalf("estado=%q rc=%d, want %q and a non-zero rc", rep.Estado, rep.RC(), CensoParcial)
			}
			if len(rep.Errores) == 0 || !strings.HasPrefix(rep.Errores[0], "docker:") {
				t.Fatalf("the failure is not named: %v", rep.Errores)
			}
		})
	}
}

// TestCensoProcfsIlegibleFallaCerrado: a procfs leg that cannot enumerate must say so, including the
// case of a procfs that lists nothing at all (the positive control: the census must see itself).
func TestCensoProcfsIlegibleFallaCerrado(t *testing.T) {
	docker := servidorDockerFalso(t, http.StatusOK, `[]`)
	buena := procFalso(t, map[int]string{yo: "test"}, cabeceraTCP, cabeceraTCP)

	sinTCP := procFalso(t, map[int]string{yo: "test"}, cabeceraTCP, cabeceraTCP)
	if err := os.Remove(filepath.Join(sinTCP, "net", "tcp")); err != nil {
		t.Fatal(err)
	}
	filaRota := procFalso(t, map[int]string{yo: "test"}, cabeceraTCP+"basura\n", cabeceraTCP)
	sinCabecera := procFalso(t, map[int]string{yo: "test"}, "no header here\n", cabeceraTCP)

	casos := map[string]string{
		"procfs does not exist":          filepath.Join(t.TempDir(), "no-hay-proc"),
		"this process is not listed":     procFalso(t, map[int]string{1: "init"}, cabeceraTCP, cabeceraTCP),
		"net/tcp missing":                sinTCP,
		"malformed net/tcp row":          filaRota,
		"net/tcp without a table header": sinCabecera,
	}
	for nombre, proc := range casos {
		t.Run(nombre, func(t *testing.T) {
			rep := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo, RepoDir: t.TempDir()}, nuevoConjuntoMediado())
			if rep.Ajenos != 0 {
				t.Fatalf("the fixture is meant to have nothing foreign: %+v", rep)
			}
			if rep.CensoProcfsOK {
				t.Fatalf("censo_procfs_ok is true although the procfs leg did not run: %+v", rep)
			}
			if !rep.CensoDockerOK {
				t.Fatalf("the Docker leg was healthy but censo_docker_ok is false: %+v", rep)
			}
			if rep.Estado != CensoParcial || rep.RC() == 0 {
				t.Fatalf("estado=%q rc=%d, want %q and a non-zero rc", rep.Estado, rep.RC(), CensoParcial)
			}
		})
	}

	// And the healthy pair is complete, so the failures above are not a constant.
	rep := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: buena, SelfPID: yo, RepoDir: t.TempDir()}, nuevoConjuntoMediado())
	if !rep.CensoDockerOK || !rep.CensoProcfsOK || rep.Estado != CensoCompleta || rep.RC() != 0 {
		t.Fatalf("a healthy census is not complete: %+v", rep)
	}
}

// TestCensoObservaLoAjeno feeds both legs a fixture whose answer is known independently of the code
// and checks every count. Emptying either observation body turns this red.
func TestCensoObservaLoAjeno(t *testing.T) {
	docker := servidorDockerFalso(t, http.StatusOK, `[
	 {"Id":"aaa","Names":["/srf-ng-5"],"Image":"opensuperficies-browser-arm64:2.0.0"},
	 {"Id":"bbb","Names":["/newton-srf-perplexity"],"Image":"opensuperficies-browser-arm64:1.0.0"},
	 {"Id":"ccc","Names":["/srf-intruso"],"Image":"opensuperficies-browser-arm64:2.0.0"},
	 {"Id":"ddd","Names":["/postgres"],"Image":"postgres:16"}]`)

	tcp := cabeceraTCP +
		filaTCP(0, 10025, "0A") + // mediated
		filaTCP(1, 10088, "0A") + // foreign
		filaTCP(2, 10020, "0A") + // declared
		filaTCP(3, 9240, "0A") + // declared
		filaTCP(4, 22, "0A") + // outside the campaign windows
		filaTCP(5, 10077, "01") // established, not listening
	tcp6 := cabeceraTCP +
		filaTCP6(0, 9225, "0A") + // mediated
		filaTCP6(1, 10088, "0A") // the same foreign port on the IPv6 table: counted once
	proc := procFalso(t, map[int]string{yo: "test", 4243: "qemu-system-aar", 4244: "qemu-system-aar", 4245: "bash"}, tcp, tcp6)

	repo := t.TempDir()
	if err := os.MkdirAll(filepath.Join(repo, "_estado", "residuos"), 0o755); err != nil {
		t.Fatal(err)
	}
	escribir(t, filepath.Join(repo, "_estado", "residuos", "declarados.json"),
		`{"motivo":"fixture","contenedores":["newton-srf-perplexity"],"redes":["newton-srf-perplexity"],"puertos":[10020,9240]}`)

	med := nuevoConjuntoMediado()
	med.total = 1
	med.contenedores["srf-ng-5"] = true
	med.puertos[10025] = true
	med.puertos[9225] = true
	med.pids[4244] = true

	rep := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo, RepoDir: repo}, med)

	if !rep.CensoDockerOK || !rep.CensoProcfsOK {
		t.Fatalf("both legs must have run: %+v", rep)
	}
	if rep.ObservadasDocker != 3 || rep.ObservadasQEMU != 2 || rep.ObservadasPuertos != 5 || rep.ObservadasTotal != 10 {
		t.Fatalf("observations docker=%d qemu=%d puertos=%d total=%d, want 3/2/5/10", rep.ObservadasDocker, rep.ObservadasQEMU, rep.ObservadasPuertos, rep.ObservadasTotal)
	}
	if got := strings.Join(rep.ContenedoresAjenos, ","); got != "srf-intruso" {
		t.Fatalf("foreign containers = %q, want srf-intruso", got)
	}
	if len(rep.ProcesosAjenos) != 1 || rep.ProcesosAjenos[0] != 4243 {
		t.Fatalf("foreign processes = %v, want [4243]", rep.ProcesosAjenos)
	}
	if len(rep.PuertosAjenos) != 1 || rep.PuertosAjenos[0] != 10088 {
		t.Fatalf("foreign ports = %v, want [10088]", rep.PuertosAjenos)
	}
	if rep.Ajenos != 3 || rep.Mediadas != 1 || rep.Declaradas != 3 {
		t.Fatalf("ajenos=%d mediadas=%d declaradas=%d, want 3/1/3", rep.Ajenos, rep.Mediadas, rep.Declaradas)
	}
	if rep.Estado != CensoIncompleta || rep.RC() != 1 {
		t.Fatalf("estado=%q rc=%d, want %q and rc 1", rep.Estado, rep.RC(), CensoIncompleta)
	}
}

// TestCensoDeclaracionesIlegiblesNoOcultanNada: an unreadable declarations file is reported and can only
// make MORE surfaces foreign, never fewer; a missing file is legitimate and is not an alarm.
func TestCensoDeclaracionesIlegiblesNoOcultanNada(t *testing.T) {
	docker := servidorDockerFalso(t, http.StatusOK,
		`[{"Id":"bbb","Names":["/newton-srf-perplexity"],"Image":"opensuperficies-browser-arm64:1.0.0"}]`)
	proc := procFalso(t, map[int]string{yo: "test"}, cabeceraTCP, cabeceraTCP)

	rota := t.TempDir()
	if err := os.MkdirAll(filepath.Join(rota, "_estado", "residuos"), 0o755); err != nil {
		t.Fatal(err)
	}
	escribir(t, filepath.Join(rota, "_estado", "residuos", "declarados.json"), `{"contenedores":["newton-srf-perplexity"],"puertos": oops}`)
	rep := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo, RepoDir: rota}, nuevoConjuntoMediado())
	if len(rep.Errores) == 0 || !strings.HasPrefix(rep.Errores[0], "declarados:") {
		t.Fatalf("a malformed declarations file was not reported: %v", rep.Errores)
	}
	if rep.Ajenos != 1 || rep.Declaradas != 0 || rep.Estado != CensoIncompleta {
		t.Fatalf("with unreadable declarations the container must be foreign: %+v", rep)
	}

	ausente := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo, RepoDir: t.TempDir()}, nuevoConjuntoMediado())
	if len(ausente.Errores) != 0 || ausente.Ajenos != 1 {
		t.Fatalf("a missing declarations file must be silent and change nothing else: %+v", ausente)
	}
}

// TestCensoMediadoPorLaTabla ties the census to the daemon's own table: a capsule the manager started
// is mediated (ajenos 0, census complete); once the manager stops mediating it, the same observation
// makes it foreign.
func TestCensoMediadoPorLaTabla(t *testing.T) {
	docker := servidorDockerFalso(t, http.StatusOK,
		`[{"Id":"aaa","Names":["/srf-ng-5"],"Image":"opensuperficies-browser-arm64:2.0.0"}]`)
	proc := procFalso(t, map[int]string{yo: "test"},
		cabeceraTCP+filaTCP(0, 10025, "0A"), cabeceraTCP+filaTCP6(0, 9225, "0A"))
	e := nuevoEntorno(t, opcionesEntorno{censo: CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo}})
	ctx := context.Background()

	if _, err := e.mgr.IniciarCapsula(ctx, "browser", 5); err != nil {
		t.Fatalf("IniciarCapsula: %v", err)
	}
	rep := e.mgr.CensoMediacion(ctx)
	if rep.Ajenos != 0 || rep.Mediadas != 1 || rep.Estado != CensoCompleta || rep.RC() != 0 || rep.ObservadasTotal != 3 || rep.Fuente != "srfd" {
		t.Fatalf("a mediated capsule is not recognised: %+v", rep)
	}

	if err := e.mgr.DetenerCapsula(ctx, 5, ""); err != nil {
		t.Fatalf("DetenerCapsula: %v", err)
	}
	rep = e.mgr.CensoMediacion(ctx)
	if rep.Ajenos != 3 || rep.Mediadas != 0 || rep.Estado != CensoIncompleta {
		t.Fatalf("the same observation must be foreign once the table no longer holds it: %+v", rep)
	}
}

// ---------------------------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------------------------

const diarioDurable = "srfd.wal,srfd.wal.flock"

// diarioSigueAbrible checks that the journal a stopped daemon left is a valid, UNLOCKED journal: it can be
// opened again (its flock was released) and it still holds `registros` records.
func diarioSigueAbrible(t *testing.T, sockDir string, registros int) {
	t.Helper()
	ruta := filepath.Join(sockDir, "srfd.wal")
	regs, err := wal.LeerRegistros(ruta)
	if err != nil {
		t.Fatalf("the journal left by the daemon cannot be read: %v", err)
	}
	if len(regs) != registros {
		t.Fatalf("the journal holds %d records, want %d", len(regs), registros)
	}
	libro, err := wal.AbrirLibro(ruta)
	if err != nil {
		t.Fatalf("the journal is still locked or damaged after the daemon stopped: %v", err)
	}
	if err := libro.Cerrar(); err != nil {
		t.Fatalf("closing the reopened journal: %v", err)
	}
}

// TestServirConservaElDiarioTrasCancelar guards the teardown of Servir. While the daemon runs, the socket,
// the journal and its flock exist (so the comparison afterwards is not vacuous); once Servir has RETURNED
// the socket is gone and the journal and its flock file are KEPT (durable state, never unlinked), closed
// and unlocked, even though this daemon served without receiving a single request.
func TestServirConservaElDiarioTrasCancelar(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	sockDir := corto(t)
	parar, _ := arrancar(t, configDePrueba(t, sockDir, e))

	if got, want := strings.Join(nombres(t, sockDir), ","), "srfd.sock,"+diarioDurable; got != want {
		t.Fatalf("while running the directory holds %q, want %q", got, want)
	}
	if err := parar(); err != nil {
		t.Fatalf("Servir returned %v after a clean cancel", err)
	}
	if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
		t.Fatalf("after Servir returned the directory holds %q, want only %q (socket unlinked, journal kept)", got, diarioDurable)
	}
	diarioSigueAbrible(t, sockDir, 0)
}

// TestServirDirectorioPropio: a socket directory this run created (0700) is used and, after a clean stop,
// KEPT because it holds the durable journal; a directory this run created whose start failed before serving
// holds nothing durable and is removed with everything this run put in it.
func TestServirDirectorioPropio(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})

	t.Run("clean stop keeps the directory with the journal", func(t *testing.T) {
		sockDir := filepath.Join(corto(t), "nuevo")
		parar, _ := arrancar(t, configDePrueba(t, sockDir, e))
		fi, err := os.Stat(sockDir)
		if err != nil || !fi.IsDir() || fi.Mode().Perm() != 0o700 {
			t.Fatalf("the daemon did not create its directory as 0700: %v, %v", fi, err)
		}
		if err := parar(); err != nil {
			t.Fatalf("Servir returned %v", err)
		}
		if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
			t.Fatalf("directory after a clean stop = %q, want %q", got, diarioDurable)
		}
	})

	t.Run("failed start removes what this run created", func(t *testing.T) {
		sockDir := filepath.Join(corto(t), "nuevo")
		cfg := configDePrueba(t, sockDir, e)
		cfg.SocketPath = filepath.Join(sockDir, strings.Repeat("x", 200)+".sock") // longer than sun_path: bind fails
		if err := Servir(context.Background(), cfg); err == nil {
			t.Fatal("Servir started on an impossible socket path")
		}
		if _, err := os.Stat(sockDir); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("the directory this run created, holding nothing durable, is still there: %v", err)
		}
	})
}

// TestServirConSenalesReales starts the daemon as a child process running EjecutarDaemon, sends it a
// real signal and looks at the directory after the process is gone: the socket must be gone, and the
// journal and its flock must be there, closed and unlocked (the attempt-1 shutdown path, a goroutine that
// main outran, left the process holding them).
func TestServirConSenalesReales(t *testing.T) {
	for nombre, senal := range map[string]syscall.Signal{"SIGTERM": syscall.SIGTERM, "SIGINT": syscall.SIGINT, "SIGHUP": syscall.SIGHUP} {
		t.Run(nombre, func(t *testing.T) {
			aux := t.TempDir()
			parada := filepath.Join(aux, "PARADA.txt")
			plaza := filepath.Join(aux, "PARADA-PLAZA.txt")
			escribir(t, parada, paradaLibre)
			escribir(t, plaza, paradaLibre)
			sockDir := corto(t)

			cmd := exec.Command(os.Args[0], "--socket", filepath.Join(sockDir, "srfd.sock"))
			cmd.Env = append(os.Environ(), "SRF_TEST_HELPER_DAEMON=1", "SRF_PARADA_REPO="+parada, "SRF_PARADA_PLAZA="+plaza)
			stderr, err := cmd.StderrPipe()
			if err != nil {
				t.Fatal(err)
			}
			if err := cmd.Start(); err != nil {
				t.Fatal(err)
			}

			listo := make(chan struct{})
			fin := make(chan struct{})
			var salida bytes.Buffer
			var mu sync.Mutex
			go func() {
				defer close(fin)
				var avisado bool
				sc := bufio.NewScanner(stderr)
				for sc.Scan() {
					mu.Lock()
					salida.WriteString(sc.Text() + "\n")
					mu.Unlock()
					if !avisado && strings.Contains(sc.Text(), "srfd: ready") {
						avisado = true
						close(listo)
					}
				}
			}()

			select {
			case <-listo:
			case <-fin:
				_ = cmd.Wait()
				t.Fatalf("the daemon exited before it was ready:\n%s", salida.String())
			case <-time.After(15 * time.Second):
				_ = cmd.Process.Kill()
				<-fin
				_ = cmd.Wait()
				t.Fatalf("timeout waiting for the daemon:\n%s", salida.String())
			}
			if got, want := strings.Join(nombres(t, sockDir), ","), "srfd.sock,"+diarioDurable; got != want {
				_ = cmd.Process.Kill()
				<-fin
				_ = cmd.Wait()
				t.Fatalf("while running the directory holds %q, want %q", got, want)
			}

			if err := cmd.Process.Signal(senal); err != nil {
				t.Fatal(err)
			}
			<-fin
			werr := cmd.Wait()
			mu.Lock()
			texto := salida.String()
			mu.Unlock()
			if werr != nil {
				t.Fatalf("the daemon did not exit 0 after %s: %v\n%s", nombre, werr, texto)
			}
			if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
				t.Fatalf("after the process exited on %s the directory holds %q, want only %q\n%s", nombre, got, diarioDurable, texto)
			}
			diarioSigueAbrible(t, sockDir, 0)
		})
	}
}

// TestServirDrenaLasCapsulasVivas: capsules still mediated when shutdown starts are stopped, through
// the same guarded path, with their class named, before the journal that describes them is removed.
func TestServirDrenaLasCapsulasVivas(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	sockDir := corto(t)
	cfg := configDePrueba(t, sockDir, e)
	parar, _ := arrancar(t, cfg)

	resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "iniciar", Clase: "desktop", Slot: 5}, 10*time.Second)
	if err != nil || !resp.OK {
		t.Fatalf("iniciar: %v %+v", err, resp)
	}
	if err := parar(); err != nil {
		t.Fatalf("Servir returned %v", err)
	}
	esperado := "iniciar --clase desktop --slot 5 --json|detener --clase desktop --slot 5 --json"
	if got := strings.Join(lineas(t, e.log), "|"); got != esperado {
		t.Fatalf("dispatcher calls = %q, want %q", got, esperado)
	}
	if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
		t.Fatalf("after a drained shutdown the directory holds %q, want only %q", got, diarioDurable)
	}
	diarioSigueAbrible(t, sockDir, 2) // the lease grant of the start and the revocation of the drain's stop
}

// TestServirEsperaLasPeticionesEnCurso: a start that is running inside the dispatcher when shutdown begins
// is waited for (its client gets the real answer), and the capsule it created is then stopped by the drain.
func TestServirEsperaLasPeticionesEnCurso(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	arranque := filepath.Join(e.dir, "arranque")
	t.Setenv("SRF_TEST_ARRANQUE", arranque)
	t.Setenv("SRF_TEST_ESPERA", "1")
	sockDir := corto(t)
	cfg := configDePrueba(t, sockDir, e)
	parar, _ := arrancar(t, cfg)

	respuesta := make(chan *Response, 1)
	fallo := make(chan error, 1)
	go func() {
		resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "iniciar", Clase: "browser", Slot: 5}, 30*time.Second)
		if err != nil {
			fallo <- err
			return
		}
		respuesta <- resp
	}()
	esperarArchivo(t, arranque) // the start is now inside the dispatcher

	if err := parar(); err != nil {
		t.Fatalf("Servir returned %v", err)
	}
	select {
	case resp := <-respuesta:
		if !resp.OK {
			t.Fatalf("the in-flight start was cut by the shutdown: %+v", *resp)
		}
	case err := <-fallo:
		t.Fatalf("the in-flight client got no answer: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("the in-flight client got no answer")
	}
	esperado := "iniciar --clase browser --slot 5 --json|detener --clase browser --slot 5 --json"
	if got := strings.Join(lineas(t, e.log), "|"); got != esperado {
		t.Fatalf("dispatcher calls = %q, want %q", got, esperado)
	}
	if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
		t.Fatalf("after shutting down during a start the directory holds %q, want only %q", got, diarioDurable)
	}
	diarioSigueAbrible(t, sockDir, 2)
}

// TestServirConParadaConservaElDiarioYLoDice: under an effective PARADA the capsules cannot be stopped,
// so the journal that describes them is KEPT and the shutdown reports rc 3 instead of claiming a clean exit.
func TestServirConParadaConservaElDiarioYLoDice(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	sockDir := corto(t)
	cfg := configDePrueba(t, sockDir, e)
	parar, _ := arrancar(t, cfg)

	resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "iniciar", Clase: "browser", Slot: 5}, 10*time.Second)
	if err != nil || !resp.OK {
		t.Fatalf("iniciar: %v %+v", err, resp)
	}
	escribir(t, e.parada, paradaActiva)

	err = parar()
	if !errors.Is(err, ErrParadaActiva) || rcDeError(err) != RCParada {
		t.Fatalf("Servir returned %v, want an error carrying ErrParadaActiva (rc %d)", err, RCParada)
	}
	if !strings.Contains(err.Error(), "journal kept") {
		t.Fatalf("the kept journal is not named in the error: %v", err)
	}
	for _, l := range lineas(t, e.log) {
		if strings.HasPrefix(l, "detener") {
			t.Fatalf("a capsule was stopped under PARADA: %q", l)
		}
	}
	if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
		t.Fatalf("directory after a PARADA shutdown = %q, want %q (socket gone, journal kept)", got, diarioDurable)
	}
	diarioSigueAbrible(t, sockDir, 1) // the lease grant: the capsule it describes is still alive
}

// TestServirFalloDeArranqueNoDejaResiduos: a start that fails after the journal was opened releases
// and removes the empty journal THIS run created (it holds nothing), and leaves alone a journal that
// pre-existed, empty or not (a crashed predecessor's, which is recovery input).
func TestServirFalloDeArranqueNoDejaResiduos(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	larga := strings.Repeat("x", 200) + ".sock" // longer than the 108-byte sun_path limit: bind fails

	t.Run("fresh directory", func(t *testing.T) {
		sockDir := corto(t)
		cfg := configDePrueba(t, sockDir, e)
		cfg.SocketPath = filepath.Join(sockDir, larga)
		if err := Servir(context.Background(), cfg); err == nil {
			t.Fatal("Servir started on an impossible socket path")
		}
		if left := nombres(t, sockDir); len(left) != 0 {
			t.Fatalf("residues after a failed start: %v", left)
		}
	})

	t.Run("empty journal of a predecessor", func(t *testing.T) {
		sockDir := corto(t)
		libro, err := wal.AbrirLibro(filepath.Join(sockDir, "srfd.wal"))
		if err != nil {
			t.Fatal(err)
		}
		if err := libro.Cerrar(); err != nil { // an empty journal that this run did not create
			t.Fatal(err)
		}
		cfg := configDePrueba(t, sockDir, e)
		cfg.SocketPath = filepath.Join(sockDir, larga)
		if err := Servir(context.Background(), cfg); err == nil {
			t.Fatal("Servir started on an impossible socket path")
		}
		if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
			t.Fatalf("directory = %q, want %q: a journal this run did not create is never removed, empty or not", got, diarioDurable)
		}
	})

	t.Run("journal of a crashed predecessor", func(t *testing.T) {
		sockDir := corto(t)
		previo := filepath.Join(sockDir, "srfd.wal")
		libro, err := wal.AbrirLibro(previo)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := libro.EscribirBegin("tx-previa"); err != nil {
			t.Fatal(err)
		}
		if err := libro.Cerrar(); err != nil { // leaves srfd.wal and srfd.wal.flock, as a crash would
			t.Fatal(err)
		}

		cfg := configDePrueba(t, sockDir, e)
		cfg.SocketPath = filepath.Join(sockDir, larga)
		if err := Servir(context.Background(), cfg); err == nil {
			t.Fatal("Servir started on an impossible socket path")
		}
		if got, want := strings.Join(nombres(t, sockDir), ","), "srfd.wal,srfd.wal.flock"; got != want {
			t.Fatalf("directory = %q, want %q: a predecessor's journal is recovery input", got, want)
		}
		regs, err := wal.LeerRegistros(previo)
		if err != nil || len(regs) != 1 {
			t.Fatalf("the predecessor's journal was damaged: %d records, %v", len(regs), err)
		}
	})
}

// ---------------------------------------------------------------------------------------------
// Durable journal: the fencing epoch survives restarts
// ---------------------------------------------------------------------------------------------

// epocasConcedidas re-reads the journal FROM DISK and returns the epoch of every lease grant in order:
// a source that is independent of the lessor's memory and of what the daemon answered.
func epocasConcedidas(t *testing.T, ruta string) []uint64 {
	t.Helper()
	regs, err := wal.LeerRegistros(ruta)
	if err != nil {
		t.Fatalf("read journal %s: %v", ruta, err)
	}
	var out []uint64
	for _, r := range regs {
		var d struct {
			Epoca  uint64 `json:"epoca"`
			Accion string `json:"accion"`
		}
		if json.Unmarshal(r.Detalles, &d) == nil && d.Accion == "" && d.Epoca > 0 {
			out = append(out, d.Epoca)
		}
	}
	return out
}

// TestServirTresEncarnacionesEpocaCreciente guards the durable journal: three consecutive daemons on the
// same directory each start slot 5 and stop cleanly. The lessor rebuilds its counter from the journal, so
// the fencing epoch of the lease strictly increases across incarnations and the journal is still there
// after every clean stop. A daemon that unlinks its journal at shutdown restarts every epoch at 1.
func TestServirTresEncarnacionesEpocaCreciente(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	sockDir := corto(t)
	var vistas []uint64

	for i := 1; i <= 3; i++ {
		cfg := configDePrueba(t, sockDir, e)
		parar, _ := arrancar(t, cfg)

		resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "iniciar", Clase: "browser", Slot: 5}, 10*time.Second)
		if err != nil || !resp.OK {
			t.Fatalf("incarnation %d: iniciar: %v %+v", i, err, resp)
		}
		var info SurfaceInfo
		if err := json.Unmarshal(resp.Datos, &info); err != nil {
			t.Fatalf("incarnation %d: %v", i, err)
		}
		vistas = append(vistas, info.LeaseEpoca)
		if resp, err := EnviarComando(cfg.SocketPath, Request{Comando: "detener", Slot: 5}, 10*time.Second); err != nil || !resp.OK {
			t.Fatalf("incarnation %d: detener: %v %+v", i, err, resp)
		}
		if err := parar(); err != nil {
			t.Fatalf("incarnation %d: Servir returned %v", i, err)
		}
		if got := strings.Join(nombres(t, sockDir), ","); got != diarioDurable {
			t.Fatalf("incarnation %d: after the clean stop the directory holds %q, want %q", i, got, diarioDurable)
		}
	}

	if vistas[0] != 1 {
		t.Fatalf("the first incarnation of a fresh directory must grant epoch 1: %v", vistas)
	}
	for i := 1; i < len(vistas); i++ {
		if vistas[i] <= vistas[i-1] {
			t.Fatalf("the fencing epoch did not strictly increase across incarnations: %v", vistas)
		}
	}
	if enDisco := epocasConcedidas(t, filepath.Join(sockDir, "srfd.wal")); fmt.Sprint(enDisco) != fmt.Sprint(vistas) {
		t.Fatalf("the daemon answered epochs %v but the journal on disk holds %v", vistas, enDisco)
	}
}

// ---------------------------------------------------------------------------------------------
// Admission of the caller's kernel identity
// ---------------------------------------------------------------------------------------------

// TestPeerAdmitidoTabla guards PeerAdmitido as a pure predicate: only the permitted uid is admitted, and
// uid+1, root and nobody (65534) are refused unless one of them IS the permitted uid. A real peer with a
// different uid cannot be created in this environment (uid_map 1000 1000 1, no privilege), so this table is
// the test of the refusal branch; TestServidorAdmiteSoloAlUIDConfigurado tests that atender uses it.
func TestPeerAdmitidoTabla(t *testing.T) {
	propio := uint32(os.Getuid())
	casos := []struct {
		nombre    string
		peer      uint32
		permitido uint32
		admitido  bool
	}{
		{"the permitted uid", 1000, 1000, true},
		{"this process's uid", propio, propio, true},
		{"permitted uid + 1", 1001, 1000, false},
		{"this process's uid + 1", propio + 1, propio, false},
		{"root against a non-root daemon", 0, 1000, false},
		{"nobody against a non-root daemon", 65534, 1000, false},
		{"permitted uid - 1", 999, 1000, false},
		{"largest uid", ^uint32(0), 1000, false},
		{"root daemon admits root", 0, 0, true},
		{"root daemon refuses uid 1000", 1000, 0, false},
		{"root daemon refuses nobody", 65534, 0, false},
		{"nobody daemon admits nobody", 65534, 65534, true},
		{"nobody daemon refuses root", 0, 65534, false},
	}
	for _, c := range casos {
		t.Run(c.nombre, func(t *testing.T) {
			cred := PeerCredentials{PID: 4242, UID: c.peer, GID: c.peer}
			if got := PeerAdmitido(cred, c.permitido); got != c.admitido {
				t.Fatalf("PeerAdmitido(uid %d, permitted %d) = %v, want %v", c.peer, c.permitido, got, c.admitido)
			}
		})
	}
	// The group and the pid never matter: a peer with the permitted gid but another uid is refused.
	if PeerAdmitido(PeerCredentials{PID: 1, UID: 1001, GID: 1000}, 1000) {
		t.Fatal("a peer was admitted through its gid")
	}
}

// ---------------------------------------------------------------------------------------------
// Exit codes and the request reader
// ---------------------------------------------------------------------------------------------

// TestRCDeErrorTabla guards the mapping from a manager error to the exit code the client receives,
// including errors wrapped with %w.
func TestRCDeErrorTabla(t *testing.T) {
	if RCParada != 3 || RCSlotOcupado != 69 || RCUso != 2 || RCFallo != 1 || RCPeerRechazado != 9 {
		t.Fatalf("the exit codes changed: parada=%d slot=%d uso=%d fallo=%d peer=%d", RCParada, RCSlotOcupado, RCUso, RCFallo, RCPeerRechazado)
	}
	casos := []struct {
		err error
		rc  int
	}{
		{ErrParadaActiva, 3},
		{ErrSlotOcupado, 69},
		{ErrClaseInvalida, 2},
		{ErrClaseDistinta, 2},
		{ErrSlotAusente, 2},
		{errors.New("anything else"), 1},
		{fmt.Errorf("wrapped: %w", ErrSlotOcupado), 69},
		{fmt.Errorf("wrapped: %w", ErrParadaActiva), 3},
		{fmt.Errorf("wrapped: %w", ErrClaseInvalida), 2},
	}
	for _, c := range casos {
		if got := rcDeError(c.err); got != c.rc {
			t.Fatalf("rcDeError(%v) = %d, want %d", c.err, got, c.rc)
		}
	}
}

// TestRPCSlotOcupadoDevuelveRC69 guards the code of a busy slot on the wire: starting a slot that is
// already mediated answers rc 69, not the generic failure.
func TestRPCSlotOcupadoDevuelveRC69(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	cfg := configDePrueba(t, corto(t), e)
	arrancar(t, cfg)

	req := Request{Comando: "iniciar", Clase: "browser", Slot: 5}
	if resp, err := EnviarComando(cfg.SocketPath, req, 10*time.Second); err != nil || !resp.OK {
		t.Fatalf("first start: %v %+v", err, resp)
	}
	resp, err := EnviarComando(cfg.SocketPath, req, 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if resp.OK || resp.RC != 69 {
		t.Fatalf("second start of a mediated slot = %+v, want rc 69", *resp)
	}
	if got := len(lineas(t, e.log)); got != 1 {
		t.Fatalf("the dispatcher ran %d times for one slot", got)
	}
}

// intercambioCrudo writes raw bytes to the daemon and reads its one-line answer.
func intercambioCrudo(t *testing.T, socket string, datos []byte, plazo time.Duration) (*Response, error) {
	t.Helper()
	conn, err := net.Dial("unix", socket)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(plazo))
	if _, err := conn.Write(datos); err != nil {
		return nil, err
	}
	linea, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	var resp Response
	if err := json.Unmarshal(linea, &resp); err != nil {
		return nil, fmt.Errorf("unmarshal %q: %w", linea, err)
	}
	return &resp, nil
}

// peticionDeBytes builds a valid `estado` request line of exactly n bytes, newline included.
func peticionDeBytes(t *testing.T, n int) []byte {
	t.Helper()
	const prefijo, sufijo = `{"comando":"estado","clase":"`, "\"}\n"
	relleno := n - len(prefijo) - len(sufijo)
	if relleno < 0 {
		t.Fatalf("cannot build a request of %d bytes", n)
	}
	linea := []byte(prefijo + strings.Repeat("a", relleno) + sufijo)
	var req Request
	if len(linea) != n || json.Unmarshal(linea, &req) != nil {
		t.Fatalf("built %d bytes, valid json=%v", len(linea), json.Unmarshal(linea, &req) == nil)
	}
	return linea
}

// TestPeticionDemasiadoLargaSeRechaza guards the request-size cap of atender (maxPeticion, 4096 bytes):
// a VALID json line one byte over the cap is refused with rc 2 and not parsed, the same line at exactly
// the cap is served, so neither a higher nor a lower cap goes unnoticed.
func TestPeticionDemasiadoLargaSeRechaza(t *testing.T) {
	if maxPeticion != 4096 {
		t.Fatalf("the request cap is %d, the contract of this daemon is 4096", maxPeticion)
	}
	e := nuevoEntorno(t, opcionesEntorno{})
	cfg := configDePrueba(t, corto(t), e)
	arrancar(t, cfg)

	resp, err := intercambioCrudo(t, cfg.SocketPath, peticionDeBytes(t, maxPeticion), 5*time.Second)
	if err != nil || !resp.OK {
		t.Fatalf("a request of exactly %d bytes was not served: %v %+v", maxPeticion, err, resp)
	}
	for _, n := range []int{maxPeticion + 1, 5000, 70000} {
		resp, err := intercambioCrudo(t, cfg.SocketPath, peticionDeBytes(t, n), 5*time.Second)
		if err != nil {
			t.Fatalf("a request of %d bytes: %v", n, err)
		}
		if resp.OK || resp.RC != RCUso {
			t.Fatalf("a request of %d bytes (over the cap) = %+v, want rc %d", n, *resp, RCUso)
		}
	}
}

// TestPeticionQueNoLlegaSeCorta guards the read deadline of atender: a peer that connects and sends an
// unfinished line is answered rc 2 after the deadline instead of holding the handler (and the shutdown)
// forever. The deadline is shortened through ConfigDaemon.PlazoLectura; the client waits far longer.
func TestPeticionQueNoLlegaSeCorta(t *testing.T) {
	e := nuevoEntorno(t, opcionesEntorno{})
	cfg := configDePrueba(t, corto(t), e)
	cfg.PlazoLectura = 300 * time.Millisecond
	arrancar(t, cfg)

	inicio := time.Now()
	resp, err := intercambioCrudo(t, cfg.SocketPath, []byte(`{"comando":"est`), 4*time.Second) // no newline, then silence
	if err != nil {
		t.Fatalf("the daemon did not cut a stalled peer within %v: %v", time.Since(inicio), err)
	}
	if resp.OK || resp.RC != RCUso || !strings.Contains(resp.Error, "no complete request line") {
		t.Fatalf("stalled peer = %+v, want rc %d and the reason", *resp, RCUso)
	}
	if d := time.Since(inicio); d > 3*time.Second {
		t.Fatalf("the stalled peer was answered after %v", d)
	}
}

// ---------------------------------------------------------------------------------------------
// Census guards: port windows and container detection
// ---------------------------------------------------------------------------------------------

// TestVentanasDePuertosDeLaCampana guards the campaign port windows (Xpra 10020-10099, CDP 9220-9299) at
// both edges of both windows and at the census level: a listening port just outside a window is neither
// observed nor foreign.
func TestVentanasDePuertosDeLaCampana(t *testing.T) {
	casos := map[int]bool{
		10019: false, 10020: true, 10025: true, 10099: true, 10100: false,
		9219: false, 9220: true, 9225: true, 9299: true, 9300: false,
		22: false, 20000: false, 40000: false, 65000: false, 65535: false,
	}
	for puerto, dentro := range casos {
		if got := puertoDeCampana(puerto); got != dentro {
			t.Errorf("puertoDeCampana(%d) = %v, want %v", puerto, got, dentro)
		}
	}

	tcp := cabeceraTCP
	for i, p := range []int{10099, 10100, 40000, 65000, 9299, 9300, 20000, 10019} {
		tcp += filaTCP(i, p, "0A")
	}
	proc := procFalso(t, map[int]string{yo: "test"}, tcp, cabeceraTCP)
	docker := servidorDockerFalso(t, http.StatusOK, `[]`)
	rep := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo, RepoDir: t.TempDir()}, nuevoConjuntoMediado())
	if rep.ObservadasPuertos != 2 || fmt.Sprint(rep.PuertosAjenos) != "[9299 10099]" {
		t.Fatalf("observed ports = %d, foreign = %v; want exactly the two inside the windows [9299 10099]", rep.ObservadasPuertos, rep.PuertosAjenos)
	}
}

// TestEsSuperficieDetectaPorImagenYPorNombre guards the detection of a campaign container: by image
// prefix alone, by name prefix alone, and never for an unrelated container.
func TestEsSuperficieDetectaPorImagenYPorNombre(t *testing.T) {
	casos := []struct {
		nombre  string
		imagen  string
		nombres []string
		es      bool
	}{
		{"campaign image, unrelated name", "opensuperficies-browser-arm64:1.0.0", []string{"/otro"}, true},
		{"srf- image, unrelated name", "srf-desktop:2", []string{"/otro"}, true},
		{"unrelated image, srf- name", "alpine:3", []string{"/srf-solo-nombre"}, true},
		{"unrelated image, newton-srf- name", "alpine:3", []string{"/newton-srf-perplexity"}, true},
		{"campaign image and name", "opensuperficies-browser-arm64:2.0.0", []string{"/srf-ng-5"}, true},
		{"unrelated container", "postgres:16", []string{"/postgres"}, false},
		{"unrelated image, no name", "postgres:16", nil, false},
		{"name that merely contains srf-", "postgres:16", []string{"/mi-srf-db"}, false},
	}
	for _, c := range casos {
		if got := esSuperficie(c.imagen, c.nombres); got != c.es {
			t.Errorf("%s: esSuperficie(%q, %v) = %v, want %v", c.nombre, c.imagen, c.nombres, got, c.es)
		}
	}

	docker := servidorDockerFalso(t, http.StatusOK, `[
	 {"Id":"eee","Names":["/otro-nombre"],"Image":"opensuperficies-browser-arm64:1.0.0"},
	 {"Id":"fff","Names":["/otro"],"Image":"srf-desktop:2"},
	 {"Id":"ggg","Names":["/srf-solo-nombre"],"Image":"alpine:3"},
	 {"Id":"hhh","Names":["/db"],"Image":"postgres:16"}]`)
	proc := procFalso(t, map[int]string{yo: "test"}, cabeceraTCP, cabeceraTCP)
	rep := censar(context.Background(), CensoOpciones{DockerSocket: docker, ProcDir: proc, SelfPID: yo, RepoDir: t.TempDir()}, nuevoConjuntoMediado())
	if rep.ObservadasDocker != 3 || strings.Join(rep.ContenedoresAjenos, ",") != "otro,otro-nombre,srf-solo-nombre" {
		t.Fatalf("observed %d containers, foreign %v; want the three campaign containers by image or by name", rep.ObservadasDocker, rep.ContenedoresAjenos)
	}
}

// ---------------------------------------------------------------------------------------------
// The command lines: exit codes are the evidence of the fail-closed census and of the stop signal
// ---------------------------------------------------------------------------------------------

var (
	dirBinarios string // where the command-line tests built cmd/srf and cmd/srf-mediacion; removed by TestMain
	binOnce     sync.Once
	binErr      error
)

// binario builds cmd/srf and cmd/srf-mediacion once and returns the path of one of them: the tests that use
// it run the very main package a user runs, so an exit code forced inside main.go is seen.
func binario(t *testing.T, nombre string) string {
	t.Helper()
	binOnce.Do(func() {
		dir, err := os.MkdirTemp("", "srfbin")
		if err != nil {
			binErr = err
			return
		}
		dirBinarios = dir
		gobin, err := exec.LookPath("go")
		if err != nil {
			gobin = filepath.Join(runtime.GOROOT(), "bin", "go")
		}
		cmd := exec.Command(gobin, "build", "-trimpath", "-o", dir+string(os.PathSeparator), "./cmd/srf", "./cmd/srf-mediacion")
		cmd.Dir = findRepoRoot()
		cmd.Env = append(os.Environ(), "CGO_ENABLED=0")
		if out, err := cmd.CombinedOutput(); err != nil {
			binErr = fmt.Errorf("go build (%s): %v\n%s", gobin, err, out)
		}
	})
	if binErr != nil {
		t.Fatalf("cannot build the commands under test: %v", binErr)
	}
	return filepath.Join(dirBinarios, nombre)
}

// correr runs a binary and returns its exit code with stdout and stderr.
func correr(t *testing.T, env []string, bin string, args ...string) (int, string, string) {
	t.Helper()
	cmd := exec.Command(bin, args...)
	cmd.Env = append(os.Environ(), env...)
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	err := cmd.Run()
	var ee *exec.ExitError
	switch {
	case err == nil:
		return 0, out.String(), errb.String()
	case errors.As(err, &ee):
		return ee.ExitCode(), out.String(), errb.String()
	default:
		t.Fatalf("running %s: %v", bin, err)
		return -1, "", ""
	}
}

// censoSano returns the options of a census whose two legs are healthy and see nothing foreign.
func censoSano(t *testing.T) CensoOpciones {
	t.Helper()
	return CensoOpciones{
		DockerSocket: servidorDockerFalso(t, http.StatusOK, `[]`),
		ProcDir:      procFalso(t, map[int]string{yo: "test"}, cabeceraTCP, cabeceraTCP),
		SelfPID:      yo,
		RepoDir:      t.TempDir(),
	}
}

func ejecutarMediacion(t *testing.T, censo CensoOpciones, args ...string) (int, SrfMediacionReport, string) {
	t.Helper()
	var out, errb bytes.Buffer
	rc := EjecutarMediacion(args, &out, &errb, censo)
	var rep SrfMediacionReport
	if strings.Contains(strings.Join(args, " "), "--json") {
		if err := json.Unmarshal(out.Bytes(), &rep); err != nil {
			t.Fatalf("srf-mediacion --json printed %q: %v", out.String(), err)
		}
	}
	return rc, rep, errb.String()
}

// TestSrfMediacionCodigoDeSalida guards the exit code of the census command, which is the fail-closed
// evidence of the census: 0 only for a complete census with nothing foreign, 1 for a foreign surface AND
// for a Docker leg that did not run (a dead DOCKER_HOST), where the number of foreign surfaces is 0.
func TestSrfMediacionCodigoDeSalida(t *testing.T) {
	sinDemonio := filepath.Join(corto(t), "no-hay-srfd.sock")

	t.Run("healthy census is rc 0", func(t *testing.T) {
		rc, rep, _ := ejecutarMediacion(t, censoSano(t), "--json", "--socket", sinDemonio)
		if rc != 0 || rep.Estado != CensoCompleta || rep.Fuente != "local" || !rep.CensoDockerOK || !rep.CensoProcfsOK {
			t.Fatalf("rc=%d %+v, want rc 0 and a complete local census", rc, rep)
		}
	})

	t.Run("a foreign container is rc 1", func(t *testing.T) {
		censo := censoSano(t)
		censo.DockerSocket = servidorDockerFalso(t, http.StatusOK, `[{"Id":"a","Names":["/srf-intruso"],"Image":"alpine:3"}]`)
		rc, rep, _ := ejecutarMediacion(t, censo, "--json", "--socket", sinDemonio)
		if rc != 1 || rep.Ajenos != 1 || rep.Estado != CensoIncompleta {
			t.Fatalf("rc=%d %+v, want rc 1 and one foreign surface", rc, rep)
		}
	})

	t.Run("a dead DOCKER_HOST is rc 1", func(t *testing.T) {
		t.Setenv("DOCKER_HOST", "unix://"+filepath.Join(corto(t), "no-hay-docker.sock"))
		censo := censoSano(t)
		censo.DockerSocket = "" // the production path: the Docker socket comes from $DOCKER_HOST
		rc, rep, stderr := ejecutarMediacion(t, censo, "--json", "--socket", sinDemonio)
		if rep.Ajenos != 0 || rep.CensoDockerOK || !rep.CensoProcfsOK || rep.Estado != CensoParcial {
			t.Fatalf("the fixture must be: nothing foreign, docker leg dead, procfs leg healthy: %+v", rep)
		}
		if rc != 1 {
			t.Fatalf("a census whose Docker leg did not run exited %d, want 1 (stderr: %s)", rc, stderr)
		}
	})

	t.Run("a dead procfs is rc 1", func(t *testing.T) {
		censo := censoSano(t)
		censo.ProcDir = filepath.Join(t.TempDir(), "no-hay-proc")
		rc, rep, _ := ejecutarMediacion(t, censo, "--json", "--socket", sinDemonio)
		if rc != 1 || rep.CensoProcfsOK || rep.Estado != CensoParcial {
			t.Fatalf("rc=%d %+v, want rc 1 and censo_parcial", rc, rep)
		}
	})

	t.Run("the daemon's census with a dead Docker is rc 1", func(t *testing.T) {
		censo := censoSano(t)
		censo.DockerSocket = filepath.Join(corto(t), "no-hay-docker.sock")
		e := nuevoEntorno(t, opcionesEntorno{censo: censo})
		cfg := configDePrueba(t, corto(t), e)
		cfg.Manager.Censo = censo
		arrancar(t, cfg)
		rc, rep, _ := ejecutarMediacion(t, CensoOpciones{}, "--json", "--socket", cfg.SocketPath)
		if rc != 1 || rep.Fuente != "srfd" || rep.CensoDockerOK || rep.Estado != CensoParcial {
			t.Fatalf("rc=%d %+v, want rc 1 and a partial census produced by srfd", rc, rep)
		}
	})

	t.Run("a daemon that refuses is its own rc, never a local census", func(t *testing.T) {
		e := nuevoEntorno(t, opcionesEntorno{})
		cfg := configDePrueba(t, corto(t), e)
		otro := uint32(os.Getuid()) + 1
		cfg.UIDPermitido = &otro
		arrancar(t, cfg)
		rc, _, stderr := ejecutarMediacion(t, censoSano(t), "--socket", cfg.SocketPath)
		if rc != RCPeerRechazado || !strings.Contains(stderr, "refused") {
			t.Fatalf("rc=%d stderr=%q, want rc %d and the refusal", rc, stderr, RCPeerRechazado)
		}
	})
}

// TestSrfMediacionBinarioConDockerMuertoSaleConUno runs the built cmd/srf-mediacion with a dead
// DOCKER_HOST and no daemon: the process must exit 1 and say why. Forcing the exit code to 0 in
// cmd/srf-mediacion/main.go turns this red.
func TestSrfMediacionBinarioConDockerMuertoSaleConUno(t *testing.T) {
	bin := binario(t, "srf-mediacion")
	dir := corto(t)
	rc, stdout, stderr := correr(t, []string{"DOCKER_HOST=unix://" + filepath.Join(dir, "no-hay-docker.sock")},
		bin, "--json", "--socket", filepath.Join(dir, "no-hay-srfd.sock"))
	var rep SrfMediacionReport
	if err := json.Unmarshal([]byte(stdout), &rep); err != nil {
		t.Fatalf("stdout %q: %v (stderr %s)", stdout, err, stderr)
	}
	if rep.CensoDockerOK || rep.Estado != CensoParcial || rep.Fuente != "local" {
		t.Fatalf("the census did not report the dead Docker leg: %+v", rep)
	}
	if rc != 1 {
		t.Fatalf("srf-mediacion with a dead DOCKER_HOST exited %d, want 1", rc)
	}
}

// clienteSrf helpers ---------------------------------------------------------------------------

func ejecutarSrf(args ...string) (int, string, string) {
	var out, errb bytes.Buffer
	rc := EjecutarSrf(args, &out, &errb)
	return rc, out.String(), errb.String()
}

// TestSrfCodigosDeSalida guards the exit codes of the client against a real daemon: PARADA is rc 3 for both
// mutating verbs BEFORE any argument is validated, the same arguments are rc 2 under LIBRE, a busy slot is
// rc 69, an unreachable daemon is rc 1 and read-only verbs answer rc 0 under PARADA.
func TestSrfCodigosDeSalida(t *testing.T) {
	t.Run("PARADA in either file answers rc 3 whatever the arguments", func(t *testing.T) {
		for _, par := range pares {
			e := nuevoEntorno(t, opcionesEntorno{parada: par.repo, plaza: par.plaza})
			cfg := configDePrueba(t, corto(t), e)
			arrancar(t, cfg)
			for _, args := range [][]string{
				{"capsula", "iniciar", "--clase", "browser", "--slot", "5", "--json", "--socket", cfg.SocketPath},
				{"capsula", "iniciar", "--clase", "clase-que-no-existe", "--slot", "5", "--json", "--socket", cfg.SocketPath},
				{"capsula", "iniciar", "--clase", "$(touch x)", "--slot", "99999", "--socket", cfg.SocketPath},
				{"capsula", "detener", "--clase", "../../x", "--slot", "5", "--json", "--socket", cfg.SocketPath},
				{"capsula", "detener", "--slot", "7", "--socket", cfg.SocketPath},
			} {
				if rc, _, _ := ejecutarSrf(args...); rc != RCParada {
					t.Fatalf("%s: srf %v exited %d under PARADA, want %d", par.nombre, args, rc, RCParada)
				}
			}
			if rc, out, _ := ejecutarSrf("estado", "--json", "--socket", cfg.SocketPath); rc != 0 || !strings.Contains(out, `"parada":"PARADA"`) {
				t.Fatalf("%s: estado under PARADA: rc %d, %q", par.nombre, rc, out)
			}
			if rc, _, _ := ejecutarSrf("listar", "--socket", cfg.SocketPath); rc != 0 {
				t.Fatalf("%s: listar under PARADA exited %d", par.nombre, rc)
			}
			if l := lineas(t, e.log); len(l) != 0 {
				t.Fatalf("%s: the dispatcher ran under PARADA: %q", par.nombre, l)
			}
		}
	})

	t.Run("under LIBRE the same arguments are refused as usage and a busy slot is rc 69", func(t *testing.T) {
		e := nuevoEntorno(t, opcionesEntorno{})
		cfg := configDePrueba(t, corto(t), e)
		arrancar(t, cfg)
		if rc, _, _ := ejecutarSrf("capsula", "iniciar", "--clase", "clase-que-no-existe", "--slot", "5", "--socket", cfg.SocketPath); rc != RCUso {
			t.Fatalf("an invalid class under LIBRE exited %d, want %d", rc, RCUso)
		}
		if rc, _, stderr := ejecutarSrf("capsula", "iniciar", "--clase", "browser", "--slot", "5", "--socket", cfg.SocketPath); rc != 0 {
			t.Fatalf("start under LIBRE exited %d: %s", rc, stderr)
		}
		if rc, _, _ := ejecutarSrf("capsula", "iniciar", "--clase", "browser", "--slot", "5", "--socket", cfg.SocketPath); rc != RCSlotOcupado {
			t.Fatalf("a busy slot exited %d, want %d", rc, RCSlotOcupado)
		}
		if rc, _, stderr := ejecutarSrf("capsula", "detener", "--slot", "5", "--socket", cfg.SocketPath); rc != 0 {
			t.Fatalf("stop exited %d: %s", rc, stderr)
		}
	})

	t.Run("no daemon is rc 1 and bad usage is rc 2", func(t *testing.T) {
		sin := filepath.Join(corto(t), "no-hay-srfd.sock")
		if rc, _, stderr := ejecutarSrf("estado", "--socket", sin); rc != RCFallo || !strings.Contains(stderr, sin) {
			t.Fatalf("estado without a daemon: rc %d stderr %q", rc, stderr)
		}
		for _, args := range [][]string{nil, {"nada"}, {"capsula"}, {"capsula", "nada"}, {"estado", "--no-such-flag"}} {
			if rc, _, _ := ejecutarSrf(args...); rc != RCUso {
				t.Fatalf("srf %v exited %d, want %d", args, rc, RCUso)
			}
		}
	})
}

// TestSrfBinarioCodigosDeSalida runs the built cmd/srf against a daemon under PARADA: the process must
// exit with the daemon's rc 3 (and with 1 when there is no daemon), so nothing in main.go can rewrite it.
func TestSrfBinarioCodigosDeSalida(t *testing.T) {
	bin := binario(t, "srf")
	e := nuevoEntorno(t, opcionesEntorno{plaza: paradaActiva})
	cfg := configDePrueba(t, corto(t), e)
	arrancar(t, cfg)

	if rc, _, stderr := correr(t, nil, bin, "capsula", "iniciar", "--clase", "clase-que-no-existe", "--slot", "5", "--json", "--socket", cfg.SocketPath); rc != RCParada {
		t.Fatalf("srf capsula iniciar under PARADA exited %d, want %d (%s)", rc, RCParada, stderr)
	}
	if rc, out, _ := correr(t, nil, bin, "estado", "--json", "--socket", cfg.SocketPath); rc != 0 || !strings.Contains(out, `"parada":"PARADA"`) {
		t.Fatalf("srf estado under PARADA: rc %d, %q", rc, out)
	}
	if rc, _, _ := correr(t, nil, bin, "estado", "--socket", filepath.Join(corto(t), "no-hay.sock")); rc != RCFallo {
		t.Fatalf("srf estado without a daemon exited %d, want %d", rc, RCFallo)
	}
}
