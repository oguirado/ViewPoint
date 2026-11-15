package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"opensuperficies.local/srf/src/w8builder"
)

func TestCLIRealRecipeLintAndApproval(t *testing.T) {
	dir := t.TempDir()
	pins, err := os.ReadFile(filepath.Join("..", "..", ".wavemind", "env", "pins.json"))
	if err != nil {
		t.Fatal(err)
	}
	jules, err := w8builder.JulesSetup(pins)
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"pins.json": pins, "jules-setup.sh": jules,
		"base/Dockerfile": nil,
		"base/assert.sh":  []byte("#!/bin/sh\nexit 0\n"),
	}
	dockerfile, err := os.ReadFile("../../.wavemind/env/base/Dockerfile")
	if err != nil {
		t.Fatal(err)
	}
	files["base/Dockerfile"] = dockerfile
	for _, name := range []string{"assert.sh", "w8runner/main.go", "w8runner/process.go", "w8runner/probes.go", "w8runner/text.go", "w8runner/extract.go"} {
		data, e := os.ReadFile(filepath.Join("../../.wavemind/env/base", name))
		if e != nil {
			t.Fatal(e)
		}
		files["base/"+name] = data
	}
	for _, name := range []string{"opensuperficies", "vision-engine", "wender-agent", "godot", "blender"} {
		files["warm/"+name+".json"] = []byte(`{"offline_env":{"GOTOOLCHAIN":"local"}}`)
	}
	flush := func() {
		t.Helper()
		keys := make([]string, 0, len(files))
		for name := range files {
			keys = append(keys, name)
		}
		sort.Strings(keys)
		var manifest strings.Builder
		for _, name := range keys {
			dst := filepath.Join(dir, filepath.FromSlash(name))
			if err := os.MkdirAll(filepath.Dir(dst), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(dst, files[name], 0600); err != nil {
				t.Fatal(err)
			}
			h := sha256.Sum256(files[name])
			fmt.Fprintf(&manifest, "%s  %s\n", hex.EncodeToString(h[:]), name)
		}
		if err := os.WriteFile(filepath.Join(dir, "base", "recipe.manifest"), []byte(manifest.String()), 0600); err != nil {
			t.Fatal(err)
		}
	}
	flush()
	invokeReal := func(args ...string) (int, string) {
		var out, diag bytes.Buffer
		rc := run(context.Background(), args, strings.NewReader(""), &out, &diag)
		if rc == 1 {
			t.Fatal("exit 1")
		}
		if rc != 0 {
			t.Logf("CLI diagnostics: %s", diag.String())
		}
		return rc, out.String()
	}
	if rc, _ := invokeReal("lint", "--recipe-dir", dir); rc != 0 {
		t.Fatalf("lint rc=%d", rc)
	}
	rc, sha := invokeReal("recipe-sha", dir)
	if rc != 0 || !digest.MatchString(strings.TrimSpace(sha)) {
		t.Fatalf("sha rc=%d %q", rc, sha)
	}
	rc, printed := invokeReal("approval-cmd", "--recipe-dir", dir, "--approval-id", "b2-0001", "--note", "Fixture only")
	if rc != 0 || !strings.HasPrefix(printed, "recipe_sha256="+sha) {
		t.Fatalf("approval rc=%d %q", rc, printed)
	}
	files["warm/opensuperficies.json"] = []byte(`{"offline_env":{"GOTOOLCHAIN":"local","GOPROXY":"off"}}`)
	flush()
	if rc, _ := invokeReal("lint", dir); rc != 2 {
		t.Fatalf("unknown env rc=%d", rc)
	}
	files["warm/opensuperficies.json"] = []byte(`{"offline_env":{"GOTOOLCHAIN":"local"}}`)
	placeholder, err := os.ReadFile(filepath.Join("..", "..", "testdata", "w8builder", "placeholder_pins.json"))
	if err != nil {
		t.Fatal(err)
	}
	files["pins.json"] = placeholder
	flush()
	if rc, out := invokeReal("approval-cmd", "--recipe-dir", dir, "--approval-id", "b2-0001", "--note", "Fixture only"); rc != 2 || out != "" {
		t.Fatalf("placeholder rc=%d %q", rc, out)
	}
}
