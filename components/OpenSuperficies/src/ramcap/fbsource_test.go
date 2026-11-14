package ramcap

import (
	"context"
	"encoding/binary"
	"sync"
	"testing"
	"time"
)

type execFunc func(context.Context, []string, int64) ([]byte, []byte, int, error)

func (f execFunc) Run(ctx context.Context, a []string, n int64) ([]byte, []byte, int, error) {
	return f(ctx, a, n)
}
func pairBytes(a, b []byte) []byte {
	out := make([]byte, 16+len(a)+len(b))
	binary.BigEndian.PutUint64(out, uint64(len(a)))
	copy(out[8:], a)
	off := 8 + len(a)
	binary.BigEndian.PutUint64(out[off:], uint64(len(b)))
	copy(out[off+8:], b)
	return out
}
func TestFbreadArgvAllowlist(t *testing.T) {
	base := []string{"docker", "exec", "--user", "10001", "srf-ng-1", FBREAD}
	tails := [][]string{{"read", FBPath}, {"getimage", ":100", "0"}, {"pair", ":100", "4294967295"}}
	for _, tail := range tails {
		if e := validateArgv(append(append([]string{}, base...), tail...)); e != nil {
			t.Fatal(e)
		}
	}
	bad := [][]string{{"read", "/tmp/x"}, {"pair", ":100", "4294967296"}, {"pair", ":100", "01"}, {"getimage", ":100", "-1"}, {"getimage", ":101", "1"}, {"sh", "-c", "x"}, {"pair", ":100", "1", "extra"}}
	for _, tail := range bad {
		code(t, validateArgv(append(append([]string{}, base...), tail...)), "E_FB_ARGV")
	}
}
func TestFbsourceStdoutOnly(t *testing.T) {
	frame := buildXWD(4, 2, nil, "")
	count := 0
	s, e := NewFBSource(execFunc(func(ctx context.Context, a []string, n int64) ([]byte, []byte, int, error) {
		count++
		if e := validateArgv(a); e != nil {
			t.Fatal(e)
		}
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 2*time.Second {
			t.Fatal("unbounded")
		}
		return frame, []byte("not frame"), 0, nil
	}), 4, 2)
	if e != nil {
		t.Fatal(e)
	}
	out, e := s.Read(context.Background(), "srf-ng-1")
	if e != nil || string(out) != string(frame) || count != 1 {
		t.Fatal(e, count)
	}
}
func TestHostRejectsExtraBytes(t *testing.T) {
	b := append(buildXWD(4, 2, nil, ""), 0)
	s, _ := NewFBSource(execFunc(func(context.Context, []string, int64) ([]byte, []byte, int, error) { return b, nil, 0, nil }), 4, 2)
	_, e := s.Read(context.Background(), "srf-ng-1")
	code(t, e, "E_XWD_SIZE")
}
func TestMaliciousXWDHeaderOverflowAndSlowLoris(t *testing.T) {
	for _, field := range []int{0, 19, 12} {
		b := buildXWD(4, 2, nil, "")
		binary.BigEndian.PutUint32(b[field*4:], 0xffffffff)
		_, e := ParseHeader(b)
		code(t, e, "E_XWD_GEOMETRY")
	}
	calls := 0
	s, _ := NewFBSource(execFunc(func(ctx context.Context, _ []string, _ int64) ([]byte, []byte, int, error) {
		calls++
		<-ctx.Done()
		return nil, nil, 5, ctx.Err()
	}), 4, 2)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	start := time.Now()
	_, e := s.Read(ctx, "srf-ng-1")
	code(t, e, "E_TIMEOUT")
	if calls != 1 || time.Since(start) > time.Second {
		t.Fatal("host retries or deadline overrun")
	}
}
func TestStreamingBoundsKillAtHeaderOrExtraByte(t *testing.T) {
	for _, pair := range []bool{false, true} {
		b := buildXWD(4, 2, nil, "")
		if pair {
			b = pairBytes(b, b)
		}
		ctx, cancel := context.WithCancel(context.Background())
		s := streamBuffer{limit: 2 << 20, pair: pair, cancel: cancel}
		if _, e := s.Write(b); e != nil {
			t.Fatal(e)
		}
		if _, e := s.Write([]byte{0}); e == nil {
			t.Fatal("extra byte accepted")
		}
		if ctx.Err() == nil {
			t.Fatal("not killed")
		}
		cancel()
	}
	b := buildXWD(4, 2, nil, "")
	binary.BigEndian.PutUint32(b[0:], 0xffffffff)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	s := streamBuffer{limit: 2 << 20, cancel: cancel}
	_, e := s.Write(b[:100])
	code(t, e, "E_XWD_GEOMETRY")
	if ctx.Err() == nil {
		t.Fatal("bad header not killed")
	}
}

// shared fake is race-safe: ticks and synchronous verification use one source.
type movingExec struct {
	mu               sync.Mutex
	n                int
	moving, mismatch bool
	reads, pairs     int
}

func (f *movingExec) Run(ctx context.Context, a []string, _ int64) ([]byte, []byte, int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if e := ctx.Err(); e != nil {
		return nil, nil, 5, e
	}
	f.n++
	b := buildXWD(8, 4, nil, "srf-fbread xid=42\x00")
	if f.moving {
		b[len(b)-4] = byte(f.n)
	}
	switch a[6] {
	case "read":
		f.reads++
		return b, nil, 0, nil
	case "pair":
		f.pairs++
		direct := append([]byte{}, b...)
		if f.mismatch {
			direct[len(direct)-4]++
		}
		return pairBytes(b, direct), nil, 0, nil
	default:
		return b, nil, 0, nil
	}
}

func TestReaderErrorCodeParity(t *testing.T) {
	s, _ := NewFBSource(execFunc(func(context.Context, []string, int64) ([]byte, []byte, int, error) {
		return nil, []byte("E_TORN no stable framebuffer"), 5, fail("process exited")
	}), 4, 2)
	_, e := s.Read(context.Background(), "srf-ng-1")
	code(t, e, "E_TORN")
}
