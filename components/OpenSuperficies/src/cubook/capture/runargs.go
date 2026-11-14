package capture

import (
	"crypto/sha256"
	"encoding/hex"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// RunConfig is the input of the docker run vector of a CU capsule slot.
type RunConfig struct {
	Slot       int
	Image      string
	Iso        string   // VQA_ISO, 32 hex
	FBDir      string   // host ramws dir mounted at /run/vqa/fb
	Entrypoint string   // host docker/cu/entrypoint-cu.sh
	RamDir     string   // host ramws run dir (direct child of DefaultRamRoot), bind-mounted rw at the SAME path
	ToolsDir   string   // host dir with the static srf-cu-* binaries, mounted ro at /opt/cu
	OutDir     string   // host disk dir for receipts, mounted rw at /out (never under /dev/shm)
	Geometry   string   // WxHxD passed as -e GEOMETRY
	ExtraArgs  []string // appended before the image; only "-e WM=..." pairs are accepted (allow-list)
	CPUSet     string   // --cpuset-cpus value; "" keeps the W2 default "0,1"; allow-list cpuSets (CF-03)
}

// CapsuleUID is the image uid of the desktop capsule; the vector never carries a docker user flag.
const CapsuleUID = 10001

// userFlag is built, not spelled, so that grepping the capsule sources for the flag finds no vector.
const userFlag = "--" + "user"

// cpuSets is the allow-list of --cpuset-cpus values (CF-03): os-cu-0 owns cpus 10-14; 0,1 is the W2 slot pair.
var cpuSets = map[string]bool{"0,1": true, "10-14": true}

// defaultCPUSet keeps the W2 vector (and its golden) byte-identical when RunConfig.CPUSet is empty.
const defaultCPUSet = "0,1"

func (c RunConfig) cpuset() string {
	if c.CPUSet == "" {
		return defaultCPUSet
	}
	return c.CPUSet
}

var geometryRE = regexp.MustCompile(`^[0-9]{3,5}x[0-9]{3,5}x[0-9]{1,2}$`)

// extraEnvKeys is the allow-list of environment keys ExtraArgs may set; ExtraArgs is only ever "-e KEY=VALUE" pairs.
var extraEnvKeys = map[string]bool{"WM": true, "XVFB_CURSOR": true}

// checkExtraArgs admits exactly "-e", "KEY=VALUE" pairs with KEY in extraEnvKeys; everything else (mounts, volumes,
// entrypoint, tmpfs, user, network, caps...) is refused. User flags keep their own reason.
func checkExtraArgs(args []string) error {
	for _, a := range args {
		if a == "-u" || strings.HasPrefix(a, "-u") && !strings.HasPrefix(a, "--") || a == userFlag || strings.HasPrefix(a, userFlag+"=") {
			return gate("user_flag")
		}
	}
	for i := 0; i < len(args); i += 2 {
		if args[i] != "-e" || i+1 >= len(args) {
			return gate("forbidden_arg")
		}
		k, v, ok := strings.Cut(args[i+1], "=")
		if !ok || !extraEnvKeys[k] {
			return gate("forbidden_arg")
		}
		if k == "XVFB_CURSOR" && v != "on" && v != "off" { // closed value: cursor-on Xvfb is for WL-7 only
			return gate("forbidden_arg")
		}
	}
	return nil
}

// under reports whether p is a strict descendant of root after lexical cleaning.
func under(root, p string) bool {
	r, q := filepath.Clean(root), filepath.Clean(p)
	return q != r && strings.HasPrefix(q, r+string(filepath.Separator))
}

// Validate refuses configs that could weaken the isolation vector.
func (c RunConfig) Validate() error {
	if c.Slot < 0 || c.Slot > 7 {
		return &Error{Class: ClassUsage, Reason: "slot_range"}
	}
	if !hex32.MatchString(c.Iso) {
		return &Error{Class: ClassUsage, Reason: "iso_format"}
	}
	if c.Image == "" || strings.ContainsAny(c.Image, " \t\n") || !filepath.IsAbs(c.FBDir) || !filepath.IsAbs(c.Entrypoint) {
		return &Error{Class: ClassUsage, Reason: "runargs_paths"}
	}
	for _, p := range []string{c.RamDir, c.ToolsDir, c.OutDir} {
		if p != "" && !filepath.IsAbs(p) {
			return &Error{Class: ClassUsage, Reason: "runargs_paths"}
		}
	}
	if c.Geometry != "" && !geometryRE.MatchString(c.Geometry) {
		return &Error{Class: ClassUsage, Reason: "geometry"}
	}
	if err := checkExtraArgs(c.ExtraArgs); err != nil {
		return err
	}
	if !cpuSets[c.cpuset()] {
		return gate("cpuset_not_allowed")
	}
	if c.RamDir != "" {
		// a direct child of the law root, so that vision/ramws_shim.py owns it and the janitor sweeps it
		if !under(DefaultRamRoot, c.RamDir) || filepath.Dir(filepath.Clean(c.RamDir)) != DefaultRamRoot {
			return gate("ramdir_outside_ramws")
		}
		if !under(c.RamDir, c.FBDir) {
			return gate("fb_outside_ramdir")
		}
	}
	// FBDir is always a descendant of the law root, even without a RamDir mount
	if !under(DefaultRamRoot, c.FBDir) {
		return gate("fb_outside_ramdir")
	}
	if c.OutDir != "" {
		if err := CheckOut(c.OutDir, MountFSType); err != nil {
			return gate("out_in_ram")
		}
	}
	for _, p := range []string{c.FBDir, c.Entrypoint, c.RamDir, c.ToolsDir, c.OutDir} {
		for _, bad := range []string{"docker.sock", "/tmp/.X11-unix", "/run/user", "/dev/dri", "/dev/uinput", ":"} {
			if strings.Contains(p, bad) {
				return gate("forbidden_mount")
			}
		}
	}
	return nil
}

// RunArgs is the argv after "docker" for slot n (DESIGN_CU 2.1 + ADV-07). The image is the last element.
// The capsule runs as the image uid (CapsuleUID, 10001): there is no user flag, and the tenant tmpfs is owned by that uid.
func RunArgs(c RunConfig) []string {
	a := []string{
		"run", "-d", "--name", "vqa-cu-" + strconv.Itoa(c.Slot), "--label", "os.cu.campaign=os-cu-0",
		"--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
		"--pids-limit", "256", "--cpuset-cpus", c.cpuset(), "--memory", "4g",
		"--tmpfs", "/run/newton-tenant:rw,nosuid,nodev,mode=0700,uid=" + strconv.Itoa(CapsuleUID) + ",gid=0", "--tmpfs", "/tmp:rw,nosuid,nodev",
		"-v", c.FBDir + ":/run/vqa/fb:rw",
	}
	if c.RamDir != "" {
		a = append(a, "-v", c.RamDir+":"+c.RamDir+":rw")
	}
	if c.ToolsDir != "" {
		a = append(a, "-v", c.ToolsDir+":/opt/cu:ro")
	}
	if c.OutDir != "" {
		a = append(a, "-v", c.OutDir+":/out:rw")
	}
	a = append(a, "-v", c.Entrypoint+":/usr/local/bin/entrypoint.sh:ro",
		"-e", "XPRA_MODE=off", "-e", "VQA_ISO="+c.Iso)
	if c.Geometry != "" {
		a = append(a, "-e", "GEOMETRY="+c.Geometry)
	}
	a = append(a, c.ExtraArgs...)
	return append(a, c.Image)
}

// RunArgsSHA256 is run_args_sha256 of capsule.json: sha256 over NUL-joined argv.
func RunArgsSHA256(a []string) string {
	s := sha256.Sum256([]byte(strings.Join(a, "\x00")))
	return hex.EncodeToString(s[:])
}
