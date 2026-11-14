//go:build cockpit

package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/surfacebook/manager"
)

const stateSchema = "os.cu.cockpit-state/v2"
const stateMaxInt = int64(9007199254740991)

var stateHostPattern = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
var stateSlugPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`)

type StateRect struct {
	H int `json:"h"`
	W int `json:"w"`
	X int `json:"x"`
	Y int `json:"y"`
}
type StateMetric struct {
	Source    string `json:"source"`
	Timestamp int64  `json:"ts_mono_ms"`
	Value     int64  `json:"value"`
}
type StateTelemetry struct {
	CPU    *StateMetric `json:"cpu_permille"`
	Memory *StateMetric `json:"mem_bytes"`
}
type StateRecording struct {
	Phase string  `json:"phase"`
	Store *string `json:"store"`
	TTL   *int64  `json:"ttl_ms"`
}
type StateCapsule struct {
	Archetype   string         `json:"archetype"`
	CDP         *int           `json:"cdp"`
	Geometry    StateRect      `json:"geometry"`
	Health      string         `json:"health"`
	LeaseEpoch  uint64         `json:"lease_epoch"`
	LeaseHolder *string        `json:"lease_holder"`
	Lifecycle   string         `json:"lifecycle"`
	Mode        string         `json:"mode"`
	Ready       bool           `json:"ready"`
	Recording   StateRecording `json:"rec_state"`
	Seq         int64          `json:"seq"`
	Slot        int            `json:"slot"`
	SlotID      string         `json:"slot_id"`
	Telemetry   StateTelemetry `json:"telemetry"`
	URI         string         `json:"uri"`
	Workspace   int            `json:"workspace"`
	Xpra        int            `json:"xpra_port"`
}

// Tree nodes are a closed union. MarshalJSON never emits the other variant's fields.
type StateTree struct {
	SlotID      string
	ScaleQ16    int
	Direction   string
	Ratio       int
	Left, Right *StateTree
}

func (t StateTree) MarshalJSON() ([]byte, error) {
	if t.SlotID != "" {
		return json.Marshal(map[string]any{"slot_id": t.SlotID, "type": "leaf", "scale_q16": t.ScaleQ16})
	}
	return json.Marshal(map[string]any{"direction": t.Direction, "left": t.Left, "ratio_q16": t.Ratio, "right": t.Right, "type": "split"})
}
func (t *StateTree) UnmarshalJSON(b []byte) error {
	var o map[string]json.RawMessage
	if err := json.Unmarshal(b, &o); err != nil {
		return err
	}
	var typ string
	if err := json.Unmarshal(o["type"], &typ); err != nil {
		return err
	}
	*t = StateTree{}
	if typ == "leaf" {
		if len(o) != 3 {
			return errors.New("leaf fields")
		}
		if err := json.Unmarshal(o["scale_q16"], &t.ScaleQ16); err != nil {
			return err
		}
		return json.Unmarshal(o["slot_id"], &t.SlotID)
	}
	if typ != "split" || len(o) != 5 {
		return errors.New("split fields")
	}
	for _, f := range []struct {
		k string
		v any
	}{{"direction", &t.Direction}, {"ratio_q16", &t.Ratio}, {"left", &t.Left}, {"right", &t.Right}} {
		if err := json.Unmarshal(o[f.k], f.v); err != nil {
			return err
		}
	}
	return nil
}

type StateWorkspace struct {
	Layout string     `json:"layout"`
	N      int        `json:"n"`
	Rect   StateRect  `json:"rect"`
	Tree   *StateTree `json:"tree"`
}
type StateDocument struct {
	Capsules   []StateCapsule   `json:"capsules"`
	Host       string           `json:"host"`
	Schema     string           `json:"schema"`
	Seq        int64            `json:"seq"`
	Timestamp  int64            `json:"ts_mono_ms"`
	WindowMode string           `json:"window_mode"`
	Workspaces []StateWorkspace `json:"workspaces"`
}
type stateCapsuleInput struct {
	Slot                               int
	Archetype, Lifecycle, Health, Mode string
	LeaseEpoch                         uint64
	LeaseHolder                        *string
	Recording                          StateRecording
	Telemetry                          StateTelemetry
}
type stateInput struct {
	Host, WindowMode string
	Cap              int
	Seq, Timestamp   int64
	Capsules         []stateCapsuleInput
}

// Presentation-only Q16 aspect fit of the immutable capsule framebuffer.
func stateScaleQ16(r StateRect) int { return min(65536, r.W*65536/1280, r.H*65536/900) }

func stateURI(host string, workspace, slot int) string {
	return fmt.Sprintf("os://%s/ws/%d/slot/srf-ng-%d", host, workspace, slot)
}
func buildStateSnapshot(in stateInput) (StateDocument, error) {
	d := StateDocument{Capsules: []StateCapsule{}, Host: in.Host, Schema: stateSchema, Seq: in.Seq, Timestamp: in.Timestamp, WindowMode: in.WindowMode, Workspaces: []StateWorkspace{}}
	if in.Cap < 1 || in.Cap > 80 {
		return d, errors.New("invalid workspace cap")
	}
	input := slices.Clone(in.Capsules)
	slices.SortFunc(input, func(a, b stateCapsuleInput) int { return a.Slot - b.Slot })
	// Pack complete walls first; retain the existing single/dual geometry for remainders.
	placements := make(map[int]StateRect)
	workspaces := make(map[int]int)
	for first := 0; first < len(input); {
		count := 1
		if in.Cap >= 4 && len(input)-first >= 4 {
			count = 4
		} else if in.WindowMode == "column_1_3_dual" && in.Cap >= 2 && len(input)-first >= 2 {
			count = 2
		}
		ws := len(d.Workspaces) + 1
		height := 1024
		if in.WindowMode == "column_1_3_tall" {
			height = 2080
		}
		layout := "1x1"
		rect := StateRect{W: 1264, H: height}
		if count == 2 {
			layout = "1x2"
			rect.H = 2048
		}
		if count == 4 {
			layout = "2x2"
			rect.H = 1024
		}
		leaves := make([]*StateTree, count)
		for local := 0; local < count; local++ {
			r := StateRect{W: 1264, H: height, Y: local * height}
			if count == 4 {
				r = StateRect{W: 632, H: 512, X: (local % 2) * 632, Y: (local / 2) * 512}
			}
			c := input[first+local]
			placements[c.Slot] = r
			workspaces[c.Slot] = ws
			leaves[local] = &StateTree{SlotID: fmt.Sprintf("srf-ng-%d", c.Slot), ScaleQ16: stateScaleQ16(r)}
		}
		tree := leaves[0]
		if count == 2 {
			tree = &StateTree{Direction: "vertical", Ratio: 32768, Left: leaves[0], Right: leaves[1]}
		}
		if count == 4 {
			tree = &StateTree{Direction: "vertical", Ratio: 32768, Left: &StateTree{Direction: "horizontal", Ratio: 32768, Left: leaves[0], Right: leaves[1]}, Right: &StateTree{Direction: "horizontal", Ratio: 32768, Left: leaves[2], Right: leaves[3]}}
		}
		d.Workspaces = append(d.Workspaces, StateWorkspace{Layout: layout, N: ws, Rect: rect, Tree: tree})
		first += count
	}
	for _, c := range input {
		ws := workspaces[c.Slot]
		id := fmt.Sprintf("srf-ng-%d", c.Slot)
		rec := c.Recording
		if rec.Phase == "" {
			rec.Phase = "idle"
		}
		c.Telemetry.CPU = freshStateMetric(c.Telemetry.CPU, in.Timestamp)
		c.Telemetry.Memory = freshStateMetric(c.Telemetry.Memory, in.Timestamp)
		block := StateCapsule{Archetype: c.Archetype, Geometry: placements[c.Slot], Health: c.Health, LeaseEpoch: c.LeaseEpoch, LeaseHolder: c.LeaseHolder, Lifecycle: c.Lifecycle, Mode: c.Mode, Ready: c.Lifecycle == "ready", Recording: rec, Seq: in.Seq, Slot: c.Slot, SlotID: id, Telemetry: c.Telemetry, URI: stateURI(in.Host, ws, c.Slot), Workspace: ws, Xpra: 10020 + c.Slot}
		if c.Archetype == "browser" {
			p := 9220 + c.Slot
			block.CDP = &p
		}
		d.Capsules = append(d.Capsules, block)
	}
	// The frozen contract has no representation for an empty workspace (tree/capsule bijection).
	// Absence of capsules must therefore fail closed, rather than fabricate a slot.
	if err := validateState(d); err != nil {
		return d, err
	}
	return d, nil
}
func freshStateMetric(m *StateMetric, now int64) *StateMetric {
	if m == nil || m.Timestamp < 1 || m.Timestamp > now || now-m.Timestamp > 3000 {
		return nil
	}
	return m
}

// canonicalState is the sole serializer for documents and copied blocks. UseNumber preserves integers.
func canonicalState(v any) ([]byte, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var obj any
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	if err = dec.Decode(&obj); err != nil {
		return nil, err
	}
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	if err = enc.Encode(obj); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(out.Bytes(), []byte{'\n'}), nil
}
func stateEnum(v string, allowed ...string) bool { return slices.Contains(allowed, v) }
func validateState(d StateDocument) error {
	bad := func(s string) error { return errors.New("invalid state: " + s) }
	if d.Schema != stateSchema || !stateHostPattern.MatchString(d.Host) || d.Seq < 0 || d.Seq > stateMaxInt || d.Timestamp < 1 || d.Timestamp > stateMaxInt {
		return bad("root")
	}
	if !stateEnum(d.WindowMode, "tile_1_6", "column_1_3_tall", "column_1_3_dual") || d.Capsules == nil || len(d.Capsules) > 80 || len(d.Workspaces) < 1 || len(d.Workspaces) > 999 {
		return bad("collections/mode")
	}
	type placement struct {
		n    int
		rect StateRect
	}
	geo := map[string]placement{}
	lastWS := 0
	for _, w := range d.Workspaces {
		if w.N <= lastWS || w.N > 999 {
			return bad("workspace order")
		}
		lastWS = w.N
		h := 1024
		arity := 1
		if d.WindowMode == "column_1_3_tall" {
			h = 2080
		}
		if w.Layout == "2x2" {
			h = 1024
			arity = 4
		} else if d.WindowMode == "column_1_3_dual" && w.Layout == "1x2" {
			h = 2048
			arity = 2
		} else if w.Layout != "1x1" {
			return bad("layout")
		}
		if w.Rect != (StateRect{W: 1264, H: h}) {
			return bad("canvas")
		}
		count := 0
		var visit func(*StateTree, StateRect, int) error
		visit = func(t *StateTree, r StateRect, depth int) error {
			if t == nil {
				return bad("missing tree")
			}
			if t.SlotID != "" {
				if t.Left != nil || t.Right != nil || t.Direction != "" || t.Ratio != 0 {
					return bad("leaf")
				}
				if _, dup := geo[t.SlotID]; dup {
					return bad("duplicate tree leaf")
				}
				if r.W < 600 || r.H < 420 || t.ScaleQ16 != stateScaleQ16(r) {
					return bad("useful size")
				}
				geo[t.SlotID] = placement{w.N, r}
				count++
				return nil
			}
			if t.ScaleQ16 != 0 || depth >= 2 || t.Ratio < 4096 || t.Ratio > 61440 || !stateEnum(t.Direction, "vertical", "horizontal") {
				return bad("split")
			}
			a, b := r, r
			if t.Direction == "vertical" {
				a.H = r.H * t.Ratio / 65536
				b.Y += a.H
				b.H -= a.H
			} else {
				a.W = r.W * t.Ratio / 65536
				b.X += a.W
				b.W -= a.W
			}
			if err := visit(t.Left, a, depth+1); err != nil {
				return err
			}
			return visit(t.Right, b, depth+1)
		}
		if err := visit(w.Tree, w.Rect, 0); err != nil {
			return err
		}
		if count != arity {
			return bad("layout arity")
		}
	}
	if len(geo) != len(d.Capsules) {
		return bad("bijection")
	}
	lastSlot := -1
	for _, c := range d.Capsules {
		if c.Slot <= lastSlot || c.Slot < 0 || c.Slot > 79 || c.SlotID != fmt.Sprintf("srf-ng-%d", c.Slot) {
			return bad("slot order/identity")
		}
		lastSlot = c.Slot
		if !stateEnum(c.Archetype, "browser", "desktop", "microvm", "terminal") || !stateEnum(c.Health, "healthy", "attaching", "degraded", "breach", "zombie") || !stateEnum(c.Lifecycle, "booting", "ready", "busy", "stopping", "stopped", "failed") || !stateEnum(c.Mode, "read_only", "operator_controlled", "agent_locked", "switching") {
			return bad("capsule enum")
		}
		if c.Xpra != 10020+c.Slot || (c.Archetype == "browser" && (c.CDP == nil || *c.CDP != 9220+c.Slot)) || (c.Archetype != "browser" && c.CDP != nil) {
			return bad("ports")
		}
		if c.Mode == "agent_locked" {
			if c.LeaseHolder == nil || !stateSlugPattern.MatchString(*c.LeaseHolder) || c.LeaseEpoch < 1 {
				return bad("lease")
			}
		} else if c.LeaseHolder != nil {
			return bad("lease holder")
		}
		if c.Ready != (c.Lifecycle == "ready") || c.Seq != d.Seq {
			return bad("ready/seq")
		}
		p, ok := geo[c.SlotID]
		if !ok || p.n != c.Workspace || p.rect != c.Geometry || c.URI != stateURI(d.Host, c.Workspace, c.Slot) {
			return bad("geometry/uri")
		}
		rec := c.Recording
		if !stateEnum(rec.Phase, "idle", "starting", "recording", "stopping", "sealed", "error") {
			return bad("recording phase")
		}
		if rec.Store != nil && !stateEnum(*rec.Store, "ram", "disk") {
			return bad("recording store")
		}
		ram := rec.Store != nil && *rec.Store == "ram"
		if ram != (rec.TTL != nil) || (rec.TTL != nil && *rec.TTL < 0) {
			return bad("recording ttl")
		}
		if (rec.Phase == "idle" && rec.Store != nil) || (stateEnum(rec.Phase, "starting", "recording", "stopping") && !ram) || (rec.Store != nil && *rec.Store == "disk" && rec.Phase != "sealed") || (rec.Phase == "sealed" && rec.Store == nil) {
			return bad("recording state machine")
		}
		for _, m := range []*StateMetric{c.Telemetry.CPU, c.Telemetry.Memory} {
			if m != nil && (!stateEnum(m.Source, "cgroup", "procfs", "srfd", "sysfs", "vadapt") || m.Value < 0 || m.Timestamp < 1 || m.Timestamp > d.Timestamp || d.Timestamp-m.Timestamp > 3000) {
				return bad("metric")
			}
		}
	}
	return nil
}

// stateRequired rejects missing fields (including nullable fields), before typed decode.
func stateRequired(raw any, typ reflect.Type) error {
	if typ.Kind() == reflect.Pointer {
		if raw == nil {
			return nil
		}
		return stateRequired(raw, typ.Elem())
	}
	if typ == reflect.TypeFor[StateTree]() {
		return nil
	} // Its custom decoder enforces the closed union.
	switch typ.Kind() {
	case reflect.Struct:
		o, ok := raw.(map[string]any)
		if !ok {
			return errors.New("object required")
		}
		for i := 0; i < typ.NumField(); i++ {
			f := typ.Field(i)
			name := f.Tag.Get("json")
			v, exists := o[name]
			if !exists {
				return errors.New("missing field: " + name)
			}
			if err := stateRequired(v, f.Type); err != nil {
				return err
			}
		}
	case reflect.Slice:
		a, ok := raw.([]any)
		if !ok {
			return errors.New("array required")
		}
		for _, v := range a {
			if err := stateRequired(v, typ.Elem()); err != nil {
				return err
			}
		}
	default:
		if raw == nil {
			return errors.New("non-null field required")
		}
	}
	return nil
}
func validateStateBytes(b []byte) error {
	var raw any
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return err
	}
	if err := stateRequired(raw, reflect.TypeFor[StateDocument]()); err != nil {
		return err
	}
	var d StateDocument
	dec = json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&d); err != nil {
		return err
	}
	if err := dec.Decode(new(any)); err != io.EOF {
		return errors.New("trailing JSON")
	}
	return validateState(d)
}

type stateServer struct {
	runtimeLock    *os.File
	mu             sync.Mutex
	app            *App
	http           *http.Server
	listener       net.Listener
	credential     string
	url, discovery string
	discoveryInfo  os.FileInfo
	source         func() (stateInput, error)
	previous       []byte
	contents       []byte
	latest         []byte
	seq            int64
	epoch          time.Time
}

var stateServers = struct {
	sync.Mutex
	items map[*App]*stateServer
}{items: map[*App]*stateServer{}}

// StartState and StopState are lifecycle functions, not Wails-bound App methods.
// WIRE owns their calls from App.startup and App.shutdown.
func StartState(a *App) error { _, err := startStateServer(a); return err }
func StopState(a *App) {
	stateServers.Lock()
	s := stateServers.items[a]
	delete(stateServers.items, a)
	stateServers.Unlock()
	if s != nil {
		s.close()
	}
}
func startStateServer(a *App) (func(), error) {
	if a == nil {
		return nil, errors.New("state app unavailable")
	}
	stateServers.Lock()
	defer stateServers.Unlock()
	if _, ok := stateServers.items[a]; ok {
		return nil, errors.New("state listener already started")
	}
	s, err := newStateServer(a)
	if err != nil {
		return nil, err
	}
	stateServers.items[a] = s
	return func() { StopState(a) }, nil
}
func newStateServer(a *App) (*stateServer, error) {
	runtimeDir := os.Getenv("XDG_RUNTIME_DIR")
	if runtimeDir == "" || !filepath.IsAbs(runtimeDir) {
		return nil, errors.New("state runtime directory unavailable")
	}
	addr := os.Getenv("OSCU_STATE_ADDR")
	if addr == "" {
		addr = "127.0.0.1:0"
	}
	host, _, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, errors.New("invalid state address")
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() || ip.To4() == nil {
		return nil, errors.New("state address must be IPv4 loopback")
	}
	// Bind only the canonical loopback address; Host checks use this same address.
	if !ip.Equal(net.ParseIP("127.0.0.1")) {
		return nil, errors.New("state address must be 127.0.0.1")
	}
	dir := filepath.Join(runtimeDir, "os-cu")
	if err = os.Mkdir(dir, 0700); err != nil && !os.IsExist(err) {
		return nil, err
	}
	st, err := os.Lstat(dir)
	if err != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
		return nil, errors.New("unsafe state runtime directory")
	}
	if sys, ok := st.Sys().(*syscall.Stat_t); !ok || int(sys.Uid) != os.Getuid() {
		return nil, errors.New("foreign state runtime directory")
	}
	lock, err := os.OpenFile(dir, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = lock.Close()
		return nil, errors.New("state runtime directory in use")
	}
	accepted := false
	defer func() {
		if !accepted {
			_ = lock.Close()
		}
	}()
	s := &stateServer{app: a, discovery: filepath.Join(dir, "cockpit-state.json"), epoch: time.Now(), runtimeLock: lock}
	s.source = s.readInputs
	f, err := os.OpenFile(s.discovery, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if os.IsExist(err) {
		old, readErr := os.OpenFile(s.discovery, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if readErr != nil {
			return nil, errors.New("unsafe existing state discovery")
		}
		info, statErr := old.Stat()
		var entry struct {
			PID int `json:"pid"`
		}
		readErr = json.NewDecoder(io.LimitReader(old, 4096)).Decode(&entry)
		_ = old.Close()
		if statErr != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0600 || readErr != nil || entry.PID <= 0 {
			return nil, errors.New("invalid existing state discovery")
		}
		if sys, ok := info.Sys().(*syscall.Stat_t); !ok || int(sys.Uid) != os.Getuid() {
			return nil, errors.New("foreign state discovery")
		}
		if syscall.Kill(entry.PID, 0) != syscall.ESRCH {
			return nil, errors.New("state discovery owner may be alive")
		}
		current, e := os.Lstat(s.discovery)
		if e != nil || !os.SameFile(info, current) {
			return nil, errors.New("state discovery changed")
		}
		if err = os.Remove(s.discovery); err != nil {
			return nil, err
		}
		f, err = os.OpenFile(s.discovery, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	}
	if err != nil {
		return nil, err
	}
	if err = f.Chmod(0600); err != nil {
		_ = f.Close()
		_ = os.Remove(s.discovery)
		return nil, err
	}
	s.discoveryInfo, err = f.Stat()
	if err != nil {
		_ = f.Close()
		_ = os.Remove(s.discovery)
		return nil, err
	}
	clean := func() { _ = f.Close(); s.removeDiscovery() }
	entropy := make([]byte, 16)
	if _, err = rand.Read(entropy); err != nil {
		clean()
		return nil, err
	}
	s.credential = hex.EncodeToString(entropy)
	s.listener, err = net.Listen("tcp4", addr)
	if err != nil {
		clean()
		return nil, err
	}
	s.url = "http://" + s.listener.Addr().String() + "/state"
	entry := struct {
		PID        int    `json:"pid"`
		Credential string `json:"token"`
		URL        string `json:"url"`
	}{os.Getpid(), s.credential, s.url}
	if err = json.NewEncoder(f).Encode(entry); err == nil {
		err = f.Sync()
	}
	if err != nil {
		_ = s.listener.Close()
		clean()
		return nil, err
	}
	_ = f.Close()
	s.http = &http.Server{Handler: s, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 15 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 8192}
	accepted = true
	go func() { _ = s.http.Serve(s.listener) }()
	return s, nil
}
func (s *stateServer) removeDiscovery() {
	if info, err := os.Lstat(s.discovery); err == nil && s.discoveryInfo != nil && os.SameFile(info, s.discoveryInfo) {
		_ = os.Remove(s.discovery)
	}
}
func (s *stateServer) close() {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if s.http != nil {
		if s.http.Shutdown(ctx) != nil {
			_ = s.http.Close()
		}
	}
	s.removeDiscovery()
	if s.runtimeLock != nil {
		_ = s.runtimeLock.Close()
	}
}
func (s *stateServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	_, port, _ := net.SplitHostPort(s.listener.Addr().String())
	if (r.Host != "127.0.0.1:"+port && r.Host != "localhost:"+port) || len(r.Header.Values("Origin")) > 1 || r.Header.Get("Origin") != "" || len(r.Header.Values("Sec-Fetch-Site")) > 1 || (r.Header.Get("Sec-Fetch-Site") != "" && r.Header.Get("Sec-Fetch-Site") != "none") || len(r.Header.Values("Authorization")) != 1 || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+s.credential)) != 1 || r.URL.RawQuery != "" {
		http.Error(w, "forbidden", 403)
		return
	}
	if r.URL.Path != "/state" || r.URL.EscapedPath() != "/state" {
		http.NotFound(w, r)
		return
	}
	if r.Method != "GET" && r.Method != "HEAD" {
		w.Header().Set("Allow", "GET, HEAD")
		http.Error(w, "method not allowed", 405)
		return
	}
	b, err := s.snapshot()
	if err != nil {
		http.Error(w, "state unavailable", 500)
		return
	}
	digest := sha256.Sum256(b)
	w.Header().Set("ETag", `"sha256-`+hex.EncodeToString(digest[:])+`"`)
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Content-Length", strconv.Itoa(len(b)))
	if r.Method == "GET" {
		_, _ = w.Write(b)
	}
}
func (s *stateServer) snapshot() ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	in, err := s.source()
	if err != nil {
		return nil, err
	}
	in.Timestamp = max(1, time.Since(s.epoch).Milliseconds())
	in.Seq = 0
	doc, err := buildStateSnapshot(in)
	if err != nil {
		return nil, err
	}
	fingerprint := doc
	fingerprint.Timestamp = 1
	fingerprint.Capsules = slices.Clone(doc.Capsules)
	for i := range fingerprint.Capsules {
		fingerprint.Capsules[i].Telemetry = StateTelemetry{}
	}
	key, err := canonicalState(fingerprint)
	if err != nil {
		return nil, err
	}
	if s.previous != nil && !bytes.Equal(key, s.previous) {
		s.seq++
	}
	s.previous = key
	doc.Seq = s.seq
	for i := range doc.Capsules {
		doc.Capsules[i].Seq = s.seq
	}
	// A clock tick alone is not a new snapshot. Keep the exact document shared
	// by HTTP and the AX mirror until a field (including telemetry) changes.
	content := doc
	content.Timestamp = 1
	contentKey, err := canonicalState(content)
	if err != nil {
		return nil, err
	}
	if s.latest != nil && bytes.Equal(contentKey, s.contents) {
		return slices.Clone(s.latest), nil
	}
	body, err := canonicalState(doc)
	if err != nil {
		return nil, err
	}
	s.contents, s.latest = contentKey, body
	return slices.Clone(body), nil
}
func (s *stateServer) readInputs() (stateInput, error) {
	hostname, err := os.Hostname()
	if err != nil {
		hostname = "localhost"
	}
	hostname = strings.ToLower(strings.Split(hostname, ".")[0])
	if !stateHostPattern.MatchString(hostname) {
		hostname = "localhost"
	}
	mode := os.Getenv("OSCU_WINDOW_MODE")
	if mode == "" {
		mode = "tile_1_6"
	}
	cap := 4
	if value, ok := os.LookupEnv("max_capsulas_por_espacio"); ok {
		cap, err = strconv.Atoi(value)
		if err != nil {
			return stateInput{}, errors.New("invalid workspace cap")
		}
	}
	in := stateInput{Host: hostname, WindowMode: mode, Cap: cap, Capsules: []stateCapsuleInput{}}
	response, err := manager.EnviarComando(manager.RutaSocket(""), manager.Request{Comando: "listar"}, 10*time.Second)
	if err != nil {
		return in, err
	}
	if !response.OK {
		return in, errors.New("srfd list refused")
	}
	// Additive read-only wire fields can carry the input lease once srfd supplies it.
	var surfaces []struct {
		manager.SurfaceInfo
		Mode   string  `json:"mode"`
		Holder *string `json:"lease_holder"`
	}
	if err = json.Unmarshal(response.Datos, &surfaces); err != nil {
		return in, err
	}
	var recording RecordingState
	if s.app.grab != nil {
		recording = s.app.EstadoGrabacion()
	}
	for _, v := range surfaces {
		lifecycle, health := "", ""
		switch v.Estado {
		case "iniciando":
			lifecycle, health = "booting", "attaching"
		case "listo":
			lifecycle, health = "ready", "healthy"
		case "deteniendo":
			lifecycle, health = "stopping", "attaching"
		case "huerfana":
			lifecycle, health = "failed", "zombie"
		default:
			return in, errors.New("unmapped srfd lifecycle")
		}
		// Match the existing capsule projection: listo is OPERATOR-CONTROL, other
		// lifecycle states are READ-ONLY. Explicit srfd input-lease state takes priority.
		inputMode := "read_only"
		if lifecycle == "ready" {
			inputMode = "operator_controlled"
		}
		if v.Mode != "" {
			inputMode = v.Mode
		}
		c := stateCapsuleInput{Slot: int(v.Slot), Archetype: v.Clase, Lifecycle: lifecycle, Health: health, Mode: inputMode, LeaseHolder: v.Holder, LeaseEpoch: v.LeaseEpoca, Recording: StateRecording{Phase: "idle"}}
		if s.app != nil && s.app.telem != nil {
			c.Telemetry = s.app.telem.Capsula(v.PID, s.epoch)
		}
		// Missing sensors and input holders stay null.
		// The legacy disk recorder is intentionally rejected while live (Q8).
		if recording.Slot == int(v.Slot) && recording.Fase != "" && recording.Fase != "idle" {
			c.Recording.Phase = recording.Fase
			// Decode only explicit storage metadata from REC's future additive contract.
			b, _ := json.Marshal(recording)
			var meta struct {
				Store *string `json:"store"`
				TTL   *int64  `json:"ttl_ms"`
			}
			_ = json.Unmarshal(b, &meta)
			c.Recording.Store = meta.Store
			c.Recording.TTL = meta.TTL
			if c.Recording.Store == nil && recording.Dir != "" {
				disk := "disk"
				c.Recording.Store = &disk
			}
		}
		in.Capsules = append(in.Capsules, c)
	}
	return in, nil
}
func (a *App) EstadoCanonico() string {
	stateServers.Lock()
	s := stateServers.items[a]
	stateServers.Unlock()
	if s == nil {
		return ""
	}
	b, err := s.snapshot()
	if err != nil {
		return ""
	}
	return string(b)
}
func (a *App) BloqueSlot(slotID string) string {
	b := a.EstadoCanonico()
	if b == "" {
		return ""
	}
	var d struct {
		Capsules []json.RawMessage `json:"capsules"`
	}
	if json.Unmarshal([]byte(b), &d) != nil {
		return ""
	}
	for _, raw := range d.Capsules {
		var c struct {
			ID string `json:"slot_id"`
		}
		if json.Unmarshal(raw, &c) == nil && c.ID == slotID {
			return string(raw)
		}
	}
	return ""
}
func (a *App) StateURL() string {
	stateServers.Lock()
	defer stateServers.Unlock()
	if s := stateServers.items[a]; s != nil {
		return s.url
	}
	return ""
}
