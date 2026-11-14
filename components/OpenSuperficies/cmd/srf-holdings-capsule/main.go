// srf-holdings-capsule starts Holdings test capsules and gives computer-use clients
// exact frames (RAM only), XTEST input with monotonic stamps and agent-lane state.
// Every command prints one JSON line. Exit codes: 0 ok, 2 input, 3 setup, 4 unavailable,
// 5 capture/verification, 6 indeterminate; never 1.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"golang.org/x/sys/unix"

	"opensuperficies.local/srf/src/holdingscap"
)

type execRunner struct{}

func (execRunner) Run(ctx context.Context, argv []string, stdin []byte) ([]byte, error) {
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	if e := cmd.Run(); e != nil {
		msg := strings.TrimSpace(errb.String())
		if len(msg) > 400 {
			msg = msg[:400]
		}
		return out.Bytes(), fmt.Errorf("%v: %s", e, msg)
	}
	return out.Bytes(), nil
}

func mono() uint64 {
	var ts unix.Timespec
	_ = unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts)
	return uint64(ts.Sec)*1e9 + uint64(ts.Nsec)
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, holdingscap.Tool{R: execRunner{}, Clock: mono}))
}

func run(args []string, out, errOut io.Writer, t holdingscap.Tool) int {
	usage := "usage: srf-holdings-capsule start --slots N --app DIR --scenario ID --seed N [--ui lit|v2] | frame --container C [--state] | input --container C (--key K | --click X,Y [--button B]) | observe --container C | stop --container C"
	if len(args) == 0 {
		fmt.Fprintln(errOut, usage)
		return 2
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	fs.SetOutput(errOut)
	slots := fs.Int("slots", 1, "capsules to start (1..4)")
	app := fs.String("app", "", "Holdings linux-arm64 build folder")
	scenario := fs.String("scenario", "h3-core-journey", "Holdings scenario id")
	seed := fs.Int64("seed", 4242, "scenario seed")
	ui := fs.String("ui", "", "app renderer: lit or v2 (default: the app's own default)")
	container := fs.String("container", "", "capsule name from start")
	state := fs.Bool("state", false, "frame: also return the agent-lane observation")
	key := fs.String("key", "", "input: X keysym")
	click := fs.String("click", "", "input: X,Y")
	button := fs.Int("button", 1, "input: mouse button 1..3")
	timeout := fs.Duration("timeout", 3*time.Minute, "overall deadline")
	if e := fs.Parse(args[1:]); e != nil || fs.NArg() != 0 {
		fmt.Fprintln(errOut, usage)
		return 2
	}
	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()
	var v any
	var e error
	switch args[0] {
	case "start":
		v, e = t.Start(ctx, holdingscap.StartSpec{Slots: *slots, AppDir: *app, Scenario: *scenario, Seed: *seed, UI: *ui})
	case "frame":
		v, e = t.Frame(ctx, *container, *state)
	case "observe":
		v, e = t.Observe(ctx, *container)
	case "input":
		in := holdingscap.Input{Key: *key, Button: *button}
		if *key == "" {
			x, y, ok := strings.Cut(*click, ",")
			xi, e1 := strconv.Atoi(x)
			yi, e2 := strconv.Atoi(y)
			if !ok || e1 != nil || e2 != nil {
				fmt.Fprintln(errOut, "input needs --key or --click X,Y")
				return 2
			}
			in.X, in.Y = xi, yi
		}
		v, e = t.Input(ctx, *container, in)
	case "stop":
		e = t.Stop(ctx, *container)
		v = map[string]any{"stopped": *container}
	default:
		fmt.Fprintln(errOut, usage)
		return 2
	}
	if e != nil {
		fmt.Fprintln(errOut, args[0]+": "+e.Error())
		// A failed start has already removed every capsule it created (holdingscap.Start).
		return holdingscap.Code(e)
	}
	if json.NewEncoder(out).Encode(v) != nil {
		return 6
	}
	return 0
}
