package cas

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

// ErrCorrupcionCAS indicates a cryptographic checksum mismatch between the expected CAS key and the stored content.
var ErrCorrupcionCAS = errors.New("cas: checksum mismatch (stored content corrupted)")

// ErrObjetoNoExiste indicates the requested CAS object does not exist.
var ErrObjetoNoExiste = errors.New("cas: object does not exist")

// AlmacenCAS provides a content-addressed storage backed by local disk.
// Objects are indexed by their SHA-256 hash and written atomically with end-to-end verification.
type AlmacenCAS struct {
	mu   sync.RWMutex
	raiz string
}

// NuevoAlmacenCAS opens or initializes a content-addressed store in the given directory.
func NuevoAlmacenCAS(raiz string) (*AlmacenCAS, error) {
	if raiz == "" {
		raiz = "_cas/cosechas"
	}
	if err := os.MkdirAll(raiz, 0755); err != nil {
		return nil, fmt.Errorf("cas: mkdir failed for %s: %w", raiz, err)
	}
	return &AlmacenCAS{raiz: raiz}, nil
}

// Raiz returns the root directory of the CAS store.
func (a *AlmacenCAS) Raiz() string {
	return a.raiz
}

// Ruta returns the resolved path for a given SHA-256 hash.
// It checks for <hash>.md first, then <hash>, defaulting to <hash>.md.
func (a *AlmacenCAS) Ruta(hashHex string) string {
	hashHex = strings.TrimSpace(hashHex)
	mdPath := filepath.Join(a.raiz, hashHex+".md")
	if _, err := os.Stat(mdPath); err == nil {
		return mdPath
	}
	rawPath := filepath.Join(a.raiz, hashHex)
	if _, err := os.Stat(rawPath); err == nil {
		return rawPath
	}
	return mdPath
}

// Almacenar persists the provided data under its SHA-256 hash.
// It executes an atomic write via temporary file, fsyncs data and directory,
// and immediately verifies the written file cold against the computed checksum.
func (a *AlmacenCAS) Almacenar(datos []byte) (string, string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	sum := sha256.Sum256(datos)
	hashHex := hex.EncodeToString(sum[:])
	destPath := filepath.Join(a.raiz, hashHex+".md")

	// If already exists and verifies, return immediately
	if _, err := os.Stat(destPath); err == nil {
		if ok, err := a.verificarInterno(destPath, hashHex); err == nil && ok {
			return hashHex, destPath, nil
		}
	}

	tmpPath := filepath.Join(a.raiz, fmt.Sprintf(".tmp.%s.%d", hashHex, os.Getpid()))
	f, err := os.OpenFile(tmpPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
	if err != nil {
		return "", "", fmt.Errorf("cas: create temp file failed: %w", err)
	}

	if _, err := f.Write(datos); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		return "", "", fmt.Errorf("cas: write temp file failed: %w", err)
	}

	if err := f.Sync(); err != nil {
		_ = f.Close()
		_ = os.Remove(tmpPath)
		return "", "", fmt.Errorf("cas: sync temp file failed: %w", err)
	}

	if err := f.Close(); err != nil {
		_ = os.Remove(tmpPath)
		return "", "", fmt.Errorf("cas: close temp file failed: %w", err)
	}

	if err := os.Rename(tmpPath, destPath); err != nil {
		_ = os.Remove(tmpPath)
		return "", "", fmt.Errorf("cas: atomic rename to %s failed: %w", destPath, err)
	}

	// Fsync parent directory to ensure directory entry durability (ARIES / WAL invariant pattern)
	if dirF, err := os.Open(a.raiz); err == nil {
		_ = dirF.Sync()
		_ = dirF.Close()
	}

	// Cold verification of stored artifact
	ok, err := a.verificarInterno(destPath, hashHex)
	if err != nil {
		return "", "", fmt.Errorf("cas: cold verification error: %w", err)
	}
	if !ok {
		return "", "", ErrCorrupcionCAS
	}

	return hashHex, destPath, nil
}

// Verificar performs a cold read and recalculates the SHA-256 checksum of the object.
func (a *AlmacenCAS) Verificar(hashHex string) (bool, error) {
	a.mu.RLock()
	defer a.mu.RUnlock()

	p := a.Ruta(hashHex)
	return a.verificarInterno(p, hashHex)
}

func (a *AlmacenCAS) verificarInterno(path string, expectedHash string) (bool, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return false, ErrObjetoNoExiste
		}
		return false, fmt.Errorf("cas: open file failed: %w", err)
	}
	defer f.Close()

	hasher := sha256.New()
	if _, err := io.Copy(hasher, f); err != nil {
		return false, fmt.Errorf("cas: stream hashing failed: %w", err)
	}

	actualHex := hex.EncodeToString(hasher.Sum(nil))
	if !strings.EqualFold(actualHex, strings.TrimSpace(expectedHash)) {
		return false, fmt.Errorf("%w: expected %s got %s", ErrCorrupcionCAS, expectedHash, actualHex)
	}

	return true, nil
}

// Recuperar reads an object from CAS, verifying its checksum end-to-end before returning.
func (a *AlmacenCAS) Recuperar(hashHex string) ([]byte, error) {
	a.mu.RLock()
	defer a.mu.RUnlock()

	p := a.Ruta(hashHex)
	f, err := os.Open(p)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, ErrObjetoNoExiste
		}
		return nil, fmt.Errorf("cas: open failed: %w", err)
	}
	defer f.Close()

	data, err := io.ReadAll(f)
	if err != nil {
		return nil, fmt.Errorf("cas: read failed: %w", err)
	}

	sum := sha256.Sum256(data)
	actualHex := hex.EncodeToString(sum[:])
	if !strings.EqualFold(actualHex, strings.TrimSpace(hashHex)) {
		return nil, fmt.Errorf("%w: expected %s got %s", ErrCorrupcionCAS, hashHex, actualHex)
	}

	return data, nil
}

// Existe returns true if an object exists and passes cryptographic verification.
func (a *AlmacenCAS) Existe(hashHex string) bool {
	ok, err := a.Verificar(hashHex)
	return err == nil && ok
}
