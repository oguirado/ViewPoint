package lanzador

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"path/filepath"
	"strings"
)

// ErrComandoProhibido is returned when a command is rejected by the strict allowlist.
var ErrComandoProhibido = errors.New("lanzador: comando no permitido por la lista blanca")

// Allowed targets defined strictly by KernelBook L0 (PROMPT_CONSTRUCCION_U00):
// 1. qemu-system-aarch64
// 2. bash (PTY sessions)
// 3. python3 /home/user/Documentos/OpenFresnel/companions/grabador/cli.py
// 4. /home/user/Documentos/OpenSwitchboard/bin/sbd
// 5. ffmpeg
// 6. xwininfo
// 7. Xvfb (tests only)
// 8. bin/srf-capsula-*
// docker is absent by construction.

const grabadorCLI = "/home/user/Documentos/OpenFresnel/companions/grabador/cli.py"
const switchboardSbd = "/home/user/Documentos/OpenSwitchboard/bin/sbd"

// Validate verifies whether the command and arguments comply with the launcher allowlist.
func Validate(name string, args ...string) error {
	base := filepath.Base(name)

	// Explicit rejection: docker is absent by construction.
	if base == "docker" || strings.Contains(name, "docker") {
		return fmt.Errorf("%w: docker está ausente por construcción y prohibido en toda la jerarquía", ErrComandoProhibido)
	}

	// 1. qemu-system-aarch64
	if base == "qemu-system-aarch64" {
		return nil
	}

	// 2. bash
	if base == "bash" {
		return nil
	}

	// 3. python3 with companions/grabador/cli.py
	if base == "python3" || base == "python" {
		if len(args) > 0 && (args[0] == grabadorCLI || filepath.Clean(args[0]) == grabadorCLI) {
			return nil
		}
		return fmt.Errorf("%w: python3 solo permitido con %s, recibido %v", ErrComandoProhibido, grabadorCLI, args)
	}

	// 4. /home/user/Documentos/OpenSwitchboard/bin/sbd (absolute path only, reject bare 'sbd')
	if filepath.Clean(name) == switchboardSbd {
		return nil
	}

	// 5. ffmpeg
	if base == "ffmpeg" {
		return nil
	}

	// 6. xwininfo
	if base == "xwininfo" {
		return nil
	}

	// 7. Xvfb (tests only)
	if base == "Xvfb" {
		return nil
	}

	// 8. bin/srf-capsula-*
	if strings.HasPrefix(base, "srf-capsula-") {
		return nil
	}

	return fmt.Errorf("%w: ejecutable no autorizado %q", ErrComandoProhibido, name)
}

// Command creates an *exec.Cmd after validating the target against the allowlist.
func Command(name string, args ...string) (*exec.Cmd, error) {
	if err := Validate(name, args...); err != nil {
		return nil, err
	}
	return exec.Command(name, args...), nil
}

// CommandContext creates an *exec.Cmd with context after validating the target against the allowlist.
func CommandContext(ctx context.Context, name string, args ...string) (*exec.Cmd, error) {
	if err := Validate(name, args...); err != nil {
		return nil, err
	}
	return exec.CommandContext(ctx, name, args...), nil
}
