// srf-vm-entry is the static container PID 1 and its exact-path relay shims.
package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"opensuperficies.local/srf/src/vmsurface/helpers"
	"opensuperficies.local/srf/src/vmsurface/proto"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

func run(ctx context.Context, name string, args []string) int {
	switch name {
	case "srf-vm-entry", "identity-entrypoint.sh":
		if len(args) == 0 {
			env := map[string]string{}
			for _, s := range os.Environ() {
				k, v, _ := strings.Cut(s, "=")
				env[k] = v
			}
			if e := boot(ctx, env, os.Stdout); e != nil {
				fmt.Fprintln(os.Stderr, e)
				var p *proto.Error
				if errors.As(e, &p) {
					return p.Code
				}
				return 3
			}
			return 0
		}
		if name == "srf-vm-entry" || len(args) != 1 || (args[0] != "xpra-start" && args[0] != "xpra-stop") {
			return 2
		}
	case "xdotool", "python3", "srf-fbread":
	default:
		return 2
	}
	argv := append([]string{name}, args...)
	o := proto.Open{Argv: argv, Env: proto.ForwardEnv(os.Environ())}
	if name == "python3" && len(args) >= 3 && args[0] == "-I" && args[1] == "-u" {
		o.StdinCap = -1
	}
	deadline, e := proto.Allowed(o, helpers.Hashes())
	if e != nil {
		fmt.Fprintln(os.Stderr, e)
		var p *proto.Error
		if errors.As(e, &p) {
			return p.Code
		}
		return 2
	}
	if deadline > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, shimDeadline(deadline))
		defer cancel()
	}
	c, e := (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", "/run/newton-tenant/vm-agent.sock")
	if e != nil {
		return 6
	}
	return client(ctx, c, o, os.Stdin, os.Stdout, os.Stderr)
}

// shimDeadline reserves a second for the agent to report its policy timeout.
// Policy entries must leave this deadline strictly inside the surface wrapper.
// A zero policy is the cancellable record stream and has no time deadline.
func shimDeadline(policyMS int64) time.Duration {
	if policyMS == 0 {
		return 0
	}
	return time.Duration(policyMS)*time.Millisecond + time.Second
}

func main() {
	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	os.Exit(run(ctx, filepath.Base(os.Args[0]), os.Args[1:]))
}
