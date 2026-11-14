package capture

import (
	"bufio"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
)

// DefaultRamRoot is the only place frames may be written (STORAGE LAW L2, tool=opensuperficies, campaign=os-cu-0).
const DefaultRamRoot = "/dev/shm/vqa-ramws/opensuperficies/os-cu-0"

// PathGuard refuses any frame path that is not under Root on a tmpfs/ramfs mount.
type PathGuard struct {
	Root   string
	FSType func(path string) string // "" when unknown
}

// DefaultGuard is the production guard over DefaultRamRoot and /proc/self/mountinfo.
func DefaultGuard() PathGuard { return PathGuard{Root: DefaultRamRoot, FSType: MountFSType} }

func resolve(p string) string {
	cur := filepath.Clean(p)
	rest := ""
	for {
		if r, err := filepath.EvalSymlinks(cur); err == nil {
			return filepath.Join(r, rest)
		}
		parent := filepath.Dir(cur)
		if parent == cur {
			return filepath.Clean(p)
		}
		rest = filepath.Join(filepath.Base(cur), rest)
		cur = parent
	}
}

// Check returns E_RAM_NOT_TMPFS (rc 5) unless p is inside Root (symlinks resolved) and Root is RAM-backed.
func (g PathGuard) Check(p string) error {
	if !filepath.IsAbs(p) || !filepath.IsAbs(g.Root) {
		return gate("E_RAM_NOT_TMPFS")
	}
	root := resolve(g.Root)
	full := resolve(p)
	if full != root && !strings.HasPrefix(full, root+string(filepath.Separator)) {
		return gate("E_RAM_NOT_TMPFS")
	}
	fs := ""
	if g.FSType != nil {
		fs = g.FSType(full)
	}
	if fs != "tmpfs" && fs != "ramfs" {
		return gate("E_RAM_NOT_TMPFS")
	}
	return nil
}

// CheckRunDir is Check plus the managed-run rules: the directory must be a real (non-symlink) direct child of
// Root, created by a live vision/ramws_shim.py session (owner.json present and .lock flock-held by that session).
// Unmanaged directories would never be swept by the janitor, so they are refused (rc 5 E_RAM_NOT_TMPFS).
func (g PathGuard) CheckRunDir(p string) error {
	if err := g.Check(p); err != nil {
		return err
	}
	if filepath.Dir(resolve(p)) != resolve(g.Root) {
		return gate("E_RAM_NOT_TMPFS")
	}
	if st, err := os.Lstat(p); err != nil || !st.IsDir() {
		return gate("E_RAM_NOT_TMPFS")
	}
	if st, err := os.Stat(filepath.Join(p, "owner.json")); err != nil || !st.Mode().IsRegular() {
		return gate("unmanaged_run_dir")
	}
	// O_RDONLY: flock works on it, and the capsule uid (shared via ramws) can only read the host-created .lock (AW2-01)
	lk, err := os.OpenFile(filepath.Join(p, ".lock"), os.O_RDONLY, 0)
	if err != nil {
		return gate("unmanaged_run_dir")
	}
	defer lk.Close()
	live, err := probeLive(lk)
	if err != nil || !live {
		return gate("unmanaged_run_dir") // nobody holds the session lock, or the probe itself failed
	}
	return nil
}

// probeLive is the AMEND_W2 C5 liveness probe: LOCK_SH|LOCK_NB on the (read-only) .lock, released at once and never
// held; the host janitor owns EX. A shared lock succeeds unless the host session holds LOCK_EX (EWOULDBLOCK = live).
// Any other flock error is returned, never read as "live".
func probeLive(lk *os.File) (bool, error) {
	err := syscall.Flock(int(lk.Fd()), syscall.LOCK_SH|syscall.LOCK_NB)
	if err == nil {
		return false, syscall.Flock(int(lk.Fd()), syscall.LOCK_UN)
	}
	if errors.Is(err, syscall.EWOULDBLOCK) {
		return true, nil
	}
	return false, err
}

// CheckOut refuses an evidence directory that resolves (symlinks followed) under /dev/shm or onto RAM-backed storage.
func CheckOut(p string, fsType func(string) string) error {
	if !filepath.IsAbs(p) {
		a, err := filepath.Abs(p)
		if err != nil {
			return &Error{Class: ClassUsage, Reason: "out"}
		}
		p = a
	}
	full := resolve(p)
	for _, bad := range []string{"/dev/shm", resolve("/dev/shm")} {
		if full == bad || strings.HasPrefix(full, bad+"/") {
			return &Error{Class: ClassUsage, Reason: "out_in_ram"}
		}
	}
	if fsType != nil {
		if fs := fsType(full); fs == "tmpfs" || fs == "ramfs" {
			return &Error{Class: ClassUsage, Reason: "out_in_ram"}
		}
	}
	return nil
}

// FS returns the filesystem type used for the receipt ("" if unknown).
func (g PathGuard) FS(p string) string {
	if g.FSType == nil {
		return ""
	}
	return g.FSType(resolve(p))
}

// MountFSType returns the fstype of the longest mount-point prefix of p in /proc/self/mountinfo.
func MountFSType(p string) string {
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return ""
	}
	defer f.Close()
	best, fs := "", ""
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		parts := strings.Fields(sc.Text())
		sep := -1
		for i, x := range parts {
			if x == "-" {
				sep = i
				break
			}
		}
		if sep < 0 || sep+1 >= len(parts) || len(parts) < 5 {
			continue
		}
		mp := strings.ReplaceAll(parts[4], `\040`, " ")
		if (p == mp || strings.HasPrefix(p, strings.TrimSuffix(mp, "/")+"/")) && len(mp) >= len(best) {
			best, fs = mp, parts[sep+1]
		}
	}
	return fs
}

// CapsuleProbe abstracts the container markers so tests can fake them.
type CapsuleProbe struct{ Exists func(path string) bool }

// HostProbe reads the real filesystem.
func HostProbe() CapsuleProbe {
	return CapsuleProbe{Exists: func(p string) bool { _, err := os.Stat(p); return err == nil }}
}

// InCapsule derives "running inside the capsule" from the environment (container marker files), never from a flag.
func InCapsule(pr CapsuleProbe) bool {
	return pr.Exists != nil && (pr.Exists("/.dockerenv") || pr.Exists("/run/.containerenv"))
}

var hex32 = regexp.MustCompile(`^[0-9a-f]{32}$`)

// GuardDisplay allows exactly ":100" and only inside the capsule netns; anything else is host_display (rc 5).
func GuardDisplay(display string, inCapsule bool) error {
	if display != ":100" || !inCapsule {
		return gate("host_display")
	}
	return nil
}

// GuardIso compares the root-window _OPENNEWTON_ISOLATION_ID with the expected VQA_ISO (32 hex).
func GuardIso(expected, actual string) error {
	if !hex32.MatchString(expected) || expected != actual {
		return gate("iso_mismatch")
	}
	return nil
}
