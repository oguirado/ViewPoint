package xpra

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"time"

	"opensuperficies.local/srf/src/dockerapi"
)

// ConmutacionResult details the outcome of an Xpra readonly/operator switch operation.
type ConmutacionResult struct {
	Slot       uint16 `json:"slot"`
	Contenedor string `json:"contenedor"`
	ReadOnly   bool   `json:"readonly"`
	MS         int64  `json:"ms"`
	Estado     string `json:"estado"`
}

// ConmutarOptions configures options for switching the Xpra operating mode.
type ConmutarOptions struct {
	DockerSocket string
	Timeout      time.Duration
}

// Intervenir transitions the container's Xpra layer to operator mode (readonly=false).
func Intervenir(ctx context.Context, slot uint16) (*ConmutacionResult, error) {
	return Conmutar(ctx, slot, false)
}

// Blindar transitions the container's Xpra layer to read-only mode (readonly=true).
func Blindar(ctx context.Context, slot uint16) (*ConmutacionResult, error) {
	return Conmutar(ctx, slot, true)
}

// Conmutar switches the Xpra layer between read-only and operator mode.
func Conmutar(ctx context.Context, slot uint16, readOnly bool) (*ConmutacionResult, error) {
	return ConmutarWithOptions(ctx, slot, readOnly, ConmutarOptions{})
}

// ConmutarWithOptions executes the mode switch through Docker Engine API exec on the frozen xpra-layer.sh.
func ConmutarWithOptions(ctx context.Context, slot uint16, readOnly bool, opts ConmutarOptions) (*ConmutacionResult, error) {
	if opts.DockerSocket == "" {
		opts.DockerSocket = dockerapi.DefaultSocketPath
	}
	if opts.Timeout <= 0 {
		opts.Timeout = 10 * time.Second
	}

	containerName := fmt.Sprintf("srf-ng-%d", slot)

	client, err := dockerapi.NewClient(opts.DockerSocket)
	if err != nil {
		return nil, fmt.Errorf("xpra switch: error connecting to docker: %w", err)
	}

	arg := "no"
	if readOnly {
		arg = "yes"
	}

	// Optimization & A2-04 fix: pre-terminate Xpra process with bounded wait and escalation.
	// 1. Verify that the pid in /tmp/xpra-layer.pid actually belongs to an xpra process before signalling.
	// 2. Send SIGTERM and fast-poll (0.01s) with a bound (up to 350ms).
	// 3. If the process is deaf to SIGTERM, escalate to SIGKILL (up to 150ms) so it terminates cleanly
	//    rather than spinning until the 10s exec context expires.
	// 4. Remove /tmp/xpra-layer.pid and invoke xpra-layer.sh readonly <arg>.
	cmd := []string{
		"sh", "-c",
		fmt.Sprintf("P=$(cat /tmp/xpra-layer.pid 2>/dev/null || true); if [ -n \"$P\" ] && [ -d \"/proc/$P\" ]; then CMD=$(tr '\\0' ' ' < \"/proc/$P/cmdline\" 2>/dev/null || true); case \"$CMD\" in *xpra*) kill -TERM \"$P\" 2>/dev/null || true; for _ in $(seq 1 35); do kill -0 \"$P\" 2>/dev/null || break; sleep 0.01; done; if kill -0 \"$P\" 2>/dev/null; then kill -KILL \"$P\" 2>/dev/null || true; for _ in $(seq 1 15); do kill -0 \"$P\" 2>/dev/null || break; sleep 0.01; done; fi ;; esac; fi; rm -f /tmp/xpra-layer.pid; /usr/local/bin/xpra-layer.sh readonly %s", arg),
	}

	ctxTimeout, cancel := context.WithTimeout(ctx, opts.Timeout)
	defer cancel()

	// U09-F3 & A2-05 fix: measure FULL span from just before CreateExec to after switch is complete
	wallStart := time.Now()

	var stdout, stderr bytes.Buffer
	execID, err := client.CreateExec(ctxTimeout, containerName, dockerapi.ExecConfig{
		AttachStdout: true,
		AttachStderr: true,
		Cmd:          cmd,
		Env: []string{
			"DISPLAY_NUM=100",
			"XPRA_BIND_PORT=10000",
			"DISPLAY=:100",
			"HOME=/home/tenant",
			"XDG_RUNTIME_DIR=/run/newton-tenant",
		},
	})
	if err != nil {
		return nil, fmt.Errorf("xpra switch: create exec failed: %w", err)
	}

	conn, reader, err := client.StartExecHijack(ctxTimeout, execID)
	if err != nil {
		return nil, fmt.Errorf("xpra switch: start exec hijack failed: %w", err)
	}

	demuxErr := dockerapi.Demux(reader, &stdout, &stderr)
	_ = conn.Close()

	if demuxErr != nil {
		return nil, fmt.Errorf("xpra switch: demux error: %w", demuxErr)
	}

	insp, err := client.InspectExec(ctxTimeout, execID)
	if err != nil {
		return nil, fmt.Errorf("xpra switch: inspect exec failed: %w", err)
	}

	if insp.ExitCode != 0 {
		return nil, fmt.Errorf("xpra switch: exec returned code %d (stdout: %s, stderr: %s)",
			insp.ExitCode, stdout.String(), stderr.String())
	}

	// U09-F2 & A2-03 fix: report genuine read-back state from container, not input argument.
	// Estado is derived strictly from actualRO so ReadOnly and Estado can never contradict.
	actualRO, err := estadoWithClient(ctxTimeout, client, containerName)
	if err != nil {
		return nil, fmt.Errorf("xpra switch: read-back estado failed: %w", err)
	}

	estado := "intervened"
	if actualRO {
		estado = "shielded"
	}

	// Full span measurement in milliseconds
	elapsed := time.Since(wallStart).Milliseconds()

	return &ConmutacionResult{
		Slot:       slot,
		Contenedor: containerName,
		ReadOnly:   actualRO,
		MS:         elapsed,
		Estado:     estado,
	}, nil
}

// Estado determines the current read-only state of the Xpra layer inside the container.
func Estado(ctx context.Context, slot uint16) (bool, error) {
	return EstadoWithOptions(ctx, slot, ConmutarOptions{})
}

// EstadoWithOptions determines the current read-only state with custom options.
func EstadoWithOptions(ctx context.Context, slot uint16, opts ConmutarOptions) (bool, error) {
	if opts.DockerSocket == "" {
		opts.DockerSocket = dockerapi.DefaultSocketPath
	}
	containerName := fmt.Sprintf("srf-ng-%d", slot)
	client, err := dockerapi.NewClient(opts.DockerSocket)
	if err != nil {
		return false, err
	}
	return estadoWithClient(ctx, client, containerName)
}

func estadoWithClient(ctx context.Context, client *dockerapi.Client, containerName string) (bool, error) {
	var stdout, stderr bytes.Buffer
	cmd := []string{
		"sh", "-c",
		`if [ -f /tmp/xpra-layer.pid ]; then
  P=$(cat /tmp/xpra-layer.pid 2>/dev/null || true)
  if [ -n "$P" ] && [ -d "/proc/$P" ]; then
    CMD=$(tr '\0' ' ' < "/proc/$P/cmdline" 2>/dev/null || true)
    case "$CMD" in
      *xpra*) echo "PIDFILE:$CMD"; exit 0 ;;
    esac
  fi
fi
pgrep -a -f '[x]pra start-desktop' || true`,
	}
	if err := client.ExecRun(ctx, containerName, cmd, &stdout, &stderr); err != nil {
		return false, err
	}
	return parseXpraEstado(stdout.String())
}

func parseXpraEstado(out string) (bool, error) {
	lines := strings.Split(out, "\n")
	for _, l := range lines {
		trimmed := strings.TrimSpace(l)
		if strings.HasPrefix(trimmed, "PIDFILE:") {
			cmdline := strings.TrimSpace(strings.TrimPrefix(trimmed, "PIDFILE:"))
			hasYes := strings.Contains(cmdline, "--readonly=yes")
			hasNo := strings.Contains(cmdline, "--readonly=no")
			if hasYes && !hasNo {
				return true, nil
			}
			if hasNo && !hasYes {
				return false, nil
			}
			return false, fmt.Errorf("xpra estado: pidfile process cmdline ambiguous or missing readonly flag: %q", cmdline)
		}
	}

	var candidates []string
	for _, l := range lines {
		trimmed := strings.TrimSpace(l)
		if trimmed == "" || strings.Contains(trimmed, "pgrep") {
			continue
		}
		if strings.Contains(trimmed, "xpra") && strings.Contains(trimmed, "start-desktop") {
			candidates = append(candidates, trimmed)
		}
	}

	if len(candidates) == 0 {
		return false, fmt.Errorf("xpra estado: no xpra start-desktop process found in output: %q", strings.TrimSpace(out))
	}
	if len(candidates) > 1 {
		return false, fmt.Errorf("xpra estado: multiple xpra start-desktop candidates found (%d)", len(candidates))
	}

	cand := candidates[0]
	hasYes := strings.Contains(cand, "--readonly=yes")
	hasNo := strings.Contains(cand, "--readonly=no")
	if hasYes && !hasNo {
		return true, nil
	}
	if hasNo && !hasYes {
		return false, nil
	}
	return false, fmt.Errorf("xpra estado: unable to determine readonly state from cmdline: %q", cand)
}
