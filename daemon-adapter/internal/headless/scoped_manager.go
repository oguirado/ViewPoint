package headless

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"time"
)

type scopedRefusal struct {
	error
}

func reconcileScoped(profile *Profile) error {
	config := *profile.ScopedLaunch
	path := filepath.Join(profile.StateRoot, "attempt-"+config.RunID)
	if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
		if err != nil {
			return err
		}
		_, err = openScopedAttempt(config, profile.StateRoot, nil)
		return err
	}
	return nil
}

func (m *Manager) launchScoped(ctx context.Context) (process, error) {
	p := m.profile
	var config struct {
		Artifacts struct {
			Engine Pin `json:"engine"`
			Kernel Pin `json:"kernel"`
			Init   Pin `json:"init"`
		} `json:"artifacts"`
	}
	if err := json.Unmarshal(p.ConfigTemplate, &config); err != nil {
		return nil, err
	}
	pins := []Pin{p.Python, p.Runner, config.Artifacts.Engine, config.Artifacts.Kernel, config.Artifacts.Init}
	for _, file := range p.Files {
		pins = append(pins, Pin{file.Path, file.SHA256})
	}
	pins = append(pins, p.ScopedLaunch.ObserverPins...)
	a, err := openScopedAttempt(*p.ScopedLaunch, p.StateRoot, pins)
	if err != nil {
		return nil, err
	}
	m.scoped = a
	refuse := func(cause error) (process, error) {
		defer a.release()
		if err := a.refuse(cause); err != nil {
			return nil, errors.Join(cause, err)
		}
		return nil, &scopedRefusal{cause}
	}
	if err := a.preflight(); err != nil {
		return refuse(err)
	}
	if _, _, err := m.prepare(); err != nil {
		return refuse(err)
	}
	// The actual launch consumes private snapshots. Pin those alongside the
	// source artefacts and the observer inputs.
	for _, pin := range []Pin{
		{filepath.Join(a.dir, "python"), p.Python.SHA256},
		{filepath.Join(a.dir, "runner.py"), p.Runner.SHA256},
	} {
		a.pins = append(a.pins, scopedDigest{Pin: pin})
	}
	for _, file := range p.Files {
		a.pins = append(a.pins, scopedDigest{Pin: Pin{filepath.Join(a.dir, "bundle", file.Relative), file.SHA256}})
	}
	launchPath := filepath.Join(a.dir, "launch.json")
	raw, err := readRegular(launchPath, true, 128<<10)
	if err != nil {
		return refuse(err)
	}
	sum := sha256.Sum256(raw)
	a.pins = append(a.pins, scopedDigest{Pin: Pin{launchPath, hex.EncodeToString(sum[:])}})
	var spec struct {
		Runtime string `json:"runtime_root"`
	}
	if err := json.Unmarshal(raw, &spec); err != nil {
		return refuse(err)
	}
	child, err := startScopedProcess(ctx, a, p, config.Artifacts.Engine, spec.Runtime)
	if err == nil {
		return child, nil
	}
	if child == nil {
		return refuse(err)
	}
	closeCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return nil, errors.Join(err, child.Close(closeCtx))
}

func (a *scopedAttempt) ready() error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.sealed || len(a.roster.PIDs) != len(a.roster.Expected) {
		return errCustody
	}
	members, err := a.leaf.members()
	if err != nil || len(members) != len(a.roster.Expected) {
		return errors.Join(errCustody, err)
	}
	if _, err := a.proveMembers(members); err != nil {
		return err
	}
	if err := a.checkPins(true); err != nil {
		return err
	}
	return a.stage("guest_ready")
}
