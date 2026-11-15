// Package programsurface holds the program kind of the capsule surface: a compiled-in catalog of
// desktop programs and the pure launch/readiness helpers that src/browserid/surface uses for
// `start --kind program`. It imports nothing from src/browserid (no import cycle).
//
// FROZEN API (os-vm-0 P1 design §10, reviewed by os-cu-0). Lane PKG implements the bodies; the
// names, types and signatures below must not change without a new review.
package programsurface

// ReadyToken prefixes the readiness line printed by the program entrypoint. The JSON after it has
// the same shape as IDENTITY_READY: {"windows":[{"id","x","y","w","h","xid"}]}.
const ReadyToken = "PROGRAM_READY"

// CapsulePrefix starts every program capsule name: ^srf-pg-[a-z0-9][a-z0-9-]{0,39}$.
const CapsulePrefix = "srf-pg-"

// ExchangeDir is the default folder for file exchange inside a program capsule.
const ExchangeDir = "/profile/exchange"

type Network string

// Runtime selects how the in-capsule contract is provided.
type Runtime string

const (
	RuntimeContainer Runtime = "container"
	RuntimeVM        Runtime = "vm"
)

// VMCapsulePrefix starts every vm capsule name: ^srf-vm-[a-z0-9][a-z0-9-]{0,39}$.
const VMCapsulePrefix = "srf-vm-"

const (
	NetNone  Network = "none"
	NetProxy Network = "proxy"
)

type Program struct {
	ID                  string // ^[a-z][a-z0-9-]{1,23}$
	Image               string
	Network             Network
	SeccompIdentity     bool // true: identity seccomp profile (Chromium/Electron); false: Docker default
	ClearSingleton      bool // run the Chromium-style singleton lock cleanup before start
	MinWidth, MinHeight int
	MaxWindows          int    // 1..16
	ExchangeDir         string // absolute path under /profile
	A11y                bool   // AT-SPI bridge on (P2)
	Enabled             bool   // false: catalogued but refused at start

	// W3 (frozen, additive; os-vm-0 W3 design §4 and §10.4). Zero values mean Runtime container.
	Runtime        Runtime // RuntimeContainer or RuntimeVM
	MemoryMiB      int     // container --memory/--memory-swap (vm: 8192, profile M)
	KVM            bool    // vm only: pass exactly /dev/kvm (+ --group-add kvm gid); no other device
	GuestCPUs      int     // vm only: QEMU -smp
	GuestMemMiB    int     // vm only: QEMU -m
	ProfileDiskMiB int     // vm only: size of /profile/disk.ext4

	// Electron capsule (frozen, additive): fixed window geometry at 0,0 instead of the tiling plan.
	// Both zero = tiling plan; both set = MaxWindows must be 1 and the window is FixedWidth x FixedHeight.
	FixedWidth  int
	FixedHeight int
}

type Tile struct {
	ID         string
	X, Y, W, H int
}

type ReadyWindow struct {
	ID  string `json:"id"`
	X   int    `json:"x"`
	Y   int    `json:"y"`
	W   int    `json:"w"`
	H   int    `json:"h"`
	XID string `json:"xid"`
}

// Error carries an exit code in 2..7 (never 1).
type Error struct {
	Code   int
	Detail string
}

func (e *Error) Error() string { return e.Detail }

// Lookup returns the catalog entry; an unknown or disabled id is an *Error with code 2.
func Lookup(id string) (Program, error) {
	return lookup(id)
}

// Validate self-checks one catalog entry (code 3 on an invalid entry).
func (p Program) Validate() error { return validate(p) }

// ValidCapsule reports whether name is a valid program capsule name.
func ValidCapsule(name string) bool { return programCapsuleRE.MatchString(name) }

// RunArgs returns the full `docker run ...` argv of a program capsule with the same hardening as
// surface.RunArgs. proxy is "" when p.Network == NetNone (then --network none and no
// SRF_EGRESS_PROXY). It never contains typed text or profile contents.
func RunArgs(container, profile, viewDir string, p Program, tiles []Tile, proxy string) []string {
	return runArgs(container, profile, viewDir, p, tiles, proxy)
}

// ParseReady parses one PROGRAM_READY line against the planned tiles; code 6 on malformed JSON,
// a duplicate XID or a tile mismatch.
func ParseReady(line string, tiles []Tile) ([]ReadyWindow, error) {
	return parseReady(line, tiles)
}
