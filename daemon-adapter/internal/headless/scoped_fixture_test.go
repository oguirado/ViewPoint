package headless

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

const stubVMM = `#!/usr/bin/python3
import socket

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
    server.bind("vsock.sock")
    server.listen(1)
    conn, _ = server.accept()
    with conn, conn.makefile("rb") as stream:
        if stream.readline() != b"CONNECT 19001\n":
            raise ValueError("unexpected CONNECT")
        conn.sendall(b"OK 1\n")
        hello = stream.readline()
        if not hello.startswith(b"HELLO "):
            raise ValueError("unexpected HELLO")
        conn.sendall(hello)
        nonce = hello[len(b"HELLO "):]
        if stream.readline() != b"STOP " + nonce:
            raise ValueError("unexpected STOP")
        conn.sendall(b"STOPPING " + nonce)
`

// Append only a fixture create method to the real ArmBackend. Stop and release
// run through the production implementation against the offline guest socket.
const stubBackendCreate = `
import shutil

class ArmBackend(ArmBackend):
    def create(self):
        self._check_cgroup()
        self.directory = Path(tempfile.mkdtemp(prefix="stub-", dir=self.config["runtime_root"]))
        self.files = {"engine": self.directory / "firecracker", "vsock": self.directory / "vsock.sock"}
        shutil.copyfile(self.config["artifacts"]["engine"]["path"], self.files["engine"])
        self.files["engine"].chmod(0o500)
        self.process = self._spawn()
        self.pidfd = os.pidfd_open(self.process.pid)
        self.record("fixture_cgroup", self._check_cgroup(self.process.pid))
        self.harness.wait_socket(self.files["vsock"], self.process, self._deadline("startup"))

    def run(self, n):
        self._check_cgroup(self.process.pid)
        return n * (n + 1) * (2 * n + 1) // 6
`

type scopeFixture struct {
	launch ScopedLaunch
	root   string
	state  string
	pin    Pin
}

func kernelFixture(t *testing.T, name string) scopeFixture {
	t.Helper()
	leaf, root := os.Getenv("LH_REDO_FIXTURE_LEAF"), os.Getenv("LH_REDO_EVIDENCE_DIR")
	if leaf == "" || root == "" {
		t.Skip("requires disposable capped cgroup and unshare -Urpf --mount-proc")
	}
	mapping := strings.Fields(string(fixtureRead(t, "/proc/self/uid_map")))
	if os.Getuid() != 0 || len(mapping) != 3 || mapping[0] != "0" || mapping[1] == "0" || mapping[2] != "1" {
		t.Fatal("fixture must be in an unprivileged user namespace")
	}
	if !strings.HasPrefix(filepath.Base(leaf), "lh-redo-") {
		t.Fatal("fixture leaf is outside its disposable surface")
	}
	if err := privateDir(root); err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp(root, name+"-")
	if err != nil {
		t.Fatal(err)
	}
	state := filepath.Join(dir, "state")
	authority := filepath.Join(dir, "authority")
	for _, sub := range []string{state, authority, filepath.Join(dir, "run")} {
		if err := os.Mkdir(sub, 0700); err != nil {
			t.Fatal(err)
		}
	}
	stopPath, lease := filepath.Join(dir, "PARADA.txt"), filepath.Join(dir, "ISOLATED_CORES.lease")
	fixtureWrite(t, stopPath, []byte("LIBRE\n"), 0600)
	observer := filepath.Join(dir, "observer.py")
	fixtureWrite(t, observer, []byte("# fixture observer\n"), 0600)
	scope := os.Getenv("LH_REDO_FIXTURE_SCOPE")
	if scope == "" {
		scope = leaf
	}
	f := scopeFixture{
		launch: ScopedLaunch{RunID: nonce(), RunScope: scope, AttemptLeaf: leaf, AuthorityRoot: authority, LeasePath: lease, StopPath: stopPath},
		root:   dir, state: state, pin: fixturePin(t, observer),
	}
	f.launch.ObserverPins = []Pin{f.pin}
	t.Logf("attempt artifact directory %s", f.attemptPath())
	return f
}

func (f scopeFixture) attemptPath() string {
	return filepath.Join(f.state, "attempt-"+f.launch.RunID)
}

func (f scopeFixture) acknowledge(t *testing.T, kind string) {
	t.Helper()
	boot := strings.TrimSpace(string(fixtureRead(t, "/proc/sys/kernel/random/boot_id")))
	first, err := json.Marshal(map[string]any{
		"run_id": f.launch.RunID, "boot_id": boot, "event": "start", "relay": "Maestro",
		"detail": map[string]string{"scope": filepath.Base(f.launch.RunScope)},
	})
	if err != nil {
		t.Fatal(err)
	}
	first = append(first, '\n')
	fixtureWrite(t, filepath.Join(f.launch.AuthorityRoot, "ANNOUNCE.jsonl"),
		append(append([]byte(nil), first...), []byte("{\"event\":\"end\"}\n")...), 0600)
	if kind == "missing" {
		return
	}
	runID := f.launch.RunID
	if kind == "mismatch" {
		runID = nonce()
	}
	sum := sha256.Sum256(first)
	if err := writeNew(filepath.Join(f.launch.AuthorityRoot, "MAESTRO_NOTIFIED.json"), map[string]string{
		"run_id": runID, "start_sha256": hex.EncodeToString(sum[:]),
	}); err != nil {
		t.Fatal(err)
	}
}

func fixtureRead(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func fixtureWrite(t *testing.T, path string, raw []byte, mode os.FileMode) {
	t.Helper()
	if err := os.WriteFile(path, raw, mode); err != nil {
		t.Fatal(err)
	}
}

func fixtureJSON[T any](t *testing.T, path string) T {
	t.Helper()
	var output T
	if err := json.Unmarshal(fixtureRead(t, path), &output); err != nil {
		t.Fatal(err)
	}
	return output
}

func fixturePin(t *testing.T, path string) Pin {
	t.Helper()
	raw := fixtureRead(t, path)
	hash := sha256.Sum256(raw)
	return Pin{Path: path, SHA256: hex.EncodeToString(hash[:])}
}

func fixtureAttempt(t *testing.T, f scopeFixture) *scopedAttempt {
	t.Helper()
	a, err := openScopedAttempt(f.launch, f.state, []Pin{f.pin})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(a.release)
	return a
}

func fixtureWitness(t *testing.T, f scopeFixture) scopedWitness {
	t.Helper()
	return fixtureJSON[scopedWitness](t, filepath.Join(f.attemptPath(), "close-witness.json"))
}

func fixtureControllerProfile(t *testing.T, f scopeFixture) string {
	t.Helper()
	pwd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	adapter := filepath.Clean(filepath.Join(pwd, "../.."))
	engine := filepath.Join(f.root, "stub-vmm")
	fixtureWrite(t, engine, []byte(stubVMM), 0500)
	python, err := filepath.EvalSymlinks("/usr/bin/python3")
	if err != nil {
		t.Fatal(err)
	}
	// Host root is intentionally unmapped in this namespace. Own the
	// interpreter snapshot as a fixture, just like the other pinned inputs.
	donor := filepath.Join(f.root, "python-donor")
	fixtureWrite(t, donor, fixtureRead(t, python), 0500)
	prefix, err := exec.Command(donor, "-I", "-B", "-c", "import sys; print(sys.prefix)").Output()
	if err != nil {
		t.Fatal(err)
	}
	donors := filepath.Join(f.root, "donors")
	if err := os.Mkdir(donors, 0700); err != nil {
		t.Fatal(err)
	}
	runner := filepath.Join(donors, "headless_runner.py")
	fixtureWrite(t, runner, fixtureRead(t, filepath.Join(adapter, "tools", "headless_runner.py")), 0400)
	manifest := []File{}
	for _, name := range []string{
		"mc06-controller/controller.py", "mc06-controller/arm_backend.py", "mc06-controller/contract_guard.py",
		"mc04-runtime/arm_harness.py", "mc05-contract-candidate/reference_model.py",
		"mc05-contract-candidate/contracts/v0.2/headless_request.schema.json",
		"mc05-contract-candidate/contracts/v0.2/headless_event.schema.json",
		"mc05-contract-candidate/contracts/v0.2/headless_session.schema.json",
	} {
		path := filepath.Join(donors, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		data := fixtureRead(t, filepath.Join(adapter, "guest-kit", name))
		if name == "mc06-controller/arm_backend.py" {
			data = append(data, []byte(stubBackendCreate)...)
		}
		fixtureWrite(t, path, data, 0400)
		pin := fixturePin(t, path)
		manifest = append(manifest, File{Relative: name, Path: path, SHA256: pin.SHA256})
	}
	owner := "actor-" + strings.Repeat("a", 64)
	runtime := filepath.Join(f.root, "run")
	config, err := json.Marshal(map[string]any{
		"schema": "mc06-host-config/v1", "owner_id": owner, "generation": 1, "runtime_admitted": true,
		"artifacts":    map[string]Pin{"engine": fixturePin(t, engine), "kernel": f.pin, "init": f.pin},
		"resources":    map[string]int{"mem_size_mib": 256, "vcpu_count": 1},
		"deadlines":    map[string]int{"startup_seconds": 3, "job_seconds": 3, "stop_seconds": 3},
		"runtime_root": runtime, "external_cgroup": f.launch.AttemptLeaf,
	})
	if err != nil {
		t.Fatal(err)
	}
	profile := Profile{
		Schema: "mv-headless-host/v1", OwnerID: owner, GuardianUnit: filepath.Base(f.launch.RunScope),
		Python: fixturePin(t, donor), PythonPrefix: strings.TrimSpace(string(prefix)),
		Runner:             fixturePin(t, runner),
		ControllerRelative: "mc06-controller/controller.py", Files: manifest, ConfigTemplate: config,
		RuntimeRoot: runtime, StateRoot: f.state, ScopedLaunch: &f.launch,
	}
	path := filepath.Join(f.root, "profile.json")
	if err := writeNew(path, profile); err != nil {
		t.Fatal(err)
	}
	return path
}

func verifyFixtureUnits(t *testing.T, fixture scopeFixture, stage string) {
	t.Helper()
	request := filepath.Join(fixture.root, "units-"+stage)
	fixtureWrite(t, request+".request", []byte(fixture.launch.AttemptLeaf), 0600)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		raw, err := os.ReadFile(request + ".json")
		if errors.Is(err, os.ErrNotExist) {
			time.Sleep(10 * time.Millisecond)
			continue
		}
		var units []json.RawMessage
		if err != nil || json.Unmarshal(raw, &units) != nil || units == nil || len(units) != 0 {
			t.Fatalf("unexpected mv-headless units or unverified listing: %s %v", raw, err)
		}
		return
	}
	t.Fatalf("host systemd unit census missing at %s", stage)
}

type fixtureSession struct {
	Phase    string
	Workload string `json:"workload_id"`
	Gen      int    `json:"generation"`
}

func startScopedStub(t *testing.T, f scopeFixture, validate func(json.RawMessage) error) (*Manager, fixtureSession) {
	t.Helper()
	t.Setenv("MV_HEADLESS_PROFILE", fixtureControllerProfile(t, f))
	t.Setenv("MV_HEADLESS_GUARDIAN_UNIT", "")
	m, err := NewFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := m.Close(); err != nil {
			t.Error(err)
		}
	})
	out, err := m.Handle(t.Context(), json.RawMessage(fixtureCreate), validate)
	if err != nil || len(out) != 2 {
		t.Fatalf("create: %s, %v", out, err)
	}
	var ready struct {
		Event string
		Data  struct {
			Session fixtureSession
		}
	}
	if err := json.Unmarshal(out[1], &ready); err != nil || ready.Event != "state_changed" ||
		ready.Data.Session.Phase != "ready" || ready.Data.Session.Workload == "" {
		t.Fatalf("stub readiness not proved: %s (%v)", out[1], err)
	}
	return m, ready.Data.Session
}

func TestScopedStubCycle(t *testing.T) {
	f := kernelFixture(t, "cycle")
	f.acknowledge(t, "matching")
	ack := filepath.Join(f.launch.AuthorityRoot, "MAESTRO_NOTIFIED.json")
	before := string(fixtureRead(t, ack))
	validate := actualEventValidator(t)
	verifyFixtureUnits(t, f, "before")
	m, ready := startScopedStub(t, f, validate)
	limits := fixtureJournalData[scopedLimits](t, f, "fixture_cgroup")
	if limits.Path != f.launch.AttemptLeaf || limits.MemoryHigh != 6<<30 || limits.MemoryMax != 8<<30 ||
		limits.SwapMax != 0 || limits.PIDsMax != 256 || limits.AllowedCPUs != "0-15" {
		t.Fatalf("unexpected scoped backend limits: %+v", limits)
	}
	t.Logf("scoped backend limits: %+v", limits)
	roster := fixtureJSON[scopedRoster](t, filepath.Join(f.attemptPath(), "pids.json"))
	if len(roster.PIDs) != 2 || len(roster.Expected) != 2 {
		t.Fatalf("invalid live roster: %+v", roster)
	}
	for _, pid := range roster.PIDs {
		group, err := procGroup(pid.PID)
		if err != nil || group != strings.TrimPrefix(f.launch.AttemptLeaf, cgroupRoot) || group != pid.Cgroup {
			t.Fatalf("recorded PID not inside run scope: %v (%v)", pid, err)
		}
		var cpu string
		for _, line := range strings.Split(string(fixtureRead(t, "/proc/"+strconv.Itoa(pid.PID)+"/status")), "\n") {
			if after, ok := strings.CutPrefix(line, "Cpus_allowed_list:"); ok {
				cpu = strings.TrimSpace(after)
				break
			}
		}
		if err := verifyCPUSet(cpu); err != nil {
			t.Fatal(err)
		}
		t.Logf("role=%s PID=%d start_ticks=%d cgroup=%s CPUs=%s", pid.Role, pid.PID, pid.StartTicks, group, cpu)
	}
	verifyFixtureUnits(t, f, "during")
	stop, err := json.Marshal(map[string]any{
		"schema": "headless_request/v0.2", "request_id": "request-live-stop", "op": "stop",
		"params": map[string]any{"workload_id": ready.Workload, "generation": ready.Gen},
	})
	if err != nil {
		t.Fatal(err)
	}
	out, err := m.Handle(t.Context(), stop, validate)
	if err != nil || len(out) != 2 || !strings.Contains(string(out[1]), `"phase":"stopped"`) ||
		!strings.Contains(string(out[1]), `"ownership":"released"`) {
		t.Fatalf("stop: %s, %v", out, err)
	}
	proof := fixtureJournalData[struct {
		Closed     bool
		Outcome    string
		ExitStatus *int `json:"exit_status"`
	}](t, f, "firecracker_stop")
	if !proof.Closed || proof.Outcome != "guest_stopped" || proof.ExitStatus == nil || *proof.ExitStatus != 0 {
		t.Fatalf("real ArmBackend.stop() closure unproved: %+v", proof)
	}
	t.Logf("ArmBackend.stop: outcome=%s exit_status=%d", proof.Outcome, *proof.ExitStatus)
	verifyFixtureUnits(t, f, "after")
	if string(fixtureRead(t, ack)) != before {
		t.Fatal("product changed the fixture owner's acknowledgement")
	}
	w := fixtureWitness(t, f)
	teardown := fixtureJSON[scopedWitness](t, filepath.Join(f.attemptPath(), "teardown.json"))
	if w.Status != "PASS" || teardown.Status != "PASS" || !w.Empty || len(w.Signalled) != 0 || !slices.Equal(w.PIDs, roster.PIDs) {
		t.Fatalf("stub closure unproved: %+v", w)
	}
	for _, pid := range w.PIDs {
		if _, err := startTicks(pid.PID); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("recorded identity still visible: %+v", pid)
		}
	}
	digests := fixtureJSON[[]scopedDigest](t, filepath.Join(f.attemptPath(), "digests.json"))
	if len(digests) == 0 {
		t.Fatal("no source digests")
	}
	for _, pin := range digests {
		if pin.Before != pin.SHA256 || pin.After != pin.SHA256 || pin.Error != "" {
			t.Fatalf("unstable digest: %+v", pin)
		}
	}
	checkFixtureStages(t, f, roster.BootID)
}

func fixtureJournalData[T any](t *testing.T, f scopeFixture, kind string) T {
	t.Helper()
	var data T
	raw := fixtureRead(t, filepath.Join(f.attemptPath(), "state", "journal.jsonl"))
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var row struct {
			Kind string
			Data json.RawMessage
		}
		if err := json.Unmarshal([]byte(line), &row); err != nil {
			t.Fatal(err)
		}
		if row.Kind == kind {
			if err := json.Unmarshal(row.Data, &data); err != nil {
				t.Fatal(err)
			}
			return data
		}
	}
	t.Fatalf("journal record %q missing", kind)
	return data
}

func TestScopedBackendRejectsChangedScope(t *testing.T) {
	for _, test := range []struct {
		name, value string
	}{
		{"pids.max", "257"},
		{"memory.high", "5368709120"},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := kernelFixture(t, "changed-scope")
			f.acknowledge(t, "matching")
			validate := actualEventValidator(t)
			m, ready := startScopedStub(t, f, validate)
			path := filepath.Join(f.launch.RunScope, test.name)
			original := fixtureRead(t, path)
			t.Cleanup(func() { fixtureWrite(t, path, original, 0600) })
			fixtureWrite(t, path, []byte(test.value), 0600)
			if actual := strings.TrimSpace(string(fixtureRead(t, path))); actual != test.value {
				t.Fatalf("changed scope limit not enforced: %s=%q", test.name, actual)
			}
			body, err := json.Marshal(map[string]any{
				"schema": "headless_request/v0.2", "request_id": "request-changed-limit", "op": "run",
				"params": map[string]any{"workload_id": ready.Workload, "generation": ready.Gen,
					"job_id": "job-changed-limit", "operation": "sumsq", "n": 3},
			})
			if err != nil {
				t.Fatal(err)
			}
			out, err := m.Handle(t.Context(), body, validate)
			if err != nil || len(out) != 2 || !strings.Contains(string(out[1]), `"event":"outcome_unknown"`) {
				t.Fatalf("run under changed scope: %s, %v", out, err)
			}
			failure := fixtureJournalData[map[string]string](t, f, "failure")
			if failure["op"] != "run" || failure["type"] != "RuntimeError" {
				t.Fatalf("scoped backend refusal unproved: %+v", failure)
			}
			t.Logf("admitted original scope and refused changed %s=%s", test.name, test.value)
		})
	}
}

func checkFixtureStages(t *testing.T, f scopeFixture, boot string) {
	t.Helper()
	var last int64
	var seq uint64
	var names []string
	scanner := bufio.NewScanner(strings.NewReader(string(fixtureRead(t, filepath.Join(f.attemptPath(), "stages.jsonl")))))
	for scanner.Scan() {
		var row struct {
			Stage    string
			Attempt  string `json:"attempt_id"`
			Boot     string `json:"boot_id"`
			Sequence uint64
			Clock    int64 `json:"monotonic_ns"`
		}
		if json.Unmarshal(scanner.Bytes(), &row) != nil || row.Attempt != f.launch.RunID || row.Boot != boot ||
			row.Sequence != seq+1 || row.Clock <= last {
			t.Fatalf("invalid stage row: %s", scanner.Bytes())
		}
		last, seq = row.Clock, row.Sequence
		names = append(names, row.Stage)
	}
	expected := []string{"create_request", "controller_spawn_intent", "controller_spawned", "controller_ready",
		"firecracker_spawn_intent", "firecracker_spawned", "guest_ready", "stop", "exit"}
	if scanner.Err() != nil || !slices.Equal(names, expected) {
		t.Fatalf("stages %v: %v", names, scanner.Err())
	}
	t.Logf("ordered stage names %v on boot %s", names, boot)
}

const fixtureCreate = `{"schema":"headless_request/v0.2","request_id":"request-live-create","op":"create","params":{"template":"headless-sumsq"}}`

func requireProductRefusal(t *testing.T, f scopeFixture) {
	t.Helper()
	t.Setenv("MV_HEADLESS_PROFILE", fixtureControllerProfile(t, f))
	t.Setenv("MV_HEADLESS_GUARDIAN_UNIT", "")
	m, err := NewFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	defer m.Close()
	validate := actualEventValidator(t)
	out, err := m.Handle(t.Context(), json.RawMessage(fixtureCreate), validate)
	if err != nil || len(out) != 1 || validate(out[0]) != nil ||
		!strings.Contains(string(out[0]), `"event":"refused"`) || !strings.Contains(string(out[0]), `"code":"UNAVAILABLE"`) {
		t.Fatalf("scoped create did not refuse: %s, %v", out, err)
	}
}

func TestScopedGateRefusals(t *testing.T) {
	for _, test := range []struct {
		name string
		ack  string
		code error
	}{
		{"lease", "matching", errOccupancy},
		{"maestro_missing", "missing", errMaestro},
		{"maestro_mismatch", "mismatch", errMaestro},
		{"pin_mutation", "matching", errPin},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := kernelFixture(t, test.name)
			f.acknowledge(t, test.ack)
			if err := (&scopedAttempt{config: f.launch}).window(); err != nil {
				t.Fatalf("numeric headroom/load and clear lease baseline must pass: %v", err)
			}
			t.Log("numeric headroom/load pass and lease absent before contrast")
			if test.name == "pin_mutation" {
				a := fixtureAttempt(t, f)
				if err := a.preflight(); err != nil {
					t.Fatal(err)
				}
				fixtureWrite(t, f.pin.Path, []byte("# changed after pin\n"), 0600)
				_, refusal := a.spawn("controller", exec.Command("/usr/bin/true"))
				if !errors.Is(refusal, test.code) {
					t.Fatalf("expected %v, got %v", test.code, refusal)
				}
				if err := a.refuse(refusal); err != nil {
					t.Fatal(err)
				}
			} else {
				if test.name == "lease" {
					fixtureWrite(t, f.launch.LeasePath, []byte("fixture-competing-owner\n"), 0600)
				}
				requireProductRefusal(t, f)
			}
			w := fixtureWitness(t, f)
			if w.Status != "never_started" || w.Code != test.code.Error() || !w.Empty || len(w.PIDs) != 0 || len(w.Signalled) != 0 {
				t.Fatalf("refusal closure invalid: %+v", w)
			}
			if test.ack == "missing" {
				if _, err := os.Lstat(filepath.Join(f.launch.AuthorityRoot, "MAESTRO_NOTIFIED.json")); !errors.Is(err, os.ErrNotExist) {
					t.Fatal("missing ack appeared")
				}
			}
			t.Logf("code=%s status=%s empty=%t recorded_pids=%d signalled=%d", w.Code, w.Status, w.Empty, len(w.PIDs), len(w.Signalled))
		})
	}
}

func TestScopedCPUs16Through19Refused(t *testing.T) {
	if os.Getenv("LH_REDO_CPU_NEGATIVE") == "" {
		t.Skip("requires an empty, disposable scope configured for CPUs 16-19")
	}
	f := kernelFixture(t, "cpus-16-19")
	f.acknowledge(t, "matching")
	cpus := strings.TrimSpace(string(fixtureRead(t, filepath.Join(f.launch.AttemptLeaf, "cpuset.cpus.effective"))))
	if cpus != "16-19" {
		t.Fatalf("negative fixture has cpuset %q", cpus)
	}
	requireProductRefusal(t, f)
	w := fixtureWitness(t, f)
	if w.Status != "never_started" || !w.Empty || len(w.PIDs) != 0 || !strings.Contains(w.Detail, "effective cpuset outside CPUs") {
		t.Fatalf("CPU refusal not witnessed: %+v", w)
	}
	t.Logf("cpuset=%s status=%s empty=%t recorded_pids=%d", cpus, w.Status, w.Empty, len(w.PIDs))
}

func fixtureIdleChild(t *testing.T, g *scopedGroup, role string) *scopedChild {
	t.Helper()
	cmd := exec.Command("/usr/bin/sleep", "infinity")
	raw := -1
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: int(g.fd.Fd()), PidFD: &raw}
	if err := cmd.Start(); err != nil || raw < 0 {
		t.Fatalf("fixture child start: %v", err)
	}
	id, err := g.identity(role, cmd.Process.Pid)
	if err != nil {
		t.Fatal(err)
	}
	p := &scopedChild{pid: id, handle: os.NewFile(uintptr(raw), role+"-fixture-pidfd"), exited: make(chan struct{})}
	go func() {
		_ = cmd.Wait()
		close(p.exited)
	}()
	return p
}

func fixtureClear(t *testing.T, g *scopedGroup, children ...*scopedChild) {
	t.Helper()
	for _, p := range children {
		if errors.Is(signalScopedPID(p.handle, 0), syscall.ESRCH) {
			continue
		}
		current, err := g.identity(p.pid.Role, p.pid.PID)
		if err != nil || current != p.pid {
			t.Fatalf("fixture cleanup identity unproved: %v", err)
		}
		if err := signalScopedPID(p.handle, syscall.SIGKILL); err != nil {
			t.Fatal(err)
		}
	}
	if err := g.writeFreeze("0"); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	for _, p := range children {
		select {
		case <-p.exited:
		case <-ctx.Done():
			t.Fatal("fixture child exit missing")
		}
	}
	if err := g.waitEvent(ctx, "populated", "0"); err != nil {
		t.Fatal(err)
	}
}

func TestScopedSignalCustody(t *testing.T) {
	for _, kind := range []string{"unrecorded", "stale_start_ticks", "recorded_forced"} {
		t.Run(kind, func(t *testing.T) {
			f := kernelFixture(t, kind)
			f.acknowledge(t, "matching")
			a := fixtureAttempt(t, f)
			if err := a.preflight(); err != nil {
				t.Fatal(err)
			}
			known, err := a.spawn("controller", exec.Command("/usr/bin/sleep", "infinity"))
			if err != nil {
				t.Fatal(err)
			}
			children := []*scopedChild{known}
			t.Cleanup(func() {
				defer func() {
					for _, child := range children[1:] {
						child.handle.Close()
					}
				}()
				fixtureClear(t, a.leaf, children...)
			})
			switch kind {
			case "unrecorded":
				extra := fixtureIdleChild(t, a.leaf, "unrecorded")
				children = append(children, extra)
			case "stale_start_ticks":
				a.roster.PIDs[0].StartTicks++
				if err := replaceScopedJSON(a.dir, "pids.json", a.roster); err != nil {
					t.Fatal(err)
				}
			}
			ctx, cancel := context.WithTimeout(t.Context(), 3*time.Second)
			defer cancel()
			err = a.retire(ctx)
			w := fixtureWitness(t, f)
			if kind == "recorded_forced" {
				if err != nil || w.Status != "PASS" || !w.Frozen || !w.Empty || !slices.Equal(w.Signalled, []int{known.pid.PID}) {
					t.Fatalf("retained pidfd force closure failed: %+v (%v)", w, err)
				}
				before := fixtureJSON[scopedWitness](t, filepath.Join(a.dir, "pre-signal-witness.json"))
				if before.Status != "custody_proved" || !before.Frozen ||
					!slices.Equal(before.Members, w.Signalled) || len(before.Signalled) != 0 {
					t.Fatalf("pre-signal witness invalid: %+v", before)
				}
			} else {
				if !errors.Is(err, errCustody) || w.Status != "quarantine" || w.Code != "CUSTODY_UNPROVED" ||
					!w.Frozen || w.Empty || len(w.Signalled) != 0 {
					t.Fatalf("bad custody refusal: %+v (%v)", w, err)
				}
				for _, child := range children {
					if signalScopedPID(child.handle, 0) != nil {
						t.Fatalf("fixture PID %d was signalled", child.pid.PID)
					}
				}
			}
			t.Logf("status=%s code=%s detail=%s frozen=%t signalled=%v", w.Status, w.Code, w.Detail, w.Frozen, w.Signalled)
		})
	}
}

func TestScopedCrashRecovery(t *testing.T) {
	f := kernelFixture(t, "crash")
	f.acknowledge(t, "matching")
	// This private test process adopts the unclaimed child solely to clean up
	// the fixture. Recovery itself receives no pidfd from the writer.
	const setChildSubreaper = 36
	if _, _, e := syscall.Syscall6(syscall.SYS_PRCTL, setChildSubreaper, 1, 0, 0, 0, 0); e != 0 {
		t.Fatal(e)
	}
	config, err := json.Marshal(f.launch)
	if err != nil {
		t.Fatal(err)
	}
	sockets, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_SEQPACKET|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		t.Fatal(err)
	}
	parent := os.NewFile(uintptr(sockets[0]), "fixture-owner")
	defer parent.Close()
	writerEnd := os.NewFile(uintptr(sockets[1]), "fixture-writer")
	defer writerEnd.Close()
	if err := syscall.SetsockoptTimeval(sockets[0], syscall.SOL_SOCKET, syscall.SO_RCVTIMEO, &syscall.Timeval{Sec: 5}); err != nil {
		t.Fatal(err)
	}
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(self, "-test.run=^TestScopedCrashWriterHelper$")
	cmd.Env = append(os.Environ(), "LH_REDO_CRASH_WRITER="+f.state, "LH_REDO_CRASH_CONFIG="+string(config))
	cmd.ExtraFiles = []*os.File{writerEnd}
	cmd.Stderr = os.Stderr
	writerFD := -1
	cmd.SysProcAttr = &syscall.SysProcAttr{PidFD: &writerFD}
	if err := cmd.Start(); err != nil || writerFD < 0 {
		t.Fatalf("fixture writer start: %v", err)
	}
	writerEnd.Close()
	writerHandle := os.NewFile(uintptr(writerFD), "fixture-writer-pidfd")
	t.Cleanup(func() { writerHandle.Close() })
	writerTicks, err := startTicks(cmd.Process.Pid)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		// The normal path has already reaped this exact writer.
		if ticks, err := startTicks(cmd.Process.Pid); err == nil && ticks == writerTicks {
			_ = signalScopedPID(writerHandle, syscall.SIGKILL)
			_ = cmd.Wait()
		}
	})
	body := make([]byte, 4096)
	control := make([]byte, syscall.CmsgSpace(4))
	n, used, _, _, err := syscall.Recvmsg(sockets[0], body, control, syscall.MSG_CMSG_CLOEXEC)
	if err != nil {
		t.Fatal(err)
	}
	var child scopedPID
	if err := json.Unmarshal(body[:n], &child); err != nil {
		t.Fatal(err)
	}
	messages, err := syscall.ParseSocketControlMessage(control[:used])
	if err != nil || len(messages) != 1 {
		t.Fatalf("fixture custody ancillary data: %v", err)
	}
	fds, err := syscall.ParseUnixRights(&messages[0])
	if err != nil || len(fds) != 1 {
		t.Fatalf("fixture custody descriptor: %v", err)
	}
	childHandle := os.NewFile(uintptr(fds[0]), "fixture-child-pidfd")
	defer childHandle.Close()
	g, err := openScopedGroup(f.launch.AttemptLeaf)
	if err != nil {
		t.Fatal(err)
	}
	defer g.close()
	if actual, err := g.identity(child.Role, child.PID); err != nil || actual != child {
		t.Fatalf("fixture child identity: %+v (%v)", actual, err)
	}
	roster := fixtureJSON[scopedRoster](t, filepath.Join(f.attemptPath(), "pids.json"))
	if len(roster.PIDs) != 0 {
		t.Fatal("writer already recorded spawned PID")
	}
	if actual, err := startTicks(cmd.Process.Pid); err != nil || actual != writerTicks {
		t.Fatalf("fixture writer start identity changed: %d (%v)", actual, err)
	}
	if err := signalScopedPID(writerHandle, syscall.SIGKILL); err != nil {
		t.Fatal(err)
	}
	waitErr := cmd.Wait()
	if status, ok := cmd.ProcessState.Sys().(syscall.WaitStatus); waitErr == nil || !ok || status.Signal() != syscall.SIGKILL {
		t.Fatalf("simulated crash was not observed: %v", waitErr)
	}
	defer func() {
		if actual, err := g.identity(child.Role, child.PID); err != nil || actual != child {
			t.Errorf("cleanup identity missing: %+v (%v)", actual, err)
			return
		}
		if err := signalScopedPID(childHandle, syscall.SIGKILL); err != nil {
			t.Error(err)
			return
		}
		if err := g.writeFreeze("0"); err != nil {
			t.Error(err)
			return
		}
		var status syscall.WaitStatus
		if pid, err := syscall.Wait4(child.PID, &status, 0, nil); err != nil || pid != child.PID || status.Signal() != syscall.SIGKILL {
			t.Errorf("fixture adopted child not reaped: pid=%d status=%v err=%v", pid, status, err)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := g.waitEvent(ctx, "populated", "0"); err != nil {
			t.Error(err)
		}
		if _, err := startTicks(child.PID); !errors.Is(err, os.ErrNotExist) {
			t.Errorf("fixture child remains: %v", err)
		}
		t.Log("fixture owner reaped the quarantined child using its separately retained handle; leaf empty")
	}()
	err = reconcileScoped(&Profile{ScopedLaunch: &f.launch, StateRoot: f.state})
	if !errors.Is(err, errCustody) {
		t.Fatalf("recovery should retain responsibility: %v", err)
	}
	w := fixtureWitness(t, f)
	if w.Status != "quarantine" || w.Code != "CUSTODY_UNPROVED" || !w.Frozen || w.Empty ||
		len(w.Signalled) != 0 || len(w.PIDs) != 0 || !slices.Equal(w.Members, []int{child.PID}) ||
		!slices.Equal(w.Observed, []scopedPID{{Role: "unclaimed", PID: child.PID, StartTicks: child.StartTicks, Cgroup: child.Cgroup}}) {
		t.Fatalf("recovery quarantine unproved: %+v", w)
	}
	t.Logf("writer PID=%d start_ticks=%d terminated by fixture; unrecorded PID=%d start_ticks=%d frozen, signalled=%v",
		cmd.Process.Pid, writerTicks, child.PID, child.StartTicks, w.Signalled)
}

func TestScopedCrashWriterHelper(t *testing.T) {
	state := os.Getenv("LH_REDO_CRASH_WRITER")
	if state == "" {
		t.Skip("fixture-owned writer only")
	}
	var config ScopedLaunch
	if err := json.Unmarshal([]byte(os.Getenv("LH_REDO_CRASH_CONFIG")), &config); err != nil {
		t.Fatal(err)
	}
	a, err := openScopedAttempt(config, state, config.ObserverPins)
	if err != nil {
		t.Fatal(err)
	}
	defer a.release()
	if err := a.preflight(); err != nil {
		t.Fatal(err)
	}
	if err := a.stage("firecracker_spawn_intent"); err != nil {
		t.Fatal(err)
	}
	// Hold the deliberate crash point after clone3 and before the durable
	// roster append. The side-channel is solely the test owner's cleanup proof.
	p := fixtureIdleChild(t, a.leaf, "firecracker")
	data, err := json.Marshal(p.pid)
	if err != nil {
		t.Fatal(err)
	}
	if err = syscall.Sendmsg(3, data, syscall.UnixRights(int(p.handle.Fd())), nil, 0); err != nil {
		t.Fatal(err)
	}
	time.Sleep(30 * time.Second)
	t.Fatal("fixture writer crash did not arrive")
}
