package pty

import (
	"fmt"
	"os"
	"os/exec"
	"syscall"
	"unsafe"

	"golang.org/x/sys/unix"
)

// PTY encapsulates the master and slave descriptors of a Linux pseudoterminal.
type PTY struct {
	Master *os.File
	Slave  *os.File
}

// Winsize represents terminal window dimensions in cells and pixels.
type Winsize struct {
	Rows   uint16
	Cols   uint16
	Xpixel uint16
	Ypixel uint16
}

// Open allocates a new pseudoterminal master via /dev/ptmx and acquires the
// slave peer file descriptor directly using the Linux TIOCGPTPEER ioctl.
// This avoids /dev/pts path lookup TOCTOU vulnerabilities and works across namespaces.
func Open() (*PTY, error) {
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|unix.O_CLOEXEC|unix.O_NOCTTY, 0)
	if err != nil {
		return nil, fmt.Errorf("pty: open /dev/ptmx: %w", err)
	}

	// Unlock slave pseudoterminal device (TIOCSPTLCK)
	var unlock int = 0
	_, _, errno := unix.Syscall(
		unix.SYS_IOCTL,
		master.Fd(),
		uintptr(unix.TIOCSPTLCK),
		uintptr(unsafe.Pointer(&unlock)),
	)
	if errno != 0 {
		_ = master.Close()
		return nil, fmt.Errorf("pty: unlockpt (TIOCSPTLCK): %w", errno)
	}

	// Acquire slave peer descriptor using TIOCGPTPEER
	flags := os.O_RDWR | unix.O_NOCTTY | unix.O_CLOEXEC
	r1, _, errno := unix.Syscall(
		unix.SYS_IOCTL,
		master.Fd(),
		uintptr(unix.TIOCGPTPEER),
		uintptr(flags),
	)
	if errno != 0 {
		_ = master.Close()
		return nil, fmt.Errorf("pty: TIOCGPTPEER: %w", errno)
	}

	slave := os.NewFile(r1, "pty-slave")
	return &PTY{
		Master: master,
		Slave:  slave,
	}, nil
}

// Number returns the pseudoterminal index number via TIOCGPTN.
func (p *PTY) Number() (int, error) {
	if p == nil || p.Master == nil {
		return -1, fmt.Errorf("pty: master is closed")
	}
	var ptyNum int
	_, _, errno := unix.Syscall(
		unix.SYS_IOCTL,
		p.Master.Fd(),
		uintptr(unix.TIOCGPTN),
		uintptr(unsafe.Pointer(&ptyNum)),
	)
	if errno != 0 {
		return -1, fmt.Errorf("pty: TIOCGPTN: %w", errno)
	}
	return ptyNum, nil
}

// PtsName returns the path in /dev/pts/ corresponding to the slave device.
func (p *PTY) PtsName() (string, error) {
	n, err := p.Number()
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("/dev/pts/%d", n), nil
}

// Resize updates the terminal window dimensions via TIOCSWINSZ.
func (p *PTY) Resize(cols, rows uint16) error {
	if p == nil || p.Master == nil {
		return fmt.Errorf("pty: master is closed")
	}
	ws := unix.Winsize{
		Row: rows,
		Col: cols,
	}
	return unix.IoctlSetWinsize(int(p.Master.Fd()), unix.TIOCSWINSZ, &ws)
}

// Winsize retrieves current terminal window dimensions via TIOCGWINSZ.
func (p *PTY) Winsize() (*Winsize, error) {
	if p == nil || p.Master == nil {
		return nil, fmt.Errorf("pty: master is closed")
	}
	ws, err := unix.IoctlGetWinsize(int(p.Master.Fd()), unix.TIOCGWINSZ)
	if err != nil {
		return nil, err
	}
	return &Winsize{
		Rows:   ws.Row,
		Cols:   ws.Col,
		Xpixel: ws.Xpixel,
		Ypixel: ws.Ypixel,
	}, nil
}

// Close closes both slave and master file descriptors.
func (p *PTY) Close() error {
	if p == nil {
		return nil
	}
	var errMaster, errSlave error
	if p.Slave != nil {
		errSlave = p.Slave.Close()
		p.Slave = nil
	}
	if p.Master != nil {
		errMaster = p.Master.Close()
		p.Master = nil
	}
	if errMaster != nil {
		return errMaster
	}
	return errSlave
}

// StartCmd connects standard streams to the slave PTY, configures process attributes
// (session leader and controlling terminal), and starts the command.
func (p *PTY) StartCmd(cmd *exec.Cmd) error {
	cmd.Stdin = p.Slave
	cmd.Stdout = p.Slave
	cmd.Stderr = p.Slave
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setsid = true
	cmd.SysProcAttr.Setctty = true
	cmd.SysProcAttr.Ctty = 0
	return cmd.Start()
}
