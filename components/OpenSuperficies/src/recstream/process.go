// Package recstream records the native capsule stream into a bounded RAM MP4.
// Raw pixels exist only in Go memory and anonymous process pipes.
package recstream

import (
	"context"
	"io"
	"os/exec"
	"time"
)

type Process interface{ Wait() error }
type Launcher func(context.Context, []string, io.Reader, io.Writer) (Process, error)

func launch(ctx context.Context, argv []string, in io.Reader, out io.Writer) (Process, error) {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = in, out, io.Discard
	cmd.WaitDelay = 2 * time.Second
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return cmd, nil
}
