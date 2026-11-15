// srf-w8-builder is an operator-plane CLI. It is never an agent API or binding.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/workerbridge"
)

type services struct {
	lint        func(string) error
	recipeSHA   func(string) (string, error)
	build       func(context.Context, w8builder.BuildConfig) (map[string]any, error)
	checkRecord func([]byte) error
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, 60*time.Minute)
	defer cancel()
	os.Exit(run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}
func run(ctx context.Context, args []string, in io.Reader, out, diag io.Writer) int {
	return runWithServices(ctx, args, in, out, diag, services{
		lint: w8builder.Lint, recipeSHA: w8builder.RecipeSHA, build: w8builder.Build, checkRecord: w8builder.CheckRecord,
	})
}
func invalid(detail string) error {
	return &workerbridge.Reject{Code: 2, Class: "usage", Detail: detail}
}
func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'" }

var approvalID = regexp.MustCompile(`^b2-[0-9]{4}$`)
var operatorNote = regexp.MustCompile(`^[A-Za-z0-9 .,:_-]{1,200}$`)
var digest = regexp.MustCompile(`^[0-9a-f]{64}$`)

// runWithServices returns normalized codes even on a panic; no helper exits.
func runWithServices(ctx context.Context, args []string, in io.Reader, out, diag io.Writer, svc services) (code int) {
	defer func() {
		if recover() != nil {
			fmt.Fprintln(diag, "indeterminate: builder panic; no result published")
			code = 6
		}
	}()
	report := func(err error) int {
		if err == nil {
			return 0
		}
		fmt.Fprintln(diag, err)
		return w8builder.ExitCode(err)
	}
	if len(args) == 0 {
		return report(invalid("expected lint, recipe-sha, approval-cmd, build or record-check"))
	}
	command := args[0]
	fs := flag.NewFlagSet("srf-w8-builder "+command, flag.ContinueOnError)
	fs.SetOutput(diag)
	recipeDir := ".wavemind/env"
	approvalFile := "/etc/opensuperficies/builder_approval.json"
	stateDir := ""
	approval, note, record := "", "", ""
	repoPins := ""
	switch command {
	case "lint", "recipe-sha", "approval-cmd":
		fs.StringVar(&recipeDir, "recipe-dir", recipeDir, "base recipe directory")
		if command == "approval-cmd" {
			fs.StringVar(&approval, "approval-id", "", "single-use b2-NNNN approval id")
			fs.StringVar(&note, "note", "", "operator note")
		}
	case "build":
		fs.StringVar(&repoPins, "repo-pins", "", "host repository pin JSON file (three pinned targets)")
		fs.StringVar(&approvalFile, "approval-file", approvalFile, "operator-owned approval file")
		fs.StringVar(&stateDir, "state-dir", "", "dedicated user's builder state directory")
	case "record-check":
		fs.StringVar(&record, "record", "", "record JSON file, or - for stdin")
	default:
		return report(invalid("unknown subcommand"))
	}
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	switch command {
	case "lint", "recipe-sha":
		if fs.NArg() > 1 {
			return report(invalid("at most one recipe directory"))
		}
		if fs.NArg() == 1 {
			recipeDir = fs.Arg(0)
		}
		if err := svc.lint(recipeDir); err != nil {
			return report(err)
		}
		if command == "lint" {
			return 0
		}
		sha, err := svc.recipeSHA(recipeDir)
		if err != nil {
			return report(err)
		}
		if !digest.MatchString(sha) {
			return report(invalid("recipe digest is not lowercase sha256"))
		}
		_, err = fmt.Fprintln(out, sha)
		return report(err)
	case "approval-cmd":
		if fs.NArg() != 0 || !approvalID.MatchString(approval) || !operatorNote.MatchString(note) {
			return report(invalid("approval requires --approval-id b2-NNNN and an ASCII operator --note"))
		}
		if err := svc.lint(recipeDir); err != nil {
			return report(err)
		}
		sha, err := svc.recipeSHA(recipeDir)
		if err != nil {
			return report(err)
		}
		if !digest.MatchString(sha) {
			return report(invalid("recipe digest is not lowercase sha256"))
		}
		doc := struct {
			Schema     string `json:"schema"`
			Mode       string `json:"mode"`
			Image      string `json:"image"`
			Arch       string `json:"arch"`
			RecipeSHA  string `json:"recipe_sha256"`
			ApprovalID string `json:"approval_id"`
			Note       string `json:"operator_note"`
		}{"os.cu.builder-approval/v1", "per_build", "os-worker-base", "aarch64", sha, approval, note}
		data, err := json.Marshal(doc)
		if err != nil {
			return report(err)
		}
		inner := "printf '%s\\n' " + shellQuote(string(data)) + " | sudo install -D -m 0644 -o root -g root /dev/stdin /etc/opensuperficies/builder_approval.json; rc=$?; [ \"$rc\" -eq 1 ] && rc=5; exit \"$rc\""
		_, err = fmt.Fprintf(out, "recipe_sha256=%s\nsh -c %s\n", sha, shellQuote(inner))
		return report(err)
	case "build":
		if fs.NArg() != 1 || fs.Arg(0) != "-" {
			return report(invalid("build requires one tar context on stdin: build -"))
		}
		if approvalFile == "" {
			return report(invalid("approval file path must not be empty"))
		}
		if stateDir == "" {
			home, err := os.UserHomeDir()
			if err != nil || home == "" {
				return report(invalid("dedicated user's home unavailable"))
			}
			stateDir = filepath.Join(home, ".local", "state", "w8")
		}
		var repositories []w8builder.RepositoryPin
		if repoPins != "" {
			f, err := os.Open(repoPins)
			if err != nil {
				return report(invalid("repository pin data unavailable"))
			}
			data, err := io.ReadAll(io.LimitReader(f, (64<<10)+1))
			_ = f.Close()
			if err != nil {
				return report(invalid("repository pin data read failed"))
			}
			repositories, err = w8builder.DecodeRepositoryPins(data)
			if err != nil {
				return report(err)
			}
		}
		result, err := svc.build(ctx, w8builder.BuildConfig{Repositories: repositories, ApprovalPath: approvalFile, StateDir: stateDir, Context: in, Runner: w8builder.ExecRunner{}})
		if err != nil {
			return report(err)
		}
		return report(json.NewEncoder(out).Encode(result))
	case "record-check":
		if fs.NArg() > 1 || (record != "" && fs.NArg() != 0) {
			return report(invalid("record-check takes one record file or -"))
		}
		if fs.NArg() == 1 {
			record = fs.Arg(0)
		}
		if record == "" {
			return report(invalid("record-check needs a file or -"))
		}
		var source io.Reader = in
		if record != "-" {
			file, err := os.Open(record)
			if err != nil {
				return report(invalid("record file unavailable"))
			}
			defer file.Close()
			source = file
		}
		data, err := io.ReadAll(io.LimitReader(source, 65537))
		if err != nil {
			return report(err)
		}
		if len(data) > 65536 {
			return report(invalid("record exceeds 64 KiB"))
		}
		return report(svc.checkRecord(data))
	}
	return 6
}
