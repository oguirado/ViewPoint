package ramcapture

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
)

func NewRecorder(p Policy) (*Recorder, error) {
	if p.MaxAgeNS == 0 || p.SafetyReserveBytes == 0 {
		return nil, errors.New("caller policy requires freshness and safety reserve")
	}
	return &Recorder{Policy: p, baselines: map[string]Record{}, previous: map[string]Record{}}, nil
}

func bounded(s string) error {
	if len(s) > maxInputBytes {
		return errors.New("snapshot exceeds input bound")
	}
	if strings.IndexByte(s, 0) >= 0 {
		return errors.New("NUL in captured text")
	}
	return nil
}
func unsigned(s string) (uint64, error) {
	if s == "" {
		return 0, errors.New("empty integer")
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0, fmt.Errorf("invalid integer %q", s)
		}
	}
	return strconv.ParseUint(s, 10, 64)
}
func multiply(a, b uint64) (uint64, error) {
	if b != 0 && a > math.MaxUint64/b {
		return 0, errors.New("byte multiplication overflow")
	}
	return a * b, nil
}
func singleBytes(s string) (*uint64, error) {
	if err := bounded(s); err != nil {
		return nil, err
	}
	parts := strings.Fields(s)
	if len(parts) != 1 {
		return nil, errors.New("cgroup byte field must be one unsigned byte count")
	}
	v, err := unsigned(parts[0])
	if err != nil {
		return nil, err
	}
	return &v, nil
}

func exactDecimal(s string) (Decimal, error) {
	p := strings.Split(s, ".")
	if len(p) > 2 || len(p) == 0 || p[0] == "" {
		return Decimal{}, errors.New("invalid PSI decimal")
	}
	if len(p) == 2 && (p[1] == "" || len(p[1]) > 18) {
		return Decimal{}, errors.New("invalid PSI decimal scale")
	}
	digits := strings.Join(p, "")
	v, e := unsigned(digits)
	if e != nil {
		return Decimal{}, e
	}
	scale := uint64(1)
	if len(p) == 2 {
		for range p[1] {
			scale *= 10
		}
	}
	if v/scale > 100 || (v/scale == 100 && v%scale != 0) {
		return Decimal{}, errors.New("PSI percentage > 100")
	}
	return Decimal{v, scale}, nil
}
func parseKeyValues(s string, allowed map[string]bool) (map[string]string, error) {
	if err := bounded(s); err != nil {
		return nil, err
	}
	out := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		f := strings.Fields(line)
		if len(f) != 2 {
			return nil, fmt.Errorf("invalid field %q", line)
		}
		if allowed != nil && !allowed[f[0]] {
			return nil, fmt.Errorf("unexpected field %q", f[0])
		}
		if _, exists := out[f[0]]; exists {
			return nil, errors.New("duplicate field")
		}
		out[f[0]] = f[1]
	}
	return out, nil
}
func ParseEvents(s string) (map[string]uint64, error) {
	values, e := parseKeyValues(s, nil)
	if e != nil {
		return nil, e
	}
	out := map[string]uint64{}
	for k, v := range values {
		n, err := unsigned(v)
		if err != nil {
			return nil, fmt.Errorf("event %s: %w", k, err)
		}
		out[k] = n
	}
	return out, nil
}
func ParsePressure(s string) (Pressure, error) {
	if err := bounded(s); err != nil {
		return Pressure{}, err
	}
	p := Pressure{}
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		if strings.TrimSpace(line) == "" {
			continue
		}
		f := strings.Fields(line)
		if len(f) != 5 || (f[0] != "some" && f[0] != "full") {
			return Pressure{}, errors.New("invalid pressure row")
		}
		fields := map[string]string{}
		for _, kv := range f[1:] {
			k, v, ok := strings.Cut(kv, "=")
			if !ok {
				return Pressure{}, errors.New("PSI missing equals")
			}
			if _, exists := fields[k]; exists {
				return Pressure{}, errors.New("duplicate PSI field")
			}
			fields[k] = v
		}
		for _, k := range []string{"avg10", "avg60", "avg300", "total"} {
			if fields[k] == "" {
				return Pressure{}, errors.New("missing PSI field")
			}
		}
		if len(fields) != 4 {
			return Pressure{}, errors.New("unknown PSI field")
		}
		a, e := exactDecimal(fields["avg10"])
		if e != nil {
			return Pressure{}, e
		}
		b, e := exactDecimal(fields["avg60"])
		if e != nil {
			return Pressure{}, e
		}
		c, e := exactDecimal(fields["avg300"])
		if e != nil {
			return Pressure{}, e
		}
		total, e := unsigned(fields["total"])
		if e != nil {
			return Pressure{}, e
		}
		row := &PressureRow{a, b, c, total}
		if f[0] == "some" {
			if p.Some != nil {
				return Pressure{}, errors.New("duplicate some row")
			}
			p.Some = row
		} else {
			if p.Full != nil {
				return Pressure{}, errors.New("duplicate full row")
			}
			p.Full = row
		}
	}
	if p.Some == nil {
		return Pressure{}, errors.New("pressure some row absent")
	}
	return p, nil
}
func ParseMeminfo(s string, pageBytes uint64, provenance string) (Meminfo, error) {
	if err := bounded(s); err != nil {
		return Meminfo{}, err
	}
	out := Meminfo{}
	seen := map[string]bool{}
	for _, line := range strings.Split(strings.TrimSpace(s), "\n") {
		f := strings.Fields(line)
		if len(f) == 0 {
			continue
		}
		key := strings.TrimSuffix(f[0], ":")
		if key != "MemTotal" && key != "MemAvailable" && key != "HugePages_Total" {
			continue
		}
		if seen[key] {
			return out, errors.New("duplicate meminfo key")
		}
		seen[key] = true
		if len(f) < 2 {
			return out, errors.New("invalid meminfo line")
		}
		n, e := unsigned(f[1])
		if e != nil {
			return out, e
		}
		if key == "HugePages_Total" {
			if len(f) != 2 || pageBytes == 0 || provenance == "" {
				return out, errors.New("huge pages require caller page size and provenance")
			}
			v, e := multiply(n, pageBytes)
			if e != nil {
				return out, e
			}
			out.HugePagesBytes = &v
			continue
		}
		if len(f) != 3 || f[2] != "kB" {
			return out, errors.New("meminfo requires kB unit")
		}
		v, e := multiply(n, 1024)
		if e != nil {
			return out, e
		}
		if key == "MemTotal" {
			out.TotalBytes = &v
		} else {
			out.AvailableBytes = &v
		}
	}
	return out, nil
}
