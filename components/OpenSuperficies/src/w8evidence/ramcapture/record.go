package ramcapture

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

func (r *Recorder) Record(raw Raw, nowNS uint64) (Record, error) {
	bytes, _ := json.Marshal(raw)
	digest := sha256.Sum256(bytes)
	record := Record{Raw: raw, Digest: hex.EncodeToString(digest[:]), Absent: []string{}}
	finish := func(err error) (Record, error) {
		if err != nil {
			record.Reason = err.Error()
		}
		r.Records = append(r.Records, record)
		return record, err
	}
	required := []string{raw.WorkloadID, raw.InstanceID, raw.BaselineID, raw.CgroupScope}
	for _, x := range required {
		if strings.TrimSpace(x) == "" {
			return finish(errors.New("missing workload, instance, baseline or cgroup identity"))
		}
	}
	if raw.AcquisitionScope != "workload-cgroup" {
		return finish(errors.New("acquisition scope mismatch"))
	}
	if raw.Unit != "bytes" {
		return finish(errors.New("unit confusion: cgroup samples require bytes"))
	}
	if raw.CapAsMeasurement {
		return finish(errors.New("imposed cap is not a measurement"))
	}
	if raw.Phase != "baseline" && raw.Phase != "boot" && raw.Phase != "THINK" && raw.Phase != "collection" {
		return finish(errors.New("invalid phase"))
	}
	if raw.Outcome != "success" && raw.Outcome != "failed" && raw.Outcome != "oom" && raw.Outcome != "censored" {
		return finish(errors.New("invalid outcome"))
	}
	if raw.Outcome != "success" && strings.TrimSpace(raw.Reason) == "" {
		return finish(errors.New("failed, OOM or censored evidence must retain reason"))
	}
	if raw.MonotonicNS == 0 || nowNS < raw.MonotonicNS || nowNS-raw.MonotonicNS > r.Policy.MaxAgeNS {
		return finish(errors.New("stale or invalid monotonic sample"))
	}
	for _, d := range []*Demand{raw.BackgroundClient, raw.Seat, raw.Collector} {
		if d != nil && (d.Scope == "" || d.Scope == raw.CgroupScope || d.Unit != "bytes") {
			return finish(errors.New("auxiliary demand requires separate scope and bytes unit"))
		}
	}
	if raw.HostPressureText != "" && raw.HostPressureScope != "host" {
		return finish(errors.New("host pressure requires host scope"))
	}
	for _, s := range []string{raw.CurrentText, raw.PeakText, raw.EventsText, raw.PressureText, raw.MeminfoText, raw.HostPressureText} {
		if e := bounded(s); e != nil {
			return finish(e)
		}
	}
	missing := func(name, text string) bool {
		if strings.TrimSpace(text) != "" {
			return false
		}
		record.Absent = append(record.Absent, name+":AUSENTE")
		return true
	}
	for _, item := range []struct{ name, text string }{{"meminfo", raw.MeminfoText}, {"current", raw.CurrentText}, {"peak", raw.PeakText}, {"events", raw.EventsText}, {"pressure", raw.PressureText}} {
		if missing(item.name, item.text) && !r.Policy.AllowedMissing[item.name] {
			return finish(fmt.Errorf("%s missing without caller permission", item.name))
		}
	}
	if raw.MeminfoText != "" {
		m, e := ParseMeminfo(raw.MeminfoText, raw.PageSizeBytes, raw.PageSizeProvenance)
		if e != nil {
			return finish(e)
		}
		record.Sample.Host = m
		if m.TotalBytes == nil || m.AvailableBytes == nil {
			record.Absent = append(record.Absent, "host-meminfo-fields:AUSENTE")
		}
	}
	if raw.CurrentText != "" {
		n, e := singleBytes(raw.CurrentText)
		if e != nil {
			return finish(e)
		}
		record.Sample.CurrentBytes = n
	}
	if raw.PeakText != "" {
		n, e := singleBytes(raw.PeakText)
		if e != nil {
			return finish(e)
		}
		record.Sample.PeakBytes = n
	}
	if record.Sample.CurrentBytes != nil && record.Sample.PeakBytes != nil && *record.Sample.PeakBytes < *record.Sample.CurrentBytes {
		return finish(errors.New("peak lower than same-scope current"))
	}
	if raw.EventsText != "" {
		v, e := ParseEvents(raw.EventsText)
		if e != nil {
			return finish(e)
		}
		record.Sample.Events = v
	}
	if raw.PressureText != "" {
		v, e := ParsePressure(raw.PressureText)
		if e != nil {
			return finish(e)
		}
		record.Sample.CgroupPressure = &v
	}
	if raw.HostPressureText != "" {
		v, e := ParsePressure(raw.HostPressureText)
		if e != nil {
			return finish(e)
		}
		record.Sample.HostPressure = &v
	} else if r.Policy.AllowedMissing["host_pressure"] {
		record.Absent = append(record.Absent, "host_pressure:AUSENTE")
	}
	key := raw.WorkloadID + "\x00" + raw.InstanceID + "\x00" + raw.BaselineID
	scopeKey := raw.WorkloadID + "\x00" + raw.InstanceID + "\x00" + raw.CgroupScope
	if last, ok := r.previous[scopeKey]; ok {
		if raw.MonotonicNS <= last.Raw.MonotonicNS {
			return finish(errors.New("monotonic time did not increase"))
		}
		if len(record.Sample.Events) > 0 && len(last.Sample.Events) > 0 {
			for k, old := range last.Sample.Events {
				if newValue, exists := record.Sample.Events[k]; exists && newValue < old {
					return finish(errors.New("memory.events counter reset"))
				}
			}
		}
		if last.Sample.PeakBytes != nil && record.Sample.PeakBytes != nil && *record.Sample.PeakBytes < *last.Sample.PeakBytes {
			return finish(errors.New("memory.peak counter reset"))
		}
	}
	if raw.Phase == "baseline" {
		if _, exists := r.baselines[key]; exists {
			return finish(errors.New("duplicate baseline"))
		}
	} else {
		base, ok := r.baselines[key]
		if !ok {
			return finish(errors.New("missing explicit baseline"))
		}
		if base.Raw.CgroupScope != raw.CgroupScope || base.Raw.AcquisitionScope != raw.AcquisitionScope {
			return finish(errors.New("baseline scope mismatch"))
		}
		if raw.MonotonicNS <= base.Raw.MonotonicNS {
			return finish(errors.New("baseline time mismatch"))
		}
		if base.Sample.CurrentBytes == nil {
			return finish(errors.New("baseline current absent"))
		}
		if record.Sample.CurrentBytes != nil && *record.Sample.CurrentBytes >= *base.Sample.CurrentBytes {
			n := *record.Sample.CurrentBytes - *base.Sample.CurrentBytes
			record.IncrementalSteadyBytes = &n
		}
		if record.Sample.PeakBytes != nil && base.Sample.PeakBytes != nil && *record.Sample.PeakBytes >= *base.Sample.PeakBytes && *record.Sample.PeakBytes >= *base.Sample.CurrentBytes {
			n := *record.Sample.PeakBytes - *base.Sample.CurrentBytes
			record.IncrementalPeakBytes = &n
		}
		if record.IncrementalSteadyBytes == nil {
			record.Absent = append(record.Absent, "incremental-steady:AUSENTE")
		}
		if record.IncrementalPeakBytes == nil {
			record.Absent = append(record.Absent, "incremental-peak:AUSENTE")
		}
	}
	record.Valid = true
	record.ReserveSatisfied = record.Sample.Host.AvailableBytes != nil && *record.Sample.Host.AvailableBytes >= r.Policy.SafetyReserveBytes
	record.CompleteForPlanning = record.ReserveSatisfied && raw.Phase != "baseline" && raw.Outcome == "success" && len(record.Absent) == 0 && record.IncrementalSteadyBytes != nil && record.IncrementalPeakBytes != nil
	r.Records = append(r.Records, record)
	if raw.Phase == "baseline" {
		r.baselines[key] = record
	}
	r.previous[scopeKey] = record
	return record, nil
}
