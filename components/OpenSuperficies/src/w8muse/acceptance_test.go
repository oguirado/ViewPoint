package w8muse

import (
	"context"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"testing"
	"time"
)

// Parse only: never execute the conductor's operator/Docker commands. sh -n
// alone cannot detect syntax errors in embedded Python acceptance oracles.
func TestAcceptanceEmbeddedPythonParses(t *testing.T) {
	b, e := os.ReadFile("../../scripts/w8/b3_live_acceptance.sh")
	if e != nil {
		t.Fatal(e)
	}
	source := string(b)
	var snippets []string
	for _, m := range regexp.MustCompile(`(?m)<<'(PY|PYLOCAL)'\n`).FindAllStringSubmatchIndex(source, -1) {
		tag := source[m[2]:m[3]]
		rest := source[m[1]:]
		end := strings.Index(rest, "\n"+tag+"\n")
		if end < 0 {
			t.Fatal("unterminated Python heredoc")
		}
		snippets = append(snippets, rest[:end])
	}
	for _, m := range regexp.MustCompile(`(?s)python3 -c '([^']*)'`).FindAllStringSubmatch(source, -1) {
		snippets = append(snippets, m[1])
	}
	if len(snippets) < 5 {
		t.Fatal("acceptance oracles absent")
	}
	for _, snippet := range snippets {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		cmd := exec.CommandContext(ctx, "python3", "-c", "import ast,sys; ast.parse(sys.stdin.read())")
		cmd.Stdin = strings.NewReader(snippet)
		cmd.Env = append(os.Environ(), "PYTHONDONTWRITEBYTECODE=1")
		out, e := cmd.CombinedOutput()
		cancel()
		if e != nil {
			t.Fatalf("Python parse: %v %s", e, out)
		}
	}
}
