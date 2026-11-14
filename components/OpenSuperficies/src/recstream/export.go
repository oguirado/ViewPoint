package recstream

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
)

// Export is a host-only capability. The cockpit adapter installs it only in
// recordingPolicy; no App binding, state route, lease or HTTP endpoint exposes it.
func (e *Engine) Export(ctx context.Context, id, target string) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	s, err := e.get(id)
	if err != nil {
		return "", err
	}
	select {
	case <-s.done:
		if s.err != nil {
			return "", s.err
		}
	default:
		return "", errors.New("REFUSED: recording not sealed")
	}
	if !filepath.IsAbs(target) {
		return "", errors.New("REFUSED: absolute operator directory required")
	}
	root, err := filepath.EvalSymlinks(target)
	if err != nil {
		return "", err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	dir, err := os.MkdirTemp(root, "recording-")
	if err != nil {
		return "", err
	}
	ok := false
	defer func() {
		if !ok {
			os.RemoveAll(dir)
		}
	}()
	for _, name := range []string{"video-000.mp4", "eventos.jsonl", "FLUJO.md", "manifiesto.json"} {
		if err = copyArtifact(ctx, filepath.Join(s.dir, name), filepath.Join(dir, name)); err != nil {
			return "", err
		}
	}
	ok = true
	return dir, nil
}
func copyArtifact(ctx context.Context, source, target string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer out.Close()
	buf := make([]byte, 64<<10)
	for {
		if err = ctx.Err(); err != nil {
			return err
		}
		n, readErr := in.Read(buf)
		if n > 0 {
			written, err := out.Write(buf[:n])
			if err != nil {
				return err
			}
			if written != n {
				return io.ErrShortWrite
			}
		}
		if readErr == io.EOF {
			return out.Sync()
		}
		if readErr != nil {
			return readErr
		}
	}
}
