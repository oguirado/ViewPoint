package supervisor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// BodyRef is a content-addressed text body for a specific immutable attempt.
type BodyRef struct {
	AttemptID string
	SHA256    string
}

// Acknowledger reports durable aggregator acknowledgement, not a local read.
// It is injected; the supervisor does not infer acknowledgement from a receipt.
type Acknowledger interface {
	Acknowledged(context.Context, BodyRef) (bool, error)
}

// ReapAcknowledgedBodies only removes regular files named by lowercase SHA-256
// inside the given run_dir/bodies. Unacknowledged and unverifiable bodies remain.
// It rejects symlinks, invalid/recycled attempt references, and hash mismatches.
func ReapAcknowledgedBodies(ctx context.Context, runDir, attemptID string, refs []BodyRef, ack Acknowledger) ([]string, error) {
	if ack == nil || !isLowerHex(attemptID, 64) {
		return nil, errors.New("w8 supervisor: invalid acknowledgement boundary")
	}
	info, err := os.Lstat(runDir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("w8 supervisor: invalid attempt directory")
	}
	bodyDir := filepath.Join(runDir, "bodies")
	info, err = os.Lstat(bodyDir)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, errors.New("w8 supervisor: invalid body directory")
	}

	deleted := make([]string, 0, len(refs))
	seen := make(map[string]bool, len(refs))
	for _, ref := range refs {
		if !isLowerHex(ref.SHA256, 64) || ref.AttemptID != attemptID {
			return deleted, ErrLedgerConflict
		}
		if seen[ref.SHA256] {
			continue
		}
		seen[ref.SHA256] = true
		if err := ctx.Err(); err != nil {
			return deleted, err
		}
		confirmed, err := ack.Acknowledged(ctx, ref)
		if err != nil {
			return deleted, err
		}
		if !confirmed {
			continue
		}
		path := filepath.Join(bodyDir, ref.SHA256)
		before, err := os.Lstat(path)
		if errors.Is(err, os.ErrNotExist) {
			continue // idempotent reaping of an acknowledged body
		}
		if err != nil {
			return deleted, err
		}
		if !before.Mode().IsRegular() {
			return deleted, fmt.Errorf("w8 supervisor: nonregular body: %s", ref.SHA256)
		}
		f, err := os.Open(path)
		if err != nil {
			return deleted, err
		}
		h := sha256.New()
		_, copyErr := io.Copy(h, f)
		closeErr := f.Close()
		if copyErr != nil {
			return deleted, copyErr
		}
		if closeErr != nil {
			return deleted, closeErr
		}
		if hex.EncodeToString(h.Sum(nil)) != ref.SHA256 {
			return deleted, fmt.Errorf("w8 supervisor: body digest changed: %s", ref.SHA256)
		}
		after, err := os.Lstat(path)
		if err != nil || !os.SameFile(before, after) || !after.Mode().IsRegular() {
			return deleted, errors.New("w8 supervisor: body identity changed during verification")
		}
		if err := os.Remove(path); err != nil {
			return deleted, err
		}
		deleted = append(deleted, ref.SHA256)
	}
	return deleted, nil
}
