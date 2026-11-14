package mcp

import (
	"net"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var defaultOZ = []string{"/home/user/Documentos/OZ"}

// CheckEndpoint admits only the capsule forwarder 127.0.0.1:19876+n (n 0..7).
func CheckEndpoint(ep string) error {
	h := strings.TrimPrefix(strings.TrimPrefix(ep, "http://"), "tcp://")
	if i := strings.IndexByte(h, '/'); i >= 0 {
		h = h[:i]
	}
	host, ps, err := net.SplitHostPort(h)
	if err != nil {
		return gate("endpoint_not_forwarder")
	}
	port, err := strconv.Atoi(ps)
	if err != nil {
		return gate("endpoint_not_forwarder")
	}
	if port == 9876 || port == 9877 || port == 8770 {
		return gate("operator_session")
	}
	if host != "127.0.0.1" || port < 19876 || port > 19883 {
		return gate("endpoint_not_forwarder")
	}
	return nil
}

func under(p, root string) bool {
	root = filepath.Clean(root)
	return p == root || strings.HasPrefix(p, root+"/")
}

// GuardGodotProject allows only a clean absolute path inside the capsule
// scratch dir and never under an OZ root.
func GuardGodotProject(p, scratch string, oz []string) error {
	if !filepath.IsAbs(p) || filepath.Clean(p) != p {
		return gate("operator_session: project path not canonical")
	}
	for _, r := range append(append([]string{}, defaultOZ...), oz...) {
		if r != "" && under(p, r) {
			return gate("operator_session: OZ tree")
		}
	}
	if scratch == "" || !under(p, scratch) {
		return gate("operator_session: outside capsule scratch")
	}
	if rp, err := filepath.EvalSymlinks(p); err == nil {
		if rs, err := filepath.EvalSymlinks(scratch); err == nil && !under(rp, rs) {
			return gate("operator_session: symlink escape")
		}
	}
	return nil
}

var (
	reImportStmt = regexp.MustCompile(`(?:\bfrom[ \t]+([\w.]*)[ \t]+)?\bimport\b[ \t]*([^\n;]*)`)
	reDyn        = regexp.MustCompile(`\b(exec|eval|compile|getattr|setattr|delattr|globals|locals|breakpoint|input|help)\b`)
	reMisc       = regexp.MustCompile(`__import__|__builtins__|bpy\.app\.handlers|addon_utils\.enable`)
	reDunder     = regexp.MustCompile(`__\w+__`)
	// reBpyExec denies Blender's own exec-equivalents, also through an alias
	// (bare names) or with spaces around the dots.
	reBpyExec = regexp.MustCompile(`\bbpy\.utils\.(execfile|load_scripts|register_submodule_factory)\b|\bbpy\.ops\.script\.|\bbpy\.ops\.text\.run_script\b|\bvars\s*\(|\bcompile\s*\(` +
		`|\b(execfile|load_scripts|register_submodule_factory|python_file_run|run_script|vars)\b|\bops\s*\.\s*script\b`)
	// reTextExec denies running or loading other code: text blocks as modules,
	// library/mainfile loads and the use_scripts switch (FJ-01 b).
	reTextExec = regexp.MustCompile(`\bas_module\b|\btexts\s*\.\s*load\b|\blibraries\s*\.\s*(load|write)\b|\buse_scripts\b` +
		`|\b(open_mainfile|read_homefile|append|link)\b`)
	rePathKw   = regexp.MustCompile(`\b(filepath|filename|directory)\s*=`)
	rePathKey  = regexp.MustCompile(`['"](filepath|filename|directory)['"]`)
	rePathLit  = regexp.MustCompile(`^\s*(?:'([^'\\\n]*)'|"([^"\\\n]*)")[ \t]*(?:[,)\n;]|$)`)
	reOpenWord = regexp.MustCompile(`\bopen\b`)
	reOpenCall = regexp.MustCompile(`^open\s*\(\s*(?:'([^'\\\n]*)'|"([^"\\\n]*)")\s*,\s*(?:'([^'\\\n]*)'|"([^"\\\n]*)")\s*\)`)
)

// importAllow is the only set of top-level modules a Blender script may import.
var importAllow = map[string]bool{"bpy": true, "math": true, "mathutils": true}

func importRoot(name string) string {
	name = strings.TrimSpace(name)
	if i := strings.IndexAny(name, " \t."); i >= 0 {
		name = name[:i]
	}
	return name
}

// ScanScript statically denies unsafe execute_blender_code / python_exec
// scripts: imports are an allowlist (bpy, math, mathutils), dynamic-code and
// attribute-reflection names are denied, and open( is admitted only as a call
// with exactly two literal arguments (path, mode) whose path is canonical and
// under outDir. Comments are dropped first and backslash continuations are
// refused; dunders and Blender's exec-equivalents are denied, and every
// filepath=/filename=/directory= and every literal starting with "/" is a
// canonical path under outDir. Any non-ASCII byte outside a plain literal is
// refused (Python NFKC-normalises identifiers). This is a defence-in-depth
// lint, known bypassable, not a sandbox: the boundary is the capsule.
func ScanScript(src, outDir string) error {
	src, lits, err := codeOnly(src)
	if err != nil {
		return err
	}
	for _, l := range lits {
		if strings.HasPrefix(l, "/") && (strings.ContainsRune(l, '\\') || !inOut(l, outDir)) {
			return gate("script_denied: path literal outside out_dir")
		}
	}
	for _, m := range reImportStmt.FindAllStringSubmatch(src, -1) {
		if strings.HasPrefix(m[0], "from") { // from X import ...: only X matters
			if r := importRoot(m[1]); !importAllow[r] {
				return gate("script_denied: from " + r)
			}
			continue
		}
		if strings.TrimSpace(m[2]) == "" {
			continue // the word "import" with nothing after it (comment prose) imports nothing
		}
		for _, part := range strings.Split(m[2], ",") {
			if r := importRoot(part); !importAllow[r] {
				return gate("script_denied: import " + r)
			}
		}
	}
	if m := reDyn.FindStringSubmatch(src); m != nil {
		return gate("script_denied: " + m[1])
	}
	if m := reMisc.FindString(src); m != "" {
		return gate("script_denied: " + m)
	}
	if m := reDunder.FindString(src); m != "" {
		return gate("script_denied: dunder " + m)
	}
	if m := reBpyExec.FindString(src); m != "" {
		return gate("script_denied: " + m)
	}
	if m := reTextExec.FindString(src); m != "" {
		return gate("script_denied: " + m)
	}
	if rePathKey.MatchString(src) {
		return gate("script_denied: path key outside a literal kwarg")
	}
	for _, loc := range rePathKw.FindAllStringIndex(src, -1) {
		m := rePathLit.FindStringSubmatch(src[loc[1]:])
		if m == nil || !inOut(m[1]+m[2], outDir) {
			return gate("script_denied: filepath/filename outside out_dir")
		}
	}
	for _, loc := range reOpenWord.FindAllStringIndex(src, -1) {
		m := reOpenCall.FindStringSubmatch(src[loc[0]:])
		if m == nil {
			return gate("script_denied: open must be open(literal path, literal mode)")
		}
		if !inOut(m[1]+m[2], outDir) {
			return gate("script_denied: open outside out_dir")
		}
	}
	return nil
}

// inOut reports whether a literal path is canonical, absolute and under outDir.
func inOut(path, outDir string) bool {
	return outDir != "" && filepath.Clean(path) == path && filepath.IsAbs(path) && under(path, outDir)
}

// codeOnly drops # comments string-aware (a '#' inside a literal is kept and
// cannot hide the rest of its line) and refuses backslash continuations and
// unterminated literals, so one physical line is one logical line for the scan.
// It also returns every literal's raw body, and refuses any byte >= 0x80
// outside a literal or inside an f-string (whose {} parts are code).
func codeOnly(src string) (string, []string, error) {
	var b strings.Builder
	var lits []string
	q, start, fstr := "", 0, false // open quote: ', ", or a triple of either
	for i := 0; i < len(src); i++ {
		c := src[i]
		if q == "" {
			switch c {
			case '#':
				for i+1 < len(src) && src[i+1] != '\n' {
					i++
				}
			case '\\':
				return "", nil, gate("script_denied: backslash continuation")
			case '\'', '"':
				fstr = strings.ContainsAny(literalPrefix(src[:i]), "fF")
				q = string(c)
				if strings.HasPrefix(src[i:], strings.Repeat(q, 3)) {
					q = strings.Repeat(q, 3)
				}
				b.WriteString(q)
				i += len(q) - 1
				start = i + 1
			default:
				if c >= 0x80 {
					return "", nil, gate("script_denied: non-ascii")
				}
				b.WriteByte(c)
			}
			continue
		}
		switch {
		case c >= 0x80 && fstr:
			return "", nil, gate("script_denied: non-ascii")
		case c == '\\':
			if i+1 >= len(src) || src[i+1] == '\n' {
				return "", nil, gate("script_denied: backslash continuation")
			}
			b.WriteByte(c)
			i++
			b.WriteByte(src[i])
		case c == '\n' && len(q) == 1:
			return "", nil, gate("script_denied: unterminated literal")
		case strings.HasPrefix(src[i:], q):
			lits = append(lits, src[start:i])
			b.WriteString(q)
			i += len(q) - 1
			q = ""
		default:
			b.WriteByte(c)
		}
	}
	if q != "" {
		return "", nil, gate("script_denied: unterminated literal")
	}
	return b.String(), lits, nil
}

// literalPrefix returns the string-prefix letters (r, b, f, u) right before a quote.
func literalPrefix(before string) string {
	j := len(before)
	for j > 0 && strings.IndexByte("rRbBfFuU", before[j-1]) >= 0 {
		j--
	}
	if j > 0 && (before[j-1] == '_' || before[j-1] >= 0x80 || ('0' <= before[j-1] && before[j-1] <= '9') ||
		('a' <= before[j-1] && before[j-1] <= 'z') || ('A' <= before[j-1] && before[j-1] <= 'Z')) {
		return "" // part of a longer identifier, not a prefix
	}
	return before[j:]
}

// CheckEnvelope fails when any 12-byte window of server content appears in the
// envelope (server bytes must live only in CAS).
func CheckEnvelope(env, server []byte) error {
	const w = 12
	if len(server) < w || len(env) < w {
		return nil
	}
	set := make(map[string]struct{}, len(env))
	for i := 0; i+w <= len(env); i++ {
		set[string(env[i:i+w])] = struct{}{}
	}
	for i := 0; i+w <= len(server); i++ {
		if _, ok := set[string(server[i:i+w])]; ok {
			return gate("envelope_leak")
		}
	}
	return nil
}
