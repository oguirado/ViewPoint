package proxy

import (
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"strings"
	"syscall"
)

// decodeFile never follows a final symlink, never reads beyond the metadata
// cap, and never includes file contents in errors. Registries contain IPs only.
func decodeFile(path string, out any) error {
	fd, e := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if e != nil {
		return errors.New("metadata unavailable")
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Size() > 1<<20 {
		return errors.New("invalid metadata file")
	}
	dec := json.NewDecoder(io.LimitReader(f, (1<<20)+1))
	dec.DisallowUnknownFields()
	if e = dec.Decode(out); e != nil {
		return errors.New("invalid metadata JSON")
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return errors.New("trailing metadata JSON")
	}
	return nil
}

// ReadRegistry is the CLI's capsule-id -> source-IP interchange format. The
// writer must atomically replace the registry; readers reload every request.
func ReadRegistry(path string) (map[string]string, error) {
	var m map[string]string
	if e := decodeFile(path, &m); e != nil {
		return nil, e
	}
	if m == nil || len(m) > 4096 {
		return nil, errors.New("invalid registry size")
	}
	seen := map[string]bool{}
	for c, s := range m {
		ip := net.ParseIP(s)
		if !validCapsule(c) || ip == nil || seen[ip.String()] {
			return nil, errors.New("invalid registry entry")
		}
		seen[ip.String()] = true
	}
	return m, nil
}

type policyDocument struct {
	PrivateTargets map[string][]string         `json:"private_targets,omitempty"`
	Schema         string                      `json:"schema"`
	Approved       map[string][]string         `json:"approved"`
	Denied         []string                    `json:"denied"`
	ConnectPorts   map[string]map[string][]int `json:"connect_ports"`
}

// FilePolicy is an explicit operator-supplied adapter until browserid/egress
// lands. No file means no policy, never a permissive default. Host lists are
// exact normalized names, not wildcards or suffix matches. Reload is atomic
// when the operator replaces the file atomically.
type FilePolicy struct{ Path string }

func (p FilePolicy) load() (policyDocument, error) {
	var d policyDocument
	if e := decodeFile(p.Path, &d); e != nil {
		return d, e
	}
	if d.Schema != "browserid.proxy-policy/v1" || d.Approved == nil || d.Denied == nil {
		return d, errors.New("invalid policy schema")
	}
	check := func(s string) bool { h, e := NormalizeHost(s); return e == nil && h == s }
	for c, hosts := range d.PrivateTargets {
		if !validCapsule(c) {
			return d, errHost
		}
		for _, h := range hosts {
			if !check(h) {
				return d, errHost
			}
		}
	}
	for c, hosts := range d.Approved {
		if !validCapsule(c) {
			return d, errHost
		}
		for _, h := range hosts {
			if !check(h) {
				return d, errHost
			}
		}
	}
	for _, h := range d.Denied {
		if !check(h) {
			return d, errHost
		}
	}
	for c, hosts := range d.ConnectPorts {
		if !validCapsule(c) {
			return d, errHost
		}
		for h, ports := range hosts {
			if !check(h) {
				return d, errHost
			}
			for _, port := range ports {
				if port < 1 || port > 65535 {
					return d, errHost
				}
			}
		}
	}
	return d, nil
}
func (p FilePolicy) Validate() error { _, e := p.load(); return e }
func (p FilePolicy) Decide(cap, host string) Verdict {
	d, e := p.load()
	if e != nil {
		return Denied
	}
	for _, h := range d.Denied {
		if h == host {
			return Denied
		}
	}
	for _, h := range d.Approved[cap] {
		if h == host {
			return Approved
		}
	}
	return Pending
}
func (p FilePolicy) Account(string, string, int64) error { return nil } // Proxy owns quotas for this adapter.
func (p FilePolicy) AllowPort(cap, host string, port int) bool {
	d, e := p.load()
	if e != nil {
		return false
	}
	for _, n := range d.ConnectPorts[cap][host] {
		if n == port {
			return true
		}
	}
	return false
}

// OpenAudit requires an existing directory, creates append-only metadata at
// 0600, and refuses symlinks, devices, and group/other-readable existing files.
func OpenAudit(path string) (*os.File, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("missing audit path")
	}
	fd, e := syscall.Open(path, syscall.O_WRONLY|syscall.O_CREAT|syscall.O_APPEND|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0600)
	if e != nil {
		return nil, errors.New("audit unavailable")
	}
	f := os.NewFile(uintptr(fd), path)
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 {
		f.Close()
		return nil, errors.New("invalid audit permissions")
	}
	return f, nil
}

// AllowPrivate only grants an exact-host exception from the operator file.
// The proxy separately requires Approved, so this field cannot approve a host.
func (p FilePolicy) AllowPrivate(cap, host string) bool {
	d, e := p.load()
	if e != nil {
		return false
	}
	for _, h := range d.PrivateTargets[cap] {
		if h == host {
			return true
		}
	}
	return false
}
