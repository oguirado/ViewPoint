package patchpolicy

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

func TestSharedCases(t *testing.T) {
	b, e := os.ReadFile("../../../testdata/w8seat/patchpolicy_cases.json")
	if e != nil {
		t.Fatal(e)
	}
	var cases []struct {
		Name       string   `json:"name"`
		Diff       string   `json:"diff"`
		WritePaths []string `json:"write_paths"`
		Accept     bool     `json:"accept"`
	}
	if e = json.Unmarshal(b, &cases); e != nil {
		t.Fatal(e)
	}
	for _, c := range cases {
		t.Run(c.Name, func(t *testing.T) {
			f, e := Check([]byte(c.Diff), c.WritePaths)
			if (e == nil) != c.Accept {
				t.Fatalf("accept=%v got %v", c.Accept, e)
			}
			if c.Accept && len(f) == 0 {
				t.Fatal("empty change set")
			}
		})
	}
}

func TestDeltaPatchWriteSurfaceAndModeEnforcement(t *testing.T) {
	diff := []byte("diff --git a/src/a.go b/src/a.go\nnew file mode 100755\n--- /dev/null\n+++ b/src/a.go\n@@ -0,0 +1 @@\n+x\n")
	c, e := Check(diff, []string{"src/a.go"})
	if e != nil || len(c) != 1 || c[0].Mode != "100755" || c[0].Operation != "add" {
		t.Fatal(c, e)
	}
	if _, e = Check(diff, []string{"src/b.go"}); e == nil {
		t.Fatal("surface escaped")
	}
	if _, e = Check(bytes.Replace(diff, []byte("100755"), []byte("120000"), 1), []string{"src/a.go"}); e == nil {
		t.Fatal("symlink accepted")
	}
}
