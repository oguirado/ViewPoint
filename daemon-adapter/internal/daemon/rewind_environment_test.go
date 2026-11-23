package daemon

import (
	"os"
	"strings"
	"testing"
)

func TestRewindModeRequiresExplicitExclusiveAdmission(t *testing.T) {
	const enabled = "MV_REWIND_ENABLED"
	const only = "MV_REWIND_ONLY"
	const guest = "MV_GUEST_COMMAND_ONLY"
	const headless = "MV_HEADLESS_ONLY"
	cases := []struct {
		name string
		env  map[string]string
	}{
		{"exclusive", map[string]string{enabled: "1", only: "1"}},
		{"missing_only", map[string]string{enabled: "1"}},
		{"missing_enabled", map[string]string{only: "1"}},
		{"invalid_enabled", map[string]string{enabled: "true", only: "1"}},
		{"invalid_only", map[string]string{enabled: "1", only: "0"}},
		{"empty_enabled", map[string]string{enabled: "", only: "1"}},
		{"empty_only", map[string]string{enabled: "1", only: ""}},
		{"conflicting_guest", map[string]string{enabled: "1", only: "1", guest: "1"}},
		{"conflicting_headless", map[string]string{enabled: "1", only: "1", headless: "1"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bootstrapEnvironment(t)
			for _, key := range []string{enabled, only, guest, headless} {
				t.Setenv(key, "")
				if err := os.Unsetenv(key); err != nil {
					t.Fatal(err)
				}
			}
			for key, value := range tc.env {
				t.Setenv(key, value)
			}
			loads := 0
			r, err := runtimeFromEnv(func() (guestCommandOwner, error) {
				loads++
				return &commandBootstrapOwner{root: t.TempDir()}, nil
			})
			if tc.name == "exclusive" {
				if err != nil {
					t.Fatal(err)
				}
				defer r.Close()
				if loads != 1 || !r.Client || !r.Streaming {
					t.Fatalf("expected exclusive client runtime, got %d loader calls", loads)
				}
			} else {
				if err == nil || !strings.Contains(err.Error(), "exclusive rewind requires") || loads != 0 {
					t.Fatalf("expected configuration refusal before loading rewind, got err=%v loads=%d", err, loads)
				}
			}
		})
	}
}
