//go:build mutante_reinicio_total

package main

import (
	"context"
	"fmt"
	"time"

	"opensuperficies.local/srf/src/displaybook/browser"
	"opensuperficies.local/srf/src/dockerapi"
)

func init() {
	mutantRestartContainer = func(ctx context.Context, containerName string) error {
		// Mutator: restarts the entire container instead of only recycling the Xpra layer.
		// As a consequence, Chromium terminates and restarts with a new PID.
		dockerClient, err := dockerapi.NewClient(dockerapi.DefaultSocketPath)
		if err != nil {
			return fmt.Errorf("error creating docker client: %w", err)
		}

		var slot uint16
		_, _ = fmt.Sscanf(containerName, "srf-ng-%d", &slot)

		// Clean residual X11 display locks so restarted Xvfb can bind :100 cleanly
		_ = dockerClient.ExecRun(ctx, containerName, []string{"rm", "-f", "/tmp/.X100-lock", "/tmp/.X11-unix/X100"}, nil, nil)

		// Stop and restart container
		if err := dockerClient.StopContainer(ctx, containerName, 1); err != nil {
			return fmt.Errorf("error stopping container: %w", err)
		}
		if err := dockerClient.StartContainer(ctx, containerName); err != nil {
			return fmt.Errorf("error starting container: %w", err)
		}

		// Wait until Chromium process has spawned inside the restarted container
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			if pid, err := browser.PID(ctx, slot, "chromium"); err == nil && pid > 0 {
				return nil
			}
			time.Sleep(100 * time.Millisecond)
		}

		return fmt.Errorf("timeout waiting for chromium after restart")
	}
}
