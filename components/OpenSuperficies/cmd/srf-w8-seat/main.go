// srf-w8-seat runs only on the dedicated osworker host account.
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/w8seat"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr))
}
func run(ctx context.Context, args []string, out, diag io.Writer) (code int) {
	defer func() {
		if recover() != nil {
			fmt.Fprintln(diag, "indeterminate: seat panic")
			code = 6
		}
	}()
	fs := flag.NewFlagSet("srf-w8-seat", flag.ContinueOnError)
	fs.SetOutput(diag)
	state := fs.String("state-dir", "", "dedicated account W8 state")
	cache := fs.String("cache-manifest", os.Getenv("W8_CACHE_MANIFEST"), "operator warm-job manifest JSON")
	sha := fs.Bool("profile-sha", false, "print embedded seccomp sha256")
	if e := fs.Parse(args); e != nil || fs.NArg() != 0 {
		return 2
	}
	if *sha {
		fmt.Fprintln(out, w8seat.ProfileSHA())
		return 0
	}
	if os.Geteuid() == 0 {
		fmt.Fprintln(diag, "operator_capability: seat must run as osworker")
		return 7
	}
	if *state == "" {
		home, e := os.UserHomeDir()
		if e != nil {
			return 4
		}
		*state = filepath.Join(home, ".local", "state", "w8")
	}
	if *cache == "" {
		fmt.Fprintln(diag, "unavailable: warm-cache manifest AUSENTE; supply --cache-manifest or W8_CACHE_MANIFEST")
		return 4
	}
	c, e := w8seat.LoadConfig(ctx, *state, *cache, w8builder.ExecRunner{})
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8seat.ExitCode(e)
	}
	seat, e := w8seat.NewSeat(ctx, c)
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8seat.ExitCode(e)
	}
	defer seat.Close()
	e = seat.Serve(ctx, w8seat.SocketPath)
	if e != nil {
		fmt.Fprintln(diag, e)
	}
	return w8seat.ExitCode(e)
}
