// w8runner is a non-inferential multicall capsule tool. No host execution path.
package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const (
	bundleCap   int64 = 512 << 20
	patchCap    int64 = 1 << 20
	evidenceCap       = 102400
	archiveCap        = 196608
	entryCap          = 64
)

type runner struct {
	root          string
	commands      commands
	probe         func(string) (probes, error)
	workspaceUsed func(string) (int64, error)
}
type progress struct {
	RC                  int    `json:"rc"`
	WorkspacePeakBytes  *int64 `json:"workspace_peak_bytes"`
	RawStatus           int    `json:"raw_status"`
	Signal              string `json:"signal"`
	TimedOut            bool   `json:"timed_out"`
	State               string `json:"state"`
	StdoutTotalBytes    int    `json:"stdout_total_bytes"`
	StdoutRetainedBytes int    `json:"stdout_retained_bytes"`
	StdoutSHA256        string `json:"stdout_sha256"`
	StdoutTruncated     bool   `json:"stdout_truncated"`
}
type ingestRequest struct {
	Commit           string            `json:"commit"`
	BundleLen        int64             `json:"bundle_len"`
	PatchLen         int64             `json:"patch_len"`
	WritePaths       []string          `json:"write_paths"`
	Gateway          string            `json:"gateway"`
	Sources          []source          `json:"sources,omitempty"`
	EvidenceRequests []evidenceRequest `json:"evidence_requests,omitempty"`
}
type ingestResponse struct {
	RC     int    `json:"rc"`
	Reason string `json:"reason"`
	Head   string `json:"head"`
	Probes probes `json:"probes"`
}
type gateRequest struct {
	Argv      []string `json:"argv"`
	TimeoutMS int64    `json:"timeout_ms"`
}

func main() {
	r := runner{root: "/workspace", commands: execCommands{}, probe: hostProbes}
	os.Exit(r.run(filepath.Base(os.Args[0]), os.Stdin, os.Stdout))
}
func (r runner) run(verb string, in io.Reader, out io.Writer) int {
	switch verb {
	case "ingest":
		return r.ingest(in, out)
	case "gate":
		return r.gate(in, out)
	case "status":
		b, e := readRegular(filepath.Join(r.root, ".w8/status.json"), 1<<20)
		if e != nil {
			return emit(out, progress{RC: 4, State: "unavailable"}, 4)
		}
		var s progress
		if json.Unmarshal(b, &s) != nil {
			return 5
		}
		return emit(out, s, 0)
	case "extract":
		// Refresh progress before exporting: the seat reads status again after extract.
		data, err := readRegular(filepath.Join(r.root, ".w8/status.json"), 1<<20)
		var state progress
		if err != nil || json.Unmarshal(data, &state) != nil || r.save(state) != nil {
			return 4
		}
		b, e := readRegular(filepath.Join(r.root, ".w8/request.json"), 1<<20)
		if e != nil {
			return 4
		}
		var req ingestRequest
		if json.Unmarshal(b, &req) != nil {
			return 5
		}
		if e = extract(filepath.Join(r.root, "out"), out, req.WritePaths); e != nil {
			return 5
		}
		return 0
	case "purge":
		return r.purge(out)
	default:
		return 2
	}
}
func emit(out io.Writer, v any, rc int) int {
	if json.NewEncoder(out).Encode(v) != nil {
		return 5
	}
	return rc
}
func decode(in io.Reader, v any, limit int64) error {
	b, e := io.ReadAll(io.LimitReader(in, limit+1))
	if e != nil {
		return e
	}
	if int64(len(b)) > limit {
		return errors.New("input cap")
	}
	d := json.NewDecoder(strings.NewReader(string(b)))
	d.DisallowUnknownFields()
	if e = d.Decode(v); e != nil {
		return e
	}
	if d.Decode(new(any)) != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}
func writeHeader(out io.Writer, b []byte) error {
	if e := binary.Write(out, binary.BigEndian, uint32(len(b))); e != nil {
		return e
	}
	_, e := out.Write(b)
	return e
}
func readHeader(in io.Reader, v any) error {
	var n uint32
	if e := binary.Read(in, binary.BigEndian, &n); e != nil {
		return e
	}
	if n == 0 || n > 1<<20 {
		return errors.New("header cap")
	}
	b := make([]byte, n)
	if _, e := io.ReadFull(in, b); e != nil {
		return e
	}
	return decode(strings.NewReader(string(b)), v, 1<<20)
}
func normalize(raw int) int {
	if raw == 0 {
		return 0
	}
	if raw >= 2 && raw <= 7 {
		return raw
	}
	return 5
}

var commitRE = regexp.MustCompile(`^[a-f0-9]{40}$`)

func (r runner) ingest(in io.Reader, out io.Writer) int {
	if _, err := r.sampleWorkspace(); err != nil {
		return 4
	}
	// C2-C5 must pass before even the header/bundle is read.
	p, e := r.probe("")
	if e != nil {
		return emit(out, ingestResponse{RC: 5, Reason: "containment", Probes: p}, 5)
	}
	var req ingestRequest
	if readHeader(in, &req) != nil || !commitRE.MatchString(req.Commit) || req.BundleLen <= 0 || req.BundleLen > bundleCap || req.PatchLen < 0 || req.PatchLen > patchCap || len(req.WritePaths) > 256 || len(req.Sources) > 256 || len(req.EvidenceRequests) > 32 {
		return emit(out, ingestResponse{RC: 2, Reason: "shape", Probes: p}, 2)
	}
	// The gateway address is not accepted from task input. The route is measured
	// from /proc/net/route by hostProbes; retained field must remain empty.
	if req.Gateway != "" {
		return emit(out, ingestResponse{RC: 2, Reason: "shape", Probes: p}, 2)
	}
	if e = os.MkdirAll(filepath.Join(r.root, ".w8"), 0700); e != nil {
		return 3
	}
	if e = os.MkdirAll(filepath.Join(r.root, "out"), 0700); e != nil {
		return 3
	}
	bundle := filepath.Join(r.root, ".w8/input.bundle")
	f, e := os.OpenFile(bundle, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return 3
	}
	_, e = io.CopyN(f, in, req.BundleLen)
	ce := f.Close()
	defer os.Remove(bundle)
	if e != nil || ce != nil {
		return 2
	}
	patch := make([]byte, req.PatchLen)
	if _, e = io.ReadFull(in, patch); e != nil {
		return 2
	}
	if len(patch) > 0 {
		if e = checkPatch(patch, req.WritePaths); e != nil {
			return emit(out, ingestResponse{RC: 2, Reason: "patch", Probes: p}, 2)
		}
	}
	// Every child runs with a fixed credential-free environment and a deadline.
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	git := []string{"git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "protocol.file.allow=always", "-c", "protocol.ext.allow=never", "-c", "protocol.allow=never", "-c", "core.attributesFile=/dev/null"}
	runGit := func(args []string, input io.Reader, output io.Writer) int {
		raw, _, _ := r.commands.run(ctx, r.root, append(append([]string{}, git...), args...), input, output)
		return raw
	}
	repo := filepath.Join(r.root, "repo")
	if runGit([]string{"clone", "--no-hardlinks", "--no-tags", "--no-checkout", "--template=", bundle, repo}, nil, io.Discard) != 0 {
		return emit(out, ingestResponse{RC: 3, Reason: "setup", Probes: p}, 3)
	}
	if runGit([]string{"-C", repo, "checkout", "--detach", req.Commit}, nil, io.Discard) != 0 {
		return 3
	}
	var head limitedRing
	head.cap = 128
	if runGit([]string{"-C", repo, "rev-parse", "HEAD"}, nil, &head) != 0 {
		return 3
	}
	got := strings.TrimSpace(string(head.data))
	if got != req.Commit {
		return emit(out, ingestResponse{RC: 3, Reason: "setup", Head: got, Probes: p}, 3)
	}
	if _, err := r.sampleWorkspace(); err != nil {
		return 4
	}
	for _, name := range []string{"objects/info/alternates", "info/grafts", "modules"} {
		if _, e := os.Lstat(filepath.Join(repo, ".git", name)); e == nil {
			return 5
		} else if !os.IsNotExist(e) {
			return 5
		}
	}
	if _, e := os.Lstat(filepath.Join(repo, ".gitmodules")); e == nil {
		return 5
	} else if !os.IsNotExist(e) {
		return 5
	}
	observed, e := r.observeSources(req.Sources)
	if e != nil {
		return emit(out, ingestResponse{RC: 5, Reason: "verification", Head: got, Probes: p}, 5)
	}
	sourceData, _ := json.Marshal(observed)
	if os.WriteFile(filepath.Join(r.root, ".w8/sources.json"), sourceData, 0600) != nil {
		return 3
	}
	if len(patch) > 0 {
		for _, args := range [][]string{{"-C", repo, "apply", "--check", "--whitespace=error-all", "-"}, {"-C", repo, "apply", "--whitespace=error-all", "-"}} {
			if runGit(args, strings.NewReader(string(patch)), io.Discard) != 0 {
				return 3
			}
		}
	}
	saved, _ := json.Marshal(req)
	if os.WriteFile(filepath.Join(r.root, ".w8/request.json"), saved, 0600) != nil {
		return 3
	}
	if r.save(progress{State: "ready"}) != nil {
		return 3
	}
	return emit(out, ingestResponse{Reason: "ok", Head: got, Probes: p}, 0)
}

// statfs reports allocated filesystem bytes, including checkout and tmpfs metadata.
// C1 independently proves this filesystem is the capsule workspace tmpfs.
func workspaceUsed(root string) (int64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(root, &st); err != nil {
		return 0, err
	}
	if st.Bsize <= 0 || st.Bfree > st.Blocks {
		return 0, errors.New("invalid workspace statfs")
	}
	blocks := st.Blocks - st.Bfree
	if blocks > uint64((1<<63-1)/st.Bsize) {
		return 0, errors.New("workspace statfs overflow")
	}
	return int64(blocks) * st.Bsize, nil
}
func (r runner) sampleWorkspace() (int64, error) {
	measure := r.workspaceUsed
	if measure == nil {
		measure = workspaceUsed
	}
	used, err := measure(r.root)
	if err != nil {
		return 0, err
	}
	if used < 0 {
		return 0, errors.New("negative workspace usage")
	}
	name := filepath.Join(r.root, ".w8/workspace-peak")
	if data, e := readRegular(name, 64); e == nil {
		previous, e := strconv.ParseInt(strings.TrimSpace(string(data)), 10, 64)
		if e != nil || previous < 0 {
			return 0, errors.New("invalid workspace peak")
		}
		if previous > used {
			used = previous
		}
	} else if !os.IsNotExist(e) {
		return 0, e
	}
	if err = os.MkdirAll(filepath.Dir(name), 0700); err != nil {
		return 0, err
	}
	tmp := name + ".tmp"
	// Refuse a task-created symlink rather than following it.
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return 0, err
	}
	defer os.Remove(tmp)
	_, err = io.WriteString(f, strconv.FormatInt(used, 10)+"\n")
	closeErr := f.Close()
	if err != nil {
		return 0, err
	}
	if closeErr != nil {
		return 0, closeErr
	}
	if err = os.Rename(tmp, name); err != nil {
		return 0, err
	}
	return used, nil
}
func (r runner) save(s progress) error {
	peak, err := r.sampleWorkspace()
	if err != nil {
		return err
	}
	s.WorkspacePeakBytes = &peak

	dir := filepath.Join(r.root, ".w8")
	if e := os.MkdirAll(dir, 0700); e != nil {
		return e
	}
	b, e := json.Marshal(s)
	if e != nil {
		return e
	}
	p := filepath.Join(dir, "status.tmp")
	if e = os.WriteFile(p, b, 0600); e != nil {
		return e
	}
	return os.Rename(p, filepath.Join(dir, "status.json"))
}
func (r runner) gate(in io.Reader, out io.Writer) int {
	var req gateRequest
	if decode(in, &req, 1<<20) != nil || len(req.Argv) == 0 || len(req.Argv) > 256 || req.TimeoutMS <= 0 || req.TimeoutMS > 900000 {
		return 2
	}
	for _, arg := range req.Argv {
		if strings.ContainsRune(arg, 0) || len(arg) > 65536 {
			return 2
		}
	}
	if req.Argv[0] == "" {
		return 2
	}
	if os.MkdirAll(filepath.Join(r.root, "out"), 0700) != nil {
		return 3
	}
	// go test execs its binaries from GOTMPDIR; /tmp is noexec, the workspace is not.
	if os.MkdirAll(filepath.Join(r.root, ".gotmp"), 0700) != nil {
		return 3
	}
	if r.save(progress{State: "running"}) != nil {
		return 3
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(req.TimeoutMS)*time.Millisecond)
	defer cancel()
	ring := &limitedRing{cap: 65536}
	raw, sig, timed := r.commands.run(ctx, filepath.Join(r.root, "repo"), req.Argv, nil, ring)
	s := progress{RC: normalize(raw), RawStatus: raw, Signal: sig, TimedOut: timed, State: "finished", StdoutTotalBytes: ring.total, StdoutRetainedBytes: len(ring.data), StdoutSHA256: sha(ring.data), StdoutTruncated: ring.total > len(ring.data)}
	if timed {
		s.RC = 5
		s.RawStatus = 124
	}
	finish := func() int {
		peak, err := r.sampleWorkspace()
		if err != nil {
			s.RC = 4
			s.WorkspacePeakBytes = nil
		} else {
			s.WorkspacePeakBytes = &peak
			if r.save(s) != nil {
				s.RC = 4
				s.WorkspacePeakBytes = nil
			}
		}
		return emit(out, s, s.RC)
	}
	fail := func() int { s.RC = 5; return finish() }
	if os.WriteFile(filepath.Join(r.root, "out/gate.log.txt"), ring.data, 0644) != nil {
		return fail()
	}
	// Produce the only code delta path; never stage binary output.
	if reqData, e := readRegular(filepath.Join(r.root, ".w8/request.json"), 1<<20); e == nil {
		var ingest ingestRequest
		if json.Unmarshal(reqData, &ingest) != nil {
			return fail()
		}
		sourcesData, e := readRegular(filepath.Join(r.root, ".w8/sources.json"), 1<<20)
		if e != nil {
			return fail()
		}
		var observed []sourceObserved
		if decode(strings.NewReader(string(sourcesData)), &observed, 1<<20) != nil {
			return fail()
		}
		delta := &limitedRing{cap: evidenceCap + 1}
		diffctx, stop := context.WithTimeout(context.Background(), 10*time.Second)
		stageRC, _, _ := r.commands.run(diffctx, filepath.Join(r.root, "repo"), []string{"git", "-c", "core.hooksPath=/dev/null", "add", "--intent-to-add", "--", "."}, nil, io.Discard)
		if stageRC != 0 {
			stop()
			return fail()
		}
		dr, _, _ := r.commands.run(diffctx, filepath.Join(r.root, "repo"), deltaArgv(ingest.Commit), nil, delta)
		stop()
		if dr != 0 || delta.total > evidenceCap {
			s.RC = 5
		} else if len(delta.data) > 0 {
			if validText("delta.patch", delta.data, ingest.WritePaths) != nil {
				s.RC = 5
			} else if os.WriteFile(filepath.Join(r.root, "out/delta.patch"), delta.data, 0644) != nil {
				s.RC = 5
			}
		}
		if s.RC == 0 {
			if r.receiptData(ingest, delta.data, observed) != nil {
				s.RC = 5
			}
		}
	}
	return finish()
}
func (r runner) purge(out io.Writer) int {
	entries, e := os.ReadDir(r.root)
	if e != nil {
		return emit(out, map[string]any{"remaining_workspace_bytes": nil}, 6)
	}
	for _, entry := range entries {
		if e = os.RemoveAll(filepath.Join(r.root, entry.Name())); e != nil {
			return emit(out, map[string]any{"remaining_workspace_bytes": nil}, 6)
		}
	}
	rest, e := os.ReadDir(r.root)
	if e != nil || len(rest) != 0 {
		return emit(out, map[string]any{"remaining_workspace_bytes": nil}, 6)
	}
	return emit(out, map[string]int{"remaining_workspace_bytes": 0}, 0)
}

// deltaArgv lists the code delta against the pin. git has no --no-binary flag (it
// printed usage and failed every gate); binary changes surface as "Binary files"
// and are refused by checkPatch.
func deltaArgv(commit string) []string {
	return []string{"git", "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", commit, "--"}
}
