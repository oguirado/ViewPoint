package supervisor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type fakeAcknowledgements struct {
	ack  map[string]bool
	seen []BodyRef
}

func (f *fakeAcknowledgements) Acknowledged(_ context.Context, ref BodyRef) (bool, error) {
	f.seen = append(f.seen, ref)
	return f.ack[ref.SHA256], nil
}

func makeBody(t *testing.T, dir string, content string) string {
	t.Helper()
	h := sha256.Sum256([]byte(content))
	digest := hex.EncodeToString(h[:])
	if err := os.WriteFile(filepath.Join(dir, digest), []byte(content), 0600); err != nil {
		t.Fatal(err)
	}
	return digest
}

func TestReapBodiesOnlyAfterAggregatorAcknowledgement(t *testing.T) {
	root := t.TempDir()
	bodyDir := filepath.Join(root, "bodies")
	if err := os.Mkdir(bodyDir, 0700); err != nil {
		t.Fatal(err)
	}
	a, b := makeBody(t, bodyDir, "acknowledged"), makeBody(t, bodyDir, "still pending")
	attempt := strings.Repeat("a", 64)
	ack := &fakeAcknowledgements{ack: map[string]bool{a: true, b: false}}
	refs := []BodyRef{{AttemptID: attempt, SHA256: a}, {AttemptID: attempt, SHA256: b}}
	deleted, err := ReapAcknowledgedBodies(context.Background(), root, attempt, refs, ack)
	if err != nil || len(deleted) != 1 || deleted[0] != a {
		t.Fatalf("wrong reap list: %v %v", deleted, err)
	}
	if _, err := os.Stat(filepath.Join(bodyDir, a)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("acknowledged body retained: %v", err)
	}
	if _, err := os.Stat(filepath.Join(bodyDir, b)); err != nil {
		t.Fatalf("unacknowledged body deleted: %v", err)
	}
	deleted, err = ReapAcknowledgedBodies(context.Background(), root, attempt, refs, ack)
	if err != nil || len(deleted) != 0 {
		t.Fatalf("not idempotent: %v %v", deleted, err)
	}
}

func TestReapRejectsRecycledIdentityAndDigestTampering(t *testing.T) {
	root := t.TempDir()
	bodyDir := filepath.Join(root, "bodies")
	if err := os.Mkdir(bodyDir, 0700); err != nil {
		t.Fatal(err)
	}
	digest := makeBody(t, bodyDir, "must stay")
	attempt := strings.Repeat("a", 64)
	ack := &fakeAcknowledgements{ack: map[string]bool{digest: true}}
	if _, err := ReapAcknowledgedBodies(context.Background(), root, attempt,
		[]BodyRef{{AttemptID: strings.Repeat("b", 64), SHA256: digest}}, ack); !errors.Is(err, ErrLedgerConflict) {
		t.Fatalf("recycled attempt accepted: %v", err)
	}
	if _, err := ReapAcknowledgedBodies(context.Background(), root, attempt,
		[]BodyRef{{AttemptID: attempt, SHA256: "../outside"}}, ack); !errors.Is(err, ErrLedgerConflict) {
		t.Fatalf("path traversal accepted: %v", err)
	}
	if err := os.WriteFile(filepath.Join(bodyDir, digest), []byte("changed bytes"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := ReapAcknowledgedBodies(context.Background(), root, attempt,
		[]BodyRef{{AttemptID: attempt, SHA256: digest}}, ack); err == nil {
		t.Fatal("digest tamper accepted")
	}
	if _, err := os.Stat(filepath.Join(bodyDir, digest)); err != nil {
		t.Fatalf("failed body deleted: %v", err)
	}
}

func TestReapRefusesSymlinkedBodies(t *testing.T) {
	root := t.TempDir()
	other := t.TempDir()
	bodyDir := filepath.Join(root, "bodies")
	if err := os.Symlink(other, bodyDir); err != nil {
		t.Fatal(err)
	}
	attempt := strings.Repeat("a", 64)
	ack := &fakeAcknowledgements{ack: map[string]bool{}}
	if _, err := ReapAcknowledgedBodies(context.Background(), root, attempt, nil, ack); err == nil {
		t.Fatal("body directory symlink accepted")
	}
	if err := os.Remove(bodyDir); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(bodyDir, 0700); err != nil {
		t.Fatal(err)
	}
	digest := makeBody(t, other, "external body")
	if err := os.Symlink(filepath.Join(other, digest), filepath.Join(bodyDir, digest)); err != nil {
		t.Fatal(err)
	}
	ack.ack[digest] = true
	if _, err := ReapAcknowledgedBodies(context.Background(), root, attempt,
		[]BodyRef{{AttemptID: attempt, SHA256: digest}}, ack); err == nil {
		t.Fatal("body file symlink accepted")
	}
	if _, err := os.Stat(filepath.Join(other, digest)); err != nil {
		t.Fatalf("outside file deleted: %v", err)
	}
}

func TestReapAbstainsOnAckError(t *testing.T) {
	root := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "bodies"), 0700); err != nil {
		t.Fatal(err)
	}
	attempt := strings.Repeat("a", 64)
	ack := &rejectAck{}
	_, err := ReapAcknowledgedBodies(context.Background(), root, attempt,
		[]BodyRef{{AttemptID: attempt, SHA256: strings.Repeat("1", 64)}}, ack)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("ack error ignored: %v", err)
	}
}

type rejectAck struct{}

func (*rejectAck) Acknowledged(context.Context, BodyRef) (bool, error) {
	return false, context.DeadlineExceeded
}
