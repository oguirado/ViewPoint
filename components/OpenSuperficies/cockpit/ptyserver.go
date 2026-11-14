//go:build cockpit

package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/coder/websocket"

	"opensuperficies.local/srf/src/kernelbook/cgroup"
	"opensuperficies.local/srf/src/kernelbook/escalera"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
	"opensuperficies.local/srf/src/kernelbook/pty"
	"opensuperficies.local/srf/src/kernelbook/reaper"
)

// The PTY WebSocket broker of the cockpit (VIEW-03, wsstream.ts): ws://127.0.0.1:<port>/pty/<agent>?t=<token>.
// Loopback only, ephemeral port, one random token per cockpit process (any local process could otherwise open a
// shell through the cockpit). One connection spawns one shell in its own sibling cgroup (srf-term-<agent>) on a
// PTY of the kernelbook, and the connection's end tears the shell down through the 7-rung ladder
// (escalera.Sesion.Derribar). PTY bytes travel as SRF2 binary frames both ways; JSON text frames carry RESIZE,
// PAUSE_FLOW and RESUME_FLOW from the client and EXIT {code} to it.

var reAgente = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)

// terminalPty is one shell as the broker drives it; production builds it on the kernelbook, tests on a pipe.
type terminalPty struct {
	Master        io.ReadWriteCloser
	Redimensionar func(cols, rows uint16) error
	// Esperar blocks until the shell exits and returns its exit code (-1 when unknown).
	Esperar  func() int
	Derribar func() error
}

type lanzadorDeShell func(id string) (*terminalPty, error)

type PtyServer struct {
	l        net.Listener
	srv      *http.Server
	token    string
	lanzar   lanzadorDeShell
	mu       sync.Mutex
	sesiones map[*terminalPty]struct{}
	cerrado  bool
}

func nuevoPtyServer(lanzar lanzadorDeShell) (*PtyServer, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, fmt.Errorf("pty server: %w", err)
	}
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		_ = l.Close()
		return nil, fmt.Errorf("pty server token: %w", err)
	}
	s := &PtyServer{l: l, token: hex.EncodeToString(b[:]), lanzar: lanzar, sesiones: map[*terminalPty]struct{}{}}
	s.srv = &http.Server{Handler: s, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = s.srv.Serve(l) }()
	return s, nil
}

// Puerto is the ephemeral loopback port.
func (s *PtyServer) Puerto() int { return s.l.Addr().(*net.TCPAddr).Port }

// URL is the stream URL of one agent; the id is validated, never interpolated raw.
func (s *PtyServer) URL(agente string) (string, error) {
	if !reAgente.MatchString(agente) {
		return "", fmt.Errorf("agent id %q: letters, digits, '_', '.' and '-' only, 1..64 characters", agente)
	}
	return fmt.Sprintf("ws://127.0.0.1:%d/pty/%s?t=%s", s.Puerto(), agente, s.token), nil
}

// Cerrar stops accepting and tears every shell down.
func (s *PtyServer) Cerrar() error {
	s.mu.Lock()
	s.cerrado = true
	vivas := make([]*terminalPty, 0, len(s.sesiones))
	for t := range s.sesiones {
		vivas = append(vivas, t)
	}
	s.mu.Unlock()
	for _, t := range vivas {
		_ = t.Derribar()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return s.srv.Shutdown(ctx)
}

func (s *PtyServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !strings.HasPrefix(r.URL.Path, "/pty/") {
		http.NotFound(w, r)
		return
	}
	agente := strings.TrimPrefix(r.URL.Path, "/pty/")
	if !reAgente.MatchString(agente) {
		http.Error(w, "bad agent id", http.StatusBadRequest)
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.URL.Query().Get("t")), []byte(s.token)) != 1 {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	s.mu.Lock()
	cerrado := s.cerrado
	s.mu.Unlock()
	if cerrado {
		http.Error(w, "shutting down", http.StatusServiceUnavailable)
		return
	}
	// The webview's origin (wails://, http://wails.localhost) is not this loopback host: the token is the gate.
	c, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true, CompressionMode: websocket.CompressionDisabled})
	if err != nil {
		return
	}
	s.atender(r.Context(), c, agente)
}

// compuerta is the flow-control gate: PAUSE_FLOW holds the PTY reader, RESUME_FLOW releases it.
type compuerta struct {
	mu      sync.Mutex
	abierta chan struct{}
}

func nuevaCompuerta() *compuerta {
	c := &compuerta{abierta: make(chan struct{})}
	close(c.abierta)
	return c
}

func (c *compuerta) Pausar() {
	c.mu.Lock()
	defer c.mu.Unlock()
	select {
	case <-c.abierta:
		c.abierta = make(chan struct{})
	default:
	}
}

func (c *compuerta) Reanudar() {
	c.mu.Lock()
	defer c.mu.Unlock()
	select {
	case <-c.abierta:
	default:
		close(c.abierta)
	}
}

func (c *compuerta) Esperar(ctx context.Context) error {
	c.mu.Lock()
	ch := c.abierta
	c.mu.Unlock()
	select {
	case <-ch:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

type controlCliente struct {
	Type string `json:"type"`
	Cols uint16 `json:"cols"`
	Rows uint16 `json:"rows"`
}

func (s *PtyServer) atender(ctx context.Context, c *websocket.Conn, agente string) {
	t, err := s.lanzar(agente)
	if err != nil {
		_ = c.Close(websocket.StatusInternalError, ultimos(err.Error(), 120))
		return
	}
	s.mu.Lock()
	s.sesiones[t] = struct{}{}
	s.mu.Unlock()
	defer func() {
		_ = t.Derribar()
		s.mu.Lock()
		delete(s.sesiones, t)
		s.mu.Unlock()
	}()

	ctx, cancelar := context.WithCancel(ctx)
	defer cancelar()
	c.SetReadLimit(1 << 20)
	inicio := time.Now()
	flujo := nuevaCompuerta()
	var escritura sync.Mutex
	enviar := func(tipo websocket.MessageType, datos []byte) error {
		escritura.Lock()
		defer escritura.Unlock()
		wctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		return c.Write(wctx, tipo, datos)
	}

	// PTY -> client: SRF2 frames, sequence and milliseconds since this connection opened.
	go func() {
		buf := make([]byte, 32*1024)
		var seq uint32
		for {
			if err := flujo.Esperar(ctx); err != nil {
				return
			}
			n, err := t.Master.Read(buf)
			if n > 0 {
				trama := codificarTrama(buf[:n], seq, uint32(time.Since(inicio).Milliseconds()))
				seq++
				if err := enviar(websocket.MessageBinary, trama); err != nil {
					cancelar()
					return
				}
			}
			if err != nil {
				return
			}
		}
	}()

	// shell exit -> EXIT {code} and a normal close
	go func() {
		codigo := t.Esperar()
		msg, _ := json.Marshal(map[string]any{"type": "EXIT", "code": codigo})
		_ = enviar(websocket.MessageText, msg)
		_ = c.Close(websocket.StatusNormalClosure, "exit")
		cancelar()
	}()

	// client -> PTY
	for {
		tipo, datos, err := c.Read(ctx)
		if err != nil {
			return
		}
		switch tipo {
		case websocket.MessageBinary:
			trama, err := decodificarTrama(datos)
			if err != nil {
				continue // a frame that does not hold the contract is dropped, never guessed at
			}
			if len(trama.Cuerpo) > 0 {
				if _, err := t.Master.Write(trama.Cuerpo); err != nil {
					return
				}
			}
		case websocket.MessageText:
			var ctl controlCliente
			if err := json.Unmarshal(datos, &ctl); err != nil {
				continue
			}
			switch ctl.Type {
			case "RESIZE":
				if ctl.Cols > 0 && ctl.Rows > 0 {
					_ = t.Redimensionar(ctl.Cols, ctl.Rows)
				}
			case "PAUSE_FLOW":
				flujo.Pausar()
			case "RESUME_FLOW":
				flujo.Reanudar()
			}
		}
	}
}

func ultimos(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

// lanzarShellEnjaulado is the production launcher: an interactive bash on a kernelbook PTY, born inside the sibling
// cgroup srf-term-<agent> (CLONE_INTO_CGROUP, else migrated), torn down by escalera.Sesion.Derribar. A cgroup that
// cannot be created is an error: the shell never runs uncontained.
func lanzarShellEnjaulado(rp *reaper.ProcessReaper) lanzadorDeShell {
	return func(id string) (*terminalPty, error) {
		nombre := "srf-term-" + id
		cg, err := cgroup.CreateSibling(nombre)
		if err != nil {
			return nil, fmt.Errorf("pty %s: %w", id, err)
		}
		p, err := pty.Open()
		if err != nil {
			_ = cg.Destroy()
			return nil, fmt.Errorf("pty %s: %w", id, err)
		}
		entorno := append(os.Environ(), "TERM=xterm-256color", "SRF_AGENTE="+id)
		crear := func(enCgroup bool) (*os.Process, *escalera.Sesion, error) {
			cmd, err := lanzador.Command("bash", "-i")
			if err != nil {
				return nil, nil, err
			}
			cmd.Env = entorno
			cmd.Dir = os.Getenv("HOME")
			if enCgroup {
				cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: cg.Fd()}
			}
			if err := p.StartCmd(cmd); err != nil { // Setsid + Setctty: the shell leads its own session and process group
				return nil, nil, err
			}
			return cmd.Process, &escalera.Sesion{ID: nombre, Cgroup: cg, Cmd: cmd, PTY: p, Reaper: rp, PGID: cmd.Process.Pid}, nil
		}
		proc, ses, err := crear(true)
		if err != nil {
			// clone3 without CLONE_INTO_CGROUP support: coordinated migration after birth
			proc, ses, err = crear(false)
			if err != nil {
				_ = p.Close()
				_ = cg.Destroy()
				return nil, fmt.Errorf("pty %s: start bash: %w", id, err)
			}
			if err := cg.AddProc(proc.Pid); err != nil {
				_ = proc.Kill()
				_, _ = proc.Wait()
				_ = p.Close()
				_ = cg.Destroy()
				return nil, fmt.Errorf("pty %s: cgroup migration: %w", id, err)
			}
		}
		rp.RegisterDirectChild(proc.Pid)
		escalera.RegisterTrackedPID(proc.Pid, nombre)
		salida := make(chan int, 1)
		go func() {
			err := ses.Cmd.Wait()
			codigo := 0
			var ee *exec.ExitError
			if errors.As(err, &ee) {
				codigo = ee.ExitCode()
			} else if err != nil {
				codigo = -1
			}
			salida <- codigo
		}()
		return &terminalPty{
			Master:        p.Master,
			Redimensionar: p.Resize,
			Esperar:       func() int { return <-salida },
			Derribar:      ses.Derribar,
		}, nil
	}
}
