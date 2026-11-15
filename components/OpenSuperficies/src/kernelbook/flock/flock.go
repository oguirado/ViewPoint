package flock

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"golang.org/x/sys/unix"
)

// ExitCodeLockCollision is the deterministic exit code (69) when an advisory lock is already held.
const ExitCodeLockCollision = 69

// ErrLockHeld indicates that the file lock is already acquired by another process.
var ErrLockHeld = errors.New("flock: file lock already held")

// LockError represents a lock failure with a deterministic exit code.
type LockError struct {
	Code int
	Path string
	Err  error
}

func (e *LockError) Error() string {
	return fmt.Sprintf("lock error (rc %d) on %s: %v", e.Code, e.Path, e.Err)
}

func (e *LockError) Unwrap() error {
	return e.Err
}

// FileLock holds an active advisory flock and the underlying file descriptor.
type FileLock struct {
	Path string
	File *os.File
}

// AcquireExclusive attempts to acquire a non-blocking exclusive lock (LOCK_EX | LOCK_NB).
// If the lock is held, returns a *LockError with Code 69.
func AcquireExclusive(path string) (*FileLock, error) {
	return acquire(path, unix.LOCK_EX|unix.LOCK_NB)
}

// AcquireShared attempts to acquire a non-blocking shared lock (LOCK_SH | LOCK_NB).
// If the lock is held exclusively, returns a *LockError with Code 69.
func AcquireShared(path string) (*FileLock, error) {
	return acquire(path, unix.LOCK_SH|unix.LOCK_NB)
}

// AcquireExclusiveTimeout attempts to acquire an exclusive lock, retrying until timeout expires.
func AcquireExclusiveTimeout(path string, timeout time.Duration) (*FileLock, error) {
	deadline := time.Now().Add(timeout)
	for {
		fl, err := AcquireExclusive(path)
		if err == nil {
			return fl, nil
		}
		if !IsLockError(err) {
			return nil, err
		}
		if time.Now().After(deadline) {
			return nil, &LockError{Code: ExitCodeLockCollision, Path: path, Err: ErrLockHeld}
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func acquire(path string, flag int) (*FileLock, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return nil, fmt.Errorf("flock: mkdir failed: %w", err)
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0644)
	if err != nil {
		return nil, fmt.Errorf("flock: open failed: %w", err)
	}

	fd := int(f.Fd())
	if err := unix.Flock(fd, flag); err != nil {
		_ = f.Close()
		if errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN) {
			return nil, &LockError{Code: ExitCodeLockCollision, Path: path, Err: ErrLockHeld}
		}
		return nil, fmt.Errorf("flock: syscall failed: %w", err)
	}

	return &FileLock{
		Path: path,
		File: f,
	}, nil
}

// Unlock releases the advisory lock and closes the file descriptor.
func (l *FileLock) Unlock() error {
	if l == nil || l.File == nil {
		return nil
	}
	fd := int(l.File.Fd())
	_ = unix.Flock(fd, unix.LOCK_UN)
	err := l.File.Close()
	l.File = nil
	return err
}

// Release is an alias for Unlock.
func (l *FileLock) Release() error {
	return l.Unlock()
}

// IsLockError checks if the error is a lock collision (rc 69 / ErrLockHeld).
func IsLockError(err error) bool {
	if err == nil {
		return false
	}
	var lockErr *LockError
	if errors.As(err, &lockErr) {
		return lockErr.Code == ExitCodeLockCollision || errors.Is(lockErr.Err, ErrLockHeld)
	}
	return errors.Is(err, ErrLockHeld) || errors.Is(err, unix.EWOULDBLOCK) || errors.Is(err, unix.EAGAIN)
}
