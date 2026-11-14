package main

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"unicode/utf8"
)

func extract(dir string, out io.Writer, writePaths []string) error {
	fd, e := syscall.Open(dir, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if e != nil {
		return e
	}
	root := os.NewFile(uintptr(fd), dir)
	defer root.Close()
	names, e := root.Readdirnames(entryCap + 1)
	if e != nil && e != io.EOF {
		return e
	}
	if len(names) == 0 || len(names) > entryCap {
		return errors.New("entry cap")
	}
	sort.Strings(names)
	var archive bytes.Buffer
	tw := tar.NewWriter(&archive)
	total := 0
	for _, name := range names {
		if len(name) > 100 || strings.ContainsAny(name, "/\\") || !safePath(name) {
			return errors.New("flat names required")
		}
		child, e := syscall.Openat(fd, name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
		if e != nil {
			return e
		}
		f := os.NewFile(uintptr(child), name)
		info, e := f.Stat()
		if e != nil {
			f.Close()
			return e
		}
		if !info.Mode().IsRegular() || info.Size() <= 0 || info.Size() > evidenceCap || int64(total)+info.Size() > evidenceCap {
			f.Close()
			return errors.New("entry size/type")
		}
		b, e := io.ReadAll(io.LimitReader(f, info.Size()+1))
		f.Close()
		if e != nil {
			return e
		}
		if int64(len(b)) != info.Size() {
			return errors.New("entry changed")
		}
		if e = validText(name, b, writePaths); e != nil {
			return e
		}
		total += len(b)
		if e = tw.WriteHeader(&tar.Header{Name: name, Mode: 0644, Size: int64(len(b)), Typeflag: tar.TypeReg, Format: tar.FormatUSTAR}); e != nil {
			return e
		}
		if _, e = tw.Write(b); e != nil {
			return e
		}
	}
	if e = tw.Close(); e != nil {
		return e
	}
	if archive.Len() > archiveCap {
		return errors.New("archive cap")
	}
	_, e = io.Copy(out, &archive)
	return e
}

// Never block on, follow, or allocate from a task-controlled metadata file.
func readRegular(name string, cap int64) ([]byte, error) {
	fd, e := syscall.Open(name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), name)
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return nil, e
	}
	if !st.Mode().IsRegular() || st.Size() > cap {
		return nil, errors.New("metadata size/type")
	}
	b, e := io.ReadAll(io.LimitReader(f, cap+1))
	if e != nil {
		return nil, e
	}
	if int64(len(b)) > cap {
		return nil, errors.New("metadata cap")
	}
	return b, nil
}

type source struct {
	Path   string  `json:"path"`
	SHA256 *string `json:"sha256"`
}
type sourceObserved struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}
type evidenceRequest struct {
	RequestID string `json:"request_id"`
	Kind      string `json:"kind"`
	Path      string `json:"path,omitempty"`
	Line      int    `json:"line,omitempty"`
	Text      string `json:"text,omitempty"`
}
type answer struct {
	RequestID string `json:"request_id"`
	Answer    string `json:"answer"`
}
type changedFile struct {
	Path      string  `json:"path"`
	Operation string  `json:"operation"`
	Mode      string  `json:"mode"`
	SHA256    *string `json:"sha256"`
	Bytes     int     `json:"bytes"`
}
type receiptData struct {
	Files           []changedFile    `json:"files"`
	SourcesObserved []sourceObserved `json:"sources_observed"`
	Answers         []answer         `json:"answers"`
}

func sha(b []byte) string { sum := sha256.Sum256(b); return hex.EncodeToString(sum[:]) }
func (r runner) repoFile(name string) ([]byte, os.FileMode, error) {
	if !safePath(name) {
		return nil, 0, errors.New("repository path")
	}
	root, e := syscall.Open(filepath.Join(r.root, "repo"), syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
	if e != nil {
		return nil, 0, e
	}
	parts := strings.Split(name, "/")
	fd := root
	for _, part := range parts[:len(parts)-1] {
		next, e := syscall.Openat(fd, part, syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0)
		if fd != root {
			syscall.Close(fd)
		}
		if e != nil {
			syscall.Close(root)
			return nil, 0, e
		}
		fd = next
	}
	file, e := syscall.Openat(fd, parts[len(parts)-1], syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if fd != root {
		syscall.Close(fd)
	}
	syscall.Close(root)
	if e != nil {
		return nil, 0, e
	}
	f := os.NewFile(uintptr(file), name)
	defer f.Close()
	st, e := f.Stat()
	if e != nil {
		return nil, 0, e
	}
	if !st.Mode().IsRegular() || st.Size() > 16<<20 {
		return nil, 0, errors.New("repository file type/size")
	}
	b, e := io.ReadAll(io.LimitReader(f, 16<<20+1))
	if e != nil {
		return nil, 0, e
	}
	if len(b) > 16<<20 || int64(len(b)) != st.Size() {
		return nil, 0, errors.New("repository read cap")
	}
	return b, st.Mode(), nil
}
func (r runner) observeSources(sources []source) ([]sourceObserved, error) {
	observed := make([]sourceObserved, 0, len(sources))
	seen := map[string]bool{}
	for _, src := range sources {
		if seen[src.Path] {
			return nil, errors.New("duplicate source")
		}
		seen[src.Path] = true
		b, _, e := r.repoFile(src.Path)
		if e != nil {
			return nil, e
		}
		hash := sha(b)
		if src.SHA256 != nil && *src.SHA256 != hash {
			return nil, errors.New("source pin mismatch")
		}
		observed = append(observed, sourceObserved{Path: src.Path, SHA256: hash})
	}
	return observed, nil
}
func (r runner) receiptData(req ingestRequest, delta []byte, observed []sourceObserved) error {
	record := receiptData{Files: []changedFile{}, SourcesObserved: observed, Answers: []answer{}}
	if record.SourcesObserved == nil {
		record.SourcesObserved = []sourceObserved{}
	}
	if len(delta) > 0 {
		if e := checkPatch(delta, req.WritePaths); e != nil {
			return e
		}
		var current *changedFile
		for _, line := range strings.Split(string(delta), "\n") {
			if strings.HasPrefix(line, "diff --git ") {
				fields := strings.Fields(line)
				record.Files = append(record.Files, changedFile{Path: strings.TrimPrefix(fields[3], "b/"), Operation: "modify", Mode: "100644"})
				current = &record.Files[len(record.Files)-1]
			}
			if current == nil {
				continue
			}
			if strings.HasPrefix(line, "new file mode ") {
				current.Operation = "add"
				current.Mode = strings.TrimPrefix(line, "new file mode ")
			}
			if strings.HasPrefix(line, "deleted file mode ") {
				current.Operation = "delete"
				current.Mode = strings.TrimPrefix(line, "deleted file mode ")
			}
		}
		seen := map[string]bool{}
		for i := range record.Files {
			f := &record.Files[i]
			if seen[f.Path] {
				return errors.New("duplicate delta file")
			}
			seen[f.Path] = true
			if f.Operation == "delete" {
				if f.Mode != "100644" && f.Mode != "100755" {
					return errors.New("delete mode")
				}
				continue
			}
			b, mode, e := r.repoFile(f.Path)
			if e != nil {
				return e
			}
			f.Mode = "100644"
			if mode.Perm()&0111 != 0 {
				f.Mode = "100755"
			}
			hash := sha(b)
			f.SHA256 = &hash
			f.Bytes = len(b)
		}
	}
	// Text evidence is authored by the host/task, not by this non-inferential tool.
	supplied := map[string]string{}
	if b, e := readRegular(filepath.Join(r.root, "out/evidence.json"), evidenceCap); e == nil {
		var doc struct {
			Answers []answer `json:"answers"`
		}
		if decode(bytes.NewReader(b), &doc, evidenceCap) != nil {
			return errors.New("evidence answers")
		}
		for _, a := range doc.Answers {
			if _, exists := supplied[a.RequestID]; exists || len(a.Answer) > 4096 {
				return errors.New("duplicate/oversize answer")
			}
			supplied[a.RequestID] = a.Answer
		}
	} else if !os.IsNotExist(e) {
		return e
	}
	ids := map[string]bool{}
	for _, q := range req.EvidenceRequests {
		if q.RequestID == "" || ids[q.RequestID] {
			return errors.New("evidence request ID")
		}
		ids[q.RequestID] = true
		value := ""
		if q.Kind == "text" {
			var ok bool
			value, ok = supplied[q.RequestID]
			if !ok {
				return errors.New("text answer unavailable")
			}
		} else {
			b, _, e := r.repoFile(q.Path)
			if e != nil {
				return e
			}
			switch q.Kind {
			case "sha256":
				value = sha(b)
			case "line":
				if !utf8.Valid(b) {
					return errors.New("line encoding")
				}
				lines := strings.Split(string(b), "\n")
				if q.Line < 1 || q.Line > len(lines) {
					return errors.New("evidence line unavailable")
				}
				value = lines[q.Line-1]
			case "marker":
				if q.Text == "" || !bytes.Contains(b, []byte(q.Text)) {
					return errors.New("marker absent")
				}
				value = q.Text
			default:
				return errors.New("evidence kind")
			}
		}
		if len(value) > 4096 {
			return errors.New("answer cap")
		}
		record.Answers = append(record.Answers, answer{RequestID: q.RequestID, Answer: value})
	}
	b, e := json.Marshal(record)
	if e != nil {
		return e
	}
	if e = validText("receipt-data.json", b, req.WritePaths); e != nil {
		return e
	}
	return os.WriteFile(filepath.Join(r.root, "out/receipt-data.json"), b, 0644)
}
