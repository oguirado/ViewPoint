package ramcapture

import (
	"math"
	"strings"
	"testing"
)

const psi = "some avg10=0.01 avg60=1.25 avg300=10.000 total=100\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
const mem = "MemTotal: 100000 kB\nMemAvailable: 50000 kB\n"

func input(phase string, clock uint64) Raw {
	return Raw{WorkloadID: "game-A", InstanceID: "instance-1", BaselineID: "before-boot", Phase: phase,
		AcquisitionScope: "workload-cgroup", CgroupScope: "cg-1", Unit: "bytes", MonotonicNS: clock,
		MeminfoText: mem, CurrentText: "1024", PeakText: "1024", EventsText: "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n", PressureText: psi,
		HostPressureText: psi, HostPressureScope: "host", Outcome: "success"}
}
func recorder(t *testing.T) *Recorder {
	t.Helper()
	r, e := NewRecorder(Policy{MaxAgeNS: 100, SafetyReserveBytes: 1024})
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func TestPositivePhases(t *testing.T) {
	r := recorder(t)
	base, e := r.Record(input("baseline", 10), 20)
	if e != nil || !base.Valid || base.CompleteForPlanning {
		t.Fatalf("baseline: %+v, %v", base, e)
	}
	boot := input("boot", 30)
	boot.CurrentText = "4096"
	boot.PeakText = "5120"
	a, e := r.Record(boot, 35)
	if e != nil || !a.CompleteForPlanning || *a.IncrementalSteadyBytes != 3072 || *a.IncrementalPeakBytes != 4096 {
		t.Fatalf("boot: %+v %v", a, e)
	}
	think := input("THINK", 40)
	think.CurrentText = "3072"
	think.PeakText = "7168"
	b, e := r.Record(think, 45)
	if e != nil || !b.CompleteForPlanning || *b.IncrementalSteadyBytes != 2048 || *b.IncrementalPeakBytes != 6144 {
		t.Fatalf("THINK: %+v %v", b, e)
	}
	collection := input("collection", 50)
	collection.CurrentText = "2048"
	collection.PeakText = "8192"
	collection.BackgroundClient = &Demand{Scope: "client-other", Unit: "bytes", Bytes: 99}
	collection.Seat = &Demand{Scope: "seat-other", Unit: "bytes", Bytes: 88}
	collection.Collector = &Demand{Scope: "collector-other", Unit: "bytes", Bytes: 77}
	limit := uint64(4096)
	collection.LimitBytes = &limit
	c, e := r.Record(collection, 55)
	if e != nil || !c.CompleteForPlanning || *c.IncrementalSteadyBytes != 1024 || *c.IncrementalPeakBytes != 7168 {
		t.Fatalf("collection: %+v %v", c, e)
	}
	if c.Sample.Host.AvailableBytes == nil || *c.Sample.Host.AvailableBytes != 50000*1024 || c.Sample.HostPressure.Some.Avg60.Value != 125 || c.Sample.HostPressure.Some.Avg60.Scale != 100 {
		t.Fatal("host facts lost or PSI rounded")
	}
	if c.Raw.BackgroundClient.Bytes != 99 || c.Raw.LimitBytes == nil || *c.Raw.LimitBytes != 4096 {
		t.Fatal("context has been subtracted or discarded")
	}
	if len(r.Records) != 4 {
		t.Fatal("lost phase")
	}
}
func TestPreserveFailedOOMCensored(t *testing.T) {
	r := recorder(t)
	_, _ = r.Record(input("baseline", 10), 11)
	for i, outcome := range []string{"failed", "oom", "censored"} {
		x := input("THINK", uint64(20+i))
		x.Outcome = outcome
		x.Reason = "observed " + outcome
		x.PeakText = "1024"
		rec, err := r.Record(x, uint64(21+i))
		if err != nil || rec.CompleteForPlanning || rec.Reason != "" || len(rec.Digest) != 64 || rec.Raw.Reason == "" {
			t.Fatalf("lost %s: %+v %v", outcome, rec, err)
		}
	}
	if len(r.Records) != 4 {
		t.Fatalf("lost failures: %d", len(r.Records))
	}
}
func TestMustFailTable(t *testing.T) {
	cases := map[string]func(*Raw){
		"cap-as-measurement":      func(x *Raw) { x.CapAsMeasurement = true },
		"unit-confusion":          func(x *Raw) { x.Unit = "MiB" },
		"scope-mismatch":          func(x *Raw) { x.AcquisitionScope = "host" },
		"stale-sample":            func(x *Raw) { x.MonotonicNS = 1 },
		"peak-below-current":      func(x *Raw) { x.PeakText = "5"; x.CurrentText = "6" },
		"missing-peak":            func(x *Raw) { x.PeakText = "" },
		"missing-outcome-reason":  func(x *Raw) { x.Outcome = "oom"; x.Reason = "" },
		"unmatched-baseline":      func(x *Raw) { x.BaselineID = "other" },
		"baseline-scope-mismatch": func(x *Raw) { x.CgroupScope = "cg-else" },
		"aux-scope-mismatch":      func(x *Raw) { x.Seat = &Demand{Scope: "cg-1", Unit: "bytes", Bytes: 10} },
		"meminfo-wrong-unit":      func(x *Raw) { x.MeminfoText = "MemTotal: 42 MB\nMemAvailable: 20 kB\n" },
		"memory-overflow":         func(x *Raw) { x.CurrentText = "18446744073709551616" },
		"pressure-overflow":       func(x *Raw) { x.PressureText = "some avg10=0.00 avg60=1.1 avg300=0.00 total=18446744073709551616" },
		"trailing-current-units":  func(x *Raw) { x.CurrentText = "5 kB" },
		"duplicate-events":        func(x *Raw) { x.EventsText = "oom 0\noom 1" },
		"hugepage-no-provenance":  func(x *Raw) { x.MeminfoText = mem + "HugePages_Total: 2\n" },
		"oversized-sample":        func(x *Raw) { x.CurrentText = strings.Repeat("1", maxInputBytes+1) },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			r := recorder(t)
			_, e := r.Record(input("baseline", 10), 11)
			if e != nil {
				t.Fatal(e)
			}
			x := input("THINK", 20)
			change(&x)
			_, e = r.Record(x, 125)
			if e == nil {
				t.Fatal("MUST-FAIL passed")
			}
			if len(r.Records) != 2 || r.Records[1].Digest == "" {
				t.Fatal("invalid evidence silently dropped")
			}
		})
	}
}
func TestMissingAllowedStillIneligible(t *testing.T) {
	r, e := NewRecorder(Policy{MaxAgeNS: 100, SafetyReserveBytes: 1024, AllowedMissing: map[string]bool{"peak": true}})
	if e != nil {
		t.Fatal(e)
	}
	_, _ = r.Record(input("baseline", 10), 11)
	x := input("THINK", 20)
	x.PeakText = ""
	record, e := r.Record(x, 21)
	if e != nil || !record.Valid || record.CompleteForPlanning || record.Sample.PeakBytes != nil || len(record.Absent) == 0 {
		t.Fatalf("allowed missing became measurement: %+v %v", record, e)
	}
}
func TestCounterResetAndPeakReset(t *testing.T) {
	for _, name := range []string{"event", "peak"} {
		t.Run(name, func(t *testing.T) {
			r := recorder(t)
			b := input("baseline", 10)
			b.EventsText = "oom 2\n"
			b.PeakText = "6000"
			b.CurrentText = "1000"
			_, e := r.Record(b, 11)
			if e != nil {
				t.Fatal(e)
			}
			x := input("THINK", 20)
			x.CurrentText = "2000"
			x.PeakText = "7000"
			x.EventsText = "oom 3\n"
			if name == "event" {
				x.EventsText = "oom 1\n"
			} else {
				x.PeakText = "5000"
			}
			if _, e := r.Record(x, 21); e == nil {
				t.Fatal("counter reset accepted")
			}
		})
	}
}
func TestParsers(t *testing.T) {
	p, e := ParsePressure(psi)
	if e != nil || p.Some.Avg10.Value != 1 || p.Some.Avg10.Scale != 100 || p.Some.Avg300.Value != 10000 || p.Some.Avg300.Scale != 1000 {
		t.Fatalf("PSI exact %v %+v", e, p)
	}
	for _, x := range []string{"some avg10=100.01 avg60=0 avg300=0 total=1", "some avg10=0. avg60=0 avg300=0 total=1", "some avg10=0.1 avg60=0 avg300=0 total=-1", "some avg10=0.1 avg60=0 avg300=0", "full avg10=0 avg60=0 avg300=0 total=0"} {
		if _, e := ParsePressure(x); e == nil {
			t.Errorf("accepted malformed pressure %q", x)
		}
	}
	if _, e := ParseMeminfo("MemTotal: 18014398509481984 kB\n", 0, ""); e == nil {
		t.Fatal("expected overflow")
	}
	pgs, e := ParseMeminfo("MemTotal: 2 kB\nMemAvailable: 1 kB\nHugePages_Total: 3\n", 4096, "caller/sysconf")
	if e != nil || *pgs.HugePagesBytes != 12288 {
		t.Fatalf("page-size conversion %+v %v", pgs, e)
	}
	if _, e := singleBytes("-1"); e == nil {
		t.Fatal("negative bytes accepted")
	}
	if _, e := multiply(math.MaxUint64, 2); e == nil {
		t.Fatal("overflow accepted")
	}
}
func TestPolicyAndMonotonic(t *testing.T) {
	if _, e := NewRecorder(Policy{}); e == nil {
		t.Fatal("no policy accepted")
	}
	r := recorder(t)
	_, _ = r.Record(input("baseline", 10), 11)
	x := input("THINK", 10)
	if _, e := r.Record(x, 11); e == nil {
		t.Fatal("same time accepted")
	}
	x.MonotonicNS = 20
	x.CgroupScope = "cg-new"
	if _, e := r.Record(x, 21); e == nil {
		t.Fatal("different cgroup accepted")
	}
}

func TestSafetyReserve(t *testing.T) {
	r, e := NewRecorder(Policy{MaxAgeNS: 100, SafetyReserveBytes: 60000 * 1024})
	if e != nil {
		t.Fatal(e)
	}
	_, _ = r.Record(input("baseline", 10), 11)
	x := input("THINK", 20)
	x.CurrentText = "4000"
	x.PeakText = "5000"
	got, e := r.Record(x, 21)
	if e != nil || got.CompleteForPlanning || got.ReserveSatisfied {
		t.Fatalf("safety reserve ignored: %+v %v", got, e)
	}
}
