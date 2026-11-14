package grabador

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"
)

// ---------------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------------

// TestMain doubles as the helper process of the observer tests: when SRF_TEST_AYUDANTE names a
// socket, the binary connects to it and holds the connection until its stdin closes (after saying
// "listo"), or for SRF_TEST_AYUDANTE_SEG seconds (saying "listo <pid>" first).
func TestMain(m *testing.M) {
	if sock := os.Getenv("SRF_TEST_AYUDANTE"); sock != "" {
		c, err := net.Dial("unix", sock)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if seg, _ := strconv.Atoi(os.Getenv("SRF_TEST_AYUDANTE_SEG")); seg > 0 {
			fmt.Println("listo", os.Getpid())
			time.Sleep(time.Duration(seg) * time.Second) // hold the connection for a fixed time
		} else {
			fmt.Println("listo")
			_, _ = io.Copy(io.Discard, os.Stdin)
		}
		c.Close()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// enRaizRepo makes the repository root (the directory holding go.mod) the working directory,
// which is where the campaign runs every literal from.
func enRaizRepo(t *testing.T) string {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			break
		}
		padre := filepath.Dir(dir)
		if padre == dir {
			t.Fatal("cannot find go.mod")
		}
		dir = padre
	}
	t.Chdir(dir)
	return dir
}

// xvfbDePrueba starts the nested Xvfb of the unit. It never skips: a missing Xvfb is a red test,
// because the oracle needs it as well.
func xvfbDePrueba(t *testing.T) *XvfbServer {
	t.Helper()
	srv, err := ArrancarXvfb(context.Background(), ":150")
	if err != nil {
		t.Fatalf("could not start Xvfb on :150: %v", err)
	}
	t.Cleanup(func() {
		if err := srv.Detener(); err != nil {
			t.Errorf("Xvfb teardown: %v", err)
		}
	})
	return srv
}

func procesosPorComm(comm, argumento string) []int {
	var out []int
	ents, _ := os.ReadDir("/proc")
	for _, e := range ents {
		pid, err := strconv.Atoi(e.Name())
		if err != nil {
			continue
		}
		b, err := os.ReadFile("/proc/" + e.Name() + "/comm")
		if err != nil || strings.TrimSpace(string(b)) != comm {
			continue
		}
		cl, _ := os.ReadFile("/proc/" + e.Name() + "/cmdline")
		if argumento == "" || bytes.Contains(cl, []byte(argumento)) {
			out = append(out, pid)
		}
	}
	return out
}

// sinResiduosX fails when a lock, a socket or a process of the nested display survives.
func sinResiduosX(t *testing.T, num int) {
	t.Helper()
	for _, p := range []string{fmt.Sprintf("/tmp/.X%d-lock", num), fmt.Sprintf("/tmp/.X11-unix/X%d", num)} {
		if _, err := os.Lstat(p); err == nil {
			t.Errorf("residue: %s exists", p)
		}
	}
	if pids := procesosPorComm("Xvfb", fmt.Sprintf(":%d", num)); len(pids) > 0 {
		t.Errorf("residue: live Xvfb for :%d: %v", num, pids)
	}
}

// sinHijos fails when the test process still has children (ffmpeg, the witness, an Xvfb the test
// did not declare as permitted).
func sinHijos(t *testing.T, permitidos ...int) {
	t.Helper()
	pids, err := arbolDeProcesos(os.Getpid(), nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, pid := range pids {
		if pid == os.Getpid() {
			continue
		}
		permitido := false
		for _, p := range permitidos {
			permitido = permitido || p == pid
		}
		if !permitido {
			t.Errorf("residue: the test still has child %d (tree %v)", pid, pids)
		}
	}
}

type espia struct {
	conexiones []string
	comandos   []string
}

// instalarEspias replaces the dial and spawn seams by recorders that fail: with them a test can
// call any entry point with a host spelling and prove that nothing was dialled or spawned, and if
// a guard is removed the call reaches the spy, not the operator's display.
func instalarEspias(t *testing.T) *espia {
	t.Helper()
	e := &espia{}
	antesC, antesK := conectarX, crearComando
	conectarX = func(d string) (*xgb.Conn, error) {
		e.conexiones = append(e.conexiones, d)
		return nil, errors.New("espia: no se conecta")
	}
	crearComando = func(ctx context.Context, name string, args ...string) (*exec.Cmd, error) {
		e.comandos = append(e.comandos, name+" "+strings.Join(args, " "))
		return nil, errors.New("espia: no se lanza")
	}
	t.Cleanup(func() { conectarX, crearComando = antesC, antesK })
	return e
}

func (e *espia) nada(t *testing.T) {
	t.Helper()
	if len(e.conexiones) != 0 || len(e.comandos) != 0 {
		t.Errorf("the guard let a call through: dials=%v spawns=%v", e.conexiones, e.comandos)
	}
}

func escribirScript(t *testing.T, dir, nombre, cuerpo string) string {
	t.Helper()
	p := filepath.Join(dir, nombre)
	if err := os.WriteFile(p, []byte("#!/bin/bash\n"+cuerpo), 0755); err != nil {
		t.Fatal(err)
	}
	return p
}

// ffmpegFalso puts a stand-in named ffmpeg first in PATH. It writes a few bytes to the output file
// (the last argument) like the real one creates it, and then runs cuerpo. A call that decodes to
// the null muxer (the session's video verification) is handed to the real ffmpeg, so the stand-in
// records nothing for it and creates no file named "-".
func ffmpegFalso(t *testing.T, cuerpo string) {
	t.Helper()
	real, err := exec.LookPath("ffmpeg")
	if err != nil {
		t.Fatalf("ffmpeg is needed by the tests: %v", err)
	}
	dir := t.TempDir()
	escribirScript(t, dir, "ffmpeg", fmt.Sprintf("case \" $* \" in *\" -f null \"*) exec %q \"$@\";; esac\n", real)+
		"for ultimo; do :; done\necho fake-mp4 > \"$ultimo\"\n"+cuerpo+"\n")
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

// testigoFalso is a witness stand-in that prints n lines.
func testigoFalso(t *testing.T, n int) string {
	t.Helper()
	return escribirScript(t, t.TempDir(), "testigo.sh", fmt.Sprintf("for i in $(seq 1 %d); do echo linea-$i; done\n", n))
}

// servidorX listens on <tmp>/.X11-unix/X<n>, a name the observer recognises as an X display, and
// accepts connections in the background.
func servidorX(t *testing.T, n int) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), ".X11-unix")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatal(err)
	}
	ruta := filepath.Join(dir, "X"+strconv.Itoa(n))
	ln, err := net.Listen("unix", ruta)
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var conns []net.Conn
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			mu.Lock()
			conns = append(conns, c)
			mu.Unlock()
		}
	}()
	t.Cleanup(func() {
		ln.Close()
		mu.Lock()
		for _, c := range conns {
			c.Close()
		}
		mu.Unlock()
	})
	return ruta
}

// esperarHasta polls cond until it reports true, or fails the test at the deadline with what cond
// said was missing. It waits for the condition the caller asserts, never for a proxy of it.
func esperarHasta(t *testing.T, plazo time.Duration, cond func() (bool, string)) {
	t.Helper()
	fin := time.Now().Add(plazo)
	for {
		ok, falta := cond()
		if ok {
			return
		}
		if time.Now().After(fin) {
			t.Fatalf("condition not met within %v: %s", plazo, falta)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// sondaFalsa replaces the witness canary by one that answers ok, for tests whose witness is a
// stand-in that never reads a display. Tests of the canary itself never use it.
func sondaFalsa(t *testing.T) {
	t.Helper()
	antes := sondaTestigo
	sondaTestigo = func(context.Context, string, []string) error { return nil }
	t.Cleanup(func() { sondaTestigo = antes })
}

// esperarQueSeVaya waits until pid is gone from the process table, reaping adopted orphans on the way.
func esperarQueSeVaya(t *testing.T, pid int, plazo time.Duration) {
	t.Helper()
	esperarHasta(t, plazo, func() (bool, string) {
		recogerAdoptados()
		if _, err := os.Stat("/proc/" + strconv.Itoa(pid)); err == nil {
			return false, fmt.Sprintf("pid %d is still in /proc", pid)
		}
		return true, ""
	})
}

// anuncioDelAyudante reads the pid the helper announced ("listo <pid>") into path.
func anuncioDelAyudante(t *testing.T, path string) int {
	t.Helper()
	var pid int
	esperarHasta(t, 5*time.Second, func() (bool, string) {
		b, err := os.ReadFile(path)
		if err != nil {
			return false, err.Error()
		}
		if _, err := fmt.Sscanf(string(b), "listo %d", &pid); err != nil || pid <= 0 {
			return false, fmt.Sprintf("the helper has not announced its pid yet: %q", b)
		}
		return true, ""
	})
	return pid
}

func conexionesA(obs ObservacionX, display int) []ConexionX {
	var out []ConexionX
	for _, c := range obs.Conexiones {
		if c.Display == display {
			out = append(out, c)
		}
	}
	return out
}

func claves(t *testing.T, res *SesionResultado) map[string]any {
	t.Helper()
	b, err := json.Marshal(res)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// ventanaCruda creates a window with the raw protocol, optionally unmapped or override-redirect.
func ventanaCruda(t *testing.T, c *xgb.Conn, w, h int, mapear, overrideRedirect bool) xproto.Window {
	t.Helper()
	screen := xproto.Setup(c).DefaultScreen(c)
	id, err := xproto.NewWindowId(c)
	if err != nil {
		t.Fatal(err)
	}
	mask := uint32(xproto.CwBackPixel)
	vals := []uint32{screen.WhitePixel}
	if overrideRedirect {
		mask |= xproto.CwOverrideRedirect
		vals = append(vals, 1)
	}
	if err := xproto.CreateWindowChecked(c, screen.RootDepth, id, screen.Root, 0, 0, uint16(w), uint16(h), 0,
		xproto.WindowClassInputOutput, screen.RootVisual, mask, vals).Check(); err != nil {
		t.Fatal(err)
	}
	atomPID, err := internarAtomo(c, "_NET_WM_PID")
	if err != nil {
		t.Fatal(err)
	}
	pid := make([]byte, 4)
	binary.LittleEndian.PutUint32(pid, uint32(os.Getpid()))
	if err := xproto.ChangePropertyChecked(c, xproto.PropModeReplace, id, atomPID, xproto.AtomCardinal, 32, 1, pid).Check(); err != nil {
		t.Fatal(err)
	}
	if mapear {
		if err := xproto.MapWindowChecked(c, id).Check(); err != nil {
			t.Fatal(err)
		}
	}
	return id
}

// ---------------------------------------------------------------------------------------------
// Target selection (genuine in attempt 1, kept) and its extensions
// ---------------------------------------------------------------------------------------------

func TestElegirHost_GanadorClaro(t *testing.T) {
	cands := []Candidato{
		{XID: 0x1001, Nombre: "leader", WH: []int{10, 10}, Mapeada: false, OverrideRedirect: false},
		{XID: 0x2001, Nombre: "pequena", WH: []int{400, 300}, Mapeada: true, OverrideRedirect: false},
		{XID: 0x2002, Nombre: "grande", WH: []int{1024, 768}, Mapeada: true, OverrideRedirect: false},
		{XID: 0x3001, Nombre: "popup", WH: []int{200, 100}, Mapeada: true, OverrideRedirect: true},
	}

	elegido, err := ElegirHost(cands)
	if err != nil {
		t.Fatalf("expected success, got error: %v", err)
	}
	if elegido.XID != 0x2002 {
		t.Fatalf("expected XID 0x2002, got 0x%x", elegido.XID)
	}
	if elegido.Area() != 1024*768 {
		t.Fatalf("expected area %d, got %d", 1024*768, elegido.Area())
	}
}

func TestElegirHost_FiltrosLiderYOverrideRedirect(t *testing.T) {
	cands := []Candidato{
		{XID: 0x1001, Nombre: "leader-10x10", WH: []int{10, 10}, Mapeada: false, OverrideRedirect: false},
		{XID: 0x1002, Nombre: "menu-override", WH: []int{500, 500}, Mapeada: true, OverrideRedirect: true},
		{XID: 0x1003, Nombre: "zero-area", WH: []int{0, 100}, Mapeada: true, OverrideRedirect: false},
	}

	_, err := ElegirHost(cands)
	if !errors.Is(err, ErrSinObjetivo) {
		t.Fatalf("expected ErrSinObjetivo, got: %v", err)
	}
}

func TestElegirHost_EmpateFailClosed(t *testing.T) {
	cands := []Candidato{
		{XID: 0x1001, Nombre: "leader", WH: []int{10, 10}, Mapeada: false, OverrideRedirect: false},
		{XID: 0x2001, Nombre: "ventana-A", WH: []int{800, 600}, Mapeada: true, OverrideRedirect: false},
		{XID: 0x2002, Nombre: "ventana-B", WH: []int{800, 600}, Mapeada: true, OverrideRedirect: false},
		{XID: 0x3001, Nombre: "tooltip", WH: []int{100, 50}, Mapeada: true, OverrideRedirect: true},
	}

	_, err := ElegirHost(cands)
	if !errors.Is(err, ErrObjetivoAmbiguo) {
		t.Fatalf("expected ErrObjetivoAmbiguo on an exact area tie, got: %v", err)
	}
}

func TestElegirHost_EmpatesTresViasYRotadoFallanCerrados(t *testing.T) {
	mapeada := func(xid uint64, w, h int) Candidato {
		return Candidato{XID: xid, WH: []int{w, h}, Mapeada: true}
	}
	casos := map[string][]Candidato{
		"tres vias":        {mapeada(1, 800, 600), mapeada(2, 800, 600), mapeada(3, 800, 600)},
		"rotado":           {mapeada(1, 800, 600), mapeada(2, 600, 800)},
		"empate al frente": {mapeada(1, 100, 100), mapeada(2, 800, 600), mapeada(3, 800, 600)},
		"empate al fondo":  {mapeada(1, 800, 600), mapeada(2, 100, 100), mapeada(3, 800, 600)},
	}
	for nombre, cands := range casos {
		if _, err := ElegirHost(cands); !errors.Is(err, ErrObjetivoAmbiguo) {
			t.Errorf("%s: expected ErrObjetivoAmbiguo, got %v", nombre, err)
		}
	}
	// The same XID listed twice is one window, not a tie.
	if got, err := ElegirHost([]Candidato{mapeada(7, 800, 600), mapeada(7, 800, 600)}); err != nil || got.XID != 7 {
		t.Errorf("a repeated XID is not a tie: %v %v", got, err)
	}
}

func TestCargarCandidatos_ArrayYObjeto(t *testing.T) {
	rawArray := []byte(`[
		{"xid": 100, "nombre": "w1", "wh": [100, 100], "mapeada": true, "override_redirect": false}
	]`)
	cands1, err := CargarCandidatos(rawArray)
	if err != nil || len(cands1) != 1 || cands1[0].XID != 100 {
		t.Fatalf("failed loading an array: %v (len=%d)", err, len(cands1))
	}

	rawObj := []byte(`{
		"candidatos": [
			{"xid": 200, "nombre": "w2", "wh": [200, 200], "mapeada": true, "override_redirect": false}
		]
	}`)
	cands2, err := CargarCandidatos(rawObj)
	if err != nil || len(cands2) != 1 || cands2[0].XID != 200 {
		t.Fatalf("failed loading an object wrapper: %v (len=%d)", err, len(cands2))
	}
}

// The fixture the contrast uses must be a real tie for the selector.
func TestFixtureEmpate_EsAmbiguo(t *testing.T) {
	raiz := enRaizRepo(t)
	cands, err := CargarCandidatosArchivo(filepath.Join(raiz, "fixture/ng/grabador/candidatos-empate.json"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ElegirHost(cands); !errors.Is(err, ErrObjetivoAmbiguo) {
		t.Fatalf("the contrast fixture must be ambiguous, got %v", err)
	}
}

// The out-of-process oracle: the candidates come from the X server itself, with an unmapped
// leader, an override-redirect popup, and real ties.
func TestCandidatosDeDisplay_ServidorReal(t *testing.T) {
	xvfbDePrueba(t)
	c, err := conectarX(":150")
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	lider := ventanaCruda(t, c, 2000, 2000, false, false)
	menu := ventanaCruda(t, c, 1024, 768, true, true)
	a := ventanaCruda(t, c, 800, 600, true, false)

	cands, err := CandidatosDeDisplay(":150", os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	porXID := map[uint64]Candidato{}
	for _, cd := range cands {
		porXID[cd.XID] = cd
	}
	if l := porXID[uint64(lider)]; l.Mapeada || l.OverrideRedirect || l.Area() != 2000*2000 {
		t.Errorf("the leader must be unmapped, not override-redirect and 2000x2000: %+v", l)
	}
	if m := porXID[uint64(menu)]; !m.Mapeada || !m.OverrideRedirect {
		t.Errorf("the popup must be mapped and override-redirect: %+v", m)
	}
	if got, err := ElegirHost(cands); err != nil || got.XID != uint64(a) {
		t.Fatalf("must pick the only mapped normal window %#x, got %+v %v", uint32(a), got, err)
	}

	b := ventanaCruda(t, c, 800, 600, true, false)
	cands, err = CandidatosDeDisplay(":150", os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ElegirHost(cands); !errors.Is(err, ErrObjetivoAmbiguo) {
		t.Fatalf("two mapped windows of equal area must be ambiguous, got %v", err)
	}

	grande := ventanaCruda(t, c, 900, 700, true, false)
	cands, err = CandidatosDeDisplay(":150", os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	if got, err := ElegirHost(cands); err != nil || got.XID != uint64(grande) {
		t.Fatalf("must pick the largest %#x (a=%#x b=%#x), got %+v %v", uint32(grande), uint32(a), uint32(b), got, err)
	}

	ajenas, err := CandidatosDeDisplay(":150", os.Getpid()+1)
	if err != nil || len(ajenas) != 0 {
		t.Errorf("the _NET_WM_PID filter must drop the windows of another pid: %v %v", ajenas, err)
	}
}

// ---------------------------------------------------------------------------------------------
// Display spellings and the host guard
// ---------------------------------------------------------------------------------------------

func TestNormalizarDisplay_Grafias(t *testing.T) {
	type caso struct {
		spec     string
		numero   int
		pantalla int
		local    bool
	}
	ok := []caso{
		{":150", 150, 0, true}, {":150.0", 150, 0, true}, {":150.3", 150, 3, true},
		{"unix:150", 150, 0, true}, {"UNIX:150.0", 150, 0, true}, {"unix/unix:150", 150, 0, true},
		{":0150", 150, 0, true}, {":+150", 150, 0, true}, {":-0", 0, 0, true},
		{"localhost:150", 150, 0, false}, {"tcp/localhost:150", 150, 0, false},
		{"[::1]:150", 150, 0, false}, {"::1:150", 150, 0, false}, {"127.0.0.1:150.2", 150, 2, false},
		{"/tmp/.X11-unix/X150", 150, 0, false}, {"unix:/tmp/.X11-unix/X150", 150, 0, false},
	}
	for _, c := range ok {
		d, err := NormalizarDisplay(c.spec)
		if err != nil {
			t.Errorf("%q: unexpected error %v", c.spec, err)
			continue
		}
		if d.Numero != c.numero || d.Pantalla != c.pantalla || d.Local() != c.local {
			t.Errorf("%q: number=%d screen=%d local=%v, expected %d %d %v", c.spec, d.Numero, d.Pantalla, d.Local(), c.numero, c.pantalla, c.local)
		}
	}
	mal := []string{"", " :150", ":150 ", ":150\n", ":150;x", "$DISPLAY", ":", "150", "x", ":150.", ":-1", ":150.x",
		":99999999", "bogus/host:1", "@/tmp/.X11-unix/X150", "unix:150/../x", "/tmp/launch:0"}
	for _, spec := range mal {
		if _, err := NormalizarDisplay(spec); !errors.Is(err, ErrDisplayInvalido) {
			t.Errorf("%q must be unreadable, got %v", spec, err)
		}
	}
}

// Every spelling that resolves to :0 or :1 is refused as the operator's display; nothing that is
// unparseable or not local is accepted either; the canonical nested spellings pass.
func TestValidarDisplayAnidado_RechazaCadaGrafiaDelHost(t *testing.T) {
	t.Setenv("DISPLAY", ":1")
	host, err := os.Hostname()
	if err != nil {
		t.Fatal(err)
	}
	delHost := []string{
		":1", ":0", ":1.0", ":0.0", ":01", ":001", ":+1", ":-0", ":1.5",
		"unix:1", "unix:1.0", "UNIX:1", "Unix:0", "unix/unix:1",
		"localhost:1", "localhost:1.0", "LOCALHOST:0", "127.0.0.1:1", "127.0.0.1:0.0",
		"[::1]:1", "::1:1", "tcp/localhost:1", "inet/localhost:1", "inet6/::1:0", host + ":1", host + ":0.0",
		"/tmp/.X11-unix/X1", "/tmp/.X11-unix/X0", "unix:/tmp/.X11-unix/X1", "/run/x/.X11-unix/X01",
	}
	for _, spec := range delHost {
		if _, err := ValidarDisplayAnidado(spec); !errors.Is(err, ErrDisplayProhibido) {
			t.Errorf("%q must be ErrDisplayProhibido, got %v", spec, err)
		}
	}
	noAnidados := []string{"", " :1", ":1 ", ":1\n", ":1;", "$DISPLAY", ":", "1", ":-1", "@/tmp/.X11-unix/X1",
		"localhost:150", "tcp/:150", "otro-equipo:150", "[::1]:150"}
	for _, spec := range noAnidados {
		if _, err := ValidarDisplayAnidado(spec); err == nil {
			t.Errorf("%q must not be accepted as a nested display", spec)
		}
	}
	for spec, canon := range map[string]string{":150": ":150", ":150.0": ":150", "unix:150": ":150", ":0150": ":150", "UNIX:7.1": ":7"} {
		d, err := ValidarDisplayAnidado(spec)
		if err != nil || d.Canonico() != canon {
			t.Errorf("%q: canonical=%q err=%v, expected %q", spec, d.Canonico(), err, canon)
		}
	}
}

// The operator's display is whatever the ambient $DISPLAY resolves to, not only :0 and :1.
func TestValidarDisplayAnidado_ElDisplayAmbienteEsDelOperador(t *testing.T) {
	t.Setenv("DISPLAY", ":77")
	for _, spec := range []string{":77", ":77.0", "unix:77", "localhost:77.0"} {
		if _, err := ValidarDisplayAnidado(spec); !errors.Is(err, ErrDisplayProhibido) {
			t.Errorf("%q must be the ambient display: %v", spec, err)
		}
	}
	if _, err := ValidarDisplayAnidado(":78"); err != nil {
		t.Errorf(":78 is not the ambient display: %v", err)
	}
	t.Setenv("DISPLAY", "localhost:10.0")
	if _, err := ValidarDisplayAnidado(":10"); !errors.Is(err, ErrDisplayProhibido) {
		t.Errorf(":10 is the operator's forwarded display: %v", err)
	}
}

// The guard runs before anything is stat'ed, dialled or spawned, at every entry point. The spies
// replace the dial and spawn seams, so a removed guard reaches a spy and never the real display.
func TestGuardiaDelHost_EnCadaPuntoDeEntrada(t *testing.T) {
	t.Setenv("DISPLAY", ":1")
	espias := instalarEspias(t)
	raiz := t.TempDir()
	for _, spec := range []string{":1", ":1.0", "unix:1", "localhost:1.0", "[::1]:01", "/tmp/.X11-unix/X1", ":0", "unix:0.0"} {
		if _, err := ArrancarXvfb(context.Background(), spec); !errors.Is(err, ErrDisplayProhibido) {
			t.Errorf("ArrancarXvfb(%q): %v", spec, err)
		}
		if _, err := CrearVentanaPrueba(spec, "x", 10, 10); !errors.Is(err, ErrDisplayProhibido) {
			t.Errorf("CrearVentanaPrueba(%q): %v", spec, err)
		}
		if _, err := CandidatosDeDisplay(spec, 0); !errors.Is(err, ErrDisplayProhibido) {
			t.Errorf("CandidatosDeDisplay(%q): %v", spec, err)
		}
		res, err := EjecutarSesion(context.Background(), SesionOpciones{Display: spec, Raiz: raiz, Segundos: 1})
		if !errors.Is(err, ErrDisplayProhibido) && (err == nil || !strings.Contains(err.Error(), ErrDisplayProhibido.Error())) {
			t.Errorf("EjecutarSesion(%q): %v", spec, err)
		}
		if res == nil || res.OK || res.ManifiestoEstado != "ausente" {
			t.Errorf("EjecutarSesion(%q): the refusal report must exist and not be OK: %+v", spec, res)
		}
	}
	espias.nada(t)
	if ents, _ := os.ReadDir(raiz); len(ents) != 0 {
		t.Errorf("a refused display must not create session directories: %v", ents)
	}
}

// A refused run still reports, with the whole field set the oracle reads (A-53): the failing path
// carries display_host_tocado from an observation of the bare process tree, and null counts.
func TestInformeDeFallo_LlevaElConjuntoDeCampos(t *testing.T) {
	espias := instalarEspias(t)
	res, err := PruebaAnidada(context.Background(), ":1.0", 1, t.TempDir())
	if err == nil || res == nil {
		t.Fatalf("a host display must be refused with a report: %v %v", res, err)
	}
	m := claves(t, res)
	for _, k := range []string{"ok", "artefactos_en_t0", "manifiesto_estado", "display_host_tocado", "diff_alcance_lineas", "testigo_alcance", "displays_x_observados", "muestras_x", "error"} {
		if _, ok := m[k]; !ok {
			t.Errorf("key %q is missing from the failure report", k)
		}
	}
	if m["ok"] != false || m["manifiesto_estado"] != "ausente" {
		t.Errorf("a refusal report can be neither OK nor have a manifest: %v", m)
	}
	if res.DisplayHostTocado != nil || res.MuestrasX < 1 || !strings.Contains(res.Error, "grabador_no_observado") {
		t.Errorf("refused before any recorder ran: the tree was read but display_host_tocado is unknown (null), never false: %+v", res)
	}
	if m["diff_alcance_lineas"] != nil {
		t.Errorf("no witness ran, so there is no count: %v", m["diff_alcance_lineas"])
	}
	espias.nada(t)
}

// ---------------------------------------------------------------------------------------------
// The kernel observation of the safety field
// ---------------------------------------------------------------------------------------------

func TestDisplayDeNombre(t *testing.T) {
	casos := map[string]struct {
		n  int
		ok bool
	}{
		"@/tmp/.X11-unix/X150":     {150, true},
		"/tmp/.X11-unix/X1":        {1, true},
		"/run/user/1/.X11-unix/X0": {0, true},
		"@/tmp/.X11-unix/X01":      {1, true},
		"/run/user/1000/bus":       {0, false},
		"":                         {0, false},
		"@/tmp/dbus-abc":           {0, false},
	}
	for nombre, want := range casos {
		if n, ok := displayDeNombre(nombre); n != want.n || ok != want.ok {
			t.Errorf("%q: (%d,%v), expected (%d,%v)", nombre, n, ok, want.n, want.ok)
		}
	}
}

// msgDiag builds one unix_diag reply exactly as the kernel lays it out.
func msgDiag(ino uint32, nombre []byte, peer uint32) []byte {
	cuerpo := make([]byte, 16)
	cuerpo[0], cuerpo[1], cuerpo[2] = 1, 1, 1
	binary.NativeEndian.PutUint32(cuerpo[4:], ino)
	attr := func(tipo uint16, data []byte) []byte {
		a := make([]byte, 4+len(data))
		binary.NativeEndian.PutUint16(a[0:], uint16(4+len(data)))
		binary.NativeEndian.PutUint16(a[2:], tipo)
		copy(a[4:], data)
		for len(a)%4 != 0 {
			a = append(a, 0)
		}
		return a
	}
	if nombre != nil {
		cuerpo = append(cuerpo, attr(unixDiagName, nombre)...)
	}
	if peer != 0 {
		p := make([]byte, 4)
		binary.NativeEndian.PutUint32(p, peer)
		cuerpo = append(cuerpo, attr(unixDiagPeer, p)...)
	}
	return nlMsg(20, cuerpo)
}

func nlMsg(tipo uint16, cuerpo []byte) []byte {
	m := make([]byte, 16, 16+len(cuerpo))
	binary.NativeEndian.PutUint32(m[0:], uint32(16+len(cuerpo)))
	binary.NativeEndian.PutUint16(m[4:], tipo)
	m = append(m, cuerpo...)
	for len(m)%4 != 0 {
		m = append(m, 0)
	}
	return m
}

func TestParsearDiagUnix_NombreYPeer(t *testing.T) {
	var b []byte
	b = append(b, msgDiag(100, []byte("\x00/tmp/.X11-unix/X150"), 0)...)
	b = append(b, msgDiag(101, nil, 100)...)
	b = append(b, msgDiag(102, []byte("/tmp/.X11-unix/X1\x00"), 0)...)
	b = append(b, nlMsg(3, make([]byte, 4))...) // NLMSG_DONE
	dst := map[uint32]sockUnix{}
	fin, err := parsearDiagUnix(b, dst)
	if err != nil || !fin {
		t.Fatalf("fin=%v err=%v", fin, err)
	}
	if got := dst[100].Nombre; got != "@/tmp/.X11-unix/X150" {
		t.Errorf("an abstract name must start with @: %q", got)
	}
	if dst[101].Peer != 100 || dst[101].Nombre != "" {
		t.Errorf("the client must point to its peer and have no name: %+v", dst[101])
	}
	if got := dst[102].Nombre; got != "/tmp/.X11-unix/X1" {
		t.Errorf("a pathname must lose its trailing NUL: %q", got)
	}

	// An error reply and a truncated one are errors, never an empty table.
	e := make([]byte, 4)
	errno := int32(syscall.EPERM)
	binary.NativeEndian.PutUint32(e, uint32(-errno))
	if _, err := parsearDiagUnix(nlMsg(2, e), map[uint32]sockUnix{}); !errors.Is(err, syscall.EPERM) {
		t.Errorf("NLMSG_ERROR must return EPERM: %v", err)
	}
	if _, err := parsearDiagUnix(nlMsg(20, make([]byte, 8)), map[uint32]sockUnix{}); err == nil {
		t.Error("a truncated reply must be an error")
	}
}

func TestParsearTCP(t *testing.T) {
	tabla := `  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:B4E2 0100007F:1771 01 00000000:00000000 00:00000000 00000000  1000        0 555001 1 0000000000000000 20 4 30 10 -1
   1: 00000000:1771 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 555002 1 0000000000000000 100 0 0 10 0
   2: 0100007F:B4E3 0100007F:0050 01 00000000:00000000 00:00000000 00000000  1000        0 555003 1 0000000000000000 20 4 30 10 -1
`
	got, err := parsearTCP(strings.NewReader(tabla))
	if err != nil {
		t.Fatal(err)
	}
	if got[555001].PuertoRemoto != 6001 || got[555001].Estado != "01" {
		t.Errorf("the client connected to 6001 must show: %+v", got[555001])
	}
	if got[555002].Estado != "0A" || got[555003].PuertoRemoto != 80 {
		t.Errorf("the listener and the web client: %+v %+v", got[555002], got[555003])
	}
}

// The observer reads live kernel state: exactly one client connection to the fake display
// appears, the accepted (server) end is not mistaken for a client, and closing the client makes
// it disappear.
func TestObservador_VeLaConexionClienteYNoLaDelServidor(t *testing.T) {
	ruta := servidorX(t, 77)
	cli, err := net.Dial("unix", ruta)
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()

	// The accepted end is added by the accept goroutine, later than the client end: wait for what is
	// asserted below (the client connection to display 77 and at least the listener, the accepted
	// and the client socket), not for the first of them.
	var obs ObservacionX
	esperarHasta(t, 10*time.Second, func() (bool, string) {
		var err error
		if obs, err = ObservarConexionesX(os.Getpid()); err != nil {
			t.Fatal(err)
		}
		got := conexionesA(obs, 77)
		if len(got) == 0 || obs.SocketsUnix < 3 {
			return false, fmt.Sprintf("connections to display 77: %v (want one), unix sockets inspected: %d (want at least 3: listener, accepted, client)", got, obs.SocketsUnix)
		}
		return true, ""
	})
	want := []ConexionX{{PID: os.Getpid(), Familia: "unix", Display: 77}}
	if got := conexionesA(obs, 77); !reflect.DeepEqual(got, want) {
		t.Fatalf("connections to display 77: %v, expected exactly %v (the server end does not count)", got, want)
	}

	cli.Close()
	esperarHasta(t, 10*time.Second, func() (bool, string) {
		obs, err := ObservarConexionesX(os.Getpid())
		if err != nil {
			t.Fatal(err)
		}
		got := conexionesA(obs, 77)
		return len(got) == 0, fmt.Sprintf("no connection may remain once the client closed: %v", got)
	})
}

// The tree matters: a child holding the connection is seen with its own pid, and a subtree can be
// excluded (the witness that reads the operator's display on purpose).
func TestObservador_VeLosDescendientesYExcluyeUnSubarbol(t *testing.T) {
	ruta := servidorX(t, 78)
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), "SRF_TEST_AYUDANTE="+ruta)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		stdin.Close()
		_ = cmd.Wait()
	})
	linea, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil || strings.TrimSpace(linea) != "listo" {
		t.Fatalf("the helper did not connect: %q %v", linea, err)
	}

	obs, err := ObservarConexionesX(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	want := ConexionX{PID: cmd.Process.Pid, Familia: "unix", Display: 78}
	encontrada := false
	for _, c := range conexionesA(obs, 78) {
		encontrada = encontrada || c == want
	}
	if !encontrada {
		t.Fatalf("child %d connected to display 78 is not seen: %v", cmd.Process.Pid, obs.Conexiones)
	}
	obs, err = ObservarConexionesX(os.Getpid(), cmd.Process.Pid)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range obs.Conexiones {
		if c.PID == cmd.Process.Pid {
			t.Errorf("the excluded subtree must not be observed: %v", c)
		}
	}
}

// A process caught between fork and exec is briefly unreadable: the observer retries it; a process
// that ended is not counted; a process that exists and stays unreadable is an error, never "none".
func TestInodosDeSockets_ProcesoEnTransicionYProcesoIlegible(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("permission bits do not apply to root")
	}
	fake := t.TempDir()
	antesProc, antesPausa := procDir, pausaReintento
	procDir = fake
	t.Cleanup(func() { procDir, pausaReintento = antesProc, antesPausa })
	fdDir := filepath.Join(fake, "555", "fd")
	if err := os.MkdirAll(fdDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("socket:[4242]", filepath.Join(fdDir, "3")); err != nil {
		t.Fatal(err)
	}
	stat := filepath.Join(fake, "555", "stat")
	if err := os.WriteFile(stat, []byte("555 (proc) S 1 555 555 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 1 0 0\n"), 0644); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(fdDir, 0755) })

	// (a) unreadable at first, readable on the retry
	if err := os.Chmod(fdDir, 0); err != nil {
		t.Fatal(err)
	}
	pausaReintento = func() { _ = os.Chmod(fdDir, 0755) }
	inos, err := inodosDeSockets(555)
	if err != nil || !reflect.DeepEqual(inos, []uint64{4242}) {
		t.Fatalf("a process that becomes readable on a retry must be observed: %v %v", inos, err)
	}

	// (b) still unreadable while the process exists: an error
	if err := os.Chmod(fdDir, 0); err != nil {
		t.Fatal(err)
	}
	pausaReintento = func() {}
	if _, err := inodosDeSockets(555); !errors.Is(err, fs.ErrPermission) {
		t.Errorf("an unreadable process that exists must be an error, got %v", err)
	}

	// (c) unreadable and gone (no stat): not counted
	if err := os.Remove(stat); err != nil {
		t.Fatal(err)
	}
	if inos, err := inodosDeSockets(555); inos != nil || err != nil {
		t.Errorf("a process that ended must not count: %v %v", inos, err)
	}
	if inos, err := inodosDeSockets(2147483646); inos != nil || err != nil {
		t.Errorf("a pid that does not exist must not count: %v %v", inos, err)
	}
}

// TCP: a client connected to port 6000+N is a connection to display N.
func TestObservador_VeUnDisplayPorTCP(t *testing.T) {
	var ln net.Listener
	var puerto int
	for p := tcpBaseX + 40; p <= tcpMaxX; p++ {
		l, err := net.Listen("tcp", "127.0.0.1:"+strconv.Itoa(p))
		if err == nil {
			ln, puerto = l, p
			break
		}
	}
	if ln == nil {
		t.Fatal("no free port between 6040 and 6063 for the test")
	}
	defer ln.Close()
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			defer c.Close()
		}
	}()
	cli, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()
	obs, err := ObservarConexionesX(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	want := ConexionX{PID: os.Getpid(), Familia: "tcp", Display: puerto - tcpBaseX}
	for _, c := range obs.Conexiones {
		if c == want {
			return
		}
	}
	t.Fatalf("the TCP connection to port %d is not seen: %v", puerto, obs.Conexiones)
}

// The positive control against a real X server: a client of Xvfb :150 is seen, and only while it
// is connected.
func TestObservador_VeUnClienteRealDeXvfb(t *testing.T) {
	srv := xvfbDePrueba(t)
	c, err := conectarX(":150")
	if err != nil {
		t.Fatal(err)
	}
	obs, err := ObservarConexionesX(os.Getpid())
	if err != nil {
		c.Close()
		t.Fatal(err)
	}
	propias := conexionesA(obs, 150)
	if len(propias) < 1 || propias[0].PID != os.Getpid() {
		c.Close()
		t.Fatalf("the xgb client of :150 is not seen: %v", obs.Conexiones)
	}
	for _, x := range obs.Conexiones {
		if x.PID == srv.PID {
			t.Errorf("Xvfb is a server, not a client: %v", x)
		}
	}
	c.Close()
	time.Sleep(150 * time.Millisecond)
	obs, err = ObservarConexionesX(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	if got := conexionesA(obs, 150); len(got) != 0 {
		t.Errorf("once the client closed it must not show: %v", got)
	}
}

// The verdict logic: false is only reported when the recorder itself was observed alive in at least
// three samples and the observer saw the recorder's own connection; anything less is unknown (null),
// and a foreign connection is true whatever else went wrong.
func TestDecidir_NuncaAfirmaFalseSinVer(t *testing.T) {
	m := nuevoMuestreador(150)
	if v, motivo := m.decidir(); v != nil || motivo != "observador_sin_muestras" {
		t.Errorf("no samples: (%v,%q)", v, motivo)
	}
	m.muestras = 10
	if v, motivo := m.decidir(); v != nil || !strings.Contains(motivo, "grabador_no_observado") {
		t.Errorf("samples that never saw the recorder: (%v,%q)", v, motivo)
	}
	m.grabadorVivo = minMuestrasGrabador - 1
	m.grabadorConectado = 5
	if v, motivo := m.decidir(); v != nil || !strings.Contains(motivo, "grabador_no_observado") {
		t.Errorf("a recorder seen alive in only %d samples is not enough: (%v,%q)", m.grabadorVivo, v, motivo)
	}
	m.grabadorVivo = minMuestrasGrabador
	m.grabadorConectado = 0
	if v, motivo := m.decidir(); v != nil || !strings.Contains(motivo, "observador_ciego") {
		t.Errorf("a recorder alive but never seen connected to the nested display: (%v,%q)", v, motivo)
	}
	m.grabadorConectado = 2
	if v, motivo := m.decidir(); v == nil || *v || motivo != "" {
		t.Errorf("with the recorder alive and connected, and no foreign connection, it must be false: %v %q", v, motivo)
	}
	m.sinSubreaper = errors.New("prctl: denied")
	if v, motivo := m.decidir(); v != nil || !strings.Contains(motivo, "membresia_no_garantizada") {
		t.Errorf("without the subreaper the membership is not guaranteed: (%v,%q)", v, motivo)
	}
	m.sinSubreaper = nil
	m.errores = 1
	if v, motivo := m.decidir(); v != nil || !strings.Contains(motivo, "observador_no_disponible") {
		t.Errorf("a failed sample makes the value unknown: (%v,%q)", v, motivo)
	}
	m.foraneas[ConexionX{PID: 5, Familia: "unix", Display: 1}] = true
	if v, _ := m.decidir(); v == nil || !*v {
		t.Errorf("a foreign connection is true even with failures: %v", v)
	}
}

// registrar tells the recorder's own connection from the supervisor's: the supervisor (the observing
// process) and a helper of its own are in the tree as well, and their connection to the nested
// display must not satisfy the "observer can see the recorder" canary. A recorder pid that now has
// another start time is a different process and is not seen alive.
func TestRegistrar_SoloLaConexionDelGrabadorSatisfaceElCanario(t *testing.T) {
	const yo, grabador, hijo, otro = 100, 500, 501, 600
	m := nuevoMuestreador(150)
	m.grabador = procID{PID: grabador, Inicio: 7}
	obs := func(conexiones ...ConexionX) ObservacionX {
		return ObservacionX{
			Conexiones: conexiones,
			padres:     map[int]int{yo: 1, grabador: yo, hijo: grabador, otro: yo},
			inicios:    map[int]uint64{yo: 1, grabador: 7, hijo: 8, otro: 9},
		}
	}
	dispuesta := func(pid int) ConexionX { return ConexionX{PID: pid, Familia: "unix", Display: 150} }

	m.registrar(obs(dispuesta(yo), dispuesta(otro)))
	if m.grabadorVivo != 1 || m.grabadorConectado != 0 {
		t.Errorf("the supervisor's and another child's connections are not the recorder's: alive=%d connected=%d", m.grabadorVivo, m.grabadorConectado)
	}
	m.registrar(obs(dispuesta(grabador)))
	if m.grabadorVivo != 2 || m.grabadorConectado != 1 {
		t.Errorf("the recorder's own connection must count: alive=%d connected=%d", m.grabadorVivo, m.grabadorConectado)
	}
	m.registrar(obs(dispuesta(hijo)))
	if m.grabadorConectado != 2 {
		t.Errorf("a child of the recorder is part of the recorder subtree: connected=%d", m.grabadorConectado)
	}
	reusado := obs(dispuesta(grabador))
	reusado.inicios[grabador] = 99 // the number now belongs to another process
	m.registrar(reusado)
	if m.grabadorVivo != 3 || m.grabadorConectado != 2 {
		t.Errorf("a reused recorder pid is not the recorder: alive=%d connected=%d", m.grabadorVivo, m.grabadorConectado)
	}
	if len(m.foraneas) != 0 {
		t.Errorf("all of it went to the nested display: %v", m.foraneas)
	}
	// a cyclic parent table (a corrupt reading) must end, not loop
	ciclo := ObservacionX{padres: map[int]int{1: 2, 2: 1}, inicios: map[int]uint64{1: 1, 2: 2}}
	if ciclo.bajo(1, procID{PID: 77, Inicio: 1}) {
		t.Errorf("no member descends from an unrelated pid")
	}
}

// The observer must not depend on the guard: a connection to the operator's display is foreign even
// when that display is the one that was declared, and a connection to any other display is foreign
// when it is not the declared one.
func TestRegistrar_LaConexionAlHostEsAjenaAunSiEsLaDeclarada(t *testing.T) {
	t.Setenv("DISPLAY", ":1")
	m := nuevoMuestreador(1) // a bypassed guard would leave the host display declared
	m.grabador = procID{PID: 9, Inicio: 1}
	obs := func(c ...ConexionX) ObservacionX {
		return ObservacionX{Conexiones: c, padres: map[int]int{9: 1}, inicios: map[int]uint64{9: 1}}
	}
	m.registrar(obs(ConexionX{PID: 9, Familia: "unix", Display: 1}))
	if len(m.foraneas) != 1 || m.grabadorConectado != 0 {
		t.Errorf("a connection to display :1 must be foreign even when declared: foreign=%v connected=%d", m.foraneas, m.grabadorConectado)
	}
	m = nuevoMuestreador(150)
	m.grabador = procID{PID: 9, Inicio: 1}
	m.registrar(obs(ConexionX{PID: 9, Familia: "unix", Display: 150}, ConexionX{PID: 9, Familia: "tcp", Display: 151}))
	if len(m.foraneas) != 1 || m.grabadorConectado != 1 || !m.foraneas[ConexionX{PID: 9, Familia: "tcp", Display: 151}] {
		t.Errorf("only the connection to 151 is foreign: foreign=%v connected=%d", m.foraneas, m.grabadorConectado)
	}
}

// ---------------------------------------------------------------------------------------------
// Nested Xvfb and the test window
// ---------------------------------------------------------------------------------------------

func TestXvfb_ArrancaYDetenerNoDejaResiduos(t *testing.T) {
	srv, err := ArrancarXvfb(context.Background(), ":150")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = srv.Detener() })
	for _, p := range []string{srv.LockPath, srv.SocketPath} {
		if _, err := os.Lstat(p); err != nil {
			t.Fatalf("with Xvfb alive %s must exist: %v", p, err)
		}
	}
	if _, err := os.Stat("/proc/" + strconv.Itoa(srv.PID)); err != nil {
		t.Fatalf("Xvfb %d must be alive: %v", srv.PID, err)
	}
	if _, err := ArrancarXvfb(context.Background(), ":150"); err == nil || !strings.Contains(err.Error(), "already served") {
		t.Errorf("a second Xvfb on a live display must be refused: %v", err)
	}
	if err := srv.Detener(); err != nil {
		t.Fatalf("Detener must be clean on SIGTERM, neither forced nor leaving leftovers: %v", err)
	}
	sinResiduosX(t, 150)
	if _, err := os.Stat("/proc/" + strconv.Itoa(srv.PID)); err == nil {
		t.Errorf("Xvfb %d is still in /proc after Detener (not reaped)", srv.PID)
	}
	if err := srv.Detener(); err != nil {
		t.Errorf("Detener must be idempotent: %v", err)
	}
}

func TestCrearVentanaPrueba_EsVisibleYSeCierraLimpia(t *testing.T) {
	xvfbDePrueba(t)
	win, err := CrearVentanaPrueba(":150", "titulo-de-prueba", 640, 480)
	if err != nil {
		t.Fatal(err)
	}
	// What the server says, read on a second connection.
	cands, err := CandidatosDeDisplay(":150", os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	if len(cands) != 1 || cands[0].XID != win.XID || !cands[0].Mapeada || cands[0].Nombre != "titulo-de-prueba" ||
		cands[0].Clase != "srf-ventana-prueba" || cands[0].Area() != 640*480 {
		t.Fatalf("the server does not see the window as created: %+v (xid=%#x)", cands, win.XID)
	}
	if err := win.Cerrar(); err != nil {
		t.Fatalf("Cerrar: %v", err)
	}
	cands, err = CandidatosDeDisplay(":150", os.Getpid())
	if err != nil || len(cands) != 0 {
		t.Errorf("after Cerrar the server must not see the window: %v %v", cands, err)
	}
	if err := win.Cerrar(); err != nil {
		t.Errorf("Cerrar must be idempotent: %v", err)
	}
}

// ---------------------------------------------------------------------------------------------
// Recording sessions
// ---------------------------------------------------------------------------------------------

// The full nested test with the real witness (the campaign's own script over the operator's
// display) and every claim re-read by a source other than the session's own result.
func TestPruebaAnidada_RealObservaSinTocarElHost(t *testing.T) {
	enRaizRepo(t)
	raiz := t.TempDir()
	res, err := PruebaAnidada(context.Background(), ":150", 1, raiz)
	if err != nil || res == nil || !res.OK {
		t.Fatalf("the nested test must be OK: err=%v res=%+v", err, res)
	}

	// artifacts: counted by this test on the directory, not taken from the result
	ents, err := os.ReadDir(res.Dir)
	if err != nil {
		t.Fatal(err)
	}
	var nombres []string
	for _, e := range ents {
		nombres = append(nombres, e.Name())
	}
	if want := []string{FicheroFlujo, FicheroEventos, FicheroManifiesto, FicheroVideo}; !sameSet(nombres, want) {
		t.Errorf("the session directory must hold exactly the 4 artifacts and no supervisor.pid or temporary: %v", nombres)
	}
	if res.ArtefactosEnT0 != 4 {
		t.Errorf("artefactos_en_t0=%d", res.ArtefactosEnT0)
	}

	// manifest: re-read here from disk
	raw, err := os.ReadFile(filepath.Join(res.Dir, FicheroManifiesto))
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatal(err)
	}
	if m["estado"] != "completa" || res.ManifiestoEstado != "completa" {
		t.Errorf("manifest on disk=%v, result=%q", m["estado"], res.ManifiestoEstado)
	}

	// video and events: stat and count by this test
	fi, err := os.Stat(filepath.Join(res.Dir, FicheroVideo))
	if err != nil || fi.Size() == 0 || fi.Size() != res.VideoBytes {
		t.Errorf("video: stat=%v err=%v result=%d", fi, err, res.VideoBytes)
	}
	ev, err := os.ReadFile(filepath.Join(res.Dir, FicheroEventos))
	if err != nil {
		t.Fatal(err)
	}
	lineas := strings.Split(strings.TrimSuffix(string(ev), "\n"), "\n")
	if len(lineas) != res.EventosLineas || len(lineas) < 2 {
		t.Errorf("eventos.jsonl has %d lines, the result says %d", len(lineas), res.EventosLineas)
	}
	for i, l := range lineas {
		var e map[string]any
		if err := json.Unmarshal([]byte(l), &e); err != nil || e["sequence"] != float64(i+1) {
			t.Errorf("event line %d is invalid: %q %v", i+1, l, err)
		}
	}
	if !strings.Contains(lineas[0], `"session.open"`) || !strings.Contains(lineas[len(lineas)-1], `"session.close"`) {
		t.Errorf("eventos.jsonl must open with session.open and close with session.close")
	}

	// safety: the kernel saw the nested display only
	if res.DisplayHostTocado == nil || *res.DisplayHostTocado {
		t.Errorf("display_host_tocado must be a measured false: %v", res.DisplayHostTocado)
	}
	if !reflect.DeepEqual(res.DisplaysXObservados, []int{150}) || len(res.ConexionesForaneas) != 0 {
		t.Errorf("observed displays=%v foreign=%v", res.DisplaysXObservados, res.ConexionesForaneas)
	}
	if res.MuestrasX < 3 || res.ConexionesXMax < 2 {
		t.Errorf("the observer must have seen ffmpeg and the window: samples=%d max=%d", res.MuestrasX, res.ConexionesXMax)
	}
	if res.MuestrasGrabadorVivo < minMuestrasGrabador || res.MuestrasGrabadorConectado < 1 {
		t.Errorf("false needs the recorder alive in %d samples and seen connected: alive=%d connected=%d", minMuestrasGrabador, res.MuestrasGrabadorVivo, res.MuestrasGrabadorConectado)
	}

	// the video: ffprobe, a source other than the session's own decode, must play it
	fotogramas, duracion, errP := ffprobeIndependiente(filepath.Join(res.Dir, FicheroVideo))
	if errP != nil || fotogramas < 1 || duracion <= 0 {
		t.Errorf("the recorded mp4 must be playable: ffprobe frames=%d duration=%v err=%v", fotogramas, duracion, errP)
	}
	if res.VideoValidacion != "ok" || res.VideoFotogramas != fotogramas {
		t.Errorf("the session decoded %d frames (%q), ffprobe counts %d", res.VideoFotogramas, res.VideoValidacion, fotogramas)
	}
	if m["video"].(map[string]any)["fotogramas"] != float64(fotogramas) {
		t.Errorf("the manifest on disk says %v frames, ffprobe %d", m["video"], fotogramas)
	}

	// scope witness
	if res.DiffAlcanceLineas == nil || *res.DiffAlcanceLineas != 0 || res.TestigoAlcance != TestigoAlcance {
		t.Errorf("witness: lines=%v script=%q", res.DiffAlcanceLineas, res.TestigoAlcance)
	}

	// XID: the server-enumerated one against the client-allocated one
	if res.XID == "" || res.XID != res.XIDAsignado || res.XIDCoincide == nil || !*res.XIDCoincide {
		t.Errorf("xid=%q allocated=%q matches=%v", res.XID, res.XIDAsignado, res.XIDCoincide)
	}

	sinResiduosX(t, 150)
	sinHijos(t)
}

// ffprobeIndependiente is the second source for the video: ffprobe (which the production code
// cannot run through the launcher) reads the first video stream of path. A non-nil error means
// ffprobe refused the file.
func ffprobeIndependiente(path string) (fotogramas int, duracionS float64, err error) {
	salida, err := exec.Command("ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
		"-show_entries", "stream=nb_read_frames:format=duration", "-of", "json", path).Output()
	if err != nil {
		return 0, 0, err
	}
	var j struct {
		Streams []struct {
			NbReadFrames string `json:"nb_read_frames"`
		} `json:"streams"`
		Format struct {
			Duration string `json:"duration"`
		} `json:"format"`
	}
	if err := json.Unmarshal(salida, &j); err != nil {
		return 0, 0, err
	}
	if len(j.Streams) == 0 {
		return 0, 0, errors.New("ffprobe found no video stream")
	}
	fotogramas, _ = strconv.Atoi(j.Streams[0].NbReadFrames)
	duracionS, _ = strconv.ParseFloat(j.Format.Duration, 64)
	return fotogramas, duracionS, nil
}

func sameSet(a, b []string) bool {
	x, y := append([]string(nil), a...), append([]string(nil), b...)
	sortStrings(x)
	sortStrings(y)
	return reflect.DeepEqual(x, y)
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}

// The safety field must be able to be true. The recorder is pointed, through the test seam, at a
// display other than the one declared: the kernel table shows ffmpeg connected to it and the field
// flips. The control run right after, with the recorder on the declared display, stays false.
func TestSesion_ElCampoDeSeguridadPuedeSerTrue(t *testing.T) {
	enRaizRepo(t)
	srv := xvfbDePrueba(t)
	base := SesionOpciones{Raiz: t.TempDir(), Segundos: 1, Titulo: "seguridad", entornoTestigo: []string{"SRF_DISPLAY=:150"}}

	ajena := base
	ajena.Display = ":149"       // the display the guard approved
	ajena.entradaFFmpeg = ":150" // where ffmpeg really connects
	res, err := EjecutarSesion(context.Background(), ajena)
	if err == nil || res.OK {
		t.Fatalf("a session whose ffmpeg touches another display cannot be OK: %+v", res)
	}
	if res.DisplayHostTocado == nil || !*res.DisplayHostTocado {
		t.Fatalf("display_host_tocado must be true: %+v", res)
	}
	if len(res.ConexionesForaneas) == 0 || res.ConexionesForaneas[0].Display != 150 || res.ConexionesForaneas[0].PID == os.Getpid() {
		t.Errorf("the foreign connection must belong to a child (ffmpeg) and go to display 150: %v", res.ConexionesForaneas)
	}
	if res.ManifiestoEstado != "completa" {
		t.Errorf("the video was recorded; the rejection is only about the display: %q", res.ManifiestoEstado)
	}

	control := base
	control.Display = ":150"
	res, err = EjecutarSesion(context.Background(), control)
	if err != nil || !res.OK || res.DisplayHostTocado == nil || *res.DisplayHostTocado {
		t.Fatalf("the control must be false and OK: err=%v res=%+v", err, res)
	}
	sinHijos(t, srv.PID)
}

// The witness count is the number of lines the script printed during THIS session, not a
// constant: a mapped campaign window on the watched display makes the script print a violation,
// and the session reports exactly what an independent run of the script prints.
func TestSesion_ElTestigoDeAlcanceCuentaLasLineasQueImprime(t *testing.T) {
	enRaizRepo(t)
	xvfbDePrueba(t)
	win, err := CrearVentanaPrueba(":150", "ventana-de-campana", 300, 200)
	if err != nil {
		t.Fatal(err)
	}
	defer win.Cerrar()

	res, err := EjecutarSesion(context.Background(), SesionOpciones{
		Display: ":150", Raiz: t.TempDir(), Segundos: 1, Titulo: "testigo", XID: win.XID,
		entornoTestigo: []string{"SRF_DISPLAY=:150"},
	})
	if err == nil || res.OK {
		t.Fatalf("a witness that reports violations must invalidate the session: %+v", res)
	}
	independiente := exec.Command("bash", TestigoAlcance, "diff")
	independiente.Env = append(os.Environ(), "SRF_DISPLAY=:150")
	salida, errI := independiente.Output()
	if errI != nil {
		t.Fatalf("the independent witness failed: %v", errI)
	}
	n := bytes.Count(salida, []byte{'\n'})
	if n == 0 {
		t.Fatalf("an undeclared srf-* window must be a violation: %q", salida)
	}
	if res.DiffAlcanceLineas == nil || *res.DiffAlcanceLineas != n || res.TestigoAlcance != TestigoAlcance {
		t.Errorf("the session says %v lines from %q, the independent witness printed %d", res.DiffAlcanceLineas, res.TestigoAlcance, n)
	}
	if !strings.Contains(res.Error, "scope witness") {
		t.Errorf("the error must name the witness: %q", res.Error)
	}
}

// The witness reads the operator's display on purpose and is not a recorder: a connection held by
// its subtree must not make the session "touched". The witness stand-in runs the helper (this test
// binary) holding a connection to a display other than the nested one for longer than the recording;
// a goroutine of the test, which does not exclude the witness, proves that the connection is really
// visible in the kernel table while the session runs.
func TestSesion_ElTestigoNoCuentaComoRecolector(t *testing.T) {
	sondaFalsa(t)
	enRaizRepo(t)
	srv := xvfbDePrueba(t)
	sock := servidorX(t, 78)
	testigo := escribirScript(t, t.TempDir(), "testigo.sh",
		fmt.Sprintf("SRF_TEST_AYUDANTE=%q SRF_TEST_AYUDANTE_SEG=3 exec %q >/dev/null\n", sock, os.Args[0]))

	var visto atomic.Bool
	parar := make(chan struct{})
	terminado := make(chan struct{})
	go func() {
		defer close(terminado)
		for {
			select {
			case <-parar:
				return
			default:
			}
			if obs, err := ObservarConexionesX(os.Getpid()); err == nil {
				for _, c := range conexionesA(obs, 78) {
					if c.PID != os.Getpid() {
						visto.Store(true)
					}
				}
			}
			time.Sleep(20 * time.Millisecond)
		}
	}()
	res, err := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigo})
	close(parar)
	<-terminado

	if !visto.Load() {
		t.Fatal("the helper's connection to display 78 was never visible: the test would prove nothing")
	}
	if err != nil || !res.OK || res.DisplayHostTocado == nil || *res.DisplayHostTocado {
		t.Fatalf("a connection held by the witness subtree must not count: err=%v res=%+v", err, res)
	}
	sinHijos(t, srv.PID)
}

// A failing witness (rc != 0) leaves the count unknown, never zero.
func TestSesion_UnTestigoQueFallaDejaElConteoDesconocido(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "exec sleep 5")
	fallido := escribirScript(t, t.TempDir(), "testigo.sh", "echo boom >&2\nexit 7\n")
	res, err := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: fallido})
	if err == nil || res.OK || res.DiffAlcanceLineas != nil {
		t.Fatalf("the count must be null and the session not OK: err=%v %+v", err, res)
	}
	if m := claves(t, res); m["diff_alcance_lineas"] != nil {
		t.Errorf("diff_alcance_lineas must serialize as null: %v", m["diff_alcance_lineas"])
	}
	sinHijos(t)
}

// The failing path writes its report and leaves nothing behind: ffmpeg dies at once, the manifest
// on disk says incompleta, there is no supervisor.pid naming a dead pid, no children.
func TestSesion_ElFalloEscribeInformeYNoDejaResiduos(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "exit 3")
	raiz := t.TempDir()
	res, err := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: raiz, Segundos: 2, TestigoAlcance: testigoFalso(t, 0)})
	if err == nil || res == nil || res.OK {
		t.Fatalf("a dying ffmpeg must give a session that is not OK: %v %+v", err, res)
	}
	if !strings.Contains(res.Error, "ffmpeg exited early") {
		t.Errorf("the error must say ffmpeg exited early: %q", res.Error)
	}
	m, errM := LeerManifiesto(filepath.Join(res.Dir, FicheroManifiesto))
	if errM != nil || m.Estado != "incompleta" || res.ManifiestoEstado != "incompleta" {
		t.Errorf("the manifest on disk must be incompleta: %v %v", m, errM)
	}
	if _, err := os.Stat(filepath.Join(res.Dir, FicheroPID)); err == nil {
		t.Errorf("supervisor.pid was left in %s naming a dead process", res.Dir)
	}
	if res.DisplayHostTocado != nil && *res.DisplayHostTocado {
		t.Errorf("nobody touched any display: %v", res.DisplayHostTocado)
	}
	if res.DisplayHostTocado != nil {
		t.Errorf("without seeing the connection to the nested display the field is unknown (null), not false: %v", *res.DisplayHostTocado)
	}
	if res.DiffAlcanceLineas == nil || *res.DiffAlcanceLineas != 0 {
		t.Errorf("the fake witness printed 0 lines: %v", res.DiffAlcanceLineas)
	}
	for _, k := range []string{"artefactos_en_t0", "manifiesto_estado", "display_host_tocado", "diff_alcance_lineas", "testigo_alcance"} {
		if _, ok := claves(t, res)[k]; !ok {
			t.Errorf("%q is missing from the failure report", k)
		}
	}
	sinHijos(t)
}

// A cancelled context ends the recording loop (a break that only left the select spun until some
// other case fired), stops the children and leaves a consistent, incomplete session. The recorder
// stand-in ignores SIGINT, so it survives the cancel and only ctx.Done can end the loop: a loop that
// kept going would keep writing tick events for seconds.
func TestSesion_LaCancelacionCortaElBucleYNoDejaResiduos(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "trap '' INT\nexec sleep 30")
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(400 * time.Millisecond)
		cancel()
	}()
	inicio := time.Now()
	res, err := EjecutarSesion(ctx, SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 8, TestigoAlcance: testigoFalso(t, 0)})
	if d := time.Since(inicio); d > 6*time.Second {
		t.Errorf("cancellation took %v; the loop must end at once", d)
	}
	if err == nil || res.OK || !strings.Contains(res.Error, "cancelled") {
		t.Fatalf("a cancelled session is not OK and says so: %v %+v", err, res)
	}
	if res.EventosLineas > 3 {
		t.Errorf("the loop kept recording after the cancel: %d event lines", res.EventosLineas)
	}
	if res.ManifiestoEstado != "incompleta" {
		t.Errorf("manifest=%q", res.ManifiestoEstado)
	}
	if _, err := os.Stat(filepath.Join(res.Dir, FicheroPID)); err == nil {
		t.Errorf("supervisor.pid was left after cancelling")
	}
	sinHijos(t)
}

// Cancelling while the real Xvfb and ffmpeg run tears everything down without residues: Xvfb gets
// SIGTERM (a killed Xvfb leaves /tmp/.X150-lock behind), ffmpeg gets SIGINT, no child survives.
func TestPruebaAnidada_LaCancelacionLimpiaTodo(t *testing.T) {
	sondaFalsa(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() {
		time.Sleep(1500 * time.Millisecond)
		cancel()
	}()
	inicio := time.Now()
	res, err := pruebaAnidada(ctx, SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 30, Titulo: "cancelled", TestigoAlcance: testigoFalso(t, 0)})
	if d := time.Since(inicio); d > 8*time.Second {
		t.Errorf("cancellation took %v", d)
	}
	if err == nil || res == nil || res.OK || !strings.Contains(res.Error, "cancelled") {
		t.Fatalf("a cancelled test is not OK and says so: %v %+v", err, res)
	}
	if strings.Contains(res.Error, "Xvfb") {
		t.Errorf("the Xvfb teardown must be clean after cancelling (neither forced nor leaving leftovers): %s", res.Error)
	}
	if res.ManifiestoEstado != "incompleta" {
		t.Errorf("manifest=%q", res.ManifiestoEstado)
	}
	sinResiduosX(t, 150)
	sinHijos(t)
}

// The session directory can never leave the root, whatever the title says.
func TestSesion_ElTituloNoEscapaDeLaRaiz(t *testing.T) {
	instalarEspias(t) // ffmpeg fails to start; only the directory matters here
	raiz := t.TempDir()
	res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: raiz, Titulo: "/../../escape"})
	if res.Dir == "" || filepath.Dir(res.Dir) != raiz {
		t.Fatalf("the session directory %q must hang directly from %q", res.Dir, raiz)
	}
}

// ---------------------------------------------------------------------------------------------
// Artifacts on disk (genuine in attempt 1, kept) and the manifest write
// ---------------------------------------------------------------------------------------------

func TestContarArtefactosEnDisco(t *testing.T) {
	tmpDir := t.TempDir()

	count0, _ := ContarArtefactosEnDisco(tmpDir)
	if count0 != 0 {
		t.Fatalf("expected 0 artifacts, measured %d", count0)
	}

	_ = os.WriteFile(filepath.Join(tmpDir, FicheroEventos), []byte("{}\n"), 0644)
	_ = os.WriteFile(filepath.Join(tmpDir, FicheroFlujo), []byte("# Flujo\n"), 0644)
	_ = os.WriteFile(filepath.Join(tmpDir, FicheroManifiesto), []byte(`{"estado":"incompleta"}`), 0644)

	count3, _ := ContarArtefactosEnDisco(tmpDir)
	if count3 != 3 {
		t.Fatalf("expected 3 artifacts, measured %d", count3)
	}

	_ = os.WriteFile(filepath.Join(tmpDir, FicheroVideo), []byte("fake mp4 content"), 0644)

	count4, lista := ContarArtefactosEnDisco(tmpDir)
	if count4 != 4 || len(lista) != 4 {
		t.Fatalf("expected 4 artifacts, measured %d (%v)", count4, lista)
	}
}

func TestManifiesto_IncompletaYCompleta(t *testing.T) {
	tmpDir := t.TempDir()
	manifestPath := filepath.Join(tmpDir, FicheroManifiesto)
	m := &Manifiesto{
		SchemaVersion:   "1.0.0",
		SessionID:       "test-session-123",
		Estado:          "incompleta",
		RazonIncompleta: "sesion en curso",
		Display:         ":150",
	}

	if err := EscribirManifiestoAtomico(manifestPath, m); err != nil {
		t.Fatalf("failed writing the atomic manifest: %v", err)
	}
	mLeido, err := LeerManifiesto(manifestPath)
	if err != nil {
		t.Fatalf("failed reading the manifest: %v", err)
	}
	if mLeido.Estado != "incompleta" {
		t.Fatalf("expected state incompleta, read %q", mLeido.Estado)
	}

	mLeido.Estado = "completa"
	mLeido.RazonIncompleta = ""
	if err := EscribirManifiestoAtomico(manifestPath, mLeido); err != nil {
		t.Fatalf("failed writing the completa transition: %v", err)
	}
	mFinal, err := LeerManifiesto(manifestPath)
	if err != nil {
		t.Fatalf("failed reading the final manifest: %v", err)
	}
	if mFinal.Estado != "completa" {
		t.Fatalf("expected state completa, read %q", mFinal.Estado)
	}
}

// A manifest write that fails must not leave its temporary file behind.
func TestEscribirManifiestoAtomico_NoDejaElTemporalSiFalla(t *testing.T) {
	dir := t.TempDir()
	destino := filepath.Join(dir, FicheroManifiesto)
	if err := os.MkdirAll(filepath.Join(destino, "ocupado"), 0755); err != nil { // a non-empty directory cannot be replaced
		t.Fatal(err)
	}
	if err := EscribirManifiestoAtomico(destino, &Manifiesto{Estado: "completa"}); err == nil {
		t.Fatal("renaming over a non-empty directory must fail")
	}
	if _, err := os.Stat(destino + ".escribiendo"); err == nil {
		t.Errorf("the temporary %s.escribiendo was left behind after the failure", destino)
	}
}

// ---------------------------------------------------------------------------------------------
// Attempt 3: membership that survives re-parenting (H-4), the meaning of false (H-5, H-6, H-11),
// the witness canary (H-2, H-10), the playable video (H-3), and the controls that had no test
// (H-7, H-8, H-9, H-11)
// ---------------------------------------------------------------------------------------------

// procFalso is a process table on disk: procDir points at it, so a test can forge exactly the rows
// a consumer reads (stat, descriptors, the TCP table) and prove what the consumer does with them.
type procFalso struct{ dir string }

func nuevoProcFalso(t *testing.T) *procFalso {
	t.Helper()
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "net"), 0755); err != nil {
		t.Fatal(err)
	}
	antes := procDir
	procDir = dir
	t.Cleanup(func() { procDir = antes })
	return &procFalso{dir: dir}
}

// proceso writes (or rewrites) the stat and the socket descriptors of a forged process.
func (p *procFalso) proceso(t *testing.T, pid, ppid, pgrp int, inicio uint64, inodos ...uint64) {
	t.Helper()
	dir := filepath.Join(p.dir, strconv.Itoa(pid))
	if err := os.MkdirAll(filepath.Join(dir, "fd"), 0755); err != nil {
		t.Fatal(err)
	}
	stat := fmt.Sprintf("%d (forjado) S %d %d %d 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 %d 0 0\n", pid, ppid, pgrp, pgrp, inicio)
	if err := os.WriteFile(filepath.Join(dir, "stat"), []byte(stat), 0644); err != nil {
		t.Fatal(err)
	}
	for i, ino := range inodos {
		enlace := filepath.Join(dir, "fd", strconv.Itoa(3+i))
		_ = os.Remove(enlace)
		if err := os.Symlink(fmt.Sprintf("socket:[%d]", ino), enlace); err != nil {
			t.Fatal(err)
		}
	}
}

// tcp writes the forged /proc/net/tcp: one row per (inode, remote port, state).
func (p *procFalso) tcp(t *testing.T, filas ...[3]string) {
	t.Helper()
	tabla := "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
	for i, f := range filas {
		tabla += fmt.Sprintf("   %d: 0100007F:B4E2 0100007F:%s %s 00000000:00000000 00:00000000 00000000  1000        0 %s 1 0000000000000000 20 4 30 10 -1\n", i, f[1], f[2], f[0])
	}
	if err := os.WriteFile(filepath.Join(p.dir, "net", "tcp"), []byte(tabla), 0644); err != nil {
		t.Fatal(err)
	}
}

// inodoDeSocket is the kernel inode of the socket behind c, read from the descriptor table.
func inodoDeSocket(t *testing.T, c net.Conn) uint64 {
	t.Helper()
	sc, err := c.(*net.UnixConn).SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var ino uint64
	var errL error
	if err := sc.Control(func(fd uintptr) {
		var l string
		if l, errL = os.Readlink(fmt.Sprintf("/proc/self/fd/%d", fd)); errL == nil {
			ino, errL = strconv.ParseUint(strings.TrimSuffix(strings.TrimPrefix(l, "socket:["), "]"), 10, 64)
		}
	}); err != nil || errL != nil {
		t.Fatalf("cannot read the inode of the socket: %v %v", err, errL)
	}
	return ino
}

// A double-forked descendant: the subshell starts the helper in the background and exits, so the
// helper's parent is gone at once and the kernel re-parents it. Because the process is a child
// subreaper it goes to this process, not to pid 1, and the walk from this process reaches it.
func TestObservador_AtribuyeAlDescendienteReparentado(t *testing.T) {
	sock := servidorX(t, 79)
	anuncio := filepath.Join(t.TempDir(), "ayudante.txt")
	cmd, err := crearComando(context.Background(), "bash", "-c",
		`( SRF_TEST_AYUDANTE="$1" SRF_TEST_AYUDANTE_SEG=3 "$2" > "$3" 2>/dev/null </dev/null & ); exit 0`, "bash", sock, os.Args[0], anuncio)
	if err != nil {
		t.Fatal(err)
	}
	p, err := iniciar(cmd, syscall.SIGTERM, time.Second, nil) // the package's spawn point arms the subreaper
	if err != nil {
		t.Fatal(err)
	}
	<-p.done
	pid := anuncioDelAyudante(t, anuncio)

	// the premise: the helper is no longer a child of the shell that started it
	esperarHasta(t, 5*time.Second, func() (bool, string) {
		inf, ok := leerInfo(pid)
		if !ok {
			return false, fmt.Sprintf("helper %d is not in the process table", pid)
		}
		return inf.PPid == os.Getpid(), fmt.Sprintf("helper %d has parent %d, expected the observing process %d (a subreaper adopts orphans; pid 1 would not)", pid, inf.PPid, os.Getpid())
	})
	want := ConexionX{PID: pid, Familia: "unix", Display: 79}
	esperarHasta(t, 5*time.Second, func() (bool, string) {
		obs, err := ObservarConexionesX(os.Getpid())
		if err != nil {
			t.Fatal(err)
		}
		for _, c := range conexionesA(obs, 79) {
			if c == want {
				return true, ""
			}
		}
		return false, fmt.Sprintf("the re-parented helper's connection %v is not attributed: %v", want, obs.Conexiones)
	})
	obs, err := ObservarConexionesX(os.Getpid(), pid)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range obs.Conexiones {
		if c.PID == pid {
			t.Errorf("an excluded pid must not be attributed: %v", c)
		}
	}
	// the helper ends by itself; the adopted zombie is reaped, nothing is left in the table
	esperarQueSeVaya(t, pid, 10*time.Second)
	sinHijos(t)
}

// The same through a whole session: the recorder double-forks a helper that holds a connection to
// a display that is not the nested one, and the session says the display was touched, naming the
// helper. Without the subreaper the helper would be a child of pid 1 and invisible.
func TestSesion_UnDescendienteReparentadoDelGrabadorEsAtribuido(t *testing.T) {
	sondaFalsa(t)
	sock := servidorX(t, 78)
	anuncio := filepath.Join(t.TempDir(), "ayudante.txt")
	ffmpegFalso(t, fmt.Sprintf("( SRF_TEST_AYUDANTE=%q SRF_TEST_AYUDANTE_SEG=3 %q > %q 2>/dev/null </dev/null & )\nexec sleep 30", sock, os.Args[0], anuncio))

	res, err := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 2, TestigoAlcance: testigoFalso(t, 0)})
	pid := anuncioDelAyudante(t, anuncio)
	if err == nil || res.OK {
		t.Fatalf("a session whose recorder leaves a helper on another display cannot be OK: %+v", res)
	}
	if res.DisplayHostTocado == nil || !*res.DisplayHostTocado {
		t.Fatalf("display_host_tocado must be true: %+v", res)
	}
	atribuida := false
	for _, c := range res.ConexionesForaneas {
		atribuida = atribuida || (c.PID == pid && c.Display == 78)
	}
	if !atribuida {
		t.Errorf("the re-parented helper %d must be named among the foreign connections: %v", pid, res.ConexionesForaneas)
	}
	esperarQueSeVaya(t, pid, 10*time.Second)
	sinHijos(t)
}

// What the witness leaves behind after it exits (an orphan the subreaper adopts) is still the
// witness's, not a recorder's: it shares the witness's process group and stays excluded.
func TestSesion_LoQueDejaElTestigoReparentadoNoCuentaComoRecolector(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "exec sleep 30")
	sock := servidorX(t, 78)
	anuncio := filepath.Join(t.TempDir(), "ayudante.txt")
	testigo := escribirScript(t, t.TempDir(), "testigo.sh",
		fmt.Sprintf("( SRF_TEST_AYUDANTE=%q SRF_TEST_AYUDANTE_SEG=6 %q > %q 2>/dev/null </dev/null & )\nexit 0\n", sock, os.Args[0], anuncio))

	res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigo})
	pid := anuncioDelAyudante(t, anuncio)

	// the test would prove nothing if the helper had not been adopted, outside our own group, and visible
	inf, ok := leerInfo(pid)
	if !ok || inf.PPid != os.Getpid() || inf.Pgrp == syscall.Getpgrp() {
		t.Fatalf("the helper must be an adopted orphan in the witness's group: %+v alive=%v", inf, ok)
	}
	obs, err := ObservarConexionesX(os.Getpid())
	if err != nil {
		t.Fatal(err)
	}
	visible := false
	for _, c := range conexionesA(obs, 78) {
		visible = visible || c.PID == pid
	}
	if !visible {
		t.Fatalf("the helper's connection is not visible to an observer that excludes nothing: %v", obs.Conexiones)
	}
	if len(res.ConexionesForaneas) != 0 {
		t.Errorf("what the witness left behind is not a recorder: %v", res.ConexionesForaneas)
	}
	esperarQueSeVaya(t, pid, 15*time.Second)
	sinHijos(t)
}

// The membership function on hand-built snapshots: the witness's subtree and process group are out
// (even when the leader is gone), an exclusion whose pid was reused is ignored, zombies are not listed.
func TestMiembros_ExcluyeElSubarbolYElGrupoDelTestigo(t *testing.T) {
	const yo, grab, testigo, hijo, huerfano, otro, zombi = 100, 200, 300, 301, 302, 400, 500
	snap := func() map[int]infoProc {
		return map[int]infoProc{
			yo:       {PPid: 1, Estado: 'S', Pgrp: yo, Inicio: 1},
			grab:     {PPid: yo, Estado: 'S', Pgrp: yo, Inicio: 2},
			testigo:  {PPid: yo, Estado: 'S', Pgrp: testigo, Inicio: 3},
			hijo:     {PPid: testigo, Estado: 'S', Pgrp: testigo, Inicio: 4},
			huerfano: {PPid: yo, Estado: 'S', Pgrp: testigo, Inicio: 5}, // the witness's orphan, adopted by the observer
			otro:     {PPid: grab, Estado: 'S', Pgrp: otro, Inicio: 6},
			zombi:    {PPid: yo, Estado: 'Z', Pgrp: yo, Inicio: 7},
			999:      {PPid: 1, Estado: 'S', Pgrp: 999, Inicio: 8}, // unrelated
		}
	}
	casos := []struct {
		nombre string
		snap   map[int]infoProc
		excl   []procID
		want   []int
	}{
		{"witness subtree, group and zombie out", snap(), []procID{{testigo, 3}}, []int{yo, grab, otro}},
		{"nothing excluded", snap(), nil, []int{yo, grab, testigo, hijo, huerfano, otro}},
	}
	sinLider := snap()
	delete(sinLider, testigo)
	delete(sinLider, hijo)
	casos = append(casos, struct {
		nombre string
		snap   map[int]infoProc
		excl   []procID
		want   []int
	}{"leader gone: its orphan stays out through the group", sinLider, []procID{{testigo, 3}}, []int{yo, grab, otro}})
	reusado := snap()
	reusado[testigo] = infoProc{PPid: yo, Estado: 'S', Pgrp: testigo, Inicio: 99} // the pid now belongs to another process
	casos = append(casos, struct {
		nombre string
		snap   map[int]infoProc
		excl   []procID
		want   []int
	}{"exclusion of a reused pid is ignored", reusado, []procID{{testigo, 3}}, []int{yo, grab, testigo, hijo, huerfano, otro}})

	for _, c := range casos {
		if got := miembros(c.snap, yo, c.excl); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: members %v, want %v", c.nombre, got, c.want)
		}
	}
}

// A subreaper must reap the orphans it adopted, and must never reap a child this package started:
// that child's own Wait has to see the exit status.
func TestRecogerAdoptados_ReapaLosHuerfanosYNoTocaALosHijosPropios(t *testing.T) {
	esZombi := func(pid int) (bool, string) {
		inf, ok := leerInfo(pid)
		return ok && inf.Estado == 'Z', fmt.Sprintf("pid %d: %+v alive=%v", pid, inf, ok)
	}
	// a child this package started, exited but not yet waited for
	propio := exec.Command("bash", "-c", "exit 7")
	if err := propio.Start(); err != nil {
		t.Fatal(err)
	}
	esperarHasta(t, 5*time.Second, func() (bool, string) { return esZombi(propio.Process.Pid) })
	hijosConocidos.Store(propio.Process.Pid, struct{}{})
	snap, err := instantanea()
	if err != nil {
		t.Fatal(err)
	}
	if n := recogerDe(snap); n != 0 {
		t.Errorf("reaped %d zombies, but the only one is a child this package started", n)
	}
	hijosConocidos.Delete(propio.Process.Pid)
	var ee *exec.ExitError
	if err := propio.Wait(); !errors.As(err, &ee) || ee.ExitCode() != 7 {
		t.Errorf("Wait must still see the child's own exit status (7): %v", err)
	}

	// an adopted orphan: a zombie child that nobody started through this package
	huerfano := exec.Command("bash", "-c", "exit 0")
	if err := huerfano.Start(); err != nil {
		t.Fatal(err)
	}
	esperarHasta(t, 5*time.Second, func() (bool, string) { return esZombi(huerfano.Process.Pid) })
	if n := recogerAdoptados(); n < 1 {
		t.Errorf("the adopted orphan %d was not reaped", huerfano.Process.Pid)
	}
	if _, ok := leerInfo(huerfano.Process.Pid); ok {
		t.Errorf("the reaped orphan %d is still in the process table", huerfano.Process.Pid)
	}
}

// The TCP consumer only counts connected sockets: a listening socket (state 0A) whose row shows a
// remote port inside 6000..6063 is not a client. Forged rows, because the kernel never writes that
// combination, prove the filter and not the kernel.
func TestObservador_UnSocketTCPEnEscuchaNoEsUnCliente(t *testing.T) {
	pf := nuevoProcFalso(t)
	const escucha, cliente = 9001001, 9001002
	pf.proceso(t, 555, 1, 555, 10, escucha, cliente)
	pf.tcp(t, [3]string{strconv.Itoa(escucha), "1771", "0A"}, [3]string{strconv.Itoa(cliente), "1772", "01"})
	obs, err := ObservarConexionesX(555)
	if err != nil {
		t.Fatal(err)
	}
	want := []ConexionX{{PID: 555, Familia: "tcp", Display: 2}}
	if !reflect.DeepEqual(obs.Conexiones, want) {
		t.Errorf("only the connected socket (port 6002) counts: got %v, want %v", obs.Conexiones, want)
	}
}

// A pid is attributed to the process it was when the process table was read. If the number changes
// hands before its descriptors are read (a different start time), the new process's sockets are not
// the old member's.
func TestObservador_NoAtribuyeUnPidReutilizado(t *testing.T) {
	ruta := servidorX(t, 79)
	cli, err := net.Dial("unix", ruta)
	if err != nil {
		t.Fatal(err)
	}
	defer cli.Close()
	inodo := inodoDeSocket(t, cli)
	pf := nuevoProcFalso(t)
	pf.proceso(t, 555, 1, 555, 100, inodo)

	obs, err := ObservarConexionesX(555)
	if err != nil {
		t.Fatal(err)
	}
	if want := []ConexionX{{PID: 555, Familia: "unix", Display: 79}}; !reflect.DeepEqual(conexionesA(obs, 79), want) {
		t.Fatalf("control: the member's connection must be attributed: %v", obs.Conexiones)
	}

	antes := trasLaInstantanea
	trasLaInstantanea = func() { pf.proceso(t, 555, 1, 555, 200, inodo) } // the pid now belongs to another process
	t.Cleanup(func() { trasLaInstantanea = antes })
	obs, err = ObservarConexionesX(555)
	if err != nil {
		t.Fatal(err)
	}
	if got := conexionesA(obs, 79); len(got) != 0 {
		t.Errorf("a reused pid must not inherit the old member's connections: %v", got)
	}
}

// The safety observer is sampled while recording, not only at four instants: with a stand-in
// recorder that stays alive, a session of 3 s must read the kernel at least 3000/250 times. The
// readings are counted here by a wrapper of the read itself, a source other than the session's own
// counter.
func TestSesion_LaObservacionSeMuestreaMientrasGraba(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "exec sleep 30")
	var lecturas atomic.Int64
	antes := lecturaX
	lecturaX = func(snap map[int]infoProc, raiz int, excl []procID) (ObservacionX, error) {
		lecturas.Add(1)
		return antes(snap, raiz, excl)
	}
	t.Cleanup(func() { lecturaX = antes })

	const segundos = 3
	res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: segundos, TestigoAlcance: testigoFalso(t, 0)})
	minimo := int64(segundos * 1000 / 250)
	if got := lecturas.Load(); got < minimo {
		t.Errorf("%d readings of the kernel in a %d s recording, at least %d (one per 250 ms) are required", got, segundos, minimo)
	}
	if int64(res.MuestrasX) != lecturas.Load() {
		t.Errorf("muestras_x=%d but the kernel was read %d times", res.MuestrasX, lecturas.Load())
	}
	if res.MuestrasGrabadorVivo < int(minimo) {
		t.Errorf("the stand-in recorder stayed alive the whole time: alive in %d samples, at least %d", res.MuestrasGrabadorVivo, minimo)
	}
}

// escuchandoEnPuerto reports whether some TCP socket of the host listens on the local port.
func escuchandoEnPuerto(t *testing.T, puerto int) bool {
	t.Helper()
	for _, f := range []string{"/proc/net/tcp", "/proc/net/tcp6"} {
		b, err := os.ReadFile(f)
		if err != nil {
			continue
		}
		for i, linea := range strings.Split(string(b), "\n") {
			campos := strings.Fields(linea)
			if i == 0 || len(campos) < 4 || campos[3] != "0A" {
				continue
			}
			if j := strings.LastIndex(campos[1], ":"); j >= 0 && strings.EqualFold(campos[1][j+1:], fmt.Sprintf("%04X", puerto)) {
				return true
			}
		}
	}
	return false
}

// The nested display must only be reachable through its unix socket (the guard refuses every
// other spelling because of it). Two sources: the command line the server was started with, and
// the kernel's table while it runs: none of its sockets listens on TCP, and nothing listens on 6150.
func TestXvfb_NoEscuchaPorTCP(t *testing.T) {
	var argumentos []string
	antes := crearComando
	crearComando = func(ctx context.Context, nombre string, args ...string) (*exec.Cmd, error) {
		if nombre == "Xvfb" {
			argumentos = append(argumentos, args...)
		}
		return antes(ctx, nombre, args...)
	}
	t.Cleanup(func() { crearComando = antes })
	srv := xvfbDePrueba(t)

	nolisten := false
	for i := 0; i+1 < len(argumentos); i++ {
		nolisten = nolisten || (argumentos[i] == "-nolisten" && argumentos[i+1] == "tcp")
	}
	if !nolisten {
		t.Errorf("Xvfb must be started with -nolisten tcp: %v", argumentos)
	}
	if escuchandoEnPuerto(t, tcpBaseX+150) {
		t.Errorf("something listens on TCP port %d while the nested display :150 runs", tcpBaseX+150)
	}
	inodos, err := leerSockets(srv.PID)
	if err != nil {
		t.Fatal(err)
	}
	tabla, err := leerTablasTCP()
	if err != nil {
		t.Fatal(err)
	}
	for _, ino := range inodos {
		if e, ok := tabla[ino]; ok {
			t.Errorf("Xvfb %d holds a TCP socket (inode %d, state %s): the nested display must not touch the network", srv.PID, ino, e.Estado)
		}
	}
}

// The dual oracle: the XID the server enumerates must equal the one the client allocated, and a
// disagreement fails closed. The control run right before shows the same run is OK when they agree.
func TestPruebaAnidada_UnXIDQueNoCoincideFallaCerrado(t *testing.T) {
	enRaizRepo(t)
	control, err := PruebaAnidada(context.Background(), ":150", 1, t.TempDir())
	if err != nil || control == nil || !control.OK || control.XIDCoincide == nil || !*control.XIDCoincide {
		t.Fatalf("control: with agreeing ids the run must be OK: err=%v res=%+v", err, control)
	}

	antes := elegirObjetivo
	elegirObjetivo = func(display string) (Candidato, error) {
		c, err := antes(display)
		c.XID++ // the server "says" another window
		return c, err
	}
	t.Cleanup(func() { elegirObjetivo = antes })
	res, err := PruebaAnidada(context.Background(), ":150", 1, t.TempDir())
	if err == nil || res == nil || res.OK {
		t.Fatalf("an XID mismatch must fail: err=%v res=%+v", err, res)
	}
	if res.XIDCoincide == nil || *res.XIDCoincide {
		t.Errorf("xid_coincide must be false: %v", res.XIDCoincide)
	}
	if !strings.Contains(err.Error(), "xid_no_coincide") || !strings.Contains(res.Error, "does not match") {
		t.Errorf("the failure must name the mismatch: err=%v error=%q", err, res.Error)
	}
	sinResiduosX(t, 150)
	sinHijos(t)
}

// An empty display (an unset shell variable) is refused like any spelling that is not provably a
// nested display: no default, nothing dialled or spawned, no session directory.
func TestPruebaAnidada_UnDisplayVacioSeRechazaSinLanzarNada(t *testing.T) {
	espias := instalarEspias(t)
	raiz := t.TempDir()
	res, err := PruebaAnidada(context.Background(), "", 1, raiz)
	if !errors.Is(err, ErrDisplayInvalido) && (err == nil || !strings.Contains(err.Error(), ErrDisplayInvalido.Error())) {
		t.Fatalf("an empty display must be refused as invalid: %v", err)
	}
	if res == nil || res.OK || res.Display != "" || res.DisplaySolicitado != "" || res.ManifiestoEstado != "ausente" {
		t.Errorf("the refusal report must exist, must not have been rewritten to :150 and must not be OK: %+v", res)
	}
	espias.nada(t)
	if ents, _ := os.ReadDir(raiz); len(ents) != 0 {
		t.Errorf("an empty display must not create session directories: %v", ents)
	}
}

// testigo_alcance says which witness ran: it is set only when the witness was started.
func TestSesion_TestigoAlcanceSoloSiElTestigoArranco(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "exec sleep 30")
	sesion := func(testigo string) *SesionResultado {
		res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigo})
		return res
	}
	if res := sesion(filepath.Join(t.TempDir(), "no-existe.sh")); res.TestigoAlcance != "" || claves(t, res)["testigo_alcance"] != "" ||
		res.DiffAlcanceLineas != nil || !strings.Contains(res.Error, "witness missing") {
		t.Errorf("a witness that never started must not be reported as having run: %+v", res)
	}
	ok := testigoFalso(t, 0)
	if res := sesion(ok); res.TestigoAlcance != ok || res.DiffAlcanceLineas == nil || *res.DiffAlcanceLineas != 0 {
		t.Errorf("a witness that ran is named and counted: %+v", res)
	}
	roto := escribirScript(t, t.TempDir(), "testigo.sh", "exit 7\n")
	if res := sesion(roto); res.TestigoAlcance != roto || res.DiffAlcanceLineas != nil {
		t.Errorf("a witness that started and failed is named and has no count: %+v", res)
	}
}

// Controls the report claims: the recorder gets exactly DISPLAY and PATH (nothing of the caller's
// environment: no XAUTHORITY, no secrets) and the canonical display, whatever spelling the caller
// used. A stand-in recorder writes what it received.
func TestSesion_ElGrabadorRecibeSoloDisplayYPathYElDisplayCanonico(t *testing.T) {
	sondaFalsa(t)
	t.Setenv("XAUTHORITY", "/no/existe/centinela")
	t.Setenv("SRF_CENTINELA", "secreto")
	dir := t.TempDir()
	entorno, argumentos := filepath.Join(dir, "entorno.txt"), filepath.Join(dir, "argumentos.txt")
	ffmpegFalso(t, fmt.Sprintf("env > %q\nprintf '%%s\\n' \"$@\" > %q\nexec sleep 30", entorno, argumentos))

	res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: "unix:150.0", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigoFalso(t, 0)})
	if res.Display != ":150" || res.DisplaySolicitado != "unix:150.0" {
		t.Fatalf("the canonical display is :150, asked as unix:150.0: %+v", res)
	}
	bruto, err := os.ReadFile(entorno)
	if err != nil {
		t.Fatalf("the stand-in recorder did not run: %v", err)
	}
	permitidas := map[string]bool{"DISPLAY": true, "PATH": true, "PWD": true, "SHLVL": true, "_": true} // PWD, SHLVL and _ are added by bash itself
	vars := map[string]string{}
	for _, l := range strings.Split(strings.TrimSpace(string(bruto)), "\n") {
		k, v, _ := strings.Cut(l, "=")
		vars[k] = v
		if !permitidas[k] {
			t.Errorf("the recorder inherited %q: it may only receive DISPLAY and PATH", k)
		}
	}
	if vars["DISPLAY"] != ":150" || vars["PATH"] == "" {
		t.Errorf("the recorder must get DISPLAY=:150 and PATH: %v", vars)
	}
	args, err := os.ReadFile(argumentos)
	if err != nil {
		t.Fatal(err)
	}
	lista := strings.Split(strings.TrimSpace(string(args)), "\n")
	entrada := ""
	for i := 0; i+1 < len(lista); i++ {
		if lista[i] == "-i" {
			entrada = lista[i+1]
		}
	}
	if entrada != ":150" {
		t.Errorf("ffmpeg -i must receive the canonical display :150, not the caller's spelling: %q (args %v)", entrada, lista)
	}
}

// The safety field is false only when the observer saw the recorder itself. The supervisor holds a
// connection to the nested display too (its test window), and that must not be enough.
func TestSesion_UnGrabadorQueNoSeVeDejaElCampoEnNull(t *testing.T) {
	sondaFalsa(t)
	xvfbDePrueba(t)
	win, err := CrearVentanaPrueba(":150", "ventana-del-supervisor", 300, 200)
	if err != nil {
		t.Fatal(err)
	}
	defer win.Cerrar()
	base := SesionOpciones{Display: ":150", Segundos: 1, XID: win.XID, TestigoAlcance: testigoFalso(t, 0)}

	casos := []struct {
		nombre, cuerpo, motivo string
	}{
		{"recorder that dies at once", "exit 1", "grabador_no_observado"},
		{"recorder alive that never connects to the display", "exec sleep 30", "observador_ciego"},
	}
	for _, c := range casos {
		ffmpegFalso(t, c.cuerpo)
		opts := base
		opts.Raiz = t.TempDir()
		res, err := EjecutarSesion(context.Background(), opts)
		if err == nil || res.OK {
			t.Errorf("%s: cannot be OK: %+v", c.nombre, res)
		}
		if res.DisplayHostTocado != nil || !strings.Contains(res.Error, c.motivo) {
			t.Errorf("%s: the supervisor's own connection to :150 must not turn 'unknown' into false; want null with %q, got %v / %q", c.nombre, c.motivo, res.DisplayHostTocado, res.Error)
		}
	}
}

// ---------------------------------------------------------------------------------------------
// The witness canary
// ---------------------------------------------------------------------------------------------

func TestDisplayDelTestigo(t *testing.T) {
	casos := []struct {
		env  []string
		want string
	}{
		{nil, ":1"},
		{[]string{"PATH=/bin", "SRF_DISPLAY=:150"}, ":150"},
		{[]string{"SRF_DISPLAY=:150", "SRF_DISPLAY=:160"}, ":160"},
		{[]string{"SRF_DISPLAY="}, ":1"},
	}
	for _, c := range casos {
		if got := displayDelTestigo(c.env); got != c.want {
			t.Errorf("displayDelTestigo(%v)=%q, want %q", c.env, got, c.want)
		}
	}
}

// The probe is the witness's own read (a root query). It succeeds on a live display and fails on
// one that does not answer; it changes nothing on the display.
func TestSondearDisplay_VeUnDisplayVivoYNoUnoMuerto(t *testing.T) {
	xvfbDePrueba(t)
	env := os.Environ()
	if err := sondearDisplay(context.Background(), ":150", env); err != nil {
		t.Errorf("a live display must answer the probe: %v", err)
	}
	if err := sondearDisplay(context.Background(), ":169", env); err == nil {
		t.Errorf("a display nobody serves must fail the probe")
	}
}

// A witness whose display cannot be read prints nothing and exits 0, which looks like a clean
// display. The session must say it was blind: not OK, count null, the reason named. The control
// right after, with a witness display that answers, is OK.
func TestSesion_UnTestigoCiegoNoAfirmaCero(t *testing.T) {
	xvfbDePrueba(t)
	sesion := func(displayDelTestigo string) (*SesionResultado, error) {
		return EjecutarSesion(context.Background(), SesionOpciones{
			Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigoFalso(t, 0),
			entornoTestigo: []string{"SRF_DISPLAY=" + displayDelTestigo},
		})
	}
	res, err := sesion(":169")
	if err == nil || res.OK {
		t.Fatalf("a blind witness cannot make the session OK: %+v", res)
	}
	if !res.TestigoCiego || res.DiffAlcanceLineas != nil || res.TestigoDisplay != ":169" || !strings.Contains(res.TestigoCiegoMotivo, ":169") {
		t.Errorf("the session must say the witness was blind and give no count: blind=%v lines=%v display=%q reason=%q", res.TestigoCiego, res.DiffAlcanceLineas, res.TestigoDisplay, res.TestigoCiegoMotivo)
	}
	if m := claves(t, res); m["diff_alcance_lineas"] != nil || m["testigo_ciego"] != true {
		t.Errorf("the report must carry diff_alcance_lineas=null and testigo_ciego=true: %v %v", m["diff_alcance_lineas"], m["testigo_ciego"])
	}

	control, err := sesion(":150")
	if err != nil || !control.OK || control.TestigoCiego || control.DiffAlcanceLineas == nil || *control.DiffAlcanceLineas != 0 {
		t.Fatalf("control: a witness that can read its display and prints nothing is OK: err=%v %+v", err, control)
	}
}

// The probe asks the display the witness is expected to read. A witness that reads another display
// (its script changed) and cannot open it says so on its stderr while exiting 0 with no output: that
// is blindness too, seen by a second source.
func TestSesion_UnTestigoQueDiceNoPoderAbrirSuDisplayEstaCiego(t *testing.T) {
	sondaFalsa(t)
	ffmpegFalso(t, "exec sleep 30")
	mudo := escribirScript(t, t.TempDir(), "testigo.sh", "echo 'xwininfo: error: unable to open display \":169\"' >&2\nexit 0\n")
	res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: mudo})
	if !res.TestigoCiego || res.DiffAlcanceLineas != nil || !strings.Contains(res.TestigoCiegoMotivo, "unable to open display") || res.TestigoAlcance != mudo {
		t.Errorf("a witness that could not open its display must be reported blind, named, with no count: %+v", res)
	}
	limpio := testigoFalso(t, 0)
	res, _ = EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: limpio})
	if res.TestigoCiego || res.DiffAlcanceLineas == nil || *res.DiffAlcanceLineas != 0 {
		t.Errorf("control: a silent witness with a quiet stderr is not blind: %+v", res)
	}
}

// The canary is taken before the witness starts and again after it ends: a display that answers at
// the start and no longer at the end leaves the count unknown.
func TestSesion_ElCanarioSeTomaAntesYDespuesDelTestigo(t *testing.T) {
	ffmpegFalso(t, "exec sleep 30")
	var llamadas atomic.Int64
	antes := sondaTestigo
	sondaTestigo = func(context.Context, string, []string) error {
		if llamadas.Add(1) == 1 {
			return nil // before: alive
		}
		return errors.New("unable to open display")
	}
	t.Cleanup(func() { sondaTestigo = antes })

	res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigoFalso(t, 0)})
	if llamadas.Load() != 2 {
		t.Errorf("the canary must run twice (before and after), it ran %d times", llamadas.Load())
	}
	if !res.TestigoCiego || res.DiffAlcanceLineas != nil || !strings.Contains(res.TestigoCiegoMotivo, "after the session") || res.TestigoAlcance == "" {
		t.Errorf("a display that died during the session leaves the count unknown, with the witness named: %+v", res)
	}

	// dead at the start: the witness is not even started, and is not named
	llamadas.Store(0)
	sondaTestigo = func(context.Context, string, []string) error {
		llamadas.Add(1)
		return errors.New("unable to open display")
	}
	res, _ = EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigoFalso(t, 0)})
	if llamadas.Load() != 1 || !res.TestigoCiego || !strings.Contains(res.TestigoCiegoMotivo, "before the session") || res.TestigoAlcance != "" {
		t.Errorf("a display that never answered: one probe, blind, witness not started nor named: probes=%d %+v", llamadas.Load(), res)
	}
}

// ---------------------------------------------------------------------------------------------
// The recorded video must play
// ---------------------------------------------------------------------------------------------

// mp4 makes a real video of one second with ffmpeg (a source of playable bytes that the code under
// test did not write).
func mp4Real(t *testing.T, dir string) string {
	t.Helper()
	ruta := filepath.Join(dir, "valido.mp4")
	if salida, err := exec.Command("ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
		"-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", ruta).CombinedOutput(); err != nil {
		t.Fatalf("cannot make a reference video: %v %s", err, salida)
	}
	return ruta
}

func TestVerificarVideo_ReproduciblesSiCorruptosNo(t *testing.T) {
	dir := t.TempDir()
	valido := mp4Real(t, dir)
	datos, err := os.ReadFile(valido)
	if err != nil {
		t.Fatal(err)
	}
	truncado := filepath.Join(dir, "truncado.mp4") // what a SIGKILLed ffmpeg leaves: the index is written at the end
	texto := filepath.Join(dir, "texto.mp4")
	vacio := filepath.Join(dir, "vacio.mp4")
	sinVideo := filepath.Join(dir, "solo-audio.mp4")
	for ruta, contenido := range map[string][]byte{truncado: datos[:len(datos)/2], texto: []byte("fake-mp4\n"), vacio: nil} {
		if err := os.WriteFile(ruta, contenido, 0644); err != nil {
			t.Fatal(err)
		}
	}
	if salida, err := exec.Command("ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1",
		"-y", sinVideo).CombinedOutput(); err != nil {
		t.Fatalf("cannot make an audio-only reference: %v %s", err, salida)
	}

	v, err := verificarVideo(context.Background(), valido)
	fotogramas, duracion, errP := ffprobeIndependiente(valido)
	if err != nil || errP != nil {
		t.Fatalf("a real mp4 must verify: %v (ffprobe: %v)", err, errP)
	}
	if v.Fotogramas != fotogramas || v.Fotogramas < 10 || v.DuracionS <= 0 || v.DuracionS < duracion-0.1 || v.DuracionS > duracion+0.1 {
		t.Errorf("decoded frames=%d duration=%v, ffprobe frames=%d duration=%v", v.Fotogramas, v.DuracionS, fotogramas, duracion)
	}
	for _, ruta := range []string{truncado, texto, vacio, sinVideo} {
		if _, err := verificarVideo(context.Background(), ruta); err == nil {
			t.Errorf("%s must not verify", filepath.Base(ruta))
		}
		if _, _, err := ffprobeIndependiente(ruta); err == nil {
			t.Errorf("ffprobe accepts %s, so it is not a valid negative control", filepath.Base(ruta))
		}
	}
}

// "completa" needs a playable video, not just bytes: the same session is completa with a valid mp4
// and incompleta, with the reason, with a truncated one.
func TestSesion_ElVideoNoReproducibleNoEsCompleto(t *testing.T) {
	sondaFalsa(t)
	dir := t.TempDir()
	valido := mp4Real(t, dir)
	datos, err := os.ReadFile(valido)
	if err != nil {
		t.Fatal(err)
	}
	truncado := filepath.Join(dir, "truncado.mp4")
	if err := os.WriteFile(truncado, datos[:len(datos)/2], 0644); err != nil {
		t.Fatal(err)
	}
	sesion := func(video string) *SesionResultado {
		ffmpegFalso(t, fmt.Sprintf("cp %q \"$ultimo\"\nexec sleep 30", video))
		res, _ := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigoFalso(t, 0)})
		return res
	}

	res := sesion(valido)
	if res.ManifiestoEstado != "completa" || res.VideoValidacion != "ok" || res.VideoFotogramas < 10 {
		t.Errorf("a playable video makes the manifest completa: %q %q frames=%d", res.ManifiestoEstado, res.VideoValidacion, res.VideoFotogramas)
	}
	res = sesion(truncado)
	if res.ManifiestoEstado != "incompleta" || res.VideoBytes == 0 || res.VideoValidacion == "ok" {
		t.Errorf("a truncated video must not be completa although it has bytes: %q bytes=%d validation=%q", res.ManifiestoEstado, res.VideoBytes, res.VideoValidacion)
	}
	m, errM := LeerManifiesto(filepath.Join(res.Dir, FicheroManifiesto))
	if errM != nil || m.Estado != "incompleta" || !strings.Contains(m.RazonIncompleta, "video not playable") {
		t.Errorf("the manifest on disk must say why: %+v %v", m, errM)
	}
	if !strings.Contains(res.Error, "video is not playable") {
		t.Errorf("the error must name the video: %q", res.Error)
	}
}

// ---------------------------------------------------------------------------------------------
// Measured detection curve (run with SRF_CURVA=1; it takes about a minute)
// ---------------------------------------------------------------------------------------------

// A poller misses a connection shorter than its period. This measures how much, with the real
// observer read on the real 100 ms cadence of a session: a client of a stand-in display is held for
// T ms, 40 trials per T, each starting at a different phase of the sampler's period.
func TestCurvaDeDeteccion_Medida(t *testing.T) {
	if os.Getenv("SRF_CURVA") == "" {
		t.Skip("measurement, not a check: run with SRF_CURVA=1")
	}
	const periodo, ensayos = 100 * time.Millisecond, 40
	ruta := servidorX(t, 79)

	var mu sync.Mutex
	visto := false
	var lecturas int
	var total time.Duration
	parar := make(chan struct{})
	terminado := make(chan struct{})
	base := time.Now()
	go func() {
		defer close(terminado)
		tk := time.NewTicker(periodo)
		defer tk.Stop()
		for {
			select {
			case <-parar:
				return
			case <-tk.C:
				mu.Lock()
				inicio := time.Now()
				if obs, err := ObservarConexionesX(os.Getpid()); err == nil && len(conexionesA(obs, 79)) > 0 {
					visto = true
				}
				lecturas++
				total += time.Since(inicio)
				mu.Unlock()
			}
		}
	}()
	t.Cleanup(func() { close(parar); <-terminado })

	for _, ms := range []int{10, 30, 60, 100, 150, 250} {
		vistos := 0
		for i := 0; i < ensayos; i++ {
			fase := time.Duration(i) * periodo / ensayos
			// start at the phase `fase` after a tick of the sampler (ticks fall on base + k*periodo)
			k := time.Since(base)/periodo + 2
			time.Sleep(time.Until(base.Add(k*periodo + fase)))
			mu.Lock()
			visto = false
			mu.Unlock()
			cli, err := net.Dial("unix", ruta)
			if err != nil {
				t.Fatal(err)
			}
			time.Sleep(time.Duration(ms) * time.Millisecond)
			cli.Close()
			time.Sleep(periodo + 50*time.Millisecond) // let a reading that was in progress finish
			mu.Lock()
			if visto {
				vistos++
			}
			mu.Unlock()
		}
		t.Logf("CURVA connection_ms=%d seen=%d/%d", ms, vistos, ensayos)
	}
	mu.Lock()
	t.Logf("CURVA reading_cost_ms_mean=%.2f readings=%d", float64(total.Microseconds())/1000/float64(lecturas), lecturas)
	mu.Unlock()
}

// verificarVideo asks for frames and a duration, not only for ffmpeg's rc 0: stand-ins that exit 0
// but report no frame, or no duration, must be refused. The stand-in sits first in PATH.
func TestVerificarVideo_ExigeFotogramasYDuracion(t *testing.T) {
	casos := []struct {
		nombre, stderr, stdout string
		ok                     bool
	}{
		{"frames and duration", "  Duration: 00:00:01.50, start: 0.0", "frame=12\nprogress=end", true},
		{"no frame", "  Duration: 00:00:01.50, start: 0.0", "frame=0\nprogress=end", false},
		{"no duration", "  Duration: N/A, bitrate: N/A", "frame=12\nprogress=end", false},
		{"zero duration", "  Duration: 00:00:00.00, start: 0.0", "frame=12\nprogress=end", false},
	}
	for _, c := range casos {
		dir := t.TempDir()
		escribirScript(t, dir, "ffmpeg", fmt.Sprintf("printf '%%b\\n' %q >&2\nprintf '%%b\\n' %q\nexit 0\n", c.stderr, c.stdout))
		t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
		v, err := verificarVideo(context.Background(), "irrelevante.mp4")
		if (err == nil) != c.ok {
			t.Errorf("%s: ok=%v, got err=%v (%+v)", c.nombre, c.ok, err, v)
		}
	}
}

// The safety field is only false when the observer can be sure of the membership of the session
// tree. If the process could not become a child subreaper, an orphaned descendant would be
// invisible: the field is unknown then, whatever the observer saw. A real recorder and a real
// display, so that everything else would read false.
func TestSesion_SinSubreaperElCampoEsDesconocido(t *testing.T) {
	xvfbDePrueba(t)
	sondaFalsa(t)
	_ = activarSubreaper()
	antes := subreaperErr
	subreaperErr = errors.New("prctl: operation not permitted")
	t.Cleanup(func() { subreaperErr = antes })

	res, err := EjecutarSesion(context.Background(), SesionOpciones{Display: ":150", Raiz: t.TempDir(), Segundos: 1, TestigoAlcance: testigoFalso(t, 0)})
	if err == nil || res.OK {
		t.Fatalf("membership that is not guaranteed cannot be OK: %+v", res)
	}
	if res.DisplayHostTocado != nil || !strings.Contains(res.Error, "membresia_no_garantizada") {
		t.Errorf("without the subreaper display_host_tocado is unknown (null), never false: %v %q", res.DisplayHostTocado, res.Error)
	}
	if res.MuestrasGrabadorVivo < minMuestrasGrabador || res.MuestrasGrabadorConectado < 1 {
		t.Errorf("control: the recorder was watched and seen, so only the membership is missing: %+v", res)
	}
}

// The rule of the safety field, pinned: at least three samples with the recorder alive.
func TestMinimoDeMuestrasDelGrabador(t *testing.T) {
	if minMuestrasGrabador != 3 {
		t.Errorf("false requires the recorder alive in at least 3 samples, the constant is %d", minMuestrasGrabador)
	}
}
