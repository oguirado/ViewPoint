package capture

import (
	"fmt"
	"net"
	"strings"
	"syscall"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"
)

// XFreezer freezes the server with XGrabServer+sync and thaws with XUngrabServer+sync.
// Only used against the capsule display (guarded by GuardDisplay); never constructed in tests.
type XFreezer struct {
	c *xgb.Conn
}

// DialX connects to display (already vetted by GuardDisplay).
func DialX(display string) (*XFreezer, *xgb.Conn, error) {
	c, err := xgb.NewConnDisplay(display)
	if err != nil {
		// The capsule Xvfb runs -nolisten unix: only the abstract socket exists.
		if c, err = dialAbstract(display); err != nil {
			return nil, nil, ioErr("x_dial", err)
		}
	}
	return &XFreezer{c: c}, c, nil
}

func dialAbstract(display string) (*xgb.Conn, error) {
	num := strings.TrimPrefix(display, ":")
	if i := strings.Index(num, "."); i >= 0 {
		num = num[:i]
	}
	nc, err := net.Dial("unix", fmt.Sprintf("@/tmp/.X11-unix/X%s", num))
	if err != nil {
		return nil, err
	}
	return xgb.NewConnNet(nc)
}

func (f *XFreezer) sync() error {
	_, err := xproto.GetInputFocus(f.c).Reply()
	return err
}

// Freeze implements Freezer.
func (f *XFreezer) Freeze() error {
	if err := xproto.GrabServerChecked(f.c).Check(); err != nil {
		return err
	}
	return f.sync()
}

// Thaw implements Freezer.
func (f *XFreezer) Thaw() error {
	if err := xproto.UngrabServerChecked(f.c).Check(); err != nil {
		return err
	}
	return f.sync()
}

// ReadIso reads _OPENNEWTON_ISOLATION_ID from the root window.
func ReadIso(c *xgb.Conn) (string, error) {
	root := xproto.Setup(c).DefaultScreen(c).Root
	at, err := xproto.InternAtom(c, true, uint16(len("_OPENNEWTON_ISOLATION_ID")), "_OPENNEWTON_ISOLATION_ID").Reply()
	if err != nil || at.Atom == 0 {
		return "", gate("iso_mismatch")
	}
	p, err := xproto.GetProperty(c, false, root, at.Atom, xproto.AtomString, 0, 16).Reply()
	if err != nil {
		return "", ioErr("x_getprop", err)
	}
	return string(p.Value), nil
}

// MmapFile maps path read-only and returns a ReaderAt over it plus a release func.
func MmapFile(path string) (*MmapReader, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY, 0)
	if err != nil {
		return nil, ioErr("fb_open", err)
	}
	defer syscall.Close(fd)
	var st syscall.Stat_t
	if err := syscall.Fstat(fd, &st); err != nil || st.Size <= 0 {
		return nil, ioErr("fb_stat", err)
	}
	b, err := syscall.Mmap(fd, 0, int(st.Size), syscall.PROT_READ, syscall.MAP_SHARED)
	if err != nil {
		return nil, ioErr("fb_mmap", err)
	}
	return &MmapReader{b: b}, nil
}

// MmapReader is an io.ReaderAt over an mmap of Xvfb_screen0.
type MmapReader struct{ b []byte }

// ReadAt implements io.ReaderAt.
func (m *MmapReader) ReadAt(p []byte, off int64) (int, error) {
	if off < 0 || off >= int64(len(m.b)) {
		return 0, ioErr("fb_range", nil)
	}
	n := copy(p, m.b[off:])
	if n < len(p) {
		return n, ioErr("fb_range", nil)
	}
	return n, nil
}

// Close unmaps.
func (m *MmapReader) Close() error { return syscall.Munmap(m.b) }
