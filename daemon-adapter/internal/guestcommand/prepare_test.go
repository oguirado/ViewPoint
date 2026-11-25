package guestcommand

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestPrepareRuntimeFitsNativePathBudget(t *testing.T) {
	for _, rewind := range []bool{false, true} {
		t.Run(map[bool]string{false: "normal", true: "rewind"}[rewind], func(t *testing.T) {
			// Same exact-length root allocated by the normal ARM64 session helper.
			root := filepath.Join("/dev/shm", "metaviewer-arm64-"+nonce()[:24]+"-")
			if err := os.Mkdir(root, 0700); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.RemoveAll(root) })
			state, runtime := filepath.Join(root, "state"), filepath.Join(root, "r")
			for _, path := range []string{state, runtime} {
				if err := os.Mkdir(path, 0700); err != nil {
					t.Fatal(err)
				}
			}
			pin := func(name string, mode os.FileMode) Pin {
				path := filepath.Join(root, name)
				data := []byte("synthetic " + name)
				if err := os.WriteFile(path, data, mode); err != nil {
					t.Fatal(err)
				}
				h := sha256.Sum256(data)
				return Pin{Path: path, SHA256: hex.EncodeToString(h[:])}
			}
			m := &Manager{profile: &Profile{OwnerID: "actor-" + strings.Repeat("a", 64),
				StateRoot: state, RuntimeRoot: runtime, Rewind: rewind,
				Python: pin("python", 0700), Runner: pin("runner.py", 0600), ConfigTemplate: json.RawMessage(`{}`)},
				reservation: filepath.Join(state, "reservation.json"), sessionID: "session-test"}
			attempt, _, err := m.prepare()
			if err != nil {
				t.Fatal(err)
			}
			raw, err := os.ReadFile(filepath.Join(attempt, "launch.json"))
			if err != nil {
				t.Fatal(err)
			}
			var launch struct {
				RuntimeRoot string `json:"runtime_root"`
			}
			if err = json.Unmarshal(raw, &launch); err != nil {
				t.Fatal(err)
			}
			suffix, ok := strings.CutPrefix(filepath.Base(launch.RuntimeRoot), "r-")
			if !ok || strings.Trim(suffix, "0123456789") != "" || len(suffix) == 0 || len(suffix) > 10 {
				t.Fatalf("unexpected runtime name: %s", launch.RuntimeRoot)
			}
			longest := filepath.Join(filepath.Dir(launch.RuntimeRoot), "r-4294967295")
			if longest != launch.RuntimeRoot {
				if err := os.Mkdir(longest, 0700); err != nil {
					t.Fatal(err)
				}
			}
			legacy := filepath.Join(root, "runtime", "headless-4294967295")
			if err := os.MkdirAll(legacy, 0700); err != nil {
				t.Fatal(err)
			}
			cmd := exec.Command("python3", "-B", "-c", `
import sys
sys.path.insert(0, "../..")
from guest_command_runtime.native_checks import runtime_root

for path in sys.argv[1:3]:
    runtime_root(path)
try:
    runtime_root(sys.argv[3])
except RuntimeError as error:
    if str(error) != "runtime root too long for Unix sockets":
        raise
else:
    raise AssertionError("native gate admitted the legacy runtime root")
`, launch.RuntimeRoot, longest, legacy)
			if output, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("production native runtime gate: %v\n%s", err, output)
			}
			info, err := os.Stat(launch.RuntimeRoot)
			if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
				t.Fatalf("runtime custody: %v %v", info, err)
			}
		})
	}
}
