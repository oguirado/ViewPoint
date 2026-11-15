package capture

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// Chunked 60 fps live capture driver (os-cu-0 W2 lane D). Time is integer nanoseconds from an injected clock
// (VG-0008 I1-I3); a late frame advances render_index and the gap stays visible, it is never back-filled; frames live
// in <ramws>/chunk_<k>/frames.rgb24 and at most two chunks are resident unless the run is explicitly kept (VG-0103).
const (
	FrameNs            int64 = 16_666_667 // 1e9/60 rounded up, integer
	DefaultChunkFrames       = 60
	DefaultQuota       int64 = 2 << 30
	MinSegmentMs       int64 = 5000
	DefaultTTLSeconds  int64 = 7200
	MaxQuota           int64 = 8 << 30 // STORAGE LAW: explicit per-run ceiling
	Max4KChunkFrames         = 30
	ModeWL                   = "wl"
	ModeLive                 = "live"
	ModeHandover1s           = "handover-1s"
	ModeBench                = "bench"

	maxDeltaNs = int64(1) << 40
	maxFrameNs = int64(1) << 62
)

// Pacer is the fixed-step accumulator: a in [0,Δ), negative durations clamp to zero, one tick maps to k >= 0 steps.
type Pacer struct {
	delta, last, acc int64
	cur              uint64
	started          bool
}

// NewPacer returns a pacer with step deltaNs (a step outside (0,2^40] never produces a frame, VG-0008 I5).
func NewPacer(deltaNs int64) *Pacer { return &Pacer{delta: deltaNs} }

// Tick samples the clock once. ok=false means no step is due yet. Otherwise idx is the render_index of the frame
// and missing = steps skipped before it (overrun), which the caller logs and never back-fills.
func (p *Pacer) Tick(nowNs int64) (idx, missing uint64, ok bool) {
	if p.delta <= 0 || p.delta > maxDeltaNs {
		return 0, 0, false
	}
	if !p.started {
		p.started, p.last, p.acc, p.cur = true, nowNs, 0, 0
		return 0, 0, true
	}
	f := int64(0)
	if nowNs > p.last {
		f = nowNs - p.last
		if f > maxFrameNs {
			f = maxFrameNs
		}
		p.last = nowNs
	}
	t := p.acc + f
	k := t / p.delta
	p.acc = t % p.delta
	if k == 0 {
		return 0, 0, false
	}
	p.cur += uint64(k)
	return p.cur, uint64(k - 1), true
}

// WaitNs is the time until the next step is due, in (0,Δ].
func (p *Pacer) WaitNs() int64 { return p.delta - p.acc }

// InBounds is the addition-free range check of SW-0009 I3: o <= m && l <= m-o (o+l is never formed).
func InBounds(o, l, m uint64) bool { return o <= m && l <= m-o }

// FrameSource fills dst (W*H*3 packed RGB24) with the current screen.
type FrameSource interface{ Frame(dst []byte) error }

// XWDSource reads an XWD image (Xvfb_screen0 or a synthetic one) through io.ReaderAt of Size bytes.
type XWDSource struct {
	Src     io.ReaderAt
	Size    int64
	scratch []byte
	hdr     Header // parsed on frame 0 and reused (a geometry change is caught by the dst length check)
	hdrOK   bool
	closed  bool
}

// Close marks the source done (the file stays owned by the opener); Frame after Close is rc 5 source_closed.
func (x *XWDSource) Close() error { x.closed = true; x.scratch = nil; return nil }

func (x *XWDSource) Frame(dst []byte) error {
	if x.closed {
		return ioErr("source_closed", nil)
	}
	if !x.hdrOK {
		h, err := ParseXWDHeader(x.Src)
		if err != nil {
			return err
		}
		x.hdr, x.hdrOK = h, true
	}
	h := x.hdr
	if h.Width*h.Height*3 != len(dst) {
		return &Error{Class: ClassStale, Reason: "geometry_changed"}
	}
	need := h.BytesPerLine * h.Height
	if x.Size <= 0 || !InBounds(uint64(h.PixelOffset), uint64(need), uint64(x.Size)) {
		return ioErr("pixels_out_of_bounds", nil)
	}
	if cap(x.scratch) < need {
		x.scratch = make([]byte, need)
	}
	raw := x.scratch[:need]
	if n, _ := x.Src.ReadAt(raw, int64(h.PixelOffset)); n < need {
		return ioErr("pixels_short", nil)
	}
	BGRXToRGB24(dst, raw, h.Width, h.Height, h.BytesPerLine)
	return nil
}

// Releaser frees one chunk directory (default os.RemoveAll).
type Releaser func(chunkDir string) error

// Config of one driver run. Clock/Sleep are injected (ns); NowSec is wall seconds for the TTL marker only.
type Config struct {
	Guard             PathGuard
	OutFS             func(string) string // evidence fs probe (nil = MountFSType)
	RunDir            string              // ramws run dir from vision/ramws_shim.py
	OutDir            string              // disk: receipt.json, rows.jsonl, gaps.jsonl
	W, H              int
	Frames            int64 // render slots to cover (render_index < Frames)
	ChunkFrames       int
	Mode              string
	Det               bool
	Keep              bool // keep chunks resident (quota-bound) instead of releasing
	QuotaBytes        int64
	MinSegmentMs      int64
	Clock             func() int64
	Sleep             func(ns int64)
	NowSec            func() int64
	TTLSeconds        int64
	Source            FrameSource
	Release           Releaser
	Hook              ChunkHook // per-chunk analysis (lane E), after det and before release
	Analyze           bool      // --analyze engine: 4K live runs are rows-only and need it
	RequireContinuous bool      // frames < requested or any gap -> rc 5 segment_gappy
	SrcName           string    // receipt src: readat|mmap|xshm
	RunNonce          string    // 128-bit hex written in START (W4 AW4-02; empty = generated in Run)
	WriteStart        bool      // write <RunDir>/START with the CLOCK_MONOTONIC t0 of render_index 0 (CF-01 handshake)
}

// ChunkHook is the per-chunk analysis callback (lane E): it runs after det and before release on the chunk worker.
type ChunkHook func(dir string, row *ChunkRow) error

// Gap is one gaps.jsonl row.
type Gap struct {
	After   uint64 `json:"after"`
	Missing uint64 `json:"missing"`
}

// ChunkRow is one rows.jsonl row (integers and hex only).
type ChunkRow struct {
	Chunk           int              `json:"chunk"`
	FirstRender     uint64           `json:"first_render"`
	Frames          int              `json:"frames"`
	CaptureUsPerF   int64            `json:"capture_us_per_frame"`
	AnalyzeUsPerF   int64            `json:"analyze_us_per_frame"`
	CaptureP50Us    int64            `json:"capture_us_p50"`
	CaptureP95Us    int64            `json:"capture_us_p95"`
	WriteUsPerF     int64            `json:"write_us_per_frame"`
	Grid            int              `json:"grid,omitempty"`
	ClassCounts     map[string]int64 `json:"class_counts,omitempty"`
	EngineWheelSha8 string           `json:"engine_wheel_sha8,omitempty"`
	AnalyzeRC       int              `json:"analyze_rc"`
	AnalyzeTimeout  int              `json:"analyze_timeout,omitempty"` // 1: host marker missing after the bounded wait
	AnalyzedSha     string           `json:"analyzed_sha256,omitempty"` // sha256 of chunk_<k>/ANALYZED (--analyze host)
	PeakBytes       int64            `json:"peak_bytes"`
	Gaps            int              `json:"gaps"`
	Det             []string         `json:"det_sha256,omitempty"`
}

// Result of Run. Conservation: Published = Frames + Overrun + Dropped (RT2_R INV-LOSS).
type Result struct {
	Published, Frames, Overrun, Dropped     uint64
	Gaps                                    []Gap
	Chunks                                  []ChunkRow
	SegmentMs                               int64
	SpanMs, LongestRunMs                    int64
	CaptureP50Us, CaptureP95Us, WriteUsPerF int64
	PeakResident                            int64
	ResidentEnd                             int64 // bytes still accounted resident when Run returns (a failed release stays counted)
}

func usage(why string) *Error { return &Error{Class: ClassUsage, Reason: why} }

type chunkState struct {
	row     ChunkRow
	dir     string
	done    chan struct{}
	err     error
	bytes   int64
	rel     bool
	capNs   int64
	started int
	wrNs    int64
	caps    []int64
	hookErr error
	relErr  error
}

type rec struct {
	idx uint64
	tNs int64
}

func validate(c *Config) error {
	if c.ChunkFrames <= 0 {
		c.ChunkFrames = DefaultChunkFrames
	}
	if c.QuotaBytes <= 0 {
		c.QuotaBytes = DefaultQuota
	}
	if c.W <= 0 || c.H <= 0 || c.W > MaxDim || c.H > MaxDim || c.Frames <= 0 || c.Clock == nil || c.Sleep == nil || c.Source == nil || c.OutDir == "" {
		return usage("args")
	}
	switch c.Mode {
	case ModeLive:
		if c.Det {
			return usage("det_not_allowed_live")
		}
		if c.W*c.H > 1920*1080 && !c.Analyze {
			return usage("analyze_required_4k_live")
		}
	case ModeWL:
		if !c.Det {
			return usage("det_required_wl")
		}
	case ModeHandover1s:
		if c.W != 3840 || c.H != 2160 {
			return usage("handover_1s_res")
		}
		if c.Frames != 60 {
			return usage("handover_1s_frames")
		}
		c.Keep = true
		if c.QuotaBytes > DefaultQuota {
			return gate("E_RAM_QUOTA")
		}
	case ModeBench: // pacing measurement only: nothing is retained, analysed, handed over or started (AW4-03)
		if c.Keep {
			return usage("keep_not_allowed_bench")
		}
		if c.Analyze {
			return usage("analyze_not_allowed_bench")
		}
		c.Hook, c.WriteStart, c.RequireContinuous, c.MinSegmentMs = nil, false, false, 0
	default:
		return usage("mode")
	}
	if c.Keep { // a kept run always carries the hard TTL marker (AMEND C4)
		if c.TTLSeconds <= 0 {
			c.TTLSeconds = DefaultTTLSeconds
		}
		if c.TTLSeconds > DefaultTTLSeconds { // C4: the hard TTL is 2 h at most
			return gate("ttl_exceeded")
		}
		if c.NowSec == nil {
			return usage("ttl_clock")
		}
	}
	if c.Analyze && c.Hook == nil {
		return usage("analyze_hook_missing")
	}
	if c.QuotaBytes > MaxQuota {
		return gate("E_RAM_QUOTA")
	}
	if c.W*c.H > 1920*1080 && c.ChunkFrames > Max4KChunkFrames {
		return gate("chunk_4k_max_30")
	}
	if c.Keep && c.Mode != ModeHandover1s && c.W*c.H > 1920*1080 {
		return gate("retention_refused")
	}
	fb := int64(c.W) * int64(c.H) * 3
	chunks := (c.Frames + int64(c.ChunkFrames) - 1) / int64(c.ChunkFrames)
	perChunk := fb * int64(c.ChunkFrames)
	if perChunk/fb != int64(c.ChunkFrames) {
		return usage("args")
	}
	var resident int64
	if c.Keep {
		resident = fb * c.Frames
	} else {
		n := chunks
		if n > 2 {
			n = 2
		}
		resident = perChunk * n
	}
	if resident > c.QuotaBytes || resident < 0 {
		return gate("E_RAM_QUOTA")
	}
	return nil
}

// Run captures cfg.Frames render slots in chunks. Gaps are logged, never back-filled.
func Run(cfg Config) (*Result, error) {
	if err := validate(&cfg); err != nil {
		return nil, err
	}
	outFS := cfg.OutFS
	if outFS == nil {
		outFS = MountFSType
	}
	if err := CheckOut(cfg.OutDir, outFS); err != nil {
		return nil, err
	}
	if err := cfg.Guard.CheckRunDir(cfg.RunDir); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(cfg.OutDir, 0o750); err != nil {
		return nil, ioErr("out_mkdir", err)
	}
	rel := cfg.Release
	if rel == nil {
		rel = func(d string) error { return os.RemoveAll(d) }
	}
	if cfg.WriteStart && cfg.RunNonce == "" {
		if cfg.RunNonce = NewRunNonce(); cfg.RunNonce == "" {
			return nil, &Error{Class: ClassGate, Reason: "run_nonce"} // JW4-05: never an unfenced run
		}
	}
	fb := int64(cfg.W) * int64(cfg.H) * 3
	buf := make([]byte, fb)
	pc := NewPacer(FrameNs)
	res := &Result{}
	var (
		chunks   []*chunkState
		cur      *chunkState
		curFile  *os.File
		recs     []rec
		start    int64
		lastIdx  uint64
		haveLast bool
		startOK  bool // this run wrote START (only then may a failure remove it)
		mu       sync.Mutex
		resident int64
	)
	// One analysis worker (K=1, SW-0103): det -> ChunkHook -> unconditional release, off the pacer. Without Keep, opening
	// chunk k needs chunk k-2 done, so at most one job waits and the send below never blocks. A kept run skips that wait,
	// so its queue holds every chunk of the run (residency is already quota-bound at fb*Frames, validate).
	jobs := make(chan *chunkState, jobsCap(cfg))
	var workerWG sync.WaitGroup
	closeJobs := sync.OnceFunc(func() { close(jobs) }) // the loop end and a post-loop abort both close it (RJ3-01)
	workerWG.Add(1)
	go func() {
		defer workerWG.Done()
		for k := range jobs {
			func() {
				defer close(k.done)
				if cfg.Det {
					k.row.Det, k.err = hashChunk(filepath.Join(k.dir, "frames.rgb24"), fb)
				}
				if k.err == nil && cfg.Hook != nil {
					func() {
						defer func() {
							if r := recover(); r != nil {
								k.hookErr = gate("analyze_failed")
							}
						}()
						k.hookErr = cfg.Hook(k.dir, &k.row)
					}()
					if k.hookErr != nil {
						k.row.AnalyzeRC = 5
					}
				}
				if !cfg.Keep { // released even when det/hook failed: a failed analysis must not leak RAM
					relErr := rel(k.dir)
					if relErr == nil {
						mu.Lock()
						resident -= k.bytes
						k.rel = true
						mu.Unlock()
					} else {
						k.relErr = relErr // kept apart from a det error so chunk_release is never masked
					}
				}
			}()
		}
	}()
	var allCaps []int64
	var wrTotalNs, framesWritten int64
	closeChunk := func() error {
		if cur == nil {
			return nil
		}
		err := curFile.Close()
		k := cur
		curFile, cur = nil, nil
		if k.row.Frames > 0 {
			k.row.CaptureUsPerF = k.capNs / 1000 / int64(k.row.Frames)
			k.row.WriteUsPerF = k.wrNs / 1000 / int64(k.row.Frames)
			k.row.CaptureP50Us, k.row.CaptureP95Us = pctUs(k.caps, 50), pctUs(k.caps, 95)
		}
		jobs <- k
		if err != nil {
			return ioErr("chunk_close", err)
		}
		return nil
	}
	// openChunk returns opened=false when chunk k-2 is still in flight after the tick budget: the slot is then an
	// overrun (never a 3rd resident chunk, never a stalled pacer).
	openChunk := func(first uint64, budgetNs int64) (bool, error) {
		n := len(chunks)
		if !cfg.Keep && n >= 2 {
			if !waitDone(chunks[n-2].done, cfg.Clock, budgetNs) {
				return false, nil
			}
			if chunks[n-2].relErr != nil {
				return false, ioErr("chunk_release", chunks[n-2].relErr)
			}
			if chunks[n-2].err != nil {
				return false, ioErr("chunk_process", chunks[n-2].err)
			}
			if chunks[n-2].hookErr != nil {
				return false, gate("analyze_failed")
			}
		}
		d := filepath.Join(cfg.RunDir, "chunk_"+strconv.Itoa(n))
		if err := os.Mkdir(d, 0o770); err != nil { // no world bits; host access comes from the run's default ACL
			return false, ioErr("chunk_mkdir", err)
		}
		f, err := os.OpenFile(filepath.Join(d, "frames.rgb24"), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o660)
		if err != nil {
			return false, ioErr("chunk_open", err)
		}
		cur = &chunkState{dir: d, done: make(chan struct{})}
		cur.row = ChunkRow{Chunk: n, FirstRender: first}
		curFile = f
		chunks = append(chunks, cur)
		return true, nil
	}
	// releaseAll frees every chunk, the TTL marker and START of a failed run, kept or not (nothing may outlive a
	// failure); a chunk the releaser freed leaves the resident count, a failed release stays counted.
	releaseAll := func() {
		for _, k := range chunks {
			<-k.done
			relErr := rel(k.dir)
			os.RemoveAll(k.dir)
			if relErr == nil && !k.rel {
				mu.Lock()
				resident -= k.bytes
				k.rel = true
				mu.Unlock()
			}
		}
		os.Remove(filepath.Join(cfg.RunDir, "handover.json"))
		if startOK {
			os.Remove(filepath.Join(cfg.RunDir, StartFile))
		}
	}
	abort := func(err error) (*Result, error) {
		closeChunk()
		closeJobs()
		workerWG.Wait()
		releaseAll()
		mu.Lock()
		res.ResidentEnd = resident
		mu.Unlock()
		return res, err
	}
	bumpGap := func() { // a gap is counted on the open chunk, else on the last closed one: sum(chunk gaps) == len(gaps)
		if cur != nil {
			cur.row.Gaps++
		} else if len(chunks) > 0 {
			chunks[len(chunks)-1].row.Gaps++
		}
	}

	maxIter := cfg.Frames*8 + 1024
	for it := int64(0); ; it++ {
		if it > maxIter {
			return abort(gate("pacer_stalled"))
		}
		now := cfg.Clock()
		idx, missing, ok := pc.Tick(now)
		if !ok {
			cfg.Sleep(pc.WaitNs())
			continue
		}
		if idx >= uint64(cfg.Frames) {
			if haveLast && uint64(cfg.Frames) > lastIdx+1 { // tail overrun: the lost slots stay visible
				g := Gap{After: lastIdx, Missing: uint64(cfg.Frames) - lastIdx - 1}
				res.Gaps = append(res.Gaps, g)
				res.Overrun += g.Missing
				bumpGap()
				lastIdx = uint64(cfg.Frames) - 1
			}
			break
		}
		if cfg.WriteStart && !startOK { // first due tick = render_index 0: its clock value is the shared t0 (CF-01)
			if err := writeStart(cfg, now-int64(idx)*FrameNs); err != nil {
				return abort(err)
			}
			startOK = true
		}
		if cur == nil {
			opened, err := openChunk(idx, pc.WaitNs())
			if err != nil {
				return abort(err)
			}
			if !opened {
				continue
			}
		}
		if len(recs) == 0 {
			start = now
		}
		if haveLast && idx > lastIdx+1 {
			g := Gap{After: lastIdx, Missing: idx - lastIdx - 1}
			res.Gaps = append(res.Gaps, g)
			res.Overrun += g.Missing
			bumpGap()
		}
		_ = missing
		t0 := cfg.Clock()
		if err := cfg.Source.Frame(buf); err != nil {
			return abort(err)
		}
		t1 := cfg.Clock()
		cur.capNs += t1 - t0
		cur.caps = append(cur.caps, t1-t0)
		allCaps = append(allCaps, t1-t0)
		if _, err := curFile.Write(buf); err != nil {
			return abort(ioErr("chunk_write", err))
		}
		t2 := cfg.Clock()
		cur.wrNs += t2 - t1
		wrTotalNs += t2 - t1
		framesWritten++
		cur.bytes += fb
		cur.row.Frames++
		mu.Lock()
		resident += fb
		if resident > res.PeakResident {
			res.PeakResident = resident
		}
		cur.row.PeakBytes = res.PeakResident
		mu.Unlock()
		recs = append(recs, rec{idx, now - start})
		lastIdx, haveLast = idx, true
		res.Frames++
		if cur.row.Frames == cfg.ChunkFrames {
			if err := closeChunk(); err != nil {
				return abort(err)
			}
		}
		if wait := now + pc.WaitNs() - cfg.Clock(); wait > 0 {
			cfg.Sleep(wait)
		}
	}
	cerr := closeChunk()
	closeJobs()
	workerWG.Wait()
	if cerr != nil {
		return abort(cerr)
	}
	for _, k := range chunks {
		if k.hookErr != nil {
			return abort(gate("analyze_failed"))
		}
		if k.relErr != nil {
			return abort(ioErr("chunk_release", k.relErr))
		}
		if k.err != nil {
			return abort(ioErr("chunk_process", k.err))
		}
		res.Chunks = append(res.Chunks, k.row)
	}
	if haveLast {
		res.Published = lastIdx + 1
	}
	res.LongestRunMs = segmentMs(recs)
	res.SegmentMs = res.LongestRunMs                        // segment_ms keeps the old meaning: the longest gap-free run, not the span
	res.SpanMs = int64(res.Published) * FrameNs / 1_000_000 // VG-0008: published slots x Δ, tail gaps included
	res.CaptureP50Us, res.CaptureP95Us = pctUs(allCaps, 50), pctUs(allCaps, 95)
	if cfg.Mode == ModeBench {
		mu.Lock()
		res.ResidentEnd = resident
		mu.Unlock()
	}
	if framesWritten > 0 {
		res.WriteUsPerF = wrTotalNs / 1000 / framesWritten
	}
	if res.Published != res.Frames+res.Overrun+res.Dropped {
		releaseAll()
		return res, gate("conservation")
	}
	if cfg.Keep && cfg.TTLSeconds > 0 && cfg.NowSec != nil {
		now := cfg.NowSec()
		if now < 0 || now > math.MaxInt64-cfg.TTLSeconds { // ttl_until must never wrap negative
			releaseAll()
			return res, gate("ttl_clock_range")
		}
		b, _ := json.Marshal(map[string]int64{"ttl_until": now + cfg.TTLSeconds})
		if err := os.WriteFile(filepath.Join(cfg.RunDir, "handover.json"), b, 0o660); err != nil {
			releaseAll()
			return res, ioErr("handover_write", err)
		}
	}
	status := "ok" // the segment verdict is decided before the receipt so a receipt-only reader sees a refusal (JW3-03)
	if cfg.RequireContinuous && (res.Frames < uint64(cfg.Frames) || res.Overrun > 0 || len(res.Gaps) > 0) {
		status = "segment_gappy"
	} else if cfg.MinSegmentMs > 0 && res.SegmentMs < cfg.MinSegmentMs {
		status = "segment_short"
	}
	if err := writeReceipt(cfg, res, status); err != nil {
		releaseAll()
		return res, err
	}
	if status != "ok" {
		releaseAll()
		return res, gate(status)
	}
	return res, nil
}

// jobsCap sizes the analysis queue so closeChunk's send never blocks the pacer (JW3-01).
func jobsCap(c Config) int {
	if c.Keep && c.ChunkFrames > 0 {
		return int((c.Frames+int64(c.ChunkFrames)-1)/int64(c.ChunkFrames)) + 1
	}
	return 1
}

func segmentMs(recs []rec) int64 {
	var best, runStart int
	var bestMs int64
	for i := range recs {
		if i > 0 && recs[i].idx != recs[i-1].idx+1 {
			runStart = i
		}
		ms := (recs[i].tNs - recs[runStart].tNs + FrameNs) / 1_000_000
		if ms > bestMs {
			bestMs, best = ms, i
		}
	}
	_ = best
	return bestMs
}

func hashChunk(path string, frameBytes int64) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	buf := make([]byte, frameBytes)
	var out []string
	for {
		if _, err := io.ReadFull(f, buf); err != nil {
			if err == io.EOF {
				return out, nil
			}
			return nil, err
		}
		s := sha256.Sum256(buf)
		out = append(out, hex.EncodeToString(s[:]))
	}
}

func writeJSONL(path string, rows []any) error {
	var b []byte
	for _, r := range rows {
		x, err := json.Marshal(r)
		if err != nil {
			return err
		}
		b = append(append(b, x...), '\n')
	}
	return os.WriteFile(path, b, 0o640)
}

func writeReceipt(cfg Config, res *Result, status string) error {
	if cfg.Mode == ModeBench {
		return writeBenchReceipt(cfg, res, status)
	}
	src := cfg.SrcName
	if src == "" {
		src = "readat"
	}
	b2i := func(v bool) int {
		if v {
			return 1
		}
		return 0
	}
	timeouts := 0
	for _, c := range res.Chunks {
		timeouts += c.AnalyzeTimeout
	}
	rec := map[string]any{
		"schema": "os.cu.wlcap-receipt/v1", "mode": cfg.Mode, "width": cfg.W, "height": cfg.H,
		"requested": cfg.Frames, "chunk_frames": cfg.ChunkFrames, "delta_ns": FrameNs, "published": res.Published, "frames": res.Frames,
		"overrun": res.Overrun, "dropped": res.Dropped, "segment_ms": res.SegmentMs,
		"peak_resident_bytes": res.PeakResident, "quota_bytes": cfg.QuotaBytes, "det": b2i(cfg.Det),
		"keep": b2i(cfg.Keep), "chunks": len(res.Chunks), "gaps": len(res.Gaps),
		"span_ms": res.SpanMs, "longest_run_ms": res.LongestRunMs, "continuous_required": b2i(cfg.RequireContinuous),
		"src": src, "analyze": b2i(cfg.Analyze), "capture_us_p50": res.CaptureP50Us, "capture_us_p95": res.CaptureP95Us,
		"write_us_per_frame": res.WriteUsPerF, "status": status, "analyze_timeouts": timeouts,
	}
	b, err := json.Marshal(rec)
	if err == nil {
		err = os.WriteFile(filepath.Join(cfg.OutDir, "receipt.json"), append(b, '\n'), 0o640)
	}
	if err == nil {
		rows := make([]any, 0, len(res.Chunks))
		for _, c := range res.Chunks {
			rows = append(rows, c)
		}
		err = writeJSONL(filepath.Join(cfg.OutDir, "rows.jsonl"), rows)
	}
	if err == nil {
		gs := make([]any, 0, len(res.Gaps))
		for _, g := range res.Gaps {
			gs = append(gs, g)
		}
		err = writeJSONL(filepath.Join(cfg.OutDir, "gaps.jsonl"), gs)
	}
	if err != nil {
		return ioErr("receipt_write", err)
	}
	return nil
}

// writeStart publishes <RunDir>/START (tmp + rename, 0o640, never over an existing one): the CLOCK_MONOTONIC ns of
// render_index 0; render i is due at t0_ns + i*delta_ns. The actuator schedules plan t_us relative to it (CF-01).
func writeStart(cfg Config, t0 int64) error {
	if cfg.RunNonce == "" {
		return ioErr("run_nonce", nil)
	}
	p := filepath.Join(cfg.RunDir, StartFile)
	if _, err := os.Lstat(p); err == nil {
		return ioErr("start_exists", nil)
	}
	b, _ := json.Marshal(map[string]any{"schema": "os.cu.wlcap-start/v1", "clock": "CLOCK_MONOTONIC", "t0_ns": t0,
		"delta_ns": FrameNs, "frames": cfg.Frames, "w": cfg.W, "h": cfg.H, "chunk_frames": cfg.ChunkFrames, "mode": cfg.Mode,
		"run_nonce": cfg.RunNonce})
	tmp := p + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0o640)
	if err != nil {
		return ioErr("start_write", err)
	}
	err = f.Chmod(0o640)
	if err == nil {
		_, err = f.Write(append(b, '\n'))
	}
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp, p)
	}
	if err != nil {
		os.Remove(tmp)
		return ioErr("start_write", err)
	}
	return nil
}

var chunkRe = regexp.MustCompile(`^chunk_[0-9]+$`)

// ReapExpired releases every chunk_<k> of runDir once handover.json's ttl_until has passed (hard TTL, AMEND C4).
func ReapExpired(runDir string, nowSec int64) (bool, error) {
	b, err := os.ReadFile(filepath.Join(runDir, "handover.json"))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return false, nil
		}
		return false, ioErr("handover_read", err)
	}
	var h struct {
		TTLUntil int64 `json:"ttl_until"`
	}
	if json.Unmarshal(b, &h) != nil || h.TTLUntil <= 0 {
		return false, schemaErr("handover_json", nil)
	}
	if nowSec < h.TTLUntil {
		return false, nil
	}
	ents, err := os.ReadDir(runDir)
	if err != nil {
		return false, ioErr("reap_readdir", err)
	}
	for _, e := range ents {
		if e.IsDir() && chunkRe.MatchString(e.Name()) {
			if err := os.RemoveAll(filepath.Join(runDir, e.Name())); err != nil {
				return false, ioErr("reap_remove", err)
			}
		}
	}
	os.Remove(filepath.Join(runDir, "handover.json"))
	return true, nil
}

var _ = fmt.Sprintf

// RequireContinuousDefault is the --require-continuous default: on for wl, off otherwise; an explicit flag wins.
func RequireContinuousDefault(mode string, set, val bool) bool {
	if set {
		return val
	}
	return mode == ModeWL
}

// waitDone polls ch until it closes or budgetNs of the injected clock elapse (a frozen fake clock waits for ch).
func waitDone(ch <-chan struct{}, clock func() int64, budgetNs int64) bool {
	t0 := clock()
	for {
		select {
		case <-ch:
			return true
		default:
		}
		if clock()-t0 >= budgetNs {
			return false
		}
		time.Sleep(20 * time.Microsecond)
	}
}

// pctUs is the nearest-rank percentile (integer) of ns samples, in µs.
func pctUs(ns []int64, pct int) int64 { return xkUs(ns, int64(pct)*10000) }

// NewRunNonce returns a 128-bit random hex nonce; an unreadable entropy source yields "" and START is refused.
// It is a package var so a test can inject an entropy failure (JW4-05).
var NewRunNonce = func() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return ""
	}
	return hex.EncodeToString(b[:])
}

// xkUs is the integer order statistic x(k), k=max(1,ceil(ppm*n/1e6)) (SW-0167), in microseconds. ppm is clamped to
// [0,1e6]; n*ppm stays far below 2^63 for any realistic n (VG-0029 Lemma 1: 64-bit integer arithmetic only).
func xkUs(ns []int64, ppm int64) int64 {
	if len(ns) == 0 {
		return 0
	}
	if ppm < 0 {
		ppm = 0
	}
	if ppm > 1_000_000 {
		ppm = 1_000_000
	}
	v := append([]int64(nil), ns...)
	sort.Slice(v, func(i, j int) bool { return v[i] < v[j] })
	k := (int64(len(v))*ppm + 999_999) / 1_000_000
	if k < 1 {
		k = 1
	}
	return v[k-1] / 1000
}

// writeBenchReceipt writes os.cu.wlcap-bench/v1: pacing numbers only, nothing retained. Conservation
// published = frames + overrun + dropped (SW-0058 Thm 4) is enforced by Run before this is written.
func writeBenchReceipt(cfg Config, res *Result, status string) error {
	src := cfg.SrcName
	if src == "" {
		src = "readat"
	}
	det := 0
	if cfg.Det {
		det = 1
	}
	rec := map[string]any{
		"schema": "os.cu.wlcap-bench/v1", "src": src, "det": det, "width": cfg.W, "height": cfg.H,
		"requested": cfg.Frames, "published": res.Published, "frames": res.Frames, "overrun": res.Overrun, "dropped": res.Dropped,
		"gaps": len(res.Gaps), "span_ms": res.SpanMs, "longest_run_ms": res.LongestRunMs,
		"capture_us_p50": res.CaptureP50Us, "capture_us_p95": res.CaptureP95Us, "write_us_per_frame": res.WriteUsPerF,
		"peak_resident_bytes": res.PeakResident, "resident_end_bytes": res.ResidentEnd, "status": status,
	}
	b, err := json.Marshal(rec)
	if err == nil {
		err = os.WriteFile(filepath.Join(cfg.OutDir, "receipt.json"), append(b, '\n'), 0o640)
	}
	if err != nil {
		return ioErr("receipt_write", err)
	}
	return nil
}
