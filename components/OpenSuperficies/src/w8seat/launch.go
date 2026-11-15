package w8seat

import (
	"context"
	"io"
	"reflect"
	"strings"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

// ImagePATH is a recipe constant, never a task environment or launch field.
const ImagePATH = w8builder.ImagePATH

var seatEnv = map[string]string{
	"LC_ALL": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "TINI_SUBREAPER": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true", "GOFLAGS": "-mod=mod", "GOCACHE": "/workspace/.cache/go-build", "GOMODCACHE": "/opt/cache/gomod", "GOTOOLCHAIN": "local", "CARGO_HOME": "/opt/cache/cargo", "CARGO_NET_OFFLINE": "true", "PIP_NO_INDEX": "1", "HOME": "/home/worker", "TMPDIR": "/tmp",
}
var envOrder = []string{"LC_ALL", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "TINI_SUBREAPER", "GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "GOFLAGS", "GOCACHE", "GOMODCACHE", "GOTOOLCHAIN", "CARGO_HOME", "CARGO_NET_OFFLINE", "PIP_NO_INDEX", "HOME", "TMPDIR"}

type LaunchPolicy struct {
	CPUs                              CPUConfig
	SeccompFile, SeccompSHA, ImageSHA string
	// SeccompInline is json.Compact of the pinned profile: the docker CLI sends the
	// profile content, so `docker inspect` reports "seccomp=<compact JSON>", never the path.
	SeccompInline string
}

func DefaultLaunchSpec(p LaunchPolicy) []byte {
	return canonical(map[string]any{"schema": "os.cu.launch-spec/v1", "image_digest": p.ImageSHA, "user": "10001:10001", "init": true, "read_only": true, "cap_drop": []string{"ALL"}, "security_opt": []string{"no-new-privileges:true"}, "privileged": false, "network": "none", "tmpfs": []any{map[string]any{"target": "/workspace", "options": "rw,exec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=2g"}, map[string]any{"target": "/tmp", "options": "rw,noexec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=256m"}, map[string]any{"target": "/home/worker", "options": "rw,noexec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=64m"}, map[string]any{"target": "/dev/shm", "options": "rw,noexec,nosuid,nodev,mode=0770,uid=10001,gid=10001,size=64m"}}, "volumes": []any{map[string]any{"source": "os_worker_cache", "target": "/opt/cache", "read_only": true}}, "devices": []any{}, "memory_bytes": int64(4 << 30), "memswap_bytes": int64(4 << 30), "pids_limit": 256, "nofile": 8192, "oom_score_adj": 800, "cpuset": p.CPUs.Worker, "env_names": envOrder, "log_driver": "none", "restart": "no", "ulimit_core": 0})
}
func seccompPin(p LaunchPolicy) error {
	if p.SeccompFile != SeccompPath || !hex64.MatchString(p.SeccompSHA) {
		return refusal(5, "containment", "seccomp path/pin")
	}
	b, e := secureRead(p.SeccompFile, 1<<20, true)
	if e != nil || hash(b) != p.SeccompSHA {
		return refusal(4, "unavailable", "seccomp profile unavailable or pin mismatch")
	}
	return nil
}

// RenderArgv validates the contract and the narrower Phase-2 policy before any
// journal or Runner call. No task field can supply a launch flag or environment.
func RenderArgv(spec []byte, container, lease string, p LaunchPolicy) ([]string, error) {
	if e := workerbridge.ValidateLaunchSpec(spec, "docker", p.CPUs.Interactive); e != nil {
		return nil, refusal(5, "containment", "launch contract refused")
	}
	m, e := workerbridge.Parse(spec)
	if e != nil {
		return nil, refusal(5, "containment", "launch JSON")
	}
	if !id12.MatchString(container) || lease == "" || strings.ContainsAny(lease, "\n\r \t") || !hex64.MatchString(p.ImageSHA) || !hex64.MatchString(p.SeccompSHA) || p.SeccompFile != SeccompPath {
		return nil, refusal(5, "containment", "launch identity or seccomp pin")
	}
	if string(canonical(m)) != string(DefaultLaunchSpec(p)) {
		return nil, refusal(5, "containment", "launch differs from seat policy")
	}
	a := []string{"docker", "run", "-d", "--name", container, "--label", "w8.seat=1", "--label", "w8.lease=" + lease, "--user", "10001:10001", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--security-opt", "seccomp=" + p.SeccompFile, "--network", "none"}
	for _, t := range arr(m["tmpfs"]) {
		v := obj(t)
		a = append(a, "--tmpfs", str(v["target"])+":"+str(v["options"]))
	}
	a = append(a, "--mount", "type=volume,src=os_worker_cache,dst=/opt/cache,readonly", "--cpuset-cpus", cpuList(p.CPUs.Worker), "--cpus", "2", "--cpu-shares", "128", "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "256", "--ulimit", "nofile=8192:8192", "--ulimit", "core=0", "--oom-score-adj", "800", "--log-driver", "none", "--restart", "no")
	for _, name := range envOrder {
		a = append(a, "--env", name+"="+seatEnv[name])
	}
	return append(a, "os-worker-base@sha256:"+p.ImageSHA, "sleep", "infinity"), nil
}

// AssertArgv is an exact allowlist, rather than a denylist of spellings. The
// seccomp file is re-opened and hashed immediately before container creation.
func AssertArgv(argv []string, container, lease string, p LaunchPolicy) error {
	expected, e := RenderArgv(DefaultLaunchSpec(p), container, lease, p)
	if e != nil {
		return e
	}
	if !reflect.DeepEqual(expected, argv) {
		return refusal(5, "containment", "argv outside fixed policy")
	}
	return seccompPin(p)
}
func Create(ctx context.Context, r w8builder.Runner, spec []byte, container, lease string, uid int, p LaunchPolicy) error {
	a, e := RenderArgv(spec, container, lease, p)
	if e != nil {
		return e
	}
	if e = AssertArgv(a, container, lease, p); e != nil {
		return e
	}
	return w8builder.JournalCreate(ctx, r, w8builder.CapsuleJournal{Kind: "task", Lease: lease, Image: p.ImageSHA, Container: container, CallerUID: uid}, a, nil, io.Discard)
}

var verbs = []string{"ingest", "gate", "status", "extract", "purge"}

func ExecArgv(container, verb string) ([]string, error) {
	if !id12.MatchString(container) {
		return nil, refusal(2, "contract", "container shape")
	}
	ok := false
	for _, v := range verbs {
		ok = ok || v == verb
	}
	if !ok {
		return nil, refusal(5, "containment", "exec verb refused")
	}
	a := []string{"docker", "exec"}
	if verb == "ingest" || verb == "gate" {
		a = append(a, "-i")
	}
	return append(a, "-u", "10001:10001", container, "tini", "-s", "--", "setsid", "--wait", "/opt/w8/"+verb), nil
}
func ExecAllowlistSHA() string {
	vectors := make([]any, 0, 5)
	for _, v := range verbs {
		a, _ := ExecArgv("000000000000", v)
		vectors = append(vectors, a)
	}
	return hash(canonical(vectors))
}

func decodeObject(b []byte) (map[string]any, error) {
	m, e := workerbridge.Parse(b)
	if e != nil {
		return nil, refusal(5, "verification", "capsule JSON shape")
	}
	return m, nil
}
func shapeEqual(a, b any) bool { return string(canonical(a)) == string(canonical(b)) }
