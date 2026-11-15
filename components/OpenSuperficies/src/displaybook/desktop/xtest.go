package desktop

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	"github.com/jezek/xgb"
	"github.com/jezek/xgb/xproto"
	"github.com/jezek/xgb/xtest"
)

// Common mouse button constants for XTEST synthetic events.
const (
	ButtonLeft      byte = 1
	ButtonMiddle    byte = 2
	ButtonRight     byte = 3
	ButtonWheelUp   byte = 4
	ButtonWheelDown byte = 5
)

// XTestClient encapsulates a pure-Go X11 connection with the XTEST extension initialized.
type XTestClient struct {
	Conn   *xgb.Conn
	Root   xproto.Window
	Width  uint16
	Height uint16
}

// NewXTestClient connects to the specified X display (e.g. ":100") and initializes XTEST.
// If display is empty, the DISPLAY environment variable is used.
func NewXTestClient(display string) (*XTestClient, error) {
	if display == "" {
		display = os.Getenv("DISPLAY")
	}
	if display == "" {
		display = ":100"
	}

	var c *xgb.Conn
	var err error

	c, err = xgb.NewConnDisplay(display)
	if err != nil {
		// Fallback to Linux abstract UNIX domain socket (e.g. @/tmp/.X11-unix/X100)
		dispNum := strings.TrimPrefix(display, ":")
		if dotIdx := strings.Index(dispNum, "."); dotIdx >= 0 {
			dispNum = dispNum[:dotIdx]
		}
		abstractPath := fmt.Sprintf("@/tmp/.X11-unix/X%s", dispNum)
		if netConn, netErr := net.Dial("unix", abstractPath); netErr == nil {
			c, err = xgb.NewConnNet(netConn)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("xtest: failed to connect to X display %s: %w", display, err)
	}

	if err := xtest.Init(c); err != nil {
		c.Close()
		return nil, fmt.Errorf("xtest: failed to initialize XTEST extension: %w", err)
	}

	setup := xproto.Setup(c)
	if setup == nil || len(setup.Roots) == 0 {
		c.Close()
		return nil, errors.New("xtest: failed to query X screen setup")
	}

	screen := setup.DefaultScreen(c)
	return &XTestClient{
		Conn:   c,
		Root:   screen.Root,
		Width:  screen.WidthInPixels,
		Height: screen.HeightInPixels,
	}, nil
}

// Close terminates the X connection.
func (c *XTestClient) Close() error {
	if c == nil || c.Conn == nil {
		return nil
	}
	c.Conn.Close()
	return nil
}

// MovePointer moves the pointer to absolute root coordinates (x, y).
func (c *XTestClient) MovePointer(x, y int16) error {
	if c == nil || c.Conn == nil {
		return errors.New("xtest client is nil")
	}
	return xtest.FakeInputChecked(c.Conn, xproto.MotionNotify, 0, 0, c.Root, x, y, 0).Check()
}

// ButtonDown sends a mouse button press event for the given button (1=Left, 2=Middle, 3=Right).
func (c *XTestClient) ButtonDown(button byte) error {
	if c == nil || c.Conn == nil {
		return errors.New("xtest client is nil")
	}
	return xtest.FakeInputChecked(c.Conn, xproto.ButtonPress, button, 0, c.Root, 0, 0, 0).Check()
}

// ButtonUp sends a mouse button release event for the given button.
func (c *XTestClient) ButtonUp(button byte) error {
	if c == nil || c.Conn == nil {
		return errors.New("xtest client is nil")
	}
	return xtest.FakeInputChecked(c.Conn, xproto.ButtonRelease, button, 0, c.Root, 0, 0, 0).Check()
}

// Click moves the pointer to (x, y) and performs a click (down then up).
func (c *XTestClient) Click(x, y int16, button byte) error {
	if err := c.MovePointer(x, y); err != nil {
		return err
	}
	if err := c.ButtonDown(button); err != nil {
		return err
	}
	time.Sleep(10 * time.Millisecond)
	return c.ButtonUp(button)
}

// KeyDown sends an XTEST KeyPress event for the given keycode.
func (c *XTestClient) KeyDown(keycode byte) error {
	if c == nil || c.Conn == nil {
		return errors.New("xtest client is nil")
	}
	return xtest.FakeInputChecked(c.Conn, xproto.KeyPress, keycode, 0, c.Root, 0, 0, 0).Check()
}

// KeyUp sends an XTEST KeyRelease event for the given keycode.
func (c *XTestClient) KeyUp(keycode byte) error {
	if c == nil || c.Conn == nil {
		return errors.New("xtest client is nil")
	}
	return xtest.FakeInputChecked(c.Conn, xproto.KeyRelease, keycode, 0, c.Root, 0, 0, 0).Check()
}

// TypeKey sends KeyPress followed by KeyRelease for the given keycode.
func (c *XTestClient) TypeKey(keycode byte) error {
	if err := c.KeyDown(keycode); err != nil {
		return err
	}
	time.Sleep(5 * time.Millisecond)
	return c.KeyUp(keycode)
}

// QueryWindows returns the list of child windows of the root window.
func (c *XTestClient) QueryWindows() ([]xproto.Window, error) {
	if c == nil || c.Conn == nil {
		return nil, errors.New("xtest client is nil")
	}
	tree, err := xproto.QueryTree(c.Conn, c.Root).Reply()
	if err != nil {
		return nil, fmt.Errorf("xtest: failed to query window tree: %w", err)
	}
	return tree.Children, nil
}

// GetWindowGeometry returns the geometry (x, y, width, height) of the given window.
func (c *XTestClient) GetWindowGeometry(win xproto.Window) (*xproto.GetGeometryReply, error) {
	if c == nil || c.Conn == nil {
		return nil, errors.New("xtest client is nil")
	}
	return xproto.GetGeometry(c.Conn, xproto.Drawable(win)).Reply()
}

// GetWindowName reads the WM_NAME string property of the given window.
func (c *XTestClient) GetWindowName(win xproto.Window) (string, error) {
	if c == nil || c.Conn == nil {
		return "", errors.New("xtest client is nil")
	}
	prop, err := xproto.GetProperty(c.Conn, false, win, xproto.AtomWmName, xproto.GetPropertyTypeAny, 0, 1024).Reply()
	if err != nil {
		return "", fmt.Errorf("xtest: failed to get WM_NAME property: %w", err)
	}
	if prop == nil || len(prop.Value) == 0 {
		return "", nil
	}
	return string(prop.Value), nil
}

// GetWindowPID queries the _NET_WM_PID property on a window to retrieve the client PID.
func (c *XTestClient) GetWindowPID(win xproto.Window) (uint32, error) {
	if c == nil || c.Conn == nil {
		return 0, errors.New("xtest client is nil")
	}
	atomName := "_NET_WM_PID"
	atomReply, err := xproto.InternAtom(c.Conn, false, uint16(len(atomName)), atomName).Reply()
	if err != nil {
		return 0, fmt.Errorf("xtest: failed to intern %s atom: %w", atomName, err)
	}

	prop, err := xproto.GetProperty(c.Conn, false, win, atomReply.Atom, xproto.AtomCardinal, 0, 1).Reply()
	if err != nil {
		return 0, fmt.Errorf("xtest: failed to get _NET_WM_PID: %w", err)
	}
	if prop == nil || len(prop.Value) < 4 {
		return 0, fmt.Errorf("xtest: _NET_WM_PID property empty or too short")
	}

	pid := binary.LittleEndian.Uint32(prop.Value[:4])
	return pid, nil
}

// QueryPointer returns the current coordinates of the pointer on the root window.
func (c *XTestClient) QueryPointer() (int16, int16, error) {
	if c == nil || c.Conn == nil {
		return 0, 0, errors.New("xtest client is nil")
	}
	reply, err := xproto.QueryPointer(c.Conn, c.Root).Reply()
	if err != nil {
		return 0, 0, fmt.Errorf("xtest: failed to query pointer: %w", err)
	}
	return reply.RootX, reply.RootY, nil
}

// ScreenDimensions returns the width and height of the default screen.
func (c *XTestClient) ScreenDimensions() (uint16, uint16) {
	if c == nil {
		return 0, 0
	}
	return c.Width, c.Height
}
