package recstream

import (
	"context"
	"encoding/binary"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) { clear(p); return len(p), nil }

// Opt-in offline codec test. No display/capsule/browser is opened, and neither
// generated pixels nor encoded video are persisted to a disk filesystem.
func TestMP4WithRealEncoder(t *testing.T) {
	if os.Getenv("RECSTREAM_CODEC_TEST") != "1" {
		t.Skip("opt-in offline ffmpeg check")
	}
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		t.Fatal(err)
	}
	root := ramDir(t)
	encode := func(name string, cap int64) []byte {
		t.Helper()
		f, err := os.OpenFile(filepath.Join(root, name), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			t.Fatal(err)
		}
		sink := newMP4Sink(f, cap)
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		p, err := launch(ctx, encoderArgs(), io.LimitReader(zeroReader{}, int64(Width*Height*3*65)), sink)
		if err != nil {
			t.Fatal(err)
		}
		err = p.Wait()
		if err != nil && !sink.limited {
			t.Fatalf("encoder: %v", err)
		}
		if err = sink.finish(); err != nil {
			t.Fatal(err)
		}
		if err = f.Close(); err != nil {
			t.Fatal(err)
		}
		verify := exec.CommandContext(ctx, "ffmpeg", "-v", "error", "-i", f.Name(), "-f", "null", "-")
		// Error output is bounded by a generated, tiny local video.
		if out, err := verify.CombinedOutput(); err != nil || len(out) != 0 {
			t.Fatalf("decode %s: %v %.500s", name, err, out)
		}
		b, err := os.ReadFile(f.Name())
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	full := encode("full.mp4", 1<<20)
	var firstFragment int
	for pos := 0; pos+8 <= len(full); {
		sz := int(binary.BigEndian.Uint32(full[pos:]))
		if sz < 8 || sz > len(full)-pos {
			t.Fatal("invalid ffmpeg top-level box")
		}
		kind := string(full[pos+4 : pos+8])
		pos += sz
		if kind == "mdat" {
			firstFragment = pos
			break
		}
	}
	if firstFragment == 0 || firstFragment == len(full) {
		t.Fatal("fragmented video not produced")
	}
	// The next moof cannot fit; cap trimming must preserve a decodable prefix.
	capped := encode("capped.mp4", int64(firstFragment+1))
	if len(capped) != firstFragment || len(capped) >= len(full) {
		t.Fatalf("cap full=%d prefix=%d capped=%d", len(full), firstFragment, len(capped))
	}
	t.Logf("[VM] offline real encoder/decode: full=%d bytes, capped=%d bytes; live fps AUSENTE", len(full), len(capped))
}
