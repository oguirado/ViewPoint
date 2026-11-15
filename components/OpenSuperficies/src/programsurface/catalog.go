package programsurface

import (
	"path"
	"regexp"
	"strings"
)

// Kept in sync with the surface display by TestDisplayGeometryMatchesSurface.
const (
	displayWidth  = 1280
	displayHeight = 900
)

var (
	programIDRE      = regexp.MustCompile(`^[a-z][a-z0-9-]{1,23}$`)
	imageRE          = regexp.MustCompile(`^opensuperficies-program-[a-z0-9-]+-arm64:[0-9]+\.[0-9]+\.[0-9]+$`)
	vmImageRE        = regexp.MustCompile(`^opensuperficies-vm-arm64:[0-9]+\.[0-9]+\.[0-9]+$`)
	vmCapsuleRE      = regexp.MustCompile(`^srf-vm-[a-z0-9][a-z0-9-]{0,39}$`)
	programCapsuleRE = regexp.MustCompile(`^srf-pg-[a-z0-9][a-z0-9-]{0,39}$`)
)

// Values are copied on lookup; callers cannot mutate the compiled catalog.
var catalog = map[string]Program{
	"metaviewer-nested": {ID: "metaviewer-nested", Image: "opensuperficies-program-electron-arm64:0.1.0", Network: NetNone, SeccompIdentity: true, ClearSingleton: true, MinWidth: 640, MinHeight: 450, MaxWindows: 1, FixedWidth: 1280, FixedHeight: 720, ExchangeDir: ExchangeDir, A11y: true, Enabled: true},
	"notepad-vm":        {ID: "notepad-vm", Image: "opensuperficies-vm-arm64:0.1.0", Network: NetNone, Runtime: RuntimeVM, MemoryMiB: 8192, KVM: true, GuestCPUs: 4, GuestMemMiB: 6144, ProfileDiskMiB: 1024, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir, A11y: true, Enabled: true},
	"notepad":           {ID: "notepad", Image: "opensuperficies-program-gtk-arm64:0.1.0", Network: NetNone, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir, A11y: true, Enabled: true},
	"mousepad":          {ID: "mousepad", Image: "opensuperficies-program-office-arm64:0.1.0", Network: NetNone, MinWidth: 400, MinHeight: 300, MaxWindows: 4, ExchangeDir: ExchangeDir, A11y: true, Enabled: false},
}

func lookup(id string) (Program, error) {
	p, ok := catalog[id]
	if !ok || !p.Enabled {
		return Program{}, &Error{2, "AUSENTE — unknown or disabled program"}
	}
	if err := p.Validate(); err != nil {
		return Program{}, err
	}
	return p, nil
}

func validate(p Program) error {
	if !programIDRE.MatchString(p.ID) {
		return &Error{3, "invalid program id"}
	}
	switch p.Runtime {
	case RuntimeVM:
		if !vmImageRE.MatchString(p.Image) || !p.KVM || p.Network != NetNone || p.GuestCPUs < 1 || p.GuestCPUs > 5 || p.MemoryMiB < 1536 || p.MemoryMiB > 8192 || p.GuestMemMiB < 512 || p.GuestMemMiB > p.MemoryMiB-1024 || p.ProfileDiskMiB < 64 || p.ProfileDiskMiB > 4096 {
			return &Error{3, "invalid VM program configuration"}
		}
	case "", RuntimeContainer:
		if p.KVM || p.MemoryMiB != 0 || p.GuestCPUs != 0 || p.GuestMemMiB != 0 || p.ProfileDiskMiB != 0 {
			return &Error{3, "invalid container VM fields"}
		}
	default:
		return &Error{3, "invalid program runtime"}
	}
	if p.Runtime != RuntimeVM && !imageRE.MatchString(p.Image) {
		return &Error{3, "invalid program image"}
	}
	if p.Network != NetNone && p.Network != NetProxy {
		return &Error{3, "invalid program network"}
	}
	if p.MaxWindows < 1 || p.MaxWindows > 16 {
		return &Error{3, "invalid program window limit"}
	}
	if p.MinWidth < 200 || p.MinHeight < 200 || p.MinWidth > displayWidth || p.MinHeight > displayHeight {
		return &Error{3, "invalid program minimum geometry"}
	}
	if p.FixedWidth != 0 || p.FixedHeight != 0 {
		if p.FixedWidth < p.MinWidth || p.FixedWidth > displayWidth || p.FixedHeight < p.MinHeight || p.FixedHeight > displayHeight || p.MaxWindows != 1 {
			return &Error{3, "invalid program fixed geometry"}
		}
	}
	if !path.IsAbs(p.ExchangeDir) || !strings.HasPrefix(p.ExchangeDir, "/profile/") || strings.Contains(p.ExchangeDir, "..") || strings.ContainsRune(p.ExchangeDir, 0) || path.Clean(p.ExchangeDir) == "/profile" {
		return &Error{3, "invalid program exchange directory"}
	}
	return nil
}

func validVMCapsule(name string) bool { return vmCapsuleRE.MatchString(name) }
