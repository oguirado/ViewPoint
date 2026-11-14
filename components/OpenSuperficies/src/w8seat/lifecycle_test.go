package w8seat

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
	"opensuperficies.local/srf/src/workerbridge"
)

type fakeRuntime struct {
	gates                                                 int
	mu                                                    sync.Mutex
	policy                                                LaunchPolicy
	containers                                            map[string]string
	created, journals, maxLive                            int
	block                                                 chan struct{}
	headMismatch, unsafeInspect, unsafeProbe, failCleanup bool
	raw, rc                                               int
	calls                                                 [][]string
	statusPeak                                            any
}

func newRuntime() *fakeRuntime {
	return &fakeRuntime{policy: testPolicy(), statusPeak: int64(4096), containers: map[string]string{}}
}
func (f *fakeRuntime) Run(ctx context.Context, a []string, in io.Reader, out io.Writer) error {
	f.mu.Lock()
	f.calls = append(f.calls, append([]string(nil), a...))
	f.mu.Unlock()
	if a[0] == "systemd-cat" {
		b, _ := io.ReadAll(in)
		if bytes.Contains(b, []byte("W8_CAPSULE_CREATED")) {
			f.mu.Lock()
			f.journals++
			f.mu.Unlock()
		}
		return nil
	}
	if a[0] != "docker" {
		return fmt.Errorf("unexpected command")
	}
	switch a[1] {
	case "info":
		_, _ = io.WriteString(out, `{"SecurityOptions":["name=rootless"],"CgroupVersion":"2"}`)
	case "run":
		container, lease := "", ""
		for i, x := range a {
			if x == "--name" {
				container = a[i+1]
			}
			if strings.HasPrefix(x, "w8.lease=") {
				lease = strings.TrimPrefix(x, "w8.lease=")
			}
		}
		f.mu.Lock()
		f.created++
		f.containers[container] = lease
		if len(f.containers) > f.maxLive {
			f.maxLive = len(f.containers)
		}
		f.mu.Unlock()
	case "inspect":
		f.mu.Lock()
		lease := f.containers[a[2]]
		f.mu.Unlock()
		m := fakeInspect(f.policy, a[2], lease)
		if f.unsafeInspect {
			obj(m["HostConfig"])["ReadonlyRootfs"] = false
		}
		_, _ = out.Write(canonical([]any{m}))
	case "exec":
		verb := strings.TrimPrefix(a[len(a)-1], "/opt/w8/")
		switch verb {
		case "ingest":
			var h map[string]any
			if e := ReadFrame(in, &h); e != nil {
				return e
			}
			_, _ = io.Copy(io.Discard, in)
			head := strings.Repeat("a", 40)
			if f.headMismatch {
				head = strings.Repeat("b", 40)
			}
			p := map[string]any{"etc_write": "read_only", "home_admin": "absent", "egress_probe": "BLOCKED", "gateway_probe": "NO_ROUTE", "env_secret_names": 0, "runtime_socket_visible": false, "inference_processes": 0}
			if f.unsafeProbe {
				p["home_admin"] = "present"
			}
			_, _ = out.Write(canonical(map[string]any{"rc": 0, "head": head, "reason": "ok", "probes": p}))
		case "gate":
			f.mu.Lock()
			f.gates++
			f.mu.Unlock()
			if f.block != nil {
				select {
				case <-f.block:
				case <-ctx.Done():
					return ctx.Err()
				}
			}
			_, _ = out.Write(canonical(map[string]any{"rc": f.rc, "raw_status": f.raw, "signal": "", "timed_out": false, "state": "done", "workspace_peak_bytes": 2048, "stdout_total_bytes": 0, "stdout_retained_bytes": 0, "stdout_sha256": hash(nil), "stdout_truncated": false}))
		case "status":
			_, _ = out.Write(canonical(map[string]any{"rc": 0, "state": "running", "workspace_peak_bytes": f.statusPeak}))
		case "extract":
			_, _ = out.Write(fakeArtifacts())
		case "purge":
			if f.failCleanup {
				return fmt.Errorf("daemon lost")
			}
			_, _ = io.WriteString(out, `{"remaining_workspace_bytes":0}`)
		default:
			return fmt.Errorf("unexpected exec verb")
		}
	case "top":
		// Real dockerd: ps options without a pid column are refused.
		if !strings.Contains(a[len(a)-1], "pid") {
			return fmt.Errorf("Couldn't find PID field in ps output")
		}
		_, _ = io.WriteString(out, "PID                 STAT                COMMAND\n4101                Ss                  tini\n4133                S                   sleep\n")
	case "rm":
		f.mu.Lock()
		delete(f.containers, a[len(a)-1])
		f.mu.Unlock()
	case "ps":
		f.mu.Lock()
		defer f.mu.Unlock()
		for c, l := range f.containers {
			if strings.Contains(strings.Join(a, " "), "label=w8.seat=1") || strings.Contains(strings.Join(a, " "), "label=w8.lease="+l) {
				_, _ = io.WriteString(out, c+"\n")
			}
		}
	default:
		return fmt.Errorf("unexpected docker operation")
	}
	return nil
}
func fakeInspect(p LaunchPolicy, c, lease string) map[string]any {
	spec, _ := workerbridge.Parse(DefaultLaunchSpec(p))
	tmp := map[string]any{}
	for _, x := range arr(spec["tmpfs"]) {
		v := obj(x)
		tmp[str(v["target"])] = v["options"]
	}
	env := []string{"PATH=" + ImagePATH}
	for _, k := range envOrder {
		env = append(env, k+"="+seatEnv[k])
	}
	return map[string]any{"Name": "/" + c, "Config": map[string]any{"Labels": map[string]any{"w8.seat": "1", "w8.lease": lease}, "User": "10001:10001", "Entrypoint": []string{"/usr/bin/tini", "-s", "--"}, "Image": "os-worker-base@sha256:" + p.ImageSHA, "Env": env}, "State": map[string]any{"Pid": 123}, "HostConfig": map[string]any{"ReadonlyRootfs": true, "Privileged": false, "NetworkMode": "none", "CapDrop": []string{"ALL"}, "Binds": nil, "Devices": []any{}, "DeviceRequests": nil, "CapAdd": nil, "Memory": int64(4 << 30), "MemorySwap": int64(4 << 30), "PidsLimit": 256, "OomScoreAdj": 800, "NanoCpus": int64(2e9), "CpuShares": 128, "CpusetCpus": cpuList(p.CPUs.Worker), "SecurityOpt": []string{"no-new-privileges:true", "seccomp=" + p.SeccompInline}, "Ulimits": []any{map[string]any{"Name": "nofile", "Hard": 8192, "Soft": 8192}, map[string]any{"Name": "core", "Hard": 0, "Soft": 0}}, "LogConfig": map[string]any{"Type": "none"}, "RestartPolicy": map[string]any{"Name": "no"}, "Tmpfs": tmp}, "Mounts": []any{map[string]any{"Type": "volume", "Name": "os_worker_cache", "Destination": "/opt/cache", "RW": false}}}
}
func fakeArtifacts() []byte {
	var b bytes.Buffer
	w := tar.NewWriter(&b)
	for _, x := range []struct{ name, body string }{{"report.txt", "gate passed\n"}, {"receipt-data.json", `{"files":[],"sources_observed":[{"path":"src/work.go","sha256":"` + strings.Repeat("b", 64) + `"}],"answers":[{"request_id":"e0","answer":"gate passed (fake)"}]}`}} {
		_ = w.WriteHeader(&tar.Header{Name: x.name, Mode: 0644, Size: int64(len(x.body)), Format: tar.FormatUSTAR})
		_, _ = w.Write([]byte(x.body))
	}
	_ = w.Close()
	return b.Bytes()
}
func taskFixture(t *testing.T, attempt string) []byte {
	t.Helper()
	b, e := os.ReadFile("../../testdata/workerbridge/codec.json")
	if e != nil {
		t.Fatal(e)
	}
	var all map[string]json.RawMessage
	_ = json.Unmarshal(b, &all)
	bridge, e := workerbridge.Parse(all["bridge"])
	if e != nil {
		t.Fatal(e)
	}
	bridge["attempt_id"] = attempt
	bridge["lease_id"] = "lease-" + attempt
	bridge["expires_at_utc"] = time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	bridge["evidence_requests"] = []any{"State gate result."}
	bridge["writer_branch"] = "bridge/os-w8-fixture/" + attempt
	worker, e := workerbridge.BridgeToWorker(canonical(bridge), "fake task", 1000)
	if e != nil {
		t.Fatal(e)
	}
	return worker
}
func configFixture(dir string, r *fakeRuntime) Config {
	_ = os.Chmod(dir, 0700)
	return Config{StateDir: dir, ReadPeaks: func(string) (int64, int64, error) { return 4096, 2, nil }, Runner: r, Policy: r.policy, CacheSHA: strings.Repeat("c", 64), BaseImage: map[string]any{"name": "os-worker-base", "digest": r.policy.ImageSHA, "recipe_sha256": strings.Repeat("f", 64), "approval_sha256": strings.Repeat("b", 64)}, Telemetry: &gobernador.TelemetriaSintetica{DisponibleBytes: 100 << 30}, CheckProfile: func(LaunchPolicy) error { return nil }, CgroupPath: func(int) (string, error) { return "fixture-memory.current", nil }}
}
func seatFixture(t *testing.T, r *fakeRuntime) *Seat {
	t.Helper()
	s, e := NewSeat(context.Background(), configFixture(t.TempDir(), r))
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	return s
}
func submitFixture(t *testing.T, s *Seat, attempt string) (Response, Request) {
	t.Helper()
	w := taskFixture(t, attempt)
	req := Request{Verb: "submit", WorkerTask: w, BundleLen: 6, ObservedIntegrationTip: strings.Repeat("a", 40)}
	r, e := s.Submit(1000, req, strings.NewReader("bundle"))
	if e != nil {
		t.Fatal(e)
	}
	return r, req
}
func awaitFixture(t *testing.T, s *Seat, h string) Response {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	r, e := s.Request(ctx, 1000, Request{Verb: "wait", Handle: h})
	if e != nil || !r.Ready || len(r.Receipt) == 0 {
		t.Fatalf("not ready %+v %v", r, e)
	}
	if e = validateReceipt(r.Receipt); e != nil {
		t.Fatal(e, string(r.Receipt))
	}
	return r
}
func TestResubmitNoSecondCapsuleOrJournalLine(t *testing.T) {
	r := newRuntime()
	s := seatFixture(t, r)
	first, req := submitFixture(t, s, "dedupe")
	again, e := s.Submit(1000, req, strings.NewReader("bundle"))
	if e != nil || again.Handle != first.Handle {
		t.Fatal(again, e)
	}
	done := awaitFixture(t, s, first.Handle)
	if done.ExitCode != 0 {
		t.Fatal(string(done.Receipt))
	}
	replay, e := s.Submit(1000, req, strings.NewReader("bundle"))
	if e != nil || !bytes.Equal(replay.Receipt, done.Receipt) {
		t.Fatal(replay, e)
	}
	_, e = s.Submit(1000, req, strings.NewReader("other!"))
	if ExitCode(e) != 2 {
		t.Fatal(e)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.created != 1 || r.journals != 1 {
		t.Fatal(r.created, r.journals)
	}
}
func TestSeatCPUCeilingAndNoLatencyFeed(t *testing.T) {
	r := newRuntime()
	r.block = make(chan struct{})
	s := seatFixture(t, r)
	handles := []string{}
	for i := 0; i < 8; i++ {
		resp, _ := submitFixture(t, s, fmt.Sprintf("a%d", i))
		handles = append(handles, resp.Handle)
	}
	time.Sleep(50 * time.Millisecond)
	state, e := s.GovernorState()
	if e != nil || state.Vivas > 2 || state.MaxCapsulas != 2 {
		t.Fatal(state, e)
	}
	close(r.block)
	for _, h := range handles {
		if got := awaitFixture(t, s, h); got.ExitCode != 0 {
			t.Fatal(string(got.Receipt))
		}
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.maxLive > 2 || r.created != 8 {
		t.Fatal(r.maxLive, r.created)
	}
}
func TestSeatReceiptLeaseIDMatchesBridgeTask(t *testing.T) {
	s := seatFixture(t, newRuntime())
	r, _ := submitFixture(t, s, "binding")
	done := awaitFixture(t, s, r.Handle)
	m, _ := workerbridge.Parse(done.Receipt)
	if number(obj(m["observations"])["workspace_peak_bytes"]) != 4096 {
		t.Fatal("extract peak not carried into receipt", m)
	}
	if m["lease_id"] != "lease-binding" {
		t.Fatal(m)
	}
}
func TestLeaseValidatedPerVerb(t *testing.T) {
	r := newRuntime()
	r.block = make(chan struct{})
	s := seatFixture(t, r)
	response, _ := submitFixture(t, s, "fenced")
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		r.mu.Lock()
		g := r.gates
		r.mu.Unlock()
		if g > 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if e := s.lessor.RevocarCapsula("lease-fenced"); e != nil {
		t.Fatal(e)
	}
	for _, verb := range []string{"receipt", "wait", "cancel", "ack"} {
		if _, e := s.Request(context.Background(), 1000, Request{Verb: verb, Handle: response.Handle}); ExitCode(e) != 2 {
			t.Fatal(verb, e)
		}
	}
	close(r.block)
}
func TestIngestHeadMismatchRc3(t *testing.T) {
	r := newRuntime()
	r.headMismatch = true
	s := seatFixture(t, r)
	response, _ := submitFixture(t, s, "wrong-head")
	if got := awaitFixture(t, s, response.Handle); got.ExitCode != 3 {
		t.Fatal(string(got.Receipt))
	}
}
func TestReceiptSchemaOnCancelC1FailTTL(t *testing.T) {
	for _, mode := range []string{"cancel", "C1", "gate-verification", "cleanup", "ttl"} {
		t.Run(mode, func(t *testing.T) {
			r := newRuntime()
			if mode == "cancel" || mode == "ttl" {
				r.block = make(chan struct{})
			}
			if mode == "C1" {
				r.unsafeInspect = true
			}
			if mode == "gate-verification" {
				r.rc = 5
			}
			if mode == "cleanup" {
				r.failCleanup = true
			}
			s := seatFixture(t, r)
			var resp Response
			if mode == "ttl" {
				w, _ := workerbridge.Parse(taskFixture(t, "failure-ttl"))
				obj(w["bridge"])["expires_at_utc"] = time.Now().Add(2 * time.Second).UTC().Format(time.RFC3339)
				var e error
				resp, e = s.Submit(1000, Request{Verb: "submit", WorkerTask: canonical(w), BundleLen: 6, ObservedIntegrationTip: strings.Repeat("a", 40)}, strings.NewReader("bundle"))
				if e != nil {
					t.Fatal(e)
				}
			} else {
				resp, _ = submitFixture(t, s, "failure-"+strings.ToLower(mode))
			}
			if mode == "cancel" {
				deadline := time.Now().Add(time.Second)
				for time.Now().Before(deadline) {
					r.mu.Lock()
					created := r.created
					r.mu.Unlock()
					if created > 0 {
						break
					}
					time.Sleep(time.Millisecond)
				}
				_, e := s.Request(context.Background(), 1000, Request{Verb: "cancel", Handle: resp.Handle})
				if e != nil {
					t.Fatal(e)
				}
			}
			done := awaitFixture(t, s, resp.Handle)
			want := 5
			if mode == "cleanup" {
				want = 6
			}
			if done.ExitCode != want {
				t.Fatal(string(done.Receipt))
			}
		})
	}
}
func TestJanitorReapsOrphanLease(t *testing.T) {
	dir := t.TempDir()
	r := newRuntime()
	r.block = make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	s, e := NewSeat(ctx, configFixture(dir, r))
	if e != nil {
		t.Fatal(e)
	}
	resp, _ := submitFixture(t, s, "restart")
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		r.mu.Lock()
		n := r.created
		r.mu.Unlock()
		if n > 0 {
			break
		}
		time.Sleep(time.Millisecond)
	}
	cancel()
	s.Close()
	r.block = nil
	s2, e := NewSeat(context.Background(), configFixture(dir, r))
	if e != nil {
		t.Fatal(e)
	}
	defer s2.Close()
	done := awaitFixture(t, s2, resp.Handle)
	if done.ExitCode != 6 {
		t.Fatal(string(done.Receipt))
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.created != 1 {
		t.Fatal("restarted capsule")
	}
}
func TestBodyStoreQuotaAndOrphanReap(t *testing.T) {
	dir := t.TempDir()
	_ = os.Chmod(dir, 0700)
	st, e := OpenStore(dir)
	if e != nil {
		t.Fatal(e)
	}
	defer st.Close()
	h := strings.Repeat("a", 64)
	a := &Attempt{Handle: h, Digest: strings.Repeat("b", 64), Task: json.RawMessage(`{}`), Response: Response{Receipt: json.RawMessage(`{}`)}, Created: time.Now().Add(-25 * time.Hour).UnixMilli()}
	if _, _, e = st.Reserve(a); e != nil {
		t.Fatal(e)
	}
	if e = os.MkdirAll(filepath.Join(st.dir, "bodies", h), 0700); e != nil {
		t.Fatal(e)
	}
	lines := 0
	if e = st.Reap(time.Now(), func(string) error { lines++; return nil }); e != nil {
		t.Fatal(e)
	}
	if lines != 1 {
		t.Fatal(lines)
	}
	got, _ := st.Get(h)
	if !got.Acked {
		t.Fatal("not reaped")
	}
	a2 := *a
	a2.Handle = strings.Repeat("c", 64)
	a2.Task = bytes.Repeat([]byte(" "), 64<<20)
	if _, _, e = st.Reserve(&a2); ExitCode(e) != 4 {
		t.Fatal(e)
	}
}

func TestLiveEncodeBeforeCanonRc4(t *testing.T) {
	s := seatFixture(t, newRuntime())
	resp, _ := submitFixture(t, s, "no-canon")
	done := awaitFixture(t, s, resp.Handle)
	if done.BridgeResult != nil || done.Reason != "unavailable" {
		t.Fatal(done)
	}
	task, _ := workerbridge.Parse(taskFixture(t, "no-canon"))
	obj(task["bridge"])["surface"] = workerbridge.OSSurface
	if _, e := workerbridge.EncodeBridgeResult(canonical(task), done.Receipt, "PROMPT synthetic", true); workerbridge.ExitCode(e) != 4 {
		t.Fatal(e)
	}
}

func TestExtractPeakMustRemainMeasuredAndMonotonic(t *testing.T) {
	for _, peak := range []any{nil, "4096", -1, 0, int64(2<<30) + 1, 1.5} {
		t.Run(fmt.Sprint(peak), func(t *testing.T) {
			r := newRuntime()
			r.statusPeak = peak
			s := seatFixture(t, r)
			response, _ := submitFixture(t, s, "bad-peak")
			done := awaitFixture(t, s, response.Handle)
			if done.ExitCode == 0 {
				t.Fatal("invalid measurement admitted", string(done.Receipt))
			}
		})
	}
}

func TestSafeTopNeedsPIDColumnAndIdleBody(t *testing.T) {
	for in, want := range map[string]bool{
		"PID STAT COMMAND\n41 Ss tini\n42 S sleep\n":          true,
		"STAT COMMAND\nSs tini\nS sleep\n":                    false,
		"PID STAT COMMAND\n41 Ss tini\n42 Z sleep\n":          false,
		"PID STAT COMMAND\n41 Ss tini\n42 S sh\n":             false,
		"PID STAT COMMAND\n41 Ss tini\n":                      false,
		"PID STAT COMMAND\nx Ss tini\n42 S sleep\n":           false,
		"PID STAT COMMAND\n41 Ss tini\n42 S sleep\n43 S go\n": false,
	} {
		if got := safeTop([]byte(in)); got != want {
			t.Errorf("%q: %v", in, got)
		}
	}
}

func TestDiagNamesStepFirstCodeAndFailedWitness(t *testing.T) {
	r := newRuntime()
	r.headMismatch = true
	r.failCleanup = true
	var diag syncBuffer
	c := configFixture(t.TempDir(), r)
	c.Diag = &diag
	s, e := NewSeat(context.Background(), c)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	response, _ := submitFixture(t, s, "diag")
	if got := awaitFixture(t, s, response.Handle); got.ExitCode != 6 {
		t.Fatal(string(got.Receipt))
	}
	line := diag.String()
	for _, want := range []string{"step=head", "code=6", "first_code=3", "first_reason=setup", "purge=false"} {
		if !strings.Contains(line, want) {
			t.Fatalf("diag %q lacks %q", line, want)
		}
	}
	if strings.Count(line, "\n") != 1 {
		t.Fatalf("one line per failed attempt: %q", line)
	}
}

type syncBuffer struct {
	mu sync.Mutex
	b  strings.Builder
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}
func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

// dockerd reports the inlined compact profile (measured on 29.6.2), never the path;
// the path form or a different profile is a containment mismatch.
func TestInspectAdmissionSeccompIsTheInlinedProfile(t *testing.T) {
	p := testPolicy()
	for name, opt := range map[string]string{"path": "seccomp=" + p.SeccompFile, "other": `seccomp={"defaultAction":"SCMP_ACT_ALLOW"}`, "inline": "seccomp=" + p.SeccompInline} {
		doc := fakeInspect(p, "abcdef012345", "lease-x")
		obj(doc["HostConfig"])["SecurityOpt"] = []string{"no-new-privileges:true", opt}
		b, _ := json.Marshal([]any{doc})
		_, _, e := inspectAdmission(b, p, "abcdef012345", "lease-x")
		if (e == nil) != (name == "inline") {
			t.Fatalf("%s: %v", name, e)
		}
	}
}

func TestDiagNamesFailingInspectField(t *testing.T) {
	r := newRuntime()
	r.unsafeInspect = true
	var diag syncBuffer
	c := configFixture(t.TempDir(), r)
	c.Diag = &diag
	s, e := NewSeat(context.Background(), c)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(s.Close)
	response, _ := submitFixture(t, s, "diag-inspect")
	if got := awaitFixture(t, s, response.Handle); got.ExitCode != 5 {
		t.Fatal(string(got.Receipt))
	}
	if line := diag.String(); !strings.Contains(line, "step=inspect") || !strings.Contains(line, "effective launch mismatch: ") {
		t.Fatalf("diag %q", line)
	}
}
