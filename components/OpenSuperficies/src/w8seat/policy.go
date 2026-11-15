// Package w8seat is the host-only, non-building W8 capsule seat.
package w8seat

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/user"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"

	"opensuperficies.local/srf/src/workerbridge"
)

const CPUPath = "/etc/opensuperficies/w8_cpus.json"
const SeccompPath = "/usr/local/share/opensuperficies/w8-seccomp.json"
const SocketPath = "/run/opensuperficies/w8-seat.sock"
const MaxHeader = 1 << 20
const MaxBundle = 512 << 20
const MaxPatch = 1 << 20
const MaxArchive = 196608
const MaxText = 102400

func refusal(code int, reason, detail string) error {
	return &workerbridge.Reject{Code: code, Class: reason, Detail: detail}
}
func Normalize(raw int) int {
	if raw == 0 {
		return 0
	}
	if raw >= 2 && raw <= 7 {
		return raw
	}
	return 5
}
func ExitCode(e error) int {
	if e == nil {
		return 0
	}
	var r *workerbridge.Reject
	if errors.As(e, &r) {
		return Normalize(r.Code)
	}
	return 6
}
func hash(b []byte) string     { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func canonical(v any) []byte   { b, _ := workerbridge.Canonical(v); return b }
func obj(v any) map[string]any { m, _ := v.(map[string]any); return m }
func arr(v any) []any          { a, _ := v.([]any); return a }
func str(v any) string         { s, _ := v.(string); return s }
func number(v any) int64       { n, _ := strconv.ParseInt(fmt.Sprint(v), 10, 64); return n }
func strs(v any) []string {
	var s []string
	for _, x := range arr(v) {
		s = append(s, str(x))
	}
	return s
}

var hex64 = regexp.MustCompile(`^[a-f0-9]{64}$`)
var hex40 = regexp.MustCompile(`^[a-f0-9]{40}$`)
var id12 = regexp.MustCompile(`^[a-f0-9]{12}$`)

func ParseCPUs(s string) ([]int, error) {
	s = strings.ReplaceAll(strings.TrimSpace(s), " ", ",")
	if s == "" {
		return nil, refusal(4, "unavailable", "CPU set absent")
	}
	seen := map[int]bool{}
	var out []int
	for _, word := range strings.Split(s, ",") {
		if word == "" {
			continue
		}
		lo, hi, rangeOK := strings.Cut(word, "-")
		a, e := strconv.Atoi(lo)
		if e != nil || a < 0 || a > 65535 {
			return nil, refusal(4, "unavailable", "CPU set shape")
		}
		b := a
		if rangeOK {
			b, e = strconv.Atoi(hi)
			if e != nil || b < a || b > 65535 {
				return nil, refusal(4, "unavailable", "CPU range")
			}
		}
		for n := a; n <= b; n++ {
			if seen[n] {
				return nil, refusal(4, "unavailable", "duplicate CPU")
			}
			seen[n] = true
			out = append(out, n)
		}
	}
	sort.Ints(out)
	if len(out) == 0 {
		return nil, refusal(4, "unavailable", "CPU set empty")
	}
	return out, nil
}

type CPUConfig struct {
	Worker      []int
	Interactive []int
}

func LoadCPUConfig(path, affinity string) (CPUConfig, error) {
	var c CPUConfig
	b, e := os.ReadFile(path)
	if e != nil || len(b) > 4096 {
		return c, refusal(4, "unavailable", "operator CPU file absent")
	}
	m, e := workerbridge.Parse(b)
	if e != nil || len(m) != 2 {
		return c, refusal(4, "unavailable", "CPU file shape")
	}
	c.Worker, e = ParseCPUs(str(m["worker"]))
	if e != nil {
		return c, e
	}
	c.Interactive, e = ParseCPUs(str(m["interactive"]))
	if e != nil {
		return c, e
	}
	a, e := ParseCPUs(affinity)
	if e != nil || fmt.Sprint(a) != fmt.Sprint(c.Worker) {
		return c, refusal(4, "unavailable", "worker CPUs differ from daemon affinity")
	}
	for _, w := range c.Worker {
		for _, i := range c.Interactive {
			if i == w {
				return c, refusal(4, "unavailable", "worker/interactive CPU overlap")
			}
		}
	}
	if len(c.Worker) < 2 {
		return c, refusal(4, "unavailable", "worker CPU budget")
	}
	return c, nil
}
func cpuList(c []int) string {
	s := make([]string, len(c))
	for i, n := range c {
		s[i] = strconv.Itoa(n)
	}
	return strings.Join(s, ",")
}
func secureRead(path string, max int, root bool) ([]byte, error) {
	fd, e := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if e != nil {
		return nil, refusal(4, "unavailable", "trusted file unavailable")
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Mode().Perm()&0022 != 0 {
		return nil, refusal(4, "unavailable", "unsafe trusted file")
	}
	s, ok := st.Sys().(*syscall.Stat_t)
	if !ok || (root && s.Uid != 0) {
		return nil, refusal(4, "unavailable", "trusted file owner")
	}
	b, e := io.ReadAll(io.LimitReader(f, int64(max+1)))
	if e != nil || len(b) > max {
		return nil, refusal(4, "unavailable", "trusted file cap")
	}
	return b, nil
}

// AuthorizePeer includes supplementary groups and the kernel's primary GID.
func AuthorizePeer(uid, gid uint32, adminGID string, lookup func(uint32) ([]string, error)) error {
	if uid == 0 || adminGID == "" {
		return refusal(7, "operator_capability", "peer not permitted")
	}
	groups, e := lookup(uid)
	if e != nil {
		return refusal(7, "operator_capability", "peer groups unavailable")
	}
	groups = append(groups, strconv.FormatUint(uint64(gid), 10))
	for _, g := range groups {
		if g == adminGID {
			return nil
		}
	}
	return refusal(7, "operator_capability", "peer not admin")
}
func systemGroups(uid uint32) ([]string, error) {
	u, e := user.LookupId(strconv.FormatUint(uint64(uid), 10))
	if e != nil {
		return nil, e
	}
	return u.GroupIds()
}
func strictJSON(b []byte, v any) error {
	if _, e := workerbridge.Decode(b); e != nil {
		return refusal(2, "contract", "invalid JSON")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e := d.Decode(v); e != nil {
		return refusal(2, "contract", "unknown field or wrong shape")
	}
	return nil
}

func contract(value any, def string) error {
	b, e := workerbridge.Canonical(value)
	if e != nil {
		return e
	}
	v, e := workerbridge.Decode(b)
	if e != nil {
		return e
	}
	return workerbridge.ValidateDef(v, def)
}
