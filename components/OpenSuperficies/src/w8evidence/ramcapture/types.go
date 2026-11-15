// Package ramcapture records offline caller-supplied memory evidence, without I/O or grants.
package ramcapture

const maxInputBytes = 64 * 1024 // TARGET: bound for each caller-supplied text snapshot.

// Decimal is an exact nonnegative decimal: Value / Scale, not a float.
type Decimal struct {
	Value uint64 `json:"value"`
	Scale uint64 `json:"scale"`
}
type PressureRow struct {
	Avg10       Decimal `json:"avg10"`
	Avg60       Decimal `json:"avg60"`
	Avg300      Decimal `json:"avg300"`
	TotalMicros uint64  `json:"total_micros"`
}
type Pressure struct {
	Some *PressureRow `json:"some"`
	Full *PressureRow `json:"full"`
}
type Meminfo struct {
	TotalBytes     *uint64 `json:"total_bytes"`
	AvailableBytes *uint64 `json:"available_bytes"`
	HugePagesBytes *uint64 `json:"huge_pages_bytes"`
}

// Demand is attributed separately. A host, seat, client, or collector value is
// NEVER subtracted from a workload-cgroup value.
type Demand struct {
	Scope string `json:"scope"`
	Unit  string `json:"unit"`
	Bytes uint64 `json:"bytes"`
}

type Raw struct {
	WorkloadID         string  `json:"workload_id"`
	InstanceID         string  `json:"instance_id"`
	BaselineID         string  `json:"baseline_id"`
	Phase              string  `json:"phase"`             // baseline, boot, THINK, collection
	AcquisitionScope   string  `json:"acquisition_scope"` // workload-cgroup
	CgroupScope        string  `json:"cgroup_scope"`      // identity of a single cgroup
	Unit               string  `json:"unit"`              // bytes
	MonotonicNS        uint64  `json:"monotonic_ns"`
	MeminfoText        string  `json:"meminfo_text"` // host-wide /proc/meminfo; never workload bytes
	CurrentText        string  `json:"memory_current_text"`
	PeakText           string  `json:"memory_peak_text"`
	EventsText         string  `json:"memory_events_text"`
	PressureText       string  `json:"memory_pressure_text"`
	HostPressureText   string  `json:"host_pressure_text"`
	HostPressureScope  string  `json:"host_pressure_scope"` // host, if present
	PageSizeBytes      uint64  `json:"page_size_bytes"`
	PageSizeProvenance string  `json:"page_size_provenance"`
	BackgroundClient   *Demand `json:"background_client"`
	Seat               *Demand `json:"seat"`
	Collector          *Demand `json:"collector"`
	LimitBytes         *uint64 `json:"imposed_limit_bytes"` // context only; never a measurement
	CapAsMeasurement   bool    `json:"cap_as_measurement"`
	Outcome            string  `json:"outcome"` // success, failed, oom, censored
	Reason             string  `json:"reason"`
}

// Policy must be supplied by the caller. Missing-field exceptions do not
// manufacture measurements and never qualify incomplete evidence for planning.
type Policy struct {
	MaxAgeNS           uint64
	SafetyReserveBytes uint64
	AllowedMissing     map[string]bool // meminfo, current, peak, events, pressure, host_pressure
}

type Sample struct {
	CurrentBytes   *uint64           `json:"current_bytes"`
	PeakBytes      *uint64           `json:"peak_bytes"`
	Host           Meminfo           `json:"host"`
	Events         map[string]uint64 `json:"events"`
	CgroupPressure *Pressure         `json:"cgroup_pressure"`
	HostPressure   *Pressure         `json:"host_pressure"`
}
type Record struct {
	Raw                    Raw      `json:"raw"`
	Digest                 string   `json:"digest"`
	Reason                 string   `json:"record_reason"`
	Absent                 []string `json:"absent"`
	Valid                  bool     `json:"valid"`
	CompleteForPlanning    bool     `json:"complete_for_planning"` // not an admission decision
	ReserveSatisfied       bool     `json:"reserve_satisfied"`
	Sample                 Sample   `json:"sample"`
	IncrementalSteadyBytes *uint64  `json:"incremental_steady_bytes"`
	IncrementalPeakBytes   *uint64  `json:"incremental_peak_bytes"`
}
type Recorder struct {
	Policy    Policy
	Records   []Record
	baselines map[string]Record
	previous  map[string]Record
}
