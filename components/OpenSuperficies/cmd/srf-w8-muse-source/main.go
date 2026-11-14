// srf-w8-muse-source is an offline wave producer, not a worker runner.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"opensuperficies.local/srf/src/w8wave/musesource"
	"opensuperficies.local/srf/src/workerbridge"
)

func main() { os.Exit(run(os.Args[1:], os.Stderr)) }

func run(args []string, errOut io.Writer) int {
	flags := flag.NewFlagSet("srf-w8-muse-source", flag.ContinueOnError)
	flags.SetOutput(errOut)
	queuePath := flags.String("queue", "", "TAB queue: id, absolute brief path, absolute output path")
	wavePath := flags.String("wave-out", "", "exclusive wave JSON destination")
	manifestPath := flags.String("manifest-out", "", "exclusive frozen manifest JSON destination")
	bridge := flags.String("bridge-dir", "", "existing bridge registration root")
	repository := flags.String("repository", "", "pinned owner/repository")
	commit := flags.String("base-commit", "", "pinned 40-hex source commit")
	attempt := flags.String("attempt", "a1", "registered attempt ID")
	gates := flags.String("gate-argv-json", "", "required ordered JSON array of argv arrays")
	resources := flags.String("resource-policy-json", "", "required bounded JSON policy with inference=host")
	obligations := flags.String("obligations-json", "", "required JSON array of obligations")
	maxInFlight := flags.Int("max-in-flight", 6, "maximum concurrent TARGET credits, default 6")
	inFlight := flags.Int("in-flight", 0, "already consumed TARGET credits")
	waveOpen := flags.Bool("wave-open", false, "a previous wave is still open")
	seatReady := flags.Bool("seat-ready", false, "operator independently verified that a seat is available")
	guestInference := flags.Bool("guest-inference", false, "unsupported: inference must remain on host")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 2
	}
	if flags.NArg() != 0 || !absoluteClean(*queuePath) || !absoluteClean(*wavePath) ||
		!absoluteClean(*manifestPath) || !absoluteClean(*bridge) ||
		*wavePath == *manifestPath || *queuePath == *wavePath || *queuePath == *manifestPath ||
		*gates == "" || *resources == "" || *obligations == "" {
		fmt.Fprintln(errOut, "invalid source arguments")
		return 2
	}
	if existing(*wavePath) || existing(*manifestPath) {
		fmt.Fprintln(errOut, "EN_DUDA: an output or previous wave exists; reconcile before producing")
		return 6
	}
	var argv [][]string
	if err := strictDecode([]byte(*gates), &argv); err != nil {
		fmt.Fprintln(errOut, "invalid gate argv")
		return 2
	}
	var duties []string
	if err := strictDecode([]byte(*obligations), &duties); err != nil {
		fmt.Fprintln(errOut, "invalid obligations")
		return 2
	}
	policy, err := workerbridge.Parse([]byte(*resources))
	if err != nil {
		fmt.Fprintln(errOut, "invalid resource policy")
		return 2
	}
	file, err := os.Open(*queuePath)
	if err != nil {
		fmt.Fprintln(errOut, "queue unavailable")
		return 2
	}
	data, readErr := io.ReadAll(io.LimitReader(file, musesource.MaxQueueBytes+1))
	closeErr := file.Close()
	if readErr != nil || closeErr != nil || len(data) > musesource.MaxQueueBytes {
		fmt.Fprintln(errOut, "queue read or cap failed")
		return 2
	}
	cfg := musesource.Config{
		BridgeDir: *bridge, Repository: *repository, BaseCommit: *commit,
		Attempt: *attempt, GateArgv: argv, ResourcePolicy: policy,
		Obligations: duties, MaxInFlight: *maxInFlight, InFlight: *inFlight,
		WaveOpen: *waveOpen, SeatReady: *seatReady, GuestInference: *guestInference,
	}
	wave, manifest, err := musesource.Build(data, cfg)
	if err != nil {
		fmt.Fprintln(errOut, err)
		return musesource.ExitCode(err)
	}
	// The manifest is committed to disk first. The wave is the final
	// publication point. On a failed second write no wave is published.
	if err = exclusiveWrite(*manifestPath, manifest); err != nil {
		fmt.Fprintln(errOut, "manifest destination unavailable")
		return 6
	}
	if err = exclusiveWrite(*wavePath, wave); err != nil {
		_ = os.Remove(*manifestPath)
		fmt.Fprintln(errOut, "EN_DUDA: wave publication failed")
		return 6
	}
	return 0
}

func strictDecode(b []byte, out any) error {
	if _, err := workerbridge.Decode(b); err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(b))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(out); err != nil {
		return err
	}
	if _, err := decoder.Token(); err != io.EOF {
		return fmt.Errorf("trailing JSON")
	}
	return nil
}

func absoluteClean(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || strings.ContainsAny(path, "\x00\r\n\\") {
		return false
	}
	for _, component := range strings.Split(path, string(filepath.Separator)) {
		if component == "." || component == ".." {
			return false
		}
	}
	return true
}

func existing(path string) bool {
	_, err := os.Lstat(path)
	return err == nil || !os.IsNotExist(err)
}

func exclusiveWrite(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, writeErr := f.Write(data)
	if writeErr == nil {
		writeErr = f.Sync()
	}
	closeErr := f.Close()
	if writeErr == nil {
		writeErr = closeErr
	}
	if writeErr != nil {
		_ = os.Remove(path)
	}
	return writeErr
}
