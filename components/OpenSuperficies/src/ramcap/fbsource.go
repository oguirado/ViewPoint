package ramcap

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const FBREAD = "/usr/local/libexec/srf-fbread"
const FBPath = "/run/vqa/fb/Xvfb_screen0"

type Exec interface {
	Run(context.Context, []string, int64) (stdout, stderr []byte, rc int, err error)
}
type CommandExec struct{}

// streamBuffer rejects invalid headers, advertised lengths and the first extra
// byte while the child is running, rather than waiting for process completion.
type streamBuffer struct {
	mu     sync.Mutex
	b      []byte
	limit  int64
	pair   bool
	err    error
	cancel context.CancelFunc
}

func (s *streamBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.err != nil {
		return 0, s.err
	}
	if int64(len(s.b))+int64(len(p)) > s.limit {
		s.err = fail("E_XWD_SIZE")
	} else {
		s.b = append(s.b, p...)
		s.err = streamCheck(s.b, s.pair, s.limit, false)
	}
	if s.err != nil {
		s.cancel()
		return 0, s.err
	}
	return len(p), nil
}
func streamCheck(b []byte, pair bool, limit int64, eof bool) error {
	if !pair {
		if len(b) >= 100 {
			h, e := ParseHeader(b)
			if e != nil {
				return e
			}
			if h.Size >= limit || int64(len(b)) > h.Size {
				return fail("E_XWD_SIZE")
			}
			if eof && int64(len(b)) != h.Size {
				return fail("E_XWD_SIZE")
			}
		} else if eof {
			return fail("E_XWD_SHORT")
		}
		return nil
	}
	off := 0
	for range 2 {
		if len(b)-off < 8 {
			if eof {
				return fail("E_XWD_SIZE")
			}
			return nil
		}
		n := binary.BigEndian.Uint64(b[off:])
		off += 8
		if n < 100 || n > uint64((limit-16)/2) {
			return fail("E_XWD_SIZE")
		}
		if len(b)-off >= 100 {
			h, e := ParseHeader(b[off:])
			if e != nil {
				return e
			}
			if uint64(h.Size) != n {
				return fail("E_XWD_SIZE")
			}
		}
		if uint64(len(b)-off) < n {
			if eof {
				return fail("E_XWD_SIZE")
			}
			return nil
		}
		off += int(n)
	}
	if len(b) > off || (eof && len(b) != off) {
		return fail("E_XWD_SIZE")
	}
	return nil
}

type stderrBuffer struct{ b []byte }

func (s *stderrBuffer) Write(p []byte) (int, error) {
	n := len(p)
	if len(s.b) < 4096 {
		s.b = append(s.b, p[:min(len(p), 4096-len(s.b))]...)
	}
	return n, nil
}
func (CommandExec) Run(ctx context.Context, argv []string, maxOut int64) ([]byte, []byte, int, error) {
	if e := validateArgv(argv); e != nil {
		return nil, nil, 2, e
	}
	if maxOut < 101 || maxOut > 2*(4*16384*16384+1<<20)+18 {
		return nil, nil, 2, fail("E_XWD_SIZE")
	}
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.WaitDelay = 100 * time.Millisecond
	out := &streamBuffer{limit: maxOut, pair: argv[6] == "pair", cancel: cancel}
	stderr := &stderrBuffer{}
	cmd.Stdout = out
	cmd.Stderr = stderr
	err := cmd.Run()
	rc := 0
	if err != nil {
		rc = 5
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			rc = ee.ExitCode()
		}
	}
	if out.err != nil {
		return nil, stderr.b, 5, out.err
	}
	if ctx.Err() != nil {
		return nil, stderr.b, 5, fail("E_TIMEOUT")
	}
	if err != nil {
		return out.b, stderr.b, rc, err
	}
	if e := streamCheck(out.b, out.pair, maxOut, true); e != nil {
		return nil, stderr.b, 5, e
	}
	return out.b, stderr.b, 0, nil
}

var containerName = regexp.MustCompile(`^srf-ng-(0|[1-9][0-9]*)$`)
var xidArg = regexp.MustCompile(`^(0|[1-9][0-9]{0,9})$`)

func validateArgv(a []string) error {
	if len(a) < 8 || a[0] != "docker" || a[1] != "exec" || a[2] != "--user" || a[3] != "10001" || !containerName.MatchString(a[4]) || a[5] != FBREAD {
		return fail("E_FB_ARGV")
	}
	if len(a) == 8 && a[6] == "read" && a[7] == FBPath {
		return nil
	}
	if len(a) != 9 || (a[6] != "getimage" && a[6] != "pair") || a[7] != ":100" || !xidArg.MatchString(a[8]) {
		return fail("E_FB_ARGV")
	}
	if _, e := strconv.ParseUint(a[8], 10, 32); e != nil {
		return fail("E_FB_ARGV")
	}
	return nil
}

type pointerSample struct {
	X, Y int
	XID  uint32
}
type FBSource struct {
	mu            sync.Mutex
	pointer       *pointerSample
	exec          Exec
	width, height int
}

func NewFBSource(ex Exec, w, h int) (*FBSource, error) {
	if w <= 0 || h <= 0 || w > 16384 || h > 16384 {
		return nil, fail("E_XWD_GEOMETRY")
	}
	if ex == nil {
		ex = CommandExec{}
	}
	return &FBSource{exec: ex, width: w, height: h}, nil
}
func (s *FBSource) call(ctx context.Context, ctr, sub string, xid uint32) ([]byte, error) {
	s.mu.Lock()
	s.pointer = nil
	s.mu.Unlock()
	a := []string{"docker", "exec", "--user", "10001", ctr, FBREAD, sub}
	if sub == "read" {
		a = append(a, FBPath)
	} else {
		a = append(a, ":100", strconv.FormatUint(uint64(xid), 10))
	}
	if e := validateArgv(a); e != nil {
		return nil, e
	}
	cap := 4*int64(s.width)*int64(s.height) + (1 << 20) + 1
	if sub == "pair" {
		cap = 2*cap + 16
	}
	child, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	b, stderr, rc, e := s.exec.Run(child, a, cap)
	if child.Err() != nil {
		return nil, fail("E_TIMEOUT")
	}
	if rc != 0 {
		tokens := strings.Fields(string(stderr[:min(len(stderr), 4096)]))
		if len(tokens) > 0 {
			switch tokens[0] {
			case "E_XWD_SHORT", "E_XWD_VERSION", "E_XWD_FORMAT", "E_XWD_VISUAL", "E_XWD_GEOMETRY", "E_XWD_SIZE", "E_TORN", "E_FB_NOT_TMPFS":
				return nil, fail(tokens[0])
			}
		}
		if e != nil {
			return nil, e
		}
		return nil, fail("E_FB_READ")
	}
	if e != nil {
		return nil, e
	}
	if int64(len(b)) >= cap {
		return nil, fail("E_XWD_SIZE")
	}
	if e = streamCheck(b, sub == "pair", cap, true); e != nil {
		return nil, e
	}
	if sub == "pair" || sub == "getimage" {
		var p struct {
			X   *int    `json:"x"`
			Y   *int    `json:"y"`
			XID *uint32 `json:"xid"`
		}
		if len(stderr) <= 4096 && json.Unmarshal(stderr, &p) == nil && p.X != nil && p.Y != nil && p.XID != nil && *p.XID != 0 && *p.X >= -32768 && *p.X <= 32767 && *p.Y >= -32768 && *p.Y <= 32767 {
			s.mu.Lock()
			s.pointer = &pointerSample{*p.X, *p.Y, *p.XID}
			s.mu.Unlock()
		}
	}
	return b, nil
}
func (s *FBSource) takePointer() *pointerSample {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.pointer
	s.pointer = nil
	return p
}
func (s *FBSource) Read(ctx context.Context, ctr string) ([]byte, error) {
	return s.call(ctx, ctr, "read", 0)
}
func (s *FBSource) GetImage(ctx context.Context, ctr string, xid uint32) ([]byte, error) {
	return s.call(ctx, ctr, "getimage", xid)
}
func (s *FBSource) Pair(ctx context.Context, ctr string, xid uint32) ([]byte, []byte, error) {
	b, e := s.call(ctx, ctr, "pair", xid)
	if e != nil {
		return nil, nil, e
	}
	n := int(binary.BigEndian.Uint64(b))
	a := b[8 : 8+n]
	off := 8 + n
	n = int(binary.BigEndian.Uint64(b[off:]))
	return a, b[off+8 : off+8+n], nil
}

var _ io.Writer = (*streamBuffer)(nil)
