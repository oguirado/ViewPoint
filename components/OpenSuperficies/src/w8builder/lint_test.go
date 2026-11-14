package w8builder

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestLintLeakMultiArgFlagsJsonArrayAndContinuations(t *testing.T) {
	for _, src := range []string{
		"COPY ~/.config/x /x", "COPY ~/.ssh/x /x", "COPY $HOME/x /x", "COPY operator.token /x", "ADD ../x /x",
		"COPY safe /etc/passwd /x", "COPY --chown=10001:10001 ../../src /x", `COPY ["~/.ssh/id_ed25519","/x"]`, "COPY \\\n ~/.config/gh/hosts.yml /x",
	} {
		if rc := ExitCode(LintDockerfile([]byte("FROM debian@sha256:" + strings.Repeat("a", 64) + "\n" + src))); rc != 5 {
			t.Errorf("%q rc=%d", src, rc)
		}
	}
}
func TestWarmManifestRejectsUnknownEnvironment(t *testing.T) {
	if rc := ExitCode(LintWarm([]byte(`{"offline_env":{"GOPROXY":"off"}}`))); rc != 2 {
		t.Fatalf("GOPROXY rc=%d", rc)
	}
	if err := LintWarm([]byte(`{"offline_env":{"GOTOOLCHAIN":"local","GOFLAGS":"-mod=mod"}}`)); err != nil {
		t.Fatal(err)
	}
}

func TestRecipeManifestRefusesUnlistedFilesAndJulesDrift(t *testing.T) {
	f, e := loadDir("../../.wavemind/env")
	if e != nil {
		t.Fatal(e)
	}
	if e = lintRecipe(f); e != nil {
		t.Fatal(e)
	}
	f["extra.txt"] = []byte("unlisted fixture")
	if ExitCode(lintRecipe(f)) != 2 {
		t.Fatal("unlisted file accepted")
	}
	delete(f, "extra.txt")
	f["jules-setup.sh"] = append(f["jules-setup.sh"], []byte("# drift\n")...)
	if ExitCode(lintRecipe(f)) != 2 {
		t.Fatal("Jules drift accepted")
	}
}

func TestWarmEnumMatchesFrozenContract(t *testing.T) {
	data, e := os.ReadFile("../../contracts/worker/os.cu.worker-task.v1.schema.json")
	if e != nil {
		t.Fatal(e)
	}
	var schema map[string]any
	if e = json.Unmarshal(data, &schema); e != nil {
		t.Fatal(e)
	}
	defs := schema["$defs"].(map[string]any)
	hardening := defs["hardening"].(map[string]any)
	props := hardening["properties"].(map[string]any)
	names := props["env_names"].(map[string]any)["items"].(map[string]any)["enum"].([]any)
	if len(names) != len(envNames) {
		t.Fatal("closed enum drift")
	}
	for _, n := range names {
		if !envNames[n.(string)] {
			t.Fatalf("enum drift: %s", n)
		}
	}
}

func TestLintB3RunnerCopyAllowlist(t *testing.T) {
	prefix := "FROM debian@sha256:" + strings.Repeat("a", 64) + " AS fetch\n"
	if e := LintDockerfile([]byte(prefix + "COPY base/w8runner/ /runner/\n")); e != nil {
		t.Fatal(e)
	}
	for _, mutant := range []string{
		"FROM debian@sha256:" + strings.Repeat("a", 64) + " AS runtime\nCOPY base/w8runner/ /runner/\n",
		"COPY --from=runner opt/w8 /opt/w8\n", "COPY base/w8runner/ /opt/w8/evil\n",
	} {
		if ExitCode(LintDockerfile([]byte(prefix+mutant))) != 5 {
			t.Fatalf("accepted %s", mutant)
		}
	}
}

func TestLintB3RunnerDestinationCannotWiden(t *testing.T) {
	prefix := "FROM debian@sha256:" + strings.Repeat("a", 64) + " AS runtime\n"
	for _, line := range []string{
		"COPY --from=fetch opt/w8/evil /opt/w8/evil\n",
		"COPY --from=fetch opt/w8 /opt/w8/evil\n",
		"COPY base/assert.sh /opt/w8/evil\n",
	} {
		if ExitCode(LintDockerfile([]byte(prefix+line))) != 5 {
			t.Errorf("accepted %s", line)
		}
	}
}

func TestB3RecipeSHA(t *testing.T) {
	b, e := os.ReadFile("../../.wavemind/env/pins.json")
	if e != nil {
		t.Fatal(e)
	}
	pins, e := parseObject(b)
	if e != nil {
		t.Fatal(e)
	}
	sources, ok := pins["runner_sources"].(map[string]any)
	if !ok || len(sources) != 5 {
		t.Fatal("B3 runner sources absent from recipe pins")
	}
	sha, e := RecipeSHA("../../.wavemind/env")
	if e != nil {
		t.Fatal(e)
	}
	t.Logf("srf-w8-builder recipe-sha .wavemind/env => %s", sha)
}

func TestRuntimeEpochDoesNotPersistInImageEnv(t *testing.T) {
	b, e := os.ReadFile("../../.wavemind/env/base/Dockerfile")
	if e != nil {
		t.Fatal(e)
	}
	stage := ""
	for _, line := range strings.Split(string(b), "\n") {
		if strings.HasPrefix(line, "FROM ") {
			f := strings.Fields(line)
			stage = f[len(f)-1]
		}
		if stage == "runtime" && strings.HasPrefix(line, "ENV SOURCE_DATE_EPOCH=") {
			t.Fatal("build-only epoch retained in runtime Config.Env")
		}
	}
}

func TestRecipePATHCannotBeWidened(t *testing.T) {
	files, e := loadDir("../../.wavemind/env")
	if e != nil {
		t.Fatal(e)
	}
	for _, line := range []string{"ENV PATH=/tmp:/usr/bin", "ENV UNKNOWN=value", ""} {
		changed := recipe{}
		for k, v := range files {
			changed[k] = v
		}
		changed["base/Dockerfile"] = []byte(strings.Replace(string(files["base/Dockerfile"]), "ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", line, 1))
		// Check this recipe invariant directly so stale manifest/source pins cannot
		// hide a missing PATH check behind an unrelated refusal.
		if e := lintImagePATH(changed["base/Dockerfile"]); ExitCode(e) != 5 {
			t.Fatalf("%q rc=%d %v", line, ExitCode(e), e)
		}
	}
	if e := lintImagePATH(files["base/Dockerfile"]); e != nil {
		t.Fatal(e)
	}
}
