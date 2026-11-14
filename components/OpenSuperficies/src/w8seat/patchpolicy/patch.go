// Package patchpolicy admits a bounded regular-file unified diff. It never applies it.
package patchpolicy

import (
	"bytes"
	"fmt"
	"path"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"
)

type ChangedFile struct {
	Path      string `json:"path"`
	Mode      string `json:"mode"`
	Operation string `json:"operation"`
}

var hunk = regexp.MustCompile(`^@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?$`)

func safe(p string) bool {
	if p == "" || path.Clean(p) != p || strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\\\x00\t\r\n\" ") {
		return false
	}
	for _, s := range strings.Split(p, "/") {
		if s == ".." || s == "." || s == ".git" || s == ".wavemind" {
			return false
		}
	}
	return true
}
func allowed(p string, paths []string) bool {
	for _, w := range paths {
		if safe(w) && (p == w || strings.HasSuffix(w, "/") && strings.HasPrefix(p, w)) {
			return true
		}
	}
	return false
}

// Check is the shared BL1/BL3 API. Paths are exact contract write_paths.
// Ambiguous quoted paths, copies, renames, symlinks, submodules and binary diffs fail closed.
func Check(diff []byte, writePaths []string) ([]ChangedFile, error) {
	fail := func() ([]ChangedFile, error) { return nil, fmt.Errorf("patch policy: invalid diff or write surface") }
	if len(diff) == 0 || len(diff) > 1<<20 || !utf8.Valid(diff) || bytes.IndexByte(diff, 0) >= 0 || diff[len(diff)-1] != '\n' {
		return fail()
	}
	lines := strings.Split(strings.TrimSuffix(string(diff), "\n"), "\n")
	var result []ChangedFile
	seen := map[string]bool{}
	var cur *ChangedFile
	var oldHeader, newHeader bool
	var minus, plus int
	var haveHunk bool
	finish := func() bool { return cur != nil && oldHeader && newHeader && haveHunk && minus == 0 && plus == 0 }
	for _, l := range lines {
		if minus > 0 || plus > 0 {
			if l == "\\ No newline at end of file" {
				continue
			}
			if l == "" {
				return fail()
			}
			switch l[0] {
			case ' ':
				minus--
				plus--
			case '-':
				minus--
			case '+':
				plus--
			default:
				return fail()
			}
			if minus < 0 || plus < 0 {
				return fail()
			}
			continue
		}
		if strings.HasPrefix(l, "diff --git ") {
			if cur != nil && !finish() {
				return fail()
			}
			f := strings.Fields(l)
			if len(f) != 4 || !strings.HasPrefix(f[2], "a/") || !strings.HasPrefix(f[3], "b/") {
				return fail()
			}
			a, b := strings.TrimPrefix(f[2], "a/"), strings.TrimPrefix(f[3], "b/")
			if a != b || !safe(a) || !allowed(a, writePaths) || seen[a] {
				return fail()
			}
			seen[a] = true
			result = append(result, ChangedFile{Path: a, Mode: "100644", Operation: "modify"})
			cur = &result[len(result)-1]
			oldHeader = false
			newHeader = false
			haveHunk = false
			continue
		}
		if cur == nil {
			return fail()
		}
		switch {
		case strings.HasPrefix(l, "index "):
			f := strings.Fields(l)
			if len(f) < 2 || len(f) > 3 {
				return fail()
			}
			if len(f) == 3 {
				if f[2] != "100644" && f[2] != "100755" {
					return fail()
				}
				cur.Mode = f[2]
			}
		case strings.HasPrefix(l, "new file mode "), strings.HasPrefix(l, "deleted file mode "), strings.HasPrefix(l, "old mode "), strings.HasPrefix(l, "new mode "):
			f := strings.Fields(l)
			m := f[len(f)-1]
			if m != "100644" && m != "100755" {
				return fail()
			}
			cur.Mode = m
			if strings.HasPrefix(l, "new file") {
				cur.Operation = "add"
			}
			if strings.HasPrefix(l, "deleted file") {
				cur.Operation = "delete"
			}
		case strings.HasPrefix(l, "--- "):
			if oldHeader || newHeader {
				return fail()
			}
			p := strings.TrimPrefix(l, "--- ")
			if p != "a/"+cur.Path && (p != "/dev/null" || cur.Operation != "add") {
				return fail()
			}
			oldHeader = true
		case strings.HasPrefix(l, "+++ "):
			if !oldHeader || newHeader {
				return fail()
			}
			p := strings.TrimPrefix(l, "+++ ")
			if p != "b/"+cur.Path && (p != "/dev/null" || cur.Operation != "delete") {
				return fail()
			}
			newHeader = true
		case strings.HasPrefix(l, "@@ "):
			if !oldHeader || !newHeader {
				return fail()
			}
			m := hunk.FindStringSubmatch(l)
			if m == nil {
				return fail()
			}
			count := func(s string) int {
				if s == "" {
					return 1
				}
				n, e := strconv.Atoi(s)
				if e != nil || n > 1<<20 {
					return -1
				}
				return n
			}
			minus, plus = count(m[2]), count(m[4])
			if minus < 0 || plus < 0 {
				return fail()
			}
			haveHunk = true
		case l == "\\ No newline at end of file" && haveHunk:
		default:
			return fail()
		}
	}
	if !finish() || len(result) > 256 {
		return fail()
	}
	return result, nil
}
