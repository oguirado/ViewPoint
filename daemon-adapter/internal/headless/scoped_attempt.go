package headless

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"syscall"
	"time"
)

// ScopedLaunch is host configuration. Neither scope authority nor command pins
// come from the headless request. RunID also identifies the one-use launch attempt.
type ScopedLaunch struct {
	RunID         string `json:"run_id"`
	RunScope      string `json:"run_scope"`
	AttemptLeaf   string `json:"attempt_leaf"`
	AuthorityRoot string `json:"authority_root"`
	LeasePath     string `json:"lease_path"`
	StopPath      string `json:"stop_path"`
	ObserverPins  []Pin  `json:"observer_pins"`
}

type scopedRoster struct {
	AttemptID string         `json:"attempt_id"`
	BootID    string         `json:"boot_id"`
	Leaf      cgroupIdentity `json:"cgroup"`
	Expected  []string       `json:"expected_roster"`
	PIDs      []scopedPID    `json:"pids"`
}

type scopedWitness struct {
	AttemptID string         `json:"attempt_id"`
	BootID    string         `json:"boot_id"`
	Leaf      cgroupIdentity `json:"cgroup"`
	Status    string         `json:"status"`
	Code      string         `json:"code,omitempty"`
	Detail    string         `json:"detail,omitempty"`
	PIDs      []scopedPID    `json:"pids"`
	Observed  []scopedPID    `json:"observed_unclaimed,omitempty"`
	Members   []int          `json:"members"`
	Signalled []int          `json:"signalled"`
	Frozen    bool           `json:"frozen"`
	Empty     bool           `json:"cgroup_empty"`
}

type scopedDigest struct {
	Pin
	Before  string `json:"before_launch,omitempty"`
	Recheck string `json:"last_pre_spawn,omitempty"`
	After   string `json:"after_launch,omitempty"`
	Error   string `json:"error,omitempty"`
}

type scopedChild struct {
	pid    scopedPID
	handle *os.File
	exited chan struct{}
	status int
}

type scopedAttempt struct {
	mu       sync.Mutex
	config   ScopedLaunch
	dir      string
	lock     *os.File
	scope    *scopedGroup
	leaf     *scopedGroup
	roster   scopedRoster
	seq      uint64
	clock    int64
	pins     []scopedDigest
	child    map[string]*scopedChild
	sealed   bool
	stopping bool
	closed   bool
}

var errCustody = errors.New("CUSTODY_UNPROVED")
var errOccupancy = errors.New("OCCUPANCY_CONFLICT")
var errMaestro = errors.New("MAESTRO_UNPROVED")
var errPin = errors.New("ARTIFACT_PIN_MISMATCH")

func openScopedAttempt(config ScopedLaunch, stateRoot string, pins []Pin) (*scopedAttempt, error) {
	if !regexpInvocation.MatchString(config.RunID) {
		return nil, errors.New("unique attempt ID required")
	}
	if err := privateDir(stateRoot); err != nil {
		return nil, err
	}
	lock, err := os.Open(stateRoot)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, fmt.Errorf("attempt owner lock: %w", err)
	}
	a := &scopedAttempt{
		config: config,
		dir:    filepath.Join(stateRoot, "attempt-"+config.RunID),
		lock:   lock,
		child:  make(map[string]*scopedChild),
	}
	if err := a.openGroups(); err != nil {
		a.release()
		return nil, err
	}
	if err := os.Mkdir(a.dir, 0700); err != nil {
		if errors.Is(err, os.ErrExist) {
			err = errors.Join(err, a.recover())
		}
		a.release()
		return nil, err
	}
	if err := lock.Sync(); err != nil {
		a.release()
		return nil, err
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		a.release()
		return nil, err
	}
	a.roster = scopedRoster{
		AttemptID: config.RunID, BootID: strings.TrimSpace(string(boot)), Leaf: a.leaf.cgroupIdentity,
		Expected: []string{"controller", "firecracker"}, PIDs: []scopedPID{},
	}
	for _, pin := range pins {
		a.pins = append(a.pins, scopedDigest{Pin: pin})
	}
	if err := writeNew(filepath.Join(a.dir, "pids.json"), a.roster); err != nil {
		a.release()
		return nil, err
	}
	if err := a.stage("create_request"); err != nil {
		a.release()
		return nil, err
	}
	return a, nil
}

func (a *scopedAttempt) openGroups() error {
	var err error
	a.scope, err = openScopedGroup(a.config.RunScope)
	if err != nil {
		return err
	}
	a.leaf, err = openScopedGroup(a.config.AttemptLeaf)
	return err
}

func (a *scopedAttempt) release() {
	for _, child := range a.child {
		if child.handle != nil {
			child.handle.Close()
		}
	}
	if a.leaf != nil {
		a.leaf.close()
	}
	if a.scope != nil {
		a.scope.close()
	}
	a.lock.Close()
}

func (a *scopedAttempt) note(name string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.sealed {
		return errors.New("attempt already sealed")
	}
	if name == "stop" {
		return a.stopStage()
	}
	return a.stage(name)
}

func (a *scopedAttempt) stopStage() error {
	if a.stopping {
		return nil
	}
	if err := a.stage("stop"); err != nil {
		return err
	}
	a.stopping = true
	return nil
}

func (a *scopedAttempt) stage(name string) error {
	ns, err := monotonicNS()
	if err != nil {
		return err
	}
	if a.roster.BootID == "" || ns <= a.clock {
		return errors.New("non-increasing monotonic stage clock")
	}
	f, err := os.OpenFile(filepath.Join(a.dir, "stages.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return err
	}
	row := struct {
		Stage       string `json:"stage"`
		AttemptID   string `json:"attempt_id"`
		BootID      string `json:"boot_id"`
		Sequence    uint64 `json:"sequence"`
		MonotonicNS int64  `json:"monotonic_ns"`
	}{name, a.roster.AttemptID, a.roster.BootID, a.seq + 1, ns}
	err = json.NewEncoder(f).Encode(row)
	if err == nil {
		err = f.Sync()
	}
	if err = errors.Join(err, f.Close()); err != nil {
		return err
	}
	a.seq++
	a.clock = ns
	return syncScopedDir(a.dir)
}

func replaceScopedJSON(dir, name string, value any) error {
	f, err := os.CreateTemp(dir, "."+name+"-")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	err = json.NewEncoder(f).Encode(value)
	if err == nil {
		err = f.Sync()
	}
	if err = errors.Join(err, f.Close()); err != nil {
		return err
	}
	if err = os.Rename(f.Name(), filepath.Join(dir, name)); err != nil {
		return err
	}
	return syncScopedDir(dir)
}

func syncScopedDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

func (a *scopedAttempt) maestro() error {
	if err := privateDir(a.config.AuthorityRoot); err != nil {
		return errors.Join(errMaestro, err)
	}
	history, err := readRegular(filepath.Join(a.config.AuthorityRoot, "ANNOUNCE.jsonl"), true, 64<<10)
	if err != nil {
		return errors.Join(errMaestro, err)
	}
	first, _, found := bytes.Cut(history, []byte{'\n'})
	var start struct {
		RunID  string `json:"run_id"`
		Event  string `json:"event"`
		BootID string `json:"boot_id"`
		Relay  string `json:"relay"`
		Detail struct {
			Scope string `json:"scope"`
		} `json:"detail"`
	}
	if !found || uniqueJSON(first) != nil || json.Unmarshal(first, &start) != nil ||
		start.RunID != a.config.RunID || start.Event != "start" || start.Relay != "Maestro" ||
		start.BootID != a.roster.BootID || start.Detail.Scope != filepath.Base(a.config.RunScope) {
		return fmt.Errorf("%w: first announcement does not bind this attempt and boot", errMaestro)
	}
	raw, err := readRegular(filepath.Join(a.config.AuthorityRoot, "MAESTRO_NOTIFIED.json"), true, 64<<10)
	if err != nil {
		return errors.Join(errMaestro, err)
	}
	var ack struct {
		RunID string `json:"run_id"`
		Start string `json:"start_sha256"`
	}
	sum := sha256.Sum256(history[:len(first)+1])
	if uniqueJSON(raw) != nil || json.Unmarshal(raw, &ack) != nil ||
		ack.RunID != a.config.RunID || ack.Start != hex.EncodeToString(sum[:]) {
		return fmt.Errorf("%w: acknowledgement differs from the attempt announcement", errMaestro)
	}
	return nil
}

func (a *scopedAttempt) window() error {
	stop, err := readRegular(a.config.StopPath, false, 4096)
	if err != nil || strings.TrimSpace(string(stop)) != "LIBRE" {
		return errors.New("stop signal active or unverified")
	}
	memory, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return err
	}
	var available uint64
	for _, line := range strings.Split(string(memory), "\n") {
		if rest, ok := strings.CutPrefix(line, "MemAvailable:"); ok {
			if _, err := fmt.Sscanf(rest, "%d kB", &available); err != nil {
				return err
			}
			break
		}
	}
	load, err := os.ReadFile("/proc/loadavg")
	if err != nil {
		return err
	}
	var load1, load5 float64
	if _, err := fmt.Sscan(string(load), &load1, &load5); err != nil {
		return err
	}
	if available < (20+8)*1024*1024 || !(load5 >= 0 && load5 <= 24) {
		return fmt.Errorf("numeric headroom or load refused: MemAvailable=%d KiB load5=%g", available, load5)
	}
	if filepath.Base(a.config.LeasePath) != "ISOLATED_CORES.lease" || canonical(filepath.Dir(a.config.LeasePath)) != nil {
		return fmt.Errorf("%w: lease path unverified", errOccupancy)
	}
	if _, err = os.Lstat(a.config.LeasePath); !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("%w: lease occupied or unverified", errOccupancy)
	}
	return nil
}

func (a *scopedAttempt) checkPins(after bool) error {
	var failed error
	for i := range a.pins {
		pin := &a.pins[i]
		raw, err := readRegular(pin.Path, false, 256<<20)
		if err == nil {
			sum := sha256.Sum256(raw)
			digest := hex.EncodeToString(sum[:])
			switch {
			case after:
				pin.After = digest
			case pin.Before == "":
				pin.Before = digest
			default:
				pin.Recheck = digest
			}
			if digest != pin.SHA256 {
				err = errors.New("SHA-256 differs")
			}
		}
		if err != nil {
			pin.Error = err.Error()
			failed = errors.Join(failed, fmt.Errorf("%w: %s: %v", errPin, pin.Path, err))
		}
	}
	if len(a.pins) == 0 {
		failed = fmt.Errorf("%w: no source pins supplied", errPin)
	}
	return errors.Join(failed, replaceScopedJSON(a.dir, "digests.json", a.pins))
}

func (a *scopedAttempt) preflight() error {
	if err := verifyScope(a.scope, a.leaf); err != nil {
		return err
	}
	members, err := a.leaf.members()
	if err != nil || len(members) != 0 {
		return errors.Join(errCustody, err)
	}
	return errors.Join(a.checkPins(false), a.window(), a.maestro())
}

func (a *scopedAttempt) spawn(role string, cmd *exec.Cmd) (*scopedChild, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.sealed || a.child[role] != nil || !slices.Contains(a.roster.Expected, role) {
		return nil, errors.New("spawn role unavailable")
	}
	if err := errors.Join(verifyScope(a.scope, a.leaf), a.window(), a.maestro()); err != nil {
		return nil, err
	}
	members, err := a.leaf.members()
	if err != nil {
		return nil, err
	}
	if _, err := a.proveMembers(members); err != nil {
		return nil, err
	}
	if err := a.stage(role + "_spawn_intent"); err != nil {
		return nil, err
	}
	if err := a.checkPins(false); err != nil {
		return nil, err
	}
	rawPIDFD := -1
	cmd.SysProcAttr = &syscall.SysProcAttr{UseCgroupFD: true, CgroupFD: int(a.leaf.fd.Fd()), PidFD: &rawPIDFD}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	child := &scopedChild{exited: make(chan struct{})}
	if rawPIDFD >= 0 {
		child.handle = os.NewFile(uintptr(rawPIDFD), role+"-pidfd")
	}
	a.child[role] = child
	child.pid, err = a.leaf.identity(role, cmd.Process.Pid)
	go func() {
		_ = cmd.Wait()
		child.status = cmd.ProcessState.ExitCode()
		close(child.exited)
	}()
	if err != nil || child.handle == nil {
		return child, errors.Join(errCustody, err)
	}
	a.roster.PIDs = append(a.roster.PIDs, child.pid)
	if err := replaceScopedJSON(a.dir, "pids.json", a.roster); err != nil {
		// A handle without a durable roster must never authorize escalation.
		a.roster.PIDs = a.roster.PIDs[:len(a.roster.PIDs)-1]
		return child, err
	}
	if err := a.stage(role + "_spawned"); err != nil {
		return child, err
	}
	return child, a.checkPins(true)
}

func (a *scopedAttempt) proveMembers(members []int) ([]*scopedChild, error) {
	known := make([]*scopedChild, 0, len(members))
	for _, pid := range members {
		index := slices.IndexFunc(a.roster.PIDs, func(record scopedPID) bool { return record.PID == pid })
		if index < 0 {
			return nil, fmt.Errorf("%w: unrecorded PID %d", errCustody, pid)
		}
		record := a.roster.PIDs[index]
		child := a.child[record.Role]
		current, err := a.leaf.identity(record.Role, pid)
		if err != nil || current != record || child == nil || child.pid != record || signalScopedPID(child.handle, 0) != nil {
			return nil, fmt.Errorf("%w: PID %d identity or retained pidfd differs", errCustody, pid)
		}
		known = append(known, child)
	}
	for _, record := range a.roster.PIDs {
		if slices.Contains(members, record.PID) {
			continue
		}
		child := a.child[record.Role]
		if child == nil || !errors.Is(signalScopedPID(child.handle, 0), syscall.ESRCH) {
			return nil, fmt.Errorf("%w: recorded PID %d absent from leaf with no exit proof", errCustody, record.PID)
		}
	}
	return known, nil
}

func (a *scopedAttempt) witness(status string) scopedWitness {
	return scopedWitness{
		AttemptID: a.roster.AttemptID, BootID: a.roster.BootID, Leaf: a.roster.Leaf, Status: status,
		PIDs: a.roster.PIDs, Members: []int{}, Signalled: []int{},
	}
}

func (a *scopedAttempt) publish(w scopedWitness) error {
	if err := replaceScopedJSON(a.dir, "teardown.json", w); err != nil {
		return err
	}
	return replaceScopedJSON(a.dir, "close-witness.json", w)
}

func (a *scopedAttempt) refuse(cause error) error {
	a.sealed = true
	w := a.witness("never_started")
	w.Code = refusalCode(cause)
	w.Detail = cause.Error()
	members, err := a.leaf.members()
	if err != nil || len(members) != 0 || len(a.child) != 0 {
		w.Status = "quarantine"
		w.Code = errCustody.Error()
	} else {
		w.Empty = true
	}
	if members != nil {
		w.Members = members
	}
	if !w.Empty {
		err = errors.Join(err, errCustody)
	}
	return errors.Join(err, a.stage(w.Status), a.publish(w))
}

func refusalCode(cause error) string {
	for _, code := range []error{errCustody, errOccupancy, errMaestro, errPin} {
		if errors.Is(cause, code) {
			return code.Error()
		}
	}
	return "PREFLIGHT_REFUSED"
}

func (a *scopedAttempt) retire(ctx context.Context) (err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closed {
		return nil
	}
	a.sealed = true
	w := a.witness("PASS")
	defer func() {
		if err != nil {
			w.Status = "quarantine"
			w.Code = errCustody.Error()
			w.Detail = err.Error()
		}
		err = errors.Join(err, a.publish(w))
		a.closed = err == nil
	}()
	if err := a.stopStage(); err != nil {
		return err
	}
	if err := a.leaf.verifyPath(); err != nil {
		return err
	}
	members, err := a.leaf.members()
	if err != nil {
		return err
	}
	w.Members = members
	if len(members) > 0 {
		if err := a.leaf.writeFreeze("1"); err != nil {
			return err
		}
		if err := a.leaf.waitEvent(ctx, "frozen", "1"); err != nil {
			return err
		}
		w.Frozen = true
		w.Members, err = a.leaf.members()
		if err != nil {
			return err
		}
		known, err := a.proveMembers(w.Members)
		if err != nil {
			return err
		}
		confirmation, err := a.leaf.members()
		if err != nil || !slices.Equal(confirmation, w.Members) {
			return fmt.Errorf("%w: frozen membership changed", errCustody)
		}
		before := w
		before.Status = "custody_proved"
		if err := writeNew(filepath.Join(a.dir, "pre-signal-witness.json"), before); err != nil {
			return err
		}
		// The scope owner must exclude external migration. Check again after
		// syncing the witness so changes during that write also refuse.
		current, err := a.leaf.members()
		if err != nil || !slices.Equal(current, w.Members) {
			w.Members = current
			return fmt.Errorf("%w: membership changed after pre-signal witness", errCustody)
		}
		if err := a.leaf.waitEvent(ctx, "frozen", "1"); err != nil {
			return errors.Join(errCustody, err)
		}
		for _, child := range known {
			err := signalScopedPID(child.handle, syscall.SIGKILL)
			if err != nil && !errors.Is(err, syscall.ESRCH) {
				return err
			}
			if err == nil {
				w.Signalled = append(w.Signalled, child.pid.PID)
			}
		}
		if err := a.leaf.writeFreeze("0"); err != nil {
			return err
		}
	}
	if err := a.leaf.waitEvent(ctx, "populated", "0"); err != nil {
		return err
	}
	for _, child := range a.child {
		select {
		case <-child.exited:
			if child.handle == nil || !errors.Is(signalScopedPID(child.handle, 0), syscall.ESRCH) {
				return fmt.Errorf("%w: PID closure unproved", errCustody)
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	w.Empty = true
	return a.stage("exit")
}

func (a *scopedAttempt) recover() error {
	if err := privateDir(a.dir); err != nil {
		return err
	}
	previous := filepath.Join(a.dir, "close-witness.json")
	if _, err := os.Lstat(previous); err == nil {
		return errors.New("attempt already has a close witness; no replay")
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	raw, err := readRegular(filepath.Join(a.dir, "pids.json"), true, 128<<10)
	if err != nil || uniqueJSON(raw) != nil || json.Unmarshal(raw, &a.roster) != nil ||
		a.roster.AttemptID != a.config.RunID || a.roster.Leaf != a.leaf.cgroupIdentity {
		return errors.Join(errCustody, err, errors.New("recovery journal or cgroup identity unproved"))
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil || a.roster.BootID != strings.TrimSpace(string(boot)) {
		return errors.Join(errCustody, err, errors.New("recovery journal boot unproved"))
	}
	if err := verifyScope(a.scope, a.leaf); err != nil {
		return errors.Join(errCustody, err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	w := a.witness("quarantine")
	w.Code = errCustody.Error()
	w.Detail = "writer exited; retained pidfds unavailable"
	err = a.leaf.writeFreeze("1")
	if err == nil {
		err = a.leaf.waitEvent(ctx, "frozen", "1")
	}
	if err == nil {
		w.Frozen = true
		w.Members, err = a.leaf.members()
		w.Empty = err == nil && len(w.Members) == 0
		for _, pid := range w.Members {
			if observed, readErr := a.leaf.identity("unclaimed", pid); readErr == nil {
				w.Observed = append(w.Observed, observed)
			}
		}
	}
	return errors.Join(errCustody, err, a.publish(w))
}

func monotonicNS() (int64, error) {
	var timestamp syscall.Timespec
	if err := readMonotonicClock(&timestamp); err != nil {
		return 0, err
	}
	return timestamp.Nano(), nil
}
