package delegationcapture

import (
    "math"
    "strings"
    "testing"
    "time"
)

func validFixture() (Evidence, Policy, time.Time) {
    now := time.Date(2026, 10, 20, 10, 0, 0, 0, time.UTC)
    stamp := now.Add(-time.Second)
    group := "/workers.slice/seat.service"
    e := Evidence{
        Scope: "systemd-unit", Owner: "host-seat-operator", Unit: "seat.service", CapturedAt: stamp,
        SystemdText: "Delegate=yes\nControlGroup=" + group + "\n",
        Ancestors: []Snapshot{
            {Path: "/", Owner: "host-seat-operator", Unit: "seat.service", CapturedAt: stamp,
                Controllers: "cpu cpuset io memory pids", SubtreeControl: "cpuset memory pids",
                CPUsEffective: "0-7", MemsEffective: "0-1", MemoryMax: "max", SwapMax: "max", PIDsMax: "max"},
            {Path: "/workers.slice", Owner: "host-seat-operator", Unit: "seat.service", CapturedAt: stamp,
                Controllers: "cpuset memory pids", SubtreeControl: "+cpuset +memory +pids",
                CPUsEffective: "2-5", MemsEffective: "0", MemoryMax: "2048", SwapMax: "max", PIDsMax: "64"},
            {Path: group, Owner: "host-seat-operator", Unit: "seat.service", CapturedAt: stamp,
                Controllers: "cpuset memory pids", SubtreeControl: "",
                CPUsEffective: "4-5", MemsEffective: "0", MemoryMax: "4096", SwapMax: "0", PIDsMax: "128"},
        },
    }
    p := Policy{
        Owner: "host-seat-operator", Unit: "seat.service", ControlGroup: group,
        WorkerCPUs: "4-5", InteractiveCPUs: "0-1",
        MaxAge: time.Minute, MinMemoryBytes: 1024, MaxMemoryBytes: 3072,
        MinPIDs: 32, MaxPIDs: 80, RequireZeroSwap: true,
    }
    return e, p, now
}

func TestEffectiveHierarchyPassAndLimits(t *testing.T) {
    e, p, now := validFixture()
    got := Evaluate(e, p, now)
    if got.Verdict != Pass { t.Fatalf("valid complete hierarchy: %+v", got) }
    if len(got.Ancestors) != 3 { t.Fatalf("expected all ancestor reports, got %d", len(got.Ancestors)) }
    if got.MemoryLimitingAncestor != "/workers.slice" || got.EffectiveMemory.Unbounded || got.EffectiveMemory.Value != 2048 {
        t.Fatalf("incorrect inherited memory limit: %+v", got)
    }
    if got.PIDsLimitingAncestor != "/workers.slice" || got.EffectivePIDs.Value != 64 {
        t.Fatalf("incorrect inherited PID limit: %+v", got)
    }
    if got.SwapLimitingAncestor != "/workers.slice/seat.service" || got.EffectiveSwap.Unbounded || got.EffectiveSwap.Value != 0 {
        t.Fatalf("incorrect inherited swap limit: %+v", got)
    }
    if !got.Ancestors[0].MemoryMax.Unbounded || !got.Ancestors[0].SwapMax.Unbounded {
        t.Fatalf("max incorrectly treated as zero: %+v", got.Ancestors[0])
    }
}

func TestMustFailEvidenceFixtures(t *testing.T) {
    tests := []struct {
        name string
        change func(*Evidence, *Policy, *time.Time)
        class string
    }{
        {"unavailable cpuset controller", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[1].Controllers = "memory pids"
        }, Ausente},
        {"Delegate configured without effective delegation", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[0].SubtreeControl = "memory pids"
        }, Ausente},
        {"worker versus interactive overlap", func(_ *Evidence, p *Policy, _ *time.Time) {
            p.InteractiveCPUs = "1,5"
        }, Containment},
        {"ignored parent memory limit", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[1].MemoryMax = "512" // child still says 4096; inherited limit is 512
        }, Containment},
        {"ignored parent PID limit", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[1].PIDsMax = "8"
        }, Containment},
        {"omitted ancestor", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors = append(e.Ancestors[:1], e.Ancestors[2:]...)
        }, Ausente},
        {"unknown ancestor", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[1].Path = "/unknown.slice"
        }, Ausente},
        {"nonzero swap", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[2].SwapMax = "1024"
        }, Containment},
        {"unbounded swap", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[2].SwapMax = "max"
        }, Containment},
        {"range overflow", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[2].CPUsEffective = "4-18446744073709551616"
        }, Ausente},
        {"contradictory CPU snapshots", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[2].CPUsEffective = "4-6"
        }, Containment},
        {"contradictory timestamps", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[2].CapturedAt = e.CapturedAt.Add(time.Second)
        }, Ausente},
        {"misbound owner", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.Ancestors[1].Owner = "other-operator"
        }, Ausente},
        {"stale capture", func(_ *Evidence, _ *Policy, now *time.Time) {
            *now = now.Add(5 * time.Minute)
        }, Ausente},
        {"missing policy", func(_ *Evidence, p *Policy, _ *time.Time) {
            p.MaxAge = 0
        }, Ausente},
        {"missing zero-swap policy", func(_ *Evidence, p *Policy, _ *time.Time) {
            p.RequireZeroSwap = false
        }, Ausente},
        {"contradictory systemd ControlGroup", func(e *Evidence, _ *Policy, _ *time.Time) {
            e.SystemdText = "Delegate=yes\nControlGroup=/other.service"
        }, Ausente},
    }
    for _, tc := range tests {
        t.Run(tc.name, func(t *testing.T) {
            e, p, now := validFixture()
            tc.change(&e, &p, &now)
            got := Evaluate(e, p, now)
            if got.Verdict != Hold || got.Class != tc.class {
                t.Fatalf("must fail closed as %s: %+v", tc.class, got)
            }
        })
    }
}

func TestCheckedIntervalsAndLimits(t *testing.T) {
    valid := []string{"0", "0-0,1", "1,3-8", "0-18446744073709551615"}
    for _, input := range valid {
        got, err := ParseIntervals(input)
        if err != nil || len(got) == 0 { t.Errorf("%q should parse: %v", input, err) }
    }
    invalid := []string{"", "-1", "5-4", "0-18446744073709551616", "18446744073709551616", "0,0", "0-3,2-5", "4,", "+2", "1 -3"}
    for _, input := range invalid {
        if _, err := ParseIntervals(input); err == nil { t.Errorf("%q must reject", input) }
    }
    max, err := ParseLimit("max\n")
    if err != nil || !max.Unbounded || max.Value != 0 { t.Fatalf("max incorrectly handled: %+v %v", max, err) }
    zero, err := ParseLimit("0")
    if err != nil || zero.Unbounded || zero.Value != 0 { t.Fatalf("finite zero incorrectly handled: %+v %v", zero, err) }
    full, err := ParseLimit("18446744073709551615")
    if err != nil || full.Value != math.MaxUint64 { t.Fatalf("uint64 bound parse: %+v %v", full, err) }
    for _, s := range []string{"", "-1", "18446744073709551616", "maxx", "1 2"} {
        if _, err := ParseLimit(s); err == nil { t.Errorf("finite %q must reject", s) }
    }
    if !strings.Contains(Evaluate(Evidence{}, Policy{}, time.Time{}).Detail, "policy") { t.Fatal("missing policy not explained") }
}
