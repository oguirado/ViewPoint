package exec

import (
	"bufio"
	"fmt"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"opensuperficies.local/srf/src/cubook/action"
	"opensuperficies.local/srf/src/cubook/rc"
)

// Decision is the result of the pre-act gate; zero Verdict means "go".
type Decision struct {
	Verdict string
	Stop    string
	RC      int
}

type fillRule struct {
	r, g, b, tol int
}

// DenyList is act_deny.tsv (DESIGN_CU §6).
type DenyList struct {
	names    map[string]bool
	handlers map[string]bool
	regexes  []*regexp.Regexp
	fills    []fillRule
}

// LoadDenyList parses `kind<TAB>value[<TAB>tol]`, kinds name|handler|regex|fill.
func LoadDenyList(path string) (*DenyList, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, rc.New("E_IO", err.Error())
	}
	defer f.Close()
	d := &DenyList{names: map[string]bool{}, handlers: map[string]bool{}}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		ln := strings.TrimRight(sc.Text(), "\r")
		if ln == "" || strings.HasPrefix(ln, "#") {
			continue
		}
		p := strings.Split(ln, "\t")
		if len(p) < 2 {
			return nil, rc.New("E_SCHEMA", "bad deny line: "+ln)
		}
		switch p[0] {
		case "name":
			d.names[p[1]] = true
		case "handler":
			d.handlers[p[1]] = true
		case "regex":
			re, err := regexp.Compile("(?i)" + p[1])
			if err != nil {
				return nil, rc.New("E_SCHEMA", err.Error())
			}
			d.regexes = append(d.regexes, re)
		case "fill":
			tol := 4
			if len(p) > 2 {
				tol, _ = strconv.Atoi(p[2])
			}
			r, g, b, ok := parseHex(p[1])
			if !ok {
				return nil, rc.New("E_SCHEMA", "bad fill: "+p[1])
			}
			d.fills = append(d.fills, fillRule{r, g, b, tol})
		default:
			return nil, rc.New("E_SCHEMA", "unknown deny kind: "+p[0])
		}
	}
	return d, nil
}

func parseHex(s string) (r, g, b int, ok bool) {
	if len(s) != 7 || s[0] != '#' {
		return
	}
	v, err := strconv.ParseUint(s[1:], 16, 32)
	if err != nil {
		return
	}
	return int(v >> 16 & 255), int(v >> 8 & 255), int(v & 255), true
}

func abs(x int) int {
	if x < 0 {
		return -x
	}
	return x
}

// Denied reports whether a widget name/handler/fill hits the list.
func (d *DenyList) Denied(name, handler, fill string) bool {
	if d == nil {
		return false
	}
	if d.names[name] || (handler != "" && d.handlers[handler]) {
		return true
	}
	for _, re := range d.regexes {
		if (name != "" && re.MatchString(name)) || (handler != "" && re.MatchString(handler)) {
			return true
		}
	}
	if r, g, b, ok := parseHex(fill); ok {
		for _, f := range d.fills {
			if abs(r-f.r) <= f.tol && abs(g-f.g) <= f.tol && abs(b-f.b) <= f.tol {
				return true
			}
		}
	}
	return false
}

var deniedChords = map[string]bool{"alt+f4": true, "ctrl+q": true, "ctrl+w": true}

var modNames = map[string]bool{"ctrl": true, "alt": true, "shift": true, "super": true, "meta": true}

// NormChord lowercases, sorts modifiers and puts the key last.
func NormChord(chord string, mods []string) string {
	var ms []string
	key := ""
	for _, p := range append(strings.Split(strings.ToLower(chord), "+"), lower(mods)...) {
		p = strings.TrimSpace(p)
		switch {
		case p == "":
		case p == "control":
			ms = append(ms, "ctrl")
		case modNames[p]:
			ms = append(ms, p)
		default:
			key = p
		}
	}
	sort.Strings(ms)
	return strings.Join(append(dedup(ms), key), "+")
}

func lower(a []string) []string {
	o := make([]string, len(a))
	for i, s := range a {
		o[i] = strings.ToLower(s)
	}
	return o
}

func dedup(a []string) []string {
	var o []string
	for i, s := range a {
		if i == 0 || s != a[i-1] {
			o = append(o, s)
		}
	}
	return o
}

// Counters is the cross-step state the pure gate reads.
type Counters struct {
	NoEffect map[string]int
}

// SafetyIn is every input of the pure pre-act function.
type SafetyIn struct {
	Action    *action.Action
	Loc       Located
	Before    Frame
	BaseW     int
	BaseH     int
	Deny      *DenyList
	Mock      bool
	NoEffect  map[string]int
	ActionSHA string
}

// StuckKey is the (screen_id, action_sha256) stuck-counter key.
func StuckKey(screenID, actionSHA string) string { return screenID + "|" + actionSHA }

func gate(stop string, r int) Decision {
	if r == rc.Human {
		return Decision{Verdict: "STOPPED", Stop: stop, RC: r}
	}
	return Decision{Verdict: "NO_ACT", Stop: stop, RC: r}
}

// Check is the pure pre-act gate. Any non-zero Decision means zero events.
func Check(in SafetyIn) Decision {
	a := in.Action
	if in.BaseW != 0 && (in.Before.W != in.BaseW || in.Before.H != in.BaseH) {
		return Decision{Verdict: "CANNOT_SEE", Stop: "E_STALE", RC: rc.Stale}
	}
	if in.Loc.RI != 0 && in.Loc.RI != in.Before.RI {
		return Decision{Verdict: "CANNOT_SEE", Stop: "E_STALE", RC: rc.Stale}
	}
	if a.Pre.ScreenIs != "" && a.Pre.ScreenIs != in.Before.ScreenID {
		return Decision{Verdict: "NO_ACT", Stop: "screen_mismatch", RC: rc.Gate}
	}
	if a.Risk == "irrev" {
		return gate("irrev", rc.Human)
	}
	if a.Risk == "write" && Actuating(a.Op) {
		if in.Loc.MatchLevel != "L0" && in.Loc.MatchLevel != "L1" {
			return gate("match_teeth", rc.Gate)
		}
	}
	if Actuating(a.Op) || a.Target != nil {
		allowed := a.Allow != nil && in.Mock && *a.Allow == in.Loc.RID && in.Loc.RID != ""
		name := in.Loc.Name
		if name == "" && a.Target != nil && a.Target.A11y != nil {
			name = a.Target.A11y.Name
		}
		if !allowed && in.Deny.Denied(name, in.Loc.Handler, in.Loc.Fill) {
			return gate("deny_list", rc.Gate)
		}
	}
	if a.Op == "key" && deniedChords[NormChord(a.Args.Chord, a.Args.Mods)] {
		return gate("key_denied", rc.Gate)
	}
	if a.Op == "type" && in.Loc.AtRole == "terminal" {
		return gate("typed_into_shell", rc.Gate)
	}
	if in.NoEffect[StuckKey(in.Before.ScreenID, in.ActionSHA)] >= 3 {
		return gate("stuck", rc.Gate)
	}
	return Decision{}
}

// CycleHit reports whether the tail of hist is a pattern of length 2..4
// repeated 3 times (length 1 is the stuck rule's job).
func CycleHit(hist []string) bool {
	for l := 2; l <= 4; l++ {
		n := 3 * l
		if len(hist) < n {
			continue
		}
		t := hist[len(hist)-n:]
		distinct := false
		for _, x := range t[:l] {
			if x != t[0] {
				distinct = true
			}
		}
		if !distinct {
			continue
		}
		ok := true
		for i := l; i < n; i++ {
			if t[i] != t[i-l] {
				ok = false
				break
			}
		}
		if ok {
			return true
		}
	}
	return false
}

func errf(name, f string, a ...any) error { return rc.New(name, fmt.Sprintf(f, a...)) }
