package w8builder

import (
	"archive/tar"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/url"
	"opensuperficies.local/srf/src/workerbridge"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

const ContextCap int64 = 16 << 20

var hex64 = regexp.MustCompile(`^[a-f0-9]{64}$`)

func digest(b []byte) string    { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func validDigest(s string) bool { return hex64.MatchString(s) && s != strings.Repeat("0", 64) }
func boundedRead(r io.Reader, cap int64) ([]byte, error) {
	b, e := io.ReadAll(io.LimitReader(r, cap+1))
	if e != nil {
		return nil, e
	}
	if int64(len(b)) > cap {
		return nil, refusal(2, "shape", "input cap exceeded")
	}
	return b, nil
}

type recipe map[string][]byte

func loadDir(dir string) (recipe, error) {
	files := recipe{}
	var total int64
	err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, e error) error {
		if e != nil {
			return refusal(2, "shape", "recipe inaccessible")
		}
		rel, e := filepath.Rel(dir, p)
		if e != nil {
			return e
		}
		if rel == "." {
			return nil
		}
		rel = filepath.ToSlash(rel)
		if d.Type()&os.ModeSymlink != 0 {
			return refusal(5, "leak", "symlink in recipe")
		}
		if d.IsDir() {
			return nil
		}
		if !d.Type().IsRegular() {
			return refusal(5, "leak", "non-regular context entry")
		}
		if dangerousPath(rel) {
			return refusal(5, "leak", "forbidden context name")
		}
		f, e := os.Open(p)
		if e != nil {
			return e
		}
		b, e := boundedRead(f, ContextCap-total)
		f.Close()
		if e != nil {
			return e
		}
		total += int64(len(b))
		files[rel] = b
		return nil
	})
	if err != nil {
		return nil, err
	}
	return files, nil
}
func loadTar(r io.Reader) (recipe, error) {
	files := recipe{}
	lr := &io.LimitedReader{R: r, N: ContextCap + 1}
	tr := tar.NewReader(lr)
	for count := 0; count < 4096; count++ {
		h, e := tr.Next()
		if e == io.EOF {
			_, e = io.Copy(io.Discard, lr)
			if e != nil {
				return nil, e
			}
			if lr.N == 0 {
				return nil, refusal(2, "shape", "context cap")
			}
			return files, nil
		}
		if e != nil {
			return nil, refusal(2, "shape", "invalid context tar")
		}
		n := strings.TrimPrefix(h.Name, "./")
		if h.Typeflag == tar.TypeDir {
			if n != "" && n != "." && dangerousPath(strings.TrimSuffix(n, "/")) {
				return nil, refusal(5, "leak", "directory traversal")
			}
			continue
		}
		if h.Typeflag != tar.TypeReg || dangerousPath(n) {
			return nil, refusal(5, "leak", "context entry")
		}
		if _, ok := files[n]; ok {
			return nil, refusal(2, "shape", "duplicate context entry")
		}
		if h.Size < 0 || h.Size > ContextCap {
			return nil, refusal(2, "shape", "context size")
		}
		b, e := boundedRead(tr, ContextCap)
		if e != nil {
			return nil, e
		}
		files[n] = b
	}
	return nil, refusal(2, "shape", "too many context entries")
}
func dangerousPath(n string) bool {
	l := strings.ToLower(n)
	if strings.HasPrefix(n, "/") || strings.Contains(n, "\\") || strings.Contains(n, "$") || strings.Contains(n, "~") || path.Clean(n) != n {
		return true
	}
	for _, p := range strings.Split(l, "/") {
		if p == ".." || p == "." || p == ".config" || p == ".ssh" || p == ".beckett" || p == "operator.token" || p == "launch.token" || p == "auth.json" || strings.HasSuffix(p, ".key") {
			return true
		}
	}
	return false
}
func Lint(dir string) error {
	f, e := loadDir(dir)
	if e != nil {
		return e
	}
	return lintRecipe(f)
}
func RecipeSHA(dir string) (string, error) {
	f, e := loadDir(dir)
	if e != nil {
		return "", e
	}
	if e = lintRecipe(f); e != nil {
		return "", e
	}
	return digest(f["base/recipe.manifest"]), nil
}
func parseObject(data []byte) (map[string]any, error) {
	v, e := workerbridge.Decode(data)
	if e != nil {
		return nil, refusal(2, "shape", "invalid JSON")
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, refusal(2, "shape", "expected object")
	}
	return m, nil
}

// ImagePATH belongs to the base recipe, outside the frozen task env_names enum.
const ImagePATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

func lintImagePATH(dockerfile []byte) error {
	stage := ""
	paths := 0
	for _, line := range strings.Split(string(dockerfile), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		if fields[0] == "FROM" {
			stage = fields[len(fields)-1]
		}
		if stage != "runtime" || fields[0] != "ENV" {
			continue
		}
		for _, entry := range fields[1:] {
			k, v, ok := strings.Cut(entry, "=")
			if !ok {
				return refusal(5, "lint", "runtime environment shape")
			}
			if k == "PATH" {
				paths++
				if v != ImagePATH {
					return refusal(5, "containment", "recipe PATH differs from fixed literal")
				}
			} else if !envNames[k] {
				return refusal(5, "containment", "runtime environment outside frozen enum")
			}
		}
	}
	if paths != 1 {
		return refusal(5, "containment", "one literal runtime PATH required")
	}
	return nil
}
func lintRecipe(f recipe) error {
	for _, n := range []string{"pins.json", "jules-setup.sh", "base/Dockerfile", "base/assert.sh", "base/recipe.manifest", "warm/opensuperficies.json", "warm/vision-engine.json", "warm/wender-agent.json", "warm/godot.json", "warm/blender.json"} {
		if len(f[n]) == 0 {
			return refusal(2, "shape", "missing recipe file: "+n)
		}
	}
	pins, e := parseObject(f["pins.json"])
	if e != nil {
		return e
	}
	allowed := map[string]bool{"schema": true, "base": true, "apt_snapshot": true, "source_date_epoch": true, "tools": true, "runner_sha256": true, "runner_sources": true}
	for k := range pins {
		if !allowed[k] {
			return refusal(2, "shape", "unknown pin field")
		}
	}
	if len(pins) != len(allowed) || pins["schema"] != "os.cu.env-pins/v1" {
		return refusal(2, "shape", "pins schema")
	}
	if e := lintRunnerPins(f, pins); e != nil {
		return e
	}
	if e := lintImagePATH(f["base/Dockerfile"]); e != nil {
		return e
	}
	base, _ := pins["base"].(string)
	parts := strings.Split(base, "@sha256:")
	if len(parts) != 2 || !strings.HasPrefix(parts[0], "debian:bookworm-slim") || !validDigest(parts[1]) {
		return refusal(2, "pin", "base digest")
	}
	snap, _ := pins["apt_snapshot"].(string)
	u, e := url.Parse(snap)
	if e != nil || u.Scheme != "https" || u.Host != "snapshot.debian.org" || !strings.Contains(u.Path, "/archive/") || strings.ContainsAny(snap, "<>\n\r") {
		return refusal(2, "pin", "snapshot")
	}
	epoch, ok := pins["source_date_epoch"].(json.Number)
	if !ok {
		return refusal(2, "pin", "epoch")
	}
	i, e := strconv.ParseInt(string(epoch), 10, 64)
	if e != nil || i <= 0 {
		return refusal(2, "pin", "epoch")
	}
	tools, ok := pins["tools"].([]any)
	if !ok || len(tools) == 0 {
		return refusal(2, "pin", "tool pins")
	}
	names := map[string]bool{}
	for _, v := range tools {
		m, ok := v.(map[string]any)
		if !ok || len(m) != 4 {
			return refusal(2, "pin", "tool shape")
		}
		for _, k := range []string{"name", "version", "url", "sha256"} {
			s, ok := m[k].(string)
			if !ok || s == "" || strings.ContainsAny(s, "<>\n\r") {
				return refusal(2, "pin", "tool placeholder")
			}
			if s == "AUSENTE" {
				return refusal(4, "unavailable", "tool unavailable")
			}
		}
		n := m["name"].(string)
		if names[n] {
			return refusal(2, "pin", "duplicate tool")
		}
		names[n] = true
		if !validDigest(m["sha256"].(string)) {
			return refusal(2, "pin", "tool checksum")
		}
		u, e := url.Parse(m["url"].(string))
		if e != nil || u.Scheme != "https" || u.Host == "" {
			return refusal(2, "pin", "tool URL")
		}
		if n == "go" && m["version"] != "1.26.0" {
			return refusal(2, "pin", "Go version")
		}
	}
	for _, n := range []string{"go", "tini", "node", "rust/cargo", "python3-venv", "pip", "build-essential", "cmake", "pkg-config", "godot", "blender"} {
		if !names[n] {
			return refusal(2, "pin", "missing tool: "+n)
		}
	}
	for n, b := range f {
		if strings.HasPrefix(n, "warm/") && strings.HasSuffix(n, ".json") {
			if e := LintWarm(b); e != nil {
				return e
			}
		}
	}
	if e := LintDockerfile(f["base/Dockerfile"]); e != nil {
		return e
	}
	if e := bindRecipePins(f, pins); e != nil {
		return e
	}
	expected, e := JulesSetup(f["pins.json"])
	if e != nil {
		return e
	}
	if !bytes.Equal(expected, f["jules-setup.sh"]) {
		return refusal(2, "pin", "Jules script differs from generated pins")
	}
	var manifest strings.Builder
	keys := make([]string, 0, len(f))
	for n := range f {
		if n != "base/recipe.manifest" {
			keys = append(keys, n)
		}
	}
	sort.Strings(keys)
	for _, n := range keys {
		fmt.Fprintf(&manifest, "%s  %s\n", digest(f[n]), n)
	}
	if manifest.String() != string(f["base/recipe.manifest"]) {
		return refusal(2, "shape", "manifest mismatch or unlisted context file")
	}
	return nil
}

var envNames = map[string]bool{"LC_ALL": true, "PYTHONUNBUFFERED": true, "PYTHONDONTWRITEBYTECODE": true, "TINI_SUBREAPER": true, "GIT_TERMINAL_PROMPT": true, "GIT_ASKPASS": true, "GOFLAGS": true, "GOCACHE": true, "GOMODCACHE": true, "GOTOOLCHAIN": true, "CARGO_HOME": true, "CARGO_NET_OFFLINE": true, "PIP_NO_INDEX": true, "HOME": true, "TMPDIR": true}

func LintWarm(b []byte) error {
	m, e := parseObject(b)
	if e != nil {
		return e
	}
	v, ok := m["offline_env"].(map[string]any)
	if !ok {
		return refusal(2, "shape", "warm offline_env")
	}
	for k, val := range v {
		if !envNames[k] {
			return refusal(2, "shape", "warm environment name")
		}
		if _, ok := val.(string); !ok {
			return refusal(2, "shape", "warm environment value")
		}
	}
	if v["GOTOOLCHAIN"] != "local" {
		return refusal(2, "shape", "GOTOOLCHAIN must be local")
	}
	return nil
}

// LintDockerfile tokenizes COPY/ADD after continuation folding, including JSON form.
func LintDockerfile(data []byte) error {
	text := strings.ReplaceAll(string(data), "\\\r\n", "")
	text = strings.ReplaceAll(text, "\\\n", "")
	stage := ""
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		p := strings.IndexAny(line, " \t")
		if p < 0 {
			return refusal(5, "lint", "malformed Docker instruction")
		}
		op, rest := strings.ToUpper(line[:p]), strings.TrimSpace(line[p:])
		switch op {
		case "FROM":
			fs := strings.Fields(rest)
			if len(fs) == 0 {
				return refusal(5, "lint", "FROM")
			}
			stage = ""
			if len(fs) >= 3 && strings.EqualFold(fs[len(fs)-2], "AS") {
				stage = strings.ToLower(fs[len(fs)-1])
			}
			image := fs[0]
			if strings.HasPrefix(image, "--platform=") {
				fs = fs[1:]
				if len(fs) == 0 {
					return refusal(5, "lint", "FROM")
				}
				image = fs[0]
			}
			sp := strings.Split(image, "@sha256:")
			if len(sp) != 2 || !validDigest(sp[1]) {
				return refusal(5, "lint", "unpinned FROM")
			}
		case "ADD", "COPY":
			from := ""
			for strings.HasPrefix(rest, "--") {
				p := strings.IndexAny(rest, " \t")
				if p < 0 {
					return refusal(5, "lint", "COPY flags")
				}
				flag := rest[:p]
				rest = strings.TrimSpace(rest[p:])
				if strings.HasPrefix(flag, "--from=") {
					from = strings.TrimPrefix(flag, "--from=")
				} else if flag != "--link" && !strings.HasPrefix(flag, "--chown=") && !strings.HasPrefix(flag, "--chmod=") {
					return refusal(5, "lint", "unrecognized COPY flag")
				}
			}
			var args []string
			if strings.HasPrefix(rest, "[") {
				if e := json.Unmarshal([]byte(rest), &args); e != nil {
					return refusal(5, "lint", "COPY JSON")
				}
			} else {
				var e error
				args, e = shellWords(rest)
				if e != nil {
					return e
				}
			}
			if len(args) < 2 {
				return refusal(5, "lint", "COPY arity")
			}
			destination := strings.TrimSuffix(args[len(args)-1], "/")
			for _, src := range args[:len(args)-1] {
				source := strings.TrimSuffix(src, "/")
				runnerSource := source == "opt/w8" || strings.HasPrefix(source, "opt/w8/")
				runnerDest := destination == "/opt/w8" || strings.HasPrefix(destination, "/opt/w8/")
				if runnerSource || runnerDest {
					if from != "fetch" || stage != "runtime" || source != "opt/w8" || destination != "/opt/w8" || len(args) != 2 {
						return refusal(5, "leak", "runner runtime placement")
					}
				}
				if dangerousPath(strings.TrimSuffix(src, "/")) {
					return refusal(5, "leak", "forbidden COPY source")
				}
				if from != "" {
					if from != "fetch" {
						return refusal(5, "leak", "unapproved stage")
					}
				} else if strings.TrimSuffix(src, "/") == "base/w8runner" {
					if stage != "fetch" || args[len(args)-1] != "/runner/" {
						return refusal(5, "leak", "runner source only in fetch /runner/")
					}
				} else if src != "base/assert.sh" {
					return refusal(5, "leak", "context source not allowlisted")
				}
			}
		}
	}
	return nil
}
func shellWords(s string) ([]string, error) {
	var out []string
	var b strings.Builder
	var quote rune
	escape, active := false, false
	for _, c := range s {
		if escape {
			b.WriteRune(c)
			escape = false
			active = true
			continue
		}
		if c == '\\' {
			escape = true
			active = true
			continue
		}
		if quote != 0 {
			if c == quote {
				quote = 0
			} else {
				b.WriteRune(c)
			}
			active = true
			continue
		}
		if c == '\'' || c == '"' {
			quote = c
			active = true
			continue
		}
		if c == ' ' || c == '\t' {
			if active {
				out = append(out, b.String())
				b.Reset()
				active = false
			}
			continue
		}
		b.WriteRune(c)
		active = true
	}
	if quote != 0 || escape {
		return nil, refusal(5, "lint", "unterminated shell token")
	}
	if active {
		out = append(out, b.String())
	}
	return out, nil
}

// bindRecipePins prevents a valid pins file from blessing an unrelated recipe.
func bindRecipePins(f recipe, pins map[string]any) error {
	docker := string(f["base/Dockerfile"])
	stages := 0
	stage := ""
	epochArgs, epochEnvs := 0, 0
	for _, line := range strings.Split(docker, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		switch strings.ToUpper(fields[0]) {
		case "FROM":
			stages++
			stage = ""
			if len(fields) >= 3 && strings.EqualFold(fields[len(fields)-2], "AS") {
				stage = strings.ToLower(fields[len(fields)-1])
			}
			i := 1
			if len(fields) > i && strings.HasPrefix(fields[i], "--platform=") {
				i++
			}
			if len(fields) <= i || fields[i] != pins["base"] {
				return refusal(5, "lint", "FROM differs from pinned base")
			}
		case "ARG":
			if strings.TrimSpace(strings.TrimPrefix(line, "ARG")) == "SOURCE_DATE_EPOCH" {
				epochArgs++
			}
		case "ENV":
			if strings.TrimSpace(strings.TrimPrefix(line, "ENV")) == "SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}" {
				if stage != "fetch" {
					return refusal(5, "lint", "epoch is build-only in runtime")
				}
				epochEnvs++
			}
		}
	}
	if stages != 2 || epochArgs != stages || epochEnvs != 1 {
		return refusal(5, "lint", "every stage must bind epoch ARG; ENV only in fetch")
	}
	if !strings.Contains(docker, pins["apt_snapshot"].(string)) {
		return refusal(5, "lint", "apt snapshot differs from pins")
	}
	for _, v := range pins["tools"].([]any) {
		tool := v.(map[string]any)
		verified := false
		for _, line := range strings.Split(docker, "\n") {
			if strings.Contains(line, tool["url"].(string)) && strings.Contains(line, tool["sha256"].(string)) && strings.Contains(line, "sha256sum -c -") {
				verified = true
			}
		}
		if !verified {
			return refusal(5, "lint", "pinned fetch lacks checksum assertion")
		}
	}
	return nil
}

// A draft recipe can be reviewed and hashed before binary compilation. Null
// binary pins confer no permission: image assertions and ScanLayers fail closed.
func lintRunnerPins(f recipe, pins map[string]any) error {
	m, ok := pins["runner_sha256"].(map[string]any)
	if !ok || len(m) != 5 {
		return refusal(2, "pin", "five runner pins required")
	}
	for _, v := range runnerVerbs {
		value, ok := m[v]
		if !ok {
			return refusal(2, "pin", "runner pin missing")
		}
		if value != nil {
			s, ok := value.(string)
			if !ok || !validDigest(s) || s != ApprovedRunnerSHA256 {
				return refusal(5, "pin", "runner binary hash not approved")
			}
		}
	}
	sources, ok := pins["runner_sources"].(map[string]any)
	if !ok || len(sources) != 5 {
		return refusal(2, "pin", "runner source pins")
	}
	for _, n := range []string{"main.go", "process.go", "probes.go", "text.go", "extract.go"} {
		b, ok := f["base/w8runner/"+n]
		if !ok || len(b) == 0 || sources[n] != digest(b) {
			return refusal(5, "pin", "runner source digest")
		}
	}
	return nil
}
