package w8seat

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"path/filepath"
	"strconv"
	"strings"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

type boundedOutput struct {
	b    bytes.Buffer
	cap  int
	over bool
}

func (w *boundedOutput) Write(b []byte) (int, error) {
	if len(b) > w.cap-w.b.Len() {
		w.over = true
		return 0, refusal(5, "verification", "output cap")
	}
	return w.b.Write(b)
}
func capture(ctx context.Context, r w8builder.Runner, in io.Reader, limit int, argv ...string) ([]byte, error) {
	w := &boundedOutput{cap: limit}
	e := func() (err error) {
		defer func() {
			if recover() != nil {
				err = refusal(6, "indeterminate", "runner panic")
			}
		}()
		return r.Run(ctx, argv, in, w)
	}()
	if w.over {
		return nil, refusal(5, "verification", "output cap")
	}
	return w.b.Bytes(), e
}
func daemonProbe(ctx context.Context, r w8builder.Runner) error {
	b, e := capture(ctx, r, nil, 1<<20, "docker", "info", "--format", "{{json .}}")
	if e != nil {
		return refusal(4, "unavailable", "daemon info unavailable")
	}
	m, e := decodeObject(bytes.TrimSpace(b))
	if e != nil {
		return e
	}
	found := false
	for _, v := range strs(m["SecurityOptions"]) {
		found = found || strings.Contains(v, "rootless")
	}
	if !found || fmt.Sprint(m["CgroupVersion"]) != "2" {
		return refusal(5, "containment", "dedicated rootless cgroup v2 daemon required")
	}
	return nil
}
func inspectAdmission(b []byte, p LaunchPolicy, container, lease string) (map[string]any, int, error) {
	field := "inspect JSON"
	fail := func() (map[string]any, int, error) {
		return nil, 0, refusal(5, "containment", "effective launch mismatch: "+field)
	}
	value, e := workerbridge.Decode(b)
	if e != nil {
		return fail()
	}
	list := arr(value)
	if len(list) != 1 {
		return fail()
	}
	field = "identity"
	m := obj(list[0])
	h := obj(m["HostConfig"])
	c := obj(m["Config"])
	labels := obj(c["Labels"])
	if str(m["Name"]) != "/"+container || labels["w8.seat"] != "1" || labels["w8.lease"] != lease || c["User"] != "10001:10001" || !shapeEqual(c["Entrypoint"], []string{"/usr/bin/tini", "-s", "--"}) || c["Image"] != "os-worker-base@sha256:"+p.ImageSHA || h["ReadonlyRootfs"] != true || h["Privileged"] != false || h["NetworkMode"] != "none" || !shapeEqual(h["CapDrop"], []string{"ALL"}) {
		return fail()
	}
	field = "mounts/devices/caps/ports"
	if len(arr(h["Binds"])) != 0 || len(arr(h["Devices"])) != 0 || len(arr(h["DeviceRequests"])) != 0 || len(arr(h["CapAdd"])) != 0 || len(arr(h["PortBindings"])) != 0 || len(obj(h["PortBindings"])) != 0 {
		return fail()
	}
	field = "namespaces"
	for _, ns := range []string{"PidMode", "IpcMode", "UTSMode", "UsernsMode"} {
		if h[ns] == "host" {
			return fail()
		}
	}
	field = "resources"
	if number(h["Memory"]) != 4<<30 || number(h["MemorySwap"]) != 4<<30 || number(h["PidsLimit"]) != 256 || number(h["OomScoreAdj"]) != 800 || number(h["NanoCpus"]) != 2e9 || (number(h["CpuShares"]) <= 0 || number(h["CpuShares"]) > 256) {
		return fail()
	}
	field = "cpuset"
	cpu, e := ParseCPUs(str(h["CpusetCpus"]))
	if e != nil || !shapeEqual(cpu, p.CPUs.Worker) {
		return fail()
	}
	field = "security_opt"
	if p.SeccompInline == "" || !shapeEqual(h["SecurityOpt"], []string{"no-new-privileges:true", "seccomp=" + p.SeccompInline}) {
		return fail()
	}
	field = "ulimits"
	nofile, core := false, false
	for _, x := range arr(h["Ulimits"]) {
		v := obj(x)
		if v["Name"] == "nofile" && number(v["Hard"]) == 8192 && number(v["Soft"]) == 8192 {
			nofile = true
		}
		if v["Name"] == "core" && number(v["Hard"]) == 0 && number(v["Soft"]) == 0 {
			core = true
		}
	}
	if !nofile || !core {
		return fail()
	}
	field = "log/restart"
	if obj(h["LogConfig"])["Type"] != "none" || obj(h["RestartPolicy"])["Name"] != "no" {
		return fail()
	}
	field = "tmpfs"
	spec, _ := workerbridge.Parse(DefaultLaunchSpec(p))
	wantTmp := map[string]any{}
	for _, x := range arr(spec["tmpfs"]) {
		v := obj(x)
		wantTmp[str(v["target"])] = v["options"]
	}
	if !shapeEqual(h["Tmpfs"], wantTmp) {
		return fail()
	}
	field = "mounts"
	volume := false
	for _, x := range arr(m["Mounts"]) {
		v := obj(x)
		switch v["Type"] {
		case "volume":
			if volume || v["Name"] != "os_worker_cache" || v["Destination"] != "/opt/cache" || v["RW"] != false {
				return fail()
			}
			volume = true
		case "tmpfs":
			if wantTmp[str(v["Destination"])] == nil {
				return fail()
			}
		default:
			return fail()
		}
	}
	if !volume {
		return fail()
	}
	field = "env"
	seen := map[string]bool{}
	for _, entry := range strs(c["Env"]) {
		k, v, ok := strings.Cut(entry, "=")
		want, allowed := seatEnv[k]
		if k == "PATH" {
			want, allowed = ImagePATH, true
		}
		if !ok || !allowed || want != v || seen[k] {
			return fail()
		}
		seen[k] = true
	}
	if !seen["PATH"] || len(seen) != len(seatEnv)+1 {
		return fail()
	}
	pid := int(number(obj(m["State"])["Pid"]))
	if pid <= 0 {
		return fail()
	}
	return map[string]any{"inspect_sha256": hash(b), "memswap_equals_memory": true, "oom_score_adj": 800, "nofile": 8192, "devices": []any{}, "bind_mounts": 0, "env_file_used": false, "env_secret_names": 0, "runtime_socket_visible": false, "daemon": "rootless"}, pid, nil
}
func completeProbes(host, inside map[string]any) error {
	if inside["etc_write"] != "read_only" || inside["home_admin"] != "absent" || inside["egress_probe"] != "BLOCKED" || (inside["gateway_probe"] != "BLOCKED" && inside["gateway_probe"] != "NO_ROUTE") {
		return refusal(5, "containment", "unsafe in-capsule probe")
	}
	for _, k := range []string{"env_secret_names", "inference_processes"} {
		v, ok := inside[k]
		if !ok || number(v) != 0 {
			return refusal(5, "containment", "missing/unsafe process or environment probe")
		}
	}
	if inside["runtime_socket_visible"] != false {
		return refusal(5, "containment", "runtime socket probe unsafe")
	}
	for _, k := range []string{"etc_write", "home_admin", "egress_probe", "env_secret_names", "runtime_socket_visible", "inference_processes"} {
		host[k] = inside[k]
	}
	host["orphans_after_task"] = nil
	return nil
}
func observations(container string, p LaunchPolicy, probes map[string]any) map[string]any {
	return map[string]any{"capsule_id": container, "uid": 10001, "rootfs_read_only": true, "cap_drop": []string{"ALL"}, "no_new_privileges": true, "no_host_mounts": true, "no_runtime_socket": true, "seccomp_profile_sha256": p.SeccompSHA, "cpuset": p.CPUs.Worker, "memory_limit_bytes": int64(4 << 30), "peak_memory_bytes": nil, "pids_limit": 256, "peak_pids": nil, "workspace_limit_bytes": int64(2 << 30), "workspace_peak_bytes": nil, "workspace_root": "/workspace/repo", "frames_root": "/dev/shm/w8/frames", "frames_disk_bytes": 0, "swap_enabled": false, "core_dumps_enabled": false, "network": map[string]any{"mode": "none", "allowlist": []any{}}, "denied_network_attempts": 0, "ttl_enforced": true, "xpra": "off", "x11_listeners": 0, "public_listeners": 0, "probes": probes}
}
func cgroupMemory(pid int) (string, error) {
	b, e := secureRead(fmt.Sprintf("/proc/%d/cgroup", pid), 4096, false)
	if e != nil {
		return "", e
	}
	for _, line := range strings.Split(string(b), "\n") {
		if p, ok := strings.CutPrefix(line, "0::"); ok && strings.HasPrefix(p, "/") && !strings.Contains(p, "..") {
			return filepath.Join("/sys/fs/cgroup", p, "memory.current"), nil
		}
	}
	return "", refusal(4, "unavailable", "capsule cgroup unavailable")
}

func readPeaks(cgroupMemoryPath string) (int64, int64, error) {
	read := func(name string) (int64, error) {
		b, e := secureRead(filepath.Join(filepath.Dir(cgroupMemoryPath), name), 128, false)
		if e != nil {
			return 0, e
		}
		n, e := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
		if e != nil || n < 0 {
			return 0, refusal(4, "unavailable", "cgroup peak absent")
		}
		return n, nil
	}
	m, e := read("memory.peak")
	if e != nil {
		return 0, 0, e
	}
	p, e := read("pids.peak")
	return m, p, e
}

func measuredWorkspacePeak(v any) (int64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	peak, err := n.Int64()
	return peak, err == nil && peak >= 0 && peak <= 2<<30
}
