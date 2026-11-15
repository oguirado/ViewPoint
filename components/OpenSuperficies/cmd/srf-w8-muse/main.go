// srf-w8-muse is a host-only registered-attempt driver. It has no model client,
// Docker command, gate executor, builder API or automatic approval path.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/w8muse"
	"opensuperficies.local/srf/src/workerbridge"
)

func main() { os.Exit(run(os.Args[1:], os.Stdin, os.Stdout, os.Stderr)) }
func run(args []string, in io.Reader, out, errOut io.Writer) int {
	flags := flag.NewFlagSet("srf-w8-muse", flag.ContinueOnError)
	flags.SetOutput(errOut)
	dry := flags.Bool("dry", false, "collect registered writer tasks without publishing")
	reconcile := flags.Bool("reconcile", false, "fetch existing authoritative receipt; never submit")
	campaign := flags.String("campaign-dir", "", "local receipt destination (required)")
	bridge := flags.String("bridge-dir", "", "canon campaign bridge directory (required)")
	repo := flags.String("repo-dir", "", "read-only, current local repository mirror (required)")
	socket := flags.String("socket", "/run/opensuperficies/w8-seat.sock", "host seat socket")
	wave := flags.String("wave", "-", "wave JSON array, or - for stdin")
	tipSource := flags.String("tip-source", "remote", "integration tip: remote (ls-remote, public repos) or mirror (conductor-fetched "+w8muse.IntegrationRef+")")
	gateTimeout := flags.Int64("gate-timeout-ms", 900000, "gate monotonic timeout, at most 900000")
	deadline := flags.Duration("attempt-timeout", 17*time.Minute, "driver deadline including cleanup grace")
	waveDeadline := flags.Duration("wave-timeout", 150*time.Minute, "overall deadline, at most 150m")
	if e := flags.Parse(args); e != nil {
		if e == flag.ErrHelp {
			return 0
		}
		return 2
	}
	if flags.NArg() != 0 || *campaign == "" || *bridge == "" || *repo == "" || *socket == "" || *gateTimeout <= 0 || *gateTimeout > 900000 || *deadline <= 0 || *deadline > 17*time.Minute || *waveDeadline <= 0 || *waveDeadline > 150*time.Minute || (*tipSource != "remote" && *tipSource != "mirror") {
		fmt.Fprintln(errOut, "invalid input: required paths or deadline")
		return 2
	}
	if *wave != "-" {
		f, e := os.Open(*wave)
		if e != nil {
			fmt.Fprintln(errOut, "wave unavailable")
			return 2
		}
		defer f.Close()
		in = f
	}
	b, e := io.ReadAll(io.LimitReader(in, w8muse.MaxHeader+1))
	if e != nil || len(b) > w8muse.MaxHeader {
		fmt.Fprintln(errOut, "wave read/cap")
		return 2
	}
	if _, e = workerbridge.Decode(b); e != nil {
		fmt.Fprintln(errOut, "invalid wave JSON")
		return 2
	}
	var items []w8muse.Item
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if e = d.Decode(&items); e != nil || len(items) == 0 || len(items) > 64 {
		fmt.Fprintln(errOut, "wave shape/count")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, *waveDeadline)
	defer cancel()
	runner := w8muse.ExecRunner{Home: *campaign}
	cfg := w8muse.Config{Dry: *dry, CampaignDir: *campaign, BridgeDir: *bridge, RepoDir: *repo, GateTimeoutMS: *gateTimeout, Timeout: *deadline, Seat: w8muse.SocketSeat{Path: *socket}, Repository: w8muse.GitRepository{Runner: runner, TipFromMirror: *tipSource == "mirror"}, Publisher: w8muse.GitPublisher{Runner: runner}}
	rc := 0
	encoder := json.NewEncoder(out)
	for _, item := range items {
		var result w8muse.Result
		if *reconcile {
			result, e = w8muse.Reconcile(ctx, cfg, item)
		} else {
			result, e = w8muse.Run(ctx, cfg, item)
		}
		code := result.ExitCode
		if e != nil {
			// The stdout result carries only the code; name the refusal so an operator can act on it.
			fmt.Fprintf(errOut, "%s %s: %v\n", item.TaskID, item.Attempt, e)
		}
		if e != nil && w8muse.ExitCode(e) > code {
			code = w8muse.ExitCode(e)
		}
		if code == 1 || code < 0 || code > 7 {
			code = 6
		}
		result.ExitCode = code
		if e = encoder.Encode(result); e != nil {
			return 6
		}
		if code > rc {
			rc = code
		}
		if ctx.Err() != nil {
			if rc < 5 {
				rc = 5
			}
			break
		}
	}
	return rc
}
