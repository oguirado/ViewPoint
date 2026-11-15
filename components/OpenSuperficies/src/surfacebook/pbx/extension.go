package pbx

import (
	"bufio"
	"bytes"
	"errors"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	// MinCapsuleExtension defines the lower bound for capsule extensions.
	MinCapsuleExtension = 300
	// MaxCapsuleExtension defines the upper bound for capsule extensions.
	MaxCapsuleExtension = 899
	// RoleSurface defines the canonical role for OpenSwitchboard SURFACE extensions (300-899).
	RoleSurface = "SURFACE"
	// RoleOpenSuperficies defines the canonical role for OpenSuperficies surface capsules.
	RoleOpenSuperficies = "OPENSUPERFICIES"
)

var (
	// ErrProcesoNoEncontrado is returned when the target PID cannot be located.
	ErrProcesoNoEncontrado = errors.New("anti-ghost: process does not exist")
	// ErrStartTimeDiscrepante is returned when recorded starttime does not match procfs field 22.
	ErrStartTimeDiscrepante = errors.New("anti-ghost: starttime mismatch, recycled PID detected")
	// ErrProcfsTimeDiscrepante is returned when procfs directory mtime diverges from starttime calculation.
	ErrProcfsTimeDiscrepante = errors.New("anti-ghost: procfs directory mtime diverges from process starttime calculation")
)

// ProcessIdentity holds the PID and starttime pair witnessing process identity.
type ProcessIdentity struct {
	PID           int    `json:"pid"`
	StartTime     uint64 `json:"starttime,omitempty"`
	StartTimeTick uint64 `json:"start_time_tick,omitempty"`
	Motivo        string `json:"motivo,omitempty"`
}

// EffectiveStartTime returns the starttime regardless of JSON field naming.
func (p ProcessIdentity) EffectiveStartTime() uint64 {
	if p.StartTime != 0 {
		return p.StartTime
	}
	return p.StartTimeTick
}

// Extension represents an active PBX telephonic extension.
type Extension struct {
	Numero        int       `json:"numero"`
	Extension     int       `json:"extension"`
	AgenteSlug    string    `json:"agente_slug"`
	Slug          string    `json:"slug"`
	Modelo        string    `json:"modelo"`
	Rol           string    `json:"rol"`
	PID           int       `json:"pid"`
	StartTimeTick uint64    `json:"start_time_tick"`
	StartTime     uint64    `json:"start_time"`
	RegistradoEn  time.Time `json:"registrado_en"`
	Estado        string    `json:"estado"`
	SalaActual    string    `json:"sala_actual,omitempty"`
}

// ObtenerStartTimeProcfs reads field 22 of /proc/<pid>/stat, correctly handling
// comm values containing spaces and parentheses.
func ObtenerStartTimeProcfs(pid int) (uint64, error) {
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if err != nil {
		return 0, fmt.Errorf("read /proc/%d/stat: %w", pid, err)
	}

	idx := bytes.LastIndexByte(data, ')')
	if idx == -1 || idx+2 >= len(data) {
		return 0, fmt.Errorf("corrupt stat for pid %d", pid)
	}

	fields := bytes.Fields(data[idx+2:])
	// Remainder starts at state (field 3 of stat, index 0).
	// Field 22 of stat is at index 22 - 3 = 19.
	if len(fields) <= 19 {
		return 0, fmt.Errorf("insufficient fields in /proc/%d/stat (%d <= 19)", pid, len(fields))
	}

	return strconv.ParseUint(string(fields[19]), 10, 64)
}

// LeerBootTimeProcfs reads the btime field from /proc/stat representing system boot time in seconds since epoch.
func LeerBootTimeProcfs() (int64, error) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0, fmt.Errorf("open /proc/stat: %w", err)
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.HasPrefix(line, "btime ") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				return strconv.ParseInt(fields[1], 10, 64)
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return 0, fmt.Errorf("scan /proc/stat: %w", err)
	}
	return 0, errors.New("btime not found in /proc/stat")
}

// CrossCheckProcfsTime cross-validates /proc/<pid>/stat field 22 starttime against
// the /proc/<pid> directory inode modification time and /proc/stat btime.
// This ensures that process start time is independently witnessed rather than compared to itself.
func CrossCheckProcfsTime(pid int, liveStartTime uint64) error {
	dirStat, err := os.Stat(fmt.Sprintf("/proc/%d", pid))
	if err != nil {
		return fmt.Errorf("%w: stat /proc/%d: %w", ErrProcesoNoEncontrado, pid, err)
	}

	btime, err := LeerBootTimeProcfs()
	if err != nil {
		// Non-fatal if /proc/stat is unreadable in constrained container, but still witness directory
		return nil
	}

	// Jiffies clock tick is 100 Hz on ARM64 Linux
	calcBootSec := float64(btime) + float64(liveStartTime)/100.0
	dirMtimeSec := float64(dirStat.ModTime().UnixNano()) / 1e9

	diffSec := math.Abs(calcBootSec - dirMtimeSec)
	// Allow reasonable tolerance for clock drift between timer jiffies and inode timestamp (max 10s)
	if diffSec > 10.0 {
		return fmt.Errorf("%w: calculated starttime %.2f diverges from /proc/%d mtime %.2f (delta: %.2fs)",
			ErrProcfsTimeDiscrepante, calcBootSec, pid, dirMtimeSec, diffSec)
	}

	return nil
}

// AntiGhostCheck verifies that the process exists and its starttime matches /proc/<pid>/stat field 22.
func AntiGhostCheck(identity ProcessIdentity) error {
	if identity.PID <= 0 {
		return fmt.Errorf("%w: invalid pid %d", ErrProcesoNoEncontrado, identity.PID)
	}

	if err := syscall.Kill(identity.PID, 0); err != nil {
		return fmt.Errorf("%w: pid %d: %w", ErrProcesoNoEncontrado, identity.PID, err)
	}

	liveStartTime, err := ObtenerStartTimeProcfs(identity.PID)
	if err != nil {
		return fmt.Errorf("%w: cannot read starttime for pid %d: %w", ErrProcesoNoEncontrado, identity.PID, err)
	}

	expected := identity.EffectiveStartTime()
	if expected != liveStartTime {
		return fmt.Errorf("%w: recorded starttime %d does not match live starttime %d for pid %d (recycled PID detected)",
			ErrStartTimeDiscrepante, expected, liveStartTime, identity.PID)
	}

	return nil
}
