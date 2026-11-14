package w8builder

import (
	"bytes"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
)

const DefaultApprovalPath = "/etc/opensuperficies/builder_approval.json"

var approvalID = regexp.MustCompile(`^b2-[0-9]{4}$`)
var approvalNote = regexp.MustCompile(`^[A-Za-z0-9 .,:_-]{1,200}$`)

type approval struct {
	Schema       string `json:"schema"`
	Mode         string `json:"mode"`
	Image        string `json:"image"`
	Arch         string `json:"arch"`
	RecipeSHA256 string `json:"recipe_sha256"`
	ApprovalID   string `json:"approval_id"`
	OperatorNote string `json:"operator_note"`
}

func openApproval(name string) ([]byte, error) {
	fd, e := syscall.Open(name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0)
	if e != nil {
		return nil, refusal(7, "operator_recipe", "approval unavailable or symlink")
	}
	f := os.NewFile(uintptr(fd), name)
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return nil, refusal(7, "operator_recipe", "approval stat")
	}
	s, ok := st.Sys().(*syscall.Stat_t)
	if !ok || !st.Mode().IsRegular() || st.Mode().Perm() != 0644 || s.Uid != 0 || s.Gid != 0 {
		return nil, refusal(7, "operator_recipe", "approval must be root:root regular 0644")
	}
	b, e := boundedRead(f, 4096)
	if e != nil {
		return nil, refusal(7, "operator_recipe", "approval size/read")
	}
	return b, nil
}
func parseApproval(b []byte, recipeSHA string) (approval, error) {
	var a approval
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if e := dec.Decode(&a); e != nil {
		return a, refusal(7, "operator_recipe", "approval JSON")
	}
	var extra any
	if dec.Decode(&extra) != io.EOF {
		return a, refusal(7, "operator_recipe", "approval trailing data")
	}
	// Decode rejects duplicate keys, even ones a typed decoder would overwrite.
	if _, e := parseObject(b); e != nil {
		return a, refusal(7, "operator_recipe", "ambiguous approval")
	}
	if a.Schema != "os.cu.builder-approval/v1" || a.Mode != "per_build" || a.Image != "os-worker-base" || a.Arch != "aarch64" || a.RecipeSHA256 != recipeSHA || !validDigest(recipeSHA) || !approvalID.MatchString(a.ApprovalID) || !approvalNote.MatchString(a.OperatorNote) {
		return a, refusal(7, "operator_recipe", "approval does not match recipe")
	}
	return a, nil
}

type buildLock struct {
	file *os.File
	dir  string
}

func acquireLock(dir string) (*buildLock, error) {
	if dir == "" {
		return nil, refusal(2, "shape", "state directory required")
	}
	if e := os.MkdirAll(dir, 0700); e != nil {
		return nil, refusal(4, "unavailable", "state directory")
	}
	st, e := os.Lstat(dir)
	if e != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 || st.Mode().Perm()&0077 != 0 {
		return nil, refusal(5, "containment", "unsafe state directory")
	}
	s, ok := st.Sys().(*syscall.Stat_t)
	if !ok || int(s.Uid) != os.Geteuid() {
		return nil, refusal(5, "containment", "state directory owner")
	}
	f, e := stateFile(filepath.Join(dir, "builder.lock"))
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		f.Close()
		return nil, refusal(4, "unavailable", "builder lock busy")
	}
	return &buildLock{f, dir}, nil
}
func (l *buildLock) close() { _ = syscall.Flock(int(l.file.Fd()), syscall.LOCK_UN); _ = l.file.Close() }
func stateFile(p string) (*os.File, error) {
	fd, e := syscall.Open(p, syscall.O_RDWR|syscall.O_APPEND|syscall.O_CREAT|syscall.O_NOFOLLOW|syscall.O_CLOEXEC|syscall.O_NONBLOCK, 0600)
	if e != nil {
		return nil, refusal(6, "indeterminate", "state file unavailable")
	}
	f := os.NewFile(uintptr(fd), p)
	st, e := f.Stat()
	if e != nil {
		f.Close()
		return nil, e
	}
	s, ok := st.Sys().(*syscall.Stat_t)
	if !ok || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || int(s.Uid) != os.Geteuid() {
		f.Close()
		return nil, refusal(6, "indeterminate", "unsafe state file")
	}
	return f, nil
}
func appendSynced(f *os.File, b []byte, dir string) error {
	b = append(b, '\n')
	n, e := f.Write(b)
	if e != nil || n != len(b) {
		return refusal(6, "indeterminate", "state append failed")
	}
	if e = f.Sync(); e != nil {
		return refusal(6, "indeterminate", "state sync failed")
	}
	d, e := os.Open(dir)
	if e != nil {
		return refusal(6, "indeterminate", "state dir sync")
	}
	defer d.Close()
	if e = d.Sync(); e != nil {
		return refusal(6, "indeterminate", "state dir sync")
	}
	return nil
}
func (l *buildLock) consume(b []byte, recipeSHA string) (approval, string, error) {
	a, e := parseApproval(b, recipeSHA)
	if e != nil {
		return a, "", e
	}
	f, e := stateFile(filepath.Join(l.dir, "approvals.jsonl"))
	if e != nil {
		return a, "", e
	}
	defer f.Close()
	old, e := boundedRead(f, 1<<20)
	if e != nil {
		return a, "", refusal(6, "indeterminate", "approval ledger cap/read")
	}
	if len(old) > 0 && old[len(old)-1] != '\n' {
		return a, "", refusal(6, "indeterminate", "partial approval ledger")
	}
	for _, line := range strings.Split(strings.TrimSpace(string(old)), "\n") {
		if line == "" {
			continue
		}
		m, e := parseObject([]byte(line))
		if e != nil {
			return a, "", refusal(6, "indeterminate", "invalid approval ledger")
		}
		if m["approval_id"] == a.ApprovalID {
			return a, "", refusal(7, "operator_recipe", "approval already consumed")
		}
	}
	hash := digest(b)
	entry, _ := json.Marshal(map[string]string{"approval_id": a.ApprovalID, "approval_file_sha256": hash, "recipe_sha256": recipeSHA})
	if e = appendSynced(f, entry, l.dir); e != nil {
		return a, "", e
	}
	return a, hash, nil
}
