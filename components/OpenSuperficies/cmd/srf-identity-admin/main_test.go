package main

import (
	"bytes"
	"path/filepath"
	"strings"
	"testing"
)

func TestCreateThenShow(t *testing.T) {
	root := filepath.Join(t.TempDir(), "store")
	var out, errOut bytes.Buffer
	if rc := run([]string{"create", "--store-root", root, "--id", "perplexity-1", "--label", "Perplexity main", "--domains", "perplexity.ai"}, &out, &errOut); rc != 0 {
		t.Fatal(rc, errOut.String())
	}
	if !strings.Contains(out.String(), `"profile_id":"perplexity-1"`) {
		t.Fatal(out.String())
	}
	if rc := run([]string{"create", "--store-root", root, "--id", "perplexity-1", "--label", "x", "--domains", "perplexity.ai"}, &out, &errOut); rc != 4 {
		t.Fatal("duplicate identity must be refused", rc)
	}
	if rc := run([]string{"show", "--store-root", root, "--id", "absent-1"}, &out, &errOut); rc != 4 {
		t.Fatal("absent identity", rc)
	}
	if rc := run([]string{"delete"}, &out, &errOut); rc != 2 {
		t.Fatal("unknown verb", rc)
	}
}
