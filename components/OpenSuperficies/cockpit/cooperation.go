//go:build cockpit

package main

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/wailsapp/wails/v2/pkg/runtime"
	"opensuperficies.local/srf/src/orchestratorbook/arrendador"
	"opensuperficies.local/srf/src/orchestratorbook/wal"
)

// CooperationConfig is installed by the native host, never exposed as a binding.
// Operator is the replaceable authority seam. Production v1 allows and journals
// operator verbs (OQ-W1 v2); tests can inject a refusing gate.
// Missing actuator hooks fail closed rather than simulating srfd authority.
type CooperationConfig struct {
	nativeCustody bool
	Operator      func(kind, target string, epoch uint64) bool
	Clip          func(string) error
	SetMode       func(slotID, mode string) error
	Stop          func(slotID string) error
	Revoke        func(slotID string, epoch uint64) error
	Grant         func(slotID, holder string, epoch uint64) error
	PBX           *CooperationPBX
}
type LeaseCredential struct {
	Holder string `json:"holder"`
	Epoch  uint64 `json:"epoch"`
	Token  string `json:"token"`
}
type CopyResult struct {
	Seq   int64  `json:"Seq"`
	Kind  string `json:"Kind"`
	Bytes string `json:"Bytes"`
}
type cooperationOverlay struct {
	Epoch             uint64
	ReadOnly, Revoked bool
}
type cooperationStore struct {
	custody    *custodyStore
	clipboard  string
	launching  string
	mu         sync.Mutex
	operation  sync.Mutex
	config     CooperationConfig
	book       *wal.LibroConsumo
	lessor     *arrendador.SrfArrendador
	sessions   map[string]arrendador.LeaseSession
	selections map[string]uint64
	overlay    map[string]cooperationOverlay
	cap        int
	server     *stateServer
	base       func() (stateInput, error)
}

var cooperationStores = struct {
	sync.Mutex
	items map[*App]*cooperationStore
}{items: map[*App]*cooperationStore{}}

func cooperationFor(a *App) *cooperationStore {
	cooperationStores.Lock()
	defer cooperationStores.Unlock()
	return cooperationStores.items[a]
}
func cooperationID(slot int) string { return fmt.Sprintf("srf-ng-%d", slot) }
func cooperationMissing() error     { return errors.New("AUSENTE: cooperation store") }

// StartCooperation must follow StartState. Native custody is owned by this
// cockpit instance; injected producers retain the narrowing-only overlay.
func StartCooperation(a *App, config CooperationConfig) error {
	cooperationStores.Lock()
	defer cooperationStores.Unlock()
	if cooperationStores.items[a] != nil {
		return errors.New("cooperation already started")
	}
	root := os.Getenv("XDG_RUNTIME_DIR")
	if root == "" || !filepath.IsAbs(root) {
		return cooperationMissing()
	}
	dir := filepath.Join(root, "os-cu")
	if err := os.Mkdir(dir, 0700); err != nil && !os.IsExist(err) {
		return err
	}
	st, err := os.Lstat(dir)
	if err != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
		return errors.New("unsafe cooperation directory")
	}
	path := filepath.Join(dir, "cooperation.wal")
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err == nil {
		err = f.Close()
	} else if os.IsExist(err) {
		st, err = os.Lstat(path)
		if err == nil && (!st.Mode().IsRegular() || st.Mode().Perm() != 0600) {
			err = errors.New("unsafe cooperation WAL")
		}
	}
	if err != nil {
		return err
	}
	book, err := wal.AbrirLibro(path)
	if err != nil {
		return err
	}
	lessor, err := arrendador.NuevoSrfArrendador(nil, book)
	if err != nil {
		_ = book.Cerrar()
		return err
	}
	c := &cooperationStore{config: config, book: book, lessor: lessor, sessions: map[string]arrendador.LeaseSession{}, selections: map[string]uint64{}, overlay: map[string]cooperationOverlay{}}
	if config.nativeCustody {
		c.custody = &custodyStore{slots: map[string]slotCustody{}, journal: c.journalCustody}
	}
	stateServers.Lock()
	defer stateServers.Unlock()
	s := stateServers.items[a]
	if s == nil {
		_ = book.Cerrar()
		return errors.New("AUSENTE: state producer")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	c.server = s
	c.base = s.source
	s.source = c.readInputs
	cooperationStores.items[a] = c
	return nil
}
func StopCooperation(a *App) {
	cooperationStores.Lock()
	c := cooperationStores.items[a]
	delete(cooperationStores.items, a)
	cooperationStores.Unlock()
	if c == nil {
		return
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	stateServers.Lock()
	c.server.mu.Lock()
	c.server.source = c.base
	c.server.mu.Unlock()
	stateServers.Unlock()
	_ = c.book.Cerrar()
}
func (c *cooperationStore) readInputs() (stateInput, error) {
	in, err := c.custodyInputs()
	if err != nil {
		return in, err
	}
	// Own the slice before touching it; source data can be shared with srfd caches.
	in.Capsules = append([]stateCapsuleInput(nil), in.Capsules...)
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.cap != 0 {
		in.Cap = c.cap
	}
	for i := range in.Capsules {
		v := &in.Capsules[i]
		if c.launching == cooperationID(v.Slot) && v.Lifecycle != "stopped" {
			return stateInput{}, errors.New("REFUSED: launch custody pending")
		}
		o, ok := c.overlay[cooperationID(v.Slot)]
		if !ok {
			continue
		}
		if v.LeaseHolder != nil && (o.ReadOnly || o.Revoked) && v.LeaseEpoch >= o.Epoch {
			return stateInput{}, errors.New("cooperation holder conflict")
		}
		if o.Revoked {
			v.LeaseHolder = nil
			if v.Mode == "agent_locked" {
				v.Mode = "read_only"
			}
		}
		if o.ReadOnly {
			if v.LeaseHolder != nil {
				return stateInput{}, errors.New("cooperation holder conflict")
			}
			v.Mode = "read_only"
		}
		v.LeaseEpoch = max(v.LeaseEpoch, o.Epoch)
	}
	return in, nil
}
func (c *cooperationStore) document() (StateDocument, error) {
	var d StateDocument
	b, err := c.server.snapshot()
	if err != nil {
		return d, err
	}
	err = json.Unmarshal(b, &d)
	return d, err
}
func (c *cooperationStore) slot(id string, epoch uint64) (StateCapsule, error) {
	d, err := c.document()
	if err != nil {
		return StateCapsule{}, err
	}
	for _, s := range d.Capsules {
		if s.SlotID == id {
			if s.LeaseEpoch != epoch {
				return s, fmt.Errorf("REFUSED: %s STALE-EPOCH", id)
			}
			return s, nil
		}
	}
	return StateCapsule{}, fmt.Errorf("REFUSED: %s UNKNOWN-SLOT", id)
}
func (c *cooperationStore) operator(kind, target string, epoch uint64) error {
	if c.config.Operator == nil || !c.config.Operator(kind, target, epoch) {
		return fmt.Errorf("REFUSED: %s OPERATOR-AUTHORITY", target)
	}
	return c.journalOperator(kind, target, epoch)
}
func (c *cooperationStore) journalOperator(kind, target string, epoch uint64) error {
	b, err := json.Marshal(struct {
		Kind   string `json:"kind"`
		Target string `json:"target"`
		Epoch  uint64 `json:"epoch"`
	}{kind, target, epoch})
	if err != nil {
		return err
	}
	if _, err = c.book.Escribir(&wal.Registro{Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "operator", Detalles: b}); err != nil {
		return err
	}
	return c.book.Sync()
}
func (c *cooperationStore) journal(kind, target string) error {
	b, _ := json.Marshal(map[string]string{"action": kind, "target": target})
	_, err := c.book.Escribir(&wal.Registro{Clase: wal.ClaseExterna, Tipo: wal.TipoIntencion, Clave: "cooperation", Detalles: b})
	if err != nil {
		return err
	}
	return nil
}
func (c *cooperationStore) authenticate(s StateCapsule, cred *LeaseCredential) error {
	if s.Mode == "operator_controlled" {
		return fmt.Errorf("REFUSED: %s OPERATOR-CONTROL", s.SlotID)
	}
	if cred == nil || s.LeaseHolder == nil || cred.Holder != *s.LeaseHolder {
		return fmt.Errorf("REFUSED: %s LEASE-REQUIRED", s.SlotID)
	}
	if cred.Epoch != s.LeaseEpoch {
		return fmt.Errorf("REFUSED: %s STALE-EPOCH", s.SlotID)
	}
	b, err := hex.DecodeString(cred.Token)
	if err != nil || len(b) != 16 {
		return errors.New("REFUSED: invalid lease credential")
	}
	var token [16]byte
	copy(token[:], b)
	c.mu.Lock()
	session := c.sessions[s.SlotID]
	c.mu.Unlock()
	if !c.lessor.AutenticarToken(s.SlotID, uint32(s.Slot), cred.Epoch, token) || c.lessor.Validar(session) != nil || session.Titular != cred.Holder {
		return fmt.Errorf("REFUSED: %s LEASE-REQUIRED", s.SlotID)
	}
	return nil
}
func (a *App) CopiarSlot(id string, request any) (CopyResult, error) {
	target, err := decodeCopyRequest(request)
	if err != nil {
		return CopyResult{}, err
	}
	kind, cred := target.Kind, target.Credential
	c := cooperationFor(a)
	if c == nil {
		return CopyResult{}, cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	raw := a.BloqueSlot(id)
	if raw == "" {
		return CopyResult{}, errors.New("AUSENTE: canonical slot")
	}
	var s StateCapsule
	if err := json.Unmarshal([]byte(raw), &s); err != nil {
		return CopyResult{}, err
	}
	if s.Mode == "agent_locked" || s.LeaseHolder != nil || cred != nil {
		if err := c.authenticate(s, cred); err != nil {
			return CopyResult{}, err
		}
	}
	text := raw
	switch kind {
	case "uri":
		text = s.URI
	case "bloque":
	default:
		return CopyResult{}, errors.New("REFUSED: copy kind")
	}
	var clip func(string) error
	if c != nil {
		clip = c.config.Clip
	}
	if clip == nil && a.ctx != nil {
		clip = func(v string) error { return runtime.ClipboardSetText(a.ctx, v) }
	}
	if clip == nil && c != nil && a.ctx == nil {
		clip = func(v string) error { c.mu.Lock(); defer c.mu.Unlock(); c.clipboard = v; return nil }
	}
	if clip == nil {
		return CopyResult{}, errors.New("AUSENTE: clipboard writer")
	}
	if err := clip(text); err != nil {
		return CopyResult{}, err
	}
	return CopyResult{Seq: s.Seq, Kind: kind, Bytes: text}, nil
}
func (a *App) CederControl(id string, epoch uint64) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	s, err := c.slot(id, epoch)
	if err != nil {
		return err
	}
	if err = c.operator("cede", id, epoch); err != nil {
		return err
	}
	if s.LeaseHolder != nil {
		return fmt.Errorf("REFUSED: %s AGENT-HELD", id)
	}
	if c.config.SetMode == nil {
		return errors.New("AUSENTE: mode actuator")
	}
	if err = c.config.SetMode(id, "read_only"); err != nil {
		return err
	}
	after, err := c.slot(id, epoch)
	if err != nil {
		return err
	}
	if after.Mode != "read_only" {
		return errors.New("failed: mode not observed")
	}
	if err = c.journal("cede", id); err != nil {
		return err
	}
	c.mu.Lock()
	c.selections[id] = epoch
	c.mu.Unlock()
	return nil
}
func (a *App) SolicitarArriendo(id, holder string, epoch uint64) (LeaseCredential, error) {
	c := cooperationFor(a)
	if c == nil {
		return LeaseCredential{}, cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	s, err := c.slot(id, epoch)
	if err != nil {
		return LeaseCredential{}, err
	}
	if s.Mode == "operator_controlled" {
		return LeaseCredential{}, fmt.Errorf("REFUSED: %s OPERATOR-CONTROL", id)
	}
	if !stateSlugPattern.MatchString(holder) || s.Mode != "read_only" || s.LeaseHolder != nil {
		return LeaseCredential{}, fmt.Errorf("REFUSED: %s LEASE-UNAVAILABLE", id)
	}
	// A prior native-authorized IA selection is required. Neither cold read-only
	// state nor an untokened CederControl grants authority. Consume the selection.
	c.mu.Lock()
	selected, ok := c.selections[id]
	delete(c.selections, id)
	c.mu.Unlock()
	if !ok || selected != epoch {
		return LeaseCredential{}, fmt.Errorf("REFUSED: %s OPERATOR-AUTHORITY", id)
	}
	if c.config.Grant == nil {
		return LeaseCredential{}, errors.New("AUSENTE: srfd grant actuator")
	}
	session, err := c.lessor.Conceder(id, uint32(s.Slot), holder, 30*time.Second, false)
	if err != nil {
		return LeaseCredential{}, err
	}
	if err = c.book.Sync(); err != nil {
		_ = c.lessor.RevocarCapsula(id)
		return LeaseCredential{}, err
	}
	if session.Epoca <= epoch {
		_ = c.lessor.RevocarCapsula(id)
		return LeaseCredential{}, fmt.Errorf("REFUSED: %s STALE-EPOCH", id)
	}
	c.mu.Lock()
	delete(c.overlay, id)
	c.mu.Unlock()
	if err = c.config.Grant(id, holder, session.Epoca); err != nil {
		_ = c.lessor.RevocarCapsula(id)
		return LeaseCredential{}, err
	}
	after, err := c.slot(id, session.Epoca)
	if err != nil || after.LeaseHolder == nil || *after.LeaseHolder != holder || after.Mode != "agent_locked" {
		_ = c.lessor.RevocarCapsula(id)
		return LeaseCredential{}, errors.New("failed: grant not observed")
	}
	c.mu.Lock()
	delete(c.overlay, id)
	c.sessions[id] = session
	c.mu.Unlock()
	return LeaseCredential{Holder: holder, Epoch: session.Epoca, Token: hex.EncodeToString(session.Token[:])}, nil
}
func (c *cooperationStore) revoke(s StateCapsule) error {
	if s.LeaseHolder == nil {
		return nil
	}
	if c.config.Revoke == nil {
		return errors.New("AUSENTE: srfd revoke actuator")
	}
	if err := c.lessor.RevocarCapsula(s.SlotID); err != nil {
		return err
	}
	if err := c.book.Sync(); err != nil {
		return err
	}
	if err := c.config.Revoke(s.SlotID, s.LeaseEpoch); err != nil {
		return err
	}
	// The live producer must acknowledge revocation before the overlay is installed.
	doc, err := c.document()
	if err != nil {
		return err
	}
	var after StateCapsule
	found := false
	for _, v := range doc.Capsules {
		if v.SlotID == s.SlotID {
			after = v
			found = true
			break
		}
	}
	if !found || after.LeaseEpoch <= s.LeaseEpoch {
		return errors.New("failed: revocation epoch not observed")
	}
	if after.LeaseHolder != nil {
		return errors.New("failed: lease remains")
	}
	c.mu.Lock()
	delete(c.sessions, s.SlotID)
	delete(c.selections, s.SlotID)
	c.overlay[s.SlotID] = cooperationOverlay{Epoch: after.LeaseEpoch, Revoked: true}
	c.mu.Unlock()
	return nil
}
func (a *App) TomarControl(id string, epoch uint64) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	s, err := c.slot(id, epoch)
	if err != nil {
		return err
	}
	if err = c.operator("takeover", id, epoch); err != nil {
		return err
	}
	if err = c.revoke(s); err != nil {
		return err
	}
	if c.config.SetMode == nil {
		return errors.New("AUSENTE: mode actuator")
	}
	if err = c.config.SetMode(id, "operator_controlled"); err != nil {
		return err
	}
	c.mu.Lock()
	delete(c.overlay, id)
	c.mu.Unlock()
	d, err := c.document()
	if err != nil {
		return err
	}
	for _, v := range d.Capsules {
		if v.SlotID == id && v.Mode == "operator_controlled" && v.LeaseHolder == nil {
			return c.journal("takeover", id)
		}
	}
	return errors.New("failed: takeover not observed")
}
func (a *App) CerrarSlot(id string, request any) error {
	target, err := decodeSlotAuthority(request)
	if err != nil {
		return err
	}
	epoch, cred := target.Epoch, target.Credential
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	s, err := c.slot(id, epoch)
	if err != nil {
		return err
	}
	if s.Mode == "agent_locked" || s.LeaseHolder != nil || cred != nil {
		if err = c.authenticate(s, cred); err != nil {
			return err
		}
	} else if s.Mode != "operator_controlled" {
		return fmt.Errorf("REFUSED: %s OPERATOR-CONTROL required", id)
	}
	if err = c.operator("close", id, epoch); err != nil {
		return err
	}
	if c.config.Stop == nil {
		return errors.New("AUSENTE: stop actuator")
	}
	if err = stopRecordingForAbort(a, id); err != nil {
		return err
	}
	if err = c.revoke(s); err != nil {
		return err
	}
	if err = c.config.Stop(id); err != nil {
		return err
	}
	d, err := c.document()
	if err != nil {
		return err
	}
	for _, v := range d.Capsules {
		if v.SlotID == id && v.Lifecycle != "stopped" {
			return errors.New("failed: close not observed")
		}
	}
	return c.journal("close", id)
}

// AbortarTodo is intentionally callable by everyone. It never stops at the first
// failing slot; all failures are named and success requires a producer barrier.
func (a *App) AbortarTodo() error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	// Emergency authority is open even when a future operator gate refuses.
	_ = c.operator("abort", "cockpit", 0)
	d, err := c.document()
	if err != nil {
		return err
	}
	failures := []string{}
	for _, s := range d.Capsules {
		if err = c.revoke(s); err != nil {
			failures = append(failures, s.SlotID+": "+err.Error())
		}
		if c.config.Stop == nil {
			failures = append(failures, s.SlotID+": AUSENTE: stop actuator")
		} else if err = c.config.Stop(s.SlotID); err != nil {
			failures = append(failures, s.SlotID+": "+err.Error())
		}
	}
	after, err := c.document()
	if err != nil {
		failures = append(failures, "cockpit: "+err.Error())
	} else {
		for _, s := range after.Capsules {
			if s.LeaseHolder != nil || s.Lifecycle != "stopped" {
				failures = append(failures, s.SlotID+": abort not observed")
			}
		}
	}
	if len(failures) > 0 {
		return errors.New(strings.Join(failures, "; "))
	}
	return c.journal("emergency-abort", "cockpit")
}

// Layout capacity is cockpit-wide; v2 permits scaled walls in the main window.
func (a *App) FijarDisposicion(ws int, layout string, epoch uint64, cred *LeaseCredential) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	c.operation.Lock()
	defer c.operation.Unlock()
	d, err := c.document()
	if err != nil {
		return err
	}
	if layout != "1x1" && layout != "2x2" && (layout != "1x2" || d.WindowMode != "column_1_3_dual") {
		return errors.New("REFUSED: layout needs legal canvas")
	}
	found, count := false, 0
	var primary *StateCapsule
	for i := range d.Capsules {
		s := &d.Capsules[i]
		if s.Workspace == ws {
			found = true
			count++
			// Epochs are per-slot: several held slots can share the same epoch.
			// Match the actual capability rather than choosing the last same-epoch slot.
			if cred != nil && primary == nil && s.LeaseHolder != nil && *s.LeaseHolder == cred.Holder && s.LeaseEpoch == epoch && c.authenticate(*s, cred) == nil {
				primary = s
			}
		}
	}
	if cred != nil {
		if primary == nil {
			return errors.New("REFUSED: layout LEASE-REQUIRED")
		}
	}
	// The cap is global: validate every affected capsule, including other workspaces.
	for _, s := range d.Capsules {
		if s.Mode == "operator_controlled" && s.LeaseHolder == nil {
			continue
		}
		if cred == nil {
			return fmt.Errorf("REFUSED: %s LEASE-REQUIRED", s.SlotID)
		}
		if s.Mode != "agent_locked" || s.LeaseHolder == nil || *s.LeaseHolder != cred.Holder {
			return fmt.Errorf("REFUSED: %s LEASE_HELD", s.SlotID)
		}
		c.mu.Lock()
		session, ok := c.sessions[s.SlotID]
		valid := ok && session.Titular == cred.Holder && session.Epoca == s.LeaseEpoch && c.lessor.Validar(session) == nil
		c.mu.Unlock()
		if !valid {
			return fmt.Errorf("REFUSED: %s LEASE-REQUIRED", s.SlotID)
		}
	}
	if cred == nil {
		// Bind operator layout authority to the canonical document sequence, not per-slot epochs.
		if d.Seq < 0 {
			return errors.New("REFUSED: invalid state sequence")
		}
		if err = c.operator("layout", fmt.Sprintf("ws-%d", ws), uint64(d.Seq)); err != nil {
			return err
		}
	}
	if !found {
		return errors.New("REFUSED: workspace unavailable")
	}
	if layout == "2x2" && len(d.Capsules) < 4 {
		return errors.New("REFUSED: 2x2 needs four capsules")
	}
	if layout == "1x2" && count < 2 {
		return errors.New("REFUSED: 1x2 needs two capsules")
	}
	c.mu.Lock()
	previousCap := c.cap
	if layout == "1x1" {
		c.cap = 1
	} else if layout == "2x2" {
		c.cap = 4
	} else {
		c.cap = 2
	}
	c.mu.Unlock()
	if _, err = c.document(); err != nil {
		c.mu.Lock()
		c.cap = previousCap
		c.mu.Unlock()
		return err
	}
	return c.journal("layout", strconv.Itoa(ws))
}

// AuthorizeCooperationOperator is a host-only helper for WIRE's legacy bindings.
// It is deliberately not an App method and therefore not Wails-bound.
func AuthorizeCooperationOperator(a *App, kind, target string, epoch uint64) error {
	c := cooperationFor(a)
	if c == nil {
		return cooperationMissing()
	}
	return c.operator(kind, target, epoch)
}
