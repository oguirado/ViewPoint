package workerbridge

// ValidateLaunchSpec is a pure function mirroring launch_admission in the v3
// stdlib model. It validates JSON data only; it renders or runs no command.
// interactiveCPUs is a required trusted input: nil is absent, not an empty set.
func ValidateLaunchSpec(data []byte, backend string, interactiveCPUs []int) error {
	if backend != "docker" && backend != "microvm" {
		return reject("MALFORMED", "unsupported local backend")
	}
	if interactiveCPUs == nil {
		return reject("MALFORMED", "interactive CPU set absent")
	}
	spec, e := Parse(data)
	if e != nil {
		return e
	}
	if e = validate(spec, "launch_spec"); e != nil {
		return e
	}
	if !equal(spec["memory_bytes"], spec["memswap_bytes"]) {
		return reject("MALFORMED", "swap exceeds memory")
	}
	for _, n := range array(spec["cpuset"]) {
		for _, i := range interactiveCPUs {
			if num(n) == int64(i) {
				return reject("MALFORMED", "interactive CPU overlap")
			}
		}
	}
	devices := []any{}
	if backend == "microvm" {
		devices = append(devices, "/dev/kvm")
	}
	if !equal(spec["devices"], devices) {
		return reject("MALFORMED", "device outside backend policy")
	}
	for _, v := range array(spec["tmpfs"]) {
		if object(v)["target"] == "/workspace" {
			return nil
		}
	}
	return reject("MALFORMED", "workspace tmpfs missing")
}
