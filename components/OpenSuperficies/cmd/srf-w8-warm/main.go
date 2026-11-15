// srf-w8-warm is an explicitly operator-approved host warm-cache job.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"opensuperficies.local/srf/src/w8builder"
	"opensuperficies.local/srf/src/w8warm"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, out, diag io.Writer) (code int) {
	defer func() {
		if recover() != nil {
			fmt.Fprintln(diag, "indeterminate: warm panic")
			code = 6
		}
	}()
	fs := flag.NewFlagSet("srf-w8-warm", flag.ContinueOnError)
	fs.SetOutput(diag)
	recipePath := fs.String("recipe", "", "os.cu.warm-manifest/v1 recipe")
	ledger := fs.String("ledger", "", "base_image_records.jsonl path")
	rulesPath := fs.String("rules", "", "operator-applied host rules record path")
	target := fs.String("out", "", "seat cache-manifest.json destination")
	dry := fs.Bool("dry", false, "print the immutable argv plan only")
	approval := fs.Bool("approve", false, "explicit operator authorisation for this warm execution")
	if err := fs.Parse(args); err != nil || fs.NArg() != 0 {
		return 2
	}
	if *recipePath == "" || *ledger == "" || *rulesPath == "" || *target == "" {
		fmt.Fprintln(diag, "input: --recipe, --ledger, --rules and --out are required")
		return 2
	}
	read := func(p string, max int) ([]byte, error) {
		f, e := os.Open(p)
		if e != nil {
			return nil, e
		}
		defer f.Close()
		st, e := f.Stat()
		if e != nil || !st.Mode().IsRegular() || st.Mode().Perm()&0022 != 0 {
			return nil, fmt.Errorf("unsafe input file")
		}
		b, e := io.ReadAll(io.LimitReader(f, int64(max)+1))
		if e != nil || len(b) > max {
			return nil, fmt.Errorf("input cap/read")
		}
		return b, nil
	}
	recipeBytes, e := read(*recipePath, 1<<20)
	if e != nil {
		fmt.Fprintln(diag, "unavailable: recipe not readable")
		return 4
	}
	r, e := w8warm.ParseRecipe(recipeBytes)
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	ledgerBytes, e := read(*ledger, 4<<20)
	if e != nil {
		fmt.Fprintln(diag, "unavailable: approved base ledger not readable")
		return 4
	}
	base, e := w8warm.BaseFromLedger(ledgerBytes)
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	rulesBytes, e := read(*rulesPath, 4096)
	if e != nil {
		fmt.Fprintln(diag, "unavailable: operator host-rules record not readable")
		return 4
	}
	rules, e := w8warm.ParseRules(rulesBytes)
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	// No persistent approval is inferred from a recipe, record or environment.
	if !*approval && !*dry {
		fmt.Fprintln(diag, "operator_capability: require --approve")
		return 7
	}
	base.Approved = *approval || *dry
	plan, e := w8warm.Plan(r, base, rules)
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	if *dry {
		b, e := json.MarshalIndent(map[string]any{"fill": plan.Fill, "measure": plan.Measure}, "", "  ")
		if e != nil {
			return 5
		}
		fmt.Fprintln(out, string(b))
		return 0
	}
	if os.Geteuid() == 0 {
		fmt.Fprintln(diag, "operator_capability: warm job refuses root")
		return 7
	}
	if e := w8warm.CheckOutputPath(*target); e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	listing, sha, e := w8warm.Run(ctx, w8builder.ExecRunner{}, plan)
	if e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	// A successful, nonempty measured listing alone permits publication.
	if len(listing) == 0 || sha == "" {
		return 6
	}
	if e = w8warm.WriteSeatManifest(*target, sha); e != nil {
		fmt.Fprintln(diag, e)
		return w8warm.ExitCode(e)
	}
	fmt.Fprintln(out, "manifest_sha256="+sha)
	return 0
}
