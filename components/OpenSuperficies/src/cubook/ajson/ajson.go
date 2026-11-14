// Package ajson is the append-only act_steps.jsonl writer: one process holds
// flock(LOCK_EX), a torn last line is truncated on open.
package ajson

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"syscall"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/rc"
)

// syncFile is the durability barrier after every append (a test seam).
var syncFile = func(f *os.File) error { return f.Sync() }

type Writer struct {
	f         *os.File
	last      uint32 // highest step on disk
	have      bool
	Recovered int64 // bytes truncated from a torn tail on open
}

// Open locks (non-blocking) and repairs the file; a second holder gets E_RAM_LOCKED-class rc 5.
func Open(path string) (*Writer, error) {
	f, err := os.OpenFile(path, os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, rc.New("E_IO", err.Error())
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, rc.New("E_GATE", "act_steps.jsonl locked by another writer")
	}
	w := &Writer{f: f}
	if err := w.recover(); err != nil {
		w.Close()
		return nil, err
	}
	return w, nil
}

// recover repairs only a torn FINAL line (unterminated trailing bytes). A bad
// or non-increasing line anywhere else is corruption: E_SCHEMA, file untouched.
func (w *Writer) recover() error {
	data, err := io.ReadAll(w.f)
	if err != nil {
		return rc.New("E_IO", err.Error())
	}
	good := 0
	for good < len(data) {
		i := bytes.IndexByte(data[good:], '\n')
		if i < 0 {
			break // unterminated tail: torn final line
		}
		s, err := action.DecodeStep(data[good : good+i])
		if err != nil {
			return rc.New("E_SCHEMA", "corrupt mid-file line: "+err.Error())
		}
		if err := w.advance(s.Step); err != nil {
			return err
		}
		good += i + 1
	}
	if good != len(data) {
		w.Recovered = int64(len(data) - good)
		if err := w.f.Truncate(int64(good)); err != nil {
			return rc.New("E_IO", err.Error())
		}
	}
	if _, err := w.f.Seek(int64(good), io.SeekStart); err != nil {
		return rc.New("E_IO", err.Error())
	}
	return nil
}

// advance enforces strictly increasing step numbers.
func (w *Writer) advance(step uint32) error {
	if w.have && step <= w.last {
		return rc.New("E_SCHEMA", "step not strictly increasing")
	}
	w.last, w.have = step, true
	return nil
}

// Append writes one step line (must carry action_sha256 and input_sha256) and fsyncs.
func (w *Writer) Append(s action.Step) error {
	if w == nil || w.f == nil {
		return rc.New("E_IO", "writer closed")
	}
	b, err := json.Marshal(s)
	if err != nil {
		return rc.New("E_IO", err.Error())
	}
	if _, err := action.DecodeStep(b); err != nil {
		return err
	}
	if w.have && s.Step <= w.last {
		return rc.New("E_SCHEMA", "step not strictly increasing")
	}
	if _, err := w.f.Write(append(b, '\n')); err != nil {
		return rc.New("E_IO", err.Error())
	}
	w.last, w.have = s.Step, true
	return syncFile(w.f)
}

// Close releases the lock; idempotent.
func (w *Writer) Close() error {
	if w == nil || w.f == nil {
		return nil
	}
	err := w.f.Close()
	w.f = nil
	return err
}

// ReadAll returns the decoded steps of a jsonl file (every line must be valid).
func ReadAll(path string) ([]action.Step, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, rc.New("E_IO", err.Error())
	}
	var out []action.Step
	w := &Writer{}
	for _, ln := range bytes.Split(bytes.TrimRight(data, "\n"), []byte("\n")) {
		if len(ln) == 0 {
			continue
		}
		s, err := action.DecodeStep(ln)
		if err != nil {
			return nil, err
		}
		if err := w.advance(s.Step); err != nil {
			return nil, err
		}
		out = append(out, *s)
	}
	return out, nil
}
