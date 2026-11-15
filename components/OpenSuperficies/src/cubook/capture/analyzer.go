package capture

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"time"
)

// Per-chunk engine analysis (os-cu-0 W3 lane E). Analyzer.Analyze is the ChunkHook P calls after det and before release
// (SW-0148 I3: the chunk is leased until it returns); Offload is the OFFLOAD worker of SW-0103 (K=1, queue 1): admission
// is non-blocking, so a busy analyzer is an overrun for the pacer, never a stall. All sums are 64-bit (VG-0029 Lemma 1).

const (
	analyzeCellMax = 4096 // visioncore.layout CELL_MAX
	analyzeAxisMax = 255  // visioncore.layout AXIS_MAX
	analyzeTileMin = 16   // visioncore.layout TILE_MIN_PX
	analyzeOutMax  = 64 << 10
)

var analyzeClasses = [...]string{"UNCHANGED", "JITTER", "CHANGED", "MOVED", "FLICKER", "FROZEN", "DRIFT"}

// GridFor mirrors visioncore.layout.grid_params: the smallest power of two >= 16 with cells <= 4096 and both axes <= 255.
// 0 when the geometry is outside the engine domain. chunk_analyze.py re-derives it and refuses a mismatch (E_GRID).
func GridFor(w, h int) int {
	if w < 1 || h < 1 {
		return 0
	}
	for s := int64(analyzeTileMin); s <= 1<<16; s <<= 1 {
		gw, gh := (int64(w)+s-1)/s, (int64(h)+s-1)/s
		if gw*gh <= analyzeCellMax && gw <= analyzeAxisMax && gh <= analyzeAxisMax {
			return int(s)
		}
	}
	return 0
}

// AnalyzeRunner runs the engine on one chunk and returns the stdout of vision/chunk_analyze.py.
type AnalyzeRunner func(chunkDir string, w, h, frames, grid int) ([]byte, error)

// ExecRunner returns the production runner: python3 -B <script> analyze ... with a wall timeout (0 = 300 s).
func ExecRunner(script string, timeout time.Duration) AnalyzeRunner {
	if timeout <= 0 {
		timeout = 300 * time.Second
	}
	return func(dir string, w, h, frames, grid int) ([]byte, error) {
		ctx, cancel := context.WithTimeout(context.Background(), timeout)
		defer cancel()
		cmd := exec.CommandContext(ctx, "python3", "-B", script, "analyze", "--chunk-dir", dir,
			"--w", itoa(w), "--h", itoa(h), "--frames", itoa(frames), "--grid", itoa(grid))
		var out bytes.Buffer
		cmd.Stdout = &limitedWriter{w: &out, left: analyzeOutMax}
		if err := cmd.Run(); err != nil {
			return out.Bytes(), err
		}
		return out.Bytes(), nil
	}
}

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

type limitedWriter struct {
	w    io.Writer
	left int
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if len(p) > l.left {
		p = p[:l.left]
	}
	l.left -= len(p)
	return l.w.Write(p)
}

// analyzeOut is the closed row of chunk_analyze.py (unknown field -> analyze_failed).
type analyzeOut struct {
	Chunk           int              `json:"chunk"`
	Frames          int              `json:"frames"`
	Grid            int              `json:"grid"`
	ClassCounts     map[string]int64 `json:"class_counts"`
	AnalyzeUsPerF   int64            `json:"analyze_us_per_frame"`
	EngineWheelSha8 string           `json:"engine_wheel_sha8"`
	RunNonce        string           `json:"run_nonce,omitempty"` // host echo of START run_nonce (W4 AW4-02)
}

// AnalyzeTotals are the 64-bit accumulators over all analyzed chunks.
type AnalyzeTotals struct {
	Chunks, Frames, Failed int64
	UsTotal                int64 // sum over chunks of analyze_us_per_frame * frames
	Timeouts               int64 // --analyze host: chunks whose marker never came within the bounded wait
}

// Analyzer implements ChunkHook for one run geometry.
type Analyzer struct {
	W, H int
	Run  AnalyzeRunner
	// WantNonce (host mode): the row must echo this run_nonce, else rc 5 marker_replay. Empty = not fenced (pre-W4 runs).
	WantNonce string

	mu  sync.Mutex // K = 1 is enforced by the single livedriver worker; mu only guards tot
	tot AnalyzeTotals
}

// Hook is the ChunkHook P wires into closeChunk.
func (a *Analyzer) Hook() ChunkHook { return a.Analyze }

// Totals returns a snapshot of the 64-bit accumulators.
func (a *Analyzer) Totals() AnalyzeTotals {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.tot
}

// Analyze runs the engine on chunkDir and fills row. On any failure row.AnalyzeRC=5 and an E_GATE analyze_failed error is
// returned; the caller must still release the chunk (AW3-02).
func (a *Analyzer) Analyze(chunkDir string, row *ChunkRow) error {
	fail := func() error {
		row.AnalyzeRC = 5
		a.mu.Lock()
		a.tot.Failed++
		a.mu.Unlock()
		return gate("analyze_failed")
	}
	grid := GridFor(a.W, a.H)
	if a.Run == nil || grid == 0 || row.Frames <= 0 {
		return fail()
	}
	raw, err := a.Run(chunkDir, a.W, a.H, row.Frames, grid)
	if err != nil {
		return fail()
	}
	var o analyzeOut
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&o); err != nil || dec.More() {
		return fail()
	}
	var sum int64
	for _, c := range analyzeClasses {
		v, ok := o.ClassCounts[c]
		if !ok || v < 0 {
			return fail()
		}
		sum += v
	}
	if a.WantNonce != "" && o.RunNonce != a.WantNonce {
		row.AnalyzeRC = 5
		a.mu.Lock()
		a.tot.Failed++
		a.mu.Unlock()
		return gate("marker_replay")
	}
	if len(o.ClassCounts) != len(analyzeClasses) || o.Chunk != row.Chunk || o.Frames != row.Frames || o.Grid != grid || o.AnalyzeUsPerF <= 0 ||
		len(o.EngineWheelSha8) != 8 || sum <= 0 {
		return fail()
	}
	row.Grid, row.ClassCounts, row.AnalyzeUsPerF, row.EngineWheelSha8, row.AnalyzeRC = o.Grid, o.ClassCounts, o.AnalyzeUsPerF, o.EngineWheelSha8, 0
	a.mu.Lock()
	a.tot.Chunks++
	a.tot.Frames += int64(o.Frames)
	a.tot.UsTotal += o.AnalyzeUsPerF * int64(o.Frames)
	a.mu.Unlock()
	return nil
}

// Host analysis handshake (CF-02, contract frozen by lane P2 on bus_P2.md). The capsule has no numpy, so the engine runs
// on the host: for chunk k the worker writes chunk_<k>/READY after frames + det, then waits at most Wait for the host's
// chunk_<k>/ANALYZED (one chunk_analyze.py row) or chunk_<k>/ANALYZE_FAILED; only then does the driver release the chunk.
// Every marker is written as <name>.tmp and renamed. The wait runs on the analysis worker, never on the pacer: a slow host
// becomes overrun/gap rows (JW3-01), and a missing answer is a row with analyze_timeout=1, analyze_rc=4.
const (
	StartFile         = "START"
	ReadyFile         = "READY"
	AnalyzedFile      = "ANALYZED"
	AnalyzeFailedFile = "ANALYZE_FAILED"

	DefaultHostWait = 30 * time.Second
	MaxHostWait     = 600 * time.Second
	hostPollMin     = time.Millisecond
	hostMarkerMax   = 4096 // ANALYZE_FAILED bound (ANALYZED is bounded by analyzeOutMax)
)

// HostAnalyzer is the ChunkHook of --analyze host. Its rows are validated by the same closed decoder as Analyzer.
type HostAnalyzer struct {
	W, H int
	Wait time.Duration // bounded wait per chunk (0 = DefaultHostWait, capped at MaxHostWait)
	Poll time.Duration // marker poll period (0 = 2 ms)
	// RunNonce is the 128-bit hex of START (empty = no replay fencing, pre-W4 runs): READY carries it, ANALYZED/ANALYZE_FAILED must echo it.
	RunNonce string

	mu       sync.Mutex
	inner    Analyzer
	timeouts int64
}

// Hook is the ChunkHook P wires into the worker.
func (h *HostAnalyzer) Hook() ChunkHook { return h.Analyze }

// Totals returns the analyzer totals plus the host timeouts.
func (h *HostAnalyzer) Totals() AnalyzeTotals {
	t := h.inner.Totals()
	h.mu.Lock()
	t.Timeouts = h.timeouts
	h.mu.Unlock()
	return t
}

// Analyze publishes READY for chunkDir and consumes the host marker (fail closed on anything but a valid ANALYZED).
// W4 (AW4-06): markers are opened directly (no lstat probe), a pre-planted ANALYZED/ANALYZE_FAILED refuses the chunk, and
// an answer must be newer than READY, name this chunk and echo run_nonce (when the run has one), else rc 5.
func (h *HostAnalyzer) Analyze(chunkDir string, row *ChunkRow) error {
	grid := GridFor(h.W, h.H)
	fail5 := func(reason string) error {
		row.AnalyzeRC = 5
		h.inner.mu.Lock()
		h.inner.tot.Failed++
		h.inner.mu.Unlock()
		return gate(reason)
	}
	if grid == 0 || row.Frames <= 0 {
		row.AnalyzeRC = 5
		return gate("analyze_failed")
	}
	for _, n := range []string{AnalyzedFile, AnalyzeFailedFile} { // nothing may pre-exist READY (a future-dated answer cannot be pre-planted)
		if _, err := os.Lstat(filepath.Join(chunkDir, n)); !errors.Is(err, fs.ErrNotExist) {
			return fail5("marker_preexisting")
		}
	}
	m := map[string]any{"schema": "os.cu.chunk-ready/v1", "chunk": row.Chunk, "first_render": row.FirstRender,
		"frames": row.Frames, "w": h.W, "h": h.H, "grid": grid, "det_sha256": row.Det}
	if h.RunNonce != "" {
		m["run_nonce"] = h.RunNonce
	}
	ready, _ := json.Marshal(m)
	if err := writeMarker(chunkDir, ReadyFile, append(ready, '\n')); err != nil {
		row.AnalyzeRC = 5
		return gate("analyze_failed")
	}
	var readyNs int64
	if fi, err := os.Lstat(filepath.Join(chunkDir, ReadyFile)); err == nil {
		readyNs = fi.ModTime().UnixNano()
	}
	wait, poll := h.Wait, h.Poll
	if wait <= 0 {
		wait = DefaultHostWait
	}
	if wait > MaxHostWait {
		wait = MaxHostWait
	}
	if poll < hostPollMin {
		poll = 2 * time.Millisecond
	}
	deadline := time.Now().Add(wait)
	for i := int64(0); i <= int64(wait/poll)+1; i++ { // bounded even if the wall clock stops
		fb, fns, ferr := openMarker(filepath.Join(chunkDir, AnalyzeFailedFile), hostMarkerMax)
		if !errors.Is(ferr, fs.ErrNotExist) { // failure wins over ANALYZED; a present-but-unsafe marker is rc 5
			row.AnalyzeRC = failedRC(fb, fns, ferr, row.Chunk, h.RunNonce, readyNs)
			h.inner.mu.Lock()
			h.inner.tot.Failed++
			h.inner.mu.Unlock()
			return gate("analyze_failed")
		}
		b, ans, rerr := openMarker(filepath.Join(chunkDir, AnalyzedFile), analyzeOutMax)
		if !errors.Is(rerr, fs.ErrNotExist) {
			if rerr == nil && ans < readyNs {
				rerr = errors.New("marker_stale")
			}
			h.inner.W, h.inner.H, h.inner.WantNonce = h.W, h.H, h.RunNonce
			h.inner.Run = func(string, int, int, int, int) ([]byte, error) { return b, rerr }
			if err := h.inner.Analyze(chunkDir, row); err != nil {
				return err
			}
			s := sha256.Sum256(b)
			row.AnalyzedSha = hex.EncodeToString(s[:])
			return nil
		}
		if !time.Now().Before(deadline) {
			break
		}
		time.Sleep(poll)
	}
	row.AnalyzeRC, row.AnalyzeTimeout = 4, 1
	h.mu.Lock()
	h.timeouts++
	h.mu.Unlock()
	return nil
}

// failedRC reads the host's rc from ANALYZE_FAILED ({"chunk":k,"rc":2..7,"reason":...,"run_nonce":...}). Anything unsafe,
// stale, of another chunk or another run is 5: a replayed marker never chooses the rc.
func failedRC(b []byte, mtimeNs int64, err error, chunk int, nonce string, readyNs int64) int {
	var m struct {
		Chunk *int   `json:"chunk"`
		RC    int    `json:"rc"`
		Nonce string `json:"run_nonce"`
	}
	if err != nil || mtimeNs < readyNs || json.Unmarshal(b, &m) != nil || m.Chunk == nil || *m.Chunk != chunk ||
		(nonce != "" && m.Nonce != nonce) || m.RC < 2 || m.RC > 7 {
		return 5
	}
	return m.RC
}

// openMarker is the one host-marker reader (W4 contract, mirrored by driver.py _open_marker): open O_RDONLY|O_NOFOLLOW|
// O_NONBLOCK|O_CLOEXEC straight away (no lstat probe: no check-then-open window, a FIFO never blocks), fstat on the open
// descriptor must be S_ISREG with st_nlink == 1 and 0 < size <= max, then one bounded read of that descriptor. ENOENT
// is an error wrapping fs.ErrNotExist (absent); every other refusal is a non-ErrNotExist error. Returns the bytes and the
// marker mtime in ns.
func openMarker(p string, max int64) ([]byte, int64, error) {
	f, err := os.OpenFile(p, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return nil, 0, err
	}
	defer f.Close()
	var st syscall.Stat_t
	if err := syscall.Fstat(int(f.Fd()), &st); err != nil {
		return nil, 0, err
	}
	if st.Mode&syscall.S_IFMT != syscall.S_IFREG || st.Nlink != 1 || st.Size <= 0 || st.Size > max {
		return nil, 0, errors.New("marker_not_regular_or_oversized")
	}
	b, err := io.ReadAll(io.LimitReader(f, max+1))
	if err != nil {
		return nil, 0, err
	}
	if int64(len(b)) > max {
		return nil, 0, errors.New("marker_not_regular_or_oversized")
	}
	return b, st.Mtim.Nano(), nil
}

// readMarker is the bytes-only form of openMarker.
func readMarker(p string, max int64) ([]byte, error) {
	b, _, err := openMarker(p, max)
	return b, err
}

// writeMarker writes dir/name through dir/name.tmp + rename, 0o640 (no world bits; the host reads through the run ACL).
func writeMarker(dir, name string, b []byte) error {
	tmp := filepath.Join(dir, name+".tmp")
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW|syscall.O_CLOEXEC, 0o640)
	if err != nil {
		return err
	}
	_, err = f.Write(b)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp, filepath.Join(dir, name))
	}
	if err != nil {
		os.Remove(tmp)
	}
	return err
}
