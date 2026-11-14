package gobernador

import (
	"errors"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func vecPtr(m, c, p, f int64) *ResourceVector {
	return &ResourceVector{
		MemoryBytes: m,
		CPUMilli:    c,
		PIDs:        p,
		OpenFiles:   f,
	}
}

func baseTelemetryStore(version uint64, monoNS int64) *W8SnapshotStore {
	return NewW8SnapshotStore(&W8TelemetrySnapshot{
		Version:           version,
		MonotonicNS:       monoNS,
		MemAvailableBytes: 64 << 30,
		CapsuleUsageBytes: 0,
		CPUPSI10:          1.5,
		Cgroups:           map[string]W8CgroupTelemetry{},
	})
}

func defaultPlacement(child string, childLim, sliceLim, rootLim *ResourceVector) PlacementSpec {
	return PlacementSpec{
		Child: CgroupLimit{Cgroup: child, Limit: childLim},
		Ancestors: []CgroupLimit{
			{Cgroup: "osworker.slice/w8-capsules.slice", Limit: sliceLim},
			{Cgroup: "osworker.slice", Limit: rootLim},
		},
	}
}

func TestW8MustFailNullOrZeroCeiling(t *testing.T) {
	nowMono := int64(10_000_000_000)
	store := baseTelemetryStore(1, nowMono)
	validLim := vecPtr(8<<30, 4000, 512, 8192)
	validReqVec := ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024}

	cases := []struct {
		name        string
		hostCeiling *ResourceVector
		placement   PlacementSpec
		reqVec      ResourceVector
	}{
		{
			name:        "nil_host_ceiling",
			hostCeiling: nil,
			placement:   defaultPlacement("cg-child-1", validLim, validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "zero_host_memory_ceiling",
			hostCeiling: vecPtr(0, 4000, 512, 8192),
			placement:   defaultPlacement("cg-child-1", validLim, validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "zero_host_cpu_ceiling",
			hostCeiling: vecPtr(8<<30, 0, 512, 8192),
			placement:   defaultPlacement("cg-child-1", validLim, validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "zero_host_pids_ceiling",
			hostCeiling: vecPtr(8<<30, 4000, 0, 8192),
			placement:   defaultPlacement("cg-child-1", validLim, validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "zero_host_open_files_ceiling",
			hostCeiling: vecPtr(8<<30, 4000, 512, 0),
			placement:   defaultPlacement("cg-child-1", validLim, validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "nil_child_limit",
			hostCeiling: validLim,
			placement:   defaultPlacement("cg-child-1", nil, validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "zero_child_dimension",
			hostCeiling: validLim,
			placement:   defaultPlacement("cg-child-1", vecPtr(4<<30, 2000, 0, 4096), validLim, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "nil_ancestor_limit",
			hostCeiling: validLim,
			placement:   defaultPlacement("cg-child-1", validLim, nil, validLim),
			reqVec:      validReqVec,
		},
		{
			name:        "zero_ancestor_dimension",
			hostCeiling: validLim,
			placement:   defaultPlacement("cg-child-1", validLim, validLim, vecPtr(8<<30, 4000, 512, -1)),
			reqVec:      validReqVec,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
			adapter := NewW8Adapter(gov, W8Config{
				HostCeiling:     tc.hostCeiling,
				MaxTelemetryAge: 5 * time.Second,
				Telemetry:       store.Source(),
				NowMono:         func() int64 { return nowMono },
			})
			dec := adapter.Admit(W8AdmissionRequest{
				ReservationID: "res-" + tc.name,
				Clase:         "terminal",
				Resources:     tc.reqVec,
				Placement:     tc.placement,
			})
			if dec.Admitted || dec.RC != W8RCInput || dec.Reason != "null_or_zero_ceiling" {
				t.Fatalf("expected refusal with rc=%d reason=null_or_zero_ceiling, got %+v", W8RCInput, dec)
			}
			if !adapter.ReservedVector().IsZero() {
				t.Fatalf("expected zero reserved vector after refusal, got %+v", adapter.ReservedVector())
			}
		})
	}
}

func TestW8MustFailArithmeticOverflow(t *testing.T) {
	base := ResourceVector{MemoryBytes: 100, CPUMilli: 100, PIDs: 100, OpenFiles: 100}

	overflowAdds := []struct {
		name  string
		other ResourceVector
	}{
		{"mem_overflow", ResourceVector{MemoryBytes: math.MaxInt64 - 50, CPUMilli: 1, PIDs: 1, OpenFiles: 1}},
		{"cpu_overflow", ResourceVector{MemoryBytes: 1, CPUMilli: math.MaxInt64 - 50, PIDs: 1, OpenFiles: 1}},
		{"pids_overflow", ResourceVector{MemoryBytes: 1, CPUMilli: 1, PIDs: math.MaxInt64 - 50, OpenFiles: 1}},
		{"files_overflow", ResourceVector{MemoryBytes: 1, CPUMilli: 1, PIDs: 1, OpenFiles: math.MaxInt64 - 50}},
		{"negative_input", ResourceVector{MemoryBytes: -1, CPUMilli: 1, PIDs: 1, OpenFiles: 1}},
	}
	for _, tc := range overflowAdds {
		t.Run("add_"+tc.name, func(t *testing.T) {
			if _, err := base.Add(tc.other); !errors.Is(err, ErrVectorOverflow) {
				t.Fatalf("expected ErrVectorOverflow on Add(%s), got %v", tc.name, err)
			}
		})
	}

	underflowSubs := []struct {
		name  string
		other ResourceVector
	}{
		{"mem_underflow", ResourceVector{MemoryBytes: 101, CPUMilli: 1, PIDs: 1, OpenFiles: 1}},
		{"cpu_underflow", ResourceVector{MemoryBytes: 1, CPUMilli: 101, PIDs: 1, OpenFiles: 1}},
		{"pids_underflow", ResourceVector{MemoryBytes: 1, CPUMilli: 1, PIDs: 101, OpenFiles: 1}},
		{"files_underflow", ResourceVector{MemoryBytes: 1, CPUMilli: 1, PIDs: 1, OpenFiles: 101}},
		{"negative_sub", ResourceVector{MemoryBytes: 1, CPUMilli: -1, PIDs: 1, OpenFiles: 1}},
	}
	for _, tc := range underflowSubs {
		t.Run("sub_"+tc.name, func(t *testing.T) {
			if _, err := base.Sub(tc.other); !errors.Is(err, ErrVectorOverflow) {
				t.Fatalf("expected ErrVectorOverflow on Sub(%s), got %v", tc.name, err)
			}
		})
	}

	// Admission-level overflow when accumulating against an already-large reservation.
	nowMono := int64(10_000_000_000)
	store := baseTelemetryStore(1, nowMono)
	hugeLim := vecPtr(math.MaxInt64, math.MaxInt64, math.MaxInt64, math.MaxInt64)
	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     hugeLim,
		MaxTelemetryAge: 5 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
	})

	dec1 := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-huge-1",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: math.MaxInt64 - 10, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
		Placement:     defaultPlacement("cg-huge-1", hugeLim, hugeLim, hugeLim),
	})
	if !dec1.Admitted {
		t.Fatalf("expected initial reservation to succeed, got %+v", dec1)
	}

	dec2 := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-huge-2",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: 20, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
		Placement:     defaultPlacement("cg-huge-2", hugeLim, hugeLim, hugeLim),
	})
	if dec2.Admitted || dec2.RC != W8RCInput || dec2.Reason != "arithmetic_overflow" {
		t.Fatalf("expected arithmetic_overflow refusal with rc=%d, got %+v", W8RCInput, dec2)
	}
}

func TestW8MustFailAggregateFitsButChildOrAncestorOverflows(t *testing.T) {
	nowMono := int64(10_000_000_000)
	store := baseTelemetryStore(1, nowMono)

	hostLim := vecPtr(16<<30, 8000, 1024, 16384)
	rootLim := vecPtr(16<<30, 8000, 1024, 16384)
	sliceLim := vecPtr(6<<30, 4000, 512, 8192)
	childTightLim := vecPtr(4<<30, 2000, 128, 4096)
	childStandardLim := vecPtr(4<<30, 2000, 256, 4096)

	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     hostLim,
		MaxTelemetryAge: 5 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
	})

	// Case A: Aggregate and ancestors fit, but child PIDs limit (128) overflows (requesting 200 PIDs).
	decChild := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-child-overflow",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: 2 << 30, CPUMilli: 1000, PIDs: 200, OpenFiles: 1024},
		Placement:     defaultPlacement("cg-child-tight", childTightLim, sliceLim, rootLim),
	})
	if decChild.Admitted || decChild.RC != W8RCUnavailable || decChild.Reason != "child_overflow" {
		t.Fatalf("expected child_overflow refusal with rc=%d, got %+v", W8RCUnavailable, decChild)
	}

	// Case B: First child under slice takes 4 GiB. Nested charges at slice and root must be 4 GiB (counted once, never summed twice).
	dec1 := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-slice-1",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: 4 << 30, CPUMilli: 2000, PIDs: 128, OpenFiles: 2048},
		Placement:     defaultPlacement("cg-child-1", childStandardLim, sliceLim, rootLim),
	})
	if !dec1.Admitted {
		t.Fatalf("expected res-slice-1 to be admitted, got %+v", dec1)
	}

	rootCharge, err := adapter.CgroupCharge("osworker.slice")
	if err != nil {
		t.Fatalf("unexpected CgroupCharge error: %v", err)
	}
	if rootCharge.MemoryBytes != 4<<30 {
		t.Fatalf("nested charge on root ancestor must be counted once (4 GiB), got %d", rootCharge.MemoryBytes)
	}

	// Second child under the same slice requests 4 GiB:
	// - Fits child-2 limit (4 GiB <= 4 GiB)
	// - Fits root limit (8 GiB <= 16 GiB)
	// - Fits host ceiling (8 GiB <= 16 GiB)
	// - Overflows intermediate ancestor slice limit (4 GiB + 4 GiB = 8 GiB > 6 GiB)
	decAncestor := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-slice-2",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: 4 << 30, CPUMilli: 1000, PIDs: 128, OpenFiles: 2048},
		Placement:     defaultPlacement("cg-child-2", childStandardLim, sliceLim, rootLim),
	})
	if decAncestor.Admitted || decAncestor.RC != W8RCUnavailable || decAncestor.Reason != "ancestor_overflow" {
		t.Fatalf("expected ancestor_overflow refusal with rc=%d, got %+v", W8RCUnavailable, decAncestor)
	}
}

func TestW8MustFailTwoRacingGrantsOnOneSnapshot(t *testing.T) {
	nowMono := int64(10_000_000_000)
	store := baseTelemetryStore(1, nowMono)

	// Host ceiling has capacity for exactly ONE 4 GiB reservation.
	oneSlotLim := vecPtr(4<<30, 2000, 256, 8192)
	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})

	// Barrier synchronizes both racing goroutines right after both have evaluated snapshot v1 at decision time.
	var arrived atomic.Int32
	proceed := make(chan struct{})
	var once sync.Once

	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     oneSlotLim,
		MaxTelemetryAge: 5 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
		OnDecisionEvaluated: func(ver uint64, seq uint64) {
			if arrived.Add(1) == 2 {
				once.Do(func() { close(proceed) })
			}
			<-proceed
		},
	})

	var wg sync.WaitGroup
	decisions := make([]W8AdmissionDecision, 2)
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			decisions[idx] = adapter.Admit(W8AdmissionRequest{
				ReservationID: fmt.Sprintf("race-res-%d", idx+1),
				Clase:         "terminal",
				Resources:     ResourceVector{MemoryBytes: 4 << 30, CPUMilli: 2000, PIDs: 256, OpenFiles: 8192},
				Placement:     defaultPlacement(fmt.Sprintf("cg-race-%d", idx+1), oneSlotLim, oneSlotLim, oneSlotLim),
			})
		}(i)
	}
	wg.Wait()

	admittedCount := 0
	refusedCount := 0
	for _, d := range decisions {
		if d.Admitted {
			admittedCount++
		} else {
			refusedCount++
			if d.RC != W8RCUnavailable {
				t.Fatalf("expected loser racing grant to fail with rc=%d, got %+v", W8RCUnavailable, d)
			}
		}
	}
	if admittedCount != 1 || refusedCount != 1 {
		t.Fatalf("two racing grants on one snapshot must admit 1 and refuse 1; got admitted=%d refused=%d decisions=%+v",
			admittedCount, refusedCount, decisions)
	}
	if adapter.ReservedVector().MemoryBytes != 4<<30 {
		t.Fatalf("expected exactly one 4 GiB reservation committed, got %+v", adapter.ReservedVector())
	}
}

func TestW8MustFailCPUTotalAlreadyFull(t *testing.T) {
	nowMono := int64(10_000_000_000)
	store := baseTelemetryStore(1, nowMono)

	// Plenty of RAM (64 GiB), PIDs (2048), OpenFiles (65536), but CPU ceiling is 4000 milli-units (4 CPUs).
	hostLim := vecPtr(64<<30, 4000, 2048, 65536)
	childLim := vecPtr(16<<30, 4000, 1024, 16384)

	t.Run("cpu_filled_via_w8_adapter", func(t *testing.T) {
		gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
		adapter := NewW8Adapter(gov, W8Config{
			HostCeiling:     hostLim,
			MaxTelemetryAge: 5 * time.Second,
			Telemetry:       store.Source(),
			NowMono:         func() int64 { return nowMono },
		})

		dec1 := adapter.Admit(W8AdmissionRequest{
			ReservationID: "cpu-1",
			Clase:         "terminal",
			Resources:     ResourceVector{MemoryBytes: 2 << 30, CPUMilli: 4000, PIDs: 128, OpenFiles: 1024},
			Placement:     defaultPlacement("cg-cpu-1", childLim, hostLim, hostLim),
		})
		if !dec1.Admitted {
			t.Fatalf("expected first CPU reservation to succeed, got %+v", dec1)
		}

		// CPU total is now full (4000 / 4000 mCPU). Next request must fail with cpu_ceiling.
		dec2 := adapter.Admit(W8AdmissionRequest{
			ReservationID: "cpu-2",
			Clase:         "terminal",
			Resources:     ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 500, PIDs: 64, OpenFiles: 512},
			Placement:     defaultPlacement("cg-cpu-2", childLim, hostLim, hostLim),
		})
		if dec2.Admitted || dec2.RC != W8RCUnavailable || dec2.Reason != "cpu_ceiling" {
			t.Fatalf("expected cpu_ceiling refusal with rc=%d when CPU total already full, got %+v", W8RCUnavailable, dec2)
		}
	})

	t.Run("cpu_filled_via_srf_gobernador_line_249", func(t *testing.T) {
		gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
		// Legacy SrfGobernador.Admitir accumulates g.cpus += s.CPUs at gobernador.go:249 without checking a ceiling.
		srfDec := gov.Admitir(SrfSolicitudAdmision{
			Clase:     "terminal",
			Reserva:   2 << 30,
			CPUs:      4.0,
			CandadoOK: true,
		})
		if !srfDec.Admitida {
			t.Fatalf("expected legacy SrfGobernador.Admitir to succeed, got %+v", srfDec)
		}

		adapter := NewW8Adapter(gov, W8Config{
			HostCeiling:     hostLim,
			MaxTelemetryAge: 5 * time.Second,
			Telemetry:       store.Source(),
			NowMono:         func() int64 { return nowMono },
		})

		// The W8 adapter closes the gap by comparing accumulated CPUs against the ceiling.
		dec := adapter.Admit(W8AdmissionRequest{
			ReservationID: "cpu-after-legacy",
			Clase:         "terminal",
			Resources:     ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 500, PIDs: 64, OpenFiles: 512},
			Placement:     defaultPlacement("cg-cpu-legacy", childLim, hostLim, hostLim),
		})
		if dec.Admitted || dec.RC != W8RCUnavailable || dec.Reason != "cpu_ceiling" {
			t.Fatalf("expected adapter to refuse with cpu_ceiling after legacy CPU accumulation, got %+v", dec)
		}
	})
}

func TestW8MustFailStaleTelemetry(t *testing.T) {
	sampledMono := int64(1_000_000_000) // 1s
	nowMono := int64(5_000_000_000)     // 5s (age = 4s > MaxTelemetryAge 2s)
	store := baseTelemetryStore(1, sampledMono)

	lim := vecPtr(16<<30, 8000, 1024, 16384)
	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     lim,
		MaxTelemetryAge: 2 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
	})

	dec := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-stale",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
		Placement:     defaultPlacement("cg-stale", lim, lim, lim),
	})
	if dec.Admitted || dec.RC != W8RCUnavailable || dec.Reason != "stale_telemetry" {
		t.Fatalf("expected stale_telemetry refusal with rc=%d, got %+v", W8RCUnavailable, dec)
	}
}

func TestW8MustFailMissingTelemetry(t *testing.T) {
	nowMono := int64(10_000_000_000)
	lim := vecPtr(16<<30, 8000, 1024, 16384)

	cases := []struct {
		name      string
		source    W8TelemetrySource
		expectWhy string
	}{
		{
			name:      "nil_source",
			source:    nil,
			expectWhy: "missing_telemetry",
		},
		{
			name: "source_error",
			source: func() (*W8TelemetrySnapshot, error) {
				return nil, errors.New("sensor offline")
			},
			expectWhy: "missing_telemetry",
		},
		{
			name: "zero_snapshot_version",
			source: func() (*W8TelemetrySnapshot, error) {
				return &W8TelemetrySnapshot{Version: 0, MonotonicNS: nowMono, MemAvailableBytes: 64 << 30}, nil
			},
			expectWhy: "missing_telemetry",
		},
		{
			name: "zero_monotonic_time",
			source: func() (*W8TelemetrySnapshot, error) {
				return &W8TelemetrySnapshot{Version: 1, MonotonicNS: 0, MemAvailableBytes: 64 << 30}, nil
			},
			expectWhy: "missing_telemetry",
		},
		{
			name: "nan_psi_telemetry",
			source: func() (*W8TelemetrySnapshot, error) {
				return &W8TelemetrySnapshot{Version: 1, MonotonicNS: nowMono, MemAvailableBytes: 64 << 30, CPUPSI10: math.NaN()}, nil
			},
			expectWhy: "non_finite_telemetry",
		},
		{
			name: "inf_psi_telemetry",
			source: func() (*W8TelemetrySnapshot, error) {
				return &W8TelemetrySnapshot{Version: 1, MonotonicNS: nowMono, MemAvailableBytes: 64 << 30, CPUPSI10: math.Inf(1)}, nil
			},
			expectWhy: "non_finite_telemetry",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
			adapter := NewW8Adapter(gov, W8Config{
				HostCeiling:     lim,
				MaxTelemetryAge: 5 * time.Second,
				Telemetry:       tc.source,
				NowMono:         func() int64 { return nowMono },
			})
			dec := adapter.Admit(W8AdmissionRequest{
				ReservationID: "res-" + tc.name,
				Clase:         "terminal",
				Resources:     ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
				Placement:     defaultPlacement("cg-missing", lim, lim, lim),
			})
			if dec.Admitted || dec.RC != W8RCUnavailable || dec.Reason != tc.expectWhy {
				t.Fatalf("expected refusal reason=%s rc=%d, got %+v", tc.expectWhy, W8RCUnavailable, dec)
			}
		})
	}
}

func TestW8MustFailUnmatchedSibling(t *testing.T) {
	nowMono := int64(10_000_000_000)
	hostLim := vecPtr(16<<30, 8000, 1024, 16384)
	sliceLim := vecPtr(8<<30, 4000, 512, 8192)
	childLim := vecPtr(4<<30, 2000, 256, 4096)

	t.Run("unbounded_sibling_returns_hold", func(t *testing.T) {
		snap := &W8TelemetrySnapshot{
			Version:           1,
			MonotonicNS:       nowMono,
			MemAvailableBytes: 64 << 30,
			Cgroups: map[string]W8CgroupTelemetry{
				"osworker.slice/w8-capsules.slice": {
					Observed: ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 500, PIDs: 32, OpenFiles: 256},
					UnmatchedSiblings: []W8SiblingUsage{
						{
							Sibling:  "untracked.scope",
							Observed: ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 500, PIDs: 32, OpenFiles: 256},
							Bounded:  false,
							MaxBound: nil,
						},
					},
				},
			},
		}
		store := NewW8SnapshotStore(snap)
		gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
		adapter := NewW8Adapter(gov, W8Config{
			HostCeiling:     hostLim,
			MaxTelemetryAge: 5 * time.Second,
			Telemetry:       store.Source(),
			NowMono:         func() int64 { return nowMono },
		})

		dec := adapter.Admit(W8AdmissionRequest{
			ReservationID: "res-hold-1",
			Clase:         "terminal",
			Resources:     ResourceVector{MemoryBytes: 2 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
			Placement:     defaultPlacement("cg-hold-1", childLim, sliceLim, hostLim),
		})
		if dec.Admitted || dec.Decision != W8DecisionHold || dec.RC != W8RCUnavailable || dec.Reason != "unmatched_sibling" {
			t.Fatalf("expected HOLD decision with rc=%d reason=unmatched_sibling, got %+v", W8RCUnavailable, dec)
		}
	})

	t.Run("bounded_sibling_charged_conservatively_causes_ancestor_overflow", func(t *testing.T) {
		// Unmatched sibling has observed 1 GiB but conservative bound 5 GiB under 8 GiB slice.
		// Requesting 4 GiB under that slice: 5 GiB (conservative bound) + 4 GiB = 9 GiB > 8 GiB -> refuses.
		snap := &W8TelemetrySnapshot{
			Version:           2,
			MonotonicNS:       nowMono,
			MemAvailableBytes: 64 << 30,
			Cgroups: map[string]W8CgroupTelemetry{
				"osworker.slice/w8-capsules.slice": {
					Observed: ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 500, PIDs: 32, OpenFiles: 256},
					UnmatchedSiblings: []W8SiblingUsage{
						{
							Sibling:  "bounded-helper.scope",
							Observed: ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 500, PIDs: 32, OpenFiles: 256},
							MaxBound: vecPtr(5<<30, 1000, 128, 1024),
							Bounded:  true,
						},
					},
				},
			},
		}
		store := NewW8SnapshotStore(snap)
		gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
		adapter := NewW8Adapter(gov, W8Config{
			HostCeiling:     hostLim,
			MaxTelemetryAge: 5 * time.Second,
			Telemetry:       store.Source(),
			NowMono:         func() int64 { return nowMono },
		})

		decOverflow := adapter.Admit(W8AdmissionRequest{
			ReservationID: "res-sib-overflow",
			Clase:         "terminal",
			Resources:     ResourceVector{MemoryBytes: 4 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
			Placement:     defaultPlacement("cg-sib-1", childLim, sliceLim, hostLim),
		})
		if decOverflow.Admitted || decOverflow.RC != W8RCUnavailable || decOverflow.Reason != "ancestor_overflow" {
			t.Fatalf("expected conservative sibling charge to cause ancestor_overflow, got %+v", decOverflow)
		}

		// Requesting 3 GiB fits conservatively: 5 GiB + 3 GiB == 8 GiB <= 8 GiB.
		decFits := adapter.Admit(W8AdmissionRequest{
			ReservationID: "res-sib-fits",
			Clase:         "terminal",
			Resources:     ResourceVector{MemoryBytes: 3 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024},
			Placement:     defaultPlacement("cg-sib-2", childLim, sliceLim, hostLim),
		})
		if !decFits.Admitted {
			t.Fatalf("expected request within remaining conservative margin to be admitted, got %+v", decFits)
		}
	})
}

func TestW8MustFailPrematureQuarantineRelease(t *testing.T) {
	nowMono := int64(10_000_000_000)
	store := baseTelemetryStore(1, nowMono)

	lim := vecPtr(4<<30, 2000, 256, 8192)
	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     lim,
		MaxTelemetryAge: 5 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
	})

	reqVec := ResourceVector{MemoryBytes: 4 << 30, CPUMilli: 2000, PIDs: 256, OpenFiles: 8192}
	dec := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-quarantine-1",
		Clase:         "terminal",
		Resources:     reqVec,
		Placement:     defaultPlacement("cg-q-1", lim, lim, lim),
	})
	if !dec.Admitted {
		t.Fatalf("expected initial reservation to be admitted, got %+v", dec)
	}

	// Mark quarantined (rc 6).
	if err := adapter.Quarantine("res-quarantine-1", "indeterminate cleanup"); err != nil {
		t.Fatalf("Quarantine failed: %v", err)
	}
	st, rc, ok := adapter.ReservationState("res-quarantine-1")
	if !ok || st != W8StateQuarantined || rc != W8RCQuarantined {
		t.Fatalf("expected state=%s rc=%d, got state=%s rc=%d ok=%v", W8StateQuarantined, W8RCQuarantined, st, rc, ok)
	}

	// Premature release (verifiedClean=false) must refuse with rc 5 and keep all 4 dimensions charged.
	rel, err := adapter.Release("res-quarantine-1", "idem-key-premature", false)
	if err == nil || W8ExitCode(err) != W8RCContainment || rel.Released {
		t.Fatalf("expected premature quarantine release to refuse with rc=%d, got rel=%+v err=%v", W8RCContainment, rel, err)
	}
	if adapter.ReservedVector() != reqVec {
		t.Fatalf("quarantined reservation must stay charged in all 4 dimensions after premature release attempt; got %+v", adapter.ReservedVector())
	}

	// Release of an unknown ID must refuse with rc 2.
	if _, err := adapter.Release("unknown-reservation-id", "idem-key-unknown", true); err == nil || W8ExitCode(err) != W8RCInput {
		t.Fatalf("expected unknown reservation release to refuse with rc=%d, got %v", W8RCInput, err)
	}

	// Because all 4 dimensions remain charged while quarantined, another reservation cannot be admitted.
	decBlocked := adapter.Admit(W8AdmissionRequest{
		ReservationID: "res-while-quarantined",
		Clase:         "terminal",
		Resources:     ResourceVector{MemoryBytes: 1, CPUMilli: 1, PIDs: 1, OpenFiles: 1},
		Placement:     defaultPlacement("cg-q-2", lim, lim, lim),
	})
	if decBlocked.Admitted {
		t.Fatalf("expected new admission to be blocked while quarantined reservation holds ceiling, got %+v", decBlocked)
	}
}

func TestW8PositiveExactBoundaryAndVerifiedIdempotentRelease(t *testing.T) {
	nowMono := int64(10_000_000_000)
	exactVec := ResourceVector{
		MemoryBytes: 4 << 30,
		CPUMilli:    2000,
		PIDs:        256,
		OpenFiles:   8192,
	}
	exactLim := &exactVec

	// Before admission no reservation owns this placement's usage. Unexplained
	// live usage must hold; the proposed reservation is not proof of ownership.
	snap := &W8TelemetrySnapshot{
		Version:           7,
		MonotonicNS:       nowMono,
		MemAvailableBytes: 64 << 30,
		Cgroups: map[string]W8CgroupTelemetry{
			"cg-leaf": {Observed: ResourceVector{MemoryBytes: 2 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 512}},
			"osworker.slice/w8-capsules.slice/sub.slice": {},
			"osworker.slice/w8-capsules.slice":           {Observed: ResourceVector{MemoryBytes: 0, CPUMilli: 0, PIDs: 0, OpenFiles: 0}},
			"osworker.slice":                             {Observed: ResourceVector{MemoryBytes: 0, CPUMilli: 0, PIDs: 0, OpenFiles: 0}},
		},
	}
	store := NewW8SnapshotStore(snap)

	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     exactLim,
		MaxTelemetryAge: 5 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
	})

	req := W8AdmissionRequest{
		ReservationID: "res-exact-1",
		Clase:         "terminal",
		Resources:     exactVec,
		Placement: PlacementSpec{
			Child: CgroupLimit{Cgroup: "cg-leaf", Limit: exactLim},
			Ancestors: []CgroupLimit{
				{Cgroup: "osworker.slice/w8-capsules.slice/sub.slice", Limit: exactLim},
				{Cgroup: "osworker.slice/w8-capsules.slice", Limit: exactLim},
				{Cgroup: "osworker.slice", Limit: exactLim},
			},
		},
	}
	dec := adapter.Admit(req)
	if dec.Admitted || dec.Decision != W8DecisionHold || dec.RC != W8RCUnavailable || dec.Reason != "unmatched_sibling" {
		t.Fatalf("expected unreserved live usage to hold, got %+v", dec)
	}
	if !adapter.ReservedVector().IsZero() {
		t.Fatal("held placement must not acquire a reservation")
	}

	// Positive control 1: reserve an empty placement before allocating into it.
	for cg := range snap.Cgroups {
		snap.Cgroups[cg] = W8CgroupTelemetry{}
	}
	snap.Version++
	store.Set(snap, nil)
	dec = adapter.Admit(req)
	if !dec.Admitted || dec.RC != W8RCOK || dec.Certificate == nil {
		t.Fatalf("expected exact-boundary placement to be admitted with rc=0, got %+v", dec)
	}
	if adapter.ReservedVector() != exactVec {
		t.Fatalf("expected reserved vector %+v, got %+v", exactVec, adapter.ReservedVector())
	}
	// Once admitted, the same usage rolls up through the entire ancestor chain.
	// Each cgroup has one charge owner; neither its ancestors nor its own
	// reservation are unmatched siblings, and no extra bound may be charged.
	for cg := range snap.Cgroups {
		snap.Cgroups[cg] = W8CgroupTelemetry{Observed: exactVec}
	}
	snap.Version++
	store.Set(snap, nil)
	for _, cg := range []string{"cg-leaf", "osworker.slice/w8-capsules.slice/sub.slice", "osworker.slice/w8-capsules.slice", "osworker.slice"} {
		ch, err := adapter.CgroupCharge(cg)
		if err != nil || ch != exactVec {
			t.Fatalf("expected single charge %+v on %s, got %+v (err=%v)", exactVec, cg, ch, err)
		}
		gov.mu.Lock()
		bound, hold, err := adapter.evaluateUnmatchedSiblingsLocked(cg, ch, snap)
		gov.mu.Unlock()
		if err != nil || hold || !bound.IsZero() {
			t.Fatalf("own rolled-up usage on %s must not count as an unmatched sibling: bound=%+v hold=%v err=%v", cg, bound, hold, err)
		}
	}

	// Quarantine (rc 6) and then perform explicit verified-clean release with idempotency key.
	if err := adapter.Quarantine("res-exact-1", "janitor verification required"); err != nil {
		t.Fatalf("Quarantine failed: %v", err)
	}

	// Positive control 2: verified release frees exactly once; second release with same key is a no-op.
	rel1, err := adapter.Release("res-exact-1", "clean-key-001", true)
	if err != nil || !rel1.Released || rel1.NoOp || rel1.RC != W8RCOK {
		t.Fatalf("expected first verified release to free resources, got rel=%+v err=%v", rel1, err)
	}
	if !adapter.ReservedVector().IsZero() {
		t.Fatalf("expected zero reserved vector after verified release, got %+v", adapter.ReservedVector())
	}
	est, err := gov.Estado()
	if err != nil || est.Vivas != 0 || est.ReservadoBytes != 0 || est.Cpus != 0 {
		t.Fatalf("expected underlying SrfGobernador counters freed to zero, got %+v (err=%v)", est, err)
	}

	// Second release with the exact same idempotency key must be a no-op and not underflow.
	rel2, err := adapter.Release("res-exact-1", "clean-key-001", true)
	if err != nil || rel2.Released || !rel2.NoOp || rel2.RC != W8RCOK {
		t.Fatalf("expected second release with same key to be a no-op, got rel=%+v err=%v", rel2, err)
	}

	// Release with a different key for the already-released reservation must refuse.
	if _, err := adapter.Release("res-exact-1", "different-key", true); err == nil || W8ExitCode(err) != W8RCInput {
		t.Fatalf("expected mismatched idempotency key to refuse with rc=%d, got %v", W8RCInput, err)
	}
}

func TestW8RacingGrantsConcurrency(t *testing.T) {
	nowMono := int64(20_000_000_000)
	store := baseTelemetryStore(1, nowMono)

	// Capacity for at most 4 concurrent reservations of (1 GiB, 1000 mCPU, 64 PIDs, 1024 OpenFiles).
	unitVec := ResourceVector{MemoryBytes: 1 << 30, CPUMilli: 1000, PIDs: 64, OpenFiles: 1024}
	hostLim := vecPtr(4<<30, 4000, 256, 4096)
	childLim := vecPtr(1<<30, 1000, 64, 1024)

	gov := NuevoGobernador(&TelemetriaSintetica{DisponibleBytes: 64 << 30})
	for i := 0; i < 32; i++ {
		gov.ObservarLatencia(20 * time.Millisecond)
	}

	adapter := NewW8Adapter(gov, W8Config{
		HostCeiling:     hostLim,
		MaxTelemetryAge: 5 * time.Second,
		Telemetry:       store.Source(),
		NowMono:         func() int64 { return nowMono },
	})

	const workers = 24
	const iterations = 6
	var activeCount atomic.Int32
	var maxObservedActive atomic.Int32
	var wg sync.WaitGroup

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(workerID int) {
			defer wg.Done()
			for iter := 0; iter < iterations; iter++ {
				if iter%2 == 0 {
					store.Set(&W8TelemetrySnapshot{
						Version:           uint64(workerID*iterations + iter + 1),
						MonotonicNS:       nowMono,
						MemAvailableBytes: 64 << 30,
						CPUPSI10:          2.0,
					}, nil)
				}

				resID := fmt.Sprintf("conc-w%d-i%d", workerID, iter)
				cgName := fmt.Sprintf("cg-w%d-i%d", workerID, iter)
				dec := adapter.Admit(W8AdmissionRequest{
					ReservationID: resID,
					Clase:         "terminal",
					Resources:     unitVec,
					Placement:     defaultPlacement(cgName, childLim, hostLim, hostLim),
				})
				if dec.RC == 1 || (dec.RC != 0 && (dec.RC < 2 || dec.RC > 7)) {
					t.Errorf("forbidden W8 exit code %d", dec.RC)
					return
				}
				if !dec.Admitted {
					continue
				}

				cur := activeCount.Add(1)
				for {
					prev := maxObservedActive.Load()
					if cur <= prev || maxObservedActive.CompareAndSwap(prev, cur) {
						break
					}
				}
				if cur > 4 {
					t.Errorf("concurrency invariant violated: %d simultaneous reservations exceeded ceiling 4", cur)
				}

				// Quarantine half of admitted reservations and exercise verified-clean + idempotent duplicate release.
				idemKey := "idem-" + resID
				if (workerID+iter)%2 == 0 {
					if err := adapter.Quarantine(resID, "simulated rc6"); err != nil {
						t.Errorf("Quarantine(%s) failed: %v", resID, err)
					}
					if _, err := adapter.Release(resID, idemKey, false); err == nil {
						t.Errorf("premature quarantine release succeeded for %s", resID)
					}
				}

				activeCount.Add(-1)
				rel1, err := adapter.Release(resID, idemKey, true)
				if err != nil || !rel1.Released || rel1.NoOp {
					t.Errorf("first verified release failed for %s: rel=%+v err=%v", resID, rel1, err)
				}
				rel2, err := adapter.Release(resID, idemKey, true)
				if err != nil || rel2.Released || !rel2.NoOp {
					t.Errorf("second idempotent release was not no-op for %s: rel=%+v err=%v", resID, rel2, err)
				}
			}
		}(w)
	}

	wg.Wait()

	if !adapter.ReservedVector().IsZero() {
		t.Fatalf("expected all resources freed after concurrent workers finished, got %+v", adapter.ReservedVector())
	}
	est, err := gov.Estado()
	if err != nil || est.Vivas != 0 || est.ReservadoBytes != 0 {
		t.Fatalf("expected underlying governor clean state after concurrency test, got %+v err=%v", est, err)
	}
}
