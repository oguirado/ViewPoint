//go:build cockpit

package main

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"

	"opensuperficies.local/srf/src/kernelbook/cgroup"
	"opensuperficies.local/srf/src/kernelbook/reaper"
)

// The production launcher against a real sibling cgroup and PTY: skipped, not faked, where the host denies cgroup
// delegation (a bench without a delegated slice). Where it runs, an interactive bash answers on the SRF2 stream and
// the ladder tears it down.
func TestLanzadorEnjauladoEcoDeBashReal(t *testing.T) {
	if _, err := cgroup.FindSiblingParent(); err != nil {
		t.Skipf("no sibling cgroup parent here: %v", err)
	}
	sonda, err := cgroup.CreateSibling("srf-term-sonda-cockpit")
	if err != nil {
		t.Skipf("cgroup delegation denied here: %v", err)
	}
	_ = sonda.Destroy()
	rp, err := reaper.NewProcessReaper()
	if err != nil {
		t.Skipf("subreaper: %v", err)
	}
	s, err := nuevoPtyServer(lanzarShellEnjaulado(rp))
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = s.Cerrar() }()
	url, _ := s.URL("prueba-real")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	c, _, err := websocket.Dial(ctx, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseNow()
	if err := c.Write(ctx, websocket.MessageBinary, codificarTrama([]byte("echo SRF2-OK-$((40+2))\r"), 0, 0)); err != nil {
		t.Fatal(err)
	}
	var visto strings.Builder
	for time.Now().Before(time.Now().Add(15 * time.Second)) {
		tipo, datos, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("stream ended before the echo: %v (seen %q)", err, visto.String())
		}
		if tipo != websocket.MessageBinary {
			continue
		}
		d, err := decodificarTrama(datos)
		if err != nil {
			t.Fatal(err)
		}
		visto.Write(d.Cuerpo)
		if strings.Contains(visto.String(), "SRF2-OK-42") {
			return
		}
	}
	t.Fatalf("no echo within the deadline: %q", visto.String())
}
