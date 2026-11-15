package desktop

import (
	"fmt"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/dockerapi"
)

func checkRB2FramebufferConfig(cfg dockerapi.ContainerConfig) error {
	if cfg.HostConfig == nil {
		return fmt.Errorf("missing HostConfig")
	}
	want := "rw,nosuid,nodev,noexec,mode=0700,uid=10001,size=5656576"
	if got := cfg.HostConfig.Tmpfs["/run/vqa/fb"]; got != want {
		return fmt.Errorf("fb tmpfs: got %q want %q", got, want)
	}
	for _, bind := range cfg.HostConfig.Binds {
		if strings.Contains(bind, "/run/vqa/fb") {
			return fmt.Errorf("framebuffer host bind")
		}
	}
	return nil
}

func TestRB2DesktopFramebufferTmpfs(t *testing.T) {
	cfg := BuildContainerConfig(2, 10022, "srf-net-2")
	if err := checkRB2FramebufferConfig(cfg); err != nil {
		t.Fatal(err)
	}
	delete(cfg.HostConfig.Tmpfs, "/run/vqa/fb")
	if checkRB2FramebufferConfig(cfg) == nil {
		t.Fatal("must reject config without framebuffer tmpfs")
	}
}
