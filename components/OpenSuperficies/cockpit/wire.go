//go:build cockpit

package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"time"
)

// AppOption configures native seams. Options are package functions, never bindings.
type AppOption func(*App)

func WithOperatorGate(gate func(kind, target string, epoch uint64) bool) AppOption {
	return func(a *App) { a.operatorGate = gate }
}
func nativeCooperationConfig(a *App) CooperationConfig {
	gate := a.operatorGate
	if gate == nil {
		gate = func(string, string, uint64) bool { return true }
	}
	return CooperationConfig{nativeCustody: true, Operator: gate,
		SetMode: func(id, mode string) error { return nativeCustodyUpdate(a, id, mode, "", 0) },
		Grant: func(id, holder string, epoch uint64) error {
			return nativeCustodyUpdate(a, id, "agent_locked", holder, epoch)
		},
		Revoke: func(id string, epoch uint64) error { return nativeCustodyUpdate(a, id, "revoke", "", epoch) },
		Stop: func(id string) error {
			if err := stopRecordingForAbort(a, id); err != nil {
				return err
			}
			slot, err := wireSlot(id)
			if err != nil {
				return err
			}
			response, err := sendSrfd("detener", "", slot)
			if err != nil {
				return err
			}
			if !response.OK {
				return errors.New("REFUSED: srfd stop")
			}
			return nil
		}}
}
func wireSlot(id string) (uint16, error) {
	if !strings.HasPrefix(id, "srf-ng-") {
		return 0, errors.New("REFUSED: invalid slot")
	}
	n, err := strconv.ParseUint(strings.TrimPrefix(id, "srf-ng-"), 10, 16)
	if err != nil || cooperationID(int(n)) != id {
		return 0, errors.New("REFUSED: invalid slot")
	}
	return uint16(n), nil
}

// Fixed-arity union DTOs preserve legacy read/operator calls and carry slot
// credentials without adding a binding. Wails invokes methods with reflect.Call.
type CopyRequest struct {
	Kind       string           `json:"kind"`
	Credential *LeaseCredential `json:"credential"`
}
type SlotAuthority struct {
	Epoch      uint64           `json:"epoch"`
	Credential *LeaseCredential `json:"credential"`
}
type LaunchRequest struct {
	Slot   uint16 `json:"slot"`
	Holder string `json:"holder"`
}

func wireRequestBytes(request any) ([]byte, error) {
	b, err := json.Marshal(request)
	if err != nil || bytes.Equal(b, []byte("null")) {
		return nil, errors.New("REFUSED: missing binding request")
	}
	return b, nil
}
func decodeWireObject(b []byte, key string, target any) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(b, &fields); err != nil || fields[key] == nil || bytes.Equal(fields[key], []byte("null")) {
		return errors.New("REFUSED: missing binding field")
	}
	decoder := json.NewDecoder(bytes.NewReader(b))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return errors.New("REFUSED: invalid binding request")
	}
	return nil
}
func decodeCopyRequest(request any) (CopyRequest, error) {
	if kind, ok := request.(string); ok {
		return CopyRequest{Kind: kind}, nil
	}
	var target CopyRequest
	b, err := wireRequestBytes(request)
	if err != nil {
		return target, err
	}
	err = decodeWireObject(b, "kind", &target)
	return target, err
}
func decodeSlotAuthority(request any) (SlotAuthority, error) {
	var target SlotAuthority
	b, err := wireRequestBytes(request)
	if err != nil {
		return target, err
	}
	if len(b) > 0 && b[0] == '{' {
		err = decodeWireObject(b, "epoch", &target)
	} else {
		err = json.Unmarshal(b, &target.Epoch)
	}
	if err != nil {
		return target, errors.New("REFUSED: invalid slot authority")
	}
	return target, nil
}
func decodeLaunchRequest(request any) (LaunchRequest, error) {
	var target LaunchRequest
	b, err := wireRequestBytes(request)
	if err != nil {
		return target, err
	}
	if len(b) > 0 && b[0] == '{' {
		err = decodeWireObject(b, "slot", &target)
	} else {
		err = json.Unmarshal(b, &target.Slot)
	}
	if err != nil {
		return target, errors.New("REFUSED: invalid launch request")
	}
	return target, nil
}

type srfdSender func(string, string, uint16) (*RespuestaSrfd, error)

func launchGuard(a *App, clase string, slot uint16, holder string, send srfdSender) (*RespuestaSrfd, error) {
	if !stateSlugPattern.MatchString(holder) {
		return nil, errors.New("REFUSED: launch requires holder")
	}
	c := cooperationFor(a)
	if c == nil {
		return nil, cooperationMissing()
	}
	if !c.operation.TryLock() {
		return nil, errors.New("REFUSED: capsule operation in flight")
	}
	defer c.operation.Unlock()
	if c.config.Grant == nil || c.config.SetMode == nil || c.config.Revoke == nil || c.config.Stop == nil {
		return nil, errors.New("AUSENTE: srfd launch custody actuators")
	}
	// Inspect the producer directly: a first launch has no canonical capsule yet.
	in, err := c.readInputs()
	if err != nil {
		return nil, err
	}
	id := cooperationID(int(slot))
	for _, s := range in.Capsules {
		if s.Slot == int(slot) && (s.Lifecycle != "stopped" || s.LeaseHolder != nil) {
			return nil, fmt.Errorf("REFUSED: %s SLOT-OCCUPIED", id)
		}
	}
	if err = c.journal("start-capsule", id); err != nil {
		return nil, err
	}
	if err = c.book.Sync(); err != nil {
		return nil, err
	}
	c.mu.Lock()
	c.launching = id
	c.mu.Unlock()
	clearPending := func() { c.mu.Lock(); c.launching = ""; c.mu.Unlock() }
	response, err := send("iniciar", clase, slot)
	// A transport failure can follow a successful start. Stop before clearing the
	// pending barrier; retain it if cleanup cannot be confirmed.
	if err != nil || response == nil {
		stopErr := c.config.Stop(id)
		if stopErr == nil {
			clearPending()
		}
		if err == nil {
			err = errors.New("REFUSED: empty srfd launch response")
		}
		return nil, errors.Join(err, stopErr)
	}
	if !response.OK {
		clearPending()
		return response, nil
	}
	// Grant before publishing success. If custody cannot be observed, stop the new slot.
	var grantedEpoch uint64
	fail := func(cause error) (*RespuestaSrfd, error) {
		revokeErr := c.lessor.RevocarCapsula(id)
		var nativeRevokeErr error
		if grantedEpoch != 0 {
			nativeRevokeErr = c.config.Revoke(id, grantedEpoch)
		}
		stopErr := c.config.Stop(id)
		if stopErr == nil {
			clearPending()
		}
		return nil, errors.Join(cause, revokeErr, nativeRevokeErr, stopErr)
	}
	if err = c.config.SetMode(id, "read_only"); err != nil {
		return fail(err)
	}
	session, err := c.lessor.Conceder(id, uint32(slot), holder, 30*time.Second, false)
	if err != nil {
		return fail(err)
	}
	if err = c.book.Sync(); err != nil {
		return fail(err)
	}
	grantedEpoch = session.Epoca
	if err = c.config.Grant(id, holder, session.Epoca); err != nil {
		return fail(err)
	}
	observedInputs, err := c.custodyInputs()
	observed := false
	if err == nil {
		for _, v := range observedInputs.Capsules {
			if v.Slot == int(slot) && v.LeaseEpoch == session.Epoca && v.Mode == "agent_locked" && v.LeaseHolder != nil && *v.LeaseHolder == holder {
				observed = true
			}
		}
	}
	if err != nil || !observed {
		return fail(errors.New("failed: launch custody not observed"))
	}
	c.mu.Lock()
	c.sessions[id] = session
	c.launching = ""
	delete(c.overlay, id)
	c.mu.Unlock()
	response.Lease = &LeaseCredential{Holder: holder, Epoch: session.Epoca, Token: hex.EncodeToString(session.Token[:])}
	return response, nil
}

// D11's probe digests the actual stored RAM frame, never an independent screenshot.
type CaptureProvenance struct {
	Source, Codec, Store string
	XID                  uint32
	Swap, Disk           bool
}
type FrameDigest struct {
	Seq          uint64
	W, H, Stride int
	Format       string
	SHA256       [32]byte
}
type D11Capture interface {
	RamCapture
	Provenance(context.Context, string) (CaptureProvenance, error)
	Probe(context.Context, string, string, uint32) (FrameDigest, error)
}
type DirectFramebuffer interface {
	Read(context.Context, string, uint32) (FrameDigest, error)
}

// FrozenFramebuffer brackets admission and subsequent verification with a server
// grab (or the static calibration fixture). Read alone cannot establish a freeze.
type FrozenFramebuffer interface {
	DirectFramebuffer
	Freeze(context.Context, string, uint32) (release func() error, err error)
}
type admittedSession struct {
	xid   uint32
	frame FrameDigest
}
type admittedCapture struct {
	mu       sync.Mutex
	d        D11Capture
	fb       FrozenFramebuffer
	display  string
	sessions map[string]admittedSession
}

func nilCapture(value any) bool {
	if value == nil {
		return true
	}
	v := reflect.ValueOf(value)
	switch v.Kind() {
	case reflect.Pointer, reflect.Interface, reflect.Func, reflect.Map, reflect.Slice, reflect.Chan:
		return v.IsNil()
	}
	return false
}
func admitRamCapture(d D11Capture, fb DirectFramebuffer, display string) RamCapture {
	frozen, ok := fb.(FrozenFramebuffer)
	if nilCapture(d) || !ok || nilCapture(frozen) || display == "" {
		return nil
	}
	return &admittedCapture{d: d, fb: frozen, display: display, sessions: map[string]admittedSession{}}
}
func (a *admittedCapture) reap(ctx context.Context, id string) error {
	// Cleanup still runs if the original operation timed out.
	cleanup, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
	defer cancel()
	delete(a.sessions, id)
	return a.d.Reap(cleanup, id)
}
func d11Refusal(reason string) error { return errors.New("REFUSED: D11 " + reason) }
func (a *admittedCapture) check(ctx context.Context, id string, xid uint32) (FrameDigest, error) {
	p, err := a.d.Provenance(ctx, id)
	if err != nil {
		return FrameDigest{}, d11Refusal("provenance unavailable")
	}
	if p.Source != "x11-framebuffer" || (p.Codec != "raw" && p.Codec != "none") || p.Store != "vadapt.ramws" || p.Swap || p.Disk || p.XID == 0 || (xid != 0 && xid != p.XID) {
		return FrameDigest{}, d11Refusal("provenance")
	}
	held, err := a.d.Probe(ctx, id, a.display, p.XID)
	if err != nil {
		return held, d11Refusal("stored frame unavailable")
	}
	direct, err := a.fb.Read(ctx, a.display, p.XID)
	if err != nil {
		return held, d11Refusal("framebuffer unavailable")
	}
	if held.W <= 0 || held.H <= 0 || held.W > int(^uint(0)>>1)/4 || held.Stride < 4*held.W || (held.Format != "BGRA" && held.Format != "RGBA") || held.SHA256 == ([32]byte{}) || held != direct {
		return held, d11Refusal("stored frame mismatch")
	}
	return held, nil
}
func (a *admittedCapture) Start(ctx context.Context, slot int, xid uint32, ttl int64) (id string, err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	release, err := a.fb.Freeze(ctx, a.display, xid)
	if err != nil || release == nil {
		return "", d11Refusal("frame not frozen")
	}
	defer func() {
		if e := release(); e != nil {
			err = errors.Join(err, d11Refusal("frame unfreeze"))
			if id != "" {
				err = errors.Join(err, a.reap(ctx, id))
				id = ""
			}
		}
	}()
	id, err = a.d.Start(ctx, slot, xid, ttl)
	if err != nil || id == "" {
		cleanup := a.reap(ctx, id)
		return "", errors.Join(d11Refusal("start"), cleanup)
	}
	p, err := a.d.Provenance(ctx, id)
	if err != nil || p.XID == 0 || (xid != 0 && xid != p.XID) {
		cleanup := a.reap(ctx, id)
		return "", errors.Join(d11Refusal("provenance unavailable"), cleanup)
	}
	frame, err := a.check(ctx, id, p.XID)
	if err != nil {
		cleanup := a.reap(ctx, id)
		return "", errors.Join(err, cleanup)
	}
	if _, exists := a.sessions[id]; exists {
		cleanup := a.reap(ctx, id)
		return "", errors.Join(d11Refusal("duplicate session"), cleanup)
	}
	a.sessions[id] = admittedSession{xid: p.XID, frame: frame}
	return id, nil
}
func (a *admittedCapture) verify(ctx context.Context, id string) (release func() error, err error) {
	session, ok := a.sessions[id]
	if !ok {
		return nil, d11Refusal("unknown session")
	}
	release, err = a.fb.Freeze(ctx, a.display, session.xid)
	if err != nil || release == nil {
		return nil, errors.Join(d11Refusal("frame not frozen"), a.reap(ctx, id))
	}
	frame, err := a.check(ctx, id, session.xid)
	if err == nil && (frame.Seq < session.frame.Seq || (frame.Seq == session.frame.Seq && frame != session.frame)) {
		err = d11Refusal("stored sequence regressed")
	}
	if err != nil {
		err = errors.Join(err, release(), a.reap(ctx, id))
		return nil, err
	}
	a.sessions[id] = admittedSession{xid: session.xid, frame: frame}
	return release, nil
}
func (a *admittedCapture) SizeBytes(ctx context.Context, id string) (size int64, err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	release, err := a.verify(ctx, id)
	if err != nil {
		return 0, err
	}
	defer func() {
		if e := release(); e != nil {
			err = errors.Join(err, d11Refusal("frame unfreeze"), a.reap(ctx, id))
			size = 0
		}
	}()
	size, err = a.d.SizeBytes(ctx, id)
	if err == nil && size >= 0 {
		var frame FrameDigest
		session := a.sessions[id]
		frame, err = a.check(ctx, id, session.xid)
		if err == nil && (frame.Seq < session.frame.Seq || (frame.Seq == session.frame.Seq && frame != session.frame)) {
			err = d11Refusal("stored sequence changed identity")
		}
		if err == nil {
			a.sessions[id] = admittedSession{xid: session.xid, frame: frame}
		}
	}
	if err != nil || size < 0 {
		err = errors.Join(d11Refusal("size or stored frame"), a.reap(ctx, id))
		size = 0
	}
	return size, err
}
func (a *admittedCapture) Stop(ctx context.Context, id string) (err error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	release, err := a.verify(ctx, id)
	if err != nil {
		return err
	}
	defer func() {
		if e := release(); e != nil {
			err = errors.Join(err, d11Refusal("frame unfreeze"), a.reap(ctx, id))
		}
	}()
	if err = a.d.Stop(ctx, id); err != nil {
		return errors.Join(d11Refusal("stop"), a.reap(ctx, id))
	}
	// A backend may downgrade while sealing. Check the stored frame again.
	session := a.sessions[id]
	frame, err := a.check(ctx, id, session.xid)
	if err == nil && (frame.Seq < session.frame.Seq || (frame.Seq == session.frame.Seq && frame != session.frame)) {
		err = d11Refusal("stored sequence changed identity")
	}
	if err != nil {
		return errors.Join(err, a.reap(ctx, id))
	}
	a.sessions[id] = admittedSession{xid: session.xid, frame: frame}
	return nil
}
func (a *admittedCapture) Reap(ctx context.Context, id string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.reap(ctx, id)
}
