//go:build cockpit

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

// shellDePrueba is an in-memory shell: it echoes what it is written, records resizes and exits when closed.
type shellDePrueba struct {
	mu       sync.Mutex
	entrada  *io.PipeWriter
	salida   *io.PipeReader
	resizes  [][2]uint16
	fin      chan struct{}
	finOnce  sync.Once
	derribos int
}

type extremoPrueba struct {
	r *io.PipeReader
	w *io.PipeWriter
}

func (e extremoPrueba) Read(p []byte) (int, error)  { return e.r.Read(p) }
func (e extremoPrueba) Write(p []byte) (int, error) { return e.w.Write(p) }
func (e extremoPrueba) Close() error                { _ = e.r.Close(); return e.w.Close() }

func lanzadorDePrueba(t *testing.T) (lanzadorDeShell, *shellDePrueba) {
	t.Helper()
	sh := &shellDePrueba{fin: make(chan struct{})}
	return func(id string) (*terminalPty, error) {
		// what the broker writes to the master comes back to it (an echo shell)
		r, w := io.Pipe()
		sh.mu.Lock()
		sh.entrada, sh.salida = w, r
		sh.mu.Unlock()
		return &terminalPty{
			Master: extremoPrueba{r: r, w: w},
			Redimensionar: func(cols, rows uint16) error {
				sh.mu.Lock()
				defer sh.mu.Unlock()
				sh.resizes = append(sh.resizes, [2]uint16{cols, rows})
				return nil
			},
			Esperar: func() int { <-sh.fin; return 7 },
			Derribar: func() error {
				sh.mu.Lock()
				sh.derribos++
				sh.mu.Unlock()
				sh.finOnce.Do(func() { close(sh.fin) })
				_ = r.Close()
				return nil
			},
		}, nil
	}, sh
}

func TestSRF2CodecRoundTrip(t *testing.T) {
	cuerpo := []byte("hola PTY\r\n")
	trama := codificarTrama(cuerpo, 41, 1234)
	if len(trama) != srf2Cabecera+len(cuerpo) {
		t.Fatalf("frame length %d", len(trama))
	}
	if string(trama[0:4]) != "SRF2" {
		t.Fatalf("magic %q", trama[0:4])
	}
	d, err := decodificarTrama(trama)
	if err != nil {
		t.Fatal(err)
	}
	if d.Seq != 41 || d.Ms != 1234 || d.Stride != 1 || d.Cuenta != uint32(len(cuerpo)) || !bytes.Equal(d.Cuerpo, cuerpo) {
		t.Fatalf("decoded %+v", d)
	}
	for _, mal := range [][]byte{trama[:10], append([]byte("XXXX"), trama[4:]...), func() []byte { c := append([]byte(nil), trama...); c[25] = 1; return c }(), trama[:len(trama)-1]} {
		if _, err := decodificarTrama(mal); err == nil {
			t.Fatalf("a bad frame decoded: %v", mal[:8])
		}
	}
}

func TestPtyServerUrlAndTokenGate(t *testing.T) {
	lanzar, _ := lanzadorDePrueba(t)
	s, err := nuevoPtyServer(lanzar)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Cerrar() }()
	url, err := s.URL("agente-1")
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(url, "ws://127.0.0.1:") || !strings.Contains(url, "/pty/agente-1?t=") {
		t.Fatalf("url %q", url)
	}
	for _, malo := range []string{"", "../x", "a b", strings.Repeat("a", 65), "-x"} {
		if _, err := s.URL(malo); err == nil {
			t.Fatalf("id %q accepted", malo)
		}
	}
	sinToken := "http://127.0.0.1:" + strings.TrimPrefix(strings.Split(url, "/pty/")[0], "ws://127.0.0.1:") + "/pty/agente-1?t=nope"
	resp, err := http.Get(sinToken)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusForbidden {
		t.Fatalf("wrong token: status %d, want 403", resp.StatusCode)
	}
}

func TestPtyServerStreamsSRF2BothWaysAndReportsExit(t *testing.T) {
	lanzar, sh := lanzadorDePrueba(t)
	s, err := nuevoPtyServer(lanzar)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Cerrar() }()
	url, _ := s.URL("eco")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow()

	if err := c.Write(ctx, websocket.MessageText, []byte(`{"type":"RESIZE","cols":120,"rows":40}`)); err != nil {
		t.Fatal(err)
	}
	if err := c.Write(ctx, websocket.MessageBinary, codificarTrama([]byte("ping"), 0, 5)); err != nil {
		t.Fatal(err)
	}
	tipo, datos, err := c.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if tipo != websocket.MessageBinary {
		t.Fatalf("first message type %v", tipo)
	}
	d, err := decodificarTrama(datos)
	if err != nil {
		t.Fatal(err)
	}
	if string(d.Cuerpo) != "ping" || d.Seq != 0 {
		t.Fatalf("echo %q seq %d", d.Cuerpo, d.Seq)
	}
	// a frame that breaks the contract is dropped, the stream goes on
	if err := c.Write(ctx, websocket.MessageBinary, []byte("garbage")); err != nil {
		t.Fatal(err)
	}
	if err := c.Write(ctx, websocket.MessageBinary, codificarTrama([]byte("pong"), 1, 6)); err != nil {
		t.Fatal(err)
	}
	_, datos, err = c.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if d, _ = decodificarTrama(datos); string(d.Cuerpo) != "pong" || d.Seq != 1 {
		t.Fatalf("second echo %q seq %d", d.Cuerpo, d.Seq)
	}
	sh.mu.Lock()
	resizes := append([][2]uint16(nil), sh.resizes...)
	sh.mu.Unlock()
	if len(resizes) != 1 || resizes[0] != [2]uint16{120, 40} {
		t.Fatalf("resizes %v", resizes)
	}

	// the shell ends: EXIT {code} then a normal close, and the shell is torn down exactly once
	sh.finOnce.Do(func() { close(sh.fin) })
	tipo, datos, err = c.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if tipo != websocket.MessageText {
		t.Fatalf("exit message type %v", tipo)
	}
	var salida struct {
		Type string `json:"type"`
		Code int    `json:"code"`
	}
	if err := json.Unmarshal(datos, &salida); err != nil || salida.Type != "EXIT" || salida.Code != 7 {
		t.Fatalf("exit frame %s (%v)", datos, err)
	}
	if _, _, err := c.Read(ctx); err == nil {
		t.Fatal("the stream stayed open after EXIT")
	} else if websocket.CloseStatus(err) != websocket.StatusNormalClosure {
		t.Fatalf("close status %v", websocket.CloseStatus(err))
	}
	plazo := time.Now().Add(3 * time.Second)
	for {
		sh.mu.Lock()
		n := sh.derribos
		sh.mu.Unlock()
		if n >= 1 || time.Now().After(plazo) {
			if n != 1 {
				t.Fatalf("derribos = %d, want 1", n)
			}
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func TestCompuertaPausaYReanuda(t *testing.T) {
	g := nuevaCompuerta()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := g.Esperar(ctx); err != nil {
		t.Fatal(err)
	}
	g.Pausar()
	corto, cancelCorto := context.WithTimeout(ctx, 50*time.Millisecond)
	defer cancelCorto()
	if err := g.Esperar(corto); err == nil {
		t.Fatal("a paused gate let the reader through")
	}
	g.Reanudar()
	if err := g.Esperar(ctx); err != nil {
		t.Fatal(err)
	}
}
