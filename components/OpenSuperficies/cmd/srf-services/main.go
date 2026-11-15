// srf-services exposes AI queries and request submission, never operator writes.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"regexp"

	"opensuperficies.local/srf/src/browserid/services"
)

type fileCatalog map[string]bool

func (c fileCatalog) HasService(s string) bool { return c[s] }
func (c fileCatalog) Services() []string {
	a := []string{}
	for s := range c {
		a = append(a, s)
	}
	return a
}
func emit(w io.Writer, v any, rc int) int {
	if json.NewEncoder(w).Encode(v) != nil {
		return 5
	}
	return rc
}
func failure(w io.Writer, e error, rc int) int {
	return emit(w, map[string]string{"error": e.Error()}, rc)
}
func loadCatalog(path string) (fileCatalog, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	var names []string
	data, e := io.ReadAll(io.LimitReader(f, 1024*1024+1))
	if e != nil {
		return nil, e
	}
	if len(data) > 1024*1024 {
		return nil, services.ErrInvalid
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	if e = dec.Decode(&names); e != nil {
		return nil, e
	}
	if dec.Decode(new(any)) != io.EOF || names == nil {
		return nil, services.ErrInvalid
	}
	c := fileCatalog{}
	valid := regexp.MustCompile(`^[a-z][a-z0-9_-]{1,31}$`)
	for _, s := range names {
		if !valid.MatchString(s) || c[s] {
			return nil, services.ErrInvalid
		}
		c[s] = true
	}
	return c, nil
}
func run(args []string, out io.Writer) int {
	flags := flag.NewFlagSet("srf-services", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	dir := flags.String("dir", "", "caller-owned assignment directory")
	cat := flags.String("catalog", "", "operator-owned service-name JSON array")
	if e := flags.Parse(args); e != nil {
		return failure(out, e, 2)
	}
	a := flags.Args()
	if len(a) == 0 {
		return failure(out, services.ErrInvalid, 2)
	}
	required := map[string]int{"where": 2, "status": 2, "request": 5, "pending": 1}
	n, ok := required[a[0]]
	if !ok || len(a) != n || *dir == "" || *cat == "" {
		return failure(out, services.ErrInvalid, 2)
	}
	catalog, e := loadCatalog(*cat)
	if e != nil {
		return failure(out, e, 5)
	}
	registry, e := services.Open(*dir, catalog)
	if e != nil {
		return failure(out, e, 5)
	}
	var result any
	switch a[0] {
	case "where":
		result, e = registry.WhereCanOpen(a[1])
	case "status":
		result, e = registry.Status(a[1])
	case "request":
		result, e = registry.RequestAccess(a[1], a[2], a[3], a[4])
	case "pending":
		result, e = registry.ListPending()
	}
	if e != nil {
		rc := 5
		switch {
		case errors.Is(e, services.ErrInvalid):
			rc = 2
		case errors.Is(e, services.ErrUnauthorized):
			rc = 3
		case errors.Is(e, services.ErrUnknownService):
			rc = 4
		case errors.Is(e, services.ErrPendingLimit), errors.Is(e, services.ErrNotPending):
			rc = 6
		}
		return failure(out, e, rc)
	}
	return emit(out, result, 0)
}
func main() { os.Exit(run(os.Args[1:], os.Stdout)) }
