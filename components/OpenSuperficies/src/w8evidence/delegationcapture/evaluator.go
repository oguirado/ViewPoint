// Package delegationcapture evaluates passive, caller-supplied cgroup v2 evidence.
// It neither probes a host nor grants or changes delegation.
package delegationcapture

import (
    "fmt"
    "sort"
    "strconv"
    "strings"
    "time"
)

const (
    Pass = "PASS"
    Hold = "HOLD"
    Ausente = "AUSENTE"
    Containment = "CONTAINMENT"
)

// Interval is an inclusive unsigned CPU or NUMA-node range. It is deliberately
// not expanded into individual numbers: untrusted ranges can be very large.
type Interval struct {
    First uint64
    Last uint64
}

// Limit keeps the distinction between cgroup v2's max and the finite zero.
type Limit struct {
    Unbounded bool
    Value uint64
}

// Snapshot is one captured cgroup directory, supplied by a trusted caller.
// Owner, Unit, and CapturedAt bind every record to the same capture scope.
type Snapshot struct {
    Path string
    Owner string
    Unit string
    CapturedAt time.Time
    Controllers string
    SubtreeControl string
    CPUsEffective string
    MemsEffective string
    MemoryMax string
    SwapMax string
    PIDsMax string
}

// Evidence holds passive systemctl show text and the complete root-to-unit
// cgroup lineage. No file paths are opened by this package.
type Evidence struct {
    Scope string // exactly "systemd-unit"
    Owner string
    Unit string
    CapturedAt time.Time
    SystemdText string
    Ancestors []Snapshot
}

// Policy must be supplied explicitly by the owner. Bounds are effective
// limits for the unit, not merely its local cgroup file values.
type Policy struct {
    Owner string
    Unit string
    ControlGroup string
    WorkerCPUs string
    InteractiveCPUs string
    MaxAge time.Duration
    MinMemoryBytes uint64
    MaxMemoryBytes uint64
    MinPIDs uint64
    MaxPIDs uint64
    RequireZeroSwap bool
}

// AncestorFinding retains each raw restriction and its accumulated bound.
type AncestorFinding struct {
    Path string
    CPUsEffective []Interval
    MemsEffective []Interval
    MemoryMax Limit
    SwapMax Limit
    PIDsMax Limit
    InheritedMemory Limit
    InheritedSwap Limit
    InheritedPIDs Limit
}

// Decision describes verified success or a fail-closed HOLD. Class AUSENTE
// denotes unknown/unproven evidence; CONTAINMENT denotes a proven violation.
type Decision struct {
    Verdict string
    Class string
    Detail string
    Ancestors []AncestorFinding
    EffectiveMemory Limit
    EffectiveSwap Limit
    EffectivePIDs Limit
    MemoryLimitingAncestor string
    SwapLimitingAncestor string
    PIDsLimitingAncestor string
}

func hold(class, detail string, d Decision) Decision {
    d.Verdict, d.Class, d.Detail = Hold, class, detail
    return d
}

// ParseIntervals accepts nonnegative comma-separated singleton and inclusive
// ranges. Checked uint64 conversion and interval operations avoid overflow and
// memory amplification, including on 0-18446744073709551615.
func ParseIntervals(raw string) ([]Interval, error) {
    s := strings.TrimSpace(raw)
    if s == "" { return nil, fmt.Errorf("empty range set") }
    var ranges []Interval
    for _, item := range strings.Split(s, ",") {
        if item == "" || strings.TrimSpace(item) != item { return nil, fmt.Errorf("malformed range") }
        a, b, ranged := strings.Cut(item, "-")
        if a == "" || (ranged && (b == "" || strings.Contains(b, "-"))) { return nil, fmt.Errorf("malformed range") }
        first, err := strconv.ParseUint(a, 10, 64)
        if err != nil { return nil, fmt.Errorf("range start: %w", err) }
        last := first
        if ranged {
            last, err = strconv.ParseUint(b, 10, 64)
            if err != nil { return nil, fmt.Errorf("range end: %w", err) }
        }
        if last < first { return nil, fmt.Errorf("reversed range") }
        ranges = append(ranges, Interval{first, last})
    }
    sort.Slice(ranges, func(i, j int) bool { return ranges[i].First < ranges[j].First })
    normalized := make([]Interval, 0, len(ranges))
    for _, current := range ranges {
        n := len(normalized)
        if n == 0 { normalized = append(normalized, current); continue }
        previous := &normalized[n-1]
        if current.First <= previous.Last { return nil, fmt.Errorf("overlapping/duplicate range") }
        // Guard the increment when Last is math.MaxUint64.
        if previous.Last != ^uint64(0) && current.First == previous.Last+1 {
            previous.Last = current.Last
        } else {
            normalized = append(normalized, current)
        }
    }
    return normalized, nil
}

func subset(child, parent []Interval) bool {
    for _, c := range child {
        found := false
        for _, p := range parent {
            if p.First <= c.First && c.Last <= p.Last { found = true; break }
        }
        if !found { return false }
    }
    return true
}

func intersects(a, b []Interval) bool {
    for _, x := range a {
        for _, y := range b {
            if x.First <= y.Last && y.First <= x.Last { return true }
        }
    }
    return false
}

func equalRanges(a, b []Interval) bool {
    if len(a) != len(b) { return false }
    for i := range a { if a[i] != b[i] { return false } }
    return true
}

// ParseLimit accepts exactly a finite unsigned decimal or max.
func ParseLimit(raw string) (Limit, error) {
    s := strings.TrimSpace(raw)
    if s == "max" { return Limit{Unbounded: true}, nil }
    if s == "" { return Limit{}, fmt.Errorf("missing limit") }
    n, err := strconv.ParseUint(s, 10, 64)
    if err != nil { return Limit{}, fmt.Errorf("invalid finite limit: %w", err) }
    return Limit{Value: n}, nil
}

func inherit(previous, current Limit) (Limit, bool) {
    if previous.Unbounded { return current, !current.Unbounded }
    if !current.Unbounded && current.Value < previous.Value { return current, true }
    return previous, false
}

func controllers(raw string) map[string]bool {
    set := make(map[string]bool)
    for _, token := range strings.Fields(raw) {
        if strings.HasPrefix(token, "-") { continue }
        set[strings.TrimPrefix(token, "+")] = true
    }
    return set
}

func hasRequired(raw string) bool {
    s := controllers(raw)
    return s["cpuset"] && s["memory"] && s["pids"]
}

func parseSystemd(raw string) (string, bool) {
    var group string
    foundGroup, foundDelegate := false, false
    for _, row := range strings.Split(raw, "\n") {
        row = strings.TrimSpace(row)
        if row == "" { continue }
        key, val, ok := strings.Cut(row, "=")
        if !ok { return "", false }
        switch key {
        case "ControlGroup":
            if foundGroup || val == "" { return "", false }
            foundGroup, group = true, val
        case "Delegate":
            if foundDelegate || val != "yes" { return "", false }
            foundDelegate = true
        default:
            // Only the two explicitly requested properties are permitted.
            return "", false
        }
    }
    return group, foundGroup && foundDelegate
}

func pathLineage(path string) ([]string, bool) {
    if !strings.HasPrefix(path, "/") || strings.HasSuffix(path, "/") || strings.Contains(path, "//") || path == "/" { return nil, false }
    out := []string{"/"}
    current := ""
    for _, part := range strings.Split(strings.TrimPrefix(path, "/"), "/") {
        if part == "" || part == "." || part == ".." { return nil, false }
        current += "/" + part
        out = append(out, current)
    }
    return out, true
}

// Evaluate is deterministic: now is an injected clock instant, not a host
// clock read. It never changes state. A configured Delegate=yes alone is not
// sufficient: the entire controller-enabled ancestry must be evidenced.
func Evaluate(e Evidence, p Policy, now time.Time) Decision {
    d := Decision{Verdict: Hold}
    missing := func(reason string) Decision { return hold(Ausente, reason, d) }
    violation := func(reason string) Decision { return hold(Containment, reason, d) }
    if p.Owner == "" || p.Unit == "" || p.ControlGroup == "" || p.WorkerCPUs == "" || p.InteractiveCPUs == "" ||
        p.MaxAge <= 0 || p.MinMemoryBytes == 0 || p.MaxMemoryBytes < p.MinMemoryBytes ||
        p.MinPIDs == 0 || p.MaxPIDs < p.MinPIDs || !p.RequireZeroSwap || now.IsZero() {
        return missing("complete owner, CPU, freshness, memory, PID and zero-swap policy required")
    }
    worker, err := ParseIntervals(p.WorkerCPUs)
    if err != nil { return missing("invalid approved worker CPU partition: " + err.Error()) }
    interactive, err := ParseIntervals(p.InteractiveCPUs)
    if err != nil { return missing("invalid interactive CPU partition: " + err.Error()) }
    if intersects(worker, interactive) { return violation("approved worker and interactive CPUs overlap") }
    lineage, ok := pathLineage(p.ControlGroup)
    if !ok { return missing("invalid or unknown unit ControlGroup path") }
    if e.Scope != "systemd-unit" || e.Owner != p.Owner || e.Unit != p.Unit || e.CapturedAt.IsZero() {
        return missing("capture scope or owner binding unavailable")
    }
    if now.Before(e.CapturedAt) || now.Sub(e.CapturedAt) > p.MaxAge { return missing("stale or future capture") }
    group, valid := parseSystemd(e.SystemdText)
    if !valid || group != p.ControlGroup { return missing("systemd Delegate/ControlGroup evidence unavailable or contradictory") }
    if len(e.Ancestors) != len(lineage) { return missing("omitted or unknown cgroup ancestor") }

    var parentCPUs, parentMems []Interval
    memory := Limit{Unbounded: true}
    pids := Limit{Unbounded: true}
    swap := Limit{Unbounded: true}
    for i, snap := range e.Ancestors {
        if snap.Path != lineage[i] || snap.Owner != e.Owner || snap.Unit != e.Unit || !snap.CapturedAt.Equal(e.CapturedAt) {
            return missing("unknown ancestor, owner mismatch or contradictory capture instant")
        }
        if !hasRequired(snap.Controllers) { return missing("cpuset, memory or pids controller unavailable at " + snap.Path) }
        if i < len(e.Ancestors)-1 && !hasRequired(snap.SubtreeControl) {
            return missing("effective subtree delegation unavailable at " + snap.Path)
        }
        cpus, cpuErr := ParseIntervals(snap.CPUsEffective)
        mems, memErr := ParseIntervals(snap.MemsEffective)
        if cpuErr != nil || memErr != nil { return missing("effective cpuset CPU/NUMA evidence unavailable at " + snap.Path) }
        if i > 0 && (!subset(cpus, parentCPUs) || !subset(mems, parentMems)) {
            return violation("contradictory inherited CPU or NUMA node sets at " + snap.Path)
        }
        mem, em := ParseLimit(snap.MemoryMax)
        pid, ep := ParseLimit(snap.PIDsMax)
        sw, es := ParseLimit(snap.SwapMax)
        if em != nil || ep != nil || es != nil { return missing("invalid or unavailable cgroup limit at " + snap.Path) }
        if (!mem.Unbounded && mem.Value == 0) || (!pid.Unbounded && pid.Value == 0) {
            return violation("memory or PID bound prohibits any worker at " + snap.Path)
        }
        var bounded bool
        memory, bounded = inherit(memory, mem)
        if bounded { d.MemoryLimitingAncestor = snap.Path }
        pids, bounded = inherit(pids, pid)
        if bounded { d.PIDsLimitingAncestor = snap.Path }
        swap, bounded = inherit(swap, sw)
        if bounded { d.SwapLimitingAncestor = snap.Path }
        d.Ancestors = append(d.Ancestors, AncestorFinding{
            Path: snap.Path, CPUsEffective: cpus, MemsEffective: mems,
            MemoryMax: mem, SwapMax: sw, PIDsMax: pid,
            InheritedMemory: memory, InheritedSwap: swap, InheritedPIDs: pids,
        })
        parentCPUs, parentMems = cpus, mems
    }
    d.EffectiveMemory, d.EffectivePIDs, d.EffectiveSwap = memory, pids, swap
    if !equalRanges(parentCPUs, worker) { return violation("unit effective CPUs do not match approved worker partition") }
    if intersects(parentCPUs, interactive) { return violation("unit effective CPUs overlap interactive CPUs") }
    if memory.Unbounded || pids.Unbounded { return missing("finite inherited memory and PID bounds unavailable") }
    if memory.Value < p.MinMemoryBytes || memory.Value > p.MaxMemoryBytes { return violation("inherited memory bound outside approved policy") }
    if pids.Value < p.MinPIDs || pids.Value > p.MaxPIDs { return violation("inherited PID bound outside approved policy") }
    if swap.Unbounded || swap.Value != 0 { return violation("zero swap not effectively enforced") }
    d.Verdict, d.Class, d.Detail = Pass, "", "complete ownership-bound, fresh, effective hierarchy"
    return d
}
