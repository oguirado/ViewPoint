package headless

import (
	"bufio"
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sync"
	"syscall"
	"time"
)

//go:embed scoped_runner.py
var scopedRunner string

type scopedProcess struct {
	attempt    *scopedAttempt
	controller *scopedChild
	input      io.WriteCloser
	lines      chan lineResult
	stopRead   chan struct{}
	broker     net.Conn
	brokerDone chan struct{}
	closeOnce  sync.Once
	closeErr   error
	timer      *time.Timer
}

func startScopedProcess(ctx context.Context, a *scopedAttempt, profile *Profile, engine Pin, runtime string) (*scopedProcess, error) {
	fds, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	parent := os.NewFile(uintptr(fds[0]), "scoped-launch-owner")
	child := os.NewFile(uintptr(fds[1]), "scoped-controller-launch-channel")
	defer child.Close()
	broker, err := net.FileConn(parent)
	parent.Close()
	if err != nil {
		return nil, err
	}
	p := &scopedProcess{
		attempt: a, lines: make(chan lineResult, 8), stopRead: make(chan struct{}),
		broker: broker, brokerDone: make(chan struct{}),
	}
	cmd := exec.Command(filepath.Join(a.dir, "python"), "-I", "-B", "-c", scopedRunner, a.dir, profile.PythonPrefix)
	cmd.Env = []string{"PATH=/usr/bin:/bin", "LANG=C"}
	cmd.ExtraFiles = []*os.File{child}
	p.input, err = cmd.StdinPipe()
	if err != nil {
		broker.Close()
		return nil, err
	}
	// The reader owns its pipe even after cmd.Wait, so buffered terminal events
	// survive an immediately exiting controller.
	out, output, err := os.Pipe()
	if err != nil {
		p.input.Close()
		broker.Close()
		return nil, err
	}
	cmd.Stdout = output
	p.controller, err = a.spawn("controller", cmd)
	output.Close()
	if err != nil {
		out.Close()
		p.input.Close()
		broker.Close()
		close(p.brokerDone)
		if p.controller == nil {
			return nil, err
		}
		return p, err
	}
	go scanController(out, p.lines, p.stopRead)
	go p.serveBroker(runtime, engine)
	deadline, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	raw, err := p.Read(deadline)
	if err != nil {
		return p, err
	}
	var ready struct {
		Ready  bool   `json:"mc09_ready"`
		Cgroup string `json:"cgroup"`
	}
	if json.Unmarshal(raw, &ready) != nil || !ready.Ready || ready.Cgroup != a.leaf.Path {
		return p, errors.New("scoped controller readiness unproved")
	}
	if err := a.note("controller_ready"); err != nil {
		return p, err
	}
	p.timer = time.AfterFunc(180*time.Second, func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = p.Close(ctx)
	})
	return p, nil
}

type scopedBrokerRequest struct {
	Op        string `json:"op"`
	Directory string `json:"directory"`
	TimeoutMS int    `json:"timeout_ms"`
	ChildPID  int    `json:"child_pid"`
}

type scopedBrokerReply struct {
	PID      int           `json:"pid,omitempty"`
	ExitCode *int          `json:"exit_code"`
	Cgroup   *scopedLimits `json:"cgroup,omitempty"`
	Error    string        `json:"error,omitempty"`
}

func (p *scopedProcess) serveBroker(runtime string, engine Pin) {
	defer close(p.brokerDone)
	defer p.broker.Close()
	var vm *scopedChild
	reader := bufio.NewScanner(p.broker)
	writer := json.NewEncoder(p.broker)
	for reader.Scan() {
		var request scopedBrokerRequest
		if uniqueJSON(reader.Bytes()) != nil || json.Unmarshal(reader.Bytes(), &request) != nil {
			_ = writer.Encode(scopedBrokerReply{Error: "invalid launch request"})
			return
		}
		var reply scopedBrokerReply
		var err error
		switch request.Op {
		case "check_cgroup":
			reply.Cgroup, err = p.checkCgroup(request.ChildPID)
		case "spawn":
			vm, err = p.spawnVMM(runtime, engine, request.Directory)
			if err == nil {
				reply.PID = vm.pid.PID
			}
		case "poll", "wait":
			var wait time.Duration
			if request.Op == "wait" {
				if request.TimeoutMS < 1 || request.TimeoutMS > 30000 {
					err = errors.New("controller wait outside admitted bounds")
					break
				}
				wait = time.Duration(request.TimeoutMS) * time.Millisecond
			}
			reply.ExitCode, err = scopedExit(vm, wait)
		default:
			err = errors.New("unsupported launch owner operation")
		}
		if err != nil {
			reply.Error = err.Error()
		}
		if writer.Encode(reply) != nil || (err != nil && request.Op == "spawn") {
			return
		}
	}
}

func (p *scopedProcess) checkCgroup(childPID int) (*scopedLimits, error) {
	a := p.attempt
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.sealed {
		return nil, errors.New("scoped cgroup check after seal")
	}
	limits, err := readScopeLimits(a.scope, a.leaf)
	if err != nil {
		return nil, err
	}
	expected := []int{p.controller.pid.PID}
	if childPID != 0 {
		vm := a.child["firecracker"]
		if vm == nil || vm.pid.PID != childPID {
			return nil, fmt.Errorf("%w: requested child differs from recorded VMM", errCustody)
		}
		expected = append(expected, childPID)
		slices.Sort(expected)
	}
	members, err := a.leaf.members()
	if err != nil || !slices.Equal(members, expected) {
		return nil, errors.Join(err, fmt.Errorf("%w: scoped cgroup membership differs from expected controller and child", errCustody))
	}
	if _, err := a.proveMembers(members); err != nil {
		return nil, err
	}
	return &limits, nil
}

func (p *scopedProcess) spawnVMM(runtime string, engine Pin, dir string) (*scopedChild, error) {
	if filepath.Dir(dir) != runtime || privateDir(dir) != nil {
		return nil, errors.New("VMM stage directory outside private runtime root")
	}
	path := filepath.Join(dir, "firecracker")
	staged := Pin{Path: path, SHA256: engine.SHA256}
	if _, err := pinnedExecutable(staged); err != nil {
		return nil, err
	}
	api := filepath.Join(dir, "api.sock")
	if _, err := os.Lstat(api); !errors.Is(err, os.ErrNotExist) {
		return nil, errors.New("VMM API path occupied or unverified")
	}
	p.attempt.mu.Lock()
	p.attempt.pins = append(p.attempt.pins, scopedDigest{Pin: staged})
	p.attempt.mu.Unlock()
	cmd := exec.Command(path, "--api-sock", api)
	cmd.Dir = dir
	cmd.Env = []string{"PATH=/usr/bin:/bin", "LANG=C"}
	return p.attempt.spawn("firecracker", cmd)
}

func scopedExit(vm *scopedChild, wait time.Duration) (*int, error) {
	if vm == nil {
		return nil, errors.New("VMM identity absent")
	}
	if wait == 0 {
		select {
		case <-vm.exited:
			return &vm.status, nil
		default:
			return nil, nil
		}
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-vm.exited:
		return &vm.status, nil
	case <-timer.C:
		return nil, nil
	}
}

func (p *scopedProcess) Send(ctx context.Context, b []byte) error {
	return sendControllerLine(ctx, p.input, b)
}

func (p *scopedProcess) Read(ctx context.Context) ([]byte, error) {
	return readControllerLine(ctx, p.lines)
}

func (p *scopedProcess) Close(ctx context.Context) error {
	p.closeOnce.Do(func() {
		defer p.attempt.release()
		if p.timer != nil {
			p.timer.Stop()
		}
		defer close(p.stopRead)
		p.attempt.mu.Lock()
		p.attempt.sealed = true
		p.attempt.mu.Unlock()
		_ = p.input.Close()
		select {
		case <-p.controller.exited:
		case <-ctx.Done():
		case <-time.After(2 * time.Second):
		}
		p.broker.Close()
		p.closeErr = p.attempt.retire(ctx)
		if p.closeErr != nil {
			p.closeErr = fmt.Errorf("scoped teardown: %w", p.closeErr)
		}
		// A wait in the broker uses the child's exit channel and does not
		// hold the custody lock. The broker cannot spawn after seal.
		select {
		case <-p.brokerDone:
		case <-ctx.Done():
		}
	})
	return p.closeErr
}
