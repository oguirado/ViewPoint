package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"path"
	"regexp"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"
)

var encodedRun = regexp.MustCompile(`[A-Za-z0-9+/=]{100,}`)
var hexRun = regexp.MustCompile(`[a-fA-F0-9]{129,}`)
var lowerHex = regexp.MustCompile(`^[a-f0-9]+$`)

func validText(name string, b []byte, writePaths []string) error {
	if len(b) == 0 || len(b) > evidenceCap || !utf8.Valid(b) || bytes.IndexByte(b, 0) >= 0 {
		return errors.New("text size or encoding")
	}
	if hexRun.Match(b) {
		return errors.New("hex encoded binary")
	}
	for _, m := range encodedRun.FindAll(b, -1) {
		if !lowerHex.Match(m) {
			return errors.New("encoded binary")
		}
	}
	for _, r := range string(b) {
		if !unicode.IsPrint(r) && r != '\n' && r != '\r' && r != '\t' {
			return errors.New("non-printable text")
		}
	}
	switch path.Ext(name) {
	case ".md", ".txt":
		return nil
	case ".json":
		d := json.NewDecoder(bytes.NewReader(b))
		d.UseNumber()
		tok, e := d.Token()
		if e != nil || tok != json.Delim('{') {
			return errors.New("JSON object required")
		}
		if e = jsonObject(d, 0); e != nil {
			return e
		}
		if _, e = d.Token(); e != io.EOF {
			return errors.New("trailing JSON")
		}
		return nil
	case ".patch", ".diff":
		return checkPatch(b, writePaths)
	default:
		return errors.New("suffix")
	}
}
func jsonObject(d *json.Decoder, depth int) error {
	if depth > 64 {
		return errors.New("JSON depth")
	}
	seen := map[string]bool{}
	for d.More() {
		key, e := d.Token()
		if e != nil {
			return e
		}
		s, ok := key.(string)
		if !ok || seen[s] || encodedString(s) {
			return errors.New("duplicate JSON key")
		}
		seen[s] = true
		if e = jsonValue(d, depth+1); e != nil {
			return e
		}
	}
	t, e := d.Token()
	if e != nil || t != json.Delim('}') {
		return errors.New("JSON object")
	}
	return nil
}
func jsonValue(d *json.Decoder, depth int) error {
	if depth > 64 {
		return errors.New("JSON depth")
	}
	t, e := d.Token()
	if e != nil {
		return e
	}
	if value, ok := t.(string); ok && encodedString(value) {
		return errors.New("encoded JSON string")
	}
	if delim, ok := t.(json.Delim); ok {
		switch delim {
		case '{':
			return jsonObject(d, depth)
		case '[':
			for d.More() {
				if e = jsonValue(d, depth+1); e != nil {
					return e
				}
			}
			end, e := d.Token()
			if e != nil || end != json.Delim(']') {
				return errors.New("JSON array")
			}
		default:
			return errors.New("JSON delimiter")
		}
	}
	return nil
}
func safePath(p string) bool {
	if p == "" || path.Clean(p) != p || strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\\\x00\t\r\n\"") {
		return false
	}
	for _, part := range strings.Split(p, "/") {
		if part == ".." || part == ".git" || part == ".wavemind" {
			return false
		}
	}
	return true
}
func allowed(p string, patterns []string) bool {
	if !safePath(p) {
		return false
	}
	for _, s := range patterns {
		if safePath(s) && s == p {
			return true
		}
	}
	return false
}

var hunkHeader = regexp.MustCompile(`^@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?$`)

func checkPatch(b []byte, writePaths []string) error {
	if len(b) == 0 || int64(len(b)) > patchCap || !utf8.Valid(b) || bytes.IndexByte(b, 0) >= 0 || b[len(b)-1] != '\n' {
		return errors.New("patch encoding")
	}
	if bytes.Contains(b, []byte("GIT binary patch")) || bytes.Contains(b, []byte("Binary files ")) {
		return errors.New("binary patch")
	}
	var old, new string
	operation := "modify"
	seen := map[string]bool{}
	started, minus, plus, hunk := false, false, false, false
	oldLeft, newLeft := 0, 0
	finish := func() error {
		if !started || !minus || !plus || !hunk || oldLeft != 0 || newLeft != 0 {
			return errors.New("patch structure")
		}
		return nil
	}
	lines := strings.Split(string(b), "\n")
	for lineNo, line := range lines {
		switch {
		case strings.HasPrefix(line, "diff --git "):
			if started {
				if e := finish(); e != nil {
					return e
				}
			}
			f := strings.Fields(line)
			if len(f) != 4 || !strings.HasPrefix(f[2], "a/") || !strings.HasPrefix(f[3], "b/") {
				return errors.New("diff header")
			}
			old, new = strings.TrimPrefix(f[2], "a/"), strings.TrimPrefix(f[3], "b/")
			if old != new || !allowed(old, writePaths) || !allowed(new, writePaths) || seen[new] {
				return errors.New("patch surface or rename")
			}
			seen[new] = true
			operation = "modify"
			started = true
			minus = false
			plus = false
			hunk = false
		case strings.HasPrefix(line, "rename ") || strings.HasPrefix(line, "copy ") || strings.HasPrefix(line, "similarity ") || strings.HasPrefix(line, "dissimilarity "):
			return errors.New("rename/copy")
		case strings.HasPrefix(line, "old mode ") || strings.HasPrefix(line, "new mode ") || strings.HasPrefix(line, "new file mode ") || strings.HasPrefix(line, "deleted file mode "):
			if !started || hunk {
				return errors.New("mode position")
			}
			f := strings.Fields(line)
			mode := f[len(f)-1]
			if mode != "100644" && mode != "100755" {
				return errors.New("mode")
			}
			if strings.HasPrefix(line, "new file mode ") {
				operation = "add"
			}
			if strings.HasPrefix(line, "deleted file mode ") {
				operation = "delete"
			}
		case strings.HasPrefix(line, "--- ") && !hunk:
			if !started || minus {
				return errors.New("old header")
			}
			p := strings.TrimPrefix(line, "--- ")
			if p != "a/"+old && (p != "/dev/null" || operation != "add") {
				return errors.New("old path")
			}
			minus = true
		case strings.HasPrefix(line, "+++ ") && !hunk:
			if !minus || plus {
				return errors.New("new header")
			}
			p := strings.TrimPrefix(line, "+++ ")
			if p != "b/"+new && (p != "/dev/null" || operation != "delete") {
				return errors.New("new path")
			}
			plus = true
		case strings.HasPrefix(line, "index ") && !hunk:
			fields := strings.Fields(line)
			if !started || len(fields) < 2 || len(fields) > 3 {
				return errors.New("index header")
			}
			if len(fields) == 3 && fields[2] != "100644" && fields[2] != "100755" {
				return errors.New("index mode")
			}
		case strings.HasPrefix(line, "@@ "):
			if !minus || !plus || oldLeft != 0 || newLeft != 0 {
				return errors.New("hunk")
			}
			m := hunkHeader.FindStringSubmatch(line)
			if m == nil {
				return errors.New("hunk header")
			}
			count := func(s string) (int, error) {
				if s == "" {
					return 1, nil
				}
				n, e := strconv.Atoi(s)
				if e != nil || n > int(patchCap) {
					return 0, errors.New("hunk count")
				}
				return n, nil
			}
			var e error
			oldLeft, e = count(m[2])
			if e != nil {
				return e
			}
			newLeft, e = count(m[4])
			if e != nil {
				return e
			}
			hunk = true
		case hunk:
			if line == "" && lineNo == len(lines)-1 {
				continue
			}
			if line == `\ No newline at end of file` {
				continue
			}
			if line == "" {
				return errors.New("empty hunk line")
			}
			switch line[0] {
			case ' ':
				oldLeft--
				newLeft--
			case '-':
				oldLeft--
			case '+':
				newLeft--
			default:
				return errors.New("hunk body")
			}
			if oldLeft < 0 || newLeft < 0 {
				return errors.New("hunk overflow")
			}
		default:
			if line != "" && (!started || !strings.HasPrefix(line, "index ")) {
				return errors.New("patch metadata")
			}
		}
	}
	return finish()
}

func encodedString(s string) bool {
	if strings.ContainsRune(s, 0) || hexRun.MatchString(s) {
		return true
	}
	for _, run := range encodedRun.FindAllString(s, -1) {
		if !lowerHex.MatchString(run) {
			return true
		}
	}
	return false
}
