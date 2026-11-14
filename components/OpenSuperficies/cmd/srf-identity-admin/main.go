// srf-identity-admin is the OPERATOR's tool to create a named browser identity in the private
// store (src/browserid/store). AIs read identities through srf-identity-surface; only the
// operator creates them (rulings 2026-10-22). It never opens profile payloads.
//
//	srf-identity-admin create --store-root DIR --id perplexity-1 --label "Perplexity main" --domains perplexity.ai
//	srf-identity-admin show   --store-root DIR --id perplexity-1
//
// Exit codes: 0 ok, 2 usage, 4 store refused or identity absent.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"

	"opensuperficies.local/srf/src/browserid/store"
)

func main() { os.Exit(run(os.Args[1:], os.Stdout, os.Stderr)) }

func run(args []string, out, errOut io.Writer) int {
	if len(args) < 1 || (args[0] != "create" && args[0] != "show") {
		fmt.Fprintln(errOut, "usage: srf-identity-admin create|show --store-root DIR --id ID [--label L --domains a,b --browser chromium]")
		return 2
	}
	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	fs.SetOutput(errOut)
	root := fs.String("store-root", os.Getenv("SRF_IDENTITY_STORE_ROOT"), "identity store root")
	id := fs.String("id", "", "profile id")
	label := fs.String("label", "", "account label")
	domains := fs.String("domains", "", "comma-separated registrable domains")
	browser := fs.String("browser", "chromium", "chromium or firefox")
	if fs.Parse(args[1:]) != nil || fs.NArg() != 0 || *root == "" || *id == "" {
		return 2
	}
	st, e := store.New(*root)
	if e != nil {
		fmt.Fprintln(errOut, "store:", e)
		return 4
	}
	if args[0] == "create" {
		if *label == "" || *domains == "" {
			return 2
		}
		if e = st.Create(store.Identity{ProfileID: *id, Browser: *browser, AccountLabel: *label, AllowedDomains: strings.Split(*domains, ",")}); e != nil {
			fmt.Fprintln(errOut, "create:", e)
			return 4
		}
	}
	got, e := st.Get(*id)
	if e != nil {
		fmt.Fprintln(errOut, "show:", e)
		return 4
	}
	_ = json.NewEncoder(out).Encode(got)
	return 0
}
