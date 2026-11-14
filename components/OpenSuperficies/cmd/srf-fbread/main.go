// srf-fbread emits lossless XWD bytes only. It never creates a capture file.
package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"
	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/ramcap"
)

const fbPath = "/run/vqa/fb/Xvfb_screen0"
const maxFrame = 4*16384*16384 + 1<<20
const grabLimit = 500 * time.Millisecond

type request struct {
	sub string
	xid uint32
}

func parseArgs(args []string) (request, error) {
	if len(args) == 2 && args[0] == "read" && args[1] == fbPath {
		return request{sub: "read"}, nil
	}
	if len(args) == 3 && (args[0] == "getimage" || args[0] == "pair" || args[0] == "stream") && args[1] == ":100" {
		v, err := strconv.ParseUint(args[2], 10, 32)
		if err == nil && strconv.FormatUint(v, 10) == args[2] {
			return request{sub: args[0], xid: uint32(v)}, nil
		}
	}
	return request{}, errors.New("usage: read /run/vqa/fb/Xvfb_screen0 | getimage :100 <u32> | pair :100 <u32> | stream :100 <u32>")
}

type imageServer interface {
	Grab() error
	Ungrab() error
	Image(uint32) ([]byte, error)
	Close()
}
type dependencies struct {
	read  func() ([]byte, error)
	open  func() (imageServer, error)
	stdin io.Reader     // stream mode: host control records
	mono  func() uint64 // stream mode: capsule monotonic clock in ns
}

func main() {
	// xgb's authority diagnostics must not leak into the frame or diagnostic stream.
	xgb.Logger.SetOutput(io.Discard)
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, dependencies{read: readFramebuffer, open: openServer, stdin: os.Stdin, mono: monoNow}))
}

func run(args []string, stdout, stderr io.Writer, d dependencies) (rc int) {
	defer func() {
		if recover() != nil {
			rc = 5
			_, _ = io.WriteString(stderr, "E_XWD_FORMAT: capture panic\n")
		}
	}()
	req, err := parseArgs(args)
	if err != nil {
		_, _ = io.WriteString(stderr, err.Error()+"\n")
		return 2
	}
	if req.sub == "stream" {
		return runStream(req.xid, stdout, stderr, d)
	}
	var out []byte
	if req.sub == "read" {
		out, err = d.read()
	} else {
		var srv imageServer
		srv, err = d.open()
		if err == nil {
			defer srv.Close()
			if req.sub == "pair" {
				out, err = capturePairXID(srv, d.read, grabLimit, req.xid)
			} else {
				out, err = srv.Image(req.xid)
			}
			if meta, ok := srv.(interface{ PointerMetadata() []byte }); ok && err == nil {
				_, _ = stderr.Write(meta.PointerMetadata())
			}
		}
	}
	if err != nil {
		msg := err.Error()
		if len(msg) > 4095 {
			msg = msg[:4095]
		}
		_, _ = io.WriteString(stderr, msg+"\n")
		return 5
	}
	if req.sub != "pair" {
		if _, err = ramcap.Validate(out, 0, 0); err != nil {
			_, _ = io.WriteString(stderr, err.Error()+"\n")
			return 5
		}
	}
	if n, writeErr := stdout.Write(out); writeErr != nil || n != len(out) {
		return 5
	}
	return 0
}

// capturePair publishes nothing until a checked UngrabServer has completed.
// Closing the private X connection at the deadline also releases the server grab.
func capturePair(srv imageServer, read func() ([]byte, error), limit time.Duration) (out []byte, err error) {
	return capturePairXID(srv, read, limit, 0)
}
func capturePairXID(srv imageServer, read func() ([]byte, error), limit time.Duration, xid uint32) ([]byte, error) {
	a, b, err := capturePairLegs(srv, read, limit, xid)
	if err != nil {
		return nil, err
	}
	var framed bytes.Buffer
	for _, frame := range [][]byte{a, b} {
		var size [8]byte
		binary.BigEndian.PutUint64(size[:], uint64(len(frame)))
		framed.Write(size[:])
		framed.Write(frame)
	}
	return framed.Bytes(), nil
}

// capturePairLegs returns both validated legs only after a checked UngrabServer.
func capturePairLegs(srv imageServer, read func() ([]byte, error), limit time.Duration, xid uint32) (a, b []byte, err error) {
	deadline := time.Now().Add(limit)
	timer := time.AfterFunc(limit, srv.Close)
	defer timer.Stop()
	if err = srv.Grab(); err != nil {
		return nil, nil, fmt.Errorf("E_XWD_FORMAT: grab: %w", err)
	}
	defer func() {
		ungrabErr := srv.Ungrab()
		if ungrabErr != nil {
			a, b = nil, nil
			err = fmt.Errorf("E_XWD_FORMAT: ungrab: %w", ungrabErr)
		}
		if !time.Now().Before(deadline) {
			a, b = nil, nil
			err = errors.New("E_TIMEOUT: server grab deadline")
		}
	}()
	if a, err = read(); err != nil {
		return nil, nil, err
	}
	if !time.Now().Before(deadline) {
		return nil, nil, errors.New("E_TIMEOUT: server grab deadline")
	}
	if b, err = srv.Image(xid); err != nil {
		return nil, nil, err
	}
	for _, frame := range [][]byte{a, b} {
		if _, err = ramcap.Validate(frame, 0, 0); err != nil {
			return nil, nil, err
		}
	}
	return a, b, nil
}

func readFramebuffer() ([]byte, error) {
	fd, err := unix.Open(fbPath, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	defer unix.Close(fd)
	var fs unix.Statfs_t
	if err = unix.Fstatfs(fd, &fs); err != nil {
		return nil, err
	}
	var st unix.Stat_t
	if err = unix.Fstat(fd, &st); err != nil {
		return nil, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG {
		return nil, errors.New("E_XWD_SIZE")
	}
	return readSnapshot(int64(fs.Type), st.Size, func() ([]byte, func(), error) {
		mapped, err := unix.Mmap(fd, 0, int(st.Size), unix.PROT_READ, unix.MAP_SHARED)
		return mapped, func() {
			if mapped != nil {
				_ = unix.Munmap(mapped)
			}
		}, err
	})
}

// readSnapshot's injectable mapping keeps tests entirely in memory.
func readSnapshot(fsType, size int64, mapRO func() ([]byte, func(), error)) ([]byte, error) {
	if fsType != unix.TMPFS_MAGIC {
		return nil, errors.New("E_FB_NOT_TMPFS")
	}
	if size < 100 || size > maxFrame {
		return nil, errors.New("E_XWD_SIZE")
	}
	mapped, unmap, err := mapRO()
	if err != nil {
		return nil, err
	}
	defer unmap()
	if int64(len(mapped)) != size {
		return nil, errors.New("E_XWD_SIZE")
	}
	// All retries are in this one process, never multiple container execs.
	frame, err := ramcap.ConsistentRead(func() ([]byte, error) { return bytes.Clone(mapped), nil }, 8)
	if err != nil {
		return nil, err
	}
	if _, err = ramcap.Validate(frame, 0, 0); err != nil {
		return nil, err
	}
	return frame, nil
}

type imageData struct {
	XID, W, H, RootW, RootH                              uint32
	X, Y                                                 int32
	ByteOrder, VisualClass, RedMask, GreenMask, BlueMask uint32
	Pixels                                               []byte
}

func buildXWD(im imageData) ([]byte, error) {
	if im.XID == 0 || im.X < 0 || im.Y < 0 || im.W == 0 || im.H == 0 || im.W > 16384 || im.H > 16384 || int64(im.X)+int64(im.W) > int64(im.RootW) || int64(im.Y)+int64(im.H) > int64(im.RootH) {
		return nil, errors.New("E_XWD_GEOMETRY")
	}
	if int64(len(im.Pixels)) != int64(im.W)*int64(im.H)*4 {
		return nil, errors.New("E_XWD_SIZE")
	}
	name := []byte(fmt.Sprintf("srf-fbread xid=%d\x00", im.XID))
	fields := []uint32{100 + uint32(len(name)), 7, 2, 24, im.W, im.H, 0, im.ByteOrder, 32, im.ByteOrder, 32, 32, 4 * im.W, im.VisualClass, im.RedMask, im.GreenMask, im.BlueMask, 8, 0, 0, im.W, im.H, uint32(im.X), uint32(im.Y), 0}
	out := make([]byte, 100+len(name)+len(im.Pixels))
	for i, v := range fields {
		binary.BigEndian.PutUint32(out[i*4:], v)
	}
	copy(out[100:], name)
	copy(out[100+len(name):], im.Pixels)
	if _, err := ramcap.Validate(out, 0, 0); err != nil {
		return nil, err
	}
	return out, nil
}

type xServer struct {
	conn    *xgb.Conn
	socket  net.Conn
	setup   *xproto.SetupInfo
	root    xproto.ScreenInfo
	once    sync.Once
	pointer []byte
}

func openServer() (imageServer, error) {
	raw, err := net.DialTimeout("unix", "@/tmp/.X11-unix/X100", time.Second)
	if err != nil {
		return nil, err
	}
	if err = raw.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		raw.Close()
		return nil, err
	}
	conn, err := xgb.NewConnNet(raw)
	if err != nil {
		raw.Close()
		return nil, err
	}
	setup := xproto.Setup(conn)
	if len(setup.Roots) == 0 {
		conn.Close()
		raw.Close()
		return nil, errors.New("E_XWD_GEOMETRY")
	}
	return &xServer{conn: conn, socket: raw, setup: setup, root: setup.Roots[0]}, nil
}
func (s *xServer) Close() { s.once.Do(func() { _ = s.socket.Close(); s.conn.Close() }) }

// Renew re-arms the socket deadline. openServer arms an absolute 2 s deadline,
// right for one-shot captures; the resident stream renews it before each pair.
func (s *xServer) Renew(d time.Duration) error { return s.socket.SetDeadline(time.Now().Add(d)) }
func (s *xServer) Grab() error                 { return xproto.GrabServerChecked(s.conn).Check() }
func (s *xServer) Ungrab() error               { return xproto.UngrabServerChecked(s.conn).Check() }
func (s *xServer) Image(xid uint32) ([]byte, error) {
	if xid == 0 {
		xid = uint32(s.root.Root)
	}
	win := xproto.Window(xid)
	g, err := xproto.GetGeometry(s.conn, xproto.Drawable(win)).Reply()
	if err != nil {
		return nil, err
	}
	if g.Root != s.root.Root || g.Depth != 24 {
		return nil, errors.New("E_XWD_FORMAT")
	}
	tr, err := xproto.TranslateCoordinates(s.conn, win, s.root.Root, 0, 0).Reply()
	if err != nil {
		return nil, err
	}
	x, y := int32(tr.DstX), int32(tr.DstY)
	if !tr.SameScreen || x < 0 || y < 0 || uint32(x)+uint32(g.Width) > uint32(s.root.WidthInPixels) || uint32(y)+uint32(g.Height) > uint32(s.root.HeightInPixels) {
		return nil, errors.New("E_XWD_GEOMETRY")
	}
	image, err := xproto.GetImage(s.conn, xproto.ImageFormatZPixmap, xproto.Drawable(win), 0, 0, g.Width, g.Height, 0xffffffff).Reply()
	if err != nil {
		return nil, err
	}
	if image.Depth != 24 {
		return nil, errors.New("E_XWD_FORMAT")
	}
	// Pointer metadata is sampled separately; it is never drawn into captured pixels.
	pointer, err := xproto.QueryPointer(s.conn, s.root.Root).Reply()
	if err != nil {
		return nil, err
	}
	s.pointer = []byte(fmt.Sprintf("{\"x\":%d,\"y\":%d,\"xid\":%d}\n", pointer.RootX, pointer.RootY, xid))
	var visual *xproto.VisualInfo
	for _, depth := range s.root.AllowedDepths {
		for _, v := range depth.Visuals {
			if v.VisualId == image.Visual {
				vv := v
				visual = &vv
			}
		}
	}
	if visual == nil {
		return nil, errors.New("E_XWD_VISUAL")
	}
	formatOK := false
	for _, f := range s.setup.PixmapFormats {
		if f.Depth == 24 && f.BitsPerPixel == 32 && f.ScanlinePad == 32 {
			formatOK = true
		}
	}
	if !formatOK {
		return nil, errors.New("E_XWD_FORMAT")
	}
	return buildXWD(imageData{XID: xid, W: uint32(g.Width), H: uint32(g.Height), RootW: uint32(s.root.WidthInPixels), RootH: uint32(s.root.HeightInPixels), X: x, Y: y, ByteOrder: uint32(s.setup.ImageByteOrder), VisualClass: uint32(visual.Class), RedMask: visual.RedMask, GreenMask: visual.GreenMask, BlueMask: visual.BlueMask, Pixels: image.Data})
}

func (s *xServer) PointerMetadata() []byte { return bytes.Clone(s.pointer) }
