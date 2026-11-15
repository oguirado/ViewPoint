package lanzador

import (
	"context"
	"errors"
	"testing"
)

// TestLanzadorRechazaDocker verifies Gate B-10: docker CLI is strictly rejected.
func TestLanzadorRechazaDocker(t *testing.T) {
	cases := []struct {
		name string
		args []string
	}{
		{"docker", []string{"ps"}},
		{"/usr/bin/docker", []string{"run", "alpine"}},
		{"/usr/local/bin/docker", []string{"version"}},
		{"docker-compose", []string{"up"}},
	}

	for _, tc := range cases {
		_, err := Command(tc.name, tc.args...)
		if err == nil {
			t.Fatalf("lanzador permitio indebidamente docker: %s %v", tc.name, tc.args)
		}
		if !errors.Is(err, ErrComandoProhibido) {
			t.Fatalf("se esperaba ErrComandoProhibido para %s, obtenido: %v", tc.name, err)
		}
	}
}

// TestLanzadorListaBlanca verifies that allowlisted commands are accepted.
func TestLanzadorListaBlanca(t *testing.T) {
	allowlist := []struct {
		name string
		args []string
	}{
		{"qemu-system-aarch64", []string{"-m", "1024"}},
		{"/usr/bin/qemu-system-aarch64", []string{"--version"}},
		{"bash", []string{"-c", "echo ok"}},
		{"/bin/bash", []string{"-i"}},
		{"python3", []string{"/home/user/Documentos/OpenFresnel/companions/grabador/cli.py", "--puerto", "9000"}},
		{"/usr/bin/python3", []string{"/home/user/Documentos/OpenFresnel/companions/grabador/cli.py"}},
		{"/home/user/Documentos/OpenSwitchboard/bin/sbd", []string{"--daemon"}},
		{"ffmpeg", []string{"-version"}},
		{"xwininfo", []string{"-root"}},
		{"Xvfb", []string{":99"}},
		{"bin/srf-capsula-browser", []string{"--slot", "1"}},
		{"./bin/srf-capsula-vm", []string{"--slot", "2"}},
		{"/opt/bin/srf-capsula-deck", []string{}},
	}

	for _, tc := range allowlist {
		cmd, err := Command(tc.name, tc.args...)
		if err != nil {
			t.Fatalf("comando permitido fue rechazado: %s %v: %v", tc.name, tc.args, err)
		}
		if cmd == nil {
			t.Fatalf("comando nulo retornado para: %s %v", tc.name, tc.args)
		}
	}
}

// TestLanzadorRechazaNoAutorizados verifies that commands outside the allowlist are rejected.
func TestLanzadorRechazaNoAutorizados(t *testing.T) {
	prohibidos := []struct {
		name string
		args []string
	}{
		{"sh", []string{"-c", "echo ok"}},
		{"zsh", []string{}},
		{"python3", []string{"malicious.py"}},
		{"python3", []string{"-c", "import os"}},
		{"curl", []string{"http://localhost"}},
		{"wget", []string{"http://localhost"}},
		{"nc", []string{"-l", "1234"}},
		{"systemctl", []string{"status"}},
		{"sbd", []string{"--daemon"}},
	}

	for _, tc := range prohibidos {
		_, err := Command(tc.name, tc.args...)
		if err == nil {
			t.Fatalf("comando no autorizado fue aceptado: %s %v", tc.name, tc.args)
		}
		if !errors.Is(err, ErrComandoProhibido) {
			t.Fatalf("se esperaba ErrComandoProhibido para %s, obtenido: %v", tc.name, err)
		}
	}
}

// TestLanzadorRechazaBareSbd verifies Finding H5: bare 'sbd' without absolute path is rejected.
func TestLanzadorRechazaBareSbd(t *testing.T) {
	_, err := Command("sbd", "--daemon")
	if err == nil {
		t.Fatalf("lanzador permitio indebidamente comando sbd sin ruta absoluta")
	}
	if !errors.Is(err, ErrComandoProhibido) {
		t.Fatalf("se esperaba ErrComandoProhibido para 'sbd', obtenido: %v", err)
	}
}

func TestLanzadorCommandContext(t *testing.T) {
	ctx := context.Background()
	cmd, err := CommandContext(ctx, "bash", "-c", "true")
	if err != nil {
		t.Fatalf("CommandContext fallo: %v", err)
	}
	if cmd == nil {
		t.Fatal("cmd es nil")
	}
}
