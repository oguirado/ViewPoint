package w8seat

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"io"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
	"opensuperficies.local/srf/src/orchestratorbook/gobernador"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/w8seat/patchpolicy"
	"opensuperficies.local/srf/src/workerbridge"
)

type Config struct {
	ReadPeaks func(string) (int64, int64, error)
	StateDir  string
	Policy    LaunchPolicy
	BaseImage map[string]any
	CacheSHA  string
	Runner    w8builder.Runner
	Telemetry gobernador.SrfTelemetria
	// CheckProfile and CgroupPath are test seams. Production uses secure host reads.
	CheckProfile func(LaunchPolicy) error
	CgroupPath   func(int) (string, error)
	// Diag receives one line per failed attempt (step and codes only, never task
	// data); production writes it to the unit's journal. Nil discards.
	Diag io.Writer
}
type pending struct {
	started       time.Time
	attempt       *Attempt
	task          map[string]any
	bundle, patch []byte
	ctx           context.Context
	cancel        context.CancelFunc
	lease         arrendador.LeaseSession
}
type active struct {
	finalizing bool
	lease      arrendador.LeaseSession
	cancel     context.CancelFunc
	cgroup     string
}
type Seat struct {
	cfg          Config
	store        *Store
	wal          *wal.LibroConsumo
	lessor       *arrendador.SrfArrendador
	governor     *gobernador.SrfGobernador
	mu           sync.Mutex
	active       map[string]*active
	queue        chan *pending
	ctx          context.Context
	cancel       context.CancelFunc
	wg           sync.WaitGroup
	payloadBytes int64
}

func Handle(taskID, attemptID, leaseID string) string {
	return hash(canonical([]any{taskID, attemptID, leaseID}))
}
func NewSeat(ctx context.Context, c Config) (*Seat, error) {
	if c.Runner == nil || len(c.Policy.CPUs.Worker) < 2 || c.Policy.CPUs.Interactive == nil || !hex64.MatchString(c.CacheSHA) {
		return nil, refusal(4, "unavailable", "seat preconditions absent")
	}
	if e := contract(c.BaseImage, "base_image"); e != nil || c.BaseImage["digest"] != c.Policy.ImageSHA {
		return nil, refusal(4, "unavailable", "base image record absent or differs")
	}
	if c.CheckProfile == nil {
		c.CheckProfile = seccompPin
	}
	if c.ReadPeaks == nil {
		c.ReadPeaks = readPeaks
	}
	if c.CgroupPath == nil {
		c.CgroupPath = cgroupMemory
	}
	if e := c.CheckProfile(c.Policy); e != nil {
		return nil, e
	}
	st, e := OpenStore(c.StateDir)
	if e != nil {
		return nil, e
	}
	book, e := wal.AbrirLibro(filepath.Join(c.StateDir, "seat.wal"))
	if e != nil {
		st.Close()
		return nil, e
	}
	lessor, e := arrendador.NuevoSrfArrendador(nil, book)
	if e != nil {
		book.Cerrar()
		st.Close()
		return nil, e
	}
	ctx, cancel := context.WithCancel(ctx)
	s := &Seat{cfg: c, store: st, wal: book, lessor: lessor, active: map[string]*active{}, queue: make(chan *pending, 64), ctx: ctx, cancel: cancel}
	if c.Telemetry == nil {
		c.Telemetry = gobernador.NuevaTelemetriaProc(func() []string {
			s.mu.Lock()
			defer s.mu.Unlock()
			var paths []string
			for _, a := range s.active {
				if a.cgroup != "" {
					paths = append(paths, a.cgroup)
				}
			}
			return paths
		})
	}
	s.governor = gobernador.NuevoGobernador(c.Telemetry)
	s.governor.MaxCapsulas = len(c.Policy.CPUs.Worker) / 2
	// Reconcile durable uncertain rows before accepting any request.
	if e = s.Janitor(ctx, true); e != nil {
		s.cancel()
		book.Cerrar()
		st.Close()
		return nil, e
	}
	s.wg.Add(2)
	go s.schedule()
	go s.janitorLoop()
	return s, nil
}
func (s *Seat) Close() { s.cancel(); s.wg.Wait(); _ = s.wal.Cerrar(); s.store.Close() }
func (s *Seat) Submit(uid uint32, req Request, streams io.Reader) (Response, error) {
	if uid == 0 {
		return Response{}, refusal(7, "operator_capability", "root peer refused")
	}
	if req.Verb != "submit" || req.Handle != "" || req.BundleLen <= 0 || req.BundleLen > MaxBundle || req.PatchLen < 0 || req.PatchLen > MaxPatch || !hex40.MatchString(req.ObservedIntegrationTip) {
		return Response{}, refusal(2, "contract", "submit shape")
	}
	task, e := workerbridge.Parse(req.WorkerTask)
	if e != nil {
		return Response{}, refusal(2, "contract", "worker task JSON")
	}
	if _, e = workerbridge.WorkerToBridge(req.WorkerTask); e != nil {
		return Response{}, refusal(2, "contract", "worker/bridge binding")
	}
	network := obj(obj(task["policy"])["network"])
	if network != nil && network["mode"] != "none" {
		return Response{}, refusal(4, "unavailable", "Phase-2 seat supports network none only")
	}
	bridge := obj(task["bridge"])
	expiry, e := time.Parse(time.RFC3339, str(bridge["expires_at_utc"]))
	if e != nil {
		return Response{}, refusal(2, "contract", "expired dispatched lease")
	}

	size := req.BundleLen + req.PatchLen
	s.mu.Lock()
	if s.payloadBytes+size > 256<<20 {
		s.mu.Unlock()
		return Response{}, refusal(4, "unavailable", "seat RAM stream budget")
	}
	s.payloadBytes += size
	s.mu.Unlock()
	release := true
	defer func() {
		if release {
			s.mu.Lock()
			s.payloadBytes -= size
			s.mu.Unlock()
		}
	}()
	bundle, e := readStream(streams, req.BundleLen, MaxBundle)
	if e != nil {
		return Response{}, e
	}
	patch, e := readStream(streams, req.PatchLen, MaxPatch)
	if e != nil {
		return Response{}, e
	}
	if len(patch) > 0 {
		if _, e := patchpolicy.Check(patch, strs(task["write_paths"])); e != nil {
			return Response{}, refusal(2, "contract", "input patch refused")
		}
	}
	handle := Handle(str(task["task_id"]), str(task["attempt_id"]), str(task["lease_id"]))
	digest := hash(canonical([]any{task, req.ObservedIntegrationTip, hash(bundle), hash(patch)}))
	now := time.Now()
	ttl := 15 * time.Minute
	if requested := number(obj(task["policy"])["ttl_ms"]); requested > 0 && requested < 900000 {
		ttl = time.Duration(requested) * time.Millisecond
	}
	end := now.Add(ttl)
	if expiry.Before(end) {
		end = expiry
	}
	a := &Attempt{Handle: handle, Digest: digest, UID: uid, Task: canonical(task), Tip: req.ObservedIntegrationTip, Created: now.UnixMilli(), Expires: end.UnixMilli(), State: "QUEUED", Response: Response{Handle: handle, BridgeResult: nil}}
	// Serialize reserve + lease + enqueue so a duplicate cannot race the first grant.
	s.mu.Lock()
	defer s.mu.Unlock()
	if previous, err := s.store.Get(handle); err == nil {
		if previous.UID != uid || previous.Digest != digest {
			return Response{}, refusal(2, "contract", "attempt digest/owner differs")
		}
		return s.response(previous), nil
	}
	if time.Until(expiry) < time.Second {
		return Response{}, refusal(2, "contract", "expired dispatched lease")
	}
	old, fresh, e := s.store.Reserve(a)
	if e != nil {
		return Response{}, e
	}
	if !fresh {
		return s.response(old), nil
	}
	if len(s.queue) >= cap(s.queue) || len(s.active) >= s.governor.MaxCapsulas+64 {
		_ = s.store.Update(handle, func(x *Attempt) {
			x.State = "REFUSED"
			x.Response = Response{ExitCode: 4, Reason: "unavailable", Handle: handle, Ready: true, BridgeResult: nil}
		})
		return Response{}, refusal(4, "unavailable", "FIFO full")
	}
	for _, row := range s.store.All() {
		if row.Handle != handle && len(row.Response.Receipt) == 0 && row.State != "REFUSED" {
			m, _ := workerbridge.Parse(row.Task)
			if m["lease_id"] == task["lease_id"] {
				return Response{}, refusal(2, "contract", "lease already belongs to another attempt")
			}
		}
	}
	lease, e := s.lessor.Conceder(str(task["lease_id"]), 0, strconv.FormatUint(uint64(uid), 10), time.Until(end), false)
	if e != nil {
		return Response{}, refusal(6, "indeterminate", "durable lease grant failed")
	}
	b := make([]byte, 6)
	if _, e = rand.Read(b); e != nil {
		return Response{}, refusal(6, "indeterminate", "container ID unavailable")
	}
	a.Container = hex.EncodeToString(b)
	a.Epoch = lease.Epoca
	a.Grant, e = s.grant(a, task)
	if e != nil {
		return Response{}, refusal(6, "indeterminate", "grant validation failed: "+e.Error())
	}
	if e = s.store.Update(handle, func(x *Attempt) { x.Container = a.Container; x.Epoch = lease.Epoca; x.Grant = a.Grant }); e != nil {
		return Response{}, e
	}
	jobCtx, cancel := context.WithDeadline(s.ctx, end)
	job := &pending{started: now, attempt: a, task: task, bundle: bundle, patch: patch, ctx: jobCtx, cancel: cancel, lease: lease}
	s.active[handle] = &active{lease: lease, cancel: cancel}
	s.queue <- job
	release = false
	return Response{Handle: handle, Ready: false, BridgeResult: nil}, nil
}
func (s *Seat) response(a *Attempt) Response {
	r := a.Response
	r.Handle = a.Handle
	if len(r.Receipt) > 0 {
		r.Ready = true
		r.Reason = "unavailable"
	}
	return r
}
func (s *Seat) Request(ctx context.Context, uid uint32, req Request) (Response, error) {
	if req.Verb == "health" {
		if req.Handle != "" || len(req.WorkerTask) > 0 || req.BundleLen != 0 || req.PatchLen != 0 {
			return Response{}, refusal(2, "contract", "health shape")
		}
		return Response{Ready: true, BridgeResult: nil}, nil
	}
	if !hex64.MatchString(req.Handle) || len(req.WorkerTask) > 0 || req.BundleLen != 0 || req.PatchLen != 0 || req.ObservedIntegrationTip != "" {
		return Response{}, refusal(2, "contract", "verb shape")
	}
	switch req.Verb {
	case "wait", "receipt", "ack", "cancel":
	default:
		return Response{}, refusal(2, "contract", "unknown verb")
	}
	a, e := s.store.Get(req.Handle)
	if e != nil {
		return Response{}, e
	}
	if a.UID != uid {
		return Response{}, refusal(7, "operator_capability", "attempt owned by another peer")
	}
	s.mu.Lock()
	live := s.active[req.Handle]
	s.mu.Unlock()
	if live != nil && len(a.Response.Receipt) == 0 {
		if e = s.lessor.Validar(live.lease); e != nil {
			s.mu.Lock()
			current := s.active[req.Handle]
			finalizing := current != nil && current.finalizing
			s.mu.Unlock()
			latest, getErr := s.store.Get(req.Handle)
			if getErr == nil && len(latest.Response.Receipt) > 0 {
				a = latest
			} else if !(finalizing && (req.Verb == "wait" || req.Verb == "receipt")) {
				return Response{}, refusal(2, "contract", "lease expired or fenced")
			}
		}
	}
	if req.Verb == "cancel" {
		if live != nil {
			live.cancel()
		}
		return s.response(a), nil
	}
	if req.Verb == "ack" {
		if len(a.Response.Receipt) == 0 {
			return Response{}, refusal(2, "contract", "ack before receipt")
		}
		if e = s.store.Ack(a.Handle); e != nil {
			return Response{}, e
		}
		return s.response(a), nil
	}
	if req.Verb == "wait" && len(a.Response.Receipt) == 0 {
		deadline := time.NewTimer(25 * time.Second)
		defer deadline.Stop()
		tick := time.NewTicker(25 * time.Millisecond)
		defer tick.Stop()
		for {
			select {
			case <-ctx.Done():
				return s.response(a), nil
			case <-deadline.C:
				return s.response(a), nil
			case <-tick.C:
				a, e = s.store.Get(req.Handle)
				if e != nil {
					return Response{}, e
				}
				if len(a.Response.Receipt) > 0 {
					return s.response(a), nil
				}
			}
		}
	}
	return s.response(a), nil
}
func (s *Seat) schedule() {
	defer s.wg.Done()
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-s.ctx.Done():
			for {
				select {
				case j := <-s.queue:
					s.finishUnstarted(j, 5, "cancelled")
				default:
					return
				}
			}
		case j := <-s.queue:
			for {
				if j.ctx.Err() != nil {
					s.finishUnstarted(j, 5, s.cancelReason(j))
					break
				}
				d := s.governor.Admitir(gobernador.SrfSolicitudAdmision{Clase: "terminal", Reserva: 4 << 30, CPUs: 2, CandadoOK: true})
				if d.Admitida {
					s.wg.Add(1)
					go func() { defer s.wg.Done(); s.execute(j, d) }()
					break
				}
				select {
				case <-j.ctx.Done():
				case <-tick.C:
				}
			}
		}
	}
}
func (s *Seat) cancelReason(j *pending) string {
	if j.ctx.Err() == context.DeadlineExceeded || time.Now().UnixMilli() >= j.attempt.Expires {
		return "timeout"
	}
	return "cancelled"
}
func (s *Seat) release(j *pending) {
	j.cancel()
	s.mu.Lock()
	delete(s.active, j.attempt.Handle)
	s.payloadBytes -= int64(len(j.bundle) + len(j.patch))
	s.mu.Unlock()
}
func (s *Seat) finishUnstarted(j *pending, code int, reason string) {
	defer s.release(j)
	s.markFinalizing(j.attempt.Handle)
	if e := s.lessor.RevocarCapsula(str(j.task["lease_id"])); e != nil {
		code = 6
		reason = "indeterminate"
	}
	if s.ctx.Err() != nil {
		return
	}
	s.finish(j, code, reason, nil, nil, nil, map[string]any{"status": "not_applicable", "remaining_pids": nil, "remaining_workspace_bytes": nil, "resources_released": nil}, nil)
}
func (s *Seat) journal(ctx context.Context, line string) error {
	return s.cfg.Runner.Run(ctx, []string{"systemd-cat", "-t", "w8-capsule"}, bytes.NewBufferString(line), io.Discard)
}
func (s *Seat) janitorLoop() {
	defer s.wg.Done()
	tick := time.NewTicker(30 * time.Second)
	defer tick.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case <-tick.C:
			ctx, cancel := context.WithTimeout(s.ctx, 20*time.Second)
			_ = s.Janitor(ctx, false)
			cancel()
		}
	}
}
func (s *Seat) GovernorState() (gobernador.SrfEstadoGobernador, error) { return s.governor.Estado() }

func (s *Seat) markFinalizing(handle string) {
	s.mu.Lock()
	if a := s.active[handle]; a != nil {
		a.finalizing = true
	}
	s.mu.Unlock()
	_ = s.store.Update(handle, func(a *Attempt) { a.State = "STOPPING" })
}
