package wal

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"

	"opensuperficies.local/srf/src/kernelbook/flock"
)

// ClaseRegistro defines whether a WAL record belongs to the local or external class.
type ClaseRegistro string

const (
	// ClaseLocal applies to operations that can be redone or undone locally via CLR.
	ClaseLocal ClaseRegistro = "LOCAL"
	// ClaseExterna applies to irreversible external operations (INTENCION, DESENLACE)
	// which are NEVER redone and NEVER undone during recovery.
	ClaseExterna ClaseRegistro = "EXTERNA"
)

// TipoRegistro defines the specific operational type of a WAL record.
type TipoRegistro string

const (
	TipoBegin      TipoRegistro = "BEGIN"
	TipoReserva    TipoRegistro = "RESERVA"
	TipoIntencion  TipoRegistro = "INTENCION"
	TipoDesenlace  TipoRegistro = "DESENLACE"
	TipoCLR        TipoRegistro = "CLR"
	TipoEnd        TipoRegistro = "END"
	TipoResolucion TipoRegistro = "RESOLUCION"
)

// Registro represents a single WAL record in the ARIES-adapted two-class log.
type Registro struct {
	LSN         uint64          `json:"lsn"`
	PrevLSN     uint64          `json:"prev_lsn"`
	TxID        string          `json:"tx_id,omitempty"`
	Clase       ClaseRegistro   `json:"clase"`
	Tipo        TipoRegistro    `json:"tipo"`
	Clave       string          `json:"clave,omitempty"`
	Desenlace   SrfDesenlace    `json:"desenlace,omitempty"`
	RC          *int            `json:"rc,omitempty"`
	UndoNextLSN uint64          `json:"undo_next_lsn,omitempty"`
	Timestamp   time.Time       `json:"timestamp"`
	Detalles    json.RawMessage `json:"detalles,omitempty"`
}

// LibroConsumo manages appending and syncing records to the write-ahead log.
type LibroConsumo struct {
	mu         sync.Mutex
	path       string
	file       *os.File
	lock       *flock.FileLock
	lastLSN    uint64
	flushedLSN uint64
	txLastLSN  map[string]uint64
}

// AbrirLibro opens or creates a WAL file, acquiring an exclusive lock.
func AbrirLibro(path string) (*LibroConsumo, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return nil, fmt.Errorf("wal: mkdir error: %w", err)
	}

	lockPath := path + ".flock"
	fl, err := flock.AcquireExclusive(lockPath)
	if err != nil {
		return nil, fmt.Errorf("wal: acquire lock failed: %w", err)
	}

	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR|os.O_APPEND, 0644)
	if err != nil {
		_ = fl.Unlock()
		return nil, fmt.Errorf("wal: open log file failed: %w", err)
	}

	// H19/H25: Fsync containing directory once after file creation to ensure
	// directory entry durability before any synchronous intention dispatch.
	// Directory fsync is asserted by inspection and is not covered by a test.
	dirFile, err := os.Open(filepath.Dir(path))
	if err != nil {
		_ = f.Close()
		_ = fl.Unlock()
		return nil, fmt.Errorf("wal: open parent dir failed: %w", err)
	}
	if err := dirFile.Sync(); err != nil {
		_ = dirFile.Close()
		_ = f.Close()
		_ = fl.Unlock()
		return nil, fmt.Errorf("wal: fsync parent dir failed: %w", err)
	}
	_ = dirFile.Close()

	libro := &LibroConsumo{
		path:      path,
		file:      f,
		lock:      fl,
		txLastLSN: make(map[string]uint64),
	}

	// Scan existing records to determine highest LSN and valid offset
	registros, validOffset, err := leerRegistrosInterno(path)
	if err != nil {
		_ = f.Close()
		_ = fl.Unlock()
		return nil, fmt.Errorf("wal: scan existing records failed: %w", err)
	}

	// H20: Truncate any torn trailing record to ensure clean append boundary
	stat, err := f.Stat()
	if err == nil && stat.Size() > validOffset {
		if err := f.Truncate(validOffset); err != nil {
			_ = f.Close()
			_ = fl.Unlock()
			return nil, fmt.Errorf("wal: truncate torn tail failed: %w", err)
		}
		if _, err := f.Seek(validOffset, io.SeekStart); err != nil {
			_ = f.Close()
			_ = fl.Unlock()
			return nil, fmt.Errorf("wal: seek to valid offset failed: %w", err)
		}
	}

	for _, reg := range registros {
		if reg.LSN > libro.lastLSN {
			libro.lastLSN = reg.LSN
		}
		if reg.TxID != "" {
			libro.txLastLSN[reg.TxID] = reg.LSN
		}
	}
	libro.flushedLSN = libro.lastLSN

	return libro, nil
}

// Path returns the path of the underlying log file.
func (l *LibroConsumo) Path() string {
	return l.path
}

// LastLSN returns the most recently assigned LSN.
func (l *LibroConsumo) LastLSN() uint64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastLSN
}

// FlushedLSN returns the highest LSN confirmed flushed to disk via fsync.
func (l *LibroConsumo) FlushedLSN() uint64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.flushedLSN
}

// Sync flushes all pending writes to disk, updating FlushedLSN.
func (l *LibroConsumo) Sync() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file == nil {
		return errors.New("wal: log file is closed")
	}
	if err := l.file.Sync(); err != nil {
		return fmt.Errorf("wal: fsync failed: %w", err)
	}
	l.flushedLSN = l.lastLSN
	return nil
}

// Escribir appends a record to the WAL, enforcing LSN sequencing and the external flush invariant.
func (l *LibroConsumo) Escribir(reg *Registro) (uint64, error) {
	l.mu.Lock()
	defer l.mu.Unlock()

	if l.file == nil {
		return 0, errors.New("wal: log file is closed")
	}

	l.lastLSN++
	reg.LSN = l.lastLSN

	if reg.TxID != "" {
		reg.PrevLSN = l.txLastLSN[reg.TxID]
		l.txLastLSN[reg.TxID] = reg.LSN
	}

	if reg.Timestamp.IsZero() {
		reg.Timestamp = time.Now().UTC()
	}

	data, err := json.Marshal(reg)
	if err != nil {
		return 0, fmt.Errorf("wal: marshal record failed: %w", err)
	}
	data = append(data, '\n')

	if _, err := l.file.Write(data); err != nil {
		return 0, fmt.Errorf("wal: write record failed: %w", err)
	}

	// External WAL Invariant: INTENCION and DESENLACE records must be committed
	// to disk via fsync immediately before external communication or outcome finalization.
	if reg.Clase == ClaseExterna || reg.Tipo == TipoIntencion || reg.Tipo == TipoDesenlace || reg.Tipo == TipoResolucion {
		if err := l.file.Sync(); err != nil {
			return 0, fmt.Errorf("wal: fsync for external record failed: %w", err)
		}
		l.flushedLSN = l.lastLSN
	}

	return reg.LSN, nil
}

// EscribirBegin records the start of a transaction.
func (l *LibroConsumo) EscribirBegin(txID string) (uint64, error) {
	return l.Escribir(&Registro{
		TxID:  txID,
		Clase: ClaseLocal,
		Tipo:  TipoBegin,
	})
}

// EscribirReserva records a local quota reservation.
func (l *LibroConsumo) EscribirReserva(txID string, detalles any) (uint64, error) {
	raw, err := marshalDetalles(detalles)
	if err != nil {
		return 0, err
	}
	return l.Escribir(&Registro{
		TxID:     txID,
		Clase:    ClaseLocal,
		Tipo:     TipoReserva,
		Detalles: raw,
	})
}

// EscribirIntencion records an external intention before physical dispatch.
// By the external WAL invariant, this is synchronously flushed to disk.
func (l *LibroConsumo) EscribirIntencion(txID, clave string, detalles any) (uint64, error) {
	raw, err := marshalDetalles(detalles)
	if err != nil {
		return 0, err
	}
	return l.Escribir(&Registro{
		TxID:     txID,
		Clase:    ClaseExterna,
		Tipo:     TipoIntencion,
		Clave:    clave,
		Detalles: raw,
	})
}

// EscribirDesenlace records the transactional outcome of an external dispatch.
// Synchronously flushed to disk.
func (l *LibroConsumo) EscribirDesenlace(txID, clave string, desenlace SrfDesenlace, rc SrfRCMotor, detalles any) (uint64, error) {
	raw, err := marshalDetalles(detalles)
	if err != nil {
		return 0, err
	}
	rcInt := int(rc)
	return l.Escribir(&Registro{
		TxID:      txID,
		Clase:     ClaseExterna,
		Tipo:      TipoDesenlace,
		Clave:     clave,
		Desenlace: desenlace,
		RC:        &rcInt,
		Detalles:  raw,
	})
}

// EscribirCLR records a compensation log record during undo phase.
func (l *LibroConsumo) EscribirCLR(txID string, undoNextLSN uint64, detalles any) (uint64, error) {
	raw, err := marshalDetalles(detalles)
	if err != nil {
		return 0, err
	}
	return l.Escribir(&Registro{
		TxID:        txID,
		Clase:       ClaseLocal,
		Tipo:        TipoCLR,
		UndoNextLSN: undoNextLSN,
		Detalles:    raw,
	})
}

// EscribirEnd records the conclusion of a transaction.
func (l *LibroConsumo) EscribirEnd(txID string) (uint64, error) {
	lsn, err := l.Escribir(&Registro{
		TxID:  txID,
		Clase: ClaseLocal,
		Tipo:  TipoEnd,
	})
	if err != nil {
		return 0, err
	}
	if err := l.Sync(); err != nil {
		return 0, err
	}
	return lsn, nil
}

// EscribirResolucion records a signed operator audit resolution for a key in EN_DUDA.
func (l *LibroConsumo) EscribirResolucion(clave string, desenlace SrfDesenlace, detalles any) (uint64, error) {
	raw, err := marshalDetalles(detalles)
	if err != nil {
		return 0, err
	}
	return l.Escribir(&Registro{
		Clase:     ClaseExterna,
		Tipo:      TipoResolucion,
		Clave:     clave,
		Desenlace: desenlace,
		Detalles:  raw,
	})
}

// Cerrar syncs and closes the log file, releasing the advisory lock.
func (l *LibroConsumo) Cerrar() error {
	l.mu.Lock()
	defer l.mu.Unlock()

	var errs []error
	if l.file != nil {
		if err := l.file.Sync(); err != nil {
			errs = append(errs, fmt.Errorf("wal: sync on close: %w", err))
		}
		if err := l.file.Close(); err != nil {
			errs = append(errs, fmt.Errorf("wal: close file: %w", err))
		}
		l.file = nil
	}
	if l.lock != nil {
		if err := l.lock.Unlock(); err != nil {
			errs = append(errs, fmt.Errorf("wal: unlock flock: %w", err))
		}
		l.lock = nil
	}

	return errors.Join(errs...)
}

// rawLineInfo stores line number, raw data bytes, and ending byte offset in the file.
type rawLineInfo struct {
	lineNum   int
	data      []byte
	endOffset int64
}

// leerRegistrosInterno reads all valid JSONL records from a WAL file and returns the
// valid records along with the byte offset up to the end of the last valid record.
// H20: A torn record at the tail (crash during append) is treated as incomplete: reading
// stops there without error. A malformed record in the middle is a hard error.
func leerRegistrosInterno(path string) ([]Registro, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, 0, nil
		}
		return nil, 0, fmt.Errorf("wal: open log for read failed: %w", err)
	}
	defer f.Close()

	var rawLines []rawLineInfo
	reader := bufio.NewReader(f)
	var currentOffset int64
	lineNum := 0

	for {
		lineNum++
		data, readErr := reader.ReadBytes('\n')
		if len(data) > 0 {
			currentOffset += int64(len(data))
			rawLines = append(rawLines, rawLineInfo{
				lineNum:   lineNum,
				data:      data,
				endOffset: currentOffset,
			})
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			return nil, 0, fmt.Errorf("wal: read lines failed: %w", readErr)
		}
	}

	if len(rawLines) == 0 {
		return nil, 0, nil
	}

	// Find the index of the last non-empty line
	lastNonEmptyIdx := -1
	for i := len(rawLines) - 1; i >= 0; i-- {
		if len(bytes.TrimSpace(rawLines[i].data)) > 0 {
			lastNonEmptyIdx = i
			break
		}
	}

	if lastNonEmptyIdx == -1 {
		return nil, 0, nil
	}

	var registros []Registro
	var validOffset int64

	for i := 0; i <= lastNonEmptyIdx; i++ {
		line := rawLines[i]
		trimmed := bytes.TrimSpace(line.data)
		if len(trimmed) == 0 {
			continue
		}

		var reg Registro
		if err := json.Unmarshal(trimmed, &reg); err != nil {
			if i == lastNonEmptyIdx {
				// Torn trailing record at tail: crash during append. Stop reading, do not error.
				break
			}
			// Malformed record in the middle: hard error.
			return nil, 0, fmt.Errorf("wal: parse json line %d failed: %w", line.lineNum, err)
		}

		registros = append(registros, reg)
		validOffset = line.endOffset
	}

	return registros, validOffset, nil
}

// LeerRegistros reads all valid JSONL records from a WAL file.
func LeerRegistros(path string) ([]Registro, error) {
	regs, _, err := leerRegistrosInterno(path)
	return regs, err
}

func marshalDetalles(detalles any) (json.RawMessage, error) {
	if detalles == nil {
		return nil, nil
	}
	if raw, ok := detalles.(json.RawMessage); ok {
		return raw, nil
	}
	data, err := json.Marshal(detalles)
	if err != nil {
		return nil, fmt.Errorf("wal: marshal detalles failed: %w", err)
	}
	return data, nil
}
